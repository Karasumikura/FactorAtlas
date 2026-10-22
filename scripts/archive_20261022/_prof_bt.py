"""测量单个候选回测的时间分布。"""
import time, sys
sys.path.insert(0, "/opt/quant-lab")
from core import data, backtest
from factors.engine import factor_series_to_frame
from factors.exposure import tradable_mask, size_proxy
from core.backtest_fast import rank_ic_fast, layered_ls_fast

panel = data.build_panel()
print("panel:", {k: v.shape for k, v in panel.items()})

specs = [
    {"op": "vol", "params": {"n": 20}, "direction": 1, "name": "vol_n20"},
    {"op": "mom", "params": {"n": 60, "skip": 0}, "direction": 1, "name": "mom_n60"},
]

# 预热
factor_series_to_frame(panel, specs[0])
tradable_mask(panel)

for spec in specs:
    t = {}
    a = time.perf_counter()
    f = factor_series_to_frame(panel, spec)
    t["compute_factor"] = time.perf_counter() - a
    a = time.perf_counter()
    fwd = backtest.forward_returns(panel, hold=1)
    t["fwd"] = time.perf_counter() - a
    a = time.perf_counter()
    mask = tradable_mask(panel)
    t["mask"] = time.perf_counter() - a
    a = time.perf_counter()
    factor_series_to_frame(panel, spec)
    t["compute_factor_2nd"] = time.perf_counter() - a
    a = time.perf_counter()
    ic = rank_ic_fast(f, fwd, mask)
    t["rank_ic"] = time.perf_counter() - a
    a = time.perf_counter()
    gross, net, lo, to = layered_ls_fast(f, fwd, mask=mask)
    t["layered_ls"] = time.perf_counter() - a
    a = time.perf_counter()
    backtest.evaluate_with_split(panel, spec["name"], spec, hold=1)
    t["evaluate_with_split_TOTAL"] = time.perf_counter() - a
    print(spec["name"], {k: round(v, 4) for k, v in t.items()})
