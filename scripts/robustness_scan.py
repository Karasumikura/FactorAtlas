"""因子稳健性体检：把一个 spec 放到多个 (持有期 × 训练/测试切分) 组合下重算，
看它是不是只在某一个配置里好。

用途有两个：
  1) 质量：库内因子逐个复核，找出「只在 1 个配置里有效」的假稳健因子；
  2) 算力：这个任务纯粹是评估、不做搜索，不会撑大试验数 N（DSR 惩罚不变），
     所以在挖掘冷却期跑它，等于把空闲的 2 核拿来做真正有用的活。

用法：
    python scripts/robustness_scan.py --market us
    python scripts/robustness_scan.py --market crypto --holds 1 5 --splits 0.5 0.7
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import backtest, data  # noqa: E402
from core.factor_library import library_specs  # noqa: E402


def scan(market: str, holds: list[int], splits: list[float], limit: int | None):
    specs = library_specs(max_factors=100, dedupe_corr=0, market=market)
    if limit:
        specs = specs[:limit]
    print(f"面板: market={market} | 因子 {len(specs)} 个 | "
          f"配置 {len(holds)}×{len(splits)} = {len(holds)*len(splits)} 组", flush=True)

    t0 = time.time()
    panel = data.build_panel(market=market)
    print(f"面板加载完成 {time.time()-t0:.1f}s", flush=True)

    rows = []
    for i, spec in enumerate(specs, 1):
        name = spec.get("name") or f"f{i}"
        recs = []
        for hold in holds:
            for tf in splits:
                t = time.time()
                try:
                    r = backtest.evaluate_with_split(panel, name, spec, hold=hold,
                                                     train_frac=tf)
                    recs.append({"hold": hold, "train_frac": tf,
                                 "ic": r.ic_mean, "oos_ic": r.oos_ic_mean,
                                 "icir": r.icir, "oos_sharpe": r.oos_ls_sharpe,
                                 "net_sharpe": r.net_ls_sharpe, "turnover": r.turnover})
                except Exception as e:  # noqa: BLE001
                    recs.append({"hold": hold, "train_frac": tf, "error": str(e)[:120]})
                print(f"  [{i}/{len(specs)}] {name[:30]:30s} hold={hold:2d} tf={tf:.1f} "
                      f"{time.time()-t:.1f}s", flush=True)

        ok = [x for x in recs if "oos_ic" in x and x["oos_ic"] is not None]
        if ok:
            ics = [x["oos_ic"] for x in ok]
            shs = [x["oos_sharpe"] for x in ok if x.get("oos_sharpe") is not None]
            pos = sum(1 for v in ics if v > 0)
            rows.append({
                "name": name, "n_cfg": len(ok),
                "oos_ic_median": round(st.median(ics), 4),
                "oos_ic_min": round(min(ics), 4),
                "oos_ic_max": round(max(ics), 4),
                "sign_consistency": round(pos / len(ics), 3),
                "sharpe_median": round(st.median(shs), 3) if shs else None,
                "configs": recs,
            })
        else:
            rows.append({"name": name, "n_cfg": 0, "error": "全部配置失败",
                         "configs": recs})

    rows.sort(key=lambda x: (x.get("sign_consistency", 0), x.get("oos_ic_median") or -9),
              reverse=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="us")
    ap.add_argument("--holds", type=int, nargs="+", default=[1, 5, 20])
    ap.add_argument("--splits", type=float, nargs="+", default=[0.5, 0.7])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    rows = scan(args.market, args.holds, args.splits, args.limit)

    print("\n=== 稳健性排名（符号一致率优先）===")
    print(f"{'因子':34s} {'配置':>4s} {'IC中位':>8s} {'IC最小':>8s} {'符号一致':>7s} {'Sharpe中位':>9s}")
    for r in rows:
        if r["n_cfg"] == 0:
            print(f"{r['name'][:34]:34s}   失败 {r.get('error')}")
            continue
        print(f"{r['name'][:34]:34s} {r['n_cfg']:4d} {r['oos_ic_median']:+8.4f} "
              f"{r['oos_ic_min']:+8.4f} {r['sign_consistency']:7.2f} "
              f"{str(r['sharpe_median']):>9s}")

    out = Path(args.out) if args.out else \
        Path("runs") / f"robust_{args.market}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"market": args.market, "holds": args.holds, "splits": args.splits,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "factors": rows},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n已写入 {out}")

    # 判据：符号一致率 < 0.8 或 IC 中位 < 0.01 → 建议复核
    weak = [r for r in rows if r["n_cfg"] and
            (r["sign_consistency"] < 0.8 or r["oos_ic_median"] < 0.01)]
    print(f"\n建议复核（只在部分配置有效）: {len(weak)} 个")
    for r in weak:
        print(f"  {r['name'][:34]:34s} 一致率={r['sign_consistency']:.2f} "
              f"IC中位={r['oos_ic_median']:+.4f} IC最小={r['oos_ic_min']:+.4f}")


if __name__ == "__main__":
    main()
