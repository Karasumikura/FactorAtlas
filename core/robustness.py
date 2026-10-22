"""稳健性检验：确认幸存因子不是数据假象。

关键检查：
1. 幸存者偏差：当前 universe 是「今天的流动性榜单」，回测到 2021 会有
   选择偏差（今天大的，当年未必符合条件）。这里做子样本稳健性检验。
2. 分年度 IC：信号是否只在某一年有效。
3. 分位数单调性：5组收益是否单调，还是只有极值组。
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from core.backtest_fast import rank_ic_fast, layered_ls_fast
from core.backtest import forward_returns, _neutralize_fast
from factors.engine import factor_series_to_frame
from factors.exposure import tradable_mask, size_proxy


def yearly_ic(panel: dict, spec: dict) -> dict:
    """按年计算 IC，看信号稳定性。"""
    factor = factor_series_to_frame(panel, spec)
    fwd = forward_returns(panel, 1)
    mask = tradable_mask(panel)
    ic = rank_ic_fast(factor, fwd, mask)
    out = {}
    for year, grp in ic.groupby(ic.index.year):
        out[int(year)] = {"ic": float(grp.mean()), "n": int(len(grp))}
    return out


def quantile_returns(panel: dict, spec: dict, n_groups: int = 5) -> list[float]:
    """5 分组的平均前瞻收益，看单调性。"""
    factor = factor_series_to_frame(panel, spec)
    fwd = forward_returns(panel, 1)
    mask = tradable_mask(panel)
    f, r = factor.copy(), fwd.copy()
    if mask is not None:
        f = f.where(mask.reindex_like(f))
    fr = f.rank(axis=1, method="first")
    n = f.notna().sum(axis=1)
    q = np.floor(fr.div(n, axis=0) * n_groups).clip(upper=n_groups - 1)
    out = []
    for g in range(n_groups):
        grp_ret = r.where(q == g).mean(axis=1)
        out.append(float(grp_ret.mean() * 252))
    return out


def subsample_ic(panel: dict, spec: dict, n_splits: int = 4) -> list[dict]:
    """把样本切成 n 段，看每段 IC 是否同向（稳健性）。"""
    factor = factor_series_to_frame(panel, spec)
    fwd = forward_returns(panel, 1)
    mask = tradable_mask(panel)
    ic = rank_ic_fast(factor, fwd, mask)
    if not len(ic):
        return []
    idx_chunks = np.array_split(np.arange(len(ic)), n_splits)
    out = []
    for i, pos in enumerate(idx_chunks):
        ch = ic.iloc[pos]
        out.append({
            "seg": i + 1,
            "start": str(ch.index[0].date()), "end": str(ch.index[-1].date()),
            "ic": float(ch.mean()),
        })
    return out


def yearly_ls_sharpe(panel: dict, spec: dict) -> dict:
    """按年计算【多空组合 Sharpe】——与幸存判定口径一致（不用 IC）。

    A股强 beta 下，分年 IC 可能整体为负但 LS 组合为正；用分年 LS Sharpe 才
    能真实反映"该年是否可交易"。
    """
    from core.backtest_fast import layered_ls_fast
    factor = factor_series_to_frame(panel, spec)
    fwd = forward_returns(panel, 1)
    mask = tradable_mask(panel)
    gross, _, _, _ = layered_ls_fast(factor, fwd, mask=mask)
    if not len(gross):
        return {}
    out = {}
    for year, grp in gross.groupby(gross.index.year):
        if len(grp) < 20 or grp.std() == 0:
            out[int(year)] = {"sharpe": 0.0, "n": int(len(grp))}
        else:
            out[int(year)] = {"sharpe": float(grp.mean() / grp.std() * np.sqrt(252)),
                              "n": int(len(grp))}
    return out


def subperiod_sharpe(period_returns, periods_per_year: float, n_splits: int = 5) -> list[dict]:
    """把真实调仓的期收益序列等分成 n_splits 段，逐段算 Sharpe。

    这是最诚实的时段稳健性检验：直接看"每一段是否可交易"，而不是
    IC 或日频口径。用于检测"全期漂亮但某段崩"的因子。
    """
    pr = np.asarray(period_returns, dtype=float)
    pr = pr[np.isfinite(pr)]
    if len(pr) < n_splits * 2:
        return []
    seg = len(pr) // n_splits
    out = []
    for i in range(n_splits):
        s = pr[i * seg:(i + 1) * seg] if i < n_splits - 1 else pr[i * seg:]
        if len(s) < 2 or np.std(s) == 0:
            out.append({"seg": i, "sharpe": 0.0, "ann": 0.0, "hit": 0.0, "n": len(s)})
            continue
        out.append({
            "seg": i,
            "sharpe": float(np.mean(s) / np.std(s) * np.sqrt(periods_per_year)),
            "ann": float(np.mean(s) * periods_per_year),
            "hit": float(np.mean(s > 0)),
            "n": len(s),
        })
    return out


def robustness_report(panel: dict, spec: dict) -> dict:
    return {
        "spec": spec,
        "yearly_ic": yearly_ic(panel, spec),
        "yearly_ls_sharpe": yearly_ls_sharpe(panel, spec),
        "quantile_annual_returns": quantile_returns(panel, spec),
        "subsample_ic": subsample_ic(panel, spec),
    }
