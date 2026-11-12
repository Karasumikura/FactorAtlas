"""Demo 实盘对账：把币安 Demo 的真实成交，和我们回测的假设逐笔比对。

回答三个问题（这才是"链上美股值不值得做"的最终检验）：
  1. 滑点：Demo 成交均价 vs 下单时的参考价（正规市场口径），差多少 bps？
  2. 资金费率：持仓期间实际累计收/付了多少？（这是 A股没有的成本）
  3. 跟踪：Demo 持仓的净值 vs 同权重用正规收盘价算的理论净值，偏离多少？

数据来源：
  - 币安 Demo：userTrades（成交）、positionRisk（持仓）、income（资金费/手续费）
  - 正规市场：data/hist 收盘价（研究口径）

无需 key 也能跑 --mode replay（用本地成交记录文件），方便先验证逻辑。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJ = Path(__file__).resolve().parent.parent
LEDGER = PROJ / "data" / "binance" / "demo_exec_ledger.jsonl"

# 当前对账的账本名。赛马隔离后两盘各用不同币安账户，
# 对账必须读**本盘自己的账户**，否则会拿 A 盘的流水去对 B 盘的仓。
_BOOK: str = ""


def set_book(book: str | None) -> None:
    """账本隔离：重绑成交台账路径 + 记住账本名（供拉取时选账户）。"""
    global LEDGER, _BOOK
    from core.book import paths, normalize
    _BOOK = normalize(book)
    LEDGER = paths(_BOOK)["ledger"]


def get_book() -> str:
    return _BOOK


# ---------------------------------------------------------------------------
# 成交台账（本地记录，每次下单后追加）
# ---------------------------------------------------------------------------
def append_ledger(records: list[dict]) -> None:
    """追加成交台账。带独占文件锁，避免多盘/巡检并发写入交错成损坏行。"""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001
            pass
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        f.flush()


def load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# 从币安 Demo 拉实际成交 / 资金费
# ---------------------------------------------------------------------------
def fetch_demo_trades(symbol: str, testnet: bool = True,
                      book: str | None = None) -> list[dict]:
    """拉本盘账户的成交明细。

    book 不传时用 set_book() 记住的账本 → 自动选对账户。
    """
    from core.binance_exec import BinanceExec
    c = BinanceExec.from_env(testnet=testnet, book=book or _BOOK or None)
    return c.user_trades(symbol, limit=1000)


def fetch_demo_income(symbol: str | None = None, testnet: bool = True,
                      book: str | None = None) -> list[dict]:
    """资金费 / 手续费 / 实现盈亏流水（/fapi/v1/income）—— 读本盘自己的账户。"""
    from core.binance_exec import BinanceExec
    c = BinanceExec.from_env(testnet=testnet, book=book or _BOOK or None)
    params = {"limit": 1000}
    if symbol:
        params["symbol"] = symbol
    return c.signed_get("/fapi/v1/income", **params)


# ---------------------------------------------------------------------------
# 滑点对账
# ---------------------------------------------------------------------------
@dataclass
class SlippageRow:
    symbol: str
    side: str
    qty: float
    ref_price: float        # 下单时的参考价（正规收盘 or 下单前 mark）
    fill_price: float       # 实际成交均价
    slippage_bps: float     # (fill - ref)/ref * 1e4，买正=吃亏，卖负=吃亏
    notional: float


def compute_slippage(trades: list[dict], ref_prices: dict[str, float]) -> list[SlippageRow]:
    """trades: 币安 userTrades 格式（含 symbol/side/price/qty/realizedPnl...）。
    按 symbol 聚合成交均价，和 ref_price 比。
    """
    by_sym: dict[str, list[dict]] = {}
    for t in trades:
        by_sym.setdefault(t["symbol"], []).append(t)

    rows: list[SlippageRow] = []
    for sym, ts in by_sym.items():
        ref = ref_prices.get(sym)
        if not ref or ref <= 0:
            continue
        # 加权成交均价
        tot_qty = sum(float(t["qty"]) for t in ts)
        if tot_qty <= 0:
            continue
        vwap = sum(float(t["price"]) * float(t["qty"]) for t in ts) / tot_qty
        side = ts[0].get("side", "")
        # 买单：成交价高于参考价 = 吃亏（正 bps 表示不利）
        sign = 1.0 if side == "BUY" else -1.0
        slip = (vwap - ref) / ref * 1e4 * sign
        rows.append(SlippageRow(
            symbol=sym, side=side, qty=tot_qty, ref_price=ref,
            fill_price=vwap, slippage_bps=slip, notional=tot_qty * vwap,
        ))
    return rows


# ---------------------------------------------------------------------------
# 资金费率对账
# ---------------------------------------------------------------------------
@dataclass
class FundingRow:
    symbol: str
    funding_sum: float      # 累计资金费（正=收到，负=付出）
    commission_sum: float   # 累计手续费
    realized_pnl: float
    n_records: int


def compute_funding_income(income: list[dict], symbols: list[str] | None = None) -> list[FundingRow]:
    """income: /fapi/v1/income 列表。incomeType: FUNDING_FEE / COMMISSION / REALIZED_PNL。"""
    by_sym: dict[str, dict] = {}
    for r in income:
        sym = r.get("symbol", "")
        if symbols and sym not in symbols:
            continue
        d = by_sym.setdefault(sym, {"FUNDING_FEE": 0.0, "COMMISSION": 0.0,
                                    "REALIZED_PNL": 0.0, "n": 0})
        it = r.get("incomeType", "")
        amt = float(r.get("income", 0))
        if it in d:
            d[it] += amt
        d["n"] += 1
    out: list[FundingRow] = []
    for sym, d in by_sym.items():
        out.append(FundingRow(
            symbol=sym, funding_sum=d["FUNDING_FEE"], commission_sum=d["COMMISSION"],
            realized_pnl=d["REALIZED_PNL"], n_records=d["n"],
        ))
    out.sort(key=lambda x: x.funding_sum)
    return out


# ---------------------------------------------------------------------------
# 跟踪误差（Demo 净值 vs 正规收盘理论净值）
# ---------------------------------------------------------------------------
def tracking_vs_regular(positions: dict[str, float],
                        binance_base_map: dict[str, str],
                        start_date: str, end_date: str) -> dict:
    """positions: {binance_symbol: qty}（带符号）。
    binance_base_map: {binance_symbol: us_ticker}。
    用正规 hist 收盘价算理论净值变化。
    """
    panel = pd.read_parquet(PROJ / "data" / "hist" / "hist_c.parquet")
    idx = panel.index
    idx = idx[(idx >= pd.Timestamp(start_date)) & (idx <= pd.Timestamp(end_date))]
    if len(idx) < 2:
        return {"error": "区间内无正规交易日"}

    rows = []
    for sym, qty in positions.items():
        tk = binance_base_map.get(sym)
        if not tk or tk not in panel.columns:
            continue
        s = panel[tk].loc[idx].dropna()
        if len(s) < 2:
            continue
        pnl = float(qty) * (s.iloc[-1] - s.iloc[0])
        rows.append({"symbol": sym, "ticker": tk, "qty": qty,
                     "px_start": float(s.iloc[0]), "px_end": float(s.iloc[-1]),
                     "pnl_regular": pnl})
    return {"n_days": len(idx),
            "start": str(idx[0].date()), "end": str(idx[-1].date()),
            "positions": rows,
            "theoretical_pnl": sum(r["pnl_regular"] for r in rows)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _summary(slippage: list[SlippageRow], funding: list[FundingRow],
             theo: dict) -> dict:
    s = {}
    if slippage:
        bps = [r.slippage_bps for r in slippage]
        s["slippage_mean_bps"] = float(np.mean(bps))
        s["slippage_median_bps"] = float(np.median(bps))
        s["slippage_worst_bps"] = float(np.min(bps))
        s["slippage_n"] = len(slippage)
    if funding:
        s["funding_total"] = float(sum(r.funding_sum for r in funding))
        s["commission_total"] = float(sum(r.commission_sum for r in funding))
        s["realized_pnl_total"] = float(sum(r.realized_pnl for r in funding))
    if theo and "theoretical_pnl" in theo:
        s["theoretical_pnl_regular"] = theo["theoretical_pnl"]
    return s


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Demo 实盘对账")
    ap.add_argument("--symbols", default="", help="逗号分隔；空=全部")
    ap.add_argument("--book", default="", choices=["", "lowfreq", "resonance"],
                    help="账本名；赛马隔离后各盘账户不同，必须指定（空=老账户）")
    ap.add_argument("--testnet", action="store_true", default=True)
    ap.add_argument("--mainnet", dest="testnet", action="store_false")
    ap.add_argument("--save", default=str(PROJ / "runs" / "demo_reconciliation.json"))
    args = ap.parse_args()

    if args.book:
        set_book(args.book)
        # 默认输出按账本分开存，避免两盘互相覆盖
        if args.save == str(PROJ / "runs" / "demo_reconciliation.json"):
            args.save = str(PROJ / "runs" / f"demo_reconciliation_{args.book}.json")

    syms = [x.strip() for x in args.symbols.split(",") if x.strip()]
    try:
        trades = []
        for s in syms or [""]:
            trades += fetch_demo_trades(s, args.testnet) if s else []
        income = fetch_demo_income(syms[0] if len(syms) == 1 else None, args.testnet)
    except Exception as e:  # noqa: BLE001
        print(f"拉取 Demo 数据失败（可能未配 key）：{e}")
        print("先用 --mode replay 或补 Demo key 再跑。")
        return

    print(f"账本: {args.book or '(默认/老账户)'}")
    print(f"成交 {len(trades)} 笔，流水 {len(income)} 条")
    funding = compute_funding_income(income)
    print(f"\n=== 资金费 / 手续费（{len(funding)} 个标的）===")
    print(f"{'symbol':14s} {'资金费':>12s} {'手续费':>12s} {'实现盈亏':>12s}")
    print("-" * 54)
    for r in funding[:25]:
        print(f"{r.symbol:14s} {r.funding_sum:>+12.4f} {r.commission_sum:>+12.4f} "
              f"{r.realized_pnl:>+12.4f}")

    s = _summary([], funding, {})
    print(f"\n=== 汇总 ===")
    for k, v in s.items():
        print(f"  {k:24s} {v:+.4f}")

    Path(args.save).write_text(json.dumps(
        {"funding": [r.__dict__ for r in funding], "summary": s},
        indent=1, default=str))
    print(f"\n已存 {args.save}")


if __name__ == "__main__":
    main()
