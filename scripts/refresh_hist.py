#!/usr/bin/env python3
"""美股日线面板（data/hist）**增量**刷新。

背景（2026-10-21 巡检发现）
==========================
data/hist/hist_*.parquet 停在 2026-10-13，而当天已是 2026-10-20。
后果是 core.bridge_us_binance.compute_targets() 里
`valid = f.index[f.index <= t]; t_use = valid[-1]` 永远取到 10-13 那一行的
因子值 —— 交易端连续 8 天用同一份「旧信号」算目标持仓，heartbeat 里
`signal_date` 一直显示 2026-10-13。这是**静默故障**：服务全绿、无异常，
但信号实质停滞。

为什么之前没人刷新
==================
config.ALPACA_FEED 现在是 'sip'，而当前 Alpaca key 的订阅
**不允许查近期 SIP 数据**（HTTP 403: subscription does not permit querying
recent SIP data）。所以任何「用配置里的 feed 增量拉」的尝试都会 403。
但历史数据本身是用 'iex' feed 建的（data/hist/meta.json: feed=iex），
且实测 **iex 与 sip 在历史日期上完全一致**（AAPL/MSFT/TSLA/NVDA/AAOI/AAL
在 10-09/10-12/10-13 三天 diff=0.000%）。因此增量刷新用 iex 既能成功，
又不会引入口径不一致。

安全设计（绝不重演 2026-10-18「空面板覆盖历史」事故）
====================================================
1. 只取 **末日之后** 的新交易日，拼接后去重（新值覆盖旧值）。
2. 落盘前多重校验：非空、列数不低于原面板的 95%、行数不超过 30 行增量。
   任一不满足 → 拒绝写盘，保留原文件。
3. 写临时文件 + os.replace 原子替换，中途失败不留半截数据。
4. 幂等：没有新交易日时直接退出（exit 0），不改动任何文件。
5. feed 依次尝试 [meta.feed, config.ALPACA_FEED, 'iex', 'sip']，
   第一个全批次成功的为准；全部失败则报错退出（非零）。

用法
====
    venv/bin/python scripts/refresh_hist.py            # 增量刷新
    venv/bin/python scripts/refresh_hist.py --dry      # 只探测不写盘
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

import pandas as pd  # noqa: E402

from core import config  # noqa: E402
from data.history import fetch_bars_paged  # noqa: E402

FIELDS = ["o", "h", "l", "c", "v", "vw"]
HIST_DIR = config.DATA_DIR / "hist"
META = HIST_DIR / "meta.json"

# 单次增量上限：超过这个行数说明窗口算错了，宁可拒绝写盘
MAX_NEW_DAYS = 30


def _log(msg: str) -> None:
    print(f"[refresh_hist] {msg}", flush=True)


def _load_existing() -> dict[str, pd.DataFrame] | None:
    out = {}
    for k in FIELDS:
        p = HIST_DIR / f"hist_{k}.parquet"
        if not p.exists():
            return None
        out[k] = pd.read_parquet(p)
    return out


def _candidate_feeds() -> list[str]:
    """feed 尝试顺序：面板自身记录的 feed 优先，其次配置，最后是全部已知 feed。"""
    seq: list[str] = []
    try:
        if META.exists():
            f = json.loads(META.read_text()).get("feed")
            if f:
                seq.append(f)
    except Exception:  # noqa: BLE001
        pass
    seq.append(config.ALPACA_FEED)
    seq += ["iex", "sip"]
    out = []
    for f in seq:
        if f and f not in out:
            out.append(f)
    return out


def _fetch_new(symbols: list[str], start: str, end: str) -> tuple[dict, str]:
    """依次尝试各 feed，返回 (bars, 成功的 feed)。全部失败则抛异常。"""
    errs = []
    for feed in _candidate_feeds():
        try:
            bars = fetch_bars_paged(symbols, start, end, feed=feed)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{feed}: {str(e)[:120]}")
            continue
        if bars:
            return bars, feed
        errs.append(f"{feed}: 返回空")
    raise RuntimeError("全部 feed 均失败 -> " + " | ".join(errs))


def merge_new(existing: dict[str, pd.DataFrame], new: dict[str, pd.DataFrame],
               max_new_days: int = MAX_NEW_DAYS) -> dict[str, pd.DataFrame]:
    """把增量帧安全地拼到老面板上；任何异常都抛 ValueError（调用方拒绝写盘）。

    校验项（对应 2026-10-18「空面板覆盖 1451×655 历史」事故）：
      * 增量天数必须 > 0 且 <= max_new_days
      * 新数据的标的覆盖率 >= 95%（防止拿到一份只有几只票的残缺结果）
      * 合并后总天数与「老天数 + 新增天数」一致（允许 2 天容差，跨时区取整）
    """
    c_old = existing.get("c")
    if c_old is None or not len(c_old.index):
        raise ValueError("老面板为空，拒绝合并")

    c_new = new.get("c")
    n_new = len(c_new.index) if c_new is not None and len(c_new.index) else 0
    if n_new == 0:
        raise ValueError("无新交易日")
    if n_new > max_new_days:
        raise ValueError(f"新交易日 {n_new} 天 > 上限 {max_new_days}，疑似窗口错误")

    old_cols = set(c_old.columns)
    new_cols = set(c_new.columns) if c_new is not None else set()
    overlap = len(old_cols & new_cols) / max(1, len(old_cols))
    if overlap < 0.95:
        raise ValueError(f"新数据标的覆盖率仅 {overlap:.1%} < 95%，疑似异常")

    merged: dict[str, pd.DataFrame] = {}
    for k in FIELDS:
        old_df = existing[k]
        new_df = new.get(k)
        if new_df is None or not len(new_df.index):
            merged[k] = old_df
            continue
        new_df = new_df[[col for col in new_df.columns if col in old_cols]]
        if not len(new_df.columns):
            merged[k] = old_df
            continue
        m = pd.concat([old_df, new_df])
        m = m[~m.index.duplicated(keep="last")].sort_index()
        merged[k] = m

    n_days = len(merged["c"].index)
    expect = len(c_old.index) + n_new
    if n_days < expect or n_days > expect + 2:
        raise ValueError(f"合并后天数 {n_days} 与预期 {expect} 不符")
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只探测新数据，不写盘")
    args = ap.parse_args()

    cur = _load_existing()
    if cur is None:
        _log("❌ data/hist 面板不完整，请先跑 scripts/pull_history.py")
        return 2

    c = cur["c"]
    if not len(c.index):
        _log("❌ 面板为空，拒绝操作")
        return 2
    last_day = pd.Timestamp(c.index.max()).date()
    today = pd.Timestamp.utcnow().date()
    if last_day >= today:
        _log(f"已是最新（末日 {last_day}），无需刷新")
        return 0

    start = (last_day + pd.Timedelta(days=1)).isoformat()
    end = today.isoformat()
    symbols = list(c.columns)
    _log(f"面板末日 {last_day}，尝试增量拉取 {len(symbols)} 只票 {start}..{end}")

    bars, feed = _fetch_new(symbols, start, end)
    _log(f"收到 {len(bars)} 只票的数据（feed={feed}）")

    from core.data import _bars_to_frames  # noqa: E402  与历史落盘口径完全一致

    new = _bars_to_frames(bars)
    try:
        merged = merge_new(cur, new)
    except ValueError as e:
        # 「无新交易日」是正常情况（未收盘/假期），不算失败
        if str(e).startswith("无新交易日"):
            _log("无新交易日（可能未到收盘/假期），不改动面板")
            return 0
        _log(f"❌ {e}，拒绝写盘（原面板保持不变）")
        return 2

    n_new = len(new["c"].index)
    n_days = len(merged["c"].index)
    n_syms = len(merged["c"].columns)
    _log(f"合并后面板: {n_days} 交易日 x {n_syms} 只票 "
         f"（新增 {n_new} 天："
         f"{', '.join(str(d.date()) for d in new['c'].index)}）")

    if args.dry:
        _log("--dry 模式，不写盘")
        return 0

    # ── 原子落盘 ──
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    for k in FIELDS:
        tmp = HIST_DIR / f".hist_{k}.parquet.tmp"
        merged[k].to_parquet(tmp)
        os.replace(tmp, HIST_DIR / f"hist_{k}.parquet")

    META.write_text(json.dumps({
        "n_symbols": int(n_syms), "n_days": int(n_days),
        "start": str(merged["c"].index.min().date()),
        "end": str(merged["c"].index.max().date()),
        "feed": feed,
        "incremental_feed": feed,
    }, indent=2))
    _log(f"✅ 已落盘，末日 -> {merged['c'].index.max().date()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
