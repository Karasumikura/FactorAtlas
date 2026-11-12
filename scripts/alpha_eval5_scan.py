#!/usr/bin/env python3
"""冷却期五维评估入口（AlphaEval 指标，不新增候选 → 不抬试验数 N）。

读最新一轮 mine 的 survivor + 正式因子库，算五维，落盘 runs/alpha_eval5.json。
由 scripts/robust_idle.sh 在 quant-robust.timer 的空闲窗口里调用。
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

PROJ = Path("/opt/quant-lab")
sys.path.insert(0, str(PROJ))

from core import data  # noqa: E402
from core.alpha_eval5 import evaluate_set  # noqa: E402


def latest_survivors(market: str, limit_rounds: int = 2) -> list[dict]:
    files = sorted(glob.glob(str(PROJ / "runs" / "loop_*" / "round_*" /
                                 f"mine_{market}.json")))[-limit_rounds:]
    specs, seen = [], set()
    for f in files:
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for r in d.get("survivors", []):
            s = r.get("spec")
            if not s:
                continue
            key = json.dumps(s, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            specs.append(s)
    return specs


def library_specs() -> list[dict]:
    p = PROJ / "data" / "factor_library.json"
    try:
        d = json.loads(p.read_text())
        vals = d.get("factors", {}) if isinstance(d, dict) else d
        items = vals.values() if isinstance(vals, dict) else vals
        # 库内每条是 {"name":..., "spec": {...}, "n_sessions":...}，取 spec
        return [v.get("spec", v) for v in items if isinstance(v, dict)]
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="us")
    ap.add_argument("--hold", type=int, default=1)
    ap.add_argument("--out", default=str(PROJ / "runs" / "alpha_eval5.json"))
    args = ap.parse_args()

    t0 = time.time()
    panel = data.build_panel(market=args.market)
    print(f"面板加载 {time.time()-t0:.1f}s  市场={args.market}", flush=True)

    lib = library_specs()
    surv = latest_survivors(args.market)
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "market": args.market,
        "hold": args.hold,
        "source": "AlphaEval five dimensions (KDD 2026, MIT) — numpy reimplementation",
        "library": evaluate_set(panel, lib, hold=args.hold),
        "recent_survivors": evaluate_set(panel, surv, hold=args.hold),
        "wall_s": None,
    }
    result["wall_s"] = round(time.time() - t0, 1)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                         default=str))
    libdiv = result["library"]["diversity"]
    sdiv = result["recent_survivors"]["diversity"]
    print(f"完成：库内 {len(lib)} 因子 diversity={libdiv} | "
          f"近期 survivor {len(surv)} 个 diversity={sdiv} | "
          f"耗时 {result['wall_s']}s → {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
