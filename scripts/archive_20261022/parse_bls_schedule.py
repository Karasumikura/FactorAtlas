#!/usr/bin/env python3
"""Parse BLS official 'Schedule of Selected Releases' markdown caches
into a structured macro-event table.

Source: https://www.bls.gov/schedule/<YEAR>/home.htm (official, authoritative).
The pages were fetched via Tavily/web_extract (bls.gov blocks plain curl).
Cached markdown lives under /root/.hermes/cache/web/www.bls.gov-*.md.

Output: data/macro_calendar/bls_releases.parquet + .csv
Columns: date (US trading day, ET), time_et, event_type, release_name,
         ref_period, source_url, year
No look-ahead: the date is the *publication* date as scheduled by BLS.
"""
from __future__ import annotations

import glob
import os
import re
from datetime import date

import pandas as pd

CACHE_GLOB = "/root/.hermes/cache/web/www.bls.gov-*.md"
OUT_DIR = "data/macro_calendar"

# Map release-name prefixes to canonical event types.
EVENT_MAP = [
    ("Consumer Price Index", "CPI"),
    ("Producer Price Index", "PPI"),
    ("Employment Situation", "NFP"),          # nonfarm payrolls
    ("Real Earnings", "REAL_EARN"),
]

ROW_RE = re.compile(
    r"\|\s*(?P<dow>[A-Za-z]+),\s*(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})\s*"
    r"\|\s*(?P<time>\d{1,2}:\d{2}\s*[AP]M)?\s*"
    r"\|\s*(?P<name>[^|]+?)\s*\|"
)


def classify(name: str) -> str | None:
    for prefix, code in EVENT_MAP:
        if name.startswith(prefix):
            return code
    return None


def ref_period(name: str) -> str:
    m = re.search(r"\bfor (.+?)\s*$", name)
    return m.group(1).strip() if m else ""


def parse_file(path: str, source_url: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    rows = []
    for m in ROW_RE.finditer(text):
        name = m.group("name").strip()
        code = classify(name)
        if code is None:
            continue
        # Skip holiday/exclusion rows (no time) — events we care about have a time.
        if not m.group("time"):
            continue
        dt = date(
            int(m.group("year")),
            pd.Timestamp(f"{m.group('month')} 1").month,
            int(m.group("day")),
        )
        rows.append(
            {
                "date": pd.Timestamp(dt),
                "time_et": m.group("time").replace(" ", ""),
                "event_type": code,
                "release_name": name,
                "ref_period": ref_period(name),
                "source_url": source_url,
            }
        )
    return rows


def main() -> None:
    all_rows: list[dict] = []
    for path in sorted(glob.glob(CACHE_GLOB)):
        with open(path, encoding="utf-8") as fh:
            head = fh.read(500)
        ym = re.search(r"Schedule of Selected Releases (\d{4})", head)
        if not ym:
            continue
        year = ym.group(1)
        url = f"https://www.bls.gov/schedule/{year}/home.htm"
        rows = parse_file(path, url)
        if rows:
            print(f"  {year}: {len(rows)} events from {os.path.basename(path)}")
            all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    if df.empty:
        raise SystemExit("No BLS events parsed — check cache files.")
    # De-dupe (a release can appear in multiple yearly pages' overlaps).
    df = df.drop_duplicates(subset=["date", "event_type", "release_name"])
    df = df.sort_values(["date", "event_type"]).reset_index(drop=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    df.to_parquet(f"{OUT_DIR}/bls_releases.parquet", index=False)
    df.to_csv(f"{OUT_DIR}/bls_releases.csv", index=False)

    print(f"\nTotal events: {len(df)}")
    print(df["event_type"].value_counts().to_string())
    print(f"\nDate range: {df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"\nCPI releases by year:")
    print(df[df.event_type == "CPI"].groupby(df["date"].dt.year).size().to_string())
    print("\nSaved ->", f"{OUT_DIR}/bls_releases.parquet")


if __name__ == "__main__":
    main()
