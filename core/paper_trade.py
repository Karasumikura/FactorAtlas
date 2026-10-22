"""模拟盘（paper trading）引擎：本地自建的可信模拟账户。

为什么自建而不用同花顺客户端：
- 同花顺/券商客户端的"自动化下单"都依赖 Windows GUI 点击（easytrader/ths_trade），
  本服务器是 Linux 无桌面，跑不了。
- 同花顺 SuperMind 的"模拟交易"是网页端策略托管，不能从外部服务器拉取？
  实际可以，见 README 说明——但对外部策略支持有限。
- 因此本模块实现【事件驱动的本地模拟账户】：用真实行情复现 A股交易规则，
  每日按策略信号调仓，记录持仓/成交/净值。这是最可信、最可控的验证方式。

交易规则（尽量贴近 A股真实）：
- T+1：当日买入次日才可卖。
- 涨跌停：涨停不可买、跌停不可卖（用日线近似：收盘触及限幅且封板）。
- 费用：佣金 2.5bps（最低5元）、印花税 5bps（仅卖出）、过户费 0.1bps。
- 停牌：不可交易。
- 100 股整手。
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from core import config
from core.ashare_backtest import limit_price
from factors.engine import factor_series_to_frame

STATE_DIR = config.DATA_DIR / "paper"


@dataclass
class Position:
    code: str
    qty: int = 0
    avg_cost: float = 0.0
    buy_date: str = ""       # 用于 T+1 判断


@dataclass
class Account:
    cash: float = 1_000_000.0
    positions: dict = field(default_factory=dict)   # code -> Position
    nav_history: list = field(default_factory=list)  # [{date, nav, cash, mkt}]
    trades: list = field(default_factory=list)       # 成交流水

    def market_value(self, prices: dict) -> float:
        tot = 0.0
        for c, p in self.positions.items():
            px = prices.get(c, p.avg_cost)
            if px is None or (isinstance(px, float) and px != px):  # NaN 用成本价
                px = p.avg_cost
            tot += p.qty * px
        return tot

    def nav(self, prices: dict) -> float:
        return self.cash + self.market_value(prices)


# A股费用模型
COMMISSION_BPS = 2.5      # 佣金（双边）
COMMISSION_MIN = 5.0      # 最低佣金
STAMP_TAX_BPS = 5.0       # 印花税（仅卖出）
TRANSFER_BPS = 0.1        # 过户费


def _fees(amount: float, is_sell: bool) -> float:
    comm = max(amount * COMMISSION_BPS / 10000.0, COMMISSION_MIN)
    stamp = amount * STAMP_TAX_BPS / 10000.0 if is_sell else 0.0
    transfer = amount * TRANSFER_BPS / 10000.0
    return comm + stamp + transfer


def _limit_blocked(panel: dict, code: str, date) -> tuple[bool, bool]:
    """返回 (涨停封板, 跌停封板)，用于判断能否买入/卖出。"""
    try:
        i = panel["c"].index.get_loc(date)
    except KeyError:
        return False, False
    if i == 0:
        return False, False
    prev = panel["c"].iloc[i - 1][code]
    cur = panel["c"].iloc[i][code]
    hi = panel["h"].iloc[i][code]
    lo = panel["l"].iloc[i][code]
    if pd.isna(prev) or pd.isna(cur) or prev <= 0:
        return False, False
    pct = limit_price(prev, code)
    if pd.isna(pct):
        pct = 0.10
    r = cur / prev - 1.0
    up = (r >= pct - 0.005) and (abs(hi - cur) / cur < 0.002)
    dn = (r <= -pct + 0.005) and (abs(cur - lo) / cur < 0.002)
    return bool(up), bool(dn)


def run_paper(panel: dict, spec: dict, name: str, hold: int = 5,
              top_pct: float = 0.1, start: str | None = None,
              init_cash: float = 1_000_000.0) -> Account:
    """用单因子信号跑一个 A股模拟账户（多空中的多头一侧，A股不可裸空）。

    - 每 hold 日按因子分位选前 top_pct 建多（等权）。
    - T+1、涨跌停、停牌、费用、整手都按 A股规则。
    """
    f = factor_series_to_frame(panel, spec)
    c = panel["c"]
    dates = list(c.index)
    if start:
        dates = [d for d in dates if str(d.date()) >= start]
    if not dates:
        raise ValueError("无有效日期")

    acct = Account(cash=init_cash)
    last_rebal_i = None

    for i, t in enumerate(dates):
        px_row = c.loc[t]
        pos_prices = {code: float(px_row[code]) for code in acct.positions
                      if code in px_row.index and not pd.isna(px_row[code])}
        # 每个交易日先记 NAV
        acct.nav_history.append({
            "date": str(t.date()), "nav": round(acct.nav(pos_prices), 2),
            "cash": round(acct.cash, 2),
            "mkt": round(acct.market_value(pos_prices), 2),
        })

        # 调仓日判断
        if last_rebal_i is not None and (i - last_rebal_i) < hold:
            continue

        fv = f.loc[t].dropna()
        if len(fv) < 10:
            continue
        # 只保留可交易标的
        tradable = [code for code in fv.index if not pd.isna(c.loc[t, code]) and c.loc[t, code] > 0]
        fv = fv.reindex(tradable).dropna()
        if len(fv) < 10:
            continue
        n_top = max(1, int(len(fv) * top_pct))
        targets = list(fv.sort_values(ascending=False).index[:n_top])
        target_set = set(targets)

        # 卖出不在目标里的（T+1：仅卖出非当日买入的）
        for code in list(acct.positions):
            if code in target_set:
                continue
            p = acct.positions[code]
            if p.qty <= 0:
                continue
            if p.buy_date == str(t.date()):   # T+1
                continue
            up, dn = _limit_blocked(panel, code, t)
            if dn:                            # 跌停卖不出
                continue
            try:
                price = float(c.loc[t, code])
            except (KeyError, TypeError, ValueError):
                continue
            if pd.isna(price) or price <= 0:
                continue
            amt = price * p.qty
            fee = _fees(amt, is_sell=True)
            acct.cash += amt - fee
            acct.trades.append({"date": str(t.date()), "code": code, "side": "sell",
                                "qty": p.qty, "price": round(float(price), 3), "fee": round(fee, 2)})
            del acct.positions[code]

        # 买入目标里缺的（等权分配可用现金）
        to_buy = [code for code in targets if code not in acct.positions]
        if to_buy and acct.cash > 0 and not pd.isna(acct.cash):
            per = acct.cash / len(to_buy)
            for code in to_buy:
                try:
                    price = float(c.loc[t, code])
                except (KeyError, TypeError, ValueError):
                    continue
                if pd.isna(price) or price <= 0 or pd.isna(per):
                    continue
                up, dn = _limit_blocked(panel, code, t)
                if up:                        # 涨停买不进
                    continue
                lots = int(per / (price * 100))
                if lots <= 0:
                    continue
                qty = lots * 100
                amt = price * qty
                fee = _fees(amt, is_sell=False)
                if amt + fee > acct.cash:
                    continue
                acct.cash -= amt + fee
                acct.positions[code] = Position(code=code, qty=qty,
                                                avg_cost=float(price), buy_date=str(t.date()))
                acct.trades.append({"date": str(t.date()), "code": code, "side": "buy",
                                    "qty": qty, "price": round(float(price), 3), "fee": round(fee, 2)})
        last_rebal_i = i

    return acct


def summarize(acct: Account) -> dict:
    nav = pd.Series({h["date"]: h["nav"] for h in acct.nav_history})
    ret = nav.pct_change().dropna()
    total = nav.iloc[-1] / nav.iloc[0] - 1 if len(nav) else 0.0
    ann = (1 + total) ** (252 / max(len(nav), 1)) - 1
    sh = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0.0
    peak = nav.cummax()
    dd = ((nav - peak) / peak).min() if len(nav) else 0.0
    return {
        "n_days": len(nav), "total_return": float(total), "annual_return": float(ann),
        "sharpe": float(sh), "max_drawdown": float(dd),
        "final_nav": float(nav.iloc[-1]) if len(nav) else 0.0,
        "n_trades": len(acct.trades),
        "start": nav.index[0] if len(nav) else None,
        "end": nav.index[-1] if len(nav) else None,
    }


def save_state(acct: Account, name: str):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "name": name, "saved_at": datetime.now().isoformat(),
        "cash": acct.cash,
        "positions": {k: asdict(v) for k, v in acct.positions.items()},
        "nav_history": acct.nav_history,
        "trades": acct.trades[-500:],
    }
    (STATE_DIR / f"{name}.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
    return STATE_DIR / f"{name}.json"


if __name__ == "__main__":
    import argparse, warnings
    warnings.filterwarnings("ignore")
    from core import data

    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="000300")
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--top", type=float, default=0.1)
    args = ap.parse_args()

    panel = data.build_panel(market="ashare", index=args.index)
    specs = {
        "vol_n60": {"op": "vol", "params": {"n": 60}, "direction": 1},
        "hl_range_n20": {"op": "hl_range", "params": {"n": 20}, "direction": 1},
        "mom_n120": {"op": "mom", "params": {"n": 120, "skip": 20}, "direction": 1},
    }
    print(f"模拟盘回放 | index={args.index} hold={args.hold} top={args.top:.0%}\n")
    for name, spec in specs.items():
        acct = run_paper(panel, spec, name, hold=args.hold, top_pct=args.top)
        s = summarize(acct)
        print(f"  {name:14s} 总收益={s['total_return']:+.1%} 年化={s['annual_return']:+.1%} "
              f"Sharpe={s['sharpe']:+.2f} 最大回撤={s['max_drawdown']:.1%} "
              f"成交={s['n_trades']}笔 期末净值={s['final_nav']:,.0f}")
        save_state(acct, f"paper_{name}_{args.index}_h{args.hold}")
