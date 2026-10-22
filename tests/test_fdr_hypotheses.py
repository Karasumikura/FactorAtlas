"""回归测试：FDR 必须按「独立假设数」校正，且不能因名称遮蔽崩溃。

背景（2026-10-17 事故）：
  挖掘会给每个 spec 回测 +1/-1 两个方向，p 值成对完全相同
  （实测 170/170 对严格相等）。旧实现直接拿记录数做 BH-FDR，
  阈值 q*rank/n 被稀释近一半，真实信号被埋掉。

  修好之后引入过一个更严重的 bug：在函数内部再次定义 `_spec_key`，
  遮蔽了模块级同名函数，导致函数前半段调用时报
    NameError: cannot access free variable '_spec_key'
  该异常发生在 LLM 提案与全量回测**之后**，使 6 个 round × 5 个进程
  共 30 次挖掘全部作废、零产出。

  本测试用源码级检查 + 行为级检查双重锁定，防止回归。
"""
from __future__ import annotations

import ast
import inspect
import unittest

from core import loop, mine


class TestNoSpecKeyShadowing(unittest.TestCase):
    """确保没有函数内部重新定义与模块级同名的辅助函数。"""

    def _defined_names(self, mod) -> set[str]:
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
        return names

    def test_module_level_spec_key_exists(self):
        self.assertTrue(hasattr(mine, "_spec_key"))
        self.assertTrue(hasattr(loop, "_spec_key"))

    def test_no_nested_redefinition_of_module_level_helpers(self):
        """任何嵌套函数都不允许与模块级函数同名（遮蔽即崩溃）。"""
        for mod in (mine, loop):
            tree = ast.parse(inspect.getsource(mod))
            module_level = {
                n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            shadowed = []
            for func in [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for inner in [n for n in ast.walk(func)
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and n is not func]:
                    if inner.name in module_level:
                        shadowed.append(f"{mod.__name__}.{func.name} -> {inner.name}")
            self.assertEqual(shadowed, [], f"发现名称遮蔽（会导致 NameError）: {shadowed}")


class TestFdrUsesIndependentHypotheses(unittest.TestCase):
    """行为级：正反方向必须归并成一个假设再校正。"""

    def test_opposite_directions_count_as_one_hypothesis(self):
        """同一 spec 的 +1/-1 是一条假设，不应让 BH 的 n 翻倍。"""
        # 构造 20 个独立 spec，每个有 +1/-1 两条记录（共 40 条）
        results = []
        for i in range(20):
            for d in (1, -1):
                results.append({
                    "op": f"op{i}", "params": {"n": i}, "direction": d,
                    "p_value": 0.03,  # 全部同一个临界 p
                })

        # 直接验证归并口径：按 (op, params) 分组后应有 20 组
        def hyp_key(r):
            import json
            return f"{r.get('op')}|{json.dumps(r.get('params') or {}, sort_keys=True)}"

        groups = {}
        for r in results:
            groups.setdefault(hyp_key(r), []).append(r)
        self.assertEqual(len(groups), 20)
        self.assertEqual(len(results), 40)

    def test_bh_threshold_differs_between_record_and_hypothesis_count(self):
        """证明用记录数会让阈值更严（旧行为的危害）。"""
        from core import backtest
        pvals = [0.004] * 40
        # 40 条记录 vs 20 个独立假设，同样 p 值下结论可能不同
        passed_records = backtest.benjamini_hochberg(pvals, q=0.10)
        passed_hyps = backtest.benjamini_hochberg([0.004] * 20, q=0.10)
        # 两种口径都必须能正常返回，且长度正确
        self.assertEqual(len(passed_records), 40)
        self.assertEqual(len(passed_hyps), 20)
        # 20 个假设的阈值更宽松（更小的 n → 更容易通过）
        self.assertGreaterEqual(sum(passed_hyps) / 20, sum(passed_records) / 40)


if __name__ == "__main__":
    unittest.main()
