#!/usr/bin/env bash
# 兜底守护：如果 quant-loop 没在跑，且还没到 13:00，就重新拉起。
# 由 cron 每 30 分钟调用一次。
set -uo pipefail
DEADLINE="13:00"
NOW=$(date +%s)
DL=$(date -d "today ${DEADLINE}" +%s 2>/dev/null || echo 0)

# 已过截止时间 → 什么都不做
if [ "$NOW" -ge "$DL" ]; then
    exit 0
fi

# 已经在跑 → 什么都不做
if systemctl is-active --quiet quant-loop 2>/dev/null; then
    exit 0
fi

cd /opt/quant-lab
# 还剩不到 1 小时就不重启了（避免起一个马上超时的任务）
if [ $((DL - NOW)) -lt 3600 ]; then
    exit 0
fi

systemd-run --unit=quant-loop-restart-$(date +%H%M) --collect \
    /opt/quant-lab/scripts/loop_until.sh "$DEADLINE" >/dev/null 2>&1 || true
