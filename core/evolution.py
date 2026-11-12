#!/usr/bin/env python3
"""程序化进化搜索：不碰 LLM 的「更复杂」搜索结构。

为什么需要：
  固定网格 (generate_grid, 334 个) + 一次性 LLM 提案池，本质是「撒一次网」。
  没有假设→实验→反馈→变异的闭环，30 轮 0 晋升就是结果。
  本模块补上纯程序化的那一层：表达式树种群、按 IS 真实调仓 Sharpe 选择、
  参数/算子/方向变异、子树交叉、组合包装。

纪律（照抄 factor-research-pipeline 的硬规则）：
  - 适应度**只用 IS 段**（train_frac 切出来的训练段），OOS 一次都不看；
    否则等于用测试集做选择，过拟合会炸。
  - hold>1 一律用 `ls_nav_true_rebalance`（真实调仓），不用 evaluate_with_split
    那个 ~√hold 膨胀口径。
  - 产出的候选必须并入 mine.py 的**同一个 FDR 池**，由 promote 统一记 N；
    这里不调 record_session，也不自己宣称谁幸存。
  - 参数边界跟 mine._valid 一致：n∈[2,250]、skip∈[0,60]，整数。

用法（一般由 core.mine --evolve 调用）：
  evo = evolve(panel, hold=5, generations=3, pop_size=24)
"""
from __future__ import annotations

import copy
import gc
import json
import random
from typing import Any

import numpy as np

from factors.engine import OPS

COMBINE_OPS = ("add", "sub", "mul", "div", "max", "min")


def _key(spec: dict) -> str:
    if spec.get("kind") == "combo" or "expr" in spec:
        payload = {"expr": spec.get("expr"), "direction": spec.get("direction", 1)}
    else:
        payload = {"op": spec.get("op"), "params": spec.get("params", {}),
                   "direction": spec.get("direction", 1)}
    return json.dumps(payload, sort_keys=True, default=str)


def _leaf(spec: dict) -> dict:
    """把任意 spec 折成表达式树的叶子/子树节点。"""
    if spec.get("kind") == "combo" or "expr" in spec:
        return copy.deepcopy(spec["expr"])
    return {"op": spec["op"], "params": dict(spec.get("params") or {})}


def _leaves(node: dict, out: list[dict]) -> None:
    """收集表达式树里所有 OPS 叶子节点（原地追加）。"""
    if not isinstance(node, dict):
        return
    op = node.get("op")
    if op in OPS:
        out.append(node)
        return
    for side in ("left", "right"):
        if isinstance(node.get(side), dict):
            _leaves(node[side], out)


def _clip_param(k: str, v: Any) -> int:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return 20
    if k == "n":
        return max(2, min(250, n))
    if k == "skip":
        return max(0, min(60, n))
    return n


def _mutate(spec: dict, rng: random.Random, op_groups: dict,
            avoid_ops: set[str] | None = None) -> dict:
    s = copy.deepcopy(spec)
    mode = rng.choice(("param", "param", "op", "flip", "wrap"))

    if mode == "flip":
        s["direction"] = -int(s.get("direction", 1))
        s["name"] = (s.get("name", "evo") + "_flip")[:60]
        return s

    if mode == "wrap":
        # 单算子 → 两层组合（引入交互，这是网格里没有的结构）
        left = _leaf(s)
        right = {"op": rng.choice(list(OPS)), "params": {"n": rng.randint(5, 120)}}
        s = {"kind": "combo",
             "expr": {"op": rng.choice(COMBINE_OPS), "left": left, "right": right},
             "direction": s.get("direction", 1),
             "name": "evo_wrap_%d" % rng.randint(1000, 9999)}
        return s

    leaves: list[dict] = []
    if s.get("kind") == "combo" or "expr" in s:
        _leaves(s["expr"], leaves)
    else:
        leaves = [s]
    if not leaves:
        return s
    node = rng.choice(leaves)

    if mode == "op":
        sig = tuple(sorted((node.get("params") or {}).keys()))
        pool = [o for o in (op_groups.get(sig) or [node.get("op")])
                if not (avoid_ops and o in avoid_ops)]
        if pool:
            node["op"] = rng.choice(pool)
        s["name"] = "evo_op_%d" % rng.randint(1000, 9999)
    else:
        params = node.get("params") or {}
        if not params:
            node["params"] = {"n": rng.randint(5, 120)}
        else:
            k = rng.choice(list(params))
            factor = rng.choice((0.4, 0.6, 0.75, 1.3, 1.6, 2.0, 2.5))
            params[k] = _clip_param(k, params[k] * factor)
        s["name"] = "evo_par_%d" % rng.randint(1000, 9999)
    return s


def _combine_two(a: dict, b: dict, rng: random.Random) -> dict:
    return {
        "kind": "combo",
        "expr": {"op": rng.choice(COMBINE_OPS),
                 "left": _leaf(a), "right": _leaf(b)},
        "direction": rng.choice((1, -1)),
        "name": "evo_cross_%d" % rng.randint(1000, 9999),
        "rationale": "程序化组合：两个精英的交互项",
    }


def _crossover(a: dict, b: dict, rng: random.Random) -> dict:
    """交换两棵表达式树的一棵子树。"""
    ea, eb = _leaf(a), _leaf(b)
    la: list[dict] = []
    lb: list[dict] = []
    _leaves(ea, la)
    _leaves(eb, lb)
    if not la or not lb:
        return _combine_two(a, b, rng)
    x, y = rng.choice(la), rng.choice(lb)
    x["op"], y["op"] = y["op"], x["op"]
    x["params"], y["params"] = y.get("params", {}), x.get("params", {})
    return {"kind": "combo", "expr": ea, "direction": a.get("direction", 1),
            "name": "evo_swap_%d" % rng.randint(1000, 9999),
            "rationale": "程序化子树交叉"}


def _initial_population(pop_size: int, rng: random.Random,
                        seed_specs: list[dict] | None = None) -> list[dict]:
    from factors.sweep import generate_grid
    from factors.composite import CLASSIC_COMBOS

    pool: list[dict] = []
    # 复合/精英放前面：早期 out[:pop_size] 截断曾把后面追加的 combo
    # 全砍掉，导致初始种群一棵表达式树都没有（单测抓到）。
    for c in CLASSIC_COMBOS[:6]:
        pool.append({"kind": "combo", "name": c["name"] + "_evo",
                     "expr": {"op": c["op"], "left": c["left"], "right": c["right"]},
                     "direction": c.get("direction", 1),
                     "rationale": c.get("rationale", "")})
    for s in (seed_specs or [])[:8]:
        pool.append(copy.deepcopy(s))
    grid = generate_grid()
    need = max(1, pop_size - len(pool))
    for s in rng.sample(grid, min(need, len(grid))):
        pool.append(copy.deepcopy(s))
    # 去重（保序，combo 不会因为随机 shuffle 被挤出种群）
    out, seen = [], set()
    for s in pool:
        k = _key(s)
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out[:max(pop_size, 12)]


def _op_groups() -> dict:
    from factors.sweep import generate_grid
    groups: dict = {}
    for s in generate_grid():
        sig = tuple(sorted((s.get("params") or {}).keys()))
        groups.setdefault(sig, set()).add(s["op"])
    return {k: sorted(v) for k, v in groups.items()}


def _fitness(is_panel: dict, spec: dict, hold: int) -> float:
    """IS 段真实调仓 Sharpe；算不出来返回 -inf（不参与繁殖）。"""
    from core.ashare_backtest import ls_nav_true_rebalance
    try:
        nav = ls_nav_true_rebalance(is_panel, spec, hold=hold,
                                    cost_bps=30.0, apply_limit=False)
        sh = float(nav.get("sharpe", float("nan")))
        return sh if sh == sh and np.isfinite(sh) else float("-inf")
    except Exception:  # noqa: BLE001
        return float("-inf")


def evolve(panel: dict, hold: int = 1, train_frac: float = 0.7,
           generations: int = 3, pop_size: int = 24, elite: int = 6,
           seed: int | None = None, seed_specs: list[dict] | None = None,
           avoid: dict | None = None, verbose: bool = True) -> list[dict]:
    """返回按 IS 适应度排序、去重后的候选 spec 列表。

    调用方负责把这些 spec 并入同一个 FDR 池；本函数不判幸存、不记 N。
    """
    from core.backtest import split_periods

    rng = random.Random(seed if seed is not None else 20261111)
    groups = _op_groups()
    if avoid is None:
        from core.rejection_memory import load_constraints
        avoid = load_constraints()
    avoid_ops = set(avoid.get("avoid_ops") or [])
    tr, _ = split_periods(panel, train_frac)
    is_panel = {k: v.loc[tr] for k, v in panel.items()}

    cache: dict[str, float] = {}
    hall: dict[str, dict] = {}

    def score(s: dict) -> float:
        k = _key(s)
        if k not in cache:
            cache[k] = _fitness(is_panel, s, hold)
        hall[k] = s
        return cache[k]

    pop = _initial_population(pop_size, rng, seed_specs)
    rng.shuffle(pop)  # 顺序随机，但 combo 已在集合内，不会被截断挤掉
    # 失败记忆约束：把历史上被证伪的骨架/算子从初始种群里剔掉
    if avoid:
        from core.rejection_memory import should_avoid
        filtered = [s for s in pop if not should_avoid(s, avoid)]
        if len(filtered) >= 6:
            pop = filtered
    for s in pop:
        score(s)

    for gen in range(generations):
        ranked = sorted(pop, key=score, reverse=True)
        best = ranked[:max(2, elite)]
        nxt: list[dict] = [copy.deepcopy(s) for s in best]  # 精英保留
        # 变异 / 交叉 / 组合
        while len(nxt) < pop_size:
            r = rng.random()
            if r < 0.55:
                nxt.append(_mutate(rng.choice(best), rng, groups, avoid_ops))
            elif r < 0.80 and len(best) >= 2:
                a, b = rng.sample(best, 2)
                nxt.append(_combine_two(a, b, rng))
            elif r < 0.92 and len(best) >= 2:
                a, b = rng.sample(best, 2)
                nxt.append(_crossover(a, b, rng))
            else:
                nxt.append(rng.choice(_initial_population(4, rng)))
        # 本代去重
        seen, uniq = set(), []
        for s in nxt:
            k = _key(s)
            if k not in seen:
                seen.add(k)
                uniq.append(s)
        pop = uniq
        for s in pop:
            score(s)
        top = sorted(pop, key=score, reverse=True)[:3]
        if verbose:
            print("  [evolve] 第 %d/%d 代：种群 %d，最佳 IS Sharpe %s"
                  % (gen + 1, generations, len(pop),
                     ", ".join("%.3f" % score(s) for s in top)), flush=True)

    # 释放 IS 切片，别占着内存进正式回测
    del is_panel
    gc.collect()

    out = sorted(hall.values(), key=lambda s: cache[_key(s)], reverse=True)
    out = [s for s in out if cache[_key(s)] > float("-inf")]
    if verbose:
        print("  [evolve] 累计评估 %d 个唯一 spec，返回 %d 个候选（仅按 IS 选，OOS 未看）"
              % (len(cache), len(out[:pop_size])), flush=True)
    return out[:pop_size]
