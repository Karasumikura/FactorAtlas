"""验证两处优化：
1) _neutralize_fast：取子集后再中性化 == 先中性化再取子集（逐值等价，校验 NaN 掩码一致性）
2) rank_ic_fast：numpy 版 == pandas rank 版（逐值等价）
"""
import sys, time
sys.path.insert(0, "/opt/quant-lab")
import numpy as np
import pandas as pd

from core import data, backtest
from factors.engine import factor_series_to_frame, OPS
from factors.exposure import tradable_mask, size_proxy
from core.backtest_fast import _prep, rank_ic_fast


# ---------------- 1. 中性化可加性 ----------------
def check_neutralize(panel, exp, ops):
    worst = 0.0
    nan_mismatch = 0
    for op in ops:
        n = 5 if op == "gap" else 20
        spec = {"op": op, "params": {"n": n}, "direction": 1}
        f_full = factor_series_to_frame(panel, spec)
        neu_full = backtest._neutralize_fast(f_full, exp)
        for frac in (0.7, 0.3):
            dates = f_full.index[int(len(f_full) * frac):]
            a = neu_full.loc[dates]
            b = backtest._neutralize_fast(f_full.loc[dates], exp.loc[dates])
            m1 = a.notna().to_numpy()
            m2 = b.notna().to_numpy()
            if not np.array_equal(m1, m2):
                nan_mismatch += 1
            d = np.nanmax(np.abs(a.to_numpy() - b.to_numpy()))
            worst = max(worst, float(d))
    return worst, nan_mismatch


# ---------------- 2. rank_ic 向量化 ----------------
def rank_ic_np(factor, fwd, mask=None, min_n=20):
    f, r = _prep(factor, fwd, mask)
    fv = f.to_numpy(dtype=float)
    rv = r.to_numpy(dtype=float)
    fm = ~np.isnan(fv)
    rm = ~np.isnan(rv)
    m = fm & rm
    # 逐行 rank（nan 保持 nan）
    def _rank_rows(a, valid):
        out = np.full(a.shape, np.nan)
        for i in range(a.shape[0]):
            row = a[i]
            idx = np.flatnonzero(valid[i])
            if idx.size:
                out[i, idx] = pd.Series(row[idx]).rank().to_numpy()
        return out
    fr = _rank_rows(fv, m)
    rr = _rank_rows(rv, m)
    cnt = m.sum(axis=1)
    with np.errstate(invalid="ignore"):
        fz = fr - np.nanmean(fr, axis=1, keepdims=True)
        rz = rr - np.nanmean(rr, axis=1, keepdims=True)
        num = np.nansum(fz * rz, axis=1)
        den = np.sqrt(np.nansum(fz ** 2, axis=1) * np.nansum(rz ** 2, axis=1))
        ic = num / np.where(den == 0, np.nan, den)
    ic = np.where(cnt >= min_n, ic, np.nan)
    s = pd.Series(ic, index=f.index).dropna()
    return s


panel = data.build_panel()
fwd = backtest.forward_returns(panel, hold=1)
mask = tradable_mask(panel)
exp = size_proxy(panel)
ops = ["vol", "mom", "reversal", "hl_range", "volatility_ratio", "vwap_dev",
       "turnover", "skew", "kurt", "amihud", "price_position", "gap"]

w, nanmis = check_neutralize(panel, exp, ops)
print(f"[中性化] ops={len(ops)} x 2 段  最大差异={w:.3e}  NaN掩码不一致={nanmis}")

# rank_ic 对拍
f = factor_series_to_frame(panel, {"op": "vol", "params": {"n": 20}, "direction": 1})
fn = backtest._neutralize_fast(f, exp)
a = rank_ic_fast(fn, fwd, mask)
t0 = time.perf_counter(); b = rank_ic_np(fn, fwd, mask); t_np = time.perf_counter() - t0
t0 = time.perf_counter(); rank_ic_fast(fn, fwd, mask); t_pd = time.perf_counter() - t0
print(f"[rank_ic] idx_eq={a.index.equals(b.index)} len={len(a)}/{len(b)} "
      f"maxdiff={np.max(np.abs(a.values - b.values)):.3e}")
print(f"[rank_ic] pandas={t_pd:.3f}s numpy={t_np:.3f}s")
