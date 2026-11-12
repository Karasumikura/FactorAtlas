"""回归测试：分层回测的广度门槛必须随宇宙宽度自适应。

背景（2026-11-01 巡检发现）：
  crypto 池按用户「只留主流币」的铁律筛完只剩 17 只，而 ``_layer_masks``
  写死 ``valid = n >= n_groups*4 = 20``。17 < 20 → 每一天都不可分层 →
  ``gross`` 全是 NaN → ``oos_ls_sharpe=NaN`` → 每轮 FDR 过 200+ 条却
  永远 0 幸存。日志、服务状态全绿，属于「跑得动但产不出」的静默哑火。

  修法：按每日可用标的中位数收窄分组数，保证每组仍有 ≥4 只
  （17 只 → 4 组，门槛 16；美股 600+ 只仍是 5 组，结果逐值不变）。
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from core.backtest_fast import _effective_groups, _layer_masks, layered_ls_fast
from core import zoo as zoo_mod
from factors.exposure import tradable_mask


def _panel(n_cols: int, n_days: int = 60, seed: int = 7):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n_days, freq="D")
    cols = [f"S{i:03d}" for i in range(n_cols)]
    factor = pd.DataFrame(rng.normal(size=(n_days, n_cols)), index=dates, columns=cols)
    fwd = pd.DataFrame(rng.normal(size=(n_days, n_cols)) * 0.01, index=dates, columns=cols)
    return factor, fwd


def _reference_quintile_ls(factor: pd.DataFrame, fwd: pd.DataFrame, n_groups: int = 5):
    """逐日循环的参考实现（未改动的原始口径），用于验证美股路径逐值不变。"""
    gross = []
    for dt in factor.index:
        row = factor.loc[dt].dropna()
        if len(row) < n_groups * 4:
            continue
        ret = fwd.loc[dt].reindex(row.index)
        order = row.rank(method="first")
        group = np.floor(order / len(row) * n_groups).clip(upper=n_groups - 1)
        top = ret[group == n_groups - 1].mean()
        bot = ret[group == 0].mean()
        gross.append(top - bot)
    return pd.Series(gross)


class TestEffectiveGroups(unittest.TestCase):
    def test_crypto_breadth_uses_four_groups(self):
        f, _ = _panel(17)
        self.assertEqual(_effective_groups(f, 5), 4)

    def test_us_breadth_keeps_five_groups(self):
        f, _ = _panel(120)
        self.assertEqual(_effective_groups(f, 5), 5)

    def test_tiny_universe_floor_is_two(self):
        f, _ = _panel(9)
        self.assertEqual(_effective_groups(f, 5), 2)

    def test_empty_panel_returns_input(self):
        f = pd.DataFrame(index=pd.date_range("2024-01-01", periods=5), columns=["A"])
        self.assertEqual(_effective_groups(f, 5), 5)


class TestCryptoLayerNotDead(unittest.TestCase):
    def test_seventeen_names_produce_non_empty_gross(self):
        f, fwd = _panel(17)
        _, _, top, bot, valid, _, _, gross = _layer_masks(f, fwd)
        self.assertEqual(int(valid.sum()), len(f), "17 只宇宙每天都应可分层")
        self.assertEqual(len(gross.dropna()), len(f))
        # 4 组：17 只 → 5/4/4/4，最大组 5 只
        self.assertTrue(1 <= int(top.sum(axis=1).max()) <= 5)
        self.assertTrue(1 <= int(bot.sum(axis=1).max()) <= 5)

    def test_seventeen_names_layered_ls_returns_series(self):
        f, fwd = _panel(17)
        gross, net, long_ret, turnover = layered_ls_fast(f, fwd)
        self.assertGreater(len(gross), 50)
        self.assertGreater(len(net), 50)
        self.assertTrue(np.isfinite(gross).all())
        self.assertGreaterEqual(turnover, 0.0)


class TestCryptoMask(unittest.TestCase):
    """crypto 不能用美股的「仙股」价格下限。"""

    @staticmethod
    def _panel(price: float, cols: list[str], dv: float = 1e8):
        dates = pd.date_range("2026-01-01", periods=5, freq="D")
        c = pd.DataFrame(price, index=dates, columns=cols)
        v = pd.DataFrame(dv / price, index=dates, columns=cols)
        return {"c": c, "v": v}

    def test_low_priced_major_coin_is_tradable(self):
        p = self._panel(0.08, ["BTCUSDT", "DOGEUSDT"])
        m = tradable_mask(p)
        self.assertTrue(bool(m["DOGEUSDT"].all()))

    def test_low_priced_stock_still_excluded(self):
        p = self._panel(0.5, ["AAPL", "INTC"])
        m = tradable_mask(p)
        self.assertFalse(bool(m.to_numpy().any()), "美股仙股仍应被 5 美元下限剔除")

    def test_normal_stock_unchanged(self):
        p = self._panel(50.0, ["AAPL", "INTC"])
        m = tradable_mask(p)
        self.assertTrue(bool(m.to_numpy().all()))


class TestUsPathUnchanged(unittest.TestCase):
    def test_wide_panel_matches_original_quintile_impl(self):
        f, fwd = _panel(120)
        _, _, _, _, _, _, _, gross = _layer_masks(f, fwd)
        ref = _reference_quintile_ls(f, fwd, n_groups=5)
        self.assertEqual(len(gross), len(ref))
        np.testing.assert_allclose(gross.to_numpy(), ref.to_numpy(), atol=1e-12, rtol=0)

    def test_sparse_factor_on_wide_universe_keeps_original_gate(self):
        """美股稀疏因子不能因为自身覆盖少就被降组——那会悄悄放宽既有口径。"""
        f, fwd = _panel(120)
        sparse = f.copy()
        sparse.iloc[:, 12:] = np.nan  # 只剩 12 只覆盖
        _, _, _, _, valid, _, _, gross = _layer_masks(sparse, fwd)
        self.assertEqual(int(valid.sum()), 0, "12 只 < 20，应维持原有不可分层判定")
        self.assertEqual(len(gross.dropna()), 0)


class TestZooCacheVersion(unittest.TestCase):
    """口径变了，旧缓存必须失效，否则 crypto 会一直复用修复前的 0 幸存结果。"""

    def test_record_stamps_algo_and_reuse_requires_match(self):
        z = {"factors": {}}
        spec = {"op": "mom", "params": {"n": 20}, "direction": 1}
        zoo_mod.record(z, spec, {"oos_ls_sharpe": 0.1},
                       panel_end="2026-10-24", hold=5)
        e = z["factors"][zoo_mod._key(spec, 5)]
        self.assertTrue(zoo_mod.reusable(e, "2026-10-24", 5))
        self.assertFalse(zoo_mod.reusable(e, "2026-10-25", 5))
        self.assertFalse(zoo_mod.reusable(e, "2026-10-24", 20))

    def test_legacy_entry_without_algo_is_not_reused(self):
        z = {"factors": {}}
        spec = {"op": "mom", "params": {"n": 20}, "direction": 1}
        zoo_mod.record(z, spec, {"oos_ls_sharpe": 0.0},
                       panel_end="2026-10-24", hold=5)
        e = z["factors"][zoo_mod._key(spec, 5)]
        e.pop("algo")
        self.assertFalse(zoo_mod.reusable(e, "2026-10-24", 5))


if __name__ == "__main__":
    unittest.main()
