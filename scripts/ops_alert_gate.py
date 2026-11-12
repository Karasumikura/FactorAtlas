#!/usr/bin/env python3
"""巡检告警闸门：同类别去重，只在「新增 / 消失后重现」时推送。

2026-11-07：quant-ops.timer 恢复后，一条「加密面板陈旧」每 30 分钟推一次，
把告警训成噪音。根因是巡检直接把整份审计报告无条件推给 QQ。

规则（与店长对监控告警的一贯要求一致）：
  - 取告警「类别」（冒号前的部分）做 key：文本里的天数、MB、分钟数变化
    属于同一等级的持续状态，不算新告警，不重推。
  - 新类别出现 → 推一条；该类别消失 → 推一条「已恢复」，状态清零。
  - 完全健康且无变化 → 静默，只往日志写一行计数。

只复用 scripts/ops_watch.snapshot()，不重复实现健康检查。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path("/opt/quant-lab")
sys.path.insert(0, str(PROJ))

STATE = PROJ / "runs" / "ops_alert_state.json"
HERMES = "/root/.hermes/hermes-agent/.hermes/bin/hermes"
TARGET = "qqbot:9AA889C7B4FFA2D0F90BC46F6A21B9C6"


def category(text: str) -> str:
    """取类别：冒号前。数字/日期变化不改变类别。"""
    for sep in (":", "："):
        if sep in text:
            return text.split(sep, 1)[0].strip()
    return text.strip()


def gate(alerts: list[str], state: dict, send) -> tuple[list[str], list[str]]:
    """纯逻辑：返回 (新增告警, 已消失类别)，便于自测不发消息。"""
    cur = {category(t): t for t in alerts}
    prev = state.get("active", {})
    new = [text for cat, text in cur.items() if cat not in prev]
    gone = [cat for cat in prev if cat not in cur]

    for text in new:
        send("⚠️ 量化巡检告警\n" + text)
    if gone:
        send("✅ 量化巡检恢复\n已消失: " + ", ".join(gone))

    state["active"] = cur
    state["ts"] = datetime.now(timezone.utc).isoformat()
    return new, gone


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (ValueError, OSError):
            return {"active": {}}
    return {"active": {}}


def send(text: str) -> None:
    env = dict(os.environ)
    env["HERMES_HOME"] = "/root/.hermes/profiles/pro-analysis"
    env["PATH"] = "/root/.hermes/hermes-agent/.hermes/bin:" + env.get("PATH", "")
    r = subprocess.run([HERMES, "send", "--to", TARGET, text],
                       env=env, capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        print(f"[gate] 推送失败: {r.stderr.strip()[:160]}", file=sys.stderr)


def _self_test() -> int:
    calls: list[str] = []
    st: dict = {"active": {}}
    gate(["服务未运行: quant-trader", "内存紧张: 可用 100MB < 150MB"], st, calls.append)
    assert len(calls) == 2, "两条新告警都应推送"
    gate(["服务未运行: quant-trader", "内存紧张: 可用 90MB < 150MB"], st, calls.append)
    assert len(calls) == 2, "同类别、仅数字变化不得重推"
    gate(["服务未运行: quant-trader", "盘口长期无信号: resonance 已 6h"], st, calls.append)
    assert len(calls) == 4, "1 条新增 + 1 条恢复"
    assert any("恢复" in c for c in calls[2:]), "消失的类别要推恢复"
    gate([], st, calls.append)
    assert len(calls) == 5, "全部恢复再推一条"
    n = len(calls)
    gate([], st, calls.append)
    assert len(calls) == n, "持续健康必须静默"
    print("self-test OK: 新增推送 / 同类别去重 / 恢复 / 静默 四态全过")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    from scripts import ops_watch
    rep = ops_watch.snapshot()
    alerts = list(rep.get("alerts") or [])
    st = load_state()
    new, gone = gate(alerts, st, send)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    print(f"[gate] ts={st['ts'][:19]} alerts={len(alerts)} new={len(new)} gone={len(gone)}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
