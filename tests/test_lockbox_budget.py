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
    """登记一条当前通过的完整试验记录（H-02 起 begin_lockbox 的前置：
    G3 与 V3 都通过，且试验身份 = _trial() 的绑定值）。"""
    rid = led.record_run(
        card_id=card_id, card_version=1, strategy="s:Donchian", params={},
        symbol="XAUUSD", timeframe="H1", data_first="2024-01-01",
        data_last="2026-03-31", data_sha256="a" * 64,
        engine_version="A-1.5", cost_model_version="zero-v0",
        gates_hash="", metrics={"sharpe": 1.0}, actor="test")
    led.record_gate(rid, "G3", True, {})
    led.record_gate(rid, "V3", True, {})
    return rid


def _trial(**over):
    """与 g3_card 登记的试验身份一致的 LockboxTrial（H-02 绑定）。"""
    kw = dict(params={}, data_sha256="a" * 64, cost_model_version="zero-v0",
              strategy_sha="legacy", gates_hash="")
    kw.update(over)
    return vg.LockboxTrial(**kw)


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
                             cutoff=CUTOFF, trial=_trial())
        b = led.lockbox_budget(CUTOFF)
        assert b["used"] == 5 and b["exhausted"] is True
        g3_card(led, "H816")
        with pytest.raises(vg.LockboxBudgetExhaustedError,
                           match="H816.*预算.*5"):
            vg.begin_lockbox(led, _Card("H816"), approved_by="u",
                             cutoff=CUTOFF, trial=_trial())
        # 拒绝不烧该卡的一次性：无 started 行
        assert led.lockbox_used("H816") is False


def test_exhausted_budget_refuses_run_g5_direct_path(tmp_path):
    """直调 run_g5（无 started 行）同样受预算约束，且不落任何行。"""
    import numpy as np
    import pandas as pd

    with Ledger(tmp_path / "l.sqlite") as led:
        led.lockbox_budget(CUTOFF, 1)
        g3_card(led, "H820")
        vg.begin_lockbox(led, _Card("H820"), approved_by="u", cutoff=CUTOFF,
                         trial=_trial())
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
        vg.begin_lockbox(led, _Card("H830"), approved_by="u", cutoff=CUTOFF,
                         trial=_trial())
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
        vg.begin_lockbox(led, _Card("H820"), approved_by="u",
                         trial=_trial())                       # 无 cutoff
        g3_card(led, "H831")
        vg.begin_lockbox(led, _Card("H831"), approved_by="u",
                         trial=_trial())                       # 不受预算约束
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


# ------------------------------------------ H-03：预算检查与占位是一个事务

from concurrent.futures import ThreadPoolExecutor

from mt5quant.research import ledger as _lm


def test_h03_race_last_budget_slot_single_winner(tmp_path):
    """H-03 失败测试：两个连接竞争最后一个额度（max_uses=1）——必须一个
    成功一个拒绝；审核复现的 used=2/max=1 不允许再出现；链保持可验证。"""
    p = tmp_path / "race.sqlite"
    with Ledger(p) as led:
        led.lockbox_budget(CUTOFF, 1)
        g3_card(led, "H901")
        g3_card(led, "H902")

    def reserve(card_id):
        with Ledger(p) as led:
            try:
                led.reserve_lockbox_use(card_id, approved_by="u",
                                        cutoff=CUTOFF)
                return card_id, None
            except Exception as e:                     # noqa: BLE001
                return card_id, type(e).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ["H901", "H902"]))
    ok = [c for c, e in outcomes if e is None]
    refused = [e for _c, e in outcomes if e is not None]
    assert len(ok) == 1, outcomes
    assert refused == ["LockboxProjectBudgetExhaustedError"], outcomes
    with Ledger(p) as led:
        b = led.lockbox_budget(CUTOFF)
        assert (b["used"], b["max_uses"]) == (1, 1)    # 绝不 used=2/max=1
        assert led.verify_chain() == []


def test_h03_crash_restart_does_not_double_reserve(tmp_path):
    """H-03：占位持久（started 已入链）——"崩溃重启"后同一卡的再次占位
    被拒，预算计数仍是 1。"""
    p = tmp_path / "crash.sqlite"
    with Ledger(p) as led:
        led.lockbox_budget(CUTOFF, 2)
        led.reserve_lockbox_use("H903", approved_by="u", cutoff=CUTOFF)
    with Ledger(p) as led:                             # 新连接 = 重启
        with pytest.raises(_lm.LockboxCardUsedError):
            led.reserve_lockbox_use("H903", approved_by="u", cutoff=CUTOFF)
        assert led.lockbox_budget(CUTOFF)["used"] == 1
        assert led.lockbox_status("H903") == "started"


def test_h03_same_trial_retry_is_idempotent_single_slot(tmp_path):
    """H-03：同试验幂等重试——第二次 reserve 拒绝且不重复占位（used 与
    started 行数都不翻倍）。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        led.lockbox_budget(CUTOFF, 5)
        led.reserve_lockbox_use("H904", approved_by="u", cutoff=CUTOFF)
        for _ in range(2):
            with pytest.raises(_lm.LockboxCardUsedError):
                led.reserve_lockbox_use("H904", approved_by="u",
                                        cutoff=CUTOFF)
        assert led.lockbox_budget(CUTOFF)["used"] == 1
        n = led.conn.execute(
            "SELECT COUNT(*) n FROM lockbox_uses").fetchone()["n"]
        assert n == 1
        assert led.verify_chain() == []


def test_h03_round_cap_enforced_inside_reservation_txn(tmp_path):
    """H-03：轮次锁箱上限（lockbox_uses_max）在同一占位事务内复查——
    同轮已用满 → 拒且不留 started 行（不再依赖运行前的 check-then-act）。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        led.record_run(card_id="H905", card_version=1, strategy="s:X",
                       params={}, symbol="XAUUSD", timeframe="H1",
                       data_first="2024-01-01", data_last="2026-01-01",
                       data_sha256="a" * 64, engine_version="A-1",
                       cost_model_version="zero-v0", gates_hash="",
                       metrics={"net": 1.0}, actor="test", round_id="R1")
        led.record_lockbox_use("H905", approved_by="u", result={})
        led.lockbox_budget(CUTOFF, 5)
        with pytest.raises(_lm.LockboxRoundCapExceededError):
            led.reserve_lockbox_use("H906", approved_by="u", cutoff=CUTOFF,
                                    round_id="R1", round_lockbox_uses_max=1)
        assert led.lockbox_used("H906") is False
        assert led.lockbox_budget(CUTOFF)["used"] == 0    # 预算未扣


def test_h03_begin_lockbox_maps_reservation_errors(tmp_path):
    """H-03/H-02 集成：begin_lockbox 走原子占位——预算用尽映射回
    LockboxBudgetExhaustedError（消息含卡号与计数），且不烧该卡一次性。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        led.lockbox_budget(CUTOFF, 1)
        g3_card(led, "H907")
        g3_card(led, "H908")
        vg.begin_lockbox(led, _Card("H907"), approved_by="u", cutoff=CUTOFF,
                         trial=_trial())
        with pytest.raises(vg.LockboxBudgetExhaustedError,
                           match="H908.*预算.*1"):
            vg.begin_lockbox(led, _Card("H908"), approved_by="u",
                             cutoff=CUTOFF, trial=_trial())
        assert led.lockbox_used("H908") is False
        assert led.lockbox_budget(CUTOFF)["used"] == 1
