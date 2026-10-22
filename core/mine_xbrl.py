"""XBRL 基本面因子挖掘：价值/质量/成长/规模的横截面检验。

为什么单独跑：
  1. 基本面因子更新慢（季度），只在 XBRL 数据就绪后才有值；
  2. 数量少（11 个），不需要网格扫描；
  3. 目标是找**与价量因子低相关**的独立 alpha → 分散化来源。

★ 评估方法（关键修正）：
  多日持有必须用 ls_nav_true_rebalance（非重叠离散调仓），
  不能用 forward_returns(hold)+日频Sharpe —— 后者因重叠样本把 Sharpe 虚高 √hold 倍。
  这里同时给 hold=1 的 IC/OOS 一致性和 hold=20/60 的真实调仓 Sharpe。

严格无前视：XBRL 用 SEC 的 filed 日期，财报公布当天收盘后才可用。

用法：
  python -m core.mine_xbrl --market us
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from core import config, data
from core.ashare_backtest import ls_nav_true_rebalance
from factors.xbrl_fund import XBRL_OPS
from factors.engine import factor_series_to_frame

# 全样本 IC（用 future 收益，仅作方向参考）
def _rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame) -> float:
    f = factor.align(fwd, join="inner")
    a, b = f
    valid = a.notna() & b.notna()
    a, b = a.where(valid), b.where(valid)
    ar = a.rank(axis=1); br = b.rank(axis=1)
    az = ar.sub(ar.mean(axis=1), axis=0); bz = br.sub(br.mean(axis=1), axis=0)
    num = (az*bz).sum(axis=1)
    den = np.sqrt((az**2).sum(axis=1)*(bz**2).sum(axis=1))
    ic = (num/den.replace(0, np.nan)).dropna()
    return float(ic.mean()) if len(ic) else 0.0


def _split_index(panel, train_frac=0.7):
    idx = panel["c"].index
    n = len(idx)
    return idx[:int(n*train_frac)], idx[int(n*train_frac):]


def run(market: str = "us", holds: list[int] | None = None,
        cost_bps: float = 10.0, out_json: Path | None = None,
        verbose: bool = True) -> dict:
    holds = holds or [20, 60]
    panel = data.build_panel(market=market)
    c = panel["c"]
    train_idx, test_idx = _split_index(panel)
    fwd1 = c.shift(-2) / c.shift(-1) - 1.0  # t 收盘后持 1 日

    results = []
    if verbose:
        print(f"XBRL 因子 {len(XBRL_OPS)} 个 × 方向2 × 持有期 {holds}", flush=True)
    for op in XBRL_OPS:
        for d in (1, -1):
            spec = {"op": op, "params": {}, "direction": d}
            try:
                f = factor_series_to_frame(panel, spec)
            except Exception as e:
                if verbose: print(f"  {op}: ERR {e}", flush=True)
                continue
            rec = {"name": f"{op}_d{d}", "op": op, "direction": d, "source": "xbrl"}

            # hold=1 IC（全样本 + OOS）
            rec["ic_full"] = _rank_ic(f, fwd1)
            rec["ic_oos"] = _rank_ic(f.loc[test_idx], fwd1.loc[test_idx])

            # 真实调仓 Sharpe（非重叠）
            for h in holds:
                nav = ls_nav_true_rebalance(panel, spec, hold=h, quantile=0.2,
                                            cost_bps=cost_bps, stamp_tax_bps=0.0,
                                            apply_limit=False)
                rec[f"sharpe_h{h}"] = round(nav["sharpe"], 3)
                rec[f"ann_h{h}"] = round(nav["ann_ret"], 4)
                rec[f"dd_h{h}"] = round(nav["max_dd"], 4)
                rec[f"turn_h{h}"] = round(nav["turnover"], 3)

            # 幸存判定：最佳持有期 Sharpe > 0.3 且 IC 与方向一致
            best_h = max(holds, key=lambda h: rec.get(f"sharpe_h{h}", -9))
            sh = rec.get(f"sharpe_h{best_h}", 0)
            ic_ok = (rec["ic_full"] > 0) == (d > 0) or abs(rec["ic_full"]) < 1e-6
            rec["best_hold"] = best_h
            rec["best_sharpe"] = sh
            rec["survivor"] = bool(sh >= 0.3 and ic_ok)
            results.append(rec)
            if verbose and rec["survivor"]:
                print(f"  ★ {rec['name']:26s} h={best_h} Sh={sh:+.2f} "
                      f"ann={rec[f'ann_h{best_h}']:+.1%} dd={rec[f'dd_h{best_h}']:+.1%} "
                      f"IC={rec['ic_full']:+.4f}", flush=True)

    survs = [r for r in results if r["survivor"]]
    survs.sort(key=lambda x: -x["best_sharpe"])
    out = {
        "generated_at": datetime.now().isoformat(),
        "market": market, "holds": holds, "cost_bps": cost_bps,
        "n_tested": len(results), "n_survivors": len(survs),
        "survivors": survs, "all_results": results,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)

        def _d(o):
            if isinstance(o, (np.integer,)): return int(o)
            if isinstance(o, (np.floating,)): return float(o)
            if isinstance(o, (np.bool_,)): return bool(o)
            raise TypeError(type(o))

        out_json.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=_d))
    if verbose:
        print(f"\nXBRL 幸存 {len(survs)} / {len(results)}", flush=True)
        for s in survs:
            print(f"  {s['name']:26s} h={s['best_hold']} Sh={s['best_sharpe']:+.2f} "
                  f"IC={s['ic_full']:+.4f}", flush=True)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="us", choices=["us"])
    ap.add_argument("--hold", type=int, nargs="+", default=[20, 60])
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outp = Path(args.out) if args.out else config.RUNS_DIR / f"mine_xbrl_{ts}.json"
    run(market=args.market, holds=args.hold, cost_bps=args.cost_bps, out_json=outp)
