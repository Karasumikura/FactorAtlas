"""分析 evaluate_with_split 剩余耗时分布。"""
import sys, time
sys.path.insert(0, "/opt/quant-lab")
from core import data, backtest
from factors.engine import factor_series_to_frame
from factors.exposure import tradable_mask, size_proxy
from core.backtest_fast import rank_ic_fast, layered_ls_fast, _layer_masks

panel = data.build_panel()
fwd = backtest.forward_returns(panel, hold=1)
mask = tradable_mask(panel)
exposure = size_proxy(panel)
spec = {"op": "vol", "params": {"n": 20}, "direction": 1}

# 预热
factor_series_to_frame(panel, spec)
_layer_masks(factor_series_to_frame(panel, spec), fwd, 5, mask)
backtest._neutralize_fast(factor_series_to_frame(panel, spec), exposure)

T = {}
def timeit(k, fn):
    t0 = time.perf_counter()
    v = fn()
    T[k] = T.get(k, 0.0) + time.perf_counter() - t0
    return v

for _ in range(3):
    f = timeit("factor_series_to_frame", lambda: factor_series_to_frame(panel, spec))
    fn = timeit("_neutralize_fast", lambda: backtest._neutralize_fast(f, exposure))
    timeit("rank_ic_fast", lambda: rank_ic_fast(fn, fwd, mask))
    timeit("_layer_masks(gross+turnover)", lambda: _layer_masks(fn, fwd, 5, mask))
    timeit("layered_ls_fast", lambda: layered_ls_fast(fn, fwd, mask=mask))

print({k: round(v / 3, 4) for k, v in T.items()})
t0 = time.perf_counter()
backtest.evaluate_with_split(panel, "vol", spec)
print("evaluate_with_split total:", round(time.perf_counter() - t0, 4))
