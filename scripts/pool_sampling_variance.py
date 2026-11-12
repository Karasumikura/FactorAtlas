#!/usr/bin/env python3
"""同任务多次采样的输出稳定性度量（对应「同任务采样间标准差」）。

图里那张模型横评表最值得抄的不是综合分，是「同一个任务跑 6 次，结果
波动多大」。我们这边一直只有单次提案 + 失败重试，没有输出方差这个量。

做法：固定同一个任务配置，串行跑 N 次 propose_batch（内部仍按它自己的
并发数发请求），每次收集 spec 指纹集合，算两两 Jaccard 相似度的
均值 / 标准差 / 跨采样复现率。

硬边界：
  - 只用当前主模型路由，不换模型、不加 fallback 链（店长明令禁止）。
  - 不在生产 cron 里跑，默认 N=3，手动或单独低频触发，不烧主链路配额。
  - LLM 返回空就如实记 n=0，不编数据、不重跑补数。
  - 结果只落 state/pool/sampling_variance.json，不进主报告。

用法：
    venv/bin/python scripts/pool_sampling_variance.py            # N=3
    SAMPLES=6 venv/bin/python scripts/pool_sampling_variance.py  # 对齐图里的 6 次
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

PROJ = Path("/opt/quant-lab")
sys.path.insert(0, str(PROJ))

OUT = PROJ / "state" / "pool" / "sampling_variance.json"
SAMPLES = int(os.environ.get("SAMPLES", "3"))
TOTAL_PER_SAMPLE = int(os.environ.get("TOTAL_PER_SAMPLE", "12"))


def fingerprint(s: dict) -> str:
    return json.dumps({"op": s.get("op"), "params": s.get("params"),
                       "direction": s.get("direction")},
                      sort_keys=True, default=str)


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def main() -> int:
    if os.environ.get("QUANT_LLM_DISABLED", "").strip() == "1":
        print("本机 LLM 被禁用，采样方差只能在控制机跑", file=sys.stderr)
        return 2

    from llm.proposer_batch import propose_batch

    runs = []
    for i in range(SAMPLES):
        t0 = time.perf_counter()
        try:
            specs = propose_batch(total=TOTAL_PER_SAMPLE, concurrency=2,
                                  per_call=6, tried=[], verbose=False,
                                  hold=1) or []
        except Exception as exc:  # noqa: BLE001
            print("采样 %d 调用异常: %s" % (i + 1, str(exc)[:120]), file=sys.stderr)
            specs = []
        fps = sorted({fingerprint(s) for s in specs})
        runs.append({"i": i + 1, "n": len(specs), "n_unique": len(fps),
                     "wall_s": round(time.perf_counter() - t0, 2),
                     "specs": fps})
        print("采样 %d/%d: 候选 %d 唯一 %d 耗时 %.1fs"
              % (i + 1, SAMPLES, len(specs), len(fps), runs[-1]["wall_s"]),
              flush=True)

    pairs = [jaccard(set(a["specs"]), set(b["specs"]))
             for a, b in combinations(runs, 2)]
    # 跨采样复现率：出现在 >=2 次采样里的 spec 占全部出现过的 spec 的比例
    counts: dict[str, int] = {}
    for r in runs:
        for f in r["specs"]:
            counts[f] = counts.get(f, 0) + 1
    reproduced = sum(1 for v in counts.values() if v >= 2)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": "current-main-model-route",
        "n_samples": SAMPLES,
        "total_per_sample": TOTAL_PER_SAMPLE,
        "metric": "pairwise Jaccard of spec sets",
        "pairwise_jaccard": [round(x, 4) for x in pairs],
        "jaccard_mean": round(statistics.fmean(pairs), 4) if pairs else None,
        # 图里那个「同任务采样间标准差」的对应物
        "jaccard_std": round(statistics.pstdev(pairs), 4) if len(pairs) > 1 else None,
        "n_unique_specs_total": len(counts),
        "n_reproduced_in_2plus": reproduced,
        "reproduction_rate": round(reproduced / len(counts), 4) if counts else None,
        "per_sample_n": [r["n"] for r in runs],
        "n_std": round(statistics.pstdev([r["n"] for r in runs]), 4)
                 if len(runs) > 1 else None,
        "wall_s": [r["wall_s"] for r in runs],
        "failed_samples": sum(1 for r in runs if r["n"] == 0),
        "runs": runs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n同任务采样间标准差 Jaccard std = %s | 均值 = %s | 复现率 = %s | 失败采样 = %d"
          % (result["jaccard_std"], result["jaccard_mean"],
             result["reproduction_rate"], result["failed_samples"]))
    print("落盘: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
