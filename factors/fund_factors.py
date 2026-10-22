"""基本面因子：用 FMP 年度 ratios/key-metrics 构造价值/质量因子。

注意：FMP key 只支持年度数据，所以这些因子变化慢（按年更新），
本质是「慢变量」，适合较长持有期。按财报日+45天滞后对齐，无前视。
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from data.fundamentals import build_fundamental_panel


def _reindex_to_panel(piv: pd.DataFrame, panel) -> pd.DataFrame:
    """把按可用日索引的基本面面板，前向填充对齐到交易日面板。"""
    if piv.empty:
        return pd.DataFrame(index=panel["c"].index, columns=panel["c"].columns, dtype=float)
    piv = piv.sort_index()
    # 合并日期轴
    all_dates = piv.index.union(panel["c"].index).sort_values()
    out = piv.reindex(all_dates).ffill()
    return out.reindex(index=panel["c"].index, columns=panel["c"].columns)


def value_earnings_yield(panel, n=None):
    """盈利收益率（E/P，越高越便宜）。"""
    p = build_fundamental_panel("priceToEarningsRatio")
    pe = _reindex_to_panel(p, panel)
    return (1.0 / pe).replace([np.inf, -np.inf], np.nan)


def value_book_to_price(panel, n=None):
    """账面市值比（B/P）。"""
    p = build_fundamental_panel("priceToBookRatio")
    pb = _reindex_to_panel(p, panel)
    return (1.0 / pb).replace([np.inf, -np.inf], np.nan)


def value_sales_yield(panel, n=None):
    """营收收益率（S/P）。"""
    p = build_fundamental_panel("priceToSalesRatio")
    ps = _reindex_to_panel(p, panel)
    return (1.0 / ps).replace([np.inf, -np.inf], np.nan)


def quality_roe(panel, n=None):
    """净资产收益率。"""
    return _reindex_to_panel(build_fundamental_panel("returnOnEquity"), panel)


def quality_margin(panel, n=None):
    """净利率。"""
    return _reindex_to_panel(build_fundamental_panel("netProfitMargin"), panel)


def quality_roic(panel, n=None):
    """投入资本回报率。"""
    return _reindex_to_panel(build_fundamental_panel("returnOnInvestedCapital"), panel)


def fcf_yield(panel, n=None):
    """自由现金流收益率。"""
    return _reindex_to_panel(build_fundamental_panel("freeCashFlowYield"), panel)


def low_leverage(panel, n=None):
    """低杠杆（负的负债权益比，越高越看多）。"""
    de = _reindex_to_panel(build_fundamental_panel("debtToEquityRatio"), panel)
    return -de


FUND_OPS = {
    "value_earnings_yield": value_earnings_yield,
    "value_book_to_price": value_book_to_price,
    "value_sales_yield": value_sales_yield,
    "quality_roe": quality_roe,
    "quality_margin": quality_margin,
    "quality_roic": quality_roic,
    "fcf_yield": fcf_yield,
    "low_leverage": low_leverage,
}
