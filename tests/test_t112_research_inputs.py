"""T112: synthetic input contracts, independent of any real store."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from mt5quant.validation import events as ev
from mt5quant.research import candidates
from mt5quant.backtest.report import data_sha256
from mt5quant.backtest.engine_a import run_backtest
from mt5quant.strategies.base import BaseStrategy, ExitRules, RequiredData
from mt5quant.strategies.library.b7_monthend_hedge_rebalance import MonthEndHedgeRebalance


def calendar(start="2024-01-01", end="2024-03-31", *, holidays=()):
    days = pd.bdate_range(start, end).difference(pd.DatetimeIndex(holidays))
    return {"version": 1, "complete_through": end,
            "sessions": [{"day": str(d.date()),
                          "close_time": str(d + pd.Timedelta(hours=22))}
                         for d in days]}


def signal_bars():
    # Previous penultimate 100, previous LAST 200, current penultimate 110.
    # The old last-day anchor yields -45%; the registered window yields +10%.
    return pd.DataFrame({
        "time": pd.to_datetime(["2024-01-30 21:00", "2024-01-31 21:00",
                                "2024-02-28 21:00", "2024-02-29 21:00",
                                "2024-03-28 21:00"]),
        "close": [100., 200., 110., 9999., 90.]})


def event(ts="2024-02-29 15:00", label="observed"):
    ts = pd.Timestamp(ts)
    return ev.EventWindow("london_fix", label, ts, ts + pd.Timedelta(hours=1))


def side_rule(bars=None, cal=None, threshold=0.):
    assert hasattr(ev, "declared_event_side"), "missing declared per-event direction API"
    return ev.declared_event_side("mtd_sign", signal_bars=signal_bars() if bars is None else bars,
                                  signal_timeframe="H1", calendar=calendar() if cal is None else cal,
                                  threshold=threshold)


def test_penultimate_window_and_randomized_own_timestamp():
    fn = side_rule()
    assert fn(event()) == 1
    assert fn(event("2024-03-29 15:00", "london_fix:rand123:0")) == -1
    with pytest.raises(ValueError, match="unavailable|不可用"):
        fn(event("2024-02-20 15:00", "london_fix:rand123:0"))


def test_signal_future_mutation_and_missing_anchor_not_zero():
    bars = signal_bars()
    bars.loc[bars.time > pd.Timestamp("2024-02-29 15:00"), "close"] = 1e9
    assert side_rule(bars)(event()) == 1
    missing = bars[bars.time != pd.Timestamp("2024-02-28 21:00")]
    with pytest.raises(ValueError, match="unavailable|不可用"):
        side_rule(missing)(event())
    assert side_rule(threshold=.2)(event()) == 0
    flat = signal_bars()
    flat.loc[flat.time == pd.Timestamp("2024-02-28 21:00"), "close"] = 100.
    assert side_rule(flat)(event()) == 0


def test_explicit_calendar_partial_month_and_asynchronous_holiday():
    partial = calendar(end="2024-02-15")
    with pytest.raises(ValueError, match="calendar|日历"):
        side_rule(cal=partial)(event("2024-02-15 15:00"))
    # Signal market's last day is 28th, penultimate is 27th; trade market has 29th.
    bars = signal_bars()
    bars.loc[bars.time == pd.Timestamp("2024-02-28 21:00"), "time"] = pd.Timestamp("2024-02-27 21:00")
    assert side_rule(bars, calendar(holidays=["2024-02-29"]))(event()) == 1
    with pytest.raises(ValueError, match="unavailable|不可用"):
        side_rule()(event("2024-02-28 22:00"))  # close must be strictly before


class FixedCosts:
    swap = None
    def spread_p90(self, symbol, hour, *, price=None): return 2.
    def slippage(self, symbol, hour, *, price=None): return .5
    def commission_per_lot(self, symbol): return 4.
    def overnight_fee(self, *args, **kwargs): return 0.


def event_bars():
    return pd.DataFrame({"time": pd.to_datetime(["2024-02-29 15:00", "2024-02-29 16:00"]),
                         "open": [100., 110.], "close": [100., 110.]})


def test_zero_exposure_is_observation_without_cost():
    frame = ev.event_returns(event_bars(), [event()], side_fn=lambda _: 0,
                             costs=FixedCosts(), symbol="X", contract_size=1,
                             reference_price=100.)
    assert len(frame) == 1
    assert frame.iloc[0]["side"] == "flat"
    assert frame.iloc[0]["net"] == frame.iloc[0]["cost"] == 0.


def test_event_external_spread_only_and_all_cost_stress_distinct():
    from mt5quant.validation.robustness import ScaledCosts
    bars = event_bars()
    bars["_research_source"] = ["ext:histdata:2024", "mt5"]
    frame = ev.event_returns(bars, [event()], side_fn=lambda _: 1,
                             costs=FixedCosts(), symbol="X", contract_size=1,
                             reference_price=100.)
    assert frame.iloc[0]["spread"] == pytest.approx(.03)
    assert frame.iloc[0]["slippage"] == pytest.approx(.01)
    assert frame.iloc[0]["commission"] == pytest.approx(.04)
    stressed = ev.event_returns(bars, [event()], side_fn=lambda _: 1,
                                costs=ScaledCosts(FixedCosts(), 1.5), symbol="X",
                                contract_size=1, reference_price=100.)
    assert stressed.iloc[0]["cost"] == pytest.approx(.12)


class OneTrade(BaseStrategy):
    required_data = RequiredData(symbols=("X",), timeframe="H1", lookback_bars=2)
    exit_rules = ExitRules()
    def generate_signals(self, bars, params):
        return pd.Series([1, 0, 0, 0][:len(bars)], index=bars.index)


def test_engine_source_costs_slice_alignment_and_legacy_equality():
    bars = pd.DataFrame({"time": pd.date_range("2024-01-02", periods=4, freq="h"),
                         "open": 100., "high": 101., "low": 99., "close": 100.})
    kw = dict(symbol="X", params={}, contract_size=1., volume_lots=1.)
    old = run_backtest(OneTrade(), bars, FixedCosts(), **kw)
    mt5 = bars.assign(_research_source="mt5")
    same = run_backtest(OneTrade(), mt5, FixedCosts(), **kw)
    pd.testing.assert_frame_equal(old.trades, same.trades, check_exact=True)
    assert old.trades.to_json(date_format="iso") == same.trades.to_json(date_format="iso")
    ext = bars.assign(_research_source=["mt5", "ext:histdata:2024", "mt5", "mt5"])
    new = run_backtest(OneTrade(), ext.iloc[:3], FixedCosts(), **kw)
    assert new.trades.iloc[0]["spread_cost"] == 3.
    assert new.trades.iloc[0]["commission"] == 4.
    assert new.trades.iloc[0]["slippage_cost"] == 1.
    assert new.trades.iloc[0]["entry_time"] == old.trades.iloc[0]["entry_time"]
    assert new.trades.iloc[0]["exit_reason"] == old.trades.iloc[0]["exit_reason"]


def test_b7_research_missing_aux_refuses_and_uses_real_aux():
    bars = pd.DataFrame({"time": pd.to_datetime(["2024-02-29 14:00", "2024-02-29 15:00"]),
                         "open": 1., "high": 1., "low": 1., "close": 1.})
    with pytest.raises(ValueError, match="signal|信号"):
        MonthEndHedgeRebalance().generate_signals(bars, {"threshold_pct": 0.})
    st = MonthEndHedgeRebalance(signal_bars=signal_bars(), signal_calendar=calendar(),
                               event_calendar=calendar())
    assert st.generate_signals(bars, {"threshold_pct": 0.}).tolist() == [1, 0]


def test_declared_optional_fields_and_rule_validation():
    raw = dict(events_per_year=12, gross_return_per_event=.003, window_bars=1,
               entry_hour_ny=10, exit_hour_ny=11, overnight_nights=0, expected_sharpe=1.)
    decl = candidates._validate_declared(dict(raw, signal_symbol="USTEC", signal_timeframe="D1",
                                             side_rule="mtd_sign"), "synthetic")
    assert decl.signal_symbol == "USTEC" and decl.side_rule == "mtd_sign"
    legacy = candidates._validate_declared(raw, "old")
    assert legacy.signal_symbol is None and legacy.side_rule is None
    with pytest.raises(ValueError, match="side_rule"):
        candidates._validate_declared(dict(raw, side_rule="lookahead"), "bad")


def test_composite_identity_changes_signal_only_but_keeps_p_v2():
    bars = event_bars()
    legacy = data_sha256(bars)
    assert legacy.startswith("v2:")
    bars.attrs["research_input"] = {"version": "h-input-v1", "signal_sha256": "one",
                                     "side_rule": "mtd_sign", "window_version": "penultimate-close-v1",
                                     "source_cost_policy": "external-spread-x1.5-v1"}
    first = data_sha256(bars)
    bars.attrs["research_input"]["signal_sha256"] = "two"
    assert data_sha256(bars) != first != legacy
    bars.attrs.clear()
    assert data_sha256(bars) == legacy


def load_bt():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "bt.py"
    spec = importlib.util.spec_from_file_location("bt_t112", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runtime_coverage_timestamp_only_gaps_and_exact_ninety():
    assert hasattr(candidates, "runtime_coverage"), "missing timestamp-only coverage estimator"
    stamps = pd.date_range("2020-01-01", periods=100, freq="D")
    frame = pd.DataFrame({"time": stamps, "close": [object()] * 100})
    tags = ["ext:histdata:2020"] * 50 + ["mt5"] * 50
    actual = candidates.runtime_coverage(frame, tags)
    assert actual["years"] == pytest.approx(99 / 365.25)
    assert {s["source"] for s in actual["segments"]} == {"mt5", "ext:histdata:2020"}
    expected = actual["years"] / .9
    candidates.require_runtime_coverage(actual, expected, "X/H1")
    with pytest.raises(ValueError, match="coverage|覆盖"):
        candidates.require_runtime_coverage(actual, expected + .01, "X/H1")
    gap = pd.DataFrame({"time": pd.to_datetime(["2020-01-01", "2020-01-31", "2020-03-02", "2020-03-03"])})
    assert candidates.runtime_coverage(gap, ["mt5"] * 4)["years"] == pytest.approx(31 / 365.25)


def test_loader_uses_latest_ledger_both_merged_inputs_and_end(tmp_path):
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    assert hasattr(mod, "_load_h_research_inputs"), "missing H multi-input assembly"
    card = SimpleNamespace(symbols=("EURUSD",), timeframe="H1")
    decl = SimpleNamespace(signal_symbol="USTEC", signal_timeframe="D1", side_rule="mtd_sign")
    stamps = pd.bdate_range("2024-01-01", "2024-03-31")
    df = pd.DataFrame({"time": stamps, "open": 100., "close": 100.})
    years = 88 / 365.25
    # Loader-contract unit fixture: each input carries its own source evidence.
    # The separate R1 CLI tests generate these fields through candidate check.
    pre = {"checks": {"power": {"years": years, "years_detail": {"EURUSD/H1": years, "USTEC/D1": years},
           "coverage_segments": {"EURUSD/H1": [{"source": "histdata", "counted": True}],
                                 "USTEC/D1": [{"source": "fred", "counted": True}]}}}}
    cdir = tmp_path / "calendars"
    cdir.mkdir()
    for s in ("EURUSD", "USTEC"):
        (cdir / f"{s}.json").write_text(json.dumps(calendar()), encoding="utf8")
    calls = []
    def merged(root, sym, tf, *, end, allow_unbounded):
        calls.append((sym, tf, end, allow_unbounded))
        return df.copy(), [f"ext:{'fred' if sym == 'USTEC' else 'histdata'}:2024"] * len(df), [], ["2023-12"]
    def plain(*a, **kw):
        raise AssertionError("external coverage must use merged loader for every input")
    with Ledger(tmp_path / "ledger.sqlite") as led:
        led.record_candidate_check("C001", decision="pass", result={"checks": {"power": {"years": 100}}})
        led.record_candidate_check("C001", decision="pass", result=pre)
        ctx = mod._load_h_research_inputs(led, card, candidate_id="C001", declared=decl,
                                          data_root=str(tmp_path), end="2024-03-31", allow_unbounded=True,
                                          load_plain=plain, load_merged=merged)
    assert calls == [("EURUSD", "H1", "2024-03-31", True), ("USTEC", "D1", "2024-03-31", True)]
    assert ctx["coverage"]["EURUSD/H1"]["years"] == pytest.approx(years)
    assert ctx["coverage"]["USTEC/D1"]["bad_months"] == ["2023-12"]
    assert ctx["bars"]["EURUSD"].attrs["research_input"]["signal_sha256"]
    assert list(ctx["signals"]["EURUSD"].time) == list(stamps)


def test_new_event_round_requires_matching_declared_rule_before_open(tmp_path, monkeypatch):
    from tests.test_research_rounds import ROUND_TOML, write_round, patch_git, record_check
    from tests.test_research_candidates import CANDIDATE_TOML
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    rdir = write_round(tmp_path, ROUND_TOML, historical=True)
    cdir = tmp_path / "candidates"
    cdir.mkdir()
    (cdir / "C001.toml").write_text(CANDIDATE_TOML + '\nside_rule = "static_long"\n', encoding="utf8")
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as led:
        record_check(led, "C001", "pass")
    patch_git(monkeypatch, mod)
    assert mod.main(["round", "open", "R1", "--rounds-dir", str(rdir), "--ledger", str(path)]) == 1
    with Ledger(path) as led:
        assert led.round("R1") is None
    (rdir / "R1.toml").write_text(ROUND_TOML.replace('mode = "event",', 'mode = "event", side_rule = "static_short",'), encoding="utf8")
    assert mod.main(["round", "open", "R1", "--rounds-dir", str(rdir), "--ledger", str(path)]) == 1
    (rdir / "R1.toml").write_text(ROUND_TOML.replace('mode = "event",', 'mode = "event", side_rule = "static_long",'), encoding="utf8")
    assert mod.main(["round", "open", "R1", "--rounds-dir", str(rdir), "--ledger", str(path)]) == 0


def test_g0_truncates_attached_aux_for_prefix_and_windows():
    from mt5quant.backtest.truncation import check_no_lookahead
    from mt5quant.validation.lookahead import check_window_invariance
    class LeakyAux(BaseStrategy):
        required_data = RequiredData(symbols=("X",), timeframe="H1", lookback_bars=2)
        def __init__(self, aux): self.signal_bars = aux
        def for_signal_window(self, bars):
            end = bars.time.iloc[-1] + pd.Timedelta(hours=1)
            return LeakyAux(self.signal_bars[self.signal_bars.time < end])
        def generate_signals(self, bars, params):
            return pd.Series(int(self.signal_bars.close.iloc[-1]), index=bars.index)
    bars = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=6, freq="h"), "close": [0, 0, 0, 1, 1, 1]})
    st = LeakyAux(bars.copy())
    assert check_no_lookahead(st, bars, params={}, probe_points=5)
    assert check_window_invariance(st, bars, {}, probes=20)


def test_randomization_unavailable_anchor_fails_without_refill(monkeypatch):
    calls = []
    def frozen_sampler(events, trading_days, *, seed):
        calls.append(seed)
        return [event("2024-02-20 15:00", "random")]
    monkeypatch.setattr(ev, "randomize_event_times", frozen_sampler)
    with pytest.raises(ValueError, match="unavailable|不可用"):
        ev.randomization_test(event_bars(), [event()], side_fn=side_rule(), costs=FixedCosts(),
                              symbol="X", contract_size=1, trading_days=pd.bdate_range("2024-01-01", "2024-03-31"), sims=10)
    assert len(calls) == 1


@pytest.mark.parametrize("gates", ["G0", "V3"])
def test_explicit_event_side_conflict_rejects_before_calculations(tmp_path, monkeypatch, capsys, gates):
    from tests.test_research_bt_script import _setup_t86_round
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_t86_round(tmp_path, monkeypatch, mod)
    def tripwire(*a, **kw): raise AssertionError("CALCULATION_REACHED")
    monkeypatch.setattr(mod, "run_backtest", tripwire)
    monkeypatch.setattr(mod, "_fit_and_freeze", tripwire)
    monkeypatch.setattr(ev, "randomization_test", tripwire)
    rc = mod.main(["gate", "--card", "H703", "--cards-dir", str(cards_dir), "--data-root", str(root),
                   "--ledger", str(ledger_path), "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
                   "--costs", "zero", "--allow-unbounded-data", "--gates", gates, "--params", "{}",
                   "--event-kind", "month_end", "--event-side", "short"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "side_rule" in out and "CALCULATION_REACHED" not in out
    from mt5quant.research.ledger import Ledger
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_signal_identity_closes_reservation_dedup_auto_and_g3_g5(tmp_path):
    from mt5quant.research.ledger import Ledger
    from mt5quant.validation.gates import LockboxTrial, _matching_gate_runs
    mod = load_bt()
    bars = event_bars()
    bars.attrs["research_input"] = {"signal_sha256": "a", "side_rule": "mtd_sign"}
    a = data_sha256(bars)
    bars.attrs["research_input"]["signal_sha256"] = "b"
    b = data_sha256(bars)
    common = dict(strategy="B7", params={"threshold_pct": 0.}, symbol="EURUSD", timeframe="H1",
                  cost_model_version="cost", strategy_sha="code")
    assert mod._trial_key(data_sha256=a, **common) != mod._trial_key(data_sha256=b, **common)
    with Ledger(tmp_path / "identity.sqlite") as led:
        def record(sha):
            return led.record_run(card_id="H900-v1", card_version=1, data_sha256=sha,
                                  data_first="", data_last="", engine_version="A", gates_hash="g", actor="research",
                                  metrics={"net_profit": 1., "sharpe": 1.}, **common)
        r1 = record(a)
        record(a)
        assert led.stats()["project_n_dedup_research"] == 1
        card = SimpleNamespace(id="H900-v1", timeframe="H1", selection_rule="max_net_profit")
        assert mod._select_params_auto(led, card, symbol="EURUSD", data_sha256=a, cost_model_version="cost")["params"] == {"threshold_pct": 0.}
        with pytest.raises(ValueError, match="screen"):
            mod._select_params_auto(led, card, symbol="EURUSD", data_sha256=b, cost_model_version="cost")
        for gate in ("G3", "V3"):
            led.record_gate(r1, gate, True, {"passed": True})
            assert _matching_gate_runs(led, card.id, LockboxTrial(common["params"], a, "cost", "code", "g"), gate)
            assert not _matching_gate_runs(led, card.id, LockboxTrial(common["params"], b, "cost", "code", "g"), gate)
        record(b)
        assert led.stats()["project_n_dedup_research"] == 2


def test_select_params_auto_single_spec_direct_call_defends(tmp_path):
    """C2（范围裁定 2026-11-07）：single_spec 直调 _select_params_auto 仍
    ValueError（auto 算法本体零改动——single_spec 不进 _SELECTION_METRICS，
    从 screen 运行选参的语义不稀释），且消息自持（含规则名），不退化为
    ValueError(None)。"""
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    with Ledger(tmp_path / "c2-defense.sqlite") as led:
        card = SimpleNamespace(id="H900-v1", timeframe="H1",
                               selection_rule="single_spec")
        with pytest.raises(ValueError) as ei:
            mod._select_params_auto(led, card, symbol="EURUSD",
                                    data_sha256="a" * 64,
                                    cost_model_version="cost")
        assert "single_spec" in str(ei.value)


def test_dynamic_v3_consumes_callback_and_differs_from_static(tmp_path, monkeypatch):
    mod = load_bt()
    from mt5quant.research.ledger import Ledger
    from mt5quant.validation.robustness import BacktestSpec
    bars = pd.concat([event_bars(), event_bars().assign(time=pd.to_datetime(["2024-03-29 15:00", "2024-03-29 16:00"]),
                                                     open=[100., 90.], close=[100., 90.])], ignore_index=True)
    events = [event(), event("2024-03-29 15:00", "mar")]
    class Zero(FixedCosts):
        def spread_p90(self, *a, **k): return 0.
        def slippage(self, *a, **k): return 0.
        def commission_per_lot(self, *a): return 0.
    zero = Zero()
    observed = []
    def randomization(bars, events, *, side_fn, **kwargs):
        frame = ev.event_returns(bars, events, side_fn=side_fn, costs=zero, symbol="X")
        mean = float(frame.gross.mean())
        observed.append(mean)
        return dict(p_value=.5, n_events=len(frame), observed_mean=mean, null_mean=0.)
    monkeypatch.setattr(ev, "randomization_test", randomization)
    monkeypatch.setattr(ev, "v3_event_diagnostics", lambda *a, **k: {})
    monkeypatch.setattr(mod, "_v3_n_composition", lambda *a: {"n": 1})
    cfg, _ = mod.vgate.load_gates()
    st = OneTrade()
    spec = BacktestSpec(strategy=st, symbol="X", timeframe="H1", contract_size=1., volume_lots=1.)
    fn = side_rule()
    with Ledger(tmp_path / "v3.sqlite") as led:
        for callback, rule in ((fn, "mtd_sign"), (ev.declared_event_side("static_long"), "static_long")):
            result = mod._v3_gate_evaluate(led, SimpleNamespace(id="H900"), cfg=cfg,
                ctx={"mode": "event", "kind": "london_fix", "sims": 2, "draws_min": 2,
                     "side_rule": rule, "event_setup": (events, pd.bdate_range("2024-01-01", "2024-03-31"), callback)},
                cards_dir=tmp_path, strategy=st, spec=spec, bars=bars, costs=zero, params={}, base=SimpleNamespace(trades=pd.DataFrame()),
                summary={}, contract_size=1., symbol="X", combos=[], g0_result={"passed": True}, g3_result=None,
                data_sha="s", seed=0, known_gaps=[])
            assert result["primary"]["side_rule"] == rule
    assert observed == pytest.approx([.1, 0.])


def test_b7_g0_auxiliary_crop_preserves_known_calendar_and_causal_signals():
    from mt5quant.validation.lookahead import run_g0
    times = pd.date_range("2024-01-01", "2024-03-31 23:00", freq="h")
    bars = pd.DataFrame({"time": times, "open": 100., "high": 101., "low": 99., "close": 100.})
    signal = pd.DataFrame({"time": times, "close": 100. + np.arange(len(times)) * .01})
    st = MonthEndHedgeRebalance(signal_bars=signal, signal_calendar=calendar(), event_calendar=calendar())
    view = st.for_signal_window(bars.iloc[:30])
    assert view.signal_bars.time.max() < bars.time.iloc[29] + pd.Timedelta(hours=1)
    assert view.event_calendar == st.event_calendar  # schedule is not future prices
    result = run_g0(st, bars, {"threshold_pct": 0.}, probes=5, sweep_span=5, source="")
    assert result["passed"], result


def test_b7_full_instance_future_prices_are_invisible():
    bars = pd.DataFrame({"time": pd.to_datetime(["2024-02-29 14:00", "2024-02-29 15:00"]),
                         "open": 1., "high": 1., "low": 1., "close": 1.})
    signal = signal_bars()
    st = MonthEndHedgeRebalance(signal_bars=signal, signal_calendar=calendar(), event_calendar=calendar())
    before = st.generate_signals(bars, {"threshold_pct": 0.})
    signal.loc[signal.time >= pd.Timestamp("2024-02-29 15:00"), "close"] = 1e12
    pd.testing.assert_series_equal(before, st.generate_signals(bars, {"threshold_pct": 0.}))


def test_event_setup_keeps_future_calendar_but_samples_only_observed_days():
    mod = load_bt()
    times = pd.date_range("2024-01-01", "2024-03-31", freq="D")
    bars = pd.DataFrame({"time": times, "open": 100., "close": 100.})
    ctx = {"declared": SimpleNamespace(signal_symbol=None, signal_timeframe=None, side_rule="static_long"),
           "calendars": {"X": {"version": 1, "complete_through": "2024-04-30", "sessions": [
               {"day": str(t.date()), "close_time": str(t + pd.Timedelta(hours=22))}
               for t in pd.date_range("2024-01-01", "2024-04-30", freq="D")]}},
           "signals": {"X": bars}, "timeframe": "D1"}
    events, days, fn = mod._h_event_setup(ctx, "X", bars, "month_end", {})
    assert days.max() == pd.Timestamp("2024-03-31")
    assert len(ctx["calendars"]["X"]["sessions"]) == 121
    assert all(e.exit_time <= times[-1] for e in events)


@pytest.mark.parametrize("command", ["screen", "gate"])
def test_runtime_shortfall_stops_before_baseline(tmp_path, monkeypatch, capsys, command):
    from tests.test_research_bt_script import _setup_t86_round
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root, cards_dir, ledger_path = _setup_t86_round(tmp_path, monkeypatch, mod, mode="standard")
    with Ledger(ledger_path) as led:
        led.record_candidate_check("C003", decision="pass", result={"checks": {"power": {"years": 100., "years_detail": {"XAUUSD/H1": 100.}}}})
    def tripwire(*a, **kw): raise AssertionError("CALCULATION_REACHED")
    monkeypatch.setattr(mod, "run_backtest", tripwire)
    monkeypatch.setattr(mod, "_fit_and_freeze", tripwire)
    args = [command, "--card", "H703", "--cards-dir", str(cards_dir), "--data-root", str(root),
            "--ledger", str(ledger_path), "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold",
            "--costs", "zero", "--allow-unbounded-data"]
    if command == "gate": args += ["--gates", "G0", "--params", "{}"]
    assert mod.main(args) == 1
    out = capsys.readouterr().out
    assert "coverage" in out and "CALCULATION_REACHED" not in out
    with Ledger(ledger_path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def disk_bars(times, price=100.):
    times = pd.DatetimeIndex(times)
    return pd.DataFrame({"time": times, "open": price, "high": price + 1.,
                         "low": price - 1., "close": price, "tick_volume": 1,
                         "spread": 0, "real_volume": 0})


def write_external(root, symbol, tf, source, bars, *, used=None, cutoff="2024-04-01"):
    from mt5data.ext.core import ExtSeries, save_ext_series
    from mt5data.ext.approvals import SOURCE_APPROVALS
    manifest = dict(source=source, symbol=symbol, timeframe=tf,
                    source_url="https://example.invalid/synthetic", price_side="synthetic",
                    original_timezone="UTC", dst_rule="synthetic", terms_note="synthetic only",
                    downloaded_at_utc="2024-04-01T00:00:00Z", source_files=[{"name": "synthetic", "sha256": "0" * 64}],
                    converter_version=1, usage_restriction=SOURCE_APPROVALS[source]["usage_restriction"],
                    yearly_verification=used or {"2023": {"used": False}, "2024": {"used": True}},
                    research_cutoff=cutoff, rows=len(bars), first_time=str(bars.time.min()), last_time=str(bars.time.max()))
    save_ext_series(root / "ext", ExtSeries(bars, manifest))


def test_real_disk_merged_loader_four_results_used_badmonth_cutoff_end(tmp_path):
    from mt5data.store import save_bars
    from mt5data.ext.merge import merged_research_series
    from mt5data.lockbox import LockboxError
    from mt5quant.research.ledger import Ledger
    root = tmp_path / "synthetic-store"
    stamps = pd.bdate_range("2024-01-01", "2024-03-25")
    bad_sundays = pd.to_datetime(["2024-02-04 22:00", "2024-02-11 23:00"])
    trade = disk_bars(pd.DatetimeIndex([pd.Timestamp("2023-12-29")]).append(stamps).append(bad_sundays).sort_values(), 100.)
    signal = disk_bars(pd.DatetimeIndex([pd.Timestamp("2023-12-29")]).append(stamps), 200.)
    for symbol, tf, source, bars in (("EURUSD", "H1", "histdata", trade), ("USTEC", "D1", "fred", signal)):
        write_external(root, symbol, tf, source, bars, cutoff="2024-03-20")
        save_bars(str(root), symbol, tf, disk_bars(pd.bdate_range("2024-03-01", "2024-03-25"), 300.))
    full, tags, gaps, bad = merged_research_series(root, "EURUSD", "H1")
    assert bad == ["2024-02"] and gaps == [(pd.Timestamp("2024-01-31"), pd.Timestamp("2024-03-01"))]
    assert not (full.time.dt.year == 2023).any() and not (full.time.dt.month == 2).any()
    assert full.time.max() == pd.Timestamp("2024-03-19")
    assert full.loc[full.time >= "2024-03-01", "close"].eq(300.).all()
    assert all(t == "mt5" for t in np.array(tags)[full.time >= "2024-03-01"])
    mod = load_bt()
    with Ledger(tmp_path / "ledger.sqlite") as led:
        led.record_candidate_check("C001", decision="pass", result={"checks": {"power": {
            "years_detail": {"EURUSD/H1": .08, "USTEC/D1": .15},
            "coverage_segments": {"EURUSD/H1": [{"source": "histdata", "counted": True}],
                                  "USTEC/D1": [{"source": "fred", "counted": True}]}}}})
        ctx = mod._load_h_research_inputs(led, SimpleNamespace(symbols=("EURUSD",), timeframe="H1"),
            candidate_id="C001", declared=SimpleNamespace(signal_symbol="USTEC", signal_timeframe="D1", side_rule=None),
            data_root=str(root), end="2024-03-15", allow_unbounded=False,
            load_plain=mod.load_series, load_merged=merged_research_series)
        assert ctx["bars"]["EURUSD"].time.max() == ctx["signals"]["EURUSD"].time.max() == pd.Timestamp("2024-03-15")
        assert ctx["coverage"]["EURUSD/H1"]["bad_months"] == ["2024-02"]
        assert ctx["gaps"]["EURUSD/H1"] == gaps
        assert ctx["coverage"]["EURUSD/H1"]["years"] == pytest.approx(44 / 365.25)
        with pytest.raises(LockboxError):
            mod._load_h_research_inputs(led, SimpleNamespace(symbols=("EURUSD",), timeframe="H1"),
                candidate_id="C001", declared=SimpleNamespace(signal_symbol="USTEC", signal_timeframe="D1", side_rule=None),
                data_root=str(root), end="2024-03-21", allow_unbounded=False,
                load_plain=mod.load_series, load_merged=merged_research_series)


def setup_dynamic_cli(tmp_path, monkeypatch, *, missing_anchor=False):
    from tests.test_research_candidates import CANDIDATE_TOML
    from tests.test_research_bt_script import _CARD_H703, _ROUND_R1_EVENT, patch_card_git
    from mt5data.store import save_bars
    from mt5quant.research.ledger import Ledger
    mod = load_bt()
    root = tmp_path / "synthetic-store"
    trade_times = pd.date_range("2024-02-01", "2024-03-29 23:00", freq="h")
    trade_times = trade_times[trade_times.weekday < 5]
    save_bars(str(root), "EURUSD", "H1", disk_bars(trade_times))
    sig_times = pd.bdate_range("2024-01-01", "2024-03-29")
    if missing_anchor:
        sig_times = sig_times[sig_times != pd.Timestamp("2024-02-28")]
    sig = disk_bars(sig_times, 200.)
    sig.loc[sig.time >= "2024-02-01", ["open", "close", "high", "low"]] = [210., 210., 211., 209.]
    sig.loc[sig.time >= "2024-03-01", ["open", "close", "high", "low"]] = [190., 190., 191., 189.]
    write_external(root, "USTEC", "D1", "fred", sig)
    (root / "lockbox.json").write_text(json.dumps({"version": 2, "global_cutoff": "2024-04-01T00:00:00"}), encoding="utf8")
    specs = root / "specs"
    specs.mkdir()
    (specs / "latest.json").write_text(json.dumps({"EURUSD": {"contract_size": 1.}}), encoding="utf8")
    cdir, rdir, cards_dir = (tmp_path / p for p in ("candidates", "rounds", "cards"))
    for p in (cdir, rdir, cards_dir): p.mkdir()
    (cdir / "C003.toml").write_text(CANDIDATE_TOML.replace("C001", "C003").replace("TESTUSD", "EURUSD") +
        '\nside_rule = "mtd_sign"\nsignal_symbol = "USTEC"\nsignal_timeframe = "D1"\n', encoding="utf8")
    (rdir / "R1.toml").write_text(_ROUND_R1_EVENT.replace('mode = "event",', 'mode = "event", side_rule = "mtd_sign",'), encoding="utf8")
    (cards_dir / "H703.toml").write_text(_CARD_H703.replace("XAUUSD", "EURUSD").replace('[params]', '[params]\nthreshold_pct = [0.0]'), encoding="utf8")
    caldir = root / "calendars"
    caldir.mkdir()
    for symbol in ("EURUSD", "USTEC"):
        raw = calendar()
        raw["source"] = "synthetic exchange schedule; not a real calendar"
        for session in raw["sessions"]: session["price_source"] = "fred" if symbol == "USTEC" else "mt5"
        (caldir / f"{symbol}.json").write_text(json.dumps(raw), encoding="utf8")
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as led:
        led.record_candidate_check("C003", decision="pass", result={"checks": {"power": {
            "years_detail": {"EURUSD/H1": .14, "USTEC/D1": .20},
            "coverage_segments": {"USTEC/D1": [{"source": "fred", "counted": True}]}}}})
    patch_card_git(monkeypatch, mod)
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean", lambda *a, **k: None)
    monkeypatch.setattr(mod.cards, "strategy_fingerprint", lambda *a, **k: "f" * 64)
    assert mod.main(["round", "open", "R1", "--rounds-dir", str(rdir), "--ledger", str(path)]) == 0
    args = ["--card", "H703", "--cards-dir", str(cards_dir), "--data-root", str(root), "--ledger", str(path),
            "--strategy", "mt5quant.strategies.library.b7_monthend_hedge_rebalance:MonthEndHedgeRebalance",
            "--costs", "zero", "--volume-lots", "1"]
    return mod, root, path, args


@pytest.mark.parametrize("command", ["screen", "gate"])
def test_real_cli_missing_anchor_rejects_before_baseline(tmp_path, monkeypatch, capsys, command):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_dynamic_cli(tmp_path, monkeypatch, missing_anchor=True)
    def tripwire(*a, **kw): raise AssertionError("CALCULATION_REACHED")
    monkeypatch.setattr(mod, "run_backtest", tripwire)
    command_args = [command, *args]
    if command == "gate": command_args += ["--gates", "G0", "--params", '{"threshold_pct": 0.0}']
    assert mod.main(command_args) == 1
    out = capsys.readouterr().out
    assert "anchor" in out and "CALCULATION_REACHED" not in out
    with Ledger(path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert led.conn.execute("SELECT COUNT(*) FROM trial_reservations").fetchone()[0] == 0


def test_real_cli_screen_gate_identity_and_visible_coverage(tmp_path, monkeypatch, capsys):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_dynamic_cli(tmp_path, monkeypatch)
    assert mod.main(["screen", *args]) == 0, capsys.readouterr().out
    assert mod.main(["gate", *args, "--gates", "G0", "--params", '{"threshold_pct": 0.0}']) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "运行时覆盖" in out and "USTEC/D1" in out and "可用事件" in out
    with Ledger(path) as led:
        rows = led.conn.execute("SELECT data_sha256, params_json, metrics_json FROM runs ORDER BY run_id").fetchall()
        assert len(rows) == 2 and rows[0]["data_sha256"] == rows[1]["data_sha256"]
        assert led.round_n_dedup("R1") == 1
        assert led.round_n_trials("R1") == 1
        assert json.loads(rows[0]["params_json"]) == {"threshold_pct": 0.}
        for r in rows:
            metrics = json.loads(r["metrics_json"])["metrics"]
            assert metrics["usable_events"] == {"EURUSD": 2}
            assert metrics["runtime_coverage"]["USTEC/D1"]["signal_events_available"] == 2
        # Gate approval is tied to the composite signal input, even with exactly
        # the same trade prices, params and newer evidence from another signal.
        from mt5quant.validation.gates import LockboxTrial, _verify_lockbox_trial, LockboxPrerequisiteError
        run_id = led.conn.execute("SELECT MIN(run_id) FROM runs").fetchone()[0]
        for name in ("G3", "V3"):
            led.record_gate(run_id, name, True, {"segments": []})
        binding = led.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        trial = LockboxTrial({"threshold_pct": 0.}, binding["data_sha256"], binding["cost_model_version"],
                             binding["strategy_sha"], binding["gates_hash"])
        assert _verify_lockbox_trial(led, "H703", trial)["g3_run_id"] == run_id
        from dataclasses import replace
        with pytest.raises(LockboxPrerequisiteError):
            _verify_lockbox_trial(led, "H703", replace(trial, data_sha256="h-input-v1:signal-changed"))
        assert led.conn.execute("SELECT COUNT(*) FROM lockbox_uses").fetchone()[0] == 0


def test_real_cli_dynamic_random_chain_fails_closed_without_null_refill(tmp_path, monkeypatch, capsys):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_dynamic_cli(tmp_path, monkeypatch)
    assert mod.main(["gate", *args, "--gates", "V3", "--params", '{"threshold_pct": 0.0}',
                     "--event-kind", "london_fix"]) == 1
    with Ledger(path) as led:
        detail = json.loads(led.conn.execute("SELECT details_json FROM gates WHERE gate='V3'").fetchone()[0])
        assert not detail["passed"] and "anchor" in detail["error"]
        assert "not closed before" in detail["error"]
        assert "primary" not in detail  # no spurious significant p value


def baseline_module(relpath, name, *, revision="f6c7b8112a73f111b3553f5413521fc75940a81e"):
    """Execute only a trusted committed task blob, not store code."""
    import subprocess
    import sys
    import types
    repo = Path(__file__).resolve().parents[1]
    raw = subprocess.run(["git", "-C", str(repo), "show", revision + ":" + relpath],
                         capture_output=True, check=True).stdout.decode("utf8")
    module = types.ModuleType(name)
    module.__file__ = str(repo / relpath)
    sys.modules[name] = module
    exec(compile(raw, module.__file__, "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("risk", [False, True])
def test_baseline_engine_vs_new_mt5_bitwise_and_external_context(direction, risk):
    from tests.test_backtest_engine_a import FlatCosts, ScriptStrategy
    from mt5quant.sizing import RiskSizing
    from mt5quant.costs import source_costs
    from mt5quant.backtest.report import summarize
    old = baseline_module("src/mt5quant/backtest/engine_a.py", "t112_baseline_engine")
    class Costs(FlatCosts):
        def stop_overshoot(self, symbol, hour, *, price=None, level="p50"): return .2
    costs = Costs(half=.5, slip=.1, commission=2., overnight=-3.)
    # Both bracket levels touched on bar 31; stop must retain priority. Flat
    # overnight segment also exercises swaps, trailing mark and risk sizing.
    bars = disk_bars(pd.date_range("2024-01-02", periods=75, freq="h"))
    bars.loc[31, ["high", "low"]] = [106., 94.]
    st = ScriptStrategy([0] * 15 + [direction] * 16 + [0] * 2 + [direction] * 39 + [0] * 3,
                        exit_rules=ExitRules(stop_loss_atr=2., take_profit_atr=2.))
    kwargs = dict(symbol="X", params={}, contract_size=10., timeframe="H1")
    if risk:
        kwargs.update(sizing=RiskSizing(risk_fraction=.01), vol_min=.01, vol_step=.01, vol_max=10.)
    else:
        kwargs["volume_lots"] = .5
    before = old.run_backtest(st, bars, costs, **kwargs)
    assert len(before.trades) >= 2 and "stop" in set(before.trades.exit_reason)
    assert before.trades.swap.sum() < 0
    for data in (bars, bars.assign(_research_source="mt5")):
        after = run_backtest(st, data, costs, **kwargs)
        pd.testing.assert_frame_equal(before.trades, after.trades, check_exact=True)
        pd.testing.assert_series_equal(before.equity, after.equity, check_exact=True)
        assert before.trades.to_json(date_format="iso") == after.trades.to_json(date_format="iso")
        assert summarize(before) == summarize(after)
    # All-external context must be exactly the existing spread-only model at
    # every call site, including short Ask SL/TP and marks, without new fills.
    expected = old.run_backtest(st, bars, source_costs(costs, "ext:histdata:2024"), **kwargs)
    actual = run_backtest(st, bars.assign(_research_source="ext:histdata:2024"), costs, **kwargs)
    pd.testing.assert_frame_equal(expected.trades, actual.trades, check_exact=True)
    pd.testing.assert_series_equal(expected.equity, actual.equity, check_exact=True)


def test_h_only_candidates_p_functions_constants_and_sampler_unchanged():
    import ast
    import inspect
    import subprocess
    repo = Path(__file__).resolve().parents[1]
    # 漂移守卫基线：candidates.py 自 f6c7b811 不变；events.py 的
    # randomize_event_times 于 T187（H17-1/D74，提交 b1ae153）有意改动——
    # 随机化副本传播 EventWindow.price_timepoint（day_close 事件随机化后
    # 保持同一价格时点口径，否则零假设窗与观测窗口径不一致），基线随之
    # 重钉到该提交（逐条论证见该提交与 docs/reviews/2026-11-08-review-round17.md
    # §4.2 修复要求）。
    pins = {
        "src/mt5quant/research/candidates.py":
            "f6c7b8112a73f111b3553f5413521fc75940a81e",
        "src/mt5quant/validation/events.py": "b1ae153",
    }
    for rel, module, names in (
        ("src/mt5quant/research/candidates.py", candidates,
         ("precheck_prob", "sample_size_evidence", "_sample_size_numbers", "measured_n_required")),
        ("src/mt5quant/validation/events.py", ev, ("randomize_event_times",))):
        old = subprocess.run(["git", "-C", str(repo), "show", pins[rel] + ":" + rel],
                             check=True, capture_output=True).stdout.decode("utf8")
        current = Path(module.__file__).read_text(encoding="utf8")
        def nodes(text):
            return {n.name: ast.get_source_segment(text, n) for n in ast.parse(text).body if isinstance(n, ast.FunctionDef)}
        a, b = nodes(old), nodes(current)
        for name in names: assert a[name] == b[name], name
        if module is candidates:
            def constants(text):
                return {n.targets[0].id: ast.dump(n.value) for n in ast.parse(text).body
                        if isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name) and n.targets[0].id.isupper()
                        and not n.targets[0].id.startswith("_DECLARED")}
            assert constants(old) == constants(current)


def test_real_source_mutation_in_isolated_module_copy_blocks_even_with_enough_data(tmp_path):
    import hashlib
    import os
    import shutil
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[1]
    copied = tmp_path / "copy" / "src"
    shutil.copytree(repo / "src" / "mt5quant", copied / "mt5quant", ignore=shutil.ignore_patterns("__pycache__"))
    # The only expected-hash change is in this subprocess's memory, pointing to
    # copied files. Product gate.py and the seven drift assertions are untouched.
    program = '''
import hashlib, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(1, sys.argv[2])
from mt5quant.research import gate, candidates
from mt5quant.validation import prob_mode
snap = gate.acceptance_snapshot()
gate.EXPECTED_ACCEPTANCE_SOURCE_SHA256.update(snap["sources"])
prob_mode.METHOD_REGISTRY["synthetic-only"] = {"status": "adopted", "adopted": True}
assert gate.research_gate(method_id="synthetic-only", dp=.05, days=10**6).status == "PASS"
p = Path(candidates.__file__)
p.write_text(p.read_text(encoding="utf8") + "\\n# simulated source mutation\\n", encoding="utf8")
blocked = gate.research_gate(method_id="synthetic-only", dp=.05, days=10**9)
assert blocked.status == "FAIL", blocked
assert "src/mt5quant/research/candidates.py" in blocked.acceptance["source_drift"]
assert any("漂移" in reason for reason in blocked.reasons)
print("isolated re-anchor rehearsal PASS; real file drift FAIL even at 1e9 days")
'''
    product_hash = hashlib.sha256((repo / "src/mt5quant/research/gate.py").read_bytes()).hexdigest()
    result = subprocess.run([sys.executable, "-I", "-c", program, str(copied), str(repo / "src")],
                            cwd=repo, env=dict(os.environ, PYTHONUTF8="1"), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "real file drift FAIL" in result.stdout
    assert hashlib.sha256((repo / "src/mt5quant/research/gate.py").read_bytes()).hexdigest() == product_hash


def test_g0_forces_b7_event_cutpoints_and_asynchronous_d1_windows():
    from mt5quant.validation.lookahead import run_g0
    from mt5quant.backtest.truncation import signal_window
    times = pd.date_range("2024-01-01", "2024-04-30 23:00", freq="h")
    bars = disk_bars(times)
    cal = calendar(end="2024-04-30", holidays=["2024-03-29"])
    signal = disk_bars(pd.bdate_range("2023-12-01", "2024-04-30").difference(pd.DatetimeIndex(["2024-03-29"])))
    signal["_research_source"] = "ext:fred:2024"
    signal["close"] = 100. + np.arange(len(signal))
    signal_cal = calendar(start="2023-12-01", end="2024-04-30", holidays=["2024-03-29"])
    st = MonthEndHedgeRebalance(signal_bars=signal, signal_timeframe="D1",
                               signal_calendar=signal_cal, event_calendar=calendar(end="2024-04-30"))
    full = st.generate_signals(bars, {"threshold_pct": 0.})
    positions = np.flatnonzero(full.to_numpy())
    assert len(positions) == 4
    for t in positions:
        for start in (0, max(0, t - st.required_data.lookback_bars + 1)):
            window = bars.iloc[start:t + 1]
            view = signal_window(st, window)
            pd.testing.assert_series_equal(view.generate_signals(window, {"threshold_pct": 0.}).iloc[-1:], full.iloc[t:t + 1])
    assert run_g0(st, bars, {"threshold_pct": 0.}, probes=1, sweep_span=1)["passed"]
    class EventLeak(MonthEndHedgeRebalance):
        def generate_signals(self, bars, params):
            result = super().generate_signals(bars, params)
            if self.signal_bars is not None and len(self.signal_bars):
                result.loc[result != 0] *= 1 if self.signal_bars.time.max().month == 4 else -1
            return result
    leak = EventLeak(signal_bars=signal, signal_timeframe="D1", signal_calendar=signal_cal, event_calendar=st.event_calendar)
    result = run_g0(leak, bars, {"threshold_pct": 0.}, probes=1, sweep_span=1, source="")
    assert not result["passed"] and result["truncation_violations"]


def test_b7_research_cannot_bind_trade_proxy_or_static_rule():
    mod = load_bt()
    for signal_symbol, rule in ((None, "mtd_sign"), ("USTEC", "static_long")):
        ctx = {"declared": SimpleNamespace(signal_symbol=signal_symbol, side_rule=rule, signal_timeframe="H1"),
               "signals": {"EURUSD": signal_bars()}, "calendars": {"EURUSD": calendar(), "USTEC": calendar()}}
        with pytest.raises(ValueError, match="signal|side_rule"):
            mod._bind_h_strategy(MonthEndHedgeRebalance(), ctx, "EURUSD")


def test_b7_shared_direction_module_is_in_strategy_fingerprint_closure():
    from mt5quant.research.cards import strategy_files
    files = strategy_files("mt5quant.strategies.library.b7_monthend_hedge_rebalance:MonthEndHedgeRebalance")
    assert Path(ev.__file__).resolve() in files


def test_d1_unknown_close_requires_explicit_source_bound_mapping():
    bars = disk_bars(pd.to_datetime(["2024-01-30", "2024-02-28"]))
    bars["_research_source"] = "mt5"
    cal = calendar()
    with pytest.raises(ValueError, match="D1|close"):
        ev.signal_close_times(bars, "D1", cal)
    for item in cal["sessions"]:
        item["bar_time"] = item["day"] + " 00:00:00"
        item["price_source"] = "mt5"
    closes = ev.signal_close_times(bars, "D1", cal)
    assert closes.tolist() == [pd.Timestamp("2024-01-30 22:00"), pd.Timestamp("2024-02-28 22:00")]


@pytest.mark.parametrize("command", ["screen", "gate"])
def test_real_cli_signal_missing_or_short_rejects_before_reservation(tmp_path, monkeypatch, capsys, command):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_dynamic_cli(tmp_path, monkeypatch)
    signal_path = root / "ext" / "fred" / "USTEC" / "D1"
    for p in signal_path.iterdir(): p.unlink()
    def tripwire(*a, **kw): raise AssertionError("CALCULATION_REACHED")
    monkeypatch.setattr(mod, "run_backtest", tripwire)
    command_args = [command, *args]
    if command == "gate": command_args += ["--gates", "G0", "--params", '{"threshold_pct":0.0}']
    assert mod.main(command_args) == 1
    out = capsys.readouterr().out
    assert "USTEC/D1" in out and "CALCULATION_REACHED" not in out
    write_external(root, "USTEC", "D1", "fred", disk_bars(pd.bdate_range("2024-03-01", "2024-03-29")))
    assert mod.main(command_args) == 1
    out = capsys.readouterr().out
    assert "runtime coverage USTEC/D1" in out and "CALCULATION_REACHED" not in out
    with Ledger(path) as led:
        assert led.round_n_trials("R1") == 0
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_real_cli_changed_signal_new_reservation_and_no_old_auto_evidence(tmp_path, monkeypatch, capsys):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_dynamic_cli(tmp_path, monkeypatch)
    assert mod.main(["screen", *args]) == 0, capsys.readouterr().out
    source = root / "ext" / "fred" / "USTEC" / "D1" / "data.parquet"
    signal = pd.read_parquet(source)
    signal.loc[signal.time == pd.Timestamp("2024-02-28"), "close"] += .1
    write_external(root, "USTEC", "D1", "fred", signal)
    # Add a valid auto-selection rule to the synthetic card's unbound legacy
    # metadata; this fixture's git registration is already explicitly stubbed.
    card = Path(args[args.index("--cards-dir") + 1]) / "H703.toml"
    text = card.read_text(encoding="utf8")
    card.write_text('selection_rule = "max_net_profit"\n' + text, encoding="utf8")
    assert mod.main(["gate", *args, "--gates", "G0", "--params", "auto"]) == 1
    assert "找不到候选" in capsys.readouterr().out
    with Ledger(path) as led:
        assert led.round_n_trials("R1") == 1
    assert mod.main(["screen", *args]) == 0, capsys.readouterr().out
    with Ledger(path) as led:
        assert led.round_n_trials("R1") == 2 and led.round_n_dedup("R1") == 2
        reservations = led.conn.execute("SELECT stage FROM trial_reservations").fetchall()
        assert [r["stage"] for r in reservations] == ["completed", "completed"]


def setup_candidate_check_chain(tmp_path, monkeypatch, *, precheck_revision=None,
                                signal_symbol="USTEC", signal_timeframe="D1",
                                signal_start="2022-01-03", expected_check=0):
    """Real disk cost/cache and candidate-check CLI; never insert coverage rows."""
    from tests.test_research_candidates import CANDIDATE_TOML
    from tests.test_research_bt_script import _CARD_H703, _ROUND_R1_STANDARD, patch_card_git
    from mt5data.store import save_bars
    from mt5quant.costs import SpreadModel, SpreadStat
    mod = load_bt()
    import run_backtest as rb
    root = tmp_path / "synthetic-store"
    times = pd.date_range("2022-02-01", "2024-03-29 23:00", freq="h")
    times = times[times.weekday < 5]
    trade = disk_bars(times)
    save_bars(str(root), "EURUSD", "H1", trade)
    signal = disk_bars(pd.bdate_range(signal_start, "2024-03-29"), 200.)
    if signal_symbol is not None or signal_timeframe is not None:
        write_external(root, signal_symbol or "EURUSD", signal_timeframe or "H1", "fred", signal,
                       used={str(y): {"used": True} for y in (2022, 2023, 2024)})
    (root / "lockbox.json").write_text(json.dumps({"version": 2, "global_cutoff": "2024-04-01T00:00:00"}), encoding="utf8")
    specs = root / "specs"
    specs.mkdir()
    (specs / "latest.json").write_text(json.dumps({"EURUSD": {
        "contract_size": 1., "swap_mode": 0, "swap_long": 0., "swap_short": 0.,
        "swap_rollover3days": 3, "point": .01}}), encoding="utf8")
    stats = {h: SpreadStat(median=.0002, p90=.0002, p99=.0002, n=1000, n_days=20) for h in range(24)}
    rb.save_spread_model_cache(rb.spread_model_cache_path(str(root)),
        SpreadModel({"EURUSD": stats}, {"EURUSD": .0002}, mode="relative"),
        {"EURUSD": {"rows": 0, "first_msc": 0, "last_msc": 0}})
    cdir, rdir, cards_dir = (tmp_path / p for p in ("candidates", "rounds", "cards"))
    for p in (cdir, rdir, cards_dir): p.mkdir()
    # Ex-ante Sharpe is synthetic-only; >2 years gives enough information for
    # the test round without bypassing power or forging a pass record.
    body = CANDIDATE_TOML.replace("C001", "C003").replace("TESTUSD", "EURUSD").replace('holding = "D1"', 'holding = "H1"')
    body = body.replace("window_bars = 4", "window_bars = 1").replace("overnight_nights = 1", "overnight_nights = 0").replace("expected_sharpe = 1.0", "expected_sharpe = 10.0")
    if signal_symbol is not None:
        body += f'\nsignal_symbol = "{signal_symbol}"\n'
    if signal_timeframe is not None:
        body += f'\nsignal_timeframe = "{signal_timeframe}"\n'
    (cdir / "C003.toml").write_text(body, encoding="utf8")
    path = tmp_path / "ledger.sqlite"
    checking = mod
    if precheck_revision is not None:
        checking = baseline_module("scripts/bt.py", "t112_old_check", revision=precheck_revision)
        checking.cand = baseline_module("src/mt5quant/research/candidates.py", "t112_old_precheck", revision=precheck_revision)
    assert checking.main(["candidate", "check", "C003", "--candidates-dir", str(cdir),
                          "--data-root", str(root), "--ledger", str(path)]) == expected_check
    (rdir / "R1.toml").write_text(_ROUND_R1_STANDARD, encoding="utf8")
    (cards_dir / "H703.toml").write_text(_CARD_H703.replace("XAUUSD", "EURUSD"), encoding="utf8")
    patch_card_git(monkeypatch, mod)
    monkeypatch.setattr(mod.cards, "ensure_strategy_clean", lambda *a, **k: None)
    monkeypatch.setattr(mod.cards, "strategy_fingerprint", lambda *a, **k: "f" * 64)
    if expected_check == 0:
        assert mod.main(["round", "open", "R1", "--rounds-dir", str(rdir), "--ledger", str(path)]) == 0
    args = ["--card", "H703", "--cards-dir", str(cards_dir), "--data-root", str(root), "--ledger", str(path),
            "--strategy", "mt5quant.strategies.library.buy_hold:BuyHold", "--costs", "zero", "--volume-lots", "1"]
    return mod, root, path, args


def test_r1_candidate_check_records_trade_and_signal_coverage(tmp_path, monkeypatch):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_candidate_check_chain(tmp_path, monkeypatch)
    with Ledger(path) as led:
        payload = json.loads(led.candidate_checks("C003")[-1]["result_json"])
    power = payload["checks"]["power"]
    assert set(power["years_detail"]) == {"EURUSD/H1", "USTEC/D1"}
    assert power["years_detail"]["USTEC/D1"] == pytest.approx(816 / 365.25)
    assert [s["source"] for s in power["coverage_segments"]["USTEC/D1"] if s["counted"]] == ["fred"]
    assert set(power["sigma_by_symbol"]) == {"EURUSD"}
    assert set(payload["checks"]["ex_ante_cost"]["per_symbol"]) == {"EURUSD"}
    assert set(json.loads((root / "specs/latest.json").read_text(encoding="utf8"))) == {"EURUSD"}


@pytest.mark.parametrize("command", ["screen", "gate"])
def test_r1_candidate_check_to_screen_gate_uses_signal_only_fred(tmp_path, monkeypatch, capsys, command):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_candidate_check_chain(tmp_path, monkeypatch)
    command_args = [command, *args]
    if command == "gate": command_args += ["--gates", "G0", "--params", "{}"]
    assert mod.main(command_args) == 0, capsys.readouterr().out
    with Ledger(path) as led:
        row = led.conn.execute("SELECT metrics_json FROM runs").fetchone()
        coverage = json.loads(row["metrics_json"])["metrics"]["runtime_coverage"]
        assert coverage["USTEC/D1"]["segments"][0]["source"] == "ext:fred:2022"
        assert {s["source"] for s in coverage["EURUSD/H1"]["segments"]} == {"mt5"}


@pytest.mark.parametrize("command", ["screen", "gate"])
def test_r1_old_precheck_missing_aux_baseline_cannot_borrow_trade_years(tmp_path, monkeypatch, capsys, command):
    from mt5data.store import save_bars
    from mt5quant.research.ledger import Ledger
    # Genuine previous implementation produces the incomplete ledger row; no
    # hand-inserted signal coverage, no mutation/rechain of an existing row.
    mod, root, path, args = setup_candidate_check_chain(tmp_path, monkeypatch, precheck_revision="6f33ad585d810d17ff7cce1794ec8d15f65df0b3")
    old = baseline_module("src/mt5quant/research/candidates.py", "t112_old_candidate", revision="6f33ad585d810d17ff7cce1794ec8d15f65df0b3")
    old_bt = baseline_module("scripts/bt.py", "t112_old_candidate_cli", revision="6f33ad585d810d17ff7cce1794ec8d15f65df0b3")
    old_bt.cand = old
    cdir = Path(args[args.index("--cards-dir") + 1]).parent / "candidates"
    assert old_bt.main(["candidate", "check", "C003", "--candidates-dir", str(cdir), "--data-root", str(root), "--ledger", str(path)]) == 0
    # Auxiliary MT5 bars would let the faulty loader silently succeed by taking
    # overall trade years. No calendar is needed for this standard H fixture.
    save_bars(str(root), "USTEC", "D1", disk_bars(pd.bdate_range("2022-01-03", "2024-03-29"), 200.))
    with Ledger(path) as led:
        power = json.loads(led.candidate_checks("C003")[-1]["result_json"])["checks"]["power"]
        assert "USTEC/D1" not in power["years_detail"] and power["years"] > 0
    command_args = [command, *args]
    if command == "gate": command_args += ["--gates", "G0", "--params", "{}"]
    assert mod.main(command_args) == 1, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "USTEC/D1" in out and "candidate check" in out
    with Ledger(path) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert led.round_n_trials("R1") == 0


@pytest.mark.parametrize("signal_symbol", ["EURUSD", None])
def test_r1_same_symbol_distinct_timeframe_collects_exact_pairs(tmp_path, monkeypatch, capsys, signal_symbol):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_candidate_check_chain(tmp_path, monkeypatch,
                                                       signal_symbol=signal_symbol, signal_timeframe="D1")
    with Ledger(path) as led:
        power = json.loads(led.candidate_checks("C003")[-1]["result_json"])["checks"]["power"]
        assert set(power["years_detail"]) == {"EURUSD/H1", "EURUSD/D1"}
        assert {s["source"] for s in power["coverage_segments"]["EURUSD/D1"] if s["counted"]} == {"fred"}
    assert mod.main(["screen", *args]) == 0, capsys.readouterr().out
    assert mod.main(["gate", *args, "--gates", "G0", "--params", "{}"]) == 0, capsys.readouterr().out
    with Ledger(path) as led:
        assert led.round_n_trials("R1") == 1
        rows = led.conn.execute("SELECT metrics_json FROM runs").fetchall()
        for r in rows:
            coverage = json.loads(r["metrics_json"])["metrics"]["runtime_coverage"]
            assert set(coverage) == {"EURUSD/H1", "EURUSD/D1"}
            assert coverage["EURUSD/D1"]["segments"][0]["source"] == "ext:fred:2022"


def test_r1_exact_pairs_dedup_no_cross_product():
    declared = SimpleNamespace(signal_symbol="USTEC", signal_timeframe="D1")
    assert candidates.required_input_pairs(("EURUSD", "USDJPY", "EURUSD"), "H1", declared) == (
        ("EURUSD", "H1"), ("USDJPY", "H1"), ("USTEC", "D1"))
    assert candidates.required_input_pairs(("EURUSD",), "H1", SimpleNamespace(signal_symbol="EURUSD", signal_timeframe="H1")) == (("EURUSD", "H1"),)


def test_r1_short_signal_governs_precheck_and_runtime_uses_own_baseline(tmp_path, monkeypatch, capsys):
    from mt5quant.research.ledger import Ledger
    from mt5data.ext.merge import merged_research_series
    mod, root, path, args = setup_candidate_check_chain(tmp_path, monkeypatch,
        signal_start="2024-03-01", expected_check=2)
    with Ledger(path) as led:
        payload = json.loads(led.candidate_checks("C003")[-1]["result_json"])
        power = payload["checks"]["power"]
        assert power["years_detail"]["EURUSD/H1"] > 2
        assert power["years_detail"]["USTEC/D1"] == pytest.approx(28 / 365.25)
        assert power["years"] == pytest.approx(28 / 365.25)
        assert payload["decision"] == "waiting_data"
    cdir = Path(args[args.index("--cards-dir") + 1]).parent / "candidates"
    # Restore an ample signal and genuinely rerun the CLI precheck; then shorten
    # only the file, proving runtime compares to that signal's own ledger years.
    write_external(root, "USTEC", "D1", "fred", disk_bars(pd.bdate_range("2022-01-03", "2024-03-29"), 200.),
                   used={str(y): {"used": True} for y in (2022, 2023, 2024)})
    assert mod.main(["candidate", "check", "C003", "--candidates-dir", str(cdir), "--data-root", str(root), "--ledger", str(path)]) == 0
    write_external(root, "USTEC", "D1", "fred", disk_bars(pd.bdate_range("2024-03-01", "2024-03-29"), 200.))
    card = mod.cards.load_card(Path(args[args.index("--cards-dir") + 1]) / "H703.toml")
    with Ledger(path) as led:
        with pytest.raises(ValueError, match="runtime coverage USTEC/D1"):
            mod._load_h_research_inputs(led, card, candidate_id="C003", declared=candidates.load_candidate(cdir / "C003.toml").declared,
                data_root=str(root), end=None, allow_unbounded=False, load_plain=mod.load_series, load_merged=merged_research_series)


def test_r1_missing_signal_is_zero_coverage_not_trade_substitution(tmp_path, monkeypatch):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_candidate_check_chain(tmp_path, monkeypatch)
    for p in (root / "ext/fred/USTEC/D1").iterdir(): p.unlink()
    cdir = Path(args[args.index("--cards-dir") + 1]).parent / "candidates"
    assert mod.main(["candidate", "check", "C003", "--candidates-dir", str(cdir), "--data-root", str(root), "--ledger", str(path)]) == 2
    with Ledger(path) as led:
        power = json.loads(led.candidate_checks("C003")[-1]["result_json"])["checks"]["power"]
    assert power["years_detail"]["EURUSD/H1"] > 2
    assert power["years"] == power["years_detail"]["USTEC/D1"] == 0.
    assert power["coverage_segments"]["USTEC/D1"] == []


def test_r1_legacy_h_no_fields_preserves_precheck_and_single_input(tmp_path, monkeypatch, capsys):
    from mt5quant.research.ledger import Ledger
    mod, root, path, args = setup_candidate_check_chain(tmp_path, monkeypatch, signal_symbol=None, signal_timeframe=None)
    with Ledger(path) as led:
        initial = json.loads(led.candidate_checks("C003")[-1]["result_json"])
    old = baseline_module("scripts/bt.py", "t112_legacy_h_cli", revision="6f33ad585d810d17ff7cce1794ec8d15f65df0b3")
    old.cand = baseline_module("src/mt5quant/research/candidates.py", "t112_legacy_h_candidate", revision="6f33ad585d810d17ff7cce1794ec8d15f65df0b3")
    cdir = Path(args[args.index("--cards-dir") + 1]).parent / "candidates"
    assert old.main(["candidate", "check", "C003", "--candidates-dir", str(cdir), "--data-root", str(root), "--ledger", str(path)]) == 0
    with Ledger(path) as led:
        previous = json.loads(led.candidate_checks("C003")[-1]["result_json"])
    assert initial["checks"] == previous["checks"] and initial["decision"] == previous["decision"]
    assert mod.main(["screen", *args]) == 0, capsys.readouterr().out
    assert mod.main(["gate", *args, "--gates", "G0", "--params", "{}"]) == 0, capsys.readouterr().out
    with Ledger(path) as led:
        assert led.round_n_trials("R1") == 1
