"""系统性因子搜索：参数扫描 + 全算子遍历。

和 LLM 提因子互补：
- 扫描覆盖整个算子×参数空间，不漏。
- 结果全部进同一个多重检验校正池，避免选择偏差。
"""
from __future__ import annotations

from factors.engine import OPS


# 参数字段名（决定扫描怎么给参数）
def _param_grid(op: str) -> list[dict]:
    """给每个算子生成合理的参数网格。"""
    if op == "mom":
        return [{"n": n, "skip": s} for n in (20, 60, 120, 250) for s in (0, 20)]
    if op in ("ret", "reversal"):
        return [{"n": n} for n in (1, 3, 5, 10, 20, 60)]
    if op in ("vol", "turnover", "volume_ratio", "volatility_ratio", "skew",
              "kurt", "amihud", "downside_vol", "upside_vol", "vol_asym",
              "turnover_chg", "turnover_std", "amount_share", "illiq"):
        return [{"n": n} for n in (10, 20, 60, 120)]
    if op in ("vwap_dev", "gap", "ret_skew_fast", "overbought"):
        return [{"n": n} for n in (3, 5, 10, 20, 60)]
    if op in ("close_to_high",):
        return [{"n": n} for n in (20, 60, 120, 250)]
    if op in ("volume_price_corr", "downside_days"):
        return [{"n": n} for n in (10, 20, 60, 120)]
    if op in ("price_position", "max_drawdown"):
        return [{"n": n} for n in (20, 60, 120, 250)]
    if op in ("ma_gap", "trend_strength"):
        return [{"n": n} for n in (10, 20, 60, 120)]
    if op == "hl_range":
        return [{"n": n} for n in (5, 10, 20, 60)]
    if op.startswith("news_"):
        return [{"n": n} for n in (3, 5, 10, 20)]
    if op.startswith(("value_", "quality_", "fcf_", "low_")):
        return [{}]
    return [{}]


def generate_grid(ops: list[str] | None = None) -> list[dict]:
    """生成全网格候选（含两个方向）。"""
    out = []
    for op in (ops or list(OPS.keys())):
        for params in _param_grid(op):
            for direction in (1, -1):
                out.append({
                    "name": f"{op}_{'_'.join(f'{k}{v}' for k, v in params.items()) or 'd'.join(map(str,[direction]))}",
                    "op": op, "params": params, "direction": direction,
                    "rationale": f"系统扫描 {op}{params} dir={direction}",
                })
    return out
