#!/usr/bin/env python3
"""factors/edgar_event.py 增量回归：真实 EDGAR 数据 + 无前视 + 伪影回归。"""
from __future__ import annotations

import numpy as np
import pytest

from factors.edgar_event import EDGAR_OPS, _events
from factors.engine import OPS
from factors.sweep import generate_grid


@pytest.fixture(scope="module")
def panel():
    from core.data import build_panel
    return build_panel()


def test_ops_registered_and_experimental_grid():
    for name in EDGAR_OPS:
        assert name in OPS
    prod = generate_grid()
    assert len(prod) == 334
    assert not any(x["op"].startswith("edgar_") for x in prod)
    exp = generate_grid(include_experimental=True)
    assert len(exp) == 370
    e = [x for x in exp if x["op"].startswith("edgar_")]
    assert len(e) == 36  # 6 算子 × 3 窗口 × 2 方向
    assert all(2 <= x["params"]["n"] <= 250 for x in e)


def test_no_lookahead_and_presample_dropped(panel):
    """面板内事件生效日严格晚于申报日；面板开始前的申报不进入矩阵。"""
    from core.data import build_panel
    p = panel
    ev = _events(p)
    assert len(ev) > 20000
    assert (ev["effective_date"] > ev["filing_date"]).all()
    assert ev["filing_date"].min() >= p["c"].index[0]


def test_all_ops_finite_where_covered(panel):
    """40 个无申报标的应为 NaN（覆盖度掩码），其余必须有限且非全零。"""
    for name, fn in EDGAR_OPS.items():
        m = fn(panel, n=60)
        assert m.shape == panel["c"].shape, name
        v = m.to_numpy()
        nan_rate = np.isnan(v).mean()
        assert 0.05 < nan_rate < 0.08, (name, nan_rate)   # 40/655 ≈ 6.1%
        valid = v[~np.isnan(v)]
        assert np.isfinite(valid).all(), name
        assert (valid > 0).any(), name + " 全零"


def test_no_initial_pulse(panel):
    """面板首日不能堆入 2001-2020 的历史申报（曾造成 2021 均值 5 倍脉冲）。"""
    m = EDGAR_OPS["edgar_filing_count"](panel, n=60)
    yr = m.mean(axis=1).groupby(m.index.year).mean()
    assert yr.loc[2021] / yr.loc[2022] < 1.5, yr.to_dict()


def test_amend_share_and_gap_bounded(panel):
    a = EDGAR_OPS["edgar_amend_share"](panel, n=60).to_numpy()
    assert np.nanmin(a) >= 0.0 and np.nanmax(a) <= 1.0 + 1e-9
    g = EDGAR_OPS["edgar_earnings_gap"](panel, n=60).to_numpy()
    assert np.nanmin(g) >= 0.0 and np.nanmax(g) <= 60.0 + 1e-9
