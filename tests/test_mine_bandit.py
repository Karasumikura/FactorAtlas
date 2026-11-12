"""core/mine_bandit.py 增量测试（UCB1 调度，不碰生产状态）。"""
import json
import tempfile
from pathlib import Path

from core import mine_bandit as B

MARKETS = ["us", "crypto"]


def _tmp():
    return tempfile.mktemp(suffix=".json")


def test_unpulled_arm_priority():
    p = _tmp()
    assert B.choose(MARKETS, p) == "us"
    B.record(MARKETS, p, "us", 5.0)
    assert B.choose(MARKETS, p) == "crypto"
    Path(p).unlink(missing_ok=True)


def test_high_reward_arm_dominates_but_no_starvation():
    p = _tmp()
    for _ in range(20):
        m = B.choose(MARKETS, p)
        B.record(MARKETS, p, m, 0.0 if m == "us" else 3.0)
    d = B._load(p)
    assert d["arms"]["us"]["pulls"] >= 1, "探索臂被饿死"
    assert d["arms"]["crypto"]["pulls"] > d["arms"]["us"]["pulls"]
    assert d["total_pulls"] == 20
    Path(p).unlink(missing_ok=True)


def test_state_persists_across_instances():
    p = _tmp()
    B.record(MARKETS, p, "us", 2.0)
    d = B._load(p)
    assert d["arms"]["us"] == {"pulls": 1, "total_reward": 2.0}
    assert json.loads(Path(p).read_text())["total_pulls"] == 1
    Path(p).unlink(missing_ok=True)


def test_corrupt_state_falls_back_cleanly():
    p = _tmp()
    Path(p).write_text("NOT JSON")
    assert B.choose(MARKETS, p) == "us"
    B.record(MARKETS, p, "us", 1.0)
    assert B._load(p)["arms"]["us"]["pulls"] == 1
    Path(p).unlink(missing_ok=True)
