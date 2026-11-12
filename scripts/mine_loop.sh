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
PROJ=/opt/quant-lab          # 2026-11-08 修：本轮同步丢了这个定义，set -u 下 $PROJ 直接崩
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

# ── 跨市场轮换（2026-10-24 新增）──────────────────────────────
# 每股轮到哪个市场挖。加密永续 17 只主流币、2200 天，单轮约 2 分钟（美股约 10 分钟），
# 所以按轮次交替：奇数额美股，偶数额 crypto。
# 优势：crypto 复用同一个提案池（实测 120 个候选里 110 个能直接在币面板跑），
# 不额外打 LLM 请求，纯赚一个市场的搜索。
#
# 为什么币的聚类簇不一样：币 24/7 无财报断点，长周期动量(120/250)钝化，
# 主导簇是短中期动量(20/60)+波动率。见 core/orthogonal.py 的 CRYPTO_CLUSTER。
#
# 可用 MINE_MARKETS 覆盖，例如 MINE_MARKETS="us" 即关掉币（回到纯美股）。
# 注意：ashare 因回测太慢已停用，见下方 ENABLE_ASHARE。
MARKETS_STR="${MINE_MARKETS:-us crypto}"
read -r -a MARKET_LIST <<< "$MARKETS_STR"
N_MARKETS=${#MARKET_LIST[@]}

# ── 市场轮换指针（2026-10-25 修复）────────────────────────────────
# 事故：原来 MKT 取自 (ROUND-1) % N_MARKETS，而 ROUND 是进程内从 0 数的。
# quant-miner 因冷却/Restart 频繁重启，ROUND 每次都回到 1 —— 于是 (1-1)%2=0
# 永远选中 MARKET_LIST[0]，即 us。结果 mine_loop.log 里 crypto 一次都没跑过，
# 跨市场交替形同虚设（crypto 只存在于 mine_rotation.sh 的手动日志里）。
# 修法：指针落盘到 state/mine_market_ptr.json，与进程重启无关。每轮开始取当前
# 市场，本轮结束后推进一位；服务中途挂掉也不丢进度（宁可重复一次，不会饿死一个市场）。
MARKET_STATE="state/mine_market_ptr.json"
mkdir -p state
if [ -f "$MARKET_STATE" ]; then
    MARKET_PTR=$("$PY" -c "import json;print(int(json.load(open('$MARKET_STATE')).get('next_index',0)))" 2>/dev/null || echo 0)
else
    # 首次：沿用 mine_rotation.json 的指针，避免和旧调度器的历史进度打架
    MARKET_PTR=$("$PY" -c "import json;print(int(json.load(open('state/mine_rotation.json')).get('next_index',0)))" 2>/dev/null || echo 0)
fi
case "$MARKET_PTR" in ''|*[!0-9]*) MARKET_PTR=0 ;; esac

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
        if ! pgrep -f "[c]ore\.mine|[c]ore\.hold_sweep|pull_ashare.py|[c]ore\.promote" >/dev/null 2>&1; then
            return 0
        fi
        sleep 30; waited=$((waited + 30))
    done
}

log "===== 持续挖因子（防过拟合）启动，输出 ${OUT}，冷却 ${COOLDOWN_SEC}s ====="
wait_external

ROUND=0
while true; do
    # ── 代码热更新闸门（2026-11-05 跨机部署新增）────────────────────
    # 主控机的 core.self_repair 修的是主控机上的文件，同步脚本把代码推过来后，
    # 必须重启进程才生效。但绝不能在外部硬 restart：那会把 4 小时冷却打断成
    # "同步周期"那么短（2 小时），试验数 N 凭空加速增长、DSR 门槛越抬越高，
    # 等于用运维动作偷偷加重过拟合惩罚。改成：同步脚本只 touch 一个标记文件，
    # 本循环在下一轮开始前自己干净退出（unit 是 Restart=always，立刻被拉起，
    # 冷却时间照旧走完）。
    if [ -f state/reload_pending ]; then
        rm -f state/reload_pending
        log "检测到 state/reload_pending（主控机同步了新代码），本轮不挖，退出让 systemd 重启加载"
        exit 0
    fi

    ROUND=$((ROUND + 1))
    if [ "$ROUNDS_LIMIT" -gt 0 ] && [ "$ROUND" -gt "$ROUNDS_LIMIT" ]; then
        log "已达轮次上限 ${ROUNDS_LIMIT}，退出"
        break
    fi
    R=$(printf "round_%02d" "$ROUND")
    mkdir -p "$OUT/$R"
    # 本轮持有期：按轮次轮换（1 -> 5 -> 20 -> 1 ...）
    HOLD="${HOLD_LIST[$(( (ROUND - 1) % N_HOLDS ))]}"
    # 本轮市场：从持久化指针取（us -> crypto -> us ...），与进程重启无关
    # ── 多臂老虎机选市场（借鉴 RD-Agent(Q) 的方向调度）──
    # 只选 market，不选 hold：hold 必须跟提案池一致（HOLD="$POOL_HOLD"）。
    # 状态读不到/输出非法就回退固定轮换，绝不因调度器故障停挖。
    if [ "${MINE_BANDIT:-0}" = "1" ]; then
        BANDIT_MKT=$("$PY" -m core.mine_bandit --markets $MARKETS_STR \
            --state "$PROJ/state/mine_bandit.json" 2>/dev/null || echo "")
        case " $MARKETS_STR " in
            *" $BANDIT_MKT "*) MKT="$BANDIT_MKT" ;;
            *) MKT="${MARKET_LIST[$(( MARKET_PTR % N_MARKETS ))]}" ;;
        esac
        log "轮${ROUND} 多臂老虎机选向：${MKT}"
    else
        MKT="${MARKET_LIST[$(( MARKET_PTR % N_MARKETS ))]}"
    fi
    log "----- 第 ${ROUND} 轮开始（market=${MKT} hold=${HOLD}）-----"

    # crypto 单轮很快（~2 分钟），美股慢（~10 分钟）。crypto 轮不需要拉新闻、
    # 也不需要长持有期 —— 币没有财报断点，hold=1/5/20 的语义差异远小于美股。
    IS_CRYPTO=0
    [ "$MKT" = "crypto" ] && IS_CRYPTO=1
    # 正交搜索的复合提案量：币面板小（17 只标的），60 个复合候选足够；
    # 美股 655 只，正交搜索本身就是筛子，给 40 个即可（太多会被正交全部筛掉）。
    # 2026-11-08：本机零 LLM，提案全部来自主控机；COMPOSITE* 已无消费方

    # 1) 全局提案池：一个 round 只调用一次 LLM（幂等，已存在则复用）
    #    以前 5 个标的各自调用一次 LLM，成本 5 倍且互相看不见对方的提案。
    #    现在先建一个共享池，5 个标的从同一池取用：成本 1 倍 + 天然全局去重。
    #
    # ── 本机不做任何 LLM 推理（店长定死）────────────────────────────────
    # 深圳机的角色只有：回测 / DSR / 晋升。提案由主控机（香港机）生成后随
    # sync_panel_to_sz.sh 推到 state/pool/；新闻链路也在主控机跑（Marketaux
    # 密钥不出主控机，见 scripts/news_daily.sh）。
    # 以前这里还有「本机自己提一遍」「新闻 LLM 判断」两段 —— 那就是 429 和
    # credits 的来源，已删除。真需要提案就走主控机，别在本机回退。
    export QUANT_LLM_DISABLED=1

    # 共享提案池：只读，不生成。持有期跟着池文件走（主控机按 1→5→20 轮换），
    # 保证「提案时的 hold 提示」与「回测窗口」永远一致。
    POOL="$PROJ/state/pool/proposal_pool.json"
    POOL_N=0; POOL_HOLD=""; POOL_AGE_H=999
    if [ -f "$POOL" ]; then
        POOL_N=$("$PY" -c "import json;print(int(json.load(open('$POOL')).get('n_specs',0)))" 2>/dev/null || echo 0)
        POOL_HOLD=$("$PY" -c "import json;print(int(json.load(open('$POOL')).get('hold',1)))" 2>/dev/null || echo 1)
        POOL_AGE_H=$("$PY" -c "
import json,time,datetime
d=json.load(open('$POOL'))
print(int((time.time()-datetime.datetime.fromisoformat(d['generated_at']).timestamp())//3600))" 2>/dev/null || echo 999)
    fi
    case "$POOL_N" in ''|*[!0-9]*) POOL_N=0 ;; esac
    case "$POOL_AGE_H" in ''|*[!0-9]*) POOL_AGE_H=999 ;; esac
    POOL_ARG=()
    if [ "$POOL_N" -gt 0 ] && [ "$POOL_AGE_H" -le 12 ]; then
        HOLD="$POOL_HOLD"
        POOL_ARG=(--proposal-pool "$POOL")
        log "轮${ROUND} 提案池 ${POOL_N} 个（hold=${HOLD}，${POOL_AGE_H}h 前主控机生成）"
    else
        log "轮${ROUND} 提案池缺失/过期（n=${POOL_N} age=${POOL_AGE_H}h）→ 本轮只用本地模板，零 LLM"
    fi

    # ── 每轮回测 2 并行（2026-11-09）──────────────────────────────
    # 同机实测：单进程 79.3 候选/分钟、2 进程 163.1/分钟（加速 2.11×），
    # 单进程峰值 RSS 371MB，2 个 = 742MB < cgroup MemoryHigh=1000M。
    # promote 峰值曾到 1.59G，必须等全部 mine 结束再单独串行跑。
    # MINE_PARALLEL=1 可一键退回全串行。
    PAR="${MINE_PARALLEL:-2}"
    EVOLVE_N="${EVOLVE_N:-24}"   # 程序化进化搜索候选数（仅美股轮，纯程序化、不调 LLM）
    MINE_PIDS=()
    launch_mine() {
        local tag="$1" tmo="$2" logf="$3"; shift 3
        log "轮${ROUND} ${tag}"
        timeout "$tmo" "$PY" -m core.mine "$@" > "$logf" 2>&1 &
        MINE_PIDS+=("$!")
        # wait -n：等【任意一个】结束就腾槽位。wait 第一个会死等最慢的
        # us(12min)，crypto(2min) 早结束也补不上第三个任务，空一核十分钟。
        if [ "${#MINE_PIDS[@]}" -ge "$PAR" ]; then
            wait -n "${MINE_PIDS[@]}" 2>/dev/null
            RC=$?
            [ "$RC" -ne 0 ] && log "轮${ROUND} 某回测任务结束 rc=${RC}（继续）"
            ALIVE=()
            for pid in "${MINE_PIDS[@]}"; do
                kill -0 "$pid" 2>/dev/null && ALIVE+=("$pid")
            done
            MINE_PIDS=("${ALIVE[@]}")
            unset ALIVE
        fi
    }
    wait_mem "${MINE_PAR_MEM_KB:-800000}"

    if [ "$IS_CRYPTO" -eq 0 ]; then
        launch_mine "[美股] mining" 5400 "$OUT/$R/mine_us.log" \
            --llm-total 0 --llm-composite 0 --concurrency 2 \
            --evolve "${EVOLVE_N:-0}" --evolve-gens 3 \
            --market us --hold "$HOLD" "${POOL_ARG[@]}" --out "$OUT/$R/mine_us.json"
    fi

    # 加密永续：复用同一提案池、不额外打 LLM。面板停更就跳过，别空转。
    CRYPTO_END=$("$PY" -c "import json;print(json.load(open('data/crypto/meta.json')).get('end',''))" 2>/dev/null || echo "")
    CRYPTO_STALE=$("$PY" - "$CRYPTO_END" <<'PYEOF'
import sys, datetime
end = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    d = datetime.date.fromisoformat(end)
    print((datetime.datetime.now(datetime.timezone.utc).date() - d).days)
except Exception:
    print(99)
PYEOF
)
    if [ "${CRYPTO_STALE:-99}" -gt 2 ]; then
        log "轮${ROUND} [加密] 跳过：面板末日 ${CRYPTO_END}（滞后 ${CRYPTO_STALE} 天），先修数据再挖"
    else
        launch_mine "[加密] mining" 1800 "$OUT/$R/mine_crypto.log" \
            --llm-total 0 --llm-composite 0 --concurrency 2 \
            --market crypto --hold "$HOLD" "${POOL_ARG[@]}" --out "$OUT/$R/mine_crypto.json"
    fi

    # A股三指数：仍停用（642 候选在 0.1 个/秒下跑不完），ENABLE_ASHARE=1 才开
    if [ "${ENABLE_ASHARE:-0}" = "1" ]; then
        for IDX in 000300 000905 000852; do
            launch_mine "[A股 ${IDX}] mining" 3600 "$OUT/$R/mine_ashare_${IDX}.log" \
                --llm-total 0 --llm-composite 0 --concurrency 2 \
                --market ashare --index "$IDX" --hold "$HOLD" --lowfreq-strict \
                "${POOL_ARG[@]}" --out "$OUT/$R/mine_ashare_${IDX}.json"
        done
    fi

    # 正交搜索：找与主导簇低相关的新因子
    launch_mine "[${MKT}] 正交搜索" 3600 "$OUT/$R/ortho_${MKT}.log" \
        --llm-total 0 --llm-composite 0 --concurrency 2 \
        --market "$MKT" --orthogonal --hold "$HOLD" "${POOL_ARG[@]}" \
        --out "$OUT/$R/ortho_${MKT}.json"

    # 等剩余任务；promote 必须在全部 mine 结束后单独跑（内存峰值 1.59G）
    for pid in "${MINE_PIDS[@]}"; do
        wait "$pid" || log "轮${ROUND} 回测任务 rc=$?（继续）"
    done
    log "轮${ROUND} 回测任务全部结束（并行度=${PAR}）"

    # 5) 晋升闸门：跨会话可重复 + DSR/PBO，只有过关的进正式库
    #    2026-10-24：耗时实测 9 分 11 秒（扫描 250+ 个 mine 结果文件）。
    #    原 timeout 1200s 紧贴实测值，机器一有负载就超时、promote.log 全空。
    #    放宽到 2400s。crypto 结果文件也在扫描范围内，后续会继续变多。
    log "轮${ROUND} 因子晋升闸门"
    # 2026-10-25：去掉 --glob 'runs/mine*.json'。那个通配符扫的是 runs/ 根目录下
    # 37 个历史日跑结果（最早 10-15），每轮把同样的几百个老候选再算一次 DSR/PBO，
    # 正是 promote 吃 1.2G 内存被 OOM 杀掉的主因。loop 输出已在库里晋升过，
    # 老文件不会带来新因子 —— 只扫 loop_*/round_*/ 即可。
    timeout 2400 "$PY" -m core.promote \
        --glob 'runs/loop_*/round_*/mine*.json' \
        --glob 'runs/loop_*/round_*/ortho*.json' \
        > "$OUT/$R/promote.log" 2>&1 || log "轮${ROUND} 晋升闸门异常（继续）"
    grep -E "本次新晋|本次晋升|正式库共" "$OUT/$R/promote.log" 2>/dev/null | sed 's/^/  /' | tee -a "$OUT/driver.log"

    # 回灌老虎机（奖励 = 本轮 survivor 数，不用速率：crypto 轮天生快 5 倍，
    # 用速率会饿死美股）
    if [ "${MINE_BANDIT:-0}" = "1" ] && [ -f "$OUT/$R/mine_${MKT}.json" ]; then
        REWARD=$("$PY" - "$OUT/$R/mine_${MKT}.json" <<'PYEOF'
import json, sys
try:
    print(int(json.load(open(sys.argv[1])).get("n_survivors", 0)))
except Exception:
    print(0)
PYEOF
)
        "$PY" -m core.mine_bandit --markets $MARKETS_STR \
            --state "$PROJ/state/mine_bandit.json" \
            --record "$MKT" --reward "${REWARD:-0}" 2>/dev/null || true
    fi

    # 推进市场指针（落盘，跨进程重启有效）
    MARKET_PTR=$(( MARKET_PTR + 1 ))
    "$PY" - "$MARKET_STATE" "$MARKET_PTR" "$MKT" <<'PYEOF' 2>/dev/null || true
import json, sys, datetime
path, nxt, mkt = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    d = json.load(open(path))
except Exception:
    d = {}
d["next_index"] = nxt
d["last_market"] = mkt
d["last_run_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
json.dump(d, open(path, "w"), ensure_ascii=False, indent=1)
PYEOF
    log "轮${ROUND} 完成，市场指针 -> ${MARKET_PTR}（下一轮 ${MARKET_LIST[$(( MARKET_PTR % N_MARKETS ))]}）"

    log "----- 第 ${ROUND} 轮完成；冷却 ${COOLDOWN_SEC}s -----"
    sleep "$COOLDOWN_SEC"
done

log "===== 持续挖因子结束（共 ${ROUND} 轮）====="
