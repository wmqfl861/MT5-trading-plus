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


# ------------------------------------------------- lockbox 状态（T21a/C3）

def test_lockbox_started_status_counts_as_used(tmp_path):
    """started 后崩溃不能重跑：started 行同样算已用；二次 start 撞 UNIQUE。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    led.record_lockbox_start("H001", approved_by="user")
    assert led.lockbox_used("H001") is True
    assert led.lockbox_status("H001") == "started"
    with pytest.raises(sqlite3.IntegrityError):
        led.record_lockbox_start("H001", approved_by="user")
    led.close()


def test_lockbox_finish_updates_started_row(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    led.record_lockbox_start("H001", approved_by="user")
    led.finish_lockbox_use("H001", result={"passed": True})
    assert led.lockbox_status("H001") == "done"
    row = rows(led, "lockbox_uses")[0]
    assert row["approved_by"] == "user"           # started 行的批准人保留
    assert json.loads(row["result_json"])["passed"] is True
    with pytest.raises(sqlite3.IntegrityError, match="card_id"):
        led.finish_lockbox_use("H001", result={"passed": False})
    led.close()


def test_lockbox_finish_without_start_inserts_done(tmp_path):
    """直接路径（run_g5 单元调用，无 started 行）→ 插入完整 done 行。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    led.finish_lockbox_use("H001", result={"passed": True}, approved_by="u2")
    assert led.lockbox_status("H001") == "done"
    row = rows(led, "lockbox_uses")[0]
    assert row["approved_by"] == "u2"
    led.close()


def test_lockbox_status_column_migrated_for_old_db(tmp_path):
    """老库（status 列不存在）打开即补列，既有终态行按 done 处理。"""
    p = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE lockbox_uses(card_id TEXT NOT NULL UNIQUE,"
        " used_at_utc TEXT NOT NULL, approved_by TEXT NOT NULL,"
        " result_json TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO lockbox_uses VALUES ('H001','2026-01-01','u','{}')")
    conn.commit()
    conn.close()
    led = Ledger(p)
    assert led.lockbox_status("H001") == "done"
    assert led.lockbox_used("H001") is True
    led.close()


def test_gate_passed(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    rid = record(led)
    led.record_gate(rid, "G3", True, {})
    assert led.gate_passed("H001", "G3") is True
    assert led.gate_passed("H001", "G2") is False      # 没跑过 G2
    assert led.gate_passed("H999", "G3") is False      # 别的卡
    rid2 = record(led, run_n=2)
    led.record_gate(rid2, "G2", False, {})             # 未通过的关卡不算
    assert led.gate_passed("H001", "G2") is False
    led.close()


def test_first_run_at_version_scoped(tmp_path):
    """按卡文件判定（T21d）：version 过滤后是"该文件"的首次运行。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    t1 = datetime(2026, 10, 20, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 22, tzinfo=timezone.utc)
    led.record_run(card_id="H012-v1", card_version=1, strategy="s:C",
                   params={}, symbol="XAUUSD", timeframe="H1",
                   data_first="2024-01-01", data_last="2026-01-01",
                   data_sha256="a" * 64, engine_version="A-1.3",
                   cost_model_version="zero-v0", gates_hash="",
                   metrics={"net": 1.0}, actor="t", created_utc=t1)
    led.record_run(card_id="H012-v2", card_version=2, strategy="s:C",
                   params={}, symbol="XAUUSD", timeframe="H1",
                   data_first="2024-01-01", data_last="2026-01-01",
                   data_sha256="a" * 64, engine_version="A-1.3",
                   cost_model_version="zero-v0", gates_hash="",
                   metrics={"net": 1.0}, actor="t", created_utc=t2)
    assert led.first_run_at("H012-v1") == t1
    assert led.first_run_at("H012-v2") == t2           # 新文件自己的首跑
    assert led.first_run_at("H012-v1", version=1) == t1
    led.close()


# ------------------------------------------------- N 去重（T21c/H4/M4）

def test_stats_dedup_same_config_counted_once(tmp_path):
    """同一组参数先 screen 后 gate：total_runs 计 2，去重 N 只计 1。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    record(led, run_n=1, created_utc=datetime(2026, 10, 20, tzinfo=timezone.utc))
    # 同 (strategy, params, symbol, timeframe, data_sha256, cost_model_version)
    # 的第二次成功运行（gate 基准运行，actor 不同）
    led.record_run(
        card_id="H001", card_version=1,
        strategy="mt5quant.strategies.library.buy_hold:BuyHold",
        params={"run": 1}, symbol="XAUUSD", timeframe="H1",
        data_first="2024-01-01T00:00:00", data_last="2026-06-30T23:00:00",
        data_sha256="deadbeef" * 8, engine_version="A-1.1",
        cost_model_version="zero-v0", gates_hash="ab" * 32,
        metrics={"net_profit": 999.0}, actor="bt-gate",
        created_utc=datetime(2026, 10, 21, tzinfo=timezone.utc))
    st = led.stats("H001")
    assert st["total_runs"] == 2
    assert st["n_dedup"] == 1
    assert st["family"] == "H001"
    assert st["family_n_dedup"] == 1
    assert st["project_n_dedup"] == 1
    led.close()


def test_stats_dedup_params_key_order_insensitive(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    led.record_run(card_id="H001", card_version=1, strategy="s:C",
                   params={"a": 1, "b": 2}, symbol="X", timeframe="H1",
                   data_first="2024-01-01", data_last="2026-01-01",
                   data_sha256="a" * 64, engine_version="A-1.3",
                   cost_model_version="v1", gates_hash="",
                   metrics={"net": 1.0}, actor="t")
    led.record_run(card_id="H001", card_version=1, strategy="s:C",
                   params={"b": 2, "a": 1}, symbol="X", timeframe="H1",
                   data_first="2024-01-01", data_last="2026-01-01",
                   data_sha256="a" * 64, engine_version="A-1.3",
                   cost_model_version="v1", gates_hash="",
                   metrics={"net": 1.0}, actor="t")
    assert led.stats("H001")["n_dedup"] == 1
    led.close()


def test_stats_dedup_failed_runs_excluded(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    record(led, run_n=1, error="boom")                 # 失败运行不计 N
    assert led.stats("H001")["n_dedup"] == 0
    record(led, run_n=2)                               # 成功 1 次
    record(led, run_n=2, error="boom again")           # 同参失败不重复计
    assert led.stats("H001")["n_dedup"] == 1
    led.close()


def test_stats_family_accumulates_card_versions(tmp_path):
    """H012-v2 是新文件：v1 跑过不影响 v2 的先卡后测；谱系 N 累计两者。"""
    from mt5quant.research import cards as rcards
    led = Ledger(tmp_path / "ledger.sqlite")
    past = datetime(2026, 10, 20, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 21, tzinfo=timezone.utc)
    for cid, ver, k in (("H012-v1", 1, 10), ("H012-v2", 2, 20)):
        led.record_run(card_id=cid, card_version=ver, strategy="s:Donchian",
                       params={"entry_days": k}, symbol="XAUUSD",
                       timeframe="H1", data_first="2024-01-01",
                       data_last="2026-01-01", data_sha256="a" * 64,
                       engine_version="A-1.3", cost_model_version="v1",
                       gates_hash="", metrics={"net": 1.0}, actor="t",
                       created_utc=t1)
    # v1 跑过（first_run_at 有值）之后，v2 作为新文件仍可运行（按文件判定）
    rcards.ensure_card_precedes_run("H012-v2", past,
                                    led.first_run_at("H012-v2"),
                                    worktree_clean=True)
    st = led.stats("H012-v2")
    assert st["family"] == "H012"
    assert st["family_total_runs"] == 2
    assert st["family_n_dedup"] == 2                   # N 累计 v1 与 v2
    assert st["n_dedup"] == 2
    st_all = led.stats()
    assert st_all["n_dedup"] == 2                      # 项目级 N
    led.close()


def test_stats_project_level_n_across_families(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    record(led, run_n=1)
    record(led, run_n=1, card_id="H002", symbol="EURUSD")
    assert led.stats()["n_dedup"] == 2                # 项目级：跨谱系
    assert led.stats("H001")["n_dedup"] == 1          # 谱系级
    assert led.stats("H001")["project_n_dedup"] == 2
    led.close()
