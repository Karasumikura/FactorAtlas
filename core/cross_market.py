"""跨市场分散化分析：用【因子多空组合收益】的相关，而非因子值相关。

因子值序列的横截面rank均值近似常数，直接算相关会被市场漂移主导，得出
虚假的高相关。正确做法是把每个因子做成多空组合的日收益序列，再算相关。
"""
from __future__ import annotations
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

from core import data
from factors.engine import factor_series_to_frame


def ls_returns(panel, spec, hold: int = 1, quantile: float = 0.2) -> pd.Series:
    """因子多空组合日收益：每日按因子分组，做多高分组、做空低分组，次日持有。"""
    f = factor_series_to_frame(panel, spec)
    c = panel["c"]
    ret_next = c.shift(-hold) / c - 1.0
    idx = f.index
    q = max(1, int(f.shape[1] * quantile))
    out = {}
    for t in idx:
        fv = f.loc[t]
        rv = ret_next.loc[t] if t in ret_next.index else None
        if rv is None:
            continue
        m = fv.notna() & rv.notna()
        if m.sum() < 2 * q:
            continue
        f2, r2 = fv[m], rv[m]
        top = r2.nlargest(q).mean() if f2.nlargest(q).index.equals(f2.nlargest(q).index) else None
        # 按因子值排序取组
        order = f2.sort_values(ascending=False)
        hi = order.index[:q]; lo = order.index[-q:]
        out[t] = r2.loc[hi].mean() - r2.loc[lo].mean()
    return pd.Series(out).sort_index()


def main():
    pus = data.build_panel(market="us")
    pas = data.build_panel(market="ashare")

    us_specs = {"us_mom250": {"op": "mom", "params": {"n": 250}, "direction": 1},
                "us_trend120": {"op": "trend_strength", "params": {"n": 120}, "direction": 1}}
    as_specs = {"as_vol60": {"op": "vol", "params": {"n": 60}, "direction": -1},
                "as_rev5": {"op": "reversal", "params": {"n": 5}, "direction": 1},
                "as_skew120": {"op": "skew", "params": {"n": 120}, "direction": 1}}

    rets = {}
    for n, s in us_specs.items():
        rets[n] = ls_returns(pus, s)
    for n, s in as_specs.items():
        rets[n] = ls_returns(pas, s)

    print("=== 各因子多空组合年化收益/夏普（毛，样本全期）===")
    for n, r in rets.items():
        r = r.dropna()
        ann = (1 + r).prod() ** (252 / len(r)) - 1 if len(r) > 20 else float("nan")
        sh = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else float("nan")
        print(f"  {n:14s} 年化={ann:+.2%} 夏普={sh:+.2f} n={len(r)}")

    df = pd.DataFrame(rets).dropna()
    print(f"\n=== 因子收益相关矩阵（共同交易日 n={len(df)}）===")
    cm = df.corr()
    print(cm.round(3).to_string())

    # 关键：跨市场相关
    cross = []
    for a in as_specs:
        for u in us_specs:
            cross.append(abs(cm.loc[a, u]))
    print(f"\n=== 结论 ===")
    print(f"  跨市场（A股因子 vs 美股因子）平均|相关| = {np.mean(cross):.3f}")
    within_us = abs(cm.loc['us_mom250', 'us_trend120'])
    within_as = np.mean([abs(cm.loc[a, b]) for a in as_specs for b in as_specs if a < b])
    print(f"  美股内部平均|相关| = {within_us:.3f}")
    print(f"  A股内部平均|相关|   = {within_as:.3f}")


if __name__ == "__main__":
    main()
