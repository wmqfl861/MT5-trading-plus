"""试验台账（T11）：SQLite 表结构、失败运行也记一行、锁箱一次性、stats 计数。

表结构按计划：cards / runs / gates / lockbox_uses（card_id UNIQUE）。
R6/C5：写前验证（防洗链）、删尾/删锁箱行检测、状态更新归档进
ledger_events、锚链验证。
"""
import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mt5quant.research.ledger import Ledger, LedgerTamperedError

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_anchor():
    spec = importlib.util.spec_from_file_location(
        "ledger_anchor_r6", REPO_ROOT / "scripts" / "ledger_anchor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


# ------------------------------------------------- T37（H3/M6/D23/D27）：哈希链

def test_chain_intact_after_normal_writes(tmp_path):
    """runs/gates/lockbox_uses 三链：常规写入（含 started→done 更新）后
    verify_chain 为空；持久化后重开仍完好。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    r1 = record(led, run_n=1)
    r2 = record(led, run_n=2)
    led.record_gate(r1, "G0", True, {})
    led.record_gate(r2, "G3", False, {"why": "x"})
    led.record_lockbox_start("H001", approved_by="u")
    led.finish_lockbox_use("H001", result={"passed": True})
    assert led.verify_chain() == []
    led.close()
    with Ledger(tmp_path / "ledger.sqlite", create=False) as led2:
        assert led2.verify_chain() == []


def test_manual_row_edit_detected_by_verify(tmp_path):
    """计划用例 4：手工改一行台账 → verify 报出该行。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    record(led, run_n=1)
    record(led, run_n=2)
    assert led.verify_chain() == []
    with led.conn:   # 模拟研究账户直连 sqlite 改行（不重算哈希）
        led.conn.execute("UPDATE runs SET actor='hacker' WHERE run_id=1")
    problems = led.verify_chain()
    assert len(problems) >= 1
    assert any("run#1" in p and "篡改" in p for p in problems)
    led.close()


def test_deleted_row_reported_as_chain_break(tmp_path):
    """计划用例 5：删一行 → 报出断链（后一行的 prev_hash 对不上）。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    for i in (1, 2, 3):
        record(led, run_n=i)
    with led.conn:
        led.conn.execute("DELETE FROM runs WHERE run_id=2")
    problems = led.verify_chain()
    assert any("run#3" in p and ("断链" in p or "prev_hash" in p)
               for p in problems)
    led.close()


def test_record_run_stores_strategy_sha(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    record(led, run_n=1)                       # 不给指纹 → legacy（旧口径）
    rid = led.record_run(
        card_id="H001", card_version=1, strategy="s:C", params={},
        symbol="XAUUSD", timeframe="H1", data_first=None, data_last=None,
        data_sha256="a" * 64, engine_version="A-1.1",
        cost_model_version="zero-v0", gates_hash="", metrics={"n": 1},
        actor="t", strategy_sha="f" * 64)
    rs = rows(led, "runs")
    assert rs[0]["strategy_sha"] == "legacy"
    assert rs[1]["strategy_sha"] == "f" * 64
    assert rs[1]["run_id"] == rid
    led.close()


def test_dedup_n_includes_strategy_sha(tmp_path):
    """计划用例 1：改策略代码后同参数重跑（指纹不同）→ 去重 N 加 1；
    同指纹重跑不重复计。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    common = dict(card_id="H100", card_version=1, strategy="s:Donchian",
                  params={"k": 1}, symbol="XAUUSD", timeframe="H1",
                  data_first="2024", data_last="2026", data_sha256="a" * 64,
                  engine_version="A-1.3", cost_model_version="v1",
                  gates_hash="", metrics={"n": 1}, actor="t")
    led.record_run(strategy_sha="1" * 64, **common)
    led.record_run(strategy_sha="2" * 64, **common)    # 代码改动 → 新指纹
    led.record_run(strategy_sha="2" * 64, **common)    # 同指纹重跑只计 1
    assert led.stats("H100")["n_dedup"] == 2
    assert led.stats("H100")["project_n_dedup"] == 2
    led.close()


def test_card_strategy_sha_binding_lookup(tmp_path):
    """绑定查询（D23）：版本内首个非 legacy 指纹；legacy 行不绑定。"""
    led = Ledger(tmp_path / "ledger.sqlite")
    assert led.card_strategy_sha("H100", 1) is None
    base = dict(card_id="H100", card_version=1, strategy="s:C", params={},
                symbol="X", timeframe="H1", data_first=None, data_last=None,
                data_sha256="a" * 64, engine_version="e",
                cost_model_version="v", gates_hash="", metrics={"n": 1},
                actor="t")
    led.record_run(strategy_sha="f" * 64, **base)
    assert led.card_strategy_sha("H100", 1) == "f" * 64
    assert led.card_strategy_sha("H100", 2) is None       # 其他版本不绑定
    led.record_run(strategy_sha="legacy",
                   **dict(base, card_id="H200"))          # 旧行 → 不绑定
    assert led.card_strategy_sha("H200", 1) is None
    led.close()


def test_fresh_ledger_has_chain_columns_no_migration_row(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        rcols = {r["name"] for r in
                 led.conn.execute("PRAGMA table_info(runs)")}
        assert {"strategy_sha", "prev_hash", "row_hash"} <= rcols
        for tbl in ("gates", "lockbox_uses"):
            cols = {r["name"] for r in
                    led.conn.execute(f"PRAGMA table_info({tbl})")}
            assert {"prev_hash", "row_hash"} <= cols
        assert led.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE actor='migration'"
        ).fetchone()[0] == 0


# ------------------------------------------------- 旧库迁移（T37：列变更/重建链）

_OLD_SCHEMA = """
CREATE TABLE cards(
  card_id TEXT NOT NULL, version INTEGER NOT NULL, path TEXT NOT NULL,
  git_commit TEXT NOT NULL, committed_at_utc TEXT, registered_utc TEXT NOT NULL,
  PRIMARY KEY (card_id, version));
CREATE TABLE runs(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL,
  card_version INTEGER NOT NULL, strategy TEXT NOT NULL,
  params_json TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
  data_first TEXT, data_last TEXT, data_sha256 TEXT NOT NULL,
  engine_version TEXT NOT NULL, cost_model_version TEXT NOT NULL,
  gates_hash TEXT NOT NULL DEFAULT '', metrics_json TEXT NOT NULL,
  created_utc TEXT NOT NULL, actor TEXT NOT NULL);
CREATE INDEX idx_runs_card ON runs(card_id);
CREATE TABLE gates(
  run_id INTEGER NOT NULL REFERENCES runs(run_id), gate TEXT NOT NULL,
  passed INTEGER NOT NULL, details_json TEXT NOT NULL,
  PRIMARY KEY (run_id, gate));
CREATE TABLE lockbox_uses(
  card_id TEXT NOT NULL UNIQUE, used_at_utc TEXT NOT NULL,
  approved_by TEXT NOT NULL, result_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'done', cutoff TEXT);
"""


def _make_old_db(p, n_runs=2):
    """T37 之前的台账形态（无 strategy_sha/prev_hash/row_hash 列）。"""
    conn = sqlite3.connect(str(p))
    conn.executescript(_OLD_SCHEMA)
    for i in range(1, n_runs + 1):
        conn.execute(
            "INSERT INTO runs(card_id, card_version, strategy, params_json,"
            " symbol, timeframe, data_first, data_last, data_sha256,"
            " engine_version, cost_model_version, gates_hash, metrics_json,"
            " created_utc, actor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("H001", 1, "s:BuyHold", json.dumps({"i": i}), "XAUUSD", "H1",
             None, None, "a" * 64, "A-1.1", "zero-v0", "",
             json.dumps({"ok": True, "error": None, "metrics": {"n": i}}),
             f"2026-10-0{i}T00:00:00+00:00", "bt"))
    conn.execute("INSERT INTO gates(run_id, gate, passed, details_json)"
                 " VALUES (1,'G0',1,'{}')")
    conn.execute(
        "INSERT INTO lockbox_uses(card_id, used_at_utc, approved_by,"
        " result_json, status) VALUES ('H009','2026-01-01T00:00:00+00:00',"
        "'u','{}','done')")
    conn.commit()
    conn.close()
    return p


def test_old_ledger_migrated_on_open(tmp_path):
    """打开即迁移：补列、旧行 strategy_sha=legacy、三链自第一行重建、
    迁移本身记一行 actor=migration；重开不再补迁移行（幂等）。"""
    p = _make_old_db(tmp_path / "old.sqlite")
    with Ledger(p) as led:
        rs = rows(led, "runs")
        real = [r for r in rs if r["actor"] != "migration"]
        assert len(real) == 2
        assert all(r["strategy_sha"] == "legacy" for r in real)
        mig = [r for r in rs if r["actor"] == "migration"]
        assert len(mig) == 1
        assert led.verify_chain() == []
        # 迁移行不是试验：不计入 N，但在 total_runs 里可审计
        st = led.stats()
        assert st["n_dedup"] == 2
        assert st["total_runs"] == 3
        assert "__migration__" in st["cards"]
    with Ledger(p) as led2:                      # 幂等：不再补迁移行
        assert len([r for r in rows(led2, "runs")
                    if r["actor"] == "migration"]) == 1
        assert led2.verify_chain() == []


def test_old_ledger_preexisting_rows_unchanged_by_migration(tmp_path):
    p = _make_old_db(tmp_path / "old.sqlite")
    conn = sqlite3.connect(str(p))
    before = conn.execute(
        "SELECT run_id, card_id, metrics_json, actor FROM runs"
        " ORDER BY run_id").fetchall()
    conn.close()
    with Ledger(p) as led:
        after = [tuple(r) for r in led.conn.execute(
            "SELECT run_id, card_id, metrics_json, actor FROM runs"
            " WHERE actor<>'migration' ORDER BY run_id").fetchall()]
    assert [tuple(b) for b in before] == after


PROD_LEDGER = Path(__file__).resolve().parents[1] / "data" / "research" \
    / "ledger.sqlite"


@pytest.mark.skipif(not PROD_LEDGER.is_file(),
                    reason="本地生产台账存在时才跑（CI 克隆无 data/）")
def test_migration_on_copy_of_production_ledger(tmp_path):
    """迁移只发生在副本上；生产台账本体字节不变（生产迁移由主会话执行）。"""
    import shutil
    copy = tmp_path / "ledger-copy.sqlite"
    shutil.copyfile(PROD_LEDGER, copy)
    prod_bytes = PROD_LEDGER.read_bytes()
    with Ledger(copy) as led:
        assert led.verify_chain() == []
        assert led.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE actor='migration'"
        ).fetchone()[0] == 1
    assert PROD_LEDGER.read_bytes() == prod_bytes   # 本体未被触碰


# ================================================ R6/C5：防洗链/删尾/事件归档

def test_write_after_tamper_refused_not_washed(tmp_path):
    """C5 复现 A 的反转断言：篡改历史行 → verify 报 → 之后的**正常写入**
    被 LedgerTamperedError 拒绝（不再 _rechain 把证据洗掉），篡改仍可检出。"""
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        record(led, run_n=1)
        record(led, run_n=2)
        with led.conn:   # 模拟研究账户直连 sqlite 改行（不重算哈希）
            led.conn.execute("UPDATE runs SET actor='hacker' WHERE run_id=1")
        assert led.verify_chain()
        with pytest.raises(LedgerTamperedError):
            record(led, run_n=3)
        problems = led.verify_chain()
        assert any("run#1" in q and "篡改" in q for q in problems)
        n = led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert n == 2                        # 拒绝的写入没落库（证据保住）
    with Ledger(p, create=False) as led2:    # 重开仍报：证据没有被洗掉
        assert any("run#1" in q and "篡改" in q for q in led2.verify_chain())


def test_tampered_gates_or_lockbox_blocks_all_writers(tmp_path):
    """三链任一被篡改 → 所有写方法（record_gate / record_lockbox_start /
    finish_lockbox_use / lockbox_budget / register_card）一律 fail-closed。"""
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        rid = record(led)
        led.record_gate(rid, "G0", True, {})
        with led.conn:
            led.conn.execute(
                "UPDATE gates SET passed=0 WHERE run_id=?", (rid,))
        assert led.verify_chain()
        with pytest.raises(LedgerTamperedError):
            record(led, run_n=2)
        with pytest.raises(LedgerTamperedError):
            led.record_gate(rid, "G1", True, {})
        with pytest.raises(LedgerTamperedError):
            led.record_lockbox_start("H001", approved_by="u")
        with pytest.raises(LedgerTamperedError):
            led.finish_lockbox_use("H001", result={})
        with pytest.raises(LedgerTamperedError):
            led.lockbox_budget("2026-10-01T00:00:00")
        with pytest.raises(LedgerTamperedError):
            led.register_card("H001", 1, "p", "c",
                              datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_tail_deletion_detected_by_snapshot(tmp_path):
    """C5 复现 B 的反转断言：删最后一行（无后继、内容哈希全对）→ verify
    由 chain_meta 快照报"尾部被删"。"""
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        for i in (1, 2, 3):
            record(led, run_n=i)
    conn = sqlite3.connect(str(p))
    conn.execute("DELETE FROM runs WHERE run_id=3")
    conn.commit()
    conn.close()
    with Ledger(p, create=False) as led:
        problems = led.verify_chain()
        assert any("runs" in q and "尾部" in q for q in problems)
        with pytest.raises(LedgerTamperedError):
            record(led, run_n=4)      # 删尾后的写入同样被拒


def test_lockbox_row_deletion_detected_and_still_used(tmp_path):
    """C5 复现 C 的反转断言：删唯一 lockbox_uses 行 → verify 报且
    lockbox_used 仍 True（快照兜底），一次性预算不被重置，重插被拒。"""
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        led.record_lockbox_use("H001", approved_by="u", result={},
                               cutoff="2026-10-01T00:00:00")
    conn = sqlite3.connect(str(p))
    conn.execute("DELETE FROM lockbox_uses")
    conn.commit()
    conn.close()
    with Ledger(p, create=False) as led:
        problems = led.verify_chain()
        assert any("lockbox_uses" in q and ("尾部" in q or "删" in q)
                   for q in problems)
        assert led.lockbox_used("H001") is True   # 快照在：仍算已用
        with pytest.raises(LedgerTamperedError):  # 预算路径同样 fail-closed
            led.lockbox_budget("2026-10-01T00:00:00")
        with pytest.raises(LedgerTamperedError):
            led.record_lockbox_start("H001", approved_by="u")


def test_full_rechain_detected_by_head_snapshot(tmp_path):
    """攻击者改内容并**整链重算**（含 row_hash/prev_hash）→ 行级校验全过，
    但链头与 chain_meta 快照不符 → verify 报（整链重算瞒不过快照/锚定）。"""
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        record(led, run_n=1)
        record(led, run_n=2)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    with conn:                                   # 改一行
        conn.execute("UPDATE runs SET actor='hacker' WHERE run_id=1")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    content_cols = [c for c in cols if c not in ("prev_hash", "row_hash")]
    sel = ", ".join(f'"{c}"' for c in content_cols)
    prev = "0" * 64                              # 手工整链重算（洗链）
    for r in conn.execute(
            f"SELECT rowid AS rid, {sel} FROM runs ORDER BY rowid").fetchall():
        canon = json.dumps({c: r[c] for c in content_cols},
                           sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
        h = hashlib.sha256((canon + prev).encode("utf-8")).hexdigest()
        conn.execute("UPDATE runs SET prev_hash=?, row_hash=? WHERE rowid=?",
                     (prev, h, r["rid"]))
        prev = h
    conn.commit()
    conn.close()
    with Ledger(p, create=False) as led:
        problems = led.verify_chain()
        assert any("runs" in q and "快照" in q for q in problems)
        with pytest.raises(LedgerTamperedError):
            record(led, run_n=3)


def test_forged_append_beyond_snapshot_detected(tmp_path):
    """绕过写方法直接 INSERT（即使哈希算对）→ 行数超过快照 → verify 报。"""
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        record(led, run_n=1)
        tail = led.conn.execute(
            "SELECT row_hash FROM runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["row_hash"]
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    content_cols = [c for c in cols if c not in ("prev_hash", "row_hash",
                                                 "run_id")]
    content = {"card_id": "HX", "card_version": 1, "strategy": "s", "params_json": "{}",
               "symbol": "X", "timeframe": "H1", "data_first": None,
               "data_last": None, "data_sha256": "a" * 64, "strategy_sha": "f" * 64,
               "engine_version": "e", "cost_model_version": "v",
               "gates_hash": "", "metrics_json": "{}",
               "created_utc": "2026-10-01T00:00:00+00:00", "actor": "forger"}
    canon = json.dumps(content, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    h = hashlib.sha256((canon + tail).encode("utf-8")).hexdigest()
    names = ", ".join(f'"{c}"' for c in content_cols)
    ph = ", ".join("?" for _ in content_cols)
    with conn:
        conn.execute(
            f"INSERT INTO runs({names}, prev_hash, row_hash)"
            f" VALUES ({ph}, ?, ?)",
            tuple(content[c] for c in content_cols) + (tail, h))
    conn.close()
    with Ledger(p, create=False) as led:
        problems = led.verify_chain()
        assert any("runs" in q and ("快照" in q or "未登记" in q)
                   for q in problems)


def test_legal_append_extends_chain_incrementally(tmp_path):
    """合法追加只延伸链（增量计算 row_hash）：既有行的 prev_hash/row_hash
    一律不变；新行 prev_hash = 原链尾。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        r1 = record(led, run_n=1)
        before = {r["run_id"]: (r["prev_hash"], r["row_hash"])
                  for r in led.conn.execute("SELECT * FROM runs")}
        r2 = record(led, run_n=2)
        after = {r["run_id"]: (r["prev_hash"], r["row_hash"])
                 for r in led.conn.execute("SELECT * FROM runs")}
        assert after[r1] == before[r1]              # 旧行不被重算
        assert after[r2][0] == before[r1][1]        # 新行接在原链尾上
        assert led.verify_chain() == []


def test_finish_lockbox_use_archives_started_row_in_events(tmp_path):
    """状态更新（started→done）不再悄悄改写：旧行内容进 ledger_events
    （审计附表），行内容重算 row_hash，链保持可验证。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        record(led)
        led.record_lockbox_start("H001", approved_by="u",
                                 cutoff="2026-10-01T00:00:00")
        led.finish_lockbox_use("H001", result={"passed": True})
        assert led.verify_chain() == []
        evs = led.conn.execute(
            "SELECT * FROM ledger_events ORDER BY event_id").fetchall()
        assert len(evs) == 1
        old = json.loads(evs[0]["old_content_json"])
        assert old["status"] == "started"           # 归档的是改写前内容
        assert old["approved_by"] == "u"
        assert led.lockbox_status("H001") == "done"
        row = led.conn.execute(
            "SELECT * FROM lockbox_uses").fetchone()
        assert row["approved_by"] == "u"            # 批准人与时刻保留


def test_record_gate_re_record_archives_previous(tmp_path):
    """gates 重录（原 INSERT OR REPLACE 改写路径）同样归档旧行内容。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        rid = record(led)
        led.record_gate(rid, "G0", False, {"attempt": 1})
        led.record_gate(rid, "G0", True, {"attempt": 2})
        assert led.verify_chain() == []
        row = led.conn.execute("SELECT * FROM gates").fetchone()
        assert json.loads(row["details_json"])["attempt"] == 2
        old = json.loads(led.conn.execute(
            "SELECT old_content_json FROM ledger_events").fetchone()[0])
        assert old["passed"] == 0 and old["details_json"]


def test_events_row_deletion_detected(tmp_path):
    """审计附表 ledger_events 的行被删 → 快照行数对不上 → verify 报。"""
    p = tmp_path / "l.sqlite"
    with Ledger(p) as led:
        rid = record(led)
        led.record_gate(rid, "G0", False, {"a": 1})
        led.record_gate(rid, "G0", True, {"a": 2})   # 产生一条 events
    conn = sqlite3.connect(str(p))
    conn.execute("DELETE FROM ledger_events")
    conn.commit()
    conn.close()
    with Ledger(p, create=False) as led:
        problems = led.verify_chain()
        assert any("ledger_events" in q and "快照" in q for q in problems)


# ----------------------------------------------- R6/C5：锚链（ledger_anchor）

def _anchor_ledger(tmp_path, n=2):
    p = tmp_path / "ledger.sqlite"
    with Ledger(p) as led:
        for i in range(n):
            record(led, run_n=i + 1)
    return p


def test_anchor_rows_form_a_chain(tmp_path):
    """锚定行带前一个锚的哈希（链式锚）：三条锚的 anchor_hash 逐条相链。"""
    mod = load_anchor()
    p = _anchor_ledger(tmp_path, n=2)
    out = tmp_path / "anchors.md"
    assert mod.write_anchor(out, p, now=datetime(2026, 10, 26, tzinfo=timezone.utc))
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines()
             if ln.startswith("|") and "---" not in ln
             and not ln.startswith("| 锚定")]
    assert len(lines) == 3                          # 三表各一条
    hashes = [mod._parse_anchor_line(ln) for ln in lines]
    assert hashes[0]["prev"] == "0" * 64            # 首锚接 GENESIS
    assert hashes[1]["prev"] == hashes[0]["hash"]   # 逐条相链
    assert hashes[2]["prev"] == hashes[1]["hash"]


def test_anchor_verify_clean_then_tampered_file_reported(tmp_path):
    """--verify：干净状态无问题；改锚文件历史行 → 锚链哈希不符 → 报。"""
    mod = load_anchor()
    p = _anchor_ledger(tmp_path, n=2)
    out = tmp_path / "anchors.md"
    mod.write_anchor(out, p)
    with Ledger(p) as led:                          # 增长后再锚一次 → 两条锚
        record(led, run_n=3)
    mod.write_anchor(out, p)
    assert mod.verify_anchor(out, p) == []
    # 模拟对锚文件历史的篡改：把第一条锚（当时 2 行）的行数改大
    text = out.read_text(encoding="utf-8")
    tampered = text.replace("| runs | 2 |", "| runs | 9 |")
    assert tampered != text                         # 夹具自检：确有替换
    out.write_text(tampered, encoding="utf-8")
    problems = mod.verify_anchor(out, p)
    assert any("锚" in q for q in problems)


def test_anchor_verify_flags_ledger_rewrite_after_anchor(tmp_path):
    """锚定后台账被改写（整链重算）或删行 → 与锚不符 → 报。"""
    mod = load_anchor()
    p = _anchor_ledger(tmp_path, n=2)
    out = tmp_path / "anchors.md"
    mod.write_anchor(out, p)
    assert mod.verify_anchor(out, p) == []
    # 1) 锚后删尾（内容哈希全对、无后继断链）→ 行数 < 锚定行数 → 报
    conn = sqlite3.connect(str(p))
    conn.execute("DELETE FROM runs WHERE run_id=2")
    conn.commit()
    conn.close()
    problems = mod.verify_anchor(out, p)
    assert any("runs" in q and "删" in q for q in problems)
    # 2) 锚后改内容并整链重算 → 行数相同但链头不符 → 报
    p2 = _anchor_ledger(tmp_path / "second", n=2)
    out2 = tmp_path / "anchors2.md"
    mod.write_anchor(out2, p2)
    conn = sqlite3.connect(str(p2))
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("UPDATE runs SET actor='hacker' WHERE run_id=1")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
    content_cols = [c for c in cols if c not in ("prev_hash", "row_hash")]
    sel = ", ".join(f'"{c}"' for c in content_cols)
    prev = "0" * 64
    for r in conn.execute(
            f"SELECT rowid AS rid, {sel} FROM runs ORDER BY rowid").fetchall():
        canon = json.dumps({c: r[c] for c in content_cols},
                           sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
        h = hashlib.sha256((canon + prev).encode("utf-8")).hexdigest()
        conn.execute("UPDATE runs SET prev_hash=?, row_hash=? WHERE rowid=?",
                     (prev, h, r["rid"]))
        prev = h
    conn.commit()
    conn.close()
    problems = mod.verify_anchor(out2, p2)
    assert any("runs" in q and "链头" in q for q in problems)


def test_anchor_verify_missing_history_reported(tmp_path):
    """缺历史锚（锚文件无记录 / 表无锚定条目）→ 报。"""
    mod = load_anchor()
    p = _anchor_ledger(tmp_path, n=1)
    out = tmp_path / "anchors.md"
    problems = mod.verify_anchor(out, p)          # 文件不存在
    assert any("锚" in q for q in problems)
    out.write_text(mod._HEADER.strip() + "\n", encoding="utf-8")
    problems = mod.verify_anchor(out, p)          # 只有表头：无锚定记录
    assert any("缺" in q or "无" in q for q in problems)
    # 只锚 runs 一条（人为构造缺表锚定）：其余表 → 缺该表锚定历史
    out.write_text(mod._HEADER.strip() + "\n", encoding="utf-8")
    with open(out, "a", encoding="utf-8") as f:
        heads = mod.chain_heads(p)
        prev = "0" * 64
        ts = "2026-10-26T00:00:00+00:00"
        ah = mod._anchor_digest(ts, "l", "runs", heads["runs"]["rows"],
                                heads["runs"]["head"], prev)
        f.write(f"| {ts} | l | runs | {heads['runs']['rows']} | "
                f"{heads['runs']['head']} | {prev} | {ah} |\n")
    problems = mod.verify_anchor(out, p)
    assert any("gates" in q and "锚定历史" in q for q in problems)


def test_anchor_verify_cli_mode(tmp_path, capsys):
    """CLI：--verify 干净返回 0；锚后改台账返回 1。"""
    mod = load_anchor()
    p = _anchor_ledger(tmp_path, n=2)
    out = tmp_path / "anchors.md"
    mod.write_anchor(out, p)
    assert mod.main(["--ledger", str(p), "--anchor", str(out),
                     "--verify"]) == 0
    conn = sqlite3.connect(str(p))
    conn.execute("DELETE FROM runs WHERE run_id=2")
    conn.commit()
    conn.close()
    assert mod.main(["--ledger", str(p), "--anchor", str(out),
                     "--verify"]) == 1
    assert "删" in capsys.readouterr().out

