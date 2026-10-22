"""资金费率成本模型 —— 把币安美股永续的「持仓成本」接入因子评估。

为什么这是关键：
  A股/美股现货：买入后持有不动 = 0 成本。
  永续合约：持有不动也在每 8h 付/收资金费率 —— 这是「持仓成本」，
            和手续费（调仓时一次性）性质完全不同。

核心建模（区别于用平均费率）：
  - 多空组合的费率 = 多头腿费率 - 空头腿费率（净额）
  - 多头付正费率，空头收正费率 → 净额可能远小于单腿
  - 所以不能用「平均费率」笼统扣除，必须按腿算。
  - 持有期越短，累计费率越低（费率是时间加权，不是换手加权）。

输出：
  1. 每个标的的多头/空头年化费率
  2. LS 组合的真实净费率（按持仓天数）
  3. 剔除费率后的净 Sharpe
  4. 费率套利：哪些标的做空能净收费率
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJ = Path(__file__).resolve().parent.parent
CACHE = PROJ / "data" / "binance"
FUND_DIR = CACHE


@dataclass
class FundingStat:
    symbol: str          # 币安合约，如 NVDAUSDT
    ticker: str          # 正规代码
    n_days: int
    daily_rate: float    # 日均费率（正=多头付）
    ann_long: float      # 做多年化费率成本（正=付出）
    ann_short: float     # 做空年化费率成本（正=付出，恰为 -ann_long；做空时收正费率则该值为负）
    pos_share: float     # 正费率日子占比
    rate_vol: float      # 费率波动（稳定性）


def load_funding(symbol: str) -> pd.Series | None:
    p = FUND_DIR / f"funding_{symbol}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.empty:
        return None
    return df["rate"].sort_index()


def funding_stat(symbol: str, ticker: str) -> FundingStat | None:
    s = load_funding(symbol)
    if s is None or len(s) < 20:
        return None
    daily = float(s.mean())
    return FundingStat(
        symbol=symbol, ticker=ticker, n_days=len(s),
        daily_rate=daily,
        ann_long=daily * 252,        # 做多：付
        ann_short=-daily * 252,      # 做空：收（正=收到）
        pos_share=float((s > 0).mean()),
        rate_vol=float(s.std()),
    )


def all_funding_stats() -> list[FundingStat]:
    from core.us_mapping import build_mapping
    mapped, _ = build_mapping()
    out = []
    for m in mapped:
        st = funding_stat(m.binance_symbol, m.us_ticker)
        if st:
            out.append(st)
    return out


# ---------------------------------------------------------------------------
# LS 组合的净费率
# ---------------------------------------------------------------------------
def ls_net_funding(hold: int, quantile: float = 0.2) -> dict:
    """估算 LS 组合的净资金费率成本（年化）。

    正确逻辑（关键）：
      - 多头腿：做多股票 X → 付 funding(X)  [正费率时付出]
      - 空头腿：做空股票 Y → 收 funding(Y) → 等于付 -funding(Y)
      - 组合净费率成本 = mean_funding(多头腿) + mean_funding(空头腿)
        * 两项都是「以多头费率计的年化正数」，两者相加才是组合的净付出。
        * 只有当空头腿费率为负时，第二项才变负（空头反而收），净成本下降。

    三种情景：
      - neutral：因子与费率无关 → 两腿都取全样本均值
      - worst  ：多头恰好选中最高费率票、空头选中最低费率票（两头都吃亏）
      - best   ：多头选中最低费率票、空头选中最高费率票（两头都占便宜）
    """
    stats = all_funding_stats()
    if not stats:
        return {}
    df = pd.DataFrame([s.__dict__ for s in stats]).drop_duplicates("symbol")
    df = df.sort_values("ann_long", ascending=False).reset_index(drop=True)
    q = max(1, int(len(df) * quantile))

    hi_rows = df.head(q)               # 高费率票（DataFrame）
    lo_rows = df.tail(q)               # 低费率票（DataFrame）
    hi = hi_rows["ann_long"]
    lo = lo_rows["ann_long"]

    f_mean = float(df["ann_long"].mean())
    neutral = f_mean + f_mean                      # 两腿都取均值
    worst = float(hi.mean()) + float(hi.mean())    # 两腿都选中高费率
    best = float(lo.mean()) + float(lo.mean())     # 两腿都选中低费率
    return {
        "n_symbols": int(len(df)),
        "hold_days": hold,
        "avg_funding_ann": f_mean,
        "q90_funding_ann": float(df["ann_long"].quantile(0.9)),
        "net_ann_neutral": neutral,
        "net_ann_worst": worst,
        "net_ann_best": best,
        "per_period_cost_neutral": neutral * hold / 252,
        "per_period_cost_worst": worst * hold / 252,
        "high_funding_tickers": list(hi_rows["ticker"]),
        "low_funding_tickers": list(lo_rows["ticker"]),
    }


# ---------------------------------------------------------------------------
# 剔除费率后的净收益
# ---------------------------------------------------------------------------
def net_returns_with_funding(gross_ret: pd.Series, hold: int,
                             net_fund_ann: float) -> pd.Series:
    """把年化净费率摊到日收益上，返回净收益序列。"""
    if gross_ret.empty:
        return gross_ret
    daily_cost = net_fund_ann / 252.0
    return gross_ret - daily_cost


def _sharpe(r: pd.Series) -> float:
    r = r.dropna()
    if len(r) < 20 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * np.sqrt(252))


def _ann(r: pd.Series) -> float:
    r = r.dropna()
    if not len(r):
        return 0.0
    tot = (1 + r).prod() - 1
    return float(np.clip((1 + tot) ** (252 / len(r)) - 1, -1, 100))


# ---------------------------------------------------------------------------
# 费率套利：做空高正费率标的
# ---------------------------------------------------------------------------
def funding_carry_screen(top: int = 20) -> pd.DataFrame:
    """做空高正费率标的 → 净收费率。按年化收益排序。"""
    stats = all_funding_stats()
    if not stats:
        return pd.DataFrame()
    df = pd.DataFrame([s.__dict__ for s in stats]).drop_duplicates("symbol")
    # 做空收益 = ann_short（正=收到）
    df["short_carry_ann"] = df["ann_short"]
    df["stability"] = df["pos_share"]  # 正费率占比越高越稳定
    out = df.sort_values("short_carry_ann", ascending=False)
    return out[["ticker", "symbol", "n_days", "ann_long", "ann_short",
                "short_carry_ann", "pos_share", "rate_vol"]].head(top)


def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="资金费率成本模型")
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--quantile", type=float, default=0.2)
    ap.add_argument("--save", default=str(PROJ / "runs" / "funding_cost.json"))
    args = ap.parse_args()

    stats = all_funding_stats()
    print(f"=== 费率统计（{len(stats)} 个标的）===")
    if not stats:
        print("  无费率缓存。先跑 core.us_paper 拉数据。")
        return

    # 1. 单腿费率
    df = pd.DataFrame([s.__dict__ for s in stats]).sort_values("ann_long", ascending=False)
    print(f"\n{'ticker':8s} {'合约':14s} {'做多年费':>9s} {'做空年收':>9s} {'正费率占比':>10s}")
    print("-" * 58)
    for _, r in df.iterrows():
        print(f"{r['ticker']:8s} {r['symbol']:14s} {r['ann_long']:>+9.2%} "
              f"{r['ann_short']:>+9.2%} {r['pos_share']:>10.1%}")

    # 2. LS 净费率
    print(f"\n=== LS 组合净费率（hold={args.hold}）===")
    net = ls_net_funding(args.hold, args.quantile)
    for k, v in net.items():
        print(f"  {k:26s} {v:+.4f}" if isinstance(v, float) else f"  {k:26s} {v}")

    # 3. 套利
    print(f"\n=== 费率套利：做空高正费率标的（净收）===")
    car = funding_carry_screen(15)
    if not car.empty:
        print(f"{'ticker':8s} {'年收':>9s} {'正费率占比':>10s} {'费率波动':>10s}")
        print("-" * 42)
        for _, r in car.iterrows():
            print(f"{r['ticker']:8s} {r['short_carry_ann']:>+9.2%} "
                  f"{r['pos_share']:>10.1%} {r['rate_vol']:>10.4%}")

    Path(args.save).write_text(json.dumps({
        "stats": [s.__dict__ for s in stats],
        "ls_net": net,
        "carry": car.to_dict("records") if not car.empty else [],
    }, indent=1, default=str))
    print(f"\n已存 {args.save}")


if __name__ == "__main__":
    _main()
