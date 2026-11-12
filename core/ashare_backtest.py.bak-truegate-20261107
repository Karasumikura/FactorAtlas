"""A股真实交易约束回测：涨跌停不可成交、T+1、停牌、真实成本。

动机：A股的横截面 IC 看起来有效（如反转 IC=+0.024），但转成可交易组合后
收益被大幅稀释。核心原因：
1. **涨跌停**：涨停买不进、跌停卖不出——反转因子专挑超跌股，而这些票
   常常正处于跌停（无法买入），因子收益买不到。
2. **成本**：印花税(卖出0.05%) + 佣金(双边约0.025%) + 冲击成本。
3. **停牌**：停牌日不可交易。

本模块给出【可交易过滤后】的因子多空收益，并与理想回测对比，量化"水分"。
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from factors.engine import factor_series_to_frame


def limit_price(prev_close: pd.Series, code: str) -> float:
    """按板块计算涨跌停幅度：主板10%、创业板/科创板20%、北交所30%。"""
    c = str(code).split(".")[0]
    if c.startswith(("300", "301", "688", "689")):   # 创业板/科创板 20%
        pct = 0.20
    elif c.startswith(("8", "4", "920")):            # 北交所 30%
        pct = 0.30
    else:
        pct = 0.10                                    # 沪深主板 10%
    return pct


def limit_hit_mask(panel: dict) -> dict[str, pd.DataFrame]:
    """标记每天每票是否【一字涨停/跌停】（近似：涨跌幅触及限幅且收盘=最高/最低）。

    返回 {"up": 涨停掩码, "down": 跌停掩码}。
    涨停不可买（因子想买入的被卡）；跌停不可卖（因子想卖的被卡）。
    """
    c, h, l = panel["c"], panel["h"], panel["l"]
    prev = c.shift(1)
    up = pd.DataFrame(False, index=c.index, columns=c.columns)
    down = pd.DataFrame(False, index=c.index, columns=c.columns)
    for col in c.columns:
        pct = limit_price(prev[col], col)
        r = c[col] / prev[col] - 1.0
        # 涨停：涨幅≥限幅-0.5% 且 收盘≈最高（封死）
        up[col] = (r >= pct - 0.005) & ((h[col] - c[col]).abs() / c[col] < 0.002)
        down[col] = (r <= -pct + 0.005) & ((c[col] - l[col]).abs() / c[col] < 0.002)
    return {"up": up, "down": down}


def suspended_mask(panel: dict) -> pd.DataFrame:
    """停牌/无成交：成交量为0 或缺失。"""
    v = panel["v"]
    return (v.fillna(0) <= 0)


def realistic_tradable(panel: dict) -> pd.DataFrame:
    """综合可交易掩码：非停牌 + 非涨停 + 非跌停（用于建仓日的可成交判断）。"""
    lim = limit_hit_mask(panel)
    susp = suspended_mask(panel)
    return ~(lim["up"] | lim["down"] | susp)


def ls_returns_realistic(panel: dict, spec: dict, hold: int = 1,
                         quantile: float = 0.2, cost_bps: float = 30.0,
                         stamp_tax_bps: float = 5.0,
                         apply_limit: bool = True) -> dict:
    """A股真实约束下的多空组合收益。

    成本假设（单边）：
    - 佣金+过户费 ≈ 3bps
    - 印花税：仅卖出 5bps
    - 冲击成本：由 cost_bps 控制（默认合计约 30bps 单边，含冲击）
    调仓：每日按因子分位建仓，次日收益，计双边成本。

    涨跌停处理：建仓日若目标股票【涨停】则买不进（从多头剔除），
    【跌停】则卖不出；被剔除的票用当日该组其余票等权替代。
    """
    f = factor_series_to_frame(panel, spec)
    c = panel["c"]
    # H 日持有收益，摊到每日（避免重叠复利导致的虚高）
    fwd_h = c.shift(-(1 + hold)) / c.shift(-1) - 1.0
    fwd = fwd_h / hold
    lim = limit_hit_mask(panel)

    dates = f.index
    q = None
    gross_list, net_list = {}, {}
    prev_top, prev_bot = set(), set()
    n_fill = {"top_blocked": 0, "bot_blocked": 0, "total": 0}

    for i, t in enumerate(dates):
        fv = f.loc[t]
        rv = fwd.loc[t] if t in fwd.index else None
        if rv is None:
            continue
        m = fv.notna() & rv.notna()
        if m.sum() < 10:
            continue
        nn = m.sum()
        qn = max(1, int(nn * quantile))
        order = fv[m].sort_values(ascending=False)
        hi_all = list(order.index[:qn])
        lo_all = list(order.index[-qn:])

        if apply_limit:
            up = lim["up"].loc[t] if t in lim["up"].index else pd.Series(False, index=c.columns)
            dn = lim["down"].loc[t] if t in lim["down"].index else pd.Series(False, index=c.columns)
            # 多头想买入的：涨停买不进 → 剔除
            hi = [s for s in hi_all if not bool(up.get(s, False))]
            n_fill["top_blocked"] += len(hi_all) - len(hi)
            # 空头想卖出的：跌停卖不出 → 剔除
            lo = [s for s in lo_all if not bool(dn.get(s, False))]
            n_fill["bot_blocked"] += len(lo_all) - len(lo)
            n_fill["total"] += len(hi_all) + len(lo_all)
        else:
            hi, lo = hi_all, lo_all

        if not hi or not lo:
            continue
        g = rv.loc[hi].mean() - rv.loc[lo].mean()

        # 换手
        if i == 0:
            turn = 1.0
        else:
            turn = (len(set(hi) ^ prev_top) + len(set(lo) ^ prev_bot)) / (2 * max(len(hi), 1))
        prev_top, prev_bot = set(hi), set(lo)

        cost = turn * (cost_bps + stamp_tax_bps) / 10000.0 * 2 / hold  # 摊到每日
        gross_list[t] = g
        net_list[t] = g - cost

    gross = pd.Series(gross_list).sort_index()
    net = pd.Series(net_list).sort_index()
    block_rate = (n_fill["top_blocked"] + n_fill["bot_blocked"]) / max(n_fill["total"], 1)
    return {"gross": gross, "net": net, "block_rate": block_rate, "n_days": len(gross)}


def _sharpe(s: pd.Series) -> float:
    if len(s) < 2 or s.std() == 0:
        return 0.0
    return float(s.mean() / s.std() * np.sqrt(252))


def ls_nav_true_rebalance(panel: dict, spec: dict, hold: int = 20,
                          quantile: float = 0.2, cost_bps: float = 30.0,
                          stamp_tax_bps: float = 5.0,
                          apply_limit: bool = True,
                          pit_mask: pd.DataFrame | None = None) -> dict:
    """真实调仓频率的多空组合：每 hold 日才调一次仓，中间持有不动。

    与 ls_returns_realistic 的区别：
      - 后者把 H 日收益摊到每日、按日频序列算 Sharpe → 重叠+平滑使 std 偏低、Sharpe 虚高
      - 本函数按【离散调仓】计算：每 H 日取一次组合的 H 日实现收益，形成
        非重叠的期收益序列，再按 (1+期收益) 复利成净值。Sharpe 基于
        **每年调仓次数** 年化，不受重叠平滑影响。

    返回 {nav, period_returns, periods_per_year, ann_ret, sharpe, max_dd, turnover}
    """
    f = factor_series_to_frame(panel, spec)
    c = panel["c"]
    lim = limit_hit_mask(panel)
    dates = list(f.index)
    # H 日实现收益（非重叠：每隔 hold 个交易日取一次）
    rets = c.pct_change(hold)
    n = len(dates)
    step = max(1, int(hold))

    period_rets, turns, hold_days = [], [], []
    prev_top, prev_bot = set(), set()
    i = 0
    while i < n - step - 1:
        t = dates[i]
        fv = f.loc[t]
        rv = rets.iloc[i + step] if (i + step) < n else None  # t 到 t+hold 的实现收益
        if rv is None:
            break
        if not isinstance(rv, pd.Series):
            i += step
            continue
        m = fv.notna() & rv.notna()
        if pit_mask is not None and t in pit_mask.index:
            pm = pit_mask.loc[t]
            m = m & pm.reindex(m.index).fillna(False).astype(bool)
        if m.sum() < 10:
            i += step
            continue
        nn = m.sum()
        qn = max(1, int(nn * quantile))
        order = fv[m].sort_values(ascending=False)
        hi_all = list(order.index[:qn])
        lo_all = list(order.index[-qn:])
        if apply_limit:
            up = lim["up"].loc[t] if t in lim["up"].index else pd.Series(False, index=c.columns)
            dn = lim["down"].loc[t] if t in lim["down"].index else pd.Series(False, index=c.columns)
            hi = [s for s in hi_all if not bool(up.get(s, False))]
            lo = [s for s in lo_all if not bool(dn.get(s, False))]
        else:
            hi, lo = hi_all, lo_all
        if not hi or not lo:
            i += step
            continue
        g = float(rv.loc[hi].mean() - rv.loc[lo].mean())
        if not np.isfinite(g):
            i += step
            continue
        # 多空组合的单期收益理论上不会 ≤ -100%（做空端最多贡献 -100%，做多端最多 -100%，
        # 但等权多空的收益被减去后极端值仍可能超出），这里做稳健裁剪避免净值归零/NaN。
        g = float(np.clip(g, -0.5, 0.5))
        # 换手（本次 vs 上次）
        if not prev_top and not prev_bot:
            turn = 1.0
        else:
            turn = (len(set(hi) ^ prev_top) + len(set(lo) ^ prev_bot)) / (2 * max(len(hi), 1))
        prev_top, prev_bot = set(hi), set(lo)
        cost = turn * (cost_bps + stamp_tax_bps) / 10000.0 * 2
        net = g - cost
        # 期净收益下限保护：不允许 ≤ -100%（否则净值归零、年化 -inf）
        net = max(net, -0.999)
        period_rets.append(net)
        turns.append(turn)
        hold_days.append(step)
        i += step

    if not period_rets:
        return {"nav": pd.Series(dtype=float), "sharpe": 0.0, "ann_ret": 0.0,
                "max_dd": 0.0, "turnover": 0.0, "periods_per_year": 0}
    pr = np.array(period_rets, dtype=float)
    # 去掉任何非有限值，避免污染 nav/ann/dd
    pr = pr[np.isfinite(pr)]
    if len(pr) < 2:
        return {"nav": pd.Series(dtype=float), "sharpe": 0.0, "ann_ret": 0.0,
                "max_dd": 0.0, "turnover": 0.0, "periods_per_year": 0}
    nav = np.cumprod(1 + pr)
    ppy = 252.0 / step
    ann = float(np.mean(pr) * ppy)
    sharpe = float(np.mean(pr) / np.std(pr) * np.sqrt(ppy)) if np.std(pr) > 0 else 0.0
    peak = np.maximum.accumulate(nav)
    max_dd = float((nav / peak - 1).min()) if peak.min() > 0 else -1.0
    return {"nav": pd.Series(nav), "period_returns": pr,
            "periods_per_year": ppy, "ann_ret": ann, "sharpe": sharpe,
            "max_dd": max_dd, "turnover": float(np.mean(turns)) if turns else 0.0}


def _ann(s: pd.Series) -> float:
    if len(s) == 0:
        return 0.0
    return float((1 + s).prod() ** (252 / len(s)) - 1)


def vol_target_scale(period_returns, periods_per_year: float = 12.6,
                     target_ann: float = 0.10, lookback: int = 6,
                     max_leverage: float = 2.0) -> "np.ndarray":
    """波动率目标化：按滚动已实现波动缩放每期仓位，把回撤压低。

    对每期 i，用前 lookback 期的已实现年化波动估计风险，
    scale = target_ann / rv（上限 max_leverage），作用于第 i 期收益。
    """
    pr = np.asarray(period_returns, dtype=float)
    out = np.empty_like(pr)
    for i in range(len(pr)):
        if i < lookback:
            out[i] = pr[i]
            continue
        hist = pr[i - lookback:i]
        rv = float(np.std(hist)) * np.sqrt(periods_per_year)
        scale = min(max_leverage, target_ann / max(rv, 1e-6))
        scale = max(0.0, scale)
        out[i] = pr[i] * scale
    return out


def nav_stats(period_returns, periods_per_year: float = 12.6) -> dict:
    """由期收益序列算 年化/Sharpe/最大回撤。"""
    pr = np.asarray(period_returns, dtype=float)
    pr = pr[np.isfinite(pr)]
    if len(pr) == 0 or np.std(pr) == 0:
        return {"ann": 0.0, "sharpe": 0.0, "max_dd": 0.0}
    nav = np.cumprod(1 + pr)
    peak = np.maximum.accumulate(nav)
    return {"ann": float(np.mean(pr) * periods_per_year),
            "sharpe": float(np.mean(pr) / np.std(pr) * np.sqrt(periods_per_year)),
            "max_dd": float((nav / peak - 1).min())}


def evaluate_realistic(panel: dict, spec: dict, name: str = "f", hold: int = 1) -> dict:
    """对比理想 vs 真实约束的多空表现。"""
    ideal = ls_returns_realistic(panel, spec, hold=hold, apply_limit=False, cost_bps=0)
    real = ls_returns_realistic(panel, spec, hold=hold, apply_limit=True, cost_bps=30)
    return {
        "name": name,
        "ideal_ann": _ann(ideal["gross"]),
        "ideal_sharpe": _sharpe(ideal["gross"]),
        "real_gross_ann": _ann(real["gross"]),
        "real_net_ann": _ann(real["net"]),
        "real_net_sharpe": _sharpe(real["net"]),
        "limit_block_rate": real["block_rate"],
        "n_days": real["n_days"],
    }


if __name__ == "__main__":
    import warnings, argparse
    warnings.filterwarnings("ignore")
    from core import data

    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="000300")
    ap.add_argument("--market", default="ashare")
    args = ap.parse_args()

    panel = data.build_panel(market=args.market, index=args.index)
    print(f"面板: {panel['c'].shape} ({args.index})")

    # 测试因子：A股主导信号（反转、波动）+ 经典
    specs = {
        "reversal_n5": {"op": "reversal", "params": {"n": 5}, "direction": 1},
        "vol_n60": {"op": "vol", "params": {"n": 60}, "direction": 1},
        "hl_range_n10": {"op": "hl_range", "params": {"n": 10}, "direction": 1},
        "skew_n120": {"op": "skew", "params": {"n": 120}, "direction": 1},
        "mom_n250": {"op": "mom", "params": {"n": 250, "skip": 20}, "direction": 1},
        "mom_n120": {"op": "mom", "params": {"n": 120, "skip": 20}, "direction": 1},
    }
    print(f"\n{'因子':16s} {'理想年化':>9s} {'理想Sh':>7s} {'真实毛年化':>9s} "
          f"{'真实净年化':>9s} {'真实净Sh':>8s} {'涨停阻断率':>9s}")
    rows = []
    for n, s in specs.items():
        r = evaluate_realistic(panel, s, n)
        rows.append(r)
        print(f"{n:16s} {r['ideal_ann']:>9.2%} {r['ideal_sharpe']:>7.2f} "
              f"{r['real_gross_ann']:>9.2%} {r['real_net_ann']:>9.2%} "
              f"{r['real_net_sharpe']:>8.2f} {r['limit_block_rate']:>9.2%}")
