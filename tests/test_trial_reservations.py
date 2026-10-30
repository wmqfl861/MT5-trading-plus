"""X-01（全局审核 2026-10-30）：计算前共享试验占位（reserve_trial）。

结构与检查点（镜像 tests/test_lockbox_budget.py 的 H-03 风格）：
- Ledger.reserve_trial(round_id, trial_key, *, budget=None, actor="bt")：
  "本轮 reservation 计数 ≥ budget 的检查 + started 写入"在同一个
  BEGIN IMMEDIATE 事务内；超限 → TrialBudgetExceededError（不写任何行）；
  同 (round_id, trial_key) 已存在且非 voided → 幂等返回现有行（不占新
  额度——崩溃重试/screen→gate 同身份推进不被"已占自己的最后额度"堵死，
  审核 bug"同身份重试反被满额拒绝"不允许复现）；budget 缺省从已注册
  rounds 行读取预注册 trial_budget；
- advance_trial_stage(reservation_id, stage)：只允许前进方向
  started→reached_primary→completed|failed；voided 仅自 started（未达
  主检验）——**到达主检验后失败/崩溃/作废都不回收 N**；阶段改写经
  ledger_events 归档留痕（旧行可重放）；
- round_n_trials(round_id)：新权威口径 = 到达 started 即计（D55"每条
  到达主检验的假设计一次"，按审核提示词以 started 计、reached_primary
  只是阶段标记；voided 释放名额）；无任何 reservation 的旧轮次回退
  round_n_dedup（round_trial_regime 显式标记两套口径）；
- record_run(round_trial_budget=...)：本轮存在 reservation（新口径生效）
  时成功行不再做预算**拒绝**（权威在 reservation），但做对账——成功行
  去重数 > 上限 → TrialReservationConsistencyError、事务整体回滚（防
  双计/防绕过占位）；无 reservation 的旧轮次 T99 语义逐位不变。
"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from mt5quant.research import ledger as ledger_mod
from mt5quant.research.ledger import Ledger


def _open_round(led, round_id="R1", trial_budget=1):
    """注册一个最小合法轮次（reservation 的预算来源）。"""
    led.record_round_open(
        round_id=round_id, candidates=["C101"], trial_budget=trial_budget,
        lockbox_uses_max=0, dsr_trial_scope="project",
        advance_rule="a", close_rule="c", approved_by="u",
        evaluation={}, toml_sha256="0" * 64, git_commit="0" * 40)


def _run_kw(**over):
    """一条最小合法 runs 行的公共参数（七元组由 params/strategy 区分）。"""
    base = dict(
        card_id="H001-v1", card_version=1, strategy="h-strat",
        params={"combo": "a"}, symbol="XAUUSD", timeframe="H1",
        data_first="2020-01-01T00:00:00+00:00",
        data_last="2020-12-31T00:00:00+00:00",
        data_sha256="a" * 64, engine_version="e",
        cost_model_version="v", gates_hash="", metrics={"net": 1.0},
        actor="bt", strategy_sha="f" * 40, round_id="R1",
        run_class="research")
    base.update(over)
    return base


# ------------------------------------------- 占位事务：原子性与幂等重试


def test_x01_race_last_trial_slot_single_winner(tmp_path):
    """X-01 失败测试：两连接竞争最后一个名额（budget=1）——恰一个占位
    成功，另一个 TrialBudgetExceededError（镜像 H-03 并发测试）；链完好。"""
    p = tmp_path / "race.sqlite"
    with Ledger(p) as led:
        _open_round(led, "R1", trial_budget=1)

    def reserve(tag):
        with Ledger(p) as led:
            try:
                r = led.reserve_trial("R1", f"key-{tag}", budget=1, actor="bt")
                return tag, r["stage"], None
            except Exception as e:                     # noqa: BLE001
                return tag, None, type(e).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ["w1", "w2"]))
    ok = [t for t, _s, e in outcomes if e is None]
    refused = [e for _t, _s, e in outcomes if e is not None]
    assert len(ok) == 1, outcomes
    assert refused == ["TrialBudgetExceededError"], outcomes
    with Ledger(p) as led:
        assert led.round_n_trials("R1") == 1
        assert led.verify_chain() == []


def test_x01_same_key_idempotent_single_slot_other_keys_each_occupy(tmp_path):
    """同 trial_key 幂等重试只占一额——**含预算已满时**（审核 bug"同身份
    重试反被满额拒绝"不允许复现）；不同 trial_key 各占各的额。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        _open_round(led, "R1", trial_budget=2)
        r1 = led.reserve_trial("R1", "key-A", budget=2, actor="bt")
        assert r1["stage"] == "started"
        for _ in range(2):                     # 崩溃重试：同键幂等返回同一行
            r2 = led.reserve_trial("R1", "key-A", budget=2, actor="bt")
            assert r2["reservation_id"] == r1["reservation_id"]
        assert led.round_n_trials("R1") == 1   # 只占一额
        led.reserve_trial("R1", "key-B", budget=2, actor="bt")
        assert led.round_n_trials("R1") == 2   # 不同键各占
        # 预算已满：新键被拒；同键 A 的重试仍幂等成功（不被自己堵死）
        with pytest.raises(ledger_mod.TrialBudgetExceededError):
            led.reserve_trial("R1", "key-C", budget=2, actor="bt")
        r3 = led.reserve_trial("R1", "key-A", budget=2, actor="bt")
        assert r3["reservation_id"] == r1["reservation_id"]
        n = led.conn.execute(
            "SELECT COUNT(*) n FROM trial_reservations").fetchone()["n"]
        assert n == 2
        assert led.verify_chain() == []


def test_x01_budget_defaults_to_registered_round(tmp_path):
    """budget 缺省 → 从已注册轮次的 rounds.trial_budget（预注册申报值）
    读取；轮次未注册且未给 budget → ValueError（占位必须有明确预算来源）；
    显式非正 budget → ValueError。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        _open_round(led, "R2", trial_budget=2)
        led.reserve_trial("R2", "k1", actor="bt")       # 无 budget 参数
        led.reserve_trial("R2", "k2", actor="bt")
        with pytest.raises(ledger_mod.TrialBudgetExceededError):
            led.reserve_trial("R2", "k3", actor="bt")
        with pytest.raises(ValueError, match="未在 rounds 注册"):
            led.reserve_trial("R-unknown", "k1", actor="bt")
        with pytest.raises(ValueError, match="budget 必须为正"):
            led.reserve_trial("R2", "k4", budget=0, actor="bt")


# ------------------------------------------- 阶段机：方向约束与 N 不可回收


def test_x01_stage_forward_only_primary_n_not_reclaimed(tmp_path):
    """阶段只进不退：started→reached_primary→completed|failed；同阶段重推
    幂等 no-op；跳过 reached_primary 不允许；**到达主检验后 voided 被拒、
    failed 不回收 N**；voided 仅自 started（未达主检验）且释放名额；
    终态不可再改；改写经 ledger_events 归档留痕。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        _open_round(led, "R1", trial_budget=5)
        ra = led.reserve_trial("R1", "key-A", budget=5, actor="bt")
        rid_a = ra["reservation_id"]
        # (a) 幂等推进（同阶段 no-op）
        assert led.advance_trial_stage(rid_a, "started")["stage"] == "started"
        # (b) started 直达 completed 不允许（必须先 reached_primary）
        with pytest.raises(ValueError, match="阶段"):
            led.advance_trial_stage(rid_a, "completed")
        # (c) 到达主检验后 voided 被拒——N 不可通过作废回收
        led.advance_trial_stage(rid_a, "reached_primary")
        assert led.round_n_trials("R1") == 1
        with pytest.raises(ValueError, match="主检验"):
            led.advance_trial_stage(rid_a, "voided")
        # (d) 失败不回收 N；终态不可再推进（含回退）
        led.advance_trial_stage(rid_a, "failed")
        assert led.trial_reservation("R1", "key-A")["stage"] == "failed"
        assert led.round_n_trials("R1") == 1
        for bad in ("started", "reached_primary", "completed", "voided"):
            with pytest.raises(ValueError, match="阶段"):
                led.advance_trial_stage(rid_a, bad)
        assert led.round_n_trials("R1") == 1
        # (e) voided 仅自 started（未达主检验）：释放名额，可被新键占用
        rb = led.reserve_trial("R1", "key-B", budget=5, actor="bt")
        led.advance_trial_stage(rb["reservation_id"], "voided")
        assert led.round_n_trials("R1") == 1
        led.reserve_trial("R1", "key-C", budget=5, actor="bt")
        assert led.round_n_trials("R1") == 2
        # (f) completed 也是终态：reached_primary→completed 后不可再改
        rc = led.reserve_trial("R1", "key-D", budget=5, actor="bt")
        led.advance_trial_stage(rc["reservation_id"], "reached_primary")
        led.advance_trial_stage(rc["reservation_id"], "completed")
        with pytest.raises(ValueError, match="阶段"):
            led.advance_trial_stage(rc["reservation_id"], "failed")
        # (g) 未知 reservation / 未知阶段
        with pytest.raises(ValueError, match="阶段"):
            led.advance_trial_stage(rid_a, "exploded")
        with pytest.raises(ValueError, match="不存在"):
            led.advance_trial_stage(999999, "completed")
        # (h) 阶段改写经归档留痕（ledger_events 可重放"改写前是什么"）
        ev = led.conn.execute(
            "SELECT COUNT(*) n FROM ledger_events WHERE"
            " table_name='trial_reservations'").fetchone()["n"]
        assert ev >= 4
        assert led.verify_chain() == []


def test_x01_voided_key_re_reserve_reactivates_after_budget_check(tmp_path):
    """voided（未达主检验）的键重新占位 = 重新过预算检查后复活为 started
    （同 reservation_id、created_utc 不变，改写归档留痕）；预算满时复活
    被拒——释放的名额不是该键的私有保留。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        _open_round(led, "R1", trial_budget=1)
        r = led.reserve_trial("R1", "key-A", budget=1, actor="bt")
        led.advance_trial_stage(r["reservation_id"], "voided")
        assert led.round_n_trials("R1") == 0
        # 释放的名额被别的键占走 → key-A 复活需重新过预算检查 → 拒
        rb = led.reserve_trial("R1", "key-B", budget=1, actor="bt")
        with pytest.raises(ledger_mod.TrialBudgetExceededError):
            led.reserve_trial("R1", "key-A", budget=1, actor="bt")
        # 名额再次空出后 key-A 复活成功：同 reservation_id、stage=started
        led.advance_trial_stage(rb["reservation_id"], "voided")
        r2 = led.reserve_trial("R1", "key-A", budget=1, actor="bt")
        assert r2["reservation_id"] == r["reservation_id"]
        assert r2["stage"] == "started"
        assert r2["created_utc"] == r["created_utc"]
        assert led.round_n_trials("R1") == 1
        n = led.conn.execute(
            "SELECT COUNT(*) n FROM trial_reservations").fetchone()["n"]
        assert n == 2
        assert led.verify_chain() == []


# ------------------------------------------- 口径并存：旧轮次回退与新口径


def test_x01_legacy_round_falls_back_to_round_n_dedup(tmp_path):
    """旧轮次（无任何 reservation）回退 round_n_dedup 口径
    （round_trial_regime='legacy_dedup' 显式标记）；T99 的 record_run
    预算拒绝语义逐位不变（成功行超限被拒、不写任何行）。"""
    with Ledger(tmp_path / "l.sqlite") as led:
        _open_round(led, "R9", trial_budget=1)
        led.record_run(round_trial_budget=1, **_run_kw(round_id="R9"))
        assert led.round_trial_regime("R9") == "legacy_dedup"
        assert led.round_n_trials("R9") == led.round_n_dedup("R9") == 1
        with pytest.raises(ledger_mod.RoundTrialBudgetExceededError):
            led.record_run(round_trial_budget=1,
                           **_run_kw(round_id="R9", params={"combo": "b"},
                                     strategy="h2"))
        assert led.round_n_trials("R9") == 1


def test_x01_new_regime_record_run_not_blocked_but_reconciled(tmp_path):
    """新口径轮（本轮存在 reservation，round_trial_regime='reservations'）：
    成功行不再被 round_trial_budget 拒——screen 与 gate 同身份的第二次
    成功行（旧口径 used=1>=1 会被拒）放行；但**绕过占位的新身份**成功行
    使去重数超过上限 → TrialReservationConsistencyError、事务整体回滚
    （防双计）；不带 kwarg 不做任何检查（模块 C 接线后的常态）。"""
    p = tmp_path / "new.sqlite"
    with Ledger(p) as led:
        _open_round(led, "R1", trial_budget=1)
        led.reserve_trial("R1", "key-A", budget=1, actor="bt")
        assert led.round_trial_regime("R1") == "reservations"
        # screen：同身份第一次成功行
        led.record_run(round_trial_budget=1, **_run_kw())
        # gate：同身份（同七元组）第二次成功行——新口径放行（去重数仍 1）
        rid = led.record_run(round_trial_budget=1, **_run_kw())
        assert rid > 0
        assert led.round_n_dedup("R1") == 1
        # 对账：未占位的新身份把去重成功数推到 2>1 → 一致性错误、整体回滚
        n_before = led.conn.execute(
            "SELECT COUNT(*) n FROM runs").fetchone()["n"]
        with pytest.raises(ledger_mod.TrialReservationConsistencyError):
            led.record_run(round_trial_budget=1,
                           **_run_kw(params={"combo": "unreserved"},
                                     strategy="h-unreserved"))
        n_after = led.conn.execute(
            "SELECT COUNT(*) n FROM runs").fetchone()["n"]
        assert n_after == n_before
        assert led.round_n_dedup("R1") == 1
        # 不带 kwarg：无检查（权威在 reservation——模块 C 接线后的常态）
        rid2 = led.record_run(**_run_kw(params={"combo": "norecon"},
                                        strategy="h-norecon"))
        assert rid2 > 0
        assert led.verify_chain() == []


# ------------------------------------------- 防篡改：并入既有哈希链


def test_x01_reservations_chained_and_tamper_detected(tmp_path):
    """trial_reservations 并入既有哈希链：正常读改后 verify_chain 干净；
    绕过写方法直接改写 stage → verify 报出，后续占位被 LedgerTamperedError
    拒（fail-closed，防洗链）。"""
    p = tmp_path / "tamper.sqlite"
    with Ledger(p) as led:
        _open_round(led, "R1", trial_budget=2)
        r = led.reserve_trial("R1", "key-A", budget=2, actor="bt")
        led.advance_trial_stage(r["reservation_id"], "reached_primary")
        assert led.verify_chain() == []
    import sqlite3
    con = sqlite3.connect(p)
    with con:                                  # 绕过写方法的改写（洗 N）
        con.execute("UPDATE trial_reservations SET stage='voided'"
                    " WHERE trial_key='key-A'")
    con.close()
    with Ledger(p) as led:
        problems = led.verify_chain()
        assert any("trial_reservations" in s for s in problems), problems
        with pytest.raises(ledger_mod.LedgerTamperedError):
            led.reserve_trial("R1", "key-B", budget=2, actor="bt")
