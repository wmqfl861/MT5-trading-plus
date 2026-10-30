"""T99/P-10 跨线集成回归（全局审核 2026-10-29）。

覆盖审核 P-10 的验证点：
1. 原子试验预算（镜像 H-03 reserve_lockbox_use 的并发测试）——两个连接
   竞争最后一个名额只有一个能提交；error 行不检查不消耗；不带 kwarg
   （H 路径）不检查；round_id=None 不检查；非正上限 ValueError。
2. P 基准/关卡政策一致——P2 决策卡经 screen 的 session 政策来自
   load_session_policy(SESSIONS_PATH) 与 entry_policy_for（与 P1/prob
   test、cmd_fit、cmd_gate 同一来源与同一构造）。
3. fittable 策略的 G3 在 T73B 之前保留拒绝（e2e 级：gate 请求 G3 →
   退出码 1、零 runs）。

全部合成数据、临时 git 仓库与临时台账；不读研究库。
"""
import contextlib
import importlib.util
import io
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "scripts")]

_spec = importlib.util.spec_from_file_location("bt_p10", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
sys.modules["bt_p10"] = BT
_spec.loader.exec_module(BT)

from mt5quant.research.ledger import (                        # noqa: E402
    Ledger, RoundTrialBudgetExceededError,
)

from tests.test_prob_p2_e2e import _card_path, env as p2_env  # noqa: E402

STRATEGY_REF = ("mt5quant.strategies.library.prob.level_decision:"
                "LevelDecisionS1Demo")


def _run_kw(**over):
    """一条最小合法 runs 行的公共参数（七元组由 params/strategy 区分）。"""
    base = dict(
        card_id="H001-v1", card_version=1, strategy="h-strat",
        params={"combo": "a"}, symbol="XAUUSD", timeframe="H1",
        data_first="2020-01-01T00:00:00+00:00",
        data_last="2020-12-31T00:00:00+00:00",
        data_sha256="a" * 64, engine_version="e",
        cost_model_version="v", gates_hash="", metrics={"net": 1.0},
        actor="bt", strategy_sha="f" * 40, round_id="R9",
        run_class="research")
    base.update(over)
    return base


def test_t103_legacy_precheck_skips_reservation_regime(tmp_path):
    """批 1 复审 A1（X-01）：旧预检只服务 legacy_dedup 口径轮——
    reservation 口径轮即便旧口径计数已满也必须放行，交由 reserve_trial
    计算前判定（同身份幂等推进 screen→gate、新身份拒绝且零计算）；
    否则"自己占满最后一格"的卡会被堵死。"""
    p = tmp_path / "regime.sqlite"
    with Ledger(p) as led:
        led.record_round_open(
            round_id="R9", candidates=["C101"], trial_budget=1,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="u",
            evaluation={}, toml_sha256="0" * 64, git_commit="0" * 40)
        # 空轮先占位（此时回退计数 0/1 放行）→ completed → 写成功行
        resv = led.reserve_trial("R9", "t-abc", actor="bt")
        led.advance_trial_stage(resv["reservation_id"], "reached_primary")
        led.advance_trial_stage(resv["reservation_id"], "completed")
        led.record_run(**_run_kw())               # 旧口径计数也 1/1（已满）
        assert led.round_n_dedup("R9") == 1
        assert led.round_trial_regime("R9") == "reservations"
        ctx = {"round_id": "R9", "trial_budget": 1, "lockbox_uses_max": 0}
        assert BT._round_budget_exceeded(led, ctx) is False
        # 同身份幂等推进不受预算影响（screen→gate 不被自己堵死）；
        # 新身份在计算前被拒
        again = led.reserve_trial("R9", "t-abc", actor="bt")
        assert again["reservation_id"] == resv["reservation_id"]
        import pytest as _pytest
        from mt5quant.research.ledger import TrialBudgetExceededError
        with _pytest.raises(TrialBudgetExceededError):
            led.reserve_trial("R9", "t-new", actor="bt")


# ------------------------------------------------------- 1. 原子试验预算
def test_p10_race_last_trial_slot_single_winner(tmp_path):
    """P-10：两连接竞争最后一个试验名额（trial_budget=1、两个不同七元组）
    ——恰一个成功，另一个 RoundTrialBudgetExceededError；链可验证。"""
    p = tmp_path / "race.sqlite"
    with Ledger(p) as led:
        led.record_round_open(
            round_id="R9", candidates=["C101"], trial_budget=1,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="u",
            evaluation={}, toml_sha256="0" * 64, git_commit="0" * 40)

    def insert(tag):
        with Ledger(p) as led:
            try:
                rid = led.record_run(round_trial_budget=1,
                                     **_run_kw(params={"combo": tag},
                                               strategy=f"h-{tag}"))
                return tag, rid, None
            except Exception as e:                     # noqa: BLE001
                return tag, None, type(e).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(insert, ["w1", "w2"]))
    ok = [t for t, rid, e in outcomes if e is None]
    refused = [e for _t, _r, e in outcomes if e is not None]
    assert len(ok) == 1, outcomes
    assert refused == ["RoundTrialBudgetExceededError"], outcomes
    with Ledger(p) as led:
        assert led.round_n_dedup("R9") == 1
        assert led.verify_chain() == []


def test_p10_error_row_not_checked_not_consumed(tmp_path):
    """P-10：error 行不触发也不消耗预算（round_n_dedup 只计成功行——
    预算满时失败留痕仍可写）。"""
    p = tmp_path / "err.sqlite"
    with Ledger(p) as led:
        led.record_round_open(
            round_id="R9", candidates=["C101"], trial_budget=1,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="u",
            evaluation={}, toml_sha256="0" * 64, git_commit="0" * 40)
        led.record_run(round_trial_budget=1, **_run_kw())
        assert led.round_n_dedup("R9") == 1
        # 预算已满：error 行带 kwarg 也不被拒（失败不占预算）
        rid = led.record_run(round_trial_budget=1,
                             **_run_kw(params={"combo": "b"},
                                       metrics=None, error="boom"))
        assert rid > 0
        assert led.round_n_dedup("R9") == 1


def test_x01_h_path_refused_by_shared_reservation(tmp_path):
    """X-01（2026-10-30 审核，原 test_p10_h_path_no_kwarg_writes_past_cap
    的**错误契约**修正）：H/P 共享同一轮次预算，权威在计算前 reserve_trial
    ——旧断言"H 成功行不带 kwarg 可越过 cap 继续写"固定的正是审核 X-01
    指出的越限漏洞。新契约：
    - 最后一个名额被占（无论 H 还是 P 占）后，任何新试验的占位被拒
      （TrialBudgetExceededError，不写行、零计算调用）；
    - 同身份成功行照常写入（screen→gate 不被自己的占位堵死）；
    - **绕过占位**的新身份成功行由 record_run 的对账后盾拦下
      （TrialReservationConsistencyError，事务整体回滚——防双计）。"""
    from mt5quant.research.ledger import (
        TrialBudgetExceededError, TrialReservationConsistencyError)

    p = tmp_path / "h.sqlite"
    with Ledger(p) as led:
        led.record_round_open(
            round_id="R9", candidates=["C101"], trial_budget=1,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="u",
            evaluation={}, toml_sha256="0" * 64, git_commit="0" * 40)
        # P 试验占住最后一个名额（started 即计）
        r = led.reserve_trial("R9", "trial-p", budget=1)
        assert r["stage"] == "started"
        # H 试验（bt 的 H 正式路径同样走 reserve_trial）在计算前被拒
        with pytest.raises(TrialBudgetExceededError):
            led.reserve_trial("R9", "trial-h", budget=1)
        assert led.round_n_trials("R9") == 1
        # 占位内的成功行放行（去重 1 ≤ 上限）；同身份再写也放行
        rid = led.record_run(round_trial_budget=1, **_run_kw())
        rid2 = led.record_run(round_trial_budget=1, **_run_kw())
        assert rid > 0 and rid2 > rid
        assert led.round_n_dedup("R9") == 1
        # 绕过占位的新身份成功行：对账后盾拒绝、整体回滚
        n_before = led.conn.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]
        with pytest.raises(TrialReservationConsistencyError):
            led.record_run(round_trial_budget=1,
                           **_run_kw(params={"combo": "h2"}, strategy="h2"))
        n_after = led.conn.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]
        assert n_after == n_before
        assert led.round_n_dedup("R9") == 1
        assert led.verify_chain() == []


def test_p10_round_id_none_or_budget_none_no_check(tmp_path):
    """P-10：round_id=None（轮次之外）或 kwarg 缺省 → 无检查。"""
    p = tmp_path / "none.sqlite"
    with Ledger(p) as led:
        # kwarg 给出但 round_id=None：轮次之外无预算语义
        rid = led.record_run(round_trial_budget=1,
                             **_run_kw(round_id=None))
        assert rid > 0
        # kwarg 缺省：无检查
        rid = led.record_run(**_run_kw())
        assert rid > 0


def test_p10_nonpositive_budget_valueerror(tmp_path):
    """P-10：round_trial_budget<=0 → ValueError（零/负预算应由调用方不
    发起运行表达——与 reserve_lockbox_use 的 max_uses 校验对称）。"""
    p = tmp_path / "zero.sqlite"
    with Ledger(p) as led:
        led.record_round_open(
            round_id="R9", candidates=["C101"], trial_budget=1,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="u",
            evaluation={}, toml_sha256="0" * 64, git_commit="0" * 40)
        with pytest.raises(ValueError):
            led.record_run(round_trial_budget=0, **_run_kw())
        with pytest.raises(ValueError):
            led.record_run(round_trial_budget=-2, **_run_kw())


# ------------------------------------------------- 2. 政策同一来源（P2 侧）
def test_p10_screen_policy_from_sessions_source(p2_env, monkeypatch):
    """P-10 政策一致：P2 决策卡经 screen 的基准回测 session_policy 与
    load_session_policy(SESSIONS_PATH) 同源（--costs zero 无点差统计可
    并入 → 原样传配置源政策；auto 分支经 entry_policy_for 同一构造——
    与 prob test/cmd_fit/cmd_gate 的四个调用点同构）。"""
    from mt5data.sessions import load_session_policy
    captured = []
    real = BT.run_backtest

    def spy(strategy, bars, costs, **kwargs):
        captured.append(kwargs.get("session_policy"))
        return real(strategy, bars, costs, **kwargs)

    monkeypatch.setattr(BT, "run_backtest", spy)
    _card_path(p2_env, 1.0)
    argv = ["screen", "--card", "P002-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(p2_env["cards"]),
            "--data-root", str(p2_env["store"]),
            "--ledger", str(p2_env["ledger"]), "--costs", "zero",
            "--volume-lots", "1.0",
            "--allow-unbounded-data"]   # X-03：合成裸库夹具声明
    out = io.StringIO()
    monkeypatch.chdir(p2_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 0, out.getvalue()
    assert captured, "基准回测应至少调用一次 run_backtest"
    base_expected = load_session_policy(str(BT.SESSIONS_PATH))
    for pol in captured:
        assert pol == base_expected


# --------------------------------------- 3. fittable G3 保留拒绝（e2e）
def test_p10_fittable_g3_rejected_before_any_run(p2_env, monkeypatch):
    """P-10：T73B 完成前 fittable 策略的 G3 一律拒绝——e2e：gate 请求
    G3 → 退出码 1、零 runs（_reject_fittable_walkforward 在基准运行
    之前）。"""
    _card_path(p2_env, 1.0)
    argv = ["gate", "--card", "P002-v1", "--strategy", STRATEGY_REF,
            "--gates", "G0,G3", "--cards-dir", str(p2_env["cards"]),
            "--data-root", str(p2_env["store"]),
            "--ledger", str(p2_env["ledger"]), "--costs", "zero",
            "--volume-lots", "1.0", "--params", "{}",
            "--allow-unbounded-data"]    # X-03：合成裸库夹具声明
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(p2_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "T73B" in out.getvalue()
    with Ledger(p2_env["ledger"]) as led:
        assert led.runs_of_round("R3") == []
