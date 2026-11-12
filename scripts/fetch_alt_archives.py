#!/usr/bin/env python3
"""换通道：LuxAlgo/market-trackers-data 的年度归档（CC0-1.0，从 FINRA / Wikimedia 归集）。

原因：FINRA 官方日文件 1471 次请求实测只有 16 个/分钟（≈88 分钟），
而 archives.json 里按年打包的 release asset 一次就能拿一整年。
流式解析 gz JSON 数组，只保留本项目 655 个标的，避免 1.9G 内存 OOM。

来源：https://github.com/LuxAlgo/market-trackers-data (CC0-1.0)
运行时刻见日志；产物字段与 data/finra.py、data/wikipedia.py 保持一致。
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJ = Path("/opt/quant-lab")
ALT = PROJ / "data" / "alt"
ALT.mkdir(parents=True, exist_ok=True)
ARCH = json.loads(Path("/tmp/arch.json").read_text())["datasets"]
REL = ("https://github.com/LuxAlgo/market-trackers-data/releases/"
       "download/{tag}/{asset}")
UNI = {str(c).upper() for c in
       pd.read_parquet(PROJ / "data" / "hist" / "hist_c.parquet").columns}


def iter_objects(path: Path):
    """流式解析 `[{...},{...}]`，不把整份解压结果读进内存。"""
    dec = json.JSONDecoder()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        buf = f.read(1 << 20)
        i = 0
        while i < len(buf) and buf[i] in " \t\r\n[":
            i += 1
        buf = buf[i:]
        while buf:
            try:
                obj, end = dec.raw_decode(buf)
                yield obj
                buf = buf[end:]
                j = 0
                while j < len(buf) and buf[j] in " \t\r\n,]":
                    j += 1
                buf = buf[j:]
            except ValueError:
                more = f.read(1 << 20)
                if not more:
                    break
                buf += more


def fetch(tag: str, asset: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    r = subprocess.run(["curl", "-sSL", "--max-time", "300", "-o", str(dest),
                        REL.format(tag=tag, asset=asset)],
                       capture_output=True, text=True)
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def build_finra(years=(2021, 2022, 2023, 2024, 2025, 2026)) -> dict:
    rows = []
    for y in years:
        m = ARCH["short-volume"]["years"][str(y)]
        p = Path("/tmp") / ("finra_%s.json.gz" % y)
        print("[finra] 下载", y, m["asset"], flush=True)
        if not fetch(m["tag"], m["asset"], p):
            print("  下载失败", flush=True); continue
        n = 0
        for r in iter_objects(p):
            tk = str(r.get("ticker", "")).upper()
            if tk not in UNI:
                continue
            rows.append({"Date": int(str(r["date"]).replace("-", "")),
                         "Symbol": tk,
                         "ShortVolume": r.get("shortVolume"),
                         "ShortExemptVolume": r.get("shortExemptVolume"),
                         "TotalVolume": r.get("totalVolume")})
            n += 1
        print("  %s 保留 %d 行（累计 %d）" % (y, n, len(rows)), flush=True)
    if not rows:
        return {"error": "no rows"}
    df = (pd.DataFrame(rows).drop_duplicates(["Date", "Symbol"])
          .sort_values(["Date", "Symbol"]))
    out = ALT / "finra_short.parquet"
    df.to_parquet(out, index=False)
    return {"rows": len(df), "days": df.Date.nunique(),
            "tickers": df.Symbol.nunique(), "out": str(out),
            "range": [int(df.Date.min()), int(df.Date.max())]}


def build_wiki(years=(2021, 2022, 2023, 2024, 2025, 2026)) -> dict:
    rows = []
    for y in years:
        m = ARCH["wiki-pageviews"]["years"][str(y)]
        p = Path("/tmp") / ("wiki_%s.json.gz" % y)
        print("[wiki] 下载", y, m["asset"], flush=True)
        if not fetch(m["tag"], m["asset"], p):
            print("  下载失败", flush=True); continue
        n = 0
        for r in iter_objects(p):
            tks = r.get("tickers") or []
            day = str(r.get("day", "")).replace("-", "")
            for tk in tks:
                tk = str(tk).upper()
                if tk not in UNI:
                    continue
                rows.append({"date": int(day), "ticker": tk,
                             "year": y, "views": r.get("views")})
                n += 1
        print("  %s 保留 %d 行（累计 %d）" % (y, n, len(rows)), flush=True)
    if not rows:
        return {"error": "no rows"}
    df = (pd.DataFrame(rows).drop_duplicates(["date", "ticker"])
          .sort_values(["date", "ticker"]))
    out = ALT / "wiki_pageviews.parquet"
    df.to_parquet(out, index=False)
    return {"rows": len(df), "days": df.date.nunique(),
            "tickers": df.ticker.nunique(), "out": str(out),
            "range": [int(df.date.min()), int(df.date.max())]}


if __name__ == "__main__":
    print("时刻", datetime.now().isoformat(), flush=True)
    print("FINRA", build_finra(), flush=True)
    print("WIKI", build_wiki(), flush=True)
