"""每日新闻采集（受 Marketaux 100 req/天限制）。

策略：优先给流动性最高的一批 symbol 拉新闻，落盘累积。
每天最多用 95 次请求。
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config
from data.news import fetch_symbol_news, append_news, usage_left, load_news

# 目标 symbol 数（受额度限制）
MAX_SYMBOLS = int(sys.argv[1]) if len(sys.argv) > 1 else 90

ref = config.PRO_SCRIPTS / "multi_stock_paper_liquidity_universe.json"
import json
uni = json.loads(ref.read_text())
ranked = [r["symbol"] for r in uni["ranked"][:MAX_SYMBOLS]]

print(f"目标 {len(ranked)} 只票 | 今日剩余额度 {usage_left()}", flush=True)
total = 0
for i, sym in enumerate(ranked):
    if usage_left() <= 2:
        print(f"[news] 额度用尽，已处理 {i} 只", flush=True)
        break
    recs = fetch_symbol_news(sym, limit=50, max_pages=1)
    if recs:
        append_news(recs)
        total += len(recs)
    if i % 20 == 0:
        print(f"[news] {i}/{len(ranked)} 累计 {total} 条 | 剩余额度 {usage_left()}", flush=True)

df = load_news()
print(f"[news] 完成。本次新增 {total} 条，本地库共 {len(df)} 条", flush=True)
if not df.empty:
    print(f"[news] 覆盖 {df['symbol'].nunique()} 只票", flush=True)
