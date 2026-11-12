#!/usr/bin/env bash
# 新闻链路日跑（本机）—— 2026-11-05 从 mine_loop.sh 里拆出来。
#
# 为什么拆：新闻采集要 Marketaux 账户密钥，而密钥只在本机（深圳机上不落任何
# 账户凭据）。挖掘迁到深圳机后，新闻仍由本机每天拉一次，随 sync_panel_to_sz.sh
# 推过去。原来这段逻辑挂在 mine_loop 的 us 轮次里，miner 停掉后会一起消失 ——
# 而新闻因子是唯一的外部文本通道，断了不会报错，只会让新闻类候选永远命中缓存。
#
# 额度闸门（Marketaux 100 次/天）保留原来的口径：剩余 <10 次就不拉了，
# 一个不完整的采集不如不采 —— 半套新闻会让「今天有没有新闻」这件事变得不可信。
#
# 用法：scripts/news_daily.sh [--force]
set -uo pipefail
cd /opt/quant-lab || exit 1

PY=/opt/quant-lab/venv/bin/python
LOG=/opt/quant-lab/runs/news_daily.log
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
say() { echo "[$(date '+%F %T')] $*" >> "$LOG"; echo "[$(date '+%F %T')] $*"; }

TODAY_UTC=$("$PY" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).date().isoformat())")
STAMP=data/news/.last_pull_date
LAST=$(cat "$STAMP" 2>/dev/null || echo "")

if [ "$LAST" = "$TODAY_UTC" ] && [ "$FORCE" -eq 0 ]; then
    say "今天已采集过（$TODAY_UTC），跳过"
    exit 0
fi

LEFT=$("$PY" -c "
import sys; sys.path.insert(0,'/opt/quant-lab')
from data.news import usage_left
print(usage_left())" 2>/dev/null || echo 0)

if [ "${LEFT:-0}" -lt 10 ] && [ "$FORCE" -eq 0 ]; then
    say "Marketaux 今日剩余额度仅 ${LEFT}，跳过采集"
    exit 0
fi

say "开始采集（剩余额度 ${LEFT}）"
# 采集失败也写 stamp：宁可少拉几只票，也不要在额度边缘反复重试把它烧光。
if timeout 1800 "$PY" scripts/pull_news.py 90 >> "$LOG" 2>&1; then
    say "采集完成"
else
    say "采集未完整结束（rc=$?），仍记 stamp 防重复烧额度"
fi
echo "$TODAY_UTC" > "$STAMP"

say "清洗新闻（个股/宏观分流）"
timeout 300 "$PY" -m scripts.clean_news --save >> "$LOG" 2>&1 || say "清洗异常（继续）"

say "LLM 判断（方向+强度，不碰价位）"
timeout 900 "$PY" -m scripts.llm_news --macro --positions >> "$LOG" 2>&1 || say "LLM 判断异常（继续）"

say "新闻链路完成"
