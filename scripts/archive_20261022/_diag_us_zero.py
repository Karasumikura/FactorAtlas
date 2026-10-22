"""诊断 US 侧 0 幸存：BH-FDR 用的是 IC 的 p 值，而幸存判定用 LS Sharpe。

对比三种口径下各区间的候选能否通过 BH-FDR(q=0.10)：
  A) 现状：IC t 检验的 p 值（rank IC 均值显著性）
  B) 用 LS Sharpe 的 t 检验 p 值（多空组合收益显著性）
  C) 用 OOS 决定的稳健性
"""
import sys, json
sys.path.insert(0, "/opt/quant-lab")
import numpy as np
from scipy import stats
from core import backtest

for f in ["runs/loop_20261016_205535/round_06/mine_us.json",
          "runs/loop_20261018_024349/round_01/mine_us.json",
          "runs/loop_20261016_205535/round_06/mine_ashare_000300.json"]:
    d = json.load(open(f))
    res = d["all_results"]
    n_days = int(np.median([r.get("n_days") or 1 for r in res]))

    # 按 (op, params) 归并（与 mine.py 的 _hyp_key 口径一致）
    groups = {}
    for r in res:
        k = f"{r.get('op')}|{json.dumps(r.get('params') or {}, sort_keys=True)}"
        groups.setdefault(k, []).append(r)

    keys = list(groups)
    p_ic = [min(r.get("p_value", 1) for r in groups[k]) for k in keys]
    # B) 用 LS Sharpe 换算 t 统计量：t = sharpe * sqrt(n_years)
    #    日频 sharpe 已年化(sqrt(252))，故 n_years = n_days/252
    def sharpe_to_p(sh):
        if sh is None or sh != sh:
            return 1.0
        t = abs(sh) * np.sqrt(n_days / 252.0)
        return float(2 * (1 - stats.t.cdf(t, df=max(n_days - 1, 1))))
    p_ls = [sharpe_to_p(max(groups[k], key=lambda r: abs(r.get("ls_sharpe") or 0)).get("ls_sharpe")) for k in keys]

    pass_ic = backtest.benjamini_hochberg(p_ic, q=0.10)
    pass_ls = backtest.benjamini_hochberg(p_ls, q=0.10)

    print(f"\n=== {f.split('/')[-2]}/{f.split('/')[-1]} ===")
    print(f"记录 {len(res)} → {len(keys)} 个独立假设 | 天数 {n_days}")
    print(f"A) 现状(IC p值)      : 通过 {int(sum(pass_ic)):3d}   最小p={min(p_ic):.6f}")
    print(f"B) 改用(LS Sharpe)   : 通过 {int(sum(pass_ls)):3d}   最小p={min(p_ls):.6f}")

    # 在 A 通过的假设上，再看 OOS 门槛会刷掉多少
    oos_ok = 0
    for k, ok in zip(keys, pass_ic):
        if not ok:
            continue
        best = max(groups[k], key=lambda r: abs(r.get("ls_sharpe") or 0))
        oos = best.get("oos_ls_sharpe")
        if oos is not None and oos == oos and abs(oos) >= 0.3:
            oos_ok += 1
    print(f"   A 通过后再过 OOS(|sh|>=0.3): {oos_ok}")

    # 关键诊断：如果 BH 放宽到 q=0.10 时最小 p 还差多少
    print(f"   BH 第1名门槛 = 0.10/{len(keys)} = {0.10/len(keys):.6f}")
