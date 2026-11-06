"""Unit-level manifest validation for cost-component observation kinds.

Scope-review clauses B/E (2026-11-06) add an observation_kind enum
(measured/declared_terms/spec_snapshot/scenario). Non-measured components
cannot declare observation intervals (observed must be null) and must carry
basis/evidence_refs; scenario components must additionally carry a positive
multiplier. These isolated synthetic tests patch only the private
implementation seam of ``_validate_manifest``: no trust snapshot, Git,
claim, kernel or artifact IO happens here.
"""
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mt5quant.prob import production_adapter as adapter
from mt5quant.prob.feasibility import RULES

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
START = "2015-01-05T00:00:00Z"
END = "2016-05-01T00:00:00Z"
PARTS = ("spread", "slippage", "commission", "contract", "overshoot", "swap")
BASES = {"spread": "absolute", "slippage": "spread-fraction-v1",
         "commission": "round-trip-per-lot", "contract": "size-per-lot",
         "overshoot": "stop-level-v2", "swap": "free"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def ref(name, *, format="json"):
    return {"artifact_id": name.replace(".", "-"), "artifact_version": "fixture-v1",
            "path": name if "." in name else name + ".json", "format": format,
            "digest_domain": "raw-bytes", "sha256": "0" * 64}


def part(symbol, name, **overrides):
    value = {"artifact_ref": ref(f"{symbol}-{name}"), "source_ref": ref(f"{symbol}-{name}"),
             "component": name, "symbol": symbol, "purpose": adapter.PURPOSE,
             "account_scope_id": "fixture-account-scope", "account_purpose": "Demo", "currency": "USD",
             "observed_start": "2014-01-01T00:00:00Z", "observed_end_exclusive": "2015-01-01T00:00:00Z",
             "applies_start": START, "applies_end_exclusive": END, "basis": BASES[name],
             "units": "fixture-explicit-units", "conversion_basis": "quote-usd-v1", "dependencies": {}}
    value.update(overrides)
    return value


def manifest():
    costs = {symbol: {name: part(symbol, name) for name in PARTS} for symbol in adapter.SYMBOLS}
    for symbol in adapter.SYMBOLS:
        for name, deps in {"slippage": ("spread",), "commission": ("contract",), "swap": ("contract",)}.items():
            costs[symbol][name]["dependencies"] = {d: costs[symbol][d]["artifact_ref"] for d in deps}
    scope = {"scope_id": "fixture-count-scope", "purpose": adapter.PURPOSE,
             "account_scope_id": "fixture-account-scope", "account_purpose": "Demo",
             "currency": "USD", "quote_currency": "USD", "symbols": list(adapter.SYMBOLS),
             "source_timeframe": "H1", "target_timeframes": ["H1", "H4"],
             "range_start": START, "range_end_exclusive": END,
             "discovery_start": START, "discovery_end_exclusive": END}
    return {"schema": "t121-release-v1", "release_id": "fixture-release",
            "execution_id": "fixture-execution", "snapshot_version": 1,
            "valid_from": "2026-11-05T00:00:00Z", "expires_at": "2026-11-07T00:00:00Z",
            "scope": scope, "input_ref": ref("input"), "source_manifest_ref": ref("sources"),
            "sessions_ref": ref("sessions.toml", format="toml"), "review_ref": ref("review"),
            "costs": costs,
            "rules": {"version": RULES["version"], "digest_domain": "canonical-json-v1",
                      "sha256": sha(encoded(RULES)), "prereg_sha256": adapter.PREREG_SHA256,
                      "prereg_digest_domain": "git-blob-bytes"},
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


@pytest.fixture
def document():
    return manifest()


def test_v1_shaped_measured_components_still_validate(validate, document):
    assert validate(document)


def test_explicit_measured_kind_keeps_v1_shape_only(validate, document):
    for symbol in adapter.SYMBOLS:
        document["costs"][symbol]["spread"]["observation_kind"] = "measured"
    assert validate(document)
    # evidence_refs/multiplier are not v1 fields; measured stays strictly v1.
    document["costs"]["XAUUSD"]["spread"]["evidence_refs"] = "watermark-2026"
    with pytest.raises(adapter.AdmissionRefused, match="invalid or missing fields"):
        validate(document)


@pytest.mark.parametrize("kind", ["declared_terms", "spec_snapshot", "scenario"])
def test_non_measured_observation_window_must_be_null(validate, document, kind):
    component = document["costs"]["XAUUSD"]["commission"]
    component.update(observation_kind=kind, observed_start=None, observed_end_exclusive=None,
                     evidence_refs=["account-fee-terms-2026-11-06"])
    if kind == "scenario":
        component["multiplier"] = 1.0
    assert validate(document)
    component["observed_start"] = "2014-01-01T00:00:00Z"
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)


@pytest.mark.parametrize("kind", ["unknown", "MEASURED", "", None, True])
def test_observation_kind_enum_is_closed(validate, document, kind):
    document["costs"]["XAUUSD"]["commission"].update(
        observation_kind=kind, observed_start=None, observed_end_exclusive=None)
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)


@pytest.mark.parametrize("refs", ["account-fee-terms-2026-11-06",
                                  ["account-fee-terms-2026-11-06", "probe.json"]])
def test_declared_terms_requires_basis_and_evidence_refs(validate, document, refs):
    component = document["costs"]["XAUUSD"]["commission"]
    component.update(observation_kind="declared_terms", observed_start=None,
                     observed_end_exclusive=None, evidence_refs=refs)
    assert validate(document)
    del component["basis"]
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)
    component["basis"] = BASES["commission"]


@pytest.mark.parametrize("bad", ["missing", "empty-string", "empty-list", "numbers", "number", "blank"])
def test_declared_terms_rejects_unusable_evidence_refs(validate, document, bad):
    component = document["costs"]["XAUUSD"]["swap"]
    component.update(observation_kind="declared_terms", observed_start=None,
                     observed_end_exclusive=None)
    values = {"missing": None, "empty-string": "", "empty-list": [],
              "numbers": [1, 2], "number": 7, "blank": ["   "]}
    if bad != "missing":
        component["evidence_refs"] = values[bad]
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)


def test_spec_snapshot_component_with_evidence_refs_passes(validate, document):
    component = document["costs"]["EURUSD"]["contract"]
    component.update(observation_kind="spec_snapshot", observed_start=None,
                     observed_end_exclusive=None,
                     evidence_refs=["specs/latest.json:144bb2245479e3557c571c9f5f83ca92db9d269211da7845c9d5aa2467d3150f"])
    assert validate(document)


def test_scenario_requires_positive_multiplier(validate, document):
    component = document["costs"]["XAUUSD"]["overshoot"]
    component.update(observation_kind="scenario", observed_start=None, observed_end_exclusive=None,
                     evidence_refs="scenario_multiple_of_spread_p90")
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)
    component["multiplier"] = 1.0
    assert validate(document)


@pytest.mark.parametrize("multiplier", [0, -1.0, True, "1.0", float("nan"), float("inf")])
def test_scenario_rejects_non_positive_or_non_numeric_multiplier(validate, document, multiplier):
    component = document["costs"]["XAUUSD"]["overshoot"]
    component.update(observation_kind="scenario", observed_start=None, observed_end_exclusive=None,
                     evidence_refs="scenario_multiple_of_spread_p90", multiplier=multiplier)
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)


def test_measured_missing_observation_window_still_refused(validate, document):
    document["costs"]["XAUUSD"]["spread"].update(observed_start=None, observed_end_exclusive=None)
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)
    document["costs"]["XAUUSD"]["spread"].update(observed_start="2014-01-01T00:00:00Z")
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)
    explicit = manifest()
    explicit["costs"]["XAUUSD"]["spread"].update(
        observation_kind="measured", observed_start=None, observed_end_exclusive=None)
    with pytest.raises(adapter.AdmissionRefused):
        validate(explicit)


def test_measured_observation_must_stay_inside_discovery(validate, document):
    document["costs"]["EURUSD"]["slippage"]["observed_end_exclusive"] = "2016-05-01T00:00:00Z"
    assert validate(document)
    document["costs"]["EURUSD"]["slippage"]["observed_end_exclusive"] = "2016-05-02T00:00:00Z"
    with pytest.raises(adapter.AdmissionRefused):
        validate(document)


def test_non_measured_fields_are_exact(validate, document):
    component = document["costs"]["XAUUSD"]["commission"]
    component.update(observation_kind="declared_terms", observed_start=None,
                     observed_end_exclusive=None, evidence_refs="account-fee-terms-2026-11-06",
                     multiplier=1.0)
    with pytest.raises(adapter.AdmissionRefused, match="invalid or missing fields"):
        validate(document)
