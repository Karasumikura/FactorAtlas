"""复合因子：支持多算子嵌套，打开搜索空间。

单算子因子已被大量开发（动量、反转、波动…），真正的 alpha 常在
**因子交互**里：动量÷波动（风险调整动量）、反转×量能、价值×质量…

复合因子表达式（JSON）：
{
  "name": "risk_adj_momentum",
  "kind": "combo",
  "expr": {"op": "div", "left": {"op":"mom","params":{"n":120}},
           "right": {"op":"vol","params":{"n":60}}},
  "direction": 1,
  "rationale": "..."
}
算子：add, sub, mul, div, rank_add, rank_sub（秩变换后再组合）
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from factors.engine import OPS, factor_series_to_frame


def _eval(node: dict, panel: dict) -> pd.DataFrame | float:
    """递归求值表达式树。"""
    if not isinstance(node, dict):
        return float(node)
    op = node.get("op")
    # 组合算子
    if op in ("add", "sub", "mul", "div", "max", "min"):
        left = _eval(node["left"], panel)
        right = _eval(node["right"], panel)
        return _combine(op, left, right)
    if op in ("rank_add", "rank_sub", "rank_mul"):
        left = _eval(node["left"], panel)
        right = _eval(node["right"], panel)
        lr = _cross_rank(left)
        rr = _cross_rank(right)
        base = {"rank_add": "add", "rank_sub": "sub", "rank_mul": "mul"}[op]
        return _combine(base, lr, rr)
    if op == "neg":
        return -_eval(node["left"], panel)
    if op == "abs":
        return _eval(node["left"], panel).abs()
    # 基础算子（叶子）
    if op in OPS:
        params = node.get("params", {}) or {}
        return OPS[op](panel, **params)
    raise ValueError(f"未知算子: {op}")


def _to_frame(x) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x
    return x


def _combine(op: str, a, b):
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        a = a if isinstance(a, (int, float)) else _to_frame(a)
        b = b if isinstance(b, (int, float)) else _to_frame(b)
        if op == "add": return a + b
        if op == "sub": return a - b
        if op == "mul": return a * b
        if op == "div": return a / b if b != 0 else a
        if op == "max": return max(a, b)
        if op == "min": return min(a, b)
    a = a.align(b, join="outer")
    x, y = a
    if op == "add": return x + y
    if op == "sub": return x - y
    if op == "mul": return x * y
    if op == "div": return x / y.replace(0, np.nan)
    if op == "max": return x.where(x > y, y)
    if op == "min": return x.where(x < y, y)
    raise ValueError(op)


def _cross_rank(df: pd.DataFrame) -> pd.DataFrame:
    return df.rank(axis=1, pct=True) - 0.5


# 预置的经典复合因子（有文献支撑）
CLASSIC_COMBOS = [
    {"name": "risk_adj_mom_120", "op": "div",
     "left": {"op": "mom", "params": {"n": 120}},
     "right": {"op": "vol", "params": {"n": 60}}, "direction": 1,
     "rationale": "风险调整动量：动量除以波动率，夏普类信号"},
    {"name": "mom_x_volume", "op": "mul",
     "left": {"op": "mom", "params": {"n": 120}},
     "right": {"op": "volume_ratio", "params": {"n": 20}}, "direction": 1,
     "rationale": "带量能确认的动量，量价齐升更可靠"},
    {"name": "reversal_x_volume", "op": "mul",
     "left": {"op": "reversal", "params": {"n": 5}},
     "right": {"op": "volume_ratio", "params": {"n": 20}}, "direction": 1,
     "rationale": "放量反转：超跌且放量，反弹概率更高"},
    {"name": "mom_sub_reversal", "op": "sub",
     "left": {"op": "mom", "params": {"n": 120}},
     "right": {"op": "reversal", "params": {"n": 5}}, "direction": 1,
     "rationale": "长期动量减短期反转，分离趋势与噪音"},
    {"name": "trend_x_lowvol", "op": "div",
     "left": {"op": "trend_strength", "params": {"n": 120}},
     "right": {"op": "vol", "params": {"n": 60}}, "direction": 1,
     "rationale": "趋势强度除以波动，稳健趋势"},
    {"name": "value_x_quality", "op": "rank_add",
     "left": {"op": "value_earnings_yield", "params": {}},
     "right": {"op": "quality_roe", "params": {}}, "direction": 1,
     "rationale": "价值+质量：便宜且赚钱的公司"},
    {"name": "price_pos_div_vol", "op": "div",
     "left": {"op": "price_position", "params": {"n": 120}},
     "right": {"op": "vol", "params": {"n": 60}}, "direction": 1,
     "rationale": "相对位置除以波动"},
    {"name": "amihud_x_reversal", "op": "mul",
     "left": {"op": "amihud", "params": {"n": 20}},
     "right": {"op": "reversal", "params": {"n": 5}}, "direction": 1,
     "rationale": "非流动性放大的反转，小市值超跌"},
]


def build_combo_frame(panel: dict, spec: dict) -> pd.DataFrame:
    """构造复合因子 [date x symbol]。"""
    expr = spec.get("expr") or spec
    val = _eval(expr, panel)
    if isinstance(val, (int, float)):
        raise ValueError("复合因子求值为标量")
    return val
