#!/usr/bin/env python3
"""CPCV 证伪扫描：新另类数据因子在 15 条真实调仓路径上的 Sharpe 分布。

沿用 EDGAR 那次立的纪律：单次 70/30 split 报的高 Sharpe 不算数，
必须过 CPCV 多路径 + 真实调仓口径，才决定进不进生产 FDR 池。

用法：venv/bin/python scripts/cpcv_alt_scan.py
产物：runs/cpcv_alt_scan.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/opt/quant-lab")

from core.cpcv_eval import cpcv_sharpe_distribution
from core.data import build_panel

OUT = Path("/opt/quant-lab/runs/cpcv_alt_scan.json")
SPECS = [
    ("finra_short_ratio", {"n": 60}),
    ("finra_short_surge", {"n": 60}),
    ("finra_short_exempt", {"n": 60}),
    ("wiki_attention", {"n": 60}),
    ("wiki_attention_chg", {"n": 60}),
    ("wiki_attention_vol", {"n": 60}),
    ("mom", {"n": 250}),          # 对照组
]


def main() -> int:
    import pandas as pd
    p = build_panel()
    # 另类数据的年度归档只到 2026-03（FINRA），把 panel 截到覆盖窗口内，
    # 否则后期折的因子全 NaN、n_valid 会被无意义地稀释。
    fin = pd.read_parquet("/opt/quant-lab/data/alt/finra_short.parquet")
    last = pd.to_datetime(str(int(fin["Date"].max())))
    p = {k: v.loc[v.index <= last] for k, v in p.items()}
    print("panel 截到", p["c"].index[-1].date(), "共", len(p["c"].index), "天",
          flush=True)
    rows = []
    for op, params in SPECS:
        t0 = time.time()
        spec = {"op": op, "params": params, "direction": 1}
        try:
            r = cpcv_sharpe_distribution(p, spec, hold=5,
                                         n_splits=6, n_test_groups=2)
        except Exception as exc:  # noqa: BLE001
            r = {"error": type(exc).__name__ + ": " + str(exc)[:200]}
        r["op"] = op
        r["params"] = params
        r["wall_s"] = round(time.time() - t0, 1)
        rows.append(r)
        print("%-22s %s  %.1fs" % (op, json.dumps(
            {k: v for k, v in r.items()
             if k.startswith("sharpe") or k in ("negative_share", "n_valid", "error")},
            ensure_ascii=False), r["wall_s"]), flush=True)

    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "hold": 5, "n_splits": 6, "n_test_groups": 2,
           "results": rows}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("落盘", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
