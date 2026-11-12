"""AlphaEval 五维评估（借鉴 dhj74/AlphaEval, KDD 2026, MIT）。

原仓库依赖 Qlib 的 A 股数据格式，直接搬不动；这里只复用论文与
backtest/modeltester.py 里定义的五个可计算指标，用本项目自己的 panel 实现：

  PPS  预测力     逐日截面 rank IC 的均值
  RRE  时序稳定   1/(1+KL)，KL 是相邻两日因子 rank 分布的散度
  PFS  扰动鲁棒   注入高斯 / t 噪声后，与原始因子逐日截面相关的均值
  DIV  多样性     因子集合日度截面均值序列的协方差特征值归一化熵
  LOGIC 金融逻辑  结构规则分（执行机零 LLM，不调用模型打分）

五维里 DIV 是集合级，其余四维逐因子。全程纯 numpy/pandas，
不新增候选 → 不会抬 overfit_guard 的累计试验数 N，可在冷却期跑。

来源：
  https://arxiv.org/abs/2508.13174  (AlphaEval, KDD 2026)
  https://github.com/dhj74/AlphaEval  (MIT)
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from factors.engine import factor_series_to_frame

NOISE_VAR = 0.001          # 与原仓库 noise_proc.NoiseInjection 一致
LLM_LOGIC_PATH = Path("/opt/quant-lab/state/pool/logic_scores.json")
_LLM_LOGIC_CACHE = None
T_DOF = 3                  # 厚尾扰动
try:
    from factors.engine import OPS as _ENGINE_OPS, FUND_OPS, XBRL_OPS
    OPS_ALLOWED = set(_ENGINE_OPS) | set(FUND_OPS) | set(XBRL_OPS)
except Exception:  # noqa: BLE001
    OPS_ALLOWED = {"mom", "vol", "turnover", "reversal", "hl_range"}


def _daily_rank_ic(factor: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """逐日截面 Spearman IC，向量化（原来 1469 次 Python 循环要 5 分钟）。"""
    common = factor.index.intersection(fwd.index)
    a = factor.loc[common].rank(axis=1)
    b = fwd.loc[common].rank(axis=1)
    am = a.sub(a.mean(axis=1), axis=0)
    bm = b.sub(b.mean(axis=1), axis=0)
    num = (am * bm).sum(axis=1, min_count=1)
    den = np.sqrt((am ** 2).sum(axis=1) * (bm ** 2).sum(axis=1))
    return (num / den.replace(0, np.nan)).dropna()


def _rre(factor: pd.DataFrame) -> float:
    """AlphaEval RRE：1/(1+平均 KL)，越接近 1 越稳定。"""
    ranks = factor.rank(axis=1)
    probs = ranks.div(ranks.sum(axis=1).replace(0, np.nan), axis=0).dropna(how="all")
    if len(probs) < 2:
        return float("nan")
    prev = probs.shift(1).iloc[1:]
    cur = probs.iloc[1:]
    eps = 1e-8
    kl = (cur * np.log((cur + eps) / (prev + eps))).sum(axis=1)
    kl = kl.replace([np.inf, -np.inf], np.nan).dropna()
    if kl.empty:
        return float("nan")
    # 浮点误差可能让 KL 出现极小负值，导致 RRE > 1（实测出现过 1.05492）
    return float(1.0 / (1.0 + max(0.0, float(kl.mean()))))


def _pfs(factor: pd.DataFrame, kind: str = "gauss") -> float:
    """注入噪声后逐日截面相关均值（向量化）。"""
    sigma = math.sqrt(NOISE_VAR)
    rng = np.random.default_rng(20261109)
    if kind == "gauss":
        noise = rng.normal(0.0, sigma, size=factor.shape)
    else:
        raw = rng.standard_t(T_DOF, size=factor.shape)
        noise = raw * sigma * math.sqrt((T_DOF - 2) / T_DOF)
    noisy = pd.DataFrame(factor.values * (1.0 + noise),
                         index=factor.index, columns=factor.columns)
    a = factor.sub(factor.mean(axis=1), axis=0)
    b = noisy.sub(noisy.mean(axis=1), axis=0)
    num = (a * b).sum(axis=1)
    den = np.sqrt((a ** 2).sum(axis=1) * (b ** 2).sum(axis=1))
    corrs = (num / den.replace(0, np.nan)).dropna()
    return float(corrs.mean()) if len(corrs) else float("nan")


def _diversity(series_by_name: dict[str, pd.Series]) -> float:
    """协方差矩阵特征值的归一化熵（集合级）。

    ⚠️ 用**日度 IC 序列**而非「截面均值序列」做输入：本项目实测过，
    因子值的 rank-mean 序列会被市场共同漂移主导，把不同因子全判成
    同一个家族（skill: screening-bugs-and-honest-evaluation.md）。
    IC 序列反映每日选股能力，区分度才对。
    """
    names = [n for n, s in series_by_name.items() if s is not None and len(s) > 2]
    if len(names) < 2:
        return float("nan")
    raw = pd.DataFrame({n: series_by_name[n] for n in names})
    # 先按「非 NaN 天数」挑数据最全的 20 列；不同来源的因子 IC 序列起点
    # 不一样，直接 dropna 会把行数削到比因子数还少，熵恒为 nan。
    counts = raw.notna().sum().sort_values(ascending=False)
    names = list(counts.index[:20])
    mat = raw[names].dropna()
    while mat.shape[1] > 2 and mat.shape[0] < mat.shape[1] + 1:
        mat = mat.iloc[:, :-1]
    if mat.shape[0] < mat.shape[1] + 1:
        return float("nan")
    C = np.cov(mat.values, rowvar=False)
    eigs = np.clip(np.linalg.eigvalsh(C), 0.0, None)
    total = eigs.sum()
    if total <= 0:
        return float("nan")
    p = eigs[eigs > 0] / total
    return float(-(p * np.log(p)).sum() / np.log(len(names)))


def _load_llm_logic() -> dict:
    """读控制机离线跑出的 LLM 金融逻辑分；缺失/损坏都返回空字典。"""
    global _LLM_LOGIC_CACHE
    if _LLM_LOGIC_CACHE is not None:
        return _LLM_LOGIC_CACHE
    out = {}
    try:
        d = json.loads(LLM_LOGIC_PATH.read_text())
        for x in d.get("scores", []):
            v = x.get("llm_logic")
            if v is not None:
                out[str(x.get("name"))] = float(v)
    except Exception:  # noqa: BLE001
        out = {}
    _LLM_LOGIC_CACHE = out
    return out


def _logic(spec: dict) -> float:
    """结构规则分（不调 LLM）：算子、参数、方向、表达式复杂度。"""
    score = 100.0
    op = spec.get("op")
    if op not in OPS_ALLOWED:
        return 0.0
    params = spec.get("params") or {}
    n = params.get("n")
    if n is not None and not isinstance(n, (int, float)):
        score -= 40
    elif n is not None and not (2 <= float(n) <= 500):
        score -= 40
    elif n is not None and float(n) in (5, 10, 20, 60, 120):
        score -= 10          # 历史高度拥挤的参数档
    if "expr" in spec or "left" in spec:
        score -= 15          # 复合表达式：可解释性下降
    if spec.get("direction") not in (1, -1):
        score -= 30
    # 未来函数风险：任何含 future/lead/lookahead 命名的算子直接否决
    if any(k in op.lower() for k in ("future", "lead", "lookahead")):
        return 0.0
    return max(0.0, score)


def evaluate_set(panel: dict, specs: list[dict], hold: int = 1,
                 max_factors: int = 20) -> dict:
    """对一组因子算五维。返回逐因子结果 + 集合级 diversity。"""
    close = panel["c"]
    fwd = close.pct_change(hold).shift(-hold)
    out, ic_by_name = [], {}
    for spec in specs[:max_factors]:
        name = spec.get("name") or spec.get("op") or "f"
        try:
            f = factor_series_to_frame(panel, spec)
            ic = _daily_rank_ic(f, fwd)
            override = _load_llm_logic()
            logic, logic_source = _logic(spec), "structural"
            if name in override:
                logic, logic_source = override[name], "llm"
            row = {
                "name": name,
                "pps_ic": round(float(ic.mean()), 5) if len(ic) else None,
                "pps_icir": round(float(ic.mean() / ic.std()), 5)
                            if len(ic) > 2 and ic.std() > 0 else None,
                "rre": round(_rre(f), 5),
                "pfs_gauss": round(_pfs(f, "gauss"), 5),
                "pfs_t": round(_pfs(f, "t"), 5),
                "logic": logic,
                "logic_source": logic_source,
            }
            ic_by_name[name] = ic
        except Exception as exc:  # noqa: BLE001
            row = {"name": name, "error": str(exc)[:100]}
        out.append(row)
    return {
        "n_factors": len(out),
        "hold": hold,
        "diversity": _diversity(ic_by_name),
        "factors": out,
    }


def evaluate_library(panel: dict, library_path: str | Path,
                     hold: int = 1) -> dict:
    """便捷入口：直接对正式因子库跑五维。"""
    d = json.loads(Path(library_path).read_text())
    specs = d if isinstance(d, list) else list(d.get("factors", {}).values())
    return evaluate_set(panel, specs, hold=hold)
