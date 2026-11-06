"""N1 (Pre-FOMC drift, USTEC/D1) real assembler + main-segment precheck tests.

Mirror of tests/test_h_real_precheck.py for the N1 assembler: every test binds
schema-isomorphic synthetic copies under a temporary data root (never the real
research store); the CLI keeps the frozen N1 whitelist/calendar/candidate pins
and is only exercised in its fail-closed direction. The C002 assembler and the
frozen CLI stay untouched (zero diff is enforced by the commit, not here).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import numpy as np
import pandas as pd
import pytest

from mt5quant.costs import Costs
from mt5quant.h_readonly_precheck import REAL_ELIGIBILITY
from mt5quant.research.candidates import (
    _observed_gaps, _segments_for_span, merged_coverage_years, validate_candidate)
from mt5quant.validation import events as vevents

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/h_assemble_n1_precheck.py"
SERIES = "USTEC/D1"
MAIN_END = pd.Timestamp("2021-05-17")
ERA_START = "1994-09-27"

# fix2 (2026-11-07): exactly one whitelist entry is rebound — the FRED
# manifest rewritten by the S2-S4c yearly verification program (41 entries
# 1986..2026 all used=true; parquet bytes bit-identical).  The pre-rebind
# manifest pin 08ff3dff... stays below only as provenance of the re-binding.
NEW_MANIFEST_SHA256 = ("6f068426077c2c935ba1bb1d43946144db966fa47708d64e1d"
                       "3007f39b3469d6")
OLD_MANIFEST_SHA256 = ("08ff3dff0f48330e48832c06eb21bbf8b7564f81616d5783cd"
                       "183ce31311c5fe")

N1_WHITELIST = {
    "ext/fred/USTEC/D1/data.parquet":
        "c6645256e2cf2ceb10474318c2e5f926a5157cc0c9d50027e55f1482200a71c6",
    "ext/fred/USTEC/D1/manifest.json": NEW_MANIFEST_SHA256,
    "costs/spread_model.json":
        "c35550b90fbefef0d61b6c767e9e5365d6480cffaee232a68da696edb667ce85",
    "specs/latest.json":
        "144bb2245479e3557c571c9f5f83ca92db9d269211da7845c9d5aa2467d3150f",
}
CALENDAR_SHA256 = ("a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e8"
                   "8c1e76ec971c5")
CANDIDATE_SHA256 = ("d00adad5f6303b557a97300e2c64c6737be8f2a3d07a72ca72"
                    "06f57af5040bfa")

CANDIDATE_TOML = """
id = "C005"
title = "Pre-FOMC announcement drift (synthetic N1 fixture)"
mechanism_class = "event"
status = "registered"
symbols = ["USTEC"]
holding = "D1"
mechanism = "synthetic fixture mechanism"

[literature]
original = ["Lucca & Moench (2015), synthetic fixture reference"]
replications = []
rebuttals = []
search_log = "synthetic fixture search log"
published_effect = "synthetic 49bp/24h"
expected_effect = "synthetic conservative 24.5bp per event"

[declared]
events_per_year = 8
gross_return_per_event = 0.00245
window_bars = 1
entry_hour_ny = 16
exit_hour_ny = 16
overnight_nights = 1.0
expected_sharpe = 0.53
side_rule = "static_long"
"""

_MODULE = None


def assembler():
    """Load scripts/h_assemble_n1_precheck.py; RED until the file exists."""
    global _MODULE
    if _MODULE is None:
        assert SCRIPT.exists(), "scripts/h_assemble_n1_precheck.py is not implemented"
        spec = importlib.util.spec_from_file_location("h_assemble_n1_precheck", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULE = module
    return _MODULE


def candidate():
    return validate_candidate(tomllib.loads(CANDIDATE_TOML), source="synthetic n1 fixture")


# --------------------------------------------------------------- synthetic root

def fred_manifest(*, yearly=None):
    doc = {"source": "fred", "symbol": "USTEC", "timeframe": "D1",
           "rows": 0, "first_time": None, "last_time": None}
    if yearly is not None:
        doc["yearly_verification"] = yearly
    return doc


def spread_doc(*, p90=5.2e-05):
    hours = {str(h): {"median": 5.0e-05, "p90": p90, "p99": 5.5e-05, "n": 100,
                      "n_days": 20}
             for h in range(24) if h != 17}  # USTEC keeps the 17:00 ET break open
    return {"version": 2, "mode": "relative", "model_sha256": "f" * 64,
            "built_at_utc": "2026-10-28T21:02:28+00:00", "sampling": "bar_open_m1",
            "watermarks": {"USTEC": {"rows": 1, "first_msc": 0, "last_msc": 1}},
            "model": {"mode": "relative", "sampling": "bar_open_m1",
                      "bucket_tz": "America/New_York",
                      "per_symbol": {"USTEC": hours},
                      "overall_median": {"USTEC": 5.0e-05}}}


def specs_doc(*, contract=1.0):
    return {"USTEC": {"name": "USTEC", "contract_size": contract, "point": 0.01,
                      "swap_mode": 1, "swap_rollover3days": 5,
                      "swap_long": -652.0, "swap_short": 0.0}}


def daily_prices(*, drop=()):
    grid = pd.date_range("1994-01-03", "2021-05-21", freq="D")
    grid = grid[grid.weekday < 5]
    grid = grid[~grid.isin([pd.Timestamp(d) for d in drop])]
    closes = 100.0 + 5.0 * np.sin(np.arange(len(grid)) / 3.0)
    return pd.DataFrame({"time": grid, "close": closes})


def calendar_doc():
    return {
        "artifact": "n1-fomc-calendar",
        "purpose": "synthetic fixture calendar",
        "event_definition": {
            "default_event_set": "synthetic fixture rule",
            "era_start": ERA_START,
            "era_rationale": "synthetic fixture",
            "announcement_day_rule": "last calendar day of the meeting"},
        "counts_by_year": {"1994": {"meetings": 1}},
        "meetings": [
            {"year": 1994, "label": "fixture pre-era", "start_date": "1994-02-03",
             "end_date": "1994-02-04", "kind": "meeting", "qualifier": None,
             "source_page": "fixture.htm", "announcement_date": "1994-02-04",
             "in_lm_era": False, "in_default_event_set": False},
            {"year": 1994, "label": "fixture era start", "start_date": ERA_START,
             "end_date": ERA_START, "kind": "meeting", "qualifier": None,
             "source_page": "fixture.htm", "announcement_date": ERA_START,
             "in_lm_era": True, "in_default_event_set": True},
            {"year": 1995, "label": "fixture call", "start_date": "1995-03-01",
             "end_date": "1995-03-01", "kind": "conference_call", "qualifier": None,
             "source_page": "fixture.htm", "announcement_date": "1995-03-01",
             "in_lm_era": True, "in_default_event_set": False},
            {"year": 2020, "label": "fixture month-end friday", "start_date": "2020-01-31",
             "end_date": "2020-01-31", "kind": "meeting", "qualifier": None,
             "source_page": "fixture.htm", "announcement_date": "2020-01-31",
             "in_lm_era": True, "in_default_event_set": True},
            {"year": 2020, "label": "fixture holiday hole", "start_date": "2020-11-26",
             "end_date": "2020-11-26", "kind": "meeting", "qualifier": None,
             "source_page": "fixture.htm", "announcement_date": "2020-11-26",
             "in_lm_era": True, "in_default_event_set": True},
            {"year": 2021, "label": "fixture outside window", "start_date": "2021-06-15",
             "end_date": "2021-06-15", "kind": "meeting", "qualifier": None,
             "source_page": "fixture.htm", "announcement_date": "2021-06-15",
             "in_lm_era": True, "in_default_event_set": True},
        ],
    }


def write_root(tmp_path, *, fred=None, prices=None, spread=None, specs=None,
               drop=(), stub_prices=False, tamper_first=None):
    """Write the four schema-isomorphic bound artifacts; return (root, whitelist).

    The manifest always describes the frame; stub_prices swaps in junk parquet
    bytes so pre-decode gates prove their ordering.
    """
    root = tmp_path / "data_research"
    fred = fred if fred is not None else fred_manifest()
    frame = prices if prices is not None else daily_prices(drop=drop)
    fred.update(rows=len(frame), first_time=frame.time.iloc[0].isoformat(),
                last_time=frame.time.iloc[-1].isoformat())
    if tamper_first is not None:
        fred["first_time"] = pd.Timestamp(tamper_first).isoformat()
    payloads = {"ext/fred/USTEC/D1/manifest.json": json.dumps(fred).encode("utf-8"),
                "costs/spread_model.json": json.dumps(
                    spread if spread is not None else spread_doc()).encode("utf-8"),
                "specs/latest.json": json.dumps(
                    specs if specs is not None else specs_doc()).encode("utf-8")}
    whitelist = {}
    for rel in N1_WHITELIST:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".parquet"):
            if stub_prices:
                path.write_bytes(b"definitely not a parquet file")
            else:
                frame.to_parquet(path, index=False)
        else:
            path.write_bytes(payloads[rel])
        whitelist[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return root, whitelist


def write_calendar(tmp_path, doc=None, *, raw=None):
    path = tmp_path / "fomc_meetings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps(doc if doc is not None else calendar_doc()),
                        encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def assembled(tmp_path, **kwargs):
    module = assembler()
    root, whitelist = write_root(tmp_path, **kwargs)
    calendar, calendar_sha = write_calendar(tmp_path)
    return module.assemble_n1_inputs(root, calendar, verify_hash=whitelist,
                                     calendar_sha=calendar_sha)


# ------------------------------------------------------------------- whitelist

def test_frozen_n1_pins_match_plan_binding():
    module = assembler()
    assert module.N1_WHITELIST == N1_WHITELIST
    assert module.REQUIRED_PATHS == tuple(sorted(N1_WHITELIST))
    assert module.CALENDAR_SHA256 == CALENDAR_SHA256
    assert module.CANDIDATE_SHA256 == CANDIDATE_SHA256
    assert module.CALENDAR_LABEL.endswith("fomc_meetings.json")
    assert module.CANDIDATE_LABEL.endswith("C005.toml")


def test_hash_mismatch_missing_file_and_unbound_paths_fail_closed(tmp_path):
    module = assembler()
    root, whitelist = write_root(tmp_path)
    calendar, calendar_sha = write_calendar(tmp_path)
    bad = dict(whitelist, **{"costs/spread_model.json": "0" * 64})
    with pytest.raises(ValueError, match="costs/spread_model.json.*sha256|sha256.*costs/spread_model.json"):
        module.assemble_n1_inputs(root, calendar, verify_hash=bad, calendar_sha=calendar_sha)
    partial = {k: v for k, v in whitelist.items() if k != "specs/latest.json"}
    with pytest.raises(ValueError, match="whitelist"):
        module.assemble_n1_inputs(root, calendar, verify_hash=partial, calendar_sha=calendar_sha)
    empty, _ = write_root(tmp_path / "gap")
    (empty / "ext/fred/USTEC/D1/data.parquet").unlink()
    with pytest.raises(ValueError):
        module.assemble_n1_inputs(empty, calendar, verify_hash={
            k: v for k, v in whitelist.items() if k != "ext/fred/USTEC/D1/data.parquet"},
            calendar_sha=calendar_sha)
    # Unbound extra files are never read and never block a bound assembly.
    (root / "costs/unbound.json").write_text("{}", encoding="utf-8")
    (root / "ext/fred/USTEC/D1/other.parquet").write_bytes(b"junk")
    out = module.assemble_n1_inputs(root, calendar, verify_hash=whitelist,
                                    calendar_sha=calendar_sha)
    assert set(out["provenance"]["binding"]["files"]) == set(N1_WHITELIST)


def test_used_gate_rejects_false_year_before_price_parse(tmp_path):
    module = assembler()
    root, whitelist = write_root(
        tmp_path, stub_prices=True,
        fred=fred_manifest(yearly={"1994": {"used": False, "calendar": "na"},
                                   **{str(y): {"used": True, "calendar": "us_wall"}
                                      for y in range(1995, 2022)}}))
    calendar, calendar_sha = write_calendar(tmp_path)
    with pytest.raises(ValueError, match="1994.*used|used.*1994"):
        module.assemble_n1_inputs(root, calendar, verify_hash=whitelist,
                                  calendar_sha=calendar_sha)


def test_manifest_must_match_parquet_first_last_day(tmp_path):
    module = assembler()
    root, whitelist = write_root(tmp_path, tamper_first="1993-12-01")
    calendar, calendar_sha = write_calendar(tmp_path)
    with pytest.raises(ValueError, match="first_time|manifest"):
        module.assemble_n1_inputs(root, calendar, verify_hash=whitelist,
                                  calendar_sha=calendar_sha)


# ------------------------------------------------------------------ assembling

def _naive(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def test_assembly_measures_main_window_and_registers_hole(tmp_path):
    module = assembler()
    out = assembled(tmp_path, drop=("2020-11-26",))
    inputs = out["inputs"][SERIES]
    bars = inputs["bars"]
    assert inputs["missing_kind"] == "missing_half_open"
    assert inputs["source_eligibility"] == {"fred": REAL_ELIGIBILITY}
    assert set(inputs["source_tags"]) == {"fred"}
    assert bars.time.min() == pd.Timestamp("1994-01-03")
    assert bars.time.max() < MAIN_END
    assert (bars.time.dt.weekday < 5).all()
    assert (bars.time == bars.time.dt.floor("D")).all()  # FRED date labels
    holes = [(_naive(h.start), _naive(h.end)) for h in inputs["missing_intervals"]]
    assert (pd.Timestamp("2020-11-26"), pd.Timestamp("2020-11-27")) in holes
    (coverage,) = inputs["coverage"]
    assert (coverage.symbol, coverage.timeframe, coverage.source) == ("USTEC", "D1", "fred")
    assert _naive(coverage.start) == pd.Timestamp("1994-01-03")
    assert _naive(coverage.end) <= MAIN_END
    prov = out["provenance"]
    assert prov["main_window"] == ["1994-01-03T00:00:00", "2021-05-17T00:00:00"]
    assert prov["used_gate"][SERIES]["basis"] == "no_yearly_verification_declared"
    assert prov["mt5_series"] == "never included"
    assert isinstance(out["cost_model"], Costs)
    assert out["contract_sizes"] == {"USTEC": 1.0}


def test_provenance_registers_costs_scenario_and_calendar(tmp_path):
    module = assembler()
    out = assembled(tmp_path)
    prov = out["provenance"]
    assert prov["status"] == "provisional"
    assert prov["research_status"] == "not_evaluated"
    assert prov["execution_status"] == "not_released"
    assert prov["binding"]["calendar"]["sha256"] == hashlib.sha256(
        Path(prov["binding"]["calendar"]["path"]).read_bytes()).hexdigest()
    assert prov["binding"]["calendar"]["path"].endswith("fomc_meetings.json")
    assert prov["year_handling"]["basis"] == "not_applicable_fred_has_no_yearly_gate"
    costs = prov["costs"]
    assert costs["commission"]["basis"] == "declared_terms"
    assert costs["commission"]["round_trip_per_lot"] == 0.0
    assert "account-fee-terms-2026-11-06" in costs["commission"]["evidence"]
    assert costs["swap"]["basis"] == "declared_terms_swap_free"
    assert costs["swap"]["long_per_day"] == 0.0 and costs["swap"]["short_per_day"] == 0.0
    charged = costs["swap"]["charged_reference"]
    assert charged["basis"] == "spec_snapshot_not_deal_measured"
    assert charged["worst_side_usd_per_lot_per_night"] == pytest.approx(6.52)
    assert charged["overnight_fraction_at_reference_1_night"] > 0
    assert costs["spread"]["source_file"] == "costs/spread_model.json"
    assert costs["spread"]["scenario"] is True
    assert costs["spread"]["source_policy"] == "external-spread-x1.5-v1"
    assert costs["spread"]["missing_hours"] == [17]
    assert costs["spread"]["stats_used"]["p90_by_ny_hour"]["16"] == pytest.approx(5.2e-05)
    assert costs["slippage"]["spread_fraction"] == 0.1
    assert costs["contract"]["source_file"] == "specs/latest.json"
    assert costs["contract"]["contract_size"] == 1.0
    rules = prov["gap_rules"]
    assert "weekday_grid" in rules[SERIES]
    assert rules["weekends"] == "constructively_excluded"


def test_calendar_registration_counts_window_events_and_anchors(tmp_path):
    module = assembler()
    out = assembled(tmp_path, drop=("2020-11-26",))  # holiday hole on 2020-11-26
    cal = out["calendar"]
    assert cal["total_meetings"] == 6
    assert cal["default_event_set"] == 4
    assert cal["era_start"] == ERA_START
    assert cal["event_window"] == ["1994-09-27T00:00:00", "2021-05-17T00:00:00"]
    assert cal["window_events"] == 3          # era start, month-end, holiday hole
    assert cal["window_first"] == ERA_START and cal["window_last"] == "2020-11-26"
    assert cal["events_with_announcement_bar"] == 2   # 2020-11-26 has no FRED bar
    assert cal["events_with_entry_anchor"] == 2       # era start anchors 1994-09-26
    assert cal["dropped_no_bar"] == ["2020-11-26"]
    assert cal["dropped_no_anchor"] == []
    assert cal["month_end_overlap"]["fred_last_trading_day"] == ["2020-01-31"]
    assert cal["month_end_overlap"]["calendar_last_weekday"] == ["2020-01-31"]
    assert "provenance" in cal["note"]  # counts registered, never gating


def test_calendar_event_on_first_bar_has_no_anchor(tmp_path):
    module = assembler()
    # First delivered bar exactly on the era start: the era event cannot anchor.
    grid = pd.date_range(ERA_START, "1995-12-29", freq="D")
    weekdays = grid[grid.weekday < 5]
    frame = pd.DataFrame({"time": weekdays,
                          "close": 100.0 + 5.0 * np.sin(np.arange(len(weekdays)) / 3.0)})
    root, whitelist = write_root(tmp_path, prices=frame)
    doc = calendar_doc()
    doc["meetings"] = [m for m in doc["meetings"]
                       if m["announcement_date"] == ERA_START]
    calendar, _ = write_calendar(tmp_path, doc=doc)
    out = module.assemble_n1_inputs(
        root, calendar, verify_hash=whitelist,
        calendar_sha=hashlib.sha256(calendar.read_bytes()).hexdigest())
    cal = out["calendar"]
    assert cal["window_events"] == 1
    assert cal["events_with_announcement_bar"] == 1
    assert cal["events_with_entry_anchor"] == 0
    assert cal["dropped_no_anchor"] == [ERA_START]


def test_calendar_binding_and_structure_fail_closed(tmp_path):
    module = assembler()
    root, whitelist = write_root(tmp_path)
    # Calendar content tampered after hashing: sha mismatch refuses.
    calendar, calendar_sha = write_calendar(tmp_path)
    doc = calendar_doc()
    calendar.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="calendar.*sha256|sha256.*calendar"):
        module.assemble_n1_inputs(root, calendar, verify_hash=whitelist,
                                  calendar_sha="0" * 64)
    # Structural refusals (mutation 3 targets the default era meeting, index 1).
    for mutate, pattern in (
            (lambda d: d.update(meetings={}), "meetings"),
            (lambda d: d["meetings"][1].pop("in_default_event_set"), "field"),
            (lambda d: d["meetings"][1].update(end_date="1994-09-28"), "announcement"),
            (lambda d: d.update(event_definition={}), "era_start"),
    ):
        doc = calendar_doc()
        mutate(doc)
        bad_calendar, _ = write_calendar(tmp_path / "struct", doc=doc)
        sha = hashlib.sha256(bad_calendar.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match=pattern):
            module.assemble_n1_inputs(root, bad_calendar, verify_hash=whitelist,
                                      calendar_sha=sha)
    duplicate, _ = write_calendar(
        tmp_path / "dup", raw='{"artifact": "a", "artifact": "b", "meetings": [], '
                              '"purpose": "p", "event_definition": {"era_start": "1994-09-27"}, '
                              '"counts_by_year": {}}')
    with pytest.raises(ValueError, match="duplicate"):
        module.assemble_n1_inputs(root, duplicate, verify_hash=whitelist,
                                  calendar_sha=hashlib.sha256(
                                      duplicate.read_bytes()).hexdigest())


def test_candidate_pin_fails_closed_on_hash(tmp_path):
    module = assembler()
    path = tmp_path / "C005.toml"
    path.write_text(CANDIDATE_TOML, encoding="utf-8")
    good = module.load_candidate(path, expected_sha=hashlib.sha256(
        path.read_bytes()).hexdigest())
    assert good.id == "C005" and good.symbols == ("USTEC",)
    with pytest.raises(ValueError, match="candidate.*sha256|sha256.*candidate"):
        module.load_candidate(path, expected_sha="0" * 64)


# --------------------------------------------------------- sigma and gap rules

def test_weekday_gaps_holidays_runs_and_weekend_exclusion():
    derive = assembler().derive_weekday_gaps
    mon = pd.Timestamp("2019-12-02")
    days = [mon + pd.Timedelta(days=d) for d in range(14)]
    base = pd.DatetimeIndex([d for d in days if d.weekday() < 5])
    one = base[~base.isin([pd.Timestamp("2019-12-05")])]
    assert [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in derive(one)] == [
        (pd.Timestamp("2019-12-05"), pd.Timestamp("2019-12-06"))]
    two = base[~base.isin([pd.Timestamp("2019-12-05"), pd.Timestamp("2019-12-06")])]
    assert [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in derive(two)] == [
        (pd.Timestamp("2019-12-05"), pd.Timestamp("2019-12-09"))]
    assert list(derive(base)) == []
    assert list(derive(pd.DatetimeIndex([]))) == []


def test_d1_sigma_is_single_bar_all_windows_gap_cut_no_hour_branch():
    module = assembler()
    # Two tight weekday clusters 60 days apart: +-10% returns inside each,
    # the cross-cluster window would be a -9% jump and must be gap-cut.
    left = pd.DatetimeIndex(["1994-01-03", "1994-01-04", "1994-01-05", "1994-01-06"])
    right = pd.DatetimeIndex(["1994-03-07", "1994-03-08", "1994-03-09", "1994-03-10"])
    closes = np.array([100.0, 110.0, 121.0, 133.1, 100.0, 90.0, 81.0, 72.9])
    frame = pd.DataFrame({"time": left.append(right), "close": closes})
    gaps = [(pd.Timestamp("1994-01-06"), pd.Timestamp("1994-03-07"))]
    sigma, count = module.d1_window_sigma(
        frame, gaps, 1, main_start=pd.Timestamp("1994-01-03"), main_end=MAIN_END)
    assert count == 6  # 3 + 3 inner windows; the cross-cluster pair is cut
    assert sigma == pytest.approx(0.1 * math.sqrt(6 / 5))  # ddof=1 over +-10%
    # Without the gap the jump window joins and sigma changes.
    sigma_all, count_all = module.d1_window_sigma(
        frame, [], 1, main_start=pd.Timestamp("1994-01-03"), main_end=MAIN_END)
    assert count_all == 7 and sigma_all != pytest.approx(sigma)
    # D1 date labels carry no NY entry hour: windows from both DST seasons count.
    days = pd.DatetimeIndex([pd.Timestamp("1994-01-04"), pd.Timestamp("1994-04-05"),
                             pd.Timestamp("1994-07-05")])
    probe = pd.DataFrame({"time": days, "close": [100.0, 110.0, 99.0]})
    sigma2, count2 = module.d1_window_sigma(
        probe, [], 1, main_start=pd.Timestamp("1994-01-01"), main_end=MAIN_END)
    assert count2 == 2  # a January window and a July window - no hour filter
    assert sigma2 == pytest.approx(np.std([0.1, -0.1], ddof=1))


# ------------------------------------------------------------------- the runner

def test_run_n1_precheck_on_synthetic_copy(tmp_path):
    module = assembler()
    out = assembled(tmp_path)
    result = module.run_n1_precheck(
        candidate(), inputs=out["inputs"], cost_model=out["cost_model"],
        contract_sizes=out["contract_sizes"])
    assert result["research_status"] == "not_evaluated"
    assert result["execution_status"] == "not_released"
    assert result["diagnostic_status"] == "engineering_waiting"
    assert "cumulative_n_unknown" in result["reasons"]
    cov = result["coverage"][SERIES]
    assert cov["bars"] == len(out["inputs"][SERIES]["bars"])
    assert cov["years"] > 27.0  # 1994-01-03 .. 2021-05-14 minus no holes
    # Power-years contract: the gate counts years from the event era start
    # (1994-09-27), with the full-main-segment basis registered alongside.
    era_cov = result["coverage_event_era"][SERIES]
    assert era_cov["bars"] < cov["bars"]
    assert result["checks"]["power"]["years"] == pytest.approx(era_cov["years"])
    assert result["checks"]["power"]["years_full_main_segment"] == pytest.approx(cov["years"])
    assert "1994-09-27" in result["checks"]["power"]["years_basis"]
    power_block = result["checks"]["power"]
    assert power_block["cumulative"] == {"n_tests": None, "power": None,
                                         "status": "unknown"}
    assert power_block["sigma_window_bars"] == 1
    assert power_block["sigma_by_symbol"]["USTEC"] > 0
    detail = result["checks"]["ex_ante_cost"]["per_symbol"]["USTEC"]
    assert detail["commission"] == 0.0
    assert detail["overnight"] == 0.0
    assert detail["spread_round_trip"] == pytest.approx(1.5 * 5.2e-05)
    assert detail["slippage_entry"] == pytest.approx(0.1 * 5.2e-05)
    total = detail["round_trip_cost"]
    assert total == pytest.approx(1.5 * 5.2e-05 + 0.2 * 5.2e-05)
    assert detail["multiple"] == pytest.approx(0.00245 / total)
    assert result["checks"]["ex_ante_cost"]["passed"] is True
    assert result["checks"]["power"]["passed"] is True
    assert result["checks"]["literature"]["passed"] is True
    assert result["checks"]["holding"]["passed"] is True
    assert result["blockers"] == ["production_input_release_required",
                                  "n1_registration_package_review_pending"]


def test_run_rejects_wrong_eligibility_and_bad_candidate_shape(tmp_path):
    module = assembler()
    out = assembled(tmp_path)
    real = {k: dict(v) for k, v in out["inputs"].items()}
    real[SERIES] = dict(real[SERIES], source_eligibility={"fred": "synthetic_only"})
    with pytest.raises(PermissionError, match="real_precheck_only required"):
        module.run_n1_precheck(candidate(), inputs=real, cost_model=out["cost_model"],
                               contract_sizes=out["contract_sizes"])
    with pytest.raises(ValueError, match="eligibility"):
        module.run_n1_precheck(candidate(), inputs=out["inputs"],
                               cost_model=out["cost_model"],
                               contract_sizes=out["contract_sizes"],
                               required_eligibility="approved")
    wrong = tomllib.loads(CANDIDATE_TOML.replace('"USTEC"', '"EURUSD"'))
    with pytest.raises(ValueError, match="USTEC/D1"):
        module.run_n1_precheck(validate_candidate(wrong, source="synthetic"),
                               inputs=out["inputs"], cost_model=out["cost_model"],
                               contract_sizes=out["contract_sizes"])


# ----------------------------------------------------------------------- the CLI

def run_cli(script, args, cwd=ROOT):
    return subprocess.run([sys.executable, str(script), *args], cwd=cwd,
                          env={**os.environ, "PYTHONUTF8": "1",
                               "PYTHONDONTWRITEBYTECODE": "1"},
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)


def test_cli_fail_closed_on_synthetic_root_and_no_override_flags(tmp_path):
    module = assembler()
    root, _ = write_root(tmp_path)          # valid structure, unbound hashes
    calendar, _ = write_calendar(tmp_path)
    cand = tmp_path / "C005.toml"
    cand.write_text(CANDIDATE_TOML, encoding="utf-8")
    out = tmp_path / "never"
    proc = run_cli(SCRIPT, ["--data-root", str(root), "--calendar", str(calendar),
                            "--candidate", str(cand), "--output", str(out)])
    assert proc.returncode == 2
    assert "sha256" in proc.stderr
    assert not out.exists()
    assert not proc.stdout
    help_text = _cli_flags()
    for flag in ("--data-root", "--calendar", "--candidate", "--output"):
        assert flag in help_text
    for flag in ("--verify-hash", "--whitelist", "--sha256", "--calendar-sha",
                 "--candidate-sha", "--mode"):
        assert flag not in help_text


def _cli_flags():
    proc = run_cli(SCRIPT, ["--help"])
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# ------------------------------------------- fix1 (2026-11-07): registered payload repair
#
# run1 of the R3 main test failed assembly because the registered N1 main-only
# precheck payload carried no checks.power.coverage_segments, so
# bt._load_h_research_inputs never activated the FRED merged load path
# (.superpowers/sdd/2026-11-07-r3-main-test/root-cause-refusal.md).  The fix is
# assembler-side only: the payload must first carry the segments in exactly the
# shape bt candidate check natively produces (candidates.coverage_segments /
# _segments_for_span / merged_coverage_years); the frozen consumer in
# bt.py:3676 only needs any(s["counted"] and s["source"] != "mt5").

def test_run_registers_native_power_coverage_segments(tmp_path):
    module = assembler()
    out = assembled(tmp_path)
    result = module.run_n1_precheck(
        candidate(), inputs=out["inputs"], cost_model=out["cost_model"],
        contract_sizes=out["contract_sizes"])
    power = result["checks"]["power"]
    assert "coverage_segments" in power
    bars = out["inputs"][SERIES]["bars"]
    expected = _segments_for_span("fred", bars.time.iloc[0], bars.time.iloc[-1],
                                  _observed_gaps(bars.time))
    assert power["coverage_segments"] == {SERIES: expected}
    assert power["coverage_merged_years"] == {
        SERIES: merged_coverage_years(expected)}
    assert power["coverage_merge_method"] == _COVERAGE_MERGE_METHOD_NOTE
    # The bt merged_keys predicate (bt.py:3676) must activate on this payload.
    assert any(s["counted"] and s["source"] != "mt5"
               for s in power["coverage_segments"][SERIES])
    seg = power["coverage_segments"][SERIES][0]
    assert set(seg) == {"source", "first", "last", "years", "counted"}
    assert seg["source"] == "fred" and seg["counted"] is True
    assert seg["first"] == bars.time.iloc[0].isoformat()
    assert seg["last"] == bars.time.iloc[-1].isoformat()
    # Existing numbers stay untouched: same fields as before the fix.
    assert power["years_detail"] == {SERIES: power["years"]}


_COVERAGE_MERGE_METHOD_NOTE = (
    "计入段日历区间并集：相接/重叠的段合成连续跨度（min(first).."
    "max(last)），不相接的分段各自计入——重叠不双计（T80/G3 审核"
    "修正：相加会把完全包含/部分重叠的覆盖虚增，如 fred/USTEC "
    "39.8 年包住 mt5 4.3 年 → 合并 39.8 而非 44.1）；每段在产出侧"
    "已在 >30 天内部空洞处切开（T94/R14-L3：实测间隔+known_gaps+"
    "manifest 未启用年份，边界随段的 gaps 字段）")


def test_assemble_derives_ustec_trading_calendar_v1(tmp_path):
    """fix1 root cause 2: the assembly derives the {data-root}/calendars/USTEC.json
    v1 content from the bound FRED series (all delivered trading days, not just
    the main window) and the engine's trading_calendar() must accept it."""
    module = assembler()
    out = assembled(tmp_path)
    doc = out["trading_calendar"]
    assert doc["version"] == 1
    frame = daily_prices()
    cal = vevents.trading_calendar(doc)
    assert list(cal.day) == list(pd.DatetimeIndex(frame.time))
    assert (cal.close_time == cal.day + pd.Timedelta(days=1)).all()
    assert (cal.close_time > cal.day).all()
    assert (cal.close_time <= cal.day + pd.Timedelta(days=2)).all()
    assert pd.Timestamp(doc["complete_through"]) == pd.Timestamp(frame.time.iloc[-1])


def test_trading_calendar_counterexamples_rejected(tmp_path):
    """Counterexample guard: every malformed variant the engine's v1 validator
    refuses must differ from what the assembler exports."""
    module = assembler()
    out = assembled(tmp_path)
    doc = out["trading_calendar"]
    sessions = [dict(s) for s in doc["sessions"]]
    head = sessions[0]
    variants = {
        "wrong version": {**doc, "version": 2},
        "duplicated day": {**doc, "sessions": sessions + [dict(sessions[-1])]},
        "close_time not after day": {
            **doc, "sessions": [dict(head, close_time=head["day"])] + sessions[1:]},
        "close_time beyond day+2": {
            **doc, "sessions": [dict(
                head, close_time=(pd.Timestamp(head["day"])
                                  + pd.Timedelta(days=3)).date().isoformat())]
            + sessions[1:]},
        "tz-aware complete_through": {
            **doc, "complete_through": doc["complete_through"] + "T00:00:00+00:00"},
        "non-midnight day": {
            **doc, "sessions": [dict(head, day=head["day"] + "T12:00:00")]
            + sessions[1:]},
        "missing sessions": {k: v for k, v in doc.items() if k != "sessions"},
    }
    for name, bad in variants.items():
        with pytest.raises(ValueError):
            vevents.trading_calendar(bad)


def test_cli_export_calendar_flag_and_fail_closed_no_write(tmp_path):
    module = assembler()
    root, _ = write_root(tmp_path)          # valid structure, unbound hashes
    calendar, _ = write_calendar(tmp_path)
    cand = tmp_path / "C005.toml"
    cand.write_text(CANDIDATE_TOML, encoding="utf-8")
    out = tmp_path / "never"
    export = tmp_path / "calendars" / "USTEC.json"
    proc = run_cli(SCRIPT, ["--data-root", str(root), "--calendar", str(calendar),
                            "--candidate", str(cand), "--output", str(out),
                            "--export-calendar", str(export)])
    assert proc.returncode == 2            # binding refusal precedes any export
    assert "sha256" in proc.stderr
    assert not export.exists()
    assert not out.exists()
    assert "--export-calendar" in _cli_flags()


# ------------------------------- fix2 (2026-11-07): FRED yearly-verification rebind
#
# The S2-S4c FRED yearly verification program rewrote the bound research
# manifest to carry yearly_verification (41 entries 1986..2026, all used=true)
# with the parquet bytes bit-identical, so the frozen whitelist rebinds exactly
# one entry (.superpowers/sdd/2026-11-07-r3-main-test-fix1/
# s2-s4-real-verification/).  fix1-F1 (focused review, low severity): the
# calendar export wrote with the platform newline, so on Windows the on-disk
# artifact hash diverged from the PROVENANCE-recorded text hash — the export
# write is now pinned to LF-only bytes.


def test_whitelist_rebind_is_exactly_the_manifest_sha():
    module = assembler()
    entry = module.N1_WHITELIST["ext/fred/USTEC/D1/manifest.json"]
    assert entry == NEW_MANIFEST_SHA256
    assert entry != OLD_MANIFEST_SHA256
    others = {k: v for k, v in module.N1_WHITELIST.items()
              if k != "ext/fred/USTEC/D1/manifest.json"}
    assert others == {
        "ext/fred/USTEC/D1/data.parquet":
            "c6645256e2cf2ceb10474318c2e5f926a5157cc0c9d50027e55f1482200a71c6",
        "costs/spread_model.json":
            "c35550b90fbefef0d61b6c767e9e5365d6480cffaee232a68da696edb667ce85",
        "specs/latest.json":
            "144bb2245479e3557c571c9f5f83ca92db9d269211da7845c9d5aa2467d3150f",
    }


def test_used_gate_accepts_all_used_yearly_verification(tmp_path):
    """S5.4: with yearly_verification all used=true the used gate passes every
    touched year (no BindingError) and the year_handling / gap_rules
    registrations state the manifest's yearly reality truthfully."""
    module = assembler()
    yearly = {str(y): {"used": True, "verification": "fred_d1_v1"}
              for y in range(1994, 2022)}
    out = assembled(tmp_path, fred=fred_manifest(yearly=yearly))
    gate = out["provenance"]["used_gate"][SERIES]
    assert gate["basis"] == "yearly_verification"
    assert sorted(gate["years"]) == [str(y) for y in range(1994, 2022)]
    assert all(v == {"used": True, "calendar": None}
               for v in gate["years"].values())
    handling = out["provenance"]["year_handling"]
    assert handling["basis"] == "yearly_verification_all_touched_years_used"
    assert "1994" in handling["note"] and "2021" in handling["note"]
    explicit = out["provenance"]["gap_rules"]["explicit_registration"]
    assert "yearly_verification" in explicit
    assert "no yearly_verification at all" not in explicit
    # The no-yearly fixture keeps the pre-rebind registration verbatim.
    plain = assembled(tmp_path / "plain")
    assert plain["provenance"]["year_handling"] == {
        "basis": "not_applicable_fred_has_no_yearly_gate",
        "note": "FRED manifest 无逐年声明；2019 类回收年处理 N/A"}
    assert plain["provenance"]["gap_rules"]["explicit_registration"] == \
        assembler().GAP_RULES["explicit_registration"]
    # The pre-registered rule constant itself is untouched (rules-hash domain).
    assert "no yearly_verification at all" in \
        assembler().GAP_RULES["explicit_registration"]


def test_calendar_export_writes_lf_only_bytes(tmp_path):
    """fix1-F1: the export write is pinned to newline="\\n" so the on-disk
    artifact is byte-identical to the PROVENANCE-recorded text (no CRLF
    translation on Windows) and the returned hash is the on-disk hash."""
    module = assembler()
    doc = assembled(tmp_path)["trading_calendar"]
    target = tmp_path / "calendars" / "USTEC.json"
    sha = module._write_calendar_export(target, doc)
    raw = target.read_bytes()
    assert b"\r" not in raw
    text = json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
    assert raw == text.encode("utf-8")
    assert sha == hashlib.sha256(raw).hexdigest()
