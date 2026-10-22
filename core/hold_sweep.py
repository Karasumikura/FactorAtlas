"""降频检验：把持有期从 1 日拉长到 5/10/20/40 日，看哪些因子扣成本后转正。

动机：日频 A股因子被换手成本碾碎（见 ASHARE_TRADABILITY.md）。
降低调仓频率能大幅降换手，是让因子重新可交易的唯一现实路径。
"""
from __future__ import annotations
import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

from core import data
from core.ashare_backtest import ls_returns_realistic, _ann, _sharpe


FACTORS = {
    "vol_n60": {"op": "vol", "params": {"n": 60}, "direction": 1},
    "vol_n20": {"op": "vol", "params": {"n": 20}, "direction": 1},
    "hl_range_n20": {"op": "hl_range", "params": {"n": 20}, "direction": 1},
    "mom_n250": {"op": "mom", "params": {"n": 250, "skip": 20}, "direction": 1},
    "mom_n120": {"op": "mom", "params": {"n": 120, "skip": 20}, "direction": 1},
    "trend_strength_n120": {"op": "trend_strength", "params": {"n": 120}, "direction": 1},
    "skew_n120": {"op": "skew", "params": {"n": 120}, "direction": 1},
    "reversal_n5": {"op": "reversal", "params": {"n": 5}, "direction": 1},
    "amihud_n20": {"op": "amihud", "params": {"n": 20}, "direction": 1},
    # 新增算子（2026-10-15）
    "close_to_high_n20": {"op": "close_to_high", "params": {"n": 20}, "direction": -1},
    "close_to_high_n60": {"op": "close_to_high", "params": {"n": 60}, "direction": -1},
    "volume_price_corr_n20": {"op": "volume_price_corr", "params": {"n": 20}, "direction": 1},
    "downside_days_n20": {"op": "downside_days", "params": {"n": 20}, "direction": -1},
    "gap_n60": {"op": "gap", "params": {"n": 60}, "direction": -1},
    "turnover_std_n20": {"op": "turnover_std", "params": {"n": 20}, "direction": 1},
}

DEFAULT_HOLDS = [1, 5, 10, 20, 40]


def sweep_specs(panel, specs: dict, holds: list[int] | None = None,
                cost_bps: float = 20.0) -> dict:
    """通用：对任意 {name: spec} 清单跑持有期扫描，返回每个因子的最优持有期。

    可对任意挖掘结果（如 mine 的 survivors）复用，用于把"日频 Sharpe"升级为
    "最优持有期净值"。返回 {name: {grid:[...], best:{hold,net_ann,net_sharpe}}}。
    """
    holds = holds or DEFAULT_HOLDS
    out = {}
    for name, spec in specs.items():
        grid = []
        for h in holds:
            try:
                r = ls_returns_realistic(panel, spec, hold=h, cost_bps=cost_bps)
                grid.append({"hold": h,
                             "net_ann": round(float(_ann(r["net"])), 4),
                             "net_sharpe": round(float(_sharpe(r["net"])), 4),
                             "block_rate": round(float(r["block_rate"]), 4)})
            except Exception:
                grid.append({"hold": h, "net_ann": None, "net_sharpe": None,
                             "block_rate": None})
        valid = [g for g in grid if g["net_sharpe"] is not None]
        if valid:
            b = max(valid, key=lambda g: g["net_sharpe"])
            out[name] = {"grid": grid,
                         "best": {"hold": b["hold"], "net_ann": b["net_ann"],
                                  "net_sharpe": b["net_sharpe"]}}
    return out


def main():
    import argparse, json, time
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="000300")
    ap.add_argument("--market", default="ashare")
    ap.add_argument("--out", default=None, help="结果落盘 JSON 路径")
    args = ap.parse_args()
    panel = data.build_panel(market=args.market, index=args.index)
    print(f"面板 {panel['c'].shape} index={args.index}\n")

    holds = [1, 5, 10, 20, 40]
    print(f"{'因子':22s} " + " ".join(f"{'h='+str(h):>16s}" for h in holds))
    print(f"{'':22s} " + " ".join(f"{'净年化/净Sh':>16s}" for h in holds))
    print("-" * 105)

    best = []
    grid = {}
    for name, spec in FACTORS.items():
        cells = []
        row_cells = []
        for h in holds:
            r = ls_returns_realistic(panel, spec, hold=h, cost_bps=20)
            ann = _ann(r["net"])
            sh = _sharpe(r["net"])
            cells.append((ann, sh, r["block_rate"]))
            row_cells.append({"hold": h, "net_ann": round(float(ann), 4),
                              "net_sharpe": round(float(sh), 4),
                              "block_rate": round(float(r["block_rate"]), 4)})
        grid[name] = row_cells
        row = " ".join(f"{a:>+8.1%}/{s:>+5.2f}" for a, s, _ in cells)
        print(f"{name:22s} {row}")
        # 记录最优 hold
        bi = max(range(len(holds)), key=lambda i: cells[i][1])
        best.append((name, holds[bi], cells[bi][0], cells[bi][1]))

    print("\n=== 各因子最优持有期（按净Sharpe）===")
    summary = []
    for name, h, a, s in sorted(best, key=lambda x: -x[3]):
        flag = "  ✅ 可交易候选" if s > 0.5 else ("  ⚠️ 勉强" if s > 0.2 else "")
        print(f"  {name:22s} h={h:2d}日  净年化={a:+.1%}  净Sharpe={s:+.2f}{flag}")
        summary.append({"name": name, "best_hold": int(h),
                        "best_net_ann": round(float(a), 4),
                        "best_net_sharpe": round(float(s), 4)})

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "market": args.market, "index": args.index,
            "panel_shape": list(panel["c"].shape),
            "holds": holds, "grid": grid, "best": summary,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=2))
        print(f"\n[hold_sweep] 结果已写入 {out}")


if __name__ == "__main__":
    main()
