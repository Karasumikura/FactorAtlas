"""回归测试：共享提案池（一个 round 只调用一次 LLM）+ 自愈闸门。

背景（2026-10-17）：
  原结构下 mine_loop.sh 每轮启动 5 个 mine 进程，每个进程各自调用一次 LLM：
  成本 5 倍、跨进程重复提案、彼此信息不共享。改为「一个 round 建一次池，
  5 个进程从池里取用」。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import proposal_pool as PP


class TestProposalPool(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.round_dir = self.d.name

    def tearDown(self):
        self.d.cleanup()

    def test_load_pool_returns_none_when_missing(self):
        self.assertIsNone(PP.load_pool(self.round_dir))

    def test_load_pool_returns_none_on_corrupt_file(self):
        p = PP.pool_path(self.round_dir)
        p.write_text("{ 这不是合法 JSON")
        self.assertIsNone(PP.load_pool(self.round_dir))

    def test_pool_is_idempotent(self):
        """池已存在时必须直接复用，不重复烧 LLM。"""
        p = PP.pool_path(self.round_dir)
        p.write_text(json.dumps({"specs": [{"name": "a", "op": "vol",
                                           "params": {"n": 20}, "direction": 1}]}))
        got = PP.build_pool(self.round_dir, verbose=False)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "a")

    def test_force_rebuild_ignores_existing(self):
        p = PP.pool_path(self.round_dir)
        p.write_text(json.dumps({"specs": [{"name": "old"}]}))
        # force + 让 LLM 提案必然失败（不实际调网络），应得到空池而非旧池
        import unittest.mock as mock
        with mock.patch("llm.proposer_batch.propose_batch",
                        side_effect=RuntimeError("no network")), \
             mock.patch("llm.proposer_composite.propose_composite_batch",
                        side_effect=RuntimeError("no network")):
            got = PP.build_pool(self.round_dir, total=4, force=True, verbose=False)
        self.assertEqual(got, [])

    def test_pool_dedupes_identical_specs(self):
        import unittest.mock as mock
        dup = {"name": "x", "op": "vol", "params": {"n": 20}, "direction": 1}
        with mock.patch("llm.proposer_batch.propose_batch", return_value=[dup, dup]), \
             mock.patch("llm.proposer_composite.propose_composite_batch",
                        return_value=[dup]):
            got = PP.build_pool(self.round_dir, total=10, composite_ratio=0.0,
                                verbose=False)
        self.assertEqual(len(got), 1)

    def test_no_adaptive_shift_by_default(self):
        """默认不启用「空间饱和」降级：单算子产出低时，复合仍只拿一半配额。

        原因：该降级依据的「单算子空间已饱和」结论已被证伪
        （见 core/proposal_pool.py 的【结论修正】）。此前它在第一轮把 594 个
        配额全给了复合因子，产出 212 个复合因子，池子近一半是复合。
        """
        import unittest.mock as mock
        comp = {"name": "c", "kind": "combo", "expr": {"op": "div"},
                "direction": 1}
        with mock.patch("llm.proposer_batch.propose_batch", return_value=[]), \
             mock.patch("llm.proposer_composite.propose_composite_batch",
                        return_value=[comp]) as m:
            PP.build_pool(self.round_dir, total=10, verbose=False)
        # 复合只拿一半配额（5），而不是几乎全部（9+）
        self.assertEqual(m.call_args.kwargs.get("total", 0), 5)

    def test_adaptive_shift_when_explicitly_enabled(self):
        """显式传 adaptive=True 时，降级仍可用（保留实验能力）。"""
        import unittest.mock as mock
        comp = {"name": "c", "kind": "combo", "expr": {"op": "div"},
                "direction": 1}
        with mock.patch("llm.proposer_batch.propose_batch", return_value=[]), \
             mock.patch("llm.proposer_composite.propose_composite_batch",
                        return_value=[comp]) as m:
            PP.build_pool(self.round_dir, total=10, adaptive=True, verbose=False)
        # 复合提案应拿到接近全部配额（而不是一半）
        self.assertGreaterEqual(m.call_args.kwargs.get("total", 0), 9)


class TestSelfRepairGates(unittest.TestCase):
    """自愈闸门：不通过校验的补丁绝不能落盘。"""

    def setUp(self):
        from core import self_repair as SR
        self.SR = SR
        self.d = tempfile.TemporaryDirectory()
        self.root = Path(self.d.name)
        (self.root / "core").mkdir()

    def tearDown(self):
        self.d.cleanup()

    def test_forbidden_paths_rejected(self):
        for bad in ("data/factor_library.json", ".env", "venv/lib/x.py",
                    "tests/test_promote.py", "runs/a/b.json"):
            self.assertIsNone(self.SR._safe_path(bad), f"应拒绝 {bad}")

    def test_allowed_paths_accepted(self):
        for good in ("core/mine.py", "factors/sweep.py", "llm/client.py"):
            self.assertIsNotNone(self.SR._safe_path(good), f"应允许 {good}")

    def test_syntax_error_rejected(self):
        target = self.SR.PROJ / "core" / "proposal_pool.py"
        ok, reasons = self.SR.verify_gates(target, "def broken(:\n    pass\n")
        self.assertFalse(ok)
        self.assertTrue(any("语法" in r for r in reasons))

    def test_oversized_diff_rejected(self):
        target = self.SR.PROJ / "core" / "proposal_pool.py"
        # 造一个语法正确但改动巨大的内容 → 应因行数上限被拒
        huge = "\n".join([f"# filler {i}" for i in range(500)])
        ok, reasons = self.SR.verify_gates(target, huge)
        self.assertFalse(ok)
        self.assertTrue(any("改动过大" in r for r in reasons))

    def test_infer_source_file_from_traceback(self):
        snippet = (
            'Traceback (most recent call last):\n'
            '  File "/opt/quant-lab/core/mine.py", line 110, in add\n'
            '    k = _spec_key(s)\n'
            "NameError: cannot access free variable '_spec_key'\n"
        )
        self.assertEqual(self.SR._infer_source_file(snippet), "core/mine.py")

    def test_infer_ignores_files_outside_project(self):
        snippet = 'File "/usr/lib/python3.12/json.py", line 5, in dumps\n'
        self.assertIsNone(self.SR._infer_source_file(snippet))


if __name__ == "__main__":
    unittest.main()
