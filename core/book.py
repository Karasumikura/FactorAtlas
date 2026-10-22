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


def save_book_positions(pos: dict[str, float], book: str | None = None) -> None:
    """写某盘自有持仓账。"""
    import json
    p = pos_path(book)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({k: float(v) for k, v in pos.items() if abs(v) > 1e-12},
                            indent=1, ensure_ascii=False))
