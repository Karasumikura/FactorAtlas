"""调仓日历（rebalance calendar）测试。

目标：验证 TriggerEngine 的「累计漂移 + 最小间隔」闸门——
避免低频因子在 5 分钟 tick 下被跑成高频碎单。
"""
from types import SimpleNamespace

from core.trigger_engine import check_triggers, DEFAULT_THRESHOLDS


def _order(sym, notional, action="BUY"):
    return SimpleNamespace(binance_symbol=sym, action=action,
                           delta_qty=1.0, notional=notional)


def _call(orders, *, equity=1000.0, pnl=0.0, last_ts=None, q=None):
    return check_triggers(
        targets=[], prices={}, cur_positions={}, equity=equity,
        unrealized_pnl=pnl, orders=orders,
        last_rebalance_ts=last_ts, price_history=None,
        q=q or {},
    )


def test_single_small_leg_suppressed_when_aggr_below_threshold():
    """单腿刚越死区，但全组合累计漂移很小 → 不触发，避免碎单。"""
    # equity=1000，死区门槛 = max(5*2, 0.05*1000)=50。单笔 60 越死区。
    # 但累计仅 60/1000 = 6% < 10% aggr_drift_pct → 应被日历闸住。
    r = _call([_order("MSTRUSDT", 60.0)])
    assert "needs_rebalance" not in r.reasons
    assert "rebalance_suppressed" in r.evidence
    assert r.evidence["rebalance_suppressed"]["aggr_ok"] is False


def test_aggr_above_threshold_fires_when_interval_ok():
    """累计漂移达标 + 距上次调仓已超最小间隔 → 触发调仓。"""
    # 累计 300/1000 = 30% >= 10%；且无 last_ts（视为间隔 OK）。
    r = _call([_order("A", 150.0), _order("B", 150.0)])
    assert "needs_rebalance" in r.reasons
    ev = r.evidence["needs_rebalance"]
    assert ev["aggr_drift_pct"] >= DEFAULT_THRESHOLDS["aggr_drift_pct"]
    assert ev["n_orders"] == 2


def test_interval_gate_suppresses_even_when_drift_big():
    """累计漂移很大，但距上次调仓太近 → 仍不触发（间隔闸生效）。"""
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    r = _call([_order("A", 400.0), _order("B", 400.0)], last_ts=recent)
    assert "needs_rebalance" not in r.reasons
    sup = r.evidence["rebalance_suppressed"]
    assert sup["interval_ok"] is False
    assert sup["aggr_ok"] is True


def test_interval_elapsed_allows_fire():
    """距上次调仓超间隔 → 放行。"""
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    r = _call([_order("A", 400.0), _order("B", 400.0)], last_ts=old)
    assert "needs_rebalance" in r.reasons


def test_drawdown_override_bypasses_calendar():
    """回撤告警激活时，即使累计漂移不足、间隔未到，也必须放行降险。"""
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    # pnl = -80 / equity 1000 = -8% >= drawdown_pct(5%) → override
    r = _call([_order("A", 60.0)], pnl=-80.0, last_ts=recent)
    assert "needs_rebalance" in r.reasons
    assert "drawdown_alert" in r.reasons


def test_calendar_disabled_by_zero_thresholds():
    """aggr_drift_pct=0 且 interval=0 → 退化为旧行为（任一实质单即触发）。"""
    r = _call([_order("A", 60.0)],
              q={"aggr_drift_pct": 0.0, "min_rebalance_interval_min": 0.0})
    assert "needs_rebalance" in r.reasons


def test_below_deadband_never_triggers():
    """低于死区门槛的单根本不进 meaningful → 也不触发。"""
    r = _call([_order("A", 10.0)])  # < max(10, 50)=50
    assert "needs_rebalance" not in r.reasons
    assert "rebalance_suppressed" not in r.evidence


# --- 价格异动触发：零波动窗（Demo 僵尸标的）不得炸掉整个 tick ---

def _anomaly_call(px, hist):
    return check_triggers(
        targets=[], prices={"XYZUSDT": px}, cur_positions={}, equity=100.0,
        unrealized_pnl=0.0, orders=[], last_rebalance_ts=None,
        price_history={"XYZUSDT": hist}, q={},
    )


def test_price_anomaly_skips_zero_variance_window():
    """全平的滚动历史（Demo 的 XYZUSDT 恒 2535）→ 跳过，不触发、不抛异常。

    背景：这些静止报价会进 price_history，若在 sd==0 上做除法，
    trader_daemon 整个 tick 抛 ZeroDivisionError → 落地成 signal_error，交易端停摆。
    """
    r = _anomaly_call(2535.0, [2535.0] * 30)
    assert "price_anomaly" not in r.reasons


def test_price_anomaly_still_fires_on_real_move():
    """有波动的窗口照常判定，修复不能把正常异动一起关掉。"""
    r = _anomaly_call(2600.0, [2535.0] * 29 + [2500.0])
    assert "price_anomaly" in r.reasons


def test_price_anomaly_ignores_nan_history():
    """历史里混入 nan 时不应把窗口均值污染成 nan 后误判/崩溃。"""
    r = _anomaly_call(2535.0, [float("nan")] * 30)
    assert "price_anomaly" not in r.reasons

