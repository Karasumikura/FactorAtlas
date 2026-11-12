#!/usr/bin/env python3
"""分阶段测量 promote 内部的内存增长，定位 OOM 峰值点。

用与 core.promote 完全相同的 glob 模式（445 个文件），逐阶段打印 RSS。
"""
import resource
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))


def rss_mb() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


from core import promote  # noqa: E402

PATTERNS = [
    str(PROJ / "runs" / "mine*.json"),
    str(PROJ / "runs" / "loop_*" / "round_*" / "mine*.json"),
    str(PROJ / "runs" / "loop_*" / "round_*" / "ortho*.json"),
]

t = time.time()
acc, n_runs = promote.collect_survivors(promote.load_runs(PATTERNS))
print(f"[collect] {n_runs} files, {len(acc)} specs, RSS={rss_mb():.0f}MB ({time.time()-t:.0f}s)", flush=True)

t = time.time()
evals = promote.evaluate(acc, n_trials_cumulative=86540)
print(f"[evaluate] RSS={rss_mb():.0f}MB ({time.time()-t:.0f}s) type={type(evals).__name__}", flush=True)

# evaluate 返回 list 或 dict，兼容两种
items = evals.items() if isinstance(evals, dict) else [(e.get("spec_id"), e) for e in evals]
items = list(items)
n_pass = sum(1 for _k, v in items if isinstance(v, dict) and v.get("promoted"))
print(f"    total={len(items)} promoted={n_pass}", flush=True)

t = time.time()
lib = promote.load_library()
order = sorted(items, key=lambda kv: -abs((kv[1].get("oos_median") if isinstance(kv[1], dict) else 0) or 0))
print(f"[before gate] RSS={rss_mb():.0f}MB", flush=True)
try:
    promote._apply_redundancy_gate(order, lib, 0.8, lambda *a, **k: None)
except Exception as exc:
    print(f"[gate] FAILED: {type(exc).__name__}: {exc}", flush=True)
print(f"[after gate] RSS={rss_mb():.0f}MB ({time.time()-t:.0f}s)", flush=True)
print(f"    factor cache entries: {len(promote._FACTOR_CACHE)}", flush=True)

print(f"PEAK_RSS_MB={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f}")
