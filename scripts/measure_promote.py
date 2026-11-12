#!/usr/bin/env python3
"""测量 core.promote 的内存峰值与耗时，用于定位 OOM。

用法: venv/bin/python scripts/measure_promote.py
输出 PEAK_RSS_MB / ELAPSED / 各阶段增量。
"""
import os
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

print(f"[start] RSS={rss_mb():.0f}MB", flush=True)

from core import promote  # noqa: E402

print(f"[imported] RSS={rss_mb():.0f}MB", flush=True)

t0 = time.time()
# 分步执行，观察每一步的内存增量
from core import data  # noqa: E402
print(f"[data imported] RSS={rss_mb():.0f}MB", flush=True)

t = time.time()
panel = data.build_panel(market="us")
print(f"[build_panel us] RSS={rss_mb():.0f}MB  ({time.time()-t:.0f}s)", flush=True)
print(f"    panel shape={getattr(panel, 'shape', None)}", flush=True)
del panel

t = time.time()
result = promote.promote()
print(f"[promote done] RSS={rss_mb():.0f}MB  ({time.time()-t:.0f}s)", flush=True)

peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
print(f"PEAK_RSS_MB={peak:.0f}")
print(f"ELAPSED={time.time()-t0:.0f}s")
