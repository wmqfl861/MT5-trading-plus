"""Real-input assembler and real eligibility mode tests (synthetic copies only).

The real bound artifacts are run exactly once outside this suite; every test
here builds schema-isomorphic synthetic copies under a temporary data root and
binds them with a test-computed hash whitelist. The assembler CLI keeps the
frozen real whitelist and is only exercised in its fail-closed direction.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import numpy as np
import pandas as pd
import pytest

from mt5quant import h_readonly_precheck as hrp
from mt5quant.costs import Costs
from mt5quant.research.candidates import validate_candidate

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/h_assemble_real_precheck.py"
CLI = ROOT / "scripts/h_readonly_precheck.py"
TRADE, SIGNAL = "EURUSD/H1", "USTEC/D1"
MAIN_START = pd.Timestamp("2007-01-01")
MAIN_END = pd.Timestamp("2021-05-17")
WARMUP_START = MAIN_START - pd.Timedelta(days=40)
HOLE = (pd.Timestamp("2019-10-01"), pd.Timestamp("2019-11-01"))
CANDIDATE_DRAFT = ROOT / "docs/research/drafts/t114/C002.toml"

PLAN_WHITELIST = {
    "ext/histdata/EURUSD/H1/data.parquet":
        "10543bb6afa0c14ef033e1b719233c570830af7911a617519681e91dd0ec1863",
    "ext/histdata/EURUSD/H1/manifest.json":
        "4e0cc51be2e0b257a8e31ac14cce2ca4d9320d12b9eccf2b5b14e712c5ac78b3",
    "ext/fred/USTEC/D1/data.parquet":
        "c6645256e2cf2ceb10474318c2e5f926a5157cc0c9d50027e55f1482200a71c6",
    "ext/fred/USTEC/D1/manifest.json":
        "08ff3dff0f48330e48832c06eb21bbf8b7564f81616d5783cd183ce31311c5fe",
    "costs/calibrated.json":
        "e2cae60cf76e70f0845933863ac552c1ec379373e1b73302625c883bfa267c4c",
    "costs/spread_model.json":
        "c35550b90fbefef0d61b6c767e9e5365d6480cffaee232a68da696edb667ce85",
    "specs/latest.json":
        "144bb2245479e3557c571c9f5f83ca92db9d269211da7845c9d5aa2467d3150f",
}

_MODULE = None


def assembler():
    """Load scripts/h_assemble_real_precheck.py; RED until the file exists."""
    global _MODULE
    if _MODULE is None:
        assert SCRIPT.exists(), "scripts/h_assemble_real_precheck.py is not implemented"
        spec = importlib.util.spec_from_file_location("h_assemble_real_precheck", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULE = module
    return _MODULE


# --------------------------------------------------------------- synthetic root

def histdata_manifest(*, salvage_2019=True, override=None):
    years = {str(y): {"used": True, "calendar": "us_wall"} for y in range(2007, 2022)}
    if salvage_2019:
        years["2019"] = {
            "used": True, "calendar": "inconsistent", "salvage": True,
            "monthly": {"2019-10": {"calendar": "undetermined"}},
            "salvaged_months": {f"2019-{m:02d}": "eu_calendar"
                                for m in range(1, 13) if m != 10},
            "fix_anchor_month_end": {"status": "consistent"},
        }
    if override:
        years.update(override)
    return {"source": "histdata", "symbol": "EURUSD", "timeframe": "H1",
            "rows": 0, "first_time": None, "last_time": None,
            "yearly_verification": years}


def fred_manifest(*, yearly=None):
    doc = {"source": "fred", "symbol": "USTEC", "timeframe": "D1",
           "rows": 0, "first_time": None, "last_time": None}
    if yearly is not None:
        doc["yearly_verification"] = yearly
    return doc


def calibrated_doc(*, round_trip=0.0):
    return {"symbols": {"EURUSD": {
        "commission": {"deals_in": 8, "volume_in": 0.13, "per_lot_in": round_trip / 2,
                       "deals_out": 12, "volume_out": 0.13, "per_lot_out": round_trip / 2,
                       "round_trip_per_lot": round_trip},
        "swap": {"snapshot": {"available": True, "long_per_lot_per_night": -5.9,
                              "short_per_lot_per_night": 0.0, "triple_weekday_py": 2}}}}}


def spread_doc(*, p90=5.2e-05):
    hours = {str(h): {"median": 5.0e-05, "p90": p90, "p99": 5.5e-05, "n": 100,
                      "n_days": 20} for h in range(24)}
    return {"version": 2, "mode": "relative", "model_sha256": "f" * 64,
            "built_at_utc": "2026-10-28T21:02:28+00:00", "sampling": "bar_open_m1",
            "watermarks": {"EURUSD": {"rows": 1, "first_msc": 0, "last_msc": 1}},
            "model": {"mode": "relative", "sampling": "bar_open_m1",
                      "bucket_tz": "America/New_York",
                      "per_symbol": {"EURUSD": hours},
                      "overall_median": {"EURUSD": 5.0e-05}}}


def hourly_prices():
    # Deliberately extends past both window edges and covers the registered
    # 2019-10 hole month, so filtering and removal are both observable.
    grid = pd.date_range("2006-12-30", "2021-05-18", freq="h")
    return pd.DataFrame({"time": grid, "close": 1.2 + np.arange(len(grid)) * 1e-6})


def daily_prices():
    grid = pd.date_range("2006-11-01", "2021-05-20", freq="D")
    grid = grid[grid.weekday < 5]  # weekday calendar with no holiday holes
    return pd.DataFrame({"time": grid, "close": 100. + np.arange(len(grid))})


def write_root(tmp_path, *, hist=None, fred=None, prices_h=None, prices_d=None,
               costs=None, spread=None, specs=None, drop=(), stub_prices=False):
    """Write the seven schema-isomorphic bound artifacts; return (root, whitelist).

    stub_prices writes deliberately unparseable parquet bytes so any test that
    expects a pre-price failure also proves prices were never decoded.
    """
    root = tmp_path / "data_research"
    payloads = {}
    hist = hist if hist is not None else histdata_manifest()
    fred = fred if fred is not None else fred_manifest()
    frame_h = prices_h if prices_h is not None else hourly_prices()
    frame_d = prices_d if prices_d is not None else daily_prices()
    if not stub_prices:
        hist.update(rows=len(frame_h), first_time=frame_h.time.iloc[0].isoformat(),
                    last_time=frame_h.time.iloc[-1].isoformat())
        fred.update(rows=len(frame_d), first_time=frame_d.time.iloc[0].isoformat(),
                    last_time=frame_d.time.iloc[-1].isoformat())
    payloads["ext/histdata/EURUSD/H1/manifest.json"] = json.dumps(hist).encode("utf-8")
    payloads["ext/fred/USTEC/D1/manifest.json"] = json.dumps(fred).encode("utf-8")
    payloads["costs/calibrated.json"] = json.dumps(
        costs if costs is not None else calibrated_doc()).encode("utf-8")
    payloads["costs/spread_model.json"] = json.dumps(
        spread if spread is not None else spread_doc()).encode("utf-8")
    payloads["specs/latest.json"] = json.dumps(
        specs if specs is not None else {"EURUSD": {"contract_size": 100000.0}}).encode("utf-8")
    whitelist = {}
    for rel in PLAN_WHITELIST:
        if rel in drop:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".parquet"):
            if stub_prices:
                path.write_bytes(b"definitely not a parquet file")
            else:
                frame = frame_h if "histdata" in rel else frame_d
                frame.to_parquet(path, index=False)
        else:
            path.write_bytes(payloads[rel])
        whitelist[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return root, whitelist


def candidate():
    return validate_candidate(tomllib.loads(CANDIDATE_DRAFT.read_text(encoding="utf-8")),
                              source="synthetic draft")


# ------------------------------------------------------------------- whitelist

def test_frozen_real_whitelist_matches_plan_binding():
    assert assembler().REAL_WHITELIST == PLAN_WHITELIST


def test_hash_mismatch_missing_file_and_unbound_paths_fail_closed(tmp_path):
    module = assembler()
    root, whitelist = write_root(tmp_path)
    bad = dict(whitelist, **{"costs/calibrated.json": "0" * 64})
    with pytest.raises(ValueError, match="costs/calibrated.json.*sha256|sha256.*costs/calibrated.json"):
        module.assemble_real_inputs(root, verify_hash=bad)
    partial = {k: v for k, v in whitelist.items() if k != "specs/latest.json"}
    with pytest.raises(ValueError, match="whitelist"):
        module.assemble_real_inputs(root, verify_hash=partial)
    empty, _ = write_root(tmp_path / "gap", drop=("ext/fred/USTEC/D1/data.parquet",))
    with pytest.raises(ValueError):
        module.assemble_real_inputs(empty, verify_hash={
            k: v for k, v in whitelist.items() if k != "ext/fred/USTEC/D1/data.parquet"})
    # Unbound extra files are never read and never block a bound assembly.
    (root / "costs/unbound.json").write_text("{}", encoding="utf-8")
    (root / "ext/histdata/EURUSD/H1/other.parquet").write_bytes(b"junk")
    out = module.assemble_real_inputs(root, verify_hash=whitelist)
    assert set(out["provenance"]["binding"]["files"]) == set(PLAN_WHITELIST)


def test_used_gate_rejects_false_or_absent_years_before_price_parse(tmp_path):
    module = assembler()
    # Stub parquet bytes: if prices were parsed before the gate, pyarrow would
    # fail with a different error, so the used-gate message proves ordering.
    root, whitelist = write_root(
        tmp_path, stub_prices=True,
        hist=histdata_manifest(override={"2015": {"used": False}}))
    with pytest.raises(ValueError, match="2015.*used|used.*2015"):
        module.assemble_real_inputs(root, verify_hash=whitelist)
    root2, whitelist2 = write_root(tmp_path / "absent", stub_prices=True)
    path = root2 / "ext/histdata/EURUSD/H1/manifest.json"
    hist = json.loads(path.read_text(encoding="utf-8"))
    del hist["yearly_verification"]["2020"]
    path.write_text(json.dumps(hist), encoding="utf-8")
    whitelist2["ext/histdata/EURUSD/H1/manifest.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="2020"):
        module.assemble_real_inputs(root2, verify_hash=whitelist2)
    # A manifest that declares yearly_verification is gated for FRED too.
    root3, whitelist3 = write_root(
        tmp_path / "fredgate", stub_prices=True,
        fred=fred_manifest(yearly={"2006": {"used": False, "calendar": "na"}}))
    with pytest.raises(ValueError, match="2006"):
        module.assemble_real_inputs(root3, verify_hash=whitelist3)


# ------------------------------------------------------------------ assembling

def assembled(tmp_path, **kwargs):
    module = assembler()
    root, whitelist = write_root(tmp_path, **kwargs)
    return module.assemble_real_inputs(root, verify_hash=whitelist)


def _utc_naive(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def test_assembly_filters_window_registers_hole_and_never_touches_mt5(tmp_path):
    out = assembled(tmp_path)
    inputs = out["inputs"]
    assert set(inputs) == {TRADE, SIGNAL}
    trade, signal = inputs[TRADE], inputs[SIGNAL]
    assert trade["missing_kind"] == "missing_half_open"
    assert trade["source_eligibility"] == {"histdata": "real_precheck_only"}
    assert signal["source_eligibility"] == {"fred": "real_precheck_only"}
    assert set(trade["source_tags"]) == {"histdata"} and set(signal["source_tags"]) == {"fred"}
    assert trade["bars"].time.min() >= MAIN_START and trade["bars"].time.max() < MAIN_END
    assert signal["bars"].time.min() >= WARMUP_START and signal["bars"].time.max() < MAIN_END
    in_hole = ((trade["bars"].time >= HOLE[0]) & (trade["bars"].time < HOLE[1])).sum()
    assert in_hole == 0  # present in the parquet, cut by the registered hole
    holes = [(_utc_naive(h.start), _utc_naive(h.end)) for h in trade["missing_intervals"]]
    assert (HOLE[0], HOLE[1]) in holes
    assert list(signal["missing_intervals"]) == []  # rule-derived checked-empty
    for rec, pair in ((trade, ("EURUSD", "H1")), (signal, ("USTEC", "D1"))):
        assert rec["coverage"] and all(
            (s.symbol, s.timeframe) == pair and s.source == ("histdata" if pair[0] == "EURUSD" else "fred")
            for s in rec["coverage"])
        assert all(_utc_naive(s.end) <= MAIN_END for s in rec["coverage"])
        assert all(_utc_naive(s.start) >= (MAIN_START if pair[0] == "EURUSD" else WARMUP_START)
                   for s in rec["coverage"])
    assert isinstance(out["cost_model"], Costs)
    assert out["contract_sizes"] == {"EURUSD": 100000.0}


def test_provenance_registers_binding_2019_costs_and_scenario_basis(tmp_path):
    out = assembled(tmp_path)
    prov = out["provenance"]
    assert prov["status"] == "provisional"
    assert prov["research_status"] == "not_evaluated"
    assert prov["execution_status"] == "not_released"
    assert set(prov["binding"]["files"]) == set(PLAN_WHITELIST)
    assert prov["binding"]["policy"].startswith("path+sha256")
    gate = prov["used_gate"]
    assert gate[TRADE]["touched_years"] == [str(y) for y in range(2007, 2022)]
    assert gate[SIGNAL]["touched_years"] == [str(y) for y in range(2006, 2022)]
    assert all(y["used"] is True for y in gate[TRADE]["years"].values())
    assert gate[SIGNAL]["basis"] == "no_yearly_verification_declared"
    y2019 = prov["year_handling"]["2019"]
    assert y2019["included"] is True and y2019["calendar"] == "inconsistent"
    assert y2019["salvage"] is True and "2019-10" in y2019["excluded_months"]
    assert y2019["anchor"] == "consistent"
    assert y2019["hole"] == ["2019-10-01T00:00:00", "2019-11-01T00:00:00"]
    costs = prov["costs"]
    assert costs["commission"]["basis"] == "deal_measured"
    assert costs["commission"]["round_trip_per_lot"] == 0.0
    assert costs["commission"]["evidence"] == {"deals_in": 8, "deals_out": 12, "volume": 0.13}
    assert costs["swap"]["basis"] == "spec_snapshot_not_deal_measured"
    assert costs["swap"]["long_per_day"] == -5.9 and costs["swap"]["short_per_day"] == 0.0
    assert costs["swap"]["domain"] == "absolute_usd_per_lot_per_night"
    assert costs["spread"]["source_file"] == "costs/spread_model.json"
    assert costs["spread"]["model_sha256"] == "f" * 64
    assert costs["spread"]["scenario"] is True
    assert costs["spread"]["stats_used"]["p90_by_ny_hour"]["10"] == pytest.approx(5.2e-05)
    assert costs["slippage"]["spread_fraction"] == 0.1
    assert costs["contract"]["source_file"] == "specs/latest.json"
    assert costs["contract"]["contract_size"] == 100000.0
    rules = prov["gap_rules"]
    assert "monday_00_utc_to_friday_close" in rules["EURUSD/H1"]
    assert "weekday_grid" in rules["USTEC/D1"]
    assert rules["weekends"] == "constructively_excluded"


def test_real_mode_run_on_synthetic_copy(tmp_path):
    out = assembled(tmp_path)
    result = hrp.run_h_readonly_precheck(
        candidate(), inputs=out["inputs"], cost_model=out["cost_model"],
        contract_sizes=out["contract_sizes"],
        required_eligibility=hrp.REAL_ELIGIBILITY)
    assert result["research_status"] == "not_evaluated"
    assert result["execution_status"] == "not_released"
    trade = result["coverage"][TRADE]
    assert trade["bars"] == len(out["inputs"][TRADE]["bars"])
    assert trade["years"] > 13.5
    assert result["checks"]["power"]["cumulative"] == {
        "n_tests": None, "power": None, "status": "unknown"}
    assert "cumulative_n_unknown" in result["reasons"]
    detail = result["checks"]["ex_ante_cost"]["per_symbol"]["EURUSD"]
    assert detail["commission"] == 0.0
    # histdata spread is charged through the frozen external x1.5 policy.
    assert detail["spread_round_trip"] == pytest.approx(1.5 * 5.2e-05)


# --------------------------------------------------------- eligibility firewall

def synthetic_inputs():
    """Old-suite style synthetic fixture (synthetic_only eligibility)."""
    from mt5quant.h_main_scope import CoverageSegment
    times = pd.date_range("2007-01-01", periods=168, freq="h")
    trade = pd.DataFrame({"time": times, "close": 1.2 + np.arange(168) * 1e-4})
    sig = pd.DataFrame({"time": pd.date_range("2006-12-01", periods=60, freq="D"),
                        "close": 100. + np.arange(60)})
    return {
        TRADE: {"bars": trade, "source_tags": ["histdata"] * 168, "missing_kind": "missing_half_open",
                "missing_intervals": [], "source_eligibility": {"histdata": "synthetic_only"},
                "coverage": [CoverageSegment("EURUSD", "H1", "histdata", "2007-01-01", "2007-01-08")]},
        SIGNAL: {"bars": sig, "source_tags": ["fred"] * 60, "missing_kind": "missing_half_open",
                 "missing_intervals": [], "source_eligibility": {"fred": "synthetic_only"},
                 "coverage": [CoverageSegment("USTEC", "D1", "fred", "2006-12-01", "2007-02-01")]},
    }


def test_eligibility_constants_and_default_mode_unchanged():
    assert hrp.SYNTHETIC_ELIGIBILITY == "synthetic_only"
    assert hrp.REAL_ELIGIBILITY == "real_precheck_only"


def test_real_inputs_rejected_by_synthetic_gate_and_vice_versa(tmp_path):
    out = assembled(tmp_path)
    real = out["inputs"]
    with pytest.raises(PermissionError, match="synthetic_only required"):
        hrp.run_h_readonly_precheck(candidate(), inputs=real, cost_model=out["cost_model"],
                                    contract_sizes=out["contract_sizes"])
    # Metadata-only validation fails the same way, before any bars work.
    def meta_of(data):
        return {k: {f: v for f, v in rec.items() if f not in ("bars", "source_tags")}
                for k, rec in data.items()}

    with pytest.raises(PermissionError, match="synthetic_only required"):
        hrp._validate_metadata(meta_of(real))
    with pytest.raises(PermissionError, match="real_precheck_only required"):
        hrp._validate_metadata(meta_of(synthetic_inputs()),
                               required_eligibility=hrp.REAL_ELIGIBILITY)


def test_invalid_required_eligibility_rejected():
    meta = {k: {f: v for f, v in rec.items() if f not in ("bars", "source_tags")}
            for k, rec in synthetic_inputs().items()}
    with pytest.raises(ValueError, match="eligibility"):
        hrp._validate_metadata(meta, required_eligibility="approved")


# --------------------------------------------------------------- gap rule units

def test_hour_band_gaps_weekend_holiday_thin_close_and_empty_weeks():
    derive = assembler().derive_hour_band_gaps
    weeks = pd.date_range("2019-09-02", periods=8, freq="7D")  # eight Mondays
    frames = []
    for i, monday in enumerate(weeks):
        if i in (3, 4, 5, 6):  # a >30-day hole: four entirely empty weeks
            continue
        band = pd.date_range(monday, periods=24 * 5, freq="h")
        band = band[band.weekday < 5]
        if i == 1:  # mid-week holiday: whole Wednesday missing
            band = band[band.date != (monday + pd.Timedelta(days=2)).date()]
        if i == 2:  # thin Friday close: the post-close tail is outside the band
            band = band[~band.isin([monday + pd.Timedelta(days=4, hours=22),
                                    monday + pd.Timedelta(days=4, hours=23)])]
        frames.append(band)
    sunday = pd.date_range("2019-09-08 21:00", periods=3, freq="h")  # weekend bars
    times = pd.DatetimeIndex(sorted(frames[0].append(sunday).append(frames[1]).append(frames[2])))
    gaps = derive(times)
    intervals = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in gaps]
    wed = weeks[1] + pd.Timedelta(days=2)
    assert intervals == [(wed, wed + pd.Timedelta(days=1))]
    # The Monday-holiday case is recorded from the Monday 00:00 band start.
    monday = weeks[0]
    partial = pd.date_range(monday + pd.Timedelta(hours=8), periods=24 * 5 - 8, freq="h")
    partial = partial[partial.weekday < 5]
    assert [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in derive(partial)] == [
        (monday, monday + pd.Timedelta(hours=8))]
    assert list(derive(pd.DatetimeIndex([]))) == []


def test_weekday_gaps_holidays_runs_and_weekend_exclusion():
    derive = assembler().derive_weekday_gaps
    mon = pd.Timestamp("2019-12-02")
    days = [mon + pd.Timedelta(days=d) for d in range(14)]  # two Mon-Sun weeks
    base = pd.DatetimeIndex([d for d in days if d.weekday() < 5])
    one = base[~base.isin([pd.Timestamp("2019-12-05")])]  # Thursday holiday
    assert [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in derive(one)] == [
        (pd.Timestamp("2019-12-05"), pd.Timestamp("2019-12-06"))]
    two = base[~base.isin([pd.Timestamp("2019-12-05"), pd.Timestamp("2019-12-06")])]
    assert [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in derive(two)] == [
        (pd.Timestamp("2019-12-05"), pd.Timestamp("2019-12-09"))]  # spans the weekend
    assert list(derive(base)) == []  # weekends alone never count
    assert list(derive(pd.DatetimeIndex([]))) == []


# ----------------------------------------------------------------------- CLIs

def run_cli(script, args, cwd=ROOT):
    return subprocess.run([sys.executable, str(script), *args], cwd=cwd,
                          env={**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


def test_synthetic_cli_still_locks_mode_and_eligibility(tmp_path):
    module = assembler()
    root, _ = write_root(tmp_path)  # valid structure, but unbound synthetic hashes
    out = tmp_path / "never"
    proc = run_cli(SCRIPT, ["--data-root", str(root), "--candidate", str(CANDIDATE_DRAFT),
                            "--output", str(out)])
    assert proc.returncode == 2
    assert "sha256" in proc.stderr
    assert not out.exists()
    # The frozen synthetic CLI keeps rejecting real mode and real eligibility.
    manifest = {"mode": "synthetic", "inputs": {
        TRADE: {"bars_file": "EURUSD.json", "missing_kind": "missing_half_open",
                "missing_intervals": [], "source_eligibility": {"histdata": "real_precheck_only"},
                "coverage": [{"source": "histdata", "start": "2007-01-01", "end": "2007-01-08"}]},
        SIGNAL: {"bars_file": "USTEC.json", "missing_kind": "missing_half_open",
                 "missing_intervals": [], "source_eligibility": {"fred": "real_precheck_only"},
                 "coverage": [{"source": "fred", "start": "2006-12-01", "end": "2007-02-01"}]}}}
    frame = hourly_prices().iloc[:168]
    sig = daily_prices().iloc[:60]
    dir_ = tmp_path / "cli"
    dir_.mkdir()
    (dir_ / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    (dir_ / "EURUSD.json").write_text(json.dumps(
        {"bars": [{"time": t.isoformat(), "close": float(v)} for t, v in zip(frame.time, frame.close)],
         "source_tags": ["histdata"] * len(frame)}), encoding="utf-8")
    (dir_ / "USTEC.json").write_text(json.dumps(
        {"bars": [{"time": t.isoformat(), "close": float(v)} for t, v in zip(sig.time, sig.close)],
         "source_tags": ["fred"] * len(sig)}), encoding="utf-8")
    (dir_ / "candidate.json").write_text(
        json.dumps(tomllib.loads(CANDIDATE_DRAFT.read_text(encoding="utf-8"))), encoding="utf-8")
    (dir_ / "costs.json").write_text(json.dumps({"mode": "synthetic"}), encoding="utf-8")
    (dir_ / "specs.json").write_text(json.dumps({"mode": "synthetic"}), encoding="utf-8")
    base = ["--inputs", str(dir_ / "inputs.json"), "--candidate", str(dir_ / "candidate.json"),
            "--costs", str(dir_ / "costs.json"), "--specs", str(dir_ / "specs.json")]
    real_mode = run_cli(CLI, ["--mode", "real", *base])
    assert real_mode.returncode == 2 and "not_released" in real_mode.stderr
    cross = run_cli(CLI, ["--mode", "synthetic", *base])
    assert cross.returncode == 2 and "synthetic_only required" in cross.stderr
    assert not proc.stdout


def test_assembler_cli_runs_only_through_frozen_whitelist():
    module = assembler()
    assert module.REQUIRED_PATHS == tuple(sorted(PLAN_WHITELIST))
    # No CLI flag can override the frozen binding.
    help_text = _cli_flags()
    assert "--data-root" in help_text and "--candidate" in help_text and "--output" in help_text
    for flag in ("--verify-hash", "--whitelist", "--sha256", "--mode"):
        assert flag not in help_text


def _cli_flags():
    proc = run_cli(SCRIPT, ["--help"])
    assert proc.returncode == 0, proc.stderr
    return proc.stdout
