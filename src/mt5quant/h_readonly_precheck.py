"""T114 synthetic-only, read-only precheck over actual, explicitly supplied bars.

``inputs`` has exactly ``EURUSD/H1`` and ``USTEC/D1`` keys. Each value has:
* bars: DataFrame with time (bar OPEN, UTC; naive means UTC), close (>0);
* source_tags: aligned canonical source strings (histdata/mt5 or fred/mt5);
* coverage: CoverageSegment records for that pair, half-open;
* missing_kind: exactly 'missing_half_open'; missing_intervals: TimeInterval
  records ([] is checked-empty; None/absent is unknown and rejected);
* source_eligibility: {canonical_source: 'synthetic_only'} for every declared
  source. These are fixture declarations, never production approvals.

No data root, ledger, event returns, labels, fitting or sampler is used.
The caller supplies a standard Costs model, an explicit EURUSD per_symbol
commission record (zero allowed), absolute per-lot swap rates (no ref_price or
contract_size basis), and an explicit EURUSD contract size.
Coverage uses actual retained bar OPEN spans (runtime_coverage convention).
Volatility uses unconditional close-to-close windows; both close timestamps
must be in the main interval and the path must not cross a declared/removed
hole. USTEC retains 40 days before main start solely as signal warmup.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
import math

import numpy as np
import pandas as pd

from mt5data.ext.merge import _month_sunday_hours_ok, _sunday_open_hours
from mt5quant.costs import CommissionModel, Costs, SOURCE_COST_POLICY, source_costs
from mt5quant.h_main_scope import (
    CoverageSegment, TimeInterval, _utc, validate_h_main_scope,
)
from mt5quant.research import candidates as cand, power

TRADE = "EURUSD/H1"
SIGNAL = "USTEC/D1"
PAIRS = {TRADE: ("EURUSD", "H1"), SIGNAL: ("USTEC", "D1")}
MAIN_START = pd.Timestamp("2007-01-01")
MAIN_END = pd.Timestamp("2021-05-17")
WARMUP_START = MAIN_START - pd.Timedelta(days=40)
RULES = {
    "version": "t114-readonly-synthetic-v1", "main_start": MAIN_START.isoformat(),
    "main_end_exclusive": MAIN_END.isoformat(), "signal_warmup_days": 40,
    "cost_policy": SOURCE_COST_POLICY, "cost_multiple_min": cand._COST_MULTIPLE_MIN,
    "power_min": cand._POWER_MIN, "alpha": cand._POWER_ALPHA,
    "single_n": cand._POWER_N_TESTS, "missing_kind": "missing_half_open",
    "calendar": "ext.merge Sunday/DST monthly check; synthetic provenance only",
    "sigma": "unconditional close windows, ddof=1, NY entry hour, no gap crossing",
}
_METADATA_KEYS = {"coverage", "missing_kind", "missing_intervals", "source_eligibility"}


def _fields(value, required, label, optional=()):
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    missing = set(required) - set(value)
    extra = set(value) - set(required) - set(optional)
    if missing or extra:
        raise ValueError(f"{label}: missing {sorted(missing)}, unknown {sorted(extra)}")


def _number(value, label, *, positive=False, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        raise TypeError(f"{label} must be numeric")
    value = float(value)
    if not math.isfinite(value) or (not signed and value < 0) or (positive and value <= 0):
        raise ValueError(f"{label} must be finite and {'positive' if positive else 'valid'}")
    return value


def _items(value, label):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be an explicit sequence")
    return value


def _time(value):
    if pd.isna(value):
        raise ValueError("bar time is missing")
    # Do not permit Timestamp's sub-microsecond precision to disappear in _utc.
    if isinstance(value, pd.Timestamp) and value.nanosecond:
        raise ValueError("bar time must not have sub-microsecond precision")
    return pd.Timestamp(_utc(value)).tz_localize(None)


def _validate_metadata(inputs):
    """Pure metadata gate, also run by CLI BEFORE opening any bars artifact."""
    _fields(inputs, PAIRS, "inputs")
    coverage, missing = [], {}
    for key, pair in PAIRS.items():
        rec = inputs[key]
        _fields(rec, _METADATA_KEYS, key)
        if rec["missing_kind"] != "missing_half_open":
            raise ValueError(f"{key}: missing_kind must be missing_half_open, not legacy gap endpoints")
        holes = _items(rec["missing_intervals"], f"{key} missing_intervals (unknown rejected)")
        if not all(isinstance(h, TimeInterval) for h in holes):
            raise TypeError("missing_intervals must contain TimeInterval")
        segments = _items(rec["coverage"], f"{key} coverage")
        if not all(isinstance(s, CoverageSegment) and (s.symbol, s.timeframe) == pair for s in segments):
            raise ValueError(f"{key}: coverage pair mismatch")
        sources = {s.source for s in segments}
        eligibility = rec["source_eligibility"]
        if not isinstance(eligibility, Mapping) or set(eligibility) != sources or not sources \
                or any(v != "synthetic_only" for v in eligibility.values()):
            raise PermissionError(f"{key}: source qualification not_released; synthetic_only required")
        coverage.extend(segments)
        missing[pair] = holes
    scope = validate_h_main_scope(coverage=coverage, missing_intervals=missing)
    if scope.status != "metadata_valid":
        raise ValueError("scope waiting_data: " + ", ".join(scope.reasons))
    return scope


def _normalize_inputs(inputs):
    _fields(inputs, PAIRS, "inputs")
    for key in PAIRS:
        _fields(inputs[key], _METADATA_KEYS | {"bars", "source_tags"}, key)
    scope = _validate_metadata({k: {f: inputs[k][f] for f in _METADATA_KEYS} for k in PAIRS})
    frames, tags = {}, {}
    for key in PAIRS:
        rec, pair = inputs[key], PAIRS[key]
        bars = rec["bars"]
        if not isinstance(bars, pd.DataFrame) or not {"time", "close"} <= set(bars.columns) \
                or bars.columns.has_duplicates:
            raise TypeError(f"{key}: bars must have unique time/close columns")
        frame = bars[["time", "close"]].copy(deep=True).reset_index(drop=True)
        frame["time"] = pd.to_datetime([_time(t) for t in frame.time])
        if frame.time.duplicated().any() or not frame.time.is_monotonic_increasing:
            raise ValueError(f"{key}: bars must be unique and ordered")
        frame["close"] = [_number(v, f"{key} close", positive=True) for v in frame.close]
        if pair[1] == "H1" and any(t != t.floor("h") for t in frame.time):
            raise ValueError("H1 bars must use hour-aligned UTC open timestamps")
        sources = list(_items(rec["source_tags"], f"{key} source_tags"))
        if len(sources) != len(frame):
            raise ValueError(f"{key}: source_tags length mismatch")
        for t, source in zip(frame.time, sources):
            if not isinstance(source, str) or source not in rec["source_eligibility"]:
                raise ValueError(f"{key}: unknown source tag")
            if not any(s.source == source and _time(s.start) <= t < _time(s.end)
                       for s in rec["coverage"]):
                raise ValueError(f"{key}: bar outside matching explicit coverage")
            if any(_time(h.start) <= t < _time(h.end) for h in rec["missing_intervals"]):
                raise ValueError(f"{key}: existing bar lies inside actual missing interval")
        frames[key], tags[key] = frame, sources
    return scope, frames, tags


def _selected(frame, tags, segments, start, end):
    keep = [start <= t < end and any(s.source == source and _time(s.start) <= t < _time(s.end)
                                    for s in segments)
            for t, source in zip(frame.time, tags)]
    return frame.loc[keep].reset_index(drop=True), [s for s, k in zip(tags, keep) if k]


def _gap_edges(frame, original, holes, segments):
    """Actual missing intervals -> surviving adjacent bar endpoints, never reverse."""
    removed = set(original.time) - set(frame.time)
    bounds = [(_time(s.start), _time(s.end)) for s in segments]
    union = []
    for a, b in sorted(bounds):
        if union and a <= union[-1][1]:
            union[-1] = (union[-1][0], max(union[-1][1], b))
        else:
            union.append((a, b))
    absent = [(_time(h.start), _time(h.end)) for h in holes]
    out = []
    for left, right in zip(frame.time, frame.time.iloc[1:]):
        if (right - left > pd.Timedelta(days=30)
                or any(left < b and a < right for a, b in absent)
                or any(left < t < right for t in removed)
                or not any(a <= left and right < b for a, b in union)):
            out.append((left, right))
    return out


def _window_sigma(frame, gaps, declared):
    """Small gap-aware adaptation of frozen window_volatility's population."""
    h = declared.window_bars
    if len(frame) < h + 2:
        return None, 0
    times = frame.time
    anchors = np.arange(len(frame) - h)
    close_times = times + pd.Timedelta(hours=1)
    hours = close_times.dt.tz_localize("UTC").dt.tz_convert("America/New_York").dt.hour.to_numpy()
    valid = (hours[anchors] == declared.entry_hour_ny)
    valid &= (close_times.iloc[anchors].to_numpy() >= MAIN_START.to_datetime64())
    valid &= (close_times.iloc[anchors + h].to_numpy() < MAIN_END.to_datetime64())
    cuts = {b for _, b in gaps}
    cumulative = np.cumsum([int(t in cuts) for t in times])
    valid &= cumulative[anchors] == cumulative[anchors + h]
    count = int(valid.sum())
    if count < 2:
        return None, count
    anchors = anchors[valid]
    closes = frame.close.to_numpy(dtype="float64")
    returns = closes[anchors + h] / closes[anchors] - 1.0
    return float(np.std(returns, ddof=1)), count


def _validate_cost_model(model):
    if not isinstance(model, Costs):
        raise TypeError("cost_model must be an explicitly assembled standard Costs model")
    # Frozen CommissionModel exposes no record-presence API. Inspect its explicit
    # mapping: commission_per_lot alone cannot distinguish a record from fallback.
    if not isinstance(model.commission, CommissionModel) or "EURUSD" not in model.commission._per_symbol:
        raise ValueError("EURUSD explicit commission record required; default fallback unsupported")
    rule = model.swap.rule("EURUSD")
    if rule.ref_price is not None or rule.contract_size is not None:
        raise ValueError("EURUSD relative swap unsupported; absolute per-lot rates required")


def _cost_breakdown(model, contract, ref, declared):
    # An empty spread model has a legacy zero fallback; this entry must refuse it.
    spread = model.spread.to_dict()
    if not spread["per_symbol"].get("EURUSD"):
        raise ValueError("EURUSD spread component missing")
    model.swap.rule("EURUSD")  # Required even for an explicitly zero-night declaration.
    external = source_costs(model, "ext:histdata")
    e, x = declared.entry_hour_ny, declared.exit_hour_ny
    se = _number(external.spread_p90("EURUSD", e, price=ref), "spread entry") / ref
    sx = _number(external.spread_p90("EURUSD", x, price=ref), "spread exit") / ref
    le = _number(external.slippage("EURUSD", e, price=ref), "slippage entry") / ref
    lx = _number(external.slippage("EURUSD", x, price=ref), "slippage exit") / ref
    notional = contract * ref
    commission = _number(external.commission_per_lot("EURUSD"), "commission") / notional
    rule = external.swap.rule("EURUSD")
    worst = max(abs(_number(rule.long_per_day, "long swap", signed=True)),
                abs(_number(rule.short_per_day, "short swap", signed=True)))
    overnight = declared.overnight_nights * worst / notional
    cost = max(se, sx) + le + lx + commission + overnight
    if not math.isfinite(cost) or cost <= 0:
        raise ValueError("round_trip_cost must be positive; zero cost multiple undefined")
    return {"reference_price": ref, "reference_timeframe": "H1", "contract_size": contract,
            "spread_round_trip": max(se, sx), "spread_entry_frac": se, "spread_exit_frac": sx,
            "slippage_entry": le, "slippage_exit": lx, "commission": commission,
            "overnight": overnight, "round_trip_cost": cost,
            "multiple": declared.gross_return_per_event / cost}


def run_h_readonly_precheck(candidate, *, inputs, cost_model, contract_sizes, cumulative_n=None) -> dict:
    """Engineering diagnostics only; every result is production not_released."""
    if cumulative_n is not None and (type(cumulative_n) is not int or cumulative_n <= 0):
        raise ValueError("cumulative_n must be an explicitly checked positive integer, not bool")
    if not isinstance(candidate, cand.Candidate):
        raise TypeError("candidate must be a validated Candidate")
    # Revalidate direct dataclass callers using the same frozen declaration parser.
    raw = asdict(candidate)
    raw.pop("path")
    raw.pop("prob_declared")
    raw["symbols"] = list(candidate.symbols)
    for f in ("original", "replications", "rebuttals"):
        raw["literature"][f] = list(raw["literature"][f])
    if raw["declared"] is not None:
        raw["declared"] = {k: v for k, v in raw["declared"].items() if v is not None}
    checked = cand.validate_candidate(raw, source="synthetic readonly")
    declared = checked.declared
    if declared is None or checked.prob_declared is not None or checked.status in cand.BOOKKEEPING_STATUSES \
            or checked.symbols != ("EURUSD",) or checked.holding != "H1" \
            or declared.signal_symbol != "USTEC" or declared.signal_timeframe != "D1":
        raise ValueError("fixed H candidate requires EURUSD/H1 and USTEC/D1 declarations")
    _fields(contract_sizes, {"EURUSD"}, "contract_sizes")
    contract = _number(contract_sizes["EURUSD"], "contract_size", positive=True)
    _validate_cost_model(cost_model)
    scope, frames, tags = _normalize_inputs(inputs)
    selected, selected_tags = {}, {}
    selected[TRADE], selected_tags[TRADE] = _selected(
        frames[TRADE], tags[TRADE], scope.trade_intervals, MAIN_START, MAIN_END)
    selected[SIGNAL], selected_tags[SIGNAL] = _selected(
        frames[SIGNAL], tags[SIGNAL], scope.signal_intervals, WARMUP_START, MAIN_END)
    bad_months = []
    trade = selected[TRADE]
    for (year, month), group in trade.groupby([trade.time.dt.year, trade.time.dt.month]):
        if not _month_sunday_hours_ok(int(year), int(month), _sunday_open_hours(group)):
            bad_months.append(f"{year:04d}-{month:02d}")
    keep = ~trade.time.dt.strftime("%Y-%m").isin(bad_months)
    selected[TRADE] = trade.loc[keep].reset_index(drop=True)
    selected_tags[TRADE] = [s for s, k in zip(selected_tags[TRADE], keep) if k]
    coverage, selection, edges = {}, {}, {}
    for key in PAIRS:
        frame = selected[key]
        in_main = frame.time >= MAIN_START
        main = frame.loc[in_main].reset_index(drop=True)
        main_tags = [s for s, k in zip(selected_tags[key], in_main) if k]
        segments = scope.trade_intervals if key == TRADE else scope.signal_intervals
        edges[key] = _gap_edges(main, frames[key], inputs[key]["missing_intervals"], segments)
        cov = cand.runtime_coverage(main, main_tags, gap_spans=edges[key])
        # runtime_coverage already delegates its span union to merged_coverage_years.
        coverage[key] = cov
        selection[key] = {"bars": len(frame), "warmup_bars": int((~in_main).sum()),
                          "first": frame.time.iloc[0].isoformat() if len(frame) else None,
                          "last": frame.time.iloc[-1].isoformat() if len(frame) else None}
    selection[TRADE]["bad_months"] = bad_months
    lit = checked.literature
    checks = {
        "literature": {"passed": bool(lit.original and lit.search_log.strip() and lit.expected_effect.strip()),
                       "original_n": len(lit.original), "replications_n": len(lit.replications),
                       "rebuttals_n": len(lit.rebuttals)},
        "holding": {"passed": True, "holding": "H1", "seconds": 3600, "min": cand._HOLDING_MIN},
    }
    result = {"candidate_id": checked.id, "execution_status": "not_released",
              "research_status": "not_evaluated", "diagnostic_status": "engineering_waiting",
              "checks": checks, "coverage": coverage, "selection": selection,
              "blockers": ["production_input_release_required", "source_calendar_and_close_mapping_unverified",
                           "random_mtd_sign_current_month_anchor_unresolved"], "reasons": []}
    trade = selected[TRADE]
    detail = _cost_breakdown(cost_model, contract, float(trade.close.iloc[-1]), declared) if len(trade) else None
    cost_ok = detail is not None and detail["multiple"] >= cand._COST_MULTIPLE_MIN
    checks["ex_ante_cost"] = {"passed": bool(cost_ok) if detail else None,
                             "min": cand._COST_MULTIPLE_MIN, "source_policy": SOURCE_COST_POLICY,
                             "per_symbol": {"EURUSD": detail}, "multiple": detail["multiple"] if detail else None}
    sigma, count = _window_sigma(trade, edges[TRADE], declared)
    years_detail = {k: v["years"] for k, v in coverage.items()}
    years = min(years_detail.values())
    implied = ((declared.gross_return_per_event - detail["round_trip_cost"]) / sigma
               * math.sqrt(declared.events_per_year) if sigma is not None and sigma > 0 and detail else None)
    sharpe = min(declared.expected_sharpe, implied) if implied is not None else declared.expected_sharpe
    # Constant prices retain frozen declared-SR fallback; undefined sigma/years do not.
    known = sigma is not None and detail is not None and years > 0
    single = power.power_one_sided(sharpe, years, alpha=cand._POWER_ALPHA,
                                  n_tests=cand._POWER_N_TESTS) if known else None
    cumulative = power.power_one_sided(sharpe, years, alpha=cand._POWER_ALPHA,
                                      n_tests=cumulative_n) if known and cumulative_n is not None else None
    checks["power"] = {
        "passed": single >= cand._POWER_MIN if single is not None else None,
        "power": single, "min": cand._POWER_MIN, "alpha": cand._POWER_ALPHA, "n_tests": cand._POWER_N_TESTS,
        "years": years, "years_detail": years_detail, "expected_sharpe": declared.expected_sharpe,
        "implied_sharpe": implied, "sharpe_used": sharpe if known else None,
        "sigma_by_symbol": {"EURUSD": sigma}, "sigma_windows": count,
        "sigma_window_bars": declared.window_bars, "sigma_entry_hour_ny": declared.entry_hour_ny,
        "min_detectable_sharpe": power.min_detectable_sharpe(years, alpha=cand._POWER_ALPHA) if years > 0 else None,
        "cumulative": {"n_tests": cumulative_n, "power": cumulative,
                       "status": "known" if cumulative is not None else "unknown"},
    }
    if detail and not cost_ok:
        result["diagnostic_status"] = "engineering_fail"
        result["reasons"].append("cost_multiple_below_minimum")
    elif checks["power"]["passed"] and cumulative_n is not None:
        result["diagnostic_status"] = "engineering_pass"
    if single is None:
        result["reasons"].append("main_coverage_or_volatility_unknown")
    elif single < cand._POWER_MIN:
        result["reasons"].append("single_power_below_minimum")
    if cumulative_n is None:
        result["reasons"].append("cumulative_n_unknown")
    return result
