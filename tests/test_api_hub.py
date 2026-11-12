#!/usr/bin/env python3
"""data/api_hub.py 增量回归：全部 mock，不真调外部 API、不耗免费额度。"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/opt/quant-lab")   # pytest 的 rootdir 不保证把项目根加进 sys.path

import pandas as pd
import pytest

import data.api_hub as hub


class _R:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self._p


@pytest.fixture
def no_net(monkeypatch, tmp_path):
    monkeypatch.setattr(hub, "OUT_DIR", tmp_path)
    return monkeypatch


def test_registry_and_crypto_symbols():
    assert set(hub.ADAPTERS) == {"alpaca_news", "finnhub_insider", "twelve_bars",
                                 "av_insider", "binance_public"}
    assert all(s.endswith("USDT") for s in hub.CRYPTO_SYMBOLS)
    assert not (set(hub.CRYPTO_SYMBOLS) & {"AAPL", "MSFT", "NVDA"})


def test_alpaca_news_adapter(no_net):
    no_net.setattr(hub, "_get", lambda *a, **k: _R({"news": [
        {"created_at": "2025-01-02T10:00:00Z", "headline": "h",
         "summary": "s", "source": "Benzinga", "symbols": ["AAPL"]}]}))
    d = hub.alpaca_news(["AAPL"], "2025-01-01", "2025-01-07")
    assert d.shape == (1, 5) and d.iloc[0]["source"] == "Benzinga"


def test_finnhub_insider_adapter(no_net):
    no_net.setattr(hub, "_get", lambda *a, **k: _R({"data": [
        {"filingDate": "2026-11-05", "transactionDate": "2026-11-03",
         "transactionCode": "S", "share": 100, "change": -10,
         "transactionPrice": 300.0}]}))
    no_net.setattr(hub.time, "sleep", lambda *a: None)
    d = hub.finnhub_insider(["AAPL"], "2025-01-01", "2025-01-07")
    assert d.shape == (1, 7) and d.iloc[0]["symbol"] == "AAPL"


def test_binance_public_adapter(no_net):
    no_net.setattr(hub, "_get", lambda *a, **k: _R([
        {"timestamp": 1, "longShortRatio": "1.2",
         "longAccount": "0.55", "shortAccount": "0.45"}]))
    no_net.setattr(hub.time, "sleep", lambda *a: None)
    d = hub.binance_public(["BTCUSDT"], "2025-01-01", "2025-01-07")
    assert d.shape == (1, 5) and d.iloc[0]["symbol"] == "BTCUSDT"


def test_run_writes_parquet(no_net, tmp_path):
    no_net.setattr(hub, "_get", lambda *a, **k: _R({"news": []}))
    r = hub.run("alpaca_news", ["AAPL"], "2025-01-01", "2025-01-07")
    assert r["rows"] == 0 and r["out"] is None
    no_net.setattr(hub, "_get", lambda *a, **k: _R({"news": [
        {"created_at": "2025-01-02", "headline": "h", "summary": "",
         "source": "x", "symbols": ["AAPL"]}]}))
    r = hub.run("alpaca_news", ["AAPL"], "2025-01-01", "2025-01-07")
    assert r["rows"] == 1 and (tmp_path / "alpaca_news.parquet").exists()


def test_keys_never_in_repr():
    """接入层源码不得硬编码任何 key 值（只从 os.environ 读）。"""
    src = open(hub.__file__, encoding="utf-8").read()
    assert "_key(" in src and "os.environ.get" in src
    assert "apikey=" not in src.split("def _key")[0]
