"""汇总多次挖掘结果，生成总报告。"""
from __future__ import annotations
import json
import glob
from datetime import datetime
from pathlib import Path

from core import config
from core.report import render_report


def collect(pattern: str = "runs/*mine_*.json") -> list[dict]:
    """收集挖掘结果。默认【排除】修复前的历史结果，避免旧的错误准则污染总报告。"""
    # 修复前的旧文件（用 IC 准则、未做方向归一/可交易性复检），一律排除
    EXCLUDE_MARKERS = ("mine_ashare_v2", "mine_ashare_2026", "mine_20261015",
                       "mine_ashare_fixed_full", "mine_ashare_fixed_500",
                       "test_flip", "hybrid", "final")
    # 显式允许的白名单（修复后但幸存者无 tradeable 字段的早期修复版）
    ALLOW = ("mine_us_fixed",)
    out = []
    for f in sorted(glob.glob(pattern)):
        if any(m in f for m in EXCLUDE_MARKERS) and not any(a in f for a in ALLOW):
            continue
        try:
            d = json.load(open(f))
            # 旧结果没有 tradeable 字段 → 也排除（白名单除外）
            if (d.get("survivors") and "tradeable" not in d["survivors"][0]
                    and not any(a in f for a in ALLOW)):
                continue
            out.append(d)
        except Exception:
            continue
    return out


def render_master(runs: list[dict]) -> str:
    lines = ["# 因子挖掘 —— 总报告", ""]
    lines.append(f"生成时间：{datetime.now().isoformat()}")
    lines.append(f"纳入 {len(runs)} 次挖掘运行")
    total_cand = sum(r.get("n_candidates", r.get("n_tried", 0)) for r in runs)
    lines.append(f"累计候选因子：{total_cand}")
    lines.append("")

    # 汇总所有幸存（去重）
    seen = {}
    for r in runs:
        for s in r.get("survivors", []):
            key = json.dumps({"op": s.get("op"), "params": s.get("params", {})},
                             sort_keys=True)
            if key not in seen:
                seen[key] = s
                continue
            # 优先保留带 triple-gate 字段（cost/segment robust）的、且真实净 Sharpe 高的
            old = seen[key]
            def _score(x):
                has_gate = 1 if x.get("lowfreq_segment_robust") is not None else 0
                lf_sh = (x.get("lowfreq", {}) or {}).get("best_sharpe") or 0
                return (has_gate, lf_sh, x.get("oos_ls_sharpe", 0) or 0)
            if _score(s) > _score(old):
                seen[key] = s
    surv = sorted(seen.values(), key=lambda x: -abs(x.get("oos_ls_sharpe", 0) or 0))
    # 把"三重门槛可交易"的因子单独列出，避免被日频虚高的非交易因子盖住
    # 严格：只列确实带 triple-gate 字段且两门都过的（旧 20bps 口径的运行不含 gate 字段）
    tradeable = [s for s in seen.values()
                 if s.get("lowfreq_cost_robust") and s.get("lowfreq_segment_robust")]
    tradeable = sorted(tradeable, key=lambda x: -(x.get("lowfreq", {}).get("best_sharpe") or 0))
    lines.append(f"## ★ 三重门槛可交易因子：{len(tradeable)} 个（真正能用的）")
    lines.append("")
    if tradeable:
        lines.append("| 因子 | 最优h | 真实净Sharpe(30bps) | 成本稳健 | 时段稳健 | 回撤 |")
        lines.append("|---|---|---|---|---|---|")
        for s in tradeable:
            lf = s.get("lowfreq", {})
            vt = lf.get("vol_target") or {}
            lines.append(f"| {s['name']} | {lf.get('best_hold','-')} | "
                         f"{lf.get('best_sharpe',0):+.2f} | "
                         f"{'✅' if s.get('lowfreq_cost_robust') else '❌'} | "
                         f"{'✅' if s.get('lowfreq_segment_robust') else '❌'} | "
                         f"{lf.get('grid',{}).get(str(lf.get('best_hold')),{}).get('max_dd',float('nan')):+.1%} |")
    else:
        lines.append("> 无（本批运行未含 lowfreq 分析）")
    lines.append("")
    lines.append(f"## 日频口径的全部幸存因子：{len(surv)} 个（含未过成本门槛者，仅供参考）")
    lines.append("")
    if surv:
        lines.append("| 因子 | IC | 净Sharpe | 样本外IC | 样外Sharpe | 分年同向 |")
        lines.append("|---|---|---|---|---|---|")
        for s in surv:
            lines.append(f"| {s['name']} | {s['ic_mean']:+.4f} | "
                         f"{s.get('net_ls_sharpe', float('nan')):+.2f} | "
                         f"{s['oos_ic_mean']:+.4f} | {s['oos_ls_sharpe']:+.2f} | "
                         f"{s.get('years_same_sign','-')} |")
    lines.append("")
    lines.append("## 关键结论（2026-10-15 修复后）")
    lines.append("")
    lines.append("0. ⚠️ 本报告仅纳入【修复后】的挖掘结果。修复了三处筛选缺陷：")
    lines.append("   (a) 幸存判定从横截面 IC 改为多空组合 LS Sharpe（IC 在强 beta 市场会异号）；")
    lines.append("   (b) 独立性用多空收益相关（非因子值 rank 均值）；")
    lines.append("   (c) 幸存方向归一 + 可交易性复检。详见 reports/IC_LS_SIGN_BUG.md。")
    lines.append("")
    lines.append("### ★ 三轮深化的最终结论（见 reports/TRADEABLE_FINAL.md）")
    lines.append("")
    lines.append("**三重门槛**（这才是真正的可交易标准）：")
    lines.append("  30bps 净 Sharpe > 0.2  AND  40bps 成本下仍 > 0（成本稳健）  AND  分段负段 ≤ 1（时段稳健）。")
    lines.append("")
    lines.append("1. **A股 alpha 有强市值梯度，且只有大盘可操作**：")
    lines.append("   - 沪深300：14 个过三重门槛（波动/振幅族为主），容量**百亿级**，完全成本稳健。")
    lines.append("   - 中证500：仅 3 个过门槛（流动性 amihud/illiq 强但**近年已失效**；趋势最稳）。")
    lines.append("   - **中证1000：0/10 在 40bps 下存活 → 不可操作**（小盘冲击成本吃光收益）。")
    lines.append("2. **幸存条数 ≠ 独立 alpha 数**。沪深300 的 13 个因子聚类后**只有 2 个独立族**：")
    lines.append("   波动（10个）+ 趋势（3个）。同族内是同一信号，不可当独立因子分散。")
    lines.append("3. **沪深300 内两族 LS 相关仅 0.127** → 组合它们 Sharpe 0.58→0.70，回撤 -22.7%。")
    lines.append("   最优单因子 `trend_x_lowvol_d1`（趋势÷波动）时段最稳（0 负段）。")
    lines.append("4. **真正的分散化来自跨市场**：美股与A股 LS 相关仅 0.11~0.14（A股内部 300/500 相关 0.88）。")
    lines.append("5. **任何因子都有亏损段**（波动族共同低谷在 2022–2023）→ 组合是必需而非可选。")
    lines.append("6. **前视偏差量化**：2015 年中证1000 仅 37% 当前成分股当时存在 → 小盘结果含未来函数。")
    lines.append("")
    lines.append("### ★★ 最终实战配置：跨市场组合 Sharpe +0.96（见 reports/CROSS_MARKET_COMBO.md）")
    lines.append("")
    lines.append("| 组合 | Sharpe | 年化 | 回撤 |")
    lines.append("|---|---|---|---|")
    lines.append("| A股(波动+趋势) 风险平价 | +0.87 | — | — |")
    lines.append("| 美股(动量) | +0.51 | — | — |")
    lines.append("| **跨市场风险平价组合** | **+0.96** | **+16.9%** | **-19.8%** |")
    lines.append("")
    lines.append("- A股↔美股月频相关仅 **+0.034**（几乎完全独立）")
    lines.append("- **全部由三重门槛可交易因子构成**，真实扣成本（30bps）")
    lines.append("- 复现：`python -m core.cross_market_combo`")
    lines.append("")
    lines.append("⚠️ 全部结论受**幸存者偏差**（当前成分股）打折，点内成分股数据才能定论。")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    runs = collect()
    rpt = render_master(runs)
    p = config.REPORTS_DIR / "MASTER_report.md"
    p.write_text(rpt, encoding="utf-8")
    print(f"总报告: {p}")
    print(f"纳入 {len(runs)} 次运行")
