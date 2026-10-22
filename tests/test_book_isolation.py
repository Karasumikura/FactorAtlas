"""账本(book)隔离测试：多盘共享一个 Demo 账户时，各盘互不影响。

核心保证：
  1. 不同 book 的台账/状态/心跳/成交路径**零重叠**；
  2. 每个 book 的自有持仓账独立读写；
  3. apply_book_fills 正确累加 signed_qty；
  4. 默认账本（book 空）走历史全局路径 → 行为不变。
"""
import json

import pytest

from core import book


def test_paths_are_isolated_between_books():
    a = book.paths("lowfreq")
    b = book.paths("resonance")
    for key in ("decisions", "heartbeat", "state", "ledger", "llm_decisions"):
        assert str(a[key]) != str(b[key]), key


def test_default_book_uses_global_paths():
    d = book.paths(None)
    # 默认账本的决策文件应在 data/binance/ 根下，而不是 books/ 子目录
    assert "books" not in str(d["decisions"])
    assert d["decisions"].name == "trader_decisions.jsonl"


def test_default_and_named_book_differ():
    d = book.paths("")
    n = book.paths("resonance")
    assert str(d["decisions"]) != str(n["decisions"])


def test_book_positions_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(book, "_BINANCE", tmp_path)
    assert book.load_book_positions("x") == {}
    book.save_book_positions({"AAAUSDT": 0.5, "BBBUSDT": -0.2}, "x")
    got = book.load_book_positions("x")
    assert got == {"AAAUSDT": 0.5, "BBBUSDT": -0.2}


def test_apply_book_fills_accumulates(tmp_path, monkeypatch):
    monkeypatch.setattr(book, "_BINANCE", tmp_path)
    book.apply_book_fills([{"symbol": "AAAUSDT", "signed_qty": 0.5}], "x")
    book.apply_book_fills([{"symbol": "AAAUSDT", "signed_qty": 0.3}], "x")
    assert book.load_book_positions("x")["AAAUSDT"] == pytest.approx(0.8)


def test_apply_book_fills_removes_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(book, "_BINANCE", tmp_path)
    book.apply_book_fills([{"symbol": "AAAUSDT", "signed_qty": 0.5}], "x")
    book.apply_book_fills([{"symbol": "AAAUSDT", "signed_qty": -0.5}], "x")
    # 归零后应被移除（不残留 0.0 噪声）
    assert "AAAUSDT" not in book.load_book_positions("x")


def test_books_do_not_share_positions(tmp_path, monkeypatch):
    monkeypatch.setattr(book, "_BINANCE", tmp_path)
    book.apply_book_fills([{"symbol": "AAAUSDT", "signed_qty": 1.0}], "lowfreq")
    # 另一个盘不应看到 lowfreq 的持仓
    assert book.load_book_positions("resonance") == {}


def test_invalid_book_name_rejected():
    with pytest.raises(ValueError):
        book.normalize("bad/../name")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
