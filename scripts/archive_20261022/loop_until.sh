#!/usr/bin/env bash
# 持续迭代挖因子：循环 美股 + A股(300/500/1000) 挖掘 + 正交 + 降频 + 模拟盘，
# 跑到指定截止时间（默认北京时间今天 13:00）自动停止。
#
# 用法：./scripts/loop_until.sh [截止时间 HH:MM]
# 日志：runs/loop_<TS>/ 下按轮次分文件。
#
# 设计：
#  - 串行执行（服务器只有 ~1.9GB 内存，绝不并发跑重任务）
#  - 每个子任务失败都 `|| true`，单点失败不终止整轮
#  - 每轮开始/结束都检查截止时间，到点即停
set -uo pipefail

cd /opt/quant-lab
PY=/opt/quant-lab/venv/bin/python
DEADLINE="${1:-13:00}"                 # 北京时间截止 HH:MM
TS=$(date +%Y%m%d_%H%M%S)
OUT="runs/loop_${TS}"
mkdir -p "$OUT"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$OUT/driver.log"; }

# 返回 0 表示还没到截止时间（继续跑），1 表示应当停止
before_deadline() {
    local now
    now=$(date +%s)
    local dl
    dl=$(date -d "today ${DEADLINE}" +%s 2>/dev/null) || return 1
    # 若截止时间已过（说明是跨天后运行），视为立即停止
    [ "$now" -lt "$dl" ]
}

# 内存守卫：等待可用内存回升到阈值以上（KB），避免 OOM。最多等 20 分钟。
# 用法：wait_mem 350000
wait_mem() {
    local need_kb="${1:-350000}"
    local waited=0
    while [ "$waited" -lt 1200 ]; do
        local avail
        avail=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
        if [ "$avail" -ge "$need_kb" ]; then
            return 0
        fi
        sleep 30
        waited=$((waited + 30))
    done
    log "内存守卫超时（可用仍不足），继续但风险自负"
}

# 等待上一批外部任务（mine/hold_sweep/pull）结束，避免抢内存
wait_external() {
    local waited=0
    while [ "$waited" -lt 1800 ]; do
        if ! pgrep -f "core\.mine|core\.hold_sweep|pull_ashare.py" >/dev/null 2>&1; then
            return 0
        fi
        sleep 30
        waited=$((waited + 30))
    done
}

log "===== 持续挖因子启动，截止北京时间 ${DEADLINE}，输出 ${OUT} ====="
log "等待现有外部任务结束（避免抢内存）..."
wait_external
log "外部任务已清空，开始循环"

ROUND=0
while before_deadline; do
    ROUND=$((ROUND + 1))
    R=$(printf "round_%02d" "$ROUND")
    mkdir -p "$OUT/$R"
    log "----- 第 ${ROUND} 轮开始 -----"

    # ---------- 1) 美股：大规模 LLM 挖掘 + 系统扫描 ----------
    if before_deadline; then
        wait_mem 350000
        log "轮${ROUND} [美股] 开始 mining"
        timeout 5400 "$PY" -m core.mine --llm-total 300 --concurrency 24 \
            --market us --out "$OUT/$R/mine_us.json" \
            > "$OUT/$R/mine_us.log" 2>&1 || log "轮${ROUND} [美股] mining 异常（继续）"
        log "轮${ROUND} [美股] 完成: $(grep -c '幸存' "$OUT/$R/mine_us.log" 2>/dev/null || echo '?') 行摘要"
    fi

    # ---------- 2) A股三指数：沪深300 / 中证500 / 中证1000 ----------
    for IDX in 000300 000905 000852; do
        if before_deadline; then
            log "轮${ROUND} [A股 ${IDX}] 开始 mining"
            timeout 3600 "$PY" -m core.mine --llm-total 250 --concurrency 24 \
                --market ashare --index "$IDX" --out "$OUT/$R/mine_ashare_${IDX}.json" \
                > "$OUT/$R/mine_ashare_${IDX}.log" 2>&1 || log "轮${ROUND} [A股 ${IDX}] mining 异常（继续）"
        fi
    done

    # ---------- 3) 正交搜索：找与主导簇低相关的新因子 ----------
    if before_deadline; then
        log "轮${ROUND} [美股] 正交搜索"
        timeout 3600 "$PY" -m core.mine --llm-total 250 --concurrency 24 \
            --market us --orthogonal --out "$OUT/$R/ortho_us.json" \
            > "$OUT/$R/ortho_us.log" 2>&1 || log "轮${ROUND} [美股] 正交异常（继续）"
        log "轮${ROUND} [A股000300] 正交搜索"
        timeout 3600 "$PY" -m core.mine --llm-total 250 --concurrency 24 \
            --market ashare --index 000300 --orthogonal --out "$OUT/$R/ortho_ashare.json" \
            > "$OUT/$R/ortho_ashare.log" 2>&1 || log "轮${ROUND} [A股] 正交异常（继续）"
    fi

    # ---------- 4) 降频检验：持有期 1/5/10/20/40 的真实净收益 ----------
    if before_deadline; then
        log "轮${ROUND} 降频检验（三指数）"
        for IDX in 000300 000905 000852; do
            timeout 1800 "$PY" -m core.hold_sweep --index "$IDX" \
                > "$OUT/$R/hold_${IDX}.log" 2>&1 || true
        done
    fi

    # ---------- 5) 模拟盘：真实成本 + 涨跌停 + T+1 ----------
    if before_deadline; then
        log "轮${ROUND} 模拟盘验证"
        for IDX in 000300 000905 000852; do
            timeout 900 "$PY" -m core.paper_trade --index "$IDX" --hold 20 --top 30 \
                > "$OUT/$R/paper_${IDX}.log" 2>&1 || true
        done
    fi

    # ---------- 6) 报告 ----------
    if before_deadline; then
        log "轮${ROUND} 生成报告"
        timeout 600 "$PY" -m core.master_report > "$OUT/$R/master.log" 2>&1 || true
    fi

    log "----- 第 ${ROUND} 轮完成 -----"
done

# 收尾：做一次总报告
log "===== 到达截止时间，生成总结报告 ====="
"$PY" -m core.master_report > "$OUT/FINAL_master.log" 2>&1 || true
log "===== 持续挖因子结束（共 ${ROUND} 轮）====="
