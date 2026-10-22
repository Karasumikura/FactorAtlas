"""宏观事件监控 → LLM 判断 → 影响仓位。

这是"让 LLM 适时介入"的核心：不是纯脚本，而是当【宏观事件】发生时，
抓真实新闻喂给 LLM，让它做宏观判断（risk-on / risk-off / neutral），
然后调整仓位（缩小敞口 / 暂停开新仓 / 维持）。

三类介入时机：
  1. 官方宏观经济数据发布日（BLS 的 CPI/PPI/NFP，用官方日历）
  2. 临近重大事件（FOMC 等，事件前降风险）
  3. 突发新闻（Tavily 抓到高相关市场新闻时）

设计原则：
  * 无前视：只用真实发布时间
  * LLM 只做"宏观风险判断"，不改具体选股（选股归因子）
  * 判断结果是个乘数：risk_off → 乘以 <1，缩小敞口
  * 失败降级：LLM 挂掉就退回纯规则（事件日保守降风险）
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
CACHE = PROJ / "data" / "macro_calendar"
DECISION_LOG = PROJ / "data" / "binance" / "macro_decisions.jsonl"
MACRO_REVIEW_INTERVAL_MIN = 360  # 低频宏观复评：6小时一次，不随每个tick调用


def _cached_decision(as_of: date) -> dict | None:
    """取当天最近一次宏观 LLM 结论，避免事件日每个 tick 重复调用。"""
    if not DECISION_LOG.exists():
        return None
    last = None
    for line in DECISION_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if str(r.get("as_of")) == str(as_of) and r.get("should_intervene"):
            last = r
    return last


def _cache_age_minutes(rec: dict) -> float | None:
    try:
        ts = datetime.fromisoformat(str(rec.get("ts")))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 60)
    except Exception:  # noqa: BLE001
        return None

try:
    from core import config as _cfg  # noqa: F401
except Exception:  # noqa: BLE001
    pass


# ---------------------------------------------------------------------------
# 1) 宏观经济数据发布日（用官方 BLS 日历，无前视）
# ---------------------------------------------------------------------------
def days_since_last_big_release(as_of: date | None = None) -> tuple[str | None, int | None]:
    """最近一次重大宏观数据（CPI/PPI/NFP）发布，及距今几天。

    返回 (事件名, 天数)。若数据缺失返回 (None, None)。
    """
    as_of = as_of or date.today()
    try:
        import pandas as pd
        from core.macro_calendar import official_calendar
        cal = official_calendar()
        cal = cal[cal["dt"].dt.date <= as_of]
        if cal.empty:
            return None, None
        last = cal.iloc[-1]
        d = (as_of - last["dt"].date()).days
        return str(last["event"]), d
    except Exception:  # noqa: BLE001
        return None, None


def is_major_macro_day(as_of: date | None = None) -> tuple[bool, str]:
    """今天是否有重大宏观数据发布。返回 (是否, 事件名)。"""
    as_of = as_of or date.today()
    try:
        from core.macro_calendar import official_calendar
        cal = official_calendar()
        today_ev = cal[cal["dt"].dt.date == as_of]
        if not today_ev.empty:
            names = ",".join(today_ev["event"].astype(str).tolist())
            return True, names
    except Exception:  # noqa: BLE001
        pass
    return False, ""


# ---------------------------------------------------------------------------
# 2) 新闻抓取（Tavily，真实新闻）
# ---------------------------------------------------------------------------
def fetch_market_news(max_results: int = 5) -> list[dict]:
    """抓最近的市场/宏观新闻。失败返回空。"""
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        return []
    try:
        import requests
        r = requests.post(
            "https://api.tavily.com/search",
            json={"api_key": key,
                  "query": "stock market today Federal Reserve inflation macro",
                  "max_results": max_results, "topic": "news",
                  "days": 2},
            timeout=30)
        if r.status_code != 200:
            return []
        out = []
        for x in r.json().get("results", []):
            out.append({"title": x.get("title", ""),
                        "url": x.get("url", ""),
                        "content": (x.get("content", "") or "")[:500],
                        "published": x.get("published_date", "")})
        return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 3) LLM 宏观判断
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是量化交易的宏观风控助手。你在管理一个美股动量因子的模拟盘（币安 Demo，100美元小资金）。

你的任务：根据当前宏观经济数据发布情况 + 最新市场新闻，判断【宏观风险状态】。

只输出一个 JSON 对象，不要任何其他文字。第一个字符必须是 {，最后一个字符必须是 }：
{"regime": "RISK_ON|NEUTRAL|RISK_OFF", "confidence": 0.0-1.0, "exposure_mult": 0.0-1.2, "reason": "一句话"}

judgement 规则：
- RISK_ON：宏观数据利好、市场情绪积极 → exposure_mult 1.0~1.2（可满仓甚至略超）
- NEUTRAL：无明显宏观方向 → exposure_mult 1.0（正常仓位）
- RISK_OFF：通胀超预期、衰退担忧、地缘冲突、美联储鹰派 → exposure_mult 0.3~0.7（缩小敞口）
- 若正逢重大数据刚发布（CPI/NFP），且市场反应剧烈 → 倾向 RISK_OFF 保守处理

只依据给定信息判断，不要臆造。再次强调：只输出 JSON。"""


def llm_macro_view(news: list[dict], macro_ctx: dict) -> dict | None:
    """让 LLM 判断宏观状态。失败返回 None（调用方降级）。"""
    try:
        from llm.client import chat, extract_json
        import json as _json
        payload = {"macro_context": macro_ctx,
                   "news": [{"title": n["title"], "content": n["content"]}
                            for n in news[:6]]}
        msgs = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "当前情况：\n" +
             _json.dumps(payload, ensure_ascii=False, indent=1)[:4000]},
        ]
        raw = chat(msgs, temperature=0.1, max_tokens=600, timeout=120)
        parsed = None
        try:
            parsed = extract_json(raw)
        except Exception:  # noqa: BLE001
            parsed = None
        if isinstance(parsed, list):
            parsed = parsed[0] if parsed else None
        if not isinstance(parsed, dict) or "regime" not in parsed:
            parsed = _fallback_parse(raw)
        parsed["_raw"] = raw[:400]
        return parsed
    except Exception as e:  # noqa: BLE001
        return {"regime": None, "error": str(e), "_raw": ""}


def _fallback_parse(text: str) -> dict:
    """LLM 不守格式时的兜底：既看显式标签，也看整体情绪倾向。"""
    t = (text or "").upper()
    raw = text or ""
    # 1) 显式标签优先
    if "RISK_OFF" in t or "RISK OFF" in t or "避险" in raw or "风险规避" in raw:
        return {"regime": "RISK_OFF", "confidence": None, "exposure_mult": 0.5,
                "reason": "关键词兜底(RISK_OFF)"}
    if "RISK_ON" in t or "RISK ON" in t or "风险偏好" in raw:
        return {"regime": "RISK_ON", "confidence": None, "exposure_mult": 1.1,
                "reason": "关键词兜底(RISK_ON)"}

    # 2) 整体情绪打分：负面词多于正面词 → RISK_OFF
    neg_words = ["通胀高企", "加息", "鹰派", "衰退", "下跌", "暴跌", "风险", "抛售",
                 "收紧", "担忧", "恶化", "利空", "下行", "hike", "hawkish",
                 "recession", "sell-off", "selloff", "tumble", "drop", "fall",
                 "higher than expected", "hot inflation", "beat"]
    pos_words = ["降息", "宽松", "上涨", "反弹", "利好", "乐观", "复苏", "鸽派",
                 "rally", "dovish", "rate cut", "gains", "surge", "beat expectations",
                 "cooler", "softer"]
    neg = sum(1 for w in neg_words if w.upper() in t or w in raw)
    pos = sum(1 for w in pos_words if w.upper() in t or w in raw)

    if neg > pos:
        return {"regime": "RISK_OFF", "confidence": 0.5,
                "exposure_mult": max(0.4, 0.8 - 0.1 * (neg - pos)),
                "reason": f"情绪兜底(负面{neg}>正面{pos})，降敞口"}
    if pos > neg:
        return {"regime": "RISK_ON", "confidence": 0.5,
                "exposure_mult": min(1.2, 1.0 + 0.1 * (pos - neg)),
                "reason": f"情绪兜底(正面{pos}>负面{neg})，可维持"}
    return {"regime": "NEUTRAL", "confidence": 0.5, "exposure_mult": 1.0,
            "reason": "无法解析，保守中性"}


# ---------------------------------------------------------------------------
# 4) 综合：是否该让 LLM 做宏观介入 + 结论
# ---------------------------------------------------------------------------
def macro_intervention(as_of: date | None = None) -> dict:
    """判断是否需要宏观介入，并（需要时）调用 LLM。

    返回 {should_intervene, reason, regime, exposure_mult, macro_ctx, news}
    """
    as_of = as_of or date.today()
    rec: dict = {"ts": datetime.now(timezone.utc).isoformat(), "as_of": str(as_of)}

    major_today, ev_name = is_major_macro_day(as_of)
    last_ev, days_ago = days_since_last_big_release(as_of)

    macro_ctx = {"major_release_today": major_today, "event_today": ev_name,
                 "last_major_event": last_ev, "days_since_last": days_ago}
    rec["macro_ctx"] = macro_ctx

    # 触发条件：今天有重大数据，或近 2 天刚发布（市场还在消化）
    should = major_today or (days_ago is not None and days_ago <= 1)
    rec["should_intervene"] = bool(should)
    if not should:
        rec.update(regime="NEUTRAL", exposure_mult=1.0,
                   reason="无重大宏观事件，不介入")
        return rec

    # 抓新闻 → LLM 判断
    news = fetch_market_news()
    rec["n_news"] = len(news)
    view = llm_macro_view(news, macro_ctx)
    if view:
        rec["regime"] = view.get("regime")
        rec["exposure_mult"] = view.get("exposure_mult")
        rec["llm_reason"] = view.get("reason")
        rec["llm_raw"] = view.get("_raw", "")
        if view.get("regime") is None or view.get("error"):
            # 降级：重大数据日保守降风险
            rec["exposure_mult"] = 0.6
            rec["regime"] = "RISK_OFF"
            rec["llm_reason"] = "LLM 失败，降级为保守降风险"
    else:
        rec["exposure_mult"] = 0.6
        rec["regime"] = "RISK_OFF"
        rec["llm_reason"] = "LLM 无响应，降级"

    # 记录
    DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DECISION_LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return rec


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="宏观事件 → LLM 判断")
    ap.add_argument("--as-of", default=None)
    args = ap.parse_args()
    d = date.fromisoformat(args.as_of) if args.as_of else None
    out = macro_intervention(d)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
