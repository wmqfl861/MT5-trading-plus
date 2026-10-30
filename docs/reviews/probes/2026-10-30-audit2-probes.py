"""第二次全局审核的行为证据；全部为合成数据、假券商与临时目录。

运行：./.venv/Scripts/python.exe docs/reviews/probes/2026-10-30-audit2-probes.py
REPRODUCED 表示复现了报告所述缺陷，绝不表示产品正确。退出 0 表示所有
预期行为均已复现；修复后断言应失败，开发会话应另写正确行为的回归测试。
不连接终端、不读取行情库、不调用真实研究/标定、不修改既有探针或夹具。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT)]

import numpy as np
import pandas as pd

from mt5data import store
from mt5data.ext.merge import merged_research_series
from mt5data.lockbox import research_bars
from mt5gw.models import DEAL_REASON_CLIENT, DEAL_REASON_SL
from mt5live.executor import ExecutionAdapter, LiveExecutor
from mt5quant.costs.swap import per_lot_per_night
from mt5quant.prob.gaps import eligibility_mask
from mt5quant.prob.tables import CellEstimate
from mt5quant.research.cards import strategy_files
from mt5quant.research.ledger import Ledger
from mt5quant.research import candidates
from mt5quant.strategies.base import ExitRules
from mt5quant.strategies.library.prob import level_decision as ld
from mt5quant.validation import gates
from tests.fakes_live import FakeBroker, mk_decision


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


BT = load_script("audit2_bt", "bt.py")
PC = load_script("audit2_pc", "prob_calibration.py")


def bars(n=80, start="2024-01-01", freq="h"):
    return pd.DataFrame({"time": pd.date_range(start, periods=n, freq=freq),
                         "open": 100.0, "high": 101.0, "low": 99.0,
                         "close": 100.0, "tick_volume": 10,
                         "spread": 0, "real_volume": 0})


def exception_name(fn):
    try:
        fn()
    except Exception as exc:
        return type(exc).__name__
    return "accepted"


def run_kw(**extra):
    kw = dict(card_id="H901-v1", card_version=1, strategy="audit:synthetic",
              params={"x": 1}, symbol="XAUUSD", timeframe="H1",
              data_first="2024-01-01", data_last="2024-01-03",
              data_sha256="synthetic-data", engine_version="audit",
              cost_model_version="cost-a", gates_hash="frozen",
              strategy_sha="synthetic-code", metrics={"net_profit": 1.0},
              actor="bt", round_id="R901", run_class="research")
    kw.update(extra)
    return kw


def boundary_contract(root):
    store.save_bars(root, "XAUUSD", "H1", bars())
    (root / "lockbox.json").write_text(
        json.dumps({"XAUUSD/H1": "2024-01-03T00:00:00"}), encoding="utf-8")
    late = "2024-01-04"
    ordinary = exception_name(lambda: research_bars(root, "XAUUSD", "H1", end=late))
    merged = exception_name(lambda: merged_research_series(root, "XAUUSD", "H1", end=late))
    assert ordinary == "LockboxError" and merged == "accepted"
    mp = root / "bars/XAUUSD/H1/meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8"))
    meta["source_cutoff"] = late
    mp.write_text(json.dumps(meta), encoding="utf-8")
    conflict = [exception_name(lambda: research_bars(root, "XAUUSD", "H1")),
                exception_name(lambda: merged_research_series(root, "XAUUSD", "H1"))]
    assert conflict == ["LockboxError", "accepted"]
    return {"end_beyond_cutoff": [ordinary, merged], "conflicting_bounds": conflict}


def torn_store(root):
    old = bars(5)
    store.save_bars(root, "XAUUSD", "H1", old)
    new = bars(6)
    new.loc[0, ["open", "high", "low", "close"]] += 2
    replace = store._atomic_replace

    def fail_meta(src, dst):
        if Path(dst).name == "meta.json":
            raise OSError("synthetic crash between parquet and metadata")
        return replace(src, dst)

    with patch.object(store, "_atomic_replace", side_effect=fail_meta):
        assert exception_name(lambda: store.save_bars(root, "XAUUSD", "H1", new)) == "OSError"
    checked = exception_name(lambda: store.load_bars_checked(root, "XAUUSD", "H1"))
    read = research_bars(root, "XAUUSD", "H1")
    assert checked == "StoreConsistencyError" and read.iloc[0]["close"] == 102.0
    return {"checked_reader": checked, "research_reader_rows": len(read),
            "research_first_close": float(read.iloc[0]["close"])}


def budgets(root):
    with Ledger(root / "ledger.db") as ledger:
        ledger.record_round_open(
            round_id="R901", candidates=["C901"], trial_budget=1,
            lockbox_uses_max=0, dsr_trial_scope="project", advance_rule="audit",
            close_rule="audit", approved_by="synthetic-reviewer", evaluation={},
            toml_sha256="synthetic", git_commit="synthetic")
        ledger.record_run(**run_kw(), round_trial_budget=1)
        retry = exception_name(lambda: ledger.record_run(**run_kw(), round_trial_budget=1))
        ledger.record_run(**run_kw(params={"x": 2}))  # H 的正式调用不传预算
        ledger.record_run(**run_kw(params={"x": 3}, metrics=None,
                                  error="synthetic failure AFTER primary test"),
                          round_trial_budget=1)
        n = ledger.round_n_dedup("R901")
        assert n == 2 and retry == "RoundTrialBudgetExceededError"
        return {"cap": 1, "h_plus_p_successful_trials": n,
                "post_test_error_counted": False, "same_trial_retry": retry}


def g5_reference(root):
    with Ledger(root / "ledger.db") as ledger:
        a = ledger.record_run(**run_kw())
        ledger.record_gate(a, "G3", True, {"segments": [{"selected": "A"}]})
        ledger.record_gate(a, "V3", True, {})
        b = ledger.record_run(**run_kw(params={"x": 2}, cost_model_version="cost-b"))
        ledger.record_gate(b, "G3", True, {"segments": [{"selected": "B"}]})
        trial = gates.LockboxTrial(params={"x": 1}, data_sha256="synthetic-data",
                                  cost_model_version="cost-a", strategy_sha="synthetic-code",
                                  gates_hash="frozen")
        gates._verify_lockbox_trial(ledger, "H901-v1", trial)
        captured = []

        def capture(spec, data, costs, segments):
            captured.extend(segments)
            return pd.Series([0.0, 0.01])

        with patch.object(BT, "_stitch_oos_from_segments", side_effect=capture):
            BT._g3_reference_returns(ledger, NS(id="H901-v1"), None, bars(), None,
                                     data_sha="synthetic-data")
        assert captured == [{"selected": "B"}]
        return {"approved_trial": "A", "reference_trial": captured[0]["selected"]}


def card_for(cls):
    return NS(symbols=cls.SYMBOLS, timeframe=cls.TIMEFRAME, prob={
        "bracket": {"sl_atr": cls.SL_ATR, "tp_atr": cls.TP_ATR,
                    "horizon_bars": cls.HORIZON_BARS},
        "situations": cls.SITUATIONS, "level_grid": cls.LEVEL_GRID,
        "approach": vars(cls.APPROACH), "breakout_atr": cls.BREAKOUT_ATR,
        "retest_window_bars": cls.RETEST_WINDOW_BARS,
        "hypotheses": ["S1:A3:fade>base"]})


def whitelist(root):
    cls = ld.LevelDecisionS1Demo
    assert BT._check_card_strategy_constants(card_for(cls), cls) is None
    assert cls()._allowed_hypotheses() is None

    class WrongExit(cls):
        exit_rules = ExitRules(stop_loss_atr=9.0, take_profit_atr=10.0)

    assert BT._check_card_strategy_constants(card_for(WrongExit), WrongExit) is None
    return {"card_allowed": ["A3"], "strategy_allowed": ["A1", "A2", "A3", "A4"],
            "wrong_actual_exit_rules_accepted": True}


def boundary_state(root):
    data = bars(80)
    split = data.time.iloc[41]
    triggers = pd.DataFrame({"bar": [40, 42], "situation": ["S1", "S1"],
                             "action": ["A3", "A3"], "side": [1, 1]})
    estimate = CellEstimate(n=1000, p_win=1.0, p_loss=0.0, p_time=0.0,
                            r_win=2.0, r_loss=-1.0, r_time=0.0,
                            se_win=0.0, se_loss=0.0)
    strategy = ld.LevelDecisionS1Demo()
    strategy._table = NS(lookup=lambda *a: estimate)
    strategy._cost_r = {("S1", "A3"): 0.001}
    with patch.object(ld, "level_rows_for_situation", return_value=[]), \
         patch.object(ld, "find_level_events", return_value=[]), \
         patch.object(ld, "action_triggers", return_value=triggers):
        output = BT._ZeroedBeforeSplit(strategy, split).generate_signals(data, {})
    with patch.object(ld, "level_rows_for_situation", return_value=[]), \
         patch.object(ld, "find_level_events", return_value=[]), \
         patch.object(ld, "action_triggers", return_value=triggers.iloc[1:]):
        reset_output = strategy.generate_signals(data, {})
    assert output.iloc[42] == 0 and reset_output.iloc[42] == 1
    return {"validation_trigger_bar": 42, "masked_signal": int(output.iloc[42]),
            "signal_with_training_triggers_removed": int(reset_output.iloc[42])}


def gaps(root):
    data = bars(2200)
    data.loc[2101:, "time"] += pd.Timedelta(days=45)
    gap = [(data.time.iloc[2100], data.time.iloc[2101])]
    # A2 的 anchor 在洞前，action 在洞后；旧实现只查到 anchor 为止。
    trig = pd.DataFrame({"bar": [2102], "anchor": [2100], "action": ["A2"]})
    through_gap = eligibility_mask(trig, data, gap, "S1", 24, hold_bars=24)[0]
    tail = pd.DataFrame({"bar": [2199], "anchor": [2199], "action": ["A1"]})
    incomplete = eligibility_mask(tail, data, gap, "S1", 24, hold_bars=24)[0]
    assert through_gap and incomplete
    return {"anchor_to_action_crosses_45_days_but_eligible": through_gap,
            "last_bar_without_label_future_but_eligible": incomplete}


def calibration_identity(root):
    a = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=4),
                      "outcome": ["win", "win", "loss", "loss"]})
    b = a.copy()
    b["outcome"] = ["loss", "loss", "win", "win"]
    b["time"] += pd.Timedelta(days=120)
    equal = PC._pool_source_fingerprint(a) == PC._pool_source_fingerprint(b)
    assert equal
    return {"different_dates_and_serial_order_same_pool_fingerprint": equal}


def cluster_shift(root):
    idx = pd.DatetimeIndex(["2024-01-01 15:00", "2024-03-01 15:00", "2024-06-01 15:00"])
    trig = pd.DataFrame({"time": [idx[0]], "side": [1]})
    got = PC._shifted_triggers(trig, idx, weeks=3, perm_state=0)
    original_reused = got.time.iloc[0] == idx[0]
    assert original_reused
    return {"shift_into_internal_gap_maps_back_to_original_trigger": original_reused}


def live(root):
    magic = 901
    now = datetime(2024, 1, 8, 15)
    b = FakeBroker()

    def executor():
        adapter = ExecutionAdapter(b, b, b, symbol=b.symbol, magic=magic)
        return LiveExecutor(adapter, symbol=b.symbol, magic=magic, live_dir=str(root),
                            rules=ExitRules(reentry="immediate"))

    p = b.seed_position(side="buy", volume=0.02, magic=magic)
    ex = executor()
    ex.poll(None, now)
    b.reduce_external(p.ticket, 0.01, reason=DEAL_REASON_SL, time=now + timedelta(minutes=1))
    ex.poll(None, now + timedelta(minutes=2))  # 已观察历史 SL 后的剩余量
    b.deals_delay_polls = 100
    b.close_external(p.ticket, reason=DEAL_REASON_CLIENT, time=now + timedelta(minutes=3))
    ex.poll(None, now + timedelta(minutes=4))
    incorrectly_explained = not ex._state["pending_external_exits"]
    assert incorrectly_explained
    # 成交后、state 落盘前崩溃；重启前该仓位又退出，新执行器盲目重发同一决策。
    decision = mk_decision("audit-crash", signal=1, volume_lots=0.01)
    with patch.object(ex, "_save_state", side_effect=OSError("synthetic state-write crash")):
        assert exception_name(lambda: ex.poll(decision, now + timedelta(minutes=5))) == "OSError"
    opened = b.positions(b.symbol)[0]
    b.close_external(opened.ticket, reason=DEAL_REASON_CLIENT)
    before = len(b.market_orders)
    ex2 = executor()
    ex2.poll(decision, now + timedelta(minutes=6))
    resent = len(b.market_orders) - before
    assert resent == 1
    return {"historical_sl_explains_delayed_manual_close": incorrectly_explained,
            "same_decision_resent_after_state_write_crash": resent}


def currency(root):
    entry = {"name": "EURUSD", "swap_mode": 2, "swap_long": -5.0,
             "swap_short": -2.0, "swap_rollover3days": 3,
             "currency_profit": "USD", "currency_margin": "EUR"}
    accepted = per_lot_per_night(entry, account_currency="USD")
    entry["currency_base"] = "EUR"
    refused = exception_name(lambda: per_lot_per_night(entry, account_currency="USD"))
    assert accepted == (-5.0, -2.0) and refused == "NotImplementedError"
    return {"snapshot_without_base_currency_accepted_as_usd": list(accepted),
            "same_symbol_with_base_currency": refused}


def precheck(root):
    from tests.test_prob_precheck import prob_data, counted_df, FlatCosts

    (root / "specs").mkdir()
    (root / "specs/latest.json").write_text(
        json.dumps({"TEST": {"contract_size": 100.0}}), encoding="utf-8")
    counted = counted_df([("TEST", "S1", "A3", 9, 10.0, 100.0, t)
                          for t in pd.date_range("2010-01-01", periods=1600)])
    candidate = candidates.validate_candidate(prob_data(), source="synthetic")
    bad = counted.copy()
    bad["atr"] = np.nan
    result = candidates.precheck_prob(candidate, data_root=root,
                                     cost_model=FlatCosts(), counted=bad,
                                     split="2020-01-01")
    assert result.decision == "pass"
    d = prob_data()
    d["prob"]["cost_scenario_mult"] = [1.0, 1.5]
    candidate = candidates.validate_candidate(d, source="synthetic")
    result = candidates.precheck_prob(candidate, data_root=root,
                                     cost_model=FlatCosts(), counted=counted,
                                     split="2020-01-01")
    cost = result.checks["ex_ante_cost_prob"]
    assert cost["passed"] and cost["ext_trigger_n_by_symbol"]["TEST"] == 0
    return {"nan_atr_candidate_decision": "pass", "missing_era_cost_check_passed": True,
            "reported_cost_R": cost["cost_r_by_symbol"]["TEST"],
            "cost_R_if_all_external": (1.5 * 0.3 + 2 * 0.05) / 10.0}


def empty_ledger_round(root):
    with Ledger(root / "ledger.db") as ledger:
        admitted = BT._round_context(ledger, NS(id="H901-v1", round="", candidate=""), actor="bt")
    assert admitted is None
    return {"new_empty_ledger_without_round_admitted": True}


def dependencies(root):
    files = {p.relative_to(ROOT).as_posix() for p in strategy_files(
        "mt5quant.strategies.library.prob.level_decision:LevelDecisionS1Demo")}
    missing = sorted(f for f in (
        "src/mt5quant/prob/labels.py", "src/mt5quant/prob/tables.py",
        "src/mt5quant/prob/decision.py") if f not in files)
    assert len(missing) == 3
    # P2 模型 hash 只进 metrics，没有进 params/去重键。
    identities = []
    with Ledger(root / "model-identity.db") as ledger:
        for model_sha in ("synthetic-model-a", "synthetic-model-b"):
            ctx = {"params_prob": {"segment": "validation", "split": "2020-01-01"},
                   "metrics_prob": {"model_sha256": model_sha}}
            params, metrics = BT._prob_run_fields(ctx, {}, {"net_profit": 1.0})
            identities.append(params)
            ledger.record_run(**run_kw(params=params, metrics=metrics))
        n = ledger.round_n_dedup("R901")
    assert identities[0] == identities[1] and n == 1
    return {"direct_prob_dependencies_missing_from_strategy_hash": missing,
            "distinct_model_hashes": 2, "deduplicated_trials": n}


def main():
    cases = [boundary_contract, torn_store, budgets, g5_reference, whitelist,
             boundary_state, gaps, calibration_identity, cluster_shift, live,
             currency, precheck, empty_ledger_round, dependencies]
    failed = 0
    for case in cases:
        with tempfile.TemporaryDirectory(prefix="mt5-audit2-") as td:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = case(Path(td))
                print("REPRODUCED", case.__name__, json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                failed += 1
                # 不输出临时目录或本机绝对路径。
                print("PROBE_ERROR", case.__name__, type(exc).__name__)
    print(f"{len(cases) - failed}/{len(cases)} probes reproduced expected audit evidence")
    return int(failed != 0)


if __name__ == "__main__":
    raise SystemExit(main())
