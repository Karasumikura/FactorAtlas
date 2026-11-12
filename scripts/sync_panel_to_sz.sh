#!/usr/bin/env bash
# 主控机（本机）→ 挖矿机（深圳机）的单向同步。
#
# 为什么需要它（2026-11-05 迁移）：
#   因子挖掘常驻 loop 已迁到深圳机（本机内存只剩几百 MB），但数据面板和新闻
#   采集仍留在本机 —— 因为那几个接口都要账户密钥（Alpaca/FMP/Marketaux），
#   而深圳机上不落任何账户凭据。所以由本机负责采集，再单向推过去：
#
#     data/                 行情面板（美股 hist/panel_*、crypto、ashare、news 清洗产物）
#     state/main_model.txt  Hermes 当前主模型名（深圳机没有 hermes CLI，
#                           见 core/config.py get_main_model 的 QUANT_MODEL_FILE 分支）
#
# ⚠️ 必须排除的文件（方向相反，由 sync_from_sz.sh 负责回传）：
#     data/factor_library.json  研究成果：深圳机晋升后回传，本机这份是旧的
#     data/factor_trials.json   DSR 试验数 N，全库单调，只能由挖矿机递增
#     data/factor_zoo.json      回测缓存，只增不减，同理
#     data/news/.last_pull_date 新闻采集闸门（本机自己的节奏）
#   一个 --exclude 漏了，就会拿旧库覆盖新库，等于把晋升结果抹掉。
#
# 用法：scripts/sync_panel_to_sz.sh [--quiet]
set -uo pipefail
cd /opt/quant-lab || exit 1

LOG=runs/sync_to_sz.log
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
say() { echo "[$(date '+%F %T')] $*" >> "$LOG"; [ $QUIET -eq 0 ] && echo "[$(date '+%F %T')] $*"; }

SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"

if ! timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=10 sz true 2>/dev/null; then
    say "❌ 深圳机不可达，本轮同步跳过"
    exit 1
fi

# ── 1) 主模型名 ────────────────────────────────────────────────
# 读不到就保留旧文件（旧文件超过 48h 会被深圳机那边判过期并报错，不会静默降级）。
# 与迁移前 core/config.py 的 CLI 分支保持同一配置来源；不得继承调用者的 pro profile。
MODEL=$(HERMES_HOME=/root/.hermes HERMES_REAL_HOME=/root /root/.local/share/uv/tools/hermes-agent/bin/hermes config get model.default 2>/dev/null || true)
if [ -n "$MODEL" ]; then
    PREVIOUS_MODEL=$(cat state/main_model.txt 2>/dev/null || true)
    # 时间戳表示最近一次成功读取，而不是最近一次切模型；同名也要续期。
    # 原子替换避免同步进程读到半写入的文件。读取失败时不刷新旧文件。
    MODEL_TMP=$(mktemp state/.main_model.XXXXXX) || exit 1
    if ! { printf '%s\n' "$MODEL" > "$MODEL_TMP" && mv -f "$MODEL_TMP" state/main_model.txt; }; then
        rm -f "$MODEL_TMP"
        exit 1
    fi
    if [ "$MODEL" != "$PREVIOUS_MODEL" ]; then
        say "主模型名 -> $MODEL"
    fi
fi
if [ ! -s state/main_model.txt ]; then
    say "⚠️ state/main_model.txt 为空，深圳机会拒绝发起 LLM 请求"
else
    rsync -az -e "$SSH" state/main_model.txt sz:/opt/quant-lab/state/main_model.txt \
        || say "⚠️ 主模型名同步失败"
fi

# ── 2) 面板同步（只更新不删除；排除方向相反的成果文件）──────────
OUT=$(rsync -az --info=stats1 -e "$SSH" \
    --exclude 'factor_library.json' \
    --exclude 'factor_library.json.bak*' \
    --exclude 'factor_trials.json' \
    --exclude 'factor_zoo.json' \
    --exclude '.last_pull_date' \
    --exclude '__pycache__/' \
    data/ sz:/opt/quant-lab/data/ 2>&1)
RC=$?

if [ $RC -ne 0 ]; then
    say "❌ 面板同步失败 rc=$RC: $(echo "$OUT" | tail -3 | tr '\n' ' ')"
    exit $RC
fi

# 同 sync_from_sz.sh：stats1 不含文件计数，单独数一遍 name 列表
NFILES=$(rsync -az --info=name --dry-run -e "$SSH" \
    --exclude 'factor_library.json' --exclude 'factor_library.json.bak*' \
    --exclude 'factor_trials.json' --exclude 'factor_zoo.json' \
    --exclude '.last_pull_date' --exclude '__pycache__/' \
    data/ sz:/opt/quant-lab/data/ 2>/dev/null | grep -c . || true)
say "✅ 面板已同步（变化文件 ${NFILES:-0} 个）"

# ── 3) 代码同步 ────────────────────────────────────────────────
# 本机的 core.self_repair（LLM 自愈）只会改本机文件，而真正跑挖掘的是深圳机。
# 不同步 = 自愈修的是没人执行的代码，bug 在深圳机照旧。所以每轮带上代码，
# 有变化才在「深圳机没在挖」的窗口重启服务（正在挖时不动，等下个周期）。
# 只推远端真正会执行的东西。sync_*/news_daily/refresh_* 是本机专属运维脚本，
# 推过去除了多触发一次 reload_pending（白重启一轮）没有任何用处。
CODE_EXCL="--exclude=__pycache__/ --exclude=*.pyc --exclude=sync_*.sh
           --exclude=news_daily.sh --exclude=refresh_*.sh --exclude=ops_cron.sh"
CODE_CHANGES=$(rsync -az --dry-run --itemize-changes -e "$SSH" $CODE_EXCL \
    core factors llm scripts \
    sz:/opt/quant-lab/ 2>/dev/null | grep -c '^[<>c]') || CODE_CHANGES=0
CODE_CHANGES=${CODE_CHANGES:-0}

if [ "$CODE_CHANGES" -gt 0 ]; then
    if rsync -az -e "$SSH" $CODE_EXCL \
        core factors llm scripts sz:/opt/quant-lab/ >> "$LOG" 2>&1; then
        say "代码已同步（${CODE_CHANGES} 项变化）"
        # 不在外面硬 restart：那会把 4h 冷却打断成 2h（本脚本的周期），
        # 试验数 N 加速增长 = 偷偷加重 DSR 惩罚。只留标记，
        # mine_loop.sh 在下一轮开始前自己退出，由 Restart=always 拉起。
        ssh -o BatchMode=yes -o ConnectTimeout=10 sz \
            'touch /opt/quant-lab/state/reload_pending' 2>>"$LOG" \
            && say "  已置 reload_pending：远端下一轮开始时自动重启载入新代码" \
            || say "  ⚠️ 置 reload_pending 失败"
    else
        say "⚠️ 代码同步失败"
    fi
fi

exit 0
