"""赛马起算基准（2026-10-23 05:20 CST 起）。

背景
====
两盘在分离账户之前，共用一套持仓、互相平仓、浮盈口径混乱
（低频盘读的是全账户 totalUnrealizedProfit，与共振盘重复计量）。
2026-10-23 完成账户隔离 + 台账重建后，历史浮盈无法干净拆分，
用户决定：**浮盈从此刻起算，之前的不再追究**。

做法
====
每个盘记录一个「起算快照」：
  - baseline_unrealized: 起算瞬间该盘自有持仓的浮盈（作为零轴）
  - wallet:              起算瞬间的钱包余额（作为净值基准）
  - n_positions:         起算瞬间的持仓数（仅留档）

之后展示的「本轮浮盈」= 当前浮盈 − baseline_unrealized，
这样低频盘那个 -2.2171 的历史包袱就不会一直挂在赛马看板上，
但底层的真实持仓成本依然完整保留在 wallet 和台账里，可随时复核。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
BASELINE_FILE = PROJ / "data" / "binance" / "race_baseline.json"


def load() -> dict:
    if BASELINE_FILE.exists():
        try:
            return json.loads(BASELINE_FILE.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save(book: str, baseline_unrealized: float, wallet: float,
         n_positions: int, ts: str | None = None) -> dict:
    """记录某盘的起算基准（幂等：已有则默认不覆盖，需要重设传 force）。"""
    d = load()
    entry = {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "baseline_unrealized": round(float(baseline_unrealized), 6),
        "wallet": round(float(wallet), 6),
        "n_positions": int(n_positions),
    }
    d[book] = entry
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    return entry


def baseline_of(book: str) -> float:
    """某盘的浮盈零轴。没记录过就当 0（不影响真值，只是不偏移）。"""
    e = load().get(book) or {}
    try:
        return float(e.get("baseline_unrealized") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def since_start(book: str, current_unrealized: float) -> float:
    """把真实浮盈换算成「赛马起算以来的浮盈」。"""
    return round(float(current_unrealized) - baseline_of(book), 6)


def info(book: str) -> dict:
    return load().get(book) or {}


def main() -> None:
    """CLI：拍照 / 查看。"""
    import argparse
    ap = argparse.ArgumentParser(description="赛马浮盈起算基准")
    ap.add_argument("cmd", choices=["snap", "show"], nargs="?", default="show")
    ap.add_argument("--force", action="store_true", help="重设已有基准")
    args = ap.parse_args()

    if args.cmd == "show":
        d = load()
        if not d:
            print("尚无基准记录，跑 `snap` 拍照。")
            return
        for bk, e in d.items():
            print(f"{bk:12s} 起算={e['ts'][:19]}  零轴={e['baseline_unrealized']:+.4f}  "
                  f"钱包={e['wallet']:.2f}  持仓={e['n_positions']}")
        return

    from core.book import load_book_positions, book_unrealized
    from core.binance_exec import BinanceExec
    for bk in ("lowfreq", "resonance"):
        if not args.force and bk in load():
            print(f"{bk}: 已有基准，跳过（--force 可重设）")
            continue
        c = BinanceExec.from_env(testnet=True, book=bk)
        a = c.account_summary()
        lg = load_book_positions(bk)
        pnl, _ = book_unrealized(bk, lg, {}, testnet=True)
        e = save(bk, pnl, float(a["totalWalletBalance"]), len(lg))
        print(f"{bk}: 已拍快照 零轴={e['baseline_unrealized']:+.4f} 钱包={e['wallet']:.2f}")


if __name__ == "__main__":
    main()
