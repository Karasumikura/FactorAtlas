"""LLM 直接下单：把「持仓 + 交易所规则 + 额度 + 价格」喂给 LLM，
让它输出【具体的订单意图】，程序校验后走同一执行通道。

和 macro_watch.py 的分工：
  * macro_watch  → LLM 只做「宏观风险判断」→ 一个敞口乘数
  * llm_order    → LLM 拿到完整上下文 → 直接产出可执行订单（本文件）

设计原则（重要）：
  * LLM 能【看到】真实持仓、交易所门槛(minNotional/step/minQty)、额度、价格，
    所以它报的订单是「知道现实」的，不是凭空猜。
  * 但 LLM 的输出【必须】经过校验器：精度吸附、门槛、单笔上限、总敞口上限、
    标的可交易性。任何一条不通过就丢弃该腿，绝不把非法订单发出去。
  * 校验器不改变「几只、每只多少、多空」这些由 LLM 决定的自由度，
    只剔除【物理上无法成交】的腿 —— 这是交易所规则，不是策略硬编码。
  * Demo-only：本模块不碰 mainnet。执行统一由 bridge_us_binance.execute_orders 负责。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
DECISION_LOG = PROJ / "data" / "binance" / "llm_order_decisions.jsonl"


def set_book(book: str | None) -> None:
    """账本隔离：重绑本模块的决策日志路径（见 core/book.py）。"""
    global DECISION_LOG
    from core.book import paths, normalize
    DECISION_LOG = paths(normalize(book))["llm_decisions"]

try:
    from core import config as _cfg  # noqa: F401  加载 .env
except Exception:  # noqa: BLE001
    pass


# ---------------------------------------------------------------------------
# 1) 上下文构建：让 LLM「看到」现实
# ---------------------------------------------------------------------------
def build_context(
    *,
    equity: float,
    cur_positions: dict[str, float],
    prices: dict[str, float],
    filt: dict[str, dict],
    factor_targets: list | None = None,
    macro: dict | None = None,
    trigger: dict | None = None,
    tradable: list[str] | None = None,
    max_notional: float = 500.0,
    gross_cap: float = 1.2,
    leverage: int = 1,
    max_symbols: int = 40,
    recent_decisions: list | None = None,
) -> dict:
    """组装给 LLM 的完整上下文。

    filt: {symbol: {step, minQty, minNotional, qtyPrec, pricePrec}}
          —— 交易所真实规则，来自 binance_exec.symbol_filters。
    factor_targets: compute_targets 的输出（作为「参考建议」，非强制）。
    macro: macro_watch 的判断（regime / exposure_mult），作为背景。

    重要：候选池可能几百只，全塞进去会超出模型上下文并被截断。
    因此只保留【最相关】的 max_symbols 只：当前持仓 + 因子目标 +
    其余按「有报价且门槛可见」排序补足。宁可给 LLM 一份小而完整的清单，
    也不要给它几百只但被截断的清单。
    """
    # 当前持仓：带符号数量 + 估计名义额
    pos_rows = []
    for sym, qty in sorted(cur_positions.items()):
        px = prices.get(sym)
        notional = abs(qty) * px if px else None
        pos_rows.append({
            "symbol": sym,
            "qty": round(float(qty), 8),
            "side": "LONG" if qty > 0 else "SHORT",
            "ref_price": px,
            "notional_usd": round(notional, 2) if notional else None,
        })
    gross_now = sum(r["notional_usd"] or 0 for r in pos_rows)

    # 因子参考目标（如果 daemon 传了）
    ft = []
    if factor_targets:
        for t in factor_targets:
            ft.append({
                "symbol": getattr(t, "binance_symbol", None),
                "weight": round(float(getattr(t, "weight", 0.0)), 4),
                "factor_value": round(float(getattr(t, "factor_value", 0.0)), 4),
            })

    # --- 挑选要展示给 LLM 的标的（关键：避免上下文爆炸）---
    priority: list[str] = []
    seen = set()

    def _add(s):
        if s and s not in seen:
            seen.add(s)
            priority.append(s)

    for s in cur_positions:
        _add(s)                                     # 1) 持仓最优先（要能平/调）
    for t in (factor_targets or []):
        _add(getattr(t, "binance_symbol", None))    # 2) 因子目标
    for s in prices:                                # 3) 有其他报价的补足
        _add(s)
    for s in (tradable or []):                      # 4) 兜底
        _add(s)

    show = priority[:max_symbols]
    show_set = set(show)

    # 只保留被展示标的的规则（避免规则表本身撑爆上下文）
    per_symbol_rules = {
        s: {
            "minNotional": filt[s].get("minNotional"),
            "step": filt[s].get("step"),
            "minQty": filt[s].get("minQty"),
            "qtyPrec": filt[s].get("qtyPrec"),
        }
        for s in show if s in filt
    }
    show_prices = {s: p for s, p in prices.items() if s in show_set}

    ctx = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "account": {
            "strategy_equity_usd": equity,
            "leverage": leverage,
            "gross_now_usd": round(gross_now, 2),
            "gross_cap_usd": round(equity * gross_cap, 2),
            "max_single_order_usd": max_notional,
        },
        "exchange_rules": {
            "note": "每只标的最小下单名义额 minNotional、数量步长 step、最小数量 minQty。"
                    "低于 minNotional 的单子会被交易所拒绝。",
            "per_symbol": per_symbol_rules,
            "n_tradable_total": len(tradable) if tradable else len(filt),
            "tradable_symbols": show,
        },
        "current_positions": pos_rows,
        "prices": show_prices,
        "factor_reference_targets": [x for x in ft if x.get("symbol") in show_set],
        "recent_decisions": (recent_decisions or [])[-5:],
        "macro": macro or {},
        "trigger": trigger or {},
    }
    return ctx


# ---------------------------------------------------------------------------
# 2) 提示词：明确「你可以直接下单」
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是量化交易主脑的【下单决策器】。你在管理一个美股动量因子的模拟盘（币安 Demo，零真实风险，策略额度见上下文 account.strategy_equity_usd）。

你现在【能看到】账户真实状态：当前持仓、可用额度、每只标的的交易所下单规则（minNotional/step/minQty）、实时价格、因子参考目标、宏观判断。

你的任务：输出【具体的订单指令】。你可以自由决定：
  - 下几笔（0 到多笔，由你判断）
  - 每笔多少金额（可以不等）
  - 做多还是做空、开仓/加仓/减仓/平仓
  - 是否只做单边、集中持有一只、或分散
  - 也可以选择【不下单】（orders 为空数组）

【硬约束 - 必须遵守】
1. 所有订单的目标总敞口不得超过 account.gross_cap_usd。
2. 单笔名义额不得超过 account.max_single_order_usd。
3. 每笔的名义额（数量 × 现价）必须 >= 该标的的 exchange_rules.minNotional，否则会被交易所拒绝。
4. 数量必须符合该标的的 step（步长）与 minQty，符号要与 side 一致（BUY 买、SELL 卖）。
5. 只允许交易 tradable_symbols 里的标的。
6. 这是 Demo 模拟盘，不涉及真实资金。

【输出要求 - 最重要】
只输出一个 JSON 对象，不要任何解释、前后缀、markdown 代码块。
第一个字符必须是 {，最后一个字符必须是 }。

JSON 格式：
{"orders": [
    {"symbol": "NVDAUSDT", "side": "BUY", "notional_usd": 15.0, "reduce_only": false, "reason": "理由"},
    {"symbol": "MSTRUSDT", "side": "SELL", "notional_usd": 20.0, "reduce_only": false, "reason": "理由"}
  ],
 "confidence": 0.0-1.0,
 "reason": "整体一句话理由"}

字段说明（**关键**）：
  - symbol: 必须是 tradable_symbols 里的符号（……USDT）
  - side: "BUY"（买入/做多方向）或 "SELL"（卖出/做空方向）
  - notional_usd: **本次要成交的名义金额**（美元，正数），即「买/卖多少」，
    **不是**目标总持仓。也接受 `qty` 字段（成交数量，正数），二者等价。
  - reduce_only: 只减仓（平仓）填 true；正常开/加/减仓填 false
  - reason: 这笔的理由（短）

语义示例：当前持有 MU 多头 0.02 手，你想【再加 0.01 手】→ 报
  {"symbol":"MUUSDT","side":"BUY","qty":0.01,"reduce_only":false}
目标换手就写成「这次买卖多少」。若想全平 MU 多头 → 报 side=SELL、qty=0.02、reduce_only=true。

decision 提示：
  - 依据不足、宏观不明 → orders 输出空数组 []，并说明原因。
  - 想做空某标的 → side=SELL（Demo 永续支持双向）。
  - 你可以参考 factor_reference_targets，但【不必】照抄 —— 你是最终决策者。

【防止来回打 - 极其重要】
你的上下文里有 recent_decisions：那是你【上几次自己】下的单。请务必：
  - 只有当你有【新的、实质的】理由时才下单，不要因为持仓与参考目标有微小差异就反复微调。
  - 参考目标每 5 分钟会小幅抖动，这是噪声，不是信号。对已有持仓的「微调」
    如果金额很小（相对于账户额度），一律不要动 —— 系统会自动剔除这类噪声单。
  - 如果你上一笔刚买/卖了某标的，而现在的理由并不比那时更强，就【保持不动】，
    不要反向操作把仓位调回去。不要把同一标的来回买卖（那是在白交手续费）。
  - 判断标准：这次操作要么明显改善组合（换标的/显著加减仓），要么就不做。
    宁可不交易，也不要为了「贴合目标」做无意义的碎单。
  - 只有【开新仓 / 完全平仓 / 明显改变某腿权重】才算值得的交易。

只依据给定信息判断，不要臆造数据。再次强调：只输出 JSON。"""


def build_prompt_messages(ctx: dict) -> list[dict]:
    user = ("以下是当前账户、持仓、交易所规则和参考目标，请输出订单 JSON：\n"
            + json.dumps(ctx, ensure_ascii=False, indent=1, default=str)[:12000])
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 3) 调用 LLM 并解析
# ---------------------------------------------------------------------------
def _fallback_parse_orders(text: str) -> dict:
    """LLM 不守格式时兜底：能抠出 orders 数组就抠，抠不出就当空单（保守）。"""
    t = (text or "")
    try:
        from llm.client import extract_json
        parsed = extract_json(t)
        if isinstance(parsed, dict) and "orders" in parsed:
            return parsed
        if isinstance(parsed, list):
            return {"orders": parsed, "reason": "LLM 直接给了数组"}
    except Exception:  # noqa: BLE001
        pass
    return {"orders": [], "reason": "无法解析 LLM 输出，保守不下单", "_unparsed": t[:300]}


def llm_decide_orders(ctx: dict, *, model: str | None = None,
                      temperature: float = 0.1, max_tokens: int = 1200,
                      timeout: int = 300) -> dict:
    """让 LLM 直接产出订单意图。

    返回里始终带 `_ok` 标志（降级链路的判据）：
      _ok=True  → LLM 真的给出了判断（哪怕 orders 为空 = 它决定不交易）
      _ok=False → LLM 调用失败 / 输出无法解析（应退回纯脚本订单）
    """
    try:
        from llm.client import chat, extract_json
        msgs = build_prompt_messages(ctx)
        raw = chat(msgs, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        out = None
        try:
            out = extract_json(raw)
        except Exception:  # noqa: BLE001
            out = None
        if isinstance(out, list):
            out = {"orders": out, "reason": "LLM 直接给了数组"}
        if not isinstance(out, dict) or "orders" not in out:
            out = _fallback_parse_orders(raw)
        out["_raw"] = (raw or "")[:600]
        # 能走到这里且 orders 是列表 → LLM 真的给了判断
        out["_ok"] = isinstance(out.get("orders"), list)
        if not out["_ok"]:
            out["reason"] = out.get("reason") or "LLM 输出无法解析为 orders 列表"
        return out
    except Exception as e:  # noqa: BLE001
        return {"orders": [], "error": str(e), "_raw": "", "_ok": False,
                "reason": f"LLM 调用失败: {e}"}


def log_decision(rec: dict) -> None:
    DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DECISION_LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# 4) 校验：LLM 订单 → 可执行 Order（或丢弃）
# ---------------------------------------------------------------------------
@dataclass
class ValidationResult:
    orders: list = field(default_factory=list)      # 通过的 Order 对象
    rejected: list = field(default_factory=list)     # [{"intent":..., "why":...}]
    gross_after_usd: float = 0.0
    n_in: int = 0
    llm_ok: bool = False     # LLM 是否真的给出了可解析的判断（降级链判据）


def validate_llm_orders(
    intents: dict,
    *,
    equity: float,
    prices: dict[str, float],
    cur_positions: dict[str, float],
    filt: dict[str, dict],
    tradable: list[str] | None = None,
    gross_cap: float = 1.2,
    max_notional: float = 500.0,
    deadband_pct: float = 0.05,
    deadband_minnotional_mult: float = 2.0,
) -> ValidationResult:
    """把 LLM 的订单意图逐条校验、吸附精度、算目标数量，转成 bridge 的 Order。

    只剔除物理上无法成交的腿；不改变 LLM 对「几只/多少/多空」的自由度。

    deadband_pct / deadband_minnotional_mult：再平衡死区。已有持仓的「微调」
    （本次成交名义 < max(minNotional × mult, pct × equity)）会被当作噪声剔除，
    防止 LLM 追着小幅抖动的目标来回打。开仓/全平不受限。
    """
    from core.bridge_us_binance import Order

    res = ValidationResult()
    raw_orders = intents.get("orders") if isinstance(intents, dict) else None
    if not isinstance(raw_orders, list):
        return res
    res.n_in = len(raw_orders)

    allowed = set(tradable) if tradable else set(filt.keys())
    gross_cap_usd = equity * gross_cap

    # 目标数量（带符号），先汇总再校验总敞口
    planned: dict[str, float] = {}          # symbol -> target_qty
    meta: dict[str, dict] = {}

    for it in raw_orders:
        if not isinstance(it, dict):
            res.rejected.append({"intent": it, "why": "非对象"})
            continue
        sym = str(it.get("symbol", "")).upper().strip()
        side = str(it.get("side", "")).upper().strip()
        notional = it.get("notional_usd")
        # 兼容 LLM 常见的 camelCase 写法
        reduce_only = bool(it.get("reduce_only", it.get("reduceOnly", False)))

        if not sym or sym not in allowed:
            res.rejected.append({"intent": it, "why": f"标的不可交易: {sym}"})
            continue
        if side not in ("BUY", "SELL"):
            res.rejected.append({"intent": it, "why": f"side 非法: {side}"})
            continue
        px = prices.get(sym)
        if not px or px <= 0:
            res.rejected.append({"intent": it, "why": f"无现价: {sym}"})
            continue
        # 接受两种表达：notional_usd（名义额）或 qty（数量）。
        # LLM 经常直接给数量，这和信息等价，都收。
        raw_notional = it.get("notional_usd")
        raw_qty = it.get("qty")
        n_usd = None
        if raw_notional is not None:
            try:
                n_usd = float(raw_notional)
            except (TypeError, ValueError):
                n_usd = None
        if n_usd is None and raw_qty is not None:
            try:
                n_usd = abs(float(raw_qty)) * px
            except (TypeError, ValueError):
                n_usd = None
        if n_usd is None:
            res.rejected.append({
                "intent": it,
                "why": f"缺 notional_usd/qty: {raw_notional}/{raw_qty}"})
            continue
        if n_usd <= 0:
            res.rejected.append({"intent": it, "why": f"名义<=0: {n_usd}"})
            continue

        f = filt.get(sym, {})
        min_notional = float(f.get("minNotional") or 0) or 0.0
        # 交易所真实门槛：低于 minNotional 直接拒（这是物理约束，不是策略选择）
        if n_usd < min_notional - 1e-9:
            res.rejected.append({
                "intent": it,
                "why": f"名义 {n_usd:.2f} < minNotional {min_notional:.2f}"})
            continue
        # 单笔上限
        if n_usd > max_notional + 1e-9:
            res.rejected.append({
                "intent": it,
                "why": f"名义 {n_usd:.2f} > 单笔上限 {max_notional:.2f}"})
            continue

        # ★ 语义（关键修正）：qty / notional_usd 表示【本次交易的名义/数量】，
        #   即「增减多少」，不是「目标持仓总量」。
        #   LLM 说 "SELL 0.04" = 卖出（做空方向）0.04 手 → 目标 = 当前 + δ。
        #   之前把 qty 当绝对目标会导致方向反掉（SELL 变 BUY），已修正。
        cur = float(cur_positions.get(sym, 0.0))
        delta = n_usd / px
        signed_delta = delta if side == "BUY" else -delta

        # reduce_only：不能反向翻转，最多把当前仓位削减到 0（平仓）
        if reduce_only:
            if cur == 0:
                res.rejected.append({"intent": it, "why": "reduce_only 但无持仓"})
                continue
            if side == "BUY" and cur < 0:            # 平空：往 0 靠，不越过 0
                signed_delta = min(abs(signed_delta), abs(cur))
            elif side == "SELL" and cur > 0:          # 平多：往 0 靠，不越过 0
                signed_delta = -min(abs(signed_delta), abs(cur))
            else:
                res.rejected.append({
                    "intent": it, "why": f"reduce_only 方向与持仓不符(cur={cur})"})
                continue

        # 精度吸附（用交易所 step / qtyPrec）
        step = float(f.get("step") or 0)
        prec = int(f.get("qtyPrec") or 8)
        signed_delta = _snap_signed(signed_delta, step, prec)
        if signed_delta == 0:
            res.rejected.append({"intent": it, "why": "吸附后交易量为 0"})
            continue
        # 成交名义（trade notional）必须 ≥ minNotional —— 这才是交易所真正校验的量
        if abs(signed_delta) * px < min_notional - 1e-9:
            res.rejected.append({
                "intent": it,
                "why": f"成交名义 {abs(signed_delta)*px:.2f} < minNotional {min_notional:.2f}"})
            continue

        # 目标持仓 = 当前 + δ_1 + δ_2 ...（同一 symbol 多条累加）
        planned[sym] = planned.get(sym, cur) + signed_delta
        meta[sym] = {"side": side, "reason": it.get("reason", ""),
                     "reduce_only": reduce_only}

    # 总敞口校验：所有目标名义之和不得超过 gross cap。
    # 注意：这里是「调仓后的目标总敞口」，不是本次交易名义（和 daemon 闸门口径一致）。
    gross_after = 0.0
    for sym, tq in planned.items():
        px = prices.get(sym)
        if px:
            gross_after += abs(tq) * px
    res.gross_after_usd = round(gross_after, 2)

    if gross_after > gross_cap_usd + 1e-6 and gross_after > 0:
        # 超限：按等比例缩放到 cap（不改变腿数/方向，只缩总量）
        scale = gross_cap_usd / gross_after
        for sym in list(planned.keys()):
            step = float(filt.get(sym, {}).get("step") or 0)
            prec = int(filt.get(sym, {}).get("qtyPrec") or 8)
            planned[sym] = _snap_signed(planned[sym] * scale, step, prec)
        gross_after = 0.0
        for sym, tq in planned.items():
            px = prices.get(sym)
            if px:
                gross_after += abs(tq) * px
        res.gross_after_usd = round(gross_after, 2)

    # 转 Order（delta = 目标 - 当前）
    for sym, tq in planned.items():
        px = prices.get(sym)
        f = filt.get(sym, {})
        step = float(f.get("step") or 0)
        min_notional = float(f.get("minNotional") or 0) or 0.0
        cur = float(cur_positions.get(sym, 0.0))
        delta = tq - cur
        delta_s = _snap_signed(delta, step, int(f.get("qtyPrec") or 8))
        tq_s = _snap_signed(tq, step, int(f.get("qtyPrec") or 8))
        is_full_close = (abs(tq_s) < 1e-12) and abs(cur) > 1e-9

        if abs(delta_s) < (step / 2 if step else 1e-9) and abs(cur) < 1e-9:
            continue
        if not is_full_close and abs(delta_s * px) < min_notional:
            res.rejected.append({
                "intent": {"symbol": sym},
                "why": f"调整名义 {abs(delta_s*px):.2f} < minNotional {min_notional:.2f}"})
            continue
        # 再平衡死区：已有持仓的「微调」不值得交易 —— 只挪动一点点仓位的
        # delta 是噪声（因子目标每 tick 小幅抖动），照单全做会来回打、白交手续费。
        # 开仓/全平/减到 0 不受此限（那些是结构性动作）。
        # 口径：max(minNotional × deadband_minnotional_mult, deadband_pct × equity)。
        # 实测（$100 账户、minNotional=$5）：来回打的单都在 $5.9~9，取 $10 可清掉几乎全部。
        if not is_full_close and abs(cur) > 1e-9:
            deadband_usd = max(min_notional * deadband_minnotional_mult,
                               deadband_pct * equity)
            if abs(delta_s * px) < deadband_usd - 1e-9:
                res.rejected.append({
                    "intent": {"symbol": sym},
                    "why": f"微调 {abs(delta_s*px):.2f} < 再平衡死区 {deadband_usd:.2f}"
                           f"（minNotional×{deadband_minnotional_mult:g} 与 {deadband_pct*100:.0f}% equity 取大），"
                           f"视为噪声跳过"})
                continue

        side = "BUY" if delta_s > 0 else "SELL"
        if cur == 0:
            act = "OPEN"
        elif abs(tq_s) < 1e-12:
            act = "CLOSE"
        elif (tq_s > 0) != (cur > 0):
            act = "FLIP"
        elif abs(tq_s) > abs(cur):
            act = "INCREASE"
        else:
            act = "REDUCE"

        res.orders.append(Order(
            binance_symbol=sym, side=side, target_weight=(tq_s * px / equity) if equity else 0.0,
            target_qty=tq_s, cur_qty=cur, delta_qty=delta_s, ref_price=px,
            notional=abs(delta_s * px), action=act,
        ))

    prio = {"CLOSE": 0, "REDUCE": 1, "FLIP": 2, "INCREASE": 3, "OPEN": 4}
    res.orders.sort(key=lambda o: prio.get(o.action, 9))
    return res


def _snap_signed(x: float, step: float, prec: int) -> float:
    """对有符号数量按 step 向下吸附到精度。"""
    import math
    if x == 0:
        return 0.0
    if step and step > 0:
        q = math.floor(abs(x) / step) * step
        return round(math.copysign(q, x), prec)
    return round(x, prec)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="LLM 直接下单上下文/校验自检")
    ap.add_argument("--show-context", action="store_true")
    args = ap.parse_args()
    if args.show_context:
        ctx = build_context(equity=100.0, cur_positions={}, prices={"NVDAUSDT": 180.0},
                            filt={"NVDAUSDT": {"minNotional": 5, "step": 0.001,
                                               "minQty": 0.001, "qtyPrec": 3}})
        print(json.dumps(ctx, ensure_ascii=False, indent=1, default=str))
