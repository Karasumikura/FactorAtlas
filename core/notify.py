"""调仓通知：daemon 真执行调仓时，主动推送到 QQ。

用 `hermes send` 复用现有凭据 —— 无 LLM、无 agent 循环。
只在【真执行】（executed=True）时推送；dry/silent 一律静默，避免打扰。

失败不影响交易主流程（通知是尽力而为）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ALERT_LOG = Path(__file__).resolve().parent.parent / "data" / "binance" / "trader_alerts.jsonl"


def _fmt_orders(orders: list[dict]) -> str:
    lines = []
    for o in orders:
        sym = o.get("binance_symbol") or o.get("symbol", "?")
        act = o.get("action", "?")
        delta = o.get("delta_qty")
        notional = o.get("notional")
        if delta is None:
            qty = o.get("qty")
            side = o.get("side", "")
            sign = "+" if side == "BUY" else "-"
            delta = f"{sign}{qty}"
        lines.append(f"  {sym:12s} {act:9s} {delta}  (~{notional:.0f}U)" if notional
                     else f"  {sym:12s} {act:9s} {delta}")
    return "\n".join(lines) if lines else "  (无)"


def _fmt_positions(pos: dict[str, float]) -> str:
    if not pos:
        return "  (空仓)"
    return "\n".join(f"  {k:12s} {v:+.2f}" for k, v in sorted(pos.items()))


# 盘名标签：账本 → 中文显示名
_BOOK_LABEL = {
    "lowfreq": "低频因子盘",
    "resonance": "共振因子盘",
}


def _book_label(rec: dict) -> str:
    """从 rec 取账本名并转成中文标签，未知账本原样显示。"""
    bk = (rec.get("book") or "lowfreq").strip().lower()
    return _BOOK_LABEL.get(bk, bk or "低频因子盘")


def _fmt_pnl(rec: dict) -> str:
    """浮盈显示：缺失/报错时明确说出来，小金额保留 4 位，别把 0.003 显示成 +0.00。"""
    v = rec.get("unrealized_pnl")
    if v is None:
        why = rec.get("account_error")
        return f"未知（账户读取失败: {why[:60]}）" if why else "未知（未计算）"
    v = float(v)
    return f"{v:+.4f} USDT" if abs(v) < 1 else f"{v:+.2f} USDT"


def build_message(rec: dict) -> str:
    """把一次调仓记录格式化成简短通知。"""
    trig = rec.get("trigger_reasons") or []
    llm = rec.get("llm_decision") or "无"
    orders = rec.get("orders") or []
    results = rec.get("results") or []
    sent = [r for r in results if r.get("status") == "sent"]
    label = _book_label(rec)

    # 成交明细（含真实成交价）
    fills = []
    for r in sent:
        sym = r.get("symbol", "?")
        side = r.get("side", "")
        qty = r.get("qty")
        px = r.get("ref_price")
        fills.append(f"  {sym:12s} {side:4s} {qty} @ ~{px:.2f}" if px
                     else f"  {sym:12s} {side:4s} {qty}")

    # 对账漂移告警：走单独的简短格式，别显示"执行 0 笔"
    if rec.get("status") == "reconcile_drift":
        drift = rec.get("reconcile_drift") or []
        drows = [f"  {str(d.get('symbol','?')):12s} 交所{d.get('exchange_qty',0):+.4g} "
                 f"本地{d.get('local_qty',0):+.4g}" for d in drift]
        return "\n".join([
            f"⚠️ 量化 · {label} · 持仓漂移",
            f"时间: {rec.get('ts', '')[:19]}",
            f"{rec.get('note', '')}",
            "",
            *(drows or ["  (无明细)"]),
        ])

    lines = [
        f"🔔 量化 · {label}",
        f"时间: {rec.get('ts', '')[:19]}",
        f"触发: {', '.join(trig) or '无'}",
        f"LLM: {llm}",
        f"信号日: {rec.get('signal_date', '?')}",
        "",
        f"执行 {len(sent)} 笔:",
        *(fills or ["  (无成交)"]),
        "",
        f"当前持仓:",
        _fmt_positions(rec.get("current_positions", {})),
        "",
        f"钱包浮盈: {_fmt_pnl(rec)}",
    ]
    return "\n".join(lines)


def notify(rec: dict, target: str = "qqbot", quiet: bool = False) -> bool:
    """推送调仓通知。返回是否成功。失败不抛异常（尽力而为）。"""
    # 只推真执行
    if not rec.get("executed"):
        return False
    try:
        msg = build_message(rec)
        # 先落盘留痕（无论推送成败）
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ALERT_LOG.open("a") as f:
            f.write(json.dumps({"ts": rec.get("ts"), "msg": msg,
                                "target": target}, ensure_ascii=False) + "\n")
        # hermes 可能不在 systemd 的 PATH 里 → 优先用绝对路径
        import shutil
        exe = shutil.which("hermes")
        if not exe:
            for cand in ("/root/.local/share/uv/tools/hermes-agent/bin/hermes",
                         "/root/.local/bin/hermes", "/usr/local/bin/hermes"):
                if Path(cand).exists():
                    exe = cand
                    break
        if not exe:
            raise FileNotFoundError("找不到 hermes 可执行文件")
        r = subprocess.run(
            [exe, "send", "--to", target, msg],
            capture_output=True, text=True, timeout=60)
        ok = r.returncode == 0
        if not quiet:
            print(f"[notify] {'OK' if ok else 'FAIL'} -> {target}"
                  + ("" if ok else f" | {r.stderr[:200]}"), flush=True)
        return ok
    except Exception as e:  # noqa: BLE001
        if not quiet:
            print(f"[notify] 异常: {e}", flush=True)
        return False
