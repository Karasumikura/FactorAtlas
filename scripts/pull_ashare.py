"""拉取 A 股（指数成分股）长历史面板并落盘。

用法：
  python scripts/pull_ashare.py <index> [start] [workers] [--full]

  --full  强制全量重拉（默认增量：只补缺失/数据过少的票，合并进已有面板）
"""
from __future__ import annotations
import sys, time, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import ashare


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    full = "--full" in sys.argv
    idx = argv[0] if len(argv) > 0 else "000300"
    start = argv[1] if len(argv) > 1 else "2015-01-01"
    workers = int(argv[2]) if len(argv) > 2 else 4

    t0 = time.time()
    codes = ashare.get_universe(idx)
    print(f"[pull_ashare] 指数 {idx} 成分股 {len(codes)} 只，起始 {start}，"
          f"{'全量重拉' if full else '增量补齐'}", flush=True)

    old = None if full else ashare.load_panel(idx)
    if old is not None:
        todo = ashare.missing_codes(old, codes)
        print(f"[pull_ashare] 已有面板 {old['c'].shape}，需补 {len(todo)} 只", flush=True)
    else:
        todo = codes

    frames = ashare.pull_history(todo, start=start, workers=workers) if todo \
        else {f: None for f in ("o", "h", "l", "c", "v", "vw")}
    # 合并
    merged = ashare.merge_panel(old, frames)
    c = merged["c"]
    if c is None or c.empty:
        print("[pull_ashare] 无数据，退出", flush=True)
        return 1
    print(f"[pull_ashare] 合并后面板 {c.shape}，"
          f"{c.index.min().date()} -> {c.index.max().date()}，耗时 {round(time.time()-t0,1)}s",
          flush=True)
    ashare.save_panel(merged, meta={
        "source": f"腾讯后复权(hfq)+东财兜底；指数{idx}当前成分股",
        "index": idx, "start": start,
        "n_symbols": int(c.shape[1]), "n_days": int(c.shape[0]),
        "range": [str(c.index.min().date()), str(c.index.max().date())],
        "bias": "当前成分股回测，存在幸存者偏差/成分调整前视；后复权收益率正确",
    }, index=idx)
    dest = "data/ashare/" if idx in ("000300",) else f"data/ashare_{idx}/"
    print(f"[pull_ashare] 已保存 {dest}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
