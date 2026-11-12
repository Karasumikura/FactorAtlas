#!/usr/bin/env python3
"""冷却期工作队列：把空闲 CPU 用在「不新增候选、不涨 overfit_guard 的 N」的任务上。

为什么需要：
  miner 每轮只跑 3~20 分钟，然后冷却 7200s。冷却不能简单缩短——每轮都会
  record_session(n_candidates) 累加累计试验数 N，N 涨得越快 DSR 门槛抬得越高，
  等于用运维动作偷偷加重过拟合惩罚。所以正确做法是：**冷却期跑评估类任务**。

任务表（每次调用只跑一个，游标持久化）：
  1. audit_real_sharpe  历史 survivor 用真实调仓口径重算，分批（最耗 CPU、最有价值）
  2. robust_us          跨配置稳健性体检（美股）
  3. robust_crypto      跨配置稳健性体检（加密）
  4. eval5_us           五维评估（美股）
  5. eval5_crypto       五维评估（加密）
  6. library_corr       因子库日度 IC 相关性矩阵（独立性检验）

输入没变的任务会按指纹跳过，避免每 5 分钟重复算同一份东西。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path("/opt/quant-lab")
sys.path.insert(0, str(PROJ))
CURSOR = PROJ / "state" / "idle_work_cursor.json"
FINGERPRINTS = PROJ / "state" / "idle_work_fingerprints.json"
PY = str(PROJ / "venv" / "bin" / "python")
AUDIT_OUT = PROJ / "runs" / "real_sharpe_audit.json"
AUDIT_BATCH = int(os.environ.get("AUDIT_BATCH", "60"))


def _load(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return dict(default)


def _save(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def _fingerprint() -> dict:
    """面板末日 + 因子库 mtime + 最近一轮目录，输入不变就没必要重算。"""
    fp = {}
    for mkt, rel in (("us", "data/hist/meta.json"),
                     ("crypto", "data/crypto/meta.json")):
        try:
            fp[mkt] = json.loads((PROJ / rel).read_text()).get("end")
        except Exception:
            fp[mkt] = None
    try:
        fp["library_mtime"] = int((PROJ / "data" / "factor_library.json").stat().st_mtime)
    except Exception:
        fp["library_mtime"] = None
    loops = sorted(glob.glob(str(PROJ / "runs" / "loop_*")))
    fp["latest_loop"] = Path(loops[-1]).name if loops else None
    return fp


def _run_script(script: str, *args: str, timeout: int = 200) -> int:
    r = subprocess.run([PY, str(PROJ / "scripts" / script), *args],
                       cwd=str(PROJ), capture_output=True, text=True, timeout=timeout)
    tail = (r.stdout or "").strip().splitlines()[-3:]
    if tail:
        print("  " + " | ".join(tail), flush=True)
    if r.returncode != 0 and (r.stderr or "").strip():
        print("  stderr: " + (r.stderr or "").strip().splitlines()[-2:][-1][:200], flush=True)
    return r.returncode


# ── 任务 1：历史 survivor 真实调仓口径审计 ──────────────────────────────
def _collect_survivors() -> list[dict]:
    out, seen = [], set()
    for f in sorted(glob.glob(str(PROJ / "runs" / "loop_*" / "round_*" /
                                 "mine_*.json"))):
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
            out.append({"spec": s,
                        "amortized": r.get("oos_ls_sharpe"),
                        "hold": d.get("hold"),
                        "source": f.split("/runs/")[-1]})
    return out


def task_audit_real_sharpe(state: dict) -> str:
    from core.data import load_long_panel
    from core.true_rebalance_gate import split_true_sharpe

    items = _collect_survivors()
    done = _load(AUDIT_OUT, {"items": {}, "generated_at": None})
    todo = [x for x in items
            if json.dumps(x["spec"], sort_keys=True, default=str)
            not in done.get("items", {})]
    if not todo:
        return "audit: 全部已审完（%d 个）" % len(items)

    panel = load_long_panel()
    batch = todo[:AUDIT_BATCH]
    for x in batch:
        key = json.dumps(x["spec"], sort_keys=True, default=str)
        hold = int(x.get("hold") or 1)
        rec = {"source": x["source"], "hold": hold,
               "amortized_oos": x.get("amortized")}
        if hold <= 1:
            rec["true_is"] = rec["true_oos"] = None
            rec["note"] = "hold=1 原口径合法，不重算"
        else:
            is_t, oos_t, n_is, n_oos = split_true_sharpe(
                panel, x["spec"], hold, train_frac=0.7)
            rec.update({"true_is": is_t if is_t == is_t else None,
                        "true_oos": oos_t if oos_t == oos_t else None,
                        "n_periods": [n_is, n_oos]})
        done["items"][key] = rec
    done["generated_at"] = datetime.now(timezone.utc).isoformat()
    done["n_total"] = len(items)
    done["n_done"] = len(done["items"])
    _save(AUDIT_OUT, done)
    return "audit: 本批 %d 个，累计 %d/%d" % (len(batch), done["n_done"], len(items))


# ── 任务 6：因子库独立性（日度 IC 相关性）────────────────────────────
def task_library_corr(state: dict) -> str:
    import numpy as np
    import pandas as pd
    from core.alpha_eval5 import _daily_rank_ic
    from core.data import load_long_panel
    from factors.engine import factor_series_to_frame

    d = json.loads((PROJ / "data" / "factor_library.json").read_text())
    vals = d.get("factors", {}) if isinstance(d, dict) else d
    items = list(vals.values()) if isinstance(vals, dict) else vals
    specs = [v.get("spec", v) for v in items if isinstance(v, dict)]
    if len(specs) < 2:
        return "library_corr: 因子不足 2 个"

    panel = load_long_panel()
    fwd = panel["c"].pct_change(1).shift(-1)
    ics = {}
    for s in specs[:20]:
        try:
            ics[str(s.get("name"))] = _daily_rank_ic(
                factor_series_to_frame(panel, s), fwd)
        except Exception:
            continue
    names = list(ics)
    if len(names) < 2:
        return "library_corr: 可算因子不足"
    mat = pd.DataFrame({n: ics[n] for n in names}).dropna()
    while mat.shape[1] > 2 and mat.shape[0] < mat.shape[1] + 1:
        mat = mat.iloc[:, :-1]
    corr = mat.corr().round(3)
    out = PROJ / "runs" / "library_corr.json"
    out.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_factors": len(corr),
        "n_days": int(mat.shape[0]),
        "corr": corr.to_dict(),
        "max_abs_offdiag": round(float(
            np.nanmax(np.abs(corr.values - np.eye(len(corr))))), 3)
        if len(corr) > 1 else None,
    }, ensure_ascii=False, indent=1))
    return "library_corr: %d 个因子，最大非对角 |corr|=%s" % (
        len(corr), round(float(np.nanmax(np.abs(corr.values - np.eye(len(corr))))), 3))


TASKS = [
    ("audit_real_sharpe", None, task_audit_real_sharpe),
    ("robust_us", ("robustness_scan.py", "--market", "us"), None),
    ("eval5_us", ("alpha_eval5_scan.py", "--market", "us"), None),
    ("robust_crypto", ("robustness_scan.py", "--market", "crypto"), None),
    ("eval5_crypto", ("alpha_eval5_scan.py", "--market", "crypto"), None),
    ("library_corr", None, task_library_corr),
]


def main() -> int:
    if subprocess.run(["pgrep", "-f", "[c]ore\\.mine|[c]ore\\.promote"],
                      capture_output=True).returncode == 0:
        print("[idle] 挖掘/晋升进行中，跳过", flush=True)
        return 0

    cur = _load(CURSOR, {"next": 0})
    fps = _load(FINGERPRINTS, {})
    fp_now = _fingerprint()

    ran = 0
    for _ in range(len(TASKS)):
        idx = int(cur.get("next", 0)) % len(TASKS)
        name, script_args, fn = TASKS[idx]
        cur["next"] = (idx + 1) % len(TASKS)

        # 输入没变的任务跳过（audit 自带分批游标，不跳）
        if script_args is not None:
            fp_key = name
            if fps.get(fp_key) == fp_now and name != "audit_real_sharpe":
                print("[idle] %s 输入未变，跳过" % name, flush=True)
                continue

        t0 = time.time()
        print("[idle] 开始 %s %s" % (name, datetime.now().strftime("%F %T")), flush=True)
        try:
            if fn is not None:
                msg = fn(cur)
                print("[idle] %s 完成 %.1fs | %s" % (name, time.time() - t0, msg), flush=True)
            else:
                rc = _run_script(*script_args, timeout=200)
                print("[idle] %s 完成 %.1fs rc=%d" % (name, time.time() - t0, rc), flush=True)
                fps[name] = fp_now
        except Exception as exc:  # noqa: BLE001
            print("[idle] %s 异常: %s: %s" % (name, type(exc).__name__, str(exc)[:160]),
                  flush=True)
        ran += 1
        _save(CURSOR, cur)
        _save(FINGERPRINTS, fps)
        if ran >= 1:          # 单次只跑一个任务，控制在 timer 间隔内
            break

    _save(CURSOR, cur)
    return 0


if __name__ == "__main__":
    sys.exit(main())
