"""多周期 K 线拉取：为「4H 定方向 + 30m 入场」共振策略准备数据。

币安 /fapi/v1/klines 单次最多 1500 根。本项目策略只需近期窗口
（4H 约需 200 根 ≈ 33 天；30m 约需 200 根 ≈ 4 天），因此单次拉取足够。
落盘为 parquet：data/binance/kline_<interval>_<SYMBOL>.parquet

用法：
    python -m core.mtf_fetch --interval 4h --limit 1500
    python -m core.mtf_fetch --interval 30m --limit 1000
    python -m core.mtf_fetch --interval both
    python -m core.mtf_fetch --interval 4h --symbols GOOGLUSDT,NVDAUSDT
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from core.binance_us import klines
from core.us_mapping import build_mapping

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "binance"

# 币安 K 线列顺序
_COLS = ["open_time", "open", "high", "low", "close", "volume",
         "close_time", "quote_volume", "trades", "taker_base",
         "taker_quote", "ignore"]


def fetch_klines(symbol: str, interval: str = "4h", limit: int = 1500,
                 retries: int = 3) -> pd.DataFrame | None:
    """拉单个标的的 K 线，返回带时间索引的 OHLCV DataFrame。失败返回 None。"""
    for attempt in range(retries):
        try:
            raw = klines(symbol, interval=interval, limit=limit)
            if not raw:
                return None
            df = pd.DataFrame(raw, columns=_COLS)
            df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            for c in ("open", "high", "low", "close", "volume", "quote_volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df[["ts", "open", "high", "low", "close", "volume", "quote_volume"]]
            return df.set_index("ts").sort_index()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                print(f"  [fail] {symbol} {interval}: {e}", flush=True)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def fetch_universe(interval: str = "4h", limit: int = 1500,
                   symbols: list[str] | None = None) -> int:
    """拉一批标的的 K 线并落盘。返回成功数。"""
    if symbols is None:
        mapped, _ = build_mapping()
        symbols = [m.binance_symbol for m in mapped]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for i, sym in enumerate(symbols, 1):
        df = fetch_klines(sym, interval=interval, limit=limit)
        if df is None or df.empty:
            continue
        path = DATA_DIR / f"kline_{interval}_{sym}.parquet"
        df.to_parquet(path)
        ok += 1
        if i % 20 == 0:
            print(f"  [{i}/{len(symbols)}] 已存 {ok} ...", flush=True)
        time.sleep(0.12)   # 轻限速，避免触发币安权重限制
    print(f"[mtf_fetch] {interval}: {ok}/{len(symbols)} 个标的已落盘 -> {DATA_DIR}", flush=True)
    return ok


def load_kline(symbol: str, interval: str = "4h") -> pd.DataFrame | None:
    """读取已落盘的 K 线。返回 None 表示没有。"""
    path = DATA_DIR / f"kline_{interval}_{symbol}.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="多周期 K 线拉取")
    ap.add_argument("--interval", default="4h",
                    help="周期：4h / 30m / 1h / 1d / both(=4h+30m)")
    ap.add_argument("--limit", type=int, default=1500, help="每标的 K 线根数(<=1500)")
    ap.add_argument("--symbols", default=None,
                    help="逗号分隔；缺省=全部可映射标的")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    intervals = ["4h", "30m"] if args.interval == "both" else [args.interval]
    for iv in intervals:
        print(f"[mtf_fetch] 开始拉 {iv} ...", flush=True)
        fetch_universe(interval=iv, limit=args.limit, symbols=syms)


if __name__ == "__main__":
    main()
