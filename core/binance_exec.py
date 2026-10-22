"""币安美股永续 —— 执行层客户端（签名 + 下单 + 精度吸附）。

设计说明：
  逻辑参考并收编自开源项目 muvon/mcp-binance-futures (Apache-2.0) 的
  client.py / server.py 中经过验证的签名与 exchange-filter 处理，
  但去掉 MCP 协议层（那是给 LLM 对话客户端用的），只保留策略程序能直接调用的部分。

关键能力：
  - HMAC-SHA256 签名 + recvWindow
  - 精度吸附：stepSize / tickSize / minQty / minNotional（避免 -1111/-1013/-4164）
  - 主网 / Demo(testnet) 可切换
  - 条件单走 Algo API（币安 2025-12 起要求）
  - 关键错误码的可读翻译

安全（务必遵守）：
  - API key 必须关闭「提现」权限，只开「合约交易」。
  - 绑 IP 白名单到本服务器。
  - 先用 testnet.binancefuture.com 的 Demo key 验证。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Literal
from urllib.parse import urlencode

import requests

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
FAPI_MAIN = "https://fapi.binance.com"
FAPI_DEMO = "https://testnet.binancefuture.com"

RECV_WINDOW = 5000  # ms
_HTTP_TIMEOUT = 15.0

# 常见错误码 → 可读含义（调试用）
ERR_HINT = {
    -1021: "时间戳超出 recvWindow：服务器时钟漂移，检查 NTP 同步或调大 RECV_WINDOW",
    -1022: "签名无效：secret 错误或参数顺序/编码不一致",
    -2014: "API key 格式不对",
    -2015: "API key 无效 / IP 不在白名单 / 权限不足",
    -2019: "保证金不足",
    -4131: "对手方最优价格无法成交（限价单价格偏离太大）",
    -1111: "精度错误：quantity/price 超出 stepSize/tickSize",
    -1013: "数量不满足 minQty / minNotional",
    -4164: "名义价值低于 MIN_NOTIONAL",
}


class BinanceExecError(Exception):
    def __init__(self, code: int | str, msg: str):
        self.code = code
        self.msg = msg
        hint = ERR_HINT.get(code) if isinstance(code, int) else None
        super().__init__(f"Binance error {code}: {msg}" + (f"  [{hint}]" if hint else ""))


class BinanceExec:
    """同步版美股永续执行客户端（不依赖 asyncio，便于嵌入现有脚本）。"""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True,
                 base_url: str | None = None):
        self._key = api_key
        self._secret = api_secret.encode()
        self._base = base_url or (FAPI_DEMO if testnet else FAPI_MAIN)
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": api_key})

    # -- 构造 ----------------------------------------------------------------
    @classmethod
    def from_env(cls, testnet: bool = True) -> "BinanceExec":
        # 通过 core.config 自动加载 .env（BINANCE_API_KEY / BINANCE_API_SECRET）
        from core import config  # noqa: F401  触发 load_dotenv
        k = os.environ.get("BINANCE_API_KEY", "")
        s = os.environ.get("BINANCE_API_SECRET", "")
        if not k or not s:
            raise RuntimeError("缺少 BINANCE_API_KEY / BINANCE_API_SECRET 环境变量")
        return cls(k, s, testnet=testnet)

    # -- 签名 ----------------------------------------------------------------
    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = RECV_WINDOW
        query = urlencode(params)
        params["signature"] = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        return params

    # -- 请求 ----------------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False) -> Any:
        p = dict(params or {})
        if signed:
            p = self._sign(p)
        url = self._base + path
        if method in ("GET", "DELETE") and signed:
            # 签名参数放 query
            r = self._session.request(method, url, params=p, timeout=_HTTP_TIMEOUT)
        elif signed:
            r = self._session.request(method, url, data=p, timeout=_HTTP_TIMEOUT)
        else:
            r = self._session.request(method, url, params=p, timeout=_HTTP_TIMEOUT)
        try:
            data = r.json()
        except Exception:
            if not r.ok:
                raise BinanceExecError(r.status_code, r.text[:300])
            return r.text
        if isinstance(data, dict) and "code" in data and int(data["code"]) != 200:
            raise BinanceExecError(int(data["code"]), data.get("msg", "unknown"))
        if not r.ok:
            msg = data.get("msg", r.text) if isinstance(data, dict) else r.text
            raise BinanceExecError(r.status_code, msg)
        return data

    def public(self, path: str, **params) -> Any:
        return self._request("GET", path, params or None, signed=False)

    def signed_get(self, path: str, **params) -> Any:
        return self._request("GET", path, params or None, signed=True)

    def signed_post(self, path: str, **params) -> Any:
        return self._request("POST", path, params or None, signed=True)

    def signed_delete(self, path: str, **params) -> Any:
        return self._request("DELETE", path, params or None, signed=True)

    # -- 只读便捷 ------------------------------------------------------------
    def ping(self) -> Any:
        return self.public("/fapi/v1/ping")

    def exchange_info(self) -> dict:
        return self.public("/fapi/v1/exchangeInfo")

    def ticker(self, symbol: str) -> dict:
        return self.public("/fapi/v1/ticker/24hr", symbol=symbol)

    def premium_index(self, symbol: str) -> dict:
        return self.public("/fapi/v1/premiumIndex", symbol=symbol)

    def klines(self, symbol: str, interval="1d", limit=100) -> list:
        return self.public("/fapi/v1/klines", symbol=symbol, interval=interval, limit=limit)

    # -- 账户 ----------------------------------------------------------------
    def balance(self) -> list:
        return self.signed_get("/fapi/v2/balance")

    def positions(self, symbol: str | None = None) -> list:
        p = {"symbol": symbol} if symbol else {}
        data = self.signed_get("/fapi/v2/positionRisk", **p)
        return [x for x in data if float(x.get("positionAmt", 0)) != 0]

    def account_summary(self) -> dict:
        return self.signed_get("/fapi/v2/account")

    # -- 精度吸附 ------------------------------------------------------------
    def symbol_filters(self, symbol: str) -> dict[str, float]:
        info = self.exchange_info()
        s = next((x for x in info["symbols"] if x["symbol"] == symbol.upper()), None)
        if not s:
            raise BinanceExecError("SYMBOL_NOT_FOUND", f"{symbol} 不在合约列表")
        f = {x["filterType"]: x for x in s.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        return {
            "step": float(lot.get("stepSize") or 0),
            "minQty": float(lot.get("minQty") or 0),
            "tick": float(f.get("PRICE_FILTER", {}).get("tickSize") or 0),
            "minNotional": float(f.get("MIN_NOTIONAL", {}).get("notional") or 0),
            "qtyPrec": int(s.get("quantityPrecision", 8)),
            "pricePrec": int(s.get("pricePrecision", 8)),
            "contractType": s.get("contractType"),
        }

    @staticmethod
    def snap_qty(qty: float, step: float, min_qty: float, prec: int,
                 ref_price: float | None = None, min_notional: float = 0.0) -> float:
        q = Decimal(str(qty))
        d_step = Decimal(str(step)) if step and step > 0 else Decimal(0)
        if d_step > 0:
            q = (q / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
        if min_qty and q < Decimal(str(min_qty)):
            q = Decimal(str(min_qty))
        if min_notional and ref_price and ref_price > 0:
            need = Decimal(str(min_notional)) / Decimal(str(ref_price))
            if q < need:
                q = ((need / d_step).to_integral_value(rounding=ROUND_CEILING) * d_step
                     if d_step > 0 else need)
        return float(q.quantize(Decimal(1).scaleb(-prec)))

    @staticmethod
    def snap_price(price: float, tick: float, prec: int) -> float:
        p = Decimal(str(price))
        if tick and tick > 0:
            d_tick = Decimal(str(tick))
            p = (p / d_tick).to_integral_value(rounding=ROUND_HALF_UP) * d_tick
        return float(p.quantize(Decimal(1).scaleb(-prec)))

    # -- 下单 ----------------------------------------------------------------
    _ALGO_TYPES = {"STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT",
                   "TRAILING_STOP_MARKET"}

    def place_order(self, symbol: str, side: Literal["BUY", "SELL"],
                    order_type: str = "MARKET", quantity: float | None = None,
                    price: float | None = None, stop_price: float | None = None,
                    time_in_force: str | None = None, reduce_only: bool | None = None,
                    close_position: bool | None = None,
                    client_order_id: str | None = None) -> dict:
        filt = self.symbol_filters(symbol)
        if price is not None:
            price = self.snap_price(price, filt["tick"], int(filt["pricePrec"]))
        if stop_price is not None:
            stop_price = self.snap_price(stop_price, filt["tick"], int(filt["pricePrec"]))
        if quantity is not None and not close_position:
            ref = price if price is not None else stop_price
            quantity = self.snap_qty(quantity, filt["step"], filt["minQty"],
                                     int(filt["qtyPrec"]), ref, filt["minNotional"])
        if order_type == "LIMIT" and not time_in_force:
            time_in_force = "GTC"

        params = {k: v for k, v in {
            "symbol": symbol, "side": side, "type": order_type,
            "quantity": quantity, "price": price, "stopPrice": stop_price,
            "timeInForce": time_in_force,
            "reduceOnly": str(reduce_only).lower() if reduce_only is not None else None,
            "closePosition": str(close_position).lower() if close_position is not None else None,
            "newClientOrderId": client_order_id,
        }.items() if v is not None}

        if order_type in self._ALGO_TYPES:
            algo = {k: v for k, v in {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": side,
                "type": order_type, "quantity": quantity, "price": price,
                "triggerPrice": stop_price, "timeInForce": time_in_force,
                "reduceOnly": str(reduce_only).lower() if reduce_only is not None else None,
                "closePosition": str(close_position).lower() if close_position is not None else None,
                "clientAlgoId": client_order_id,
            }.items() if v is not None}
            return self.signed_post("/fapi/v1/algoOrder", **algo)
        return self.signed_post("/fapi/v1/order", **params)

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self.signed_post("/fapi/v1/leverage", symbol=symbol, leverage=leverage)

    def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        return self.signed_post("/fapi/v1/marginType", symbol=symbol, marginType=margin_type)

    def open_orders(self, symbol: str) -> list:
        return self.signed_get("/fapi/v1/openOrders", symbol=symbol)

    def cancel_all(self, symbol: str) -> dict:
        return self.signed_delete("/fapi/v1/allOpenOrders", symbol=symbol)

    def user_trades(self, symbol: str, limit: int = 50) -> list:
        return self.signed_get("/fapi/v1/userTrades", symbol=symbol, limit=limit)


# ---------------------------------------------------------------------------
# CLI —— dry-run 与连通性检查（不需要 key 的部分可零配置运行）
# ---------------------------------------------------------------------------
def _cli() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="币安美股永续执行层")
    ap.add_argument("cmd", choices=["ping", "symbols", "ticker", "filters", "dryrun", "balance"],
                    help="ping/symbols/ticker/filters 为公开;dryrun/balance 需 key")
    ap.add_argument("--symbol", default="NVDAUSDT")
    ap.add_argument("--testnet", action="store_true", default=True)
    ap.add_argument("--mainnet", dest="testnet", action="store_false")
    ap.add_argument("--qty", type=float, default=0.1)
    args = ap.parse_args()

    if args.cmd == "ping":
        c = BinanceExec("x", "x", testnet=args.testnet)
        print(f"base={c._base}  ping ->", c.ping())
        return
    if args.cmd == "symbols":
        c = BinanceExec("x", "x", testnet=args.testnet)
        info = c.exchange_info()
        tradfi = [s for s in info["symbols"] if s.get("contractType") == "TRADIFI_PERPETUAL"]
        print(f"TradFi 永续: {len(tradfi)} 个（demo 环境可能为 0，属正常）")
        for s in tradfi[:20]:
            print(" ", s["symbol"], s.get("underlyingType"))
        return
    if args.cmd == "ticker":
        c = BinanceExec("x", "x", testnet=args.testnet)
        t = c.ticker(args.symbol)
        m = c.premium_index(args.symbol)
        print(f"{args.symbol}: price={t['lastPrice']} 24hVol={t['quoteVolume']} "
              f"funding={m['lastFundingRate']}")
        return
    if args.cmd == "filters":
        c = BinanceExec("x", "x", testnet=args.testnet)
        print(args.symbol, "->", c.symbol_filters(args.symbol))
        return
    if args.cmd == "dryrun":
        c = BinanceExec.from_env(testnet=args.testnet)
        filt = c.symbol_filters(args.symbol)
        px = float(c.ticker(args.symbol)["lastPrice"])
        qty = c.snap_qty(args.qty, filt["step"], filt["minQty"], int(filt["qtyPrec"]),
                         px, filt["minNotional"])
        print(f"[DRY-RUN] 未发送任何订单")
        print(f"  {args.symbol} mark={px}")
        print(f"  请求 qty={args.qty} → 吸附后 qty={qty}")
        print(f"  该单名义价值 ≈ {qty * px:.2f} USDT (minNotional={filt['minNotional']})")
        print(f"  合约类型: {filt['contractType']}")
        return
    if args.cmd == "balance":
        c = BinanceExec.from_env(testnet=args.testnet)
        for b in c.balance():
            print(f"  {b['asset']}: {b['balance']} (可用 {b['availableBalance']})")
        return


if __name__ == "__main__":
    _cli()
