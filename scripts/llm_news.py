#!/usr/bin/env python
"""LLM 新闻判断层：读单只票的新闻，输出「方向 + 强度」，不给价位。

设计原则（2026-10-23 与店长对齐）
--------------------------------
店长的原始想法是「LLM 读完新闻，告诉我跌到多少可以买入」。这个方向对，
但**价位必须由代码算，不能由 LLM 给**，理由三条：

  1. LLM 会编数字，而且编得很自信 —— 它没有能力算合理价位，只会从上下文
     里凑一个看起来像的数值。
  2. 不可回测 —— 「它上次说 385」这种输出没法系统性验证对错。
  3. 不可归因 —— 每次结论维度都不一样，攒再多也总结不出规律。

所以本模块的契约是：**LLM 只回答「这条新闻让我对这个票未来 N 天看多/看空
到什么程度」，其余全交给代码。** 具体输出 schema：

    { "symbol": "MU",
      "bias": -1,              # -1 / 0 / +1，只能是这三个
      "magnitude": 0.6,        # 0.0 ~ 1.0，新闻驱动的强度
      "horizon_days": 2,       # 1 ~ 5
      "confidence": 0.7,       # 0.0 ~ 1.0，对本次判断有多确信
      "catalyst": "HBM 产能不及预期",   # 一句话核心事件
      "evidence": ["原文标题1", "原文标题2"]  # 必须是输入里出现过的
    }

下游（core/news_adjust.py）把 bias × magnitude × confidence 翻译成仓位的
乘数，再交给现有风控闸门。本模块**不碰**价格、不碰下单。

宏观股单独走 macro_view()：给一个全市场情绪分，不做逐票分配。店长指出
「大盘也会影响整体情绪」，所以大盘不是噪声，是另一条独立的信息通道。

用法：
    venv/bin/python -m scripts.llm_news --symbols MU,NVDA      # 指定票
    venv/bin/python -m scripts.llm_news --positions             # 只跑当前持仓
    venv/bin/python -m scripts.llm_news --macro                 # 只看宏观
    venv/bin/python -m scripts.llm_news --positions --dry-run   # 不调 LLM，只验证组装
"""
from __future__ import annotations

import sys
import json
import argparse
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

CLEAN_PATH = PROJ / "data" / "news" / "news_clean.parquet"
VERDICT_LOG = PROJ / "data" / "news" / "llm_verdicts.jsonl"

MAX_NEWS_PER_SYMBOL = 8     # 每票喂给 LLM 的最大新闻条数（控制 token）
MAX_MACRO_NEWS = 10         # 宏观股喂给 LLM 的条数
LOOKBACK_DAYS = 7

VALID_BIAS = (-1, 0, 1)


def _parse_verdict_json(raw: str) -> dict | None:
    """从 LLM 输出里抠出**单个 JSON 对象**。

    不能直接用 llm.client.extract_json —— 那个函数为了兼容「LLM 返回数组」
    的旧场景，优先找 `[` 和最后一个 `]`，而本模块的输出是对象、内部含
    `evidence: [...]` 数组。实测结果：extract_json 会把 evidence 数组整段
    切出来当返回值，导致 `[{"regime":...}]` 这种结构拿到的是字符串列表。
    所以这里单独实现：先剥 ```json 围栏，再按对象边界截取。
    """
    if not raw:
        return None
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j <= i:
        return None
    frag = text[i:j + 1]
    try:
        obj = json.loads(frag)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# ────────────────────────── 宏观情绪 ──────────────────────────

MACRO_SYSTEM = """你是美股宏观分析师。给你最近的全市场新闻标题，判断当前市场情绪。

只输出 JSON，不要任何其他文字：
{"regime": "RISK_ON|NEUTRAL|RISK_OFF",
 "score": -1.0 到 1.0 之间的数（负数=避险，正数=risk-on）,
 "horizon_days": 1 到 5 的整数,
 "summary": "一句话概括（30 字以内）",
 "evidence": ["原文标题1", "原文标题2"]}

判断依据：指数涨跌、板块轮动、油价/利率/汇率异动、美联储与通胀预期、突发地缘事件。
不要因为单条新闻就下极端结论，看整体方向的一致性。"""


def _fmt_news(rows, limit: int) -> str:
    lines = []
    for _, r in rows.head(limit).iterrows():
        ts = r["dt"].strftime("%m-%d %H:%M")
        lines.append(f"[{ts}] {r['title']}")
        desc = str(r.get("description") or "").strip()
        if desc:
            lines.append(f"    {desc[:180]}")
    return "\n".join(lines)


def macro_view(clean, dry_run: bool = False) -> dict | None:
    """给宏观新闻一个整体情绪判断。返回 {regime, score, ...}。"""
    macro = clean[clean["kind"] == "macro"].sort_values("dt", ascending=False)
    if macro.empty:
        print("[llm_news] 宏观股为空，跳过")
        return None

    prompt = (f"以下是最近的市场新闻（按时间倒序，共 {min(len(macro), MAX_MACRO_NEWS)} 条）：\n\n"
              f"{_fmt_news(macro, MAX_MACRO_NEWS)}\n\n请输出 JSON。")

    if dry_run:
        print(f"[dry-run] 宏观 prompt 长度 {len(prompt)} 字，新闻 {min(len(macro), MAX_MACRO_NEWS)} 条")
        return None

    from llm.client import chat
    raw = chat([{"role": "system", "content": MACRO_SYSTEM},
                {"role": "user", "content": prompt}], temperature=0.3)
    data = _parse_verdict_json(raw)
    if not isinstance(data, dict):
        print(f"[llm_news] 宏观输出不可解析: {str(raw)[:200]}")
        return None

    regime = str(data.get("regime", "NEUTRAL")).upper()
    if regime not in ("RISK_ON", "NEUTRAL", "RISK_OFF"):
        regime = "NEUTRAL"
    try:
        score = max(-1.0, min(1.0, float(data.get("score", 0.0))))
    except (TypeError, ValueError):
        score = 0.0
    try:
        horizon = int(data.get("horizon_days", 2))
        horizon = max(1, min(5, horizon))
    except (TypeError, ValueError):
        horizon = 2

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "macro",
        "regime": regime,
        "score": round(score, 3),
        "horizon_days": horizon,
        "summary": str(data.get("summary", ""))[:80],
        "evidence": [str(e)[:120] for e in (data.get("evidence") or [])][:4],
        "n_news": int(min(len(macro), MAX_MACRO_NEWS)),
    }
    return out


# ────────────────────────── 个股判断 ──────────────────────────

STOCK_SYSTEM = """你是美股个股分析师。给你一只股票的近期新闻，判断这些新闻对该股
未来 1-5 个交易日的**方向性影响**。

严格只输出 JSON，不要任何其他文字：
{"bias": -1 或 0 或 1,
 "magnitude": 0.0 到 1.0,
 "horizon_days": 1 到 5 的整数,
 "confidence": 0.0 到 1.0,
 "catalyst": "一句话核心事件（25字内）",
 "evidence": ["原文标题1", "原文标题2"]}

规则：
- bias=-1 看空，0 中性/无明确方向，+1 看多。
- magnitude 是强度：0.2=轻微，0.5=明显，0.8+=重大（如财报爆雷/重大合同）。
- 只有当新闻里确实有能影响该股的事件时才给非 0；纯分析师调价、
  泛泛的「某机构看好」这类弱信号，magnitude 不超过 0.3。
- evidence 必须逐字引用输入中出现的标题，不许自己编。
- **不要输出任何价格、目标价、买入点位** —— 那不是你的职责。"""


def build_stock_prompt(symbol: str, rows) -> str:
    return (f"股票代码：{symbol}\n\n近期新闻（按时间倒序）：\n\n"
            f"{_fmt_news(rows, MAX_NEWS_PER_SYMBOL)}\n\n请输出 JSON。")


def stock_verdict(symbol: str, rows, dry_run: bool = False) -> dict | None:
    if rows.empty:
        return None

    prompt = build_stock_prompt(symbol, rows)
    if dry_run:
        print(f"[dry-run] {symbol}: prompt {len(prompt)} 字，新闻 {min(len(rows), MAX_NEWS_PER_SYMBOL)} 条")
        return None

    from llm.client import chat
    raw = chat([{"role": "system", "content": STOCK_SYSTEM},
                {"role": "user", "content": prompt}], temperature=0.3)
    data = _parse_verdict_json(raw)
    if not isinstance(data, dict):
        print(f"[llm_news] {symbol} 输出不可解析: {str(raw)[:160]}")
        return None

    # 严格校验：bias 只能是 -1/0/1
    try:
        bias = int(data.get("bias", 0))
    except (TypeError, ValueError):
        bias = 0
    if bias not in VALID_BIAS:
        bias = max(-1, min(1, bias))

    def _clamp01(v, default=0.0):
        try:
            return round(max(0.0, min(1.0, float(v))), 3)
        except (TypeError, ValueError):
            return default

    magnitude = _clamp01(data.get("magnitude", 0.0))
    confidence = _clamp01(data.get("confidence", 0.5), 0.5)
    try:
        horizon = int(data.get("horizon_days", 2))
        horizon = max(1, min(5, horizon))
    except (TypeError, ValueError):
        horizon = 2

    # evidence 必须是输入里真实出现过的标题，防止 LLM 编造
    titles = set(rows.head(MAX_NEWS_PER_SYMBOL)["title"].astype(str))
    evid = []
    for e in (data.get("evidence") or []):
        e = str(e)[:140]
        if any(e[:40] in t or t[:40] in e for t in titles):
            evid.append(e)

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "stock",
        "symbol": symbol,
        "bias": bias,
        "magnitude": magnitude,
        "horizon_days": horizon,
        "confidence": confidence,
        "catalyst": str(data.get("catalyst", ""))[:60],
        "evidence": evid[:4],
        "n_news": int(min(len(rows), MAX_NEWS_PER_SYMBOL)),
    }


# ────────────────────────── 入口 ──────────────────────────

def current_positions() -> list[str]:
    """从最新心跳里拿当前持仓票（去掉 USDT 后缀）。"""
    hb = PROJ / "data" / "binance" / "trader_heartbeat.jsonl"
    if not hb.exists():
        return []
    last = None
    for line in hb.read_text().splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("current_positions") is not None:
            last = d
    if not last:
        return []
    return [k.replace("USDT", "") for k, v in last["current_positions"].items()
            if abs(float(v or 0)) > 0]


def append_verdicts(records: list[dict]) -> None:
    if not records:
        return
    VERDICT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with VERDICT_LOG.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="逗号分隔的股票代码")
    ap.add_argument("--positions", action="store_true", help="只跑当前持仓")
    ap.add_argument("--macro", action="store_true", help="只看宏观")
    ap.add_argument("--dry-run", action="store_true", help="不调 LLM，只验证组装")
    ap.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    args = ap.parse_args()

    import pandas as pd
    clean = pd.read_parquet(CLEAN_PATH)
    clean["dt"] = pd.to_datetime(clean["dt"], utc=True)
    now = datetime.now(timezone.utc)
    recent = clean[clean["dt"] >= now - timedelta(days=args.lookback)]

    records: list[dict] = []

    if args.macro or not (args.symbols or args.positions):
        mv = macro_view(recent, dry_run=args.dry_run)
        if mv:
            records.append(mv)
            print(f"[宏观] {mv['regime']} score={mv['score']:+.2f} | {mv['summary']}")

    symbols: list[str] = []
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.positions:
        symbols = current_positions()
        print(f"[持仓] {', '.join(symbols) if symbols else '(空)'}")

    for sym in symbols:
        rows = recent[(recent["symbol"] == sym) & (recent["kind"] == "stock")] \
            .sort_values("dt", ascending=False)
        if rows.empty:
            print(f"  {sym}: 无新闻，跳过")
            continue
        v = stock_verdict(sym, rows, dry_run=args.dry_run)
        if v:
            records.append(v)
            arrow = {-1: "↓", 0: "→", 1: "↑"}[v["bias"]]
            print(f"  {sym:6s} {arrow} mag={v['magnitude']:.2f} conf={v['confidence']:.2f} "
                  f"h={v['horizon_days']}d | {v['catalyst']}")

    if not args.dry_run:
        append_verdicts(records)
        print(f"\n已记录 {len(records)} 条判断到 {VERDICT_LOG.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
