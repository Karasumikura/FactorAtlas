"""数据层：优先用 PRO 现成 daily_reference 缓存；可增量从 Alpaca 刷新。

对外统一给出面板 (panel):
  - close: DataFrame [date x symbol]
  - volume: DataFrame [date x symbol]
  - vwap: DataFrame [date x symbol]
  - open/high/low 同理
所有时间用交易日（美东日期）。
"""
from __future__ import annotations
import json
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config

NY = ZoneInfo("America/New_York")
_CACHE = config.DATA_DIR / "panel.parquet"


def load_reference(path=None) -> dict:
    path = path or config.PRO_DAILY_REFERENCE
    with open(path) as f:
        return json.load(f)


def _bars_to_frames(bars: dict) -> dict[str, pd.DataFrame]:
    """bars: {symbol: [ {c,h,l,o,t,v,vw}, ... ]} -> {field: DataFrame}"""
    records = {"o": {}, "h": {}, "l": {}, "c": {}, "v": {}, "vw": {}}
    for sym, seq in bars.items():
        for b in seq:
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            d = ts.astimezone(NY).date().isoformat()
            for k, src in [("o", "o"), ("h", "h"), ("l", "l"), ("c", "c"),
                           ("v", "v"), ("vw", "vw")]:
                if src in b and b[src] is not None:
                    records[k].setdefault(d, {})[sym] = float(b[src])
    out = {}
    for k, dd in records.items():
        df = pd.DataFrame(dd).T  # index=date, col=symbol
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        out[k] = df
    return out


def build_panel(force: bool = False, prefer_long: bool = True,
                market: str = "us", index: str | None = None) -> dict[str, pd.DataFrame]:
    """构建/加载面板。

    market="us"  -> Alpaca 长历史（data/hist）
    market="ashare" -> A股（data/ashare 或 data/ashare_<index>，腾讯后复权）
      index: None/"000300"=沪深300, "000905"=中证500, "000852"=中证1000
    market="crypto" -> 币安 USDT-M 永续主流币（data/crypto，见 data/crypto.py）
      标的池：主流币白名单 ∧ 上市≥4年 ∧ 24h成交额≥5000万
    """
    if market == "crypto":
        from data import crypto
        p = crypto.load_panel()
        if p is None:
            raise RuntimeError(
                "加密面板不存在，请先运行：venv/bin/python -c "
                "\"from data import crypto; crypto.build_panel(force=True)\"")
        return p
    if market == "ashare":
        from data import ashare
        p = ashare.load_panel(index)
        if p is not None:
            return p
        raise RuntimeError(
            f"A股面板不存在（index={index}），请先运行 scripts/pull_ashare.py {index or '000300'}")
    if prefer_long:
        long_panel = load_long_panel()
        if long_panel is not None and not force:
            return long_panel
    if _CACHE.exists() and not force:
        return load_panel_cache()
    ref = load_reference()
    frames = _bars_to_frames(ref["bars"])
    save_panel_cache(frames, meta={"source": ref.get("source"), "fetched_at": ref.get("fetched_at")})
    return frames


def load_long_panel() -> dict | None:
    """加载 Alpaca 长历史面板（data/hist/）。"""
    hist_dir = config.DATA_DIR / "hist"
    fields = ["o", "h", "l", "c", "v", "vw"]
    out = {}
    for k in fields:
        p = hist_dir / f"hist_{k}.parquet"
        if not p.exists():
            return None
        out[k] = pd.read_parquet(p)
    return out


def save_panel_cache(frames: dict, meta: dict | None = None):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for k, df in frames.items():
        df.to_parquet(config.DATA_DIR / f"panel_{k}.parquet")
    if meta:
        (config.DATA_DIR / "panel_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def load_panel_cache() -> dict:
    fields = ["o", "h", "l", "c", "v", "vw"]
    out = {}
    for k in fields:
        p = config.DATA_DIR / f"panel_{k}.parquet"
        if not p.exists():
            return build_panel(force=True)
        out[k] = pd.read_parquet(p)
    return out


# --- 增量刷新（Alpaca SIP） ---
def fetch_alpaca_bars(symbols: list[str], start: str, end: str, timeframe="1Day") -> dict:
    if not config.has_alpaca():
        raise RuntimeError("缺少 ALPACA_API_KEY/SECRET")
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET,
    }
    out, page = {}, None
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i + 100]
        params = {
            "symbols": ",".join(batch), "timeframe": timeframe,
            "start": start, "end": end, "limit": 10000, "feed": config.ALPACA_FEED,
        }
        while True:
            if page:
                params["page_token"] = page
            r = requests.get("https://data.alpaca.markets/v2/stocks/bars",
                             headers=headers, params=params, timeout=60)
            r.raise_for_status()
            j = r.json()
            for sym, seq in (j.get("bars") or {}).items():
                out.setdefault(sym, []).extend(seq)
            page = j.get("next_page_token")
            if not page:
                break
    return out


def universe_symbols(min_history: int = 60) -> list[str]:
    ref = load_reference()
    return [s for s, seq in ref["bars"].items() if len(seq) >= min_history]
