#!/bin/bash
# 加密面板日更 —— 修复「data/crypto 停了 8 天没人发现」的问题（2026-11-01 加）
#
# 背景：core/data.py 的 crypto 分支只调 crypto.load_panel() 读缓存，
#       不做任何新鲜度检查；而 data/crypto 又没有任何 cron/timer 驱动
#       （对比：美股有 scripts/refresh_hist.py 每工作日 06:13 跑）。
#       结果 crypto 面板从 2026-10-24 起冻了 8 天，连锁后果：
#         - 每轮挖掘都用同一段历史 → data_window_id 不变 →
#           overfit_guard 把所有 crypto 会话判成「同窗口重跑」，n_counted=0，
#           累计试验数 N 一条都不涨（87918 卡了 3 天）
#         - 幸存因子全在 8 天前的行情上验证，DSR 的 N 也是死的
#
# 设计：
#   - 日更（cron 05:20，夹在 03:40 与 07:40 两轮挖掘之间，避开抢内存/IO）
#   - 日更只刷价格，宇宙沿用上一版（build_panel(symbols=...)），
#     否则 24h 成交额筛子会让标的每天进出，把窗口指纹搅乱
#   - 每 7 天做一次全量重选，跟得上成交额结构变化
#   - 挖掘进程在跑就直接跳过（2GB 机器，别和挖矿抢资源）
#   - build_panel 是「全部抓完才落盘」，中途失败不会覆盖旧面板
#
# 用法：refresh_crypto.sh [--force-reselect]

set -uo pipefail
cd /opt/quant-lab || exit 1

LOG=/opt/quant-lab/runs/refresh_crypto.log
PY=/opt/quant-lab/venv/bin/python
RESELECT_DAYS=7
FORCE_RESELECT=0
[ "${1:-}" = "--force-reselect" ] && FORCE_RESELECT=1

say() { echo "[$(date -u '+%F %T UTC')] $*" >> "$LOG"; }

# 1) 挖掘在跑就跳过：一轮挖掘约 1 小时，面板正被读取
if pgrep -f 'core\.(mine|proposal_pool|promote)|scripts[./](pull_news|clean_news|llm_news)' >/dev/null 2>&1; then
    say "挖掘进程在跑，本轮跳过"
    exit 0
fi

# 2) 今天已经刷过就跳过（幂等）
# 注意 IFS='|'：python 那边是 "end|reselected_at" 一行输出，
# 不指定 IFS 时 read 会按空白切，把整行都塞给 END（2026-11-01 踩过：
# END 变成 "2026-11-01|"，幂等判断永远不成立，每天都全量重选）。
IFS='|' read -r END RESEL < <("$PY" - <<'EOF'
import json, pathlib
p = pathlib.Path("data/crypto/meta.json")
if not p.exists():
    print("|0"); raise SystemExit
m = json.loads(p.read_text())
print(f"{m.get('end','')}|{m.get('universe_reselected_at','')}")
EOF
)

TODAY=$(date -u +%F)
if [ "$END" = "$TODAY" ] && [ "$FORCE_RESELECT" = "0" ]; then
    say "面板已是最新（$END），跳过"
    exit 0
fi

# 3) 判断这次要不要重选宇宙
DO_RESELECT=$FORCE_RESELECT
if [ -z "$RESEL" ]; then
    DO_RESELECT=1
else
    AGE=$("$PY" -c "
from datetime import date
try:
    print((date.today() - date.fromisoformat('$RESEL')).days)
except Exception:
    print(999)
")
    [ "$AGE" -ge "$RESELECT_DAYS" ] && DO_RESELECT=1
fi

say "面板末日 $END → 开始重建（重选宇宙=$DO_RESELECT）"
if [ "$DO_RESELECT" = "1" ]; then
    "$PY" -c "from data import crypto; crypto.build_panel(force=True, days=2200)" >> "$LOG" 2>&1
    RC=$?
else
    "$PY" - <<'EOF' >> "$LOG" 2>&1
import json, pathlib
from data import crypto
m = json.loads(pathlib.Path("data/crypto/meta.json").read_text())
syms = (m.get("selection") or {}).get("symbols") or []
if not syms:
    raise SystemExit("meta 里没有 symbols，改走全量重选")
crypto.build_panel(force=True, days=2200, symbols=syms)
EOF
    RC=$?
fi

# build_panel 会把 meta 整个重写，universe_reselected_at 得在这里补回去，
# 否则下一次跑又会被判成「从没重选过」→ 天天全量重选，宇宙照样天天变。
if [ $RC -eq 0 ]; then
    KEEP_RESEL="$RESEL"
    [ "$DO_RESELECT" = "1" ] && KEEP_RESEL="$TODAY"
    KEEP_RESEL="$KEEP_RESEL" "$PY" - <<'EOF' >> "$LOG" 2>&1
import json, os, pathlib
p = pathlib.Path("data/crypto/meta.json")
m = json.loads(p.read_text())
m["universe_reselected_at"] = os.environ["KEEP_RESEL"] or m.get("end")
p.write_text(json.dumps(m, ensure_ascii=False, indent=1))
EOF
fi

if [ $RC -eq 0 ]; then
    NEW=$("$PY" -c "from data import crypto; print((crypto.load_meta() or {}).get('end'))")
    N=$("$PY" -c "from data import crypto; m=crypto.load_meta() or {}; print(m.get('n_symbols'))")
    say "✅ 完成，新末日 $NEW（$N 只）"
else
    say "❌ 重建失败 rc=$RC（旧面板未被覆盖，仍是 $END）"
fi
exit $RC
