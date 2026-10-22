"""幸存因子的【独立性】分析：把相关高的合并成家族，数出真正独立的信号个数。

动机：296 个候选里 73 个"幸存"是假象——大量是同一信号的不同参数/镜像。
真正该报告的是【独立信号数】，而非幸存条数。
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from core import data
from factors.engine import factor_series_to_frame


def factor_ts(panel, spec) -> pd.Series:
    """因子压缩成时间序列（每日截面 rank 均值）。

    注意：这是【因子值】的时序，用于快速比较暴露，但它会高估相关性——
    A股里方向相反的两个因子（如高波动 vs 高换手）底层暴露相同，rank 均值也高度相关，
    却被判成同一族。真正该用的是【多空组合收益】相关，见 ls_return_ts()。
    """
    f = factor_series_to_frame(panel, spec)
    return f.rank(axis=1, pct=True).mean(axis=1)


def ls_return_ts(panel, spec, hold: int = 1) -> pd.Series:
    """因子的【多空组合日收益】时间序列。

    这是衡量两个因子是否"真独立"的正确基础：
      - 已做多空对冲，扣掉市场 beta，消除"强 market beta 导致的假相关"
      - 方向相反的两个因子这里会呈现【负相关】，而非被误判成同族
    """
    from core.backtest import forward_returns
    from core.backtest_fast import layered_ls_fast
    f = factor_series_to_frame(panel, spec)
    fwd = forward_returns(panel, hold=hold)
    gross, _, _, _ = layered_ls_fast(f, fwd)
    return gross


def cluster_sharpe(specs_ts: dict[str, pd.Series], thresh: float = 0.85) -> list[list[str]]:
    """按相关阈值做贪心聚类。"""
    names = list(specs_ts)
    used = set()
    clusters = []
    for n in names:
        if n in used:
            continue
        grp = [n]; used.add(n)
        for m in names:
            if m in used:
                continue
            a, b = specs_ts[n].align(specs_ts[m], join="inner")
            a, b = a.dropna(), b.reindex(a.index)
            if len(a) < 50 or a.std() == 0 or b.std() == 0:
                continue
            c = abs(float(a.corr(b)))
            if c >= thresh:
                grp.append(m); used.add(m)
        clusters.append(grp)
    return clusters


def analyze(mine_json: str, market: str = "ashare", thresh: float = 0.85,
            index: str | None = None, use_ls_returns: bool = True):
    d = json.load(open(mine_json))
    surv = d["survivors"]
    panel = data.build_panel(market=market, index=index)

    ts = {}
    for s in surv:
        try:
            ts[s["name"]] = (ls_return_ts(panel, s["spec"]) if use_ls_returns
                             else factor_ts(panel, s["spec"]))
        except Exception:
            continue

    clusters = cluster_sharpe(ts, thresh)
    # 每个家族选 OOS_Sharpe 最优作代表
    sh = {s["name"]: s.get("oos_ls_sharpe", 0) or 0 for s in surv}
    reps = []
    for grp in clusters:
        rep = max(grp, key=lambda n: abs(sh.get(n, 0)))
        reps.append({"rep": rep, "members": grp,
                     "n": len(grp), "oos_sharpe": sh.get(rep, 0)})

    reps.sort(key=lambda r: -abs(r["oos_sharpe"]))
    basis = "多空组合收益" if use_ls_returns else "因子值rank均值(旧,会高估相关)"
    print(f"\n===== 独立性分析（阈值 {thresh}，相关性基础={basis}，market={market}）=====")
    print(f"幸存 {len(surv)} 个 → 独立信号族 {len(clusters)} 个\n")
    for r in reps:
        print(f"  ★ {r['rep']:24s} OOS_Sh={r['oos_sharpe']:+.2f}  "
              f"含 {r['n']} 个变体: {', '.join(r['members'][:4])}"
              + (" ..." if r["n"] > 4 else ""))
    out = {"market": market, "index": index, "n_survivors": len(surv),
           "n_independent": len(clusters), "basis": basis, "families": reps}
    tag = f"{market}_{index}" if index else market
    Path(f"runs/independent_{tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n已保存 runs/independent_{tag}.json")
    return out


if __name__ == "__main__":
    mj = sys.argv[1] if len(sys.argv) > 1 else "runs/mine_ashare_v2.json"
    mk = sys.argv[2] if len(sys.argv) > 2 else "ashare"
    ix = sys.argv[3] if len(sys.argv) > 3 else None
    analyze(mj, mk, index=ix)
