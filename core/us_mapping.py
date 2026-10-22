"""币安美股永续 <-> 正规美股面板 的标的映射 + 开市日历。

核心原则（用户与项目共识）：
  - 因子研究用「正规市场长历史」（data/hist，2021-01 起，655 只）。
  - 币安永续只做「执行层」。
  - 只在正规市场开市时段交易；休市不交易 → 天然回避 7x24 永续的休市 EWMA 偏移风险。

映射规则：
  - 币安 base 与美股 ticker 归一化后匹配（去 -、. 并大写）。
  - 例如 BRK.B -> BRKB, 币安 BRKBUSDT -> BRKB。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
HIST_C = PROJ / "data" / "hist" / "hist_c.parquet"
SCREEN = PROJ / "runs" / "binance_tradfi_screen.json"


def _norm(t: str) -> str:
    return t.upper().replace("-", "").replace(".", "").strip()


@dataclass
class Mapping:
    binance_base: str        # 币安 base，如 NVDA
    binance_symbol: str      # 币安合约，如 NVDAUSDT
    us_ticker: str           # 正规面板列名，如 NVDA
    underlying_type: str
    quote_volume_24h: float
    onboard_date: str


def load_us_tickers() -> set[str]:
    import pandas as pd
    df = pd.read_parquet(HIST_C)
    return set(df.columns)


def build_mapping(screen_path: Path = SCREEN, binance_base_set: set[str] | None = None,
                  fetch_live: bool = False, testnet: bool = False) -> tuple[list[Mapping], dict]:
    """返回 (可映射列表, 诊断信息)。

    binance_base_set: 若给出，则只映射这些币安 base（用于按环境/白名单过滤）。
    fetch_live: 为 True 时实时拉取币安 exchangeInfo（可按 testnet 切环境），
                而不是读本地快照。**下单桥必须用 fetch_live=True**，
                因为 demo 与主网的标的集合不同（demo 只有约 24 个美股）。
    """
    from core import binance_us

    us_tickers = load_us_tickers()
    us_norm = {_norm(t): t for t in us_tickers}

    # 标的来源：实时拉取（可按环境）或本地快照
    if fetch_live:
        base_url = binance_us.FAPI_DEMO if testnet else binance_us.FAPI_MAIN
        rows = []
        for s in binance_us.list_tradfi_symbols(base=base_url):
            if s.underlying_type not in {"EQUITY", "PREMARKET"}:
                continue
            rows.append({"base": s.base, "underlying_type": s.underlying_type,
                         "quote_volume_24h": 0.0, "onboard_date": s.onboard_date})
    else:
        if not screen_path.exists():
            return [], {"error": f"{screen_path} 不存在，先跑 core.binance_us --save"}
        rows = json.loads(screen_path.read_text())

    mapped: list[Mapping] = []
    unmatched: list[dict] = []
    for r in rows:
        base = r["base"]
        if r.get("underlying_type") not in {"EQUITY", "PREMARKET"}:
            continue  # 只映射美股个股/ETF；港股韩股商品另议
        if binance_base_set is not None and base not in binance_base_set:
            continue
        key = _norm(base)
        us = us_norm.get(key)
        if us is None:
            unmatched.append({"base": base, "type": r["underlying_type"]})
            continue
        mapped.append(
            Mapping(
                binance_base=base,
                binance_symbol=f"{base}USDT",
                us_ticker=us,
                underlying_type=r["underlying_type"],
                quote_volume_24h=float(r.get("quote_volume_24h", 0)),
                onboard_date=r.get("onboard_date", "?"),
            )
        )
    mapped.sort(key=lambda m: -m.quote_volume_24h)
    diag = {
        "source": "live" + ("-testnet" if testnet else "-mainnet") if fetch_live else "snapshot",
        "us_tickers_in_panel": len(us_tickers),
        "binance_us_equity_perps": len(rows),
        "mapped": len(mapped),
        "unmatched_binance": len(unmatched),
        "unmatched_examples": unmatched[:15],
    }
    return mapped, diag


# ---------------------------------------------------------------------------
# 开市日历
# ---------------------------------------------------------------------------
def us_trading_days() -> list:
    """正规市场交易日 = 长历史面板的 index（面板只含交易日）。

    注意：这是【历史】交易日，数据会滞后（如面板最新只到 N-2 天）。
    判断"今天是否开市"不要用它 —— 用 is_market_open()。
    """
    import pandas as pd
    return list(pd.read_parquet(HIST_C).index)


# 美股主要假日（固定月日，简化处理；浮动假日按年维护）
_US_FIXED_HOLIDAYS = {
    (1, 1),    # New Year
    (7, 4),    # Independence Day
    (12, 25),  # Christmas
    (12, 24),  # Christmas Eve (半日/休)
    (11, 11),  # Veterans Day (债市休, 股市开; 保守起见不排除)
}
_US_FIXED_HOLIDAYS.discard((11, 11))


def is_market_open(date=None) -> bool:
    """今天是否美股交易日（不依赖滞后的历史面板）。

    规则：周一~周五，且不在已知假日集合里。
    为什么不用 panel index：面板数据滞后（最新交易日可能落后 1-2 天），
    用它判断"今天"会把正常交易日误判为休市 → 主脑永久罢工。
    """
    import pandas as pd
    from datetime import date as _date, datetime as _dt
    if date is None:
        date = _dt.now().date()
    try:
        ts = pd.Timestamp(date)
    except Exception:  # noqa: BLE001
        return False
    # 周末
    if ts.weekday() >= 5:
        return False
    # 固定假日
    if (ts.month, ts.day) in _US_FIXED_HOLIDAYS:
        return False
    # 浮动假日（感恩节=11月第4个周四；劳动节=9月第1个周一；阵亡将士=5月最后周一；马丁路德金=1月第3个周一；总统日=2月第3个周一）
    try:
        if ts.month == 11 and ts.weekday() == 3:
            # 11 月第 4 个周四 = 感恩节（day 在 22~28 之间）
            if 22 <= ts.day <= 28:
                return False
        if ts.month == 9 and ts.weekday() == 0 and ts.day <= 7:
            return False  # 劳动节
        if ts.month == 5 and ts.weekday() == 0 and ts.day > 24:
            return False  # 阵亡将士纪念日
        if ts.month == 1 and ts.weekday() == 0 and 15 <= ts.day <= 21:
            return False  # 马丁路德金日
        if ts.month == 2 and ts.weekday() == 0 and 15 <= ts.day <= 21:
            return False  # 总统日
        if ts.month == 4 and ts.weekday() == 4 and ts.day <= 7:
            return False  # 耶稣受难日（近似：4月第一个周五）
    except Exception:  # noqa: BLE001
        pass
    return True


def live_quote_symbols(testnet: bool = False) -> set[str]:
    """返回目标环境中【有真实报价】的 symbol 集合。

    为什么需要：币安（尤其 Demo）存在"僵尸标的"——exchangeInfo 里挂着符号，
    但没有行情/无流动性（如 BMNRUSDT: lastPrice=None, volume=None）。
    只用 exchangeInfo 会选到这些标的 → 下单时报错或无法成交。
    这里用一次批量 /fapi/v1/ticker/price 拉全部有价 symbol，开销极小。
    """
    from core import binance_us
    import requests
    base = binance_us.FAPI_DEMO if testnet else binance_us.FAPI_MAIN
    try:
        r = requests.get(f"{base}/fapi/v1/ticker/price", timeout=20)
        if r.status_code != 200:
            return set()
        out = set()
        for it in r.json():
            try:
                if float(it.get("price", 0)) > 0:
                    out.add(it["symbol"])
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return set()



def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="币安美股永续 <-> 正规面板 映射")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--save", default=str(PROJ / "runs" / "binance_us_mapping.json"))
    args = ap.parse_args()

    mapped, diag = build_mapping()
    print("诊断:", json.dumps(diag, ensure_ascii=False, indent=1))
    print(f"\n{'binance_symbol':18s} {'base':10s} {'us_ticker':12s} {'24hUSDT':>14s} {'上线':>10s}")
    print("-" * 70)
    for m in mapped[: args.top]:
        print(f"{m.binance_symbol:18s} {m.binance_base:10s} {m.us_ticker:12s} "
              f"{m.quote_volume_24h:>14,.0f} {m.onboard_date:>10s}")

    days = us_trading_days()
    print(f"\n开市日历: {len(days)} 个交易日  {days[0].date()} -> {days[-1].date()}")

    if args.save:
        Path(args.save).write_text(json.dumps(
            {"diag": diag, "mapping": [m.__dict__ for m in mapped]}, indent=1))
        print(f"已存 {args.save}")


if __name__ == "__main__":
    _main()
