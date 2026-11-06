"""H real-input assembler + one-shot main-segment precheck runner (provisional).

Binds exactly seven research-store artifacts by relative path + SHA256 (any
mismatch fails closed; unbound paths are never read), gates every touched
natural year through the bound manifests' yearly ``used`` declarations, only
then decodes parquet price columns (time+close projection, single read of the
verified bytes), filters prices to the fixed windows BEFORE any computation,
derives missing intervals by the pre-registered grid rules below, and runs
``run_h_readonly_precheck`` in the real eligibility mode. MT5 series never
enter the inputs. Output is a new exclusive directory; every result stays
research not_evaluated / execution not_released and is provisional until the
focused independent review; it must not be handed to A1 before that.

The frozen ``REAL_WHITELIST`` is the only binding the CLI accepts; the
``verify_hash`` parameter exists solely so tests can bind schema-isomorphic
synthetic copies and is exposed by no CLI flag.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd
import pyarrow.parquet as pq

from mt5quant.costs import (
    SOURCE_COST_POLICY, CommissionModel, Costs, SlippageModel, SpreadModel,
    SpreadStat, SwapModel, SwapRule,
)
from mt5quant.costs.spread import BUCKET_TZ, MODES as SPREAD_MODES, SAMPLINGS as SPREAD_SAMPLINGS
from mt5quant.h_main_scope import CoverageSegment, TimeInterval, _union
from mt5quant.h_readonly_precheck import (
    MAIN_END, MAIN_START, REAL_ELIGIBILITY, RULES, WARMUP_START,
    run_h_readonly_precheck,
)
from mt5quant.research import candidates as cand
from mt5quant.research.candidates import validate_candidate

TRADE = "EURUSD/H1"
SIGNAL = "USTEC/D1"
WINDOWS = {TRADE: (MAIN_START, MAIN_END), SIGNAL: (WARMUP_START, MAIN_END)}

REAL_WHITELIST = {
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
REQUIRED_PATHS = tuple(sorted(REAL_WHITELIST))

GAP_RULES = {
    "EURUSD/H1": {
        "monday_00_utc_to_friday_close": (
            "per UTC week: expected grid = every hour from Monday 00:00 to the "
            "week's last existing Mon-Fri bar open + 1h (Friday close); maximal "
            "runs of grid points without a bar are recorded as "
            "[first missing bar open, next existing bar open)"),
        "grid": "1h",
        "thin_or_absent_intra_week_hours": "conservatively recorded missing",
    },
    "USTEC/D1": {
        "weekday_grid": (
            "expected grid = every weekday (Mon-Fri) date from the first to the "
            "last delivered bar; maximal runs of missing weekdays are recorded "
            "as [first missing day 00:00, next existing bar 00:00)"),
        "grid": "1D",
    },
    "weekends": "constructively_excluded",
    "holidays": "conservatively_recorded_missing (no holiday calendar artifact is consumed)",
    "empty_weeks": (
        "a week with no Mon-Fri bars defines no band and derives nothing; such "
        "spans are covered by explicit registration below and by the frozen "
        ">30-day span split inside the precheck"),
    "interval_form": "missing_half_open",
    "explicit_registration": (
        "manifest salvage-excluded calendar months (used year with salvage=true "
        "whose monthly entry is not in salvaged_months) are registered as "
        "[month start, next month start) and their bars are cut automatically"),
}

REAL_RULES = {
    "version": "h-real-precheck-v1",
    "basis": "T114 main-segment real precheck; provisional until focused independent review",
    "main_start": MAIN_START.isoformat(),
    "main_end_exclusive": MAIN_END.isoformat(),
    "signal_warmup_days": 40,
    "windows": {key: [start.isoformat(), end.isoformat()]
                for key, (start, end) in WINDOWS.items()},
    "eligibility_required": REAL_ELIGIBILITY,
    "binding": "path+sha256 dual binding of seven artifacts; fail-closed; unbound paths never read",
    "cost_policy": SOURCE_COST_POLICY,
    "cost_multiple_min": cand._COST_MULTIPLE_MIN,
    "power_min": cand._POWER_MIN,
    "alpha": cand._POWER_ALPHA,
    "single_n": cand._POWER_N_TESTS,
    "cumulative_n": None,
    "missing_kind": "missing_half_open",
    "gap_rules": GAP_RULES,
    "sigma": RULES["sigma"],
    "calendar": "frozen ext.merge Sunday/DST monthly self-check inside the precheck",
    "mt5_series": "never included",
}


class BindingError(ValueError):
    """Fail-closed refusal for any binding/qualification violation."""


def _read_bound(data_root: Path, rel: str, expected_sha: str) -> bytes:
    path = Path(data_root) / rel
    root_resolved = Path(data_root).resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise BindingError(f"bound path escapes data root: {rel}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BindingError(f"bound file unreadable: {rel}") from exc
    got = hashlib.sha256(raw).hexdigest()
    if got != expected_sha:
        raise BindingError(f"sha256 mismatch for bound file {rel}: "
                           f"expected {expected_sha}, got {got}")
    return raw


def _parse_json(raw: bytes, rel: str) -> dict:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise BindingError(f"duplicate key in {rel}: {key}")
            out[key] = value
        return out

    def constant(value):
        raise BindingError(f"nonfinite JSON number in {rel}: {value}")

    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                         parse_constant=constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BindingError(f"bound JSON unreadable: {rel}") from exc
    if not isinstance(doc, dict):
        raise BindingError(f"bound JSON must be an object: {rel}")
    return doc


def derive_hour_band_gaps(times) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Pre-registered EURUSD/H1 weekly hour-grid holes; weekends excluded."""
    if len(times) == 0:
        return []
    index = pd.DatetimeIndex(pd.unique(pd.DatetimeIndex(times))).sort_values()
    weekdays = index[index.weekday < 5]
    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    if len(weekdays) == 0:
        return gaps
    mondays = weekdays.normalize() - pd.to_timedelta(weekdays.weekday, unit="D")
    for monday in sorted(set(mondays)):
        week = weekdays[mondays == monday]
        have = set(week)
        band_end = week[-1] + pd.Timedelta(hours=1)
        grid = pd.date_range(monday, band_end, freq="h", inclusive="left")
        run: list[pd.Timestamp] = []
        for point in grid:
            if point in have:
                if run:
                    gaps.append((run[0], point))
                    run = []
            else:
                run.append(point)
        if run:  # Unreachable by construction (band_end follows the last bar).
            gaps.append((run[0], run[-1] + pd.Timedelta(hours=1)))
    return gaps


def derive_weekday_gaps(times) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Pre-registered USTEC/D1 weekday-grid holes; weekends excluded."""
    if len(times) == 0:
        return []
    days = pd.DatetimeIndex(pd.unique(pd.DatetimeIndex(times))).sort_values().normalize()
    if len(days) == 0:
        return []
    grid = pd.date_range(days[0], days[-1] + pd.Timedelta(days=1), freq="D")
    grid = grid[grid.weekday < 5]
    have = set(days)
    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    run: list[pd.Timestamp] = []
    for point in grid:
        if point in have:
            if run:
                gaps.append((run[0], point))
                run = []
        else:
            run.append(point)
    if run:  # Unreachable by construction (grid ends after the last bar).
        gaps.append((run[0], run[-1] + pd.Timedelta(days=1)))
    return gaps


def _touched_years(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    return [str(year) for year in range(start.year, end.year + 1)]


def _used_gate(manifest: dict, key: str, years: list[str]) -> dict:
    yearly = manifest.get("yearly_verification")
    if not isinstance(yearly, dict) or not yearly:
        return {"touched_years": years, "basis": "no_yearly_verification_declared",
                "years": {}}
    detail = {}
    for year in years:
        entry = yearly.get(year)
        if not isinstance(entry, dict) or entry.get("used") is not True:
            raise BindingError(f"used gate: {key} touched year {year} is not used=true "
                               "in the bound manifest")
        detail[year] = {"used": True, "calendar": entry.get("calendar")}
    return {"touched_years": years, "basis": "yearly_verification", "years": detail}


def _explicit_month_holes(manifest: dict, window) -> tuple[list[TimeInterval], dict]:
    """Salvage-excluded months of used years -> registered calendar-month holes."""
    holes: list[TimeInterval] = []
    handling: dict = {}
    yearly = manifest.get("yearly_verification") or {}
    for year in sorted(yearly):
        entry = yearly[year]
        if not isinstance(entry, dict) or entry.get("used") is not True:
            continue
        monthly = entry.get("monthly")
        if entry.get("salvage") is not True or not isinstance(monthly, dict):
            continue
        salvaged = entry.get("salvaged_months") or {}
        excluded = sorted(month for month in monthly if month not in salvaged)
        anchor = (entry.get("fix_anchor_month_end") or {}).get("status")
        intervals = []
        for month in excluded:
            start = pd.Timestamp(f"{month}-01T00:00:00")
            end = start + pd.DateOffset(months=1)
            if start < window[1] and end > window[0]:
                holes.append(TimeInterval(start, end))
                intervals.append([start.isoformat(), end.isoformat()])
        handling[year] = {
            "included": True, "calendar": entry.get("calendar"), "salvage": True,
            "excluded_months": excluded, "anchor": anchor,
            "hole": intervals[0] if len(intervals) == 1 else None,
            "holes": intervals,
        }
    return holes, handling


def _read_prices(raw: bytes, rel: str, window) -> pd.DataFrame:
    """Decode time+close only, from the already verified bytes; filter first."""
    try:
        with pq.ParquetFile(io.BytesIO(raw)) as handle:
            table = handle.read(columns=["time", "close"], use_threads=False)
    except Exception as exc:
        raise BindingError(f"bound parquet unreadable: {rel}") from exc
    frame = table.to_pandas()
    if not {"time", "close"} <= set(frame.columns):
        raise BindingError(f"bound parquet lacks time/close: {rel}")
    times = pd.to_datetime(frame["time"], utc=True).dt.tz_localize(None)
    keep = (times >= window[0]) & (times < window[1])
    return pd.DataFrame({"time": times[keep].reset_index(drop=True),
                         "close": pd.to_numeric(frame["close"], errors="raise")
                         .astype("float64")[keep].reset_index(drop=True)})


def _naive(value) -> pd.Timestamp:
    """Any library-normalized UTC datetime/str as a naive UTC Timestamp."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def _cut_holes(frame: pd.DataFrame, holes: list[TimeInterval]) -> pd.DataFrame:
    if not holes:
        return frame
    keep = pd.Series(True, index=frame.index)
    for hole in holes:
        keep &= ~((frame.time >= _naive(hole.start)) & (frame.time < _naive(hole.end)))
    return frame.loc[keep].reset_index(drop=True)


def _histdata_coverage(years: list[str]) -> list[CoverageSegment]:
    segments = []
    for year in sorted({int(year) for year in years}):
        start = max(pd.Timestamp(year, 1, 1), WINDOWS[TRADE][0])
        end = min(pd.Timestamp(year + 1, 1, 1), WINDOWS[TRADE][1])
        if start < end:
            segments.append(CoverageSegment("EURUSD", "H1", "histdata", start, end))
    return segments


def _fred_coverage(manifest: dict) -> list[CoverageSegment]:
    first = pd.Timestamp(manifest["first_time"])
    last = pd.Timestamp(manifest["last_time"])
    start = max(WINDOWS[SIGNAL][0], first)
    end = min(WINDOWS[SIGNAL][1], last + pd.Timedelta(days=1))
    if start >= end:
        raise BindingError("fred declared span does not overlap the signal window")
    return [CoverageSegment("USTEC", "D1", "fred", start, end)]


def _number(value, label, *, positive=False, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BindingError(f"{label} must be numeric in bound artifacts")
    value = float(value)
    if not math.isfinite(value) or (not signed and value < 0) or (positive and value <= 0):
        raise BindingError(f"{label} must be finite and valid in bound artifacts")
    return value


def _assemble_costs(calibrated: dict, spread_doc: dict, specs_doc: dict) -> tuple[Costs, dict]:
    try:
        record = calibrated["symbols"]["EURUSD"]
        commission = record["commission"]
        snapshot = record["swap"]["snapshot"]
        model = spread_doc["model"]
        hours = model["per_symbol"]["EURUSD"]
        overall = model["overall_median"]["EURUSD"]
        contract = specs_doc["EURUSD"]["contract_size"]
    except (KeyError, TypeError) as exc:
        raise BindingError(f"bound cost/spec artifact lacks required fields: {exc}") from exc
    round_trip = _number(commission["round_trip_per_lot"], "commission round_trip_per_lot")
    if snapshot.get("available") is not True:
        raise BindingError("EURUSD swap snapshot unavailable in calibrated.json")
    long_rate = _number(snapshot["long_per_lot_per_night"], "swap long rate", signed=True)
    short_rate = _number(snapshot["short_per_lot_per_night"], "swap short rate", signed=True)
    swap_kwargs = {"long_per_day": long_rate, "short_per_day": short_rate}
    triple = snapshot.get("triple_weekday_py")
    if triple is not None:
        swap_kwargs["triple_weekdays"] = frozenset({int(triple)})
    if model["mode"] not in SPREAD_MODES or model["sampling"] not in SPREAD_SAMPLINGS:
        raise BindingError("spread model mode/sampling unsupported")
    if model["bucket_tz"] != BUCKET_TZ:
        raise BindingError("spread bucket_tz must be America/New_York")
    stats = {}
    for hour, stat in hours.items():
        key = int(hour)
        if not 0 <= key <= 23:
            raise BindingError("spread hour must be canonical 0..23")
        stats[key] = SpreadStat(
            _number(stat["median"], "spread median"), _number(stat["p90"], "spread p90"),
            _number(stat["p99"], "spread p99"), int(stat["n"]), int(stat["n_days"]))
    if set(stats) != set(range(24)):
        raise BindingError("spread model must cover all 24 New York hours for EURUSD")
    spread = SpreadModel({"EURUSD": stats}, {"EURUSD": _number(overall, "overall_median")},
                         mode=model["mode"], sampling=model["sampling"])
    slippage = SlippageModel(spread, spread_fraction=0.1, fixed_price=0.0,
                             event_hours=frozenset(), event_multiplier=2.0)
    contract_size = _number(contract, "contract_size", positive=True)
    costs = Costs(spread, slippage,
                  CommissionModel(default_per_lot=round_trip, per_symbol={"EURUSD": round_trip}),
                  SwapModel({"EURUSD": SwapRule(**swap_kwargs)}))
    provenance = {
        "commission": {
            "basis": "deal_measured", "round_trip_per_lot": round_trip,
            "evidence": {"deals_in": int(commission["deals_in"]),
                         "deals_out": int(commission["deals_out"]),
                         "volume": float(commission["volume_in"])},
            "source_file": "costs/calibrated.json",
        },
        "swap": {
            "basis": "spec_snapshot_not_deal_measured",
            "note": "规格快照、非成交实测（calibrated.json swap.snapshot；实测成交隔夜为空）",
            "long_per_day": long_rate, "short_per_day": short_rate,
            "triple_weekday_py": None if triple is None else int(triple),
            "domain": "absolute_usd_per_lot_per_night",
            "source_file": "costs/calibrated.json",
        },
        "spread": {
            "source_file": "costs/spread_model.json",
            "model_sha256": spread_doc.get("model_sha256"),
            "mode": model["mode"], "sampling": model["sampling"], "bucket_tz": model["bucket_tz"],
            "calibration_watermarks": spread_doc.get("watermarks"),
            "built_at_utc": spread_doc.get("built_at_utc"),
            "scenario": True,
            "scenario_note": ("2026 tick 基准施于 2007-2021 属情景外推（relative 模式随价格缩放）；"
                              "入场/出场小时按候选声明计费，见 result.checks.ex_ante_cost"),
            "stats_used": {"p90_by_ny_hour": {str(h): stats[h].p90 for h in range(24)}},
            "source_policy": SOURCE_COST_POLICY,
        },
        "slippage": {
            "spread_fraction": 0.1, "fixed_price": 0.0, "event_hours": [],
            "event_multiplier": 2.0,
            "basis": "项目口径 滑点 0.1×p90（docs/reports/scope-2026-10-25.md；事件小时集为空）",
        },
        "contract": {"source_file": "specs/latest.json", "contract_size": contract_size},
    }
    return costs, provenance


def assemble_real_inputs(data_root: Path, *, verify_hash=REAL_WHITELIST) -> dict:
    """Qualify, assemble and gate the bound real inputs; never writes anywhere."""
    data_root = Path(data_root)
    if not isinstance(verify_hash, Mapping):
        raise TypeError("verify_hash must be a mapping of relative path to sha256")
    if set(verify_hash) != set(REAL_WHITELIST):
        raise BindingError("whitelist must bind exactly the required paths: "
                           f"{list(REQUIRED_PATHS)}")
    bound = {rel: _read_bound(data_root, rel, verify_hash[rel]) for rel in REQUIRED_PATHS}
    hist_manifest = _parse_json(bound["ext/histdata/EURUSD/H1/manifest.json"],
                                "ext/histdata/EURUSD/H1/manifest.json")
    fred_manifest = _parse_json(bound["ext/fred/USTEC/D1/manifest.json"],
                                "ext/fred/USTEC/D1/manifest.json")
    gate = {key: _used_gate(manifest, key, _touched_years(*window))
            for key, (manifest, window) in ((TRADE, (hist_manifest, WINDOWS[TRADE])),
                                            (SIGNAL, (fred_manifest, WINDOWS[SIGNAL])))}
    explicit, year_handling = _explicit_month_holes(hist_manifest, WINDOWS[TRADE])
    # Prices are decoded only after the whole qualification passed above.
    trade = _cut_holes(_read_prices(bound["ext/histdata/EURUSD/H1/data.parquet"],
                                    "ext/histdata/EURUSD/H1/data.parquet", WINDOWS[TRADE]),
                       explicit)
    signal = _read_prices(bound["ext/fred/USTEC/D1/data.parquet"],
                          "ext/fred/USTEC/D1/data.parquet", WINDOWS[SIGNAL])
    derived_trade = derive_hour_band_gaps(trade.time)
    derived_signal = derive_weekday_gaps(signal.time)
    missing_trade = list(explicit) + [TimeInterval(a, b) for a, b in derived_trade]
    missing_signal = [TimeInterval(a, b) for a, b in derived_signal]
    coverage_trade = _histdata_coverage(gate[TRADE]["touched_years"])
    coverage_signal = _fred_coverage(fred_manifest)
    inputs = {
        TRADE: {"bars": trade, "source_tags": ["histdata"] * len(trade),
                "coverage": coverage_trade, "missing_kind": "missing_half_open",
                "missing_intervals": missing_trade,
                "source_eligibility": {"histdata": REAL_ELIGIBILITY}},
        SIGNAL: {"bars": signal, "source_tags": ["fred"] * len(signal),
                 "coverage": coverage_signal, "missing_kind": "missing_half_open",
                 "missing_intervals": missing_signal,
                 "source_eligibility": {"fred": REAL_ELIGIBILITY}},
    }
    cost_model, costs_provenance = _assemble_costs(
        _parse_json(bound["costs/calibrated.json"], "costs/calibrated.json"),
        _parse_json(bound["costs/spread_model.json"], "costs/spread_model.json"),
        _parse_json(bound["specs/latest.json"], "specs/latest.json"))
    provenance = {
        "status": "provisional",
        "note": ("聚焦独立评审通过前不交 A1；本产物仅为工程诊断，非研究结论、非执行许可"),
        "research_status": "not_evaluated",
        "execution_status": "not_released",
        "data_root": str(data_root),
        "binding": {
            "policy": "path+sha256 dual binding; any mismatch fails closed; unbound paths never read",
            "files": {rel: hashlib.sha256(bound[rel]).hexdigest() for rel in REQUIRED_PATHS},
        },
        "eligibility": {key: dict(rec["source_eligibility"]) for key, rec in inputs.items()},
        "windows": {key: [start.isoformat(), end.isoformat()]
                    for key, (start, end) in WINDOWS.items()},
        "used_gate": gate,
        "year_handling": year_handling,
        "gap_rules": GAP_RULES,
        "missing_intervals": {
            TRADE: [[_naive(h.start).isoformat(), _naive(h.end).isoformat()]
                    for h in _union(missing_trade)],
            SIGNAL: [[_naive(h.start).isoformat(), _naive(h.end).isoformat()]
                     for h in _union(missing_signal)],
        },
        "missing_interval_counts": {TRADE: {"explicit": len(explicit),
                                            "derived_hour_grid": len(derived_trade)},
                                    SIGNAL: {"derived_weekday_grid": len(derived_signal)}},
        "coverage_declared": {
            TRADE: [{"source": s.source, "start": _naive(s.start).isoformat(),
                     "end": _naive(s.end).isoformat()} for s in coverage_trade],
            SIGNAL: [{"source": s.source, "start": _naive(s.start).isoformat(),
                      "end": _naive(s.end).isoformat()} for s in coverage_signal],
        },
        "mt5_series": "never included",
        "costs": costs_provenance,
    }
    return {"inputs": inputs, "cost_model": cost_model,
            "contract_sizes": {"EURUSD": costs_provenance["contract"]["contract_size"]},
            "provenance": provenance}


def _code_hashes():
    root = Path(__file__).resolve().parents[1]
    files = ["scripts/h_assemble_real_precheck.py", "scripts/h_readonly_precheck.py",
             "src/mt5quant/h_readonly_precheck.py", "src/mt5quant/h_main_scope.py",
             "src/mt5quant/research/candidates.py", "src/mt5quant/research/power.py",
             "src/mt5quant/validation/events.py", "src/mt5data/ext/merge.py",
             "src/mt5quant/costs/__init__.py", "src/mt5quant/costs/spread.py",
             "src/mt5quant/costs/slippage.py", "src/mt5quant/costs/commission.py",
             "src/mt5quant/costs/swap.py"]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path,
                        help="read-only research data root holding the seven bound artifacts")
    parser.add_argument("--candidate", required=True, type=Path,
                        help="candidate TOML declaration (T114 offline draft)")
    parser.add_argument("--output", required=True, type=Path,
                        help="new exclusive output directory for result.json")
    args = parser.parse_args(argv)
    try:
        assembled = assemble_real_inputs(args.data_root)
        raw = args.candidate.read_bytes()
        candidate = validate_candidate(tomllib.loads(raw.decode("utf-8")),
                                       source="real precheck bytes")
        result = run_h_readonly_precheck(
            candidate, inputs=assembled["inputs"], cost_model=assembled["cost_model"],
            contract_sizes=assembled["contract_sizes"],
            required_eligibility=REAL_ELIGIBILITY)  # cumulative_n stays None by design.
        provenance = dict(assembled["provenance"])
        provenance["candidate"] = {"path": str(args.candidate),
                                   "sha256": hashlib.sha256(raw).hexdigest(),
                                   "id": candidate.id}
        rules = json.dumps(REAL_RULES, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload = {"result": result, "provenance": provenance,
                   "hashes": {"domain": "sha256(raw_artifact_bytes)",
                              "artifacts": dict(provenance["binding"]["files"],
                                                **{"candidate:" + candidate.id:
                                                   provenance["candidate"]["sha256"]}),
                              "rules_domain": "sha256(canonical_rules_json_utf8)",
                              "rules": hashlib.sha256(rules).hexdigest(),
                              "code": _code_hashes()}}
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        args.output.mkdir(exist_ok=False)
        with (args.output / "result.json").open("x", encoding="utf-8") as stream:
            stream.write(text)
        print(text, end="")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"h_assemble_real_precheck: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
