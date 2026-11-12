#!/usr/bin/env python3
"""factors/alt_data.py 增量回归：用合成矩阵，不依赖后台拉取是否完成。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import factors.alt_data as alt
from factors.sweep import generate_grid
from factors.engine import OPS


@pytest.fixture(scope="module")
def panel():
    from core.data import build_panel
    return build_panel()


def _fake(panel, mode="finra"):
    idx, cols = alt._frame(panel)
    rng = np.random.default_rng(11)
    v = rng.uniform(0.05, 0.5, (len(idx), len(cols)))
    m = pd.DataFrame(v, index=idx, columns=cols)
    m.iloc[:, :40] = np.nan       # 模拟 40 个未覆盖标的
    if mode == "finra":
        return {"ratio": m, "exempt": m * 0.1}
    return np.log1p(m * 1000)


def test_ops_registered_and_experimental(panel):
    for name in alt.ALT_OPS:
        assert name in OPS
    prod = generate_grid()
    assert not any(x["op"].startswith(("finra_", "wiki_", "edgar_")) for x in prod)
    exp = generate_grid(include_experimental=True)
    assert sum(x["op"].startswith("finra_") for x in exp) == 18
    assert sum(x["op"].startswith("wiki_") for x in exp) == 18


def test_effective_is_next_trading_day(panel):
    idx, _ = alt._frame(panel)
    raw = pd.Series(pd.to_datetime([idx[10], idx[10], "2020-01-01"]))
    eff, keep = alt._effective(idx, raw)
    assert keep.tolist() == [True, True, False]      # 面板前的丢弃
    assert (eff.iloc[:2] > raw.iloc[:2]).all()       # 严格晚于事件日


def test_finra_ops_mask_uncovered(panel, monkeypatch):
    monkeypatch.setattr(alt, "_CACHE", {"finra": _fake(panel, "finra")})
    for name in ("finra_short_ratio", "finra_short_surge", "finra_short_exempt"):
        m = alt.ALT_OPS[name](panel, n=60)
        assert m.shape == panel["c"].shape
        nan_rate = np.isnan(m.to_numpy()).mean()
        assert 0.05 < nan_rate < 0.08, (name, nan_rate)   # 40/655 ≈ 6.1%
        valid = m.to_numpy()[~np.isnan(m.to_numpy())]
        assert np.isfinite(valid).all(), name


def test_wiki_ops_mask_uncovered(panel, monkeypatch):
    monkeypatch.setattr(alt, "_CACHE", {"wiki": _fake(panel, "wiki")})
    for name in ("wiki_attention", "wiki_attention_chg", "wiki_attention_vol"):
        m = alt.ALT_OPS[name](panel, n=60)
        assert m.shape == panel["c"].shape
        v = m.to_numpy()
        assert 0.05 < np.isnan(v).mean() < 0.08
        assert np.isfinite(v[~np.isnan(v)]).all(), name


def test_missing_parquet_degrades_to_all_nan(panel, monkeypatch, tmp_path):
    monkeypatch.setattr(alt, "_CACHE", {})
    monkeypatch.setattr(alt, "PROJ", tmp_path)   # 没有 data/alt/
    m = alt.ALT_OPS["finra_short_ratio"](panel, n=60)
    assert m.shape == panel["c"].shape and np.isnan(m.to_numpy()).all()
