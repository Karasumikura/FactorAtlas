"""播报盘名标签：两盘播报标题必须可区分（低频因子盘 / 共振因子盘）。"""
from __future__ import annotations

from core.notify import build_message, _book_label


def _rec(**kw):
    base = {
        "ts": "2026-10-15T20:53:11+00:00",
        "executed": True,
        "trigger_reasons": ["price_anomaly"],
        "llm_decision": "无",
        "signal_date": "2026-10-13",
        "orders": [],
        "results": [{"status": "sent", "symbol": "GOOGLUSDT", "side": "BUY",
                     "qty": 0.03, "ref_price": 180.0}],
        "current_positions": {"GOOGLUSDT": 0.03},
        "unrealized_pnl": -0.45,
    }
    base.update(kw)
    return base


def test_lowfreq_label():
    assert _book_label({"book": "lowfreq"}) == "低频因子盘"
    assert _book_label({}) == "低频因子盘"          # 缺省 → 低频


def test_resonance_label():
    assert _book_label({"book": "resonance"}) == "共振因子盘"


def test_message_header_has_book_label():
    low = build_message(_rec(book="lowfreq"))
    res = build_message(_rec(book="resonance"))
    assert "低频因子盘" in low
    assert "共振因子盘" in res
    assert low.splitlines()[0] != res.splitlines()[0]   # 标题必须不同


def test_unknown_book_shows_raw_name():
    assert _book_label({"book": "mystery"}) == "mystery"


def test_drift_message_format():
    rec = _rec(book="resonance", status="reconcile_drift",
               note="持仓漂移 1 腿，已同步本地状态",
               reconcile_drift=[{"symbol": "MUUSDT", "exchange_qty": 0.05,
                                 "local_qty": 0.01, "delta_notional": 7.2}])
    msg = build_message(rec)
    assert "持仓漂移" in msg
    assert "共振因子盘" in msg
    assert "MUUSDT" in msg
    assert "执行 0 笔" not in msg        # 漂移告警不该显示执行笔数


def test_executed_message_still_shows_fills():
    msg = build_message(_rec(book="lowfreq"))
    assert "执行 1 笔" in msg
    assert "GOOGLUSDT" in msg
