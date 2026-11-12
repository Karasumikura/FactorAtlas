"""晋升门槛的「IC 方向/幅度」判据 —— 2026-11-09 新增。

为什么加：
  `direction_consistent` 只看 OOS LS Sharpe 的符号是否一致，抓不到
  「IC 与 spec.direction 反着」的因子。实测正式库里混进 4 个负 IC 因子
  （最差 intraday_amplitude_risk_flip, IC=-0.036，只被 3 次采样验证），
  它们全靠 Sharpe 符号一致蒙过闸门。

  base 候选：2026-11-08 回放显示，旧阈值下 137 个通过里有 26 个 IC<=0、
  44 个 |IC|<0.02；加上本判据后剩 74 个，IC 中位数从 +0.0237 升到 +0.0322。
"""
from __future__ import annotations

from core.promote import evaluate

# 只留 IC 变量，其余闸门放开，避免默认值漂移影响本文件语义。
BASE = dict(min_runs=2, dsr_floor=0.95, pbo_ceiling=0.5,
            min_oos_sharpe=0.0, min_abs_icir=0.0)


def _acc(oos_ic, direction=1, sessions=3, oos=0.8):
    apps = [{"oos_ls_sharpe": oos, "ls_sharpe": oos, "net_ls_sharpe": oos,
             "ic_mean": oos_ic, "icir": 0.2, "oos_ic_mean": oos_ic,
             "dsr": {"dsr": 0.99}, "pbo": {"pbo": 0.1}}
            for _ in range(sessions)]
    return {"sid-ic": {"name": "probe",
                       "spec": {"op": "mom", "params": {"n": 20}, "direction": direction},
                       "appearances": apps, "n_sessions": sessions,
                       "n_distinct_oos": sessions}}


def _ev(acc, **kw):
    return evaluate(acc, n_trials_cumulative=100, **{**BASE, **kw})[0]


def test_ic_sign_must_match_direction():
    """IC 为负但 direction=1 → 拒绝，且 ic_ok 明确为 False。"""
    v = _ev(_acc(-0.03, direction=1), min_abs_ic=0.01)
    assert v["checks"]["ic_ok"] is False
    assert v["promoted"] is False


def test_negative_ic_with_flipped_direction_passes():
    """把 direction 改成 -1，同样的负 IC 就是方向正确。"""
    v = _ev(_acc(-0.03, direction=-1), min_abs_ic=0.01)
    assert v["checks"]["ic_ok"] is True
    assert v["promoted"] is True


def test_ic_too_weak_is_rejected():
    """|IC| 低于下限（信号太弱）→ 拒绝。"""
    v = _ev(_acc(0.004, direction=1), min_abs_ic=0.01)
    assert v["checks"]["ic_ok"] is False
    assert v["promoted"] is False


def test_strong_ic_passes():
    v = _ev(_acc(0.04, direction=1), min_abs_ic=0.01)
    assert v["checks"]["ic_ok"] is True
    assert v["promoted"] is True


def test_missing_ic_does_not_kill_legacy_results():
    """老挖掘结果没有 oos_ic_mean 字段 → 放行并标记 None，不能一刀切死。"""
    acc = _acc(0.04)
    for a in acc["sid-ic"]["appearances"]:
        a.pop("oos_ic_mean")
    v = _ev(acc, min_abs_ic=0.01)
    assert v["checks"]["ic_ok"] is None
    assert v["promoted"] is True


def test_gate_can_be_disabled():
    """min_abs_ic <= 0 → 关闭本判据。"""
    v = _ev(_acc(-0.03, direction=1), min_abs_ic=0.0)
    assert v["checks"]["ic_ok"] is True
    assert v["promoted"] is True
