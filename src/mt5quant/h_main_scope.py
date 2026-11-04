"""T114 fixed main-scope metadata only; no data loading or execution approval.

Missing intervals are actual missing [start, end) times, NOT legacy merge
``gap_spans`` (the timestamps of two existing bars). Conversion requires a
separate, verified source/calendar contract; no period or 30-day rule is guessed.
Event ranges, coverage years, D33 and calendars remain outside this module.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re


_PAIR_SOURCES = {
    ("EURUSD", "H1"): frozenset(("histdata", "mt5")),
    ("USTEC", "D1"): frozenset(("fred", "mt5")),
}
_MAIN_START = datetime(2007, 1, 1, tzinfo=timezone.utc)
_MAIN_END = datetime(2021, 5, 17, tzinfo=timezone.utc)
# Explicit ISO subset: date, or date + T/space + HH:MM[:SS[.ffffff]],
# optionally followed by Z or +/-HH:MM. Never truncate sub-microsecond input.
_ISO_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:[T ](?:[01][0-9]|2[0-3]):[0-5][0-9]"
    r"(?::[0-5][0-9](?:\.[0-9]{1,6})?)?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?)?"
)


def _utc(value: datetime | str) -> datetime:
    if not isinstance(value, (datetime, str)):
        raise TypeError("time must be a datetime or an ISO string")
    try:
        if isinstance(value, str):
            if not _ISO_TIME.fullmatch(value):
                raise ValueError("time must use an explicit ISO date/time format")
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise ValueError("invalid time or time outside the UTC datetime range") from exc


def _bounds(start: datetime | str, end: datetime | str) -> tuple[datetime, datetime]:
    start, end = _utc(start), _utc(end)
    if start >= end:
        raise ValueError("interval must have start < end in UTC")
    return start, end


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """Actual missing time [start, end), normalized to UTC at construction."""

    start: datetime | str
    end: datetime | str

    def __post_init__(self) -> None:
        start, end = _bounds(self.start, self.end)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class CoverageSegment:
    """Declared coverage with an explicit canonical source; not an approval."""

    symbol: str
    timeframe: str
    source: str
    start: datetime | str
    end: datetime | str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in (self.symbol, self.timeframe, self.source)):
            raise TypeError("symbol, timeframe and source must be strings")
        sources = _PAIR_SOURCES.get((self.symbol, self.timeframe))
        if sources is None:
            raise ValueError("unsupported symbol/timeframe pair")
        if self.source not in sources:
            raise ValueError("unsupported canonical source for symbol/timeframe")
        start, end = _bounds(self.start, self.end)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class ScopeReport:
    """Immutable metadata result. Waiting reports withhold BOTH interval sets.

    Intervals retain their source. Exclusions are (CoverageSegment, reason)
    tuples. Reasons are sorted unique codes, not research eligibility results.
    """

    status: str
    trade_intervals: tuple[CoverageSegment, ...]
    signal_intervals: tuple[CoverageSegment, ...]
    excluded: tuple[tuple[CoverageSegment, str], ...]
    reasons: tuple[str, ...]
    execution_status: str = field(default="not_released", init=False)


def _sequence(value: object, label: str) -> Sequence:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{label} must be a sequence of records")
    return value


def _union(intervals: Sequence[TimeInterval]) -> tuple[TimeInterval, ...]:
    merged: list[TimeInterval] = []
    for interval in sorted(intervals, key=lambda item: (item.start, item.end)):
        if merged and interval.start <= merged[-1].end:
            merged[-1] = TimeInterval(merged[-1].start, max(merged[-1].end, interval.end))
        else:
            merged.append(interval)
    return tuple(merged)


def _missing(
    missing_intervals: Mapping[tuple[str, str], Sequence[TimeInterval] | None] | None,
) -> tuple[dict[tuple[str, str], tuple[TimeInterval, ...]], set[str]]:
    if missing_intervals is None:
        missing_intervals = {}
    if not isinstance(missing_intervals, Mapping):
        raise TypeError("missing_intervals must be a mapping or None")
    normalized = {}
    for pair, intervals in missing_intervals.items():
        if not (isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(part, str) for part in pair)):
            raise TypeError("missing_intervals keys must be (symbol, timeframe) string tuples")
        if pair not in _PAIR_SOURCES:
            raise ValueError("unsupported missing_intervals symbol/timeframe pair")
        if intervals is not None:
            intervals = _sequence(intervals, "missing_intervals value")
            if not all(isinstance(interval, TimeInterval) for interval in intervals):
                raise TypeError("missing_intervals values must contain TimeInterval records")
            normalized[pair] = _union(intervals)
    reasons = {
        f"missing_intervals_unknown:{symbol}/{timeframe}"
        for symbol, timeframe in _PAIR_SOURCES
        if (symbol, timeframe) not in normalized
    }
    return normalized, reasons


def _subtract(
    intervals: Sequence[TimeInterval], holes: Sequence[TimeInterval],
) -> tuple[tuple[TimeInterval, ...], tuple[TimeInterval, ...]]:
    """Subtract normalized holes; return retained and actually removed pieces."""
    kept, removed = [], []
    for interval in intervals:
        cursor = interval.start
        for hole in holes:
            if hole.end <= cursor:
                continue
            if hole.start >= interval.end:
                break
            if cursor < hole.start:
                kept.append(TimeInterval(cursor, hole.start))
            cut_end = min(hole.end, interval.end)
            removed.append(TimeInterval(max(cursor, hole.start), cut_end))
            cursor = cut_end
        if cursor < interval.end:
            kept.append(TimeInterval(cursor, interval.end))
    return tuple(kept), tuple(removed)


def _segment_key(segment: CoverageSegment) -> tuple:
    return (segment.symbol, segment.timeframe, segment.start, segment.end, segment.source)


def validate_h_main_scope(
    *,
    coverage: Sequence[CoverageSegment],
    missing_intervals: Mapping[tuple[str, str], Sequence[TimeInterval] | None] | None,
) -> ScopeReport:
    """Normalize explicit metadata for the fixed T114 scope, with no I/O.

    Each pair needs an explicit missing-interval sequence: [] means checked and
    empty; absent/None means unknown and forces waiting_data. Invalid metadata
    raises TypeError/ValueError even when other evidence is unknown.

    metadata_valid checks nonempty HistData trade coverage and some signal
    overlap with the main window, not full coverage, anchors, calendar validity,
    provenance truth, candidate eligibility or permission to run anything.
    """
    coverage = _sequence(coverage, "coverage")
    grouped: dict[tuple[str, str, str], list[TimeInterval]] = {}
    for segment in coverage:
        if not isinstance(segment, CoverageSegment):
            raise TypeError("coverage must contain CoverageSegment records")
        key = (segment.symbol, segment.timeframe, segment.source)
        grouped.setdefault(key, []).append(TimeInterval(segment.start, segment.end))
    holes, reasons = _missing(missing_intervals)
    unknown = bool(reasons)
    trade: list[CoverageSegment] = []
    signal: list[CoverageSegment] = []
    excluded: list[tuple[CoverageSegment, str]] = []

    for (symbol, timeframe, source), intervals in sorted(grouped.items()):
        intervals = _union(intervals)
        is_trade = (symbol, timeframe) == ("EURUSD", "H1")

        def exclude(pieces: Sequence[TimeInterval], reason: str) -> None:
            for piece in pieces:
                excluded.append((CoverageSegment(symbol, timeframe, source, piece.start, piece.end), reason))

        if is_trade and source == "mt5":
            exclude(intervals, "trade_mt5_diagnostic")
            continue
        if is_trade:
            inside, outside = [], []
            for interval in intervals:
                start, end = max(interval.start, _MAIN_START), min(interval.end, _MAIN_END)
                if start < end:
                    inside.append(TimeInterval(start, end))
                if interval.start < _MAIN_START:
                    outside.append(TimeInterval(interval.start, min(interval.end, _MAIN_START)))
                if interval.end > _MAIN_END:
                    outside.append(TimeInterval(max(interval.start, _MAIN_END), interval.end))
            exclude(outside, "trade_outside_main")
            intervals = tuple(inside)
        # Unknown holes are NOT evidence of no holes: any provisional pieces
        # stay internal and are withheld from the report by the waiting gate.
        kept, removed = _subtract(intervals, holes.get((symbol, timeframe), ()))
        exclude(removed, "declared_missing")
        destination = trade if is_trade else signal
        destination.extend(CoverageSegment(symbol, timeframe, source, piece.start, piece.end) for piece in kept)

    trade.sort(key=_segment_key)
    signal.sort(key=_segment_key)
    # Same-source coverage was already unioned. A strict overlap of consecutive
    # retained segments therefore detects competing sources, including anchors
    # before the main window and metadata after its end. Adjacency is legal.
    for left, right in zip(signal, signal[1:]):
        if left.source != right.source and right.start < left.end:
            raise ValueError("retained USTEC/D1 signal coverage overlaps across sources")
    if not trade:
        reasons.add("trade_coverage_empty")
    signal_overlaps_main = any(segment.start < _MAIN_END and segment.end > _MAIN_START for segment in signal)
    if not signal_overlaps_main:
        reasons.add("signal_main_overlap_absent")
    waiting = unknown or not trade or not signal_overlaps_main
    reasons.update(reason for _, reason in excluded)
    return ScopeReport(
        status="waiting_data" if waiting else "metadata_valid",
        trade_intervals=() if waiting else tuple(trade),
        signal_intervals=() if waiting else tuple(signal),
        excluded=tuple(sorted(excluded, key=lambda item: (*_segment_key(item[0]), item[1]))),
        reasons=tuple(sorted(reasons)),
    )
