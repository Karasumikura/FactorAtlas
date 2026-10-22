"""端到端对拍：evaluate_with_split 的所有输出字段，新实现 vs 原始实现。"""
import sys, time
sys.path.insert(0, "/opt/quant-lab")
import numpy as np
import pandas as pd

from core import data, backtest
from factors.engine import factor_series_to_frame
from factors.exposure import tradable_mask, size_proxy
from core.backtest_fast import rank_ic_fast, layered_ls_fast, _prep


# ------- 原始实现（改动前逐字复制），仅用于对拍 -------
def _old_neutralize(factor, exposure):
    f = factor.copy()
    e = exposure.reindex_like(f)
    valid = f.notna() & e.notna()
    fv = f.where(valid); ev = e.where(valid)
    em = ev.mean(axis=1); fm = fv.mean(axis=1)
    e_dm = ev.sub(em, axis=0); f_dm = fv.sub(fm, axis=0)
    num = (e_dm * f_dm).sum(axis=1); den = (e_dm ** 2).sum(axis=1)
    beta = num / den.replace(0, np.nan)
    return (f_dm - e_dm.mul(beta, axis=0)).where(valid)


def _old_sharpe(s):
    if len(s) < 2 or s.std() == 0:
        return 0.0
    return float(s.mean() / s.std() * np.sqrt(252))


def _old_layered(factor, fwd, n_groups=5, mask=None, cost_bps=5.0):
    f, r = _prep(factor, fwd, mask)
    fr = f.rank(axis=1, method="first")
    n = f.notna().sum(axis=1)
    valid = n >= n_groups * 4
    q = fr.div(n, axis=0)
    group = np.floor(q * n_groups).clip(upper=n_groups - 1)
    top = group == (n_groups - 1); bot = group == 0
    long_ret = (r.where(top).mean(axis=1)); short_ret = (r.where(bot).mean(axis=1))
    gross = (long_ret - short_ret).where(valid)
    top_idx = {d: set(top.columns[top.loc[d].fillna(False)]) for d in f.index}
    bot_idx = {d: set(bot.columns[bot.loc[d].fillna(False)]) for d in f.index}
    prev_top = prev_bot = None; turns = {}
    for d in f.index:
        if prev_top is not None:
            tl, bl = top_idx[d], bot_idx[d]
            denom = 2 * max(len(tl), 1)
            turns[d] = (len(tl ^ prev_top) + len(bl ^ prev_bot)) / denom
        prev_top, prev_bot = top_idx[d], bot_idx[d]
    turnover = pd.Series(turns).reindex(f.index)
    cost = cost_bps / 10000.0
    return gross.dropna(), (gross - turnover * cost * 2).dropna(), long_ret.dropna(), float(turnover.mean())


def evaluate_OLD(panel, name, spec, hold=1, train_frac=0.7, use_mask=True, neutralize_size=True):
    """改动前的 evaluate_with_split 原样复刻。"""
    dates = panel["c"].index
    cut = int(len(dates) * train_frac)
    train_dates, test_dates = dates[:cut], dates[cut:]
    factor = factor_series_to_frame(panel, spec)
    fwd = backtest.forward_returns(panel, hold=hold)
    mask = tradable_mask(panel) if use_mask else None
    if neutralize_size:
        factor = _old_neutralize(factor, size_proxy(panel))
    ic = rank_ic_fast(factor, fwd, mask)
    gross, net, lg, to = _old_layered(factor, fwd, mask=mask)
    out = dict(
        ic_mean=float(ic.mean()) if len(ic) else 0.0,
        ls_sharpe=_old_sharpe(gross), net_ls_sharpe=_old_sharpe(net),
        ls_return=float(gross.mean() * 252) if len(gross) else 0.0,
        turnover=to, n_days=len(ic),
    )
    f_oos = factor_series_to_frame(panel, spec).loc[test_dates]
    fwd_oos = backtest.forward_returns(panel, hold=hold).loc[test_dates]
    mask_oos = tradable_mask(panel).loc[test_dates] if use_mask else None
    if neutralize_size:
        f_oos = _old_neutralize(f_oos, size_proxy(panel).loc[test_dates])
    ic_o = rank_ic_fast(f_oos, fwd_oos, mask_oos)
    g_o, _, _, _ = _old_layered(f_oos, fwd_oos, mask=mask_oos)
    out["oos_ic_mean"] = float(ic_o.mean()) if len(ic_o) else float("nan")
    out["oos_ls_sharpe"] = _old_sharpe(g_o) if len(g_o) else float("nan")
    return out


panel = data.build_panel()
ops = ["vol", "mom", "reversal", "hl_range", "volatility_ratio", "vwap_dev",
       "turnover", "skew", "kurt", "amihud", "price_position", "gap"]
fields = ["ic_mean", "ls_sharpe", "net_ls_sharpe", "ls_return", "turnover",
          "n_days", "oos_ic_mean", "oos_ls_sharpe"]
worst = {k: 0.0 for k in fields}
new_times = []
for op in ops:
    n = 5 if op == "gap" else 20
    spec = {"op": op, "params": {"n": n}, "direction": 1}
    a = evaluate_OLD(panel, op, spec)
    t0 = time.perf_counter()
    r = backtest.evaluate_with_split(panel, op, spec, hold=1, neutralize_size=True)
    new_times.append(time.perf_counter() - t0)
    b = {k: getattr(r, k) for k in fields}
    bad = []
    for k in fields:
        d = abs(float(a[k]) - float(b[k]))
        if not (d <= 1e-12 or (np.isnan(a[k]) and np.isnan(b[k]))):
            bad.append((k, a[k], b[k], d))
        if d == d:
            worst[k] = max(worst[k], d)
    if bad:
        print("MISMATCH", op, bad)
print("最大字段差异:", {k: f"{v:.2e}" for k, v in worst.items()})
print(f"改动后平均耗时 {np.mean(new_times):.3f}s/候选  (共 {len(ops)} 个)")
