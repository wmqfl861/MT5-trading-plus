"""T27/M11：锁箱使用预算（D16 未决，默认 max_uses=5）。

结构与检查点：
- ledger.lockbox_budget(cutoff, max_uses=None)：登记（幂等）并返回
  {cutoff, max_uses, used, remaining, exhausted}；首次登记写入 max_uses
  （缺省 DEFAULT_LOCKBOX_MAX_USES=5），之后表内值权威；
- 计数口径：lockbox_uses 中 cutoff 匹配的行（含 started）——旧库 NULL 行
  属既往锁箱（预算制度前），不计入当前 cutoff；
- G5 拒绝点：begin_lockbox 在读锁箱数据**之前**检查（用尽 → 抛
  LockboxBudgetExhaustedError，不烧该卡的一次性）；run_g5 的直调路径
  （该卡无 started 行）同样检查；begin 已消耗预算的卡 run_g5 不再复查
  （否则会把自己拒掉）。
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from mt5quant.research import ledger as ledger_mod
from mt5quant.research.ledger import Ledger
from mt5quant.validation import gates as vg

CUTOFF = "2026-04-01T00:00:00"


def g3_card(led, card_id):
    """登记一条 G3 通过记录（begin_lockbox 的前置）。"""
    rid = led.record_run(
        card_id=card_id, card_version=1, strategy="s:Donchian", params={},
        symbol="XAUUSD", timeframe="H1", data_first="2024-01-01",
        data_last="2026-03-31", data_sha256="a" * 64,
        engine_version="A-1.5", cost_model_version="zero-v0",
        gates_hash="", metrics={"sharpe": 1.0}, actor="test")
    led.record_gate(rid, "G3", True, {})
    return rid


class _Card:
    def __init__(self, card_id, timeframe="H1"):
        self.id = card_id
        self.timeframe = timeframe


# ---------------------------------------------------------------- 登记语义


def test_budget_registers_with_default_five(tmp_path):
    """未传 max_uses → 默认 5（D16 未决前的硬编码默认，可参数覆盖）。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        b = led.lockbox_budget(CUTOFF)
        assert b == {"cutoff": CUTOFF, "max_uses": 5, "used": 0,
                     "remaining": 5, "exhausted": False}


def test_budget_first_call_max_uses_wins_and_is_idempotent(tmp_path):
    """首次登记的 max_uses 生效；之后的调用（哪怕传了别的值）不覆盖——
    调高预算是管理员动作（改库），不是代码缺省。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        b = led.lockbox_budget(CUTOFF, 2)
        assert b["max_uses"] == 2 and b["remaining"] == 2
        b2 = led.lockbox_budget(CUTOFF, 99)
        assert b2["max_uses"] == 2          # 表内值权威
        b3 = led.lockbox_budget(CUTOFF)
        assert b3["max_uses"] == 2


def test_budget_accepts_datetime_cutoff_normalized(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        b = led.lockbox_budget(datetime(2026, 4, 1))
        assert b["cutoff"] == CUTOFF
        assert led.lockbox_budget(CUTOFF)["max_uses"] == 5  # 同一 cutoff


def test_budget_rejects_nonpositive_max_uses(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        with pytest.raises(ValueError, match="max_uses"):
            led.lockbox_budget(CUTOFF, 0)
        with pytest.raises(ValueError, match="max_uses"):
            led.lockbox_budget(CUTOFF, -1)


def test_budget_counts_uses_per_cutoff_started_included(tmp_path):
    """used 按 cutoff 匹配计数；started 行同样占用预算；旧库 NULL 行
    （既往锁箱/预算制度前）不计入当前 cutoff。

    R6/C5：NULL cutoff 行以旧库形态**先建库再打开**（经迁移接管快照）——
    台账打开后的裸 INSERT 属于绕过写方法的写入，会被防洗链校验拒绝
    （见 test_research_ledger 的未登记写入用例）。"""
    p = tmp_path / "l.sqlite"
    con = sqlite3.connect(p)
    con.executescript("""
    CREATE TABLE runs(
      run_id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL,
      card_version INTEGER NOT NULL, strategy TEXT NOT NULL,
      params_json TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
      data_first TEXT, data_last TEXT, data_sha256 TEXT NOT NULL,
      engine_version TEXT NOT NULL, cost_model_version TEXT NOT NULL,
      gates_hash TEXT NOT NULL DEFAULT '', metrics_json TEXT NOT NULL,
      created_utc TEXT NOT NULL, actor TEXT NOT NULL);
    CREATE TABLE lockbox_uses(
      card_id TEXT NOT NULL UNIQUE, used_at_utc TEXT NOT NULL,
      approved_by TEXT NOT NULL, result_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'done');
    """)
    with con:
        con.execute(
            "INSERT INTO lockbox_uses(card_id, used_at_utc, approved_by,"
            " result_json, status) VALUES ('H799', '2026-01-01T00:00:00+00:00',"
            " 'u', '{}', 'done')")
    con.close()
    with Ledger(p) as led:                       # 打开迁移接管旧行（cutoff NULL）
        g3_card(led, "H801")
        g3_card(led, "H802")
        led.lockbox_budget(CUTOFF)
        led.record_lockbox_start("H801", approved_by="u", cutoff=CUTOFF)
        b = led.lockbox_budget(CUTOFF)
        assert b["used"] == 1 and b["exhausted"] is False
        led.record_lockbox_start("H802", approved_by="u", cutoff=CUTOFF)
        b = led.lockbox_budget(CUTOFF)
        assert b["used"] == 2 and b["remaining"] == 3   # H799（NULL）不计入
        # 另一个 cutoff 各有各的预算（退役旧锁箱 → 新 cutoff 重置）
        b2 = led.lockbox_budget("2026-10-01T00:00:00")
        assert b2["used"] == 0 and b2["remaining"] == 5


# ---------------------------------------------------------------- G5 拒绝点


def test_sixth_use_refused_after_five_uses_default_budget(tmp_path):
    """计划失败测试（T27）：默认预算 5 次——第 6 次 G5 拒绝。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        led.lockbox_budget(CUTOFF)
        for i in range(1, 6):               # 5 张卡各用一次
            g3_card(led, f"H81{i}")
            vg.begin_lockbox(led, _Card(f"H81{i}"), approved_by="u",
                             cutoff=CUTOFF)
        b = led.lockbox_budget(CUTOFF)
        assert b["used"] == 5 and b["exhausted"] is True
        g3_card(led, "H816")
        with pytest.raises(vg.LockboxBudgetExhaustedError,
                           match="H816.*预算.*5"):
            vg.begin_lockbox(led, _Card("H816"), approved_by="u",
                             cutoff=CUTOFF)
        # 拒绝不烧该卡的一次性：无 started 行
        assert led.lockbox_used("H816") is False


def test_exhausted_budget_refuses_run_g5_direct_path(tmp_path):
    """直调 run_g5（无 started 行）同样受预算约束，且不落任何行。"""
    import numpy as np
    import pandas as pd

    with Ledger(tmp_path / "l.sqlite") as led:
        led.lockbox_budget(CUTOFF, 1)
        g3_card(led, "H820")
        vg.begin_lockbox(led, _Card("H820"), approved_by="u", cutoff=CUTOFF)
        g3_card(led, "H821")
        with pytest.raises(vg.LockboxBudgetExhaustedError, match="H821"):
            vg.run_g5(
                led, _Card("H821"),
                lockbox_result={"expectancy": 5.0, "sharpe": 2.0,
                                "trade_count": 40, "bar_count": 11_250,
                                "annualization_bars_per_year": 6000.0},
                reference_returns=pd.Series(
                    7e-5 + np.random.default_rng(1).normal(0, 0.004, 30_000)),
                expected_trades=40.0, approved_by="u",
                cfg=vg.load_gates()[0], bars_per_day=24, cutoff=CUTOFF)
        assert led.lockbox_used("H821") is False


def test_run_g5_does_not_recheck_budget_for_started_card(tmp_path):
    """begin 已消耗预算槽的卡：run_g5 不再复查（否则把自己拒掉）。
    场景：预算仅剩 1，H830 begin 占用（used==max）；此时 H830 的 run_g5
    必须照常评估收尾。"""
    import numpy as np
    import pandas as pd

    with Ledger(tmp_path / "l.sqlite") as led:
        led.lockbox_budget(CUTOFF, 1)
        g3_card(led, "H830")
        vg.begin_lockbox(led, _Card("H830"), approved_by="u", cutoff=CUTOFF)
        assert led.lockbox_budget(CUTOFF)["exhausted"] is True
        out = vg.run_g5(
            led, _Card("H830"),
            lockbox_result={"expectancy": 5.0, "sharpe": 2.0,
                            "trade_count": 40, "bar_count": 11_250,
                            "annualization_bars_per_year": 6000.0},
            reference_returns=pd.Series(
                7e-5 + np.random.default_rng(2).normal(0, 0.004, 30_000)),
            expected_trades=40.0, approved_by="u",
            cfg=vg.load_gates()[0], bars_per_day=24, cutoff=CUTOFF)
        assert out["passed"] is True
        assert led.lockbox_status("H830") == "done"
        row = led.conn.execute(
            "SELECT cutoff FROM lockbox_uses WHERE card_id='H830'"
        ).fetchone()
        assert row["cutoff"] == CUTOFF           # 使用行带上 cutoff 口径


def test_run_g5_without_cutoff_skips_budget(tmp_path):
    """cutoff 未给（旧调用方/测试路径）→ 不做预算检查（向后兼容）。"""
    import numpy as np
    import pandas as pd

    with Ledger(tmp_path / "l.sqlite") as led:
        led.lockbox_budget(CUTOFF, 1)
        g3_card(led, "H820")
        vg.begin_lockbox(led, _Card("H820"), approved_by="u")  # 无 cutoff
        g3_card(led, "H831")
        vg.begin_lockbox(led, _Card("H831"), approved_by="u")  # 不受预算约束
        assert led.lockbox_used("H831") is True


# ---------------------------------------------------------------- 旧库迁移


def test_old_ledger_db_migrates_cutoff_column_and_budget_table(tmp_path):
    """旧库（lockbox_uses 无 cutoff 列、无 lockbox_budget 表）打开即迁移；
    旧行 cutoff=NULL 不计入当前 cutoff 的预算。"""
    p = tmp_path / "old.sqlite"
    con = sqlite3.connect(p)
    con.executescript("""
    CREATE TABLE runs(
      run_id INTEGER PRIMARY KEY AUTOINCREMENT, card_id TEXT NOT NULL,
      card_version INTEGER NOT NULL, strategy TEXT NOT NULL,
      params_json TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
      data_first TEXT, data_last TEXT, data_sha256 TEXT NOT NULL,
      engine_version TEXT NOT NULL, cost_model_version TEXT NOT NULL,
      gates_hash TEXT NOT NULL DEFAULT '', metrics_json TEXT NOT NULL,
      created_utc TEXT NOT NULL, actor TEXT NOT NULL);
    CREATE TABLE lockbox_uses(
      card_id TEXT NOT NULL UNIQUE, used_at_utc TEXT NOT NULL,
      approved_by TEXT NOT NULL, result_json TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'done');
    """)
    with con:
        con.execute(
            "INSERT INTO lockbox_uses(card_id, used_at_utc, approved_by,"
            " result_json, status) VALUES ('H700', '2026-01-01', 'u', '{}',"
            " 'done')")
    con.close()

    with Ledger(p) as led:
        cols = {r["name"] for r in led.conn.execute(
            "PRAGMA table_info(lockbox_uses)")}
        assert "cutoff" in cols
        tables = {r["name"] for r in led.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "lockbox_budget" in tables
        b = led.lockbox_budget(CUTOFF)
        assert b["used"] == 0 and b["max_uses"] == 5   # 旧行 NULL 不计入


def test_default_max_uses_constant_is_five():
    """D16 未决前的默认值钉死为 5（蓝图 §8 建议），写死在 ledger 模块。"""
    assert ledger_mod.DEFAULT_LOCKBOX_MAX_USES == 5


def test_record_lockbox_use_accepts_cutoff(tmp_path):
    """直写终态路径（record_lockbox_use / finish_lockbox_use）也带 cutoff。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        led.lockbox_budget(CUTOFF)
        led.record_lockbox_use("H840", approved_by="u", result={},
                               cutoff=CUTOFF)
        led.record_lockbox_start("H841", approved_by="u", cutoff=CUTOFF)
        led.finish_lockbox_use("H841", result={"passed": True},
                               cutoff=CUTOFF)
        assert led.lockbox_budget(CUTOFF)["used"] == 2
