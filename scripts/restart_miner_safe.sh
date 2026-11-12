#!/usr/bin/env bash
# 优雅重启挖掘服务：避开正在跑的轮次，只在冷却期重启。
# 用法: restart_miner_safe.sh [--check] [--max-wait 分钟]
set -uo pipefail
MAXWAIT=45; CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --max-wait) MAXWAIT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

mining_pid() {
  pgrep -f "quant-lab/venv/bin/python.*core\.(mine|promote|ortho|loop)" | head -1
}

state() {
  if [ -n "$(mining_pid)" ]; then echo "mining"; 
  elif pgrep -f "mine_loop.sh" >/dev/null; then echo "cooldown";
  else echo "stopped"; fi
}

S=$(state); echo "[$(date "+%F %T")] 当前状态: $S"
if [ "$CHECK" = "1" ]; then
  case "$S" in
    cooldown) echo "现在重启安全（冷却期，无轮次在跑）" ;;
    mining)   echo "现在重启会打断当前轮次，建议等待" ;;
    stopped) echo "服务未运行，可直接启动" ;;
  esac
  exit 0
fi

waited=0
while [ "$S" = "mining" ]; do
  if [ "$waited" -ge $((MAXWAIT*60)) ]; then
    echo "已等 ${MAXWAIT} 分钟仍在挖矿，放弃重启（未打断）"; exit 1
  fi
  echo "[$(date "+%F %T")] 轮次进行中（pid $(mining_pid)），等待结束…（已等 ${waited}s）"
  sleep 60; waited=$((waited+60)); S=$(state)
done

echo "[$(date "+%F %T")] 开始重启 quant-miner.service"
systemctl restart quant-miner.service
sleep 3
systemctl is-active quant-miner.service
echo "[$(date "+%F %T")] 完成，状态: $(state)"
