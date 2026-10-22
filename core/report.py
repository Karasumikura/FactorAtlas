"""生成可读的因子研究报告（路线1 的产出）。"""
from __future__ import annotations
import json
from pathlib import Path


def render_report(run: dict, top_n: int = 20) -> str:
    lines = []
    lines.append("# 因子挖掘研究报告")
    lines.append("")
    lines.append(f"- 生成时间：{run['generated_at']}")
    n_cand = run.get("n_candidates", run.get("n_tried", 0))
    lines.append(f"- 候选因子总数：{n_cand}（回测 {run.get('n_backtested', n_cand)}）")
    if "by_source" in run:
        src = "、".join(f"{k}:{v}" for k, v in run["by_source"].items())
        lines.append(f"- 来源分布：{src}")
    lines.append(f"- 通过 BH-FDR(q=0.10)：{run['n_fdr_pass']}")
    lines.append(f"- **最终幸存（FDR + 样本外同向）：{run['n_survivors']}**")
    lines.append(f"- 样本内比例：{run['train_frac']:.0%}，持有期：{run['hold']} 日")
    if "elapsed_sec" in run:
        lines.append(f"- 全流程耗时：{run['elapsed_sec']}s")
    lines.append("")

    lines.append("## 幸存因子")
    lines.append("")
    if not run["survivors"]:
        lines.append("> ⚠️ 本轮没有因子通过多重检验 + 样本外确认。")
        lines.append("> 这是**正常的、诚实的**结果——说明给定数据下没有稳健的横截面信号，")
        lines.append("> 而不是系统出错了。")
    else:
        lines.append("| 因子 | 来源 | IC | 净Sharpe | 样本外IC | 样外Sharpe | 分年同向 | 分组年化% | 逻辑 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for s in run["survivors"]:
            rb = s.get("robustness", {})
            qr = rb.get("quantile_annual_returns", []) if isinstance(rb, dict) else []
            qr_str = "/".join(f"{x*100:.0f}" for x in qr) if qr else "-"
            lines.append(f"| {s['name']} | {s.get('source','')} | {s['ic_mean']:+.4f} | "
                         f"{s.get('net_ls_sharpe',float('nan')):+.2f} | {s['oos_ic_mean']:+.4f} | "
                         f"{s['oos_ls_sharpe']:+.2f} | {s.get('years_same_sign','-')} | {qr_str} | "
                         f"{s.get('rationale','')[:30]} |")
    lines.append("")

    lines.append("## 全部尝试因子（按 |IC| 排序，前40）")
    lines.append("")
    lines.append("| # | 因子 | 来源 | IC | ICIR | 多空Sharpe | 净Sharpe | 样本外IC | p值 | FDR | 样外确认 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(run["all_results"][:40], 1):
        lines.append(f"| {i} | {r['name']} | {r.get('source','')} | {r['ic_mean']:+.4f} | {r['icir']:+.2f} | "
                     f"{r['ls_sharpe']:+.2f} | {r.get('net_ls_sharpe',float('nan')):+.2f} | {r['oos_ic_mean']:+.4f} | {r['p_value']:.3f} | "
                     f"{'✅' if r.get('fdr_pass') else '❌'} | "
                     f"{'✅' if r.get('oos_confirmed') else '❌'} |")
    lines.append("")

    lines.append("## 方法论与护栏")
    lines.append("")
    lines.append("1. **无前视**：因子在 t 日收盘可算，仅预测 t+1 起收益。")
    lines.append("2. **样本切分**：前 {}% 为样本内，后段为样本外，样本外只在最终评估看一次。".format(int(run["train_frac"] * 100)))
    lines.append("3. **多重检验校正**：对所有尝试因子做 Benjamini-Hochberg FDR 校正——")
    lines.append("   试的因子越多，单个显著的门槛越严，直接对抗「挖到假因子」。")
    lines.append("4. **样本外确认**：FDR 通过后，还要求样本外 IC 与样本内**同向**。")
    lines.append("")
    return "\n".join(lines)


def save_report(run: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(run), encoding="utf-8")
    return path
