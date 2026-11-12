#!/usr/bin/env python3
"""core/cpcv_eval.py 增量回归：小合成 panel，不加载 371MB 真实面板。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.cpcv_eval import cpcv_sharpe_distribution


def _panel(n_days: int = 120, n_sym: int = 24, seed: int = 7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n_days, freq="B")
    c = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0, 0.01, (n_days, n_sym)), axis=0),
                     index=idx, columns=[f"s{i}" for i in range(n_sym)])
    v = pd.DataFrame(rng.uniform(1e6, 1e7, (n_days, n_sym)), index=idx, columns=c.columns)
    return {"c": c, "o": c * 0.999, "h": c * 1.005, "l": c * 0.995,
            "v": v, "vwap": c.copy()}


def test_vendored_purgedcv_imports():
    import purgedcv
    assert hasattr(purgedcv, "CombinatorialPurgedCV")
    assert hasattr(purgedcv, "PurgedKFold")


def test_cpcv_returns_distribution():
    p = _panel()
    spec = {"name": "mom_n20", "op": "mom", "params": {"n": 20}, "direction": 1}
    r = cpcv_sharpe_distribution(p, spec, hold=5, n_splits=4, n_test_groups=2)
    assert "error" not in r, r
    assert r["n_folds"] == 6              # C(4,2)
    assert r["n_valid"] >= 4
    for k in ("sharpe_mean", "sharpe_std", "sharpe_min", "sharpe_max",
              "sharpe_median", "negative_share"):
        assert k in r and np.isfinite(r[k])
    assert 0.0 <= r["negative_share"] <= 1.0
    assert r["sharpe_min"] <= r["sharpe_median"] <= r["sharpe_max"]


def test_cpcv_rejects_short_panel():
    r = cpcv_sharpe_distribution(_panel(n_days=12),
                                 {"op": "mom", "params": {"n": 5}, "direction": 1},
                                 hold=5, n_splits=6, n_test_groups=2)
    assert r.get("error") == "panel too short"
