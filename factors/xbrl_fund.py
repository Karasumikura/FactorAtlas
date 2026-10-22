"""XBRL 基本面因子：从 SEC XBRL 原始科目构造价值/质量/成长因子。

与 fund_factors.py（FMP 版）的区别：
  - 数据源：SEC XBRL companyfacts（免费、无配额、**精确 filed 公布日**）
  - 粒度：季度值（FMP 免费档只有年度）
  - 无前视：直接用 SEC 的 filed 日期（SEC 收到即公开），不用 +45 天近似

因子构造（TTM 口径，按 filed 日 point-in-time 可用）：
  价值： earnings_yield(E/P)、book_to_price(B/P)、sales_yield(S/P)、fcf_yield
  质量： roe、gross_margin、net_margin、asset_turnover
  成长： revenue_yoy、earnings_yoy
  规模： log_market_cap 代理（shares × price，用面板价）

实现要点：
  - 每个 (ticker, tag) 取季度序列（end-filed 间隔 ~90 天的 10-Q，或 10-K）
  - TTM = 最近 4 个季度之和（流量类）/ 最近一期（存量类）
  - 用 filed 日作为"可用日"，ffill 到交易日面板 → 严格无前视
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from data.xbrl import load_facts

_FLOW = {  # 流量类科目（TTM = 近4季度和）
    "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
    "CostOfRevenue", "GrossProfit", "OperatingIncomeLoss", "NetIncomeLoss",
    "ResearchAndDevelopmentExpense", "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "DepreciationDepletionAndAmortization",
}
_STOCK = {  # 存量类科目（取最近一期）
    "Assets", "Liabilities", "StockholdersEquity", "AssetsCurrent",
    "LiabilitiesCurrent", "CashAndCashEquivalentsAtCarryingValue",
    "LongTermDebtNoncurrent", "InventoryNet",
    "CommonStockSharesOutstanding", "EarningsPerShareDiluted",
}

_cache: dict = {}


def _pit_frame(tag_candidates: list[str]) -> pd.DataFrame:
    """把多个候选 tag 合并成 [filed_date x ticker] 的 point-in-time 面板。

    - 流量类：滚动 4 季度求和（TTM）
    - 存量类：取该 filed 日最新值
    返回 index=filed_date, columns=ticker。
    """
    key = tuple(tag_candidates)
    if key in _cache:
        return _cache[key]
    df = load_facts()
    parts = []
    for tag in tag_candidates:
        sub = df[df.tag == tag]
        if sub.empty:
            continue
        is_flow = tag in _FLOW
        # 每个 ticker 按 filed 排序
        for tk, g in sub.groupby("ticker"):
            g = g.sort_values("filed")
            if is_flow:
                # 用 end 去重避免重报，取 4 季度和
                g = g.drop_duplicates(subset=["end"], keep="last")
                # filed 同日多 end → 只留最后一条，避免重复索引
                g = g.drop_duplicates(subset=["filed"], keep="last")
                val = g.set_index("filed")["val"].rolling(4, min_periods=4).sum()
            else:
                g = g.drop_duplicates(subset=["filed"], keep="last")
                val = g.set_index("filed")["val"]
            parts.append(val.rename(tk))
    if not parts:
        _cache[key] = pd.DataFrame()
        return _cache[key]
    out = pd.concat(parts, axis=1).sort_index()
    # 同一 filed 日多行 → 去重
    if out.index.has_duplicates:
        out = out.groupby(level=0).last()
    # 多候选 tag 会产生【重名列】→ 按列名合并（先有的优先，后有的补缺）
    if out.columns.has_duplicates:
        merged = {}
        for col in dict.fromkeys(out.columns):  # 保持顺序去重
            sub = out.loc[:, out.columns == col]
            if sub.shape[1] == 1:
                merged[col] = sub.iloc[:, 0]
            else:
                merged[col] = sub.iloc[:, 0].combine_first(sub.iloc[:, 1])
        out = pd.DataFrame(merged)
    _cache[key] = out
    return out


def _reindex(piv: pd.DataFrame, panel) -> pd.DataFrame:
    if piv is None or piv.empty:
        return pd.DataFrame(index=panel["c"].index, columns=panel["c"].columns, dtype=float)
    piv = piv.sort_index()
    all_dates = piv.index.union(panel["c"].index).sort_values()
    out = piv.reindex(all_dates).ffill()
    return out.reindex(index=panel["c"].index, columns=panel["c"].columns)


def _price(panel) -> pd.DataFrame:
    return panel["c"]


def _shares(panel) -> pd.DataFrame:
    return _reindex(_pit_frame(["CommonStockSharesOutstanding",
                                "WeightedAverageNumberOfDilutedSharesOutstanding"]), panel)


# ---------------- 价值类 ----------------
def xbrl_earnings_yield(panel, n=None):
    """盈利收益率 E/P = TTM 净利润 / 市值。"""
    ni = _reindex(_pit_frame(["NetIncomeLoss"]), panel)
    mcap = _shares(panel) * _price(panel)
    return (ni / mcap).replace([np.inf, -np.inf], np.nan)


def xbrl_book_to_price(panel, n=None):
    """账面市值比 B/P = 股东权益 / 市值。"""
    eq = _reindex(_pit_frame(["StockholdersEquity"]), panel)
    mcap = _shares(panel) * _price(panel)
    return (eq / mcap).replace([np.inf, -np.inf], np.nan)


def xbrl_sales_yield(panel, n=None):
    """营收收益率 S/P = TTM 营收 / 市值。"""
    rev = _reindex(_pit_frame(["Revenues",
                               "RevenueFromContractWithCustomerExcludingAssessedTax"]), panel)
    mcap = _shares(panel) * _price(panel)
    return (rev / mcap).replace([np.inf, -np.inf], np.nan)


def xbrl_fcf_yield(panel, n=None):
    """自由现金流收益率 = (经营现金流 - 资本开支) / 市值。"""
    ocf = _reindex(_pit_frame(["NetCashProvidedByUsedInOperatingActivities"]), panel)
    capex = _reindex(_pit_frame(["PaymentsToAcquirePropertyPlantAndEquipment"]), panel)
    mcap = _shares(panel) * _price(panel)
    return ((ocf - capex.abs()) / mcap).replace([np.inf, -np.inf], np.nan)


# ---------------- 质量类 ----------------
def xbrl_roe(panel, n=None):
    """净资产收益率 = TTM 净利润 / 股东权益。"""
    ni = _reindex(_pit_frame(["NetIncomeLoss"]), panel)
    eq = _reindex(_pit_frame(["StockholdersEquity"]), panel)
    return (ni / eq.abs()).replace([np.inf, -np.inf], np.nan)


def xbrl_gross_margin(panel, n=None):
    """毛利率 = TTM 毛利 / TTM 营收。"""
    gp = _reindex(_pit_frame(["GrossProfit"]), panel)
    rev = _reindex(_pit_frame(["Revenues",
                               "RevenueFromContractWithCustomerExcludingAssessedTax"]), panel)
    return (gp / rev.abs()).replace([np.inf, -np.inf], np.nan)


def xbrl_net_margin(panel, n=None):
    """净利率 = TTM 净利润 / TTM 营收。"""
    ni = _reindex(_pit_frame(["NetIncomeLoss"]), panel)
    rev = _reindex(_pit_frame(["Revenues",
                               "RevenueFromContractWithCustomerExcludingAssessedTax"]), panel)
    return (ni / rev.abs()).replace([np.inf, -np.inf], np.nan)


def xbrl_asset_turnover(panel, n=None):
    """资产周转率 = TTM 营收 / 总资产。"""
    rev = _reindex(_pit_frame(["Revenues",
                               "RevenueFromContractWithCustomerExcludingAssessedTax"]), panel)
    assets = _reindex(_pit_frame(["Assets"]), panel)
    return (rev / assets.abs()).replace([np.inf, -np.inf], np.nan)


# ---------------- 成长类 ----------------
def _yoy(frame: pd.DataFrame, panel) -> pd.DataFrame:
    """用 filed 日索引近似年增长率 = 现值 / 一年前值 - 1。"""
    if frame is None or frame.empty:
        return pd.DataFrame(index=panel["c"].index, columns=panel["c"].columns, dtype=float)
    f = frame.sort_index()
    shifted = f.shift(4)  # 4 个季度
    g = (f / shifted.abs()).replace([np.inf, -np.inf], np.nan) - 1.0
    return _reindex(g, panel)


def xbrl_revenue_yoy(panel, n=None):
    """营收同比增速。"""
    rev = _reindex(_pit_frame(["Revenues",
                               "RevenueFromContractWithCustomerExcludingAssessedTax"]), panel)
    return _yoy(rev, panel)


def xbrl_earnings_yoy(panel, n=None):
    """盈利同比增速。"""
    ni = _reindex(_pit_frame(["NetIncomeLoss"]), panel)
    return _yoy(ni, panel)


def xbrl_log_mcap(panel, n=None):
    """对数市值（规模因子，小市值溢价）。"""
    mcap = _shares(panel) * _price(panel)
    return np.log(mcap.where(mcap > 0))


XBRL_OPS = {
    "xbrl_earnings_yield": xbrl_earnings_yield,
    "xbrl_book_to_price": xbrl_book_to_price,
    "xbrl_sales_yield": xbrl_sales_yield,
    "xbrl_fcf_yield": xbrl_fcf_yield,
    "xbrl_roe": xbrl_roe,
    "xbrl_gross_margin": xbrl_gross_margin,
    "xbrl_net_margin": xbrl_net_margin,
    "xbrl_asset_turnover": xbrl_asset_turnover,
    "xbrl_revenue_yoy": xbrl_revenue_yoy,
    "xbrl_earnings_yoy": xbrl_earnings_yoy,
    "xbrl_log_mcap": xbrl_log_mcap,
}
