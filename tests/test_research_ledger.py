"""试验台账（T11）：SQLite 表结构、失败运行也记一行、锁箱一次性、stats 计数。

表结构按计划：cards / runs / gates / lockbox_uses（card_id UNIQUE）。
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from mt5quant.research.ledger import Ledger


def record(led: Ledger, run_n=1, *, card_id="H001", error=None,
           created_utc=None, symbol="XAUUSD"):
    return led.record_run(
        card_id=card_id, card_version=1,
        strategy="mt5quant.strategies.library.buy_hold:BuyHold",
        params={"run": run_n}, symbol=symbol, timeframe="H1",
        data_first="2024-01-01T00:00:00", data_last="2026-06-30T23:00:00",
        data_sha256="deadbeef" * 8, engine_version="A-1.1",
        cost_model_version="zero-v0", gates_hash="",
        metrics=None if error else {"net_profit": 123.4, "trade_count": 1},
        error=error, actor="test", created_utc=created_utc)


def rows(led: Ledger, table: str):
    return led.conn.execute(f"SELECT * FROM {table}").fetchall()


def test_creates_schema(tmp_path):
    led = Ledger(tmp_path / "research" / "ledger.sqlite")
    names = {r["name"] for r in led.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cards", "runs", "gates", "lockbox_uses"} <= names
    led.close()


def test_create_false_missing_file_is_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        Ledger(tmp_path / "nope.sqlite", create=False)


def test_record_run_round_trip(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    rid = record(led)
    r = rows(led, "runs")[0]
    assert r["run_id"] == rid
    assert r["card_id"] == "H001"
    assert r["card_version"] == 1
    assert json.loads(r["params_json"]) == {"run": 1}
    assert r["symbol"] == "XAUUSD"
    assert r["engine_version"] == "A-1.1"
    assert r["gates_hash"] == ""
    payload = json.loads(r["metrics_json"])
    assert payload["ok"] is True
    assert payload["metrics"]["net_profit"] == 123.4
    assert r["actor"] == "test"
    # created_utc 缺省取当前 UTC（aware ISO）
    assert datetime.fromisoformat(r["created_utc"]).tzinfo is not None
    led.close()


def test_failed_run_also_recorded(tmp_path):
    """抛异常的运行也有一行 runs：ok=false + 错误摘要，指标可缺。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    rid = record(led, error="ValueError: bars 为空")
    rs = rows(led, "runs")
    assert len(rs) == 1
    payload = json.loads(rs[0]["metrics_json"])
    assert payload["ok"] is False
    assert "bars 为空" in payload["error"]
    assert payload["metrics"] is None
    assert rs[0]["run_id"] == rid
    led.close()


def test_record_gate(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    rid = record(led)
    led.record_gate(rid, "G0", True, {"truncation": "ok"})
    g = rows(led, "gates")[0]
    assert (g["run_id"], g["gate"], g["passed"]) == (rid, "G0", 1)
    assert json.loads(g["details_json"]) == {"truncation": "ok"}
    led.close()


def test_register_card_upsert(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    led.register_card("H001", 1, "research/cards/H001.toml",
                      "abc123", datetime(2026, 10, 1, tzinfo=timezone.utc))
    led.register_card("H001", 1, "research/cards/H001.toml",
                      "abc123", datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len(rows(led, "cards")) == 1
    led.register_card("H001", 2, "research/cards/H001.toml",
                      "def456", None)   # 版本迭代 → 新行；committed 可为 NULL
    allcards = rows(led, "cards")
    assert len(allcards) == 2
    v2 = [c for c in allcards if c["version"] == 2][0]
    assert v2["committed_at_utc"] is None
    led.close()


def test_lockbox_use_second_insert_fails(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    led.record_lockbox_use("H001", approved_by="user",
                           result={"sharpe": 0.8})
    assert led.lockbox_used("H001") is True
    assert led.lockbox_used("H002") is False
    with pytest.raises(sqlite3.IntegrityError, match="card_id"):
        led.record_lockbox_use("H001", approved_by="user",
                               result={"sharpe": 1.1})
    assert len(rows(led, "lockbox_uses")) == 1
    led.close()


def test_first_run_at_is_earliest(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    t1 = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
    record(led, created_utc=t2)
    record(led, created_utc=t1)
    assert led.first_run_at("H001") == t1
    assert led.first_run_at("H999") is None
    led.close()


def test_stats_counts(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    record(led, run_n=1)                                   # H001 ok
    record(led, run_n=2)                                   # H001 ok
    record(led, run_n=3, error="boom")                     # H001 failed
    record(led, run_n=1, card_id="H002", symbol="EURUSD")  # H002 ok
    led.record_lockbox_use("H003", approved_by="user", result={})

    st = led.stats()
    assert st["total_runs"] == 4
    assert st["ok_runs"] == 3
    assert st["failed_runs"] == 1
    assert st["cards"]["H001"] == {"runs": 3, "ok": 2, "failed": 1}
    assert st["lockbox_uses"] == 1

    st1 = led.stats("H001")
    assert st1["total_runs"] == 3
    assert st1["failed_runs"] == 1
    assert st1["card_id"] == "H001"

    st2 = led.stats("H999")
    assert st2["total_runs"] == 0
    led.close()


def test_stats_reports_run_window(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    t1 = datetime(2026, 10, 20, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 22, tzinfo=timezone.utc)
    record(led, created_utc=t1)
    record(led, created_utc=t2)
    st = led.stats("H001")
    assert st["first_run_utc"] == t1.isoformat()
    assert st["last_run_utc"] == t2.isoformat()
    led.close()


def test_naive_timestamps_rejected(tmp_path):
    """created_utc 必须带时区（与 committed_at 比较的前提），naive 直接抛错。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    with pytest.raises(ValueError, match="时区"):
        record(led, created_utc=datetime(2026, 10, 20))
    led.close()


def test_first_run_at_offset_tolerant(tmp_path):
    """不同 UTC 偏移的 aware 时间也能比较出最早（换算成绝对时刻）。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    t_utc = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)
    t_plus8 = datetime(2026, 10, 20, 13, tzinfo=timezone(timedelta(hours=8)))
    record(led, created_utc=t_plus8)   # = 05:00 UTC，晚于 04:00 UTC
    record(led, created_utc=t_utc)
    assert led.first_run_at("H001") == t_utc
    led.close()
