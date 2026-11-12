"""并行 LLM 因子提案器：用高并发一次生成大量候选。

利用 9router free 的高并发能力（实测 10 并发线性加速）。
每一路给不同的"创意种子"（不同经济逻辑方向），避免重复。
"""
from __future__ import annotations
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from factors.engine import OP_DOC
from llm.client import chat, extract_json

# LLM 瞬时并发硬上限，避免调用方传入过大的 concurrency。
MAX_LLM_CONCURRENCY = max(1, int(os.environ.get("QUANT_MAX_LLM_CONCURRENCY", "1")))


SYSTEM = """你是顶尖量化研究员，专精横截面选股因子。任务是提出有经济学逻辑支撑的候选因子。

硬性要求：
1. 只使用给定算子，参数必须是整数，落在合理范围。
2. 每个因子必须有清晰经济学逻辑（为什么它能预测未来收益）。
3. direction=1 表示因子值越大越看多，-1 表示越看空。
4. 只输出 JSON 数组，无其他文字：
[{"name":"英文名","op":"算子名","params":{"n":20},"direction":1,"rationale":"一句话逻辑"}]
5. 提出 {n} 个，尽量多样，避免参数雷同。"""

# 创意种子：每路从不同角度切入
SEEDS = [
    "从行为金融学角度（过度反应、处置效应、锚定、羊群效应）",
    "从流动性风险角度（买卖价差、冲击成本、资金流）",
    "从波动率结构角度（波动率聚集、偏度、尾部风险）",
    "从量价背离角度（价格与成交量的背离关系）",
    "从趋势与反转的期限结构角度（不同时间尺度的冲突）",
    "从微观结构角度（隔夜跳空、日内动量、VWAP偏离）",
    "从市场微观情绪角度（极端情绪后的均值回复）",
    "从风险调整收益角度（夏普类、Sortino类信号）",
    "从价格形态角度（区间位置、回撤、突破）",
    "从相对强弱角度（跨时间窗口的相对表现）",
    "从成交活跃度变化角度（量能异动、换手突变）",
    "从波动率风险溢价角度（隐含vs实现的代理）",
]


# 当前批次的持有期。用模块级上下文而不是给 _one_call 加参数：
# 测试里 monkeypatch 的 _one_call 固定按 5 个位置参数签名，多传一个会 TypeError。
# 这样既保持调用约定不变，又能把持有期信息带进提示词。
_CALL_HOLD = 1


def _one_call(seed: str, n: int, tried_names: list[str], temperature: float,
              avoid: str = "") -> list[dict]:
    # 持有期提示：窗口长度要与持有期匹配，否则在错的尺度上找信号。
    # hold=1 不加提示，保持与历史提案完全一致的行为（避免影响已验证的产出率）。
    hold = _CALL_HOLD
    hold_hint = ""
    if hold and hold > 1:
        lo, hi = max(3, hold * 2), max(10, hold * 12)
        hold_hint = (f"\n\n【持有期约束】本轮是 hold={hold} 日持有（不是隔夜）。"
                     f"窗口长度 n 必须与该尺度匹配：建议 n 落在 {lo}~{hi} 之间。"
                     f"n<{lo} 的短窗在 {hold} 日持有下基本是噪声，不要提。")
    user = f"""{OP_DOC}

【本次切入角度】{seed}{hold_hint}

已提过（避免重复）：{tried_names[-60:] if tried_names else '无'}{avoid}

请提出 {n} 个因子，只输出 JSON 数组。"""
    try:
        raw = chat([{"role": "system", "content": SYSTEM.replace("{n}", str(n))},
                    {"role": "user", "content": user}], temperature=temperature,
                   retries=1)
        data = extract_json(raw)
        if isinstance(data, dict):
            data = [data]
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _normalize(spec: dict) -> dict | None:
    if not isinstance(spec, dict) or "op" not in spec:
        return None
    # op 必须是字符串
    if not isinstance(spec["op"], str) or not spec["op"].strip():
        return None
    params = dict(spec.get("params", {}) or {})
    for k, v in spec.items():
        if k in ("op", "name", "direction", "rationale", "params", "desc", "reason"):
            continue
        # 只吸收标量参数；列表/字典等畸形值直接跳过，避免污染 params
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            params[k] = int(v)
    # 清理 params：只保留标量，删除 list/dict/None（LLM 可能返回畸形结构）
    params = {k: v for k, v in params.items()
              if isinstance(v, (int, float, str)) and not isinstance(v, bool)}
    for alias in ("window", "lookback", "period"):
        if alias in params and "n" not in params:
            params["n"] = params.pop(alias)
    # direction 容错：LLM 可能给 list/bool/字符串，统一成 ±1
    d = spec.get("direction", 1)
    if isinstance(d, (list, tuple)):
        d = d[0] if d else 1
    try:
        d = float(d)
        direction = 1 if d >= 0 else -1
    except (TypeError, ValueError):
        direction = 1
    return {
        "name": str(spec.get("name", spec["op"]))[:40],
        "op": spec["op"], "params": params,
        "direction": direction,
        "rationale": str(spec.get("rationale", spec.get("desc", "")))[:200],
    }


def _tried_key(spec: dict) -> str:
    """去重键：(op, params)，**忽略 direction**。

    与 core/mine.py 的 FDR 归并口径 _hyp_key 保持一致：正反方向属于同一个
    因子假设，只应检验一次。params 做归一化再比较，避免 20 / 20.0 / "20"
    被当成不同参数而重复试验同一假设。
    """
    from factors.engine import OP_DOC  # noqa: F401  (确保算子表已加载)
    raw = spec.get("params", {}) or {}
    params = {}
    for k, v in raw.items():
        try:
            if isinstance(v, str):
                v = float(v) if "." in v else int(v)
            if isinstance(v, float) and v.is_integer():
                v = int(v)
        except (TypeError, ValueError):
            pass
        params[str(k)] = v
    return json.dumps({"op": spec.get("op"), "params": params}, sort_keys=True)


def _library_hint() -> str:
    """把「现有因子库」写进提示词。

    为什么需要：库里的 11 个因子由 vol / mom / turnover 三族占了绝大多数，
    而提案端此前只知道自己这批提过什么（tried_names），完全看不见**已经晋升
    的库**——于是每轮都在同一片地里反复翻土，挖出来的自然是同族变体。

    这里显式给出族分布 + 已入库名单，并要求新提案优先走未见过的算子族。
    不改任何判定逻辑，纯提示词增强；LLM 不遵守也只是回到原状，不会更差。
    """
    try:
        import json as _json
        from collections import Counter
        from pathlib import Path

        lib = _json.loads(Path("/opt/quant-lab/data/factor_library.json").read_text())
        facs = list((lib.get("factors") or {}).values())
        if not facs:
            return ""
        ops = Counter(((f.get("spec") or {}).get("op") or "?") for f in facs)
        fam = "、".join(f"{o}×{c}" for o, c in ops.most_common(6))
        names = "、".join(str(f.get("name")) for f in facs[:20])
        return ("\n\n【现有因子库（这些已经晋升在跑，不要重复提同类）】\n"
                f"族分布：{fam}\n"
                f"已入库：{names}\n"
                "要求：1) 优先提出上面**没出现过的算子族**；"
                "2) 若必须用已有族，必须给出正交理由（不同窗口 / 不同方向 / 不同经济机制），"
                "且同族提案不超过本批的 1/4；"
                "3) 已经被剪掉的弱族（如纯 volume_price_corr、窄窗口 turnover_chg）不要再堆。")
    except Exception:  # noqa: BLE001
        return ""


def propose_batch(total: int = 120, concurrency: int = 12, per_call: int = 6,
                  tried: list[dict] | None = None, temperature: float = 0.9,
                  verbose: bool = True, hold: int = 1) -> list[dict]:
    """并行生成 total 个候选（去重）。

    hold：本轮的持有期。窗口长度 n 必须与持有期匹配 —— 持有 20 天却用
    n=3 的噪声窗口，或在 hold=1 下用 n=250 的长窗口，都是在错的尺度上找信号。
    历史结果里 99% 是 hold=1，所以换持有期本身就是一条新的搜索维度。
    """
    concurrency = max(1, min(int(concurrency), MAX_LLM_CONCURRENCY))
    tried_names = [t.get("name", "") for t in (tried or [])]
    n_calls = max(1, total // per_call)
    seeds = [SEEDS[i % len(SEEDS)] for i in range(n_calls)]

    # 参数多样性引导：历史 n 值高度集中在 5/10/20/60/120，LLM 也偏好这些整数档，
    # 导致新提案几乎必然与历史撞库、被去重吃掉。这里把已用 n 值显式告知模型，
    # 并给出未被探索的区间，迫使它往空白参数空间走。
    used_n: set[int] = set()
    for t in (tried or []):
        raw = (t.get("params") or {}).get("n")
        try:
            if raw is not None:
                used_n.add(int(float(raw)))
        except (TypeError, ValueError):
            continue
    avoid = ""
    if used_n:
        used_list = sorted(used_n)
        show = ", ".join(str(x) for x in used_list[:24])
        avoid = (f"\n\n【参数避让】以下窗口长度 n 已被穷尽，**不要再用**：{show}\n"
                 f"请改用未出现过的整数窗口（例如 7,13,18,23,28,33,45,55,75,90,100,150,180 等，"
                 f"或按你的逻辑另选），确保 n 不在上述列表内。")
    avoid += _library_hint()

    global _CALL_HOLD
    _CALL_HOLD = hold

    results: list[dict] = []
    if verbose:
        print(f"  [批量提案] {n_calls} 路并发 × {per_call} 个 = 目标 {n_calls*per_call}"
              f"（避让 {len(used_n)} 个已用 n 值）", flush=True)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_one_call, s, per_call, tried_names, temperature, avoid): i
                for i, s in enumerate(seeds)}
        for f in as_completed(futs):
            results.extend(f.result() or [])

    # 规范化 + 去重
    seen = set()
    out = []
    for spec in results:
        try:
            norm = _normalize(spec)
        except Exception:
            continue  # 单个畸形候选不应中断整批
        if not norm:
            continue
        key = json.dumps({k: norm[k] for k in ("op", "params", "direction")},
                         sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)

    # 去掉和已尝试重复的。
    # 注意：这里刻意用 (op, params) 忽略 direction 作为去重口径，与 FDR 阶段
    # _hyp_key 的归并口径保持一致 —— 正反方向是同一因子假设的两个表现，
    # 不该因为"正向试过"就把"反向"也算作新发现，也避免下游再算一次同一个假设。
    if tried:
        tried_keys = {_tried_key(t) for t in tried}
        out = [o for o in out if _tried_key(o) not in tried_keys]
    if verbose:
        print(f"  [批量提案] 生成 {len(out)} 个唯一候选", flush=True)
    return out
