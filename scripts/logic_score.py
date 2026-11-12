#!/usr/bin/env python3
"""AlphaEval 第五维 LOGIC：用 Magpie 当前主模型给正式因子库打金融逻辑分。

边界（店长 2026-11-09 四次授权）：
  - 只用 **当前主模型**，不传 model 参数、不枚举模型 → 无模型切换；
  - 单次调用，并发 = 1（≤ 2）；
  - 若将来需要会产生费用的调用，必须先请求店长、拿到明确同意；
  - 失败如实记录，不重试到别的模型、不加 fallback。

原版 AlphaEval (KDD 2026, MIT) 这一维是让 LLM 打 50-100 分。
执行机（深圳）零 LLM，所以之前 core/alpha_eval5.py 用结构规则分替代；
这里在控制机离线补一次 LLM 逻辑分，落盘供对照，不接生产链路。

用法：venv/bin/python scripts/logic_score.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path("/opt/quant-lab")
sys.path.insert(0, str(PROJ))
OUT = PROJ / "state" / "pool" / "logic_scores.json"


def library_specs() -> list[dict]:
    d = json.loads((PROJ / "data" / "factor_library.json").read_text())
    vals = d.get("factors", {}) if isinstance(d, dict) else d
    items = vals.values() if isinstance(vals, dict) else vals
    return [v.get("spec", v) for v in items if isinstance(v, dict)]


def main() -> int:
    specs = library_specs()
    if not specs:
        print("因子库为空，跳过", file=sys.stderr)
        return 1

    brief = [{"name": s.get("name"), "op": s.get("op"),
              "params": s.get("params"), "direction": s.get("direction")}
             for s in specs]
    prompt = (
        "You are a quantitative research reviewer. Score each factor's "
        "FINANCIAL LOGIC (economic rationale, why it might predict returns) "
        "from 0 to 100. Return ONLY a JSON array, one object per factor, "
        "each with keys: name (string), score (number), reason (one short sentence). "
        "No markdown, no prose.\n\nFactors:\n"
        + json.dumps(brief, ensure_ascii=False)
    )

    from llm.client import chat  # 不传 model → 当前主模型
    t0 = time.perf_counter()
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": "current-main-model",
        "n_factors": len(specs),
        "concurrency": 1,
        "cost": "free-quota only",
        "scores": [],
        "error": None,
    }
    try:
        text = chat([{"role": "user", "content": prompt}],
                    temperature=0.2, max_tokens=4000, timeout=240, retries=2)
        m = re.search(r"\[[\s\S]*\]", text)
        arr = json.loads(m.group(0)) if m else []
        by_name = {str(x.get("name")): x for x in arr if isinstance(x, dict)}
        for b in brief:
            x = by_name.get(str(b["name"]), {})
            score = x.get("score")
            try:
                score = max(0.0, min(100.0, float(score)))
            except (TypeError, ValueError):
                score = None
            result["scores"].append({"name": b["name"], "llm_logic": score,
                                     "reason": str(x.get("reason", ""))[:160]})
        result["wall_s"] = round(time.perf_counter() - t0, 1)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        result["wall_s"] = round(time.perf_counter() - t0, 1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("主模型逻辑评分：%d 个因子，耗时 %ss，error=%s" %
          (len(result["scores"]), result["wall_s"], result["error"]))
    for x in result["scores"]:
        print("  %-34s logic=%s  %s" % (str(x["name"])[:34],
                                        x["llm_logic"], x["reason"][:60]))
    print("落盘:", OUT)
    return 0 if not result["error"] else 1


if __name__ == "__main__":
    sys.exit(main())
