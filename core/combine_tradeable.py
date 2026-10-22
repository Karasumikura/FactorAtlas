"""三指数可交易因子组合 —— 真实调仓口径。

从三个指数（沪深300/中证500/中证1000）的严格筛选结果中，取
`lowfreq_tradeable=True` 的因子，各按最优持有期、真实调仓法合成指数组合，
再等权合成三指数组合。输出年化/Sharpe/最大回撤 + 相关性。

用法：python -m core.combine_tradeable
"""
from __future__ import annotations
import json
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

from core import data
from core.ashare_backtest import ls_nav_true_rebalance, vol_target_scale, nav_stats

RUNS = {
    "沪深300": ("runs/mine_300_v4.json", "000300"),
    "中证500": ("runs/mine_500_v4.json", "000905"),
    "中证1000": ("runs/mine_1000_v4.json", "000852"),
}


def market_returns(path: str, index: str, cost_bps: float = 20.0):
    """返回该指数所有可交易因子的组合期收益序列（等权）及其因子数。"""
    d = json.load(open(path))
    panel = data.build_panel(market="ashare", index=index)
    prs = {}
    for x in d.get("survivors", []):
        if not x.get("lowfreq_tradeable"):
            continue
        h = x.get("lowfreq", {}).get("best_hold")
        if not h:
            continue
        try:
            r = ls_nav_true_rebalance(panel, x["spec"], hold=h, cost_bps=cost_bps)
            if len(r["period_returns"]):
                prs[x["name"]] = r["period_returns"]
        except Exception:
            continue
    if not prs:
        return None, 0
    m = min(len(v) for v in prs.values())
    combo = np.mean([v[:m] for v in prs.values()], axis=0)
    return combo, len(prs)


def main():
    ppy = 252.0 / 20.0  # 以 20 日为主口径
    sets, ns = {}, {}
    for tag, (path, idx) in RUNS.items():
        try:
            c, n = market_returns(path, idx)
        except FileNotFoundError:
            print(f"[跳过] {tag}: 找不到 {path}")
            continue
        if c is not None:
            sets[tag], ns[tag] = c, n
            st = nav_stats(c, ppy)
            print(f"{tag}: {n} 个可交易因子  年化={st['ann']:+.1%}  "
                  f"Sh={st['sharpe']:+.2f}  回撤={st['max_dd']:+.1%}")
    if len(sets) < 2:
        print("可交易因子结果不足，无法组合。请先用 --lowfreq-strict 跑挖掘。")
        return
    L = min(len(v) for v in sets.values())
    df = pd.DataFrame({k: v[:L] for k, v in sets.items()})
    print(f"\n共同期数 {L} (每期20日 ≈ {L*20/252:.1f}年)")
    print("相关性：")
    print(df.corr().round(3).to_string())
    comp = df.mean(axis=1).values
    st = nav_stats(comp, ppy)
    print(f"\n三指数等权组合: 年化={st['ann']:+.1%}  Sh={st['sharpe']:+.2f}  "
          f"最大回撤={st['max_dd']:+.1%}")
    vt = vol_target_scale(comp, ppy, target_ann=0.10)
    st2 = nav_stats(vt, ppy)
    print(f"  +波动目标化(10%): 年化={st2['ann']:+.1%}  Sh={st2['sharpe']:+.2f}  "
          f"最大回撤={st2['max_dd']:+.1%}")


if __name__ == "__main__":
    main()
