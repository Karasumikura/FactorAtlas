"""改动后回归：对比 backtest_fast.layered_ls_fast 新实现 vs 原始逐日集合实现。"""
import sys, time
sys.path.insert(0, "/opt/quant-lab")
import numpy as np
import pandas as pd

from core import data, backtest
from factors.engine import factor_series_to_frame
from factors.exposure import tradable_mask
from core.backtest_fast import layered_ls_fast, _prep


def _reference(factor, fwd, n_groups=5, mask=None, cost_bps=5.0):
    """原始实现（逐日 set 版），仅用于对拍。"""
    f, r = _prep(factor, fwd, mask)
    fr = f.rank(axis=1, method="first")
    n = f.notna().sum(axis=1)
    valid = n >= n_groups * 4
    q = fr.div(n, axis=0)
    group = np.floor(q * n_groups).clip(upper=n_groups - 1)
    top = group == (n_groups - 1)
    bot = group == 0
    long_ret = (r.where(top).mean(axis=1))
    short_ret = (r.where(bot).mean(axis=1))
    gross = (long_ret - short_ret).where(valid)
    top_idx = {d: set(top.columns[top.loc[d].fillna(False)]) for d in f.index}
    bot_idx = {d: set(bot.columns[bot.loc[d].fillna(False)]) for d in f.index}
    prev_top = prev_bot = None
    turns = {}
    for d in f.index:
        if prev_top is not None:
            tl, bl = top_idx[d], bot_idx[d]
            denom = 2 * max(len(tl), 1)
            turns[d] = (len(tl ^ prev_top) + len(bl ^ prev_bot)) / denom
        prev_top, prev_bot = top_idx[d], bot_idx[d]
    turnover = pd.Series(turns).reindex(f.index)
    cost = cost_bps / 10000.0
    net = gross - turnover * cost * 2
    return gross.dropna(), net.dropna(), long_ret.dropna(), float(turnover.mean())


panel = data.build_panel()
fwd = backtest.forward_returns(panel, hold=1)
mask = tradable_mask(panel)

ops = ["vol", "mom", "reversal", "hl_range", "volatility_ratio", "vwap_dev",
       "turnover", "skew", "kurt", "amihud", "price_position", "gap",
       "max_drawdown", "volume_ratio", "close_to_high"]
worst = 0.0
fails = 0
for op in ops:
    n = 5 if op == "gap" else 20
    spec = {"op": op, "params": {"n": n}, "direction": 1}
    f = factor_series_to_frame(panel, spec)
    a = _reference(f, fwd, mask=mask)
    b = layered_ls_fast(f, fwd, mask=mask)
    assert a[0].index.equals(b[0].index) and a[2].index.equals(b[2].index), op
    d1 = float(np.max(np.abs(a[0].values - b[0].values)))
    d2 = float(np.max(np.abs(a[1].reindex(b[1].index).values - b[1].values)))
    d3 = float(np.max(np.abs(a[2].values - b[2].values)))
    d4 = abs(a[3] - b[3])
    worst = max(worst, d1, d2, d3, d4)
    if max(d1, d2, d3, d4) > 1e-12:
        fails += 1
        print("MISMATCH", op, d1, d2, d3, d4)
print(f"ops={len(ops)} 最大差异={worst:.3e} 不一致数={fails}")

# 端到端 evaluate_with_split 计时（改动后）
t0 = time.perf_counter()
for op in ["vol", "mom", "hl_range", "reversal", "skew"]:
    backtest.evaluate_with_split(panel, op, {"op": op, "params": {"n": 20}, "direction": 1})
print(f"改动后 evaluate_with_split 平均 {(time.perf_counter()-t0)/5:.3f}s/候选")
