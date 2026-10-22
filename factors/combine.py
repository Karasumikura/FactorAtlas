"""因子组合层：把单因子合成为更强的组合因子。

支持：
- 横截面标准化（z-score / rank）
- 中性化（对市值、行业回归取残差）
- 多因子等权/IC加权合成
- 因子间正交化（避免共线）
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def cross_rank(df: pd.DataFrame) -> pd.DataFrame:
    """逐行截面排名，归一化到 [-0.5, 0.5]。"""
    return df.rank(axis=1, pct=True) - 0.5


def cross_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """逐行截面 z-score。"""
    m = df.mean(axis=1)
    s = df.std(axis=1)
    return df.sub(m, axis=0).div(s.replace(0, np.nan), axis=0)


def winsorize(df: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """逐行缩尾。"""
    lo = df.quantile(lower, axis=1)
    hi = df.quantile(upper, axis=1)
    return df.clip(lower=lo, upper=hi, axis=0)


def neutralize(factor: pd.DataFrame, exposure: pd.DataFrame) -> pd.DataFrame:
    """逐日对暴露做横截面回归，取残差（中性化）。

    factor, exposure 都是 [date x symbol]。
    """
    out = pd.DataFrame(index=factor.index, columns=factor.columns, dtype=float)
    for d in factor.index:
        if d not in exposure.index:
            continue
        f = factor.loc[d]
        e = exposure.loc[d]
        common = f.dropna().index.intersection(e.dropna().index)
        if len(common) < 20:
            continue
        x = e.loc[common].values.astype(float)
        y = f.loc[common].values.astype(float)
        x = np.column_stack([np.ones(len(x)), x])
        try:
            beta, *_ = np.linalg.lstsq(x, y, rcond=None)
            resid = y - x @ beta
            out.loc[d, common] = resid
        except np.linalg.LinAlgError:
            continue
    return out


def combine_factors(factors: dict[str, pd.DataFrame], weights: dict[str, float] | None = None,
                    method: str = "rank") -> pd.DataFrame:
    """合成多因子。method: rank/zscore/raw。"""
    if not factors:
        return pd.DataFrame()
    processed = {}
    for name, f in factors.items():
        f = f.dropna(how="all")
        if method == "rank":
            processed[name] = cross_rank(f)
        elif method == "zscore":
            processed[name] = cross_zscore(winsorize(f))
        else:
            processed[name] = f
    idx = None
    cols = None
    for f in processed.values():
        idx = f.index if idx is None else idx.union(f.index)
        cols = f.columns if cols is None else cols.union(f.columns)
    total = pd.DataFrame(0.0, index=idx, columns=cols)
    wsum = 0.0
    for name, f in processed.items():
        w = (weights or {}).get(name, 1.0)
        total = total.add(f.reindex(index=idx, columns=cols).fillna(0.0) * w, fill_value=0.0)
        wsum += abs(w)
    if wsum > 0:
        total = total / wsum
    return total
