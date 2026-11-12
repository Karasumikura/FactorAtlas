#!/usr/bin/env bash
# 冷却期工作队列：挖掘/晋升不在跑时执行，只做评估、不新增候选，
# 不抬 overfit_guard 的累计试验数 N。
# 任务轮转（跨配置体检 / 五维评估 / 历史真实 Sharpe 审计 / 因子库相关性）
# 见 scripts/idle_work.py；输入未变的任务按指纹自动跳过，避免空转。
set -uo pipefail
cd /opt/quant-lab || exit 0
if pgrep -f "[c]ore\.mine|[c]ore\.promote" >/dev/null 2>&1; then
  echo "[$(date "+%F %T")] 挖掘/晋升中，跳过"; exit 0
fi
exec nice -n 10 timeout 280 ./venv/bin/python scripts/idle_work.py
