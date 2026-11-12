"""多周期共振策略 —— 把用户的原始策略文档翻译成可执行的状态机。

策略原文核心（一句话）：
    Boll 决定做多还是观望，4 小时 K 确认趋势，MACD 确认趋势重新启动，
    MTM 确认动能加速，WR/MR 确认超卖修复，再用 30 分钟 K 的回踩结构控制入场成本。

本模块**只做决策**，不碰下单/账本 —— 执行由 strategies/resonance_runner.py 完成。

分层结构（严格按原文）：
  ① 环境层（Boll，4H）  ：中轨向上 + 价格在中轨上方 + 带宽张开    → 决定「做多 or 观望」
  ② 确认层（4H）        ：4H 收盘站上中轨 + MACD 水上金叉 + MTM 向上 + WR 超卖修复
  ③ 入场层（30m）       ：30m 回踩不破中轨 + 再启（阳线/红柱放大）→ 决定「入场时机」
  ④ 风控层             ：结构止损（4H 收盘跌破中轨 / 跌破突破阳线低点）

做多检查表（原文第八节，8 项 + 分级）：
  1. 日K/4H 趋势向上
  2. Boll 中轨向上
  3. 4H 收盘站上中轨
  4. MACD 零轴上方金叉
  5. MACD 红柱开始放大
  6. MTM 拐头向上或上穿均线
  7. WR 从 -80 以下向上突破
  8. 30m 回踩确认
  分级：≥8 强共振；6~7 普通信号（小仓试单）；≤5 等待。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from core import mtf_indicators as I


# 分级阈值（原文：8项以上=强共振，6~7=普通信号，5以下=等待）
STRONG_SCORE = 8
# NORMAL_SCORE 6 -> 5（2026-10-20 调整）
#   原因：8 项检查里 macd_golden_above_zero(零轴上方金叉) 与 wr_recover(WR 超卖修复)
#   是「事件型」条件 —— 回测最近 100 根 4H，前者成立率仅 0~3%，后者 5~9%。
#   两项要同时成立概率约 0.1%，导致实测可达分数上限被压在 6 分、恰好卡在门槛线上，
#   策略连续 151 轮 silent。按用户决定：只降门槛，不改指标定义（保持原策略语义）。
NORMAL_SCORE = 5

# 最小止损距离（2026-11-07 实测标定）
#   本地 4H K 回放 15 标的 / 7305 样本：ATR(14)/close
#   P25=0.658%  P50=1.126%  P75=1.850%  P90=2.500%
# 原实现把 stop_price 取成「当根 forming 30m 阳线低点」与 4H swing low 的较大者，
# 实测 7 个持仓里 5 个止损距入场 <0.1%、2 个恰好 0.000% —— 噪音级，任何回撤都被扫出。
# 现在强制至少 1 倍 ATR 中位数，避免把噪音当破位。
MIN_STOP_ATR_PCT = 0.011


@dataclass
class Signal:
    """单个标的的共振评估结果。"""
    symbol: str
    score: int                                  # 0..8 检查表通过项数
    grade: str                                  # "strong" / "normal" / "wait"
    direction_ok: bool                          # ① 环境层：Boll 判多
    checks: dict[str, bool] = field(default_factory=dict)
    entry_trigger: bool = False                 # ③ 30m 入场触发（回踩确认/激进）
    entry_kind: str = ""                        # "aggressive" / "pullback" / ""
    stop_price: float | None = None             # ④ 结构止损位
    ref_price: float | None = None
    notes: list[str] = field(default_factory=list)


def _closed_4h(df4h: pd.DataFrame) -> pd.DataFrame:
    """只保留已收盘的 4H K：最后一根若仍在 forming，丢掉。

    判据是时间戳而不是「无条件丢最后一根」—— 回测/单测喂进来的往往全是
    已收盘 K，无条件丢会改变指标口径，让既有测试失效。
    """
    if len(df4h) < 2:
        return df4h
    try:
        end = pd.Timestamp(df4h.index[-1]) + pd.Timedelta(hours=4)
        if end.tzinfo is None:
            end = end.tz_localize("UTC")
        if pd.Timestamp.now(tz="UTC") < end:
            return df4h.iloc[:-1]
    except Exception:
        pass
    return df4h


def evaluate(symbol: str, df4h: pd.DataFrame, df30m: pd.DataFrame,
             use_pullback_confirmation: bool = True) -> Signal:
    """对单个标的做完整共振评估。

    df4h : 4 小时 OHLCV（落盘的最后一根通常是未收盘的 forming K）
    df30m: 30 分钟 OHLCV，最后一根为当前 K（找入场）
    use_pullback_confirmation: True=稳健（等回踩确认）；False=激进（信号即入）

    2026-11-07 修复：4H 定方向只用**已收盘**K。实测三个标的的最后一根 4H
    均为 forming，其 close 每 30 分钟就变，导致方向层与出场判定抖动。
    历史回放 1500 个切片：原版出场触发率 56.2%，其中 55.3% 来自「4H 收盘
    跌破中轨」；而触发后下一根 4H 收益均值 -0.009%、为负占比仅 48%
    —— 与抛硬币无异，是纯噪音换手。
    """
    d4 = _closed_4h(df4h)
    c4, h4, l4 = d4["close"], d4["high"], d4["low"]
    c30, h30, l30 = df30m["close"], df30m["high"], df30m["low"]

    checks: dict[str, bool] = {}
    notes: list[str] = []

    # ---- ① 环境层（Boll，4H）：决定做多 or 观望 ----
    mid_up = bool(I.boll_slope_up(c4).iloc[-1])
    above_mid_now = bool(I.above_mid(c4).iloc[-1])
    band_expand = bool(I.boll_band_expanding(c4).iloc[-1])
    higher_lows = bool(I.higher_lows(l4).iloc[-1])
    # 偏多结构：中轨向上 且 当前在中轨上方（其余作为加分项）
    direction_ok = mid_up and above_mid_now

    # ---- ② 确认层（4H）----
    # 检查表 1：日K/4H 趋势向上（用中轨向上 + 高点/低点抬高近似）
    checks["trend_up"] = mid_up and higher_lows
    # 检查表 2：Boll 中轨向上
    checks["boll_mid_up"] = mid_up
    # 检查表 3：4H 收盘站上中轨（有效站上：前一根也在上方，避免盘中刺穿）
    checks["close_above_mid"] = above_mid_now and bool(I.above_mid(c4).shift(1).iloc[-1])
    # 检查表 4：MACD 零轴上方金叉
    checks["macd_golden_above_zero"] = bool(I.macd_golden_cross_above_zero(c4).iloc[-1])
    # 检查表 5：MACD 红柱放大
    checks["macd_hist_expanding"] = bool(I.macd_hist_expanding(c4).iloc[-1])
    # 检查表 6：MTM 拐头向上或上穿均线
    checks["mtm_up"] = bool(I.mtm_turning_up(c4).iloc[-1]) or bool(I.mtm_cross_above_ma(c4).iloc[-1]) \
        or bool(I.mtm_above_ma(c4).iloc[-1])
    # 检查表 7：WR 从 -80 以下向上突破（或正在超卖修复）
    wr_4h = I.williams_r(h4, l4, c4)
    checks["wr_recover"] = bool(I.wr_recovering(h4, l4, c4).iloc[-1]) \
        or bool(I.wr_turning_up(h4, l4, c4).iloc[-1] and wr_4h.iloc[-1] < -60)

    # 带宽张开作为环境加分（并入 trend/boll 逻辑，不单列）
    if band_expand:
        notes.append("带宽张开")

    # ---- ③ 入场层（30m）----
    # 30m 站上中轨 + 回踩不破 + 再启
    c30_above = bool(I.above_mid(c30).iloc[-1])
    m30_golden = bool(I.macd_golden_cross_above_zero(c30).iloc[-1])
    m30_hist_up = bool(I.macd_hist_expanding(c30).iloc[-1])
    mtm30_up = bool(I.mtm_turning_up(c30).iloc[-1])
    wr30_recover = bool(I.wr_recovering(h30, l30, c30).iloc[-1])
    # 30m 回踩不破中轨：最近一根的低点 > 中轨
    mid30 = I.boll_mid(c30)
    pullback_hold = bool(l30.iloc[-1] > mid30.iloc[-1]) and c30_above
    # 再启：阳线 或 MACD 红柱放大
    restart = bool(c30.iloc[-1] > c30.iloc[-2]) or m30_hist_up

    if use_pullback_confirmation:
        # 稳健：30m 回踩不破中轨 + 再启
        entry_trigger = pullback_hold and restart
        entry_kind = "pullback" if entry_trigger else ""
    else:
        # 激进：30m 站上中轨 + (MACD金叉 或 MTM向上) + WR修复
        entry_trigger = c30_above and (m30_golden or mtm30_up) and (wr30_recover or m30_hist_up)
        entry_kind = "aggressive" if entry_trigger else ""
    # 检查表 8：30m 回踩确认（计入评分）
    checks["m30_confirm"] = entry_trigger

    # ---- ④ 结构止损位 ----
    # 优先级：突破阳线低点 > 最近回调低点 > 4H 中轨
    recent_low_4h = float(I.swing_low(l4, lookback=10).iloc[-1])
    breakout_candle_low = None
    # 用「已收盘」的上一根 30m K：forming 根的 low 基本等于现价，不能当结构位
    if entry_trigger and len(df30m) >= 2:
        breakout_candle_low = float(l30.iloc[-2])
    structural = max([x for x in (breakout_candle_low, recent_low_4h)
                      if x is not None], default=None)
    ref_px = float(c30.iloc[-1])
    floor_stop = ref_px * (1 - MIN_STOP_ATR_PCT)
    stop_price = min(structural, floor_stop) if structural is not None else floor_stop

    # ---- 评分（8 项检查表）----
    score = sum(1 for v in checks.values() if v)
    if score >= STRONG_SCORE:
        grade = "strong"
    elif score >= NORMAL_SCORE:
        grade = "normal"
    else:
        grade = "wait"

    # 环境不成立 → 直接降级等待（原文：中轨向下，穿越中轨可能只是反弹）
    if not direction_ok:
        grade = "wait"
        notes.append("环境不成立(中轨向下/价格在中轨下)")

    # 背离警示
    if bool(I.momentum_divergence(c4).iloc[-1]):
        notes.append("4H动能背离")
        if grade == "strong":
            grade = "normal"

    return Signal(
        symbol=symbol, score=score, grade=grade, direction_ok=direction_ok,
        checks=checks, entry_trigger=entry_trigger, entry_kind=entry_kind,
        stop_price=stop_price, ref_price=float(c30.iloc[-1]), notes=notes,
    )


def should_exit(df4h: pd.DataFrame, entry_ref_low: float | None = None) -> tuple[bool, str]:
    """④ 出场判断：结构止损（价格结构优先于指标）。

    返回 (是否应出场, 原因)。
    规则（原文第五节）：
      - 4H 收盘跌回 Boll 中轨下方          → 结构止损
      - 跌破最近回调低点 / 突破阳线低点     → 结构止损
      - MACD 金叉失败并死叉 + MTM 转下      → 指标止损
    """
    # 只用已收盘 4H K；forming 根的 close 每 30 分钟都在变，拿它判「收盘跌破」
    # 等于把一个未完成的数字当成定论（配合 runner 的 4H bar 闸门使用）。
    x = _closed_4h(df4h)
    c4, h4, l4 = x["close"], x["high"], x["low"]
    mid4 = I.boll_mid(c4)
    last_close = float(c4.iloc[-1])
    # 结构止损：4H 收盘跌回中轨下方（原文第五节：结构止损优先）
    # 注意：不要求"上一根仍在中轨上方" —— 急跌破位时上一根可能已在下轨，
    # 那种情况恰恰最该止损，加该条件会漏掉最危险的下跌。
    if last_close < float(mid4.iloc[-1]):
        return True, "4H收盘跌破中轨"
    # 跌破突破阳线/回调低点
    if entry_ref_low is not None and last_close < entry_ref_low:
        return True, "跌破进场参考低点"
    # 指标止损：死叉 + MTM 转下
    if bool(I.macd_dead_cross(c4).iloc[-1]) and not bool(I.mtm_turning_up(c4).iloc[-1]):
        return True, "MACD死叉+MTM转下"
    return False, ""


def should_take_profit(df4h: pd.DataFrame) -> tuple[bool, str]:
    """⑥ 分批止盈条件（原文第六节）—— 命中任一即建议减仓。"""
    c4, h4, l4 = df4h["close"], df4h["high"], df4h["low"]
    mid4 = I.boll_mid(c4)
    _, _, hist = I.macd(c4)
    # 红柱持续缩短（连续 2 根下降）
    hist_down = (hist < hist.shift(1)) & (hist.shift(1) < hist.shift(2))
    if bool(hist_down.iloc[-1]) and bool(I.macd_dif_above_zero(c4).iloc[-1]):
        return True, "MACD红柱持续缩短"
    # 价格突破上轨后快速跌回带内
    _, up, _ = I.boll_bands(c4)
    if float(h4.iloc[-2]) > float(up.iloc[-2]) and float(c4.iloc[-1]) < float(mid4.iloc[-1]) * 1.0:
        if float(c4.iloc[-1]) < float(up.iloc[-1]):
            return True, "冲高上轨后跌回带内"
    return False, ""
