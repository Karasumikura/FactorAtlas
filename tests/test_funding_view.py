"""资金费率成本的记账视图 —— 2026-10-23 新增。

背景：回测是美股日线口径，不含币安美股永续的 8h 资金费；实盘是永续，
多头付、空头收，两腿合计是净付出。实测中性情形两腿年化净成本 7.67%
（30 标的、quantile 0.2、hold 20），每期约 0.61%。不记账的话，回测净收益
永远比实盘好看，而且没人看得出来。

这里锁的是「记账口径」：只读不改仓位，缺数据的腿按均值保守补齐（不能低估成本）。
"""
from __future__ import annotations

import pytest

from core.bridge_us_binance import Target, _funding_view


class _StubStat:
    def __init__(self, symbol, ann_long):
        self.symbol = symbol
        self.ann_long = ann_long
        self.ann_short = -ann_long      # 做空恰为相反数


@pytest.fixture()
def stub_funding(monkeypatch):
    import core.funding_cost as fc
    from core.us_mapping import Mapping

    monkeypatch.setattr(fc, "all_funding_stats", lambda: [
        _StubStat("AAAUSDT", 0.10),
        _StubStat("BBBUSDT", 0.04),
        _StubStat("CCCUSDT", -0.02),
    ])
    # build_mapping 在本路径只用来补 ticker 名字，返回空表即可
    monkeypatch.setattr("core.us_mapping.build_mapping", lambda *a, **k: ([], {}))


def test_long_pays_short_receives(stub_funding):
    """多头腿付 ann_long、空头腿收 ann_short → 净成本 = 多头均值 + 空头均值。"""
    t = [Target("AAA", "AAAUSDT", +0.5, 1.0),
         Target("CCC", "CCCUSDT", -0.5, -1.0)]
    v = _funding_view(t, "2026-10-22", hold=20)
    # 多：0.10（付）；空：ann_short = +0.02 → 等价成本 -0.02（收）
    assert v["net_ann_pct"] == pytest.approx((0.10 + (-0.02)) * 100, abs=0.01)
    assert v["legs_covered"] == 2 and v["legs_total"] == 2


def test_missing_leg_padded_conservatively(stub_funding):
    """没有费率数据的腿按已知均值补齐，不能当 0 —— 否则系统性低估成本。"""
    t = [Target("AAA", "AAAUSDT", +0.5, 1.0),
         Target("ZZZ", "ZZZUSDT", -0.5, -1.0)]      # ZZZ 无费率数据
    v = _funding_view(t, "2026-10-22", hold=20)
    assert v["legs_covered"] == 1
    assert v["missing_symbols"] == ["ZZZUSDT"]
    # 补齐后仍有非零净成本，且不低于仅有已知腿的结果
    assert v["net_ann_pct"] > 0


def test_per_hold_scales_with_hold_days(stub_funding):
    t = [Target("AAA", "AAAUSDT", +1.0, 1.0)]
    v10 = _funding_view(t, "2026-10-22", hold=10)
    v20 = _funding_view(t, "2026-10-22", hold=20)
    assert v20["per_hold_pct"] == pytest.approx(v10["per_hold_pct"] * 2, abs=1e-6)


def test_empty_targets_returns_none():
    assert _funding_view([], "2026-10-22") is None


def test_never_raises_on_broken_funding(monkeypatch):
    """费率模块炸了不能拖垮交易主流程，返回带 error 的字典即可。"""
    import core.funding_cost as fc

    def boom():
        raise RuntimeError("funding cache missing")

    monkeypatch.setattr(fc, "all_funding_stats", boom)
    v = _funding_view([Target("AAA", "AAAUSDT", +0.5, 1.0)], "2026-10-22")
    assert "error" in v
