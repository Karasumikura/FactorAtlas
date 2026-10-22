"""共振策略运行器 —— 多周期共振系统的执行层。

复用现有基础设施：build_orders / execute_orders / book 账本 / notify。
自有的东西：逐标的状态（分批阶段、进场止损位）、分批建仓（30/30/40）、结构止损。

流程（每个 tick）：
  1. 读本盘持仓账 + 逐标的状态
  2. 对每个标的评估共振（4H 定方向 + 30m 找入场）
  3. 已持仓的：检查结构止损 / 止盈 → 生成减仓/平仓目标
  4. 未持仓的：评分达 normal/strong 且 30m 入场触发 → 生成首笔建仓目标（30%）
  5. 已持仓的：检查加仓条件（回踩不破 → 再 30%；突破前高 → 40%）
  6. 汇总目标权重 → build_orders → execute_orders → 写账本 → 播报

状态文件：data/binance/books/<book>/resonance_state.json
   {"KOUSDT": {"stage": 1, "entry_price": ..., "stop_price": ..., "entry_low": ...}}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.book import paths as book_paths
from core.mtf_fetch import load_kline
from core.resonance_strategy import evaluate, should_exit, should_take_profit

# 分批建仓比例（原文：首次 30% → 回踩不破 +30% → 突破前高 +40%）
STAGE_WEIGHTS = [0.30, 0.30, 0.40]
# 单标的最大权重 = 计划投入该标的的资金比例（占本盘 equity），即原文所称「满仓」。
# 小本金($100)下，若单标的上限太低(如20%)，首仓仅 $6，币安步长取整会让高价股
# 开不出仓。因此默认取 0.50：equity=$100 → 满仓 $50、首仓 $15，能覆盖全部标的。
# 注：这意味着 $100 同时最多约持 2 个标的 —— 符合本策略「强共振才入、重质不重量」
# 的风格。本金变大时（如 $500+）可由 CLI 调低以增加分散度。
TARGET_FULL_WEIGHT = 0.50
# 每笔建仓的最小名义额保护（低于此则不下，避免取整成 0 手的无效单）
MIN_STAGE_NOTIONAL = 5.0
# 每档评分对应是否允许建仓
ENTER_GRADES = {"normal", "strong"}


def _state_path(book: str) -> Path:
    p = book_paths(book)["state"]
    return p.parent / "resonance_state.json"


def load_state(book: str) -> dict:
    p = _state_path(book)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(book: str, state: dict) -> None:
    p = _state_path(book)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def decide_targets(book: str, equity: float, universe: list[str],
                   use_pullback: bool = True, commit: bool = True) -> tuple[dict[str, float], list[dict], dict]:
    """核心决策：返回 (目标权重表 symbol->weight, 信号明细 list, 状态表)。

    目标权重是「本盘希望持有的完整目标权重」，随后由 build_orders 与
    本盘持仓账比对，得出 delta 下单。分批建仓通过 stage 控制当前目标权重。

    commit=True 时立即写状态文件（真成交后调用）；
    commit=False 时只计算、返回 (targets, detail, pending_state) 三元组，
    供 tick 在真成交后再提交 —— 避免 dry-run 写脏状态。
    """
    from core.book import load_book_positions
    cur = load_book_positions(book)
    state = load_state(book)

    targets: dict[str, float] = {}
    detail: list[dict] = []

    for sym in universe:
        d4 = load_kline(sym, "4h")
        d30 = load_kline(sym, "30m")
        if d4 is None or d30 is None or len(d4) < 60 or len(d30) < 60:
            continue

        holding = sym in cur and abs(cur.get(sym, 0.0)) > 1e-9
        st = state.get(sym, {})

        if holding:
            if not st:
                # 持仓账有、状态没有（如历史遗留仓）：以当前价补一个状态，按首档管理
                st = {"stage": 1, "entry_price": float(d4["close"].iloc[-1]),
                      "stop_price": None, "entry_low": None,
                      "entry_ts": datetime.now(timezone.utc).isoformat()}
                state[sym] = st
            # ---- 已持仓：先看止损/止盈 ----
            ex, reason = should_exit(d4, st.get("entry_low"))
            if ex:
                detail.append({"symbol": sym, "action": "exit", "reason": reason,
                               "stage": st.get("stage", 1)})
                targets[sym] = 0.0          # 平仓
                state.pop(sym, None)
                continue
            tp, tpreason = should_take_profit(d4)
            if tp:
                # 分批止盈：砍一半（降一档）
                stage = st.get("stage", 1)
                new_stage = max(1, stage - 1)
                if new_stage != stage:
                    st["stage"] = new_stage
                    state[sym] = st
                detail.append({"symbol": sym, "action": "trim", "reason": tpreason,
                               "stage": st.get("stage", 1)})
                targets[sym] = _stage_weight(st.get("stage", 1))
                continue

            # ---- 加仓检查 ----
            sig = evaluate(sym, d4, d30, use_pullback_confirmation=use_pullback)
            stage = st.get("stage", 1)
            if sig.grade in ENTER_GRADES and sig.entry_trigger and stage < len(STAGE_WEIGHTS):
                # 回踩不破 / 突破前高 → 进下一档
                st["stage"] = stage + 1
                state[sym] = st
                detail.append({"symbol": sym, "action": "add", "reason": "回踩/再启确认",
                               "score": sig.score, "stage": st["stage"]})
            targets[sym] = _stage_weight(st.get("stage", 1))
            continue

        # ---- 未持仓：评估建仓 ----
        sig = evaluate(sym, d4, d30, use_pullback_confirmation=use_pullback)
        if sig.grade in ENTER_GRADES and sig.entry_trigger:
            st = {"stage": 1, "entry_price": sig.ref_price,
                  "stop_price": sig.stop_price,
                  "entry_low": sig.stop_price,
                  "entry_ts": datetime.now(timezone.utc).isoformat()}
            state[sym] = st
            targets[sym] = _stage_weight(1)
            detail.append({"symbol": sym, "action": "open", "grade": sig.grade,
                           "score": sig.score, "stop": sig.stop_price,
                           "stage": 1, "checks": sig.checks, "notes": sig.notes})
        else:
            detail.append({"symbol": sym, "action": "watch", "grade": sig.grade,
                           "score": sig.score, "entry": sig.entry_trigger})

    if commit:
        save_state(book, state)
    return targets, detail, state


def _stage_weight(stage: int) -> float:
    """按已建仓档数，返回当前目标权重（占 equity 比例）。"""
    if stage <= 0:
        return 0.0
    frac = sum(STAGE_WEIGHTS[:stage])
    return TARGET_FULL_WEIGHT * frac


def _filter_min_notional(targets_w: dict[str, float], equity: float,
                         prices: dict[str, float], min_notional: float = 5.0) -> tuple[dict[str, float], list[str]]:
    """剔除「即便满仓目标也开不出一手(min_notional)」的标的。

    低价/高价股在固定步长下，$6 名义额可能取整为 0。若某标的当前目标权重
    换算的名义额 < min_notional，则本轮不下它，避免噪音。返回(保留目标, 被剔除列表)。
    """
    import math
    kept: dict[str, float] = {}
    dropped: list[str] = []
    for sym, w in targets_w.items():
        if w <= 0:
            kept[sym] = w          # 平仓/零目标照常保留
            continue
        px = prices.get(sym)
        if not px or px <= 0:
            dropped.append(sym)
            continue
        notional = abs(w * equity)
        if notional < min_notional:
            dropped.append(sym)
            continue
        kept[sym] = w
    return kept, dropped


# ---------------------------------------------------------------------------
# 执行层：把目标权重表 → 订单 → 成交 → 账本 → 播报
# ---------------------------------------------------------------------------
def _book_avg_price(book: str, sym: str, cur: dict[str, float]) -> float | None:
    """从本盘成交台账推当前持仓的移动平均成本（只在交易所查不到该标的时兜底用）。"""
    try:
        from core.book import paths
        p = paths(book)["ledger"]
        if not p.exists():
            return None
        net = 0.0
        avg = 0.0
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("symbol") != sym or r.get("status") != "sent":
                continue
            q = float(r.get("qty") or 0)
            px = float(r.get("ref_price") or 0)
            if q <= 0 or px <= 0:
                continue
            signed = q if r.get("side") == "BUY" else -q
            if net == 0 or (net > 0) == (signed > 0):
                avg = (avg * abs(net) + px * q) / (abs(net) + q)
            else:                       # 反手：已实现掉旧仓，成本重置为新开仓价
                if abs(signed) >= abs(net):
                    avg = px
            net += signed
        return avg if abs(net) > 1e-12 else None
    except Exception:  # noqa: BLE001
        return None


def tick(book: str, equity: float, live: bool = False, testnet: bool = True,
         universe: list[str] | None = None, use_pullback: bool = True,
         notify_target: str | None = "qqbot", max_notional: float = 500.0,
         leverage: int = 1) -> dict:
    """跑一轮共振策略：评估 → 生成目标 → 下单 → 记账 → 播报。

    复用 build_orders / execute_orders / book 账本 / notify。
    返回本轮记录（与 trader_daemon 的记录结构尽量对齐，便于播报）。
    """
    from datetime import datetime, timezone

    from core.bridge_us_binance import (Target, build_orders, execute_orders,
                                         load_positions_from_exchange)
    from core.book import apply_book_fills, load_book_positions
    from core.us_mapping import build_mapping

    rec: dict = {"ts": datetime.now(timezone.utc).isoformat(), "book": book,
                 "strategy": "resonance_mtf", "equity": equity, "ok": False}

    # 标的池：全部可映射标的（两盘共享，不划分），但**必须限定为当前环境可交易**。
    # 币安 Demo 端点只有约 18 个美股永续有真实报价，其余 104 个只在主网。
    # 若不限定，策略会对开不出仓的标的发信号 → 无效单。
    if universe is None:
        from core.us_mapping import live_quote_symbols
        # fetch_live=True + testnet 参数：按当前环境拉取真实可交易的 tradfi 标的，
        # 避免本地快照（主网）里那些 Demo 端点没有的标的混进来。
        mapped, _ = build_mapping(fetch_live=True, testnet=testnet)
        mapped_syms = list(dict.fromkeys(m.binance_symbol for m in mapped))  # 去重保序
        tradable = live_quote_symbols(testnet=testnet)
        if tradable:
            universe = [s for s in mapped_syms if s in tradable]
            rec["universe_filter"] = f"live_quote({len(universe)}/{len(mapped_syms)})"
        else:
            universe = mapped_syms
            rec["universe_filter"] = "live_quote_unavailable"
    rec["n_universe"] = len(universe)

    # 1) 决策：目标权重 + 明细（先不提交状态，等真成交再提交）
    targets_w, detail, pending_state = decide_targets(
        book, equity, universe, use_pullback=use_pullback, commit=False)
    rec["signals"] = [d for d in detail if d.get("action") in ("open", "add", "exit", "trim")]
    rec["n_watch"] = sum(1 for d in detail if d.get("action") == "watch")
    rec["n_signals"] = len(rec["signals"])

    if not targets_w:
        rec.update(status="silent", note="本轮无共振信号")
        _heartbeat(rec, book)
        return rec

    # 2) 拉价格
    from core.binance_exec import BinanceExec
    client = BinanceExec("x", "x", testnet=testnet)
    prices: dict[str, float] = {}
    for sym in targets_w:
        try:
            tk = client.ticker(sym)
            lp = tk.get("lastPrice")
            if lp is not None:
                prices[sym] = float(lp)
        except Exception:  # noqa: BLE001
            pass
    rec["n_prices"] = len(prices)

    # 2b) 剔除「当前目标名义额 < 最小挂单额」的标的（避免 $6 目标取整成 0）
    targets_w, dropped = _filter_min_notional(targets_w, equity, prices, min_notional=5.0)
    if dropped:
        rec["dropped_min_notional"] = dropped
        # 被剔除的建仓标的不应留在状态里（否则下次会误判为持仓）
        for sym in dropped:
            pending_state.pop(sym, None)
        rec["signals"] = [d for d in rec["signals"] if d.get("symbol") not in set(dropped)]
        rec["n_signals"] = len(rec["signals"])

    if not targets_w:
        rec.update(status="silent", note="信号名义额不足最小挂单额")
        _heartbeat(rec, book)
        return rec

    # 3) 本盘自有持仓账 → build_orders（只认自己发过的单，不碰别的盘）
    cur = load_book_positions(book)
    rec["current_positions"] = cur
    rec["book_positions_source"] = "book_ledger"

    # 3b) 本盘浮盈 —— 只算【本盘自有持仓】那一份，不能直接用账户合计（多盘共用账户）。
    #     币安 /fapi/v2/positionRisk 里 TP/SL 单字段名是 unRealizedProfit（大写 R），
    #     不是 unrealizedProfit —— 映射错就恒为 0。
    unrealized = 0.0
    try:
        from core.binance_exec import BinanceExec as _BE
        _ac = _BE.from_env(testnet=testnet)          # 必须带真实 key（signed 接口）
        upnl = {}
        for p in _ac.positions():
            v = p.get("unRealizedProfit", p.get("unrealizedProfit"))
            upnl[p["symbol"]] = float(v or 0.0)
        for sym, q in cur.items():
            if sym in upnl:
                unrealized += upnl[sym]
            else:                                   # 交易所查不到该标的 → 用本盘成交均价兜底
                live_px = prices.get(sym)
                avg = _book_avg_price(book, sym, cur)
                if live_px and avg:
                    unrealized += (live_px - avg) * q
        rec["pnl_source"] = "book_slice"
    except Exception as e:  # noqa: BLE001
        rec["account_error"] = str(e)
    rec["unrealized_pnl"] = round(unrealized, 4)

    targets = [Target(ticker=sym, binance_symbol=sym, weight=w, factor_value=0.0)
               for sym, w in targets_w.items()]
    # 交易所真实精度（同 trader_daemon：避免碎仓在下单侧被 minQty 抬量）
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

    orders = build_orders(targets, equity, prices, cur, min_notional=5.0,
                          deadband_pct=0.0, deadband_minnotional_mult=1.0,
                          steps=_steps, min_qtys=_minq)
    # 平仓单优先（build_orders 已排序）
    rec["orders"] = [o.__dict__ for o in orders]
    rec["n_orders"] = len(orders)

    if not orders:
        rec.update(status="silent", note="目标与持仓一致，无需调仓")
        _heartbeat(rec, book)
        return rec

    # 4) 执行
    results = execute_orders(orders, testnet=testnet, leverage=leverage,
                             max_notional=max_notional, dry=not live)
    rec["results"] = results
    sent = [r for r in results if r.get("status") == "sent"]
    rec["executed"] = bool(sent)
    rec["status"] = "executed" if sent else ("dry_run" if not live else "no_fill")

    # 5) 真成交后：写回本盘账 + 提交状态（按 symbol 匹配，避免 zip 错位）
    if live and sent:
        order_by_sym = {o.binance_symbol: o for o in orders}
        fills = []
        for r in sent:
            sym = r.get("symbol")
            q = r.get("qty")
            o = order_by_sym.get(sym)
            if not sym or q is None or o is None:
                continue
            signed = float(q) if o.side == "BUY" else -float(q)
            fills.append({"symbol": sym, "signed_qty": signed})
        if fills:
            rec["book_positions_after"] = apply_book_fills(fills, book)
            save_state(book, pending_state)
            rec["state_committed"] = True

    _heartbeat(rec, book)
    return rec


def _heartbeat(rec: dict, book: str) -> None:
    """写心跳/决策日志（按 book 分目录）。带独占文件锁，防止并发写入交错。"""
    import json
    try:
        import fcntl
    except Exception:  # noqa: BLE001
        fcntl = None
    try:
        p = book_paths(book)
        p["dir"].mkdir(parents=True, exist_ok=True)
        hb = json.dumps({k: rec.get(k) for k in
                         ("ts", "book", "status", "n_universe", "n_signals",
                          "n_orders", "executed")}, ensure_ascii=False) + "\n"
        dec = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        for path, line in ((p["heartbeat"], hb), (p["decisions"], dec)):
            with path.open("a") as f:
                if fcntl is not None:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    except Exception:  # noqa: BLE001
                        pass
                f.write(line)
                f.flush()
    except Exception:  # noqa: BLE001
        pass


def run(book: str, equity: float, interval: int = 1800, live: bool = False,
        testnet: bool = True, use_pullback: bool = True,
        notify_target: str | None = "qqbot", max_notional: float = 500.0,
        leverage: int = 1, times: int = 0) -> None:
    """常驻循环：每 interval 秒跑一轮共振策略。

    interval 默认 1800s(30min) —— 与 30m K 线周期对齐（策略的入场层是 30m）。
    times>0 时只跑 times 轮（测试用）；否则无限循环。
    """
    import time
    if live and not testnet:
        raise RuntimeError("拒绝在 mainnet 实盘运行（只允许 Demo）")
    n = 0
    while True:
        n += 1
        try:
            rec = tick(book=book, equity=equity, live=live, testnet=testnet,
                       use_pullback=use_pullback, notify_target=notify_target,
                       max_notional=max_notional, leverage=leverage)
            print(f"[resonance] #{n} status={rec.get('status')} "
                  f"signals={rec.get('n_signals')} orders={rec.get('n_orders')}",
                  flush=True)
            # 真执行 → 推送播报（复用 core.notify，标题带盘名）
            if rec.get("executed") and notify_target:
                try:
                    from core.notify import notify
                    rec["notified"] = notify(rec, target=notify_target)
                except Exception as e:  # noqa: BLE001
                    print(f"[resonance] notify 失败: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[resonance] #{n} 异常: {e}", flush=True)
        if times and n >= times:
            break
        time.sleep(interval)


def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="多周期共振策略运行器")
    ap.add_argument("--book", default="resonance", help="账本名（默认 resonance）")
    ap.add_argument("--equity", type=float, default=100.0, help="本盘名义本金")
    ap.add_argument("--loop", action="store_true", help="常驻循环")
    ap.add_argument("--interval", type=int, default=1800, help="循环间隔秒（默认 1800=30min）")
    ap.add_argument("--live", action="store_true", help="真实下单（缺省 dry-run）")
    ap.add_argument("--mainnet", action="store_true",
                    help="用主网端点（默认 Demo；与实盘账户不符时勿用）")
    ap.add_argument("--aggressive", action="store_true",
                    help="激进入场（不等回踩确认）；默认稳健（等回踩）")
    ap.add_argument("--notify", dest="notify_target", default="qqbot",
                    help="播报目标（hermes send --to，如 qqbot；传 none 关闭）")
    ap.add_argument("--max-notional", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=1)
    ap.add_argument("--times", type=int, default=0, help="只跑 N 轮（测试用）")
    args = ap.parse_args()

    testnet = not args.mainnet
    nt = None if args.notify_target.lower() == "none" else args.notify_target
    if args.loop:
        run(book=args.book, equity=args.equity, interval=args.interval,
            live=args.live, testnet=testnet, use_pullback=not args.aggressive,
            notify_target=nt, max_notional=args.max_notional,
            leverage=args.leverage, times=args.times)
    else:
        rec = tick(book=args.book, equity=args.equity, live=args.live,
                   testnet=testnet, use_pullback=not args.aggressive,
                   notify_target=nt, max_notional=args.max_notional,
                   leverage=args.leverage)
        import json
        print(json.dumps({k: rec.get(k) for k in
                          ("ts", "book", "status", "universe_filter", "n_universe",
                           "n_signals", "n_orders", "executed")},
                         ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _main()

