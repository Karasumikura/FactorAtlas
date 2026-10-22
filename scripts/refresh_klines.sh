#!/bin/bash
# 共振策略 K 线数据刷新 —— 修复「数据停在 2026-10-16，策略空转 3 天」的问题
#
# 背景：core/mtf_fetch.load_kline() 只读本地 parquet，从不在线拉取；
#       而 mtf_fetch 此前只能手动跑，没有任何 cron/timer 驱动。
#       结果共振盘每 30 分钟拿 3 天前的快照算信号，永远 silent。
#
# 本脚本按需刷新最少的周期，避免浪费币安权重：
#   - 30m：共振策略的入场层，必须跟得上 → 每 30 分钟跑
#   - 4h：定方向层，每 4 小时才出 1 根 → 每小时跑足够
#
# 用法：
#   refresh_klines.sh 30m
#   refresh_klines.sh 4h

set -uo pipefail
cd /opt/quant-lab || exit 1

INTERVAL="${1:-30m}"
PY=/opt/quant-lab/venv/bin/python
LOG=/opt/quant-lab/data/binance/kline_refresh.log
LOCK=/tmp/quantlab_kline_${INTERVAL}.lock

# 防重叠：上一轮还没跑完就跳过，避免堆积
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(date '+%F %T')] $INTERVAL 上一轮仍在运行，跳过" >> "$LOG"
    exit 0
fi

case "$INTERVAL" in
    4h)  LIMIT=1500 ;;
    30m) LIMIT=1500 ;;
    *)   echo "未知周期: $INTERVAL" >&2; exit 2 ;;
esac

echo "[$(date '+%F %T')] 开始刷新 $INTERVAL ..." >> "$LOG"
if "$PY" -m core.mtf_fetch --interval "$INTERVAL" --limit "$LIMIT" >> "$LOG" 2>&1; then
    # 刷新后校验数据新鲜度：30m 最新一根距今应 < 90 分钟，4h 应 < 6 小时
    "$PY" - "$INTERVAL" <<'EOF' >> "$LOG" 2>&1
import sys, glob, time
from datetime import datetime, timezone
import pandas as pd

iv = sys.argv[1]
max_age = {"30m": 90, "4h": 360}[iv] * 60
newest = 0.0
for f in glob.glob(f"/opt/quant-lab/data/binance/kline_{iv}_*.parquet"):
    try:
        df = pd.read_parquet(f, columns=None)
        if len(df):
            newest = max(newest, df.index[-1].timestamp())
    except Exception:
        pass
age_min = (time.time() - newest) / 60 if newest else 1e9
flag = "OK" if age_min <= max_age / 60 else "STALE"
print(f"  新鲜度检查: 最新K线 {datetime.fromtimestamp(newest, timezone.utc):%F %H:%M} UTC, "
      f"距今 {age_min:.0f} 分钟 [{flag}]")
EOF
    echo "[$(date '+%F %T')] $INTERVAL 刷新完成" >> "$LOG"
else
    echo "[$(date '+%F %T')] $INTERVAL 刷新失败(退出码 $?)" >> "$LOG"
    exit 1
fi
