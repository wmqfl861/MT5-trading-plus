"""T121 offline rules: every input below is generated, never a research store."""
import importlib
import importlib.util
import copy
import hashlib
import json

import pandas as pd
import pytest

from mt5data.sessions import SessionPolicy
from mt5quant.prob.counting import count_triggers, level_rows_for_situation
from mt5quant.prob.gaps import eligibility_reasons
from mt5quant.strategies.library.prob.actions import ActionParams
from mt5quant.strategies.library.prob.situations import Approach


@pytest.fixture
def feasibility():
    from types import SimpleNamespace
    if importlib.util.find_spec("mt5quant.prob.feasibility") is None:
        return SimpleNamespace()  # Assertions fail in tests on missing entry points.
    return importlib.import_module("mt5quant.prob.feasibility")


def bars(n=16, start="2015-01-05", freq="h"):
    return pd.DataFrame({"time": pd.date_range(start, periods=n, freq=freq),
                         "open": 100., "high": 101., "low": 99., "close": 100.})


def test_h4_requires_four_distinct_consecutive_closed_roots(feasibility):
    h1 = bars()
    h4, tags, excluded = feasibility.prepare_bars(h1, ["mt5"] * 16, "H4")
    assert list(h4.time) == list(pd.date_range("2015-01-05", periods=4, freq="4h"))
    assert tags == ["mt5"] * 4
    assert excluded == {}
    missing = h1.drop(index=1).reset_index(drop=True)
    out, _, excluded = feasibility.prepare_bars(missing, ["mt5"] * 15, "H4")
    assert len(out) == 3 and excluded == {"incomplete_h4": 1}
    # Four rows alone do not prove four distinct roots.
    duplicate = h1.copy()
    duplicate.loc[1, "time"] = duplicate.loc[0, "time"]
    with pytest.raises(ValueError, match="duplicate"):
        feasibility.prepare_bars(duplicate, ["mt5"] * 16, "H4")
    cut, _, excluded = feasibility.prepare_bars(
        bars(6, "2016-04-30 20:00"), ["mt5"] * 6, "H4")
    assert list(cut.time) == [pd.Timestamp("2016-04-30 20:00")]
    assert excluded["outside_discovery"] == 2


@pytest.mark.parametrize("source,expected", [("mt5", "mt5"),
    ("ext:HistData:2015", "external"), (None, None), ("mystery", None)])
def test_h4_source_is_conservative_and_unknown_is_not_dropped(feasibility, source, expected):
    tags = ["mt5"] * 4
    tags[2] = source
    out, sources, _ = feasibility.prepare_bars(bars(4), tags, "H4")
    assert len(out) == 1
    assert sources == [expected]


@pytest.mark.parametrize("start,bucket,next_bucket,expected_start", [
    ("2015-03-06", "2015-03-08 20:00", "2015-03-09 00:00", "2015-03-06 22:00"),
    ("2015-10-30", "2015-11-01 20:00", "2015-11-02 00:00", "2015-10-30 21:00"),
])
def test_s2_h1_sessions_are_bound_at_h4_open_across_dst(
        feasibility, start, bucket, next_bucket, expected_start):
    h1 = bars(100, start)
    h1["high"] = range(1000, 1100)
    targets = pd.Series(pd.to_datetime([bucket, next_bucket]))
    levels, starts = feasibility.s2_levels(h1, targets)
    assert starts.iloc[0] == pd.Timestamp(expected_start)
    # The later session contains higher prices, but is unavailable at bucket open.
    assert max(levels.iloc[1]) > max(levels.iloc[0])
    altered = h1.copy()
    altered.loc[altered.time >= pd.Timestamp(bucket), "high"] = 999999.
    earlier, _ = feasibility.s2_levels(altered, targets.iloc[:1])
    assert earlier.iloc[0] == levels.iloc[0]
    # A start lying inside a UTC H4 bar must not be rounded to the next H4.
    h4 = bars(30, start, "4h")
    i = int(h4.index[h4.time == pd.Timestamp(bucket)][0])
    trig = pd.DataFrame({"bar": [i], "anchor": [i], "action": ["A2"]})
    full_starts = [pd.Timestamp(expected_start)] * len(h4)
    gap = [(pd.Timestamp(expected_start), pd.Timestamp(expected_start) + pd.Timedelta(hours=1))]
    assert eligibility_reasons(trig, h4, gap, "S2", 24, feature_warmup=0,
                               situation_starts=full_starts) == ["situation_gap"]


@pytest.mark.parametrize("per_day,window", [(24, 480), (6, 120)])
def test_s3_level_and_anchor_eligibility_share_period(per_day, window):
    b = bars(520, freq="4h" if per_day == 6 else "h")
    b.loc[window, "high"] = 9999
    levels = level_rows_for_situation(b, "S3", bars_per_day=per_day)
    assert levels.iloc[window - 1] == []
    assert levels.iloc[window] == [99., 101.]
    assert levels.iloc[window + 1] == [99., 9999.]
    trig = pd.DataFrame({"bar": [window + 15], "anchor": [window + 5], "action": ["A4"]})
    gap = [(b.time.iloc[5], b.time.iloc[6])]
    assert eligibility_reasons(trig, b, gap, "S3", 24, s3_bars=window,
                               feature_warmup=0) == ["situation_gap"]
    later = [(b.time.iloc[window + 8], b.time.iloc[window + 9])]
    assert eligibility_reasons(trig, b, later, "S3", 24, s3_bars=window,
                               feature_warmup=0) == ["anchor_action_gap"]


def test_count_accepts_explicit_levels_actual_starts_and_periods():
    b = bars(40)
    b.loc[:25, ["high", "low", "close"]] = [91., 89., 90.]
    b.loc[26, ["high", "low", "close"]] = [100., 94., 99.]
    levels = pd.Series([[]] * 26 + [[100.]] + [[]] * 13)
    kw = dict(symbol="XAUUSD", situation="S2", approach=Approach(24, 1., .25),
              action_params=ActionParams(.25, 6), atr=pd.Series([2.] * 40),
              feature_warmup=0, levels_by_bar=levels, bars_per_day=6, s3_bars=120,
              situation_starts=[b.time.iloc[0]] * 40)
    assert (count_triggers(b, **kw).action == "A3").any()
    assert count_triggers(b, gap_spans=[(b.time.iloc[1], b.time.iloc[2])], **kw).empty


def test_qualification_uses_entry_and_k_plus_one_timestamp_only(feasibility):
    b = bars(2030, "2016-02-01")
    trig = pd.DataFrame({"bar": [2000, 2005, 2006], "anchor": [2000, 2005, 2006],
                         "action": ["A3"] * 3})
    kept, excluded = feasibility.qualify_triggers(
        trig, b[["time"]], symbol="XAUUSD", situation="S1", hold_bars=24,
        gap_spans=[], sessions=SessionPolicy())
    assert kept.bar.tolist() == [2000]
    assert excluded == {"future_window": 2}
    # An exit at the cutoff is not available, even though that timestamp exists.
    b.loc[2025:, "time"] = pd.date_range("2016-05-01", periods=5, freq="h")
    kept, excluded = feasibility.qualify_triggers(
        trig.iloc[:1], b[["time"]], symbol="XAUUSD", situation="S1", hold_bars=24,
        gap_spans=[], sessions=SessionPolicy())
    assert kept.empty and excluded == {"discovery_cutoff": 1}


def test_atr_pure_helper_is_identical_to_existing_production_formula():
    from mt5quant.backtest.engine_a import atr_series as engine_atr
    from mt5quant.costs.volatility import atr_series
    b = bars(40)
    b.loc[25, ["high", "low", "close"]] = [105., 98., 104.]
    pd.testing.assert_series_equal(atr_series(b), engine_atr(b))


def seal(evidence):
    evidence["payload_sha256"] = hashlib.sha256(json.dumps(
        evidence["payload"], sort_keys=True, separators=(",", ":"),
        allow_nan=True).encode()).hexdigest()
    return evidence


def cost_evidence():
    return seal({
        "kind": "synthetic", "purpose": "T121-count-cost", "symbol": "XAUUSD",
        "account_kind": "Demo", "account_currency": "USD", "quote_currency": "USD",
        "basis_ref": "synthetic-fixture-only", "applies_start": "2000-01-01",
        "applies_end": "2016-05-01", "observed_start": "2014-01-01",
        "observed_end": "2014-12-31",
        "payload": {
            "spread": {"mode": "absolute", "hours": {
                "6": {"p90": .1, "n_days": 20},
                "7": {"p90": .2, "n_days": 21},
                "8": {"p90": 999., "n_days": 19}}},
            "slippage": {"spread_fraction": .1, "fixed_price": 0.,
                         "event_hours": [6], "event_multiplier": 2.},
            "overshoot": {"version": 2, "basis": "stop_level", "hours": {
                "6": {"p90": .001, "n_days": 20},
                "7": {"p90": .002, "n_days": 20},
                "8": {"p90": 999., "n_days": 19}}},
            "commission": {"round_trip_per_lot": 2., "basis": "explicit_fee_schedule"},
            "contract": {"size": 100., "unit": "oz"},
            "swap": {"basis": "free", "scope": "Demo"}}})


def trigger_cost(feasibility, evidence=None, **over):
    kw = dict(symbol="XAUUSD", signal_time=pd.Timestamp("2015-01-05 10:00"),
              entry_time=pd.Timestamp("2015-01-05 11:00"),
              exit_time=pd.Timestamp("2015-01-06 11:00"),
              side=1, source="mt5", signal_price=100., atr=10., sl_atr=1,
              evidence=cost_evidence() if evidence is None else evidence)
    kw.update(over)
    return feasibility.trigger_cost(**kw)


def test_cost_uses_worst_qualified_hours_and_external_spread_only(feasibility):
    # max spread .2 + entry slip .02 + worst slip .02 + commission .02
    # + p90 overshoot .002 * known signal price 100 = .46, divided by R=10.
    normal = trigger_cost(feasibility)
    external = trigger_cost(feasibility, source="external")
    assert normal["cost_r"] == pytest.approx(.046)
    assert external["cost_r"] == pytest.approx(.056)
    assert normal["cost_gate"] == "fail"  # .046 > registered .02
    assert trigger_cost(feasibility, sl_atr=2)["cost_r"] == pytest.approx(.023)


@pytest.mark.parametrize("field", ["purpose", "payload_sha256", "basis_ref",
    "applies_start", "applies_end", "observed_start", "observed_end", "account_kind"])
def test_cost_missing_evidence_never_becomes_zero(feasibility, field):
    evidence = cost_evidence()
    del evidence[field]
    out = trigger_cost(feasibility, evidence)
    assert out["cost_r"] is None and out["waiting_reasons"]


@pytest.mark.parametrize("change", ["hash", "range", "2026", "purpose", "production",
    "currency", "overshoot_version", "overshoot_basis", "entry_coverage", "empty_hours"])
def test_cost_rejects_wrong_identity_scope_range_or_coverage(feasibility, change):
    e = cost_evidence()
    if change == "hash":
        e["payload"]["commission"]["round_trip_per_lot"] = 0.
    elif change == "range":
        e["applies_start"] = "2015-02-01"
    elif change == "2026":
        e["observed_end"] = "2026-01-01"
    elif change == "purpose":
        e["purpose"] = "operations"
    elif change == "production":
        e["kind"] = "production"
        e["permission"] = "approved"  # a claim cannot release execution
    elif change == "currency":
        e["account_currency"] = "EUR"
    elif change.startswith("overshoot_"):
        e["payload"]["overshoot"]["version" if change.endswith("version") else "basis"] = 1
        seal(e)
    elif change == "entry_coverage":
        e["payload"]["spread"]["hours"]["6"]["n_days"] = 19
        seal(e)
    elif change == "empty_hours":
        e["payload"]["spread"]["hours"] = {}
        seal(e)
    out = trigger_cost(feasibility, e)
    assert out["cost_r"] is None and out["waiting_reasons"]


@pytest.mark.parametrize("component", ["spread", "slippage", "commission", "contract", "overshoot", "swap"])
def test_every_cost_component_requires_explicit_evidence(feasibility, component):
    e = cost_evidence()
    del e["payload"][component]
    seal(e)
    out = trigger_cost(feasibility, e)
    assert out["cost_r"] is None and f"missing_{component}" in out["waiting_reasons"]


@pytest.mark.parametrize("field,value", [("signal_price", 0.), ("signal_price", float("nan")),
    ("atr", -1.), ("atr", float("inf")), ("sl_atr", 0.)])
def test_invalid_signal_price_atr_or_r_is_cost_fail(feasibility, field, value):
    out = trigger_cost(feasibility, **{field: value})
    assert out["cost_gate"] == "fail" and out["fail_reasons"]
    assert out["cost_r"] is None


@pytest.mark.parametrize("component,key,value", [("contract", "size", 0.),
    ("contract", "size", float("nan")), ("commission", "round_trip_per_lot", -1.),
    ("slippage", "fixed_price", float("inf"))])
def test_invalid_components_fail_even_with_another_component_missing(feasibility, component, key, value):
    e = cost_evidence()
    e["payload"][component][key] = value
    del e["payload"]["overshoot"]
    seal(e)
    out = trigger_cost(feasibility, e)
    assert out["cost_gate"] == "fail" and out["fail_reasons"]
    assert "missing_overshoot" in out["waiting_reasons"]


def test_swap_weekend_dst_uses_endpoints_and_negative_fee_as_charge(feasibility):
    e = cost_evidence()
    e["payload"]["swap"] = {"basis": "charged", "scope": "Demo",
        "long_per_day": -10., "short_per_day": -20., "triple_weekdays": [2],
        "charge_weekdays": [0, 1, 2, 3, 4], "ref_price": 100.}
    seal(e)
    # Fri before rollover -> Monday 17:00 EDT inclusive: Fri + Mon = 2 nights.
    out = trigger_cost(feasibility, e, signal_time=pd.Timestamp("2015-03-06 09:00"),
        entry_time=pd.Timestamp("2015-03-06 11:00"), exit_time=pd.Timestamp("2015-03-09 21:00"))
    assert out["cost_r"] == pytest.approx(.066)
    # Wed exactly at rollover excluded, Thursday rollover included: one night.
    out = trigger_cost(feasibility, e, entry_time=pd.Timestamp("2015-03-11 11:00"),
                       exit_time=pd.Timestamp("2015-03-12 21:00"))
    assert out["cost_r"] == pytest.approx(.086)  # Wednesday 3 + Thursday 1


def test_positive_overnight_fee_is_rejected_not_absolute_valued(feasibility, monkeypatch):
    from mt5quant.costs.swap import SwapModel
    # Malformed external model contract: all other computation stays real.
    monkeypatch.setattr(SwapModel, "overnight_fee", lambda *a, **k: 1.)
    e = cost_evidence()
    e["payload"]["swap"] = {"basis": "charged", "scope": "Demo", "long_per_day": -1.,
        "short_per_day": -1., "triple_weekdays": [2], "charge_weekdays": [0, 1, 2, 3, 4]}
    out = trigger_cost(feasibility, seal(e))
    assert out["cost_gate"] == "fail" and "invalid_overnight_fee" in out["fail_reasons"]


def test_day_aggregation_uses_trigger_utc_days_and_never_drops_missing(feasibility):
    times = pd.to_datetime(["2015-01-05 20:00", "2015-01-05 23:00", "2015-01-07 00:00"])
    costs = [{"cost_r": v, "waiting_reasons": [], "fail_reasons": []}
             for v in [.01, .03, .06]]
    out = feasibility.summarize_hypothesis(times, costs)
    assert out["n_triggers"] == 3 and out["n_days"] == 2
    assert out["cost_r"] == pytest.approx(.04)  # mean(mean(.01,.03), .06)
    assert out["triggers_per_day"] == {"p50": 1.5, "p90": 1.9, "max": 2}
    assert out["adjacent_day_gaps"] == [2]
    costs[1] = {"cost_r": None, "waiting_reasons": ["missing_spread"], "fail_reasons": []}
    out = feasibility.summarize_hypothesis(times, costs)
    assert out["n_triggers"] == 3 and out["n_days"] == 2 and out["cost_r"] is None
    assert out["status"] == "waiting_data" and "missing_spread" in out["waiting_reasons"]


def input_identity(b, tags, gaps):
    rows = b.copy()
    rows["time"] = pd.to_datetime(rows.time).map(lambda t: t.isoformat())
    payload = {"bars": rows.to_dict("records"), "source_tags": tags,
               "gap_spans": [[pd.Timestamp(s).isoformat(), pd.Timestamp(e).isoformat()] for s, e in gaps]}
    return {"kind": "synthetic", "purpose": "T121-count-cost",
            "payload_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True,
                separators=(",", ":"), allow_nan=True).encode()).hexdigest(),
            "range_start": b.time.min().isoformat(),
            "range_end": (b.time.max() + pd.Timedelta(hours=1)).isoformat(),
            "source_manifest": {"used_years": sorted(set(b.time.dt.year)),
                "sources": ["mt5", "ext:HistData"], "excluded_months": []}}


def frozen_sessions():
    return SessionPolicy(exempt_symbols=frozenset({"BTCUSD"}))


def run_one(feasibility, b=None, tags=None, identity=None, evidence=None):
    b = bars(2100, "2015-01-05") if b is None else b
    tags = ["mt5"] * len(b) if tags is None else tags
    return feasibility.run_feasibility(
        {"XAUUSD": b}, source_tags={"XAUUSD": tags}, gap_spans={"XAUUSD": []},
        sessions=frozen_sessions(), input_evidence={"XAUUSD": input_identity(b, tags, []) if identity is None else identity},
        cost_evidence={"XAUUSD": cost_evidence() if evidence is None else evidence})


def test_complete_matrix_unknown_vs_actual_zero_and_constant_hypotheses(feasibility):
    report = run_one(feasibility)
    assert len(report["rows"]) == 96 and len(report["summaries"]) == 8
    assert len({(r["family"], r["symbol"], r["situation"], r["action"]) for r in report["rows"]}) == 96
    for row in report["rows"]:
        assert row["delta_ev_r"] == .06 and row["n_required"] == 4521
        assert row["n_triggers"] == (0 if row["symbol"] == "XAUUSD" else None)
        assert row["n_days"] == (0 if row["symbol"] == "XAUUSD" else None)
        assert row["cost_r"] is None
        assert row["hypothesis"] == ("follow>base" if row["action"] in ("A1", "A2") else "fade>base")
        assert row["sl_atr"] == (1 if row["family"] in ("F1", "F3") else 2)
        assert row["hold_bars"] == (24 if row["family"] in ("F1", "F2") else 12)
        assert row["status"] == "waiting_data"


def test_sample_and_cost_equalities_and_independent_fail_waiting(feasibility):
    times = pd.date_range("2001-01-01", periods=4521, freq="D")
    equal = {"cost_r": .02, "waiting_reasons": [], "fail_reasons": []}
    passed = feasibility.summarize_hypothesis(times, [equal] * 4521)
    assert passed["status"] == "feasible" and passed["cost_gate"] == passed["sample_gate"] == "pass"
    zero = feasibility.summarize_hypothesis(times, [{**equal, "cost_r": 0.}] * 4521)
    assert zero["cost_r"] == 0. and zero["status"] == "feasible"
    failed = feasibility.summarize_hypothesis(times, [{**equal, "cost_r": .020001}] * 4521)
    assert failed["status"] == "fail" and failed["cost_gate"] == "fail"
    missing = feasibility.summarize_hypothesis(times[:1], [{"cost_r": None,
        "waiting_reasons": ["missing_spread"], "fail_reasons": ["invalid_contract_size"]}])
    assert missing["status"] == "waiting_data" and missing["cost_gate"] == "fail"
    assert missing["fail_reasons"] == ["invalid_contract_size"]
    assert "insufficient_days" in missing["waiting_reasons"]
    small = feasibility.summarize_hypothesis(times[:1], [{**equal, "cost_r": .03}])
    assert small["cost_gate"] == "fail" and small["status"] == "waiting_data"


@pytest.mark.parametrize("change", ["hash", "range", "purpose", "production", "manifest", "used_years"])
def test_bad_input_admission_never_counts(feasibility, change):
    b = bars(30)
    tags = ["mt5"] * len(b)
    identity = input_identity(b, tags, [])
    if change == "hash":
        identity["payload_sha256"] = "a" * 64
    elif change == "range":
        identity["range_start"] = "2015-02-01"
    elif change == "purpose":
        identity["purpose"] = "operations"
    elif change == "production":
        identity["kind"] = "production"
        identity["execution_approved"] = True
    elif change == "manifest":
        del identity["source_manifest"]
    else:
        identity["source_manifest"]["used_years"] = []
    report = run_one(feasibility, b, tags, identity)
    assert all(r["n_triggers"] is None and r["status"] == "waiting_data" for r in report["rows"])


def test_unknown_source_affects_full_row_without_changing_qualification(feasibility):
    b = bars(2200)
    # A3 reversal at the registered XAUUSD 5-unit grid after warmup.
    b.loc[2050, ["open", "high", "low", "close"]] = [100., 105., 99., 104.]
    good = run_one(feasibility, b)
    tags = ["mt5"] * len(b)
    tags[2050] = None
    unknown = run_one(feasibility, b, tags)
    key = lambda r: r["family"] == "F1" and r["symbol"] == "XAUUSD" and r["situation"] == "S1" and r["action"] == "A3"
    a, z = next(filter(key, good["rows"])), next(filter(key, unknown["rows"]))
    assert a["n_triggers"] > 0 and z["n_triggers"] == a["n_triggers"]
    assert z["n_days"] == a["n_days"] and z["cost_r"] is None
    assert "source_unknown" in z["waiting_reasons"]


def test_unfrozen_sessions_are_rejected(feasibility):
    with pytest.raises(ValueError, match="frozen sessions"):
        feasibility.run_feasibility({}, source_tags={}, gap_spans={},
            sessions=SessionPolicy(pre_minutes=0), input_evidence={}, cost_evidence={})


def test_future_prices_do_not_change_existing_trigger_cost_or_qualification(feasibility):
    b = bars(2200)
    b.loc[2050, ["open", "high", "low", "close"]] = [100., 105., 99., 104.]
    original = run_one(feasibility, b)
    b.loc[2051:, ["open", "high", "low", "close"]] = [100., 101., 99., 100.5]
    changed = run_one(feasibility, b)
    key = lambda r: r["family"] == "F1" and r["symbol"] == "XAUUSD" and r["situation"] == "S1" and r["action"] == "A3"
    a, z = next(filter(key, original["rows"])), next(filter(key, changed["rows"]))
    assert a["n_triggers"] > 0
    assert a == z


def test_no_label_fit_return_or_execution_dependencies(feasibility, monkeypatch):
    import ast
    import builtins
    import inspect
    from pathlib import Path
    from mt5quant.prob import counting, gaps
    forbidden = ("prob.labels", "prob.tables", "backtest.engine", "research.candidates", "prob.p1")
    for module in (feasibility, counting, gaps):
        tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ImportFrom, ast.Import)):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                assert not any(s in n for s in forbidden for n in names)
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                assert name not in {"fit", "label_triggers", "run_backtest", "pct_change", "default_rng", "seed"}
    original_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if any(s in name for s in forbidden):
            raise AssertionError("forbidden execution dependency: " + name)
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    # Reload protects against already-cached imports escaping the tripwire.
    importlib.reload(counting)
    importlib.reload(feasibility)
    report = run_one(feasibility)
    prohibited = {"net_r", "returns", "win", "loss", "outcome", "exit_price", "realized_delta_p", "pnl"}
    assert all(prohibited.isdisjoint(r) for r in report["rows"])


def test_cli_help_identity_receipts_and_overwrite_refusal(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/pline_feasibility.py"
    assert script.is_file(), "thin offline CLI is missing"
    env = {**os.environ, "PYTHONUTF8": "1"}
    help_ = subprocess.run([sys.executable, str(script), "--help"], cwd=root, env=env,
                           capture_output=True, text=True)
    assert help_.returncode == 0 and "--input" in help_.stdout and "--approve" not in help_.stdout
    b = bars(30)
    tags = ["mt5"] * 30
    rows = b.copy()
    rows["time"] = rows.time.map(lambda t: t.isoformat())
    input_ = {"symbols": {"XAUUSD": {"bars": rows.to_dict("records"), "source_tags": tags,
        "gap_spans": [], "evidence": input_identity(b, tags, [])}}}
    input_path, evidence_path, out = tmp_path / "input.json", tmp_path / "costs.json", tmp_path / "out"
    input_path.write_text(json.dumps(input_), encoding="utf-8")
    evidence_path.write_text(json.dumps({"XAUUSD": cost_evidence()}), encoding="utf-8")
    args = [sys.executable, str(script), "--input", str(input_path), "--cost-evidence", str(evidence_path),
            "--sessions", str(root / "config/sessions.toml"), "--output", str(out)]
    done = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    result = json.loads((out / "report.json").read_text(encoding="utf-8"))
    receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
    assert len(result["rows"]) == 96
    assert receipt["input_sha256"] == hashlib.sha256(input_path.read_bytes()).hexdigest()
    assert receipt["cost_evidence_sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    assert receipt["report_sha256"] == hashlib.sha256((out / "report.json").read_bytes()).hexdigest()
    assert len(receipt["code_sha"]) == 40 and receipt["code_files_sha256"]
    assert receipt["prereg_sha256"] == "c78a78d13ed81c611b5f9599c96235d7bbcefceac6fda54566b103f3e8cb3235"
    assert receipt["sessions_sha256"] and receipt["qualification_sha256"]
    assert result["inputs"]["XAUUSD"]["source_segments"]
    assert result["inputs"]["XAUUSD"]["range_start"] == "2015-01-05T00:00:00"
    assert str(tmp_path) not in json.dumps(receipt)
    first = (out / "report.json").read_bytes()
    again = subprocess.run(args, cwd=root, env=env, capture_output=True, text=True)
    assert again.returncode != 0 and (out / "report.json").read_bytes() == first


# Independent review fix round 1: reproduce all findings before changing code.
def charged_evidence():
    e = cost_evidence()
    e["payload"]["spread"]["hours"] = {"6": {"p90": .02, "n_days": 20}}
    e["payload"]["slippage"]["event_hours"] = []
    e["payload"]["overshoot"]["hours"] = {"6": {"p90": .0001, "n_days": 20}}
    e["payload"]["commission"]["round_trip_per_lot"] = .2
    e["payload"]["swap"] = {"basis": "charged", "scope": "Demo",
        "long_per_day": -20., "short_per_day": -20.,
        "charge_weekdays": [0, 1, 2, 3, 4], "triple_weekdays": [2]}
    return seal(e)


def test_fix1_valid_weekdays_charge_actual_endpoint_nights(feasibility):
    # (.02 spread + .004 slip + .01 overshoot + .002 commission + .2 night)/10.
    one_night = trigger_cost(feasibility, charged_evidence())
    assert one_night["cost_r"] == pytest.approx(.0236)
    assert one_night["cost_gate"] == "fail"
    triple = trigger_cost(feasibility, charged_evidence(),
        signal_time=pd.Timestamp("2015-01-07 10:00"),
        entry_time=pd.Timestamp("2015-01-07 11:00"),
        exit_time=pd.Timestamp("2015-01-08 11:00"))
    assert triple["cost_r"] == pytest.approx(.0636)


@pytest.mark.parametrize("field", ["charge_weekdays", "triple_weekdays"])
@pytest.mark.parametrize("bad", [[.5, 1.5, 2.5, 3.5, 4.5], [True], ["1"], [7]])
@pytest.mark.parametrize("missing_overshoot", [False, True])
def test_fix1_invalid_weekdays_cannot_hide_charges_or_failures(
        feasibility, field, bad, missing_overshoot):
    e = charged_evidence()
    e["payload"]["swap"][field] = bad
    if missing_overshoot:
        del e["payload"]["overshoot"]
    out = trigger_cost(feasibility, seal(e))
    assert out["cost_r"] is None
    assert out["cost_gate"] == "fail"
    assert "invalid_swap_" + field in out["fail_reasons"]
    if missing_overshoot:
        assert "missing_overshoot" in out["waiting_reasons"]


@pytest.mark.parametrize("component", ["spread", "overshoot"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -.01])
def test_fix1_invalid_p90_and_missing_coverage_preserve_both_gates(feasibility, component, bad):
    e = cost_evidence()
    e["payload"][component]["hours"]["6"] = {"p90": bad}
    out = trigger_cost(feasibility, seal(e))
    assert out["cost_r"] is None and out["cost_gate"] == "fail"
    assert "invalid_" + component + "_p90" in out["fail_reasons"]
    assert "invalid_" + component + "_coverage" in out["waiting_reasons"]
    row = feasibility.summarize_hypothesis([pd.Timestamp("2015-01-05")], [out])
    assert row["status"] == "waiting_data" and row["cost_gate"] == "fail"
    assert out["fail_reasons"] == row["fail_reasons"]
    assert set(out["waiting_reasons"]) <= set(row["waiting_reasons"])


@pytest.mark.parametrize("bad", [[24], [6.5], [True], ["6"]])
def test_fix1_invalid_event_hours_are_not_masked_by_missing_component(feasibility, bad):
    e = cost_evidence()
    e["payload"]["slippage"]["event_hours"] = bad
    del e["payload"]["overshoot"]
    out = trigger_cost(feasibility, seal(e))
    assert out["cost_r"] is None and out["cost_gate"] == "fail"
    assert "invalid_slippage_event_hours" in out["fail_reasons"]
    assert "missing_overshoot" in out["waiting_reasons"]


def test_fix1_rejected_production_evidence_is_not_numerically_validated(feasibility):
    e = charged_evidence()
    e["kind"] = "production"
    e["payload"]["swap"]["charge_weekdays"] = [.5]
    e["payload"]["spread"]["hours"]["6"] = {"p90": float("nan")}
    out = trigger_cost(feasibility, seal(e))
    assert out["cost_r"] is None and out["cost_gate"] == "waiting_data"
    assert out["waiting_reasons"] == ["production_cost_not_released"]
    assert out["fail_reasons"] == []


def test_fix1_gap_timezones_have_identical_qualification_and_identity(feasibility):
    b = bars(2200)
    b.loc[26:, "time"] += pd.Timedelta(days=31)
    # The first trigger's warmup spans the gap, the second's does not.
    b.loc[[2001, 2050], ["open", "high", "low", "close"]] = [100., 105., 99., 104.]
    tags = ["mt5"] * len(b)
    gap = [(b.time.iloc[25], b.time.iloc[26])]
    identity = input_identity(b, tags, gap)
    representations = [gap,
        [(s.isoformat() + "Z", e.isoformat() + "Z") for s, e in gap],
        [(s.tz_localize("UTC").tz_convert("Etc/GMT-2").isoformat(),
          e.tz_localize("UTC").tz_convert("Etc/GMT+5").isoformat()) for s, e in gap]]
    reports = []
    for spans in representations:
        report = feasibility.run_feasibility({"XAUUSD": b}, source_tags={"XAUUSD": tags},
            gap_spans={"XAUUSD": spans}, sessions=frozen_sessions(),
            input_evidence={"XAUUSD": identity}, cost_evidence={"XAUUSD": cost_evidence()})
        row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"])
                   == ("F1", "XAUUSD", "S1", "A3"))
        assert row["n_triggers"] == 1 and row["n_days"] == 1
        assert row["exclusions"]["feature_gap"] == 1
        assert report["inputs"]["XAUUSD"]["payload_sha256"] == identity["payload_sha256"]
        reports.append(report)
    assert reports[0] == reports[1] == reports[2]


@pytest.mark.parametrize("spans", [
    [("2015-01-05T02:00:00Z", "2015-01-05T01:00:00Z")],
    [("NaT", "2015-01-05T01:00:00Z")],
    [("not-a-time", "2015-01-05T01:00:00Z")],
    [("2015-01-05T01:00:00Z",)],
])
def test_fix1_malformed_gap_still_rejects_input(feasibility, spans):
    b = bars(30)
    tags = ["mt5"] * len(b)
    report = feasibility.run_feasibility({"XAUUSD": b}, source_tags={"XAUUSD": tags},
        gap_spans={"XAUUSD": spans}, sessions=frozen_sessions(),
        input_evidence={"XAUUSD": input_identity(b, tags, [])}, cost_evidence={})
    rows = [r for r in report["rows"] if r["symbol"] == "XAUUSD"]
    assert all(r["n_triggers"] is None and r["cost_r"] is None and
               r["status"] == "waiting_data" for r in rows)
    assert all("input_identity_incomplete" in r["waiting_reasons"] or
               "input_gap_range" in r["waiting_reasons"] for r in rows)


def cold_python(code, *args):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    return subprocess.run([sys.executable, "-I", "-c", code, str(root), *map(str, args)],
        cwd=root, env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, text=True,
        timeout=60)


@pytest.mark.parametrize("mode", ["help", "compute"])
def test_fix1_cold_cli_blocks_dependencies_before_first_package_import(tmp_path, mode):
    b = bars(2200)
    b.loc[2050, ["open", "high", "low", "close"]] = [100., 105., 99., 104.]
    tags = ["mt5"] * len(b)
    rows = b.copy()
    rows["time"] = rows.time.map(lambda t: t.isoformat())
    inputs = {"symbols": {"XAUUSD": {"bars": rows.to_dict("records"), "source_tags": tags,
        "gap_spans": [], "evidence": input_identity(b, tags, [])}}}
    input_path, cost_path, out = tmp_path / "input.json", tmp_path / "costs.json", tmp_path / "out"
    input_path.write_text(json.dumps(inputs), encoding="utf-8")
    e = cost_evidence()
    e["payload"]["spread"]["hours"] = {str(h): {"p90": .1, "n_days": 20} for h in range(24)}
    cost_path.write_text(json.dumps({"XAUUSD": seal(e)}), encoding="utf-8")
    result = cold_python('''
import sys
import importlib.abc
import runpy
from pathlib import Path
forbidden = ("mt5quant.prob.labels", "mt5quant.prob.tables", "mt5quant.prob.p1",
             "mt5quant.backtest.engine_a", "mt5quant.backtest.engine_b",
             "mt5quant.research.candidates")
assert not any(n.startswith(("mt5quant", "mt5data")) for n in sys.modules)
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in forbidden):
            raise AssertionError("forbidden cold import: " + fullname)
sys.meta_path.insert(0, Guard())
root, mode, input_path, cost_path, output = sys.argv[1:]
script = str(Path(root) / "scripts/pline_feasibility.py")
sys.argv = [script, "--help"] if mode == "help" else [script, "--input", input_path,
    "--cost-evidence", cost_path, "--sessions", str(Path(root) / "config/sessions.toml"),
    "--output", output]
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0, exc.code
else:
    raise AssertionError("CLI did not finish")
assert not any(n == p or n.startswith(p + ".") for n in sys.modules for p in forbidden)
print("COLD_IMPORT_GUARD_PASS")
''', mode, input_path, cost_path, out)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COLD_IMPORT_GUARD_PASS" in result.stdout
    if mode == "help":
        assert "--input" in result.stdout
        assert not out.exists()
    else:
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"])
                   == ("F1", "XAUUSD", "S1", "A3"))
        assert row["n_triggers"] == 1 and row["cost_r"] > 0
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        assert receipt["code_files_sha256"]["src/mt5quant/prob/__init__.py"] == hashlib.sha256(
            (root / "src/mt5quant/prob/__init__.py").read_bytes()).hexdigest()


def test_fix1_lazy_exports_preserve_names_identity_and_legacy_imports():
    result = cold_python('''
import sys
import importlib
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
import mt5quant.prob as prob
assert "mt5quant.prob.labels" not in sys.modules
assert "mt5quant.prob.tables" not in sys.modules
assert "mt5quant.backtest.engine_a" not in sys.modules
expected = {
    "ACTION_ORDER": "decision", "Bracket": "labels", "CellEstimate": "tables",
    "LOOKBACK_BARS": "labels", "ModelIntegrityError": "artifact", "ProbTable": "tables",
    "choose": "decision", "ev_lower": "decision", "label_triggers": "labels",
    "load_model": "artifact", "save_model": "artifact",
}
assert prob.__all__ == list(expected)
assert set(expected) <= set(dir(prob))
try:
    prob.unknown_export
except AttributeError:
    pass
else:
    raise AssertionError("unknown name was accepted")
for name, module in expected.items():
    public = getattr(prob, name)
    assert public is getattr(importlib.import_module("mt5quant.prob." + module), name)
    imported = {}
    exec("from mt5quant.prob import " + name, imported)
    assert imported[name] is public
    assert getattr(prob, name) is public
wildcard = {}
exec("from mt5quant.prob import *", wildcard)
assert all(wildcard[name] is getattr(prob, name) for name in expected)
from mt5quant.prob import counting, gaps
assert counting is importlib.import_module("mt5quant.prob.counting")
assert gaps is importlib.import_module("mt5quant.prob.gaps")
print("LEGACY_EXPORTS_PASS")
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LEGACY_EXPORTS_PASS" in result.stdout
