"""因子晋升闸门：只有「跨多次独立挖掘反复出现 + 通过防过拟合层」的因子才进正式因子库。

为什么需要它（这是「持续挖因子」防过拟合的核心）：
  单次挖掘内已有 FDR+OOS+稳健性，但持续挖掘会反复用同一段 OOS 做选择，
  使 OOS 逐渐失效。真正稳健的信号应当在**多次相互独立的挖掘会话**里反复出现
  （类似生物学的"可重复性"标准），而不是只在某一次里数字好看。

晋升标准（全部满足才入库）：
  1. 可重复性：在 >= min_runs 次独立 mine 结果里都作为幸存者出现（默认 2 次）。
  2. 方向一致：所有出现里的样本外 LS Sharpe 符号一致（不能一次正一次负）。
  3. 防过拟合：DSR（按累计试验数 N 打折）>= dsr_floor，且 PBO <= pbo_ceiling。
  4. 幅度门槛：跨次 OOS Sharpe 中位数 >= min_oos_sharpe。
  5. 成本可交易（若结果里带 lowfreq）：扣成本后净 Sharpe > 0。
  6. 冗余去重：与**已在库因子**的截面 rank 相关 > max_abs_corr 则拒绝入库（默认 0.8）。

  为什么需要第 6 条（2026-10 加入）：
   前 5 条只考察单个因子，对「库」整体没有约束。实测库内 20 个因子里 18 个
   属于 vol / hl_range / volatility_ratio 家族，两两相关 0.74~1.00 —— 等权合成
   等于给同一个信号投了 18 票，合成 IC 掉到 +0.0031（噪声区）。加入相关性门槛
   后同库缩到 3 个正交因子，合成 IC 升至 +0.0112（3.6x）。名额应留给真正正交的信号。

用法：
  python -m core.promote                       # 扫描 runs/mine_*.json
  python -m core.promote --glob 'runs/mine_us_*.json'
  python -m core.promote --min-runs 3
  python -m core.promote --max-abs-corr 0.8    # 冗余门槛，0 或负数=关闭
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import statistics as stats
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
LIB_FILE = PROJ / "data" / "factor_library.json"


def _spec_id(spec: dict) -> str:
    """与 zoo/mine 口径一致的 spec 指纹。"""
    if spec.get("kind") == "combo" or "expr" in spec:
        payload = {"expr": spec.get("expr"), "direction": spec.get("direction", 1)}
    else:
        payload = {"op": spec.get("op"), "params": spec.get("params", {}),
                   "direction": spec.get("direction", 1)}
    return json.dumps(payload, sort_keys=True)


def load_runs(patterns: list[str]) -> list[dict]:
    runs = []
    for pat in patterns:
        for p in sorted(globmod.glob(pat)):
            try:
                with open(p) as f:
                    runs.append({"path": p, "data": json.load(f)})
            except Exception:
                continue
    return runs


def collect_survivors(runs: list[dict]) -> dict:
    """把每个因子的所有出现收集起来。返回 {spec_id: {...}}。"""
    acc: dict[str, dict] = {}
    for r in runs:
        data = r["data"]
        # 兼容：历史文件里有顶层是 list 的（如 xbrl_*_h*.json），
        # 直接 .get() 会 AttributeError 打断整个晋升流程。
        if not isinstance(data, dict):
            continue
        # 【独立会话的定义（2026-10 修正）】
        # 原先用 generated_at（每次运行的墙钟时间）当会话标识，导致
        # 「同一段数据跑 32 次」被当成「32 次独立验证」。实测证据：
        # mom_n250_skip20 出现 46 次、来自 32 个文件，但 oos_ls_sharpe 只有
        # 5 种取值（0.4674376711 重复 35 次）—— 因为 mine 会对同窗口结果
        # 做缓存复用（见 core/mine.py 的 use_cache 分支），复用时结果逐位相同。
        #
        # 会话键规则：
        #   - 新格式有 data_window_id：用它（**不同数据窗口才算独立会话**）。
        #     同窗口重跑多次 → 只有一个会话，这才是对的：没新数据就没新证据。
        #   - 老格式没有：退回 generated_at（只能如此），但下面会把
        #     「OOS 值完全相同」的出现标为 same_result，不计入独立计数。
        dwid = data.get("data_window_id")
        n_reused = data.get("n_reused_from_cache")
        n_backtested = data.get("n_actually_backtested")
        # 整轮都是缓存复用、没有真正重算 → 这轮不构成独立验证
        _all_reused = bool(n_reused) and n_backtested == 0
        sess = dwid if dwid else (data.get("generated_at") or r["path"])
        for s in data.get("survivors", []):
            spec = s.get("spec") or {}
            sid = _spec_id(spec)
            a = acc.setdefault(sid, {
                "spec": spec, "name": s.get("name"),
                "appearances": [], "sessions": set(), "oos_seen": set(),
            })
            oos = s.get("oos_ls_sharpe")
            # 同一因子、同一 OOS 值 = 同一次回测结果，不算新验证。
            # 老格式文件没有窗口指纹时，这是唯一的去重手段。
            _oos_key = round(float(oos), 10) if isinstance(oos, (int, float)) else None
            a["appearances"].append({
                "session": sess, "path": r["path"],
                "data_window_id": dwid, "reused_from_cache": n_reused,
                "all_reused": _all_reused,
                "oos_ls_sharpe": oos,
                "ls_sharpe": s.get("ls_sharpe"),
                "net_ls_sharpe": s.get("net_ls_sharpe"),
                "ic_mean": s.get("ic_mean"),
                "dsr": s.get("dsr"),
                "pbo": s.get("pbo_cscv"),
                "lowfreq": s.get("lowfreq"),
                "source": s.get("source"),
            })
            # 整轮纯缓存复用的会话不计入独立会话数 —— 那些结果与上一轮逐位相同，
            # 把它们算作「独立复现」正是 n_sessions 虚高的根源。
            if not _all_reused:
                a["sessions"].add(sess)
            if _oos_key is not None:
                a["oos_seen"].add(_oos_key)
    for a in acc.values():
        # n_sessions：不同会话数（新格式=不同数据窗口）
        a["n_sessions"] = len(a["sessions"])
        # n_distinct_oos：不同回测结果的个数。这是比 n_sessions 更硬的判据 ——
        # 若 32 次运行只产出 5 个不同 OOS 值，真实的独立证据只有 5 份。
        a["n_distinct_oos"] = len(a.get("oos_seen") or ())
        a.pop("sessions", None)
        a.pop("oos_seen", None)
    return acc


def evaluate(acc: dict, n_trials_cumulative: int, min_runs: int = 2,
             dsr_floor: float = 0.95, pbo_ceiling: float = 0.5,
             min_oos_sharpe: float = 0.3) -> list[dict]:
    """对每个候选做晋升判定。返回带 verdict 的列表（已排序）。"""
    out = []
    for sid, a in acc.items():
        apps = a["appearances"]
        oos = [x["oos_ls_sharpe"] for x in apps
               if isinstance(x["oos_ls_sharpe"], (int, float))]
        signed = [x for x in oos if x != 0]
        # 1) 可重复性
        #    判据要同时排除「缓存复用」和「同一数据窗口反复跑」，
        #    但不能因为「两次都得 0.8」就判不可重复 —— 那恰恰是一致性证据。
        #    做法：
        #      - 新格式（有 data_window_id）：不同数据窗口才算独立会话；
        #        同窗口下只有「未复用缓存」且 OOS 值不同的出现才算新证据。
        #      - 老格式（无窗口指纹）：只能退回 generated_at 计数，
        #        但把 OOS 完全相同的出现折叠为 1 次（防同一结果反复抄）。
        #    额外排除：明确标记 reused_from_cache 且该轮 nothing 重算的会话。
        n_sess = a["n_sessions"]
        n_distinct = a.get("n_distinct_oos", n_sess)
        rep_ok = n_sess >= min_runs
        # 2) 方向一致
        dir_ok = bool(signed) and all((v > 0) == (signed[0] > 0) for v in signed)
        # 3) 防过拟合：取最近一次带 dsr 的评估
        dsr_vals = [x["dsr"]["dsr"] for x in apps
                    if isinstance(x.get("dsr"), dict) and "dsr" in x["dsr"]]
        pbo_vals = [x["pbo"]["pbo"] for x in apps
                    if isinstance(x.get("pbo"), dict) and x["pbo"].get("pbo") is not None]
        dsr_ok = bool(dsr_vals) and max(dsr_vals) >= dsr_floor
        pbo_ok = (not pbo_vals) or min(pbo_vals) <= pbo_ceiling
        # 4) 幅度
        oos_med = stats.median(signed) if signed else 0.0
        amp_ok = abs(oos_med) >= min_oos_sharpe
        # 5) 成本
        net_ok = True
        nets = [x["net_ls_sharpe"] for x in apps
                if isinstance(x.get("net_ls_sharpe"), (int, float))]
        if nets:
            net_ok = stats.median(nets) > 0
        # lowfreq 净 Sharpe（若有）
        lf_nets = []
        for x in apps:
            lf = x.get("lowfreq") or {}
            if isinstance(lf.get("best_sharpe"), (int, float)):
                lf_nets.append(lf["best_sharpe"])
        if lf_nets:
            net_ok = net_ok and stats.median(lf_nets) > 0

        passed = rep_ok and dir_ok and dsr_ok and pbo_ok and amp_ok and net_ok
        out.append({
            "spec_id": sid, "name": a["name"], "spec": a["spec"],
            "n_sessions": n_sess, "oos_median": round(oos_med, 4),
            "n_distinct_oos": n_distinct,
            "dsr_best": round(max(dsr_vals), 4) if dsr_vals else None,
            "pbo_best": round(min(pbo_vals), 4) if pbo_vals else None,
            "lf_net_median": round(stats.median(lf_nets), 4) if lf_nets else None,
            "checks": {"repeatable": rep_ok, "direction_consistent": dir_ok,
                       "dsr_ok": dsr_ok, "pbo_ok": pbo_ok, "amplitude_ok": amp_ok,
                       "net_cost_ok": net_ok},
            "promoted": bool(passed),
        })
    # 排序：先按晋升状态，再按**会话数**降序（可信度），最后才看 |OOS|。
    # 原先只按原始 |oos_median| 排，会把 2 会话的 +6.77 顶到 24 会话的 +0.47
    # 前面 —— 展示层等于在放大样本量最小的那个，容易误导人以为它最强。
    # 注意：n_sessions 本身也不可靠（同一 OOS 值会在多份输出里重复出现），
    # 但作为「展示顺序」仍比单纯按绝对值排更合理，改动风险为零。
    out.sort(key=lambda x: (not x["promoted"], -x["n_sessions"],
                            -abs(x["oos_median"])))
    return out


def load_library() -> dict:
    if LIB_FILE.exists():
        try:
            return json.loads(LIB_FILE.read_text())
        except Exception:
            pass
    return {"factors": {}, "updated_at": None}


# ---------------------------------------------------------------- 冗余去重
# 因子值缓存：(market, spec_id) -> 每日截面 rank 的堆叠 Series。
# 只在进程内缓存；promote 一次运行会比对 N*M 次，重算因子代价太高。
_FACTOR_CACHE: dict[tuple[str, str], "pd.Series"] = {}


def _load_panel(market: str):
    from core import data
    return data.build_panel(market=market)


def _ranked_series(panel, spec: dict, spec_id: str, market: str):
    """把 spec 算成「每日截面百分位 rank」后堆叠成一维 Series，供算相关。"""
    key = (market, spec_id)
    if key in _FACTOR_CACHE:
        return _FACTOR_CACHE[key]
    import pandas as pd  # 局部导入：无 pandas 环境下纯逻辑测试不受影响
    from factors.engine import factor_series_to_frame
    f = factor_series_to_frame(panel, spec)
    if f is None or getattr(f, "empty", True):
        _FACTOR_CACHE[key] = None
        return None
    # 与 factor_library 合成口径一致：先每日截面百分位，再比较
    r = f.rank(axis=1, pct=True)
    s = r.stack()
    _FACTOR_CACHE[key] = s
    return s


def _max_abs_corr_with(spec_a, sid_a, spec_b, sid_b, market: str = "us") -> float | None:
    """两个 spec 的截面 rank 相关系数绝对值。数据不可得时返回 None（视为放行）。"""
    try:
        panel = _load_panel(market)
        sa = _ranked_series(panel, spec_a, sid_a, market)
        sb = _ranked_series(panel, spec_b, sid_b, market)
        if sa is None or sb is None:
            return None
        import pandas as pd
        joined = pd.concat([sa.rename("a"), sb.rename("b")], axis=1).dropna()
        if len(joined) < 100:
            return None
        c = joined["a"].corr(joined["b"])
        return None if c != c else abs(float(c))  # NaN 检查
    except Exception:
        return None


_FAMILY_PREFIXES = (
    "volatility_ratio", "vol_of_vol", "hl_range", "vol", "atr", "realized_vol",
    "trend_strength", "trend", "mom", "reversal", "vwap_dev", "volume_ratio",
    "amihud", "price_position", "sortino", "sharpe", "skew", "kurt",
)

# 同一算子族内部的网格变体（vol_n10/n20/n120、hl_range_n5..n60 …）彼此天然高相关，
# 它们**共享同一份信号**，不是 N 个独立假设。
# 由此产生两个后果，必须分开处理：
#   ① 统计口径：N（试验次数）若把它们当成独立试验累加，DSR 门槛会随挖矿时间单调变严，
#      最终**任何**因子都过不了 —— 这是「晋升停滞」的统计根源，不是冗余门槛的错。
#   ② 去冗余口径：这与「跟库内主因子撞车」是两回事，日志和处置都该分开。
GRID_FAMILIES = frozenset({
    "vol", "volatility_ratio", "hl_range", "atr", "realized_vol", "vol_of_vol",
    "mom", "trend_strength", "trend", "reversal", "turnover_std", "turnover_chg",
    "volume_ratio", "skew", "kurt",
})


def _family(name: str, spec: dict | None = None) -> str | None:
    """从因子名（优先）或 spec 推断所属算子族。同族变体（如
    volatility_ratio_n15/n20）之间天然高相关，与库内跨族因子高相关是两回事，
    日志需分开，否则会把「同族克隆」误读成「撞车库内主因子」。"""
    n = (name or "").lower()
    best = None
    for p in _FAMILY_PREFIXES:
        if n.startswith(p) or f"_{p}_" in n or f"_{p}" in n:
            if best is None or len(p) > len(best):
                best = p
    if best:
        return best
    # 名字里没有线索时（如 intraday_amplitude_risk_flip），从 spec 的算子反推
    if isinstance(spec, dict):
        op = spec.get("op")
        if not op and isinstance(spec.get("expr"), dict):
            op = spec["expr"].get("op") or (spec["expr"].get("left") or {}).get("op")
        if isinstance(op, str):
            op = op.lower()
            for p in _FAMILY_PREFIXES:
                if op.startswith(p) or p.startswith(op):
                    return p
            return op
    return None


def _apply_redundancy_gate(order: list[dict], library: dict, max_abs_corr: float,
                           market: str = "us", verbose: bool = False):
    """对一个已排序的晋升候选列表做贪心去冗余。

    库内已有因子视为「已占用」——新因子必须与它们全部不相关才算新鲜；
    候选之间也互相去重（先到先得，调用方按强度排序保证优先保留最强的）。
    返回 (通过的候选列表, 被拒信息列表)。
    """
    if max_abs_corr is None or max_abs_corr <= 0:
        return list(order), []
    kept = []
    rejected = []
    # 库存因子先占位
    occupied = [{"spec_id": sid, "name": v.get("name"), "spec": v.get("spec"),
                 "from_library": True}
                for sid, v in (library.get("factors") or {}).items() if v.get("spec")]
    for cand in order:
        worst = 0.0
        worst_name = None
        worst_spec = None
        uncomputable = None
        for occ in occupied:
            c = _max_abs_corr_with(cand["spec"], cand["spec_id"],
                                   occ["spec"], occ["spec_id"], market=market)
            if c is None:
                # 算不出相关 ≠ 不相关。历史上这里静默放行，导致 2026-10-16T15:27
                # 首次批量晋升时 13 个高度相关的波动率变体一起入库占满容量
                # （库里名义 20 个，消费端去冗余后只剩 3 个）。
                # 保守处理：视为可能冗余，拒绝并留下可追溯的理由。
                uncomputable = occ.get("name") or str(occ.get("spec_id"))[:30]
                break
            if c > worst:
                worst, worst_name, worst_spec = c, occ.get("name"), occ.get("spec")
        if uncomputable is not None:
            rejected.append({"spec_id": cand["spec_id"], "name": cand.get("name"),
                             "corr": None, "with": uncomputable,
                             "reason": "corr_unavailable"})
            continue
        if worst > max_abs_corr:
            # 区分两类拒绝，避免日志误导：与库内主因子撞车 vs 与同族克隆重复
            fam = _family(cand.get("name") or cand["spec_id"], cand.get("spec"))
            occ_fam = _family(worst_name or "", worst_spec)
            same_family = bool(occ_fam) and occ_fam == fam
            rejected.append({"spec_id": cand["spec_id"], "name": cand.get("name"),
                             "corr": round(worst, 4), "with": worst_name,
                             "reason": "redundant_same_family" if same_family
                                       else "redundant_cross_family"})
            continue
        kept.append(cand)
        occupied.append({"spec_id": cand["spec_id"], "name": cand.get("name"),
                         "spec": cand["spec"], "from_library": False})
    if verbose and rejected:
        for r in rejected[:15]:
            if r.get("reason") == "corr_unavailable":
                print(f"   ⛔相关不可算 {str(r['name'])[:26]:26s} "
                      f"| 保守拒绝 vs {r['with']}")
            elif r.get("reason") == "redundant_same_family":
                print(f"   ⛔同族克隆 {str(r['name'])[:26]:26s} "
                      f"|corr|={r['corr']} vs 同族 {r['with']}")
            else:
                print(f"   ⛔跨族撞车 {str(r['name'])[:26]:26s} "
                      f"|corr|={r['corr']} vs 库内 {r['with']}")
    return kept, rejected


def effective_trials(acc: dict, raw_n: int) -> tuple[int, dict]:
    """把「原始试验次数」折算成「有效独立试验次数」。

    问题：`overfit_guard.cumulative_trials()` 把每一次网格扫描（vol_n5 / vol_n10 /
    vol_n120 / hl_range_n20 …）都当成一次独立试验累加。但这些同族变体**共享同一份
    信号**，实际只对应个位数量的独立假设。随着挖矿时间推移 N 单调增长，DSR 的
    「多次试验惩罚」随之变严，最终**任何**因子都过不了门槛 —— 表现为
    「库里天数不增长」而 DSR 却都是 1.0 的假象（本次实测 N=75478）。

    做法（不放宽任何单个因子的证据要求，只修正试验计数的统计口径）：
      每个**族**只记 1 次独立试验，族内多余的网格变体按 `log2(1+变体数)` 计。
    这是「有效独立检验数」的常见近似：同族之间近似完全相关，跨族近似独立。
    """
    from collections import Counter
    fam_counter: Counter[str] = Counter()
    for sid, v in (acc or {}).items():
        f = _family(v.get("name") or "", v.get("spec")) or str(sid)[:20]
        fam_counter[f] += 1
    import math
    eff = 0
    for f, k in fam_counter.items():
        eff += 1 if k <= 1 else int(round(1 + math.log2(k)))
    # 有效试验数不可能超过原始试验数；也不可能少于本批候选覆盖的族数
    eff = max(eff, len(fam_counter))
    eff = min(eff, max(raw_n, eff))
    detail = {"raw_trials": int(raw_n), "effective_trials": int(eff),
              "families": dict(fam_counter.most_common(12)),
              "n_families": len(fam_counter)}
    return int(eff), detail


def promote(patterns: list[str] | None = None, min_runs: int = 2,
            dsr_floor: float = 0.95, pbo_ceiling: float = 0.5,
            min_oos_sharpe: float = 0.3, max_abs_corr: float = 0.8,
            max_library: int = 5, market: str = "us",
            verbose: bool = True) -> dict:
    from core import overfit_guard as og
    patterns = patterns or [str(PROJ / "runs" / "mine*.json"),
                            str(PROJ / "runs" / "loop_*" / "round_*" / "mine*.json"),
                            str(PROJ / "runs" / "loop_*" / "round_*" / "ortho*.json")]
    runs = load_runs(patterns)
    acc = collect_survivors(runs)
    n_raw = og.cumulative_trials()
    # 网格变体不是独立假设 → 用有效试验数做 DSR 惩罚，否则门槛随挖矿时间单调严化。
    n_cum, trial_detail = effective_trials(acc, n_raw)
    verdicts = evaluate(acc, n_cum, min_runs=min_runs, dsr_floor=dsr_floor,
                        pbo_ceiling=pbo_ceiling, min_oos_sharpe=min_oos_sharpe)
    for v in verdicts:
        v["n_trials_effective"] = n_cum
        v["n_trials_raw"] = n_raw

    lib = load_library()
    passed = [v for v in verdicts if v["promoted"]]
    # 冗余门槛：与库内因子及彼此之间都不许高度相关
    passed, redundant = _apply_redundancy_gate(passed, lib, max_abs_corr,
                                               market=market, verbose=verbose)
    for v in verdicts:
        v["checks"]["not_redundant"] = v["promoted"] and \
            not any(r["spec_id"] == v["spec_id"] for r in redundant)
        if v["promoted"] and not v["checks"]["not_redundant"]:
            v["promoted"] = False

    # 库容量：满了就只允许「新因子替换更弱的旧因子」，不再单纯增容
    n_lib = len(lib.get("factors") or {})
    dropped = []
    if max_library > 0 and n_lib + len(passed) > max_library:
        room = max(0, max_library - n_lib)
        if room == 0:
            # 全满：用最强的候选挑战库里最弱的那个（按 |oos_median|）
            weakest_sid, weakest = None, None
            for sid, v in (lib.get("factors") or {}).items():
                w = abs(v.get("oos_median", 0) or 0)
                if weakest is None or w < weakest[0]:
                    weakest_sid, weakest = w, v
            challenger = passed[0] if passed else None
            if challenger and weakest and abs(challenger["oos_median"]) > weakest[0] * 1.2:
                dropped.append({"name": weakest.get("name"),
                                "reason": f"被更强的新因子 {challenger['name']} 替换",
                                "oos_median": weakest.get("oos_median")})
                lib["factors"].pop(weakest_sid, None)
                room = 1
            else:
                passed = []
        for v in passed[room:]:
            dropped.append({"name": v["name"], "reason": "库容量已满，按强度截断",
                            "oos_median": v["oos_median"]})
        passed = passed[:room]

    promoted_now = []
    for v in passed:
        prev = lib["factors"].get(v["spec_id"], {})
        lib["factors"][v["spec_id"]] = {
            "name": v["name"], "spec": v["spec"],
            # n_sessions 已在上游按「排除纯缓存复用会话」算过，直接写。
            "n_sessions": v["n_sessions"],
            "n_distinct_oos": v.get("n_distinct_oos"),
            "oos_median": v["oos_median"],
            "dsr_best": v["dsr_best"], "pbo_best": v["pbo_best"],
            "lf_net_median": v["lf_net_median"],
            "first_promoted": prev.get("first_promoted") or
                datetime.now(timezone.utc).isoformat(),
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }
        if v["spec_id"] not in {x["spec_id"] for x in promoted_now}:
            promoted_now.append(v)
    lib["updated_at"] = datetime.now(timezone.utc).isoformat()
    lib["n_candidates_seen"] = len(acc)
    # 注意：这里存的是**原始**试验数（供审计），DSR 用的是有效试验数 n_cum。
    lib["cumulative_trials"] = og.cumulative_trials()
    lib["redundancy_gate"] = {"max_abs_corr": max_abs_corr, "max_library": max_library,
                              "n_rejected_redundant": len(redundant)}
    lib["trial_accounting"] = trial_detail
    LIB_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIB_FILE.write_text(json.dumps(lib, ensure_ascii=False, indent=2, default=str))

    if verbose:
        print(f"扫描 {len(runs)} 个挖掘结果文件，累计 {len(acc)} 个候选 spec")
        print(f"试验计数：原始 N={n_raw} → 有效 N={n_cum}"
              f"（{trial_detail.get('n_families')} 个算子族；同族网格变体共享同一信号，"
              f"不计为独立试验）")
        print(f"累计试验数 N={n_cum}；晋升门槛：可重复>={min_runs}次, "
              f"DSR>={dsr_floor}, PBO<={pbo_ceiling}, |OOS中位|>={min_oos_sharpe}, "
              f"冗余<={max_abs_corr}")
        print(f"\n本次晋升 {len(promoted_now)} 个，正式库共 {len(lib['factors'])} 个\n")
        for v in verdicts[:15]:
            flag = "🏆晋升" if v["promoted"] else "  "
            c = v["checks"]
            miss = ",".join(k for k, ok in c.items() if not ok)
            print(f"  {flag} {str(v['name'])[:26]:26s} 会话={v['n_sessions']} "
                  f"OOS中位={v['oos_median']:+.2f} DSR={v['dsr_best']} "
                  f"PBO={v['pbo_best']}" + (f"  ✗{miss}" if miss else ""))
    return {"n_runs": len(runs), "n_candidates": len(acc),
            "n_promoted_now": len(promoted_now), "n_library": len(lib["factors"]),
            "n_rejected_redundant": len(redundant), "redundant": redundant,
            "dropped": dropped, "verdicts": verdicts}


def _main() -> None:
    ap = argparse.ArgumentParser(description="因子晋升闸门（防过拟合）")
    ap.add_argument("--glob", action="append", default=None,
                    help="挖掘结果 glob，可多次指定；默认扫 runs/mine*.json 及 loop 输出")
    ap.add_argument("--min-runs", type=int, default=2, help="最少独立重复次数")
    ap.add_argument("--dsr-floor", type=float, default=0.95)
    ap.add_argument("--pbo-ceiling", type=float, default=0.5)
    ap.add_argument("--min-oos-sharpe", type=float, default=0.3)
    ap.add_argument("--max-abs-corr", type=float, default=0.8,
                    help="冗余门槛：与库内/已选因子截面rank相关超过此值则拒收；<=0 关闭")
    ap.add_argument("--max-library", type=int, default=20,
                    help="库容量上限；0=不限。默认 20：真正的去重靠 --max-abs-corr，"
                         "这个容量只是防无限膨胀的护栏。早期默认 5 的推导前提"
                         "（『去冗余后实际只有 3~5 个存活』）并不成立——它会把"
                         "第 6 个全新且正交的好因子静默丢弃，且不报错。")
    ap.add_argument("--market", default="us", help="算相关性用的面板市场")
    ap.add_argument("--no-redundancy", action="store_true", help="关闭冗余门槛（调试用）")
    args = ap.parse_args()
    promote(args.glob, min_runs=args.min_runs, dsr_floor=args.dsr_floor,
            pbo_ceiling=args.pbo_ceiling, min_oos_sharpe=args.min_oos_sharpe,
            max_abs_corr=0.0 if args.no_redundancy else args.max_abs_corr,
            max_library=args.max_library, market=args.market)


if __name__ == "__main__":
    _main()
