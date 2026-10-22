#!/usr/bin/env python3
"""量化运营巡检 —— 每次运行输出一份结构化健康报告。

设计目标：让「持续挖掘 + 监控 + 迭代」可无人值守地留痕。
每项检查独立 try，单项失败不影响其它项；输出 JSON 便于外部消费 + 人类可读摘要。

检查项：
  1. 服务健康：quant-trader / quant-miner / y2a-auto 是否 active
  2. 资源：内存可用、磁盘可用、swap
  3. 量化交易：最近 tick 时间、是否卡死、最近是否有异常下单单
  4. 挖掘：当前轮进度、累计试验数 N、正式因子库大小
  5. 风控：最近是否触发熔断/降险/漂移
  6. 告警汇总：任何异常都进 alerts 列表

用法：
  python -m scripts.ops_watch            # 人类可读
  python -m scripts.ops_watch --json     # 仅 JSON
  退出码：0=全健康, 1=有告警
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent

SERVICES = ["quant-trader", "quant-trader-resonance", "quant-miner", "y2a-auto"]
DISK_MIN_GB = 2.0
MEM_MIN_MB = 150
# signal_date 滞后的告警阈值（自然日）。周末面板本来就不推进，留 2 天余量，
# 再放宽到 4 天：>=4 天说明至少有 1 个交易日没被刷进来。
SIGNAL_MAX_LAG_DAYS = 4


def _svc_active(name: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def _svc_detail(name: str) -> dict:
    d = {"active": _svc_active(name)}
    try:
        r = subprocess.run(["systemctl", "show", name, "-p", "NRestarts",
                            "-p", "ActiveEnterTimestamp", "-p", "MemoryCurrent"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    except Exception:
        pass
    return d


def _mem() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable"):
                out["avail_mb"] = int(line.split()[1]) // 1024
            elif line.startswith("SwapTotal"):
                out["swap_total_mb"] = int(line.split()[1]) // 1024
    except Exception:
        pass
    return out


def _disk() -> dict:
    try:
        st = os.statvfs("/")
        free_gb = st.f_bavail * st.f_frsize / 1e9
        total_gb = st.f_blocks * st.f_frsize / 1e9
        return {"free_gb": round(free_gb, 2), "total_gb": round(total_gb, 1),
                "used_pct": round((1 - free_gb / total_gb) * 100, 1)}
    except Exception:
        return {}


CPU_SAMPLE_FILE = PROJ / "data" / "cpu_samples.jsonl"


def _cpu_ticks() -> tuple[int, int] | None:
    """读 /proc/stat 的整机累计 (total, idle) tick 数。

    用累计值而非瞬时快照：瞬时值噪声大（一次采样可能撞上突发），
    而「上次巡检到现在」的差值反映的是这段时间的真实平均占用。
    """
    try:
        line = next(l for l in Path("/proc/stat").read_text().splitlines()
                    if l.startswith("cpu "))
        f = [int(x) for x in line.split()[1:]]
        # user nice system idle iowait irq softirq steal guest guest_nice
        total = sum(f[:8])
        idle = f[3] + f[4]  # idle + iowait
        return total, idle
    except Exception:
        return None


def _cpu(record: bool = True) -> dict:
    """CPU 占用：与上次采样对比得出区间平均，并计算近 12 小时均值。

    需求（2026-10-18）：12 小时平均占用率不得高于 90%。为了能**核查**
    而不是靠感觉，每次巡检都往 cpu_samples.jsonl 追加一行，然后用滑动
    12 小时窗口算加权平均（按两次采样之间的时长加权，避免巡检间隔不齐
    时把短区间和长区间等权，那样会失真）。
    """
    now = time.time()
    cur = _cpu_ticks()
    out: dict = {"nproc": os.cpu_count()}
    if cur is None:
        return out

    prev = None
    if CPU_SAMPLE_FILE.exists():
        try:
            last = CPU_SAMPLE_FILE.read_text().strip().splitlines()[-1]
            prev = json.loads(last)
        except Exception:
            prev = None

    total, idle = cur
    if prev and prev.get("total") and total > prev["total"]:
        dt = total - prev["total"]
        di = idle - prev["idle"]
        out["interval_pct"] = round((dt - di) / dt * 100, 1)
        out["interval_sec"] = round(now - prev.get("ts", now), 0)
    else:
        out["interval_pct"] = None

    if record:
        try:
            CPU_SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CPU_SAMPLE_FILE.open("a") as fh:
                fh.write(json.dumps({"ts": now, "total": total, "idle": idle}) + "\n")
        except Exception:
            pass

    # 12 小时滑动窗口加权平均
    try:
        cutoff = now - 12 * 3600
        rows = []
        for ln in CPU_SAMPLE_FILE.read_text().splitlines():
            try:
                r = json.loads(ln)
                if r.get("ts", 0) >= cutoff:
                    rows.append(r)
            except Exception:
                continue
        busy = tot = 0
        for a, b in zip(rows, rows[1:]):
            dt = b["total"] - a["total"]
            di = b["idle"] - a["idle"]
            if dt <= 0:
                continue
            busy += dt - di
            tot += dt
        if tot > 0:
            out["avg12h_pct"] = round(busy / tot * 100, 1)
            span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600 if len(rows) > 1 else 0
            out["avg12h_cover_h"] = round(span_h, 1)
            out["avg12h_samples"] = len(rows)
    except Exception:
        pass
    return out


def _last_trader_tick() -> dict:
    """从 heartbeat 读最近一次 tick。"""
    hb = PROJ / "data" / "binance" / "trader_heartbeat.jsonl"
    if not hb.exists():
        return {"found": False}
    try:
        # 读最后一行
        with hb.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            lines = [l for l in f.read().decode(errors="ignore").splitlines() if l.strip()]
        rec = json.loads(lines[-1])
        ts = rec.get("ts", "")
        age = None
        try:
            t = datetime.fromisoformat(ts)
            age = (datetime.now(timezone.utc) - t).total_seconds()
        except Exception:
            pass
        return {"found": True, "ts": ts, "age_sec": round(age) if age else None,
                "status": rec.get("status"), "signal_date": rec.get("signal_date")}
    except Exception as e:
        return {"found": False, "error": str(e)}


def _signal_staleness(signal_date: str | None) -> dict:
    """判断交易端用的因子信号日是否已明显滞后于数据面板末日。

    美股面板只在工作日推进，所以阈值按自然日放宽（周末 2 天属正常）。
    超过 SIGNAL_MAX_LAG_DAYS 天即判定为「陈旧」——此时交易端仍在正常
    tick，但每天都拿同一份旧信号算目标，属于静默故障。
    """
    out: dict = {"stale": False}
    if not signal_date:
        return out
    try:
        d = datetime.fromisoformat(signal_date).date()
    except Exception:
        return out
    today = datetime.now(timezone.utc).date()
    lag = (today - d).days
    out.update(lag_days=lag)
    try:
        meta = json.loads((PROJ / "data" / "hist" / "meta.json").read_text())
        out["panel_end"] = meta.get("end")
        end = meta.get("end")
        if end:
            pe = datetime.fromisoformat(end).date()
            out["panel_lag_days"] = (today - pe).days
    except Exception:
        pass
    if lag >= SIGNAL_MAX_LAG_DAYS:
        out["stale"] = True
    return out


def _miner_state() -> dict:
    """当前挖掘轮次 + 是否在跑。"""
    d: dict = {"running": False}
    try:
        r = subprocess.run(["pgrep", "-f", "core.mine"], capture_output=True,
                           text=True, timeout=10)
        d["running"] = bool(r.stdout.strip())
    except Exception:
        pass
    # 最近一轮目录
    dirs = sorted(glob.glob(str(PROJ / "runs" / "loop_*")), reverse=True)
    if dirs:
        latest = Path(dirs[0])
        d["latest_loop"] = latest.name
        rounds = sorted(latest.glob("round_*"))
        if rounds:
            rdir = rounds[-1]
            d["latest_round"] = rdir.name
            logs = list(rdir.glob("*.log"))
            d["round_files"] = [x.name for x in logs]
            # 抓进度行
            for lg in logs:
                txt = lg.read_text(errors="ignore")
                prog = [l for l in txt.splitlines() if "进度" in l or "幸存" in l]
                if prog:
                    d.setdefault("progress", {})[lg.name] = prog[-1].strip()
    return d


def _trial_and_library() -> dict:
    out = {}
    try:
        t = json.loads((PROJ / "data" / "factor_trials.json").read_text())
        out["cumulative_trials"] = t.get("total_trials", 0)
        out["n_sessions"] = len(t.get("sessions", []))
    except Exception:
        out["cumulative_trials"] = 0
    try:
        lib = json.loads((PROJ / "data" / "factor_library.json").read_text())
        out["library_factors"] = len(lib.get("factors", {}))
    except Exception:
        out["library_factors"] = 0
    return out


def _risk_recent() -> dict:
    """扫描最近决策日志，统计风控触发。"""
    dl = PROJ / "data" / "binance" / "trader_decisions.jsonl"
    out = {"breakers": {}, "derisk": 0, "drift": 0}
    if not dl.exists():
        return out
    try:
        lines = dl.read_text(errors="ignore").splitlines()[-50:]
    except Exception:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        g = r.get("guard") or {}
        for b in (g.get("blocks") or []):
            key = b.split("(")[0]
            out["breakers"][key] = out["breakers"].get(key, 0) + 1
        if r.get("active_derisk"):
            out["derisk"] += 1
        if r.get("reconcile_drift"):
            out["drift"] += 1
    return out


def snapshot() -> dict:
    alerts = []
    mem = _mem()
    disk = _disk()
    svcs = {s: _svc_detail(s) for s in SERVICES}
    tick = _last_trader_tick()
    miner = _miner_state()
    tl = _trial_and_library()
    risk = _risk_recent()

    for s, d in svcs.items():
        if not d.get("active"):
            alerts.append(f"服务未运行: {s}")
    if mem.get("avail_mb", 9999) < MEM_MIN_MB:
        alerts.append(f"内存紧张: 可用 {mem.get('avail_mb')}MB < {MEM_MIN_MB}MB")
    if disk.get("free_gb", 999) < DISK_MIN_GB:
        alerts.append(f"磁盘紧张: 可用 {disk.get('free_gb')}GB < {DISK_MIN_GB}GB")
    if tick.get("found") and tick.get("age_sec") and tick["age_sec"] > 1800:
        alerts.append(f"量化 tick 疑似卡死: 最近 tick {tick['age_sec']}s 前")
    # 信号陈旧检测：面板停在旧日期时服务照样全绿，但交易端一直用同一份
    # 旧信号算目标（2026-10-21 实测：面板停 10-13，连续 8 天 signal_date 不变）。
    stale = _signal_staleness(tick.get("signal_date"))
    if stale.get("stale"):
        alerts.append(f"因子信号陈旧: signal_date={tick.get('signal_date')}，"
                      f"已滞后 {stale['lag_days']} 个自然日（面板末日 "
                      f"{stale.get('panel_end')}）→ 跑 scripts/refresh_hist.py")
    if not miner.get("running") and not miner.get("latest_loop"):
        alerts.append("挖掘进程从未运行")
    if risk.get("drift"):
        alerts.append(f"近 {risk['drift']} 次 tick 有持仓漂移")
    if risk["breakers"].get("max_drawdown_breaker"):
        alerts.append(f"近 50 次决策触发回撤熔断 {risk['breakers']['max_drawdown_breaker']} 次")
    if risk["breakers"].get("daily_loss_breaker"):
        alerts.append(f"近 50 次决策触发日内亏损熔断 {risk['breakers']['daily_loss_breaker']} 次")

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "healthy": not alerts,
        "alerts": alerts,
        "services": {s: d.get("active") for s, d in svcs.items()},
        "service_details": svcs,
        "mem": mem, "disk": disk,
        "trader_last_tick": tick,
        "miner": miner,
        "trials": tl,
        "risk_recent": risk,
    }


def _render(rep: dict) -> str:
    L = []
    L.append(f"== 量化巡检 {rep['ts']} ==")
    L.append(f"健康: {'✅ 全绿' if rep['healthy'] else '⚠️ 有告警'}")
    L.append(f"服务: " + ", ".join(f"{k}={'up' if v else 'DOWN'}"
                                  for k, v in rep["services"].items()))
    m, d = rep["mem"], rep["disk"]
    L.append(f"资源: 内存可用 {m.get('avail_mb','?')}MB | "
             f"磁盘 {d.get('free_gb','?')}GB free ({d.get('used_pct','?')}% used)")
    t = rep["trader_last_tick"]
    if t.get("found"):
        L.append(f"量化: 最近 tick {t['age_sec']}s 前, status={t.get('status')}")
    mn = rep["miner"]
    L.append(f"挖掘: running={mn.get('running')} loop={mn.get('latest_loop')} "
             f"round={mn.get('latest_round')}")
    for k, v in (mn.get("progress") or {}).items():
        L.append(f"       {k}: {v}")
    tl = rep["trials"]
    L.append(f"因子库: 累计试验 N={tl.get('cumulative_trials')} "
             f"会话={tl.get('n_sessions')} 正式因子={tl.get('library_factors')}")
    r = rep["risk_recent"]
    if r.get("breakers") or r.get("derisk") or r.get("drift"):
        L.append(f"风控(近50决策): breakers={r.get('breakers')} "
                 f"降险={r.get('derisk')} 漂移={r.get('drift')}")
    if rep["alerts"]:
        L.append("告警:")
        for a in rep["alerts"]:
            L.append(f"  ⚠️ {a}")
    return "\n".join(L)


def main() -> None:
    as_json = "--json" in sys.argv
    rep = snapshot()
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    else:
        print(_render(rep))
    sys.exit(0 if rep["healthy"] else 1)


if __name__ == "__main__":
    main()
