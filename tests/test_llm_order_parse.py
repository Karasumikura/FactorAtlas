"""LLM 订单解析 + 降级链判据的回归测试。

背景（2026-10-23 巡检）：
  `_fallback_parse_orders` 上一版把「解析失败」也返回成 orders=[]，
  于是 `llm_decide_orders` 把 `_ok` 置 True —— 一次 LLM 故障会被记成
  「LLM 主动决定不交易」，`factor_fallback` 降级链整条失效，
  而且日志里 order_source 看起来完全正常。这组测试锁死这个语义。
"""
from __future__ import annotations

import pytest

from core.llm_order import _fallback_parse_orders, llm_decide_orders


def _ok(r: dict) -> bool:
    return isinstance(r.get("orders"), list) and not r.get("_parse_error")


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "今天天气不错，我打算持有不动。",
    '{"orders": [1, 2',          # 截断 JSON
    '{"orders": "none"}',        # orders 不是列表
    "null",
])
def test_unparseable_is_failure(raw):
    r = _fallback_parse_orders(raw)
    assert _ok(r) is False
    assert r.get("orders") == []


def test_valid_dict_passes():
    r = _fallback_parse_orders('{"orders": [{"symbol": "AAPLUSDT"}], "reason": "ok"}')
    assert _ok(r) is True
    assert len(r["orders"]) == 1


def test_valid_bare_array_passes():
    r = _fallback_parse_orders('[{"symbol": "MSFTUSDT"}]')
    assert _ok(r) is True
    assert r["orders"][0]["symbol"] == "MSFTUSDT"


def test_empty_orders_list_is_a_judgement():
    """LLM 明确给出 orders: [] 是合法判断（不是失败）。"""
    r = _fallback_parse_orders('{"orders": [], "reason": "贴合目标，不动"}')
    assert _ok(r) is True


def test_llm_call_failure_sets_ok_false(monkeypatch):
    """调用异常 → _ok=False，交易端才会回落到脚本订单。"""
    import llm.client as client

    def boom(*a, **kw):
        raise RuntimeError("HTTPConnectionPool(host='127.0.0.1', port=20128) refused")

    monkeypatch.setattr(client, "chat", boom)
    out = llm_decide_orders({"equity": 100.0})
    assert out["_ok"] is False
    assert out["orders"] == []


def test_llm_garbage_output_sets_ok_false(monkeypatch):
    """LLM 活着但吐了无法解析的内容 → 同样必须 _ok=False。"""
    import llm.client as client

    monkeypatch.setattr(client, "chat", lambda *a, **kw: "好的，我认为应该继续观察。")
    out = llm_decide_orders({"equity": 100.0})
    assert out["_ok"] is False


def test_llm_good_output_sets_ok_true(monkeypatch):
    import llm.client as client

    monkeypatch.setattr(
        client, "chat",
        lambda *a, **kw: '{"orders": [], "confidence": 0.6, "reason": "不动"}')
    out = llm_decide_orders({"equity": 100.0})
    assert out["_ok"] is True
    assert out["orders"] == []
