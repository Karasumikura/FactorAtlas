"""Binance 美股永续的「纸面跟踪」模拟器（方案 A）。

目的（离线，不需要任何 API key）：
  用正规市场长历史（data/hist，2021-01 起）研究因子，
  在正规开市日生成目标仓位，
  再用「币安永续 K 线」模拟成交，
  最后对账两件事：
    (1) 跟踪误差: 币安永续收益 vs 正规市场收益（同一标的、同一开市日）
    (2) 资金费率成本: 永续持仓每 8h 收取/支付，累加成额外损益
  并回答核心问题：链上美股用来执行，到底和正规市场差多少、贵多少。

交易假设（保守，贴合项目门槛）：
  - 只在正规市场交易日交易（!= 面板 index 之外的日期）。
  - 调仓用「币安当日开盘价」成交（模拟真实下单时点）。
  - 手续费按币安美股永续 taker 约 4bps（双边 8bps）计入。
  - 多空：美股永续可做空，故使用多空组合（LS），与 A股不同。
  - 资金费率：每次调仓期间按实际 fundingRateHistory 累加。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from core import binance_us, us_mapping
from factors.engine import factor_series_to_frame

PROJ = Path(__file__).resolve().parent.parent
CACHE = PROJ / "data" / "binance"


# ---------------------------------------------------------------------------
# 拉取并缓存币安永续历史（日线）
# ---------------------------------------------------------------------------
def fetch_perp_daily(symbol: str, start_ms: int, end_ms: int | None = None,
                     force: bool = False) -> pd.DataFrame:
    """拉币安永续日线并按 UTC 日期存。缓存为 parquet。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_p = CACHE / f"perp_{symbol}_1d.parquet"
    if cache_p.exists() and not force:
        df = pd.read_parquet(cache_p)
        if len(df.index) and df.index.min() <= pd.Timestamp(start_ms, unit="ms"):
            return df

    all_rows: list[list] = []
    cursor = start_ms
    while True:
        batch = binance_us.klines(symbol, interval="1d", limit=1500,
                                  start_ms=cursor, end_ms=end_ms)
        if not batch:
            break
        all_rows.extend(batch)
        last_open = int(batch[-1][0])
        if len(batch) < 1500:
            break
        cursor = last_open + 86_400_000
        if end_ms and cursor >= end_ms:
            break
        time.sleep(0.15)

    if not all_rows:
        return pd.DataFrame()

    recs = []
    for k in all_rows:
        recs.append({
            "date": pd.Timestamp(int(k[0]), unit="ms").normalize(),
            "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
            "c": float(k[4]), "v": float(k[5]),
            "qv": float(k[7]),
        })
    df = pd.DataFrame(recs).drop_duplicates("date").set_index("date").sort_index()
    df.to_parquet(cache_p)
    return df


def fetch_funding(symbol: str, start_ms: int, force: bool = False) -> pd.DataFrame:
    """拉资金费率历史。返回 [fundingTime, rate] 按 UTC 日期聚合求和（当日总费率）。"""
    cache_p = CACHE / f"funding_{symbol}.parquet"
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
        time.sleep(0.15)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["fundingTime"], unit="ms").dt.normalize()
    df["rate"] = df["fundingRate"].astype(float)
    daily = df.groupby("date")["rate"].sum().to_frame()
    daily.to_parquet(cache_p)
    return daily


# ---------------------------------------------------------------------------
# 跟踪误差对账
# ---------------------------------------------------------------------------
@dataclass
class TrackRow:
    us_ticker: str
    binance_symbol: str
    n_days: int
    hit_rate: float            # 日收益方向一致比例
    corr: float                # 日收益相关
    te_daily: float            # 日跟踪误差(std of diff)
    te_ann: float              # 年化跟踪误差
    mean_diff_bps: float       # 日收益平均差(bps) —— 系统性偏差
    perp_ann: float            # 永续年化
    us_ann: float              # 正规年化
    funding_ann_cost: float    # 资金费率年化（多头成本，正=付出）


def _daily_returns(s: pd.Series) -> pd.Series:
    return s.pct_change()


def track_one(m: us_mapping.Mapping, us_close: pd.Series,
              start_ms: int) -> TrackRow | None:
    perp = fetch_perp_daily(m.binance_symbol, start_ms)
    if perp.empty or len(perp) < 20:
        return None
    fund = fetch_funding(m.binance_symbol, start_ms)

    # 对齐到「正规市场交易日」：用 us_close 的 index（只含开市日）
    us = us_close.dropna()
    df = pd.DataFrame({"us": us})
    df = df.join(perp["c"].rename("perp"), how="inner")
    # 只保留正规开市日 & 两者都有
    df = df.dropna()
    if len(df) < 20:
        return None

    r_us = _daily_returns(df["us"])
    r_pp = _daily_returns(df["perp"])
    diff = (r_pp - r_us).dropna()
    common = pd.concat([r_us, r_pp], axis=1).dropna()
    if len(common) < 20 or common.iloc[:, 0].std() == 0:
        return None

    corr = float(common.iloc[:, 0].corr(common.iloc[:, 1]))
    hit = float(((common.iloc[:, 0] > 0) == (common.iloc[:, 1] > 0)).mean())
    te_d = float(diff.std())
    te_ann = te_d * np.sqrt(252)
    mean_diff = float(common.iloc[:, 0].mean() - common.iloc[:, 1].mean())

    def ann(x: pd.Series) -> float:
        # 稳健年化：先算总收益，防溢出
        if len(x) == 0:
            return 0.0
        total = (1.0 + x.replace([np.inf, -np.inf], np.nan).dropna()).prod() - 1.0
        if not np.isfinite(total) or total <= -1.0:
            return -1.0
        n = max(len(x), 1)
        return float(np.clip((1 + total) ** (252 / n) - 1, -1.0, 100.0))

    # 资金费率年化（多头的付出）
    fund_ann = 0.0
    if not fund.empty:
        fj = fund["rate"].reindex(df.index).fillna(0.0)
        fund_ann = float(fj.sum() / max(len(df), 1) * 252)  # 正=多头付出

    return TrackRow(
        us_ticker=m.us_ticker, binance_symbol=m.binance_symbol, n_days=len(df),
        hit_rate=hit, corr=corr, te_daily=te_d, te_ann=te_ann,
        mean_diff_bps=mean_diff * 1e4,
        perp_ann=ann(df["perp"]), us_ann=ann(df["us"]),
        funding_ann_cost=fund_ann,
    )


def run_tracking(top: int = 40, start_date: str = "2026-01-01") -> list[TrackRow]:
    panel_c = pd.read_parquet(PROJ / "data" / "hist" / "hist_c.parquet")
    mapped, _ = us_mapping.build_mapping()
    mapped = mapped[:top]
    start_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    out: list[TrackRow] = []
    for i, m in enumerate(mapped):
        try:
            row = track_one(m, panel_c[m.us_ticker], start_ms)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i+1}/{len(mapped)}] {m.us_ticker} ERR {e}")
            continue
        if row:
            out.append(row)
            print(f"  [{i+1}/{len(mapped)}] {m.us_ticker:8s} n={row.n_days:4d} "
                  f"corr={row.corr:.3f} 命中={row.hit_rate:.2f} "
                  f"TE年化={row.te_ann:.1%} 费率年化={row.funding_ann_cost:+.1%}")
    return out


def summarize_tracking(rows: list[TrackRow]) -> dict:
    if not rows:
        return {}
    df = pd.DataFrame([r.__dict__ for r in rows])
    return {
        "n_symbols": len(df),
        "corr_mean": float(df["corr"].mean()),
        "corr_min": float(df["corr"].min()),
        "hit_rate_mean": float(df["hit_rate"].mean()),
        "te_ann_mean": float(df["te_ann"].mean()),
        "te_ann_median": float(df["te_ann"].median()),
        "mean_diff_bps": float(df["mean_diff_bps"].mean()),
        "funding_ann_mean": float(df["funding_ann_cost"].mean()),
        "funding_ann_p90": float(df["funding_ann_cost"].quantile(0.9)),
    }


def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="币安美股永续 vs 正规市场 跟踪误差对账")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--save", default=str(PROJ / "runs" / "tracking_us_perp.json"))
    args = ap.parse_args()

    print(f"跟踪对账 | top={args.top} start={args.start}\n")
    rows = run_tracking(top=args.top, start_date=args.start)
    s = summarize_tracking(rows)
    print("\n=== 汇总 ===")
    for k, v in s.items():
        print(f"  {k:20s} {v}")

    Path(args.save).write_text(json.dumps(
        {"summary": s, "rows": [r.__dict__ for r in rows]}, indent=1, default=str))
    print(f"\n已存 {args.save}")


if __name__ == "__main__":
    _main()
