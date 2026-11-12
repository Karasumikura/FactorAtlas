#!/usr/bin/env python3
"""core/rejection_memory.py 增量回归：临时目录，不碰生产记忆文件。"""
from __future__ import annotations

import json

import pytest

import core.rejection_memory as rm


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "MEM", tmp_path)
    monkeypatch.setattr(rm, "CANDIDATES", tmp_path / "candidates.jsonl")
    monkeypatch.setattr(rm, "DECISIONS", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(rm, "CONSTRAINTS", tmp_path / "constraints.json")
    return tmp_path


def _write(run_dir, rows):
    run_dir.mkdir(parents=True)
    (run_dir / "mine_us.json").write_text(json.dumps({
        "hold": 5, "market": "us", "all_results": rows}))


def test_spec_key_ignores_name():
    a = {"name": "x", "op": "mom", "params": {"n": 20}, "direction": 1}
    b = {"op": "mom", "params": {"n": 20}, "direction": 1, "rationale": "z"}
    assert rm.spec_key(a) == rm.spec_key(b)


def test_decision_taxonomy():
    assert rm._decision({"survivor": True})[0] == "survivor"
    assert rm._decision({"fdr_pass": False}) == ("rejected", "fdr_not_passed")
    assert rm._decision({"fdr_pass": True, "oos_confirmed": False}) == (
        "rejected", "oos_not_confirmed")
    assert rm._decision({"fdr_pass": True, "oos_ls_sharpe": float("nan")}) == (
        "rejected", "oos_sharpe_nan")


def test_ingest_and_constraints(mem, tmp_path):
    rows = []
    for i in range(25):
        rows.append({"op": "amihud", "params": {"n": 10 + i}, "direction": 1,
                     "p_value": 0.9, "fdr_pass": False, "survivor": False,
                     "source": "sweep"})
    rows.append({"op": "mom", "params": {"n": 250}, "direction": 1,
                 "p_value": 0.001, "fdr_pass": True, "oos_confirmed": True,
                 "survivor": True, "source": "sweep"})
    _write(tmp_path / "runs" / "loop_x" / "round_01", rows)

    r = rm.ingest_runs(str(tmp_path / "runs" / "loop_*" / "round_*" / "mine_*.json"))
    assert r["unique_specs"] == 26 and r["events"] == 26
    c = rm.build_constraints(min_trials=20, fail_rate=0.90)
    assert "amihud" in c["avoid_ops"]
    assert "mom" not in c["avoid_ops"]
    assert c["n_rejected_keys"] == 25

    # 落盘的 constraints 能被 should_avoid 读回（默认路径已被 monkeypatch）
    assert rm.should_avoid({"op": "amihud", "params": {"n": 11}, "direction": 1})
    assert not rm.should_avoid({"op": "mom", "params": {"n": 250}, "direction": 1})


def test_load_constraints_missing_is_safe(mem):
    c = rm.load_constraints()
    assert c["avoid_ops"] == []
    assert not rm.should_avoid({"op": "mom", "params": {"n": 20}})
