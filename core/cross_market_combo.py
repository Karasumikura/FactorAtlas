"""跨市场组合 —— 沪深300(波动+趋势) × 美股动量，真实调仓口径。

动机（见 reports/TRADEABLE_FINAL.md）：
- A股内部独立 alpha 只有 2 个族（波动、趋势），且 300/500 相关 0.88
  → A股内部几乎没有分散空间
- 美股与A股 LS 收益相关仅 0.11~0.14 → 跨市场才是真正的独立来源

本模块构造两条腿，按**共同的月度调仓节奏**对齐后等权组合：
  腿A：沪深300 波动族 + 趋势族（三重门槛存活，真实调仓）
  腿B：美股动量（mom_n250）

输出：各自 Sharpe/年化/回撤、相关性、等权组合、风险平价组合。

用法：python -m core.cross_market_combo
"""
from __future__ import annotations
import json
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

from core import data
from core.ashare_backtest import ls_nav_true_rebalance, nav_stats
from core.backtest import forward_returns
from core.backtest_fast import layered_ls_fast
from factors.engine import factor_series_to_frame


def ashare_leg(index: str = "000300", run_path: str = "runs/mine_300_v6_triple.json",
               hold_fallback: int = 40) -> pd.Series:
    """沪深300 波动+趋势族，等权，真实调仓期收益（按各因子最优持有期）。"""
    d = json.load(open(run_path))
    panel = data.build_panel(market="ashare", index=index)
    # 只取三重门槛存活的
    tg = [r for r in d["survivors"]
          if r.get("lowfreq_cost_robust") and r.get("lowfreq_segment_robust")]
    # 分族：波动 vs 趋势（按算子名）
    vol_ops = {"vol", "volatility_ratio", "hl_range", "sortino", "sortino_proxy_vol_40"}
    vol_f, trd_f = [], []
    for r in tg:
        op = r.get("spec", {}).get("op", "")
        (vol_f if op in vol_ops else trd_f).append(r)
    # 各族取真实净 Sharpe 最高的一个作为代表（避免参数变体稀释）
    def best(rs):
        return max(rs, key=lambda x: x.get("lowfreq", {}).get("best_sharpe", -9)) if rs else None

    legs = []
    for grp in (vol_f, trd_f):
        rep = best(grp)
        if rep is None:
            continue
        h = rep["lowfreq"]["best_hold"]
        nav = ls_nav_true_rebalance(panel, rep["spec"], hold=h, cost_bps=30)
        pr = pd.Series(nav["period_returns"])
        ppy = nav["periods_per_year"]
        legs.append((rep["name"], pr, ppy))
    return legs, panel


def us_leg(hold: int = 20) -> tuple[str, pd.Series, float]:
    """美股动量，非重叠 hold 日期收益。"""
    panel = data.build_panel(market="us")
    spec = {"op": "mom", "params": {"n": 250, "skip": 0}, "direction": 1}
    f = factor_series_to_frame(panel, spec)
    c = panel["c"]
    rets = c.pct_change(hold)
    dates = list(f.index)
    n = len(dates)
    step = max(1, int(hold))
    prev = (set(), set())
    out, idx = [], []
    i = 0
    while i < n - step - 1:
        t = dates[i]
        fv = f.loc[t]
        rv = rets.iloc[i + step] if (i + step) < n else None
        if rv is None or not isinstance(rv, pd.Series):
            i += step; continue
        m = fv.notna() & rv.notna()
        if m.sum() < 10:
            i += step; continue
        nn = m.sum(); qn = max(1, int(nn * 0.2))
        order = fv[m].sort_values(ascending=False)
        hi = list(order.index[:qn]); lo = list(order.index[-qn:])
        g = float(rv.loc[hi].mean() - rv.loc[lo].mean())
        if not np.isfinite(g):
            i += step; continue
        g = float(np.clip(g, -0.5, 0.5))
        if not prev[0] and not prev[1]:
            turn = 1.0
        else:
            turn = (len(set(hi) ^ prev[0]) + len(set(lo) ^ prev[1])) / (2 * max(len(hi), 1))
        prev = (set(hi), set(lo))
        cost = turn * (30.0) / 1e4 * 2
        out.append(g - cost); idx.append(dates[i + step])
        i += step
    pr = pd.Series(out, index=pd.to_datetime(idx))
    return "us_mom_n250", pr, 252.0 / step


if __name__ == "__main__":
    print("=" * 60)
    print("跨市场组合：沪深300(波动+趋势) × 美股动量")
    print("=" * 60)

    legs, pa = ashare_leg()
    print("\n[A股 沪深300 两族（各取最优代表）]")
    for name, pr, ppy in legs:
        st = nav_stats(pr, periods_per_year=ppy)
        print(f"  {name:26s} Sh={st['sharpe']:+.2f} 年化={st['ann']:+.1%} 回撤={st['max_dd']:+.1%} 期数={len(pr)}")

    uname, upr, uppy = us_leg()
    ust = nav_stats(upr, periods_per_year=uppy)
    print(f"\n[美股]\n  {uname:26s} Sh={ust['sharpe']:+.2f} 年化={ust['ann']:+.1%} 回撤={ust['max_dd']:+.1%} 期数={len(upr)}")

    # A股两族等权（按最短长度对齐）
    a_names = [n for n, _, _ in legs]
    nmin = min(len(pr) for _, pr, _ in legs)
    a_comb = np.mean([np.asarray(pr)[:nmin] for _, pr, _ in legs], axis=0)
    a_ppy = legs[0][2]
    ast = nav_stats(a_comb, periods_per_year=a_ppy)
    print(f"\n[A股 波动+趋势 等权] Sh={ast['sharpe']:+.2f} 年化={ast['ann']:+.1%} 回撤={ast['max_dd']:+.1%}")

    # 相关性（A股日频序列 vs 美股日频序列，用日频 LS 收益对齐）
    print("\n[跨市场相关性]（日频 LS 收益，共同交易日）")
    # A股：用波动代表因子的日频 LS 收益
    from core.independence import ls_return_ts
    volspec = {"op": "vol", "params": {"n": 120}, "direction": 1}
    a_daily = ls_return_ts(pa, volspec, hold=1)
    pu = data.build_panel(market="us")
    usf = factor_series_to_frame(pu, {"op": "mom", "params": {"n": 250, "skip": 0}, "direction": 1})
    usfwd = forward_returns(pu, hold=1)
    usg, _, _, _ = layered_ls_fast(usf, usfwd)
    common = a_daily.index.intersection(usg.index)
    cc = None
    if len(common) > 50:
        cc = np.corrcoef(a_daily.loc[common], usg.loc[common])[0, 1]
        print(f"  沪深300波动 ↔ 美股动量: {cc:+.3f}  (共同 {len(common)} 日)")

    # ---- 跨市场组合：把A股两族腿与美股腿按共同月度网格对齐后等权/风险平价 ----
    print("\n[跨市场组合：A股(波动+趋势) × 美股动量]")
    # 统一到月度网格：把各腿期收益重采样到月频
    def to_monthly(pr, ppy):
        s = pd.Series(np.asarray(pr, dtype=float))
        # 无真实日期时按序列位置构造月末索引
        idx = pd.date_range("2015-01-31", periods=len(s), freq="ME")
        return pd.Series(s.values, index=idx)

    a_m = to_monthly(a_comb, a_ppy)
    us_m = to_monthly(np.asarray(upr, dtype=float), uppy)
    # 波动族腿单独也加进来做对照
    vol_m = to_monthly(np.asarray(legs[0][1], dtype=float), legs[0][2])
    trd_m = to_monthly(np.asarray(legs[1][1], dtype=float), legs[1][2])

    df = pd.DataFrame({"a_voltrend": a_m, "us_mom": us_m}).dropna()
    if len(df) > 20:
        eq = 0.5 * df["a_voltrend"] + 0.5 * df["us_mom"]
        st = nav_stats(eq, periods_per_year=12)
        corr = df.corr().iloc[0, 1]
        print(f"  共同月数: {len(df)}，A股↔美股月频相关: {corr:+.3f}")
        print(f"  A股腿 Sh={nav_stats(df['a_voltrend'],12)['sharpe']:+.2f}  "
              f"美股腿 Sh={nav_stats(df['us_mom'],12)['sharpe']:+.2f}")
        print(f"  等权组合  Sh={st['sharpe']:+.2f} 年化={st['ann']:+.1%} 回撤={st['max_dd']:+.1%}")
        # 风险平价（逆波动）
        w_a = 1.0 / max(df['a_voltrend'].std(), 1e-9)
        w_u = 1.0 / max(df['us_mom'].std(), 1e-9)
        w_a, w_u = w_a / (w_a + w_u), w_u / (w_a + w_u)
        rp = w_a * df['a_voltrend'] + w_u * df['us_mom']
        srp = nav_stats(rp, periods_per_year=12)
        print(f"  风险平价  Sh={srp['sharpe']:+.2f} 年化={srp['ann']:+.1%} 回撤={srp['max_dd']:+.1%}  (w_A={w_a:.2f}, w_US={w_u:.2f})")
