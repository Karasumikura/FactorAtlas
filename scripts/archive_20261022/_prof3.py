"""精确拆解 evaluate_with_split 每个子步骤占比。"""
import sys, time
sys.path.insert(0, "/opt/quant-lab")
import numpy as np
from core import data, backtest
from factors.engine import factor_series_to_frame
from factors.exposure import tradable_mask, size_proxy
from core.backtest_fast import rank_ic_fast, layered_ls_fast

panel = data.build_panel()
spec = {"op": "vol", "params": {"n": 20}, "direction": 1}
fwd = backtest.forward_returns(panel, hold=1)
mask = tradable_mask(panel)
exp = size_proxy(panel)
train_dates, test_dates = backtest.split_periods(panel, 0.7)

# 预热
factor_series_to_frame(panel, spec)
backtest._neutralize_fast(factor_series_to_frame(panel, spec), exp)
rank_ic_fast(factor_series_to_frame(panel, spec), fwd, mask)
layered_ls_fast(factor_series_to_frame(panel, spec), fwd, mask=mask)

N = 3
T = {}
def t(k, fn):
    t0 = time.perf_counter(); v = fn(); T[k] = T.get(k, 0) + time.perf_counter() - t0; return v

for _ in range(N):
    f_full = t("1 factor_series_to_frame(full)", lambda: factor_series_to_frame(panel, spec))
    f_neu = t("2 _neutralize_fast(full)", lambda: backtest._neutralize_fast(f_full, exp))
    t("3 rank_ic_fast(full)", lambda: rank_ic_fast(f_neu, fwd, mask))
    t("4 layered_ls_fast(full)", lambda: layered_ls_fast(f_neu, fwd, mask=mask))
    # OOS 段
    f_oos = t("5 factor_series_to_frame(oos)", lambda: factor_series_to_frame(panel, spec).loc[test_dates])
    t("6 fwd .loc[test]", lambda: backtest.forward_returns(panel, hold=1).loc[test_dates])
    t("7 mask .loc[test]", lambda: tradable_mask(panel).loc[test_dates])
    f_oos_neu = t("8 _neutralize_fast(oos)", lambda: backtest._neutralize_fast(f_oos, size_proxy(panel).loc[test_dates]))
    t("9 rank_ic_fast(oos)", lambda: rank_ic_fast(f_oos_neu, backtest.forward_returns(panel, hold=1).loc[test_dates], mask.loc[test_dates]))
    t("10 layered_ls_fast(oos)", lambda: layered_ls_fast(f_oos_neu, backtest.forward_returns(panel, hold=1).loc[test_dates], mask=mask.loc[test_dates]))

tot = 0
for k in sorted(T):
    v = T[k] / N
    tot += v
    print(f"{k:40s} {v:.4f}s")
print(f"{'SUM':40s} {tot:.4f}s")
