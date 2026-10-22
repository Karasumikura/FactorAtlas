"""任务1-3 深化回归测试：主动降险 / 资金费率闸门 / 持仓对账。

只测【纯逻辑】，不碰网络/交易所：
  - force_derisk：只减不加、方向不翻、gross 压到目标以下
  - 资金费率判定：多空腿成本口径
  - 对账漂移检测：阈值语义
"""
from __future__ import annotations

import unittest

from core.bridge_us_binance import force_derisk


class TestForceDerisk(unittest.TestCase):
    def setUp(self):
        self.eq = 100.0
        self.prices = {"A": 20.0, "B": 30.0, "C": 10.0, "D": 40.0}
        # 当前 gross = 100+120+20+80 = 320
        self.cur = {"A": 5.0, "B": 4.0, "C": -2.0, "D": 2.0}

    def _gross(self, orders):
        return sum(abs(o.target_qty * o.ref_price) for o in orders)

    def test_deep_drawdown_trims_to_target(self):
        dd = 0.35
        target_gross = 2.0 * self.eq * (1 - dd)   # 130
        orders = force_derisk([], self.eq, self.prices, self.cur, dd, gross_cap=2.0)
        self.assertTrue(orders, "深回撤必须产出降险单")
        self.assertLessEqual(self._gross(orders), target_gross + 1e-6)

    def test_never_increases_any_leg(self):
        orders = force_derisk([], self.eq, self.prices, self.cur, 0.35, gross_cap=2.0)
        for o in orders:
            self.assertLessEqual(abs(o.target_qty), abs(o.cur_qty) + 1e-9,
                                 f"{o.binance_symbol} 被加仓了")

    def test_never_flips_direction(self):
        orders = force_derisk([], self.eq, self.prices, self.cur, 0.35, gross_cap=2.0)
        for o in orders:
            import math
            if abs(o.target_qty) > 1e-9:
                self.assertEqual(math.copysign(1, o.target_qty),
                                 math.copysign(1, o.cur_qty),
                                 f"{o.binance_symbol} 方向被翻")

    def test_no_action_when_exposure_below_target(self):
        # 当前 gross=115，gross_cap=2.0、dd=0.12 -> target=176 > 115 -> 不动
        small = {"A": 1.0, "B": 2.0, "C": -1.5, "D": 0.5}
        orders = force_derisk([], self.eq, self.prices, small, 0.12, gross_cap=2.0)
        self.assertEqual(orders, [])

    def test_zero_drawdown_no_action(self):
        orders = force_derisk([], self.eq, self.prices, self.cur, 0.0, gross_cap=2.0)
        self.assertEqual(orders, [])

    def test_short_leg_reduced_via_buy(self):
        orders = force_derisk([], self.eq, self.prices, self.cur, 0.35, gross_cap=2.0)
        c = next(o for o in orders if o.binance_symbol == "C")
        self.assertEqual(c.side, "BUY", "空头腿减仓应是买入平仓")
        self.assertLess(abs(c.target_qty), abs(c.cur_qty))


class TestFundingGateLogic(unittest.TestCase):
    """资金费率判定口径：多头付 ann_long，空头成本 = -ann_short。"""

    @staticmethod
    def _leg_cost(weight, ann_long, ann_short):
        # 语义：ann_short 恰为 -ann_long（正=付出），两腿各取自己字段
        return ann_long if weight >= 0 else ann_short

    def test_long_pays_long_rate(self):
        self.assertAlmostEqual(self._leg_cost(0.1, 0.25, -0.25), 0.25)

    def test_short_earns_positive_rate(self):
        # 正费率下做空 → 收 >0，成本为负（ann_short = -ann_long = -0.25）
        self.assertLess(self._leg_cost(-0.1, 0.25, -0.25), 0)

    def test_high_cost_leg_pruned(self):
        thr = 0.30
        cost = self._leg_cost(0.1, 0.45, -0.45)   # 多头付 45%
        self.assertGreater(cost, thr)

    def test_low_cost_leg_kept(self):
        thr = 0.30
        cost = self._leg_cost(0.1, 0.10, -0.10)   # 多头付 10%
        self.assertLess(cost, thr)

    def test_short_high_cost_pruned(self):
        # 负费率环境下做空 → 空头要付高额成本 → ann_short 为正
        thr = 0.30
        cost = self._leg_cost(-0.1, -0.45, 0.45)
        self.assertGreater(cost, thr)


class TestReconcileDriftLogic(unittest.TestCase):
    """漂移检测阈值：相对差 >1% 或名义差 >$5。"""

    @staticmethod
    def _is_drift(exq, locq, px):
        dq = exq - locq
        dnotional = abs(dq * px) if px else abs(dq)
        if abs(dq) <= 1e-9:
            return False
        return (abs(exq) > 1e-9 and abs(dq) / max(abs(exq), 1e-9) > 0.01) or dnotional > 5.0

    def test_identical_no_drift(self):
        self.assertFalse(self._is_drift(2.0, 2.0, 50.0))

    def test_large_relative_drift(self):
        self.assertTrue(self._is_drift(2.0, 1.0, 50.0))

    def test_small_drift_below_threshold(self):
        # 1e-6 差异，名义额极小 -> 不算漂移
        self.assertFalse(self._is_drift(2.0, 2.000001, 10.0))

    def test_small_rel_but_large_notional(self):
        # 0.5% 相对差但名义额 $30 -> 算漂移（名义额触发）
        self.assertTrue(self._is_drift(100.0, 100.5, 60.0))


if __name__ == "__main__":
    unittest.main()
