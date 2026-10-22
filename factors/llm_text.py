"""LLM 文本语气因子：用大模型给新闻标题打语义分。

为什么用 LLM：词典法只能数情绪词，LLM 能理解
否定、因果、预期、不确定性、具体数字方向等语义。
成本：每条新闻一次调用，所以只对重点 symbol 的新增新闻打分，
结果缓存复用。
"""
from __future__ import annotations
import json
import hashlib
from pathlib import Path

import pandas as pd

from core import config
from data.store import NEWS_DIR
from data.news import load_news
from llm.client import chat, extract_json

SCORE_CACHE = NEWS_DIR / "llm_tone_cache.parquet"

SYSTEM = """你是金融新闻情绪分析器。对给定的股票新闻标题，输出该新闻对股价的预期影响。

只输出 JSON：
{"score": <-1.0到1.0>, "reason": "一句话"}

score 语义：-1=强烈利空，0=中性，1=强烈利好。
考虑：业绩、指引、监管、诉讼、并购、产品、宏观等。数字要判断方向。"""


def _cache_key(title: str) -> str:
    return hashlib.md5(title.encode("utf-8")).hexdigest()


def score_title(title: str) -> float | None:
    try:
        raw = chat([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"标题：{title}"}],
                   temperature=0.2, max_tokens=200, retries=2)
        obj = extract_json(raw)
        if isinstance(obj, list):
            obj = obj[0]
        s = float(obj.get("score"))
        return max(-1.0, min(1.0, s))
    except Exception:
        return None


def score_news_batch(symbols: list[str] | None = None, limit: int = 200) -> pd.DataFrame:
    """给本地新闻库里的标题打分，缓存。"""
    df = load_news()
    if df.empty:
        return pd.DataFrame(columns=["symbol", "published_at", "title", "llm_tone"])
    if symbols:
        df = df[df["symbol"].isin(symbols)]
    df = df.drop_duplicates("title").head(limit)

    cache = load_tone_cache()
    done = set(cache["title"]) if not cache.empty else set()

    rows = []
    for _, r in df.iterrows():
        title = r["title"]
        if title in done:
            continue
        s = score_title(title)
        if s is not None:
            rows.append({"symbol": r["symbol"], "published_at": r["published_at"],
                         "title": title, "llm_tone": s})
    if rows:
        new = pd.DataFrame(rows)
        cache = pd.concat([cache, new], ignore_index=True) if not cache.empty else new
        cache = cache.drop_duplicates("title")
        SCORE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache.to_parquet(SCORE_CACHE)
    return cache


def load_tone_cache() -> pd.DataFrame:
    if SCORE_CACHE.exists():
        return pd.read_parquet(SCORE_CACHE)
    return pd.DataFrame(columns=["symbol", "published_at", "title", "llm_tone"])


def llm_tone_factor(panel, n: int = 5):
    """LLM 打分的日频情绪因子。"""
    cache = load_tone_cache()
    td, syms = panel["c"].index, panel["c"].columns
    if cache.empty or "llm_tone" not in cache.columns:
        return pd.DataFrame(index=td, columns=syms, dtype=float)
    d = cache.dropna(subset=["llm_tone"]).copy()
    d["published_at"] = pd.to_datetime(d["published_at"], utc=True)
    d["date"] = d["published_at"].dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    idx = pd.DatetimeIndex(td).searchsorted(d["date"], side="right") - 1
    idx = idx.clip(0, len(td) - 1)
    d["tdate"] = td[idx]
    piv = d.pivot_table(index="tdate", columns="symbol", values="llm_tone", aggfunc="mean")
    return piv.reindex(index=td, columns=syms).rolling(n, min_periods=1).mean()
