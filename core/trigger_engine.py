"""触发引擎：判断"该不该唤醒 LLM"。

静默轮询时只做轻量计算（读价格/持仓/算偏离），命中任一条件才唤醒 LLM。
避免周期性无脑调 LLM —— 既省 token，又让 LLM 只在真正需要判断时才介入。

触发条件（任一命中）：
  1. needs_rebalance : 持仓与目标偏离超过阈值（真正需要调仓）
  2. price_anomaly   : 单标的价格短时 z-score 超过阈值（异动）
  3. drawdown_alert  : 浮动亏损超过本金比例阈值（风控）
  4. rebalance_due   : 距上次调仓 >= N 天（例行调仓日）

返回 TriggerResult，含命中的条件、证据、建议的 LLM 提示。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TriggerResult:
    fired: bool = False
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def add(self, reason: str, **data):
        self.fired = True
        self.reasons.append(reason)
        self.evidence[reason] = data


# ---------------------------------------------------------------------------
# 阈值（可调）
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS = {
    "rebalance_drift_pct": 0.25,   # 单腿权重偏离目标 > 25% 才认为需调仓
    "price_zscore": 2.5,           # 价格 z-score 超过 2.5 视为异动
    "price_zscore_lookback": 20,   # z-score 回看天数
    "drawdown_pct": 0.05,          # 浮亏超过本金 5% 风控告警
    "rebalance_interval_days": 5,  # 距上次调仓 >=5 天时例行评估
    "llm_review_interval_min": 60, # LLM 直接下单模式下，距上次决策 >=N 分钟时主动评估
    # 判定「有实质调仓需求」的最小成交名义额。必须与 build_orders 的死区口径一致，
    # 否则会出现「触发命中→唤醒 LLM→订单却被死区抑制」的空转（浪费 token、状态错乱）。
    # 实际门槛 = max(min_notional * mult, pct * equity)，由调用方按 equity 传入。
    "meaningful_min_notional": 5.0,
    "meaningful_deadband_mult": 2.0,
    "meaningful_deadband_pct": 0.05,
    # --- 调仓日历（rebalance calendar）---
    # 与因子持有期匹配的出单节奏：不是「任一腿越过死区就发单」，而是
    #   (a) 全组合累计漂移占总权益比例 >= aggr_drift_pct，且
    #   (b) 距上次实际调仓 >= min_rebalance_interval_min 分钟
    # 两条同时满足才认为「真的该调仓」。这样避免 5 分钟 tick 追着目标抖动来回打碎单，
    #   把低频因子的成本优势在执行层又交回去。
    "aggr_drift_pct": 0.10,           # 全组合 |当前-目标| 名义额合计 / equity 的门槛
    "min_rebalance_interval_min": 240,  # 两次实际调仓之间至少间隔（分钟），默认 4 小时
}


def check_triggers(
    *,
    targets: list,                      # compute_targets 的输出（Target 列表）
    prices: dict[str, float],
    cur_positions: dict[str, float],
    equity: float,
    unrealized_pnl: float,
    orders: list,                       # build_orders 的输出
    last_rebalance_ts: str | None,
    price_history: dict[str, list[float]] | None = None,
    q: dict | None = None,
    llm_review: bool = False,
    last_llm_ts: str | None = None,
) -> TriggerResult:
    """综合判断是否需要唤醒 LLM。"""
    q = {**DEFAULT_THRESHOLDS, **(q or {})}
    res = TriggerResult()
    res.evidence["summary"] = {
        "n_targets": len(targets), "n_orders": len(orders),
        "unrealized_pnl": unrealized_pnl, "equity": equity,
    }

    # --- 1) 偏离触发：有实质调仓需求 ---
    #   门槛与 build_orders 的死区口径保持一致：已有持仓的微调若低于死区，
    #   build_orders 本就不会生成订单；这里用同一门槛，避免「触发空转」——
    #   即触发了 needs_rebalance、唤醒了 LLM，最终却没有任何订单可发。
    _mn = q["meaningful_min_notional"]
    _threshold = max(_mn * q["meaningful_deadband_mult"], q["meaningful_deadband_pct"] * equity)
    meaningful = [o for o in orders if getattr(o, "notional", 0) >= _threshold - 1e-9]

    # 调仓日历：全组合累计漂移（所有订单 |notional| 之和 / equity）。
    # 只有「整本书偏离得足够多」才值得动一次；单腿轻微越死区不再单独触发。
    _aggr_drift = sum(abs(getattr(o, "notional", 0.0)) for o in orders)
    _aggr_pct = (_aggr_drift / equity) if equity > 0 else 0.0

    # 距上次实际调仓的分钟数（用于最小间隔闸）
    _mins_since = None
    if last_rebalance_ts:
        try:
            _last = datetime.fromisoformat(last_rebalance_ts)
            if _last.tzinfo is None:
                _last = _last.replace(tzinfo=timezone.utc)
            _mins_since = (datetime.now(timezone.utc) - _last).total_seconds() / 60.0
        except Exception:  # noqa: BLE001
            _mins_since = None

    # 日历条件：累计漂移达标 且 距上次调仓已过最小间隔。
    _interval_ok = (_mins_since is None) or (_mins_since >= q["min_rebalance_interval_min"])
    _aggr_ok = _aggr_pct >= q["aggr_drift_pct"]

    # 风控豁免：若浮亏已达告警线（等价于后面的 drawdown_alert 判据），
    # 调仓日历必须让路——降险/纠偏不能等满最小间隔。
    # 注意：本条在 §3 之前求值，故不能依赖 res.reasons，须独立判定。
    _risk_override = equity > 0 and (-unrealized_pnl / equity) >= q["drawdown_pct"]
    if meaningful and ((_aggr_ok and _interval_ok) or _risk_override):
        res.add("needs_rebalance",
                threshold=round(_threshold, 2),
                n_orders=len(meaningful),
                aggr_drift_usd=round(_aggr_drift, 2),
                aggr_drift_pct=round(_aggr_pct, 4),
                mins_since_rebalance=(round(_mins_since, 1) if _mins_since is not None else None),
                orders=[{"sym": o.binance_symbol, "action": o.action,
                         "delta": o.delta_qty, "notional": round(o.notional, 2)}
                        for o in meaningful[:10]])
    elif meaningful:
        # 条件未满足 → 记录「本可调但被日历闸住」，供观测，不触发下单。
        res.evidence["rebalance_suppressed"] = {
            "n_orders": len(meaningful),
            "aggr_drift_pct": round(_aggr_pct, 4),
            "aggr_ok": _aggr_ok,
            "interval_ok": _interval_ok,
            "mins_since_rebalance": (round(_mins_since, 1) if _mins_since is not None else None),
        }

    # --- 2) 异动触发：价格 z-score ---
    if price_history:
        lb = q["price_zscore_lookback"]
        for sym, px in prices.items():
            hist = price_history.get(sym, [])
            if len(hist) < max(5, lb // 2):
                continue
            window = hist[-lb:]
            mu = sum(window) / len(window)
            var = sum((x - mu) ** 2 for x in window) / len(window)
            sd = var ** 0.5
            if sd > 1e-9:
                z = (px - mu) / sd
                if abs(z) >= q["price_zscore"]:
                    res.add("price_anomaly",
                            sym=sym, px=px, mean=round(mu, 2), z=round(z, 2))

    # --- 3) 风控触发：浮动亏损 ---
    if equity > 0:
        dd = -unrealized_pnl / equity
        if dd >= q["drawdown_pct"]:
            res.add("drawdown_alert", unrealized_pnl=unrealized_pnl,
                    equity=equity, loss_pct=round(dd * 100, 2))

    # --- 4) 例行调仓日 ---
    if last_rebalance_ts:
        try:
            last = datetime.fromisoformat(last_rebalance_ts)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
            if days >= q["rebalance_interval_days"]:
                res.add("rebalance_due", days_since=round(days, 1))
        except Exception:  # noqa: BLE001
            pass

    # --- 5) LLM 主动评估：LLM 直接下单模式下，定期让 LLM 重新审视组合 ---
    #   为什么需要这一条：LLM 是最终决策者，它需要在「没有因子漂移」时也能
    #   主动判断（例如新闻突袭、想主动减仓）。否则该模式下 LLM 几乎不会被唤醒。
    if llm_review:
        due = True
        if last_llm_ts:
            try:
                last = datetime.fromisoformat(last_llm_ts)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
                due = mins >= q["llm_review_interval_min"]
            except Exception:  # noqa: BLE001
                due = True
        if due:
            res.add("llm_review_due",
                    interval_min=q["llm_review_interval_min"],
                    last_llm_ts=last_llm_ts)

    return res


# ---------------------------------------------------------------------------
# LLM 提示构造
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是量化交易风控助手。你面前是一个美股动量因子的模拟盘（Demo，零真实风险）。

【最重要的输出要求】
你的回复必须【只】是一个 JSON 对象，不能有任何解释、前后缀、markdown 代码块。
第一个字符必须是 {，最后一个字符必须是 }。

JSON 格式：
{"decision": "HOLD", "confidence": 0.5, "reason": "一句话理由"}

decision 只能取三个值之一：
  - HOLD        持仓正常或依据不足，不动
  - REBALANCE   有明确依据，按目标调仓
  - REDUCE_RISK 风险偏高，优先降低敞口

判断原则：
1. 只在有明确依据时才建议 REBALANCE；没依据就 HOLD。
2. 动量因子在震荡市容易回撤；若价格异动方向与因子相反，倾向 HOLD 或 REDUCE_RISK。
3. 只使用给定的数字，不要臆造数据。
4. 再次强调：只输出 JSON，不要任何其他文字。"""


def build_llm_prompt(trig: TriggerResult, targets: list,
                     cur_positions: dict[str, float], prices: dict[str, float]) -> list[dict]:
    """把触发证据打包成给 LLM 的 messages。"""
    longs = [t.ticker for t in targets if t.weight > 0]
    shorts = [t.ticker for t in targets if t.weight < 0]
    payload = {
        "triggered_by": trig.reasons,
        "evidence": trig.evidence,
        "current_positions": cur_positions,
        "target_longs": longs,
        "target_shorts": shorts,
        "prices": prices,
    }
    import json
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            "以下是当前模拟盘状态，请判断是否调仓：\n" +
            json.dumps(payload, ensure_ascii=False, indent=1, default=str)},
    ]
