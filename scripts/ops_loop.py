#!/usr/bin/env python3
"""自主运营循环 —— 让量化系统在半无人值守下自我巡检、自愈、留痕。

这不是替代人，而是把我的"巡检-判断-处置"节奏固化成可重复执行的脚本，
并写一份可审计的运营日志。设计原则：
  - 只做**安全且幂等**的自愈动作（重启挂掉的服务、清理超期日志）。
  - 任何有风险的动作（改参数、平仓、删数据）只**报告**，不自动执行。
  - 每轮都留痕到 runs/ops/ops_<date>.jsonl。

自愈动作（白名单）：
  - quant-trader 挂了 → systemctl restart（配置未变，幂等安全）
  - quant-miner 挂了 → restart
  - runs/ 下 >2GB 的旧 loop 目录 → 报告（不自动删，交人确认，除非 --prune）

用法：
  python -m scripts.ops_loop --once        # 跑一次（cron 用）
  python -m scripts.ops_loop --loop         # 常驻，每 --interval 秒一轮
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
OPS_DIR = PROJ / "runs" / "ops"


def _sh(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _watch() -> dict:
    """调用 ops_watch 拿快照。"""
    rc, out = _sh([str(PROJ / "venv" / "bin" / "python"),
                   "-m", "scripts.ops_watch", "--json"])
    try:
        return json.loads(out)
    except Exception:
        return {"healthy": False, "alerts": [f"ops_watch 解析失败: {out[:200]}"]}


def _restart(name: str) -> str:
    rc, out = _sh(["systemctl", "restart", name], timeout=60)
    return f"restart {name}: rc={rc} {out[:100]}"


def act(rep: dict, do_prune: bool = False) -> list[str]:
    """根据快照执行白名单自愈动作，返回动作记录。"""
    actions: list[str] = []
    svcs = rep.get("services", {})
    # 只自愈交易与挖掘服务；y2a 属另一条线，仅报告由它自己的服务守护
    for name in ("quant-trader", "quant-miner"):
        if svcs.get(name) is False:
            actions.append(_restart(name))
    # 磁盘预警时清理超期 ops 日志（安全，只删自家 ops 日志）
    disk = rep.get("disk", {})
    if disk.get("free_gb", 999) < 2.0:
        n = 0
        cutoff = time.time() - 7 * 86400
        for p in OPS_DIR.glob("ops_*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(); n += 1
            except Exception:
                pass
        if n:
            actions.append(f"清理超期 ops 日志 {n} 个（磁盘紧张）")
    # 大目录只报告
    for d in sorted((PROJ / "runs").glob("loop_*")):
        try:
            sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            if sz > 2_000_000_000 and do_prune:
                actions.append(f"[建议] {d.name} 占 {sz/1e9:.1f}GB，可清理")
        except Exception:
            pass
    return actions


def one_round(do_prune: bool = False, verbose: bool = True) -> dict:
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    rep = _watch()
    actions = act(rep, do_prune=do_prune)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "healthy": rep.get("healthy"),
        "alerts": rep.get("alerts", []),
        "actions": actions,
        "snapshot_min": {
            "services": rep.get("services"),
            "disk_free_gb": rep.get("disk", {}).get("free_gb"),
            "mem_avail_mb": rep.get("mem", {}).get("avail_mb"),
            "trader_age_sec": (rep.get("trader_last_tick") or {}).get("age_sec"),
            "miner_running": (rep.get("miner") or {}).get("running"),
            "cumulative_trials": (rep.get("trials") or {}).get("cumulative_trials"),
            "library_factors": (rep.get("trials") or {}).get("library_factors"),
            "breakers": (rep.get("risk_recent") or {}).get("breakers"),
        },
    }
    # 追加写当日日志
    day = datetime.now(timezone.utc).date().isoformat()
    with (OPS_DIR / f"ops_{day}.jsonl").open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    if verbose:
        st = "✅" if entry["healthy"] else "⚠️"
        print(f"[{entry['ts']}] {st} alerts={entry['alerts']} "
              f"actions={entry['actions']}")
    return entry


def main() -> None:
    ap = argparse.ArgumentParser(description="量化自主运营循环")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--loop", action="store_true", help="常驻循环")
    ap.add_argument("--interval", type=int, default=1800, help="循环间隔秒")
    ap.add_argument("--prune", action="store_true", help="允许清理大目录")
    args = ap.parse_args()

    if args.loop:
        while True:
            try:
                one_round(do_prune=args.prune)
            except Exception as e:  # noqa: BLE001
                print(f"[ops_loop] 轮次异常: {e}")
            time.sleep(args.interval)
    else:
        one_round(do_prune=args.prune)


if __name__ == "__main__":
    main()
