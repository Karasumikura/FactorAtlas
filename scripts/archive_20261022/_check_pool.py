"""离线验证池子配额分配（不调用 LLM，全部 mock）。"""
import sys
sys.path.insert(0, "/opt/quant-lab")
import tempfile, unittest.mock as mock
from core import proposal_pool as PP

comp = {"name": "c", "kind": "combo", "expr": {"op": "div"}, "direction": 1}

def run(total, single_n, adaptive=False, label=""):
    d = tempfile.mkdtemp()
    singles = [{"name": f"s{i}", "op": "vol", "params": {"n": 7 + i},
                "direction": 1} for i in range(single_n)]
    with mock.patch("llm.proposer_batch.propose_batch", return_value=singles) as ms, \
         mock.patch("llm.proposer_composite.propose_composite_batch",
                    return_value=[comp]) as mc:
        got = PP.build_pool(d, total=total, adaptive=adaptive, verbose=False)
    return {
        "label": label, "total": total,
        "单算子目标": ms.call_args.kwargs["total"],
        "复合目标": mc.call_args.kwargs["total"],
        "池内": len(got),
    }

print("=== 池子规模 600（旧） vs 300（新），单算子产出充足 ===")
for t in (600, 300):
    print(run(t, 999, label=f"total={t}"))

print("\n=== 单算子产出为 0（旧版会触发的场景）===")
print("旧行为 (adaptive 曾默认开启):", run(600, 0, adaptive=True, label="600/adaptive"))
print("新默认 (adaptive=False)     :", run(300, 0, adaptive=False, label="300/default"))
