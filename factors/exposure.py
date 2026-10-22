"""横截面暴露：市值代理、流动性过滤。"""
from __future__ import annotations
import numpy as np
import pandas as pd


def dollar_volume(panel: dict, n: int = 20) -> pd.DataFrame:
    """成交额均值（流动性/市值代理）。"""
    dv = panel["c"] * panel["v"]
    return dv.rolling(n).mean()


def size_proxy(panel: dict, n: int = 20) -> pd.DataFrame:
    """市值代理：对数成交额。"""
    dv = dollar_volume(panel, n)
    return np.log(dv.replace(0, np.nan))


def tradable_mask(panel: dict, min_dollar_vol: float = 1e6,
                  min_price: float = 5.0) -> pd.DataFrame:
    """可交易掩码：成交额和价格下限，剔除仙股和流动性差的。

    注意：用当日可见信息，无前视。
    """
    dv = panel["c"] * panel["v"]
    price_ok = panel["c"] >= min_price
    liq_ok = dv >= min_dollar_vol
    vol_ok = panel["v"] > 0
    return price_ok & liq_ok & vol_ok
