"""D61 V2-A fixed in-memory examples; no research samples or fitted truth."""
import copy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import decimal
from fractions import Fraction
from functools import lru_cache
import importlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest


SUPPORT = {"win": (0, 4), "loss": (-2, 0), "time": (-1, 1)}
QUERY = ("S1", "A2", "f0")


def kernel():
    # Keep the initial absent-module RED an assertion, not a collection error.
    assert importlib.util.find_spec("mt5quant.prob.d61_v2a") is not None
    return importlib.import_module("mt5quant.prob.d61_v2a")


def row(day=0, *, situation="S1", action="A2", feature="f0",
        outcome="win", gross_r=4):
    return kernel().PlanRow(day, situation, action, feature, outcome, gross_r)


def estimate(rows, **kwargs):
    return kernel().estimate_v2a(rows, query=kwargs.pop("query", QUERY),
                                 support=kwargs.pop("support", SUPPORT), **kwargs)


def test_action_pool_uses_global_days():
    # Day 0: (4-2-2)/3=0; day 1: 2. Row mean=.5, parent mean=4/3.
    rows = [row(), row(situation="S2", outcome="loss", gross_r=-2),
            row(situation="S2", outcome="loss", gross_r=-2), row(1, gross_r=2)]
    result = estimate(rows)
    assert result.selected_layer == "action"
    assert result.center == 1.0
    assert result.q == (float(Fraction(2, 3)), float(Fraction(1, 3)), 0.0)
    assert result.v == (float(Fraction(5, 3)), float(Fraction(-2, 3)), 0.0)
    assert result.p == result.q
    assert result.r == (2.5, -2.0, None)
    assert result.target_days == 2
    assert result.mean_w == 3.0
    assert result.action.row_count == 4
    assert result.action.planned_days == result.action.visible_days == 2
    assert result.action.outcome_counts == (2, 2, 0)
    assert result.action.missing_count == 0


def test_missing_rows_have_different_target_and_visible_denominators():
    result = estimate([row(), row(outcome=None, gross_r=None),
                       row(10**30, outcome=None, gross_r=None)])
    assert result.target_days == result.cell.planned_days == 2
    assert result.cell.visible_days == 1
    assert result.cell.row_count == 3
    assert result.cell.missing_count == 2
    assert result.cell.outcome_counts == (1, 0, 0)
    assert result.mean_w == -0.5
    assert result.center == 4.0
    assert result.p == (1.0, 0.0, 0.0)
    assert result.r == (4.0, None, None)


@pytest.mark.parametrize("cell_days,parent_extra,selected", [
    (99, 0, "action"), (99, 1, "parent"), (100, 0, "cell"),
])
def test_visible_day_gates_ignore_missing_days_and_row_counts(
        cell_days, parent_extra, selected):
    rows = [row(d, gross_r=2) for d in range(cell_days)] * 3
    rows += [row(200 + d, feature="f1", gross_r=3) for d in range(parent_extra)]
    rows += [row(1000 + d, outcome=None, gross_r=None) for d in range(101)]
    result = estimate(rows)
    assert result.selected_layer == selected
    assert result.cell.visible_days == cell_days
    assert result.parent.visible_days == cell_days + parent_extra
    assert result.target_days == cell_days + 101


def test_shrinkage_uses_day_contributions_and_knows_pool_only_outcome():
    rows = [row(d, gross_r=2) for d in range(100)]
    rows += [row(d, situation="S2", outcome="loss", gross_r=-2)
             for d in range(100)]
    result = estimate(rows)
    assert result.selected_layer == "cell"
    assert result.q == (0.5, 0.5, 0.0)
    assert result.v == (1.0, -1.0, 0.0)
    assert result.p == (0.75, 0.25, 0.0)
    assert result.r == (2.0, -2.0, None)
    assert result.center == 1.0
    assert result.mean_w == 2.0


def test_parent_shrinkage_is_not_a_mean_of_parent_centers():
    rows = [row(d, feature="f0" if d < 99 else "f1", gross_r=2)
            for d in range(100)]
    rows += [row(d, situation="S2", outcome="loss", gross_r=-2)
             for d in range(100)]
    result = estimate(rows)
    assert result.selected_layer == "parent"
    assert result.center == 1.0
    assert result.p == (0.75, 0.25, 0.0)
    assert result.r == (2.0, -2.0, None)
    assert result.target_days == 99


def test_action_pool_never_includes_other_actions():
    rows = [row(0, situation="S2", gross_r=2)]
    before = estimate(rows)
    rows += [row(d, action="A1", outcome="loss", gross_r=-2) for d in range(120)]
    assert estimate(rows) == before
    assert before.center == 2.0
    assert before.target_days == 0
    assert before.lower == before.guard == -2.0
    assert before.radius is before.mean_w is None


def test_no_visible_pool_preserves_unknown_values():
    result = estimate([row(outcome=None, gross_r=None)])
    assert result.selected_layer == "none"
    assert result.center is None
    assert result.p == result.r == result.q == result.v == (None, None, None)
    assert result.mean_w == -2.0
    assert result.lower == -2.0
    assert result.cell.visible_days == 0
    empty = estimate([])
    assert empty.target_days == 0
    assert empty.lower == empty.guard == -2.0
    assert empty.mean_w is empty.radius is None


@pytest.mark.parametrize("changes", [
    {"day_id": -1}, {"day_id": True}, {"day_id": 0.0},
    {"situation": "S3"}, {"situation": []}, {"action": "A3"},
    {"feature": "f2"}, {"outcome": "skipped"}, {"outcome": []},
    {"outcome": None}, {"gross_r": None}, {"gross_r": True},
    {"gross_r": "4"}, {"gross_r": Decimal(4)}, {"gross_r": Fraction(4)},
    {"gross_r": float("nan")}, {"gross_r": float("inf")},
    {"gross_r": -float("inf")}, {"gross_r": 5}, {"gross_r": -1},
])
def test_rejects_bad_rows_even_outside_query(changes):
    bad = replace(row(situation="S2"), **changes)
    with pytest.raises(ValueError):
        estimate([bad], query=("S1", "A1", "na"))


def test_validation_is_not_skipped_for_empty_target():
    with pytest.raises(ValueError):
        estimate([row(situation="S2", gross_r=5)])
    with pytest.raises(ValueError):
        estimate([row(), row(feature="f1")], query=("S1", "A1", "na"))
    with pytest.raises(ValueError):
        estimate([object()])
    for invalid in (None, "rows", 1, iter(())):
        with pytest.raises(ValueError):
            estimate(invalid)


@pytest.mark.parametrize("query", [None, (), ("S1", "A2"), "S1:A2:f0",
                                    ("S2", "A1", "f0"), ("S3", "A2", "f0"),
                                    ("S1", "A3", "f0"), ("S1", "A2", "f2"),
                                    ([], "A2", "f0")])
def test_rejects_invalid_queries(query):
    with pytest.raises(ValueError):
        estimate([], query=query)


@pytest.mark.parametrize("support", [None, {}, {"win": (0, 4)},
    {**SUPPORT, "extra": (0, 1)}, {**SUPPORT, "win": (4, 4)},
    {**SUPPORT, "win": (4, 0)}, {**SUPPORT, "win": (0,)},
    {**SUPPORT, "win": "04"}, {**SUPPORT, "win": (False, 4)},
    {**SUPPORT, "win": (0, float("inf"))},
    {**SUPPORT, "win": (float("nan"), 4)},
    {**SUPPORT, "win": (0, Decimal(4))},
])
def test_rejects_invalid_support_before_empty_return(support):
    with pytest.raises(ValueError):
        estimate([], support=support)


@pytest.mark.parametrize("lb_z", [0, 2, True, None, "1", Decimal(1),
                                   Fraction(1), float("nan"), float("inf")])
def test_lb_z_cannot_change_frozen_bound(lb_z):
    with pytest.raises(ValueError):
        estimate([], lb_z=lb_z)


@pytest.mark.parametrize("a", [2**53 + 1, -(2**53 + 1), -(2**2000)])
def test_global_lower_endpoint_must_be_exact_finite_binary64(a):
    with pytest.raises(ValueError):
        estimate([], support={o: (a, a + 2**2001) for o in SUPPORT})


def test_large_exact_lower_endpoint_and_other_inexact_endpoints_are_allowed():
    a = 2**100
    support = {o: (a, a + 3) for o in SUPPORT}
    assert estimate([], support=support).lower == float(a)
    assert estimate([row(gross_r=a + 1)], support=support).center == float(a)
    # Other lower endpoints need not be binary64-exact; upper can exceed float.
    support = {"win": (2**53 + 1, 2**2000), "loss": (-2, 0), "time": (-1, 1)}
    assert estimate([], support=support).lower == -2.0


def test_labels_are_categories_not_inferred_from_return_sign():
    result = estimate([row(outcome="loss", gross_r=1)],
                      support={o: (-2, 4) for o in SUPPORT})
    assert result.p == (0.0, 1.0, 0.0)
    assert result.r == (None, 1.0, None)


def test_records_support_and_frozen_results_are_unchanged():
    rows = [row(), row(1, gross_r=2)]
    support = {k: list(v) for k, v in SUPPORT.items()}
    original = copy.deepcopy((rows, support))
    result = estimate(rows, support=support)
    assert (rows, support) == original
    with pytest.raises(FrozenInstanceError):
        rows[0].gross_r = 0
    with pytest.raises(FrozenInstanceError):
        result.lower = 0
    with pytest.raises(FrozenInstanceError):
        result.cell.row_count = 0


def _log_interval(x, terms=120):
    """ln(x)=2 sum z^(2k+1)/(2k+1), with a positive geometric tail bound."""
    z = (x - 1) / (x + 1)
    partial = 2 * sum((z ** (2*k + 1) / (2*k + 1) for k in range(terms)),
                      Fraction(0))
    remainder = 2 * z ** (2*terms + 1) / ((2*terms + 1) * (1 - z*z))
    return partial, partial + remainder


@lru_cache(maxsize=1)
def _ln900_interval():
    two_lo, two_hi = _log_interval(Fraction(2))
    ratio_lo, ratio_hi = _log_interval(Fraction(15, 8))
    return 8*two_lo + 2*ratio_lo, 8*two_hi + 2*ratio_hi


def _sqrt_interval(x, bits=420):
    """Dyadic enclosure from integer square root, certified by exact squares."""
    scaled_floor = (x.numerator << (2*bits)) // x.denominator
    floor = math.isqrt(scaled_floor)
    lo, hi = Fraction(floor, 2**bits), Fraction(floor + 1, 2**bits)
    assert lo*lo <= x < hi*hi
    return lo, hi


@lru_cache(maxsize=None)
def _ideal_radius_interval(width, days):
    log_lo, log_hi = _ln900_interval()
    sqrt_lo, _ = _sqrt_interval(log_lo / (2*days))
    _, sqrt_hi = _sqrt_interval(log_hi / (2*days))
    return width*sqrt_lo, width*sqrt_hi


def _certify(result, *, a, width, mean_w, center):
    """An upper radius proves an independently lower ideal-bound endpoint."""
    h_lo, h_hi = _ideal_radius_interval(Fraction(width), result.target_days)
    assert result.radius is not None
    radius = Fraction(result.radius)
    assert radius >= h_hi >= h_lo > 0
    log_hi = _ln900_interval()[1]
    assert (radius / width)**2 * (2*result.target_days) >= log_hi
    guard_lo = max(Fraction(a), Fraction(mean_w) - h_hi)
    lower_lo = (Fraction(a) if center is None else
                max(Fraction(a), center - abs(center - mean_w) - h_hi))
    assert Fraction(result.guard) <= guard_lo
    assert Fraction(result.lower) <= lower_lo
    assert a <= result.lower <= result.guard
    return h_lo, h_hi


def test_bound_positive_control_matches_fixed_example():
    rows = [row(d, feature="f0" if d < 800 else "f1", gross_r=1.90)
            for d in range(1600)]
    for feature in ("f0", "f1"):
        result = estimate(rows, query=("S1", "A2", feature))
        assert result.target_days == 800
        assert result.lower >= 0.10
        assert result.radius == 0.39122101448515906
        assert result.lower == result.guard == 1.5087789855148408
        _certify(result, a=-2, width=6, mean_w=Fraction(1.90), center=Fraction(1.90))
    empty = estimate(rows, query=("S1", "A2", "na"))
    assert empty.lower == empty.guard == -2.0
    assert empty.radius is empty.mean_w is None
    assert empty.center == 1.9


@pytest.mark.parametrize("days", [1, 2, 99, 100, 800, 1600])
def test_bound_fraction_radius_has_independent_rational_certificate(days):
    # Directly certify the private rational used by lower/guard, not only its float.
    radius_upper = getattr(kernel(), "_radius_upper", None)
    assert callable(radius_upper), "the exact radius enclosure is not implemented"
    h = radius_upper(Fraction(6), days)
    assert isinstance(h, Fraction)
    _, upper = _ideal_radius_interval(Fraction(6), days)
    assert h >= upper
    assert (h / 6)**2 * (2*days) >= _ln900_interval()[1]
    assert h - upper < Fraction(1, 10**75)


def test_bound_is_computed_even_when_every_target_row_is_missing():
    result = estimate([row(d, outcome=None, gross_r=None) for d in range(100)])
    assert result.center is None
    assert result.mean_w == -2.0
    assert result.radius is not None and result.radius > 0
    assert result.lower == result.guard == -2.0
    _certify(result, a=-2, width=6, mean_w=Fraction(-2), center=None)


@pytest.mark.parametrize("outcome,value", [("loss", -1), ("time", 0), ("win", 4)])
def test_bound_legal_controls_and_clipping(outcome, value):
    result = estimate([row(d, outcome=outcome, gross_r=value) for d in range(800)])
    _certify(result, a=-2, width=6, mean_w=Fraction(value), center=Fraction(value))
    if outcome == "loss":
        assert result.lower < 0.10
    assert result.radius > 0
    one = estimate([row(outcome=outcome, gross_r=value)])
    _certify(one, a=-2, width=6, mean_w=Fraction(value), center=Fraction(value))
    assert one.lower == one.guard == -2.0


def test_bound_shrinkage_uses_exact_center_below_target_mean():
    rows = [row(d, gross_r=2) for d in range(100)]
    rows += [row(d, situation="S2", outcome="loss", gross_r=-2)
             for d in range(100)]
    result = estimate(rows)
    _certify(result, a=-2, width=6, mean_w=Fraction(2), center=Fraction(1))
    assert result.lower < result.guard


def test_bound_empty_and_missing_targets_do_not_borrow_pool_sample_size():
    rows = [row(d, situation="S2", gross_r=4) for d in range(800)]
    empty = estimate(rows)
    assert empty.lower == empty.guard == -2.0
    assert empty.radius is empty.mean_w is None
    rows += [row(5, outcome=None, gross_r=None)]
    missing = estimate(rows)
    assert missing.target_days == 1
    assert missing.mean_w == -2.0
    assert missing.radius > 7
    assert missing.lower == missing.guard == -2.0


def _float_bits(result):
    values = (result.lower, result.guard, result.radius, result.mean_w, result.center,
              *result.p, *result.r, *result.q, *result.v)
    return tuple(None if value is None else value.hex() for value in values)


@pytest.mark.parametrize("copies", [1, 3, 10])
def test_complete_global_day_replication_and_permutation_are_bitwise_invariant(copies):
    rows = [row(d, gross_r=2) for d in range(100)]
    rows += [row(d, situation="S2", outcome="loss", gross_r=-2) for d in range(100)]
    rows += [row(d, outcome=None, gross_r=None) for d in range(0, 100, 2)]
    rows += [row(d, feature="f1", outcome="time", gross_r=0.5)
             for d in range(100, 150)]
    rows += [row(d, action="A1", gross_r=4) for d in range(150)]
    before = estimate(rows)
    copied = rows * copies
    # Deterministic permutation; never use a random generator.
    copied = copied[1::2][::-1] + copied[::2][::-1]
    after = estimate(copied)
    assert _float_bits(before) == _float_bits(after)
    assert before.target_days == after.target_days == 100
    assert before.selected_layer == after.selected_layer == "cell"
    for layer in ("cell", "parent", "action"):
        a, b = getattr(before, layer), getattr(after, layer)
        assert b.row_count == copies * a.row_count
        assert b.missing_count == copies * a.missing_count
        assert b.outcome_counts == tuple(copies * n for n in a.outcome_counts)
        assert b.planned_days == a.planned_days
        assert b.visible_days == a.visible_days


def test_pool_contamination_cannot_change_target_evidence_or_exceed_guard():
    target = [row(d, gross_r=2) for d in range(100)]
    base = estimate(target)
    polluted = estimate(target + [row(d, situation="S2", outcome="loss", gross_r=-2)
                                  for d in range(100)])
    raised = estimate(target + [row(d, situation="S2", gross_r=4) for d in range(100)])
    assert polluted.center < base.center < raised.center
    assert polluted.lower < base.lower == raised.lower
    for result in (polluted, raised):
        assert result.target_days == base.target_days
        assert result.cell == base.cell
        assert result.mean_w.hex() == base.mean_w.hex()
        assert result.radius.hex() == base.radius.hex()
        assert result.guard.hex() == base.guard.hex()
        assert result.lower <= result.guard


def test_bound_near_point_one_has_no_opening_epsilon():
    # These adjacent binary64 returns straddle the ideal 0.10 + h threshold.
    above = 0.49122101448515904
    below = math.nextafter(above, -math.inf)
    h_lo, h_hi = _ideal_radius_interval(Fraction(6), 800)
    assert Fraction(below) - h_lo < Fraction(0.10)
    assert Fraction(above) - h_hi > Fraction(0.10)
    low = estimate([row(d, gross_r=below) for d in range(800)])
    high = estimate([row(d, gross_r=above) for d in range(800)])
    for value, result in ((below, low), (above, high)):
        _certify(result, a=-2, width=6, mean_w=Fraction(value), center=Fraction(value))
    assert low.lower < 0.10 <= high.lower


@pytest.mark.parametrize("outcome,multiple,expected", [("win", 2, 1), ("loss", -1, -2)])
def test_bound_outward_rounding_covers_subnormal_values(outcome, multiple, expected):
    unit = math.ulp(0.0)
    support = {"win": (0, 4*unit), "loss": (-2*unit, 0), "time": (-unit, unit)}
    value = multiple*unit
    result = estimate([row(d, outcome=outcome, gross_r=value) for d in range(800)],
                      support=support)
    _certify(result, a=-2*unit, width=Fraction(6)*Fraction(unit),
             mean_w=Fraction(value), center=Fraction(value))
    assert result.radius == unit
    assert result.lower == result.guard == expected*unit


def test_bound_diagnostics_do_not_round_exact_integers_before_arithmetic():
    a = 2**53
    result = estimate([row(d, gross_r=a + 3) for d in range(800)],
                      support={o: (a, a + 6) for o in SUPPORT})
    assert result.center == float(a + 4)  # nearest, for display only
    assert result.lower == result.guard == float(a + 2)
    _certify(result, a=a, width=6, mean_w=Fraction(a + 3), center=Fraction(a + 3))


@pytest.mark.parametrize("case", ["radius", "center", "wide"])
def test_bound_nonfinite_outputs_raise_instead_of_clipping(case):
    largest = sys.float_info.max
    if case == "radius":
        support = {o: (-largest, largest) for o in SUPPORT}
        rows = [row(gross_r=0)]
    else:
        support = {o: (0, 2**2000) for o in SUPPORT}
        rows = [row(gross_r=2**1999, situation="S2" if case == "center" else "S1")]
    with pytest.raises(ValueError):
        estimate(rows, support=support)


def test_selected_cell_uses_shared_visible_denominators_for_all_outcomes():
    rows = []
    for day in range(100):
        if day % 2 == 0:
            rows += [row(day), row(day, outcome="loss", gross_r=-2)]
        else:
            rows += [row(day, outcome="time", gross_r=1)]
        rows += [row(day, situation="S2", outcome="loss", gross_r=-2)]
    result = estimate(rows)
    assert result.selected_layer == "cell"
    assert result.q == (float(Fraction(1, 6)), float(Fraction(7, 12)), 0.25)
    assert result.v == (float(Fraction(2, 3)), float(Fraction(-7, 6)), 0.25)
    assert result.p == (float(Fraction(5, 24)), float(Fraction(5, 12)), 0.375)
    assert result.r == (4.0, -2.0, 1.0)
    assert result.mean_w == 1.0
    assert result.center == 0.375
    _certify(result, a=-2, width=6, mean_w=Fraction(1), center=Fraction(3, 8))


def test_finite_outputs_allow_support_width_exceeding_binary64():
    largest = sys.float_info.max
    result = estimate([row(d, gross_r=0) for d in range(800)],
                      support={o: (-largest, largest) for o in SUPPORT})
    _certify(result, a=-largest, width=2*Fraction(largest),
             mean_w=Fraction(0), center=Fraction(0))
    assert math.isfinite(result.radius)
    # Even the upper endpoint may exceed finite binary64 if outputs remain finite.
    result = estimate([row(d, gross_r=1) for d in range(800)],
                      support={o: (-2, 2**1024) for o in SUPPORT})
    _certify(result, a=-2, width=Fraction(2**1024 + 2),
             mean_w=Fraction(1), center=Fraction(1))
    assert math.isfinite(result.radius)
    assert result.lower == -2.0


def _context_state(context):
    return (context.prec, context.rounding, context.Emin, context.Emax,
            context.capitals, context.clamp, dict(context.traps), dict(context.flags))


def test_bound_ignores_external_decimal_context_and_does_not_mutate_it():
    rows = [row(d, gross_r=1.9) for d in range(800)]
    expected = estimate(rows)
    saved = decimal.getcontext().copy()
    saved_default = decimal.DefaultContext.copy()
    try:
        for context in (decimal.getcontext(), decimal.DefaultContext):
            context.prec = 3
            context.rounding = decimal.ROUND_FLOOR
            context.Emin, context.Emax, context.clamp, context.capitals = -9, 9, 1, 0
            for signal in context.traps:
                context.traps[signal] = True
                context.flags[signal] = True
        current_state = _context_state(decimal.getcontext())
        default_state = _context_state(decimal.DefaultContext)
        assert _float_bits(estimate(rows)) == _float_bits(expected)
        assert _context_state(decimal.getcontext()) == current_state
        assert _context_state(decimal.DefaultContext) == default_state
    finally:
        decimal.setcontext(saved)
        for name in ("prec", "rounding", "Emin", "Emax", "capitals", "clamp"):
            setattr(decimal.DefaultContext, name, getattr(saved_default, name))
        decimal.DefaultContext.traps = saved_default.traps.copy()
        decimal.DefaultContext.flags = saved_default.flags.copy()


def test_cold_import_and_call_allow_only_python_code_and_no_business_io():
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = r'''
import importlib.abc
import json
from pathlib import Path
import sys

src = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(src))
allowed_names = {"mt5quant", "mt5quant.prob", "mt5quant.prob.d61_v2a"}
allowed_files = {
    src / "mt5quant" / "__init__.py",
    src / "mt5quant" / "prob" / "__init__.py",
    src / "mt5quant" / "prob" / "d61_v2a.py",
}
stdlib = Path(sys.base_prefix).resolve()
assert not any(name.startswith("mt5quant") for name in sys.modules)

class ImportFence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in allowed_names or fullname.split(".")[0] in sys.stdlib_module_names:
            return None
        raise AssertionError("forbidden import: " + fullname)

sys.meta_path.insert(0, ImportFence())

def audit(event, args):
    if event == "open":
        path, mode, flags = args
        if not isinstance(path, (str, bytes)):
            raise AssertionError("business descriptor access")
        file = Path(path).resolve()
        read_only = not (flags & (1 | 2 | 64 | 512 | 1024))
        python_code = file.suffix.lower() in (".py", ".pyc", ".pyd", ".dll", ".zip")
        import_path = file in allowed_files or stdlib in file.parents
        # Importlib may look for the three modules' existing bytecode.
        cached = (file.suffix == ".pyc" and file.parent.name == "__pycache__"
                  and any(file.parent.parent == p.parent and
                          file.name.startswith(p.stem + ".") for p in allowed_files))
        if not read_only or not python_code or not (import_path or cached):
            raise AssertionError("business file access: " + str(file))
    if (event.startswith(("socket.", "subprocess.", "ctypes."))
            or event in ("os.system", "os.startfile", "os.posix_spawn")):
        raise AssertionError("external operation: " + event)

sys.addaudithook(audit)
from mt5quant.prob.d61_v2a import PlanRow, estimate_v2a
rows = [PlanRow(d, "S1", "A2", "f0", "win", 1.9) for d in range(800)]
result = estimate_v2a(rows, query=("S1", "A2", "f0"),
                      support={"win": (0, 4), "loss": (-2, 0), "time": (-1, 1)})
assert result.lower >= 0.10
loaded = sorted(name for name in sys.modules if name.startswith("mt5quant"))
assert set(loaded) == allowed_names
print(json.dumps({"lower": result.lower, "radius": result.radius, "modules": loaded}))
'''
    run = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", script,
                          str(source_root)], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    payload = json.loads(run.stdout)
    assert payload["lower"] == 1.5087789855148408
    assert payload["radius"] == 0.39122101448515906
    assert payload["modules"] == ["mt5quant", "mt5quant.prob", "mt5quant.prob.d61_v2a"]
