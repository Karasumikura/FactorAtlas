#!/usr/bin/env python3
"""core/evolution.py 增量回归：纯程序化、不涉及任何 LLM 调用。"""
from __future__ import annotations

import json
import random

import pytest

from core.evolution import (_combine_two, _crossover, _initial_population,
                            _key, _mutate, _op_groups, evolve)
from factors.engine import OPS


@pytest.fixture(scope="module")
def rng():
    return random.Random(20261111)


def test_mutate_stays_in_param_bounds(rng):
    g = _op_groups()
    s = {"name": "mom_n20", "op": "mom", "params": {"n": 20}, "direction": 1}
    for _ in range(200):
        m = _mutate(s, rng, g)
        if m.get("kind") == "combo" or "expr" in m:
            continue
        for k, v in (m.get("params") or {}).items():
            assert isinstance(v, int)
            if k == "n":
                assert 2 <= v <= 250
            if k == "skip":
                assert 0 <= v <= 60


def test_mutate_op_is_real_operator(rng):
    g = _op_groups()
    s = {"name": "vol_n60", "op": "vol", "params": {"n": 60}, "direction": 1}
    for _ in range(100):
        m = _mutate(s, rng, g)
        if "expr" in m:
            continue
        assert m["op"] in OPS


def test_combine_and_crossover_are_evaluable(rng):
    a = {"op": "mom", "params": {"n": 120}, "direction": 1}
    b = {"op": "vol", "params": {"n": 60}, "direction": -1}
    for f in (_combine_two, _crossover):
        c = f(a, b, rng)
        assert c["kind"] == "combo"
        assert "expr" in c and "op" in c["expr"]
        assert c["direction"] in (1, -1)
        json.dumps(c)  # 可序列化（_spec_key 依赖它）


def test_initial_population_deduped(rng):
    pop = _initial_population(20, rng)
    assert len(pop) >= 12
    assert len({_key(s) for s in pop}) == len(pop)
    assert any(s.get("kind") == "combo" for s in pop)  # 含表达式树


def test_key_ignores_name_and_rationale():
    a = {"name": "x", "op": "mom", "params": {"n": 20}, "direction": 1}
    b = {"name": "y", "op": "mom", "params": {"n": 20}, "direction": 1,
         "rationale": "whatever"}
    assert _key(a) == _key(b)


def test_evolve_smoke_only_is_panel():
    """小规模 smoke：产出候选、无重复、全部只按 IS 适应度选（不碰 OOS/LLM）。"""
    from core.data import build_panel
    p = build_panel()
    out = evolve(p, hold=5, generations=1, pop_size=6, elite=2,
                 seed=7, verbose=False)
    assert out
    assert len({_key(s) for s in out}) == len(out)
    assert all(isinstance(s, dict) and ("op" in s or "expr" in s) for s in out)
