"""回撤熔断余量（scripts/ops_watch 的「距熔断 N pp」）。

2026-11-02 加。起因：巡检只报「最大回撤 10.38%」，看不出账户是刚从熔断线上
退下来、还是正往线上撞 —— 差的是「还剩几 pp」这一个数。

三档都要钉住：有余量 / 已越线（负号最容易印成 `距熔断-0.38pp`）/ 无 dd 数据。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.ops_watch as w  # noqa: E402


def _risk(dd, alert=38):
    return {"breakers": {}, "derisk": 2, "drift": 0, "drawdown_alert": alert,
            "dd_loss_pct": dd, "dd_books": {"lowfreq": alert},
            "dd_latest": None, "dd_breach_no_derisk": 0}


@pytest.fixture()
def snap(monkeypatch):
    """把所有 I/O 打桩，只留告警拼装与渲染。"""
    stub = {
        "_mem": lambda: {"avail_mb": 604},
        "_disk": lambda: {"free_gb": 5.5, "used_pct": 82.3},
        "_svc_detail": lambda s: {"active": True},
        "_last_trader_tick": lambda: {"found": True, "age_sec": 234, "status": "no_action"},
        "_miner_state": lambda: {"running": False, "latest_loop": "L", "latest_round": "R"},
        "_trial_and_library": lambda: {"cumulative_trials": 1, "n_sessions": 1, "library_factors": 1},
        "_signal_staleness": lambda d: {"stale": False},
        "_exec_rejections": lambda: {"n_recent_errors": 0},
        "_llm_health": lambda: {"reachable": True, "n_consecutive_fallback": 0},
        "_mine_channel_health": lambda: {"markets": {}},
        "_data_freshness": lambda: {"crypto": {"lag_days": 1}},
        "_book_silence": lambda: {},
    }
    for name, fn in stub.items():
        monkeypatch.setattr(w, name, fn)

    def run(risk):
        monkeypatch.setattr(w, "_risk_recent", lambda: risk)
        rep = w.snapshot()
        line = next(l for l in w._render(rep).splitlines() if l.startswith("风控"))
        alert = next(a for a in rep["alerts"] if "回撤警戒" in a)
        return line, alert

    return run


def test_headroom_below_breaker(snap):
    line, alert = snap(_risk(8.79))
    assert "距熔断1.21pp" in line
    assert "仅剩 1.21pp" in alert


def test_crossed_breaker_has_no_negative_sign(snap):
    line, alert = snap(_risk(10.38))
    assert "已越熔断0.38pp" in line
    assert "-0.38" not in line
    assert "已越熔断线" in alert


def test_missing_dd_degrades_quietly(snap):
    line, _ = snap(_risk(None))
    assert "距熔断" not in line and "已越熔断" not in line
