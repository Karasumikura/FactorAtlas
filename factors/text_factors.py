"""文本因子：把新闻聚合成日频横截面因子。

因子类型：
1. news_sentiment_5d   —— 近5日新闻平均情绪（Marketaux 实体分）
2. news_sentiment_chg  —— 情绪变化（近5日 - 前5日）
3. news_attention      —— 新闻数量（关注度）
4. news_tone_dispersion—— 情绪分歧度（新闻多空不一致程度）
5. llm_tone            —— LLM 对新闻标题打分（可选，慢但更懂语义）
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from data.news import load_news


def _daily_sentiment(trading_dates, symbols) -> pd.DataFrame:
    """把新闻按交易日聚合到 [date x symbol] 的平均情绪。"""
    df = load_news()
    if df.empty:
        return pd.DataFrame(index=trading_dates, columns=symbols, dtype=float)
    d = df.dropna(subset=["sentiment"]).copy()
    d["published_at"] = pd.to_datetime(d["published_at"], utc=True)
    d["date"] = d["published_at"].dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    # 对齐到最近的交易日
    td = pd.DatetimeIndex(trading_dates)
    idx = td.searchsorted(d["date"], side="right") - 1
    idx = np.clip(idx, 0, len(td) - 1)
    d["tdate"] = td[idx]
    piv = d.pivot_table(index="tdate", columns="symbol", values="sentiment", aggfunc="mean")
    return piv.reindex(index=td, columns=symbols)


def _daily_count(trading_dates, symbols) -> pd.DataFrame:
    df = load_news()
    if df.empty:
        return pd.DataFrame(index=trading_dates, columns=symbols, dtype=float)
    d = df.copy()
    d["published_at"] = pd.to_datetime(d["published_at"], utc=True)
    d["date"] = d["published_at"].dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    td = pd.DatetimeIndex(trading_dates)
    idx = np.clip(td.searchsorted(d["date"], side="right") - 1, 0, len(td) - 1)
    d["tdate"] = td[idx]
    piv = d.pivot_table(index="tdate", columns="symbol", values="title", aggfunc="count")
    return piv.reindex(index=td, columns=symbols)


def _daily_dispersion(trading_dates, symbols) -> pd.DataFrame:
    df = load_news()
    if df.empty:
        return pd.DataFrame(index=trading_dates, columns=symbols, dtype=float)
    d = df.dropna(subset=["sentiment"]).copy()
    d["published_at"] = pd.to_datetime(d["published_at"], utc=True)
    d["date"] = d["published_at"].dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    td = pd.DatetimeIndex(trading_dates)
    idx = np.clip(td.searchsorted(d["date"], side="right") - 1, 0, len(td) - 1)
    d["tdate"] = td[idx]
    piv = d.pivot_table(index="tdate", columns="symbol", values="sentiment", aggfunc="std")
    return piv.reindex(index=td, columns=symbols)


def news_sentiment_5d(panel, n=5):
    td, syms = panel["c"].index, panel["c"].columns
    daily = _daily_sentiment(td, syms)
    return daily.rolling(n, min_periods=1).mean()


def news_sentiment_chg(panel, n=5):
    td, syms = panel["c"].index, panel["c"].columns
    daily = _daily_sentiment(td, syms)
    recent = daily.rolling(n, min_periods=1).mean()
    prev = daily.shift(n).rolling(n, min_periods=1).mean()
    return recent - prev


def news_attention(panel, n=5):
    td, syms = panel["c"].index, panel["c"].columns
    cnt = _daily_count(td, syms).fillna(0.0)
    return cnt.rolling(n, min_periods=1).sum()


def news_tone_dispersion(panel, n=5):
    td, syms = panel["c"].index, panel["c"].columns
    disp = _daily_dispersion(td, syms)
    return disp.rolling(n, min_periods=1).mean()


NEWS_OPS = {
    "news_sentiment_5d": news_sentiment_5d,
    "news_sentiment_chg": news_sentiment_chg,
    "news_attention": news_attention,
    "news_tone_dispersion": news_tone_dispersion,
}
