"""拉取全 universe 长历史并落盘。"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config
from data.history import build_long_history
from core.data import load_reference

ref = load_reference()
symbols = [s for s, seq in ref["bars"].items() if len(seq) >= 60]
print(f"universe: {len(symbols)} 只票", flush=True)
# 2016 年起：实测 sip feed 能回到 2016-01-04（iex 只能到 2020-07）。
# 长历史是 US 侧能否通过 BH-FDR 的关键——原来 1451 个交易日，
# ic_tstat 最高只有 3.31，p 值下不到 BH 阈值（0.00054）以下，0 幸存。
# 拉到 2016 后约 2700 个交易日，t 统计量按 √N 提升约 1.36 倍。
frames = build_long_history(symbols, start="2016-01-01")
c = frames["c"]
print(f"最终面板: {c.shape[0]} 交易日 x {c.shape[1]} 只票", flush=True)
print(f"日期范围: {c.index.min().date()} -> {c.index.max().date()}", flush=True)
(config.DATA_DIR / "hist" / "meta.json").write_text(
    json.dumps({
        "n_symbols": int(c.shape[1]), "n_days": int(c.shape[0]),
        "start": str(c.index.min().date()), "end": str(c.index.max().date()),
        "feed": config.ALPACA_FEED,
    }, indent=2))
