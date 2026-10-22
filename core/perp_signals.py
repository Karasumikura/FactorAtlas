"""币安美股永续的「独有信号」数据层。

为什么这是别处没有的：
  现货美股只有价格和成交量。币安美股永续额外暴露了：
    - 资金费率（funding rate）：多空情绪的实时定价，正=多头拥挤
    - 基差（永续价 vs 指数价）：market 对"未来"的溢价
    - 持仓量（open interest）：杠杆资金的进出
  这些是「交易者情绪/拥挤度」的直接度量，现货市场拿不到。

本模块拉取并缓存这些，供因子挖掘使用。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from core import binance_us

PROJ = Path(__file__).resolve().parent.parent
CACHE = PROJ / "data" / "binance"


# ---------------------------------------------------------------------------
# 资金费率历史（8h 粒度）
# ---------------------------------------------------------------------------
def fetch_funding_series(symbol: str, start_ms: int, force: bool = False) -> pd.DataFrame:
    """拉取 8h 粒度资金费率历史。返回 [fundingTime, rate, markPrice]。"""
    cache_p = CACHE / f"fund8h_{symbol}.parquet"
    if cache_p.exists() and not force:
        return pd.read_parquet(cache_p)

    rows: list[dict] = []
    cursor = start_ms
    while True:
        batch = binance_us._get(binance_us.FAPI_MAIN, "/fapi/v1/fundingRate",
                                {"symbol": symbol, "startTime": cursor, "limit": 1000})
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        cursor = int(batch[-1]["fundingTime"]) + 1
        time.sleep(0.12)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["fundingTime"], unit="ms")
    df["rate"] = df["fundingRate"].astype(float)
    out = df[["time", "rate"]].set_index("time").sort_index()
    CACHE.mkdir(parents=True, exist_ok=True)
    out.to_parquet(cache_p)
    return out


# ---------------------------------------------------------------------------
# 基差（永续 markPrice vs 底层 indexPrice）
# ---------------------------------------------------------------------------
def fetch_basis_history(symbol: str, interval: str = "8h", limit: int = 500) -> pd.DataFrame:
    """用 markPriceKlines 和 indexPriceKlines 算基差。

    注意：
      - markPriceKlines 用 symbol 参数
      - indexPriceKlines 用 pair 参数（币安要求，且 pair 通常是 base+USDT）
    基差 = (mark - index) / index，反映永续相对底层指数的溢价。
    """
    mark = binance_us._get(binance_us.FAPI_MAIN, "/fapi/v1/markPriceKlines",
                           {"symbol": symbol, "interval": interval, "limit": limit})
    pair = symbol  # pair 一般等于 symbol（如 NVDAUSDT）
    idx = binance_us._get(binance_us.FAPI_MAIN, "/fapi/v1/indexPriceKlines",
                          {"pair": pair, "interval": interval, "limit": limit})
    if not mark or not idx:
        return pd.DataFrame()
    mk = pd.DataFrame(mark)[[0, 4]]
    mk.columns = ["time", "mark"]
    ix = pd.DataFrame(idx)[[0, 4]]
    ix.columns = ["time", "index"]
    df = mk.merge(ix, on="time")
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df["mark"] = df["mark"].astype(float)
    df["index"] = df["index"].astype(float)
    df["basis"] = (df["mark"] - df["index"]) / df["index"]
    return df.set_index("time").sort_index()


# ---------------------------------------------------------------------------
# 持仓量（open interest）
# ---------------------------------------------------------------------------
def fetch_oi_history(symbol: str, period: str = "1d", limit: int = 500) -> pd.DataFrame:
    """拉持仓量历史 /futures/data/openInterestHist（注意是 /futures/data 前缀）。"""
    # 该接口在 fapi 主机但路径为 /futures/data/...
    data = binance_us._get(binance_us.FAPI_MAIN, "/futures/data/openInterestHist",
                           {"symbol": symbol, "period": period, "limit": limit})
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["time"] = pd.to_datetime(df["timestamp"], unit="ms")
    for c in ["sumOpenInterest", "sumOpenInterestValue"]:
        if c in df:
            df[c] = df[c].astype(float)
    return df.set_index("time").sort_index()


# ---------------------------------------------------------------------------
# 批量拉取（对可映射的美股永续）
# ---------------------------------------------------------------------------
def pull_all(start_date: str = "2026-01-01", top: int = 60) -> dict:
    from core.us_mapping import build_mapping
    mapped, _ = build_mapping()
    mapped = mapped[:top]
    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    ok = 0
    for i, m in enumerate(mapped):
        try:
            f = fetch_funding_series(m.binance_symbol, start_ms)
            if not f.empty:
                ok += 1
            print(f"  [{i+1}/{len(mapped)}] {m.us_ticker:8s} 费率 {len(f)} 条")
        except Exception as e:  # noqa: BLE001
            print(f"  [{i+1}/{len(mapped)}] {m.us_ticker:8s} ERR {e}")
    print(f"\n完成: {ok}/{len(mapped)}")
    return {"ok": ok, "total": len(mapped)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="币安永续独有信号数据层")
    ap.add_argument("cmd", choices=["pull", "funding", "basis", "oi"])
    ap.add_argument("--symbol", default="NVDAUSDT")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--top", type=int, default=60)
    args = ap.parse_args()

    if args.cmd == "pull":
        pull_all(args.start, args.top)
    elif args.cmd == "funding":
        f = fetch_funding_series(args.symbol, int(pd.Timestamp(args.start).timestamp() * 1000))
        print(f"{args.symbol} 费率: {len(f)} 条")
        print(f.tail(10).to_string())
    elif args.cmd == "basis":
        b = fetch_basis_history(args.symbol)
        print(f"{args.symbol} 基差: {len(b)} 条")
        if not b.empty:
            print(f"  基差 mean={b['basis'].mean():.5%} std={b['basis'].std():.5%} "
                  f"last={b['basis'].iloc[-1]:.5%}")
    elif args.cmd == "oi":
        o = fetch_oi_history(args.symbol)
        print(f"{args.symbol} OI: {len(o)} 条")
        if not o.empty:
            print(o.tail(5).to_string())
