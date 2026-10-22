#!/usr/bin/env python
"""新闻清洗：把原始新闻分成「大盘宏观」和「个股信号」两股。

背景与设计（2026-10-23）
------------------------
直接拿 Marketaux 原始新闻喂 LLM 是浪费 token，实测存在三类噪声：

1. **大盘综述被挂到多只票**（13.8%）：同一段 description 出现在 3 只以上
   股票名下，比如「ZeroHedge - On a long enough timeline...」挂了 9 只票。
   这类新闻**不能删** —— 店长指出「大盘也会影响整体情绪」，科技板块下挫
   确实会带动个股一起跌。正确做法是**单独成一股交给宏观判断**，而不是
   按个股重复分配（重复分配等于把同一个信息算 N 次）。

2. **GF Value 估值模板文**（6.8%）：gurufocus 的「XX 股价涨 1.9%，
   GF Value 显示高估」是机械生成的估值对比，不含事件信息。

3. **ETF/基金持仓变动**：「Invesco QQQ Sells $94.8M of NVIDIA」是资金流
   快照，不是事件驱动，且天然滞后。

输出两股：
  * macro  —— 大盘/宏观新闻（去重后一份），供 llm_news 做整体情绪判断
  * stock  —— 个股新闻（按 symbol 归集），供逐票 LLM 判断

用法：
    venv/bin/python -m scripts.clean_news            # 打印统计
    venv/bin/python -m scripts.clean_news --dump MU  # 看某只票清洗后的新闻
"""
from __future__ import annotations

import sys
import json
import re
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

NEWS_PATH = PROJ / "data" / "news" / "news_raw.parquet"
CACHE_PATH = PROJ / "data" / "news" / "news_clean.parquet"

# 同一段 description 出现在 >= 这么多只票名下，判定为大盘/综述类
MACRO_SPREAD_THRESHOLD = 3

# GF Value / GF Score 估值模板文（gurufocus 机械生成）
GF_TEMPLATE_RE = r"GF Value|GF Score|After [\d.]+% (?:Gain|Decline)|Shares (?:Fall|Rise|Move)"

# ETF / 基金持仓变动（资金流快照，非事件）
FUND_FLOW_RE = (r"\b(?:ETF|Fund)\b.*\b(?:Bought|Sells?|Sold|Buys?|Trimmed|Added|Reduced)\b"
                r"|What .* (?:Bought|Sold)\b|Holdings? (?:Update|Change)")

DESC_DEDUP_PREFIX = 100

# 公司名 -> 用于判断「这条新闻是否点名了该票」。
# 只放主关键词，够区分即可；不全的票会退化成只认代码，不会误判。
COMPANY_ALIASES = {
    "AAPL": "APPLE", "MSFT": "MICROSOFT", "GOOGL": "ALPHABET", "GOOG": "ALPHABET",
    "AMZN": "AMAZON", "NVDA": "NVIDIA", "META": "META PLATFORMS", "TSLA": "TESLA",
    "AVGO": "BROADCOM", "AMD": "ADVANCED MICRO", "INTC": "INTEL", "MU": "MICRON",
    "QCOM": "QUALCOMM", "ORCL": "ORACLE", "CRM": "SALESFORCE", "ADBE": "ADOBE",
    "NFLX": "NETFLIX", "JPM": "JPMORGAN", "BAC": "BANK OF AMERICA", "GS": "GOLDMAN",
    "MS": "MORGAN STANLEY", "V": "VISA", "MA": "MASTERCARD", "JNJ": "JOHNSON",
    "PFE": "PFIZER", "MRK": "MERCK", "LLY": "ELI LILLY", "UNH": "UNITEDHEALTH",
    "XOM": "EXXON", "CVX": "CHEVRON", "WMT": "WALMART", "COST": "COSTCO",
    "HD": "HOME DEPOT", "MCD": "MCDONALD", "KO": "COCA-COLA", "PEP": "PEPSICO",
    "DIS": "DISNEY", "BA": "BOEING", "CAT": "CATERPILLAR", "GE": "GENERAL ELECTRIC",
    "COIN": "COINBASE", "MSTR": "MICROSTRATEGY", "HOOD": "ROBINHOOD",
    "PLTR": "PALANTIR", "CRCL": "CIRCLE", "SMCI": "SUPER MICRO",
}


# 宏观/大盘词表（2026-10-23 新增）
# 背景：店长指出「大盘也会有影响整体情绪」。原先只按「同一篇跨 3 只票」判宏观，
# 但实测漏掉了一批只挂单票的大盘稿，例如 MU 名下那条
# 「European and US Markets Plunge Amid Rising Tensions」——讲的是全市场，
# 却因为只出现一次而被当成 MU 个股新闻喂给 LLM，会污染个股判断。
# 现在加一层标题词表判定：命中即归入 macro 股，与跨票复用度并联（OR）。
#
# 收紧（同日修正）：第一版用 r"Stocks? ... (Rise|Fall|Gain)" 会把
# 「Micron Technology Stock Gains 4% Premarket」这类**个股**新闻误判成宏观。
# 关键区别是**主体**：大盘稿的主体是市场/指数（Markets/Indexes/Dow/Nasdaq），
# 个股稿的主体是公司名。所以主体只认市场词，不认泛指的 Stocks。
MACRO_TITLE_RE = (
    r"\b(?:Markets?|Indexes|Indices|Wall Street|Dow|S&P|Nasdaq(?:\s+100)?|Futures)\b"
    r"[^.]{0,60}?\b(?:Plunge|Plunges|Surge|Rally|Slump|Sink|Sinks|Tumble|Tumbles"
    r"|Rise|Rises|Fall|Falls|Gain|Gains|Decline|Drop|Drops|Jump|Rebound)\b"
    # 反向语序：动作在前、主体在后（"Drags Major Indexes Lower" /
    # "Hammering Stocks"）。实测漏判过
    # 「Tech Sector Slump Drags Major Indexes Lower」，所以补这一支。
    r"|\b(?:Drags?|Dragged|Weigh(?:s|ed)?|Hammer(?:s|ed)?|Hit|Hits|Lift(?:s|ed)?|Push(?:es|ed)?"
    r"|Send|Sends|Sending)\b[^.]{0,30}?\b(?:Major\s+)?(?:Indexes|Indices|Markets|Stocks)\b"
    # 盘前/盘后整体行情 + 板块级动词（"Tech and Earnings Volatility Shape Premarket Action"）
    r"|\b(?:Tech|Earnings|Market|Sector)\b[^.]{0,30}\b(?:Volatility|Jitters|Slump|Rally|Selloff)\b"
    r"|\b(?:Premarket|Afterhours|After-hours|Overnight)\b[^.]{0,25}\b(?:Action|Volatility|Movers)\b"
    r"|European and US Markets|Market (?:Panic|Turmoil|Selloff|Rally|Runs)"
    r"|\b(?:Surging|Slumping|Falling|Rising)\b[^.]{0,20}\b(?:stocks?|indexes)\b"
    r"|\b(?:stocks?|indexes)\b[^.]{0,25}\b(?:hammering|hammered|hit|sink|slide)\b"
    r"|Sector (?:Slump|Rally|Rotation|Selloff)"
    r"|\bFed\b|Federal Reserve|\bCPI\b|\bPPI\b|Nonfarm|Jobless|Treasury (?:Yield|Yields)"
    r"|Inflation (?:Rise|Fall|Data|Report)|Rate (?:Cut|Hike|Decision)"
)


def _parse_dt(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["dt"] = pd.to_datetime(df["published_at"], utc=True, errors="coerce")
    return df.dropna(subset=["dt"])


def load_raw() -> pd.DataFrame:
    df = pd.read_parquet(NEWS_PATH)
    df = _parse_dt(df)
    df["description"] = df["description"].fillna("")
    return df


def split_macro(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按 description 复用度 + 标题主体拆成 macro / per-symbol 两股。

    macro 只保留**一份**（按指纹去重），因为它描述的是全市场状态，
    给每只票各复制一份只会让下游重复计算同一个信息。

    判定顺序（2026-10-23 定稿）：
      1. 跨票复用 >= 3  -> 宏观。同一篇稿挂到多只票名下，本质是全市场信息。
      2. 标题命中宏观词表 -> 宏观。
      3. **但**若标题/描述里明确点名了这只票本身（公司名或代码），
         则判为个股 —— 这条优先级最高。
         原因：只靠词表会误伤「Nvidia's Market Share in China Drops」
         「Micron Technology Stock Gains 4%」这类个股稿（前者含 "Market"，
         后者含 "Stock Gains"）。反过来，「Tech Sector Slump Drags Down
         Major Indexes」不提任何具体公司，才该进宏观股。
    """
    df = df.copy()
    # 用标题+描述前缀做指纹：比单用描述更稳，能抓住标题不同但正文相同的情况
    df["_fp"] = (df["title"].fillna("").str[:40] + "|" +
                 df["description"].str[:DESC_DEDUP_PREFIX])
    spread = df.groupby("_fp")["symbol"].nunique()
    df["_spread"] = df["_fp"].map(spread)

    # 该票是否在标题/描述中被点名
    def _names_self(row) -> bool:
        sym = str(row["symbol"] or "").upper()
        title = str(row["title"] or "")
        desc = str(row["description"] or "")[:300]
        blob = (title + " " + desc).upper()
        # 代码直接命中（作为独立词，避免 MU 命中 MUTUAL 之类）
        if sym and re.search(rf"(?<![A-Z]){re.escape(sym)}(?![A-Z])", blob):
            return True
        # 公司名命中（取公司名的首个显著词，长度 >= 4 才可靠）
        name = COMPANY_ALIASES.get(sym, "")
        if name and name.upper() in blob:
            return True
        return False

    named_self = df.apply(_names_self, axis=1)
    by_spread = df["_spread"] >= MACRO_SPREAD_THRESHOLD
    by_title = df["title"].str.contains(MACRO_TITLE_RE, case=False, na=False, regex=True)

    # 点名了本票 -> 一律算个股，压过词表
    is_macro = (by_spread | by_title) & ~named_self
    df["_is_macro"] = is_macro
    macro = df[is_macro].drop_duplicates("_fp").copy()   # 每个指纹只留一条
    stock = df[~is_macro].copy()
    return macro, stock


def filter_noise(stock: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """剔除个股新闻里的模板文与资金流快照。"""
    stats: dict[str, int] = {"input": len(stock)}

    gf = stock["title"].str.contains(GF_TEMPLATE_RE, case=False, na=False, regex=True)
    stats["gf_template"] = int(gf.sum())
    stock = stock[~gf]

    ff = stock["title"].str.contains(FUND_FLOW_RE, case=False, na=False, regex=True)
    stats["fund_flow"] = int(ff.sum())
    stock = stock[~ff]

    # 同票同日同描述前缀去重
    before = len(stock)
    stock = stock.drop_duplicates(["symbol", "_fp"])
    stats["dup_same_symbol"] = before - len(stock)

    stats["output"] = len(stock)
    return stock, stats


def coverage(stock: pd.DataFrame, days: int = 7) -> pd.DataFrame:
    """按 symbol 统计近 N 天的清洗后新闻条数。"""
    now = datetime.now(timezone.utc)
    recent = stock[stock["dt"] >= now - timedelta(days=days)]
    g = recent.groupby("symbol").agg(
        n=("title", "size"),
        latest=("dt", "max"),
        sentiment=("sentiment", "mean"),
    ).sort_values("n", ascending=False)
    g["latest"] = g["latest"].dt.strftime("%m-%d %H:%M")
    g["sentiment"] = g["sentiment"].round(2)
    return g


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", metavar="SYMBOL", help="打印该票清洗后的新闻")
    ap.add_argument("--save", action="store_true", help="把清洗结果落盘到 news_clean.parquet")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    raw = load_raw()
    macro, stock = split_macro(raw)
    clean, stats = filter_noise(stock)

    print("=" * 62)
    print(f"原始新闻        : {len(raw)} 条")
    print(f"  大盘/宏观     : {len(macro)} 条（去重后，跨票复用 >= {MACRO_SPREAD_THRESHOLD}）")
    print(f"  个股候选      : {len(stock)} 条")
    print("-" * 62)
    print(f"  剔除 GF 模板  : -{stats['gf_template']}")
    print(f"  剔除 ETF 资金流: -{stats['fund_flow']}")
    print(f"  剔除同票重复  : -{stats['dup_same_symbol']}")
    print(f"  个股清洗后    : {stats['output']} 条")
    print("=" * 62)

    cov = coverage(clean, days=args.days)
    print(f"\n近 {args.days} 天清洗后覆盖：{len(cov)} 只票有新闻")
    if len(cov):
        print(f"  中位数 {cov['n'].median():.0f} 条 / 均值 {cov['n'].mean():.1f} 条")
        print("\n前 12 名：")
        print(cov.head(12).to_string())

    if args.dump:
        d = clean[clean["symbol"] == args.dump].sort_values("dt", ascending=False).head(8)
        print(f"\n=== {args.dump} 清洗后新闻（最近 8 条）===")
        for _, r in d.iterrows():
            print(f"[{r['dt'].strftime('%m-%d %H:%M')}] s={r['sentiment']:+.2f} {r['source']}")
            print(f"    {r['title'][:110]}")
            if r["description"]:
                print(f"    {r['description'][:130]}")

    if args.save:
        macro_out = macro.copy()
        macro_out["kind"] = "macro"
        clean_out = clean.copy()
        clean_out["kind"] = "stock"
        keep = ["symbol", "dt", "title", "description", "source", "sentiment", "url", "kind"]
        out = pd.concat([macro_out[keep], clean_out[keep]], ignore_index=True)
        out.to_parquet(CACHE_PATH, index=False)
        print(f"\n已写入 {CACHE_PATH}（{len(out)} 条：macro {len(macro_out)} / stock {len(clean_out)}）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
