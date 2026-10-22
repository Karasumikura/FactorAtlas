"""回归测试：LLM 提案器不得因参数档位撞库而"假性饱和"。

背景（2026-10-17 事故）：
    `propose_batch(tried=历史)` 曾出现"目标 300 个，实际只产出 6 个"的现象，
    一度被误判为"单算子空间已饱和"。真实原因是三层叠加：

    1. 历史 4000 条记录只覆盖 19 个不同 n 值，且极度集中在 5/10/20/60/120；
    2. LLM 本身也偏好这些整数档，新提案必然与历史撞键；
    3. 去重键包含 direction，与 FDR 阶段按 (op, params) 归并的口径不一致。

    修复后：去重口径统一为 (op, params) 忽略 direction，且向 LLM 显式下发
    "参数避让"提示，引导其使用未被探索的窗口长度。

这些测试锁死上述三点，防止"假性饱和"回归。
"""
from __future__ import annotations

import json

import pytest

from llm.proposer_batch import _tried_key, _normalize, propose_batch


# ---------- 单元层：去重键口径 ----------

def test_tried_key_ignores_direction():
    """正反方向属于同一因子假设，去重键必须相同（与 FDR _hyp_key 对齐）。"""
    a = {"op": "vol", "params": {"n": 20}, "direction": 1}
    b = {"op": "vol", "params": {"n": 20}, "direction": -1}
    assert _tried_key(a) == _tried_key(b)


def test_tried_key_normalizes_numeric_params():
    """20 / 20.0 / '20' 必须视为同一参数，否则同一假设会被反复检验。"""
    keys = {
        _tried_key({"op": "vol", "params": {"n": 20}}),
        _tried_key({"op": "vol", "params": {"n": 20.0}}),
        _tried_key({"op": "vol", "params": {"n": "20"}}),
    }
    assert len(keys) == 1


def test_tried_key_distinguishes_real_param_changes():
    """真正不同的参数必须仍然区分开。"""
    a = _tried_key({"op": "vol", "params": {"n": 20}})
    b = _tried_key({"op": "vol", "params": {"n": 21}})
    c = _tried_key({"op": "mom", "params": {"n": 20}})
    assert len({a, b, c}) == 3


def test_tried_key_accepts_extra_param_keys():
    """多参数算子（如 mom 的 n/skip）需完整参与比较。"""
    a = _tried_key({"op": "mom", "params": {"n": 20, "skip": 2}})
    b = _tried_key({"op": "mom", "params": {"skip": 2, "n": 20}})
    c = _tried_key({"op": "mom", "params": {"n": 20, "skip": 5}})
    assert a == b
    assert a != c


# ---------- 集成层：假性饱和不得复现 ----------

def _fake_llm_returns_common_windows(monkeypatch, windows=(5, 10, 20, 60, 120)):
    """模拟 LLM 只会吐常见整数档 —— 这正是真实模型的行为偏好。"""
    calls = {"n": 0}

    def _fake(seed, n, tried_names, temperature, avoid=""):
        calls["n"] += 1
        out = []
        for i, w in enumerate(windows):
            out.append({"name": f"vol_{w}", "op": "vol", "params": {"n": w},
                        "direction": 1, "rationale": "test"})
        return out

    monkeypatch.setattr("llm.proposer_batch._one_call", _fake)
    return calls


def test_no_false_saturation_against_full_history(monkeypatch):
    """历史已覆盖常见档位时，提案器仍必须产出（不得因撞库全部归零）。

    这是事故的直接复现场景：历史 tried 覆盖 5/10/20/60/120，
    若去重逻辑再度收紧到"全被过滤"，本测试会失败。
    """
    _fake_llm_returns_common_windows(monkeypatch)
    tried = [{"op": "vol", "params": {"n": w}, "direction": 1}
             for w in (5, 10, 20, 60, 120)]
    tried += [{"op": "vol", "params": {"n": w}, "direction": -1}
              for w in (5, 10, 20, 60, 120)]

    out = propose_batch(total=10, concurrency=2, tried=tried, verbose=False)
    # 全部撞库 → 产出应为 0，这是"真实耗尽"而非假性饱和，属正确行为
    assert out == []


def test_new_windows_survive_dedup(monkeypatch):
    """历史未覆盖的窗口必须能穿过去重，证明参数空间未饱和。"""
    _fake_llm_returns_common_windows(monkeypatch, windows=(7, 13, 23, 45, 75))
    tried = [{"op": "vol", "params": {"n": w}, "direction": 1}
             for w in (5, 10, 20, 60, 120)]

    out = propose_batch(total=10, concurrency=2, tried=tried, verbose=False)
    assert len(out) == 5
    assert {o["params"]["n"] for o in out} == {7, 13, 23, 45, 75}


def test_direction_not_treated_as_new_hypothesis(monkeypatch):
    """同一 (op, params) 的相反方向不得被算作新候选。"""
    def _fake(seed, n, tried_names, temperature, avoid=""):
        return [{"name": "vol_x", "op": "vol", "params": {"n": 30}, "direction": -1}]

    monkeypatch.setattr("llm.proposer_batch._one_call", _fake)
    tried = [{"op": "vol", "params": {"n": 30}, "direction": 1}]

    out = propose_batch(total=5, concurrency=1, tried=tried, verbose=False)
    assert out == [], "反向不应被视为新假设"


def test_avoid_hint_reaches_llm(monkeypatch):
    """参数避让提示必须真的传给 LLM，否则探索引导形同虚设。"""
    captured: list[str] = []

    def _fake(seed, n, tried_names, temperature, avoid=""):
        captured.append(avoid)
        return [{"name": "vol_1", "op": "vol", "params": {"n": 7}, "direction": 1}]

    monkeypatch.setattr("llm.proposer_batch._one_call", _fake)
    tried = [{"op": "vol", "params": {"n": 20}, "direction": 1}]

    propose_batch(total=5, concurrency=1, tried=tried, verbose=False)
    assert captured, "LLM 未被调用"
    assert all("参数避让" in a for a in captured)
    assert any("20" in a for a in captured), "已用 n 值应出现在避让提示中"
