"""因子库（Factor Zoo）：所有挖过的因子落盘、去重、可复用。

持续累积——避免重复挖同一个因子，也让后续能基于历史结果进化。
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

from core import config

ZOO_FILE = config.DATA_DIR / "factor_zoo.json"


def _key(spec: dict, hold: int | None = None) -> str:
    """缓存键。hold=1（默认持有期）时**不加后缀**，保持与历史 6.5 万条记录兼容，
    避免一次性让整个 zoo 失效、全部重算；其它持有期加 :h<N> 后缀，
    使同一个 spec 在 hold=1/5/20 下的结果各自独立存放、互不覆盖。

    为什么必须区分 hold：record() 用 _key 作为主键，若不带 hold，
    跑 hold=5 会把 hold=1 的记录覆盖掉，之后复用判断
    `prev["hold"] == hold` 永远失配 → 缓存彻底失效。
    """
    payload = {"op": spec.get("op"), "params": spec.get("params", {}),
               "direction": spec.get("direction", 1)}
    if spec.get("kind") == "combo" or "expr" in spec:
        payload = {"expr": spec.get("expr"), "direction": spec.get("direction", 1)}
    k = json.dumps(payload, sort_keys=True)
    try:
        h = int(hold) if hold is not None else 1
    except (TypeError, ValueError):
        h = 1
    return k if h == 1 else f"{k}:h{h}"


def load() -> dict:
    if ZOO_FILE.exists():
        try:
            return json.loads(ZOO_FILE.read_text())
        except Exception:
            pass
    return {"factors": {}, "updated_at": None}


def save(zoo: dict):
    zoo["updated_at"] = datetime.now().isoformat()
    ZOO_FILE.parent.mkdir(parents=True, exist_ok=True)
    ZOO_FILE.write_text(json.dumps(zoo, ensure_ascii=False, indent=2))


def has(zoo: dict, spec: dict, hold: int | None = None) -> bool:
    return _key(spec, hold) in zoo["factors"]


def record(zoo: dict, spec: dict, result: dict, panel_end: str | None = None,
           hold: int | None = None):
    zoo["factors"][_key(spec, hold)] = {
        "name": spec.get("name"), "spec": spec,
        "result": result, "recorded_at": datetime.now().isoformat(),
        # 数据窗口指纹：panel_end 变化即视为样本更新，缓存不再复用
        "panel_end": panel_end, "hold": hold,
    }


def stats(zoo: dict) -> dict:
    fs = zoo["factors"].values()
    surv = [f for f in fs if f["result"].get("survivor")]
    return {"total": len(zoo["factors"]), "survivors": len(surv)}
