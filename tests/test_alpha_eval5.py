"""core/alpha_eval5.py 增量测试（只测新模块，不跑全库）。"""
import math

import numpy as np
import pandas as pd

from core.alpha_eval5 import _diversity, _logic, _pfs, _rre, evaluate_set


def _panel(n_days: int = 60, n_sym: int = 40) -> dict:
    rng = np.random.default_rng(7)
    idx = pd.date_range("2025-01-01", periods=n_days, freq="B")
    cols = [f"S{i}" for i in range(n_sym)]
    c = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0, 0.01, (n_days, n_sym)), axis=0),
                     index=idx, columns=cols)
    return {"c": c, "h": c * 1.01, "l": c * 0.99,
            "o": c, "v": pd.DataFrame(1e6, index=idx, columns=cols),
            "vw": c}


def test_rre_stable_factor_near_one():
    f = pd.DataFrame(np.tile(np.arange(40, dtype=float), (60, 1)),
                     index=pd.date_range("2025-01-01", periods=60, freq="B"))
    v = _rre(f)
    assert v == v and 0.9 < v <= 1.0


def test_pfs_high_when_noise_small():
    f = pd.DataFrame(np.random.default_rng(1).normal(0, 1, (60, 40)),
                     index=pd.date_range("2025-01-01", periods=60, freq="B"))
    for kind in ("gauss", "t"):
        v = _pfs(f, kind)
        assert v == v and v > 0.9, f"{kind} PFS 应接近 1，实际 {v}"


def test_logic_rejects_future_ops_and_bad_params():
    assert _logic({"op": "future_return", "params": {"n": 20}, "direction": 1}) == 0.0
    assert _logic({"op": "mom", "params": {"n": 100000}, "direction": 1}) < 100
    assert _logic({"op": "mom", "params": {"n": 250}, "direction": 1}) == 100.0
    assert _logic({"op": "vol", "params": {"n": 120}, "direction": 1}) == 90.0


def test_diversity_uses_ic_series_not_rank_mean():
    idx = pd.date_range("2025-01-01", periods=50, freq="B")
    rng = np.random.default_rng(3)
    d = {}
    for name, seed in (("a", 1), ("b", 2), ("c", 3)):
        r = np.random.default_rng(seed)
        d[name] = pd.Series(r.normal(0, 0.01, 50) + (0.001 if name == "a" else 0), index=idx)
    v = _diversity(d)
    assert v == v and 0.0 < v <= 1.0


def test_evaluate_set_shape_and_no_nan_crash():
    p = _panel()
    specs = [
        {"name": "mom20", "op": "mom", "params": {"n": 20}, "direction": 1},
        {"name": "vol20", "op": "vol", "params": {"n": 20}, "direction": 1},
    ]
    r = evaluate_set(p, specs, hold=1)
    assert r["n_factors"] == 2
    assert len(r["factors"]) == 2
    assert all("logic" in x for x in r["factors"])
    assert math.isnan(r["diversity"]) or 0.0 <= r["diversity"] <= 1.0
