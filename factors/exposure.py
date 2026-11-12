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


def _looks_crypto(panel: dict) -> bool:
    """按列名识别 crypto 面板（BTCUSDT/ETHUSDT...）。

    美股是 Alpaca 纯代码（AAPL/INTC），A 股是 000001.SZ，都不会以 USDT 结尾。
    """
    cols = list(panel.get("c", pd.DataFrame()).columns)
    if not cols:
        return False
    n_usdt = sum(str(c).upper().endswith("USDT") for c in cols)
    return n_usdt >= 0.8 * len(cols)


def tradable_mask(panel: dict, min_dollar_vol: float = 1e6,
                  min_price: float = 5.0) -> pd.DataFrame:
    """可交易掩码：成交额和价格下限，剔除仙股和流动性差的。

    注意：用当日可见信息，无前视。

    2026-11-01：``min_price=5`` 是给美股「仙股」定的，币价没有这个语义——
    DOGE($0.08)/ADA($0.21)/XRP($1.28) 都是主流币，却被这条门槛整段剔除，
    crypto 掩码每天只剩 7 只（上限 13），分层回测永远凑不够宽度。
    这里只对 crypto 面板把价格下限降到 0.01，成交额门槛保持不变。
    """
    if _looks_crypto(panel):
        min_price = min(min_price, 0.01)
    dv = panel["c"] * panel["v"]
    price_ok = panel["c"] >= min_price
    liq_ok = dv >= min_dollar_vol
    vol_ok = panel["v"] > 0
    return price_ok & liq_ok & vol_ok
