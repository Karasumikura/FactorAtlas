"""路线3：闭环因子挖掘（混合搜索版）。

两路并行，结果汇入同一个多重检验池：
  A) 系统扫描：遍历算子×参数空间（不漏，但被动）。
  B) LLM 提案：基于金融逻辑+反馈提出候选（主动，能跨界组合）。

纪律：
  - 所有候选统一做样本内/外评估。
  - 全局累计，最后统一 BH-FDR 校正。
  - 只有「样本内显著 + 样本外同向」才算幸存。
  - 记录全部尝试，避免选择偏差。
"""
from __future__ import annotations
import json
import time
from datetime import datetime
from pathlib import Path

from core import config, data, backtest
from llm.proposer import propose_factors
from factors.engine import OPS
from factors.sweep import generate_grid


def _valid_spec(spec: dict) -> bool:
    if spec.get("op") not in OPS:
        return False
    for k, v in (spec.get("params") or {}).items():
        if not isinstance(v, int):
            return False
        if k == "n" and not (2 <= v <= 250):
            return False
        if k == "skip" and not (0 <= v <= 60):
            return False
    return True


def _spec_key(spec: dict) -> str:
    p = {k: spec.get("params", {})[k] for k in sorted(spec.get("params", {}))}
    return f"{spec['op']}|{json.dumps(p, sort_keys=True)}|{spec.get('direction', 1)}"


def run_hybrid(panel: dict, n_llm_rounds: int = 3, per_round: int = 10,
               do_sweep: bool = True, train_frac: float = 0.7, hold: int = 1,
               neutralize_size: bool = False, out_json: Path | None = None,
               verbose: bool = True) -> dict:
    tried_keys: set[str] = set()
    all_results: list[dict] = []
    feedback: list[dict] = []

    def evaluate_batch(specs: list[dict], tag: str):
        fresh = []
        for s in specs:
            if not _valid_spec(s):
                continue
            key = _spec_key(s)
            if key in tried_keys:
                continue
            tried_keys.add(key)
            fresh.append(s)
        if verbose:
            print(f"  [{tag}] 新候选 {len(fresh)}", flush=True)
        for s in fresh:
            try:
                res = backtest.evaluate_with_split(
                    panel, s["name"], s, hold=hold, train_frac=train_frac,
                    neutralize_size=neutralize_size)
            except Exception as e:  # noqa
                if verbose:
                    print(f"    [跳过] {s['name']}: {e}", flush=True)
                continue
            rec = {
                "name": res.name, "op": s["op"], "params": s.get("params", {}),
                "direction": s.get("direction", 1), "rationale": s.get("rationale", ""),
                "source": tag,
                "ic_mean": res.ic_mean, "icir": res.icir, "ic_tstat": res.ic_tstat,
                "ls_sharpe": res.ls_sharpe, "net_ls_sharpe": res.net_ls_sharpe,
                "ls_return": res.ls_return, "turnover": res.turnover,
                "n_days": res.n_days, "p_value": res.p_value,
                "oos_ic_mean": res.oos_ic_mean, "oos_ls_sharpe": res.oos_ls_sharpe,
            }
            all_results.append(rec)
            feedback.append({**rec, "passed": False})
        return fresh

    # --- A) 系统扫描 ---
    if do_sweep:
        if verbose:
            print("\n===== 阶段A：系统扫描全算子空间 =====", flush=True)
        grid = generate_grid()
        if verbose:
            print(f"  网格规模: {len(grid)}", flush=True)
        evaluate_batch(grid, "sweep")

    # --- B) LLM 提案迭代 ---
    for rnd in range(1, n_llm_rounds + 1):
        if verbose:
            print(f"\n===== 阶段B 第 {rnd}/{n_llm_rounds} 轮：LLM 提案 =====", flush=True)
        try:
            specs = propose_factors(n=per_round, tried=list(all_results),
                                    history=feedback)
        except Exception as e:  # noqa
            if verbose:
                print(f"  [跳过] LLM 失败: {e}", flush=True)
            time.sleep(5)
            continue
        evaluate_batch(specs, f"llm_r{rnd}")

    # --- 全局多重检验校正 ---
    # 关键：BH-FDR 的 n 必须是**独立假设数**，不是记录数。
    # 挖掘会把每个 spec 的 +1/-1 两个方向都回测（方向是 spec 的一部分，
    # 但统计上是同一个假设的两种写法：|IC| 的显著性完全一致）。
    # 实测 round_06：368 条记录 = 184 个独立 spec，p 值成对完全相同。
    # 若直接拿 368 去做 BH，阈值 q*rank/368 被稀释一半，真实信号被埋掉。
    # 因此先按 spec 归并（取更小的 p 值），对独立假设做校正，再回填到记录。
    # 命名用 _hyp_key：模块级已有 _spec_key(spec)，同名会遮蔽导致 NameError。
    def _hyp_key(r: dict) -> str:
        return f"{r.get('op')}|{json.dumps(r.get('params') or {}, sort_keys=True)}"

    groups: dict[str, list[int]] = {}
    for i, r in enumerate(all_results):
        groups.setdefault(_hyp_key(r), []).append(i)
    keys = list(groups)
    # 每个独立假设的 p 值 = 该 spec 所有方向里最强的那个
    group_p = [min(all_results[i]["p_value"] for i in groups[k]) for k in keys]
    if verbose:
        print(f"\n===== 多重检验校正（{len(all_results)} 条记录 → "
              f"{len(keys)} 个独立假设）=====", flush=True)
    passed_groups = backtest.benjamini_hochberg(group_p, q=0.10)
    passed_group_set = {k for k, ok in zip(keys, passed_groups) if ok}
    for r in all_results:
        ok = _hyp_key(r) in passed_group_set
        oos_ok = False
        if r["oos_ic_mean"] == r["oos_ic_mean"] and r["ic_mean"] != 0:
            oos_ok = ((r["ic_mean"] > 0) == (r["oos_ic_mean"] > 0)
                      and abs(r["oos_ic_mean"]) > 5e-4)
        r["fdr_pass"] = bool(ok)
        r["oos_confirmed"] = bool(oos_ok)
        r["survivor"] = bool(ok and oos_ok)

    survivors = [r for r in all_results if r["survivor"]]
    survivors.sort(key=lambda x: -abs(x.get("oos_ls_sharpe", 0) or 0))

    # 去重：同一 op+params 的正反两个方向只保留 IC 为正（多头做多）的那个
    deduped = {}
    for r in survivors:
        key = f"{r['op']}|{json.dumps(r.get('params', {}), sort_keys=True)}"
        if key not in deduped:
            deduped[key] = r
        else:
            # 保留 ic_mean 更大的（方向更顺）
            if r["ic_mean"] > deduped[key]["ic_mean"]:
                deduped[key] = r
    survivors = sorted(deduped.values(),
                       key=lambda x: -abs(x.get("oos_ls_sharpe", 0) or 0))

    # 对每个幸存因子做稳健性检验（分年IC、分组单调性、分段IC）
    if survivors and verbose:
        print("\n===== 幸存因子稳健性检验 =====", flush=True)
    try:
        from core.robustness import robustness_report
        for r in survivors:
            spec = {"op": r["op"], "params": r.get("params", {}),
                    "direction": r.get("direction", 1)}
            try:
                rb = robustness_report(panel, spec)
                r["robustness"] = rb
                yrs = rb["yearly_ic"]
                pos_years = sum(1 for v in yrs.values() if (v["ic"] > 0) == (r["ic_mean"] > 0))
                r["years_same_sign"] = f"{pos_years}/{len(yrs)}"
                if verbose:
                    print(f"  {r['name']:26s} 分年同向 {pos_years}/{len(yrs)} | "
                          f"分组年化 {[round(x*100,1) for x in rb['quantile_annual_returns']]}",
                          flush=True)
            except Exception as e:
                r["robustness"] = {"error": str(e)}
    except Exception:
        pass

    out = {
        "generated_at": datetime.now().isoformat(),
        "n_tried": len(all_results), "n_fdr_pass": int(sum(r["fdr_pass"] for r in all_results)),
        "n_survivors": len(survivors), "train_frac": train_frac, "hold": hold,
        "n_sweep": int(sum(1 for r in all_results if r["source"] == "sweep")),
        "all_results": sorted(all_results, key=lambda x: -abs(x["ic_mean"])),
        "survivors": survivors,
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    if verbose:
        print(f"\n幸存（FDR + OOS同向）: {len(survivors)}", flush=True)
        for s in survivors[:15]:
            print(f"  ★ {s['name']:28s} IC={s['ic_mean']:+.4f} "
                  f"OOS_IC={s['oos_ic_mean']:+.4f} OOS_Sh={s['oos_ls_sharpe']:+.2f} "
                  f"src={s['source']}", flush=True)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--per-round", type=int, default=10)
    ap.add_argument("--no-sweep", action="store_true")
    ap.add_argument("--neutralize-size", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    p = data.build_panel()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else config.RUNS_DIR / f"hybrid_{ts}.json"
    run = run_hybrid(p, n_llm_rounds=args.rounds, per_round=args.per_round,
                     do_sweep=not args.no_sweep,
                     neutralize_size=args.neutralize_size, out_json=out_path)
    from core.report import save_report
    rpt = save_report(run, config.REPORTS_DIR / f"report_{ts}.md")
    print(f"\n结果: {out_path}\n报告: {rpt}")
