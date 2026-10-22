"""正交因子发现：找与已有因子不相关的新信号。

关键洞察：动量/趋势是一个大簇，里面挖 100 个也是同一个信号。
真正有价值的是**低相关**的新因子（组合时才有分散化收益）。

流程：
  1. 定义已有因子簇（如趋势类）。
  2. 生成候选。
  3. 计算候选与簇的横截面相关，只保留 |corr| < 阈值的。
  4. 对留下的做完整净化（FDR + OOS + 稳健性）。
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from factors.engine import factor_series_to_frame


def factor_series_mean_rank(f: pd.DataFrame) -> pd.Series:
    """把因子压缩成时间序列（每日截面 rank 的均值），用于算相关。"""
    return f.rank(axis=1, pct=True).mean(axis=1)


def _safe_corr(a: pd.Series, b: pd.Series) -> float:
    """安全相关系数：去 NaN、防常数序列。"""
    aligned = pd.concat([a, b], axis=1).dropna()
    if len(aligned) < 20:
        return 0.0
    x = aligned.iloc[:, 0]
    y = aligned.iloc[:, 1]
    if x.std() == 0 or y.std() == 0:
        return 0.0
    c = x.corr(y)
    return 0.0 if (c != c) else abs(float(c))


def corr_to_cluster(panel: dict, spec: dict, cluster_specs: list[dict],
                    train_frac: float = 0.7) -> float:
    """候选因子与已有簇的最大绝对相关。"""
    dates = panel["c"].index
    cut = int(len(dates) * train_frac)
    try:
        f = factor_series_to_frame(panel, spec).loc[:dates[cut - 1]]
        fs = factor_series_mean_rank(f)
    except Exception:
        return 999.0
    max_c = 0.0
    for cs in cluster_specs:
        try:
            cf = factor_series_to_frame(panel, cs).loc[:dates[cut - 1]]
            cs_series = factor_series_mean_rank(cf)
            max_c = max(max_c, _safe_corr(fs, cs_series))
        except Exception:
            continue
    return max_c


def select_orthogonal(panel: dict, candidates: list[dict], cluster_specs: list[dict],
                      max_corr: float = 0.5, train_frac: float = 0.7,
                      verbose: bool = True) -> list[dict]:
    """从候选里挑出与簇相关性低的。"""
    kept = []
    for s in candidates:
        c = corr_to_cluster(panel, s, cluster_specs, train_frac)
        if c < max_corr:
            kept.append({**s, "cluster_corr": round(c, 3)})
    if verbose:
        print(f"  [正交筛选] {len(candidates)} -> {len(kept)} (|corr|<{max_corr})", flush=True)
    return kept


# 已知的趋势/动量簇（用于排除）—— 美股
TREND_CLUSTER = [
    {"op": "mom", "params": {"n": 120}, "direction": 1},
    {"op": "mom", "params": {"n": 250}, "direction": 1},
    {"op": "trend_strength", "params": {"n": 120}, "direction": 1},
]

# A股的主导簇是【短期反转 + 波动/振幅】，与美股相反
ASHARE_CLUSTER = [
    {"op": "reversal", "params": {"n": 5}, "direction": 1},
    {"op": "reversal", "params": {"n": 20}, "direction": 1},
    {"op": "hl_range", "params": {"n": 10}, "direction": 1},
    {"op": "volatility_ratio", "params": {"n": 10}, "direction": 1},
]


def cluster_for(market: str) -> list[dict]:
    return ASHARE_CLUSTER if market == "ashare" else TREND_CLUSTER
