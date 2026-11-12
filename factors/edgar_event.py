#!/usr/bin/env python3
"""SEC EDGAR 申报事件因子族（纯程序化、无前视、不涉 LLM）。

数据源：data/edgar/filings.parquet（92,271 份 8-K/10-Q/10-K，615 ticker，
2001-2026，带精确 filing_date）。此前这份数据只有 data/edgar.py 的
event_dates() 和一个归档的事件研究脚本在用，**没有任何因子算子消费它**。

为什么不用 filing 正文：www.sec.gov/Archives 从本机 IP 403，且 92k 篇正文
拉取成本远高于收益。这里只用元数据（form / filing_date / report_date），
密度足够（不像 news 库 234 行/78 标的那样撑不起截面因子）。

无前视规则：申报日收盘后才公开 → 事件生效日对齐到 filing_date 之后的
**第一个交易日**（next-trading-day alignment）。t 日的因子值只含
effective_date <= t 的申报，预测 t+1 起的收益。

每个算子返回 [date x symbol] DataFrame，索引/列与 panel["c"] 对齐。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from data.edgar import load_filings

_CACHE: dict = {}
AMEND_FORMS = {"8-K/A", "10-K/A", "10-Q/A"}
EARNINGS_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}


def _events(panel: dict) -> pd.DataFrame:
    """读 filings，加 effective_date（申报后第一个交易日）与派生列。"""
    if "ev" in _CACHE:
        return _CACHE["ev"]
    df = load_filings().copy()
    df["filing_date"] = pd.to_datetime(df["filing_date"])
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    dates = pd.DatetimeIndex(panel["c"].index)
    # ⚠️ 面板开始前的申报必须丢弃：searchsorted 会把它们全部压到首日，
    # 形成 6 万条事件的初期脉冲，污染 rolling(60) 窗口整整一年
    # （实测 2021 均值 15.9 vs 2022 起 3.3，纯属对齐 bug 不是数据）。
    df = df[df["filing_date"] >= dates[0]].copy()
    # searchsorted('right')：filing 当天若非交易日，落到下一个交易日；
    # 若恰是交易日则仍取下一个交易日（当日收盘后才可见）。
    pos = np.searchsorted(dates.values, df["filing_date"].values, side="right")
    pos = np.clip(pos, 0, len(dates) - 1)
    df["effective_date"] = dates[pos]
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["is_amend"] = df["form"].isin(AMEND_FORMS)
    df["is_8k"] = df["form"].isin({"8-K", "8-K/A"})
    df["is_earnings"] = df["form"].isin(EARNINGS_FORMS)
    df["lag_days"] = (df["filing_date"] - df["report_date"]).dt.days
    _CACHE["ev"] = df
    return df


def _frame(panel: dict) -> tuple[pd.DatetimeIndex, pd.Index]:
    idx = pd.DatetimeIndex(panel["c"].index)
    cols = pd.Index([str(c).upper() for c in panel["c"].columns])
    return idx, cols


def _daily_matrix(ev: pd.DataFrame, idx, cols, value: str,
                  how: str = "sum") -> pd.DataFrame:
    """把事件表按 (effective_date, ticker) 透视成日频矩阵并对齐 panel。"""
    sub = ev[ev["ticker"].isin(cols)]
    if value == "count":
        s = sub.groupby(["effective_date", "ticker"]).size()
    else:
        s = sub.groupby(["effective_date", "ticker"])[value].agg(how)
    m = s.unstack("ticker").reindex(index=idx, columns=cols).fillna(0.0)
    return m.astype(float)


def _rolling(m: pd.DataFrame, n: int) -> pd.DataFrame:
    return m.rolling(int(n), min_periods=1).sum()


def _finish(m: pd.DataFrame, panel: dict) -> pd.DataFrame:
    """覆盖度掩码：615/655 有 SEC 申报，其余 40 个恒零会污染截面分位数。"""
    idx, cols = _frame(panel)
    covered = set(_events(panel)["ticker"].unique())
    uncovered = [c for c in cols if c not in covered]
    if uncovered:
        m = m.copy()
        m.loc[:, uncovered] = np.nan
    return m


# ── 六个算子 ────────────────────────────────────────────────────────
def edgar_filing_count(panel, n: int = 60):
    """近 n 日申报总条数：信息/披露密度。"""
    idx, cols = _frame(panel)
    return _finish(_rolling(_daily_matrix(_events(panel), idx, cols, "count"), n), panel)


def edgar_8k_count(panel, n: int = 60):
    """近 n 日 8-K 条数：重大事件密度。"""
    idx, cols = _frame(panel)
    ev = _events(panel)
    return _finish(_rolling(_daily_matrix(ev[ev["is_8k"]], idx, cols, "count"), n), panel)


def edgar_amend_share(panel, n: int = 60):
    """近 n 日修订件(A 表)占比：重述/更正信号。"""
    idx, cols = _frame(panel)
    ev = _events(panel)
    all_m = _daily_matrix(ev, idx, cols, "count")
    am_m = _daily_matrix(ev[ev["is_amend"]], idx, cols, "count")
    num, den = _rolling(am_m, n), _rolling(all_m, n)
    return _finish((num / den.replace(0, np.nan)).fillna(0.0), panel)


def edgar_report_lag(panel, n: int = 60):
    """近 n 日申报的平均披露滞后（filing - report 天数）：越慢越差。"""
    idx, cols = _frame(panel)
    ev = _events(panel)
    lag = _daily_matrix(ev[ev["lag_days"].notna() &
                           (ev["lag_days"] >= 0) & (ev["lag_days"] < 400)],
                        idx, cols, "lag_days", how="mean")
    # 事件日为 0 表示无申报；用 ffill 让最新一次滞后持续可见，再滚动均值
    lag = lag.replace(0.0, np.nan).ffill().fillna(0.0)
    return _finish(lag.rolling(int(n), min_periods=1).mean(), panel)


def edgar_earnings_gap(panel, n: int = 120):
    """距上次财报申报的交易日数（PEAD 窗口位置），上限 n。"""
    idx, cols = _frame(panel)
    ev = _events(panel)
    flag = _daily_matrix(ev[ev["is_earnings"]], idx, cols, "count")
    hit = (flag > 0).to_numpy()
    pos = np.arange(len(idx), dtype=float)[:, None]
    # 每个标的：当前交易日序号 - 最近一次财报申报的交易日序号
    last = pd.DataFrame(np.where(hit > 0, pos, np.nan),
                        index=idx, columns=cols).ffill().fillna(-1.0)
    gap = pd.DataFrame(np.broadcast_to(pos, (len(idx), len(cols))).copy(),
                       index=idx, columns=cols) - last
    return _finish(gap.clip(upper=int(n)).astype(float), panel)


def edgar_form_entropy(panel, n: int = 60):
    """近 n 日申报 form 类型分布熵：披露结构复杂度。"""
    idx, cols = _frame(panel)
    ev = _events(panel)
    forms = sorted(ev["form"].astype(str).unique())
    mats = {}
    for f in forms:
        sub = ev[ev["form"] == f]
        mats[f] = _rolling(_daily_matrix(sub, idx, cols, "count"), n)
    total = sum(mats.values())
    ent = pd.DataFrame(0.0, index=idx, columns=cols)
    for m in mats.values():
        p = (m / total.replace(0, np.nan)).fillna(0.0)
        ent = ent - (p * np.log(p.replace(0, np.nan))).fillna(0.0)
    return _finish(ent.fillna(0.0).astype(float), panel)


EDGAR_OPS = {
    "edgar_filing_count": edgar_filing_count,
    "edgar_8k_count": edgar_8k_count,
    "edgar_amend_share": edgar_amend_share,
    "edgar_report_lag": edgar_report_lag,
    "edgar_earnings_gap": edgar_earnings_gap,
    "edgar_form_entropy": edgar_form_entropy,
}
