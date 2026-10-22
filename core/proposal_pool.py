"""跨进程共享的 LLM 因子提案池 —— 一个 round 只调用一次 LLM。

为什么需要（2026-10-17）：
  原结构是 mine_loop.sh 每轮串行启动 5 个 mine 进程（美股 / A股×3 / 正交），
  每个进程各自调用一次 LLM 提案：
    - LLM 成本 = 5 倍（每轮 1150 个候选的目标量）
    - 各进程 `tried` 互不可见，可能提出同一批因子，去重要等到 promote 阶段才发现
    - 单次调用之间没有信息交流，前一个标的发现的好方向传不到下一个

  现在改为「一个 round 只调用一次 LLM，产出全局候选池，5 个进程从池中取用」：
    - LLM 成本降为 1 倍
    - 天然全局去重（池本身是去重的）
    - 提案时能看到「所有标的 + 全部历史已试因子」，不原地打转

设计要点：
  - 文件即接口：池写到 runs/loop_*/round_*/proposal_pool.json，进程间只读共享，
    不需要额外的守护进程或 IPC，失败时可单独重建。
  - 幂等：同一 round 已有池就复用，不重复烧 LLM（关机重启后不会重复付费）。
  - 降级：LLM 不可用时返回空池，调用方自动回退到「本进程自己提案」，绝不阻塞挖掘。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent


def pool_path(round_dir: str | os.PathLike, hold: int = 1) -> Path:
    """池文件路径。hold=1 用 proposal_pool.json（与历史完全兼容），
    其它持有期用 proposal_pool_h<N>.json —— 不同持有期的候选窗口分布不同，
    共用同一个池会让 hold=5 的进程拿到 hold=1 的短窗候选。"""
    base = Path(round_dir)
    try:
        h = int(hold)
    except (TypeError, ValueError):
        h = 1
    return base / ("proposal_pool.json" if h == 1 else f"proposal_pool_h{h}.json")


def load_pool_file(path: str | os.PathLike) -> list[dict] | None:
    """按完整路径读取候选池；不存在或损坏返回 None。

    用于下游（mine.py）直接消费上游（build_pool）给出的池文件：
    池文件名带 hold 后缀，用 parent + 默认 hold 再拼会错位。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        specs = data.get("specs") or []
        return specs if isinstance(specs, list) else None
    except Exception:
        return None


def load_pool(round_dir: str | os.PathLike, hold: int = 1) -> list[dict] | None:
    """读取候选池；不存在或损坏返回 None。"""
    p = pool_path(round_dir, hold)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        specs = data.get("specs") or []
        return specs if isinstance(specs, list) else None
    except Exception:
        return None


def build_pool(round_dir: str | os.PathLike, total: int = 300,
               concurrency: int = 4, force: bool = False,
               composite_ratio: float | None = None,
               adaptive: bool = False,
               verbose: bool = True, hold: int = 1) -> list[dict]:
    """为一个 round 生成全局候选池（幂等：已存在则直接复用）。

    total 是**整个 round 共享**的候选数，而不是每个标的各自一份。

    --- 规模：2026-10-18 从 600 降到 300 ---
    600 这个数字的历史成因是「A股×3 + 美股 + 正交」共 5 个标的要分摊。
    但 A股已停用（ENABLE_ASHARE=0），实际只剩 2 个进程（美股 + 正交），
    按 5 个标的规模生成池子等于一半候选没有消费方。故降到 300。

    注意：`core.mine --llm-total N` 在有 --proposal-pool 时**完全不起作用**
    （mine() 里 `if proposal_pool:` 分支会忽略 llm_total），
    真正决定 LLM 提案量的是这里的 total。

    复合因子配额（composite_ratio）:

      【结论修正】此前记录的"单算子空间已饱和（11792 次试验 → 423 个唯一组合，
      目标 20 个单算子实际产出 0）"是**误判**。真实原因是三层叠加：
        1. 历史 4000 条记录只覆盖 19 个不同 n 值，且极度集中在 5/10/20/60/120；
        2. LLM 自身也偏好这些整数档，新提案必然与历史撞键；
        3. 去重键包含 direction，与 FDR 阶段按 (op, params) 归并的口径不一致。
      修复（见 llm/proposer_batch.py）：去重口径统一为 (op, params) 忽略
      direction，并下发"参数避让"提示引导 LLM 使用未探索窗口。
      实测：目标 60 个单算子的产出从 2 个提升到 49 个，n 值落在 4/8/9/11/13/
      17/18/23/33/45/55/75 等空白区。**参数空间并未饱和。**

      composite_ratio 传 None 时默认 0.5（单算子 / 复合各一半）。
      adaptive=True 才启用"单算子产出过低就把配额全转复合"的降级分支，
      默认关闭（该分支依据的饱和结论已被证伪）。
    """
    p = pool_path(round_dir, hold)
    existing = None if force else load_pool(round_dir, hold=hold)
    if existing is not None:
        if verbose:
            print(f"  [提案池] 复用已有池：{len(existing)} 个候选", flush=True)
        return existing

    ratio = 0.5 if composite_ratio is None else float(composite_ratio)
    specs: list[dict] = []
    error = None
    tried = _load_history_specs(exclude_round=round_dir)
    base = {"total": total, "adaptive": adaptive, "n_history": len(tried)}

    def _run_composite(n: int) -> list[dict]:
        if n <= 0:
            return []
        try:
            from llm.proposer_composite import propose_composite_batch
            if verbose:
                print(f"  [提案池] 复合因子提案：目标 {n} 个", flush=True)
            return propose_composite_batch(
                total=n, concurrency=concurrency, per_call=5,
                tried=tried, history=None) or []
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  [提案池] 复合提案不可用：{exc}", flush=True)
            return []

    def _run_single(n: int) -> list[dict]:
        if n <= 0:
            return []
        try:
            from llm.proposer_batch import propose_batch
            if verbose:
                print(f"  [提案池] 单算子提案：目标 {n} 个", flush=True)
            return propose_batch(total=n, concurrency=concurrency,
                                 tried=tried, verbose=verbose, hold=hold) or []
        except Exception as exc:  # noqa: BLE001
            error_holder.append(f"single: {exc}")
            if verbose:
                print(f"  [提案池] 单算子提案不可用：{exc}", flush=True)
            return []

    error_holder: list[str] = []
    n_single = int(total * (1 - ratio))
    n_comp = total - n_single
    singles = _run_single(n_single)
    base["n_single_target"] = n_single
    base["n_single_got"] = len(singles)
    specs += singles

    # 自适应降级：2026-10-18 关闭（默认 adaptive=False）。
    #
    # 原逻辑：单算子产出 < 目标的 20% 时，判定「单算子空间已饱和」，
    # 把剩余配额全部转给复合因子。实测第一轮单算子只出 6 个，
    # 于是复合因子拿到 594 个配额、产出 212 个 —— 池子近一半是复合因子。
    #
    # 但「空间已饱和」这个前提已被证伪（见本函数 docstring 的【结论修正】）：
    # 真实原因是去重口径不一致 + LLM 偏好整数档，修复后单算子产出从 2 → 49。
    # 所以这个降级分支是建立在一个错误结论上的，默认不再启用。
    # 需要时可用 adaptive=True 恢复，便于实验对比。
    if adaptive and n_single > 0 and len(singles) < max(1, n_single * 0.2):
        n_comp = total - len(singles)
        base["adaptive_shifted"] = True
        if verbose:
            print(f"  [提案池] 单算子仅产出 {len(singles)} 个（判定空间饱和），"
                  f"剩余配额全部转给复合因子（{n_comp} 个）", flush=True)

    comps = _run_composite(n_comp)
    base["n_composite_target"] = n_comp
    base["n_composite_got"] = len(comps)
    specs += comps
    if error_holder:
        error = error_holder[0]

    # 池内去重（同一 spec 可能出现多次）
    seen, uniq = set(), []
    for s in specs:
        try:
            key = json.dumps({k: s.get(k) for k in ("op", "params", "direction", "expr")},
                             sort_keys=True, default=str)
        except Exception:
            continue
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    specs = uniq

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_total": total,
        "hold": hold,
        "composite_ratio": composite_ratio,
        "n_specs": len(specs),
        "stats": base,
        "error": error,
        "specs": specs,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if verbose:
        print(f"  [提案池] 完成：{len(specs)} 个唯一候选"
              f"（单算子 {base.get('n_single_got', 0)} / 复合 {base.get('n_composite_got', 0)}）",
              flush=True)
    return specs


def _load_history_specs(exclude_round=None, limit: int = 4000) -> list[dict]:
    """收集历史挖掘里出现过的因子，作为「已试」上下文喂给 LLM。"""
    out: list[dict] = []
    round_dir = Path(exclude_round).resolve() if exclude_round else None
    try:
        for p in sorted(PROJ.glob("runs/loop_*/round_*/mine*.json")) + \
                 sorted(PROJ.glob("runs/loop_*/round_*/ortho*.json")):
            if round_dir and p.resolve().parent == round_dir:
                continue  # 排除本轮自己
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            for r in data.get("all_results", [])[:400]:
                sp = r.get("spec") or {}
                if sp:
                    out.append(sp)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out[-limit:]
