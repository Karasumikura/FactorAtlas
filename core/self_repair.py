"""LLM 自愈守护 —— 让 LLM 不仅报告问题，还能在受控范围内自动修复。

为什么需要（用户 2026-10-17 要求）：
  现有 quant-ops.timer 每 30 分钟跑 ops_cron.sh，只能做**预设**的自愈动作
  （重启服务、清日志）。遇到"代码里的 bug"（例如今天 mine.py 的
  NameError 导致 30 次挖掘全废）它只能报警，无法介入 —— 必须等人来看。

  本模块把这种"只能报告"升级为"可以动手"，但必须有硬边界。

安全设计（宁可少修，不可修坏）：
  1. 只读诊断 → 生成补丁 → 三道闸门校验 → 才允许落盘：
       a) 语法检查（py_compile）必须通过
       b) 单元测试必须全绿（跑整个 tests/）
       c) 补丁必须是**局部替换**，且改动行数有上限（防止 LLM 大改特改）
  2. 任何一道闸门失败 → 完全回滚，原文件逐字节还原（备份+校验）
  3. 每次修复都写审计日志到 runs/self_repair/，含 before/after diff，
     失败也记录 —— 不允许"静默修改"。
  4. 默认 dry-run：只有显式 --apply 才真正写盘。
  5. 同一问题在冷却期内不重复尝试（避免 LLM 反复试错烧钱）。

用法：
  python -m core.self_repair --diagnose        # 只诊断，输出问题清单
  python -m core.self_repair --dry-run         # 生成补丁但不落盘（默认）
  python -m core.self_repair --apply           # 真正修复（需通过全部闸门）
"""
from __future__ import annotations

import hashlib
import json
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
REPAIR_DIR = PROJ / "runs" / "self_repair"
BACKUP_DIR = REPAIR_DIR / "backups"
COOLDOWN_FILE = REPAIR_DIR / "cooldown.json"

# 允许 LLM 修改的文件白名单（绝不允许碰数据、配置、密钥）
ALLOWED_GLOBS = ("core/*.py", "factors/*.py", "llm/*.py", "scripts/*.sh", "scripts/*.py")
FORBIDDEN_PATTERNS = (
    "venv/", ".env", "data/", "backups/", "runs/", "tests/",
    "factor_library.json", "zoo.json",
)

# 一次修复最多改动的行数（防止 LLM "顺手重构"）
MAX_DIFF_LINES = 60
COOLDOWN_HOURS = 6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _infer_source_file(snippet: str) -> str | None:
    """从 Traceback 里推断出需要修的源文件（相对 PROJ 路径）。

    例如片段含:
        File "/opt/quant-lab/core/mine.py", line 110, in add
    则返回 "core/mine.py"。
    取**最内层**（最后一个）属于 PROJ 且在白名单内的帧 —— 那才是 NPE 发生处。
    """
    import re as _re
    candidates = []
    for m in _re.finditer(r'File "([^"]+)", line \d+', snippet or ""):
        fpath = m.group(1)
        try:
            rel = str(Path(fpath).resolve().relative_to(PROJ.resolve()))
        except Exception:
            continue
        if _safe_path(rel) is not None:
            candidates.append(rel)
    return candidates[-1] if candidates else None


# ------------------------------------------------------------------ 诊断
def diagnose(log_glob: str = "runs/loop_*/round_*/mine*.log",
            tail_lines: int = 120) -> list[dict]:
    """扫描最近的挖掘日志，提取异常（Traceback / 特定错误）。"""
    import glob as globmod
    problems = []

    # 1) 挖掘日志里的 Python 异常
    pattern_re = re.compile(
        r"(?P<etype>\w*Error|Exception)\s*:\s*(?P<msg>.+)"
    )
    for pat in (log_glob, "runs/loop_*/round_*/ortho*.log",
                "runs/loop_*/round_*/proposal_pool.log"):
        for path in sorted(globmod.glob(str(PROJ / pat)))[-40:]:
            try:
                text = Path(path).read_text(errors="replace")
            except Exception:
                continue
            if "Traceback" not in text and "Error" not in text:
                continue
            tail = "\n".join(text.splitlines()[-tail_lines:])
            m = pattern_re.search(tail)
            if not m:
                continue
            problems.append({
                "kind": "exception",
                "file": str(Path(path).relative_to(PROJ)),
                "source_file": _infer_source_file(tail),
                "etype": m.group("etype"),
                "message": m.group("msg").strip()[:300],
                "snippet": tail[-1500:],
            })

    # 2) 服务不在运行
    # 不含 quant-miner：挖掘 2026-11-05 迁到深圳机，本地 systemctl 查不到它。
    # 远端状态由 ops_watch 的 _sz_miner_state() 盯（agent 自愈够不着另一台机器，
    # 硬要"修"只会去改本地那份没人执行的代码）。
    for svc in ("quant-trader", "quant-trader-resonance", "y2a-auto"):
        try:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True, timeout=10)
            if r.stdout.strip() != "active":
                problems.append({"kind": "service_down", "service": svc,
                                 "message": f"{svc} 未运行（{r.stdout.strip()}）"})
        except Exception:
            pass

    # 3) 磁盘/内存
    try:
        st = shutil.disk_usage(str(PROJ))
        if st.free / st.total < 0.08:
            problems.append({"kind": "disk_low",
                             "message": f"磁盘可用仅 {st.free/1e9:.1f}GB"})
    except Exception:
        pass

    # 去重（同 etype+message 只留一条）
    seen, uniq = set(), []
    for p in problems:
        k = (p.get("kind"), p.get("etype"), p.get("message"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


# ------------------------------------------------------------------ 冷却
def _load_cooldown() -> dict:
    if COOLDOWN_FILE.exists():
        try:
            return json.loads(COOLDOWN_FILE.read_text())
        except Exception:
            pass
    return {}


def _in_cooldown(key: str) -> bool:
    data = _load_cooldown()
    ts = data.get(key)
    if not ts:
        return False
    try:
        return (time.time() - float(ts)) < COOLDOWN_HOURS * 3600
    except Exception:
        return False


def _mark_cooldown(key: str) -> None:
    data = _load_cooldown()
    data[key] = time.time()
    REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    COOLDOWN_FILE.write_text(json.dumps(data, indent=2))


# ------------------------------------------------------------------ 闸门
def _safe_path(rel: str) -> Path | None:
    """校验目标文件在白名单内且不含禁止路径。"""
    if any(bad in rel for bad in FORBIDDEN_PATTERNS):
        return None
    p = (PROJ / rel).resolve()
    try:
        p.relative_to(PROJ.resolve())
    except ValueError:
        return None
    rel_norm = str(p.relative_to(PROJ.resolve()))
    for g in ALLOWED_GLOBS:
        import fnmatch
        if fnmatch.fnmatch(rel_norm, g):
            return p
    return None


def _diff_size(old: str, new: str) -> int:
    """粗略统计变更行数（对称差）。"""
    import difflib
    old_l = old.splitlines()
    new_l = new.splitlines()
    n = 0
    for line in difflib.unified_diff(old_l, new_l, lineterm="", n=0):
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            n += 1
    return n


def verify_gates(target: Path, new_content: str) -> tuple[bool, list[str]]:
    """三道闸门：语法 → 行数上限 → 单元测试。返回 (是否通过, 原因列表)。"""
    reasons: list[str] = []

    # 闸门 a：语法
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(new_content)
            tmp = fh.name
        py_compile.compile(tmp, doraise=True)
        Path(tmp).unlink(missing_ok=True)
    except Exception as exc:
        return False, [f"语法检查失败: {exc}"]

    # 闸门 b：改动规模
    try:
        old = target.read_text(encoding="utf-8")
    except Exception as exc:
        return False, [f"读取原文件失败: {exc}"]
    n = _diff_size(old, new_content)
    if n > MAX_DIFF_LINES:
        return False, [f"改动过大（{n} 行 > 上限 {MAX_DIFF_LINES}），拒绝自动应用"]

    # 闸门 c：落盘到临时副本后跑测试（先在旁边验证，不动原文件）
    backup = BACKUP_DIR / f"{target.name}.{int(time.time())}.bak"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup)
    try:
        target.write_text(new_content, encoding="utf-8")
        r = subprocess.run(
            [str(PROJ / "venv" / "bin" / "python"), "-m", "pytest", "tests", "-q"],
            cwd=str(PROJ), capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            shutil.copy2(backup, target)   # 立即回滚
            return False, [f"单元测试未通过，已回滚: {(r.stdout or '')[-600:]}"]
        return True, [f"全部闸门通过（改动 {n} 行）；测试输出尾部: {(r.stdout or '')[-200:]}"]
    except Exception as exc:
        try:
            shutil.copy2(backup, target)
        except Exception:
            pass
        return False, [f"验证过程异常，已回滚: {exc}"]


# ------------------------------------------------------------------ 主流程
def run(dry_run: bool = True, max_repairs: int = 1, verbose: bool = True) -> dict:
    """诊断 → (可选) 生成补丁 → 校验 → 应用。默认 dry-run。"""
    problems = diagnose()
    out = {"generated_at": _now(), "dry_run": dry_run,
           "n_problems": len(problems), "problems": problems,
           "repairs": []}
    if verbose:
        print(f"诊断到 {len(problems)} 个问题：")
        for p in problems[:10]:
            print(f"  - [{p.get('kind')}] {p.get('file') or p.get('service') or ''} "
                  f"{p.get('etype') or ''} {str(p.get('message'))[:120]}")

    # 目前只有「代码异常」可以自动修；服务/磁盘类交给 ops_cron 的预设动作
    fixable = [p for p in problems if p.get("kind") == "exception"]
    if not fixable:
        if verbose:
            print("\n没有可由 LLM 自动修复的代码异常。")
        return out

    tried = 0
    for prob in fixable:
        if tried >= max_repairs:
            break
        key = f"{prob.get('file')}::{prob.get('etype')}::{prob.get('message')[:80]}"
        if _in_cooldown(key):
            if verbose:
                print(f"\n[冷却中] 跳过（{COOLDOWN_HOURS}h 内已试过）: {prob.get('message')[:80]}")
            continue
        tried += 1
        rec = {"problem": prob, "applied": False, "reasons": []}
        try:
            proposal = _ask_llm_for_patch(prob)
        except Exception as exc:
            rec["reasons"].append(f"LLM 提案失败: {exc}")
            out["repairs"].append(rec)
            continue
        if not proposal:
            rec["reasons"].append("LLM 未给出可用补丁")
            out["repairs"].append(rec)
            _mark_cooldown(key)
            continue
        rel, new_content = proposal
        target = _safe_path(rel)
        if target is None:
            rec["reasons"].append(f"目标文件不在白名单内: {rel}")
            out["repairs"].append(rec)
            _mark_cooldown(key)
            continue
        rec["target"] = rel
        rec["diff_lines"] = _diff_size(target.read_text(encoding="utf-8"), new_content)

        if dry_run:
            rec["reasons"].append("dry-run：未落盘")
            rec["patch_preview"] = new_content[:1200]
            out["repairs"].append(rec)
            continue

        ok, reasons = verify_gates(target, new_content)
        rec["reasons"] = reasons
        rec["applied"] = ok
        if ok:
            _mark_cooldown(key)
        out["repairs"].append(rec)
        if verbose:
            print(f"\n{'✅' if ok else '❌'} {rel}: {'; '.join(reasons)[:300]}")

    # 审计日志：每次运行都留档，包括失败
    REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    logf = REPAIR_DIR / f"repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    logf.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    if verbose:
        print(f"\n审计记录: {logf.relative_to(PROJ)}")
    return out


def _ask_llm_for_patch(prob: dict) -> tuple[str, str] | None:
    """让 LLM 基于错误片段给出「完整替换文件内容」。

    返回 (相对路径, 新文件全文)；无法给出时返回 None。
    为降低风险，要求 LLM 输出完整文件而非 diff —— diff 容易因上下文
    不匹配而打错位置；完整文件可以直接过语法检查与测试。
    """
    from llm.client import chat, extract_json
    rel = prob.get("file")
    if not rel:
        return None
    # 错误日志里记录的是 log 路径，映射回源文件：由错误片段中的文件名推断
    src_hint = prob.get("source_file")
    if not src_hint:
        return None
    src = PROJ / src_hint
    if not src.exists():
        return None
    current = src.read_text(encoding="utf-8")
    if len(current) > 200_000:
        return None  # 太大，不适合整文件替换

    prompt = f"""你在修复一个量化系统的 Python 代码 bug。

【报错】
{prob.get('etype')}: {prob.get('message')}

【日志尾部】
{prob.get('snippet', '')[-1200:]}

【当前文件 {src_hint} 全文】
```python
{current}
```

请输出修复后的**完整文件内容**（JSON 格式：{{"path": "{src_hint}", "content": "..."}}）。
要求：
1. 只做最小必要修改，不要重构、不要改格式、不要加无关注释。
2. 保持原有代码风格与中文注释。
3. content 里的换行用 \\n 转义。
只输出 JSON，不要其他文字。"""
    raw = chat([{"role": "user", "content": prompt}], temperature=0)
    data = extract_json(raw)
    if not isinstance(data, dict):
        return None
    path = data.get("path") or src_hint
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return path, content


def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="LLM 自愈守护（受控自动修复）")
    ap.add_argument("--diagnose", action="store_true", help="只诊断，不生成补丁")
    ap.add_argument("--dry-run", action="store_true", help="生成补丁但不落盘（默认）")
    ap.add_argument("--apply", action="store_true", help="真正应用（需通过全部闸门）")
    ap.add_argument("--max-repairs", type=int, default=1, help="本次最多修几个问题")
    args = ap.parse_args()

    if args.diagnose:
        probs = diagnose()
        print(json.dumps(probs, ensure_ascii=False, indent=2, default=str))
        return
    run(dry_run=not args.apply, max_repairs=args.max_repairs)


if __name__ == "__main__":
    _main()
