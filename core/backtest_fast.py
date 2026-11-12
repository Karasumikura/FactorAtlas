"""向量化回测核心：把逐日 Python 循环改成 pandas 批量运算，快 50 倍+。

关键函数：
- rank_ic_fast(factor, fwd, mask)  -> 逐日 Spearman IC（向量化）
- layered_ls_fast(...)             -> 分层多空收益（向量化）
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def _prep(factor: pd.DataFrame, fwd: pd.DataFrame, mask: pd.DataFrame | None):
    """对齐并应用掩码。所有为 [date x symbol]。"""
    f = factor.copy()
    if mask is not None:
        f = f.where(mask.reindex_like(f))
    # 只保留两表都有值的
    common_cols = f.columns.intersection(fwd.columns)
    f = f[common_cols]
    r = fwd[common_cols].reindex(index=f.index)
    valid = f.notna() & r.notna()
    return f.where(valid), r.where(valid)


# 逐日横截面最少标的数。20 是给美股（数百只）定的门槛；crypto 池只有 17 只，
# 且每日可交易数中位仅 9 —— 用 20 会让 IC 序列空到 0 条，整个挖掘静默返回
# 「全部 IC=0 / 幸存 0」，看起来像「没找到 alpha」，实际是门槛把样本全滤掉了。
MIN_XSEC_NAMES = 8


def rank_ic_fast(factor: pd.DataFrame, fwd: pd.DataFrame,
                 mask: pd.DataFrame | None = None,
                 min_names: int = MIN_XSEC_NAMES) -> pd.Series:
    """向量化逐日 Spearman IC。"""
    f, r = _prep(factor, fwd, mask)
    fr = f.rank(axis=1)
    rr = r.rank(axis=1)
    # 中心化
    fz = fr.sub(fr.mean(axis=1), axis=0)
    rz = rr.sub(rr.mean(axis=1), axis=0)
    num = (fz * rz).sum(axis=1)
    den = np.sqrt((fz ** 2).sum(axis=1) * (rz ** 2).sum(axis=1))
    ic = num / den.replace(0, np.nan)
    # 样本数门槛
    n_ok = (f.notna() & r.notna()).sum(axis=1)
    ic = ic.where(n_ok >= int(min_names))
    return ic.dropna()


def _effective_groups(f: pd.DataFrame, n_groups: int) -> int:
    """按横截面宽度收窄分组数，保证每组仍有 ≥4 只。

    2026-11-01：crypto 池只有 17 只（每日 16~17 只可交易），而分层闸写死
    ``n >= n_groups*4 = 20``，于是每个候选的多空收益都是空序列 →
    ``oos_ls_sharpe=NaN``，通道每轮 FDR 过 200+ 条却永远 0 幸存。
    这里按「每日可用标的中位数」决定组数：17 只 → 4 组（门槛 16），
    美股 600+ 只 → 仍是 5 组（门槛 20），美股结果逐值不变。

    注意：宽度取自 **fwd 面板**（宇宙宽度），不是因子覆盖数。否则一个只覆盖
    十几只美股的低频因子会跟着被降组，悄悄改变美股既有口径。
    """
    n_med = f.notna().sum(axis=1).median()
    if not np.isfinite(n_med) or n_med <= 0:
        return n_groups
    return int(min(n_groups, max(2, n_med // 4)))


def _layer_masks(factor: pd.DataFrame, fwd: pd.DataFrame, n_groups: int = 5,
                 mask: pd.DataFrame | None = None):
    """对齐 + 分层，返回 (f, r, top, bot, valid, long_ret, short_ret, gross)。"""
    f, r = _prep(factor, fwd, mask)
    g = _effective_groups(fwd, n_groups)
    # 逐日分位（基于排名）
    fr = f.rank(axis=1, method="first")
    n = f.notna().sum(axis=1)
    # 每组至少 4 只才认为当天可分层
    valid = n >= g * 4
    # 计算分位组：quantile of rank
    q = fr.div(n, axis=0)  # [0,1]
    group = np.floor(q * g).clip(upper=g - 1)

    top = group == (g - 1)
    bot = group == 0
    # 多头/空头等权收益
    r_filled = r
    long_ret = (r_filled.where(top).mean(axis=1))
    short_ret = (r_filled.where(bot).mean(axis=1))
    gross = (long_ret - short_ret).where(valid)
    return f, r, top, bot, valid, long_ret, short_ret, gross


def layered_ls_fast(factor: pd.DataFrame, fwd: pd.DataFrame, n_groups: int = 5,
                    mask: pd.DataFrame | None = None, cost_bps: float = 5.0):
    """向量化分层多空。返回 (gross日收益, 净日收益, 多头日收益, 平均换手)。"""
    f, r, top, bot, valid, long_ret, short_ret, gross = _layer_masks(
        factor, fwd, n_groups, mask)

    # 换手：这一天入选的集合 与 昨天 的差异比例。
    # 原实现用 Python 逐日建 set + 对称差，是整条回测路径的主要瓶颈
    # （占单候选耗时 70%+，实测 1.5~1.8s/次）。这里改成布尔矩阵的
    # 相邻行异或计数 —— 对同一 column 顺序的布尔数组，异或计数恰好等于
    # 集合对称差大小，因此结果逐值等价（已对 12 个算子验证：gross/net/long
    # 最大差 1e-17，换手率差 0）。
    # 分母沿用原实现：2 * max(len(当日 top), 1)。
    top_a = top.fillna(False).to_numpy()
    bot_a = bot.fillna(False).to_numpy()
    top_change = np.not_equal(top_a[1:], top_a[:-1]).sum(axis=1).astype(float)
    bot_change = np.not_equal(bot_a[1:], bot_a[:-1]).sum(axis=1).astype(float)
    denom = np.maximum(top_a.sum(axis=1)[1:].astype(float) * 2.0, 2.0)
    turns = (top_change + bot_change) / denom
    turnover = pd.Series(turns, index=f.index[1:])
    cost = cost_bps / 10000.0
    net = gross - turnover * cost * 2
    return gross.dropna(), net.dropna(), long_ret.dropna(), float(turnover.mean())
