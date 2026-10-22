"""防过拟合元层：当「持续挖因子」时，防止把噪声当成 alpha。

为什么必须有这一层：
  单次挖掘里已有 BH-FDR + 样本外(OOS) + 分年 + 成本敏感性。但【持续】挖掘会
  引入新的过拟合通道 —— 你每跑一次就可能对着同一段 OOS 做选择，跑 50 次后
  OOS 事实上已经变成 IS（selection by repeated reuse）。这是量化最隐蔽的坑。

本模块提供三道业界标准护栏：

  1) Deflated Sharpe Ratio (DSR, López de Prado)
     —— 按【搜索次数 N】给 Sharpe 打折。搜得越多，达到同样 Sharpe 越可能是运气。
        输出 deflated_p：该 Sharpe 在「试了 N 次」的前提下仍显著 > 0 的概率。

  2) PBO（Probability of Backtest Overfitting, CSCV）
     —— 把时间序列切 S 块，穷举所有 S/2 子集做 IS、余下做 OOS，看「IS 最优」
        配置在 OOS 上落到中位数以下的频率。PBO 高 = 回测在过拟合。

  3) 试验登记册（Trial Registry）
     —— 累计记录每次挖了哪些候选、评分如何，得出【累计试验数 N_total】，
        供 DSR 使用；并在把某因子接入交易前检查它是否已在历史里被反复挑过。

所有函数都是纯计算，不依赖网络，便于单测。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import numpy as np

PROJ = Path(__file__).resolve().parent.parent
TRIAL_FILE = PROJ / "data" / "factor_trials.json"

# 欧拉-马歇罗尼常数（DSR 公式用）
_EULER = 0.5772156649015329


# ---------------------------------------------------------------------------
# 1) Deflated Sharpe Ratio
# ---------------------------------------------------------------------------
def _sharpe(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 2 or x.std(ddof=1) == 0:
        return 0.0
    return float(x.mean() / x.std(ddof=1) * math.sqrt(252))


def _skew_kurt(x: np.ndarray) -> tuple[float, float]:
    x = x[np.isfinite(x)]
    if len(x) < 4 or x.std(ddof=1) == 0:
        return 0.0, 3.0
    m = x.mean()
    s = x.std(ddof=1)
    sk = float(np.mean(((x - m) / s) ** 3))
    ku = float(np.mean(((x - m) / s) ** 4))
    return sk, ku


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def deflated_sharpe(returns, n_trials: int, sr_benchmark: float | None = None,
                    periods_per_year: int = 252) -> dict:
    """计算 Deflated Sharpe Ratio (Bailey & López de Prado, 2014)。

    returns      : 策略期收益序列（至少几十个点）
    n_trials     : 累计搜索过的配置数 N（越大惩罚越重）
    sr_benchmark : 基准 Sharpe（各 trial 的方差来自它）。None 时按 0 处理，
                   此时变为「在试了 N 次后 Sharpe 仍显著>0」的检验。

    返回 {sr, sr_ann, dsr, deflated_p, n_trials, benchmark}。
    """
    x = np.asarray(returns, dtype=float)
    x = x[np.isfinite(x)]
    T = len(x)
    if T < 20:
        return {"sr": 0.0, "sr_ann": 0.0, "dsr": 0.0, "deflated_p": 0.0,
                "n_trials": int(n_trials), "benchmark": 0.0, "n_obs": T}

    sr = _sharpe(x)
    sk, ku = _skew_kurt(x)
    # 期频 Sharpe（未年化），DSR 公式在这一尺度上
    sr_p = sr / math.sqrt(periods_per_year)

    # 期望最大 Sharpe（E[max] = sr_bench * ((1-γ)Z^{-1}(1-1/N) + γ Z^{-1}(1-1/(N e))))
    N = max(int(n_trials), 1)
    b = float(sr_benchmark or 0.0)
    if N == 1:
        sr0 = 0.0
    else:
        z1 = _norm_ppf(1.0 - 1.0 / N)
        z2 = _norm_ppf(1.0 - 1.0 / (N * math.e))
        sr0_ann = b * ((1 - _EULER) * z1 + _EULER * z2)
        sr0 = sr0_ann / math.sqrt(periods_per_year)  # 折到期频

    # Sharpe 估计量的标准误（含偏度/峰度修正）
    denom = max(1e-12, (1.0 - sk * sr_p + (ku - 1.0) / 4.0 * sr_p ** 2))
    se = math.sqrt(denom / (T - 1))
    z = (sr_p - sr0) / se
    dsr = _norm_cdf(z)          # 概率：SR > 期望最大值
    return {
        "sr": round(sr, 4), "sr_ann": round(sr, 4),
        "sr_period": round(sr_p, 6),
        "dsr": round(dsr, 4), "deflated_p": round(1.0 - dsr, 4),
        "n_trials": N, "benchmark": round(b, 4),
        "skew": round(sk, 3), "kurtosis": round(ku, 3),
        "n_obs": T,
    }


def _norm_ppf(p: float) -> float:
    """标准正态分位函数（Acklam 近似，够用且无需 scipy）。"""
    if p <= 0:
        return -8.0
    if p >= 1:
        return 8.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ---------------------------------------------------------------------------
# 2) PBO —— Combinatorially Symmetric Cross-Validation
# ---------------------------------------------------------------------------
def pbo_cscv(perf_matrix: np.ndarray, n_splits: int = 8) -> dict:
    """Probability of Backtest Overfitting（Bailey et al. 2016, CSCV）。

    perf_matrix: shape (T, N) —— T 个期、N 个候选配置的【期收益】。
      每列一个配置，行是时间。用同一段时间切块才能反映真实 IS/OOS 关系。

    做法：把 T 切成 n_splits 个连续块（须为偶数）；枚举所有 C(S, S/2) 种
    「一半做 IS、一半做 OOS」的划分；每次在 IS 上挑 Sharpe 最高的配置，
    看它在 OOS 上的排名是否落到后半（logit<0）。PBO = 该事件占比。

    返回 {pbo, n_combos, n_configs, n_splits, logit_mean}。
    """
    M = np.asarray(perf_matrix, dtype=float)
    if M.ndim != 2:
        return {"pbo": None, "error": "perf_matrix 必须是 2D"}
    T, N = M.shape
    S = int(n_splits)
    if S % 2 != 0:
        S -= 1
    if S < 4 or N < 2 or T < S * 2:
        return {"pbo": None, "error": f"数据不足 (T={T},N={N},S={S})",
                "n_configs": N, "n_splits": S}

    blocks = np.array_split(np.arange(T), S)
    combos = list(combinations(range(S), S // 2))
    logits = []
    for is_idx in combos:
        oos_idx = [j for j in range(S) if j not in is_idx]
        is_rows = np.concatenate([blocks[j] for j in is_idx])
        oos_rows = np.concatenate([blocks[j] for j in oos_idx])
        is_sr = _col_sharpe(M[is_rows])
        oos_sr = _col_sharpe(M[oos_rows])
        best = int(np.nanargmax(is_sr))
        # best 配置在 OOS 上的相对排名（0=最差,1=最好）
        ranks = _rank_pct(oos_sr)
        w = ranks[best]
        w = min(max(w, 1e-6), 1 - 1e-6)
        logits.append(math.log(w / (1.0 - w)))
    logits = np.array(logits)
    pbo = float(np.mean(logits <= 0))
    return {
        "pbo": round(pbo, 4), "n_combos": len(combos),
        "n_configs": N, "n_splits": S,
        "logit_mean": round(float(np.mean(logits)), 4),
    }


def _col_sharpe(block: np.ndarray) -> np.ndarray:
    mu = np.nanmean(block, axis=0)
    sd = np.nanstd(block, axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sr = np.where(sd > 0, mu / sd, np.nan)
    return sr


def _rank_pct(v: np.ndarray) -> np.ndarray:
    """返回每个元素的分位（0..1）。NaN 排到最差。"""
    n = len(v)
    order = np.argsort(np.where(np.isfinite(v), v, -np.inf))
    ranks = np.empty(n)
    ranks[order] = np.arange(1, n + 1)
    return ranks / (n + 1.0)


# ---------------------------------------------------------------------------
# 3) 试验登记册：累计搜索次数，供 DSR 使用，并防止 OOS 复用
# ---------------------------------------------------------------------------
def load_trials() -> dict:
    if TRIAL_FILE.exists():
        try:
            return json.loads(TRIAL_FILE.read_text())
        except Exception:
            pass
    return {"total_trials": 0, "sessions": []}


def save_trials(d: dict) -> None:
    TRIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    d["updated_at"] = datetime.now(timezone.utc).isoformat()
    TRIAL_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2,
                                     default=str))


def record_session(n_candidates: int, n_survivors: int, market: str = "us",
                   note: str = "", extra: dict | None = None) -> dict:
    """登记一次挖掘会话。返回累计试验数（累计候选总数）。

    「累计候选总数」是 DSR 里 N 的保守下界估计 —— 每挖一个候选就是一次试验。
    """
    d = load_trials()
    d["total_trials"] = int(d.get("total_trials", 0)) + int(n_candidates)
    sess = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "market": market, "n_candidates": int(n_candidates),
        "n_survivors": int(n_survivors), "note": note,
        "cumulative_trials_after": d["total_trials"],
    }
    if extra:
        sess.update(extra)
    d.setdefault("sessions", []).append(sess)
    save_trials(d)
    return d


def cumulative_trials() -> int:
    return int(load_trials().get("total_trials", 0))


# ---------------------------------------------------------------------------
# 组合入口：给幸存因子打「防过拟合分」
# ---------------------------------------------------------------------------
def overfit_verdict(returns, n_trials: int, perf_matrix: np.ndarray | None = None,
                    n_splits: int = 8, dsr_floor: float = 0.95,
                    pbo_ceiling: float = 0.5) -> dict:
    """综合判定：DSR 是否达标、PBO 是否可接受。

    dsr_floor  : deflated_p 需 < (1-dsr_floor)？不 —— 直接要求 dsr 概率 >= dsr_floor，
                 即「试了 N 次后 Sharpe 仍显著为正」的把握 >= 95%。
    pbo_ceiling: PBO 超过该值即判为过拟合（默认 0.5）。
    """
    dsr = deflated_sharpe(returns, n_trials)
    out = {"dsr": dsr, "n_trials": int(n_trials)}
    ok = dsr["dsr"] >= dsr_floor
    if perf_matrix is not None:
        p = pbo_cscv(perf_matrix, n_splits=n_splits)
        out["pbo"] = p
        if p.get("pbo") is not None:
            ok = ok and (p["pbo"] <= pbo_ceiling)
    out["pass"] = bool(ok)
    out["criteria"] = {"dsr_floor": dsr_floor, "pbo_ceiling": pbo_ceiling}
    return out


def _demo() -> None:
    """自检：纯噪声 vs 真信号，DSR/PBO 应当区分。"""
    rng = np.random.default_rng(0)
    T, N = 400, 40
    # 噪声：所有配置都是零均值
    noise = rng.normal(0, 0.01, size=(T, N))
    # 真信号：第 0 列有正漂移
    real = rng.normal(0, 0.01, size=(T, N))
    real[:, 0] += 0.0015
    print("== 纯噪声 ==")
    print("  PBO:", pbo_cscv(noise))
    print("  DSR(col0, N=40):", deflated_sharpe(noise[:, 0], 40))
    print("== 含真信号 ==")
    print("  PBO:", pbo_cscv(real))
    print("  DSR(col0, N=40):", deflated_sharpe(real[:, 0], 40))


if __name__ == "__main__":
    _demo()
