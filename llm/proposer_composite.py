"""LLM 复合因子提案器：让大模型设计【因子交互】而不是单算子。

为什么这是 LLM 的主场：
  单算子空间（mom/vol/reversal…×参数）已经被系统扫描穷举过了，
  LLM 在这上面提不出新东西。真正的 alpha 往往在交互里：
  动量÷波动、反转×量能、价值×质量、趋势-反转…
  这个空间是指数级的，扫不完，但 LLM 有经济学先验，能定向搜索。

输出格式（与 mine.py 的 combo 兼容）：
  {"name":..., "kind":"combo",
   "expr":{"op":"div","left":{"op":"mom","params":{"n":120}},
                            "right":{"op":"vol","params":{"n":60}}},
   "direction":1, "rationale":"..."}

表达式树可嵌套（left/right 各自可再是 {op,left,right}），
组合算子：add sub mul div rank_add rank_sub rank_mul neg abs

重要：free 模型常无视嵌套 schema，返回扁平 {"factors":[...],"formula":"..."}。
因此本模块优先解析嵌套 expr，解析失败则用【安全表达式解析器】从 formula 字符串
重建表达式树 —— 全程不 eval，只允许白名单算子。
"""
from __future__ import annotations
import json
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from factors.engine import OP_DOC, OPS
from llm.client import chat, extract_json

MAX_LLM_CONCURRENCY = max(1, int(os.environ.get("QUANT_MAX_LLM_CONCURRENCY", "4")))


COMBINE_OPS = "add, sub, mul, div, rank_add, rank_sub, rank_mul, neg, abs"

SYSTEM = """你是顶尖量化研究员，专精【复合因子/因子交互】设计。

单算子因子已被穷举，你要设计的是【因子之间的交互】。

【输出格式 —— 必须严格遵守，只输出 JSON 数组，无任何其他文字】
每个元素必须是这个嵌套结构：
{"name":"英文名","kind":"combo",
 "expr":{"op":"div",
         "left":{"op":"mom","params":{"n":120}},
         "right":{"op":"vol","params":{"n":60}}},
 "direction":1,"rationale":"一句话经济逻辑"}

- expr 是一棵二叉树：内部节点用组合算子（add/sub/mul/div/rank_add/rank_sub/rank_mul/neg/abs），
  叶子是基础算子 {"op":"名","params":{"n":整数}}。
- 允许嵌套：left/right 本身可以再是 {"op":"div","left":...,"right":...}。
- 禁止输出 "factors" 数组或 "formula" 字符串 —— 只能是上面的嵌套 expr。
- 基础算子只能用文档里列出的；参数为整数、落在合理范围。
- 每个因子必须有清晰的经济学逻辑。direction=1 越大越看多，-1 越大越看空。
- 提出 {n} 个，深度2~3层，避免全用同一个组合算子。"""

# 角度种子：每路从不同经济逻辑切入
SEEDS = [
    "风险调整：分子是收益类信号、分母是风险类信号（波动/回撤/偏度），做夏普类组合",
    "量价交互：趋势或反转信号 × 成交量/换手/量比，用放量与否做确认或过滤",
    "期限结构冲突：长周期动量 减/除 短周期反转，分离趋势与噪声",
    "条件性反转：只在低流动性(amihud高)或极端价格位置时才反转",
    "风险调整后的动量：动量除以波动或回撤，做稳健趋势",
    "位置×波动：价格区间位置（突破/回撤）与波动率结构交互",
    "非线性变换：对基础信号做 rank 变换后再组合，弱化肥尾",
    "正交化思路：两个经济含义不同但都有效的信号做 rank 差（rank_sub）",
    "波动率风险溢价代理：用不同窗口波动率之比刻画波动期限结构",
    "情绪/关注度代理：量能异动与价格背离的交互",
    "尾部风险调整：用偏度/峰度惩罚极端行情下的暴露",
    "隔夜与日内分解：gap（跳空）与日内动量/VWAP偏离的交互",
]

_BINOPS = {"add", "sub", "mul", "div", "max", "min", "rank_add", "rank_sub", "rank_mul"}
_UNOPS = {"neg", "abs"}
# formula 字符串里允许出现的组合算名词 → 内部算子
_FORMULA_FUNCS = {
    "rank_add": "rank_add", "rank_sub": "rank_sub", "rank_mul": "rank_mul",
}


# ---------- 安全表达式解析器（从 formula 字符串重建树，绝不 eval） ----------
def _tokenize_formula(s: str) -> list[str]:
    # 只保留可识别的 token：标识符、数字、括号、运算符
    return re.findall(r"[A-Za-z_][A-Za-z_0-9]*|\d+\.?\d*|[()+\-*/,]", s)


def _num_leaf(v: float) -> dict:
    """把裸数字包装成内部标记，供参数解析识别（不进入最终树）。"""
    return {"_num": v}


def _parse_formula(expr: str) -> dict | None:
    """把形如 '(mom(60,5) + reversal(5)) / (vol(20) + max_drawdown(60))' 解析成表达式树。

    仅支持：+ - * /、函数调用 name(arg,arg)、括号、嵌套。
    组合算子用函数名 rank_add(a,b) / rank_sub(a,b) / rank_mul(a,b) / neg(a) / abs(a)。
    返回 None 表示无法安全解析。
    """
    toks = _tokenize_formula(expr)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def eat(t=None):
        nonlocal pos
        cur = peek()
        if t is not None and cur != t:
            raise ValueError(f"期望 {t} 得到 {cur}")
        pos += 1
        return cur

    def parse_primary() -> dict:
        cur = peek()
        if cur == "(":
            eat("(")
            n = parse_expr()
            eat(")")
            return n
        if cur == "-":
            eat("-")
            return {"op": "neg", "left": parse_primary()}
        if cur is not None and re.match(r"^\d", cur):
            return _num_leaf(float(eat()))
        if cur is not None and re.match(r"^[A-Za-z_]", cur):
            name = eat()
            if peek() == "(":
                eat("(")
                args = [parse_expr()]
                while peek() == ",":
                    eat(",")
                    args.append(parse_expr())
                eat(")")
                if name in _BINOPS and len(args) == 2:
                    return {"op": name, "left": args[0], "right": args[1]}
                if name in _UNOPS and len(args) == 1:
                    return {"op": name, "left": args[0]}
                if name == "max" and len(args) == 2:
                    return {"op": "max", "left": args[0], "right": args[1]}
                if name == "min" and len(args) == 2:
                    return {"op": "min", "left": args[0], "right": args[1]}
                # 基础算子叶子：name(n=.., skip=..)
                if name in OPS:
                    params: dict = {}
                    for i, a in enumerate(args):
                        key = "n" if i == 0 else ("skip" if i == 1 else None)
                        if key is None or not isinstance(a, dict) or a.get("_num") is None:
                            # 无法识别参数 → 放弃整棵树（安全优先）
                            raise ValueError("叶子参数无法解析")
                        params[key] = int(a["_num"])
                    return {"op": name, "params": params}
                raise ValueError(f"未知函数 {name}")
            raise ValueError(f"裸标识符 {name}")
        raise ValueError(f"意外 token {cur}")

    def parse_term() -> dict:
        node = parse_primary()
        while peek() in ("*", "/"):
            o = eat()
            rhs = parse_primary()
            node = {"op": "mul" if o == "*" else "div", "left": node, "right": rhs}
        return node

    def parse_expr() -> dict:
        node = parse_term()
        while peek() in ("+", "-"):
            o = eat()
            rhs = parse_term()
            node = {"op": "add" if o == "+" else "sub", "left": node, "right": rhs}
        return node

    try:
        tree = parse_expr()
        if pos != len(toks):
            return None
        return tree
    except Exception:
        return None


def _canon_tree(node, depth: int = 0):
    """容错规范化：把 LLM 的各种畸形变体统一成标准表达式树。

    已实测过的变体：
      - 组合节点用 left/right，或 args:[a,b]，或 args:{...}
      - 叶子参数放在 params:{n:..}，或 args:{n:..}，或顶层 n:..
      - 出现未知算子（如 const）→ 整棵子树丢弃
    返回 None 表示该子树不可用；合法返回标准 dict。
    """
    if not isinstance(node, dict) or depth > 6:
        return None
    op = node.get("op")
    if not isinstance(op, str):
        return None

    # --- 组合算子 ---
    if op in _BINOPS or op in _UNOPS or op in ("max", "min"):
        left = node.get("left")
        right = node.get("right")
        if (left is None or right is None) and isinstance(node.get("args"), list):
            a = node["args"]
            if len(a) >= 2:
                left, right = a[0], a[1]
            elif len(a) == 1:
                left = a[0]
        if op in _UNOPS:
            lc = _canon_tree(left, depth + 1)
            return {"op": op, "left": lc} if lc is not None else None
        lc = _canon_tree(left, depth + 1)
        rc = _canon_tree(right, depth + 1)
        if lc is None or rc is None:
            return None
        return {"op": op, "left": lc, "right": rc}

    # --- 基础算子叶子 ---
    if op in OPS:
        params: dict = {}
        # 合并所有可能的参数来源
        for src in (node.get("params"), node.get("args") if isinstance(node.get("args"), dict) else None):
            if isinstance(src, dict):
                for k, v in src.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        params[k] = int(v)
        for k, v in node.items():
            if k in ("op", "params", "args", "name", "desc", "description",
                     "direction", "rationale"):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                params.setdefault(k, int(v))
        # n 别名
        for alias in ("window", "lookback", "period"):
            if alias in params and "n" not in params:
                params["n"] = params.pop(alias)
        # 只保留该算子真正支持的参数
        try:
            import inspect
            allowed = set(inspect.signature(OPS[op]).parameters) - {"panel"}
        except Exception:
            allowed = {"n", "skip"}
        params = {k: v for k, v in params.items() if k in allowed}
        return {"op": op, "params": params}

    # 未知算子（如 const）→ 丢弃
    return None


def _valid_tree(node, depth: int = 0) -> bool:
    if not isinstance(node, dict) or depth > 6:
        return False
    op = node.get("op")
    if op in _BINOPS:
        return _valid_tree(node.get("left"), depth + 1) and _valid_tree(node.get("right"), depth + 1)
    if op in _UNOPS:
        return _valid_tree(node.get("left"), depth + 1)
    if op in OPS:
        for k, v in (node.get("params") or {}).items():
            if not isinstance(v, int) or isinstance(v, bool):
                return False
        return True
    return False


def _flat_to_tree(spec: dict) -> dict | None:
    """把 free 模型常返回的扁平 {'factors':[...],'formula':...} 转成表达式树。"""
    formula = spec.get("formula") or spec.get("expr_str") or spec.get("expression")
    if isinstance(formula, str):
        t = _parse_formula(formula)
        if t is not None and _valid_tree(t):
            return t
    # 退而求其次：把 factors 列表里第一个基础算子当单因子（信息损失但合法）
    facs = spec.get("factors")
    if isinstance(facs, list) and facs:
        f0 = facs[0]
        if isinstance(f0, dict) and f0.get("op") in OPS:
            params = {k: int(v) for k, v in f0.items()
                      if k not in ("op", "direction", "n_", "desc") and isinstance(v, int)}
            return {"op": f0["op"], "params": params}
    return None


def _normalize(spec: dict) -> dict | None:
    if not isinstance(spec, dict):
        return None
    expr = _canon_tree(spec.get("expr") or spec.get("expression"))
    if expr is None:
        expr = _flat_to_tree(spec)  # 扁平 factors/formula 回退
    if expr is None:
        return None
    d = spec.get("direction", 1)
    if isinstance(d, (list, tuple)):
        d = d[0] if d else 1
    try:
        direction = 1 if float(d) >= 0 else -1
    except (TypeError, ValueError):
        direction = 1
    return {
        "name": str(spec.get("name", "combo"))[:40],
        "kind": "combo",
        "expr": expr,
        "direction": direction,
        "rationale": str(spec.get("rationale", spec.get("description", spec.get("desc", ""))))[:200],
    }


def _one_call(seed: str, n: int, tried_names: list[str], temperature: float) -> list[dict]:
    user = f"""{OP_DOC}

【本次交互设计角度】{seed}

已提过（避免重复）：{tried_names[-60:] if tried_names else '无'}

请提出 {n} 个复合因子，只输出 JSON 数组（嵌套 expr 结构）。"""
    try:
        raw = chat([{"role": "system", "content": SYSTEM.replace("{n}", str(n))},
                    {"role": "user", "content": user}], temperature=temperature)
        data = extract_json(raw)
        if isinstance(data, dict):
            data = [data]
        return data if isinstance(data, list) else []
    except Exception:
        return []


def propose_composite_batch(total: int = 60, concurrency: int = 12, per_call: int = 5,
                            tried: list[dict] | None = None, history: list[dict] | None = None,
                            temperature: float = 0.9, verbose: bool = True) -> list[dict]:
    """并行生成 total 个复合因子候选（去重 + 合法性校验）。

    history: 上一轮回测反馈，用于让 LLM 借鉴有效方向。
    """
    concurrency = max(1, min(int(concurrency), MAX_LLM_CONCURRENCY))
    tried_names = [t.get("name", "") for t in (tried or [])]
    n_calls = max(1, total // per_call)
    seeds = [SEEDS[i % len(SEEDS)] for i in range(n_calls)]

    if history:
        good = [h for h in history if h.get("passed")][:5]
        if good:
            seeds = [f"{s}（参考上轮有效的交互：{[g['name'] for g in good]}）" for s in seeds]

    results: list[dict] = []
    if verbose:
        print(f"  [复合提案] {n_calls} 路并发 × {per_call} 个 = 目标 {n_calls*per_call}", flush=True)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = {ex.submit(_one_call, s, per_call, tried_names, temperature): i
                for i, s in enumerate(seeds)}
        for f in as_completed(futs):
            results.extend(f.result() or [])

    seen, out = set(), []
    for spec in results:
        try:
            norm = _normalize(spec)
        except Exception:
            continue
        if not norm:
            continue
        key = json.dumps({"expr": norm["expr"], "direction": norm["direction"]}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    if verbose:
        print(f"  [复合提案] 生成 {len(out)} 个合法唯一候选", flush=True)
    return out
