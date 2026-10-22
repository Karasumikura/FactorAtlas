"""路线1：LLM 当研究员助手 —— 生成因子假设。

LLM 的任务：基于量化金融知识 + 给定的算子集合，提出候选因子。
LLM 只输出 JSON spec（算子+参数+方向），程序化转成因子，绝不写裸代码。
"""
from __future__ import annotations
import json
from factors.engine import OP_DOC
from llm.client import chat, extract_json

SYSTEM = """你是一位量化研究员，专精横截面选股因子。

你的任务：提出有经济学逻辑支撑的候选因子。每个因子用给定的算子组合表达。

硬性要求：
1. 只使用给定的算子，参数必须是整数。
2. 每个因子必须有清晰的经济学逻辑（为什么它能预测未来收益）。
3. direction=1 表示因子值越大越看多，-1 表示值越大越看空。
4. 避免和已尝试的因子重复。
5. 只输出 JSON 数组，格式：
[
  {"name": "简短英文名", "op": "算子名", "params": {"n": 20}, "direction": 1,
   "rationale": "一句话经济学逻辑"}
]
不要输出任何其他文字。"""


def propose_factors(n: int = 8, tried: list[dict] | None = None,
                     history: list[dict] | None = None) -> list[dict]:
    """让 LLM 提出 n 个新因子。tried=已尝试的 spec；history=带结果的反馈。"""
    user = f"""{OP_DOC}

请提出 {n} 个候选因子。

已知的算子参数含义（务必让参数落在合理范围）：
- 动量/反转类 n 建议 3~60；波动率类 n 建议 10~60。
- 价格位置/回撤类 n 建议 20~120。
"""

    if tried:
        names = [t.get("name") or t.get("op") for t in tried]
        user += f"\n已尝试过的因子（不要重复）：{names[-40:]}\n"

    if history:
        user += "\n【重要】上一轮回测反馈（据此提出更好的因子，避开差的）：\n"
        for h in history[-12:]:
            user += (f"- {h['name']}: IC={h['ic_mean']:+.4f} ICIR={h['icir']:+.2f} "
                     f"多空Sharpe={h['ls_sharpe']:+.2f} OOS_IC={h.get('oos_ic_mean', float('nan')):+.4f} "
                     f"→ {'有效' if h.get('passed') else '无效'}\n")
        user += "\n请优先借鉴有效的方向，并尝试新的经济逻辑组合。\n"

    last_raw = ""
    for attempt in range(3):
        raw = chat([{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user}], temperature=0.85)
        last_raw = raw
        try:
            data = extract_json(raw)
            break
        except (json.JSONDecodeError, ValueError):
            # 回退：再追问一次，强制只输出 JSON
            user = (f"你上次的输出不是合法 JSON。请只输出 JSON 数组，"
                    f"不要任何解释和 markdown。提出 {n} 个因子，格式严格为 "
                    '[{"name":"x","op":"mom","params":{"n":20},"direction":1,"rationale":"..."}]')
    else:
        raise RuntimeError(f"LLM 连续 3 次未返回合法 JSON。最后输出片段: {last_raw[:200]!r}")
    if isinstance(data, dict):
        data = [data]
    # 规范化
    out = []
    for spec in data:
        if not isinstance(spec, dict) or "op" not in spec:
            continue
        # 把散落在顶层的参数（如 n、window）收进 params
        params = dict(spec.get("params", {}) or {})
        for k, v in spec.items():
            if k in ("op", "name", "direction", "rationale", "params", "desc", "reason"):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                params[k] = int(v)
        # 常见别名归一
        for alias, std in [("window", "n"), ("lookback", "n"), ("period", "n")]:
            if alias in params and "n" not in params:
                params["n"] = params.pop(alias)
        if "skip" in params:
            params["skip"] = int(params["skip"])
        out.append({
            "name": str(spec.get("name", spec["op"]))[:40],
            "op": spec["op"],
            "params": params,
            "direction": 1 if spec.get("direction", 1) >= 0 else -1,
            "rationale": str(spec.get("rationale", spec.get("desc", "")))[:200],
        })
    return out
