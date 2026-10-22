"""量化交易主脑（Demo 事件驱动调度器）。

这是「能自己跑」的那一部分 —— 不需要人盯着敲命令。

工作方式（事件驱动，不是无脑轮询）：
  ┌─ 静默轮询（轻量：读价格 + 持仓 + 算目标，不调 LLM）
  │     ↓
  ├─ 触发引擎判断（偏离 / 异动 / 浮亏 / 例行日）
  │     ↓  命中任一
  ├─ 唤醒 LLM（9Router free）：打包证据 → 要它判断该不该动
  │     ↓
  ├─ 决策合并：LLM 建议 + 硬规则（风控/门槛）共同决定
  │     ↓
  └─ 执行（Demo）+ 台账 + 心跳留痕

安全边界（硬编码，不可绕过）：
  * 只允许 Demo（testnet）。mainnet 直接拒绝。
  * 单标的名义上限 max_notional；杠杆默认 1。
  * LLM 只能"建议"，不能绕过门槛/风控直接下单。
  * 无人值守时默认 dry（只记录决策不发单），除非显式 --live。

模式：
  --once --dry          跑一次，看会判断成什么（不发单，调 LLM）
  --once --no-llm       跑一次，跳过 LLM，纯规则
  --loop                常驻：静默轮询，触发才唤醒 LLM
  --live                允许真发单（仅 Demo）。不加则全程 dry。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent

# ── 账本隔离 ─────────────────────────────────────────────────────────────
# 本进程运行的「账本名」。空串=默认账本（走历史全局路径，行为 100% 不变）。
# 非空时，所有台账/状态/决策/价格历史文件都落在 data/binance/books/<book>/。
# 由 CLI --book 或环境变量 QUANT_BOOK 设置，进程启动早期调用 set_book() 固定。
_BOOK: str = ""


def set_book(book: str | None) -> None:
    """设定本进程账本名（在 main 早期调用一次），并重绑所有路径全局名。

    注意：这些路径是被本模块内多个函数以「全局名」引用的，所以必须在
    set_book() 里**重新赋值模块全局**，各函数才会看到新路径。
    （PEP 562 __getattr__ 对模块内部全局名查找无效，故不能用懒解析。）
    """
    global _BOOK, HEARTBEAT, STATE_LOG, DECISION_LOG, PRICE_HIST_FILE
    from core.book import normalize, paths
    _BOOK = normalize(book)
    p = paths(_BOOK)
    HEARTBEAT = p["heartbeat"]
    STATE_LOG = p["state"]
    DECISION_LOG = p["decisions"]
    PRICE_HIST_FILE = p["price_history"]
    # 同步账本到相关子模块（它们各自持有路径全局名）
    try:
        from core import llm_order as _lo
        _lo.set_book(_BOOK)
    except Exception:  # noqa: BLE001
        pass
    try:
        from core import demo_reconcile as _dr
        _dr.set_book(_BOOK)
    except Exception:  # noqa: BLE001
        pass


def get_book() -> str:
    return _BOOK


def _P() -> dict:
    """当前账本的路径集合（每次调用都按 _BOOK 解析）。"""
    from core.book import paths
    return paths(_BOOK)


# 路径全局名：默认 = 历史全局路径（默认账本）。set_book() 会按需重绑。
_HEARTBEAT = None  # 占位
from core.book import paths as _bpaths  # noqa: E402
_DP = _bpaths("")
HEARTBEAT = _DP["heartbeat"]
STATE_LOG = _DP["state"]
DECISION_LOG = _DP["decisions"]
PRICE_HIST_FILE = _DP["price_history"]


def _paths():
    return _P()


FACTORS = {
    "mom250": {"op": "mom", "params": {"n": 250}, "direction": 1},
    "mom120": {"op": "mom", "params": {"n": 120}, "direction": 1},
    "trend120": {"op": "trend_strength", "params": {"n": 120}, "direction": 1},
    # 因子库合成：动态读取 data/factor_library.json 里已晋升的因子。
    # 库为空时 tick 会自动回退到 mom250（见 _resolve_spec）。
    "library": {"kind": "composite"},
    # 共振因子（多指标共振框架的日频编码）：见 factors/resonance.py
    "resonance": {"op": "resonance", "params": {}, "direction": 1},
}


def _resolve_spec(factor: str) -> dict:
    """把 factor 名解析成 spec；library 走因子库合成，空库时回退 mom250。"""
    spec = dict(FACTORS[factor])
    if spec.get("kind") == "composite":
        try:
            from core.factor_library import library_specs
            specs = library_specs()
            if specs:
                spec["specs"] = specs
                return spec
        except Exception:  # noqa: BLE001
            pass
        # 库为空 → 回退到最稳的单因子，避免交易端"无因子可用"
        return dict(FACTORS["mom250"])
    return spec


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: Path, rec: dict) -> None:
    """Append 一行 JSON 到文件（**带独占文件锁**，多进程安全）。

    注意：本函数**绝不能因写盘失败而抛出异常**。历史上磁盘写满
    （OSError: [Errno 28] No space left on device）会让 heartbeat() 抛错，
    异常逃逸到 main() 导致整个交易守护进程退出并被 systemd 反复重启
    （restart counter 一度达到 62）。对交易系统而言，台账/心跳写失败是
    「可降级」事件，绝不能拖垮交易主循环。因此这里吞掉 IO 错误并降级到
    stderr 告警。

    2026-10-20 加锁：`data/binance/trader_heartbeat.jsonl` 曾出现两条记录
    在 903 字节处交错截断（JSON 损坏行）。原因是多个盘的进程（含巡检脚本、
    ops timer）可能同时 append 同一个文件，而 `f.write(...)` 不是原子操作。
    `flock(LOCK_EX)` 把「打开-写入-关闭」变成互斥区，彻底消除交错。
    加锁失败（如平台不支持）时退化为无锁写入 —— 宁可丢锁也不能不写。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, default=str, ensure_ascii=False) + "\n"
        with path.open("a") as f:
            locked = False
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                locked = True
            except Exception:  # noqa: BLE001
                pass
            try:
                f.write(line)
                f.flush()
            finally:
                if locked:
                    try:
                        import fcntl
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except Exception:  # noqa: BLE001
                        pass
    except OSError as e:
        # 磁盘满/只读等：告警但不中断交易
        print(f"[trader_daemon][WARN] 写台账失败（已忽略，不影响交易）: {path} -> {e}",
              file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[trader_daemon][WARN] 写台账异常（已忽略）: {path} -> {e}",
              file=sys.stderr, flush=True)


def heartbeat(rec: dict) -> None:
    _append(_P()["heartbeat"], rec)


def _write_state(state: dict) -> None:
    """写状态快照。注意：只存【状态】（持仓/敞口/宏观），不存整条决策记录。"""
    snapshot = {
        "ts": state.get("ts"),
        "status": state.get("status"),
        "current_positions": state.get("current_positions"),
        "n_targets": state.get("n_targets"),
        "macro": state.get("macro"),
        "unrealized_pnl": state.get("unrealized_pnl"),
    }
    try:
        sp = _P()["state"]
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(snapshot, indent=1, default=str, ensure_ascii=False))
    except OSError as e:
        # 磁盘满等写盘失败：降级告警，不影响交易主循环（历史教训见 _append）
        print(f"[trader_daemon][WARN] 写状态快照失败（已忽略）: {_P()['state']} -> {e}",
              file=sys.stderr, flush=True)


# --- 滚动价格历史（供 price_anomaly 触发使用）---
PRICE_HIST = None  # 延迟初始化


def _load_price_history() -> dict:
    global PRICE_HIST
    if PRICE_HIST is None:
        p = _P()["price_history"]
        try:
            PRICE_HIST = json.loads(p.read_text()) if p.exists() else {}
        except Exception:  # noqa: BLE001
            PRICE_HIST = {}
    return PRICE_HIST


def _update_price_history(prices: dict[str, float], maxlen: int = 30) -> None:
    """把本次价格追加进滚动历史（每次 tick 更新）。"""
    hist = _load_price_history()
    for sym, px in prices.items():
        seq = hist.setdefault(sym, [])
        seq.append(round(float(px), 6))
        if len(seq) > maxlen:
            del seq[:-maxlen]
    try:
        p = _P()["price_history"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(hist))
    except Exception:  # noqa: BLE001
        pass


def _last_rebalance_ts() -> str | None:
    """从决策日志读上次真正执行调仓的时间。"""
    if not DECISION_LOG.exists():
        return None
    last = None
    for line in DECISION_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("executed"):
                last = r.get("ts")
        except Exception:  # noqa: BLE001
            continue
    return last


def _count_executions_today(day: str) -> int:
    """统计今天已执行的调仓次数（防日内来回打）。"""
    if not DECISION_LOG.exists():
        return 0
    n = 0
    for line in DECISION_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("executed") and str(r.get("ts", "")).startswith(day):
                n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


def _symbol_exec_counts_today(day: str) -> dict[str, int]:
    """统计今天每个标的被交易的次数（含方向）。

    动机：MSTR 曾在一天内被来回打了 49 次 —— 总次数上限拦不住「单标的反复横跳」。
    这里按标的计数，用于「单标的日内调仓上限」，专治单标的震荡。
    """
    out: dict[str, int] = {}
    if not DECISION_LOG.exists():
        return out
    for line in DECISION_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
            if not r.get("executed") or not str(r.get("ts", "")).startswith(day):
                continue
            for o in (r.get("orders") or []):
                s = o.get("binance_symbol")
                if s:
                    out[s] = out.get(s, 0) + 1
        except Exception:  # noqa: BLE001
            continue
    return out


def _symbol_flips_today(day: str) -> dict[str, int]:
    """统计今天每个标的的【方向反转】次数（买→卖或卖→买）。

    比"交易次数"更精准地刻画震荡：MSTR 的问题本质是反复反向，不是单纯次数多。
    """
    last_side: dict[str, str] = {}
    flips: dict[str, int] = {}
    if not DECISION_LOG.exists():
        return flips
    for line in DECISION_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
            if not r.get("executed") or not str(r.get("ts", "")).startswith(day):
                continue
            for o in (r.get("orders") or []):
                s = o.get("binance_symbol")
                side = o.get("side")
                if not s or not side:
                    continue
                prev = last_side.get(s)
                if prev is not None and prev != side:
                    flips[s] = flips.get(s, 0) + 1
                last_side[s] = side
        except Exception:  # noqa: BLE001
            continue
    return flips


def _daily_realized_pnl(day: str) -> float:
    """从决策日志累计【当日已实现】盈亏（executed 记录里的 pnl 字段求和）。

    说明：模拟盘每笔成交由执行层回填 ``pnl``（无则视为 0）。用于「日内亏损熔断」。
    """
    if not DECISION_LOG.exists():
        return 0.0
    total = 0.0
    for line in DECISION_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
            if not r.get("executed") or not str(r.get("ts", "")).startswith(day):
                continue
            v = r.get("pnl")
            if isinstance(v, (int, float)):
                total += float(v)
            else:
                for res in (r.get("results") or []):
                    if isinstance(res, dict) and isinstance(res.get("pnl"), (int, float)):
                        total += float(res["pnl"])
        except Exception:  # noqa: BLE001
            continue
    return total


def _daily_turnover_usd(day: str) -> float:
    """从决策日志累计当日【已成交名义额】（用于换手/成交额上限）。"""
    if not DECISION_LOG.exists():
        return 0.0
    total = 0.0
    for line in DECISION_LOG.read_text().splitlines():
        try:
            r = json.loads(line)
            if not r.get("executed") or not str(r.get("ts", "")).startswith(day):
                continue
            for res in (r.get("results") or []):
                if isinstance(res, dict) and res.get("status") == "sent":
                    n = res.get("notional")
                    if isinstance(n, (int, float)):
                        total += abs(float(n))
        except Exception:  # noqa: BLE001
            continue
    return total



def _collect_price_history():
    """辅助：读滚动价格历史（供上下文的「近期价格」用）。"""
    return _load_price_history()


def _fetch_filters(symbols, testnet: bool = True) -> dict[str, dict]:
    """批量取每只标的的交易所下单规则（minNotional/step/minQty/qtyPrec）。

    这是把「交易所规则」喂给 LLM 的关键：LLM 要能看到门槛才报得出可成交的单。
    失败时返回空 dict（下游会保守处理）。
    """
    from core.binance_exec import BinanceExec
    out: dict[str, dict] = {}
    if not symbols:
        return out
    ex = BinanceExec("x", "x", testnet=testnet)
    # exchangeInfo 一次拿全，避免逐标的请求
    try:
        info = ex.exchange_info()
    except Exception:  # noqa: BLE001
        return out
    by_sym = {s["symbol"]: s for s in info.get("symbols", [])}
    for sym in symbols:
        s = by_sym.get(sym)
        if not s:
            continue
        f = {x["filterType"]: x for x in s.get("filters", [])}
        lot = f.get("LOT_SIZE", {})
        out[sym] = {
            "step": float(lot.get("stepSize") or 0),
            "minQty": float(lot.get("minQty") or 0),
            "tick": float(f.get("PRICE_FILTER", {}).get("tickSize") or 0),
            "minNotional": float(f.get("MIN_NOTIONAL", {}).get("notional") or 0),
            "qtyPrec": int(s.get("quantityPrecision", 8)),
            "pricePrec": int(s.get("pricePrecision", 8)),
        }
    return out


def _llm_direct_orders(targets, cur, prices, filt, *, equity, macro, trig,
                       tradable=None, max_notional=500.0, gross_cap=1.2,
                       leverage=1, recent_decisions=None):
    """让 LLM 看到完整上下文，直接产出订单。返回 (ValidationResult, llm_intents)。

    LLM 的定位（用户要求）：**既参与调仓，也对因子方向做把控**。
      - 它能看到因子选出的标的与权重（factor_reference_targets），
      - 但不受其约束：宏观/新闻/风险判断与因子冲突时，它可以否决、反转、
        减半或跳过因子选出的标的 —— 因子只是「建议」，LLM 是因子层的最终决策者。
      - 同时它会看到自己最近的决策历史，避免自相矛盾。
    """
    from core.llm_order import (
        build_context, llm_decide_orders, validate_llm_orders, log_decision,
    )
    trig_reasons = list(getattr(trig, "reasons", []) or []) if trig else []
    ctx = build_context(
        equity=equity, cur_positions=cur, prices=prices, filt=filt,
        factor_targets=targets,
        macro=macro or {},
        trigger={"reasons": trig_reasons,
                 "evidence": getattr(trig, "evidence", {})} if trig else {},
        tradable=tradable, max_notional=max_notional, gross_cap=gross_cap,
        leverage=leverage,
        recent_decisions=recent_decisions or [],
    )
    intents = llm_decide_orders(ctx)
    llm_ok = bool(intents.get("_ok")) if isinstance(intents, dict) else False
    vr = validate_llm_orders(
        intents, equity=equity, prices=prices, cur_positions=cur, filt=filt,
        tradable=tradable, gross_cap=gross_cap, max_notional=max_notional)
    # 简短整体理由：优先 LLM 总理由，其次各笔 reason 拼接
    per_order_reasons = [
        str(it.get("reason"))[:80] for it in (intents.get("orders") or [])
        if isinstance(it, dict) and it.get("reason")
    ] if isinstance(intents, dict) else []
    overall_reason = (intents.get("reason") if isinstance(intents, dict) else None) \
        or "; ".join(per_order_reasons)
    log_decision({
        "ts": _now(), "equity": equity,
        "llm_ok": llm_ok,
        "source": "llm" if vr.orders else "llm_empty",
        "trigger": ",".join(trig_reasons),
        "trigger_reason": _trigger_reason_text(trig),
        "n_intents": vr.n_in, "n_orders": len(vr.orders),
        "gross_after_usd": vr.gross_after_usd,
        "intents": intents.get("orders") if isinstance(intents, dict) else None,
        "llm_reason": overall_reason,
        "rejected": vr.rejected,
        "accepted": [{"symbol": o.binance_symbol, "side": o.side,
                      "action": o.action, "notional": round(o.notional, 2)}
                     for o in vr.orders],
    })
    vr.llm_ok = llm_ok
    vr.reason = overall_reason
    return vr, intents


def _trigger_reason_text(trig) -> str:
    """把触发原因压成一句人话，供决策日志记录。"""
    if not trig:
        return ""
    reasons = getattr(trig, "reasons", []) or []
    ev = getattr(trig, "evidence", {}) or {}
    parts = []
    if "needs_rebalance" in reasons:
        nb = ev.get("needs_rebalance", {})
        parts.append(f"因子要调仓 {nb.get('n_orders', '?')} 笔")
    if "price_anomaly" in reasons:
        pa = ev.get("price_anomaly", {})
        parts.append(f"价格异动 {pa.get('sym', '')} z={pa.get('z', '?')}")
    if "drawdown_alert" in reasons:
        da = ev.get("drawdown_alert", {})
        parts.append(f"浮亏 {da.get('loss_pct', '?')}%")
    if "rebalance_due" in reasons:
        parts.append(f"例行调仓(距上次 {ev.get('rebalance_due', {}).get('days_since', '?')} 天)")
    if "llm_review_due" in reasons:
        parts.append("LLM 定期复评")
    return "; ".join(parts) or ",".join(reasons)


def _recent_decisions(limit: int = 5) -> list[dict]:
    """读最近 N 条【实际执行过】的决策，用于喂给 LLM 防自相矛盾。

    只取真正下过单（accepted 非空）的记录，每条给：时间、做了什么、理由。
    这样 LLM 能看到「我上次/上上次对同一标的做了什么、为什么」，
    避免像 6 分钟内 MSTR 加空又减空这种自打脸。
    """
    from core.llm_order import DECISION_LOG as _L
    if not _L.exists():
        return []
    out: list[dict] = []
    for line in _L.read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        acc = r.get("accepted") or []
        if not acc:
            continue
        # 简短理由：优先每笔自带的 reason（intents 里），退回整体 reason
        reasons = []
        for it in (r.get("intents") or []):
            if isinstance(it, dict) and it.get("reason"):
                reasons.append(str(it["reason"])[:80])
        out.append({
            "ts": r.get("ts"),
            "source": r.get("source") or ("llm" if r.get("llm_ok") else "factor"),
            "trigger": r.get("trigger"),
            "did": [{"symbol": a.get("symbol"), "side": a.get("side"),
                     "action": a.get("action"), "notional": a.get("notional")}
                    for a in acc],
            "reason": (r.get("llm_reason") or r.get("reason") or
                       ("; ".join(reasons) if reasons else ""))[:160],
        })
    return out[-limit:]


def _last_llm_decision_ts() -> str | None:
    """读上次 LLM 直接下单决策的时间（用于节流 LLM 主动评估频率）。"""
    from core.llm_order import DECISION_LOG as _L
    if not _L.exists():
        return None
    last = None
    for line in _L.read_text().splitlines():
        try:
            r = json.loads(line)
            if r.get("ts"):
                last = r["ts"]
        except Exception:  # noqa: BLE001
            continue
    return last


def _ask_llm(trig, targets, cur, prices, model: str | None = None):
    """唤醒 LLM 分析。失败不影响主流程（返回 None，退回纯规则）。

    free 模型常不守 JSON 格式 → 解析失败时用关键词兜底抠 decision。
    """
    try:
        from core.trigger_engine import build_llm_prompt
        from llm.client import chat, extract_json
        msgs = build_llm_prompt(trig, targets, cur, prices)
        raw = chat(msgs, temperature=0.1, max_tokens=800, timeout=120)
        # extract_json 在文本里抠不出合法 JSON 时会抛异常 → 单独包住
        parsed = None
        try:
            parsed = extract_json(raw)
        except Exception:  # noqa: BLE001
            parsed = None
        if isinstance(parsed, list):
            parsed = parsed[0] if parsed else {}
        # 兜底：解析不出 dict 时，从文本里抠关键词
        if not isinstance(parsed, dict) or not parsed.get("decision"):
            parsed = _fallback_parse(raw)
        return {"raw": raw[:600], "parsed": parsed}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "parsed": _fallback_parse("")}


def _fallback_parse(text: str) -> dict:
    """从自由文本里兜底识别 decision 关键词。"""
    t = (text or "").upper()
    for kw, dec in (("REDUCE_RISK", "REDUCE_RISK"), ("REDUCE RISK", "REDUCE_RISK"),
                    ("降风险", "REDUCE_RISK"), ("降低敞口", "REDUCE_RISK"),
                    ("REBALANCE", "REBALANCE"), ("调仓", "REBALANCE"),
                    ("HOLD", "HOLD"), ("持有", "HOLD"), ("不动", "HOLD")):
        if kw in t:
            return {"decision": dec, "confidence": None,
                    "reason": "关键词兜底解析（LLM 未按 JSON 返回）"}
    return {"decision": None, "confidence": None, "reason": "无法解析"}


def tick(factor: str = "mom250", equity: float = 1000.0, quantile: float = 0.2,
         neutralize: str = "none", leverage: int = 1, max_notional: float = 500.0,
         min_notional: float = 5.0, live: bool = False, force: bool = False,
         use_llm: bool = True, max_daily_rebalances: int = 6,
         gross_cap: float = 1.2, notify_target: str | None = "qqbot",
         max_legs: int = 0, use_macro: bool = True,
         use_llm_orders: bool = False,
         max_drawdown_pct: float = 0.10,
         daily_loss_limit_pct: float = 0.03,
         max_daily_turnover_x: float = 3.0,
         derisk_floor: float = 0.0,
         funding_max_ann: float | None = 0.30,
         per_symbol_daily_cap: int = 4,
         per_symbol_flip_cap: int = 2,
         aggr_drift_pct: float = 0.10,
         min_rebalance_interval_min: float = 240.0,
         structural_drift_pct: float = 0.30) -> dict:
    """跑一次完整流水线（事件驱动）。"""
    from core.us_mapping import is_market_open
    from core.bridge_us_binance import (
        compute_targets, build_orders, execute_orders,
        load_positions_from_exchange,
    )
    from core.binance_exec import BinanceExec
    from core.trigger_engine import check_triggers

    _book_name = get_book() or "lowfreq"
    rec: dict = {"ts": _now(), "factor": factor, "equity": equity,
                 "book": _book_name,
                 "neutralize": neutralize, "live": live, "use_llm": use_llm, "ok": False}

    # --- 1) 开市判断 ---
    # 2026-10-22 改：原逻辑在此处直接 return，未写 ok 字段 → 落盘 ok=False + status=silent。
    # 但【静默】是正常状态（无触发条件、闸门拦下），不是失败。落 ok=False 会让
    # 巡检脚本/告警把「正常静默」误判成错误，日志里 1758/1885 条假异常就是这么来的。
    # 现在：静默类分支统一标记 ok=True + silent=True，真正的失败路径（exec_error /
    # abort_* / calendar_error / 账户读取失败）仍保留 ok=False。
    today = datetime.now(timezone.utc).date()
    if not force:
        try:
            if not is_market_open(today):
                rec.update(status="market_closed", ok=True,
                           note=f"{today} 非美股开市日")
                heartbeat(rec)
                return rec
        except Exception as e:  # noqa: BLE001
            rec.update(status="calendar_error", error=str(e))
            heartbeat(rec)
            return rec

    spec = _resolve_spec(factor)
    testnet = True  # 硬编码：只允许 Demo

    # --- 1.5) 宏观介入（LLM 适时判断，可缩小敞口）---
    macro_mult = 1.0
    if use_llm and use_macro:
        try:
            from core.macro_watch import macro_intervention
            mv = macro_intervention(today)
            rec["macro"] = {"regime": mv.get("regime"),
                            "exposure_mult": mv.get("exposure_mult"),
                            "reason": mv.get("reason"),
                            "event": mv.get("macro_ctx", {}).get("event_today"),
                            "n_news": mv.get("n_news")}
            m = mv.get("exposure_mult")
            if isinstance(m, (int, float)) and 0 < m <= 1.5:
                macro_mult = float(m)
        except Exception as e:  # noqa: BLE001
            rec["macro_error"] = str(e)

    # --- 2) 算目标持仓 ---
    try:
        targets, used_date = compute_targets(
            spec, str(today), quantile, testnet=testnet, neutralize=neutralize,
            max_legs=max_legs, equity=equity)
        # 应用宏观敞口系数（risk_off → 缩小所有目标权重）
        if macro_mult != 1.0 and targets:
            for t in targets:
                t.weight *= macro_mult
            rec["macro_applied"] = macro_mult
            # 缩仓后剔除过小的腿（宏观保守时应少持有，而非下碎单）
            from core.bridge_us_binance import _min_notional
            mn = _min_notional(testnet)
            kept = [t for t in targets if abs(t.weight * equity) >= mn]
            if kept:
                targets = kept
                rec["macro_pruned"] = True

        # --- 2b) 新闻修正（2026-10-23 新增）---
        # 与上面的 macro_mult 是两条**独立**通道，故意不合并：
        #   macro_mult 来自 Tavily 实时新闻 + LLM 宏观判断（老路径，已跑了很久）
        #   news_adjust 来自 Marketaux 个股新闻 + 逐票 LLM 判断（新路径）
        # 两者信息源不同、时间尺度不同，各自修正后相乘才是完整图景。
        # 新闻层只调整**已有** target 的权重，绝不新增标的（选股归因子层）。
        # 任何一个环节出错都不阻断交易主流程 —— 降级为「新闻层不介入」。
        news_applied: dict[str, dict] = {}
        if targets and os.getenv("ENABLE_NEWS_ADJUST", "1") == "1":
            try:
                from core.news_adjust import load_latest, get_adjustment
                _nm, _nps = load_latest()
                if _nps or _nm:
                    adjusted = []
                    for t in targets:
                        adj = get_adjustment(t.ticker, macro=_nm, per_symbol=_nps)
                        if adj["applied"]:
                            t.weight *= adj["multiplier"]
                            news_applied[t.ticker] = {
                                "mult": adj["multiplier"],
                                "reasons": adj["reasons"],
                            }
                        adjusted.append(t)
                    targets = adjusted
                    if news_applied:
                        rec["news_adjusted"] = news_applied
                        # 新闻缩仓后同样要剔碎单
                        from core.bridge_us_binance import _min_notional as _mn2
                        _mnv = _mn2(testnet)
                        _kept = [t for t in targets if abs(t.weight * equity) >= _mnv]
                        if _kept and len(_kept) < len(targets):
                            targets = _kept
                            rec["news_pruned"] = True
            except Exception as e:  # noqa: BLE001
                rec["news_adjust_error"] = str(e)[:200]

        rec["signal_date"] = used_date
        rec["n_targets"] = len(targets)
    except Exception as e:  # noqa: BLE001
        rec.update(status="signal_error", error=str(e), trace=traceback.format_exc()[-600:])
        heartbeat(rec)
        return rec

    # --- 2.5) 资金费率成本闸门（永续独有，A股/美股现货没有）---
    #   永续持仓即使不动也在每 8h 收/付费率。若某腿的年化持有成本过高，
    #   它会持续侵蚀因子 alpha。此处按「预期持有天数」估出该腿的持有成本，
    #   超过 alpha 预算上限的腿直接剔除（宁可不持有高费率腿）。
    #
    #   口径：腿为多头 → 付 ann_long；腿为空头 → 收/付 ann_short。
    #   expected_hold_days 默认 10（与挖矿 hold 网格的中枢一致）。
    if funding_max_ann is not None and targets:
        try:
            from core.funding_cost import funding_stat
            from core.us_mapping import build_mapping
            mapped, _ = build_mapping()
            sym2tkr = {m.binance_symbol: m.us_ticker for m in mapped}
            kept_f: list = []
            dropped_f: list[dict] = []
            for t in targets:
                tk = t.binance_symbol if t.binance_symbol in sym2tkr else sym2tkr.get(t.binance_symbol, t.binance_symbol)
                st = funding_stat(t.binance_symbol, tk)
                if st is None:
                    kept_f.append(t)      # 无费率数据 → 不拦（保守）
                    continue
                # 该腿方向的年化费率成本（正=付出成本）
                #   ann_long  = 做多的年化成本（正=付）
                #   ann_short = 做空的年化成本（正=付，恰为 -ann_long）
                # 所以两条腿都直接取对应字段，不做额外取负。
                ann_cost = st.ann_long if t.weight >= 0 else st.ann_short
                if ann_cost > funding_max_ann:
                    dropped_f.append({
                        "symbol": t.binance_symbol, "weight": round(t.weight, 4),
                        "ann_cost": round(ann_cost, 4),
                    })
                else:
                    kept_f.append(t)
            if dropped_f:
                targets = kept_f
                rec["funding_pruned"] = dropped_f
                rec["n_targets"] = len(targets)
        except Exception as e:  # noqa: BLE001
            rec["funding_cost_error"] = str(e)

    # --- 3) 拉价格（公开，无需 key） ---
    client = BinanceExec("x", "x", testnet=testnet)
    prices: dict[str, float] = {}
    for t in targets:
        try:
            tk = client.ticker(t.binance_symbol)
            lp = tk.get("lastPrice")
            if lp is not None:
                prices[t.binance_symbol] = float(lp)
        except Exception:  # noqa: BLE001
            pass
    rec["n_prices"] = len(prices)

    # --- 4) 读持仓 + 算 delta ---
    # 账本语义：非默认盘用【本盘自有持仓账】算 delta（只认自己发过的单），
    # 不用交易所合计持仓 —— 否则会把别的盘的仓当成自己的、在别人仓上加加减减。
    _bk = get_book()
    if _bk:
        from core.book import load_book_positions
        cur = load_book_positions(_bk)
        rec["book_positions_source"] = "book_ledger"
    else:
        try:
            cur = load_positions_from_exchange(testnet=testnet)
        except Exception as e:  # noqa: BLE001
            rec.update(status="positions_error", error=str(e))
            heartbeat(rec)
            return rec
    rec["current_positions"] = cur

    # 交易所真实精度：每个标的的 stepSize / minQty 不同（如 SPCXUSDT 是整股合约）。
    # 不传下去的话 build_orders 会用全局 step=0.01，导致碎仓被发单侧抬量（见其 docstring）。
    # 复用上面已有的公开 client（精度是公共接口，不需要 key）。
    _steps: dict[str, float] = {}
    _minq: dict[str, float] = {}
    try:
        for _s in sorted({t.binance_symbol for t in targets} | set(cur)):
            try:
                _f = client.symbol_filters(_s)
                _steps[_s] = float(_f.get("step") or 0.0)
                _minq[_s] = float(_f.get("minQty") or 0.0)
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        rec["filters_error"] = str(e)
    rec["n_filters"] = len(_steps)

    orders = build_orders(targets, equity, prices, cur, min_notional=min_notional,
                          steps=_steps, min_qtys=_minq)
    rec["n_orders"] = len(orders)
    rec["orders"] = [o.__dict__ for o in orders]

    # --- 4b) 未实现盈亏（用于风控触发）---
    #   注意：account_summary 是签名接口，必须用 from_env（带真实 key）的客户端。
    #   用占位 client（上面的 x/x）会 -2014 API-key format invalid，导致 unrealized 恒为 0，
    #   从而使 drawdown_alert 触发永远失效。这里单独建带凭据的客户端。
    unrealized = 0.0
    try:
        from core.binance_exec import BinanceExec
        acct_client = BinanceExec.from_env(testnet=testnet)
        acct = acct_client.account_summary()
        unrealized = float(acct.get("totalUnrealizedProfit", 0) or 0)
    except Exception as e:  # noqa: BLE001
        rec["account_error"] = str(e)
    rec["unrealized_pnl"] = unrealized

    # --- 5) 触发引擎 ---
    # 更新滚动价格历史（供 price_anomaly 使用），并读历史传给触发引擎
    _update_price_history(prices)
    price_hist = _load_price_history()
    trig = check_triggers(
        targets=targets, prices=prices, cur_positions=cur, equity=equity,
        unrealized_pnl=unrealized, orders=orders,
        last_rebalance_ts=_last_rebalance_ts(),
        price_history=price_hist,
        llm_review=use_llm_orders,
        last_llm_ts=_last_llm_decision_ts() if use_llm_orders else None,
        q={"aggr_drift_pct": aggr_drift_pct,
           "min_rebalance_interval_min": min_rebalance_interval_min},
    )
    rec["triggered"] = trig.fired
    rec["trigger_reasons"] = trig.reasons
    rec["trigger_evidence"] = trig.evidence

    if not trig.fired:
        rec.update(status="silent", ok=True, silent=True,
                   note="无触发条件，静默")
        heartbeat(rec)
        return rec

    # --- 6) 唤醒 LLM（可选）---
    # 两条路径：
    #   (A) use_llm_orders=True → LLM 看到「持仓+交易所规则+额度+价格」，直接产出订单
    #   (B) 默认 → LLM 只给 decision（HOLD/REBALANCE/REDUCE_RISK），订单仍由因子生成
    llm_out = None
    decision = None
    if use_llm and use_llm_orders:
        try:
            # 收集要喂给 LLM 的标的：因子目标 + 当前持仓 + 参考报价池
            from core.us_mapping import live_quote_symbols
            cand_syms = {t.binance_symbol for t in targets} | set(cur)
            try:
                tradable = [s for s in live_quote_symbols(testnet=testnet)]
            except Exception:  # noqa: BLE001
                tradable = []
            # 确保持仓标的在可交易集合里（否则无法平仓）
            trad_set = set(tradable) if tradable else set()
            for s in cur:
                trad_set.add(s)
            fetch_syms = sorted(trad_set | cand_syms)
            filt = _fetch_filters(fetch_syms, testnet=testnet)
            # 给 LLM 的价格池：候选 + 持仓里有报价的
            llm_prices = dict(prices)
            for s in fetch_syms:
                if s in llm_prices:
                    continue
                try:
                    tk = client.ticker(s)
                    lp = tk.get("lastPrice")
                    if lp:
                        llm_prices[s] = float(lp)
                except Exception:  # noqa: BLE001
                    pass
            vr, intents = _llm_direct_orders(
                targets, cur, llm_prices, filt, equity=equity,
                macro=rec.get("macro"), trig=trig,
                tradable=sorted(trad_set) if trad_set else None,
                max_notional=max_notional, gross_cap=gross_cap,
                leverage=leverage,
                # 传最近真实成交给 LLM —— 让它看到「自己上几次做了什么」，
                # 避免像 6 分钟内对同一标的来回买卖这种自打脸。
                recent_decisions=_recent_decisions(5))
            rec["llm_orders"] = {
                "n_intents": vr.n_in, "n_orders": len(vr.orders),
                "gross_after_usd": vr.gross_after_usd,
                "reason": intents.get("reason") if isinstance(intents, dict) else None,
                "rejected": vr.rejected,
                "raw": ((intents.get("_raw") or "") if isinstance(intents, dict) else "")[:300],
            }
            # --- 优先级/降级链（用户要求）---
            #   ① 有 LLM 且它给了【可成交订单】 → 用 LLM 的（脚本建议仅供参考）
            #   ② LLM 成功但【决定不下单】（空 orders，有明确理由）→ 尊重 LLM，不发单
            #   ③ LLM 失败/超时/解析不出（_ok=False）→ 退回【纯脚本】因子订单兜底
            # 这样"LLM 优先、脚本兜底"，不会因为 LLM 挂了就什么都不做。
            if vr.llm_ok and vr.orders:
                orders = vr.orders
                rec["order_source"] = "llm"
                rec["fallback"] = False
            elif vr.llm_ok and not vr.orders:
                # LLM 明确判断（可能是不该动 / 报的单都被交易所门槛剔了）
                if vr.n_in > 0:
                    # 报了单但全部不合格 → 视为无效决策，退回脚本兜底更安全
                    rec["order_source"] = "factor_fallback(llm_orders_all_invalid)"
                    rec["fallback"] = True
                else:
                    # LLM 主动选择不下单 → 尊重它
                    orders = []
                    rec["order_source"] = "llm_empty"
                    rec["fallback"] = False
            else:
                # LLM 不可用 → 纯脚本因子订单兜底
                rec["order_source"] = "factor_fallback(llm_unavailable)"
                rec["fallback"] = True
            rec["n_orders"] = len(orders)
            rec["orders"] = [o.__dict__ for o in orders]
        except Exception as e:  # noqa: BLE001
            # LLM 路径整体异常 → 退回纯脚本订单作为兜底
            rec["llm_orders_error"] = str(e)
            rec["order_source"] = "factor_fallback(llm_exception)"
            rec["fallback"] = True
            rec["n_orders"] = len(orders)
            rec["orders"] = [o.__dict__ for o in orders]
    elif use_llm:
        llm_out = _ask_llm(trig, targets, cur, prices)
        rec["llm"] = llm_out
        if llm_out and isinstance(llm_out.get("parsed"), dict):
            decision = llm_out["parsed"].get("decision")
    rec["llm_decision"] = decision

    # --- 7) 决策合并：LLM 建议 + 硬规则 ---
    # live 无人值守时的保命闸门，任何一条不通过就只记录不发单。
    guard = {"proceed": bool(orders), "blocks": []}

    # 闸1：LLM 明确说 HOLD 且不是风控告警 → 不发单
    if decision == "HOLD" and "drawdown_alert" not in trig.reasons:
        guard["proceed"] = False
        guard["blocks"].append("llm_said_hold")

    # 闸2：日内调仓次数上限 —— 防震荡市来回打（daily cap）。
    #   实测教训：没有这道闸时，$100 账户 16 小时执行了 54 次调仓、MSTR 单标的
    #   来回打了 47 次，换手 23 倍本金、白交约 1.85% 手续费。小资金成本绝对值虽小，
    #   但「无意义高频换手」本身就是在磨损本金，必须设上限。
    #   只在【真正要发单】时计数（guard.proceed 对应有实质订单）。
    #   不过：两类情况必须穿透此闸 ——
    #   (a) 风控类触发（回撤/日亏熔断、风险降杠杆）：否则极端行情里「当日已打满 6 次」
    #       会连保命减仓都被挡住，风险裸奔；
    #   (b) 结构性漂移（单标的现实权重远超其目标权重，如刚被踢出因子库却仍残留大仓位）：
    #       这属于「该标的已不在目标书里」，不处理会一直虚占敞口、反复触发漂移。
    #   日常小额再平衡仍受 6 次/日上限约束，防震荡初衷不变。
    is_hard_risk = any(r in trig.reasons for r in
                       ("drawdown_alert", "daily_loss_limit", "derisk"))
    structural_drift = False
    try:
        _eq = float(equity or 0.0)
        if _eq > 0:
            for o in orders:
                cq = float(cur.get(o.binance_symbol, 0.0) or 0.0)
                tq = float(o.target_qty or 0.0)
                # 注意：prices 只覆盖 targets 里的标的，被踢出目标书的残仓（本项目正是
                # 这种情况）永远查不到 → 必须优先用订单自带的 ref_price。
                px = float(getattr(o, "ref_price", 0.0) or prices.get(o.binance_symbol) or 0.0)
                if px > 0 and abs(cq - tq) * px / _eq >= structural_drift_pct:
                    structural_drift = True
                    break
    except Exception as e:  # noqa: BLE001
        guard.setdefault("warnings", []).append(f"structural_drift_check_error:{e}")
    if structural_drift:
        guard["structural_drift"] = True

    if (guard["proceed"] and max_daily_rebalances > 0
            and not is_hard_risk and not structural_drift):
        today_str = datetime.now(timezone.utc).date().isoformat()
        done_today = _count_executions_today(today_str)
        if done_today >= max_daily_rebalances:
            guard["proceed"] = False
            guard["blocks"].append(
                f"daily_rebalance_cap({done_today}>={max_daily_rebalances})")

    # 闸2.5：单标的日内调仓 / 方向反转上限 —— 专治单标的震荡。
    #   背景：MSTR 曾在一天内被来回打 49 次（总次数上限拦不住单标的横跳）。
    #   规则：任一标的当日交易次数 > per_symbol_cap，或方向反转次数 > flip_cap，
    #        则本次【该标的的空单被剔除】（其余标的照常，避免殃及正常调仓）。
    #   只剔除"触发上限标的"的订单，不整单否决 —— 保护面最小、最精准。
    if (guard["proceed"] and (per_symbol_daily_cap > 0 or per_symbol_flip_cap > 0)):
        try:
            day_str = datetime.now(timezone.utc).date().isoformat()
            exec_cnt = _symbol_exec_counts_today(day_str)
            flips = _symbol_flips_today(day_str)
            blocked_syms = []
            for s, c in exec_cnt.items():
                if per_symbol_daily_cap > 0 and c >= per_symbol_daily_cap:
                    blocked_syms.append(s)
                elif per_symbol_flip_cap > 0 and flips.get(s, 0) >= per_symbol_flip_cap:
                    blocked_syms.append(s)
            # 结构性漂移豁免：该标的现状权重已远超目标（如被踢出因子库却残留大仓位），
            # 此时「别来回打」让位于「把错误敞口纠正回来」，否则会永久卡死在超限仓位上。
            if blocked_syms and structural_drift:
                _eq2 = float(equity or 0.0)
                still = []
                for s in blocked_syms:
                    _big = False
                    for o in orders:
                        if o.binance_symbol != s:
                            continue
                        cq = float(cur.get(s, 0.0) or 0.0)
                        tq = float(o.target_qty or 0.0)
                        px = float(o.ref_price or prices.get(s) or 0.0)
                        if _eq2 > 0 and px > 0 and abs(cq - tq) * px / _eq2 >= structural_drift_pct:
                            _big = True
                            break
                    if _big:
                        guard.setdefault("warnings", []).append(
                            f"per_symbol_cap_bypassed_structural_drift:{s}")
                    else:
                        still.append(s)
                blocked_syms = still
            if blocked_syms:
                kept = [o for o in orders if o.binance_symbol not in blocked_syms]
                if len(kept) != len(orders):
                    guard.setdefault("warnings", []).append(
                        f"per_symbol_cap_stripped:{','.join(sorted(set(blocked_syms)))}")
                    orders = kept
                    rec["orders"] = [o.__dict__ for o in orders]
                    rec["n_orders"] = len(orders)
                    if not orders:
                        guard["proceed"] = False
                        guard["blocks"].append(
                            f"per_symbol_cap(全部标的超限:{','.join(sorted(set(blocked_syms)))})")
        except Exception as e:  # noqa: BLE001
            guard.setdefault("warnings", []).append(f"per_symbol_check_error:{e}")

    # 闸3：总敞口上限 —— 看【调仓后的目标敞口】，不是本次交易名义。
    #   否则从小仓位切到大仓位时会被误拦，且降仓（目标敞口变小）应永远允许。
    target_syms = {o.binance_symbol for o in orders}
    # 目标总敞口 = 目标订单里每个 symbol 的目标数量 * 价格
    target_gross = 0.0
    for o in orders:
        target_gross += abs(o.target_qty * o.ref_price)
    reducing = target_gross <= equity * gross_cap + 1e-9
    if equity > 0 and not reducing:
        guard["proceed"] = False
        guard["blocks"].append(f"gross_cap(target={target_gross:.0f}>{equity*gross_cap:.0f})")

    # 闸4：最大回撤熔断 —— 浮动亏损超过本金阈值时，【强制主动降险】。
    #   分两级：
    #     (a) 本批订单若非净减仓 → 拦截（防在回撤中加仓）。
    #     (b) 主动补一组 force_derisk 减仓单，把总敞口按回撤比例压下去。
    #   这使得「浮亏越深、敞口越薄」成为硬规则，不依赖 LLM 是否配合。
    if equity > 0 and max_drawdown_pct > 0 and unrealized <= 0:
        dd_pct = -unrealized / equity
        if dd_pct >= max_drawdown_pct:
            # 判断本批订单是否「净减仓」
            def _is_risk_reducing(ords: list) -> bool:
                if not ords:
                    return True
                net = 0.0
                for o in ords:
                    # 目标敞口 < 当前敞口 即为减仓（用 target_qty 与当前持仓比较）
                    cur_q = cur.get(o.binance_symbol, 0.0)
                    delta_to_flat = abs(o.target_qty) - abs(cur_q)
                    net += delta_to_flat
                return net <= 1e-9

            if not _is_risk_reducing(orders):
                guard["proceed"] = False
                guard["blocks"].append(
                    f"max_drawdown_breaker(dd={dd_pct*100:.1f}%>={max_drawdown_pct*100:.0f}%,非减仓单被拦)")
            # (b) 主动降险：无论 LLM 怎么想，都补一组减仓单把敞口压到目标以下
            try:
                from core.bridge_us_binance import force_derisk
                derisk = force_derisk(
                    targets, equity, prices, cur, dd_pct,
                    gross_cap=gross_cap, min_notional=min_notional,
                    derisk_floor=derisk_floor)
                if derisk:
                    orders = derisk
                    guard["proceed"] = True       # 降险单必须能发出去
                    guard["risk_reducing"] = True
                    guard["active_derisk"] = {
                        "dd_pct": round(dd_pct, 4),
                        "n_orders": len(derisk),
                        "target_gross": round(gross_cap * equity * (1 - min(dd_pct, 0.95)), 2),
                    }
                    guard["blocks"] = [b for b in guard["blocks"]
                                       if not b.startswith("max_drawdown_breaker")]
                    rec["active_derisk"] = guard["active_derisk"]
                elif _is_risk_reducing(orders):
                    guard["risk_reducing"] = True
            except Exception as e:  # noqa: BLE001
                guard.setdefault("warnings", []).append(f"force_derisk_error:{e}")

    # 闸5：日内亏损熔断 —— 当日已实现亏损超过本金阈值时，禁止加仓（只允许减仓）。
    if equity > 0 and daily_loss_limit_pct > 0 and guard["proceed"]:
        try:
            day_str = datetime.now(timezone.utc).date().isoformat()
            realized = _daily_realized_pnl(day_str)
            if realized < 0 and (-realized / equity) >= daily_loss_limit_pct:
                # 只放行净减仓
                net = 0.0
                for o in orders:
                    net += abs(o.target_qty) - abs(cur.get(o.binance_symbol, 0.0))
                if net > 1e-9:
                    guard["proceed"] = False
                    guard["blocks"].append(
                        f"daily_loss_breaker(loss={-realized:.2f}={-realized/equity*100:.1f}%>={daily_loss_limit_pct*100:.0f}%,禁止加仓)")
        except Exception as e:  # noqa: BLE001
            guard.setdefault("warnings", []).append(f"daily_loss_check_error:{e}")

    # 闸6：日内换手率上限 —— 当日累计成交名义额超过 equity 的倍数上限时停手。
    #   防止「看起来每次只调一点」，累计却被手续费抽干（历史教训：16 小时换手 23 倍本金）。
    if equity > 0 and max_daily_turnover_x > 0 and guard["proceed"]:
        try:
            day_str = datetime.now(timezone.utc).date().isoformat()
            turnover = _daily_turnover_usd(day_str)
            if turnover >= equity * max_daily_turnover_x:
                guard["proceed"] = False
                guard["blocks"].append(
                    f"daily_turnover_cap(turnover={turnover:.0f}={turnover/equity:.1f}x>={max_daily_turnover_x:.1f}x)")
        except Exception as e:  # noqa: BLE001
            guard.setdefault("warnings", []).append(f"turnover_check_error:{e}")

    rec["guard"] = guard
    rec["proceed"] = guard["proceed"]

    if not guard["proceed"]:
        rec.update(status="no_action", ok=True, silent=True,
                   note=f"触发但被闸门拦下: {guard['blocks'] or '无实质订单'}")
        heartbeat(rec)
        _append(DECISION_LOG, rec)
        return rec

    # --- 8) 执行（仅 live 且 Demo）---
    # 闸4：执行前重读持仓，确认不是基于过期数据下单（防重复开仓）
    # 非默认盘：重读**本盘自有账**（本盘不会被别的盘改动，一般不触发 stale）。
    if live:
        try:
            if _bk:
                from core.book import load_book_positions
                cur2 = load_book_positions(_bk)
            else:
                cur2 = load_positions_from_exchange(testnet=testnet)
            if cur2 != cur:
                rec.update(status="abort_stale",
                           note="执行前持仓已变化，放弃本次（防重复开仓）",
                           cur_before=cur, cur_now=cur2)
                heartbeat(rec)
                _append(DECISION_LOG, rec)
                return rec
        except Exception as e:  # noqa: BLE001
            rec.update(status="abort_recheck_fail", error=str(e))
            heartbeat(rec)
            return rec

    dry = not live
    try:
        results = execute_orders(
            orders, testnet=testnet, leverage=leverage,
            max_notional=max_notional, dry=dry)
        rec["results"] = results
        sent = [r for r in results if r.get("status") == "sent"]
        rec["n_sent"] = len(sent)
        rec["executed"] = bool(sent)
        rec["status"] = "dry_run" if dry else ("executed" if sent else "no_send")
    except Exception as e:  # noqa: BLE001
        rec.update(status="exec_error", error=str(e), trace=traceback.format_exc()[-600:])
        heartbeat(rec)
        return rec

    # --- 8.4) 非默认盘：把本次成交量累加进【本盘自有持仓账】---
    #   这是「每盘只认自己下的单」的落点：只有真正成交（sent）才更新自有账。
    if _bk and not dry:
        try:
            from core.book import apply_book_fills
            # 按 symbol 匹配（execute_orders 可能跳过某些单，不能靠 zip 顺序）
            by_sym = {o.binance_symbol: o for o in orders}
            fills = []
            for r in results:
                if r.get("status") != "sent":
                    continue
                o = by_sym.get(r.get("symbol"))
                if o is None:
                    continue
                signed = abs(o.delta_qty) if o.side == "BUY" else -abs(o.delta_qty)
                fills.append({"symbol": o.binance_symbol, "signed_qty": signed})
            if fills:
                rec["book_positions_after"] = apply_book_fills(fills, _bk)
        except Exception as e:  # noqa: BLE001
            rec["book_fill_error"] = str(e)

    rec["ok"] = True
    heartbeat(rec)
    _write_state(rec)
    _append(DECISION_LOG, rec)

    # --- 8.5) 自动对账：把「本地期望持仓」与「交易所真实持仓」比对，漂移即告警 ---
    #   为什么要每 tick 做：手工平仓、别的会话/脚本动过仓位、部分成交、
    #   费率结算导致的细微差异，都会让本地状态与实际脱节。脱节后算 delta
    #   会重复开仓或漏平。这里只做【持仓漂移检测】（轻量、无外部依赖），
    #   滑点/费率对账（需要拉成交明细）按需在 --reconcile 时做。
    #
    #   注意：非默认盘【跳过】此步。因为该盘自有账(book_positions) 本就 ≠ 交易所
    #   合计持仓（合计=各盘叠加），比对必然"漂移"，无意义。自有账由成交回填，
    #   不会漂移。
    if live and not _bk:
        try:
            from core.bridge_us_binance import load_positions_from_exchange as _lp
            ex_pos = _lp(testnet=testnet)
            # 期望持仓 = 本次执行后应有的目标持仓（用 orders 的 target_qty 重建）
            expected: dict[str, float] = dict(ex_pos)  # 以交易所为准做基线
            drift: list[dict] = []
            # 与本地状态文件比对
            from core.bridge_us_binance import load_positions as _loc
            local = _loc()
            for sym in set(ex_pos) | set(local):
                a = float(ex_pos.get(sym, 0.0))
                b = float(local.get(sym, 0.0))
                px = float(prices.get(sym, 0.0) or 0.0)
                dq = a - b
                dnotional = abs(dq * px) if px else abs(dq)
                # 有意噪声阈值：相对差 > 1% 且名义额 > $5 才算漂移
                if abs(dq) > 1e-9 and (abs(a) > 1e-9 and abs(dq) / max(abs(a), 1e-9) > 0.01
                                       or dnotional > 5.0):
                    drift.append({"symbol": sym, "exchange_qty": a,
                                  "local_qty": b, "delta_notional": round(dnotional, 2)})
            if drift:
                rec["reconcile_drift"] = drift
                if guard is not None:
                    guard.setdefault("warnings", []).append(
                        f"position_drift:{len(drift)}_legs")
                # 漂移即纠正本地状态，避免下一次算 delta 出错
                try:
                    from core.bridge_us_binance import save_positions as _sp
                    _sp(ex_pos)
                except Exception:  # noqa: BLE001
                    pass
                if notify_target:
                    try:
                        from core.notify import notify
                        notify({**rec, "status": "reconcile_drift",
                                "note": f"持仓漂移 {len(drift)} 腿，已同步本地状态"},
                               target=notify_target)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            rec["reconcile_error"] = str(e)

    # --- 9) 调仓通知（只在真执行时推送到 QQ）---
    if rec.get("executed") and notify_target:
        try:
            from core.notify import notify
            rec["notified"] = notify(rec, target=notify_target)
        except Exception as e:  # noqa: BLE001
            rec["notify_error"] = str(e)
    return rec


def run(interval: int, live: bool, use_llm: bool, **kw) -> None:
    """常驻：静默轮询，触发才唤醒 LLM。"""
    print(f"[trader_daemon] 常驻启动 interval={interval}s live={live} llm={use_llm}",
          flush=True)
    while True:
        try:
            rec = tick(live=live, use_llm=use_llm, **kw)
            print(f"[{rec['ts']}] status={rec.get('status')} "
                  f"trig={rec.get('trigger_reasons')} "
                  f"orders={rec.get('n_orders', 0)} sent={rec.get('n_sent', 0)} "
                  f"llm={rec.get('llm_decision')}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[trader_daemon] tick 异常: {e}", flush=True)
            heartbeat({"ts": _now(), "status": "fatal", "error": str(e)})
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="量化交易主脑（Demo 事件驱动）")
    ap.add_argument("--book", default=None,
                    help="账本名（多盘隔离）：空=默认盘；'lowfreq'/'resonance' 各用独立台账，各自独立跑")
    ap.add_argument("--factor", default="mom250", choices=list(FACTORS))
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--quantile", type=float, default=0.2)
    ap.add_argument("--neutralize", choices=["none", "sector"], default="none")
    ap.add_argument("--leverage", type=int, default=1)
    ap.add_argument("--max-notional", type=float, default=500.0)
    ap.add_argument("--min-notional", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="只跑一次")
    ap.add_argument("--loop", action="store_true", help="常驻循环")
    ap.add_argument("--interval", type=int, default=300, help="轮询间隔秒（默认 5 分钟）")
    ap.add_argument("--no-llm", dest="use_llm", action="store_false", default=True,
                    help="跳过 LLM，纯规则")
    ap.add_argument("--live", action="store_true",
                    help="允许真发单（仅 Demo）。不加则全程 dry")
    ap.add_argument("--force", action="store_true", help="忽略开市判断")
    ap.add_argument("--max-daily-rebalances", type=int, default=6,
                    help="live 模式日内调仓次数上限（默认 6；防止震荡市高频来回打）")
    ap.add_argument("--structural-drift-pct", type=float, default=0.30,
                    help="单标的偏离目标的名义额占权益比例超过此值即视为结构性漂移，"
                         "可穿透日内调仓次数上限（默认 0.30 = 30%% 权益）")
    ap.add_argument("--gross-cap", type=float, default=1.2,
                    help="目标组合总敞口上限（equity 的倍数，默认 1.2）")
    ap.add_argument("--notify", dest="notify_target", default="qqbot",
                    help="调仓通知目标（hermes send 的 --to，如 qqbot；传 none 关闭）")
    ap.add_argument("--max-legs", type=int, default=0,
                    help="每边最多持仓只数（0=按 quantile 自动）。小资金建议 1-2")
    ap.add_argument("--no-macro", dest="use_macro", action="store_false", default=True,
                    help="禁用宏观 LLM 介入")
    ap.add_argument("--llm-orders", dest="use_llm_orders", action="store_true",
                    default=False,
                    help="让 LLM 看到持仓+交易所规则后【直接产出订单】"
                         "（否则 LLM 只给 HOLD/REBALANCE 建议）")
    ap.add_argument("--max-drawdown-pct", type=float, default=0.10,
                    help="最大回撤熔断：浮亏达本金此比例时强制只允许减仓（默认 0.10=10%%，0 关闭）")
    ap.add_argument("--daily-loss-limit-pct", type=float, default=0.03,
                    help="日内亏损熔断：当日已实现亏损达本金此比例时禁止加仓（默认 0.03=3%%，0 关闭）")
    ap.add_argument("--max-daily-turnover-x", type=float, default=3.0,
                    help="日内换手上限：当日累计成交名义额达 equity 此倍数时停手（默认 3.0，0 关闭）")
    ap.add_argument("--derisk-floor", type=float, default=0.0,
                    help="主动降险时单腿占目标总敞口的最小比例（0=可一路压到平，默认 0）")
    ap.add_argument("--funding-max-ann", type=float, default=0.30,
                    help="资金费率闸门：腿的年化持有成本超过此值即剔除该腿（默认 0.30=30%%，负值关闭）")
    ap.add_argument("--per-symbol-daily-cap", type=int, default=4,
                    help="单标的日内调仓次数上限，专治单标的震荡（默认 4，0 关闭）")
    ap.add_argument("--per-symbol-flip-cap", type=int, default=2,
                    help="单标的日内方向反转次数上限（默认 2，0 关闭）")
    ap.add_argument("--aggr-drift-pct", type=float, default=0.10,
                    help="调仓日历：全组合累计漂移/equity 达此比例才调仓（默认 0.10=10%%，0 关闭日历闸）")
    ap.add_argument("--min-rebalance-interval-min", type=float, default=240,
                    help="调仓日历：两次实际调仓的最小间隔分钟数（默认 240=4h，0 关闭间隔闸）")
    args = ap.parse_args()

    # 账本隔离：必须在任何路径/台账操作前固定账本（重绑模块全局路径）
    set_book(args.book)

    nt = None if args.notify_target.lower() == "none" else args.notify_target
    kw = dict(factor=args.factor, equity=args.equity, quantile=args.quantile,
              neutralize=args.neutralize, leverage=args.leverage,
              max_notional=args.max_notional, min_notional=args.min_notional,
              force=args.force, max_daily_rebalances=args.max_daily_rebalances,
              gross_cap=args.gross_cap, notify_target=nt, max_legs=args.max_legs,
              use_macro=args.use_macro, use_llm_orders=args.use_llm_orders,
              max_drawdown_pct=args.max_drawdown_pct,
              daily_loss_limit_pct=args.daily_loss_limit_pct,
              max_daily_turnover_x=args.max_daily_turnover_x,
              derisk_floor=args.derisk_floor,
              funding_max_ann=(args.funding_max_ann if args.funding_max_ann >= 0 else None),
              per_symbol_daily_cap=args.per_symbol_daily_cap,
              per_symbol_flip_cap=args.per_symbol_flip_cap,
              aggr_drift_pct=args.aggr_drift_pct,
              min_rebalance_interval_min=args.min_rebalance_interval_min,
              structural_drift_pct=args.structural_drift_pct)

    if args.loop:
        run(args.interval, args.live, args.use_llm, **kw)
    else:
        rec = tick(live=args.live, use_llm=args.use_llm, **kw)
        print(json.dumps(rec, indent=1, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
