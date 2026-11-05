"""Isolated D61 V2-A day-weighted arithmetic; no product or research entry points."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import (Clamped, Context, Decimal, DecimalException, DivisionByZero,
                     FloatOperation, InvalidOperation, Overflow, ROUND_CEILING,
                     ROUND_HALF_EVEN, Subnormal, Underflow)
from fractions import Fraction
import math


_OUTCOMES = ("win", "loss", "time")
_FEATURES = ("f0", "f1", "na")
_QUERY_PAIRS = (("S1", "A1"), ("S1", "A2"), ("S2", "A2"))


@dataclass(frozen=True)
class PlanRow:
    day_id: int
    situation: str
    action: str
    feature: str
    outcome: str | None
    gross_r: int | float | None


@dataclass(frozen=True)
class LayerCounts:
    """Raw counts; outcome_counts always follows win/loss/time order."""

    row_count: int
    planned_days: int
    visible_days: int
    outcome_counts: tuple[int, int, int]
    missing_count: int


@dataclass(frozen=True)
class V2AResult:
    """Finite binary64 diagnostics, never fed back into the exact calculation."""

    lower: float
    guard: float
    radius: float | None
    mean_w: float | None
    center: float | None
    selected_layer: str
    target_days: int
    cell: LayerCounts
    parent: LayerCounts
    action: LayerCounts
    p: tuple[float | None, float | None, float | None]
    r: tuple[float | None, float | None, float | None]
    q: tuple[float | None, float | None, float | None]
    v: tuple[float | None, float | None, float | None]


@dataclass(frozen=True)
class _Aggregate:
    counts: LayerCounts
    k: tuple[Fraction, ...]
    s: tuple[Fraction, ...]
    mean_w: Fraction | None


def _number(value: object) -> Fraction:
    if type(value) not in (int, float):
        raise ValueError("numbers must be builtin int or float, excluding bool")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("numbers must be finite")
    return Fraction(value)


def _as_float(value: Fraction | None) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError("cannot produce finite binary64 output") from exc
    if not math.isfinite(result):
        raise ValueError("cannot produce finite binary64 output")
    return result


def _directed_float(value: Fraction, *, upward: bool) -> float:
    """Convert once, move at most one binary64 ulp outward, then recheck."""
    result = _as_float(value)
    exact = Fraction.from_float(result)
    if (upward and exact < value) or (not upward and exact > value):
        result = math.nextafter(result, math.inf if upward else -math.inf)
    if not math.isfinite(result):
        raise ValueError("directed binary64 output is not finite")
    exact = Fraction.from_float(result)
    if (upward and exact < value) or (not upward and exact > value):
        raise ValueError("directed binary64 conversion did not enclose the value")
    return result


def _radius_upper(width: Fraction, days: int) -> Fraction:
    """Fixed 80-digit enclosure; no caller/default Decimal state is inherited."""
    context = Context(
        prec=80, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
        capitals=1, clamp=0, flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow, Underflow,
               Subnormal, Clamped, FloatOperation],
    )
    try:
        ell_up = context.next_plus(context.ln(Decimal(900)))
        quotient = Fraction(ell_up) / (2 * days)
        context.rounding = ROUND_CEILING
        t = context.divide(Decimal(quotient.numerator), Decimal(quotient.denominator))
        context.rounding = ROUND_HALF_EVEN
        s_up = context.next_plus(context.sqrt(t))
        if not s_up.is_finite():
            raise ValueError("radius enclosure is not finite")
        return Fraction(s_up) * width
    except (DecimalException, OverflowError) as exc:
        raise ValueError("cannot compute the fixed Decimal radius enclosure") from exc


def _validate(rows, query, support, lb_z):
    if _number(lb_z) != 1:
        raise ValueError("lb_z is fixed at 1")
    if (not isinstance(query, (tuple, list)) or len(query) != 3
            or any(type(item) is not str for item in query)
            or tuple(query[:2]) not in _QUERY_PAIRS or query[2] not in _FEATURES):
        raise ValueError("query must be a whitelisted situation/action/feature triple")
    if not isinstance(support, Mapping) or set(support) != set(_OUTCOMES):
        raise ValueError("support must contain exactly win/loss/time")
    bounds = {}
    for outcome in _OUTCOMES:
        pair = support[outcome]
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("each support must have two endpoints")
        lo, hi = map(_number, pair)
        if lo >= hi:
            raise ValueError("support endpoints must be strictly increasing")
        bounds[outcome] = (lo, hi)
    a = min(lo for lo, _ in bounds.values())
    b = max(hi for _, hi in bounds.values())
    if Fraction.from_float(_as_float(a)) != a:
        raise ValueError("global lower endpoint must be exact finite binary64")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ValueError("rows must be a sequence of PlanRow records")
    validated = []
    features = {}
    for item in rows:
        if not isinstance(item, PlanRow):
            raise ValueError("every row must be a PlanRow")
        if type(item.day_id) is not int or item.day_id < 0:
            raise ValueError("day_id must be a nonnegative builtin int")
        if (type(item.situation) is not str or item.situation not in ("S1", "S2")
                or type(item.action) is not str or item.action not in ("A1", "A2")
                or type(item.feature) is not str or item.feature not in _FEATURES):
            raise ValueError("invalid row situation/action/feature")
        key = (item.situation, item.action, item.day_id)
        if key in features and features[key] != item.feature:
            raise ValueError("conflicting features for the same situation/action/day")
        features[key] = item.feature
        if item.outcome is None and item.gross_r is None:
            value = None
        else:
            if type(item.outcome) is not str or item.outcome not in _OUTCOMES:
                raise ValueError("outcome and gross_r must both be missing or visible")
            value = _number(item.gross_r)
            lo, hi = bounds[item.outcome]
            if not lo <= value <= hi:
                raise ValueError("visible return is outside its outcome support")
        validated.append((item, value))
    return validated, tuple(query), a, b


def _aggregate(rows, a: Fraction) -> _Aggregate:
    days = {}
    for item, value in rows:
        days.setdefault(item.day_id, []).append((item.outcome, value))
    counts = [0, 0, 0]
    k = [Fraction(0) for _ in _OUTCOMES]
    s = [Fraction(0) for _ in _OUTCOMES]
    total_w = Fraction(0)
    visible_days = missing = 0
    for group in days.values():
        visible = [(outcome, value) for outcome, value in group if value is not None]
        missing += len(group) - len(visible)
        total_w += sum((a if value is None else value for _, value in group),
                       Fraction(0)) / len(group)
        if not visible:
            continue
        visible_days += 1
        for index, outcome in enumerate(_OUTCOMES):
            values = [value for label, value in visible if label == outcome]
            counts[index] += len(values)
            k[index] += Fraction(len(values), len(visible))
            s[index] += sum(values, Fraction(0)) / len(visible)
    return _Aggregate(
        LayerCounts(len(rows), len(days), visible_days, tuple(counts), missing),
        tuple(k), tuple(s), total_w / len(days) if days else None,
    )


def estimate_v2a(rows: Sequence[PlanRow], *, query: tuple[str, str, str],
                 support: Mapping[str, tuple[int | float, int | float]],
                 lb_z: int | float = 1.0) -> V2AResult:
    """Estimate one target; validate the complete input before any empty return."""
    rows, query, a, b = _validate(rows, query, support, lb_z)
    situation, action, feature = query
    action_rows = [(item, value) for item, value in rows if item.action == action]
    parent_rows = [(item, value) for item, value in action_rows
                   if item.situation == situation]
    cell_rows = [(item, value) for item, value in parent_rows if item.feature == feature]
    cell = _aggregate(cell_rows, a)
    parent = _aggregate(parent_rows, a)
    pool = _aggregate(action_rows, a)
    q = v = p = r = (None, None, None)
    center = None
    selected_layer = "none"
    if pool.counts.visible_days:
        q = tuple(value / pool.counts.visible_days for value in pool.k)
        v = tuple(value / pool.counts.visible_days for value in pool.s)
        if cell.counts.visible_days >= 100:
            selected, selected_layer = cell, "cell"
        elif parent.counts.visible_days >= 100:
            selected, selected_layer = parent, "parent"
        else:
            selected, selected_layer = pool, "action"
        if selected_layer == "action":
            p = q
            r = tuple(value / weight if weight else None for value, weight in zip(v, q))
            center = sum(v, Fraction(0))
        else:
            weights = tuple(k + 100 * prior for k, prior in zip(selected.k, q))
            contributions = tuple(s + 100 * prior for s, prior in zip(selected.s, v))
            denominator = selected.counts.visible_days + 100
            p = tuple(weight / denominator for weight in weights)
            r = tuple(value / weight if weight else None
                      for value, weight in zip(contributions, weights))
            center = sum(contributions, Fraction(0)) / denominator
    lower = guard = a
    radius = None
    if cell.counts.planned_days:
        h_up = _radius_upper(b - a, cell.counts.planned_days)
        guard = max(a, cell.mean_w - h_up)
        if center is not None:
            lower = max(a, center - abs(center - cell.mean_w) - h_up)
        radius = _directed_float(h_up, upward=True)
    return V2AResult(
        lower=_directed_float(lower, upward=False),
        guard=_directed_float(guard, upward=False), radius=radius,
        mean_w=_as_float(cell.mean_w), center=_as_float(center),
        selected_layer=selected_layer, target_days=cell.counts.planned_days,
        cell=cell.counts, parent=parent.counts, action=pool.counts,
        p=tuple(map(_as_float, p)), r=tuple(map(_as_float, r)),
        q=tuple(map(_as_float, q)), v=tuple(map(_as_float, v)),
    )
