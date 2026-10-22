"""宏观事件日历（用于事件日屏蔽 / 冲击后动量）。

数据源与可靠性排序：
  1. ForexFactory 周历（nfs.faireconomy.media/ff_calendar_thisweek.json）
     免费、无需 key，但**只有当周**、有速率限制、无历史归档。
  2. 规则日历（FOMC 固定 8 次/年、CPI 每月、NFP 每月首个周五）—— 可合成历史。

用途：
  - 事件日屏蔽：High impact 事件当天不开新仓（降低被突袭的概率）。
  - 冲击后动量：事件后 N 天的动量延续/反转分析。

诚实边界：
  - 免费源没有干净的多年历史事件表，历史部分靠规则日历近似。
  - 当周抓取会随时间积累成真实事件库（每周跑一次即可）。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

PROJ = Path(__file__).resolve().parent.parent
CACHE = PROJ / "data" / "macro_calendar"
CACHE.mkdir(parents=True, exist_ok=True)
FF_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


# ---------------------------------------------------------------------------
# 当周日历抓取（带降级）
# ---------------------------------------------------------------------------
def fetch_ff_week() -> list[dict]:
    """抓当前周 ForexFactory 日历。限流时返回空。"""
    try:
        r = requests.get(FF_WEEK, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        if r.status_code != 200 or not r.text.strip().startswith("["):
            return []
        return r.json()
    except Exception:  # noqa: BLE001
        return []


def save_week(events: list[dict]) -> Path:
    """把当周事件存成 dated 文件，长期积累成库。"""
    if not events:
        return CACHE / "empty"
    today = date.today().isoformat()
    p = CACHE / f"ff_week_{today}.json"
    p.write_text(json.dumps(events, ensure_ascii=False, indent=1))
    return p


def load_all_cached() -> pd.DataFrame:
    """读取所有已缓存的周文件，合并成事件表。"""
    rows = []
    for p in sorted(CACHE.glob("ff_week_*.json")):
        try:
            for e in json.loads(p.read_text()):
                rows.append({
                    "time": e.get("date", ""),
                    "country": e.get("country", ""),
                    "title": e.get("title", ""),
                    "impact": e.get("impact", ""),
                    "forecast": e.get("forecast", ""),
                    "previous": e.get("previous", ""),
                    "source_file": p.name,
                })
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["time"], errors="coerce", utc=True).dt.tz_localize(None)
    return df.dropna(subset=["dt"]).sort_values("dt")


# ---------------------------------------------------------------------------
# 规则日历（可合成历史，缓解免费源无历史的问题）
# ---------------------------------------------------------------------------
def fomc_dates(year_start: int, year_end: int) -> list[date]:
    """FOMC 会议日期（每年 8 次，实际日期需维护；此处用近似规则）。

    注意：真实 FOMC 日期由美联储公布，这里用「每年 1/3/5/6/7/9/11/12 月中下旬」
    近似。精确日期应每年更新。仅用于「屏蔽窗口」这种粗粒度用途。
    """
    out = []
    months = [1, 3, 5, 6, 7, 9, 11, 12]
    for y in range(year_start, year_end + 1):
        for m in months:
            # 近似：该月第三个周三
            d = date(y, m, 1)
            wed = 0
            while wed < 3:
                if d.weekday() == 2:
                    wed += 1
                    if wed == 3:
                        out.append(d)
                        break
                d += timedelta(days=1)
    return out


def cpi_dates(year_start: int, year_end: int) -> list[date]:
    """CPI 发布日近似：每月 10-15 日之间的工作日（约每月中旬）。"""
    out = []
    for y in range(year_start, year_end + 1):
        for m in range(1, 13):
            for day in range(10, 16):
                d = date(y, m, day)
                if d.weekday() < 5:
                    out.append(d)
                    break
    return out


def nfp_dates(year_start: int, year_end: int) -> list[date]:
    """非农(NFP)：每月第一个周五。"""
    out = []
    for y in range(year_start, year_end + 1):
        for m in range(1, 13):
            d = date(y, m, 1)
            while d.weekday() != 4:
                d += timedelta(days=1)
            out.append(d)
    return out


def rule_calendar(year_start: int = 2021, year_end: int = 2026) -> pd.DataFrame:
    rows = []
    for name, dts in [("FOMC", fomc_dates(year_start, year_end)),
                      ("CPI", cpi_dates(year_start, year_end)),
                      ("NFP", nfp_dates(year_start, year_end))]:
        for d in dts:
            rows.append({"dt": pd.Timestamp(d), "event": name, "impact": "High"})
    return pd.DataFrame(rows).sort_values("dt")


# ---------------------------------------------------------------------------
# 官方 BLS 发布日历（权威、精确，来自 bls.gov/schedule/<YEAR>/home.htm）
# 解析产物：data/macro_calendar/bls_releases.parquet（见 scripts/parse_bls_schedule.py）
# 这是「无前视」事件研究的首选日历，因为日期是官方预先公布的发布时间，
# 不是从价格反推的近似值。
# ---------------------------------------------------------------------------
BLS_PARQUET = CACHE / "bls_releases.parquet"


def official_calendar() -> pd.DataFrame:
    """读取官方 BLS 发布日历（CPI/PPI/NFP）。

    返回列：dt（发布日期）、event（CPI/PPI/NFP/REAL_EARN）、time_et、ref_period。
    """
    if not BLS_PARQUET.exists():
        raise FileNotFoundError(
            f"缺少官方日历 {BLS_PARQUET}；先运行 scripts/parse_bls_schedule.py"
        )
    df = pd.read_parquet(BLS_PARQUET)
    df = df.rename(columns={"date": "dt", "event_type": "event"})
    df["dt"] = pd.to_datetime(df["dt"])
    return df[["dt", "event", "time_et", "ref_period", "release_name"]].sort_values("dt")


# ---------------------------------------------------------------------------
# 事件窗口
# ---------------------------------------------------------------------------
def event_windows(cal: pd.DataFrame, pre: int = 1, post: int = 1) -> set:
    """把事件日扩展成 ±pre/post 交易日的「屏蔽窗口」（用自然日近似）。"""
    days = set()
    for dt in cal["dt"]:
        for k in range(-pre, post + 1):
            days.add((dt + timedelta(days=k)).date())
    return days


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="宏观事件日历")
    ap.add_argument("cmd", choices=["fetch", "show", "rules"])
    ap.add_argument("--pre", type=int, default=1)
    ap.add_argument("--post", type=int, default=1)
    args = ap.parse_args()

    if args.cmd == "fetch":
        ev = fetch_ff_week()
        p = save_week(ev)
        print(f"抓到 {len(ev)} 事件 -> {p}")
        if ev:
            hi = [e for e in ev if e.get("impact") == "High"]
            print(f"High impact: {len(hi)}")
            for e in hi[:12]:
                print(f"  {e['date'][:16]:20s} {e['country']:5s} {e['title'][:45]}")
    elif args.cmd == "show":
        df = load_all_cached()
        print(f"缓存事件: {len(df)} 条" + (f"  {df['dt'].min()} ~ {df['dt'].max()}" if len(df) else ""))
        if len(df):
            print(df[df["impact"] == "High"].tail(15).to_string())
    elif args.cmd == "rules":
        rc = rule_calendar()
        print(f"规则日历: {len(rc)} 个事件")
        print(rc["event"].value_counts().to_string())
        w = event_windows(rc, args.pre, args.post)
        print(f"±{args.pre}/{args.post} 日屏蔽窗口: {len(w)} 天")
        print(rc.head(8).to_string())
