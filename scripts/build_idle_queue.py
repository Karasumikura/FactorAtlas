#!/usr/bin/env python3
"""生成评估任务队列（SQLite 版）。

为什么不再用 JSON：79k 任务 ≈ 18MB，两个 worker 每次取任务都要
json.load + json.dump 全量重写，I/O 巨大且中途崩溃会把文件写坏
（2026-11-10 实测：JSONDecodeError，worker 进入崩溃循环）。
SQLite + WAL 支持并发增量更新，取任务是一条 UPDATE，代价可忽略。
"""
from __future__ import annotations

import glob
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path("/opt/quant-lab")
DB = PROJ / "state" / "idle_queue.db"
RESULTS = PROJ / "runs" / "idle_results"
HOLDS = [2, 3, 5, 10, 20, 40, 60, 120]
SPLITS = [0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8]
ROLL_WINDOWS = [20, 40, 60, 90, 120, 180, 250]


def key_of(task: dict) -> str:
    return hashlib.md5(json.dumps(task, sort_keys=True, default=str)
                       .encode()).hexdigest()[:16]


def collect_specs() -> list[dict]:
    out, seen = [], set()
    try:
        d = json.loads((PROJ / "data" / "factor_library.json").read_text())
        vals = d.get("factors", {}) if isinstance(d, dict) else d
        items = list(vals.values()) if isinstance(vals, dict) else vals
        for v in items:
            if isinstance(v, dict):
                s = v.get("spec", v)
                k = json.dumps(s, sort_keys=True, default=str)
                if k not in seen:
                    seen.add(k); out.append(s)
    except Exception:
        pass
    for f in sorted(glob.glob(str(PROJ / "runs" / "loop_*" / "round_*" / "mine_*.json"))):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for r in d.get("survivors", []):
            s = r.get("spec")
            if not s:
                continue
            k = json.dumps(s, sort_keys=True, default=str)
            if k in seen:
                continue
            seen.add(k); out.append(s)
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--extend", type=int, default=0,
                    help="续队列：每个 spec 生成 N 个新噪声种子任务")
    args = ap.parse_args()
    specs = collect_specs()
    done = set()
    if RESULTS.exists():
        done = {p.stem for p in RESULTS.glob("*.json")}

    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS tasks ("
                 "k TEXT PRIMARY KEY, kind TEXT, payload TEXT, "
                 "done INTEGER DEFAULT 0)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_done ON tasks(done)")

    rows = []
    for s in specs:
        for h in HOLDS:
            for tf in SPLITS:
                t = {"kind": "true_sharpe", "spec": s, "hold": h, "train_frac": tf}
                rows.append((key_of(t), "true_sharpe", json.dumps(t, default=str)))
        for w in ROLL_WINDOWS:
            t = {"kind": "rolling_ic", "spec": s, "window": w}
            rows.append((key_of(t), "rolling_ic", json.dumps(t, default=str)))
        t5 = {"kind": "five_dim", "spec": s}
        rows.append((key_of(t5), "five_dim", json.dumps(t5, default=str)))
        # CPCV 多路径 OOS：每个 spec 一份 Sharpe 分布（purgedcv，MIT）
        tc = {"kind": "cpcv", "spec": s, "hold": 5,
              "n_splits": 6, "n_test_groups": 2}
        rows.append((key_of(tc), "cpcv", json.dumps(tc, default=str)))

    if args.extend > 0:
        conn.row_factory = None
        cur = conn.execute("SELECT MAX(CAST(json_extract(payload,'$.seed') AS INTEGER)) "
                           "FROM tasks WHERE kind='noise_pfs'")
        base = cur.fetchone()[0] or 0
        for s in specs:
            for seed in range(base + 1, base + 1 + args.extend):
                t = {"kind": "noise_pfs", "spec": s, "seed": seed}
                rows.append((key_of(t), "noise_pfs",
                             json.dumps(t, default=str)))
        print("续队列：seed %d..%d，新增 %d 个 noise_pfs 任务"
              % (base + 1, base + args.extend, len(specs) * args.extend))

    conn.executemany(
        "INSERT OR IGNORE INTO tasks(k, kind, payload, done) VALUES (?,?,?,?)",
        [(k, kind, payload, 1 if k in done else 0) for k, kind, payload in rows])
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    n_done = conn.execute("SELECT COUNT(*) FROM tasks WHERE done=1").fetchone()[0]
    todo = conn.execute("SELECT COUNT(*) FROM tasks WHERE done=0").fetchone()[0]
    conn.close()
    print("队列(SQLite)：spec %d，任务 %d，已完成 %d，待跑 %d → %s"
          % (len(specs), total, n_done, todo, DB))
    return 0


if __name__ == "__main__":
    sys.exit(main())
