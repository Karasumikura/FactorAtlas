"""hold>1 时 survivor gate 的真实调仓 Sharpe 口径。

evaluate_with_split 把重叠 H 日收益按日采样后乘 sqrt(252)，hold>1 时
Sharpe 膨胀约 √hold。实测 hold=5 的一轮 mine_us.json：15 个 survivor 的
oos_ls_sharpe 中位 3.98 / 最大 10.30，10/15 超过 2，其中
news_tone_dispersion_n10 只有 13 个有效观测却报 10.30。

低频块虽用 ls_nav_true_rebalance，但只在 market == "ashare" 且 do_lowfreq
时跑，结果也没回灌到 survivor 判定。这里按 train_frac 把 panel 切成
IS/OOS 两段，各自跑真实调仓 NAV，拿到不重叠期收益的 Sharpe。

hold=1 时完全不介入（原口径合法，历史缓存也不受影响）。
"""
from __future__ import annotations

import math

from core.ashare_backtest import ls_nav_true_rebalance
from core.backtest import split_periods

COST_BPS = 30.0


def _slice(panel: dict, dates) -> dict:
    return {k: v.loc[dates] for k, v in panel.items() if hasattr(v, "loc")}


def split_true_sharpe(panel: dict, spec: dict, hold: int,
                      train_frac: float = 0.7,
                      cost_bps: float = COST_BPS):
    """返回 (is_sharpe, oos_sharpe, n_is_periods, n_oos_periods)。

    任一段算不出来就返回 nan —— 调用方必须拒绝该候选，
    不能拿膨胀值兜底。宁可少报，不虚报。
    """
    tr, te = split_periods(panel, train_frac)
    if len(tr) < hold * 3 or len(te) < hold * 3:
        return (math.nan, math.nan, 0, 0)
    out = []
    for dates in (tr, te):
        try:
            nav = ls_nav_true_rebalance(
                _slice(panel, dates), spec, hold=hold,
                cost_bps=cost_bps, stamp_tax_bps=0.0, apply_limit=False)
            # period_returns 是 numpy array，不能用 `or []`（真值歧义会抛
            # ValueError 被下面 except 吞掉，结果全变 nan）。early-return
            # 的 dict 根本没有这个键，同样要判成「算不出来」。
            pr = nav.get("period_returns")
            if pr is None:
                out.append((math.nan, 0))
                continue
            sh = float(nav.get("sharpe", math.nan))
            # 少于 3 个调仓周期的 Sharpe 是噪声，不能用
            out.append((sh if len(pr) >= 3 else math.nan, len(pr)))
        except Exception:
            out.append((math.nan, 0))
    return (out[0][0], out[1][0], out[0][1], out[1][1])
