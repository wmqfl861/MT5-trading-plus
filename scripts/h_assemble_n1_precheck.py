"""N1 (Pre-FOMC drift, USTEC/D1) real-input assembler + one-shot main-segment precheck runner (provisional).

Structural mirror of ``scripts/h_assemble_real_precheck.py`` (the delivered C002
assembler) with the N1 differences pre-registered in
``.superpowers/h-new-candidates/n1-draft/N1-precheck-spec-2026-11-06.md``:
the trade series IS the FRED USTEC/D1 close series (no EURUSD, no MT5 series
ever enters the inputs); the main segment is **[bound-manifest measured first
day (expected 1986-01-02), 2021-05-17)**; the bound FOMC calendar is used only
for coverage cross-check registration (event counts are provenance, never a
gate, and no event returns are computed); sigma is the window_bars-declared
single-bar unconditional close-to-close volatility over the whole main segment
with windows never crossing a declared gap; power uses the frozen one-sided
function against the measured sigma and coverage years with ``cumulative_n``
staying None (D33 composition pending review). Costs: bound
``costs/spread_model.json`` USTEC p90 scenario behind the frozen external x1.5
source layer, slippage 0.1 x p90, commission 0 and swap 0 as **declared_terms**
(account-holder statements, ``.superpowers/account-fee-terms-2026-11-06.md``)
with the non-gating spec-snapshot charged reference registered, and the USTEC
index contract size read from the bound ``specs/latest.json``.

Binding is path+SHA256 and fail-closed: the frozen ``N1_WHITELIST`` pins the
four data-root artifacts, ``CALENDAR_SHA256`` pins the FOMC calendar and
``CANDIDATE_SHA256`` pins the N1 draft candidate; any mismatch is refused and
unbound paths are never read. The ``verify_hash``/``calendar_sha`` parameters
exist solely so tests can bind schema-isomorphic synthetic copies and are
exposed by no CLI flag. Output is a new exclusive directory; every result stays
research not_evaluated / execution not_released and is provisional until the
focused independent review.
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

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from mt5quant.costs import (
    CommissionModel, Costs, SOURCE_COST_POLICY, SlippageModel, SpreadModel,
    SpreadStat, SwapModel, SwapRule, source_costs,
)
from mt5quant.costs.spread import BUCKET_TZ, MODES as SPREAD_MODES, SAMPLINGS as SPREAD_SAMPLINGS
from mt5quant.costs.swap import per_lot_per_night
from mt5quant.h_main_scope import CoverageSegment, TimeInterval, _union
from mt5quant.h_readonly_precheck import MAIN_END, REAL_ELIGIBILITY
from mt5quant.research import candidates as cand, power
from mt5quant.research.candidates import validate_candidate

SERIES = "USTEC/D1"
PAIR = ("USTEC", "D1")
SOURCE = "fred"
EXTERNAL_SOURCE = "ext:fred"
FOMC_ERA_START = "1994-09-27"      # LM era; cross-checked against the bound calendar

N1_WHITELIST = {
    "ext/fred/USTEC/D1/data.parquet":
        "c6645256e2cf2ceb10474318c2e5f926a5157cc0c9d50027e55f1482200a71c6",
    "ext/fred/USTEC/D1/manifest.json":
        "08ff3dff0f48330e48832c06eb21bbf8b7564f81616d5783cd183ce31311c5fe",
    "costs/spread_model.json":
        "c35550b90fbefef0d61b6c767e9e5365d6480cffaee232a68da696edb667ce85",
    "specs/latest.json":
        "144bb2245479e3557c571c9f5f83ca92db9d269211da7845c9d5aa2467d3150f",
}
REQUIRED_PATHS = tuple(sorted(N1_WHITELIST))
CALENDAR_LABEL = ".superpowers/h-new-candidates/n1-fomc-calendar/fomc_meetings.json"
# Pinned after the parallel registration-package audit revision of 2026-11-06
# (2003-09-15 removed from the default event set: 310 entries / 257 default /
# 213 main-window events; the first pin 499abd64... was refused by exactly
# this check, see .superpowers/sdd/2026-11-06-n1-precheck/real-run.stderr).
CALENDAR_SHA256 = ("a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e8"
                   "8c1e76ec971c5")
# Byte-identical to the coverage_merge_method note written by
# candidates.precheck (src/mt5quant/research/candidates.py), so the
# assembler-registered payload is indistinguishable in shape from a native
# bt candidate check power block.  Locked by
# test_run_registers_native_power_coverage_segments (fix1, 2026-11-07).
_COVERAGE_MERGE_METHOD_NOTE = (
    "计入段日历区间并集：相接/重叠的段合成连续跨度（min(first).."
    "max(last)），不相接的分段各自计入——重叠不双计（T80/G3 审核"
    "修正：相加会把完全包含/部分重叠的覆盖虚增，如 fred/USTEC "
    "39.8 年包住 mt5 4.3 年 → 合并 39.8 而非 44.1）；每段在产出侧"
    "已在 >30 天内部空洞处切开（T94/R14-L3：实测间隔+known_gaps+"
    "manifest 未启用年份，边界随段的 gaps 字段）")
CANDIDATE_LABEL = ".superpowers/h-new-candidates/n1-draft/C005.toml"
# Same revision: expected_sharpe re-derived to 0.52 (213 window events);
# the pre-pin ca6e7a55... is kept here only as provenance of the re-binding.
CANDIDATE_SHA256 = ("d00adad5f6303b557a97300e2c64c6737be8f2a3d07a72ca72"
                    "06f57af5040bfa")

# Account terms (declared, not deal-measured for USTEC): the account holder
# confirmed a zero-commission account type and provider-refunded swap on
# 2026-11-06 (.superpowers/account-fee-terms-2026-11-06.md; EURUSD-deal
# corroboration in costs/calibrated.json which has no USTEC entry).
ACCOUNT_TERMS_EVIDENCE = ".superpowers/account-fee-terms-2026-11-06.md"
COMMISSION_ROUND_TRIP_PER_LOT = 0.0
SWAP_LONG_PER_DAY = 0.0
SWAP_SHORT_PER_DAY = 0.0

GAP_RULES = {
    SERIES: {
        "weekday_grid": (
            "expected grid = every weekday (Mon-Fri) date from the first to the "
            "last delivered bar; maximal runs of missing weekdays are recorded "
            "as [first missing day 00:00, next existing bar 00:00)"),
        "grid": "1D",
    },
    "weekends": "constructively_excluded",
    "holidays": "conservatively_recorded_missing (no holiday calendar artifact is consumed)",
    "empty_spans": (
        "long spans without bars derive no band; they are covered by the frozen "
        ">30-day span split inside the precheck"),
    "interval_form": "missing_half_open",
    "explicit_registration": (
        "none: the bound FRED manifest declares no salvage-excluded months "
        "(no yearly_verification at all, so the used gate registers "
        "no_yearly_verification_declared and 2019-style salvage handling is N/A)"),
}

N1_RULES = {
    "version": "h-n1-precheck-v1",
    "basis": "N1 (Pre-FOMC drift, USTEC/D1) main-segment real precheck; provisional until focused independent review",
    "main_start": None,           # measured: bound manifest first_time (filled at run)
    "main_end_exclusive": MAIN_END.isoformat(),
    "signal_warmup_days": 0,
    "windows": None,              # filled at run from the measured main start
    "event_window": [FOMC_ERA_START, MAIN_END.isoformat()],
    "eligibility_required": REAL_ELIGIBILITY,
    "binding": ("path+sha256 dual binding of six artifacts (four data-root files "
                "+ FOMC calendar + candidate); fail-closed; unbound paths never read"),
    "cost_policy": SOURCE_COST_POLICY,
    "cost_multiple_min": cand._COST_MULTIPLE_MIN,
    "power_min": cand._POWER_MIN,
    "alpha": cand._POWER_ALPHA,
    "single_n": cand._POWER_N_TESTS,
    "cumulative_n": None,
    "missing_kind": "missing_half_open",
    "gap_rules": GAP_RULES,
    "sigma": ("window_bars-declared single-bar unconditional close-to-close "
              "volatility (ddof=1) over the whole main segment; both window "
              "closes must be inside the main interval and the window must not "
              "cross a declared gap (run_h_readonly_precheck._window_sigma "
              "convention; its H1 entry-hour branch is not applicable to FRED "
              "D1 date labels, which carry no NY hour)"),
    "calendar": ("bound FOMC calendar default event set used ONLY for coverage "
                 "cross-check registration (announcement bar / entry anchor / "
                 "month-end overlap); no event returns are computed and event "
                 "counts never gate"),
    "power_years_basis": ("event era 1994-09-27 onward (registration-package audit "
                          "contract): the power gate counts coverage years from the "
                          "era start on the same frozen runtime_coverage convention; "
                          "full-main-segment years are registered alongside but are "
                          "not the gate"),
    "mt5_series": "never included",
}

_MEETING_FIELDS = frozenset({"year", "label", "start_date", "end_date", "kind",
                             "qualifier", "source_page", "announcement_date",
                             "in_lm_era", "in_default_event_set"})
_MEETING_OPTIONAL = frozenset({"note", "upcoming_at_fetch"})
_REC_FIELDS = frozenset({"bars", "source_tags", "coverage", "missing_kind",
                         "missing_intervals", "source_eligibility"})


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


def _read_pinned(path, expected_sha: str, label: str) -> bytes:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise BindingError(f"{label} unreadable: {path}") from exc
    got = hashlib.sha256(raw).hexdigest()
    if got != expected_sha:
        raise BindingError(f"sha256 mismatch for {label}: expected {expected_sha}, "
                           f"got {got}")
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


def _number(value, label, *, positive=False, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BindingError(f"{label} must be numeric in bound artifacts")
    value = float(value)
    if not math.isfinite(value) or (not signed and value < 0) or (positive and value <= 0):
        raise BindingError(f"{label} must be finite and valid in bound artifacts")
    return value


def _naive(value) -> pd.Timestamp:
    """Any library-normalized UTC datetime/str as a naive UTC Timestamp."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp


def derive_weekday_gaps(times) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Pre-registered USTEC/D1 weekday-grid holes; weekends excluded.

    Same rule the C002 assembler derives for its USTEC signal side (copied
    verbatim so the two assemblers stay independently pinnable).
    """
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


def _read_prices(raw: bytes, rel: str) -> pd.DataFrame:
    """Decode time+close only, from the already verified bytes."""
    try:
        with pq.ParquetFile(io.BytesIO(raw)) as handle:
            table = handle.read(columns=["time", "close"], use_threads=False)
    except Exception as exc:
        raise BindingError(f"bound parquet unreadable: {rel}") from exc
    frame = table.to_pandas()
    if not {"time", "close"} <= set(frame.columns):
        raise BindingError(f"bound parquet lacks time/close: {rel}")
    times = pd.to_datetime(frame["time"], utc=True).dt.tz_localize(None)
    if times.isna().any():
        raise BindingError(f"bound parquet has missing times: {rel}")
    if not (times == times.dt.floor("D")).all():
        raise BindingError(f"bound D1 series must use date labels (00:00 UTC): {rel}")
    return pd.DataFrame({"time": times.reset_index(drop=True),
                         "close": pd.to_numeric(frame["close"], errors="raise")
                         .astype("float64").reset_index(drop=True)})


def _date_str(value, label) -> str:
    if not isinstance(value, str):
        raise BindingError(f"calendar {label} must be a date string")
    try:
        stamp = pd.Timestamp(value)
    except ValueError as exc:
        raise BindingError(f"calendar {label} unreadable: {value!r}") from exc
    if stamp.isoformat()[:10] != value:
        raise BindingError(f"calendar {label} must be strict YYYY-MM-DD: {value!r}")
    return value


def _parse_calendar(raw: bytes) -> dict:
    """Validate the bound FOMC calendar structure; fail closed on any defect."""
    doc = _parse_json(raw, CALENDAR_LABEL)
    for key in ("artifact", "purpose", "event_definition", "meetings"):
        if key not in doc:
            raise BindingError(f"calendar lacks required top-level key: {key}")
    definition = doc["event_definition"]
    if not isinstance(definition, dict) or _date_str(
            definition.get("era_start"), "event_definition.era_start") != FOMC_ERA_START:
        raise BindingError("calendar event_definition.era_start missing/invalid "
                           f"(expected {FOMC_ERA_START})")
    meetings = doc["meetings"]
    if not isinstance(meetings, list):
        raise BindingError(f"calendar meetings must be a list: {CALENDAR_LABEL}")
    for meeting in meetings:
        if not isinstance(meeting, dict):
            raise BindingError("calendar meeting entry must be an object")
        missing = _MEETING_FIELDS - set(meeting)
        extra = set(meeting) - _MEETING_FIELDS - _MEETING_OPTIONAL
        if missing or extra:
            raise BindingError(f"calendar meeting field set invalid: missing "
                               f"{sorted(missing)}, unknown {sorted(extra)}")
        for field in ("start_date", "end_date", "announcement_date"):
            _date_str(meeting[field], f"meeting.{field}")
        if not isinstance(meeting["in_default_event_set"], bool) \
                or not isinstance(meeting["in_lm_era"], bool):
            raise BindingError("calendar meeting era/default flags must be boolean")
        if meeting["in_default_event_set"]:
            if meeting["announcement_date"] != meeting["end_date"]:
                raise BindingError("calendar default event announcement_date must "
                                   f"equal end_date: {meeting['announcement_date']}")
            if meeting["in_lm_era"] is not True:
                raise BindingError("calendar default event must be in_lm_era=true: "
                                   f"{meeting['announcement_date']}")
    return doc


def _calendar_registration(doc: dict, bars: pd.DataFrame,
                           main_start: pd.Timestamp) -> dict:
    """Coverage cross-check registration only; never a gate, no returns."""
    meetings = doc["meetings"]
    default = [m for m in meetings if m["in_default_event_set"] is True]
    event_start = max(pd.Timestamp(doc["event_definition"]["era_start"]), main_start)
    window = [m for m in default
              if event_start <= pd.Timestamp(m["announcement_date"]) < MAIN_END]
    window.sort(key=lambda m: m["announcement_date"])
    bar_days = pd.DatetimeIndex(bars.time).normalize()
    day_set = set(bar_days)
    month_last_bar = {}
    for day in bar_days:
        month_last_bar[(day.year, day.month)] = day  # ordered bars keep the last
    no_bar, no_anchor = [], []
    fred_month_end, calendar_month_end = [], []
    for event in window:
        day = pd.Timestamp(event["announcement_date"])
        if day not in day_set:
            no_bar.append(event["announcement_date"])
            continue
        position = bar_days.searchsorted(day)
        if position == 0 or bar_days[position - 1] < main_start:
            no_anchor.append(event["announcement_date"])
            continue  # fail-closed drop per spec: missing entry anchor
        if month_last_bar.get((day.year, day.month)) == day:
            fred_month_end.append(event["announcement_date"])
        if _last_weekday(day.year, day.month) == day.day:
            calendar_month_end.append(event["announcement_date"])
    return {
        "total_meetings": len(meetings),
        "default_event_set": len(default),
        "era_start": doc["event_definition"]["era_start"],
        "announcement_day_rule": doc["event_definition"].get("announcement_day_rule"),
        "event_window": [event_start.isoformat(), MAIN_END.isoformat()],
        "window_events": len(window),
        "window_first": window[0]["announcement_date"] if window else None,
        "window_last": window[-1]["announcement_date"] if window else None,
        "events_with_announcement_bar": len(window) - len(no_bar),
        "events_with_entry_anchor": len(window) - len(no_bar) - len(no_anchor),
        "dropped_no_bar": no_bar,
        "dropped_no_anchor": no_anchor,
        "month_end_overlap": {"fred_last_trading_day": fred_month_end,
                              "calendar_last_weekday": calendar_month_end,
                              "purpose": "K3 mechanical cross-check: FOMC days are "
                                         "not month-end-flow days by construction"},
        "note": ("event counts and coverage cross-checks are provenance "
                 "registration only; the precheck gates on unconditional window "
                 "volatility, ex-ante costs and coverage-years power, never on "
                 "event returns (no event return is computed)"),
        "open_review_items": {
            "sep_2003_double_entry": ("resolved by the 2026-11-06 registration-package "
                                      "audit revision bound here: 2003-09-15 excluded "
                                      "from the default event set (official September "
                                      "2003 meeting is the 9/16 single-day meeting); "
                                      "the entry is retained in the calendar for "
                                      "diagnostics"),
        },
    }


def _last_weekday(year: int, month: int) -> int:
    import calendar
    day = calendar.monthrange(year, month)[1]
    while calendar.weekday(year, month, day) >= 5:
        day -= 1
    return day


CALENDAR_ARTIFACT_VERSION = 1


def ustec_trading_calendar(prices: pd.DataFrame, *,
                           manifest_last: pd.Timestamp) -> dict:
    """Derive the ``{data-root}/calendars/USTEC.json`` v1 artifact (fix1).

    Root cause 2 of the run1 refusal
    (.superpowers/sdd/2026-11-07-r3-main-test/root-cause-refusal.md): the
    event branch of bt requires an explicit trading calendar.  The source of
    truth here is the bound FRED series itself — the delivered trading-day
    list (**all** days, not only the main window) plus the bound manifest
    ``last_time`` as the completeness bound.  The frozen consumer is
    ``mt5quant.validation.events.trading_calendar``: ``version == 1``,
    ``sessions`` of strictly increasing midnight ``day`` labels with
    ``close_time`` in ``(day, day + 2 days]``, and a naive
    ``complete_through``.  FRED date labels carry no session clock, so
    ``close_time`` is declared as the next UTC midnight — the same convention
    as the engine's D1 fallback close (bar time + 86400 s), not a
    market-clock claim.
    """
    stamps = pd.DatetimeIndex(pd.to_datetime(prices.time))
    days = stamps.normalize()
    if (days.hasnans or len(days) == 0 or days.has_duplicates
            or not days.is_monotonic_increasing
            or not (stamps == days).all()):
        raise BindingError("bound series is not unique ordered D1 date labels")
    last = _naive(manifest_last).normalize()
    if days[-1] != last:
        raise BindingError("calendar completeness bound must equal the "
                           "bound manifest last_time")
    sessions = [{"day": day.date().isoformat(),
                 "close_time": (day + pd.Timedelta(days=1)).date().isoformat()}
                for day in days]
    return {"version": CALENDAR_ARTIFACT_VERSION, "sessions": sessions,
            "complete_through": last.date().isoformat()}


def _assemble_costs(spread_doc: dict, specs_doc: dict, reference_price: float) -> tuple[Costs, dict]:
    try:
        model = spread_doc["model"]
        hours = model["per_symbol"]["USTEC"]
        overall = model["overall_median"]["USTEC"]
        spec = specs_doc["USTEC"]
        contract = spec["contract_size"]
    except (KeyError, TypeError) as exc:
        raise BindingError(f"bound cost/spec artifact lacks required fields: {exc}") from exc
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
    if not stats:
        raise BindingError("spread model has no USTEC hours")
    missing_hours = sorted(set(range(24)) - set(stats))
    spread = SpreadModel({"USTEC": stats}, {"USTEC": _number(overall, "overall_median")},
                         mode=model["mode"], sampling=model["sampling"])
    slippage = SlippageModel(spread, spread_fraction=0.1, fixed_price=0.0,
                             event_hours=frozenset(), event_multiplier=2.0)
    contract_size = _number(contract, "contract_size", positive=True)
    commission = CommissionModel(default_per_lot=COMMISSION_ROUND_TRIP_PER_LOT,
                                 per_symbol={"USTEC": COMMISSION_ROUND_TRIP_PER_LOT})
    swap = SwapModel({"USTEC": SwapRule(long_per_day=SWAP_LONG_PER_DAY,
                                       short_per_day=SWAP_SHORT_PER_DAY)})
    costs = Costs(spread, slippage, commission, swap)
    # Non-gating charged-basis reference (spec §3): worst spec-snapshot side
    # x 1 declared night against the same reference notional. The declared
    # account terms are swap-free; this is registered evidence, not the gate.
    try:
        long_usd, short_usd = per_lot_per_night(spec)
    except (KeyError, ValueError, NotImplementedError, TypeError) as exc:
        raise BindingError(f"USTEC spec snapshot unusable for charged reference: {exc}") from exc
    worst = max(abs(float(long_usd)), abs(float(short_usd)))
    notional = contract_size * reference_price
    provenance = {
        "commission": {
            "basis": "declared_terms",
            "round_trip_per_lot": COMMISSION_ROUND_TRIP_PER_LOT,
            "evidence": ("account-holder statement 2026-11-06 (zero-commission "
                         f"account type) via {ACCOUNT_TERMS_EVIDENCE}; "
                         "costs/calibrated.json has no USTEC entry (EURUSD "
                         "deal-measured 0.0 corroborates the account type)"),
        },
        "swap": {
            "basis": "declared_terms_swap_free",
            "note": ("账户条款声明口径（服务商返还、实质 swap-free），swap_dependent "
                     "预标注；charged 口径另检见 charged_reference"),
            "long_per_day": SWAP_LONG_PER_DAY, "short_per_day": SWAP_SHORT_PER_DAY,
            "domain": "absolute_usd_per_lot_per_night",
            "evidence": ACCOUNT_TERMS_EVIDENCE,
            "charged_reference": {
                "basis": "spec_snapshot_not_deal_measured",
                "worst_side_usd_per_lot_per_night": worst,
                "nights": 1.0,
                "reference_price": reference_price,
                "overnight_fraction_at_reference_1_night": worst / notional,
                "gating": False,
                "note": ("specs/latest.json swap_mode=1 点数换算（×point×contract）；"
                         "MT5 显示值按用户声明会被返还，仅作 charged 口径参考"),
            },
        },
        "spread": {
            "source_file": "costs/spread_model.json",
            "model_sha256": spread_doc.get("model_sha256"),
            "mode": model["mode"], "sampling": model["sampling"], "bucket_tz": model["bucket_tz"],
            "calibration_watermarks": spread_doc.get("watermarks"),
            "built_at_utc": spread_doc.get("built_at_utc"),
            "scenario": True,
            "scenario_note": ("2026 tick 基准施于 1986-2021 属情景外推（relative 模式随"
                              "价格缩放）；来源层 ×1.5 = 冻结 external-spread-x1.5-v1，"
                              "入场/出场小时按候选声明计费（16/16），见 result.checks.ex_ante_cost"),
            "hours_present": sorted(stats),
            "missing_hours": missing_hours,
            "stats_used": {"p90_by_ny_hour": {str(h): stats[h].p90 for h in sorted(stats)}},
            "source_policy": SOURCE_COST_POLICY,
        },
        "slippage": {
            "spread_fraction": 0.1, "fixed_price": 0.0, "event_hours": [],
            "event_multiplier": 2.0,
            "basis": "项目口径 滑点 0.1×p90（docs/reports/scope-2026-10-25.md；事件小时集为空）",
        },
        "contract": {"source_file": "specs/latest.json", "contract_size": contract_size,
                     "note": "USTEC 指数合约（contract_size=1，每手 1 指数单位）"},
    }
    return costs, provenance


def load_candidate(path, *, expected_sha: str = CANDIDATE_SHA256) -> "cand.Candidate":
    """Read + SHA-pin + loader-validate the N1 draft candidate."""
    raw = _read_pinned(path, expected_sha, "candidate " + CANDIDATE_LABEL)
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BindingError(f"candidate TOML unreadable: {CANDIDATE_LABEL}") from exc
    return validate_candidate(doc, source="n1 precheck bytes")


def assemble_n1_inputs(data_root, calendar_path, *,
                       verify_hash=N1_WHITELIST,
                       calendar_sha: str = CALENDAR_SHA256) -> dict:
    """Qualify, assemble and gate the bound N1 inputs; never writes anywhere."""
    data_root = Path(data_root)
    if not isinstance(verify_hash, Mapping):
        raise TypeError("verify_hash must be a mapping of relative path to sha256")
    if set(verify_hash) != set(N1_WHITELIST):
        raise BindingError("whitelist must bind exactly the required paths: "
                           f"{list(REQUIRED_PATHS)}")
    if not isinstance(calendar_sha, str) or len(calendar_sha) != 64:
        raise BindingError("calendar_sha must be a sha256 hex digest")
    bound = {rel: _read_bound(data_root, rel, verify_hash[rel]) for rel in REQUIRED_PATHS}
    calendar_raw = _read_pinned(calendar_path, calendar_sha,
                                "calendar " + CALENDAR_LABEL)
    fred_manifest = _parse_json(bound["ext/fred/USTEC/D1/manifest.json"],
                                "ext/fred/USTEC/D1/manifest.json")
    if (fred_manifest.get("source"), fred_manifest.get("symbol"),
            fred_manifest.get("timeframe")) != (SOURCE,) + PAIR:
        raise BindingError("bound manifest must declare fred/USTEC/D1")
    try:
        first_declared = pd.Timestamp(fred_manifest["first_time"])
        last_declared = pd.Timestamp(fred_manifest["last_time"])
    except (KeyError, ValueError, TypeError) as exc:
        raise BindingError("bound manifest lacks first_time/last_time") from exc
    main_start = _naive(first_declared)
    main_last = _naive(last_declared)
    if main_start > main_last or main_start >= MAIN_END:
        raise BindingError("bound manifest span does not overlap the main window")
    gate = _used_gate(fred_manifest, SERIES, _touched_years(main_start, MAIN_END))
    # Prices are decoded only after the whole qualification above passed.
    prices = _read_prices(bound["ext/fred/USTEC/D1/data.parquet"],
                          "ext/fred/USTEC/D1/data.parquet")
    if prices.time.iloc[0] != main_start or prices.time.iloc[-1] != main_last:
        raise BindingError("manifest first_time/last_time do not match the bound "
                           f"parquet: declared [{main_start}, {main_last}], measured "
                           f"[{prices.time.iloc[0]}, {prices.time.iloc[-1]}]")
    keep = (prices.time >= main_start) & (prices.time < MAIN_END)
    bars = pd.DataFrame({"time": prices.time[keep].reset_index(drop=True),
                         "close": prices.close[keep].reset_index(drop=True)})
    derived = derive_weekday_gaps(bars.time)
    missing = [TimeInterval(a, b) for a, b in derived]
    coverage = [CoverageSegment(PAIR[0], PAIR[1], SOURCE, main_start,
                                min(MAIN_END, main_last + pd.Timedelta(days=1)))]
    inputs = {SERIES: {"bars": bars, "source_tags": [SOURCE] * len(bars),
                       "coverage": coverage, "missing_kind": "missing_half_open",
                       "missing_intervals": missing,
                       "source_eligibility": {SOURCE: REAL_ELIGIBILITY}}}
    calendar_doc = _parse_calendar(calendar_raw)
    calendar = _calendar_registration(calendar_doc, bars, main_start)
    trading_calendar = ustec_trading_calendar(prices, manifest_last=main_last)
    cost_model, costs_provenance = _assemble_costs(
        _parse_json(bound["costs/spread_model.json"], "costs/spread_model.json"),
        _parse_json(bound["specs/latest.json"], "specs/latest.json"),
        float(bars.close.iloc[-1]))
    provenance = {
        "status": "provisional",
        "note": ("聚焦独立评审通过前不交 A1；本产物仅为工程诊断，非研究结论、非执行许可"),
        "research_status": "not_evaluated",
        "execution_status": "not_released",
        "data_root": str(data_root),
        "main_window": [main_start.isoformat(), MAIN_END.isoformat()],
        "main_start_basis": "bound manifest first_time (measured against the parquet)",
        "binding": {
            "policy": ("path+sha256 dual binding; any mismatch fails closed; "
                       "unbound paths never read"),
            "files": {rel: hashlib.sha256(bound[rel]).hexdigest() for rel in REQUIRED_PATHS},
            "calendar": {"path": str(Path(calendar_path)), "sha256": calendar_sha,
                         "label": CALENDAR_LABEL},
        },
        "eligibility": {SERIES: {SOURCE: REAL_ELIGIBILITY}},
        "used_gate": {SERIES: gate},
        "year_handling": {"basis": "not_applicable_fred_has_no_yearly_gate",
                          "note": "FRED manifest 无逐年声明；2019 类回收年处理 N/A"},
        "gap_rules": GAP_RULES,
        "missing_intervals": {SERIES: [[_naive(h.start).isoformat(), _naive(h.end).isoformat()]
                                       for h in _union(missing)]},
        "missing_interval_counts": {SERIES: {"derived_weekday_grid": len(derived)}},
        "coverage_declared": {SERIES: [{"source": s.source, "start": _naive(s.start).isoformat(),
                                        "end": _naive(s.end).isoformat()} for s in coverage]},
        "mt5_series": "never included",
        "trading_calendar_export": {
            "artifact": "calendars/USTEC.json (engine v1 schema; consumer "
                        "mt5quant.validation.events.trading_calendar via "
                        "bt._load_h_research_inputs/_h_event_setup)",
            "fix1_root_cause": 2,
            "derived_from": ("bound ext/fred/USTEC/D1 delivered trading days "
                             "(all days, main window NOT truncated) + bound "
                             "manifest last_time as the completeness bound"),
            "day_basis": "FRED date labels = trading days (close-only series)",
            "close_time_basis": ("next UTC midnight (day+1d) — the engine D1 "
                                 "fallback close convention (time + 86400 s); "
                                 "no market-clock claim, no fabricated session"),
            "exported_via": "--export-calendar (exclusive; refuses overwrite)",
        },
        "costs": costs_provenance,
        "calendar": calendar,
    }
    return {"inputs": inputs, "cost_model": cost_model,
            "contract_sizes": {"USTEC": costs_provenance["contract"]["contract_size"]},
            "calendar": calendar, "trading_calendar": trading_calendar,
            "provenance": provenance}


def _fields(value, required, label):
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    missing = set(required) - set(value)
    extra = set(value) - set(required)
    if missing or extra:
        raise ValueError(f"{label}: missing {sorted(missing)}, unknown {sorted(extra)}")


def _gap_edges(frame, original, holes, segments):
    """Actual missing intervals -> surviving adjacent bar endpoints, never reverse."""
    removed = set(original.time) - set(frame.time)
    bounds = [(_naive(s.start), _naive(s.end)) for s in segments]
    union = []
    for a, b in sorted(bounds):
        if union and a <= union[-1][1]:
            union[-1] = (union[-1][0], max(union[-1][1], b))
        else:
            union.append([a, b])
    absent = [(_naive(h.start), _naive(h.end)) for h in holes]
    out = []
    for left, right in zip(frame.time, frame.time.iloc[1:]):
        if (right - left > pd.Timedelta(days=30)
                or any(left < b and a < right for a, b in absent)
                or any(left < t < right for t in removed)
                or not any(a <= left and right < b for a, b in union)):
            out.append((left, right))
    return out


def d1_window_sigma(frame, gaps, window_bars: int, *, main_start, main_end):
    """D1 branch of the frozen ``_window_sigma`` convention (no hour filter).

    ``run_h_readonly_precheck._window_sigma`` IS the single-bar convention
    (ddof=1 close-to-close, both window closes inside the main interval,
    cumulative cut never crossing a gap); its entry-hour filter is H1-only and
    has no meaning for FRED D1 date labels, so this is the same function minus
    that branch (frozen ``window_volatility`` spec: D1 has a single daily start).
    """
    h = window_bars
    if len(frame) < h + 2:
        return None, 0
    times = frame.time
    anchors = np.arange(len(frame) - h)
    close_times = times + pd.Timedelta(hours=1)
    valid = (close_times.iloc[anchors].to_numpy() >= pd.Timestamp(main_start).to_datetime64())
    valid &= (close_times.iloc[anchors + h].to_numpy() < pd.Timestamp(main_end).to_datetime64())
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
    if not isinstance(model.commission, CommissionModel) or "USTEC" not in model.commission._per_symbol:
        raise ValueError("USTEC explicit commission record required; default fallback unsupported")
    rule = model.swap.rule("USTEC")
    if rule.ref_price is not None or rule.contract_size is not None:
        raise ValueError("USTEC relative swap unsupported; absolute per-lot rates required")


def _cost_breakdown(model, contract, ref, declared):
    # An empty spread model has a legacy zero fallback; this entry must refuse it.
    spread = model.spread.to_dict()
    if not spread["per_symbol"].get("USTEC"):
        raise ValueError("USTEC spread component missing")
    model.swap.rule("USTEC")  # Required even for the declared swap-free terms.
    external = source_costs(model, EXTERNAL_SOURCE)
    e, x = declared.entry_hour_ny, declared.exit_hour_ny
    se = _number(external.spread_p90("USTEC", e, price=ref), "spread entry") / ref
    sx = _number(external.spread_p90("USTEC", x, price=ref), "spread exit") / ref
    le = _number(external.slippage("USTEC", e, price=ref), "slippage entry") / ref
    lx = _number(external.slippage("USTEC", x, price=ref), "slippage exit") / ref
    notional = contract * ref
    commission = _number(external.commission_per_lot("USTEC"), "commission") / notional
    rule = external.swap.rule("USTEC")
    worst = max(abs(_number(rule.long_per_day, "long swap", signed=True)),
                abs(_number(rule.short_per_day, "short swap", signed=True)))
    overnight = declared.overnight_nights * worst / notional
    cost = max(se, sx) + le + lx + commission + overnight
    if not math.isfinite(cost) or cost <= 0:
        raise ValueError("round_trip_cost must be positive; zero cost multiple undefined")
    return {"reference_price": ref, "reference_timeframe": "D1", "contract_size": contract,
            "spread_round_trip": max(se, sx), "spread_entry_frac": se, "spread_exit_frac": sx,
            "slippage_entry": le, "slippage_exit": lx, "commission": commission,
            "overnight": overnight, "round_trip_cost": cost,
            "multiple": declared.gross_return_per_event / cost}


def _normalize_series(inputs, required_eligibility):
    from collections.abc import Sequence
    _fields(inputs, {SERIES}, "inputs")
    rec = inputs[SERIES]
    _fields(rec, _REC_FIELDS, SERIES)
    if rec["missing_kind"] != "missing_half_open":
        raise ValueError(f"{SERIES}: missing_kind must be missing_half_open")
    eligibility = rec["source_eligibility"]
    if not isinstance(eligibility, Mapping) or set(eligibility) != {SOURCE} \
            or eligibility[SOURCE] != required_eligibility:
        raise PermissionError(f"{SERIES}: source qualification not_released; "
                              f"{required_eligibility} required")
    holes = rec["missing_intervals"]
    if not isinstance(holes, Sequence) or isinstance(holes, (str, bytes)) \
            or not all(isinstance(h, TimeInterval) for h in holes):
        raise TypeError("missing_intervals must contain TimeInterval")
    (coverage,) = rec["coverage"]
    if (coverage.symbol, coverage.timeframe, coverage.source) != PAIR + (SOURCE,):
        raise ValueError(f"{SERIES}: coverage pair/source mismatch")
    bars = rec["bars"]
    if not isinstance(bars, pd.DataFrame) or not {"time", "close"} <= set(bars.columns) \
            or bars.columns.has_duplicates:
        raise TypeError(f"{SERIES}: bars must have unique time/close columns")
    frame = bars[["time", "close"]].copy(deep=True).reset_index(drop=True)
    frame["time"] = pd.to_datetime([_naive(t) for t in frame.time])
    if frame.time.duplicated().any() or not frame.time.is_monotonic_increasing:
        raise ValueError(f"{SERIES}: bars must be unique and ordered")
    frame["close"] = [_number(v, f"{SERIES} close", positive=True) for v in frame.close]
    if not (frame.time == frame.time.dt.floor("D")).all():
        raise ValueError("D1 bars must use date labels (00:00 UTC)")
    if len(frame) == 0:
        raise ValueError(f"{SERIES}: empty bars")
    main_start = _naive(coverage.start)
    main_end = _naive(coverage.end)
    if not (main_start <= frame.time.min() and frame.time.max() < main_end):
        raise ValueError(f"{SERIES}: bars outside the declared coverage window")
    tags = list(rec["source_tags"])
    if len(tags) != len(frame) or set(tags) != {SOURCE}:
        raise ValueError(f"{SERIES}: source_tags must align and be {SOURCE!r} only")
    for stamp in frame.time:
        if any(_naive(h.start) <= stamp < _naive(h.end) for h in holes):
            raise ValueError(f"{SERIES}: existing bar lies inside actual missing interval")
    return frame, tags, holes, main_start, main_end


def run_n1_precheck(candidate_obj, *, inputs, cost_model, contract_sizes,
                    required_eligibility: str = REAL_ELIGIBILITY,
                    cumulative_n=None) -> dict:
    """Engineering diagnostics only; every result is production not_released."""
    if required_eligibility != REAL_ELIGIBILITY:
        raise ValueError(f"unknown required eligibility: {required_eligibility!r}")
    if cumulative_n is not None and (type(cumulative_n) is not int or cumulative_n <= 0):
        raise ValueError("cumulative_n must be an explicitly checked positive integer, not bool")
    from dataclasses import asdict
    if not isinstance(candidate_obj, cand.Candidate):
        raise TypeError("candidate must be a validated Candidate")
    raw = asdict(candidate_obj)
    raw.pop("path")
    raw.pop("prob_declared")
    raw["symbols"] = list(candidate_obj.symbols)
    for f in ("original", "replications", "rebuttals"):
        raw["literature"][f] = list(raw["literature"][f])
    if raw["declared"] is not None:
        raw["declared"] = {k: v for k, v in raw["declared"].items() if v is not None}
    checked = cand.validate_candidate(raw, source="n1 readonly")
    declared = checked.declared
    if declared is None or checked.prob_declared is not None \
            or checked.status in cand.BOOKKEEPING_STATUSES \
            or checked.symbols != ("USTEC",) or checked.holding != "D1":
        raise ValueError("N1 candidate requires USTEC/D1 declarations")
    _fields(contract_sizes, {"USTEC"}, "contract_sizes")
    contract = _number(contract_sizes["USTEC"], "contract_size", positive=True)
    _validate_cost_model(cost_model)
    frame, tags, holes, main_start, main_end = _normalize_series(inputs, required_eligibility)
    edges = _gap_edges(frame, frame, holes, inputs[SERIES]["coverage"])
    coverage = cand.runtime_coverage(frame, tags, gap_spans=edges)
    # Power-years contract (registration-package audit 2026-11-06): the gate
    # counts coverage years from the event era start on the same frozen
    # runtime_coverage convention; sigma stays a whole-main-segment property.
    era_start = max(pd.Timestamp(FOMC_ERA_START), main_start)
    in_era = frame.time >= era_start
    era_frame = frame.loc[in_era].reset_index(drop=True)
    era_tags = [s for s, k in zip(tags, in_era) if k]
    era_edges = [(a, b) for a, b in edges if a >= era_start]
    era_coverage = (cand.runtime_coverage(era_frame, era_tags, gap_spans=era_edges)
                    if len(era_frame) else {"years": 0.0})
    lit = checked.literature
    checks = {
        "literature": {"passed": bool(lit.original and lit.search_log.strip() and lit.expected_effect.strip()),
                       "original_n": len(lit.original), "replications_n": len(lit.replications),
                       "rebuttals_n": len(lit.rebuttals)},
        "holding": {"passed": True, "holding": "D1", "seconds": 86400, "min": cand._HOLDING_MIN},
    }
    result = {"candidate_id": checked.id, "execution_status": "not_released",
              "research_status": "not_evaluated", "diagnostic_status": "engineering_waiting",
              "checks": checks, "coverage": {SERIES: coverage},
              "coverage_event_era": {SERIES: era_coverage, "basis": {
                  "start": era_start.isoformat(),
                  "note": "power gate basis (registration-package audit contract "
                          "2026-11-06); full-main-segment coverage is the "
                          "data-side registration above"}},
              "selection": {SERIES: {"bars": len(frame),
                                     "first": frame.time.iloc[0].isoformat(),
                                     "last": frame.time.iloc[-1].isoformat(),
                                     "main_window": [main_start.isoformat(),
                                                     main_end.isoformat()]}},
              "blockers": ["production_input_release_required",
                           "n1_registration_package_review_pending"],
              "reasons": []}
    detail = _cost_breakdown(cost_model, contract, float(frame.close.iloc[-1]), declared)
    cost_ok = detail["multiple"] >= cand._COST_MULTIPLE_MIN
    checks["ex_ante_cost"] = {"passed": bool(cost_ok), "min": cand._COST_MULTIPLE_MIN,
                              "source_policy": SOURCE_COST_POLICY,
                              "per_symbol": {"USTEC": detail},
                              "multiple": detail["multiple"]}
    sigma, count = d1_window_sigma(frame, edges, declared.window_bars,
                                   main_start=main_start, main_end=main_end)
    years = era_coverage["years"]  # event-era basis: the power gate contract
    implied = ((declared.gross_return_per_event - detail["round_trip_cost"]) / sigma
               * math.sqrt(declared.events_per_year)
               if sigma is not None and sigma > 0 else None)
    sharpe = min(declared.expected_sharpe, implied) if implied is not None else declared.expected_sharpe
    known = sigma is not None and years > 0
    single = power.power_one_sided(sharpe, years, alpha=cand._POWER_ALPHA,
                                   n_tests=cand._POWER_N_TESTS) if known else None
    cumulative = power.power_one_sided(sharpe, years, alpha=cand._POWER_ALPHA,
                                       n_tests=cumulative_n) if known and cumulative_n is not None else None
    checks["power"] = {
        "passed": single >= cand._POWER_MIN if single is not None else None,
        "power": single, "min": cand._POWER_MIN, "alpha": cand._POWER_ALPHA,
        "n_tests": cand._POWER_N_TESTS,
        "years": years, "years_detail": {SERIES: years},
        "years_basis": (f"event era {era_start.date().isoformat()} onward "
                        "(registration-package audit contract); full-main-segment "
                        "years reported separately and not used by the gate"),
        "years_full_main_segment": coverage["years"],
        "expected_sharpe": declared.expected_sharpe,
        "implied_sharpe": implied, "sharpe_used": sharpe if known else None,
        "sigma_by_symbol": {"USTEC": sigma}, "sigma_windows": count,
        "sigma_window_bars": declared.window_bars,
        "sigma_entry_hour_ny": declared.entry_hour_ny,
        "sigma_entry_hour_applied": None,
        "sigma_note": ("D1 date labels carry no NY entry hour; the frozen "
                       "window_volatility spec keeps a single daily start, so "
                       "every main-segment window counts"),
        "min_detectable_sharpe": power.min_detectable_sharpe(years, alpha=cand._POWER_ALPHA) if years > 0 else None,
        "cumulative": {"n_tests": cumulative_n, "power": cumulative,
                       "status": "known" if cumulative is not None else "unknown"},
    }
    # fix1 (root cause 1, 2026-11-07): the registered payload must carry the
    # main-window coverage segments in exactly the native bt candidate check
    # shape (candidates.coverage_segments -> _segments_for_span /
    # merged_coverage_years), so the frozen consumer
    # bt._load_h_research_inputs can activate the FRED merged load path
    # (any counted non-mt5 segment; bt.py merged_keys predicate).  Segment
    # data comes from the already assembled main-window bars — no new
    # computation, and every pre-existing number stays byte-identical.
    coverage_segments = {SERIES: cand._segments_for_span(
        SOURCE, frame.time.iloc[0], frame.time.iloc[-1],
        cand._observed_gaps(frame.time))}
    checks["power"]["coverage_segments"] = coverage_segments
    checks["power"]["coverage_merged_years"] = {
        SERIES: cand.merged_coverage_years(coverage_segments[SERIES])}
    checks["power"]["coverage_merge_method"] = _COVERAGE_MERGE_METHOD_NOTE
    if not cost_ok:
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


def _code_hashes():
    root = Path(__file__).resolve().parents[1]
    files = ["scripts/h_assemble_n1_precheck.py",
             "src/mt5quant/h_readonly_precheck.py", "src/mt5quant/h_main_scope.py",
             "src/mt5quant/research/candidates.py", "src/mt5quant/research/power.py",
             "src/mt5quant/costs/__init__.py", "src/mt5quant/costs/spread.py",
             "src/mt5quant/costs/slippage.py", "src/mt5quant/costs/commission.py",
             "src/mt5quant/costs/swap.py"]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path,
                        help="read-only research data root holding the four bound data artifacts")
    parser.add_argument("--calendar", required=True, type=Path,
                        help="SHA256-pinned FOMC calendar artifact (fomc_meetings.json)")
    parser.add_argument("--candidate", required=True, type=Path,
                        help="SHA256-pinned N1 draft candidate TOML (loader-validated)")
    parser.add_argument("--output", required=True, type=Path,
                        help="new exclusive output directory for result.json")
    parser.add_argument("--export-calendar", type=Path, default=None,
                        help="optional: write the derived {data-root}/calendars/USTEC.json "
                             "v1 trading calendar artifact from the same bound inputs "
                             "(exclusive; refuses to overwrite an existing file)")
    args = parser.parse_args(argv)
    try:
        assembled = assemble_n1_inputs(args.data_root, args.calendar)
        candidate_obj = load_candidate(args.candidate)
        result = run_n1_precheck(candidate_obj, inputs=assembled["inputs"],
                                 cost_model=assembled["cost_model"],
                                 contract_sizes=assembled["contract_sizes"])
        provenance = dict(assembled["provenance"])
        provenance["candidate"] = {"path": str(args.candidate),
                                   "sha256": CANDIDATE_SHA256, "id": candidate_obj.id,
                                   "label": CANDIDATE_LABEL}
        # Conservative registration diagnostic (provenance only, never the
        # gate): the same frozen power functions on event-effective years.
        declared = candidate_obj.declared
        events = assembled["calendar"]["window_events"]
        if declared is not None and events:
            eff_years = events / declared.events_per_year
            sharpe_used = result["checks"]["power"]["sharpe_used"]
            provenance["calendar"]["event_effective_power"] = {
                "basis": ("conservative registration basis: window events / declared "
                          "events_per_year; the gate itself uses measured coverage years"),
                "declared_events_per_year": declared.events_per_year,
                "window_events": events,
                "event_effective_years": eff_years,
                "sr_req_50pct": power.min_detectable_sharpe(
                    eff_years, alpha=cand._POWER_ALPHA, power=0.5),
                "power_at_event_effective_years": (
                    power.power_one_sided(sharpe_used, eff_years,
                                          alpha=cand._POWER_ALPHA,
                                          n_tests=cand._POWER_N_TESTS)
                    if sharpe_used is not None else None),
            }
        # Calendar export precedes the result write: a failed export must not
        # leave a half-completed run behind (fail-closed, exclusive target).
        if args.export_calendar is not None:
            if args.export_calendar.exists():
                raise BindingError(f"calendar export target already exists "
                                   f"(refusing to overwrite): {args.export_calendar}")
            calendar_text = json.dumps(assembled["trading_calendar"],
                                       ensure_ascii=False, indent=1,
                                       allow_nan=False) + "\n"
            args.export_calendar.parent.mkdir(parents=True, exist_ok=True)
            args.export_calendar.write_text(calendar_text, encoding="utf-8")
            print(f"calendar export: {args.export_calendar} (sha256 "
                  f"{hashlib.sha256(calendar_text.encode('utf-8')).hexdigest()})",
                  file=sys.stderr)
        rules = dict(N1_RULES)
        rules["main_start"] = provenance["main_window"][0]
        rules["windows"] = {SERIES: list(provenance["main_window"])}
        rules_json = json.dumps(rules, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload = {"result": result, "provenance": provenance,
                   "hashes": {"domain": "sha256(raw_artifact_bytes)",
                              "artifacts": dict(provenance["binding"]["files"],
                                                **{"calendar:" + CALENDAR_LABEL:
                                                   provenance["binding"]["calendar"]["sha256"],
                                                   "candidate:" + candidate_obj.id:
                                                   provenance["candidate"]["sha256"]}),
                              "rules_domain": "sha256(canonical_rules_json_utf8)",
                              "rules": hashlib.sha256(rules_json).hexdigest(),
                              "code": _code_hashes()}}
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        args.output.mkdir(exist_ok=False)
        with (args.output / "result.json").open("x", encoding="utf-8") as stream:
            stream.write(text)
        print(text, end="")
        return 0
    except (OSError, ValueError, TypeError, KeyError, PermissionError) as exc:
        print(f"h_assemble_n1_precheck: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
