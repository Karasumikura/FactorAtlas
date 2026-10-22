"""因子库消费器 —— 把晋升闸门产出的正式因子，合成一个可交易的综合信号。

为什么需要：晋升闸门（core.promote）只是"入库"，不接入交易就毫无价值。
本模块把 `data/factor_library.json` 里的**正交且稳健**因子，等权合成为
一个横截面综合信号，供 `compute_targets` 使用。

为什么用「等权 + 横截面 rank」而不是加权/IC 加权：
  - 等权是最稳的（不引入拟合参数 —— 加权本身就是过拟合通道）。
  - 横截面 rank 对不同量纲的因子做了天然归一，且抗离群。
  - 库里的因子已通过 DSR/PBO/跨会话可重复，合起来比单因子更不容易失效。

  2026-10 补充（实测否定了一次加权重试）：
    曾试过按 n_sessions 加权（w=n/(n+4)），8 因子等权 ICIR=+0.0361 →
    加权 ICIR=+0.0520，看起来改善 +44%。但追查 n_sessions 的来源发现它
    不可靠：mom_n250_skip20 的 46 次出现里 OOS 只有 5 种值（0.4674 重复
    35 次），本质上是「同一结果在不同输出文件里被抄了很多遍」，
    mine 结果里也没有记录数据窗口，无法证明各次用的是不同样本。
    所以加权建立在一个失效指标上，已回退。等权保持不动。
    真正的修法轮不到这一层 —— 要先在 mine 结果里记录数据窗口并对
    「同一 OOS 值」去重，n_sessions 变成真实独立验证次数后才有讨论加权的意义。

用法：
  python -m core.factor_library                      # 打印库里因子 + 合成 IC
  from core.factor_library import composite_factor   # 交易端调用
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

PROJ = Path(__file__).resolve().parent.parent
LIB_FILE = PROJ / "data" / "factor_library.json"


def load_library() -> dict:
    if LIB_FILE.exists():
        try:
            return json.loads(LIB_FILE.read_text())
        except Exception:
            pass
    return {"factors": {}}


def library_specs(max_factors: int = 20, dedupe_corr: float = 0.8,
                  market: str = "us") -> list[dict]:
    """返回库里按 |OOS 中位数| 排序的 spec 列表（去冗余后）。

    dedupe_corr > 0 时做贪心去冗余：按强度从高到低，保留与已选因子
    截面 rank 相关 <= dedupe_corr 的，其余丢弃。这是**消费端兜底**——
    即便库里历史遗留了高冗余因子（例如上线去冗余门槛之前入库的那批），
    合成信号也不会被同一信号重复投票稀释。算不出相关时放行（不误伤）。
    """
    lib = load_library()
    items = list(lib.get("factors", {}).values())
    items.sort(key=lambda x: -abs(x.get("oos_median", 0) or 0))
    specs = [it["spec"] for it in items[:max_factors] if it.get("spec")]
    if dedupe_corr and dedupe_corr > 0 and len(specs) > 1:
        try:
            specs = _dedupe_specs(specs, dedupe_corr, market)
        except Exception:
            pass
    return specs


def _dedupe_specs(specs: list[dict], max_abs_corr: float, market: str) -> list[dict]:
    import pandas as pd
    from core import data
    from factors.engine import factor_series_to_frame
    panel = data.build_panel(market=market)
    ranked = []
    for sp in specs:
        try:
            f = factor_series_to_frame(panel, sp)
            ranked.append(None if f is None or f.empty else f.rank(axis=1, pct=True).stack())
        except Exception:
            ranked.append(None)
    keep_idx: list[int] = []
    for i, si in enumerate(ranked):
        if si is None:
            keep_idx.append(i)          # 算不出的保留，由下游 try/except 兜底
            continue
        ok = True
        for j in keep_idx:
            sj = ranked[j]
            if sj is None:
                continue
            j2 = pd.concat([si.rename("a"), sj.rename("b")], axis=1).dropna()
            if len(j2) < 100:
                continue
            c = j2["a"].corr(j2["b"])
            if c == c and abs(float(c)) > max_abs_corr:
                ok = False
                break
        if ok:
            keep_idx.append(i)
    return [specs[i] for i in sorted(keep_idx)]


def composite_factor(panel: dict, specs: list[dict] | None = None,
                     max_factors: int = 20, min_factors: int = 1) -> pd.DataFrame | None:
    """把库里因子合成为横截面综合信号（每日截面 rank 均值）。

    返回形状与 panel["c"] 相同的 DataFrame；库为空时返回 None（交易端应回退单因子）。
    每个因子先取截面百分位 rank（0..1），再对所有因子等权平均；
    对「方向」已在 spec.direction 内生效（factor_series_to_frame 已含方向）。
    """
    from factors.engine import factor_series_to_frame
    specs = specs if specs is not None else library_specs(max_factors)
    if len(specs) < min_factors:
        return None
    frames = []
    for sp in specs:
        try:
            f = factor_series_to_frame(panel, sp)
            if f is None or f.empty:
                continue
            # 横截面百分位（每日），NaN 保持 NaN
            r = f.rank(axis=1, pct=True)
            frames.append(r)
        except Exception:
            continue
    if not frames:
        return None
    # 对齐到共同 (日期) 索引
    idx = panel["c"].index
    aligned = [fr.reindex(index=idx) for fr in frames]
    # 列取并集后逐日求均值（NaN 安全）
    stack = pd.concat(aligned, axis=0).groupby(level=0)
    comp = stack.mean()
    return comp


def composite_as_spec(panel: dict, specs: list[dict] | None = None,
                      max_factors: int = 20) -> dict | None:
    """把合成信号包装成一个可喂给 compute_targets 的「伪 spec」。

    compute_targets 走 factor_series_to_frame(panel, spec)。为了让合成信号
    也能走同一通道，这里注册一个动态 op。但避免污染全局 OPS，改为返回
    {kind:'composite', specs:[...]}，由交易端识别。
    """
    lib = library_specs(max_factors)
    if not lib:
        return None
    return {"kind": "composite", "specs": lib, "name": f"composite_lib_{len(lib)}"}


def _report() -> None:
    import warnings
    warnings.filterwarnings("ignore")
    from core import data
    specs = library_specs()
    lib = load_library()
    print(f"因子库: {len(lib.get('factors', {}))} 个正式因子")
    print(f"累计试验 N={lib.get('cumulative_trials')} "
          f"候选见过={lib.get('n_candidates_seen')}")
    if not specs:
        print("\n库为空 —— 尚未有因子通过晋升闸门。")
        print("这是正常的：闸门很严，避免了过拟合。继续跑挖掘积累可重复性证据。")
        return
    print("\n库内因子（按会话数降序 = 可信度降序）:")
    for it in sorted(lib["factors"].values(),
                     key=lambda x: -(x.get("n_sessions", 0) or 0))[:15]:
        n = it.get("n_sessions") or 0
        raw = it.get("oos_median") or 0
        print(f"  {str(it.get('name'))[:28]:28s} 会话={n:>2d} "
              f"OOS中位={raw:+.2f} DSR={it.get('dsr_best')} PBO={it.get('pbo_best')}")
    # 合成信号 IC
    try:
        p = data.build_panel(market="us")
        comp = composite_factor(p, specs)
        if comp is not None:
            from core.backtest import forward_returns
            from core.backtest_fast import rank_ic_fast
            fwd = forward_returns(p, 1)
            ic = rank_ic_fast(comp, fwd)
            print(f"\n合成信号 IC 均值={float(ic.mean()):+.4f} "
                  f"ICIR={float(ic.mean()/ic.std()):+.2f} "
                  f"（{len(specs)} 个因子等权）")
    except Exception as e:  # noqa: BLE001
        print(f"\n合成 IC 计算跳过: {e}")


if __name__ == "__main__":
    _report()
