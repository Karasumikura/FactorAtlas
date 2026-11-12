"""盘口沉寂监控（scripts/ops_watch._book_silence）。

2026-11-01 加。起因：共振盘 10-29 20:23 平掉最后一腿后连续 55 小时 silent，
心跳正常、tick 正常、巡检全绿 —— 账户躺平两天多而没人知道。

这里全部在 tmp_path 里造心跳夹具，不碰真实 data/。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scripts.ops_watch as w  # noqa: E402

NOW = datetime.now(timezone.utc)


def _hb(ts: datetime, **kw) -> str:
    return json.dumps({"ts": ts.isoformat(), **kw})


def _book(root: Path, name: str, lines: list[str]) -> None:
    d = root / "data" / "binance" / "books" / name
    d.mkdir(parents=True)
    (d / "trader_heartbeat.jsonl").write_text("\n".join(lines) + "\n")


@pytest.fixture()
def books(tmp_path, monkeypatch):
    """四个盘：刚出信号 / 120h 没信号 / 心跳不带 n_signals / 脏行。"""
    _book(tmp_path, "fresh", [
        _hb(NOW - timedelta(hours=6), status="silent", n_signals=0),
        _hb(NOW - timedelta(hours=1), status="executed", n_signals=2),
        _hb(NOW, status="silent", n_signals=0),
    ])
    _book(tmp_path, "stale", [
        _hb(NOW - timedelta(hours=120), status="executed", n_signals=1),
        _hb(NOW, status="silent", n_signals=0),
    ])
    # lowfreq 那种：心跳只有 executed / market_closed，没有 n_signals
    _book(tmp_path, "no_nsig", [
        _hb(NOW - timedelta(days=9), status="executed"),
        _hb(NOW, status="market_closed", note="非美股开市日"),
    ])
    # 截断行 + 空行 + 非法 ts：不许抛，且仍取到 50h 那条
    _book(tmp_path, "dirty", [
        '{"ts": "garbage"',
        "",
        _hb(NOW - timedelta(hours=50), status="dry_run", n_signals=1)[:20],
        _hb(NOW - timedelta(hours=50), status="dry_run", n_signals=1),
    ])
    monkeypatch.setattr(w, "PROJ", tmp_path)
    return w._book_silence()


def test_silent_hours_measures_last_real_signal(books):
    assert 0.5 <= books["fresh"]["silent_hours"] <= 1.5
    assert 119 <= books["stale"]["silent_hours"] <= 121
    assert all(v["heartbeat_age_min"] >= 0 for v in books.values())


def test_book_without_n_signals_is_not_tracked(books):
    """lowfreq 拿 executed 当信号代理会在正常空仓期误报 —— 必须跳过，不猜。"""
    assert books["no_nsig"]["silent_hours"] is None
    assert books["no_nsig"]["tracked"] is False


def test_dirty_lines_are_skipped_not_fatal(books):
    assert 49 <= books["dirty"]["silent_hours"] <= 51


def test_threshold_fires_only_past_limit(books, monkeypatch):
    def fired(limit):
        monkeypatch.setattr(w, "BOOK_SILENT_HOURS", limit)
        return sorted(k for k, v in books.items()
                      if v["silent_hours"] is not None and v["silent_hours"] >= limit)

    assert fired(96) == ["stale"]
    assert fired(200) == []          # 不误报
    assert fired(48) == ["dirty", "stale"]


def test_render_marks_untracked_book(books):
    out = w._render({"ts": "T", "healthy": True, "alerts": [], "services": {},
                     "mem": {}, "disk": {}, "trader_last_tick": {"found": False},
                     "book_silence": books, "miner": {}, "trials": {},
                     "llm_health": {}, "risk_recent": {}})
    assert "盘口[stale]" in out
    assert "不跟踪沉寂" in out
