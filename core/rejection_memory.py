#!/usr/bin/env python3
"""失败假设结构化记忆（纯程序化、不涉 LLM）。

数据结构抄自 gyx09212214-proj/worldquant-harness（MIT，见 README）：
其 docs/AGENT_HARNESS_CONTRACT.md 把 hypotheses.jsonl / review_decisions.jsonl /
reflection_records.jsonl / memory_delta.jsonl 分开存，核心一句是
「Failures become structured memory and next-round constraints」。
本项目只抄这个分层思路，用轻量 JSONL，不搬它的 FastAPI/React/SQLAlchemy。

用途：30 轮 0 晋升、每轮重新撒网。把「哪些算子/参数区被证伪」沉淀下来，
下一轮程序化进化搜索直接避开，而不是重新试一遍。
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path("/opt/quant-lab")
MEM = PROJ / "state" / "rejection_memory"
CANDIDATES = MEM / "candidates.jsonl"
DECISIONS = MEM / "decisions.jsonl"
CONSTRAINTS = MEM / "constraints.json"


def spec_key(spec: dict) -> str:
    if spec.get("kind") == "combo" or "expr" in spec:
        payload = {"expr": spec.get("expr"), "direction": spec.get("direction", 1)}
    else:
        payload = {"op": spec.get("op"), "params": spec.get("params", {}),
                   "direction": spec.get("direction", 1)}
    return json.dumps(payload, sort_keys=True, default=str)


def _spec_from_row(r: dict) -> dict:
    s = r.get("spec")
    if isinstance(s, dict) and s:
        return s
    return {"name": r.get("name"), "op": r.get("op"),
            "params": r.get("params") or {}, "direction": r.get("direction", 1)}


def _decision(r: dict) -> tuple[str, str]:
    """返回 (decision, reason)，照 wq-harness 的 review_decisions 分类。"""
    if r.get("survivor"):
        return "survivor", "passed_fdr_and_oos"
    if not r.get("fdr_pass"):
        return "rejected", "fdr_not_passed"
    if r.get("oos_confirmed") is False:
        return "rejected", "oos_not_confirmed"
    oos = r.get("oos_ls_sharpe")
    if oos is None or oos != oos:  # NaN
        return "rejected", "oos_sharpe_nan"
    return "rejected", "gate_other"


def ingest_runs(pattern: str | None = None, limit: int = 400) -> dict:
    """扫历史 mine json，写 candidates.jsonl + decisions.jsonl（按 spec_key 去重、保留最新）。"""
    MEM.mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(pattern or str(PROJ / "runs" / "loop_*" / "round_*" /
                                            "mine_*.json")))[-limit:]
    seen: dict[str, dict] = {}
    events: list[dict] = []
    for f in files:
        try:
            d = json.loads(Path(f).read_text())
        except Exception:  # noqa: BLE001
            continue
        run_id = "/".join(Path(f).parts[-3:])
        for r in d.get("all_results") or []:
            spec = _spec_from_row(r)
            k = spec_key(spec)
            dec, reason = _decision(r)
            rec = {"run_id": run_id, "spec_key": k, "spec": spec,
                   "source": r.get("source"), "hold": d.get("hold"),
                   "market": d.get("market"),
                   "p_value": r.get("p_value"), "fdr_pass": r.get("fdr_pass"),
                   "ls_sharpe": r.get("ls_sharpe"),
                   "oos_ls_sharpe": r.get("oos_ls_sharpe"),
                   "oos_confirmed": r.get("oos_confirmed"),
                   "decision": dec, "reason": reason}
            seen[k] = rec
            events.append({"run_id": run_id, "spec_key": k, "decision": dec,
                           "reason": reason})
    # 原子替换
    for path, rows in ((CANDIDATES, list(seen.values())), (DECISIONS, events[-20000:])):
        tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
        with tmp.open("w", encoding="utf-8") as fh:
            for x in rows:
                fh.write(json.dumps(x, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, path)
    return {"runs": len(files), "unique_specs": len(seen), "events": len(events)}


def build_constraints(min_trials: int = 20, fail_rate: float = 0.90) -> dict:
    """聚合出下一轮约束：高失败率算子 + 明确被证伪的 spec_key。"""
    by_op: dict[str, list[bool]] = defaultdict(list)
    rejected_keys: list[str] = []
    total = 0
    if CANDIDATES.exists():
        with CANDIDATES.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                total += 1
                spec = r.get("spec") or {}
                op = spec.get("op") or (spec.get("expr") or {}).get("op")
                if op:
                    by_op[str(op)].append(r.get("decision") == "survivor")
                if r.get("decision") == "rejected" and r.get("reason") in (
                        "fdr_not_passed", "oos_not_confirmed"):
                    rejected_keys.append(r.get("spec_key"))
    avoid_ops = sorted(op for op, flags in by_op.items()
                       if len(flags) >= min_trials
                       and (1.0 - sum(flags) / len(flags)) >= fail_rate)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_records": total,
        "min_trials": min_trials, "fail_rate": fail_rate,
        "avoid_ops": avoid_ops,
        "n_rejected_keys": len(set(rejected_keys)),
        "rejected_keys": sorted(set(rejected_keys))[-5000:],
        "op_stats": {op: {"n": len(f), "survivors": int(sum(f))}
                     for op, f in sorted(by_op.items())},
    }
    MEM.mkdir(parents=True, exist_ok=True)
    tmp = CONSTRAINTS.with_name(CONSTRAINTS.name + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    os.replace(tmp, CONSTRAINTS)
    return out


def load_constraints() -> dict:
    try:
        return json.loads(CONSTRAINTS.read_text())
    except Exception:  # noqa: BLE001
        return {"avoid_ops": [], "rejected_keys": []}


def should_avoid(spec: dict, constraints: dict | None = None) -> bool:
    c = constraints if constraints is not None else load_constraints()
    if spec_key(spec) in set(c.get("rejected_keys") or []):
        return True
    op = spec.get("op") or (spec.get("expr") or {}).get("op")
    return bool(op and op in set(c.get("avoid_ops") or []))
