"""多周期共振策略测试 —— 验证分层门、评分、分批、止损逻辑。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core import mtf_indicators as I
from core import resonance_strategy as S


def _mk(bars: int = 200, trend: float = 0.0, start: float = 100.0,
        noise: float = 0.5, seed: int = 0) -> pd.DataFrame:
    """造一段带趋势的 OHLCV。trend>0 上升，<0 下降。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=bars, freq="4h", tz="UTC")
    steps = np.arange(bars) * trend + rng.normal(0, noise, bars)
    close = start + steps
    high = close + abs(rng.normal(0, noise, bars))
    low = close - abs(rng.normal(0, noise, bars))
    return pd.DataFrame({"open": close, "high": high, "low": low,
                         "close": close, "volume": 1000.0}, index=idx)


def test_boll_mid_slope():
    up = _mk(trend=0.5)
    dn = _mk(trend=-0.5)
    assert bool(I.boll_slope_up(up["close"]).iloc[-1]) is True
    assert bool(I.boll_slope_up(dn["close"]).iloc[-1]) is False


def test_above_mid():
    up = _mk(trend=0.5)
    assert bool(I.above_mid(up["close"]).iloc[-1]) is True


def test_williams_r_range():
    df = _mk()
    wr = I.williams_r(df["high"], df["low"], df["close"])
    v = wr.dropna()
    assert v.max() <= 0.0 + 1e-9
    assert v.min() >= -100.0 - 1e-9


def test_macd_relation():
    df = _mk(trend=0.4)
    dif, dea, hist = I.macd(df["close"])
    # hist 必须恒等于 dif - dea
    assert np.allclose((dif - dea).dropna(), hist.dropna(), atol=1e-9)


def test_downtrend_no_long_signal():
    """下降趋势：环境不成立，评分应为 wait。"""
    d4 = _mk(trend=-0.8, seed=1)
    d30 = _mk(trend=-0.8, seed=2)
    sig = S.evaluate("TEST", d4, d30)
    assert sig.direction_ok is False
    assert sig.grade == "wait"
    assert sig.entry_trigger is False


def test_uptrend_can_signal():
    """明确上升趋势：方向成立，检查表应有较多项通过。"""
    d4 = _mk(trend=1.0, seed=3)
    d30 = _mk(trend=1.0, seed=4)
    sig = S.evaluate("TEST", d4, d30)
    assert sig.direction_ok is True
    assert sig.score >= 3          # 上升趋势至少通过趋势/中轨/站上中轨等项
    assert 0 <= sig.score <= 8


def test_checks_keys_complete():
    d4 = _mk(trend=0.5)
    d30 = _mk(trend=0.5)
    sig = S.evaluate("TEST", d4, d30)
    assert set(sig.checks.keys()) == {
        "trend_up", "boll_mid_up", "close_above_mid", "macd_golden_above_zero",
        "macd_hist_expanding", "mtm_up", "wr_recover", "m30_confirm"}


def test_exit_on_mid_break():
    """4H 收盘跌破中轨 → 结构止损。"""
    # 先涨后急跌
    up = _mk(bars=150, trend=0.6, seed=5)
    down = _mk(bars=40, trend=-4.0, start=float(up["close"].iloc[-1]), seed=6)
    df = pd.concat([up, down])
    ex, reason = S.should_exit(df)
    assert ex is True
    assert "中轨" in reason or "低点" in reason


def test_no_exit_in_uptrend():
    df = _mk(trend=0.6, seed=7)
    ex, _ = S.should_exit(df)
    assert ex is False


def test_stage_weights_sum_to_one():
    from core.resonance_runner import STAGE_WEIGHTS
    assert abs(sum(STAGE_WEIGHTS) - 1.0) < 1e-9
    assert STAGE_WEIGHTS[0] == pytest.approx(0.30)
    assert STAGE_WEIGHTS[-1] == pytest.approx(0.40)


def test_stage_weight_progression():
    from core.resonance_runner import TARGET_FULL_WEIGHT, _stage_weight
    w1 = _stage_weight(1)
    w2 = _stage_weight(2)
    w3 = _stage_weight(3)
    assert w1 < w2 < w3
    assert w3 == pytest.approx(TARGET_FULL_WEIGHT)
    assert w1 == pytest.approx(TARGET_FULL_WEIGHT * 0.30)


def test_min_notional_filter():
    from core.resonance_runner import _filter_min_notional
    targets = {"AAA": 0.15, "BBB": 0.15, "CCC": 0.0}
    prices = {"AAA": 10.0, "BBB": 1000.0}   # BBB: 0.15*100=15 ok; 换个小的
    # equity=100: AAA=15, BBB=1500 都过; 造一个过不了的
    targets["DDD"] = 0.001
    prices["DDD"] = 10.0                     # 0.001*100=0.1 < 5 → 剔除
    kept, dropped = _filter_min_notional(targets, 100.0, prices, min_notional=5.0)
    assert "DDD" in dropped
    assert "AAA" in kept
    assert "CCC" in kept                     # 零目标（平仓）保留
