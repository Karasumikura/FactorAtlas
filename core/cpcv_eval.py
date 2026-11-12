#!/usr/bin/env python3
"""CPCV 多路径 OOS：把「一次 70/30 split 给一个 Sharpe」换成 Sharpe 分布。

算法抄自 eslazarev/purged-cross-validation（MIT，vendored 在 core/_vendor/purgedcv/，
来源与许可证见 README）。Quant.SE 54927 的判据：k-fold / walk-forward 只给
一个 Sharpe，CPCV 通过 C(N,K) 组合折 + purge + embargo 给出 Sharpe 分布。

本项目用法：对 hold>1 的因子，每个 test 折单独跑 ls_nav_true_rebalance
（真实调仓口径，不用 evaluate_with_split 的 ~√hold 膨胀值），汇总分布。
纯评估、不新增候选 → 不抬 overfit_guard 的 N。

用法：venv/bin/python -c "from core.cpcv_eval import cpcv_sharpe_distribution; ..."
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJ = Path("/opt/quant-lab")
_VENDOR = str(PROJ / "core" / "_vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)


def cpcv_sharpe_distribution(panel: dict, spec: dict, hold: int = 5,
                             n_splits: int = 6, n_test_groups: int = 2,
                             cost_bps: float = 30.0) -> dict:
    """返回 CPCV 多路径 OOS Sharpe 分布；失败返回带 error 的 dict。"""
    from purgedcv import CombinatorialPurgedCV
    from core.ashare_backtest import ls_nav_true_rebalance

    dates = panel["c"].index
    n = len(dates)
    if n < n_splits * 20:
        return {"error": "panel too short", "n_days": n}

    # label 在 t+hold 个交易日后结算 → 评估时点按交易日平移（不是日历日）
    step_days = int(np.median([
        (dates[min(i + hold, n - 1)] - dates[i]).days
        for i in range(0, n - hold, max(1, (n - hold) // 200))
    ])) or hold
    horizon = pd.Timedelta(days=max(1, step_days))
    eval_times = pd.Series(
        [dates[i] + horizon if i + hold < n else dates[-1] for i in range(n)])

    cv = CombinatorialPurgedCV(
        n_splits=n_splits, n_test_groups=n_test_groups,
        prediction_times=pd.Series(dates), evaluation_times=eval_times,
        purge_horizon=horizon, embargo=horizon)

    sharpes: list[float] = []
    folds = 0
    for _train_idx, test_idx in cv.split(np.arange(n)):
        folds += 1
        sub = {k: v.iloc[test_idx] for k, v in panel.items()}
        try:
            nav = ls_nav_true_rebalance(sub, spec, hold=hold,
                                        cost_bps=cost_bps, apply_limit=False)
            sh = float(nav.get("sharpe", float("nan")))
            if sh == sh and np.isfinite(sh):
                sharpes.append(sh)
        except Exception:  # noqa: BLE001
            continue
    del cv

    if not sharpes:
        return {"error": "no valid fold", "folds": folds, "n_days": n}
    a = np.asarray(sharpes, dtype=float)
    return {
        "n_folds": folds,
        "n_valid": int(a.size),
        "sharpe_mean": round(float(a.mean()), 4),
        "sharpe_std": round(float(a.std(ddof=1)), 4) if a.size > 1 else 0.0,
        "sharpe_min": round(float(a.min()), 4),
        "sharpe_max": round(float(a.max()), 4),
        "sharpe_p25": round(float(np.percentile(a, 25)), 4),
        "sharpe_median": round(float(np.median(a)), 4),
        "sharpe_p75": round(float(np.percentile(a, 75)), 4),
        "negative_share": round(float((a < 0).mean()), 3),
        "hold": hold,
        "purge_horizon_days": step_days,
    }
