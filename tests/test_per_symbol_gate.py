"""闸2.5 单标的震荡闸门 的回归测试：交易次数 + 方向反转计数与拦截语义。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import trader_daemon as td


def _rec(ts, executed, orders):
    return {"ts": ts, "executed": executed, "orders": orders}


class TestSymbolCounters(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.log = self.d / "dec.jsonl"
        td.DECISION_LOG = self.log
        recs = [
            _rec("2026-10-15T15:01:00+00:00", True,
                 [{"binance_symbol": "MSTRUSDT", "side": "BUY"}]),
            _rec("2026-10-15T15:05:00+00:00", True,
                 [{"binance_symbol": "MSTRUSDT", "side": "SELL"}]),   # flip 1
            _rec("2026-10-15T15:10:00+00:00", True,
                 [{"binance_symbol": "MSTRUSDT", "side": "BUY"}]),    # flip 2
            _rec("2026-10-15T15:15:00+00:00", True,
                 [{"binance_symbol": "MSTRUSDT", "side": "SELL"}]),   # flip 3
            _rec("2026-10-15T15:20:00+00:00", True,
                 [{"binance_symbol": "NVDAUSDT", "side": "BUY"}]),
            _rec("2026-10-16T01:00:00+00:00", True,
                 [{"binance_symbol": "MSTRUSDT", "side": "BUY"}]),    # 隔日不计
            _rec("2026-10-15T15:25:00+00:00", False,
                 [{"binance_symbol": "MSTRUSDT", "side": "BUY"}]),    # 未执行不计
        ]
        with self.log.open("w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")

    def test_symbol_exec_count(self):
        cnt = td._symbol_exec_counts_today("2026-10-15")
        self.assertEqual(cnt.get("MSTRUSDT"), 4)
        self.assertEqual(cnt.get("NVDAUSDT"), 1)

    def test_symbol_flips(self):
        flips = td._symbol_flips_today("2026-10-15")
        self.assertEqual(flips.get("MSTRUSDT"), 3)   # BUY→SELL→BUY→SELL
        self.assertIsNone(flips.get("NVDAUSDT"))

    def test_day_isolation(self):
        cnt = td._symbol_exec_counts_today("2026-10-16")
        self.assertEqual(cnt.get("MSTRUSDT"), 1)     # 只算 10-16 那条

    def test_not_executed_ignored(self):
        # 未执行的那条不应计入
        cnt = td._symbol_exec_counts_today("2026-10-15")
        self.assertEqual(cnt.get("MSTRUSDT"), 4)

    def test_monotone_direction_no_flip(self):
        # 全是 BUY 则反转数为 0
        d2 = Path(tempfile.mkdtemp())
        log2 = d2 / "d.jsonl"
        td.DECISION_LOG = log2
        with log2.open("w") as f:
            for i in range(5):
                f.write(json.dumps(_rec(f"2026-10-15T15:0{i}:00+00:00", True,
                                        [{"binance_symbol": "X", "side": "BUY"}])) + "\n")
        self.assertEqual(td._symbol_flips_today("2026-10-15").get("X", 0), 0)
        self.assertEqual(td._symbol_exec_counts_today("2026-10-15").get("X"), 5)


class TestPerSymbolCapLogic(unittest.TestCase):
    """复刻闸2.5 的拦截语义：触发上限的标的被剔除，其余保留。"""

    @staticmethod
    def _apply(orders, exec_cnt, flips, daily_cap, flip_cap):
        blocked = []
        for s, c in exec_cnt.items():
            if daily_cap > 0 and c >= daily_cap:
                blocked.append(s)
            elif flip_cap > 0 and flips.get(s, 0) >= flip_cap:
                blocked.append(s)
        kept = [o for o in orders if o["binance_symbol"] not in blocked]
        return kept, blocked

    def test_churn_symbol_stripped_others_kept(self):
        orders = [{"binance_symbol": "MSTRUSDT", "side": "BUY"},
                  {"binance_symbol": "NVDAUSDT", "side": "BUY"}]
        kept, blocked = self._apply(orders, {"MSTRUSDT": 5, "NVDAUSDT": 1},
                                    {}, 4, 2)
        self.assertIn("MSTRUSDT", blocked)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["binance_symbol"], "NVDAUSDT")

    def test_flip_cap_triggers_even_under_daily_cap(self):
        # 交易 3 次（<4）但反转 3 次（>=2）→ 仍拦
        orders = [{"binance_symbol": "MSTRUSDT", "side": "BUY"}]
        kept, blocked = self._apply(orders, {"MSTRUSDT": 3}, {"MSTRUSDT": 3}, 4, 2)
        self.assertIn("MSTRUSDT", blocked)
        self.assertEqual(kept, [])

    def test_cap_zero_disables(self):
        orders = [{"binance_symbol": "MSTRUSDT", "side": "BUY"}]
        kept, blocked = self._apply(orders, {"MSTRUSDT": 99}, {"MSTRUSDT": 99}, 0, 0)
        self.assertEqual(blocked, [])
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
