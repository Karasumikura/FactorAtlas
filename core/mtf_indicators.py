"""多周期指标库 —— 为「Boll+MACD+MTM+WR 共振策略」提供计算层。

策略原文（用户文档）要求的四类指标 + 周期分工：
    Boll : 趋势方向与价格位置（中轨斜率、价格相对中轨、带宽）
    MACD : 中期趋势与转折（水上金叉、红柱放大）
    MTM  : 动能是否加速（拐头向上、上穿自身均线）
    WR/MR: 情绪是否由超卖区修复（-80 下方拐头向上、重新上穿）

本模块只做**纯计算**，不含任何交易决策 —— 决策在 strategies/resonance_system.py。
所有函数严格只用 <= t 的数据（无前视），输入为 OHLCV DataFrame，输出为同索引序列。

参数全部取社区标准值（Boll=20、MACD=12/26/9、WR=14、MTM=10），**不做参数搜索**。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Boll：趋势方向与价格位置
# ---------------------------------------------------------------------------
def boll_mid(c: pd.Series, n: int = 20) -> pd.Series:
    """布林中轨（n 期均线）。"""
    return c.rolling(n).mean()


def boll_bands(c: pd.Series, n: int = 20, k: float = 2.0):
    """返回 (mid, upper, lower)。"""
    mid = c.rolling(n).mean()
    sd = c.rolling(n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def boll_slope_up(c: pd.Series, n: int = 20, lookback: int = 1) -> pd.Series:
    """中轨向上：当前中轨 > lookback 根前的中轨。"""
    mid = boll_mid(c, n)
    return mid > mid.shift(lookback)


def boll_bandwidth(c: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    """带宽 = (上轨 - 下轨) / 中轨。用于判断收窄/张开。"""
    mid, up, lo = boll_bands(c, n, k)
    return (up - lo) / mid.replace(0, np.nan)


def boll_band_expanding(c: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    """带宽张开：当前带宽 > 前一根。"""
    bw = boll_bandwidth(c, n, k)
    return bw > bw.shift(1)


def price_position(c: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    """价格在通道内的位置 ∈ 大致 [-1, 1]：0=中轨, +1=上轨, -1=下轨。"""
    mid, up, lo = boll_bands(c, n, k)
    half = (up - mid).replace(0, np.nan)
    return (c - mid) / half


def above_mid(c: pd.Series, n: int = 20) -> pd.Series:
    """收盘价在中轨上方。"""
    return c > boll_mid(c, n)


# ---------------------------------------------------------------------------
# MACD：中期趋势与转折
# ---------------------------------------------------------------------------
def macd(c: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    """返回 (dif, dea, hist)。hist = dif - dea（红柱>0）。"""
    ema_f = c.ewm(span=fast, adjust=False).mean()
    ema_s = c.ewm(span=slow, adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=sig, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def macd_golden_cross_above_zero(c: pd.Series) -> pd.Series:
    """水上金叉：DIF、DEA 都在零轴上方，且 DIF 上穿 DEA。"""
    dif, dea, _ = macd(c)
    above = (dif > 0) & (dea > 0)
    cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    return above & cross


def macd_hist_expanding(c: pd.Series) -> pd.Series:
    """红柱放大：hist>0 且 hist > 前一根。"""
    _, _, hist = macd(c)
    return (hist > 0) & (hist > hist.shift(1))


def macd_dead_cross(c: pd.Series) -> pd.Series:
    """死叉：DIF 下穿 DEA。"""
    dif, dea, _ = macd(c)
    return (dif < dea) & (dif.shift(1) >= dea.shift(1))


def macd_dif_above_zero(c: pd.Series) -> pd.Series:
    dif, _, _ = macd(c)
    return dif > 0


# ---------------------------------------------------------------------------
# MTM：动能是否加速
# ---------------------------------------------------------------------------
def mtm(c: pd.Series, n: int = 10) -> pd.Series:
    """动量：收盘 - n 根前收盘。"""
    return c - c.shift(n)


def mtm_turning_up(c: pd.Series, n: int = 10) -> pd.Series:
    """MTM 拐头向上：当前 > 前一根。"""
    m = mtm(c, n)
    return m > m.shift(1)


def mtm_low_rising(c: pd.Series, n: int = 10, lookback: int = 3) -> pd.Series:
    """MTM 低点抬高：当前 MTM 高于 lookback 根前的 MTM。"""
    m = mtm(c, n)
    return m > m.shift(lookback)


def mtm_cross_above_ma(c: pd.Series, n: int = 10, ma: int = 10) -> pd.Series:
    """MTM 上穿自身均线。"""
    m = mtm(c, n)
    mma = m.rolling(ma).mean()
    return (m > mma) & (m.shift(1) <= mma.shift(1))


def mtm_above_ma(c: pd.Series, n: int = 10, ma: int = 10) -> pd.Series:
    m = mtm(c, n)
    return m > m.rolling(ma).mean()


def momentum_divergence(c: pd.Series, n: int = 10, lookback: int = 5) -> pd.Series:
    """动能背离（警示）：价格创新高但 MTM 未同步创高。"""
    m = mtm(c, n)
    price_hh = c >= c.rolling(lookback).max()
    mtm_hh = m >= m.rolling(lookback).max()
    return price_hh & (~mtm_hh)


# ---------------------------------------------------------------------------
# WR / Williams %R：情绪是否由超卖修复
# ---------------------------------------------------------------------------
def williams_r(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    """Williams %R ∈ [-100, 0]。<-80 偏超卖，>-20 偏超买。"""
    hh = h.rolling(n).max()
    ll = l.rolling(n).min()
    rng = (hh - ll).replace(0, np.nan)
    return -100.0 * (hh - c) / rng


def wr_oversold(h, l, c, n: int = 14, level: float = -80.0) -> pd.Series:
    """处于超卖区（WR <= -80）。"""
    return williams_r(h, l, c, n) <= level


def wr_recovering(h, l, c, n: int = 14, level: float = -80.0) -> pd.Series:
    """超卖修复：前一根还在 -80 下方，当前上穿 -80。

    对应原文「先进入 -80 以下 → 拐头向上 → 重新上穿」。"""
    wr = williams_r(h, l, c, n)
    return (wr > level) & (wr.shift(1) <= level)


def wr_turning_up(h, l, c, n: int = 14) -> pd.Series:
    """WR 拐头向上（无论是否已上穿）。"""
    wr = williams_r(h, l, c, n)
    return wr > wr.shift(1)


def wr_overbought(h, l, c, n: int = 14, level: float = -20.0) -> pd.Series:
    return williams_r(h, l, c, n) >= level


# ---------------------------------------------------------------------------
# 结构：回调低点 / 突破阳线低点（用于止损位）
# ---------------------------------------------------------------------------
def swing_low(l: pd.Series, lookback: int = 10) -> pd.Series:
    """最近 lookback 根内的最低点（回调低点）。"""
    return l.rolling(lookback).min()


def higher_lows(l: pd.Series, lookback: int = 10) -> pd.Series:
    """低点逐步抬高：当前滚动低点 > 上一个滚动低点。"""
    sl = swing_low(l, lookback)
    return sl > sl.shift(lookback)
