"""共振因子（resonance）—— 把用户的「多指标共振」框架编码成**日频**因子。

框架原意（用户文档）::
    Boll 决定方向 → 4H K 确认趋势 → MACD 确认重启 → MTM 确认加速
    → WR/MR 确认超卖修复 → 30min K 回踩入场。

现实约束
========
我们**只有日频面板**（1451 天 × 655 标的，无 4H/30min）。因此本模块只落地
框架中**可日频计算的内核**：
    (1) 趋势方向：收盘 > Boll 中轨，且中轨向上；
    (2) 动能重启：MACD 在零轴上方金叉 / 红柱放大；
    (3) 动能加速：MTM 拐头向上（今日 > 昨日，且上穿自身均线）；
    (4) 超卖修复：Williams %R 从 -80 下方上穿 -80。
四项合成一个 0..8 的共振分（每项可给 0/1/2 分）。分数越高=共振越强=越看多。

防过拟合原则
============
- **尽量用相对量**（"上穿自身均线"、"红柱放大"）而非绝对阈值；
- 唯一绝对阈值是 WR 的 -80（经典超卖线，行业惯例，非本策略拟合出来的）；
- 参数（窗口）取社区常用值（Boll=20、MACD=12/26/9、WR=14、MTM=10），
  **不做参数搜索** → 从源头杜绝「为历史最优调参」。
- 合成分数后走标准横截面 rank，不学权重。

输出：因子值越大越看多（正向信号）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 基础指标（全部只用 <= t 的数据，严格无前视）
# ---------------------------------------------------------------------------
def _boll_mid(c: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    return c.rolling(n).mean()


def _macd(c: pd.DataFrame, fast: int = 12, slow: int = 26, sig: int = 9):
    """返回 (dif, dea, hist)。hist = (dif - dea)，红柱>0。"""
    ema_f = c.ewm(span=fast, adjust=False).mean()
    ema_s = c.ewm(span=slow, adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=sig, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def _williams_r(panel: dict, n: int = 14) -> pd.DataFrame:
    """Williams %R ∈ [-100, 0]。<-80 超卖，>-20 超买。"""
    h = panel["h"].rolling(n).max()
    l = panel["l"].rolling(n).min()
    c = panel["c"]
    rng = (h - l).replace(0, np.nan)
    return -100.0 * (h - c) / rng


def _mtm(c: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """动量：收盘 - n 日前收盘。"""
    return c - c.shift(n)


# ---------------------------------------------------------------------------
# 四项共振分（各 0..2 分）
# ---------------------------------------------------------------------------
def _score_trend(panel: dict, n: int = 20) -> pd.DataFrame:
    """① 趋势：收盘>Boll中轨(+1)；中轨向上(+1)。"""
    c = panel["c"]
    mid = _boll_mid(c, n)
    above = (c > mid).astype(float)
    rising = (mid > mid.shift(1)).astype(float)
    return above + rising


def _score_macd(panel: dict) -> pd.DataFrame:
    """② 动能重启：DIF>本质零轴上方(+1)；红柱放大或金叉(+1)。

    金叉 = 今日 hist>0 且 昨日 hist<=0；红柱放大 = hist>0 且 hist>hist.shift(1)。
    """
    c = panel["c"]
    dif, dea, hist = _macd(c)
    above_zero = (dif > 0).astype(float)
    golden = ((hist > 0) & (hist.shift(1) <= 0)).astype(float)
    hist_up = ((hist > 0) & (hist > hist.shift(1))).astype(float)
    # 金叉或红柱放大 任一成立即得 1 分
    momentum = np.maximum(golden, hist_up)
    return above_zero + momentum


def _score_mtm(panel: dict, n: int = 10, ma: int = 10) -> pd.DataFrame:
    """③ 动能加速：MTM 拐头向上(+1)；MTM 上穿自身均线(+1)。"""
    m = _mtm(panel["c"], n)
    turn_up = (m > m.shift(1)).astype(float)
    cross_ma = ((m > m.rolling(ma).mean()) & (m.shift(1) <= m.rolling(ma).mean().shift(1))).astype(float)
    # 退而求其次：已站上自身均线也给 1 分
    above_ma = (m > m.rolling(ma).mean()).astype(float)
    return turn_up + np.maximum(cross_ma, above_ma)


def _score_wr(panel: dict, n: int = 14, oversold: float = -80.0) -> pd.DataFrame:
    """④ 超卖修复：WR 上穿 -80(+2)；仅在 -80 附近有向上拐头(+1)。

    这是框架里**唯一保留的绝对阈值**（-80 是经典超卖线，非拟合所得）。
    """
    wr = _williams_r(panel, n)
    cross_up = ((wr > oversold) & (wr.shift(1) <= oversold)).astype(float)
    # 拐头：仍在超卖区但开始向上
    near_os = (wr.shift(1) <= oversold) | (wr <= oversold + 5)
    turning = (near_os & (wr > wr.shift(1))).astype(float)
    return 2.0 * cross_up + np.maximum(0.0, turning - cross_up)


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------
def resonance_score(panel: dict) -> pd.DataFrame:
    """共振总分 [date x symbol]，范围 0..8。越大越看多。"""
    s = (_score_trend(panel) + _score_macd(panel)
         + _score_mtm(panel) + _score_wr(panel))
    return s


def resonance_factor(panel: dict, direction: int = 1,
                     require_trend: bool = False) -> pd.DataFrame:
    """共振因子。

    require_trend=True 时，只在「趋势向上（收盘>中轨 且 中轨向上）」的标的上
    保留分数，其余置 NaN —— 对应框架「Boll 决定做多还是观望」的第一性原则：
    不做逆势共振。**但注意：这会把可选标的压得很少**（可交易名称本就不多），
    因此默认 False —— 让趋势判断仅作为打分的一部分（见 _score_trend），
    而不是硬性筛选，保证横截面有足够宽度供 quantile 选股。
    输出值越大越看多；direction=-1 取负。
    """
    score = resonance_score(panel)
    c = panel["c"]
    if require_trend:
        mid = _boll_mid(c, 20)
        trend_ok = (c > mid) & (mid > mid.shift(1))
        score = score.where(trend_ok)
    # 把 0 分（完全无共振）视为"无信号"置 NaN，避免噪声股被 rank 选中
    score = score.where(score > 0)
    return score * float(direction)


RESONANCE_OPS = {
    "resonance": lambda panel, **kw: resonance_factor(panel, **{k: v for k, v in kw.items()
                                                                 if k in ("direction", "require_trend")}),
    "resonance_score": lambda panel, **kw: resonance_score(panel),
}
