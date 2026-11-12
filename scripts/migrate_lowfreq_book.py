#!/usr/bin/env python3
"""低频盘账本迁移：把「交易所持仓 − 共振盘台账」的结果固化为 lowfreq 自有账。

背景
====
quant-trader.service 启动时**没有传 --book**，于是 _BOOK="" → 走默认账本的
历史全局路径：
  - 读持仓：load_positions_from_exchange()（全交易所合计，含共振盘的仓）
  - 回填台账：`if _bk and not dry` —— _bk 为空串，条件不成立，一笔都没记
  → books/lowfreq/ 从来就没有 book_positions.json，低频盘是个「无账盘」。

后果（2026-10-23 实测）：
  - 共振盘台账经 184 笔成交逐笔核对是准确的（SPY .01/CRCL .17/QQQ .01/COIN .01）
  - 交易所 CRCLUSDT 只有 0.07，共振盘却记 0.17 → 低频盘在共振盘的仓上做减仓
  - 两盘互相踩：CRCL 在 10-22 16:22~10-23 03:25 之间被反复 flip 8 次
  - 浮盈口径：低频盘读 totalUnrealizedProfit（全账户），与共振盘重复计量

迁移只做一件事：把倒推出的低频盘真实持仓写成书籍初始账。
不改任何仓位、不下任何单，纯文件写入。

用法：
    python3 scripts/migrate_lowfreq_book.py            # 预览
    python3 scripts/migrate_lowfreq_book.py --apply    # 落盘
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))


def snapshot() -> tuple[dict[str, float], dict[str, float]]:
    from core.binance_exec import BinanceExec
    from core.book import load_book_positions
    testnet = os.getenv("BINANCE_TESTNET", "1") == "1"
    c = BinanceExec.from_env(testnet=testnet)
    ex: dict[str, float] = {}
    for p in c.positions():
        q = float(p.get("positionAmt") or 0)
        if abs(q) > 1e-12:
            ex[p["symbol"]] = q
    return ex, load_book_positions("resonance")


def derive() -> dict[str, float]:
    """低频盘真实持仓 = 交易所合计 − 共振盘自有台账（共振盘台账已验证准确）。"""
    ex, res = snapshot()
    low: dict[str, float] = {}
    for sym in set(ex) | set(res):
        d = round(ex.get(sym, 0.0) - res.get(sym, 0.0), 10)
        if abs(d) > 1e-9:
            low[sym] = d
    return low


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    args = ap.parse_args()

    ex, res = snapshot()
    low = derive()

    print("交易所合计:")
    for s, v in sorted(ex.items()):
        print(f"   {s:12s} {v:+.4f}")
    print("\n共振盘台账（已验证 = 其 184 笔成交累计）:")
    for s, v in sorted(res.items()):
        print(f"   {s:12s} {v:+.4f}")
    print("\n→ 低频盘应有持仓（= 交易所 − 共振盘）:")
    for s, v in sorted(low.items()):
        print(f"   {s:12s} {v:+.4f}")
    print(f"\n重叠标的（历史上被互相踩过）: {sorted(set(low) & set(res)) or '无'}")

    target = PROJ / "data" / "binance" / "books" / "lowfreq" / "book_positions.json"
    if target.exists():
        print(f"\n⚠️ {target} 已存在，内容:")
        print("   " + target.read_text())
        print("   为避免覆盖真实账，本次不写入。如需重建请先手工移走该文件。")
        return

    if not args.apply:
        print(f"\n[预览] 将写入 {target}（加 --apply 生效）")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({k: float(v) for k, v in low.items()},
                                 indent=1, ensure_ascii=False))
    print(f"\n✅ 已写入 {target}")
    print("   内容: " + target.read_text())


if __name__ == "__main__":
    main()
