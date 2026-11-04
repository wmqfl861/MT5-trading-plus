"""Synthetic metadata contracts; no prices, calendars or research execution."""
from __future__ import annotations

import builtins
import copy
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
import importlib
import io
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


TRADE = ("EURUSD", "H1")
SIGNAL = ("USTEC", "D1")


def _api():
    # Import inside tests so an absent implementation produces a RED test run,
    # not a collection failure that prevents the synthetic cases from running.
    return importlib.import_module("mt5quant.h_main_scope")


def _coverage(api):
    return [
        api.CoverageSegment(*TRADE, "histdata", "2007-01-01", "2021-05-17"),
        api.CoverageSegment(*SIGNAL, "fred", "2006-12-01", "2021-06-01"),
    ]


def _known_empty():
    return {TRADE: [], SIGNAL: []}


def _rows(segments):
    return tuple(
        (s.symbol, s.timeframe, s.source, s.start.isoformat(), s.end.isoformat())
        for s in segments
    )


def _bounds(segments):
    return tuple((s.start.isoformat(), s.end.isoformat()) for s in segments)


def test_mixed_trade_sources_are_clipped_and_diagnostic_parts_are_excluded():
    api = _api()
    coverage = [
        api.CoverageSegment(*TRADE, "histdata", "2020-01-01", "2022-01-01"),
        api.CoverageSegment(*TRADE, "mt5", "2005-01-01", "2023-01-01"),
        api.CoverageSegment(*TRADE, "histdata", "2006-01-01", "2020-01-01"),
        api.CoverageSegment(*SIGNAL, "fred", "2006-12-01", "2022-01-01"),
    ]
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=_known_empty())
    assert isinstance(report, api.ScopeReport)
    assert report.status == "metadata_valid"
    assert report.execution_status == "not_released"
    assert _rows(report.trade_intervals) == (
        ("EURUSD", "H1", "histdata", "2007-01-01T00:00:00+00:00", "2021-05-17T00:00:00+00:00"),
    )
    assert _bounds(report.signal_intervals) == (
        ("2006-12-01T00:00:00+00:00", "2022-01-01T00:00:00+00:00"),
    )
    assert tuple((_rows((segment,))[0], reason) for segment, reason in report.excluded) == (
        (("EURUSD", "H1", "mt5", "2005-01-01T00:00:00+00:00", "2023-01-01T00:00:00+00:00"), "trade_mt5_diagnostic"),
        (("EURUSD", "H1", "histdata", "2006-01-01T00:00:00+00:00", "2007-01-01T00:00:00+00:00"), "trade_outside_main"),
        (("EURUSD", "H1", "histdata", "2021-05-17T00:00:00+00:00", "2022-01-01T00:00:00+00:00"), "trade_outside_main"),
    )
    assert report.reasons == ("trade_mt5_diagnostic", "trade_outside_main")


@pytest.mark.parametrize("start,end", [
    ("2006-12-01", "2007-01-01"),
    ("2021-05-17", "2021-06-01"),
])
def test_trade_touching_main_boundary_has_empty_intersection(start, end):
    api = _api()
    coverage = _coverage(api)
    coverage[0] = api.CoverageSegment(*TRADE, "histdata", start, end)
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=_known_empty())
    assert report.status == "waiting_data"
    assert report.trade_intervals == report.signal_intervals == ()
    assert report.reasons == ("trade_coverage_empty", "trade_outside_main")
    assert _bounds(s for s, _ in report.excluded) == (
        (f"{start}T00:00:00+00:00", f"{end}T00:00:00+00:00"),
    )


def test_half_open_missing_boundaries_do_not_delete_touching_coverage():
    api = _api()
    missing = {
        TRADE: [
            api.TimeInterval("2006-12-01", "2007-01-01"),
            api.TimeInterval("2021-05-17", "2022-01-01"),
        ],
        SIGNAL: [],
    }
    report = api.validate_h_main_scope(coverage=_coverage(api), missing_intervals=missing)
    assert report.status == "metadata_valid"
    assert _bounds(report.trade_intervals) == (
        ("2007-01-01T00:00:00+00:00", "2021-05-17T00:00:00+00:00"),
    )
    assert report.excluded == ()


def test_missing_union_is_subtracted_once_without_bar_period_or_thirty_day_guess():
    api = _api()
    coverage = _coverage(api)
    coverage[0] = api.CoverageSegment(*TRADE, "histdata", "2007-01-01", "2007-01-12")
    missing = {
        TRADE: [
            api.TimeInterval("2007-01-05", "2007-01-09"),
            api.TimeInterval("2007-01-02", "2007-01-04"),
            api.TimeInterval("2007-01-04", "2007-01-06"),
            api.TimeInterval("2007-01-02", "2007-01-04"),
            api.TimeInterval("2006-12-01", "2007-01-01"),
            api.TimeInterval("2007-01-10T12:00:00Z", "2007-01-10T12:00:00.000001Z"),
            api.TimeInterval("2007-01-11", "2007-01-20"),
        ],
        SIGNAL: [],
    }
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=missing)
    assert report.status == "metadata_valid"
    assert _bounds(report.trade_intervals) == (
        ("2007-01-01T00:00:00+00:00", "2007-01-02T00:00:00+00:00"),
        ("2007-01-09T00:00:00+00:00", "2007-01-10T12:00:00+00:00"),
        ("2007-01-10T12:00:00.000001+00:00", "2007-01-11T00:00:00+00:00"),
    )
    assert _bounds(s for s, _ in report.excluded) == (
        ("2007-01-02T00:00:00+00:00", "2007-01-09T00:00:00+00:00"),
        ("2007-01-10T12:00:00+00:00", "2007-01-10T12:00:00.000001+00:00"),
        ("2007-01-11T00:00:00+00:00", "2007-01-12T00:00:00+00:00"),
    )
    assert report.reasons == ("declared_missing",)


@pytest.mark.parametrize("missing,expected", [
    (None, ("missing_intervals_unknown:EURUSD/H1", "missing_intervals_unknown:USTEC/D1")),
    ({}, ("missing_intervals_unknown:EURUSD/H1", "missing_intervals_unknown:USTEC/D1")),
    ({TRADE: None, SIGNAL: None}, ("missing_intervals_unknown:EURUSD/H1", "missing_intervals_unknown:USTEC/D1")),
    ({TRADE: []}, ("missing_intervals_unknown:USTEC/D1",)),
    ({SIGNAL: []}, ("missing_intervals_unknown:EURUSD/H1",)),
    ({TRADE: [], SIGNAL: None}, ("missing_intervals_unknown:USTEC/D1",)),
    ({TRADE: None, SIGNAL: []}, ("missing_intervals_unknown:EURUSD/H1",)),
])
def test_unknown_missing_evidence_never_releases_partial_intervals(missing, expected):
    api = _api()
    report = api.validate_h_main_scope(coverage=_coverage(api), missing_intervals=missing)
    assert report.status == "waiting_data"
    assert report.execution_status == "not_released"
    assert report.trade_intervals == report.signal_intervals == ()
    assert report.reasons == expected


def test_explicit_empty_missing_and_partial_coverage_are_metadata_only():
    api = _api()
    coverage = [
        api.CoverageSegment(*TRADE, "histdata", "2010-01-01", "2010-01-02"),
        api.CoverageSegment(*SIGNAL, "mt5", "2011-01-01", "2011-01-02"),
    ]
    report = api.validate_h_main_scope(coverage=tuple(coverage), missing_intervals={TRADE: (), SIGNAL: ()})
    assert report.status == "metadata_valid"
    assert len(report.trade_intervals) == len(report.signal_intervals) == 1
    assert report.execution_status == "not_released"
    for field in ("candidate_pass", "power", "years", "N", "samples", "cost", "returns", "approved_by"):
        assert not hasattr(report, field)


@pytest.mark.parametrize("mode", ["empty", "mt5_only", "fully_missing", "no_signal"])
def test_absent_usable_metadata_waits_and_withholds_both_interval_sets(mode):
    api = _api()
    coverage = _coverage(api)
    missing = _known_empty()
    if mode == "empty":
        coverage = []
    elif mode == "mt5_only":
        coverage[0] = api.CoverageSegment(*TRADE, "mt5", "2007-01-01", "2021-05-17")
    elif mode == "fully_missing":
        missing[TRADE] = [api.TimeInterval("2000-01-01", "2030-01-01")]
    else:
        coverage.pop()
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=missing)
    assert report.status == "waiting_data"
    assert report.trade_intervals == report.signal_intervals == ()
    assert ("signal_main_overlap_absent" if mode == "no_signal" else "trade_coverage_empty") in report.reasons
    assert report.reasons == tuple(sorted(set(report.reasons)))


def test_signal_retains_previous_month_and_later_metadata_with_its_own_missing():
    api = _api()
    missing = {
        TRADE: [api.TimeInterval("2007-01-10", "2007-01-20")],
        SIGNAL: [api.TimeInterval("2006-12-10", "2006-12-12")],
    }
    report = api.validate_h_main_scope(coverage=_coverage(api), missing_intervals=missing)
    assert report.status == "metadata_valid"
    assert _bounds(report.signal_intervals) == (
        ("2006-12-01T00:00:00+00:00", "2006-12-10T00:00:00+00:00"),
        ("2006-12-12T00:00:00+00:00", "2021-06-01T00:00:00+00:00"),
    )
    assert _bounds(report.trade_intervals) == (
        ("2007-01-01T00:00:00+00:00", "2007-01-10T00:00:00+00:00"),
        ("2007-01-20T00:00:00+00:00", "2021-05-17T00:00:00+00:00"),
    )
    assert tuple(s.symbol for s, _ in report.excluded) == ("EURUSD", "USTEC")


@pytest.mark.parametrize("start,end,remove_main", [
    ("2006-12-01", "2007-01-01", False),
    ("2021-05-17", "2021-06-01", False),
    ("2006-12-01", "2021-06-01", True),
])
def test_signal_requires_strict_main_overlap_after_its_missing(start, end, remove_main):
    api = _api()
    coverage = _coverage(api)
    coverage[1] = api.CoverageSegment(*SIGNAL, "fred", start, end)
    missing = _known_empty()
    if remove_main:
        missing[SIGNAL] = [api.TimeInterval("2007-01-01", "2021-05-17")]
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=missing)
    assert report.status == "waiting_data"
    assert report.trade_intervals == report.signal_intervals == ()
    assert "signal_main_overlap_absent" in report.reasons


@pytest.mark.parametrize("start,end", [
    ("2006-12-10", "2006-12-20"),
    ("2010-01-01", "2011-01-01"),
    ("2021-05-20", "2021-05-21"),
])
def test_cross_source_signal_overlap_is_rejected_even_outside_main(start, end):
    api = _api()
    coverage = _coverage(api) + [api.CoverageSegment(*SIGNAL, "mt5", start, end)]
    with pytest.raises(ValueError, match="overlap"):
        api.validate_h_main_scope(coverage=coverage, missing_intervals=_known_empty())


def test_cross_source_signal_adjacency_preserves_source_identity():
    api = _api()
    coverage = [
        _coverage(api)[0],
        api.CoverageSegment(*SIGNAL, "mt5", "2010-01-01", "2021-06-01"),
        api.CoverageSegment(*SIGNAL, "fred", "2006-12-01", "2010-01-01"),
    ]
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=_known_empty())
    assert report.status == "metadata_valid"
    assert _rows(report.signal_intervals) == (
        ("USTEC", "D1", "fred", "2006-12-01T00:00:00+00:00", "2010-01-01T00:00:00+00:00"),
        ("USTEC", "D1", "mt5", "2010-01-01T00:00:00+00:00", "2021-06-01T00:00:00+00:00"),
    )


def test_signal_overlap_removed_by_explicit_missing_is_not_a_retained_conflict():
    api = _api()
    coverage = [
        _coverage(api)[0],
        api.CoverageSegment(*SIGNAL, "fred", "2006-12-01", "2011-01-01"),
        api.CoverageSegment(*SIGNAL, "mt5", "2010-01-01", "2021-06-01"),
    ]
    missing = {TRADE: [], SIGNAL: [api.TimeInterval("2010-01-01", "2011-01-01")]}
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=missing)
    assert report.status == "metadata_valid"
    assert _bounds(report.signal_intervals) == (
        ("2006-12-01T00:00:00+00:00", "2010-01-01T00:00:00+00:00"),
        ("2011-01-01T00:00:00+00:00", "2021-06-01T00:00:00+00:00"),
    )


def test_reordering_duplicates_and_same_source_overlaps_do_not_change_report():
    api = _api()
    coverage = _coverage(api) + [
        api.CoverageSegment(*TRADE, "histdata", "2008-01-01", "2010-01-01"),
        api.CoverageSegment(*TRADE, "histdata", "2021-05-17", "2022-01-01"),
        api.CoverageSegment(*TRADE, "histdata", "2021-06-01", "2022-02-01"),
        api.CoverageSegment(*TRADE, "mt5", "2020-01-01", "2022-01-01"),
        api.CoverageSegment(*SIGNAL, "fred", "2021-06-01", "2022-01-01"),
        api.CoverageSegment(*SIGNAL, "fred", "2007-01-01", "2010-01-01"),
    ]
    missing = {
        TRADE: [api.TimeInterval("2010-01-01", "2011-01-01"), api.TimeInterval("2009-01-01", "2010-01-01")],
        SIGNAL: [api.TimeInterval("2012-01-01", "2013-01-01")],
    }
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=missing)
    reordered = api.validate_h_main_scope(
        coverage=list(reversed(coverage)) + coverage,
        missing_intervals={key: list(reversed(value)) + value for key, value in reversed(list(missing.items()))},
    )
    assert reordered == report
    assert _bounds(report.trade_intervals) == (
        ("2007-01-01T00:00:00+00:00", "2009-01-01T00:00:00+00:00"),
        ("2011-01-01T00:00:00+00:00", "2021-05-17T00:00:00+00:00"),
    )
    assert _bounds(report.signal_intervals) == (
        ("2006-12-01T00:00:00+00:00", "2012-01-01T00:00:00+00:00"),
        ("2013-01-01T00:00:00+00:00", "2022-01-01T00:00:00+00:00"),
    )
    assert report.reasons == ("declared_missing", "trade_mt5_diagnostic", "trade_outside_main")


@pytest.mark.parametrize("pair,source,error", [
    (TRADE, None, TypeError), (TRADE, 1, TypeError), (TRADE, [], TypeError),
    (TRADE, "", ValueError), (TRADE, "HistData", ValueError), (TRADE, "histdata ", ValueError),
    (TRADE, "unknown", ValueError), (TRADE, "fred", ValueError),
    (SIGNAL, "histdata", ValueError), (("GBPUSD", "H1"), "histdata", ValueError),
    (("EURUSD", "D1"), "histdata", ValueError), (("USTEC", "H1"), "mt5", ValueError),
    (("eurusd", "H1"), "histdata", ValueError), (("EURUSD", "h1"), "histdata", ValueError),
    ((None, "H1"), "histdata", TypeError), (("EURUSD", 1), "histdata", TypeError),
])
def test_invalid_source_and_pair_metadata_is_rejected_even_when_missing_unknown(pair, source, error):
    api = _api()
    with pytest.raises(error):
        coverage = _coverage(api) + [api.CoverageSegment(*pair, source, "2007-01-01", "2008-01-01")]
        api.validate_h_main_scope(coverage=coverage, missing_intervals=None)


@pytest.mark.parametrize("value", [None, {}, "coverage.csv", 7, iter(())])
def test_coverage_requires_a_sequence_of_records(value):
    api = _api()
    with pytest.raises(TypeError):
        api.validate_h_main_scope(coverage=value, missing_intervals=_known_empty())


@pytest.mark.parametrize("value", [{}, ("EURUSD", "H1", "histdata", "2007-01-01", "2008-01-01"), None])
def test_coverage_rejects_implicit_record_coercion(value):
    api = _api()
    with pytest.raises(TypeError):
        api.validate_h_main_scope(coverage=[value], missing_intervals=_known_empty())


@pytest.mark.parametrize("value", [[], "gaps.json", 7])
def test_missing_requires_a_mapping_or_explicit_unknown(value):
    api = _api()
    with pytest.raises(TypeError):
        api.validate_h_main_scope(coverage=_coverage(api), missing_intervals=value)


@pytest.mark.parametrize("key,error", [
    (("GBPUSD", "H1"), ValueError), (("USTEC", "H1"), ValueError),
    ("EURUSD/H1", TypeError), (("EURUSD",), TypeError), (("EURUSD", 1), TypeError),
])
def test_missing_rejects_extra_or_malformed_pair_keys(key, error):
    api = _api()
    with pytest.raises(error):
        api.validate_h_main_scope(coverage=_coverage(api), missing_intervals={**_known_empty(), key: []})


@pytest.mark.parametrize("value", ["", {}, 5, iter(()), [None], [("2007-01-01", "2008-01-01")]])
def test_missing_values_require_interval_sequences_without_guessing(value):
    api = _api()
    with pytest.raises(TypeError):
        api.validate_h_main_scope(coverage=_coverage(api), missing_intervals={TRADE: value, SIGNAL: []})


@pytest.mark.parametrize("value,error", [
    (None, TypeError), (True, TypeError), (1234567890, TypeError), (float("nan"), TypeError),
    (date(2007, 1, 1), TypeError), (b"2007-01-01", TypeError),
    ("", ValueError), ("yesterday", ValueError), ("01/02/2007", ValueError),
    ("20070101", ValueError), ("2007-02-29", ValueError),
    ("2007-01-01T24:00:00Z", ValueError), ("2007-01-01T00:00:60Z", ValueError),
    ("2007-01-01x00:00:00", ValueError), ("2007-01-01T00:00:00+01:60", ValueError),
    ("2007-01-01T00:00:00.0000001Z", ValueError),
    (" 2007-01-01", ValueError), ("0001-01-01T00:00:00+01:00", ValueError),
])
@pytest.mark.parametrize("record", ["interval", "coverage"])
def test_strict_time_inputs_reject_invalid_or_ambiguous_values(value, error, record):
    api = _api()
    with pytest.raises(error):
        if record == "interval":
            api.TimeInterval(value, "2021-05-17")
        else:
            api.CoverageSegment(*TRADE, "histdata", value, "2021-05-17")


@pytest.mark.parametrize("start,end", [
    ("2007-01-01", "2007-01-01"),
    ("2008-01-01", "2007-01-01"),
    ("2007-01-01T01:00:00+01:00", "2007-01-01T00:00:00Z"),
    ("2007-01-01", "invalid-end"),
])
@pytest.mark.parametrize("record", ["interval", "coverage"])
def test_interval_endpoints_must_define_positive_valid_utc_duration(start, end, record):
    api = _api()
    with pytest.raises(ValueError):
        if record == "interval":
            api.TimeInterval(start, end)
        else:
            api.CoverageSegment(*TRADE, "histdata", start, end)


def test_iso_offsets_and_naive_datetimes_canonicalize_to_utc_before_union():
    api = _api()
    coverage = [
        api.CoverageSegment(*TRADE, "histdata", datetime(2007, 1, 1), "2010-01-01T01:00:00+01:00"),
        api.CoverageSegment(*TRADE, "histdata", "2009-12-31T19:00:00-05:00", datetime(2021, 5, 17, 2, tzinfo=timezone(timedelta(hours=2)))),
        _coverage(api)[1],
    ]
    missing = {TRADE: [api.TimeInterval("2011-01-01T01:00:00+01:00", datetime(2011, 1, 2))], SIGNAL: []}
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=missing)
    assert _bounds(report.trade_intervals) == (
        ("2007-01-01T00:00:00+00:00", "2011-01-01T00:00:00+00:00"),
        ("2011-01-02T00:00:00+00:00", "2021-05-17T00:00:00+00:00"),
    )
    for segment in (*coverage, *report.trade_intervals, *report.signal_intervals, *missing[TRADE]):
        assert segment.start.tzinfo is segment.end.tzinfo is timezone.utc


def test_inputs_are_deeply_unchanged_and_returned_records_are_immutable():
    api = _api()
    coverage = _coverage(api)
    missing = {TRADE: [api.TimeInterval("2010-01-01", "2011-01-01")], SIGNAL: []}
    before = copy.deepcopy((coverage, missing))
    report = api.validate_h_main_scope(coverage=coverage, missing_intervals=missing)
    assert (coverage, missing) == before
    saved_hash = hash(report)
    coverage.clear()
    missing[TRADE].clear()
    missing[SIGNAL].append(api.TimeInterval("2007-01-01", "2021-05-17"))
    assert len(report.trade_intervals) == 2
    assert hash(report) == saved_hash
    for obj, attribute, value in (
        (report, "status", "approved"), (report, "execution_status", "released"),
        (report.trade_intervals[0], "source", "mt5"), (report.signal_intervals[0], "start", None),
        (report.excluded[0][0], "end", None), (before[1][TRADE][0], "start", None),
    ):
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            setattr(obj, attribute, value)
    assert all(isinstance(value, tuple) for value in (
        report.trade_intervals, report.signal_intervals, report.excluded, report.excluded[0], report.reasons,
    ))
    with pytest.raises(TypeError):
        report.excluded[0][1] = "approved"


@pytest.mark.parametrize("extra", [
    {"start": "2010-01-01"}, {"symbol": "GBPUSD"}, {"timezone": "Europe/London"},
    {"max_gap_days": 30}, {"data_root": "."}, {"bars": []}, {"returns": []}, {"ledger": None},
])
def test_entrypoint_cannot_change_scope_or_accept_execution_inputs(extra):
    api = _api()
    with pytest.raises(TypeError):
        api.validate_h_main_scope(coverage=_coverage(api), missing_intervals=_known_empty(), **extra)
    with pytest.raises(TypeError):
        api.validate_h_main_scope(_coverage(api), _known_empty())


def test_cold_import_loads_no_research_engine_data_or_third_party_modules():
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = textwrap.dedent("""
        import importlib.abc
        import sys
        sys.path.insert(0, sys.argv[1])
        blocked = ('mt5data', 'mt5gw', 'numpy', 'pandas', 'sqlite3')
        attempts = []
        class Guard(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if (fullname.split('.')[0] in blocked or
                    (fullname.startswith('mt5quant.') and fullname != 'mt5quant.h_main_scope')):
                    attempts.append(fullname)
                    raise AssertionError('forbidden dependency: ' + fullname)
        sys.meta_path.insert(0, Guard())
        import mt5quant.h_main_scope
        assert not attempts, attempts
        assert not any(name.split('.')[0] in blocked for name in sys.modules)
        assert {name for name in sys.modules if name.startswith('mt5quant.')} == {'mt5quant.h_main_scope'}
    """)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(source_root)],
        cwd=source_root.parent, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_validation_performs_no_file_io(monkeypatch):
    api = _api()

    def forbidden(*args, **kwargs):
        raise AssertionError("metadata validation must not access files")

    with monkeypatch.context() as patch:
        for owner, names in ((builtins, ("open",)), (io, ("open",)), (os, ("open", "stat", "listdir", "scandir"))):
            for name in names:
                patch.setattr(owner, name, forbidden)
        report = api.validate_h_main_scope(coverage=_coverage(api), missing_intervals=_known_empty())
        waiting = api.validate_h_main_scope(coverage=_coverage(api), missing_intervals=None)
    assert report.status == "metadata_valid"
    assert waiting.status == "waiting_data"
