"""新闻修正层：把 LLM 的新闻判断翻译成仓位乘数。

设计依据（2026-10-23 与店长对齐）
--------------------------------
分工严格划清：

  LLM 负责「方向判断」   —— 这条新闻让我对这只票短期看多/看空到什么程度
  代码负责「换算动作」   —— 乘数怎么算、仓位打几折、要不要否决

为什么不让 LLM 直接给价位：它会编数字、不可回测、不可归因。所以本模块
只消费 bias / magnitude / confidence / horizon 四个字段，全部做过范围
校验（见 scripts/llm_news.py），**任何价格、目标价、止损位都不经 LLM**。

两级修正
--------
1. **个股修正**：目标仓位 × (1 + bias × magnitude × confidence × K_STOCK)
   K_STOCK 默认 0.5 —— 满强度（bias±1, mag=1, conf=1）最多把仓位放大/缩小
   50%，且缩小后不会变成反向仓位（下限 0，不做空翻）。

2. **宏观修正**：整体敞口 × (1 + macro_score × K_MACRO)
   K_MACRO 默认 0.3 —— 极端 RISK_OFF(-1.0) 时整体降 30% 敞口。

两级相乘后仍要过现有风控闸门（单笔风险 0.5%、敞口上限、日内熔断）。
本模块**只算乘数，不碰下单**，也不改任何已有参数默认值。

时效性
------
判断带 horizon_days，过期即失效 —— 一条 3 天前的新闻判断不该影响今天的仓位。
get_adjustment() 会按 ts + horizon 过滤过期记录。

回测基础
--------
所有判断都写进 data/news/llm_verdicts.jsonl，含当时的价格快照由调用方补充。
攒够样本才能校准 K_STOCK / K_MACRO —— 在拿到至少 100 条带结果的样本前，
这两个系数保持保守默认值，不做"优化"。

用法：
    from core.news_adjust import load_latest, get_adjustment, apply_to_targets
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
VERDICT_LOG = PROJ / "data" / "news" / "llm_verdicts.jsonl"

# 修正强度系数。保守起步，等样本回来再谈调整。
K_STOCK = 0.5      # 个股：满强度最多 ±50%
K_MACRO = 0.3      # 宏观：极端情绪最多 ±30% 总敞口

# 低于这个置信度直接忽略 —— 低置信判断带来的噪声大于信息
MIN_CONFIDENCE = 0.5

# 低于这个强度忽略 —— magnitude 太小说明没有实质催化剂
MIN_MAGNITUDE = 0.15

# 宏观 score 低于这个绝对值不介入。
# 本模块每天都跑，若阈值太低会天天微调仓位，手续费和噪声都吃不消。
# 0.3 意味着「明显偏空/偏多」才动手，中性区间一律放过。
MACRO_MIN_SCORE = 0.3


def load_latest(lookback_hours: int = 72) -> tuple[dict | None, dict[str, dict]]:
    """读最近的有效判断。

    返回 (macro_verdict, {symbol: verdict})。
    同一只票有多条时取**最新**那条（ts 最大）。
    过期的按 horizon_days 剔除。
    """
    if not VERDICT_LOG.exists():
        return None, {}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)

    macro = None
    per_symbol: dict[str, dict] = {}

    for line in VERDICT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            ts = datetime.fromisoformat(r["ts"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue

        horizon = int(r.get("horizon_days") or 2)
        if now > ts + timedelta(days=horizon):
            continue          # 判断已过期

        if r.get("kind") == "macro":
            if macro is None or ts > datetime.fromisoformat(macro["ts"]).replace(tzinfo=timezone.utc):
                macro = r
        elif r.get("kind") == "stock":
            sym = str(r.get("symbol") or "").upper()
            if not sym:
                continue
            prev = per_symbol.get(sym)
            if prev is None or ts > datetime.fromisoformat(prev["ts"]).replace(tzinfo=timezone.utc):
                per_symbol[sym] = r

    return macro, per_symbol


def stock_multiplier(verdict: dict | None) -> tuple[float, str]:
    """个股仓位乘数。返回 (multiplier, reason)。

    multiplier 下限 0.0 —— 新闻再差也不会把仓位打成负数（不开反向仓）。
    只有 magnitude 和 confidence 都够、且 bias 非 0 才生效。
    """
    if not verdict:
        return 1.0, ""
    bias = int(verdict.get("bias") or 0)
    mag = float(verdict.get("magnitude") or 0.0)
    conf = float(verdict.get("confidence") or 0.0)

    if bias == 0:
        return 1.0, ""
    if mag < MIN_MAGNITUDE:
        return 1.0, ""
    if conf < MIN_CONFIDENCE:
        return 1.0, ""

    mult = 1.0 + bias * mag * conf * K_STOCK
    mult = max(0.0, min(2.0, mult))     # 硬上限 ±100%，防极端值
    if abs(mult - 1.0) < 1e-6:
        return 1.0, ""
    reason = f"新闻{bias:+d} mag={mag:.2f} conf={conf:.2f} -> ×{mult:.2f}"
    return mult, reason


def macro_multiplier(verdict: dict | None) -> tuple[float, str]:
    """宏观敞口乘数。RISK_OFF 收缩，RISK_ON 放大，NEUTRAL 不动。

    与老路径 core/macro_watch 的分工（2026-10-23 澄清）：
      * macro_watch 是**事件驱动**：只在宏观数据发布日（CPI/FOMC/非农等）
        或发布后 1-2 天才调 LLM 看新闻，平时直接返回 NEUTRAL 不介入。
      * 本模块是**持续监测**：每天都看持仓票的新闻流，做温和的整体情绪修正。
    两者触发条件不重合，所以同时存在不矛盾 —— 今天没有数据发布，
    macro_watch 按设计不介入，本模块照常微调，这是预期行为。

    但正因为本模块每天都跑，这里的乘数必须**保守**：宏观判断本来就比
    个股判断更容易过拟合（大盘噪声大、样本少）。
    """
    if not verdict:
        return 1.0, ""
    try:
        score = float(verdict.get("score") or 0.0)
    except (TypeError, ValueError):
        return 1.0, ""
    if abs(score) < MACRO_MIN_SCORE:      # 弱信号不介入，避免天天微调
        return 1.0, ""
    mult = 1.0 + score * K_MACRO
    mult = max(0.6, min(1.25, mult))      # 宏观修正不该太激进
    if abs(mult - 1.0) < 1e-6:
        return 1.0, ""
    return mult, f"宏观 {verdict.get('regime')} score={score:+.2f} -> ×{mult:.2f}"


def get_adjustment(symbol: str, *,
                   macro: dict | None = None,
                   per_symbol: dict[str, dict] | None = None) -> dict:
    """给单只票算最终乘数（个股 × 宏观）。

    返回 {"multiplier": float, "reasons": [str], "applied": bool}
    """
    if per_symbol is None:
        macro_loaded, per_symbol = load_latest()
        if macro is None:
            macro = macro_loaded

    sym = symbol.upper().replace("USDT", "")
    m_stock, r_stock = stock_multiplier(per_symbol.get(sym))
    m_macro, r_macro = macro_multiplier(macro)

    total = m_stock * m_macro
    total = max(0.0, min(2.0, total))
    reasons = [r for r in (r_stock, r_macro) if r]

    return {
        "multiplier": round(total, 4),
        "reasons": reasons,
        "applied": abs(total - 1.0) > 1e-6,
        "stock_mult": round(m_stock, 4),
        "macro_mult": round(m_macro, 4),
    }


def apply_to_targets(targets: dict[str, float], *,
                     macro: dict | None = None,
                     per_symbol: dict[str, dict] | None = None,
                     exclude_macro: bool = False) -> tuple[dict[str, float], dict[str, dict]]:
    """把新闻修正应用到 {symbol: 目标仓位} 上。

    返回 (修正后的 targets, 每只票的修正明细)。
    注意：只调整**已有**目标仓位的票，不会凭空新增——新闻不该成为选股的
    唯一依据，选股仍然归因子层。没有目标仓位的票即使新闻再好也不进场。

    参数语义（2026-10-23 修）：
      * macro / per_symbol 传 None 表示「自己去磁盘读」
      * 想显式表示「没有宏观判断」用 exclude_macro=True，不要用 macro=None
        —— 单元测试踩过这个坑：传 macro=None 期望不施加宏观修正，结果
        函数回退去读了磁盘上的真实判断，把测试里的票也缩了水。
    """
    macro_loaded, per_sym = load_latest()
    if macro is None:
        macro = macro_loaded
    if per_symbol is None:
        per_symbol = per_sym
    if exclude_macro:
        macro = None

    out: dict[str, float] = {}
    detail: dict[str, dict] = {}
    for sym, tgt in targets.items():
        adj = get_adjustment(sym, macro=macro, per_symbol=per_symbol)
        out[sym] = tgt * adj["multiplier"]
        if adj["applied"]:
            detail[sym] = adj
    return out, detail


if __name__ == "__main__":
    # 自检：打印当前有效的修正
    m, ps = load_latest()
    print("=== 当前有效的新闻修正 ===")
    if m:
        mm, mr = macro_multiplier(m)
        print(f"宏观: {m.get('regime')} score={m.get('score')} -> ×{mm:.3f}  {mr}")
    else:
        print("宏观: 无有效判断")
    if not ps:
        print("个股: 无有效判断")
    for sym, v in sorted(ps.items()):
        mult, reason = stock_multiplier(v)
        flag = "生效" if abs(mult - 1.0) > 1e-6 else "忽略"
        print(f"  {sym:6s} [{flag}] ×{mult:.3f}  {reason or '(中性/强度不足)'}")
