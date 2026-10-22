"""防过拟合元层的回归测试：DSR / PBO / 试验登记册。

关键：纯噪声应被判定为"过拟合/不显著"，真信号应通过。这是该层存在的意义。
"""
from __future__ import annotations

import unittest

import numpy as np

from core.overfit_guard import (deflated_sharpe, pbo_cscv, overfit_verdict,
                                 _norm_ppf, _norm_cdf)


def _signal(rng, T=500, N=50, alpha=0.0015):
    x = rng.normal(0, 0.01, size=(T, N))
    x[:, 0] += alpha
    return x


class TestNormFuncs(unittest.TestCase):
    def test_ppf_cdf_roundtrip(self):
        for p in (0.01, 0.1, 0.5, 0.9, 0.99):
            self.assertAlmostEqual(_norm_cdf(_norm_ppf(p)), p, places=3)

    def test_ppf_known(self):
        self.assertAlmostEqual(_norm_ppf(0.975), 1.959964, places=3)
        self.assertAlmostEqual(_norm_ppf(0.5), 0.0, places=6)


class TestDeflatedSharpe(unittest.TestCase):
    def test_real_signal_high_dsr(self):
        rng = np.random.default_rng(1)
        x = _signal(rng)[:, 0]
        d = deflated_sharpe(x, n_trials=50)
        self.assertGreater(d["dsr"], 0.95)

    def test_noise_low_dsr(self):
        rng = np.random.default_rng(2)
        x = rng.normal(0, 0.01, size=300)
        d = deflated_sharpe(x, n_trials=1000)
        self.assertLess(d["dsr"], 0.95)

    def test_more_trials_lowers_dsr(self):
        # 同一收益，试验数越多，DSR 越低（惩罚越重）——但用 benchmark 才体现
        rng = np.random.default_rng(3)
        x = rng.normal(0.0008, 0.01, size=400)
        d10 = deflated_sharpe(x, 10, sr_benchmark=1.0)
        d1000 = deflated_sharpe(x, 1000, sr_benchmark=1.0)
        self.assertGreaterEqual(d10["dsr"], d1000["dsr"])

    def test_insufficient_data(self):
        d = deflated_sharpe([0.01, 0.02], 10)
        self.assertEqual(d["n_obs"], 2)
        self.assertEqual(d["dsr"], 0.0)


class TestPBO(unittest.TestCase):
    def test_noise_high_pbo(self):
        rng = np.random.default_rng(10)
        m = rng.normal(0, 0.01, size=(400, 40))
        r = pbo_cscv(m, n_splits=8)
        self.assertGreater(r["pbo"], 0.5)

    def test_signal_low_pbo(self):
        rng = np.random.default_rng(11)
        m = _signal(rng, T=500, N=40)
        r = pbo_cscv(m, n_splits=8)
        self.assertLess(r["pbo"], 0.5)

    def test_insufficient(self):
        r = pbo_cscv(np.zeros((4, 2)), n_splits=8)
        self.assertIsNone(r["pbo"])
        self.assertIn("error", r)


class TestVerdict(unittest.TestCase):
    def test_real_signal_passes(self):
        rng = np.random.default_rng(20)
        m = _signal(rng, T=500, N=40)
        v = overfit_verdict(m[:, 0], n_trials=50, perf_matrix=m)
        self.assertTrue(v["pass"])

    def test_noise_fails(self):
        rng = np.random.default_rng(21)
        m = rng.normal(0, 0.01, size=(500, 40))
        v = overfit_verdict(m[:, 0], n_trials=5000, perf_matrix=m)
        self.assertFalse(v["pass"])


if __name__ == "__main__":
    unittest.main()
