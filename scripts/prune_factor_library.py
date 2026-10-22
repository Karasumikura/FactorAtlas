#!/usr/bin/env python
"""一次性治理：清理因子库内的波动率族冗余。

背景：2026-10-16T15:27 首次批量晋升时，_max_abs_corr_with 因数据面板不可用
返回 None（"算不出相关就放行"），13 个高度相关的波动率变体一起入库并占满
容量。消费端 library_specs(dedupe_corr=0.8) 虽然能兜底只剩 3 个，但库里
名义 20 个 → 有效权重被单一信号垄断。

本脚本按「强度优先 + 候选间互斥」重建库，与晋升闸门口径一致：
  1. 按 |oos_median| 从高到低排序
  2. 依次尝试保留，与已保留因子两两 |corr| <= MAX_CORR 才留
  3. 算不出相关时**不放行**（conservative：宁可少留，也不让冗余混进来）

用法：
  python scripts/prune_factor_library.py --dry-run   # 只看会删什么
  python scripts/prune_factor_library.py             # 真正执行（先自动备份）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

LIB_FILE = PROJ / "data" / "factor_library.json"
MAX_CORR = 0.8


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-corr", type=float, default=MAX_CORR)
    ap.add_argument("--market", default="us")
    args = ap.parse_args()

    from core import promote as P

    lib = json.loads(LIB_FILE.read_text())
    factors = lib.get("factors") or {}
    if not factors:
        print("库为空，无需处理")
        return 0

    items = sorted(factors.items(),
                   key=lambda kv: -abs(kv[1].get("oos_median", 0) or 0))
    print(f"库内共 {len(items)} 个因子，按 |oos_median| 降序尝试保留"
          f"（阈值 |corr| <= {args.max_corr}）\n")

    kept: list[tuple[str, dict]] = []
    dropped: list[tuple[str, dict, float | None, str]] = []
    unknown: list[tuple[str, dict, str]] = []

    for sid, val in items:
        spec = val.get("spec")
        name = val.get("name") or sid[:40]
        if not spec:
            dropped.append((sid, val, None, "无 spec"))
            continue
        worst, worst_name, corrable = 0.0, None, True
        for ksid, kval in kept:
            c = P._max_abs_corr_with(spec, sid, kval["spec"], ksid,
                                     market=args.market)
            if c is None:
                corrable = False
                worst, worst_name = 999.0, kval.get("name") or ksid[:30]
                break
            if c > worst:
                worst, worst_name = c, kval.get("name") or ksid[:30]
        if not corrable:
            # 算不出相关 → 保守拒绝（与"静默放行"相反）
            unknown.append((sid, val, str(worst_name)))
            dropped.append((sid, val, None, f"相关不可算 vs {worst_name}"))
            continue
        if worst > args.max_corr:
            dropped.append((sid, val, worst, str(worst_name)))
            continue
        kept.append((sid, val))

    print("保留：")
    for sid, val in kept:
        print(f"  ✅ {val.get('name') or sid[:38]:42s} "
              f"oos={val.get('oos_median', 0):+.3f}")

    print(f"\n移除 {len(dropped)} 个：")
    for sid, val, corr, why in dropped:
        tag = f"|corr|={corr:.3f} vs {why}" if corr is not None else why
        print(f"  ❌ {val.get('name') or sid[:38]:42s} {tag}")

    if unknown:
        print(f"\n⚠️  {len(unknown)} 个因子因相关不可算被保守移除"
              f"（原逻辑会静默放行，这里故意收紧）")

    # 校验：保留集两两都不超阈值
    if len(kept) > 1:
        print("\n保留集两两相关性校验：")
        mx = 0.0
        for i in range(len(kept)):
            for j in range(i + 1, len(kept)):
                c = P._max_abs_corr_with(kept[i][1]["spec"], kept[i][0],
                                         kept[j][1]["spec"], kept[j][0],
                                         market=args.market)
                if c is not None and c > mx:
                    mx = c
        print(f"  最大 |corr| = {mx:.4f} {'✅' if mx <= args.max_corr else '❌ 超标'}")

    if args.dry_run:
        print("\n[dry-run] 未写入。去掉 --dry-run 执行。")
        return 0

    backup = LIB_FILE.with_suffix(
        f".json.bak-prune-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(LIB_FILE, backup)
    print(f"\n已备份 → {backup.name}")

    lib["factors"] = {sid: val for sid, val in kept}
    lib["updated_at"] = datetime.now(timezone.utc).isoformat()
    gate = lib.setdefault("redundancy_gate", {})
    gate["pruned_at"] = datetime.now(timezone.utc).isoformat()
    gate["pruned_removed"] = len(dropped)
    gate["pruned_kept"] = len(kept)
    LIB_FILE.write_text(json.dumps(lib, ensure_ascii=False, indent=2))
    print(f"已写入：库从 {len(items)} → {len(kept)} 个因子")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
