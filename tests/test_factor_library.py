"""因子库消费 + composite 合成 的回归测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import factor_library as FL


def _seed_lib(d: Path, factors: dict) -> None:
    (d / "lib.json").write_text(json.dumps({"factors": factors}))


class TestComposite(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        FL.LIB_FILE = self.d / "lib.json"
        self._orig = None

    def test_empty_library_returns_none(self):
        _seed_lib(self.d, {})
        self.assertEqual(FL.library_specs(), [])
        self.assertIsNone(FL.composite_as_spec(None))

    def test_library_specs_sorted_by_abs_oos(self):
        _seed_lib(self.d, {
            "a": {"name": "x", "spec": {"op": "mom", "params": {"n": 10}},
                  "oos_median": 0.2},
            "b": {"name": "y", "spec": {"op": "vol", "params": {"n": 20}},
                  "oos_median": -0.9},
        })
        specs = FL.library_specs()
        # |−0.9| > |0.2| → b 在前
        self.assertEqual(specs[0]["op"], "vol")

    def test_composite_from_library_produces_frame(self):
        import warnings
        warnings.filterwarnings("ignore")
        from core import data
        _seed_lib(self.d, {
            "a": {"name": "mom", "spec": {"op": "mom", "params": {"n": 120},
                                          "direction": 1}, "oos_median": 0.5},
            "b": {"name": "vol", "spec": {"op": "vol", "params": {"n": 60},
                                          "direction": 1}, "oos_median": 0.4},
        })
        p = data.build_panel(market="us")
        comp = FL.composite_factor(p)
        self.assertIsNotNone(comp)
        self.assertEqual(comp.shape, p["c"].shape)
        # 覆盖率应较高
        self.assertGreater(float(comp.notna().mean().mean()), 0.5)

    def test_library_spec_mode_fallback_empty(self):
        _seed_lib(self.d, {})
        self.assertIsNone(FL.composite_as_spec(None))


class TestTraderResolve(unittest.TestCase):
    def test_library_falls_back_when_empty(self):
        import warnings
        warnings.filterwarnings("ignore")
        from core import factor_library as FL2
        from core import trader_daemon as td
        d = Path(tempfile.mkdtemp())
        FL2.LIB_FILE = d / "lib.json"
        spec = td._resolve_spec("library")
        # 空库 → 回退 mom250
        self.assertEqual(spec.get("op"), "mom")
        self.assertEqual(spec.get("params", {}).get("n"), 250)

    def test_named_factor_passthrough(self):
        from core import trader_daemon as td
        spec = td._resolve_spec("trend120")
        self.assertEqual(spec.get("op"), "trend_strength")


if __name__ == "__main__":
    unittest.main()
