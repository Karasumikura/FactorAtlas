"""Binance 美股/TradFi 永续接入 —— 只读层（无需 API key）。

覆盖：
  - 标的发现（TRADIFI_PERPETUAL：美股个股、ETF、指数、港股、韩股、商品、Pre-IPO）
  - 实时行情、资金费率、历史K线
  - 流动性排序，筛出可交易标的

设计原则：
  - 只依赖 requests（venv 已有），不依赖 ccxt（其 sandbox 模式已被币安弃用）。
  - 主网 endpoint 常量集中，便于切 demo。
  - 所有函数对网络错误返回空/None，不抛异常，方便批量调用。

注意（重要，策略层面）：
  - 这是 7x24 永续，但底层美股有休市。周末/夜盘盘口靠 EWMA 模式维持，
    资金费率在休市期会有系统性偏移 —— A股不存在的新风险，必须建模。
  - 交易底层是 Nest Trading(ADGM) -> Alpaca(US)，执行/清算是真券商路径。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
FAPI_MAIN = "https://fapi.binance.com"
FAPI_DEMO = "https://testnet.binancefuture.com"  # Futures Demo Trading（下单层用）

_TIMEOUT = 20.0


def _get(base: str, path: str, params: dict | None = None, retries: int = 3) -> Any:
    """带重试的 GET，失败返回 None。"""
    url = base + path
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)
        time.sleep(0.5 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# 标的元数据
# ---------------------------------------------------------------------------
# underlyingType 分类（币安实测值）
EQUITY_TYPES = {"EQUITY", "HK_EQUITY", "KR_EQUITY", "CN_EQUITY", "PREMARKET"}
COMMODITY_TYPES = {"COMMODITY"}
TRADFI_TYPES = EQUITY_TYPES | COMMODITY_TYPES | {"FX"}


@dataclass
class TradFiSymbol:
    symbol: str
    base: str
    quote: str
    underlying_type: str
    sub_types: list[str] = field(default_factory=list)
    onboard_ms: int = 0
    price_precision: int = 2
    qty_precision: int = 3
    min_qty: float = 0.0
    step_size: float = 0.0
    tick_size: float = 0.0

    @property
    def is_equity(self) -> bool:
        return self.underlying_type in EQUITY_TYPES

    @property
    def is_commodity(self) -> bool:
        return self.underlying_type in COMMODITY_TYPES

    @property
    def onboard_date(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.onboard_ms / 1000)) if self.onboard_ms else "?"


def _parse_filters(filters: list[dict]) -> dict:
    out = {"min_qty": 0.0, "step_size": 0.0, "tick_size": 0.0}
    for f in filters or []:
        ft = f.get("filterType")
        if ft == "LOT_SIZE":
            out["min_qty"] = float(f.get("minQty", 0) or 0)
            out["step_size"] = float(f.get("stepSize", 0) or 0)
        elif ft == "PRICE_FILTER":
            out["tick_size"] = float(f.get("tickSize", 0) or 0)
    return out


def list_tradfi_symbols(base: str = FAPI_MAIN) -> list[TradFiSymbol]:
    """返回全部 TRADIFI_PERPETUAL 标的元数据。无需 key。"""
    data = _get(base, "/fapi/v1/exchangeInfo")
    if not data:
        return []
    out: list[TradFiSymbol] = []
    for s in data.get("symbols", []):
        if s.get("contractType") != "TRADIFI_PERPETUAL":
            continue
        flt = _parse_filters(s.get("filters", []))
        out.append(
            TradFiSymbol(
                symbol=s["symbol"],
                base=s.get("baseAsset", ""),
                quote=s.get("quoteAsset", ""),
                underlying_type=s.get("underlyingType", ""),
                sub_types=s.get("underlyingSubType", []) or [],
                onboard_ms=int(s.get("onboardDate", 0) or 0),
                price_precision=int(s.get("pricePrecision", 2) or 2),
                qty_precision=int(s.get("quantityPrecision", 3) or 3),
                **flt,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------
def tickers_24h(base: str = FAPI_MAIN) -> dict[str, dict]:
    data = _get(base, "/fapi/v1/ticker/24hr")
    if not data:
        return {}
    return {t["symbol"]: t for t in data}


def premium_index(base: str = FAPI_MAIN) -> dict[str, dict]:
    """含 lastFundingRate（当期资金费率，通常 8h 一次）。"""
    data = _get(base, "/fapi/v1/premiumIndex")
    if not data:
        return {}
    return {p["symbol"]: p for p in data}


def klines(symbol: str, interval: str = "1d", limit: int = 500,
           start_ms: int | None = None, end_ms: int | None = None,
           base: str = FAPI_MAIN) -> list[list]:
    """官方 K 线。返回原始 list-of-list：
    [openTime, open, high, low, close, volume, closeTime, quoteVol, trades, ...]
    """
    params: dict = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms:
        params["startTime"] = start_ms
    if end_ms:
        params["endTime"] = end_ms
    data = _get(base, "/fapi/v1/klines", params)
    return data or []


def funding_rate_history(symbol: str, limit: int = 100, base: str = FAPI_MAIN) -> list[dict]:
    data = _get(base, "/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})
    return data or []


# ---------------------------------------------------------------------------
# 流动性排序
# ---------------------------------------------------------------------------
@dataclass
class LiquidRow:
    symbol: str
    base: str
    underlying_type: str
    quote_volume_24h: float
    last_price: float
    funding_rate: float  # 当期 8h 费率
    onboard_date: str


def liquid_screen(tradfi_only: bool = True, min_quote_vol: float = 0.0) -> list[LiquidRow]:
    """按 24h USDT 成交额降序返回标的，附资金费率。"""
    syms = list_tradfi_symbols()
    if not tradfi_only:
        syms = [s for s in syms if True]  # 占位，接口一致
    tk = tickers_24h()
    pm = premium_index()
    rows: list[LiquidRow] = []
    for s in syms:
        t = tk.get(s.symbol, {})
        p = pm.get(s.symbol, {})
        try:
            qv = float(t.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            qv = 0.0
        if qv < min_quote_vol:
            continue
        try:
            px = float(t.get("lastPrice", 0) or 0)
        except (TypeError, ValueError):
            px = 0.0
        try:
            fr = float(p.get("lastFundingRate", 0) or 0)
        except (TypeError, ValueError):
            fr = 0.0
        rows.append(LiquidRow(s.symbol, s.base, s.underlying_type, qv, px, fr, s.onboard_date))
    rows.sort(key=lambda r: -r.quote_volume_24h)
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Binance TradFi(美股) 永续只读工具")
    ap.add_argument("--top", type=int, default=30, help="显示成交额前 N 个")
    ap.add_argument("--type", default=None, help="过滤 underlyingType，如 EQUITY / COMMODITY")
    ap.add_argument("--min-vol", type=float, default=0.0, help="最小 24h 成交额(USDT)")
    ap.add_argument("--save", default=None, help="把完整清单存成 JSON")
    args = ap.parse_args()

    rows = liquid_screen(min_quote_vol=args.min_vol)
    if args.type:
        rows = [r for r in rows if r.underlying_type == args.type]

    print(f"{'symbol':18s} {'base':12s} {'type':10s} {'24hUSDT':>16s} {'price':>10s} {'fund/8h':>10s} {'上线':>10s}")
    print("-" * 92)
    for r in rows[: args.top]:
        print(f"{r.symbol:18s} {r.base:12s} {r.underlying_type:10s} "
              f"{r.quote_volume_24h:>16,.0f} {r.last_price:>10.2f} "
              f"{r.funding_rate:>9.4%} {r.onboard_date:>10s}")

    eq = [r for r in rows if r.underlying_type in EQUITY_TYPES]
    cm = [r for r in rows if r.underlying_type in COMMODITY_TYPES]
    print(f"\n合计 {len(rows)} 个（美股类 {len(eq)}、商品 {len(cm)}）")

    if args.save:
        with open(args.save, "w") as f:
            json.dump([r.__dict__ for r in rows], f, indent=1)
        print(f"已存 {args.save}")


if __name__ == "__main__":
    _main()
