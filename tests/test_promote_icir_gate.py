"""晋升门槛的「信号强度」(ICIR) 判据 —— 2026-10-23 新增。

为什么加：
  原门槛只看 OOS LS Sharpe 中位数 >= 0.3。可 Sharpe 对低波动、低换手的因子
  太宽松：几十个交易日里只选中 3 只票，也能把 Sharpe 凑过线。
  实测库里 14 个因子有 6 个 |ICIR| < 0.03（其中一个样本外年化 Sharpe 为 -0.11），
  却因为过了 Sharpe 门槛而全部等权投票进了合成信号。

判据用 ICIR（IC 均值 / IC 波动）而不是 IC 均值：
  ICIR 已经把「IC 小但极稳」和「IC 大但忽正忽负」区分开了，不会误杀真信号。

注意：本文件只测 ICIR 这一个判据，其余闸门一律用 BASE 显式钉住旧值，
避免哪天默认值收紧（2026-11-09 就收紧过一次）把这里的断言连坐。
"""
from __future__ import annotations

from core.promote import evaluate

# 只留 ICIR 变量，其它闸门全部放开，避免默认值漂移影响本文件的语义。
BASE = dict(min_runs=2, dsr_floor=0.95, pbo_ceiling=0.5,
            min_oos_sharpe=0.0, min_abs_ic=0.0)


def _ev(acc, **kw):
    return evaluate(acc, n_trials_cumulative=100, **{**BASE, **kw})[0]


def _acc(icir, oos=0.8, sessions=3):
    """构造一个「除 ICIR 外全部达标」的候选。"""
    apps = [{"oos_ls_sharpe": oos, "ls_sharpe": oos, "net_ls_sharpe": oos,
             "ic_mean": 0.01, "icir": icir, "oos_ic_mean": 0.01,
             "dsr": {"dsr": 0.99}, "pbo": {"pbo": 0.1}}
            for _ in range(sessions)]
    return {"sid-x": {"name": "probe", "spec": {"op": "mom", "params": {"n": 20}},
                      "appearances": apps, "n_sessions": sessions,
                      "n_distinct_oos": sessions}}


def test_weak_icir_is_rejected():
    v = _ev(_acc(0.012), min_abs_icir=0.03)
    assert v["checks"]["icir_ok"] is False
    assert v["promoted"] is False
    assert v["icir_median"] == 0.012


def test_strong_icir_passes():
    v = _ev(_acc(0.08), min_abs_icir=0.03)
    assert v["checks"]["icir_ok"] is True
    assert v["promoted"] is True


def test_negative_icir_uses_absolute_value():
    """方向已由 direction 字段处理，ICIR 比的是强度，取绝对值。"""
    v = _ev(_acc(-0.09), min_abs_icir=0.03)
    assert v["checks"]["icir_ok"] is True


def test_threshold_is_configurable():
    v = _ev(_acc(0.02), min_abs_icir=0.01)
    assert v["checks"]["icir_ok"] is True


def test_missing_icir_does_not_kill_legacy_results():
    """老挖掘结果没有 icir 字段 → 不能因为缺字段就全判死，应放行并标记 None。"""
    v = _ev({"sid-y": {
        "name": "legacy", "spec": {"op": "mom", "params": {"n": 20}},
        "appearances": [{"oos_ls_sharpe": 0.8, "net_ls_sharpe": 0.8,
                         "dsr": {"dsr": 0.99}, "pbo": {"pbo": 0.1}}] * 3,
        "n_sessions": 3, "n_distinct_oos": 3}}, min_abs_icir=0.03)
    assert v["checks"]["icir_ok"] is None
    assert v["promoted"] is True
