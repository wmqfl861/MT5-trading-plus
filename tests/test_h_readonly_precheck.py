"""Synthetic-only behavioral tests for the real T114 precheck and CLI."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import numpy as np
import pandas as pd
import pytest

from mt5quant.costs import (
    CommissionModel, Costs, SlippageModel, SpreadModel, SwapModel, SwapRule,
    source_costs,
)
from mt5quant.costs.spread import SpreadStat
from mt5quant.h_main_scope import CoverageSegment, TimeInterval
from mt5quant.research.candidates import precheck, validate_candidate
from mt5quant.research.power import power_one_sided
from mt5quant.validation.events import window_volatility

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/h_readonly_precheck.py"
TRADE, SIGNAL = "EURUSD/H1", "USTEC/D1"
CANDIDATE = '''
id = "C002"
title = "Synthetic H scope fixture"
mechanism_class = "flow_calendar"
status = "registered"
symbols = ["EURUSD"]
holding = "H1"
mechanism = "Synthetic calendar declaration, no measured signal"
[literature]
original = ["Synthetic original citation"]
replications = []
rebuttals = []
search_log = "Synthetic search record"
published_effect = "Synthetic published claim"
expected_effect = "Synthetic ex ante discount, not a sample estimate"
[declared]
events_per_year = 12
gross_return_per_event = 0.003
window_bars = 2
entry_hour_ny = 10
exit_hour_ny = 12
overnight_nights = 1
expected_sharpe = 1.0
signal_symbol = "USTEC"
signal_timeframe = "D1"
side_rule = "mtd_sign"
'''


def runner():
    # Assertion, rather than a collection error, records the initial missing feature.
    assert importlib.util.find_spec("mt5quant.h_readonly_precheck") is not None, \
        "Task1 actual readonly precheck is not implemented"
    return importlib.import_module("mt5quant.h_readonly_precheck").run_h_readonly_precheck


def candidate():
    return validate_candidate(tomllib.loads(CANDIDATE), source="synthetic")


def costs():
    stats = {h: SpreadStat(.0002, .0002, .0002, 100, 20) for h in range(24)}
    spread = SpreadModel({"EURUSD": stats}, {"EURUSD": .0002}, mode="relative")
    return Costs(spread, SlippageModel(spread, spread_fraction=.1),
                 CommissionModel(default_per_lot=6, per_symbol={"EURUSD": 6}),
                 SwapModel(per_symbol={"EURUSD": SwapRule(-5, -1)}))


def main_bars(start="2007-01-01", periods=168):
    times = pd.date_range(start, periods=periods, freq="h")
    closes = 1.2 + np.arange(periods) * .00001 + np.sin(np.arange(periods)) * .001
    return pd.DataFrame({"time": times, "close": closes})


def record(pair, bars, tags, coverage):
    return {"bars": bars, "source_tags": tags, "coverage": coverage,
            "missing_kind": "missing_half_open", "missing_intervals": [],
            "source_eligibility": {s: "synthetic_only" for s in set(tags)}}


def inputs(*, mixed=True):
    trade = main_bars()
    main_end = trade.time.iloc[-1] + pd.Timedelta(hours=1)
    tcov = [CoverageSegment("EURUSD", "H1", "histdata", trade.time.iloc[0], main_end)]
    ttags = ["histdata"] * len(trade)
    sig_times = pd.date_range("2007-01-01", "2007-01-08", freq="D")
    signal = pd.DataFrame({"time": sig_times, "close": 100 + np.arange(len(sig_times))})
    if mixed:
        trade = pd.concat([trade, pd.DataFrame({
            "time": pd.to_datetime(["2015-01-01", "2021-05-17", "2022-01-01"]),
            "close": [999., 888., 777.],
        })], ignore_index=True)
        ttags += ["mt5", "histdata", "histdata"]
        tcov += [CoverageSegment("EURUSD", "H1", "mt5", "2015-01-01", "2015-01-02"),
                 CoverageSegment("EURUSD", "H1", "histdata", "2021-05-17", "2022-01-02")]
        signal = pd.concat([pd.DataFrame({
            "time": pd.to_datetime(["2006-11-21", "2006-12-29"]), "close": [90., 99.]}),
            signal, pd.DataFrame({"time": pd.to_datetime(["2022-01-01"]), "close": [9999.]})],
            ignore_index=True)
    scov = [CoverageSegment("USTEC", "D1", "fred", signal.time.iloc[0],
                            signal.time.iloc[-1] + pd.Timedelta(days=1))]
    return {TRADE: record(TRADE, trade, ttags, tcov),
            SIGNAL: record(SIGNAL, signal, ["fred"] * len(signal), scov)}


def run(data=None, *, n=None, c=None, model=None, specs=None):
    return runner()(c or candidate(), inputs=inputs() if data is None else data,
                    cost_model=costs() if model is None else model,
                    contract_sizes={"EURUSD": 100000.} if specs is None else specs,
                    cumulative_n=n)


def test_real_bars_scope_diagnostic_invariance_and_input_immutability():
    original = inputs()
    snapshot = deepcopy(original)
    result = run(original)
    changed = deepcopy(original)
    changed[TRADE]["bars"].loc[168:, "close"] *= 1000
    changed[SIGNAL]["bars"].loc[[0, 10], "close"] *= 1000
    assert run(changed) == result
    assert result["execution_status"] == "not_released"
    assert result["research_status"] == "not_evaluated"
    assert result["diagnostic_status"] == "engineering_waiting"
    assert result["coverage"][TRADE]["bars"] == 168
    assert result["coverage"][TRADE]["years"] == pytest.approx(167 / (24 * 365.25))
    assert result["coverage"][SIGNAL]["bars"] == 8
    assert result["coverage"][SIGNAL]["years"] == pytest.approx(7 / 365.25)
    assert result["selection"][SIGNAL]["warmup_bars"] == 1
    assert result["selection"][SIGNAL]["first"] == "2006-12-29T00:00:00"
    assert result["selection"][SIGNAL]["last"] == "2007-01-08T00:00:00"
    assert result["checks"]["ex_ante_cost"]["per_symbol"]["EURUSD"]["reference_price"] \
        == main_bars().close.iloc[-1]
    assert result["checks"]["power"]["cumulative"]["n_tests"] is None
    assert result["checks"]["power"]["cumulative"]["power"] is None
    for key in original:
        pd.testing.assert_frame_equal(original[key]["bars"], snapshot[key]["bars"])
        assert {k: v for k, v in original[key].items() if k != "bars"} == \
            {k: v for k, v in snapshot[key].items() if k != "bars"}


@pytest.mark.parametrize("n", [True, False, 0, -1, 2., "2"])
def test_cumulative_n_never_coerced_or_inferred(n):
    with pytest.raises((ValueError, TypeError), match="cumulative_n"):
        run(n=n)


def test_cost_components_single_and_cumulative_power_match_frozen_rules():
    r = run(n=17)
    d = r["checks"]["ex_ante_cost"]["per_symbol"]["EURUSD"]
    ref = main_bars().close.iloc[-1]
    assert d["spread_round_trip"] == pytest.approx(.0003)
    assert d["slippage_entry"] == pytest.approx(.00002)
    assert d["slippage_exit"] == pytest.approx(.00002)
    assert d["commission"] == pytest.approx(6 / (100000 * ref))
    assert d["overnight"] == pytest.approx(5 / (100000 * ref))
    assert d["round_trip_cost"] == pytest.approx(.00034 + 11 / (100000 * ref))
    p = r["checks"]["power"]
    assert p["sigma_by_symbol"]["EURUSD"] == window_volatility(
        main_bars(), 2, entry_hour_ny=10)
    assert p["power"] == power_one_sided(p["sharpe_used"], p["years"])
    assert p["cumulative"]["power"] == power_one_sided(
        p["sharpe_used"], p["years"], n_tests=17)
    assert p["cumulative"]["power"] < p["power"]


@pytest.mark.parametrize("problem", ["unknown_missing", "legacy_gap", "bad_source", "short_tags",
                                     "duplicate", "unsorted", "outside_coverage", "wrong_pair",
                                     "missing_contains_bar", "nonfinite", "qualification"])
def test_invalid_inputs_fail_closed(problem):
    data = inputs()
    t = data[TRADE]
    if problem == "unknown_missing":
        t["missing_intervals"] = None
    elif problem == "legacy_gap":
        t["missing_kind"] = "existing_bar_endpoints"
    elif problem == "bad_source":
        t["source_tags"][0] = "fred"
    elif problem == "short_tags":
        t["source_tags"].pop()
    elif problem == "duplicate":
        t["bars"].loc[1, "time"] = t["bars"].loc[0, "time"]
    elif problem == "unsorted":
        t["bars"].loc[[0, 1], "time"] = t["bars"].loc[[1, 0], "time"].to_numpy()
    elif problem == "outside_coverage":
        t["coverage"][0] = replace(t["coverage"][0], start="2007-01-02")
    elif problem == "wrong_pair":
        data["XAUUSD/H1"] = data.pop(TRADE)
    elif problem == "missing_contains_bar":
        t["missing_intervals"] = [TimeInterval("2007-01-01", "2007-01-02")]
    elif problem == "nonfinite":
        t["bars"].loc[0, "close"] = float("nan")
    else:
        t["source_eligibility"]["histdata"] = "approved"
    with pytest.raises((ValueError, TypeError, PermissionError)):
        run(data)


def test_utc_equivalence_and_missing_touching_start_do_not_remove_first_bar():
    original = inputs()
    changed = deepcopy(original)
    for pair in changed:
        rec = changed[pair]
        rec["bars"]["time"] = pd.to_datetime(rec["bars"].time).dt.tz_localize("UTC").dt.tz_convert("Etc/GMT-8")
        rec["coverage"] = [replace(s, start=s.start.astimezone(__import__("datetime").timezone(
            __import__("datetime").timedelta(hours=8)))) for s in rec["coverage"]]
    changed[TRADE]["missing_intervals"] = [TimeInterval("2006-12-30", "2007-01-01")]
    result = run(changed)
    baseline = run(original)
    assert result["coverage"] == baseline["coverage"]
    assert result["checks"] == baseline["checks"]


def test_explicit_short_gap_splits_actual_adjacent_bars_and_window_population():
    data = inputs(mixed=False)
    frame = data[TRADE]["bars"]
    # Remove one bar crossed by the NY10 window on Jan 3. The surviving bar at
    # 14:00 is NOT missing: legacy endpoint conversion must not delete it.
    hole = pd.Timestamp("2007-01-03T15:00")
    data[TRADE]["bars"] = frame[frame.time != hole].reset_index(drop=True)
    data[TRADE]["source_tags"] = ["histdata"] * 167
    data[TRADE]["missing_intervals"] = [TimeInterval(hole, hole + pd.Timedelta(hours=1))]
    r = run(data)
    cov = r["coverage"][TRADE]
    assert cov["bars"] == 167
    assert cov["years"] == pytest.approx(165 / (24 * 365.25))
    assert cov["spans"][0]["last"] == "2007-01-03T14:00:00"
    assert cov["spans"][1]["first"] == "2007-01-03T16:00:00"
    rets = [frame.close.iloc[d * 24 + 16] / frame.close.iloc[d * 24 + 14] - 1
            for d in [0, 1, 3, 4, 5, 6]]
    assert r["checks"]["power"]["sigma_by_symbol"]["EURUSD"] == np.std(rets, ddof=1)
    assert r["checks"]["power"]["sigma_windows"] == 6


def test_bad_histdata_month_is_actually_excluded():
    data = inputs(mixed=False)
    bars = main_bars(periods=24 * 45)
    # January Sunday openings 00 and 01 violate the frozen monthly rule.
    bars = bars[bars.time != pd.Timestamp("2007-01-14")].reset_index(drop=True)
    data[TRADE] = record(TRADE, bars, ["histdata"] * len(bars), [
        CoverageSegment("EURUSD", "H1", "histdata", "2007-01-01", "2007-02-15")])
    r = run(data)
    assert r["selection"][TRADE]["bad_months"] == ["2007-01"]
    assert r["selection"][TRADE]["first"] == "2007-02-01T00:00:00"
    assert r["coverage"][TRADE]["bars"] == 14 * 24


def test_boundary_close_at_main_end_is_not_a_volatility_endpoint():
    data = inputs(mixed=False)
    bars = main_bars("2021-05-14", 72)
    data[TRADE] = record(TRADE, bars, ["histdata"] * len(bars), [
        CoverageSegment("EURUSD", "H1", "histdata", "2021-05-14", "2021-05-17")])
    sig = pd.DataFrame({"time": pd.date_range("2021-05-14", periods=3), "close": [1., 2., 3.]})
    data[SIGNAL] = record(SIGNAL, sig, ["fred"] * 3, [
        CoverageSegment("USTEC", "D1", "fred", "2021-05-14", "2021-05-17")])
    c = candidate()
    c = replace(c, declared=replace(c.declared, entry_hour_ny=18, window_bars=2))
    r = run(data, c=c)
    # NY18 = 22UTC close, anchor21, endpoint23 closes at midnight.
    assert r["checks"]["power"]["sigma_windows"] == 2


@pytest.mark.parametrize("specs", [{}, {"EURUSD": True}, {"EURUSD": 0}, {"EURUSD": float("inf")}])
def test_contract_size_required_and_finite(specs):
    with pytest.raises((ValueError, TypeError)):
        run(specs=specs)


def test_insufficient_windows_remain_unknown_not_zero_power():
    data = inputs(mixed=False)
    data[TRADE]["bars"] = data[TRADE]["bars"].iloc[:3].copy()
    data[TRADE]["source_tags"] = ["histdata"] * 3
    r = run(data, n=3)
    p = r["checks"]["power"]
    assert p["sigma_by_symbol"]["EURUSD"] is None
    assert p["power"] is None and p["cumulative"]["power"] is None
    assert r["diagnostic_status"] == "engineering_waiting"


def test_frozen_precheck_parity_on_complete_synthetic_store(tmp_path):
    data = inputs(mixed=False)
    frame = data[TRADE]["bars"]
    store = tmp_path / "synthetic-store"
    d = store / "bars/EURUSD/H1"
    d.mkdir(parents=True)
    df = frame.assign(open=frame.close, high=frame.close, low=frame.close,
                      tick_volume=1, spread=2, real_volume=0)
    df.to_parquet(d / "data.parquet")
    (d / "meta.json").write_text(json.dumps({"symbol": "EURUSD", "timeframe": "H1",
        "rows": len(df), "first_time": str(df.time.iloc[0]), "last_closed_time": str(df.time.iloc[-1])}), encoding="utf-8")
    (store / "specs").mkdir()
    (store / "specs/latest.json").write_text('{"EURUSD":{"contract_size":100000}}', encoding="utf-8")
    actual = run(data, n=1)
    years = {k: v["years"] for k, v in actual["coverage"].items()}
    old = precheck(candidate(), data_root=store,
                   cost_model=source_costs(costs(), "ext:histdata"), research_years=years)
    for key in ("reference_price", "spread_round_trip", "slippage_entry", "slippage_exit",
                "commission", "overnight", "round_trip_cost", "multiple"):
        assert actual["checks"]["ex_ante_cost"]["per_symbol"]["EURUSD"][key] == \
            old.checks["ex_ante_cost"]["per_symbol"]["EURUSD"][key]
    for key in ("years", "power", "sigma_by_symbol", "implied_sharpe", "sharpe_used"):
        assert actual["checks"]["power"][key] == old.checks["power"][key]


def cli_files(tmp_path, *, use_toml=False):
    tmp_path.mkdir(exist_ok=True)
    data = inputs()
    manifest = {"mode": "synthetic", "inputs": {}}
    for pair, rec in data.items():
        name = pair.split("/")[0] + ".json"
        artifact = {"bars": [{"time": t.isoformat(), "close": float(v)}
                              for t, v in zip(rec["bars"].time, rec["bars"].close)],
                    "source_tags": rec["source_tags"]}
        (tmp_path / name).write_text(json.dumps(artifact), encoding="utf-8")
        manifest["inputs"][pair] = {"bars_file": name, "missing_kind": "missing_half_open",
            "missing_intervals": [], "source_eligibility": rec["source_eligibility"],
            "coverage": [{"source": s.source, "start": s.start.isoformat(), "end": s.end.isoformat()}
                         for s in rec["coverage"]]}
    (tmp_path / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    cp = tmp_path / ("candidate.toml" if use_toml else "candidate.json")
    cp.write_text(CANDIDATE if use_toml else json.dumps(tomllib.loads(CANDIDATE)), encoding="utf-8")
    costdoc = {"mode": "synthetic", "spread": costs().spread.to_dict(),
               "slippage": {"spread_fraction": .1, "fixed_price": 0.,
                            "event_hours": [], "event_multiplier": 2.},
               "commission_per_lot": {"EURUSD": 6.},
               "swap": {"EURUSD": {"long_per_day": -5., "short_per_day": -1.}}}
    (tmp_path / "costs.json").write_text(json.dumps(costdoc), encoding="utf-8")
    (tmp_path / "specs.json").write_text(json.dumps({"mode": "synthetic",
        "contract_sizes": {"EURUSD": 100000.}}), encoding="utf-8")
    return ["--mode", "synthetic", "--inputs", str(tmp_path / "inputs.json"),
            "--candidate", str(cp), "--costs", str(tmp_path / "costs.json"),
            "--specs", str(tmp_path / "specs.json")]


def cli(args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT,
                          env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                          capture_output=True, text=True, encoding="utf-8", timeout=30)


@pytest.mark.parametrize("use_toml", [False, True])
def test_real_cli_end_to_end_hashes_readonly_and_diagnostic_invariance(tmp_path, use_toml):
    args = cli_files(tmp_path, use_toml=use_toml)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    proc = cli(args)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["result"] == run()
    expected_artifacts = {"inputs": "inputs.json", "candidate": "candidate.toml" if use_toml else "candidate.json",
                          "costs": "costs.json", "specs": "specs.json",
                          "bars:EURUSD/H1": "EURUSD.json", "bars:USTEC/D1": "USTEC.json"}
    assert doc["hashes"]["artifacts"] == {
        label: hashlib.sha256(before[name]).hexdigest() for label, name in expected_artifacts.items()}
    assert len(doc["hashes"]["rules"]) == 64 and doc["hashes"]["code"]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    payload = json.loads((tmp_path / "EURUSD.json").read_bytes())
    for bar in payload["bars"][168:]:
        bar["close"] *= 1000
    (tmp_path / "EURUSD.json").write_text(json.dumps(payload), encoding="utf-8")
    aux = json.loads((tmp_path / "USTEC.json").read_bytes())
    aux["bars"][-1]["close"] *= 1000
    (tmp_path / "USTEC.json").write_text(json.dumps(aux), encoding="utf-8")
    perturbed = cli(args)
    assert perturbed.returncode == 0, perturbed.stderr
    assert json.loads(perturbed.stdout)["result"] == doc["result"]
    assert not list(tmp_path.rglob("*.sqlite*")) and not list(tmp_path.rglob("*.precheck.json"))


def test_fix1_api_rejects_relative_swap_with_otherwise_valid_costs():
    data = inputs(mixed=False)
    data[TRADE]["bars"]["close"] = 2.
    c = candidate()
    c = replace(c, declared=replace(c.declared, gross_return_per_event=.0006))
    stats = {h: SpreadStat(.00004, .00004, .00004, 100, 20) for h in range(24)}
    spread = SpreadModel({"EURUSD": stats}, {"EURUSD": .00004}, mode="relative")
    relative_rule = SwapRule(-20., -10., ref_price=1., contract_size=100000.)
    model = Costs(spread, SlippageModel(spread, spread_fraction=0., fixed_price=0.),
                  CommissionModel(per_symbol={"EURUSD": 0.}),
                  SwapModel({"EURUSD": relative_rule}))
    # Valid absolute control isolates the unsupported basis, not another field.
    absolute = replace(model, swap=SwapModel({"EURUSD": SwapRule(-20., -10.)}))
    valid = run(data, c=c, model=absolute)["checks"]["ex_ante_cost"]
    assert valid["per_symbol"]["EURUSD"]["overnight"] == .0001
    assert valid["multiple"] == pytest.approx(3.75)
    fee = model.overnight_fee("EURUSD", "long", pd.Timestamp("2007-01-02T21:00"),
                              pd.Timestamp("2007-01-02T23:00"), price=2.)
    assert fee == -40.  # One ordinary NY fee-night; relative basis doubles it.
    assert abs(fee) / 200000. == .0002
    assert .0006 / (.00006 + abs(fee) / 200000.) == pytest.approx(2.3076923076923075)
    with pytest.raises(ValueError, match="relative swap unsupported.*absolute per-lot"):
        accepted = run(data, c=c, model=model)["checks"]["ex_ante_cost"]
        print("unsupported relative swap accepted:", accepted)


@pytest.mark.parametrize("default", [0., 6.])
def test_fix1_api_rejects_missing_commission_record_even_with_default(default):
    data = inputs(mixed=False)
    data[TRADE]["bars"]["close"] = 2.
    c = candidate()
    c = replace(c, declared=replace(c.declared, gross_return_per_event=.0011))
    zero = replace(costs(), commission=CommissionModel(per_symbol={"EURUSD": 0.}))
    charged = replace(costs(), commission=CommissionModel(per_symbol={"EURUSD": 6.}))
    free_result = run(data, c=c, model=zero)["checks"]["ex_ante_cost"]
    charged_result = run(data, c=c, model=charged)["checks"]["ex_ante_cost"]
    assert free_result["multiple"] == pytest.approx(3.0136986301369864)
    assert charged_result["multiple"] == pytest.approx(2.7848101265822782)
    assert free_result["passed"] is True and charged_result["passed"] is False
    missing = CommissionModel() if default == 0. else CommissionModel(default_per_lot=default)
    with pytest.raises(ValueError, match="EURUSD explicit commission record required"):
        accepted = run(data, c=c, model=replace(costs(), commission=missing))["checks"]["ex_ante_cost"]
        print("missing commission record accepted:", accepted)


@pytest.mark.parametrize("commission,swap_charge", [(0., 0.), (6., 20.)])
def test_fix1_api_and_cli_absolute_and_explicit_zero_fees(tmp_path, commission, swap_charge):
    model = replace(costs(), commission=CommissionModel(default_per_lot=999., per_symbol={"EURUSD": commission}),
                    swap=SwapModel({"EURUSD": SwapRule(-swap_charge, 0.)}))
    expected = run(model=model)
    detail = expected["checks"]["ex_ante_cost"]["per_symbol"]["EURUSD"]
    ref = main_bars().close.iloc[-1]
    assert detail["commission"] == commission / (100000. * ref)
    assert detail["overnight"] == swap_charge / (100000. * ref)
    assert detail["round_trip_cost"] == pytest.approx(.00034 + (commission + swap_charge) / (100000. * ref))
    args = cli_files(tmp_path)
    path = tmp_path / "costs.json"
    doc = json.loads(path.read_bytes())
    doc["commission_per_lot"]["EURUSD"] = commission
    doc["swap"]["EURUSD"] = {"long_per_day": -swap_charge, "short_per_day": 0.}
    path.write_text(json.dumps(doc), encoding="utf-8")
    proc = cli(args)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["result"] == expected


def test_fix1_cli_rejects_relative_swap_with_otherwise_valid_costs(tmp_path):
    args = cli_files(tmp_path)
    path = tmp_path / "costs.json"
    doc = json.loads(path.read_bytes())
    doc["swap"]["EURUSD"] = {"long_per_day": -20., "short_per_day": -10.}
    path.write_text(json.dumps(doc), encoding="utf-8")
    valid = cli(args)
    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["result"]["checks"]["ex_ante_cost"]["per_symbol"]["EURUSD"]["overnight"] \
        == 20. / (100000. * main_bars().close.iloc[-1])
    doc["swap"]["EURUSD"].update(ref_price=1., contract_size=100000.)
    path.write_text(json.dumps(doc), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    output = tmp_path / "must-not-create"
    proc = cli([*args, "--output", str(output)])
    assert proc.returncode == 2, proc.stdout
    assert "relative swap unsupported" in proc.stderr and "absolute per-lot" in proc.stderr
    assert not proc.stdout and not output.exists()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_cli_exclusive_output_and_fixed_n(tmp_path):
    args = cli_files(tmp_path)
    dest = tmp_path / "new-output"
    proc = cli([*args, "--output", str(dest), "--cumulative-n", "17"])
    assert proc.returncode == 0, proc.stderr
    record_bytes = (dest / "result.json").read_bytes()
    assert json.loads(record_bytes)["result"]["checks"]["power"]["cumulative"]["n_tests"] == 17
    again = cli([*args, "--output", str(dest)])
    assert again.returncode == 2
    assert (dest / "result.json").read_bytes() == record_bytes


def test_cli_production_and_self_approval_rejected_before_price_reads(tmp_path):
    args = cli_files(tmp_path)
    manifest = json.loads((tmp_path / "inputs.json").read_bytes())
    manifest["inputs"][TRADE]["source_eligibility"]["histdata"] = "approved"
    (tmp_path / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "EURUSD.json").unlink()
    out = tmp_path / "never-created"
    for request in ([*args, "--mode", "production"], args):
        p = cli([*request, "--output", str(out)])
        assert p.returncode == 2 and not p.stdout
        assert "not_released" in p.stderr and "No such file" not in p.stderr
        assert not out.exists()


@pytest.mark.parametrize("field", ["commission_per_lot", "slippage", "swap"])
def test_cli_never_defaults_unknown_costs_to_zero(tmp_path, field):
    args = cli_files(tmp_path)
    p = tmp_path / "costs.json"
    doc = json.loads(p.read_bytes())
    del doc[field]
    p.write_text(json.dumps(doc), encoding="utf-8")
    result = cli(args)
    assert result.returncode == 2 and not result.stdout
    assert "missing" in result.stderr and field in result.stderr


@pytest.mark.parametrize("exclusive_output", [False, True])
def test_cold_cli_blocks_execution_ledger_and_event_calls(tmp_path, exclusive_output):
    args = cli_files(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    if exclusive_output:
        args += ["--output", str(tmp_path / "new-exclusive-output")]
    code = r'''
import hashlib, importlib.abc, os, pathlib, runpy, sys
root, script, *args = sys.argv[1:]
sys.path.insert(0, root + '/src')
artifact_root = pathlib.Path(args[args.index('--inputs') + 1]).parent
opened = {p.resolve(): 0 for p in artifact_root.iterdir()}
output_dir = pathlib.Path(args[args.index('--output') + 1]).resolve() if '--output' in args else None
assert output_dir is None or not output_dir.exists()
parsed = {}
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('MetaTrader5', 'mt5quant.backtest',
                                'mt5quant.prob.labels', 'mt5quant.validation.gates')):
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, Guard())
def guard(frame, event, arg):
    module = frame.f_globals.get('__name__', '')
    name = frame.f_code.co_name
    # Frozen research.__init__ eagerly imports ledger definitions. No function
    # call, instantiation or DB connection is permitted by this exception.
    if event == 'call' and name == '_parse_bytes' and module == '__main__':
        caller = frame.f_back.f_locals
        raw = frame.f_locals['raw']
        assert raw is caller['self'].raw[caller['path']]
        parsed[caller['label']] = hashlib.sha256(raw).hexdigest()
        assert parsed[caller['label']] == caller['self'].hashes[caller['label']]
    if event == 'call' and module == 'mt5quant.research.ledger':
        class_body = name[0].isupper() and '.' not in frame.f_code.co_qualname and not frame.f_code.co_flags & 3
        if name != '<module>' and not class_body:
            raise AssertionError('forbidden ledger call: ' + name)
    if event == 'call' and name in {
        'event_returns', 'fit', 'randomize_event_times', 'load_bars', 'load_bars_consistent',
        'load_ext_series', 'load_ticks', 'merged_research_series', 'build_costs'}:
        raise AssertionError('forbidden call: ' + name)
sys.setprofile(guard)
def audit(event, args):
    if event == 'sqlite3.connect':
        raise AssertionError('ledger opened')
    if event in {'os.remove', 'os.rename', 'os.rmdir'}:
        raise AssertionError('readonly CLI changed filesystem')
    if event == 'os.mkdir' and pathlib.Path(args[0]).resolve() != output_dir:
        raise AssertionError('unexpected mkdir')
    if event == 'open':
        path, mode, flags = args
        write = ((mode is not None and any(c in mode for c in 'wax+'))
                 or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if write:
            assert output_dir is not None and pathlib.Path(path).resolve() == output_dir / 'result.json'
            assert flags & os.O_EXCL and not flags & os.O_TRUNC
        if isinstance(path, (str, bytes)):
            p = pathlib.Path(path).resolve()
            assert not p.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.parquet'}
            assert not any((pathlib.Path(root) / d).resolve() in p.parents for d in ('data', 'data_research', 'config'))
            if p in opened:
                opened[p] += 1
                if opened[p] > 1:
                    raise AssertionError('artifact re-read: ' + p.name)
sys.addaudithook(audit)
sys.argv = [script, *args]
try:
    runpy.run_path(script, run_name='__main__')
except SystemExit as exc:
    if exc.code != 0:
        raise
assert all(n == 1 for n in opened.values()), opened
assert set(parsed) == {'inputs', 'candidate', 'costs', 'specs', 'bars:EURUSD/H1', 'bars:USTEC/D1'}
'''
    proc = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT), str(SCRIPT), *args],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["result"] == run()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("case", ["bucket", "sampling", "spread_mode", "missing_mode", "unknown_spread",
                                  "missing_swap_side", "missing_fixed_slip", "unknown_swap",
                                  "unknown_top", "bool_commission", "bad_stat"])
def test_cli_rejects_incomplete_or_ambiguous_standard_cost_models(tmp_path, case):
    args = cli_files(tmp_path)
    path = tmp_path / "costs.json"
    doc = json.loads(path.read_bytes())
    if case == "bucket":
        doc["spread"]["bucket_tz"] = "UTC"
    elif case == "sampling":
        doc["spread"]["sampling"] = "unknown"
    elif case == "spread_mode":
        doc["spread"]["mode"] = "unknown"
    elif case == "missing_mode":
        del doc["spread"]["mode"]
    elif case == "unknown_spread":
        doc["spread"]["approval"] = True
    elif case == "missing_swap_side":
        del doc["swap"]["EURUSD"]["short_per_day"]
    elif case == "missing_fixed_slip":
        del doc["slippage"]["fixed_price"]
    elif case == "unknown_swap":
        doc["swap"]["EURUSD"]["approved_by"] = "self"
    elif case == "unknown_top":
        doc["approved_by"] = "self"
    elif case == "bool_commission":
        doc["commission_per_lot"]["EURUSD"] = True
    else:
        doc["spread"]["per_symbol"]["EURUSD"]["10"]["p90"] = -1
    path.write_text(json.dumps(doc), encoding="utf-8")
    proc = cli(args)
    assert proc.returncode == 2 and not proc.stdout
    assert "can't open file" not in proc.stderr


def _toml_fixture(doc):
    """Test-only serializer for the fixed nested fixture schema."""
    def value(v):
        if isinstance(v, dict):
            return "{ " + ", ".join(json.dumps(k) + " = " + value(x) for k, x in v.items()) + " }"
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        return json.dumps(v)
    return "\n".join(json.dumps(k) + " = " + value(v) for k, v in doc.items()) + "\n"


def test_real_cli_all_toml_and_absolute_tick_cost_semantics(tmp_path):
    args = cli_files(tmp_path, use_toml=True)
    cost = json.loads((tmp_path / "costs.json").read_bytes())
    cost["spread"]["mode"] = "absolute"
    cost["spread"]["sampling"] = "tick"
    (tmp_path / "costs.json").write_text(json.dumps(cost), encoding="utf-8")
    for name in ("EURUSD", "USTEC", "costs", "specs", "inputs"):
        doc = json.loads((tmp_path / (name + ".json")).read_bytes())
        if name == "inputs":
            for rec in doc["inputs"].values():
                rec["bars_file"] = rec["bars_file"].replace(".json", ".toml")
        (tmp_path / (name + ".toml")).write_text(_toml_fixture(doc), encoding="utf-8")
    args = [arg.replace(".json", ".toml") for arg in args]
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    proc = cli(args)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    detail = doc["result"]["checks"]["ex_ante_cost"]["per_symbol"]["EURUSD"]
    assert detail["spread_round_trip"] == pytest.approx(.0003 / detail["reference_price"])
    assert detail["slippage_entry"] == pytest.approx(.00002 / detail["reference_price"])
    assert doc["hashes"]["artifacts"]["costs"] == hashlib.sha256(before["costs.toml"]).hexdigest()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("payload", ['{"mode":"synthetic","mode":"production"}',
                                     '{"mode":"synthetic","inputs":NaN}'])
def test_cli_duplicate_keys_and_nonfinite_json_fail_closed(tmp_path, payload):
    args = cli_files(tmp_path)
    (tmp_path / "inputs.json").write_text(payload, encoding="utf-8")
    proc = cli(args)
    assert proc.returncode == 2 and not proc.stdout
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_selection_excludes_an_internal_diagnostic_block_from_years_and_sigma():
    data = inputs(mixed=False)
    data[TRADE]["source_tags"][63] = "mt5"
    data[TRADE]["source_eligibility"]["mt5"] = "synthetic_only"
    data[TRADE]["coverage"].append(CoverageSegment(
        "EURUSD", "H1", "mt5", "2007-01-03T15:00", "2007-01-03T16:00"))
    baseline = run(data)
    data[TRADE]["bars"].loc[63, "close"] = 100000.
    assert run(data) == baseline
    assert baseline["coverage"][TRADE]["years"] == pytest.approx(165 / (24 * 365.25))
    assert baseline["checks"]["power"]["sigma_windows"] == 6


def test_signal_warmup_touching_bounds_and_source_transition():
    data = inputs(mixed=False)
    times = pd.to_datetime(["2006-11-21", "2006-11-22", "2006-12-29", "2007-01-01",
                            "2007-01-02", "2007-01-03", "2021-05-17"])
    frame = pd.DataFrame({"time": times, "close": np.arange(len(times)) + 100.})
    tags = ["fred"] * 4 + ["mt5"] * 3
    data[SIGNAL] = record(SIGNAL, frame, tags, [
        CoverageSegment("USTEC", "D1", "fred", "2006-11-21", "2007-01-02"),
        CoverageSegment("USTEC", "D1", "mt5", "2007-01-02", "2021-05-18")])
    r = run(data)
    assert r["selection"][SIGNAL]["first"] == "2006-11-22T00:00:00"
    assert r["selection"][SIGNAL]["warmup_bars"] == 2
    assert r["selection"][SIGNAL]["last"] == "2007-01-03T00:00:00"
    assert r["coverage"][SIGNAL]["years"] == pytest.approx(2 / 365.25)
    assert [s["source"] for s in r["coverage"][SIGNAL]["segments"]] == ["fred", "mt5"]


def test_all_invalid_months_withhold_cost_reference_and_power():
    data = inputs(mixed=False)
    frame = main_bars(periods=24 * 21)
    frame = frame[frame.time != pd.Timestamp("2007-01-14")].reset_index(drop=True)
    data[TRADE] = record(TRADE, frame, ["histdata"] * len(frame), [
        CoverageSegment("EURUSD", "H1", "histdata", "2007-01-01", "2007-01-22")])
    r = run(data)
    assert r["coverage"][TRADE]["bars"] == 0
    assert r["checks"]["ex_ante_cost"]["per_symbol"]["EURUSD"] is None
    assert r["checks"]["power"]["power"] is None
    assert r["diagnostic_status"] == "engineering_waiting"
