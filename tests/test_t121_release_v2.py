"""t121-release-v2 preconditions: USDJPY pool, watermark binding, mixed kinds.

Ruling 2026-11-06 (conditional approved, option II; coding 2a/2b/2d approved
with conditions) delegates this exact code surface. Every test maps to one
ruling condition:

- admissible pool {XAUUSD, EURUSD, USDJPY}; per-manifest declared pool,
  costs keys == pool, pool subset of the full set; v1 manifests unchanged;
- measured observation windows: v1 discovery-bounded windows keep their
  acceptance; the t121-release-v2 binding path admits exactly the per-symbol
  2026 model watermark window (borrowing another symbol's window or a 1ms
  excess is refused);
- adapter/kernel double layer: kernel envelope == min lo / max hi of the
  per-symbol windows; waiting reason string stays "evidence_range";
- scenario overshoot basis "scenario_multiple_of_spread_p90" whitelisted;
- run_production observed aggregation only over measured components (mixed
  kinds carry observed=null and previously broke the min/max aggregation);
- rules: optional per-release grids (frozen two-symbol entries byte-frozen)
  and version binding path "t121-offline-v1-usdjpy" with the v2 prereg blob.

Isolated synthetic fixtures only; no research store, prices or MT5 involved.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from mt5quant.prob import production_adapter as adapter
from mt5quant.prob import feasibility
from mt5quant.prob.feasibility import RULES

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
START = "2015-01-05T00:00:00Z"
END = "2016-05-01T00:00:00Z"
DISCOVERY_START = "1971-01-03T22:00:00Z"
PARTS = ("spread", "slippage", "commission", "contract", "overshoot", "swap")
BASES = {"spread": "absolute", "slippage": "spread-fraction-v1",
         "commission": "round-trip-per-lot", "contract": "size-per-lot",
         "overshoot": "stop-level-v2", "swap": "free"}
LEGACY_WINDOW = ("2014-01-01T00:00:00Z", "2015-01-01T00:00:00Z")
USDJPY_GRID = 0.5  # §1-bis S1 anchor derivation: 125 x 0.4% -> 0.5
# Exact bound-artifact 2026 model watermark windows (milliseconds).
# XAUUSD/EURUSD: data_research/costs/spread_model.json watermarks
# (msc 1767308700025/1793204266005, 1767312001291/1793204130156);
# USDJPY: usdjpy_spread_component.json watermarks (msc 1767312000099/1793966625848).
WATERMARKS = {
    "XAUUSD": ("2026-01-01T23:05:00.025Z", "2026-10-28T16:17:46.005Z"),
    "EURUSD": ("2026-01-02T00:00:01.291Z", "2026-10-28T16:15:30.156Z"),
    "USDJPY": ("2026-01-02T00:00:00.099Z", "2026-11-06T12:03:45.848Z"),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def ref(name, *, format="json"):
    return {"artifact_id": name.replace(".", "-"), "artifact_version": "fixture-v1",
            "path": name if "." in name else name + ".json", "format": format,
            "digest_domain": "raw-bytes", "sha256": "0" * 64}


def pool_grids(pool):
    return {symbol: {"XAUUSD": 5., "EURUSD": .005}.get(symbol, USDJPY_GRID) for symbol in pool}


def part(symbol, name, *, kind=None, observed=None, basis=None, **overrides):
    value = {"artifact_ref": ref(f"{symbol}-{name}"), "source_ref": ref(f"{symbol}-{name}"),
             "component": name, "symbol": symbol, "purpose": adapter.PURPOSE,
             "account_scope_id": "fixture-account-scope", "account_purpose": "Demo", "currency": "USD",
             "observed_start": LEGACY_WINDOW[0], "observed_end_exclusive": LEGACY_WINDOW[1],
             "applies_start": START, "applies_end_exclusive": END, "basis": basis or BASES[name],
             "units": "fixture-explicit-units", "conversion_basis": "quote-usd-v1", "dependencies": {}}
    if kind not in (None, "measured"):
        value["observed_start"] = value["observed_end_exclusive"] = None
        value["evidence_refs"] = ["account-fee-terms-2026-11-06"]
        if kind == "scenario":
            value["multiplier"] = 1.0
    if kind is not None:
        value["observation_kind"] = kind
    if observed is not None:
        value["observed_start"], value["observed_end_exclusive"] = observed
    value.update(overrides)
    return value


def rules_block(*, version=RULES["version"], grids=None, prereg=None, sha_value=None):
    effective = {**RULES, "version": version}
    if grids is not None:
        effective["grids"] = grids
    prereg = prereg or (getattr(adapter, "PREREG_V2_SHA256", None)
                        if version != RULES["version"] else None) or adapter.PREREG_SHA256
    value = {"version": version, "digest_domain": "canonical-json-v1",
             "sha256": sha(encoded(effective)) if sha_value is None else sha_value,
             "prereg_sha256": prereg, "prereg_digest_domain": "git-blob-bytes"}
    if grids is not None:
        value["grids"] = grids
    return value


def build_manifest(pool, *, mode="v2"):
    """v1: all implicit measured legacy windows, v1 rules.
    v1-mixed: commission/swap declared_terms (observed=null), rest as v1.
    v2: per §1-bis — watermark measured spread/slippage, declared commission/
    swap, spec_snapshot contract, scenario overshoot, usdjpy rules+grids."""
    costs = {}
    for symbol in pool:
        if mode == "v2":
            window = WATERMARKS[symbol]
            costs[symbol] = {
                "spread": part(symbol, "spread", kind="measured", observed=window),
                "slippage": part(symbol, "slippage", kind="measured", observed=window),
                "commission": part(symbol, "commission", kind="declared_terms"),
                "swap": part(symbol, "swap", kind="declared_terms"),
                "contract": part(symbol, "contract", kind="spec_snapshot"),
                "overshoot": part(symbol, "overshoot", kind="scenario",
                                  basis="scenario_multiple_of_spread_p90"),
            }
        else:
            costs[symbol] = {name: part(symbol, name) for name in PARTS}
            if mode == "v1-mixed":
                costs[symbol]["commission"] = part(symbol, "commission", kind="declared_terms")
                costs[symbol]["swap"] = part(symbol, "swap", kind="declared_terms")
        for name, deps in {"slippage": ("spread",), "commission": ("contract",), "swap": ("contract",)}.items():
            costs[symbol][name]["dependencies"] = {d: copy.deepcopy(costs[symbol][d]["artifact_ref"]) for d in deps}
    scope = {"scope_id": "fixture-count-scope", "purpose": adapter.PURPOSE,
             "account_scope_id": "fixture-account-scope", "account_purpose": "Demo",
             "currency": "USD", "quote_currency": "USD", "symbols": list(pool),
             "source_timeframe": "H1", "target_timeframes": ["H1", "H4"],
             "range_start": START, "range_end_exclusive": END,
             "discovery_start": DISCOVERY_START if mode == "v2" else START,
             "discovery_end_exclusive": END}
    rules = (rules_block() if mode != "v2" else
             rules_block(version="t121-offline-v1-usdjpy", grids=pool_grids(pool)))
    return {"schema": "t121-release-v1", "release_id": "fixture-release",
            "execution_id": "fixture-execution", "snapshot_version": 1,
            "valid_from": "2026-11-05T00:00:00Z", "expires_at": "2026-11-07T00:00:00Z",
            "scope": scope, "input_ref": ref("input"), "source_manifest_ref": ref("sources"),
            "sessions_ref": ref("sessions.toml", format="toml"), "review_ref": ref("review"),
            "costs": costs, "rules": rules,
            "code": {"repo_commit": "0" * 40,
                     "files": {name: {"raw_sha256": "0" * 64, "git_blob_sha256": "0" * 64}
                               for name in adapter.CODE_FILES}},
            "conditions": []}


@pytest.fixture
def validate(monkeypatch):
    monkeypatch.setattr(adapter, "_implementation", lambda m: {"head": "0" * 40})
    snapshot = SimpleNamespace(version=1)

    def run(document):
        adapter._validate_manifest(document, snapshot, "fixture-release", NOW)
        return True
    return run


# Ruling condition: constants exact (tightening rounding only if ever needed)
# and the watermark key set is closed over the full admissible pool.
def test_watermark_constants_exact_and_key_set_closed():
    assert set(adapter.SYMBOLS) == {"XAUUSD", "EURUSD", "USDJPY"}
    windows = adapter.MODEL_WATERMARK_WINDOWS
    assert set(windows) == set(adapter.SYMBOLS), "watermark key set must be the full pool"
    for symbol, (lo, hi) in WATERMARKS.items():
        expected = tuple(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (lo, hi))
        assert windows[symbol] == expected, symbol


# Ruling condition: kernel outer envelope == min lo / max hi of per-symbol
# windows (double layer with the adapter's exact per-symbol binding).
def test_kernel_envelope_locked_to_per_symbol_windows():
    lo = min(w[0] for w in adapter.MODEL_WATERMARK_WINDOWS.values())
    hi = max(w[1] for w in adapter.MODEL_WATERMARK_WINDOWS.values())
    assert feasibility.WATERMARK_ENVELOPE == (pd.Timestamp(lo.replace(tzinfo=None)),
                                              pd.Timestamp(hi.replace(tzinfo=None)))


def test_measured_watermark_window_binding(validate):
    for symbol in ("USDJPY", "EURUSD", "XAUUSD"):
        assert validate(build_manifest((symbol,)))  # exact per-symbol watermark window
    # v1 discovery-bounded windows stay acceptable, also for explicit measured.
    legacy = build_manifest(("USDJPY",))
    legacy["costs"]["USDJPY"]["spread"].update(
        observation_kind="measured", observed_start=LEGACY_WINDOW[0], observed_end_exclusive=LEGACY_WINDOW[1])
    assert validate(legacy)


def test_measured_watermark_borrowing_and_one_ms_excess_refused(validate):
    # Borrowing another symbol's watermark window (EURUSD's window declared
    # for USDJPY) must be refused although it lies inside USDJPY's window.
    borrow = build_manifest(("USDJPY",))
    borrow["costs"]["USDJPY"]["spread"].update(
        observed_start=WATERMARKS["EURUSD"][0], observed_end_exclusive=WATERMARKS["EURUSD"][1])
    with pytest.raises(adapter.AdmissionRefused):
        validate(borrow)
    # Exceeding the watermark tail or head by exactly 1ms is refused.
    for field, value in (("observed_end_exclusive", "2026-11-06T12:03:45.849Z"),
                         ("observed_start", "2026-01-02T00:00:00.098Z")):
        drift = build_manifest(("USDJPY",))
        drift["costs"]["USDJPY"]["spread"][field] = value
        with pytest.raises(adapter.AdmissionRefused):
            validate(drift)


def test_scenario_multiple_of_spread_p90_is_whitelisted(validate):
    assert validate(build_manifest(("USDJPY",)))  # basis scenario_multiple_of_spread_p90
    bad_basis = build_manifest(("USDJPY",))
    bad_basis["costs"]["USDJPY"]["overshoot"]["basis"] = "stop-level-v2-scenario"
    with pytest.raises(adapter.AdmissionRefused, match="unreviewed component basis"):
        validate(bad_basis)
    bad_multiplier = build_manifest(("USDJPY",))
    bad_multiplier["costs"]["USDJPY"]["overshoot"]["multiplier"] = 0
    with pytest.raises(adapter.AdmissionRefused):
        validate(bad_multiplier)


def test_pool_parameterization_and_costs_key_set(validate):
    assert validate(build_manifest(("USDJPY",)))                      # single-symbol pool
    assert validate(build_manifest(("EURUSD", "USDJPY", "XAUUSD")))   # any subset, any order
    for symbols in (["USTEC"], ["USDJPY", "USDJPY"], [], "USDJPY", ["xauusd"]):
        bad = build_manifest(("USDJPY",))
        bad["scope"]["symbols"] = symbols
        with pytest.raises(adapter.AdmissionRefused):
            validate(bad)
    # costs keys must equal the declared pool exactly.
    wrong_costs = build_manifest(("USDJPY",))
    wrong_costs["costs"] = build_manifest(("XAUUSD", "EURUSD"), mode="v1")["costs"]
    with pytest.raises(adapter.AdmissionRefused):
        validate(wrong_costs)
    missing = build_manifest(("EURUSD", "USDJPY", "XAUUSD"))
    del missing["costs"]["USDJPY"]
    with pytest.raises(adapter.AdmissionRefused):
        validate(missing)


def test_rules_grids_and_version_binding(validate):
    assert validate(build_manifest(("USDJPY",)))  # usdjpy path with per-release grids
    assert validate(build_manifest(("XAUUSD", "EURUSD"), mode="v1"))  # v1 rules block unchanged
    three = build_manifest(("XAUUSD", "EURUSD", "USDJPY"))
    assert validate(three)  # frozen entries for the two plus per-release USDJPY

    def refuse(mutate, *, pool=("USDJPY",)):
        document = build_manifest(pool)
        mutate(document)
        with pytest.raises(adapter.AdmissionRefused):
            validate(document)

    refuse(lambda d: d["rules"].update(version="t121-offline-v2"))
    refuse(lambda d: d["rules"].update(grids={"USDJPY": 0.}, sha256=None))
    refuse(lambda d: d["rules"].update(grids={"XAUUSD": 4.}, sha256=None))
    refuse(lambda d: d["rules"].update(sha256="0" * 64))
    refuse(lambda d: d["rules"].update(prereg_sha256=adapter.PREREG_SHA256))
    refuse(lambda d: d["rules"].update(digest_domain="raw-bytes"))
    refuse(lambda d: d["rules"].update(grids={"USDJPY": "0.5"}, sha256=None))
    refuse(lambda d: d["rules"].pop("digest_domain"))
    # A declared pool symbol without any grid step cannot run silently empty.
    refuse(lambda d: d["rules"].update(grids={"XAUUSD": 5.}, sha256=None))
    # A stale canonical sha over the merged (rather than per-release) grids is
    # refused: the manifest must recompute over exactly its own grids.
    def stale(d):
        merged = {**RULES["grids"], **d["rules"]["grids"]}
        d["rules"]["sha256"] = sha(encoded({**RULES, "version": d["rules"]["version"], "grids": merged}))
    refuse(stale)


# Registration review R2 (docs/reviews/2026-11-07-t121-release-v2-registration-
# review.md): when a manifest carries per-release grids, its key set must equal
# the declared pool exactly (previously a SYMBOLS-superset of the pool was
# accepted, production_adapter.py:435/444). The tightening is a precondition
# for any future per-release-grids manifest registration; v1 (no grids key)
# is untouched.
def test_registration_review_r2_grids_keys_must_equal_declared_pool(validate):
    equal_one = build_manifest(("USDJPY",))
    assert validate(equal_one)  # grids keys == pool (single symbol)
    equal_three = build_manifest(("XAUUSD", "EURUSD", "USDJPY"))
    assert validate(equal_three)  # grids keys == pool (frozen two + per-release USDJPY)

    # Superset of the pool (two extra SYMBOLS members with frozen-valid steps
    # and a correctly recomputed canonical sha) must be refused; the refusal
    # reason names both key-set differences (grids-only symbols; empty
    # pool-only side — USDJPY is in both sets and is not a difference).
    superset = build_manifest(("USDJPY",))
    superset["rules"] = rules_block(version="t121-offline-v1-usdjpy",
                                    grids={**RULES["grids"], "USDJPY": USDJPY_GRID})
    with pytest.raises(adapter.AdmissionRefused) as excinfo:
        validate(superset)
    message = str(excinfo.value)
    assert "grids" in message
    for side in ("grids-only", "pool-only"):
        assert side in message
    for symbol in ("XAUUSD", "EURUSD"):  # grids-only difference
        assert symbol in message
    assert "[]" in message  # the empty pool-only difference is shown explicitly

    # A pool member without any grid step stays refused (pre-R2 behavior:
    # "release pool lacks grid steps" fail-closed) — with the same
    # difference-naming form: here the pool-only side is non-empty.
    missing = build_manifest(("USDJPY",))
    missing["rules"] = rules_block(version="t121-offline-v1-usdjpy",
                                   grids={"XAUUSD": 5.})
    with pytest.raises(adapter.AdmissionRefused) as excinfo:
        validate(missing)
    missing_message = str(excinfo.value)
    assert "pool-only" in missing_message and "USDJPY" in missing_message
    assert "grids-only" in missing_message and "XAUUSD" in missing_message


# R2 (c): v1 path zero change — no grids key means no pool-key-set coupling to
# the rules block at all; the frozen v1 pool stays accepted and a pool without
# frozen grid steps stays refused exactly as before.
def test_registration_review_r2_v1_manifests_without_grids_unchanged(validate):
    assert validate(build_manifest(("XAUUSD", "EURUSD"), mode="v1"))
    no_frozen_step = build_manifest(("USDJPY",), mode="v1")
    with pytest.raises(adapter.AdmissionRefused):
        validate(no_frozen_step)


def test_v1_manifest_acceptance_set_unchanged(validate):
    # Legacy v1 manifest (implicit measured, 2014->2015 windows, v1 pool and
    # rules) is accepted with identical semantics; its historical rejections
    # (observed past cutoff, unreviewed basis, non-pool symbol) still refuse.
    document = build_manifest(("XAUUSD", "EURUSD"), mode="v1")
    assert validate(document)
    outside = build_manifest(("XAUUSD", "EURUSD"), mode="v1")
    outside["costs"]["XAUUSD"]["spread"]["observed_end_exclusive"] = "2016-05-02T00:00:00Z"
    with pytest.raises(adapter.AdmissionRefused):
        validate(outside)
    unreviewed = build_manifest(("XAUUSD", "EURUSD"), mode="v1")
    unreviewed["costs"]["XAUUSD"]["swap"]["basis"] = "charged"
    with pytest.raises(adapter.AdmissionRefused):
        validate(unreviewed)
    foreign = build_manifest(("XAUUSD", "EURUSD"), mode="v1")
    foreign["scope"]["symbols"] = ["USTEC"]
    with pytest.raises(adapter.AdmissionRefused):
        validate(foreign)


def test_frozen_grid_entries_are_byte_identical():
    # XAUUSD/EURUSD grid steps stay byte-identical inside the frozen RULES.
    frozen = json.dumps({"EURUSD": RULES["grids"]["EURUSD"], "XAUUSD": RULES["grids"]["XAUUSD"]},
                        sort_keys=True, separators=(",", ":")).encode()
    assert frozen == b'{"EURUSD":0.005,"XAUUSD":5.0}'
    assert type(RULES["grids"]["XAUUSD"]) is float and type(RULES["grids"]["EURUSD"]) is float


# Kernel layer: envelope containment, identical waiting reason, applies chain
# and legacy pre-cutoff windows untouched.
def kernel_evidence(observed, **over):
    payload = {
        "spread": {"mode": "absolute", "hours": {"6": {"p90": .1, "n_days": 20}}},
        "slippage": {"spread_fraction": .1, "fixed_price": 0., "event_hours": [], "event_multiplier": 1.},
        "overshoot": {"version": 2, "basis": "stop_level", "hours": {"6": {"p90": .001, "n_days": 20}}},
        "commission": {"round_trip_per_lot": 2., "basis": "explicit_fee_schedule"},
        "contract": {"size": 100000., "unit": "base-unit"},
        "swap": {"basis": "free", "scope": "Demo"},
    }
    evidence = {"kind": "synthetic", "purpose": "T121-count-cost", "symbol": "USDJPY",
                "account_kind": "Demo", "account_currency": "USD", "quote_currency": "USD",
                "basis_ref": "synthetic-fixture-only", "applies_start": "2000-01-01",
                "applies_end": "2016-05-01", "observed_start": observed[0],
                "observed_end": observed[1], "payload": payload}
    evidence["payload_sha256"] = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=True).encode()).hexdigest()
    kw = dict(symbol="USDJPY", signal_time=pd.Timestamp("2015-01-05 10:00"),
              entry_time=pd.Timestamp("2015-01-05 11:00"), exit_time=pd.Timestamp("2015-01-06 11:00"),
              side=1, source="mt5", signal_price=100., atr=10., sl_atr=1)
    kw.update(over)
    return feasibility.trigger_cost(evidence=evidence, **kw)


def test_kernel_accepts_watermark_windows_inside_envelope():
    # The 2026 watermark window (USDJPY) is inside the three-artifact envelope.
    out = kernel_evidence(WATERMARKS["USDJPY"])
    assert out["cost_r"] is not None and out["cost_r"] > 0
    assert "evidence_range" not in out["waiting_reasons"]


@pytest.mark.parametrize("observed", [
    ("2026-01-02T00:00:00.099Z", "2026-11-06T12:03:45.849Z"),  # tail + 1ms
    ("2026-01-01T23:05:00.024Z", "2026-11-06T12:03:45.848Z"),  # head - 1ms
    ("2014-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),          # pre-envelope, post-cutoff end
    ("2026-06-01T00:00:00Z", "2026-05-01T00:00:00Z"),          # inverted
])
def test_kernel_refuses_windows_outside_envelope(observed):
    out = kernel_evidence(observed)
    assert out["cost_r"] is None and out["waiting_reasons"] == ["evidence_range"]


def test_kernel_legacy_windows_and_reason_string_unchanged():
    legacy = kernel_evidence(("2014-01-01", "2014-12-31"))
    assert legacy["cost_r"] is not None and "evidence_range" not in legacy["waiting_reasons"]
    # The kernel treats the inclusive observed end strictly before the cutoff;
    # the last representable instant of a discovery-bounded window stays legal
    # (the adapter encodes half-open manifest windows minus 1us).
    last = kernel_evidence(("2014-01-01", "2016-04-30T23:59:59.999999Z"))
    assert last["cost_r"] is not None
    at_cutoff = kernel_evidence(("2014-01-01", "2016-05-01T00:00:00Z"))
    assert at_cutoff["cost_r"] is None and "evidence_range" in at_cutoff["waiting_reasons"]
    # applies chain untouched: a signal outside applies still refuses the same way.
    outside = kernel_evidence(WATERMARKS["USDJPY"], signal_time=pd.Timestamp("1999-01-01 10:00"))
    assert outside["cost_r"] is None and "evidence_range" in outside["waiting_reasons"]


# Full production path: mixed kinds, watermark windows, per-release rules.
SESSIONS = b'''version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 5
post_minutes = 60
exempt_symbols = ["BTCUSD"]
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
min_days_per_hour = 20
'''


def load_cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location("t121_cli_v2", ROOT / "scripts/pline_feasibility.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ref_file(directory, name, value, *, format="json"):
    raw = value if isinstance(value, bytes) else encoded(value)
    (directory / name).write_bytes(raw)
    return {"artifact_id": name.replace(".", "-"), "artifact_version": "fixture-v1",
            "path": name, "format": format, "digest_domain": "raw-bytes", "sha256": sha(raw)}


def fixture_git(root, *args):
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    cmd = ["git", "--no-replace-objects", "-c", "core.hooksPath=" + str(root / "no-hooks"),
           "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "commit.gpgSign=false",
           "-c", "user.name=T121 Fixture", "-c", "user.email=t121-fixture@invalid", *args]
    result = subprocess.run(cmd, cwd=root, env=env, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def make_code_repo(root):
    root.mkdir(parents=True)
    for name in (*adapter.CODE_FILES, adapter.PREREG, adapter.PREREG_V2):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (ROOT / name).read_bytes()
        target.write_bytes(raw.replace(b"\r\n", b"\n") if name in (adapter.PREREG, adapter.PREREG_V2) else raw)
    fixture_git(root, "init", "--initial-branch=fixture")
    fixture_git(root, "add", "--", *adapter.CODE_FILES, adapter.PREREG, adapter.PREREG_V2)
    fixture_git(root, "commit", "-m", "[P] isolated implementation (v2 prereg bound)")
    return root


def reseal(bundle):
    manifest = bundle.manifest
    review = {"schema": "t121-review-v1", "record_id": "fixture-review-record", "decision": "approved",
              "authority_ref": "isolated-fixture-authority", "reviewer_id": "independent-fixture-reviewer",
              "independence_basis": "generated test record only", "decided_at": "2026-11-03T00:00:00Z",
              "valid_from": manifest["valid_from"], "expires_at": manifest["expires_at"],
              "binding_domain": "canonical-json-v1",
              "binding_sha256": sha(encoded({k: v for k, v in manifest.items() if k != "review_ref"}))}
    manifest["review_ref"] = ref_file(bundle.directory, "review.json", review)
    bundle.raw = encoded(manifest)
    (bundle.directory / "release.json").write_bytes(bundle.raw)


def make_bundle(directory, adapter_module, *, code_root, pool, mode="v2"):
    directory.mkdir(parents=True, exist_ok=True)
    symbols, sources, costs = {}, {}, {}
    scales = {"XAUUSD": 1., "EURUSD": .001, "USDJPY": 1.}
    for symbol in pool:
        scale = scales[symbol]
        times = pd.date_range(START, periods=2200, freq="h")
        bars = [{"time": t.isoformat(), "open": 100 * scale, "high": 101 * scale,
                 "low": 99 * scale, "close": 100 * scale} for t in times]
        bars[2050].update(open=100 * scale, high=105 * scale, low=99 * scale, close=104 * scale)
        symbols[symbol] = {"bars": bars, "source_tags": ["mt5"] * 2200, "gap_spans": []}
        sources[symbol] = {"used_years": [2015], "sources": ["mt5"], "excluded_months": [],
                           "segments": [{"source": "mt5", "start": START, "end_exclusive": END}]}
        payloads = {
            "spread": {"mode": "absolute", "hours": {str(h): {"p90": .1 * scale, "n_days": 20} for h in range(24)}},
            "slippage": {"spread_fraction": .1, "fixed_price": 0., "event_hours": [], "event_multiplier": 1.},
            "commission": {"round_trip_per_lot": 2. * scale, "basis": "explicit_fee_schedule"},
            "contract": {"size": 100000. if symbol != "XAUUSD" else 100.,
                         "unit": "oz" if symbol == "XAUUSD" else "base-unit"},
            "overshoot": {"version": 2, "basis": "stop_level", "hours": {str(h): {"p90": .001, "n_days": 20} for h in range(24)}},
            "swap": {"basis": "free", "scope": "Demo"},
        }
        costs[symbol] = {}
        for name in PARTS:
            component = part(symbol, name)
            if mode == "v2":
                if name in ("spread", "slippage"):
                    component.update(observation_kind="measured", observed_start=WATERMARKS[symbol][0],
                                     observed_end_exclusive=WATERMARKS[symbol][1])
                elif name in ("commission", "swap"):
                    component.update(observation_kind="declared_terms", observed_start=None,
                                     observed_end_exclusive=None, evidence_refs=["account-fee-terms-2026-11-06"])
                elif name == "contract":
                    component.update(observation_kind="spec_snapshot", observed_start=None,
                                     observed_end_exclusive=None, evidence_refs=["usdjpy_cost_bundle.json#contract_spec"])
                else:
                    component.update(observation_kind="scenario", observed_start=None,
                                     observed_end_exclusive=None, evidence_refs="scenario_multiple_of_spread_p90",
                                     multiplier=1.0, basis="scenario_multiple_of_spread_p90")
            elif mode == "v1-mixed" and name in ("commission", "swap"):
                component.update(observation_kind="declared_terms", observed_start=None,
                                 observed_end_exclusive=None, evidence_refs=["account-fee-terms-2026-11-06"])
            component["artifact_ref"] = component["source_ref"] = ref_file(directory, f"{symbol}-{name}.json", payloads[name])
            costs[symbol][name] = component
        for name, deps in {"slippage": ("spread",), "commission": ("contract",), "swap": ("contract",)}.items():
            costs[symbol][name]["dependencies"] = {d: copy.deepcopy(costs[symbol][d]["artifact_ref"]) for d in deps}
    scope = {"scope_id": "fixture-count-scope", "purpose": adapter_module.PURPOSE,
             "account_scope_id": "fixture-account-scope", "account_purpose": "Demo",
             "currency": "USD", "quote_currency": "USD", "symbols": list(pool),
             "source_timeframe": "H1", "target_timeframes": ["H1", "H4"],
             "range_start": START, "range_end_exclusive": END,
             "discovery_start": DISCOVERY_START if mode == "v2" else START,
             "discovery_end_exclusive": END}
    rules = (rules_block() if mode != "v2" else
             rules_block(version="t121-offline-v1-usdjpy", grids=pool_grids(pool)))
    manifest = {"schema": "t121-release-v1", "release_id": "fixture-release", "execution_id": "fixture-execution",
                "snapshot_version": 1, "valid_from": "2026-11-05T00:00:00Z", "expires_at": "2026-11-07T00:00:00Z",
                "scope": scope,
                "input_ref": ref_file(directory, "input.json", {"schema": "t121-input-v1", "symbols": symbols}),
                "source_manifest_ref": ref_file(directory, "sources.json", {"schema": "t121-sources-v1", "symbols": sources}),
                "sessions_ref": ref_file(directory, "sessions.toml", SESSIONS, format="toml"),
                "costs": costs,
                "code": {"repo_commit": fixture_git(code_root, "rev-parse", "HEAD").decode().strip(),
                         "files": {p: {"raw_sha256": sha((code_root / p).read_bytes()),
                                       "git_blob_sha256": sha(fixture_git(code_root, "cat-file", "blob", "HEAD:" + p))}
                                   for p in adapter_module.CODE_FILES}},
                "rules": rules, "conditions": []}
    bundle = SimpleNamespace(directory=directory, manifest=manifest, state=directory / "state", out=directory / "out")
    reseal(bundle)
    return bundle


def install_trust(monkeypatch, adapter_module, bundle, **overrides):
    values = dict(schema="t121-trust-v1", initialized=True, version=1,
                  source={"head": fixture_git(adapter_module.ROOT, "rev-parse", "HEAD").decode().strip()},
                  valid_from="2026-11-05T00:00:00Z", expires_at="2026-11-07T00:00:00Z",
                  registry={bundle.manifest["release_id"]: sha(bundle.raw)},
                  states={bundle.manifest["release_id"]: "active"}, context="isolated-synthetic-fixture")
    values.update(overrides)
    monkeypatch.setattr(adapter_module, "_trust_snapshot", lambda: SimpleNamespace(**values))
    monkeypatch.setattr(adapter_module, "_clock", lambda: NOW)
    monkeypatch.setattr(adapter_module, "_state_root", lambda: bundle.state)


def args_for(bundle):
    return ["--mode", "production", "--release-id", bundle.manifest["release_id"],
            "--release-manifest", str(bundle.directory / "release.json"), "--output", str(bundle.out)]


@pytest.fixture
def v2_bundle(tmp_path, monkeypatch):
    code_root = make_code_repo(tmp_path / "implementation")
    monkeypatch.setattr(adapter, "ROOT", code_root)
    bundle = make_bundle(tmp_path / "artifacts", adapter, code_root=code_root, pool=("USDJPY",), mode="v2")
    bundle.code_root = code_root
    install_trust(monkeypatch, adapter, bundle)
    return bundle


@pytest.fixture
def mixed_v1_bundle(tmp_path, monkeypatch):
    code_root = make_code_repo(tmp_path / "implementation")
    monkeypatch.setattr(adapter, "ROOT", code_root)
    bundle = make_bundle(tmp_path / "artifacts", adapter, code_root=code_root,
                         pool=("XAUUSD", "EURUSD"), mode="v1-mixed")
    bundle.code_root = code_root
    install_trust(monkeypatch, adapter, bundle)
    return bundle


def test_v2_usdjpy_release_runs_mixed_kinds_and_watermark_windows(v2_bundle):
    assert load_cli().main(args_for(v2_bundle)) == 0
    report = json.loads((v2_bundle.out / "report.json").read_text())
    receipt = json.loads((v2_bundle.out / "receipt.json").read_text())
    # Option II row structure: 48 rows = 4 families x 1 symbol x 3 x 4, 4 summaries.
    assert len(report["rows"]) == 48 and len(report["summaries"]) == 4
    assert {r["symbol"] for r in report["rows"]} == {"USDJPY"}
    assert report["rules"]["version"] == "t121-offline-v1-usdjpy"
    assert report["rules"]["grids"] == {"USDJPY": USDJPY_GRID}
    row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"])
               == ("F1", "USDJPY", "S1", "A3"))
    # Grid 0.5 yields three same-day A3 triggers with identical arithmetic.
    assert row["n_triggers"] == 3 and row["n_days"] == 1
    # cost = .1 spread + .02 slippage + .104 overshoot + 2/100000 commission, R = 32/14.
    assert row["cost_r"] == pytest.approx((.1 + .02 + .104 + .00002) / (32 / 14))
    assert row["cost_gate"] == "fail" and row["fail_reasons"] == ["cost_above_limit"]
    assert row["status"] == "waiting_data" and row["waiting_reasons"] == ["insufficient_days"]
    assert receipt["manifest_raw_sha256"] == sha(v2_bundle.raw)
    assert len(receipt["artifacts"]) == 4 + 6  # input/sources/sessions/review + six cost files


def test_mixed_kind_observed_aggregation_uses_measured_components_only(mixed_v1_bundle):
    # commission/swap are declared_terms (observed=null); the aggregated kernel
    # evidence window must come from the measured components alone instead of
    # refusing on null timestamps after the durable claim.
    assert load_cli().main(args_for(mixed_v1_bundle)) == 0
    report = json.loads((mixed_v1_bundle.out / "report.json").read_text())
    assert len(report["rows"]) == 96 and len(report["summaries"]) == 8
    row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"])
               == ("F1", "XAUUSD", "S1", "A3"))
    assert row["n_triggers"] == 1
    assert row["cost_r"] == pytest.approx(.244 / (32 / 14))  # identical to the v1 fixture arithmetic
