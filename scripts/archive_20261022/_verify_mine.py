"""用真实 mine() 流程对比：全量候选的回测结果逐条对拍 + 计时。

做法：把 zoo（缓存）临时指向空文件，用 --no-cache 全量回测一批 sweep 候选，
分别用「改动前的原始实现」和「改动后」跑，比较每条记录的数值字段。
"""
import sys, os, json, time, tempfile
sys.path.insert(0, "/opt/quant-lab")
import numpy as np

# 记录是否用旧实现：通过环境变量切换不方便，改为直接 monkeypatch
MODE = os.environ.get("BT_MODE", "new")
if MODE == "old":
    # 用备份的原始实现替换
    import core.backtest_fast as bf
    import pandas as pd
    from core.backtest_fast import _prep

    def _old_layered(factor, fwd, n_groups=5, mask=None, cost_bps=5.0):
        f, r = _prep(factor, fwd, mask)
        fr = f.rank(axis=1, method="first")
        n = f.notna().sum(axis=1)
        valid = n >= n_groups * 4
        q = fr.div(n, axis=0)
        group = np.floor(q * n_groups).clip(upper=n_groups - 1)
        top = group == (n_groups - 1); bot = group == 0
        long_ret = (r.where(top).mean(axis=1)); short_ret = (r.where(bot).mean(axis=1))
        gross = (long_ret - short_ret).where(valid)
        top_idx = {d: set(top.columns[top.loc[d].fillna(False)]) for d in f.index}
        bot_idx = {d: set(bot.columns[bot.loc[d].fillna(False)]) for d in f.index}
        prev_top = prev_bot = None; turns = {}
        for d in f.index:
            if prev_top is not None:
                tl, bl = top_idx[d], bot_idx[d]
                denom = 2 * max(len(tl), 1)
                turns[d] = (len(tl ^ prev_top) + len(bl ^ prev_bot)) / denom
            prev_top, prev_bot = top_idx[d], bot_idx[d]
        turnover = pd.Series(turns).reindex(f.index)
        cost = cost_bps / 10000.0
        return gross.dropna(), (gross - turnover * cost * 2).dropna(), long_ret.dropna(), float(turnover.mean())

    bf.layered_ls_fast = _old_layered
    import core.backtest as bt
    bt.layered_ls_fast = _old_layered
    # 还原 OOS 中性化旧行为
    def _eval_with_split_old(panel, name, spec, hold=1, train_frac=0.7,
                             use_mask=True, neutralize_size=False):
        from factors.engine import factor_series_to_frame
        from factors.exposure import tradable_mask, size_proxy
        from core.backtest_fast import rank_ic_fast
        train_dates, test_dates = bt.split_periods(panel, train_frac)
        res = bt.evaluate_factor(panel, name, spec, hold=hold, use_mask=use_mask,
                                 neutralize_size=neutralize_size)
        factor = factor_series_to_frame(panel, spec).loc[test_dates]
        fwd = bt.forward_returns(panel, hold=hold).loc[test_dates]
        mask = tradable_mask(panel).loc[test_dates] if use_mask else None
        if neutralize_size:
            factor = bt._neutralize_fast(factor, size_proxy(panel).loc[test_dates])
        ic = rank_ic_fast(factor, fwd, mask)
        gross, _, _, _ = _old_layered(factor, fwd, mask=mask)
        if len(ic):
            res.oos_ic_mean = float(ic.mean())
        if len(gross):
            res.oos_ls_sharpe = bt._sharpe(gross)
        return res
    bt.evaluate_with_split = _eval_with_split_old

from core import data, mine, zoo as zoo_mod

panel = data.build_panel()
tmp = tempfile.mkdtemp()
zoo_path = os.path.join(tmp, "zoo.json")
# 指向临时文件，绝不污染真实 data/factor_zoo.json
zoo_mod.ZOO_FILE = type(zoo_mod.ZOO_FILE)(zoo_path)

t0 = time.perf_counter()
out = mine.mine(panel, llm_total=0, do_sweep=True, do_classic=False,
                llm_composite=0, neutralize_size=True, market="us",
                use_cache=False, out_json=None, verbose=False)
elapsed = time.perf_counter() - t0

recs = sorted(out["all_results"], key=lambda r: (str(r.get("op")), json.dumps(r.get("params"), sort_keys=True), r.get("direction", 1)))
payload = [{
    "name": r["name"], "op": r.get("op"), "params": r.get("params"), "dir": r.get("direction"),
    **{k: r.get(k) for k in ("ic_mean", "icir", "ic_tstat", "ls_sharpe", "net_ls_sharpe",
                             "ls_return", "turnover", "n_days", "p_value",
                             "oos_ic_mean", "oos_ls_sharpe")},
} for r in recs]
path = f"/tmp/bt_{MODE}.json"
json.dump({"n": len(payload), "elapsed": elapsed, "records": payload}, open(path, "w"), default=str)
print(f"MODE={MODE} 候选={len(payload)} 耗时={elapsed:.1f}s -> {path}")
