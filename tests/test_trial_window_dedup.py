"""试验数 N 的窗口去重 —— 2026-10-23 新增。

为什么：
  同一份数据窗口（同一批候选网格）被重跑时会话数会不断累加，`total_trials`
  单调膨胀。实测 10-16 到 10-23 的 7 天里 N 从 0 涨到 85041，而面板窗口只
  前进了 6 个交易日；同期有 369 个候选直接命中 zoo 缓存、真正重算 0 个。
  真实新试验接近 0，但 DSR 的多重检验惩罚照样线性加重 → 越挖越升不上去。

规则：会话带 data_window_id 时，该窗口第一次出现才计入 N，重跑计 0。
"""
from __future__ import annotations

import json

import pytest

from core import overfit_guard as og


@pytest.fixture()
def trials_file(tmp_path, monkeypatch):
    p = tmp_path / "factor_trials.json"
    p.write_text(json.dumps({"total_trials": 0, "sessions": []}))
    monkeypatch.setattr(og, "TRIAL_FILE", p)
    monkeypatch.setattr(og, "load_trials", lambda: json.loads(p.read_text()))
    monkeypatch.setattr(og, "save_trials",
                        lambda d: p.write_text(json.dumps(d)))
    return p


def test_first_window_counts(trials_file):
    d = og.record_session(n_candidates=400, n_survivors=5,
                          extra={"data_window_id": "w1"})
    assert d["total_trials"] == 400
    assert d["sessions"][-1]["n_counted"] == 400


def test_same_window_rerun_is_free(trials_file):
    og.record_session(n_candidates=400, n_survivors=5,
                      extra={"data_window_id": "w1"})
    d = og.record_session(n_candidates=463, n_survivors=12,
                          extra={"data_window_id": "w1"})
    assert d["total_trials"] == 400, "同一窗口重跑不能再涨 N"
    assert d["sessions"][-1]["n_counted"] == 0
    assert "同一数据窗口" in d["sessions"][-1]["counted_reason"]
    assert len(d["sessions"]) == 2, "会话仍要留痕，便于审计"


def test_new_window_counts_again(trials_file):
    og.record_session(n_candidates=400, n_survivors=5,
                      extra={"data_window_id": "w1"})
    d = og.record_session(n_candidates=300, n_survivors=3,
                          extra={"data_window_id": "w2"})
    assert d["total_trials"] == 700


def test_legacy_session_without_window_still_counts(trials_file):
    """老格式没有窗口指纹，无从判断是否重复，只能照旧累加（保守）。"""
    og.record_session(n_candidates=100, n_survivors=1)
    d = og.record_session(n_candidates=100, n_survivors=1)
    assert d["total_trials"] == 200


def test_windows_registry_persists(trials_file):
    og.record_session(n_candidates=50, n_survivors=1,
                      extra={"data_window_id": "w1"})
    d = og.record_session(n_candidates=50, n_survivors=1,
                          extra={"data_window_id": "w1"})
    assert d["windows"] == ["w1"]
