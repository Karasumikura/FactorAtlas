#!/usr/bin/env python3
"""Event-aware position/vol gate for the US bridge (execution layer).

Rationale (from us_event_study.py + us_macro_mask.py):
  * On official BLS CPI days the broad market's cross-sectional return std is
    ~1.6% vs ~1.14% on normal days; on NFP days semis/memory abnormal-return
    std jumps to ~2.2-3.0% vs ~1.7-2.1% normally. Risk is empirically elevated.
  * The *direction* of the move is NOT predictable from the calendar, and the
    post-event drift is indistinguishable from unconditional (no tradable edge).
  * Masking (skipping rebalance on event days) only marginally helps momentum
    and hurts reversal — so we do NOT rely on it as alpha.

Therefore the correct, evidence-based response is to *scale exposure*, not to
predict or fully avoid: on high-impact macro release days (and the session
before), shrink gross exposure by a factor. This directly cuts tail losses
from the "CPI -> memory crash" pattern the user described.

This module only produces a scalar gate; it makes no return predictions.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PROJ = Path(__file__).resolve().parent.parent
BLS_PARQUET = PROJ / "data" / "macro_calendar" / "bls_releases.parquet"

# Impact tiering by event type. High = full de-risk.
IMPACT = {"CPI": "high", "NFP": "high", "PPI": "mid"}

# Default gate per tier (multiplier on target gross exposure).
DEFAULT_GATES = {"high": 0.50, "mid": 0.80, "none": 1.0}


class EventGate:
    def __init__(
        self,
        parquet: Path = BLS_PARQUET,
        pre_days: int = 1,
        post_days: int = 0,
        gates: dict | None = None,
        events: tuple[str, ...] = ("CPI", "NFP", "PPI"),
    ):
        self.gates = {**DEFAULT_GATES, **(gates or {})}
        self.pre = pre_days
        self.post = post_days
        self._cal: dict[date, str] = {}
        if not parquet.exists():
            raise FileNotFoundError(parquet)
        df = pd.read_parquet(parquet)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[df.event_type.isin(events)]
        # For each event date, keep the highest impact among its releases.
        rank = {"high": 2, "mid": 1}
        for d, et in zip(df["date"], df["event_type"]):
            tier = IMPACT.get(et, "mid")
            if rank.get(tier, 0) > rank.get(self._cal.get(d, "none"), 0):
                self._cal[d] = tier

    def _levels(self, d: date) -> list[str]:
        """Impact tiers active for calendar day d (event day itself + windows)."""
        out = []
        for k in range(-self.pre, self.post + 1):
            out.append(self._cal.get(d + timedelta(days=k), "none"))
        return out

    def multiplier(self, d: date) -> float:
        """Exposure multiplier for a given US trading day (natural-date lookup).

        Uses the *most conservative* (smallest) gate across the active window,
        so the day before a High event is also de-risked.
        """
        levels = self._levels(d)
        if not levels:
            return 1.0
        best = "none"
        order = {"none": 0, "mid": 1, "high": 2}
        for lv in levels:
            if order.get(lv, 0) > order.get(best, 0):
                best = lv
        return self.gates[best]

    def schedule(self, days: list[date]) -> dict[str, float]:
        return {d.isoformat(): self.multiplier(d) for d in days}

    def upcoming(self, from_day: date | None = None, n: int = 8) -> list[dict]:
        from_day = from_day or date.today()
        rows = []
        for d in sorted(self._cal):
            if d >= from_day:
                rows.append({"date": d.isoformat(), "tier": self._cal[d],
                             "gate": self.gates[self._cal[d]]})
            if len(rows) >= n:
                break
        return rows


def main() -> None:
    import argparse
    import datetime as dt

    ap = argparse.ArgumentParser(description="宏观事件感知的仓位门控")
    ap.add_argument("cmd", choices=["upcoming", "multipliers"], nargs="?", default="upcoming")
    ap.add_argument("--pre", type=int, default=1)
    ap.add_argument("--post", type=int, default=0)
    ap.add_argument("--from", dest="from_day", default=None)
    args = ap.parse_args()

    g = EventGate(pre_days=args.pre, post_days=args.post)
    if args.cmd == "upcoming":
        start = dt.date.fromisoformat(args.from_day) if args.from_day else dt.date.today()
        print(f"未来高影响发布（门控前置 {args.pre} 天, 后置 {args.post} 天）:")
        for r in g.upcoming(start):
            print(f"  {r['date']}  {r['tier']:5s}  gate={r['gate']:.2f}")
    else:
        # Show gate for a month of trading days
        start = dt.date.fromisoformat(args.from_day) if args.from_day else dt.date.today()
        d = start
        print("近期每个交易日的敞口系数:")
        shown = 0
        while shown < 15:
            if d.weekday() < 5:
                print(f"  {d.isoformat()}  x{g.multiplier(d):.2f}")
                shown += 1
            d += dt.timedelta(days=1)


if __name__ == "__main__":
    main()
