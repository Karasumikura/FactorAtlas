#!/usr/bin/env bash
# 挖矿机（深圳机）→ 主控机（本机）的结果回传。
#
# 为什么要回传（2026-11-05 迁移）：
#   因子晋升是在深圳机上算的（core.promote 写 data/factor_library.json），
#   而用这些因子的是本机的交易端（core/trader_daemon.py 动态读 factor_library.json）。
#   不回传 = 挖出来的因子永远用不上。
#
# 回传清单与节奏：
#   factor_library.json  每 30 分钟（小文件，交易端要尽快看到新晋升）
#   factor_trials.json   每 30 分钟（DSR 的试验数 N，必须单调，别被本机旧值覆盖）
#   factor_zoo.json      每天（30M，回测缓存，只在需要重建挖掘环境时用）
#   runs/                每天（每轮 mine/ortho/promote 结果 + 日志）
#
# 用法：scripts/sync_from_sz.sh [--results|--runs|--all] [--quiet]
set -uo pipefail
cd /opt/quant-lab || exit 1

WHAT="all"; QUIET=0
for a in "$@"; do
    case "$a" in
        --results|results) WHAT=results ;;
        --runs|runs)       WHAT=runs ;;
        --all|all)         WHAT=all ;;
        --quiet)           QUIET=1 ;;
    esac
done

LOG=runs/sync_from_sz.log
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
say() { echo "[$(date '+%F %T')] $*" >> "$LOG"; [ $QUIET -eq 0 ] && echo "[$(date '+%F %T')] $*"; return 0; }

if ! timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=10 sz true 2>/dev/null; then
    say "❌ 深圳机不可达，本轮回传跳过"
    exit 1
fi

copy() {  # copy <说明> <rsync 源...>  —— 目标由调用方放在最后一个参数
    local desc="$1"; shift
    local out
    # 用 --info=name 数行数来统计传输量：--info=stats1 根本不输出
    # "Number of regular files transferred"，awk 取 $NF 永远是空（2026-11-05 踩过，
    # 日志一直写"0 个文件"但其实传了）。
    out=$(rsync -az --info=name -e "$SSH" "$@" 2>&1) || {
        say "❌ $desc 失败: $(echo "$out" | tail -2 | tr '\n' ' ')"
        return 1
    }
    local n; n=$(printf '%s\n' "$out" | grep -c . || true)
    say "✅ $desc（传输 ${n:-0} 个文件）"
}

# 成果文件只在深圳机存在时才拉，避免把本机旧值推回去
copy_results() {
    local ok=0
    for f in factor_library.json factor_trials.json; do
        if ssh -o BatchMode=yes sz "test -s /opt/quant-lab/data/$f" 2>/dev/null; then
            copy "$f" "sz:/opt/quant-lab/data/$f" "data/$f" || ok=1
        else
            say "⚠️ 深圳机缺 data/$f，跳过"
            ok=1
        fi
    done
    return $ok
}

RC=0
case "$WHAT" in
  results)
    copy_results || RC=1
    ;;
  runs)
    copy "回测缓存 zoo" sz:/opt/quant-lab/data/factor_zoo.json data/ || RC=1
    copy "runs/ 结果"   sz:/opt/quant-lab/runs/            runs/ || RC=1
    ;;
  all)
    copy_results || RC=1
    copy "回测缓存 zoo" sz:/opt/quant-lab/data/factor_zoo.json data/ || RC=1
    copy "runs/ 结果"   sz:/opt/quant-lab/runs/            runs/ || RC=1
    ;;
esac

N=$(python3 -c "import json;print(len(json.load(open('data/factor_library.json')).get('factors',[])))" 2>/dev/null || echo "?")
say "本机因子库共 ${N} 个"
exit $RC
