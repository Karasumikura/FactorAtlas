#!/usr/bin/env python3
"""主控机（香港机）侧：生成共享提案池，供深圳挖矿机只读使用。

深圳机不做任何 LLM 推理（店长定死），所以提案必须在这里生成，随后由
``scripts/sync_panel_to_sz.sh`` 推到 ``state/pool/proposal_pool.json``。
挖矿机只读那个文件，池子缺失/过期就只用本地模板，绝不自行发起 LLM 请求。

持有期按 1 → 5 → 20 轮换并落盘（``state/pool/hold_ptr.json``），挖矿机直接
采用池文件里的 ``hold`` —— 这样「提案时的持有期提示」和「回测窗口」永远一致，
也不会因为挖矿进程重启（ROUND 归零）错位。

用法：
    python scripts/build_shared_pool.py            # 正常一次（cron 用）
    POOL_TOTAL=40 python scripts/build_shared_pool.py   # 临时缩小
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

PROJ = Path("/opt/quant-lab")
sys.path.insert(0, str(PROJ))

POOL_DIR = PROJ / "state" / "pool"
TARGET = POOL_DIR / "proposal_pool.json"
PTR = POOL_DIR / "hold_ptr.json"
HOLDS = [1, 5, 20]
TOTAL = int(os.environ.get("POOL_TOTAL", "80"))
KEEP_BUILDS = 3


def _next_hold() -> tuple[int, int]:
    try:
        nxt = int(json.loads(PTR.read_text()).get("next", 0))
    except Exception:
        nxt = 0
    return HOLDS[nxt % len(HOLDS)], (nxt + 1) % len(HOLDS)


def main() -> int:
    from core.proposal_pool import build_pool, pool_path

    POOL_DIR.mkdir(parents=True, exist_ok=True)
    hold, next_idx = _next_hold()

    work = PROJ / "runs" / ("pool_build_%s" % time.strftime("%Y%m%d_%H%M%S"))
    t0 = time.time()
    specs = build_pool(str(work), total=TOTAL, concurrency=1, hold=hold)
    dt = time.time() - t0

    src = pool_path(str(work), hold)
    if not Path(src).exists():
        print("build_pool 没写出池文件: %s" % src, file=sys.stderr)
        return 1

    # ---- 失败轮次归档：0 个候选的池不进生产、不进报告 ----
    # 2026-11-07 抄自另一边的试跑做法：「第一次是上游的问题」，把那次
    # 结果存档到 archive/、不计入报告，重跑。这里同理：LLM 整批 429 /
    # 上游挂掉时 build_pool 仍会写出一个 n_specs=0 的池，原子替换会把
    # 上一份健康池冲掉，挖矿机接着用空池空转一轮，报告里还多一条
    # 「本轮 0 候选」的假记录。
    # 只归档、不覆盖、不推进 hold_ptr；重跑用同一模型同一任务，
    # 不换模型、不加 fallback 链。
    payload = json.loads(Path(src).read_text())
    n_specs = int(payload.get("n_specs", 0))
    if n_specs == 0:
        archive = PROJ / "runs" / "archive" / (
            "pool_failed_%s" % time.strftime("%Y%m%d_%H%M%S"))
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work), str(archive))
        print("[归档] 本轮提案 0 个 error=%s → %s；生产池保持上一份健康版本，"
              "hold_ptr 不推进" % (payload.get("error"), archive),
              file=sys.stderr)
        return 1

    # 原子替换，避免同步进程读到半写入的文件
    tmp = TARGET.with_name(".proposal_pool.%d.tmp" % os.getpid())
    shutil.copyfile(src, tmp)
    os.replace(tmp, TARGET)
    PTR.write_text(json.dumps({"next": next_idx, "last_hold": hold,
                               "at": time.strftime("%F %T")}, ensure_ascii=False))

    # 只保留最近几次构建目录，别把 runs/ 撑爆
    builds = sorted((PROJ / "runs").glob("pool_build_*"))
    for old in builds[:-KEEP_BUILDS]:
        shutil.rmtree(old, ignore_errors=True)

    print("[%s] pool hold=%d specs=%d wall=%.0fs total_target=%d -> %s"
          % (time.strftime("%F %T"), hold, len(specs), dt, TOTAL, TARGET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
