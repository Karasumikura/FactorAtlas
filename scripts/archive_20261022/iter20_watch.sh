#!/usr/bin/env bash
# 20 轮迭代跟踪：每轮结束检查异常，正常则静默，异常才输出（cron 只在有输出时通知）。
# 设计原则：无异常 = 无输出，避免每 30 分钟刷屏。
set -uo pipefail
cd /opt/quant-lab

ISSUES=()
INFO=()

# 1) 挖掘服务是否在跑
if ! systemctl is-active --quiet quant-miner; then
    ISSUES+=("quant-miner 服务未运行：$(systemctl is-active quant-miner)")
fi

# 2) 最近一次晋升闸门是否报错
LATEST_PROMOTE=$(ls -t runs/loop_*/round_*/promote.log 2>/dev/null | head -1)
if [ -n "$LATEST_PROMOTE" ]; then
    if grep -qiE "Traceback|AttributeError|KeyError|TypeError|MemoryError" "$LATEST_PROMOTE" 2>/dev/null; then
        ISSUES+=("晋升闸门报错: $LATEST_PROMOTE")
        ISSUES+=("$(grep -iE 'Error|Traceback' "$LATEST_PROMOTE" | tail -3)")
    fi
    ROUND=$(echo "$LATEST_PROMOTE" | grep -oP 'round_\d+' | tail -1)
    PROMOTED=$(grep -oP '本次晋升 \K\d+' "$LATEST_PROMOTE" 2>/dev/null | tail -1)
    TOTAL=$(grep -oP '正式库共 \K\d+' "$LATEST_PROMOTE" 2>/dev/null | tail -1)
    INFO+=("最近一轮: ${ROUND:-?} 晋升=${PROMOTED:-?} 库=${TOTAL:-?}")
fi

# 3) 磁盘（两块盘一起算，总 40G）
ROOT_PCT=$(df --output=pcent / | tail -1 | tr -d ' %')
DATA_PCT=$(df --output=pcent /mnt/data 2>/dev/null | tail -1 | tr -d ' %')
TOTAL_AVAIL=$(df --output=avail -BG / /mnt/data 2>/dev/null | tail -n +2 | tr -dc '0-9\n' | awk '{s+=$1} END{print s}')
if [ "${ROOT_PCT:-0}" -ge 88 ]; then
    ISSUES+=("系统盘 ${ROOT_PCT}% 告急（可用 ${TOTAL_AVAIL:-?}G / 40G）")
fi

# 4) 内存与 OOM
AVAIL_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
if [ "${AVAIL_MB:-9999}" -lt 250 ]; then
    ISSUES+=("内存可用仅 ${AVAIL_MB}MB")
fi
OOM=$(dmesg 2>/dev/null | grep -c "Out of memory" || echo 0)
if [ "${OOM:-0}" -gt 0 ] && [ -f /tmp/quant_oom_seen ] && [ "$OOM" -gt "$(cat /tmp/quant_oom_seen)" ]; then
    ISSUES+=("内核报告新的 OOM 事件（累计 $OOM 次）")
fi
echo "${OOM:-0}" > /tmp/quant_oom_seen

# 5) 晋升结果里 n_sessions 是否仍然出现虚高（同一 OOS 值重复多次）
#    新代码生效后，同窗口只算 1 个会话，n_sessions 应 <= 不同 data_window 数
if [ -n "$LATEST_PROMOTE" ]; then
    BIG=$(grep -hoP '会话=\K\d+' "$LATEST_PROMOTE" 2>/dev/null | sort -n | tail -1)
    if [ -n "$BIG" ] && [ "$BIG" -gt 30 ] 2>/dev/null; then
        INFO+=("警告: 某因子会话数=$BIG，可能仍有同窗口重复计数")
    fi
fi

# 6) 因子库变化
LIB_N=$(python3 -c "import json;print(len(json.load(open('data/factor_library.json'))['factors']))" 2>/dev/null || echo "?")
INFO+=("因子库=${LIB_N}")

# 输出：有异常则详细报告，否则静默（cron 无输出=不通知）
if [ ${#ISSUES[@]} -gt 0 ]; then
    echo "⚠️ 量化 20 轮迭代异常 $(date '+%F %T')"
    for i in "${ISSUES[@]}"; do echo "  - $i"; done
    echo "--- 参考 ---"
    for i in "${INFO[@]}"; do echo "  $i"; done
    exit 1
fi
exit 0
