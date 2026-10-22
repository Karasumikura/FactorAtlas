#!/usr/bin/env bash
# 持续挖因子（防过拟合版）—— 常驻运行，直到被显式停止（systemctl stop / kill）。
#
# 与旧 loop_until.sh 的区别：
#   1) 不再有"截止时间"，改为【无限循环 + 每轮冷却】，由服务来控制启停。
#   2) 每轮结束自动跑 regn 晋升闸门（core.promote）：只有跨多次独立挖掘
#      反复出现 + 通过 DSR/PBO 的因子才进正式因子库。
#   3) 每轮登记试验数（core.overfit_guard），DSR 的惩罚 N 随时间单调增长，
#      跑得越久、门槛越严 —— 这是对"持续挖"本身过拟合的自动刹车。
#   4) 串行执行 + 内存守卫，绝不在 ~1.9GB 主机上并发跑重任务。
#
# 用法：
#   scripts/mine_loop.sh            # 常驻循环
#   scripts/mine_loop.sh --rounds 3 # 只跑 3 轮（调试用）
set -uo pipefail

cd /opt/quant-lab
PY=/opt/quant-lab/venv/bin/python
TS=$(date +%Y%m%d_%H%M%S)
OUT="runs/loop_${TS}"
mkdir -p "$OUT"

ROUNDS_LIMIT=0
if [ "${1:-}" = "--rounds" ] && [ -n "${2:-}" ]; then
    ROUNDS_LIMIT="$2"
fi

COOLDOWN_SEC="${MINE_COOLDOWN_SEC:-14400}"   # 每轮之间冷却（默认 4 小时）

# 持有期轮换（搜索空间的第一扩展维度）：
#   历史 6.5 万次试验里 99% 都是 hold=1（隔夜到次日）。同一个因子在
#   hold=5/20 下是完全不同的假设 —— 短窗噪声因子的 IC 会在长持有期衰减到 0，
#   而慢变量（基本面/XBRL/新闻情绪）只有在长持有期才可能体现出来。
#   每轮轮流用一个持有期，等于把搜索空间沿这个维度展宽，且 zoo 里
#   hold=1 的历史缓存继续有效（zoo._key 对 hold=1 不加后缀，向后兼容）。
#   可用 MINE_HOLDS 覆盖，例如 MINE_HOLDS="1 5"。
HOLDS_STR="${MINE_HOLDS:-1 5 20}"
read -r -a HOLD_LIST <<< "$HOLDS_STR"
N_HOLDS=${#HOLD_LIST[@]}

log() { echo "[$(date '+%F %T')] $*" | tee -a "$OUT/driver.log"; }

# ── 全局互斥锁（2026-10-22 新增）────────────────────────────────
# 事故：10-22 03:06 quant-miner 第 8 轮 mining 与 cron 的 quant_nightly.sh
# 撞车，两路 core.mine 同时吃面板内存，触发宿主机 OOM，kernel 杀掉了
# hermes-gateway 的 node 进程（anon-rss 1.2G），夜跑 cron 被标 interrupted。
# 现在：miner 持锁运行，nightly 拿同一把锁 —— 谁先到谁跑，另一个等或跳过。
# 锁文件放在 runs/ 下，两个脚本共用同一路径。
LOCK_FILE="/opt/quant-lab/runs/.quant_heavy.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "另一个重任务（夜跑/mining）正在持有锁，等待其结束…"
    flock -w 3600 200 || { log "等锁超时 1 小时，本轮跳过"; exit 0; }
fi
log "已获取全局重任务锁（PID $$）"

# 内存守卫：等到可用内存 >= 阈值（KB），最多等 20 分钟
# 2026-10-22：阈值 400MB → 550MB。400MB 在 1.9G 机器上不够 —— mining 阶段
# 峰值能在几十秒内吃掉 600MB+，留 400MB 余量等于没留。
wait_mem() {
    local need_kb="${1:-550000}" waited=0 avail
    while [ "$waited" -lt 1200 ]; do
        avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
        [ "$avail" -ge "$need_kb" ] && return 0
        sleep 30; waited=$((waited + 30))
    done
    log "内存守卫超时（可用仍不足 ${need_kb}KB），继续但风险自负"
}

# 等待外部重任务结束，避免抢内存
wait_external() {
    local waited=0
    while [ "$waited" -lt 1800 ]; do
        if ! pgrep -f "core\.mine|core\.hold_sweep|pull_ashare.py|core\.promote" >/dev/null 2>&1; then
            return 0
        fi
        sleep 30; waited=$((waited + 30))
    done
}

log "===== 持续挖因子（防过拟合）启动，输出 ${OUT}，冷却 ${COOLDOWN_SEC}s ====="
wait_external

ROUND=0
while true; do
    ROUND=$((ROUND + 1))
    if [ "$ROUNDS_LIMIT" -gt 0 ] && [ "$ROUND" -gt "$ROUNDS_LIMIT" ]; then
        log "已达轮次上限 ${ROUNDS_LIMIT}，退出"
        break
    fi
    R=$(printf "round_%02d" "$ROUND")
    mkdir -p "$OUT/$R"
    # 本轮持有期：按轮次轮换（1 -> 5 -> 20 -> 1 ...）
    HOLD="${HOLD_LIST[$(( (ROUND - 1) % N_HOLDS ))]}"
    log "----- 第 ${ROUND} 轮开始（hold=${HOLD}）-----"

    # 1) 全局提案池：一个 round 只调用一次 LLM（幂等，已存在则复用）
    #    以前 5 个标的各自调用一次 LLM，成本 5 倍且互相看不见对方的提案。
    #    现在先建一个共享池，5 个标的从同一池取用：成本 1 倍 + 天然全局去重。
    #
    # ── 新闻采集（2026-10-22 从 quant-lab-nightly 并入）──────────────
    # 背景：nightly 与 miner 调的是同一个 core.mine，功能重复；唯一 nightly
    # 独有的是「拉新闻」（US 新闻因子需要）。合并后 nightly 整个停用，新闻改
    # 由 miner 负责。但 Marketaux 只有 100 次/天额度，nightly 一次要吃掉 90，
    # 而 miner 一天跑约 6 轮 —— 每轮都拉会 540 次打爆额度，后 5 轮全空转。
    # 所以这里按「当天是否已拉过」做闸门：一天只拉一次，幂等。
    NEWS_STAMP="data/news/.last_pull_date"
    TODAY_UTC=$("$PY" -c "import datetime;print(datetime.datetime.now(datetime.timezone.utc).date().isoformat())")
    LAST_PULL=$(cat "$NEWS_STAMP" 2>/dev/null || echo "")
    if [ "$LAST_PULL" != "$TODAY_UTC" ]; then
        LEFT=$("$PY" -c "
import sys; sys.path.insert(0,'/opt/quant-lab')
from data.news import usage_left
print(usage_left())" 2>/dev/null || echo 0)
        if [ "${LEFT:-0}" -ge 10 ]; then
            log "轮${ROUND} 拉取新闻（今日剩余额度 ${LEFT}）"
            # 注意：超时也写 stamp。90 只票约 4.5 分钟，但网络慢时会拖到 10 分钟+；
            # 若超时后不写 stamp，下一轮会重拉一遍、把额度白白再吃一次。
            # 新闻因子对「今天漏了几只票」不敏感，宁可少拉也不要重复烧额度。
            if timeout 1800 "$PY" scripts/pull_news.py 90 >> "runs/news_${TS}.log" 2>&1; then
                echo "$TODAY_UTC" > "$NEWS_STAMP"
            else
                rc=$?
                echo "$TODAY_UTC" > "$NEWS_STAMP"
                log "轮${ROUND} 新闻采集未完整结束（rc=${rc}），已记 stamp 防重复烧额度"
            fi
        else
            log "轮${ROUND} 跳过新闻采集（今日额度仅剩 ${LEFT}）"
        fi
    fi

    # ── 新闻清洗 + LLM 判断（2026-10-23 新增）────────────────────
    # 采集完不等于能用：原始新闻里有 13%+ 的大盘综述被挂到多只票名下、
    # 还有 GF 估值模板文和 ETF 资金流快照。清洗把「个股」和「宏观」分成
    # 两股（店长指出大盘也要算，所以宏观不是噪声、是独立通道），然后让
    # LLM 逐票给方向+强度，绝不碰价位。
    # 只判断**当前持仓**：没有仓位的票新闻再好也不该影响动作（选股归因子层）。
    # 失败不阻断挖掘——新闻层是加分项，不能因为它挂掉就停了主流程。
    if [ "${ENABLE_NEWS_VERDICT:-1}" = "1" ]; then
        log "轮${ROUND} 新闻清洗 + LLM 判断"
        timeout 300 "$PY" -m scripts.clean_news --save >> "runs/news_${TS}.log" 2>&1 \
            || log "轮${ROUND} 新闻清洗异常（继续）"
        timeout 900 "$PY" -m scripts.llm_news --macro --positions >> "runs/news_${TS}.log" 2>&1 \
            && log "轮${ROUND} 新闻判断完成" \
            || log "轮${ROUND} 新闻判断异常（继续）"
    fi

    if [ "$HOLD" = "1" ]; then POOL="$OUT/$R/proposal_pool.json"
    else POOL="$OUT/$R/proposal_pool_h${HOLD}.json"; fi
    wait_mem 550000
    log "轮${ROUND} 生成共享提案池（单次 LLM）"
    # 池子规模：600 → 300。
    # 600 是按「A股×3 + 美股 + 正交」共 5 个标的设定的；A股停用后实际只有
    # 2 个消费方（美股 + 正交），按 5 个标的分摊会有一半候选没人消费。
    # 注意：下面 mine 命令里的 --llm-total 在有 --proposal-pool 时无效，
    # 真正决定 LLM 提案量的是这里的 POOL_TOTAL。
    POOL_TOTAL="${POOL_TOTAL:-150}"
    timeout 1800 "$PY" -c "
import sys
sys.path.insert(0, '/opt/quant-lab')
from core.proposal_pool import build_pool
specs = build_pool('$OUT/$R', total=$POOL_TOTAL, concurrency=2, hold=$HOLD)
print(f'池内候选 {len(specs)} 个')
" > "$OUT/$R/proposal_pool.log" 2>&1 || log "轮${ROUND} 提案池异常（各进程将回退到自身提案）"
grep -E "池内候选|复用已有池|回退" "$OUT/$R/proposal_pool.log" 2>/dev/null | sed 's/^/  /' | tee -a "$OUT/driver.log"

    # 2) 美股挖掘
    wait_mem 550000
    log "轮${ROUND} [美股] mining"
    timeout 5400 "$PY" -m core.mine --llm-total 150 --concurrency 2 \
        --market us --hold "$HOLD" --proposal-pool "$POOL" --out "$OUT/$R/mine_us.json" \
        > "$OUT/$R/mine_us.log" 2>&1 || log "轮${ROUND} [美股] mining 异常（继续）"

    # 3) A股三指数（降频严格：只留真实调仓可交易）
    #    已停用：642 个候选在 0.1 个/秒的回测速度下需要约 6400s，
    #    而 timeout 只有 3600s，三个标的全部跑不完就被强杀，
    #    等于每轮白烧 1 小时 + 400MB 内存且零产出。等候选规模或回测提速再开。
    if [ "${ENABLE_ASHARE:-0}" = "1" ]; then
    for IDX in 000300 000905 000852; do
        wait_mem 550000
        log "轮${ROUND} [A股 ${IDX}] mining"
        timeout 3600 "$PY" -m core.mine --llm-total 100 --concurrency 2 \
            --market ashare --index "$IDX" --hold "$HOLD" --lowfreq-strict \
            --proposal-pool "$POOL" --out "$OUT/$R/mine_ashare_${IDX}.json" \
            > "$OUT/$R/mine_ashare_${IDX}.log" 2>&1 || log "轮${ROUND} [A股 ${IDX}] 异常（继续）"
    done
    fi

    # 4) 正交搜索：找与主导簇低相关的新因子（组合时才有分散收益）
    wait_mem 550000
    log "轮${ROUND} [美股] 正交搜索"
    timeout 3600 "$PY" -m core.mine --llm-total 125 --concurrency 2 \
        --market us --orthogonal --hold "$HOLD" --proposal-pool "$POOL" \
        --out "$OUT/$R/ortho_us.json" \
        > "$OUT/$R/ortho_us.log" 2>&1 || log "轮${ROUND} [美股] 正交异常（继续）"

    # 5) 晋升闸门：跨会话可重复 + DSR/PBO，只有过关的进正式库
    log "轮${ROUND} 因子晋升闸门"
    timeout 1200 "$PY" -m core.promote \
        --glob 'runs/mine*.json' \
        --glob 'runs/loop_*/round_*/mine*.json' \
        --glob 'runs/loop_*/round_*/ortho*.json' \
        > "$OUT/$R/promote.log" 2>&1 || log "轮${ROUND} 晋升闸门异常（继续）"
    grep -E "本次晋升|正式库共" "$OUT/$R/promote.log" 2>/dev/null | sed 's/^/  /' | tee -a "$OUT/driver.log"

    log "----- 第 ${ROUND} 轮完成；冷却 ${COOLDOWN_SEC}s -----"
    sleep "$COOLDOWN_SEC"
done

log "===== 持续挖因子结束（共 ${ROUND} 轮）====="
