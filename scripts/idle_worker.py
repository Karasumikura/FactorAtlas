#!/usr/bin/env python3
"""常驻评估 worker（SQLite 队列版）：持续消费任务，把空闲 CPU 顶满。

边界：
  - 只做评估，不新增候选 → 不调用 record_session，不抬 DSR 的 N。
  - miner 轮次（core.mine / core.promote）一出现就释放面板并放弃剩余批次，
    给挖掘让出内存和 CPU；挖掘结束自动恢复。
  - 队列 = state/idle_queue.db（WAL，支持 2 个 worker 并发取任务）。
  - 结果落 runs/idle_results/<k>.json。

用法：
  venv/bin/python scripts/idle_worker.py --slot 1
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path("/opt/quant-lab")
sys.path.insert(0, str(PROJ))
DB = PROJ / "state" / "idle_queue.db"
RESULTS = PROJ / "runs" / "idle_results"
PANEL = None


def key_of(task: dict) -> str:
    return hashlib.md5(json.dumps(task, sort_keys=True, default=str)
                       .encode()).hexdigest()[:16]


def mining_active() -> bool:
    # [c]ore 正则技巧：避免 pgrep 匹配到「正在执行 pgrep 的同类进程」，
    # 否则 worker 会永久误判为挖掘中而一直让路（2026-11-10 实测 CPU 2%）。
    return subprocess.run(["pgrep", "-f", "[c]ore\\.mine|[c]ore\\.promote"],
                          capture_output=True).returncode == 0


def get_panel():
    global PANEL
    if PANEL is None:
        from core.data import load_long_panel
        PANEL = load_long_panel()
    return PANEL


def drop_panel():
    global PANEL
    if PANEL is not None:
        PANEL = None
        gc.collect()


def take_tasks(batch: int = 4) -> list[dict]:
    if not DB.exists():
        return []
    conn = sqlite3.connect(str(DB), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT k, payload FROM tasks WHERE done=0 LIMIT ?", (batch,)
        ).fetchall()
        if rows:
            conn.executemany("UPDATE tasks SET done=1 WHERE k=?",
                             [(r[0],) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return [json.loads(r[1]) for r in rows]


def run_task(t: dict) -> dict:
    import numpy as np
    from core.true_rebalance_gate import split_true_sharpe
    from core.alpha_eval5 import _daily_rank_ic
    from factors.engine import factor_series_to_frame

    panel = get_panel()
    spec = t["spec"]
    kind = t["kind"]
    if kind == "true_sharpe":
        is_t, oos_t, n_is, n_oos = split_true_sharpe(
            panel, spec, int(t["hold"]), train_frac=float(t["train_frac"]))
        return {"kind": kind, "hold": t["hold"], "train_frac": t["train_frac"],
                "is_sharpe": None if is_t != is_t else round(float(is_t), 4),
                "oos_sharpe": None if oos_t != oos_t else round(float(oos_t), 4),
                "n_periods": [n_is, n_oos]}
    if kind == "rolling_ic":
        f = factor_series_to_frame(panel, spec)
        fwd = panel["c"].pct_change(1).shift(-1)
        ic = _daily_rank_ic(f, fwd)
        w = int(t["window"])
        if len(ic) < w * 2:
            return {"kind": kind, "window": w, "n_windows": 0}
        seg = [ic.iloc[i:i + w].mean() for i in range(0, len(ic) - w + 1, w)]
        seg = [float(x) for x in seg if x == x]
        return {"kind": kind, "window": w, "n_windows": len(seg),
                "ic_mean": round(float(np.mean(seg)), 5) if seg else None,
                "ic_std": round(float(np.std(seg)), 5) if seg else None,
                "same_sign_share": round(float(np.mean([x > 0 for x in seg])), 3)
                if seg else None}
    if kind == "five_dim":
        from core.alpha_eval5 import _rre, _pfs, _logic
        f = factor_series_to_frame(panel, spec)
        fwd = panel["c"].pct_change(1).shift(-1)
        ic = _daily_rank_ic(f, fwd)
        return {"kind": kind,
                "pps_ic": round(float(ic.mean()), 5) if len(ic) else None,
                "pps_icir": round(float(ic.mean() / ic.std()), 5)
                            if len(ic) > 2 and ic.std() > 0 else None,
                "rre": round(_rre(f), 5),
                "pfs_gauss": round(_pfs(f, "gauss"), 5),
                "pfs_t": round(_pfs(f, "t"), 5),
                "logic": _logic(spec)}
    if kind == "noise_pfs":
        from core.alpha_eval5 import NOISE_VAR
        f = factor_series_to_frame(panel, spec)
        fwd = panel["c"].pct_change(1).shift(-1)
        ic = _daily_rank_ic(f, fwd)
        rng = np.random.default_rng(int(t["seed"]))
        var = float(f.var().mean())
        noise = rng.normal(0.0, np.sqrt(NOISE_VAR * var),
                           size=(len(f), len(f.columns)))
        f_noisy = f + noise
        ic_noisy = _daily_rank_ic(f_noisy, fwd)
        both = np.column_stack([ic.values, ic_noisy.values])
        both = both[~np.isnan(both).any(axis=1)]
        c = float(np.corrcoef(both[:, 0], both[:, 1])[0, 1]) if len(both) > 2 else None
        return {"kind": kind, "seed": t["seed"],
                "pfs_corr": round(c, 5) if c == c else None,
                "n_days": len(both)}
    if kind == "cpcv":
        from core.cpcv_eval import cpcv_sharpe_distribution
        r = cpcv_sharpe_distribution(panel, spec, hold=int(t.get("hold", 5)),
                                     n_splits=int(t.get("n_splits", 6)),
                                     n_test_groups=int(t.get("n_test_groups", 2)))
        r.update({"kind": kind, "spec": spec})
        return r
    raise ValueError("unknown kind: " + str(kind))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=1)
    args = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    print("[worker%d] 启动（SQLite 队列）" % args.slot, flush=True)

    idle_rounds = 0
    while True:
        if mining_active():
            drop_panel()
            if idle_rounds % 20 == 0:
                print("[worker%d] 挖掘中，让路" % args.slot, flush=True)
            idle_rounds += 1
            time.sleep(10)
            continue

        batch = take_tasks()
        if not batch:
            # 队列跑完就自动续一批（seed 递增，任务永不重复），别让 CPU 闲着。
            try:
                subprocess.run(
                    [sys.executable, str(PROJ / "scripts" / "build_idle_queue.py"),
                     "--extend", "20"], cwd=str(PROJ),
                    capture_output=True, timeout=120)
                print("[worker%d] 队列跑完，已自动续队列" % args.slot, flush=True)
                continue
            except Exception as exc:  # noqa: BLE001
                print("[worker%d] 续队列失败: %s" % (args.slot, exc), flush=True)
            drop_panel()
            idle_rounds += 1
            time.sleep(20)
            continue

        for i, t in enumerate(batch):
            if mining_active():
                drop_panel()
                print("[worker%d] 挖掘启动，放弃本批剩余 %d 个"
                      % (args.slot, len(batch) - i), flush=True)
                break
            k = key_of(t)
            t0 = time.time()
            try:
                res = run_task(t)
                res["spec"] = t["spec"]
                res["wall_s"] = round(time.time() - t0, 3)
                res["ts"] = datetime.now(timezone.utc).isoformat()
            except Exception as exc:  # noqa: BLE001
                res = {"error": type(exc).__name__ + ": " + str(exc)[:200],
                       "spec": t["spec"],
                       "ts": datetime.now(timezone.utc).isoformat()}
            (RESULTS / (k + ".json")).write_text(
                json.dumps(res, ensure_ascii=False, default=str))
        idle_rounds = 0


if __name__ == "__main__":
    sys.exit(main())
