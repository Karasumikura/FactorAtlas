"""news_adjust 的单元测试：新闻修正层的边界与契约。

重点验证（这些是本模块最容易被改坏的地方）：
  1. LLM 永远不能产出负仓位（bias=-1 再狠也只能到 0，不翻空）
  2. 强度/置信度不足时必须忽略，不能因为噪声天天调仓
  3. 过期判断必须失效（horizon_days 到期后不该继续影响仓位）
  4. 没有新闻的票必须原样通过（×1.0），不能凭空改动
  5. 宏观与个股两级相乘，且宏观不能太激进
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from core import news_adjust as na


def _verdict(symbol="MU", bias=1, mag=0.5, conf=0.8, horizon=2, hours_ago=1.0):
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "ts": ts.isoformat(),
        "kind": "stock",
        "symbol": symbol,
        "bias": bias,
        "magnitude": mag,
        "confidence": conf,
        "horizon_days": horizon,
    }


def _macro(score=-1.0, regime="RISK_OFF", horizon=2, hours_ago=1.0):
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "ts": ts.isoformat(),
        "kind": "macro",
        "regime": regime,
        "score": score,
        "horizon_days": horizon,
    }


# ── 个股乘数 ──────────────────────────────────────────────

def test_neutral_bias_never_moves():
    mult, _ = na.stock_multiplier(_verdict(bias=0, mag=1.0, conf=1.0))
    assert mult == 1.0


def test_bearish_never_goes_negative():
    """bias=-1 拉满也只能压到 0，绝不产生反向仓位。"""
    mult, _ = na.stock_multiplier(_verdict(bias=-1, mag=1.0, conf=1.0))
    assert mult >= 0.0
    assert mult == pytest.approx(1 - na.K_STOCK, abs=1e-6)


def test_bullish_capped_at_two():
    mult, _ = na.stock_multiplier(_verdict(bias=1, mag=1.0, conf=1.0))
    assert mult <= 2.0
    assert mult == pytest.approx(1 + na.K_STOCK, abs=1e-6)


def test_low_magnitude_ignored():
    mult, reason = na.stock_multiplier(_verdict(mag=na.MIN_MAGNITUDE - 0.01, conf=1.0))
    assert mult == 1.0 and reason == ""


def test_low_confidence_ignored():
    mult, reason = na.stock_multiplier(_verdict(mag=1.0, conf=na.MIN_CONFIDENCE - 0.01))
    assert mult == 1.0 and reason == ""


def test_missing_verdict_is_neutral():
    mult, reason = na.stock_multiplier(None)
    assert mult == 1.0 and reason == ""


# ── 宏观乘数 ──────────────────────────────────────────────

def test_macro_weak_score_ignored():
    mult, _ = na.macro_multiplier(_macro(score=na.MACRO_MIN_SCORE - 0.01))
    assert mult == 1.0


def test_macro_neutral_band_ignored():
    """±0.2 这种噪声级情绪不该动仓位。"""
    for sc in (-0.2, 0.0, 0.2):
        mult, _ = na.macro_multiplier(_macro(score=sc))
        assert mult == 1.0


def test_macro_extreme_bounded():
    lo, _ = na.macro_multiplier(_macro(score=-1.0))
    hi, _ = na.macro_multiplier(_macro(score=1.0))
    assert 0.6 <= lo < 1.0
    assert 1.0 < hi <= 1.25


# ── 过期判断 ──────────────────────────────────────────────

def test_expired_verdict_not_loaded(tmp_path, monkeypatch):
    """horizon_days=1 且已过去 3 天 -> 不该再被 load_latest 读到。"""
    log = tmp_path / "verdicts.jsonl"
    monkeypatch.setattr(na, "VERDICT_LOG", log)
    log.write_text(json.dumps(_verdict(horizon=1, hours_ago=72)) + "\n",
                   encoding="utf-8")
    macro, per = na.load_latest(lookback_hours=10_000)
    assert per == {}


def test_fresh_verdict_loaded(tmp_path, monkeypatch):
    log = tmp_path / "verdicts.jsonl"
    monkeypatch.setattr(na, "VERDICT_LOG", log)
    log.write_text(json.dumps(_verdict(horizon=3, hours_ago=1)) + "\n",
                   encoding="utf-8")
    macro, per = na.load_latest()
    assert "MU" in per


def test_latest_wins(tmp_path, monkeypatch):
    """同一只票多条判断，取 ts 最新的。"""
    log = tmp_path / "verdicts.jsonl"
    monkeypatch.setattr(na, "VERDICT_LOG", log)
    old = _verdict(bias=-1, hours_ago=5)
    new = _verdict(bias=1, hours_ago=1)
    log.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
    _, per = na.load_latest()
    assert per["MU"]["bias"] == 1


# ── 组合 ──────────────────────────────────────────────────

def test_no_news_passes_through():
    """没有新闻的票必须原样通过。"""
    targets = {"SPY": 0.01, "QQQ": 0.02}
    out, detail = na.apply_to_targets(targets, per_symbol={}, exclude_macro=True)
    assert out == targets
    assert detail == {}


def test_apply_does_not_add_new_symbols():
    """新闻不能凭空新增仓位 —— 选股仍归因子层。"""
    out, _ = na.apply_to_targets({"MU": 0.05}, macro=None,
                                 per_symbol={"NVDA": _verdict(symbol="NVDA", mag=1.0)})
    assert set(out) == {"MU"}


def test_stock_and_macro_multiply():
    per = {"MU": _verdict(bias=1, mag=1.0, conf=1.0)}
    adj = na.get_adjustment("MU", macro=_macro(score=-1.0), per_symbol=per)
    expected = (1 + na.K_STOCK) * (1 + (-1.0) * na.K_MACRO)
    assert adj["multiplier"] == pytest.approx(expected, abs=1e-3)
    assert adj["applied"] is True


def test_usdt_suffix_stripped():
    """传 MUUSDT 也能命中 MU 的判断。"""
    per = {"MU": _verdict(bias=1, mag=1.0, conf=1.0)}
    adj = na.get_adjustment("MUUSDT", macro=None, per_symbol=per)
    assert adj["applied"] is True
