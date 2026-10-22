#!/usr/bin/env bash
# 每夜自动迭代挖因子（修复后版本：三市场 + 三指数 + 降频严格模式 + 修正报告）。
# 静默运行，只在有幸存因子时输出摘要。
set -uo pipefail
cd /opt/quant-lab
TS=$(date +%Y%m%d_%H%M%S)
PY=/opt/quant-lab/venv/bin/python

# 0) 采集新闻（失败不阻断）
$PY scripts/pull_news.py 90 >> "runs/news_${TS}.log" 2>&1 || true

# 1) 刷新 A股面板（增量，失败不阻断）
$PY scripts/pull_ashare.py 000300 2015-01-01 6 >> "runs/ashare_pull_${TS}.log" 2>&1 || true

# 2) 大规模挖因子（美股）：系统扫描 + 经典复合 + LLM + FDR + OOS + 稳健性
US_OUT="runs/mine_us_${TS}.json"
$PY -m core.mine --llm-total 300 --concurrency 20 --market us --out "$US_OUT" \
    > "runs/nightly_${TS}.log" 2>&1 || true

# 3) 大规模挖因子（A股三指数）：加 --lowfreq-strict，只留真实调仓下可交易的
declare -A AS_OUT
for IDX in 000300 000905 000852; do
    O="runs/mine_ashare_${IDX}_${TS}.json"
    AS_OUT[$IDX]="$O"
    $PY -m core.mine --llm-total 200 --concurrency 20 --market ashare --index "$IDX" \
        --lowfreq-strict --out "$O" >> "runs/nightly_${TS}.log" 2>&1 || true
done

# 4) 更新报告（master 只纳入修复后结果）
$PY -m core.master_report >> "runs/nightly_${TS}.log" 2>&1 || true
for IDX in 000300 000905 000852; do
    $PY -m core.independence "${AS_OUT[$IDX]}" ashare "$IDX" \
        >> "runs/nightly_${TS}.log" 2>&1 || true
done

# 5) 有幸存因子时打印摘要
$PY - "$US_OUT" "${AS_OUT[000300]}" "${AS_OUT[000905]}" "${AS_OUT[000852]}" <<'PY'
import json, sys

def summarize(path, tag):
    try:
        run = json.load(open(path))
    except Exception:
        return []
    n = run.get("n_survivors", 0)
    if n <= 0:
        return []
    out = [f"🧬 {tag} | 候选 {run.get('n_candidates',0)} 个，幸存 {n} 个"]
    for s in run["survivors"][:5]:
        lf = s.get("lowfreq", {})
        lftxt = (f" 真实净Sh={lf.get('best_sharpe'):+.2f}@h{lf.get('best_hold')}"
                 if lf.get("best_sharpe") is not None else "")
        out.append(
            f"  ★ {s['name']}  OOS_Sh={s['oos_ls_sharpe']:+.2f}{lftxt} "
            f"分年LS={s.get('years_same_sign_ls','-')}"
        )
    return out

lines = []
lines += summarize(sys.argv[1], "美股")
lines += summarize(sys.argv[2], "A股沪深300")
lines += summarize(sys.argv[3], "A股中证500")
lines += summarize(sys.argv[4], "A股中证1000")
if lines:
    print("\n".join(lines))
PY
