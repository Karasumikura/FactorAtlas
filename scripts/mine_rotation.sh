#!/usr/bin/env bash
# 跨市场交替挖掘调度器
#
# 用法：
#   ops_loop.sh                 # 按顺序跑一轮：us -> crypto -> us -> ...
#   ops_loop.sh crypto          # 只跑指定市场
#
# 状态记录在 state/mine_rotation.json —— 每轮结束后推进指针，
# 下一轮从下一个市场继续。**重启不会重复跑同一个市场**。
#
# 为什么交替而不是同时跑：
#   - 2GB 共享主机，两个挖掘进程并行会 OOM（历史上 Y2A 就是这么挂的）
#   - 两个市场的因子本来就不相关，交替跑等于并行推进两条线，只是错开时间
set -uo pipefail

cd /opt/quant-lab || exit 1
PY=venv/bin/python
STATE=state/mine_rotation.json
LOG_DIR=logs/mine_rotation
mkdir -p "$(dirname "$STATE")" "$LOG_DIR"

# 市场轮换顺序：美股体量大先跑，币快、后跑（万一超时也不影响主市场）
MARKETS=(us crypto)

now() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(now)] $*" | tee -a "$LOG_DIR/rotation.log"; }

# ---- 读取/推进轮换指针 ----
cur=0
if [ -f "$STATE" ]; then
  cur=$($PY -c "import json;print(json.load(open('$STATE')).get('next_index',0))" 2>/dev/null || echo 0)
fi

if [ $# -ge 1 ]; then
  target="$1"
  log "手动指定市场：$target"
else
  target="${MARKETS[$((cur % ${#MARKETS[@]}))]}"
  log "轮换指针=$cur → 本轮市场：$target"
fi

# ---- 各市场的挖掘参数 ----
# 币面板只有 17 只标的、2200 天，单轮很快；美股 655 只、6 年，慢且吃内存。
case "$target" in
  us)
    ARGS=(--market us --orthogonal)
    ;;
  crypto)
    # 币波动率是美股 3 倍，默认窗口参数全部要短；
    # 且 crypto 簇与美股相反（短中期动量+波动率），正交筛选按 CRYPTO_CLUSTER
    ARGS=(--market crypto --orthogonal)
    ;;
  ashare)
    ARGS=(--market ashare --index 000300 --orthogonal --lowfreq)
    ;;
  *)
    log "未知市场：$target（可选：us / crypto / ashare）"
    exit 2
    ;;
esac

LOGF="$LOG_DIR/${target}_$(date +%Y%m%d_%H%M%S).log"
log "开始挖掘 $target：${ARGS[*]}"
START=$(date +%s)

$PY -m core.mine "${ARGS[@]}" 2>&1 | tee "$LOGF"
rc=${PIPESTATUS[0]}
DUR=$(( $(date +%s) - START ))

# ---- 记录结果 + 推进指针 ----
$PY - "$target" "$rc" "$DUR" "$cur" "$LOGF" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
from pathlib import Path

target, rc, dur, cur, logf = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
p = Path("state/mine_rotation.json")
d = json.loads(p.read_text()) if p.exists() else {}
markets = ["us", "crypto"]
hist = d.get("history", [])
hist.append({
    "market": target,
    "rc": rc,
    "duration_s": dur,
    "at": datetime.now(timezone.utc).isoformat(),
    "log": logf,
})
d["history"] = hist[-50:]          # 只留最近 50 条
d["last_market"] = target
d["last_rc"] = rc
d["last_run_at"] = datetime.now(timezone.utc).isoformat()
# 手动指定市场时不推进指针，只有轮换模式才推进
if len(sys.argv) <= 6 or "--no-advance" not in sys.argv:
    d["next_index"] = (cur + 1) % len(markets)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, ensure_ascii=False, indent=1))
PYEOF

log "结束 $target：rc=$rc 耗时 ${DUR}s → 下轮指针 $(( (cur+1) % ${#MARKETS[@]} ))"
exit $rc
