"""因子晋升闸门回归测试：可重复性 + 方向一致性 + DSR/PBO + 成本。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import promote as P


def _run(ts, name, spec, oos, dsr=None, pbo=None, net=0.5, lowfreq=None):
    s = {"name": name, "spec": spec, "oos_ls_sharpe": oos, "ls_sharpe": oos,
         "net_ls_sharpe": net, "ic_mean": 0.03, "source": "sweep"}
    if dsr is not None:
        s["dsr"] = {"dsr": dsr, "n_trials": 342}
    if pbo is not None:
        s["pbo_cscv"] = {"pbo": pbo}
    if lowfreq is not None:
        s["lowfreq"] = lowfreq
    return {"generated_at": ts, "n_candidates": 10, "n_survivors": 1,
            "survivors": [s]}


SPEC_X = {"op": "mom", "params": {"n": 120}, "direction": 1}
SPEC_Y = {"op": "vol", "params": {"n": 20}, "direction": 1}

# 2026-11-09：晋升默认值收紧过一次（min_runs 2→5、dsr 0.95→0.999 等），
# 这些用例是逐个判据的隔离测试，必须显式钉住其余闸门的旧值，
# 否则默认值一变就集体连坐（当时挂了 5 个）。
BASE = dict(min_runs=2, dsr_floor=0.95, pbo_ceiling=0.5,
            min_oos_sharpe=0.3, min_abs_icir=0.03, min_abs_ic=0.0)


class TestPromotion(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        P.LIB_FILE = self.d / "lib.json"

    def _write(self, fname, run):
        with open(self.d / fname, "w") as f:
            json.dump(run, f)

    def _run_promote(self, **kw):
        args = {**BASE, **kw}
        return P.promote([str(self.d / "mine_*.json")], verbose=False, **args)

    def test_repeatable_consistent_promoted(self):
        self._write("mine_a.json", _run("2026-01-01", "X", SPEC_X, 0.8, dsr=0.99, pbo=0.2))
        self._write("mine_b.json", _run("2026-01-02", "X", SPEC_X, 0.7, dsr=0.97, pbo=0.3))
        r = self._run_promote()
        by = {v["name"]: v for v in r["verdicts"]}
        self.assertTrue(by["X"]["promoted"])

    def test_direction_flip_rejected(self):
        self._write("mine_a.json", _run("2026-01-01", "Y", SPEC_Y, 0.6, dsr=0.99, pbo=0.2))
        self._write("mine_b.json", _run("2026-01-02", "Y", SPEC_Y, -0.6, dsr=0.99, pbo=0.2))
        r = self._run_promote()
        by = {v["name"]: v for v in r["verdicts"]}
        self.assertFalse(by["Y"]["promoted"])
        self.assertFalse(by["Y"]["checks"]["direction_consistent"])

    def test_single_appearance_not_repeatable(self):
        self._write("mine_a.json", _run("2026-01-01", "X", SPEC_X, 1.5, dsr=0.99, pbo=0.1))
        r = self._run_promote(min_runs=2)
        by = {v["name"]: v for v in r["verdicts"]}
        self.assertFalse(by["X"]["promoted"])
        self.assertFalse(by["X"]["checks"]["repeatable"])

    def test_low_dsr_rejected(self):
        self._write("mine_a.json", _run("2026-01-01", "X", SPEC_X, 0.8, dsr=0.60, pbo=0.2))
        self._write("mine_b.json", _run("2026-01-02", "X", SPEC_X, 0.8, dsr=0.62, pbo=0.2))
        r = self._run_promote()
        by = {v["name"]: v for v in r["verdicts"]}
        self.assertFalse(by["X"]["promoted"])
        self.assertFalse(by["X"]["checks"]["dsr_ok"])

    def test_high_pbo_rejected(self):
        self._write("mine_a.json", _run("2026-01-01", "X", SPEC_X, 0.8, dsr=0.99, pbo=0.7))
        self._write("mine_b.json", _run("2026-01-02", "X", SPEC_X, 0.8, dsr=0.99, pbo=0.65))
        r = self._run_promote()
        by = {v["name"]: v for v in r["verdicts"]}
        self.assertFalse(by["X"]["promoted"])
        self.assertFalse(by["X"]["checks"]["pbo_ok"])

    def test_weak_amplitude_rejected(self):
        self._write("mine_a.json", _run("2026-01-01", "X", SPEC_X, 0.15, dsr=0.99, pbo=0.2))
        self._write("mine_b.json", _run("2026-01-02", "X", SPEC_X, 0.16, dsr=0.99, pbo=0.2))
        r = self._run_promote(min_oos_sharpe=0.3)
        by = {v["name"]: v for v in r["verdicts"]}
        self.assertFalse(by["X"]["promoted"])

    def test_negative_net_cost_rejected(self):
        self._write("mine_a.json", _run("2026-01-01", "X", SPEC_X, 0.8, dsr=0.99, pbo=0.2, net=-0.3))
        self._write("mine_b.json", _run("2026-01-02", "X", SPEC_X, 0.8, dsr=0.99, pbo=0.2, net=-0.2))
        r = self._run_promote()
        by = {v["name"]: v for v in r["verdicts"]}
        self.assertFalse(by["X"]["promoted"])
        self.assertFalse(by["X"]["checks"]["net_cost_ok"])

    def test_library_written(self):
        self._write("mine_a.json", _run("2026-01-01", "X", SPEC_X, 0.8, dsr=0.99, pbo=0.2))
        self._write("mine_b.json", _run("2026-01-02", "X", SPEC_X, 0.8, dsr=0.99, pbo=0.2))
        self._run_promote()
        lib = json.load(open(self.d / "lib.json"))
        self.assertEqual(len(lib["factors"]), 1)


class TestRedundancyGate(unittest.TestCase):
    """冗余门槛：库容量与相关性去重的单元测试（不依赖行情数据）。"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._orig_lib = P.LIB_FILE
        self._orig_corr = P._max_abs_corr_with
        P.LIB_FILE = self.d / "lib.json"

    def tearDown(self):
        P.LIB_FILE = self._orig_lib
        P._max_abs_corr_with = self._orig_corr
        P._FACTOR_CACHE.clear()

    def _stub_corr(self, table):
        """table[(name_a, name_b)] = corr；缺省 0.0。"""
        def f(spec_a, sid_a, spec_b, sid_b, market="us"):
            na, nb = spec_a.get("name"), spec_b.get("name")
            return table.get((na, nb), table.get((nb, na), 0.0))
        P._max_abs_corr_with = f

    def _seed_library(self, items):
        lib = {"factors": {}, "updated_at": None}
        for name, spec, oos in items:
            sid = P._spec_id(spec)
            lib["factors"][sid] = {"name": name, "spec": spec, "oos_median": oos}
        P.LIB_FILE.write_text(json.dumps(lib))

    def test_gate_rejects_highly_correlated_candidate(self):
        spec_v = {"op": "vol", "params": {"n": 20}, "direction": 1, "name": "vol_n20"}
        spec_h = {"op": "hl_range", "params": {"n": 20}, "direction": 1, "name": "hl_n20"}
        self._seed_library([("vol_n20", spec_v, 0.7)])
        self._stub_corr({("hl_n20", "vol_n20"): 0.98})
        lib = json.loads(P.LIB_FILE.read_text())
        cand = {"spec_id": P._spec_id(spec_h), "name": "hl_n20", "spec": spec_h,
                "oos_median": 0.9}
        kept, rej = P._apply_redundancy_gate([cand], lib, 0.8)
        self.assertEqual(kept, [])
        self.assertEqual(len(rej), 1)
        self.assertAlmostEqual(rej[0]["corr"], 0.98)

    def test_gate_keeps_orthogonal_candidate(self):
        spec_v = {"op": "vol", "params": {"n": 20}, "direction": 1, "name": "vol_n20"}
        spec_m = {"op": "mom", "params": {"n": 250}, "direction": 1, "name": "mom_n250"}
        self._seed_library([("vol_n20", spec_v, 0.7)])
        self._stub_corr({("mom_n250", "vol_n20"): 0.05})
        lib = json.loads(P.LIB_FILE.read_text())
        cand = {"spec_id": P._spec_id(spec_m), "name": "mom_n250", "spec": spec_m,
                "oos_median": 0.9}
        kept, rej = P._apply_redundancy_gate([cand], lib, 0.8)
        self.assertEqual([k["name"] for k in kept], ["mom_n250"])
        self.assertEqual(rej, [])

    def test_gate_dedupes_candidates_among_themselves(self):
        """两个新候选彼此高度相关时只留排在前面（更强的）那个。"""
        self._seed_library([])
        a = {"name": "a", "op": "vol", "params": {"n": 10}, "direction": 1}
        b = {"name": "b", "op": "vol", "params": {"n": 11}, "direction": 1}
        self._stub_corr({("a", "b"): 0.95})
        cands = [{"spec_id": P._spec_id(a), "name": "a", "spec": a, "oos_median": 1.0},
                 {"spec_id": P._spec_id(b), "name": "b", "spec": b, "oos_median": 0.5}]
        kept, rej = P._apply_redundancy_gate(cands, {"factors": {}}, 0.8)
        self.assertEqual([k["name"] for k in kept], ["a"])
        self.assertEqual([r["name"] for r in rej], ["b"])

    def test_gate_disabled_when_threshold_nonpositive(self):
        self._seed_library([])
        a = {"name": "a", "op": "vol", "params": {"n": 10}, "direction": 1}
        b = {"name": "b", "op": "vol", "params": {"n": 11}, "direction": 1}
        self._stub_corr({("a", "b"): 0.99})
        cands = [{"spec_id": P._spec_id(a), "name": "a", "spec": a, "oos_median": 1.0},
                 {"spec_id": P._spec_id(b), "name": "b", "spec": b, "oos_median": 0.5}]
        kept, rej = P._apply_redundancy_gate(cands, {"factors": {}}, 0.0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(rej, [])

    def test_corr_unavailable_rejects_conservatively(self):
        """算不出相关时**保守拒绝**（2026-10-18 语义反转）。

        历史：这里原本是 fails_open（算不出就放行）。但 2026-10-16T15:27 首次
        批量晋升时数据面板不可用，13 个 |corr| 0.85~1.00 的波动率变体借此
        一起入库占满容量（名义 20 个，消费端去冗余后只剩 3 个，合成 IC 被
        稀释到 +0.0031）。算不出相关 ≠ 不相关，静默放行是这次的真正原因。

        注意：必须有**已占用**因子才会触发相关性比较。单候选且库为空时
        occupied 为空、根本不会调用 _max_abs_corr_with —— 旧测试正是这样
        才"通过"的（空转，未覆盖目标路径）。
        """
        spec_v = {"op": "vol", "params": {"n": 20}, "direction": 1}
        spec_h = {"op": "hl_range", "params": {"n": 20}, "direction": 1}
        self._seed_library([("vol_n20", spec_v, 0.7)])
        lib = json.loads(P.LIB_FILE.read_text())
        cand = {"spec_id": P._spec_id(spec_h), "name": "hl_n20",
                "spec": spec_h, "oos_median": 0.9}

        orig = P._max_abs_corr_with
        P._max_abs_corr_with = lambda *a, **k: None
        try:
            kept, rej = P._apply_redundancy_gate([cand], lib, 0.8)
        finally:
            P._max_abs_corr_with = orig  # 必须还原，否则污染后续测试

        self.assertEqual(kept, [], "相关不可算时应保守拒绝，不能放行")
        self.assertEqual(len(rej), 1)
        self.assertEqual(rej[0].get("reason"), "corr_unavailable")
        self.assertIsNone(rej[0].get("corr"))

    def test_batch_promote_keeps_library_correlations_below_threshold(self):
        """批量入库后，库内任意两因子 |corr| 必须 <= 阈值。

        锁死 2026-10-16 的事故：13 个同族因子在同一次批量晋升里一起入库。
        这里用真实的相关系数桩模拟「一批强烈相关的波动率变体」，断言闸门
        只留下一个代表，而不是把库填满同类信号。
        """
        self._seed_library([])
        # 一批 8 个波动率族变体，彼此高度相关
        # 注意：_stub_corr 从 spec 里的 "name" 键取名字，所以必须写进 spec
        specs = []
        for n in (5, 10, 15, 20, 30, 40, 60, 120):
            sp = {"op": "vol", "params": {"n": n}, "direction": 1,
                  "name": f"vol_n{n}"}
            specs.append({"spec_id": P._spec_id(sp), "name": f"vol_n{n}",
                          "spec": sp, "oos_median": 0.7 - n / 1000.0})

        # 任意两个变体之间 |corr| = 0.9（超过 0.8 阈值）
        import itertools
        self._stub_corr({(a["name"], b["name"]): 0.9
                         for a, b in itertools.combinations(specs, 2)})

        kept, rej = P._apply_redundancy_gate(specs, {"factors": {}}, 0.8)

        self.assertEqual(len(kept), 1,
                         f"同族批量候选只该留一个代表，实际留了 {len(kept)} 个")
        self.assertEqual(len(rej), len(specs) - 1)
        self.assertEqual(kept[0]["name"], "vol_n5", "应保留最强（排序第一）的那个")


class TestCapacity(unittest.TestCase):
    """库容量护栏：满了之后按强度截断，且日志里的「新晋」数必须是真新增。

    锁死 2026-10-26 的误导：promote.log 每轮都打「本次晋升 17 个，正式库共 18 个」，
    实际 17 个全是库内老因子刷新 last_seen，一个都没新增。巡检读日志的人会
    以为挖掘一直在出因子。
    """

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self._orig_lib = P.LIB_FILE
        self._orig_corr = P._max_abs_corr_with
        P.LIB_FILE = self.d / "lib.json"
        P._max_abs_corr_with = lambda *a, **k: 0.0
        P._FACTOR_CACHE.clear()

    def tearDown(self):
        P.LIB_FILE = self._orig_lib
        P._max_abs_corr_with = self._orig_corr
        P._FACTOR_CACHE.clear()

    def _write(self, fname, run):
        (self.d / fname).write_text(json.dumps(run))

    def test_capacity_keeps_strongest_and_reports_true_new_count(self):
        specs = [{"op": "mom", "params": {"n": n}, "direction": 1}
                 for n in (40, 60, 80, 100, 120)]
        names = {n: f"mom_n{n}_x" for n in (40, 60, 80, 100, 120)}
        # 故意让强因子的 n 排在后面，验证截断前有排序
        for i, sp in enumerate(specs):
            n = sp["params"]["n"]
            self._write(f"mine_{i}.json",
                        _run(f"2026-01-0{i + 1}", names[n], sp,
                             0.3 + n / 500.0, dsr=0.99, pbo=0.2))
        for i, sp in enumerate(specs):
            n = sp["params"]["n"]
            self._write(f"mine_b{i}.json",
                        _run(f"2026-02-0{i + 1}", names[n], sp,
                             0.3 + n / 500.0, dsr=0.99, pbo=0.2))

        # 容量 2：应该留下最强的 mom_n120 / mom_n100，不是遍历顺序的第一批
        r = P.promote([str(self.d / "mine_*.json")], verbose=False, max_library=2,
                      **BASE)
        lib = json.loads(P.LIB_FILE.read_text())
        kept = sorted(v["name"] for v in lib["factors"].values())
        self.assertEqual(kept, sorted([names[100], names[120]]),
                         f"容量截断应按 |oos_median| 保留最强的，实际 {kept}")
        self.assertEqual(r["n_newly_promoted"], 2)
        self.assertEqual(r["n_library"], 2)

        # 再跑一次：库已满且无更强候选 → 新晋必须是 0，不能重复报 2
        r2 = P.promote([str(self.d / "mine_*.json")], verbose=False, max_library=2,
                       **BASE)
        self.assertEqual(r2["n_newly_promoted"], 0,
                         f"老因子刷新不应计入新晋，实际 {r2['n_newly_promoted']}")
        self.assertEqual(r2["n_library"], 2)


if __name__ == "__main__":
    unittest.main()
