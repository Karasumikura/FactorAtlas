#!/usr/bin/env bash
# 量化运营巡检（cron 每 30 分钟）—— 自愈 + 留痕，安静模式：
# 只有出现告警或执行了自愈动作时才输出（否则静默，避免刷屏）。
set -uo pipefail
cd /opt/quant-lab

PY=/opt/quant-lab/venv/bin/python

# ── 第一层：预设自愈（重启服务、清日志等固定动作）────────────────────
OUT=$($PY -m scripts.ops_loop --once 2>&1)
if echo "$OUT" | grep -qE "⚠️|actions=\[.{2,}"; then
    echo "$OUT"
fi

# ── 第二层：LLM 自愈（只修代码异常，无法预设的 bug）──────────────────
# 为什么加这一层（用户 2026-10-17 要求）：
#   预设自愈只能重启服务和清日志，遇到代码 bug 只能报警。
#   当天 mine.py 的 NameError 导致 30 次挖掘全废，就属于这类。
#   现在让 LLM 读 Traceback → 生成补丁 → 三道闸门校验 → 通过才落盘。
#
# 安全边界（见 core/self_repair.py）：
#   - 只允许改白名单文件（core/ factors/ llm/ scripts/），绝不碰 data/ .env tests/
#   - 语法 + 行数上限(60) + 全套单元测试，任一失败立即回滚
#   - 同一问题 6 小时冷却，避免反复烧 token
#   - 每次尝试（含失败）都写审计日志到 runs/self_repair/
REPAIR=$($PY -m core.self_repair --apply --max-repairs 1 2>&1)
if echo "$REPAIR" | grep -qE "✅|❌"; then
    echo "── LLM 自愈 ──"
    echo "$REPAIR"
fi
