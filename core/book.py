"""账本（book）命名空间 —— 让同一台机、同一个 Demo 账户上跑多套独立策略。

动机
====
用户要求：低频因子（mom250/composite）与高频共振因子（resonance）**并行跑两份模拟盘**。
但币安 Demo 账户**只有一套持仓**（BinanceExec 只读一组 BINANCE_API_KEY），
物理上无法开两个账户（除非再注册子账户）。

解法：**单账户 + 代码层隔离**。每套策略是一个「账本(book)」：
  - **共享标的池**：两套策略都可用全部可交易美股永续（不再划分，避免人为缩小机会集）；
  - 各自独立的 **台账 / 决策 / 心跳 / 状态 / 价格历史** 文件（按 book 名分目录）；
  - 各自独立的 **自有持仓账**（book_positions.json，只认自己发过的单）；
  - 各自独立的 **风控计数**（日内调仓、换手、单标的震荡都按 book 分别统计）。

设计原则
========
1. **默认零影响**：不传 book 时，一律走既有全局路径 —— 现有 quant-trader 行为完全不变。
2. **可逆**：只是文件路径 multi-plex，不做破坏性迁移。
3. **显式**：book 名从环境变量 `QUANT_BOOK` 或显式参数来，绝不隐式猜测。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
_BINANCE = PROJ / "data" / "binance"

# 默认账本（现有低频盘）。保持 `None`/空 = 全局路径 = 历史行为。
DEFAULT_BOOK = ""  # 空串表示「默认账本，用全局路径」


def normalize(book: str | None) -> str:
    """把 book 名规范化；空/None → 默认账本（空串）。"""
    if book is None:
        book = os.environ.get("QUANT_BOOK", "")
    book = (book or "").strip()
    # 只允许安全字符，防止路径穿越
    if book and not all(c.isalnum() or c in "-_" for c in book):
        raise ValueError(f"非法 book 名（只允许字母/数字/-/_）：{book!r}")
    return book


def _book_dir(book: str) -> Path:
    """某账本的私有目录；默认账本用 data/binance/ 根目录（历史兼容）。"""
    return _BINANCE if not book else (_BINANCE / "books" / book)


def paths(book: str | None = None) -> dict[str, Path]:
    """返回某账本的全部私有文件路径。

    默认账本（book 为空）返回的就是历史全局路径 → 现有行为 100% 不变。
    """
    b = normalize(book)
    d = _book_dir(b)
    return {
        "dir": d,
        "heartbeat": d / "trader_heartbeat.jsonl",
        "state": d / "trader_state.json",
        "decisions": d / "trader_decisions.jsonl",
        "price_history": d / "price_history.json",
        "ledger": d / "demo_exec_ledger.jsonl",
        "llm_decisions": d / "llm_order_decisions.jsonl",
    }


def ensure(book: str | None = None) -> dict[str, Path]:
    """确保账本目录存在，返回路径字典。"""
    p = paths(book)
    p["dir"].mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 账本自有持仓账（每盘只认自己发出过的单）
# ---------------------------------------------------------------------------
# 语义：单账户跑多盘，交易所里是**合计持仓**。若某盘直接拿合计持仓算 delta，
# 会把别的盘的仓当成自己的 → 在别人仓上加加减减（互踩）。
# 因此每盘维护一份「我认为我自己持有多少」的私有账：
#   - 算 delta 时用【本盘账】，不用交易所合计；
#   - 本盘成交后，把成交量累加进【本盘账】。
# 这样两盘各算各的，互不影响。
def pos_path(book: str | None = None) -> Path:
    return _book_dir(normalize(book)) / "book_positions.json"


def _book_ledger_path(book: str | None = None) -> Path:
    """某盘的成交账（demo_exec_ledger.jsonl）。"""
    return _book_dir(normalize(book)) / "demo_exec_ledger.jsonl"


def load_book_positions(book: str | None = None) -> dict[str, float]:
    """读某盘自有持仓账（{binance_symbol: signed_qty}）。不存在则空。"""
    import json
    p = pos_path(book)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        return {k: float(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def apply_book_fills(fills: list[dict], book: str | None = None) -> dict[str, float]:
    """把本盘本次成交累加进自有持仓账，返回更新后的账。

    fills: [{'symbol': 'XXXUSDT', 'signed_qty': +0.03}, ...]（signed=买正卖负）
    """
    pos = load_book_positions(book)
    for f in fills:
        sym = f.get("symbol")
        q = f.get("signed_qty")
        if not sym or q is None:
            continue
        pos[sym] = float(pos.get(sym, 0.0)) + float(q)
        if abs(pos[sym]) < 1e-12:
            pos.pop(sym, None)
    save_book_positions(pos, book)
    return pos


def _book_avg_price(book: str | None, sym: str, cur: dict[str, float]) -> float | None:
    """从本盘成交台账推当前持仓的移动平均成本（只在交易所查不到该标的时兜底用）。

    比「直接按 qty 加权」严谨的地方：处理**反手**——当一笔成交把旧方向打穿、
    净仓翻到反向时，已实现掉旧仓，成本重置为新开仓价，而不是把多空两边的
    价格混在一起平均（那样算出来的均价毫无意义）。
    """
    p = _book_ledger_path(book)
    if not p.exists():
        return None
    net = 0.0
    avg = 0.0
    try:
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("symbol") != sym:
                continue
            # 兼容带 status 字段的成交账（只认真发出去的）
            if "status" in r and r.get("status") != "sent":
                continue
            q = float(r.get("qty") or r.get("signed_qty") or 0)
            px = float(r.get("ref_price") or r.get("price") or r.get("avg_price") or 0)
            if q <= 0 or px <= 0:
                continue
            side = r.get("side")
            if side:
                signed = q if side == "BUY" else -q
            else:
                signed = float(r.get("signed_qty") or 0)
                q = abs(signed)
            if net == 0 or (net > 0) == (signed > 0):
                avg = (avg * abs(net) + px * q) / (abs(net) + q)
            else:                       # 反手：已实现掉旧仓，成本重置为新开仓价
                if abs(signed) >= abs(net):
                    avg = px
            net += signed
    except Exception:  # noqa: BLE001
        return None
    return avg if abs(net) > 1e-12 else None


def book_unrealized(
    book: str | None,
    cur: dict[str, float],
    prices: dict[str, float] | None = None,
    testnet: bool = True,
) -> tuple[float, str]:
    """算【本盘自有持仓】的浮盈 —— 绝不取账户合计。

    为什么不能取 totalUnrealizedProfit
    ==================================
    单账户多盘（见 book.py 设计说明）：交易所里是**合计持仓**。
    若某盘直接读 `account_summary()["totalUnrealizedProfit"]`，
    就把别的盘、甚至是台账之外的历史老仓的浮盈全算成自己的：

      2026-10-23 实测：全账户 -2.1586，共振盘自有 4 标的只 -0.2533。
      低频盘推送的 -2.1252 实际是全账户数，与共振盘重复计量。

    正确口径：按**本盘台账**（book_positions.json）的持仓，逐个取交易所
    `/fapi/v2/positionRisk` 的 unRealizedProfit 后求和。台账里没有的标的一律不算。
    查不到某标的时的兜底顺序：本盘成交账算均价 → 用 prices 计价。

    Returns:
        (unrealized, source)  source ∈ {"book_slice", "book_slice_fallback"}
    """
    from core.book import load_book_positions
    unrealized = 0.0
    fallback = False
    if not cur:
        return 0.0, "book_slice"
    try:
        from core.binance_exec import BinanceExec as _BE
        _ac = _BE.from_env(testnet=testnet, book=book)   # 按账本选账户
        upnl: dict[str, float] = {}
        for p in _ac.positions():
            # 币安 positionRisk 字段名是大写 R 的 unRealizedProfit
            v = p.get("unRealizedProfit", p.get("unrealizedProfit"))
            upnl[p["symbol"]] = float(v or 0.0)
    except Exception:  # noqa: BLE001
        upnl = {}

    px = prices or {}
    for sym, q in cur.items():
        if abs(float(q)) < 1e-12:
            continue
        if sym in upnl:
            unrealized += upnl[sym]
            continue
        # 交易所查不到该标的 → 用本盘成交均价 + 现价兜底
        fallback = True
        live_px = px.get(sym)
        avg = _book_avg_price(book, sym, cur)
        if live_px and avg:
            unrealized += (float(live_px) - avg) * float(q)
    return unrealized, ("book_slice_fallback" if fallback else "book_slice")


def book_position_side(book: str | None = None, client=None) -> str:
    """某盘固定使用的 positionSide —— 返回**真实生效**的值。

    赛马设计（2026-10-23）
    =====================
    两个策略要独立成立、互不干扰，但单向模式下同一标的是**一根净仓**：
    A 盘想做多、B 盘想做空，谁后下单谁就把对方平掉 —— 实测 CRCLUSDT
    在 10-22 16:22 ~ 10-23 03:25 被两个盘来回 flip 了 8 次。

    解法：切双向持仓模式（Hedge Mode），每个盘固定占一个方向腿，
    各自的下单只碰自己那条腿，永远不平掉别人的仓。

    映射规则（可用环境变量覆盖，例如 QUANT_SIDE_LOWFREQ=SHORT）：
        lowfreq   → LONG
        resonance → SHORT

    单向模式下本函数的值会被 execute_orders 忽略（币安只认 BOTH）。

    记账修复（2026-11-05）
    =====================
    实测该 Demo 账户 ``dualSidePosition = False``（单向模式），交易所把所有
    持仓强制成 ``positionSide=BOTH``。但旧代码无条件把 book_position_side()
    的 "SHORT" 写进台账，导致共振盘 376 笔全记 SHORT、实际全是 BOTH 多头，
    排查时被这个假标签误导了一轮。

    现在：传了 client 就查真实持仓模式——
      · 双向模式 → 返回映射腿（LONG/SHORT），与下单行为一致；
      · 单向模式 → 返回 "BOTH"（交易所真实生效值）。
    不传 client 时保持旧行为（仅映射），保持向后兼容。
    """
    b = normalize(book) or "lowfreq"
    env = os.environ.get(f"QUANT_SIDE_{b.upper()}")
    if env:
        s = env.strip().upper()
        mapped = s if s in ("LONG", "SHORT") else _DEFAULT_SIDE.get(b, "LONG")
    else:
        mapped = _DEFAULT_SIDE.get(b, "LONG")
    if client is not None:
        try:
            if not client.dual_position_mode():
                return "BOTH"
        except Exception:  # noqa: BLE001  查不到就按映射值，不影响下单
            pass
    return mapped


_DEFAULT_SIDE = {
    "lowfreq": "LONG",
    "resonance": "SHORT",
}


def save_book_positions(pos: dict[str, float], book: str | None = None) -> None:
    """写某盘自有持仓账。"""
    import json
    p = pos_path(book)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({k: float(v) for k, v in pos.items() if abs(v) > 1e-12},
                            indent=1, ensure_ascii=False))
