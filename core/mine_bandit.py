"""多臂老虎机调度：用历史产出决定下一轮挖哪个市场。

借鉴 RD-Agent(Q)（arXiv:2505.15155, MIT）的「多臂老虎机自适应方向选择」，
但只借调度设计，不搬框架（执行机零 LLM 是架构约束）。

硬边界：
  - 只选 market，**不选 hold** —— hold 必须跟提案池一致
    （2026-11-07 已确认 mine_loop.sh:181 的 HOLD="$POOL_HOLD" 对齐机制）。
  - 奖励用「本轮 survivor 数」，不用 survivor/耗时：crypto 轮天生快 5 倍，
    用速率会让老虎机把所有轮次都喂给币，饿死美股。
  - 状态落盘，跨进程重启有效；读不到就回退到固定轮换，绝不阻塞挖掘。

来源：
  https://arxiv.org/abs/2505.15155  (R&D-Agent-Quant)
  https://github.com/microsoft/RD-Agent  (MIT)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE = "/opt/quant-lab/state/mine_bandit.json"


def _load(path: str) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        if isinstance(d, dict) and "arms" in d:
            return d
    except Exception:
        pass
    return {"arms": {}, "total_pulls": 0, "updated_at": None}


def _save(path: str, d: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def choose(markets: list[str], state_path: str, c: float = 1.0) -> str:
    d = _load(state_path)
    arms = d["arms"]
    total = max(int(d.get("total_pulls", 0)), 1)
    best, best_score = markets[0], -float("inf")
    for m in markets:
        a = arms.get(m) or {}
        n = int(a.get("pulls", 0))
        if n == 0:
            return m                      # 未拉过的臂优先，保证每个市场都被试过
        mean = float(a.get("total_reward", 0.0)) / n
        score = mean + c * math.sqrt(2.0 * math.log(total) / n)
        if score > best_score:
            best, best_score = m, score
    return best


def record(markets: list[str], state_path: str, market: str,
           reward: float) -> dict:
    d = _load(state_path)
    arms = d["arms"]
    for m in markets:
        arms.setdefault(m, {"pulls": 0, "total_reward": 0.0})
    if market not in arms:
        return d
    arms[market]["pulls"] = int(arms[market]["pulls"]) + 1
    arms[market]["total_reward"] = float(arms[market]["total_reward"]) + float(reward)
    d["total_pulls"] = int(d.get("total_pulls", 0)) + 1
    d["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(state_path, d)
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", default=["us", "crypto"])
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--c", type=float, default=1.0)
    ap.add_argument("--record", default=None, help="记录哪个臂的结果")
    ap.add_argument("--reward", type=float, default=0.0)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if args.record:
        d = record(args.markets, args.state, args.record, args.reward)
        print("record %s reward=%.2f state=%s" % (args.record, args.reward,
                                                  json.dumps(d["arms"])))
        return 0
    if args.show:
        print(json.dumps(_load(args.state), ensure_ascii=False, indent=1))
        return 0
    print(choose(args.markets, args.state, c=args.c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
