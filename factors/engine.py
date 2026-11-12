"""因子引擎：安全表达式语言。

设计原则：
- LLM 只输出 JSON 描述（算子 + 参数 + 方向），不写裸 Python，避免注入和不稳定。
- 每个因子就是一个算子对面板的映射，输出 [date x symbol] 的因子值矩阵。
- 因子值在 t 日收盘可算，用于预测 t+1 起的收益（严格无前视）。
"""
from __future__ import annotations
import numpy as np
import pandas as pd

PANEL_KEYS = ["o", "h", "l", "c", "v", "vw"]


# ---------- 基础算子（都作用于 [date x symbol] 面板） ----------
def ret(panel, n=1):
    """n 日收益率。"""
    c = panel["c"]
    return c / c.shift(n) - 1.0


def mom(panel, n=20, skip=0):
    """动量：跳过最近 skip 日的 n 日收益。"""
    c = panel["c"]
    return c.shift(skip) / c.shift(skip + n) - 1.0


def reversal(panel, n=5):
    """短期反转（负动量）。"""
    return -ret(panel, n)


def vol(panel, n=20):
    """n 日收益波动率（越低越好时方向取负）。"""
    r = panel["c"].pct_change()
    return r.rolling(n).std()


def turnover(panel, n=20):
    """成交额相对基准的活跃度（换手代理）。"""
    dv = panel["c"] * panel["v"]
    return dv.rolling(n).mean()


def volume_ratio(panel, n=20):
    """量比：当日量 / n 日均量。"""
    v = panel["v"]
    return v / v.rolling(n).mean()


def volatility_ratio(panel, n=20):
    """高低价区间幅度均值。"""
    rng = (panel["h"] - panel["l"]) / panel["c"]
    return rng.rolling(n).mean()


def vwap_dev(panel, n=5):
    """收盘价相对 n 日 VWAP 的偏离。"""
    c = panel["c"]
    vwap = (panel["vw"] * panel["v"]).rolling(n).sum() / panel["v"].rolling(n).sum()
    return c / vwap - 1.0


def price_position(panel, n=60):
    """当前价在 n 日区间中的位置 [0,1]。"""
    c = panel["c"]
    hi = panel["h"].rolling(n).max()
    lo = panel["l"].rolling(n).min()
    return (c - lo) / (hi - lo).replace(0, np.nan)


def ma_gap(panel, n=20):
    """收盘价相对 n 日均线的偏离。"""
    c = panel["c"]
    return c / c.rolling(n).mean() - 1.0


def skew(panel, n=20):
    """收益偏度。"""
    return panel["c"].pct_change().rolling(n).skew()


def kurt(panel, n=20):
    """收益峰度。"""
    return panel["c"].pct_change().rolling(n).kurt()


def max_drawdown(panel, n=60):
    """n 日窗口内最大回撤（正数）。"""
    c = panel["c"]
    roll_max = c.rolling(n).max()
    return (roll_max - c) / roll_max


def amihud(panel, n=20):
    """非流动性：|收益| / 成交额。"""
    r = panel["c"].pct_change().abs()
    dv = panel["c"] * panel["v"]
    return (r / dv).rolling(n).mean() * 1e9


def hl_range(panel, n=10):
    """平均日内振幅占比。"""
    return ((panel["h"] - panel["l"]) / panel["l"]).rolling(n).mean()


def gap(panel, n=5):
    """n 日平均隔夜跳空。"""
    return (panel["o"] / panel["c"].shift(1) - 1.0).rolling(n).mean()


def trend_strength(panel, n=40):
    """均线斜率（标准化）。"""
    c = panel["c"]
    ma = c.rolling(n).mean()
    return ma / ma.shift(n) - 1.0


# ---------- A股/散户市场特色算子 ----------
def downside_vol(panel, n=20):
    """下行波动：只统计负收益的波动（A股恐慌敏感）。

    2026-10-24 修：原实现 `neg = r.where(r < 0)` 把非负收益置 NaN，
    再由 `rolling(n).std()` 计算 —— 而 rolling 默认 min_periods=window，
    窗口里只要有一天收益为正整段就全是 NaN，导致该因子**在所有市场上恒为全空**。
    现在显式允许部分缺失（min_periods = n//2）。
    """
    r = panel["c"].pct_change()
    neg = r.where(r < 0)
    return neg.rolling(n, min_periods=max(3, n // 2)).std()


def upside_vol(panel, n=20):
    """上行波动：只统计正收益的波动。（同 downside_vol，修 min_periods）"""
    r = panel["c"].pct_change()
    pos = r.where(r > 0)
    return pos.rolling(n, min_periods=max(3, n // 2)).std()


def vol_asym(panel, n=20):
    """波动非对称：上行波动 - 下行波动（正=上涨更剧烈）。"""
    return upside_vol(panel, n) - downside_vol(panel, n)


def turnover_chg(panel, n=20):
    """成交额变化率：近期活跃度相对前期（放量/缩量）。"""
    dv = panel["c"] * panel["v"]
    return dv.rolling(n).mean() / dv.rolling(n * 2).mean().shift(n) - 1.0


def turnover_std(panel, n=20):
    """成交额波动：换手不稳定度（A股炒作/情绪代理）。"""
    dv = panel["c"] * panel["v"]
    return dv.rolling(n).std() / dv.rolling(n).mean()


def amount_share(panel, n=20):
    """个股成交额占全市场比例（相对关注度，横截面）。"""
    dv = panel["c"] * panel["v"]
    mkt = dv.rolling(n).mean().sum(axis=1)
    return dv.rolling(n).mean().div(mkt, axis=0)


def illiq(panel, n=20):
    """Amihud 非流动性（放大版，A股流动性溢价敏感）。"""
    r = panel["c"].pct_change().abs()
    dv = panel["c"] * panel["v"]
    return (r / dv).rolling(n).mean() * 1e10


def ret_skew_fast(panel, n=10):
    """短窗收益偏度（彩票偏好/情绪代理，A股散户强）。"""
    return panel["c"].pct_change().rolling(n).skew()


def hl_pos(panel, n=20):
    """收盘在当日高低区间的位置均值 [0,1]（日内强弱）。"""
    rng = (panel["h"] - panel["l"]).replace(0, np.nan)
    pos = (panel["c"] - panel["l"]) / rng
    return pos.rolling(n).mean()


def overbought(panel, n=14):
    """相对强弱超买超卖近似（收盘相对n日均价的乖离）。"""
    c = panel["c"]
    return (c - c.rolling(n).mean()) / c.rolling(n).std()


def close_to_high(panel, n=60):
    """收盘价 / n 日最高价 —— 接近高点的程度（突破/强势信号）。
    越接近 1 表示越靠近阶段高点。"""
    c = panel["c"]
    return c / panel["h"].rolling(n).max()


def volume_price_corr(panel, n=20):
    """n 日内 价格变化 与 成交量变化 的滚动相关 —— 量价配合度。
    正值：放量上涨/缩量下跌（健康量价）；负值：量价背离。"""
    r = panel["c"].pct_change()
    dv = (panel["c"] * panel["v"]).pct_change()
    return r.rolling(n).corr(dv)


def downside_days(panel, n=20):
    """n 日内下跌天数占比 —— 下跌频率（不同于波动率，刻画阴跌/磨人程度）。"""
    r = panel["c"].pct_change()
    return (r < 0).astype(float).rolling(n).mean()


OPS = {
    "ret": ret, "mom": mom, "reversal": reversal, "vol": vol,
    "turnover": turnover, "volume_ratio": volume_ratio,
    "volatility_ratio": volatility_ratio, "vwap_dev": vwap_dev,
    "price_position": price_position, "ma_gap": ma_gap, "skew": skew,
    "kurt": kurt, "max_drawdown": max_drawdown, "amihud": amihud,
    "hl_range": hl_range, "gap": gap, "trend_strength": trend_strength,
    "downside_vol": downside_vol, "upside_vol": upside_vol,
    "vol_asym": vol_asym, "turnover_chg": turnover_chg,
    "turnover_std": turnover_std, "amount_share": amount_share,
    "illiq": illiq, "ret_skew_fast": ret_skew_fast, "hl_pos": hl_pos,
    "overbought": overbought,
    "close_to_high": close_to_high, "volume_price_corr": volume_price_corr,
    "downside_days": downside_days,
}

# 文本因子（延迟导入，避免无新闻时出错）
try:
    from factors.text_factors import NEWS_OPS
    OPS.update(NEWS_OPS)
except Exception:
    pass

# LLM 语气因子
try:
    from factors.llm_text import llm_tone_factor
    OPS["llm_tone"] = llm_tone_factor
except Exception:
    pass

# 基本面因子（需要 FMP 数据）
try:
    from factors.fund_factors import FUND_OPS
    OPS.update(FUND_OPS)
except Exception:
    pass

# XBRL 基本面因子（SEC 免费数据，精确公布日，季度粒度）
try:
    from factors.xbrl_fund import XBRL_OPS
    OPS.update(XBRL_OPS)
except Exception:
    pass

# SEC EDGAR 申报事件因子（免费、精确申报日、next-trading-day 对齐、无前视）
try:
    from factors.edgar_event import EDGAR_OPS
    OPS.update(EDGAR_OPS)
except Exception:
    pass

# 另类数据因子（FINRA 空头量 / Wikipedia 关注度，免费官方源、无前视对齐）
try:
    from factors.alt_data import ALT_OPS
    OPS.update(ALT_OPS)
except Exception:
    pass

# 共振因子（多指标共振框架的日频编码）
try:
    from factors.resonance import RESONANCE_OPS
    OPS.update(RESONANCE_OPS)
except Exception:
    pass

OP_DOC = """可用算子（factor spec 用 "op" 指定）：

【价量因子】
- ret(n=1)                n日收益率
- mom(n=20, skip=0)       动量(跳过最近skip日)
- reversal(n=5)           短期反转
- vol(n=20)               收益波动率
- turnover(n=20)          成交额活跃度
- volume_ratio(n=20)      量比(当日量/均量)
- volatility_ratio(n=20)  日内振幅均值
- vwap_dev(n=5)           收盘价相对VWAP偏离
- price_position(n=60)    价格在区间中的位置[0,1]
- ma_gap(n=20)            价格相对均线偏离
- skew(n=20)              收益偏度
- kurt(n=20)              收益峰度
- max_drawdown(n=60)      窗口内最大回撤
- amihud(n=20)            非流动性
- hl_range(n=10)          平均日内振幅占比
- gap(n=5)                隔夜跳空
- trend_strength(n=40)    均线趋势强度

【文本/新闻因子】
- news_sentiment_5d(n=5)      近n日新闻平均情绪
- news_sentiment_chg(n=5)     情绪变化(近期-前期)
- news_attention(n=5)         新闻关注度(条数)
- news_tone_dispersion(n=5)   情绪分歧度

【基本面因子(年度)】
- value_earnings_yield    盈利收益率(越高越便宜)
- value_book_to_price     账面市值比
- value_sales_yield       营收收益率
- quality_roe             净资产收益率
- quality_margin          净利率
- quality_roic            投入资本回报率
- fcf_yield               自由现金流收益率
- low_leverage            低杠杆

【XBRL 基本面因子(季度,精确公布日,无前视)】
- xbrl_earnings_yield     TTM净利润/市值(盈利收益率)
- xbrl_book_to_price      股东权益/市值(账面市值比)
- xbrl_sales_yield        TTM营收/市值(营收收益率)
- xbrl_fcf_yield          (经营现金流-资本开支)/市值
- xbrl_roe                TTM净利润/股东权益(ROE)
- xbrl_gross_margin       毛利率
- xbrl_net_margin         净利率
- xbrl_asset_turnover     资产周转率
- xbrl_revenue_yoy        营收同比增速
- xbrl_earnings_yoy       盈利同比增速
- xbrl_log_mcap           对数市值(规模)

每个因子给 direction: 1(值越大越看多) 或 -1(值越大越看空)。
注意：文本和基本面因子更新慢，适合较长持有期。"""


def make_factor(panel: dict, spec: dict) -> pd.Series:
    """根据 spec 生成因子值。spec = {op, params{}, direction} 或复合因子。"""
    # 因子库合成：kind=composite，把多个已晋升因子横截面 rank 等权合成。
    # 这样库里的稳健因子能直接走 compute_targets / 回测 的同一条通道。
    if spec.get("kind") == "composite":
        from core.factor_library import composite_factor
        comp = composite_factor(panel, specs=spec.get("specs"))
        if comp is None:
            raise ValueError("因子库为空，无法合成 composite 因子")
        direction = spec.get("direction", 1)
        return comp * direction
    # 复合因子
    if spec.get("kind") == "combo" or ("expr" in spec):
        from factors.composite import build_combo_frame
        raw = build_combo_frame(panel, spec)
        direction = spec.get("direction", 1)
        return raw * direction
    op = spec["op"]
    if op not in OPS:
        raise ValueError(f"未知算子: {op}")
    params = spec.get("params", {}) or {}
    raw = OPS[op](panel, **params)
    direction = spec.get("direction", 1)
    return raw * direction


def factor_series_to_frame(panel: dict, spec: dict) -> pd.DataFrame:
    """确保返回 [date x symbol]。"""
    v = make_factor(panel, spec)
    if isinstance(v, pd.Series):
        # 少数算子可能返回 Series，广播
        return pd.DataFrame(np.repeat(v.values[:, None], len(panel["c"].columns), axis=1),
                            index=v.index, columns=panel["c"].columns)
    return v
