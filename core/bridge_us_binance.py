"""美股动量策略 → 币安 Demo 永续下单桥（最后一块）。

链路：
  正规市场长历史(hist) 算因子
    → 生成目标持仓权重（多空，只在开市日）
    → 映射到币安永续 symbol（us_mapping）
    → 与当前持仓 diff，生成调仓指令
    → 通过 binance_exec 下单（Demo）
    → 记录成交、对账滑点与资金费率

两种运行模式：
  --mode signal   只算信号、打印目标持仓与调仓指令（不发单，零依赖 key）
  --mode execute  实际发单到币安（Demo 或主网，由 --testnet 控制）

安全默认：
  * 默认 --testnet（Demo）。主网需显式 --mainnet。
  * 单标的名义上限 --max-notional，防止把 Demo 余额一次性打光。
  * 杠杆默认 1（不加杠杆），由 --leverage 调整。
"""
from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

from core import data
from factors.engine import factor_series_to_frame
from core.us_mapping import build_mapping, Mapping
from data.us_meta import sec_sector_map

PROJ = Path(__file__).resolve().parent.parent
STATE = PROJ / "data" / "binance" / "demo_positions.json"

# 最近一次 compute_targets 算出的资金费率净成本估算（记账用，不改仓位）。
# 结构见 _funding_view()：{net_ann_pct, per_hold_pct, hold_days, legs_...}
LAST_FUNDING_VIEW: dict | None = None

# 交易所真实下单门槛（minNotional）。这是【交易所规则】，不是我定的参数。
# 优先从 exchangeInfo 动态读取；失败才退回一个保守默认。
_MIN_NOTIONAL_CACHE: dict = {}


def _min_notional(testnet: bool = True) -> float:
    """从币安 exchangeInfo 读真实 minNotional（带缓存）。这是唯一真实的物理约束。"""
    key = bool(testnet)
    if key in _MIN_NOTIONAL_CACHE:
        return _MIN_NOTIONAL_CACHE[key]
    try:
        import requests
        from core import binance_us
        base = binance_us.FAPI_DEMO if testnet else binance_us.FAPI_MAIN
        r = requests.get(f"{base}/fapi/v1/exchangeInfo", timeout=20)
        # 只统计【美股永续】(TRADIFI_PERPETUAL) 的门槛，忽略股指/加密合约
        vals = []
        for s in r.json().get("symbols", []):
            ct = s.get("contractType", "")
            if ct and ct != "TRADIFI_PERPETUAL":
                continue
            for f in s.get("filters", []):
                if f.get("filterType") == "MIN_NOTIONAL":
                    v = f.get("notional")
                    if v:
                        vals.append(float(v))
        # 用【最常见值】而非最大值（不同合约门槛不同，取 max 会虚高）
        if vals:
            from collections import Counter
            mn = Counter(vals).most_common(1)[0][0]
        else:
            mn = 5.0  # 未知时用最常见的 5
    except Exception:  # noqa: BLE001
        mn = 5.0
    _MIN_NOTIONAL_CACHE[key] = mn
    return mn



# ---------------------------------------------------------------------------
# 目标持仓生成
# ---------------------------------------------------------------------------
@dataclass
class Target:
    ticker: str
    binance_symbol: str
    weight: float          # 目标权重（多头正、空头负）
    factor_value: float


def compute_targets(spec: dict, as_of: str, quantile: float = 0.2,
                    gross_exposure: float = 1.0,
                    testnet: bool = True, neutralize: str = "none",
                    max_legs: int = 0, equity: float | None = None,
                    top_per_side: int = 0, hold: int = 20) -> list[Target]:
    """在 as_of 日，用因子在「目标环境可交易」的标的中选多空两端，给出目标权重。

    只使用 as_of 及之前的数据（严格无前视）。
    testnet=True 时只保留 demo 环境存在的标的（否则下单会 -1121）。
    neutralize:
      - "none": 全市场按因子取头部/尾部（原行为）
      - "sector": 每个行业内取头部/尾部，再跨行业等权汇总
                  （研究证明能把 mom250 最差期从 -17% 砍到 -8%）
    """
    panel = data.build_panel(market="us")
    f = factor_series_to_frame(panel, spec)
    c = panel["c"]

    t = pd.Timestamp(as_of)
    # 严格：只用 <= as_of 的最后一天因子值
    valid = f.index[f.index <= t]
    if not len(valid):
        raise ValueError(f"{as_of} 之前无因子数据")
    t_use = valid[-1]
    fv = f.loc[t_use].dropna()

    # 按目标环境实时构建映射（demo 与主网标的集合不同）
    mapped, _ = build_mapping(fetch_live=True, testnet=testnet)
    # ★ 剔除"僵尸标的"：exchangeInfo 里有符号但无报价（如 Demo 的 BMNRUSDT）
    from core.us_mapping import live_quote_symbols
    live = live_quote_symbols(testnet=testnet)
    if live:
        mapped = [m for m in mapped if m.binance_symbol in live]
    sym_by_ticker = {m.us_ticker: m for m in mapped}
    # 只保留可映射的
    fv = fv[[x for x in fv.index if x in sym_by_ticker]]
    if len(fv) < 10:
        raise ValueError(f"可映射标的太少: {len(fv)}")

    targets: list[Target] = []
    if neutralize == "sector":
        sm = sec_sector_map()
        groups: dict[str, list] = {}
        for tk in fv.index:
            groups.setdefault(sm.get(tk, "Other"), []).append(tk)
        # 自适应最小分组：理想是每行业 >=5，但 demo 可交易标的小时放宽到 >=2；
        # 若放宽后仍无有效分组，则退回普通选股（避免 0 目标）。
        for min_size in (5, 4, 3, 2):
            valid_groups = {g: tks for g, tks in groups.items() if len(tks) >= min_size}
            if valid_groups:
                break
        else:
            valid_groups = {}
        # ★ 小样本保护：若有效分组覆盖的标的过少（<60%），sector 中性会把仓位
        #   极度集中到少数 ticker 上（实测 Demo 池只选出 2 只 → 单腿名义爆表）。
        #   此时退回全市场等分，宁可放弃行业中性也不要集中持仓。
        covered = sum(len(tks) for tks in valid_groups.values()) if valid_groups else 0
        if not valid_groups or (len(fv) and covered / len(fv) < 0.6) or covered < 10:
            neutralize = "none"  # fall through to plain selection below
        else:
            for g, tks in valid_groups.items():
                sub = fv[tks]
                k = max(1, int(len(sub) * quantile))
                o = sub.sort_values(ascending=False)
                ls, ss = list(o.index[:k]), list(o.index[-k:])
                for tk in ls:
                    targets.append(Target(tk, sym_by_ticker[tk].binance_symbol, +0.5, float(fv[tk])))
                for tk in ss:
                    targets.append(Target(tk, sym_by_ticker[tk].binance_symbol, -0.5, float(fv[tk])))
            # 归一化：总敞口 = gross_exposure
            tot_w = sum(abs(t.weight) for t in targets)
            scale = gross_exposure / tot_w if tot_w else 0.0
            for tgt in targets:
                tgt.weight *= scale
            return _apply_funding_view(targets, t_use, hold)

    # ── 全信号驱动选股：唯一的硬约束是【总额 ≤ equity】+【单笔 ≥ 交易所 minNotional】
    #   其余全是自由的：
    #     * 几只？—— 不预设。信号强的标的自然入选，弱的不入。
    #     * 每只多少？—— ∝ 信号强度（排名），不是均分，不是固定值。
    #     * 多空？—— 因子正做多、因子负做空，不对称、单边都允许。
    #     * 总敞口？—— 由整体信号强度决定，不强求满仓。
    #   没有任何"每腿至少 $X""最多 N 腿"的人造参数。
    mu, sd = float(fv.mean()), float(fv.std() or 0)
    if sd < 1e-9:
        return _apply_funding_view([], t_use, hold)
    z = {tk: (float(fv[tk]) - mu) / sd for tk in fv.index}

    # 入选：多头=因子为正、空头=因子为负（不做人为"前 N 只"筛选）。
    #   下多少只、每只多少，全部交给下方【贪心预算分配】决定：
    #   从最强的开始，只要分给它的金额 ≥ 交易所门槛就下，直到预算花完。
    order = sorted(fv.index, key=lambda tk: -float(fv[tk]))
    longs_all = [tk for tk in order if float(fv[tk]) > 0]
    shorts_all = [tk for tk in reversed(order) if float(fv[tk]) < 0]
    if not longs_all and not shorts_all:
        return _apply_funding_view([], t_use, hold)

    # 份额按信号强度分配（见下方 _score），不再用排名倒数摊薄。

    # 总敞口由信号强度决定（不强求满仓）
    def _strength(tks: list) -> float:
        if not tks:
            return 0.0
        avg = sum(abs(z[tk]) for tk in tks) / len(tks)
        return min(1.0, max(0.35, avg / 2.0))

    s_long, s_short = _strength(longs_all), _strength(shorts_all)
    strength_both = max(s_long, s_short)
    n_sides = (1 if longs_all else 0) + (1 if shorts_all else 0)
    # ── 份额由【信号强度】决定，不按名次摊薄 ──
    # 2026-10-24 改（用户口径）：
    #   "信号弱就少投"是对的，但**不许按腿平均分**。每只票投多少只该由它自己的
    #   信号强度决定——信号最强的那只占满 100% 账户也合理。
    #   原实现用 1/(i+1) 排名倒数 → 第1名 1、第2名 0.5、第3名 0.33…，
    #   这是典型的"分腿均摊"，最强票被后面的弱票稀释，$100 只能落到 $29.6。
    #   现在改为：份额 ∝ 该票的强度（z 值超出阈值部分），强票自然吃掉大头。
    def _score(tks: list, sign: int) -> dict:
        """份额 = 该方向的信号强度（与方向一致的 z 值）。

        2026-10-24 修：原来用 abs(z)，负号被吃掉 →
        TSLA(z=-1.24)、QQQ(-1.14) 这类本该做空的票被算成多头买入。
        现在按方向取号：多头要 z>0，空头取 -z（即 z<0 的绝对值）。
        """
        if not tks:
            return {}
        out = {}
        for tk in tks:
            v = float(z[tk]) * sign          # 多头=+z，空头=-z
            if v > 0:
                out[tk] = v
        return out

    score_long = _score(longs_all, +1)
    score_short = _score(shorts_all, -1)

    # 强度仍决定「这一侧要不要出手 / 出多少手」的总体节奏：
    #   整体信号弱 → gross_frac 小 → 少投；信号强 → 接近满仓。
    #   注意：这是**整体**折扣，不是按腿摊薄。单只票依然可以吃掉全部预算。
    def _strength(tks: list) -> float:
        if not tks:
            return 0.0
        avg = sum(abs(z[tk]) for tk in tks) / len(tks)
        return min(1.0, max(0.35, avg / 2.0))

    s_long, s_short = _strength(longs_all), _strength(shorts_all)
    strength_both = max(s_long, s_short)
    n_sides = (1 if longs_all else 0) + (1 if shorts_all else 0)
    # 用户铁律（2026-10-24 原话）："我不允许你有分腿，每一腿一定要投多少……
    # 你投多少不是根据信号强弱来的吗？信号强你投 100，占 100% 都没关系。"
    # → 份额只由强度决定，不做按腿摊薄，也不做整体强度折扣。
    # → 总值 $100 是硬上限，绝不放大（budget = equity，gross_exposure 只允许 ≤1）。
    gross_frac = 1.0
    budget = equity * min(1.0, gross_exposure) * gross_frac


    # ── 用真实价格把"预算"分配成"整数手"：从最强的开始，把每腿预算分配到
    #    至少能下一手（≥minNotional），分不完的预算不再硬塞 → 自然决定腿数。 ──
    min_notional = _min_notional(testnet)  # 交易所真实门槛
    px = {}
    try:
        px = {tk: float(c.iloc[-1].get(tk, 0) or 0) for tk in fv.index}
    except Exception:  # noqa: BLE001
        px = {}

    def _allocate(scores: dict, sign: int) -> list[tuple]:
        """按【信号强度】分配：每腿金额 ∝ 它自己的强度。

        用户口径（2026-10-24）：不许按腿平均、不许按名次摊薄。
        每只票投多少只由它自己的信号强度决定 —— 最强的那只可以吃掉全部预算。
          * 该腿应得 = 该侧预算 × (自身强度 / 该侧强度总和)
          * 应得金额 < 交易所门槛 → 跳过（信号太弱，不值得占一腿）
          * 被跳过的额度回流给剩下的强票，最终由最强票吸收
        """
        if not scores:
            return []
        side_budget = budget / max(1, n_sides)
        live = {tk: s for tk, s in scores.items() if px.get(tk, 0) > 0}
        picked: list[tuple] = []
        remaining = side_budget
        while live and remaining >= min_notional:
            tot = sum(live.values()) or 1.0
            # 本轮按强度切分当前剩余预算
            alloc = {tk: remaining * (s / tot) for tk, s in live.items()}
            # 够门槛的按份额拿下；不够门槛的淘汰，额度回流下一轮
            good = {tk: a for tk, a in alloc.items() if a >= min_notional}
            if not good:
                # 没有一个够门槛 → 剩下的钱全给最强的那个（它信号最强，配得上）
                top = max(live, key=lambda tk: live[tk])
                if remaining >= min_notional:
                    picked.append((top, sign, remaining, px[top]))
                    remaining = 0.0
                break
            for tk, a in good.items():
                picked.append((tk, sign, a, px[tk]))
                remaining -= a
            live = {tk: s for tk, s in live.items() if tk not in good}
        return picked

    picks = _allocate(score_long, +1) + _allocate(score_short, -1)
    if not picks:
        return _apply_funding_view([], t_use, hold)

    for tk, sign, alloc, p in picks:
        # 权重 = 分配金额 / equity。**不再除以 gross_exposure**：
        # budget 已经用 min(1.0, gross_exposure) 封顶，此处再除一次会把敞口缩小
        # （实测 gross_exposure=2.0 时算出 $50）。用户要求总值严格等于 equity。
        targets.append(Target(tk, sym_by_ticker[tk].binance_symbol,
                              sign * (alloc / equity),
                              float(fv[tk])))
    return _apply_funding_view(targets, t_use, hold)


# 持仓资金费率净成本（年化）。币安美股永续每 8h 结一次，多头付、空头收。
# 实测（2026-10-23，30 个可映射标的、quantile=0.2、hold=20）：中性情形两腿
# 合计年化净付出 7.67%，最坏 17.8%。而每期（20 交易日）约 0.61% ——— 对 $100
# 本金、单笔中位 $6.8 的账户来说，这已经是决定性的量级；回测里却完全没扣。
# 这里只做**记账**，不改目标仓位：仓位口径一旦变动，回测与实盘就对不上了。
def _funding_view(targets: list[Target], t_use, hold: int = 20) -> dict:
    """按当前多空腿估算本组合的资金费率净成本（年化 + 每期）。算不出就返回 None。"""
    if not targets:
        return None
    try:
        from core import funding_cost as fc
        from core.us_mapping import build_mapping
        mapped, _ = build_mapping()
        by_ticker = {m.binance_symbol: m.us_ticker for m in mapped}
        stats = {s.symbol: s for s in fc.all_funding_stats()}
        long_r, short_r, covered, missing = [], [], 0, []
        for t in targets:
            st = stats.get(t.binance_symbol)
            if st is None:
                missing.append(by_ticker.get(t.binance_symbol) or t.binance_symbol)
                continue
            covered += 1
            if t.weight > 0:
                long_r.append(st.ann_long)          # 多头付
            elif t.weight < 0:
                short_r.append(-st.ann_short)       # 空头收 → 等价成本 -ann_short
        if not covered:
            return None
        # 缺费率数据的腿按「两腿均值」保守补齐，避免低估成本
        all_known = list(stats.values())
        avg = (sum(s.ann_long for s in all_known) / len(all_known)) if all_known else 0.0
        while len(long_r) + len(short_r) < len(targets):
            long_r.append(avg)
        lm = sum(long_r) / len(long_r) if long_r else 0.0
        sm = sum(short_r) / len(short_r) if short_r else 0.0
        net_ann = lm + sm
        return {
            "net_ann_pct": round(net_ann * 100, 2),
            "per_hold_pct": round(net_ann * hold / 252 * 100, 3),
            "hold_days": hold,
            "legs_covered": covered,
            "legs_total": len(targets),
            "missing_symbols": missing[:5],
            "as_of": str(getattr(t_use, "date", lambda: t_use)()),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:120]}


def _apply_funding_view(targets: list[Target], t_use, hold: int = 20):
    """把资金费率估算挂到模块级，供调用方记账（不改目标权重）。"""
    global LAST_FUNDING_VIEW
    LAST_FUNDING_VIEW = _funding_view(targets, t_use, hold=hold)
    return targets, str(t_use.date())


# ---------------------------------------------------------------------------
# 调仓指令生成
# ---------------------------------------------------------------------------
@dataclass
class Order:
    binance_symbol: str
    side: str              # BUY / SELL
    target_weight: float
    target_qty: float      # 目标数量（正=多，负=空）
    cur_qty: float         # 当前数量
    delta_qty: float       # 需要交易的数量（正=买，负=卖）
    ref_price: float
    notional: float
    action: str            # OPEN / CLOSE / INCREASE / REDUCE / FLIP


def load_positions() -> dict[str, float]:
    """从本地状态文件读持仓（离线用）。实盘/模拟盘调仓请用 load_positions_from_exchange。"""
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def load_positions_from_exchange(testnet: bool = True,
                                 book: str | None = None) -> dict[str, float]:
    """从交易所读【真实持仓】。这是调仓的正确依据。

    为什么必须这样做：本地状态文件会与实际持仓脱节（换环境、手工平仓、
    之前会话遗留等）。用它算 delta 会导致重复开仓/漏平。

    book: 指定账本 → 读该盘所属币安账户的持仓（赛马隔离下各盘账户不同）。
    返回 {binance_symbol: signed_qty}，只含非零仓位。
    """
    from core.binance_exec import BinanceExec
    try:
        ex = BinanceExec.from_env(testnet=testnet, book=book)
        out: dict[str, float] = {}
        for p in ex.positions():
            amt = float(p.get("positionAmt", 0) or 0)
            if abs(amt) > 1e-9:
                out[p["symbol"]] = amt
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 读取交易所持仓失败，回退本地状态: {e}")
        return load_positions()



def save_positions(pos: dict[str, float]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(pos, indent=1))


def force_derisk(targets: list[Target], equity: float, prices: dict[str, float],
                 cur_positions: dict[str, float], drawdown_pct: float,
                 gross_cap: float = 2.0, derisk_floor: float = 0.0,
                 min_notional: float = 5.0, step: float = 0.01) -> list[Order]:
    """回撤熔断下的**主动降险**：把目标敞口按回撤比例强制压缩，只产出减仓单。

    与仅"拦截加仓"的被动做法不同，这里主动把 gross 压到
    ``target_gross = min(gross_cap × equity, 当前 gross) × (1 - drawdown_pct)``
    以下，并逐腿向该上限缩减（减少 abs(target_qty)，绝不增加任何腿；
    也不新开方向相反的腿 —— 那会引入新风险）。

    为什么需要它：无人值守时若 LLM 说 HOLD，被动闸门就等于没有止损。
    主动降险保证「浮亏越深，敞口越薄」，把回撤风险真正关掉。

    2026-11-01 两处修复（此前熔断器实际是摆设）：
      1. 目标上限加了 ``min(..., cur_gross)``。原来只用 gross_cap×equity，
         低仓位账本（gross 0.57x）下目标 107U 永远高于实际 56.6U，
         判定「敞口已够低」直接返回空单，一腿都减不掉。
      2. 真正使用 ``min_notional``。低仓位时按比例微调每腿只有 0.3~1.7U，
         全部低于交易所最小名义额 → 发出去必被拒。现在先跳过这些腿，
         再**整腿平掉**最小的几条腿来补足降险量（仍然只减不加、不翻向）。

    derisk_floor：压缩后单腿目标占总敞口的比例下限（0=可一路压到平）。
    """
    orders: list[Order] = []
    if equity <= 0 or drawdown_pct <= 0:
        return orders

    # 当前总敞口（按有报价的腿计）—— 必须先算出来，目标上限要用
    cur_gross = 0.0
    for sym, q in cur_positions.items():
        px = prices.get(sym)
        if px and px > 0:
            cur_gross += abs(float(q) * px)

    # 允许的目标总敞口：回撤越深，压得越狠；上限不超过仓位上限，
    # 也不超过当前敞口（否则低仓位账本永远不会触发减仓）。
    dd = min(max(drawdown_pct, 0.0), 0.95)
    target_gross = min(gross_cap * equity, cur_gross) * (1.0 - dd)

    if cur_gross <= target_gross + 1e-9:
        return orders  # 敞口已足够低，无需主动降险

    need_reduce = cur_gross - target_gross
    scale = target_gross / cur_gross   # < 1.0

    def snap(x: float) -> float:
        return math.copysign(
            math.floor(abs(x) / step) * step if step > 0 else abs(x), x) if x else 0.0

    picked: dict[str, Order] = {}
    planned = 0.0                     # 已规划缩减的名义额

    for sym, cur in sorted(cur_positions.items()):
        px = prices.get(sym)
        if not px or px <= 0 or abs(cur) < 1e-9:
            continue
        # 目标数量：仅按比例缩小当前腿，方向不变（绝不翻向）
        new_abs = abs(cur) * scale
        if derisk_floor > 0:
            floor_abs = (derisk_floor * target_gross) / px
            new_abs = max(new_abs, min(floor_abs, abs(cur)))
        tq_s = snap(math.copysign(new_abs, cur))
        delta_s = snap(tq_s - cur)     # 必然与 cur 反向（减仓），除非不足一手
        if abs(delta_s) < (step / 2 if step else 1e-9):
            continue                   # 缩减量不足一手，跳过
        notional = abs(delta_s * px)
        if notional < min_notional:
            continue                   # 低于交易所最小名义额，发出必被拒 → 交给兜底
        picked[sym] = Order(
            binance_symbol=sym, side="BUY" if delta_s > 0 else "SELL",
            target_weight=0.0, target_qty=tq_s, cur_qty=cur, delta_qty=delta_s,
            ref_price=px, notional=notional,
            action="CLOSE" if abs(tq_s) < 1e-9 else "REDUCE",
        )
        planned += notional

    # 兜底：整腿平掉最小的几条腿。低仓位账本上按比例微调往往每一腿都
    # < min_notional（实测 7 腿 0.31~1.71U，全被拒），此时只有整腿平仓
    # 才真正降得下来。约束：平完后剩余 gross 不低于 target 的 50%，
    # 避免一次砍太狠；仍然只减不加、方向不翻。
    if planned < need_reduce - 1e-9:
        cands: list[tuple[float, str, float, float]] = []
        for sym, cur in cur_positions.items():
            px = prices.get(sym)
            if not px or px <= 0 or abs(cur) < 1e-9 or sym in picked:
                continue
            n = abs(cur * px)
            if n >= min_notional:
                cands.append((n, sym, cur, px))
        cands.sort(key=lambda t: t[0])
        floor_gross = target_gross * 0.5
        remaining = cur_gross
        for n, sym, cur, px in cands:
            if planned >= need_reduce - 1e-9:
                break
            if remaining - n < floor_gross:
                break
            dq = snap(-cur)
            if abs(dq) < 1e-9:
                continue
            picked[sym] = Order(
                binance_symbol=sym, side="BUY" if dq > 0 else "SELL",
                target_weight=0.0, target_qty=0.0, cur_qty=cur, delta_qty=dq,
                ref_price=px, notional=abs(dq * px), action="CLOSE",
            )
            planned += abs(dq * px)
            remaining -= abs(cur * px)

    orders = list(picked.values())
    prio = {"CLOSE": 0, "REDUCE": 1}
    orders.sort(key=lambda o: prio.get(o.action, 9))
    return orders


def build_orders(targets: list[Target], equity: float, prices: dict[str, float],
                 cur_positions: dict[str, float],
                 min_notional: float = 5.0, step: float = 0.01,
                 deadband_pct: float = 0.05,
                 deadband_minnotional_mult: float = 2.0,
                 steps: dict[str, float] | None = None,
                 min_qtys: dict[str, float] | None = None) -> list[Order]:
    """按目标权重和当前持仓，生成 delta 调仓指令。

    deadband_pct / deadband_minnotional_mult：再平衡死区。已有持仓的「微调」
    （本次成交名义 < max(minNotional × mult, pct × equity)）会被当作噪声跳过，
    防止追着每 tick 小幅抖动的目标来回打。开仓/全平不受限。

    steps / min_qtys：**每个标的自己的**交易所精度（stepSize / minQty）。
    2026-10-20 修复：原先用全局默认 `step=0.01` 吸附所有标的，而 SPCXUSDT
    这类标的 stepSize=1、minQty=1（整股合约）。于是目标 0.05 张被当成「合法」，
    发单后 `BinanceExec.snap_qty` 又把它抬到 minQty=1 —— **实发量是计划的 20 倍**，
    一次 $7 的建仓变成 $148 的仓位。多出来的部分持仓账本记不上，delta 永远对不平，
    进而反手开空、卡死风控。现在目标仓位在**下单前**就按真实 step 吸附，
    低于 minQty 的碎仓直接跳过（不生成订单）。
    """
    steps = steps or {}
    min_qtys = min_qtys or {}
    orders: list[Order] = []
    all_syms = {t.binance_symbol for t in targets} | set(cur_positions)
    target_by_sym = {t.binance_symbol: t for t in targets}

    for sym in sorted(all_syms):
        px = prices.get(sym)
        if not px or px <= 0:
            continue
        tg = target_by_sym.get(sym)
        tw = tg.weight if tg else 0.0
        t_qty = (tw * equity) / px  # 目标数量（带符号）
        cur = float(cur_positions.get(sym, 0.0))
        delta = t_qty - cur

        # 该标的自己的精度（缺省回落到全局默认）
        step = float(steps.get(sym) or step or 0.0)
        min_q = float(min_qtys.get(sym) or 0.0)

        # 吸附到 step
        def snap(x: float) -> float:
            if not x:
                return 0.0
            if step <= 0:
                return x
            # 2026-10-24 修复：直接 floor(abs(x)/step) 会被浮点误差咬掉一整步。
            # 例：持仓被算成 0.009999999999999998，/0.01 = 0.9999999999999998，
            # floor → 0 → 平仓单变成 0 张，残留腿永远清不掉（实测 GOOGLUSDT）。
            # 先按 step 精度四舍五入到「多少个 step」，再 floor，避免这类误差。
            n_steps = round(abs(x) / step, 6)
            return math.copysign(math.floor(n_steps + 1e-9) * step, x)

        delta_s = snap(delta)
        tq_s = snap(t_qty)

        # 是否"完全平掉该仓位"（目标权重为 0 且当前有仓）
        is_full_close = (tg is None or tw == 0.0) and abs(cur) > 1e-9
        # 目标极小且无持仓 → 跳过（噪声）
        if abs(delta_s) < (step / 2 if step else 1e-9) and abs(cur) < 1e-9:
            continue
        # 碎仓保护：吸附后目标不足交易所 minQty，且不是平仓 → 不建/不加这一腿。
        # （否则发单时会被 minQty 抬量，实发量远超计划 —— SPCXUSDT 事故根因。）
        if not is_full_close and min_q > 0 and 0 < abs(tq_s) < min_q:
            continue
        # 增量名义额过小 → 跳过（避免 0.00 的无意义单），但平仓单必须放行
        if not is_full_close and abs(delta_s * px) < min_notional:
            continue
        # 再平衡死区：已有持仓的「微调」是噪声（目标每 tick 小幅抖动），
        # 照做会来回打、白交手续费。开仓/全平不受限。口径与 llm_order 校验器一致。
        if not is_full_close and abs(cur) > 1e-9:
            deadband_usd = max(min_notional * deadband_minnotional_mult,
                               deadband_pct * equity)
            if abs(delta_s * px) < deadband_usd - 1e-9:
                continue

        side = "BUY" if delta_s > 0 else "SELL"
        # 分类动作
        if cur == 0:
            act = "OPEN"
        elif tg is None or tw == 0.0:
            act = "CLOSE"
        elif np.sign(t_qty) != np.sign(cur):
            act = "FLIP"
        elif abs(t_qty) > abs(cur):
            act = "INCREASE"
        else:
            act = "REDUCE"

        orders.append(Order(
            binance_symbol=sym, side=side, target_weight=tw, target_qty=tq_s,
            cur_qty=cur, delta_qty=delta_s, ref_price=px,
            notional=abs(delta_s * px), action=act,
        ))
    # 先平后开：CLOSE/REDUCE 优先
    prio = {"CLOSE": 0, "REDUCE": 1, "FLIP": 2, "INCREASE": 3, "OPEN": 4}
    orders.sort(key=lambda o: prio.get(o.action, 9))
    return orders


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def execute_orders(orders: list[Order], testnet: bool = True, leverage: int = 1,
                   max_notional: float = 500.0, dry: bool = True,
                   position_side: str | None = None,
                   book: str | None = None) -> list[dict]:
    """发单。dry=True 只打印。返回成交记录。

    position_side: 双向持仓模式（Hedge Mode）下用来隔离策略；
      单向模式下传 None 即可（币安会用 BOTH）。
    book: 账本名 → 决定用哪个币安账户（见 binance_exec._BOOK_ACCOUNT）。
      赛马隔离的核心：每个盘在自己的账户上交易，物理隔离持仓。
    """
    results: list[dict] = []
    exec_client = None
    if not dry:
        from core.binance_exec import BinanceExec
        exec_client = BinanceExec.from_env(testnet=testnet, book=book)
        exec_client.ping()

    for o in orders:
        if o.notional > max_notional:
            results.append({"symbol": o.binance_symbol, "skipped":
                            f"名义 {o.notional:.1f} > 上限 {max_notional}"})
            continue
        rec = {
            "symbol": o.binance_symbol, "side": o.side, "action": o.action,
            "qty": abs(o.delta_qty), "ref_price": o.ref_price, "notional": o.notional,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if dry or o.notional < 5.0:
            # 两种「没发出去」要分开记：dry 是运行模式，notional<5 是交易所最小名义额
            # 卡死的粉尘单。混成同一个 "dry-run" 会让人以为守护进程没加 --live。
            # 2026-10-30 实测：GOOGL 0.01 股（≈2.8U）平仓单连续 53/60 个 tick 卡在这里。
            rec["status"] = "dry-run" if dry else "dust_skip"
            if not dry:
                rec["note"] = f"名义 {o.notional:.2f} < 最小下单额 5.0，未发单"
            results.append(rec)
            continue
        try:
            if exec_client:
                exec_client.set_leverage(o.binance_symbol, leverage)
            r = exec_client.place_order(
                o.binance_symbol, o.side, "MARKET",
                quantity=abs(o.delta_qty),
                position_side=position_side) if exec_client else {}
            rec["status"] = "sent"
            rec["position_side"] = position_side
            rec["resp"] = {k: r.get(k) for k in ("orderId", "status", "avgPrice", "executedQty")}
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["error"] = str(e)
        results.append(rec)

    # 真实发单时，把成交写入台账，供 demo_reconcile 对账
    if not dry and exec_client is not None:
        _write_ledger(results, book)
    return results


def _write_ledger(results: list[dict], book: str | None = None) -> None:
    """写成交台账。

    必须按账本分开写：赛马隔离后两盘各用自己的账户，
    台账混在同一个文件里会让对账/统计把 A 盘的成交算进 B 盘。
    """
    try:
        from core.demo_reconcile import append_ledger, set_book
        if book:
            set_book(book)
        sent = [r for r in results if r.get("status") == "sent"]
        if sent:
            append_ledger(sent)
    except Exception as e:  # noqa: BLE001
        print(f"  台账写入失败（不影响下单）: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="美股动量 → 币安永续 下单桥")
    ap.add_argument("--factor", default="mom250")
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="信号日（默认用最新可用交易日）")
    ap.add_argument("--quantile", type=float, default=0.2)
    ap.add_argument("--equity", type=float, default=1000.0, help="资金（USDT）")
    ap.add_argument("--mode", choices=["signal", "execute"], default="signal")
    ap.add_argument("--neutralize", choices=["none", "sector"], default="none",
                    help="sector=按 SEC SIC 行业中性选股（降低板块集中尾部风险）")
    ap.add_argument("--testnet", action="store_true", default=True)
    ap.add_argument("--mainnet", dest="testnet", action="store_false")
    ap.add_argument("--leverage", type=int, default=1)
    ap.add_argument("--max-notional", type=float, default=500.0)
    ap.add_argument("--save", default=str(PROJ / "runs" / "bridge_orders.json"))
    args = ap.parse_args()

    factors = {
        "mom250": {"op": "mom", "params": {"n": 250}, "direction": 1},
        "mom120": {"op": "mom", "params": {"n": 120}, "direction": 1},
        "trend120": {"op": "trend_strength", "params": {"n": 120}, "direction": 1},
    }
    spec = factors[args.factor]

    as_of = args.as_of or str(datetime.now(timezone.utc).date())
    targets, used_date = compute_targets(spec, as_of, args.quantile, testnet=args.testnet,
                                         neutralize=args.neutralize)
    print(f"=== 目标持仓（因子={args.factor}, 信号日={used_date}, equity={args.equity}, "
          f"中性化={args.neutralize}）===")
    longs = [t for t in targets if t.weight > 0]
    shorts = [t for t in targets if t.weight < 0]
    print(f"  多头 {len(longs)}: {', '.join(t.ticker for t in longs)}")
    print(f"  空头 {len(shorts)}: {', '.join(t.ticker for t in shorts)}")

    # 拉当前价（公开，无需 key）
    from core.binance_exec import BinanceExec
    c = BinanceExec("x", "x", testnet=args.testnet)
    prices = {}
    for t in targets:
        try:
            tk = c.ticker(t.binance_symbol)
            lp = tk.get("lastPrice")
            if lp is None:
                raise ValueError(f"无 lastPrice: {list(tk)[:5]}")
            prices[t.binance_symbol] = float(lp)
        except Exception as e:  # noqa: BLE001
            print(f"  价格获取失败 {t.binance_symbol}: {e}")

    cur = load_positions_from_exchange(testnet=args.testnet)
    orders = build_orders(targets, args.equity, prices, cur)

    print(f"\n=== 调仓指令（{len(orders)} 条）===")
    print(f"{'symbol':14s} {'action':9s} {'side':5s} {'delta_qty':>12s} {'ref_px':>10s} {'notional':>10s}")
    print("-" * 68)
    for o in orders:
        print(f"{o.binance_symbol:14s} {o.action:9s} {o.side:5s} "
              f"{o.delta_qty:>+12.4f} {o.ref_price:>10.2f} {o.notional:>10.2f}")

    dry = args.mode == "signal"
    results = execute_orders(orders, testnet=args.testnet, leverage=args.leverage,
                             max_notional=args.max_notional, dry=dry)
    print(f"\n=== 执行结果（{'DRY-RUN' if dry else '实发'}）===")
    for r in results:
        print(f"  {r['symbol']:14s} {r.get('action',''):9s} {r.get('status','')} "
              f"{r.get('notional',0):>8.2f}  {r.get('error','')}")

    Path(args.save).write_text(json.dumps(
        {"as_of": used_date, "factor": args.factor, "equity": args.equity,
         "targets": [t.__dict__ for t in targets],
         "orders": [o.__dict__ for o in orders], "results": results}, indent=1, default=str))
    print(f"\n已存 {args.save}")

    if dry:
        print("\n[DRY-RUN] 未发任何订单。加 --mode execute 且配好 Demo key 后才会真实下单。")


if __name__ == "__main__":
    main()
