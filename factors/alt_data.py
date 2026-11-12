#!/usr/bin/env python3
"""另类数据因子族：FINRA 空头量 + Wikipedia 关注度（纯程序化、无前视）。

数据源（均免费、官方、无需 key，来源与限制见 README）：
  - data/alt/finra_short.parquet  ← data/finra.py  (Reg SHO 日频空头量)
  - data/alt/wiki_pageviews.parquet ← data/wikipedia.py (日频页面浏览量)

无前视规则（与 factors/edgar_event.py 一致）：两类数据都是**当日收盘后**
才完整可见，故事件生效日 = 该日期之后的第一个交易日；面板开始前的数据
一律丢弃（否则会被 searchsorted 压到首日形成脉冲）。

覆盖度不足的标的设 NaN 而非 0 —— 恒零值会污染截面分位数分层。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJ = Path("/opt/quant-lab")
_CACHE: dict = {}


def _frame(panel: dict):
    idx = pd.DatetimeIndex(panel["c"].index)
    cols = pd.Index([str(c).upper() for c in panel["c"].columns])
    return idx, cols


def _effective(dates: pd.DatetimeIndex, raw: pd.Series) -> tuple[pd.Series, pd.Series]:
    """返回 (有效日期, 是否落在面板内)；生效日严格晚于原始日期。"""
    d = pd.to_datetime(raw)
    keep = d >= dates[0]
    d = d[keep]
    pos = np.searchsorted(dates.values, d.values, side="right")
    pos = np.clip(pos, 0, len(dates) - 1)
    return pd.Series(dates[pos], index=d.index), keep


def _load_finra(panel: dict) -> pd.DataFrame:
    if "finra" in _CACHE:
        return _CACHE["finra"]
    f = PROJ / "data" / "alt" / "finra_short.parquet"
    idx, cols = _frame(panel)
    empty = pd.DataFrame(np.nan, index=idx, columns=cols)
    if not f.exists():
        _CACHE["finra"] = empty
        return empty
    d = pd.read_parquet(f)
    eff, keep = _effective(idx, pd.to_datetime(d["Date"].astype(str)))
    d = d.loc[keep.values].copy()
    d["eff"] = eff.values
    d["ticker"] = d["Symbol"].astype(str).str.upper()
    den = d["TotalVolume"].replace(0, np.nan)
    d["ratio"] = d["ShortVolume"] / den
    d["exempt"] = d["ShortExemptVolume"] / den
    out = {}
    for key in ("ratio", "exempt"):
        m = d.pivot_table(index="eff", columns="ticker", values=key,
                          aggfunc="mean").reindex(index=idx, columns=cols)
        # 年度归档按 ingestion day 落盘，观测稀疏（321 天/5 年）。
        # 状态量允许前向填充到下一次观测，上限 10 个交易日，避免把
        # 半年前的陈旧值一直带下去；完全无数据的标的仍由 _finish 设 NaN。
        out[key] = m.astype(float).ffill(limit=10)
    _CACHE["finra"] = out
    return out


def _load_wiki(panel: dict) -> pd.DataFrame:
    if "wiki" in _CACHE:
        return _CACHE["wiki"]
    f = PROJ / "data" / "alt" / "wiki_pageviews.parquet"
    idx, cols = _frame(panel)
    empty = pd.DataFrame(np.nan, index=idx, columns=cols)
    if not f.exists():
        _CACHE["wiki"] = empty
        return empty
    d = pd.read_parquet(f)
    eff, keep = _effective(idx, pd.to_datetime(d["date"].astype(str)))
    d = d.loc[keep.values].copy()
    d["eff"] = eff.values
    d["ticker"] = d["ticker"].astype(str).str.upper()
    m = d.pivot_table(index="eff", columns="ticker", values="views",
                      aggfunc="sum").reindex(index=idx, columns=cols)
    _CACHE["wiki"] = np.log1p(m.astype(float))
    return _CACHE["wiki"]


def _finish(m: pd.DataFrame) -> pd.DataFrame:
    """把完全无数据的标的整列设为 NaN。"""
    m = m.copy()
    covered = m.notna().any(axis=0)
    m.loc[:, ~covered] = np.nan
    return m


def _roll(m: pd.DataFrame, n: int) -> pd.DataFrame:
    return _finish(m.rolling(int(n), min_periods=1).mean())


# ── FINRA 三个算子 ──────────────────────────────────────────────────
def finra_short_ratio(panel, n: int = 60):
    d = _load_finra(panel)
    return d if not isinstance(d, dict) else _roll(d["ratio"], n)


def finra_short_surge(panel, n: int = 60):
    d = _load_finra(panel)
    if not isinstance(d, dict):
        return d
    fast = d["ratio"].rolling(5, min_periods=1).mean()
    slow = d["ratio"].rolling(int(n), min_periods=1).mean()
    return _finish(fast / slow.replace(0, np.nan) - 1.0)


def finra_short_exempt(panel, n: int = 60):
    d = _load_finra(panel)
    return d if not isinstance(d, dict) else _roll(d["exempt"], n)


# ── Wikipedia 三个算子 ──────────────────────────────────────────────
def wiki_attention(panel, n: int = 60):
    """近 n 日 log 浏览量的相对水平（减去自身 60 日均值）。"""
    m = _load_wiki(panel)
    return _finish(m - m.rolling(int(n), min_periods=1).mean())


def wiki_attention_chg(panel, n: int = 60):
    m = _load_wiki(panel)
    fast = m.rolling(5, min_periods=1).mean()
    slow = m.rolling(int(n), min_periods=1).mean()
    return _finish(fast - slow)


def wiki_attention_vol(panel, n: int = 60):
    m = _load_wiki(panel)
    return _finish(m.rolling(int(n), min_periods=1).std())


ALT_OPS = {
    "finra_short_ratio": finra_short_ratio,
    "finra_short_surge": finra_short_surge,
    "finra_short_exempt": finra_short_exempt,
    "wiki_attention": wiki_attention,
    "wiki_attention_chg": wiki_attention_chg,
    "wiki_attention_vol": wiki_attention_vol,
}
