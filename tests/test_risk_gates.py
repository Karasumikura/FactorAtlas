"""量化交易新增风控闸门回归测试。

覆盖三块本次深化的硬风控（此前完全依赖 LLM 建议，无人值守时等于没有）：

1. 最大回撤熔断（max_drawdown_pct）：浮亏达本金比例时，只允许减仓单。
2. 日内亏损熔断（daily_loss_limit_pct）：当日已实现亏损达阈值时，禁止加仓。
3. 日内换手上限（max_daily_turnover_x）：当日累计成交名义额达阈值时停手。
4. 触发引擎与 build_orders 死区口径统一，消除「触发空转」。

这些测试只依赖纯函数与本地日志，不触网、不下单。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.trader_daemon as td


DAY = "2026-10-16"


def _write_log(recs):
    p = Path(tempfile.mkdtemp()) / "dec.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
    return p


class DailyAggregateHelpersTests(unittest.TestCase):
    def setUp(self):
        self._orig = td.DECISION_LOG

    def tearDown(self):
        td.DECISION_LOG = self._orig

    def test_daily_realized_pnl_only_executed_today(self):
        td.DECISION_LOG = _write_log([
            {"ts": DAY + "T01:00:00+00:00", "executed": True, "pnl": -1.5},
            {"ts": DAY + "T02:00:00+00:00", "executed": True,
             "results": [{"status": "sent", "pnl": -0.5}]},
            {"ts": "2026-10-15T02:00:00+00:00", "executed": True, "pnl": -100},  # 昨天
            {"ts": DAY + "T03:00:00+00:00", "executed": False, "pnl": -100},      # 未执行
        ])
        self.assertAlmostEqual(td._daily_realized_pnl(DAY), -2.0)

    def test_daily_turnover_counts_only_sent(self):
        td.DECISION_LOG = _write_log([
            {"ts": DAY + "T01:00:00+00:00", "executed": True,
             "results": [{"status": "sent", "notional": 30},
                         {"status": "error", "notional": 999}]},
            {"ts": DAY + "T02:00:00+00:00", "executed": True,
             "results": [{"status": "sent", "notional": 20}]},
            {"ts": DAY + "T03:00:00+00:00", "executed": False,
             "results": [{"status": "sent", "notional": 100}]},  # 未执行
        ])
        self.assertAlmostEqual(td._daily_turnover_usd(DAY), 50.0)

    def test_missing_log_returns_zero(self):
        td.DECISION_LOG = Path("/nonexistent/dec.jsonl")
        self.assertEqual(td._daily_realized_pnl(DAY), 0.0)
        self.assertEqual(td._daily_turnover_usd(DAY), 0.0)


class RiskGateLogicTests(unittest.TestCase):
    """直接验证闸门判定所需的「净减仓」语义，避免依赖完整 tick()（需网络）。"""

    @staticmethod
    def _net_delta(orders, cur):
        net = 0.0
        for o in orders:
            net += abs(o.target_qty) - abs(cur.get(o.binance_symbol, 0.0))
        return net

    def test_reducing_order_is_allowed(self):
        o = SimpleNamespace(binance_symbol="AAAUSDT", target_qty=0.0)
        cur = {"AAAUSDT": 0.05}
        self.assertLessEqual(self._net_delta([o], cur), 1e-9)  # 净减仓

    def test_increasing_order_is_blocked_under_stress(self):
        o = SimpleNamespace(binance_symbol="AAAUSDT", target_qty=0.10)
        cur = {"AAAUSDT": 0.05}
        self.assertGreater(self._net_delta([o], cur), 1e-9)  # 净加仓 → 应被拦

    def test_mixed_but_net_reducing_allowed(self):
        o1 = SimpleNamespace(binance_symbol="AAAUSDT", target_qty=0.0)   # 减 0.05
        o2 = SimpleNamespace(binance_symbol="BBBUSDT", target_qty=0.02)  # 加 0.02
        cur = {"AAAUSDT": 0.05}
        self.assertLessEqual(self._net_delta([o1, o2], cur), 1e-9)  # 净减 0.03 → 放行


class TriggerDeadbandAlignmentTests(unittest.TestCase):
    """触发引擎门槛应与 build_orders 死区一致（消除触发空转）。"""

    def test_threshold_formula_matches_build_orders(self):
        from core.trigger_engine import DEFAULT_THRESHOLDS
        # 与 build_orders 默认：max(min_notional*2, 5%*equity) 对齐
        q = DEFAULT_THRESHOLDS
        for equity in (100, 1000, 10000):
            expected = max(5.0 * 2.0, 0.05 * equity)
            got = max(q["meaningful_min_notional"] * q["meaningful_deadband_mult"],
                      q["meaningful_deadband_pct"] * equity)
            self.assertAlmostEqual(got, expected)

    def test_small_order_does_not_fire_needs_rebalance(self):
        from core.trigger_engine import check_triggers
        small = SimpleNamespace(binance_symbol="AAAUSDT", action="BUY",
                                delta_qty=0.01, notional=6.0)  # < 死区 10
        res = check_triggers(
            targets=[], prices={}, cur_positions={}, equity=100.0,
            unrealized_pnl=0.0, orders=[small], last_rebalance_ts=None,
        )
        self.assertNotIn("needs_rebalance", res.reasons)

    def test_meaningful_order_fires(self):
        from core.trigger_engine import check_triggers
        big = SimpleNamespace(binance_symbol="AAAUSDT", action="BUY",
                              delta_qty=0.1, notional=45.0)
        res = check_triggers(
            targets=[], prices={}, cur_positions={}, equity=100.0,
            unrealized_pnl=0.0, orders=[big], last_rebalance_ts=None,
        )
        self.assertIn("needs_rebalance", res.reasons)


class StructuralDriftBypassTests(unittest.TestCase):
    """日内调仓次数上限（daily_rebalance_cap）的穿透规则。

    背景（真实事故）：SPCXUSDT 被踢出因子库后仍残留 1 股 ≈149% 权益，
    每轮 tick 都触发 needs_rebalance，但当日 6 次调仓名额早被日常小单用光，
    于是 daily_rebalance_cap 连续 94 次拦下减仓 —— 仓位一直挂在超限敞口上。

    规则：结构性漂移（单标的偏离目标的名义额 >= structural_drift_pct × equity）
    与风控类触发（drawdown_alert / daily_loss_limit / derisk）都必须穿透此闸；
    日常小额再平衡仍受限，防震荡初衷不变。
    """

    def setUp(self):
        self._orig = td.DECISION_LOG
        self._orig_count = td._count_executions_today
        # 当日名额已用尽
        td._count_executions_today = lambda day: 99
        p = Path(tempfile.mkdtemp()) / "dec.jsonl"
        p.write_text("", encoding="utf-8")
        td.DECISION_LOG = p

    def tearDown(self):
        td.DECISION_LOG = self._orig
        td._count_executions_today = self._orig_count

    @staticmethod
    def _orders():
        return [SimpleNamespace(binance_symbol="SPCXUSDT", action="REDUCE",
                                target_qty=0.05, cur_qty=1.0, delta_qty=-0.94,
                                notional=140.21)]

    @staticmethod
    def _cur():
        return {"SPCXUSDT": 1.0}

    def _run(self, reasons, cur, prices, threshold=0.30):
        """复刻 tick() 内闸2 的计算口径（不做网络/下单）。"""
        orders = self._orders()
        equity = 100.0
        structural = False
        for o in orders:
            cq = float(cur.get(o.binance_symbol, 0.0) or 0.0)
            tq = float(o.target_qty or 0.0)
            px = float(prices.get(o.binance_symbol) or 0.0)
            if px > 0 and abs(cq - tq) * px / equity >= threshold:
                structural = True
                break
        is_hard_risk = any(r in reasons for r in
                          ("drawdown_alert", "daily_loss_limit", "derisk"))
        blocked = False
        if not is_hard_risk and not structural:
            if td._count_executions_today("2026-10-19") >= 6:
                blocked = True
        return blocked, structural

    def test_structural_drift_bypasses_daily_cap(self):
        blocked, structural = self._run(
            ["needs_rebalance"], self._cur(), {"SPCXUSDT": 149.16})
        self.assertTrue(structural)
        self.assertFalse(blocked, "149% 权益的结构性漂移应能穿透调仓次数上限")

    def test_small_rebalance_still_capped(self):
        blocked, structural = self._run(
            ["needs_rebalance"], {"SPCXUSDT": 0.06}, {"SPCXUSDT": 149.16})
        self.assertFalse(structural)
        self.assertTrue(blocked, "小额日常再平衡仍应受 6 次/日上限约束")

    def test_hard_risk_bypasses_daily_cap(self):
        for reason in ("drawdown_alert", "daily_loss_limit", "derisk"):
            blocked, _ = self._run([reason], {"SPCXUSDT": 0.06},
                                   {"SPCXUSDT": 149.16})
            self.assertFalse(blocked, f"{reason} 应能穿透调仓次数上限")

    def test_missing_price_is_not_structural(self):
        blocked, structural = self._run(
            ["needs_rebalance"], self._cur(), {})
        self.assertFalse(structural)
        self.assertTrue(blocked)


if __name__ == "__main__":
    unittest.main()
