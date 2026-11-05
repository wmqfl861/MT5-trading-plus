"""Generated artifacts only; trust/clock/state injection lives in this test file."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
START = "2015-01-05T00:00:00Z"
END = "2016-05-01T00:00:00Z"
PARTS = ("spread", "slippage", "commission", "contract", "overshoot", "swap")
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


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def load_adapter():
    assert importlib.util.find_spec("mt5quant.prob.production_adapter"), "byte admission adapter missing"
    return importlib.import_module("mt5quant.prob.production_adapter")


def load_cli():
    spec = importlib.util.spec_from_file_location("t121_cli", ROOT / "scripts/pline_feasibility.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ref_file(directory, name, value, *, format="json"):
    raw = value if isinstance(value, bytes) else encoded(value)
    (directory / name).write_bytes(raw)
    return {"artifact_id": name.replace(".", "-"), "artifact_version": "fixture-v1",
            "path": name, "format": format, "digest_domain": "raw-bytes", "sha256": sha(raw)}


def make_bundle(directory, adapter, *, count=2200, code_root=ROOT):
    directory.mkdir(exist_ok=True)
    symbols, sources, costs = {}, {}, {}
    for symbol in ("XAUUSD", "EURUSD"):
        scale = 1. if symbol == "XAUUSD" else .001
        times = pd.date_range(START, periods=count, freq="h")
        bars = [{"time": t.isoformat(), "open": 100 * scale, "high": 101 * scale,
                 "low": 99 * scale, "close": 100 * scale} for t in times]
        if count > 2050:
            bars[2050].update(open=100 * scale, high=105 * scale,
                              low=99 * scale, close=104 * scale)
        symbols[symbol] = {"bars": bars, "source_tags": ["mt5"] * count, "gap_spans": []}
        sources[symbol] = {"used_years": [2015], "sources": ["mt5"], "excluded_months": [],
                           "segments": [{"source": "mt5", "start": START, "end_exclusive": END}]}
        payloads = {
            "spread": {"mode": "absolute", "hours": {str(h): {"p90": .1 * scale, "n_days": 20} for h in range(24)}},
            "slippage": {"spread_fraction": .1, "fixed_price": 0., "event_hours": [], "event_multiplier": 1.},
            "commission": {"round_trip_per_lot": 2. * scale, "basis": "explicit_fee_schedule"},
            "contract": {"size": 100., "unit": "oz" if symbol == "XAUUSD" else "base-unit"},
            "overshoot": {"version": 2, "basis": "stop_level", "hours": {str(h): {"p90": .001, "n_days": 20} for h in range(24)}},
            "swap": {"basis": "free", "scope": "Demo"},
        }
        costs[symbol] = {}
        bases = {"spread": "absolute", "slippage": "spread-fraction-v1",
                 "commission": "round-trip-per-lot", "contract": "size-per-lot",
                 "overshoot": "stop-level-v2", "swap": "free"}
        for part in PARTS:
            ref = ref_file(directory, f"{symbol}-{part}.json", payloads[part])
            costs[symbol][part] = {"artifact_ref": ref, "source_ref": copy.deepcopy(ref),
                "component": part, "symbol": symbol, "purpose": "T121-count-cost",
                "account_scope_id": "fixture-account-scope", "account_purpose": "Demo", "currency": "USD",
                "observed_start": "2014-01-01T00:00:00Z", "observed_end_exclusive": "2015-01-01T00:00:00Z",
                "applies_start": START, "applies_end_exclusive": END, "basis": bases[part],
                "units": "fixture-explicit-units", "conversion_basis": "quote-usd-v1", "dependencies": {}}
        for part, dependencies in {"slippage": ["spread"], "commission": ["contract"], "swap": ["contract"]}.items():
            costs[symbol][part]["dependencies"] = {p: copy.deepcopy(costs[symbol][p]["artifact_ref"]) for p in dependencies}
    scope = {"scope_id": "fixture-count-scope", "purpose": "T121-count-cost",
        "account_scope_id": "fixture-account-scope", "account_purpose": "Demo", "currency": "USD", "quote_currency": "USD",
        "symbols": ["XAUUSD", "EURUSD"], "source_timeframe": "H1", "target_timeframes": ["H1", "H4"],
        "range_start": START, "range_end_exclusive": END, "discovery_start": START, "discovery_end_exclusive": END}
    from mt5quant.prob.feasibility import RULES
    manifest = {"schema": "t121-release-v1", "release_id": "fixture-release", "execution_id": "fixture-execution",
        "snapshot_version": 1, "valid_from": "2026-11-04T00:00:00Z", "expires_at": "2026-11-06T00:00:00Z",
        "scope": scope, "input_ref": ref_file(directory, "input.json", {"schema": "t121-input-v1", "symbols": symbols}),
        "source_manifest_ref": ref_file(directory, "sources.json", {"schema": "t121-sources-v1", "symbols": sources}),
        "sessions_ref": ref_file(directory, "sessions.toml", SESSIONS, format="toml"), "costs": costs,
        "code": {"repo_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=code_root, text=True).strip(),
                 "files": {p: {"raw_sha256": sha((code_root / p).read_bytes()),
                               "git_blob_sha256": sha(subprocess.check_output(
                                   ["git", "cat-file", "blob", "HEAD:" + p], cwd=code_root))}
                           for p in adapter.CODE_FILES}},
        "rules": {"version": "t121-offline-v1", "digest_domain": "canonical-json-v1", "sha256": sha(encoded(RULES)),
                  "prereg_digest_domain": "git-blob-bytes",
                  "prereg_sha256": "c78a78d13ed81c611b5f9599c96235d7bbcefceac6fda54566b103f3e8cb3235"},
        "conditions": []}
    bundle = SimpleNamespace(directory=directory, manifest=manifest, state=directory / "state", out=directory / "out")
    reseal(bundle)
    return bundle


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


def install_trust(monkeypatch, adapter, bundle, **overrides):
    values = dict(schema="t121-trust-v1", initialized=True, version=1,
                  source={"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=adapter.ROOT, text=True).strip()},
                  valid_from="2026-11-04T00:00:00Z", expires_at="2026-11-06T00:00:00Z",
                  registry={bundle.manifest["release_id"]: sha(bundle.raw)},
                  states={bundle.manifest["release_id"]: "active"}, context="isolated-synthetic-fixture")
    values.update(overrides)
    monkeypatch.setattr(adapter, "_trust_snapshot", lambda: SimpleNamespace(**values))
    monkeypatch.setattr(adapter, "_clock", lambda: NOW)
    monkeypatch.setattr(adapter, "_state_root", lambda: bundle.state)


def args_for(bundle):
    return ["--mode", "production", "--release-id", bundle.manifest["release_id"],
            "--release-manifest", str(bundle.directory / "release.json"), "--output", str(bundle.out)]


@pytest.fixture(scope="module")
def implementation_repo(tmp_path_factory):
    return make_code_repo(tmp_path_factory.mktemp("t121-implementation") / "code")


@pytest.fixture
def bundle(tmp_path, monkeypatch, implementation_repo):
    adapter = load_adapter()
    monkeypatch.setattr(adapter, "ROOT", implementation_repo)
    value = make_bundle(tmp_path, adapter, code_root=implementation_repo)
    value.code_root = implementation_repo
    install_trust(monkeypatch, adapter, value)
    return value


def test_real_main_consumes_admitted_bytes_and_frozen_cost_kernel(bundle, monkeypatch):
    adapter = load_adapter()
    original = Path.read_bytes
    reads = {}
    data_paths = {bundle.directory / "input.json", bundle.directory / "sources.json", bundle.directory / "sessions.toml"}
    data_paths.update(bundle.directory / f"{s}-{p}.json" for s in ("XAUUSD", "EURUSD") for p in PARTS)
    def once(path):
        if path in data_paths:
            assert list(bundle.state.glob("*.claim")), "data read before durable claim"
            reads[path] = reads.get(path, 0) + 1
            assert reads[path] == 1, "verified path reopened"
            raw = original(path)
            path.write_bytes(b"changed after read")
            return raw
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", once)
    assert load_cli().main(args_for(bundle)) == 0
    report = json.loads((bundle.out / "report.json").read_text())
    receipt = json.loads((bundle.out / "receipt.json").read_text())
    assert len(report["rows"]) == 96 and len(report["summaries"]) == 8
    row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"]) == ("F1", "XAUUSD", "S1", "A3"))
    assert row["n_triggers"] == 1 and row["n_days"] == 1
    # Signal ATR after [high=105, low=99] is (13*2 + 6)/14; cost=.1+.02+.104+.02.
    assert row["cost_r"] == pytest.approx(.244 / (32 / 14))
    assert row["status"] == "waiting_data" and row["cost_gate"] == "fail"
    assert set(reads) == data_paths
    assert receipt["release_id"] == "fixture-release" and receipt["execution_id"] == "fixture-execution"
    assert receipt["context"] == "isolated-synthetic-fixture"
    assert report["production_execution"] == "not_released"
    assert receipt["manifest_raw_sha256"] == sha(bundle.raw)
    assert receipt["report_sha256"] == sha(original(bundle.out / "report.json"))
    assert len(receipt["artifacts"]) == 16  # input, source manifest, sessions, review, twelve cost files
    assert "fixture-account-scope" not in json.dumps(report)
    assert str(bundle.directory) not in json.dumps(receipt)
    first = original(bundle.out / "report.json")
    assert load_cli().main(args_for(bundle)) == 2
    assert original(bundle.out / "report.json") == first


@pytest.mark.parametrize("path,value", [
    (("scope", "account_purpose"), "Real"), (("scope", "currency"), "EUR"),
    (("scope", "symbols"), ["USTEC"]), (("scope", "symbols"), "XAUUSD"),
    (("scope", "range_end_exclusive"), "2016-05-01T01:00:00Z"),
    (("scope", "range_start"), "2015-01-05"), (("scope", "range_start"), True),
    (("scope", "target_timeframes"), ["D1"]), (("scope", "account_scope_id"), "other-account"),
    (("snapshot_version",), True), (("snapshot_version",), 0),
    (("expires_at",), "2026-11-05T00:00:00Z"),
    (("valid_from",), "2026-11-05T00:00:01Z"),
    (("costs", "XAUUSD", "spread", "observed_end_exclusive"), "2026-01-01T00:00:00Z"),
    (("costs", "XAUUSD", "slippage", "dependencies"), {}),
    (("costs", "XAUUSD", "swap", "basis"), "charged"),
    (("input_ref", "digest_domain"), "canonical-json-v1"),
    (("rules", "sha256"), "0" * 64), (("rules", "digest_domain"), "raw-bytes"),
    (("code", "repo_commit"), "0" * 40), (("code", "files"), {}),
    (("conditions",), [{"condition_id": "open", "status": "pending"}]),
])
def test_manifest_rejection_precedes_artifact_read_and_claim(bundle, monkeypatch, path, value):
    doc = bundle.manifest
    for name in path[:-1]:
        doc = doc[name]
    doc[path[-1]] = value
    reseal(bundle)
    install_trust(monkeypatch, load_adapter(), bundle)
    for filename in ("input.json", "sources.json", "sessions.toml"):
        (bundle.directory / filename).unlink()
    assert load_cli().main(args_for(bundle)) == 2
    assert not list(bundle.state.glob("*.claim")) and not bundle.out.exists()


@pytest.mark.parametrize("missing", ["scope", "input_ref", "source_manifest_ref", "sessions_ref", "review_ref", "code", "rules", "conditions", "execution_id"])
def test_missing_manifest_bindings_are_not_inferred(bundle, monkeypatch, missing):
    del bundle.manifest[missing]
    if missing != "review_ref":
        reseal(bundle)
    else:
        bundle.raw = encoded(bundle.manifest)
        (bundle.directory / "release.json").write_bytes(bundle.raw)
    install_trust(monkeypatch, load_adapter(), bundle)
    assert load_cli().main(args_for(bundle)) == 2
    assert not list(bundle.state.glob("*.claim")) and not bundle.out.exists()


@pytest.mark.parametrize("part", PARTS)
def test_six_component_binding_required_before_read(bundle, monkeypatch, part):
    del bundle.manifest["costs"]["XAUUSD"][part]
    reseal(bundle)
    install_trust(monkeypatch, load_adapter(), bundle)
    assert load_cli().main(args_for(bundle)) == 2
    assert not list(bundle.state.glob("*.claim"))


@pytest.mark.parametrize("state", [None, "unknown", "revoked"])
def test_unknown_or_revoked_snapshot_never_consumes(bundle, monkeypatch, state):
    install_trust(monkeypatch, load_adapter(), bundle, states={} if state is None else {"fixture-release": state})
    assert load_cli().main(args_for(bundle)) == 2
    assert not list(bundle.state.glob("*.claim")) and not bundle.out.exists()


@pytest.mark.parametrize("change", ["expired", "future", "old-version"])
def test_snapshot_freshness_is_trusted_not_caller_as_of(bundle, monkeypatch, change):
    overrides = {"expired": {"expires_at": "2026-11-05T00:00:00Z"},
                 "future": {"valid_from": "2026-11-06T00:00:00Z"}, "old-version": {"version": 2}}[change]
    install_trust(monkeypatch, load_adapter(), bundle, **overrides)
    assert load_cli().main(args_for(bundle)) == 2
    assert not list(bundle.state.glob("*.claim"))


@pytest.mark.parametrize("target", ["release.json", "review.json", "input.json", "XAUUSD-spread.json", "sessions.toml"])
def test_tampering_is_refused_and_post_claim_failure_never_recycles(bundle, target):
    path = bundle.directory / target
    original = path.read_bytes()
    path.write_bytes(original + b" ")
    assert load_cli().main(args_for(bundle)) == 2
    assert not bundle.out.exists()
    claimed = target not in ("release.json", "review.json")
    assert bool(list(bundle.state.glob("*.claim"))) == claimed
    if claimed:
        path.write_bytes(original)
        assert load_cli().main(args_for(bundle)) == 2
        assert not bundle.out.exists()


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'[]', b'null', b'{'])
def test_byte_verifier_rejects_ambiguous_json_even_when_hash_matches(raw):
    adapter = load_adapter()
    ref = {"artifact_id": "fixture", "artifact_version": "v1", "path": "fixture.json", "format": "json",
           "digest_domain": "raw-bytes", "sha256": sha(raw)}
    with pytest.raises(ValueError):
        adapter.verify_artifact_bytes(raw, ref)


def test_digest_domains_are_never_interchanged():
    adapter = load_adapter()
    raw = b'{ "x": 1 }'
    ref = {"artifact_id": "fixture", "artifact_version": "v1", "path": "fixture.json", "format": "json",
           "digest_domain": "raw-bytes", "sha256": sha(raw)}
    assert adapter.verify_artifact_bytes(raw, ref) == {"x": 1}
    with pytest.raises(ValueError):
        adapter.verify_artifact_bytes(raw, {**ref, "sha256": sha(encoded({"x": 1}))})
    with pytest.raises(ValueError):
        adapter.verify_artifact_bytes(raw, {**ref, "digest_domain": "canonical-json-v1"})


def test_declarative_interface_cannot_release_or_read(monkeypatch):
    adapter = load_adapter()
    def forbidden(*args, **kwargs):
        raise AssertionError("declaration performed IO")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(adapter, "_clock", forbidden)
    claims = {"approved": True, "verified": True, "approved_by": "self", "kind": "production"}
    before = copy.deepcopy(claims)
    result = adapter.check_t121_admission(claims, claims, claims)
    assert result["review_validity"] == "unverified" and result["execution_status"] == "not_released"
    assert claims == before


COLD_GUARD = '''
import sys, importlib.abc, runpy, json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
assert not any(n.startswith(("mt5quant", "mt5data")) for n in sys.modules)
forbidden = ("mt5quant.prob.labels", "mt5quant.prob.tables", "mt5quant.prob.p1",
             "mt5quant.backtest", "mt5quant.research", "mt5live", "MetaTrader5", "sqlite3")
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in forbidden):
            raise AssertionError("forbidden cold import: " + fullname)
sys.meta_path.insert(0, Guard())
root, bundle_path, output, state, manifest_sha, action = sys.argv[1:]
sys.path.insert(0, str(Path(root) / "src"))
import mt5quant.prob.production_adapter as adapter
# Fixed state root is independent of cwd and caller HOME/APPDATA/T121_* overrides.
if action == "state-root":
    print(adapter._state_root())
    raise SystemExit(0)
if action != "unknown":
    adapter._trust_snapshot = lambda: SimpleNamespace(schema="t121-trust-v1", initialized=True, version=1,
        source={"head": adapter._git("rev-parse", "HEAD").decode().strip()},
        valid_from="2026-11-04T00:00:00Z", expires_at="2026-11-06T00:00:00Z",
        registry={"fixture-release": manifest_sha}, states={"fixture-release": "active"},
        context="isolated-synthetic-fixture")
    adapter._clock = lambda: datetime(2026, 11, 5, tzinfo=timezone.utc)
adapter._state_root = lambda: Path(state)
if action == "unknown":
    original_read = Path.read_bytes
    def forbidden_read(path):
        if path.is_relative_to(Path(bundle_path)):
            raise AssertionError("unknown release read artifact")
        return original_read(path)
    Path.read_bytes = forbidden_read
cli = runpy.run_path(str(Path(root) / "scripts/pline_feasibility.py"))
result = cli["main"](["--mode", "production", "--release-id", "fixture-release",
    "--release-manifest", str(Path(bundle_path) / "release.json"), "--output", output])
assert not any(n == p or n.startswith(p + ".") for n in sys.modules for p in forbidden)
print("COLD_IMPORT_GUARD_PASS")
raise SystemExit(result)
'''


def child_command(bundle, action="run", output=None):
    return [sys.executable, "-I", "-B", "-c", COLD_GUARD, str(bundle.code_root), str(bundle.directory),
            str(output or bundle.out), str(bundle.state), sha(bundle.raw), action]


def test_cold_main_two_process_claim_restart_and_other_checkout(bundle, tmp_path):
    a = subprocess.Popen(child_command(bundle, output=tmp_path / "out-a"), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    b = subprocess.Popen(child_command(bundle, output=tmp_path / "out-b"), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    logs = [p.communicate(timeout=90) for p in (a, b)]
    assert sorted([a.returncode, b.returncode]) == [0, 2], logs
    assert all("COLD_IMPORT_GUARD_PASS" in out for out, err in logs)
    assert len(list(bundle.state.glob("*.claim"))) == 1
    # A copied checkout (including the same Git identity) runs a fresh interpreter.
    # Do not register a second worktree: this task owns exactly one Git worktree.
    other = tmp_path / "other-worktree"
    shutil.copytree(bundle.code_root, other)
    command = child_command(bundle, output=other / "output")
    command[5] = str(other)
    restart = subprocess.run(command, cwd=other, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 2 and "consumed" in restart.stderr
    assert not (other / "output").exists()


def test_empty_production_registry_cannot_be_overridden_by_json_or_env(bundle):
    bundle.manifest["approved_by"] = "self"
    (bundle.directory / "release.json").write_bytes(encoded(bundle.manifest))
    env = {**os.environ, "T121_REGISTRY": json.dumps({"fixture-release": sha(bundle.raw)}),
           "T121_APPROVED": "1", "T121_NOW": NOW.isoformat(), "T121_STATE_ROOT": str(bundle.state)}
    result = subprocess.run(child_command(bundle, "unknown"), cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "unknown release" in result.stderr, result.stderr
    assert "COLD_IMPORT_GUARD_PASS" in result.stdout
    assert not list(bundle.state.glob("*.claim")) and not bundle.out.exists()


def test_fixed_user_state_root_ignores_environment_and_cwd(bundle, tmp_path):
    first = subprocess.run(child_command(bundle, "state-root"), cwd=ROOT, capture_output=True, text=True, timeout=30)
    changed_env = {**os.environ, **{key: str(tmp_path) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_STATE_HOME", "T121_STATE_ROOT")}}
    second = subprocess.run(child_command(bundle, "state-root"), cwd=tmp_path, env=changed_env, capture_output=True, text=True, timeout=30)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first.stdout.strip() == second.stdout.strip()
    assert str(tmp_path) not in first.stdout


def test_modes_are_mutually_exclusive_before_io(tmp_path):
    cli = load_cli()
    with pytest.raises(SystemExit) as exc:
        cli.main(["--mode", "production", "--release-id", "unknown", "--release-manifest", "missing",
                  "--input", "missing", "--output", str(tmp_path / "out")])
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


def replace_artifact(bundle, key, obj):
    ref = bundle.manifest[key]
    raw = obj if isinstance(obj, bytes) else encoded(obj)
    (bundle.directory / ref["path"]).write_bytes(raw)
    ref["sha256"] = sha(raw)
    reseal(bundle)


def block_data_reads(monkeypatch, bundle):
    original = Path.read_bytes
    forbidden = {bundle.directory / bundle.manifest[k]["path"] for k in ("input_ref", "source_manifest_ref", "sessions_ref")}
    forbidden.update(bundle.directory / p["artifact_ref"]["path"] for group in bundle.manifest["costs"].values() for p in group.values())
    def guarded(path):
        assert path not in forbidden, "data/cost read before admission"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", guarded)


def test_clock_expiring_during_preflight_prevents_read_and_claim(bundle, monkeypatch):
    adapter = load_adapter()
    # Trust stays current while the narrower release/review expires during IO.
    bundle.manifest["expires_at"] = "2026-11-05T00:00:01Z"
    reseal(bundle)
    install_trust(monkeypatch, adapter, bundle)
    ticks = iter([NOW, datetime(2026, 11, 5, 0, 0, 1, tzinfo=timezone.utc)])
    monkeypatch.setattr(adapter, "_clock", lambda: next(ticks))
    block_data_reads(monkeypatch, bundle)
    assert load_cli().main(args_for(bundle)) == 2
    assert not list(bundle.state.glob("*.claim")) and not bundle.out.exists()


@pytest.mark.parametrize("change", ["empty", "short", "unknown", "boolean-version"])
def test_incomplete_or_unfrozen_sessions_cannot_use_parser_defaults(bundle, monkeypatch, change):
    raw = {"empty": b"version = 1\n[rollover]\nlocal_time=\"17:00\"\ntz=\"America/New_York\"\n[forbidden]\nexempt_symbols=[\"BTCUSD\"]\n[costs]\n",
           "short": SESSIONS.replace(b"post_minutes = 60", b"post_minutes = 59"),
           "unknown": SESSIONS + b"unknown = 1\n",
           "boolean-version": SESSIONS.replace(b"version = 1", b"version = true")}[change]
    replace_artifact(bundle, "sessions_ref", raw)
    install_trust(monkeypatch, load_adapter(), bundle)
    assert load_cli().main(args_for(bundle)) == 2
    assert len(list(bundle.state.glob("*.claim"))) == 1 and not bundle.out.exists()


@pytest.mark.parametrize("mode", ["read", "parse", "compute"])
def test_execution_failure_remains_consumed_after_restart(bundle, monkeypatch, mode):
    adapter = load_adapter()
    if mode == "read":
        (bundle.directory / "input.json").unlink()
    elif mode == "parse":
        replace_artifact(bundle, "input_ref", b'{"duplicate":1,"duplicate":2}')
        install_trust(monkeypatch, adapter, bundle)
    else:
        def broken(*args, **kwargs):
            raise ValueError("injected compute failure")
        monkeypatch.setattr(adapter, "_run_feasibility_kernel", broken)
    assert load_cli().main(args_for(bundle)) == 2
    assert len(list(bundle.state.glob("*.claim"))) == 1 and not bundle.out.exists()
    result = subprocess.run(child_command(bundle), cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "consumed" in result.stderr


@pytest.mark.parametrize("field,value", [("decision", "conditional"), ("binding_sha256", "0" * 64),
    ("binding_domain", "raw-bytes"), ("authority_ref", ""), ("decided_at", "2026-11-05T00:00:01Z")])
def test_registered_manifest_still_requires_bound_review_before_data(bundle, monkeypatch, field, value):
    review = json.loads((bundle.directory / "review.json").read_bytes())
    review[field] = value
    raw = encoded(review)
    (bundle.directory / "review.json").write_bytes(raw)
    bundle.manifest["review_ref"]["sha256"] = sha(raw)
    bundle.raw = encoded(bundle.manifest)
    (bundle.directory / "release.json").write_bytes(bundle.raw)
    install_trust(monkeypatch, load_adapter(), bundle)
    block_data_reads(monkeypatch, bundle)
    assert load_cli().main(args_for(bundle)) == 2
    assert not list(bundle.state.glob("*.claim"))


@pytest.mark.parametrize("closed", [False, True])
def test_conditions_require_original_closure_bytes(bundle, monkeypatch, closed):
    closure = {"condition_id": "fixture-condition", "status": "closed" if closed else "pending",
               "closed_at": "2026-11-04T00:00:00Z"}
    ref = ref_file(bundle.directory, "closure.json", closure)
    bundle.manifest["conditions"] = [{"condition_id": "fixture-condition", "status": "closed", "evidence_ref": ref}]
    reseal(bundle)
    install_trust(monkeypatch, load_adapter(), bundle)
    if not closed:
        block_data_reads(monkeypatch, bundle)
    assert load_cli().main(args_for(bundle)) == (0 if closed else 2)
    assert bool(list(bundle.state.glob("*.claim"))) == closed


@pytest.mark.parametrize("change", ["duplicate-key", "nonfinite", "wrong-container", "bool-cost", "path-escape"])
def test_manifest_strict_shape_rejects_before_data(bundle, monkeypatch, change):
    if change == "duplicate-key":
        raw = bundle.raw[:-1] + b',"schema":"t121-release-v1"}'
    elif change == "nonfinite":
        raw = bundle.raw[:-1] + b',"extra":NaN}'
    elif change == "wrong-container":
        raw = b"[]"
    else:
        if change == "bool-cost":
            bundle.manifest["costs"] = True
        else:
            bundle.manifest["input_ref"]["path"] = "../outside.json"
        reseal(bundle)
        raw = bundle.raw
    bundle.raw = raw
    (bundle.directory / "release.json").write_bytes(raw)
    install_trust(monkeypatch, load_adapter(), bundle)
    assert load_cli().main(args_for(bundle)) == 2
    assert not list(bundle.state.glob("*.claim"))


@pytest.mark.parametrize("change", ["excluded-month", "wrong-year", "wrong-source-segment", "cutoff-bar", "duplicate-root"])
def test_artifact_scope_is_checked_after_hash_before_count(bundle, monkeypatch, change):
    if change in ("cutoff-bar", "duplicate-root"):
        doc = json.loads((bundle.directory / "input.json").read_bytes())
        doc["symbols"]["XAUUSD"]["bars"][-1]["time"] = END if change == "cutoff-bar" else START
        replace_artifact(bundle, "input_ref", doc)
    else:
        doc = json.loads((bundle.directory / "sources.json").read_bytes())
        source = doc["symbols"]["XAUUSD"]
        if change == "excluded-month":
            source["excluded_months"] = ["2015-01"]
        elif change == "wrong-year":
            source["used_years"] = [2014]
        else:
            source["sources"] = ["ext:HistData"]
            source["segments"][0]["source"] = "ext:HistData"
        replace_artifact(bundle, "source_manifest_ref", doc)
    install_trust(monkeypatch, load_adapter(), bundle)
    assert load_cli().main(args_for(bundle)) == 2
    assert not bundle.out.exists() and len(list(bundle.state.glob("*.claim"))) == 1


def test_offset_equivalence_and_half_open_cutoff_admitted(bundle, monkeypatch):
    bundle.manifest["scope"]["range_start"] = "2015-01-05T02:00:00+02:00"
    bundle.manifest["scope"]["range_end_exclusive"] = "2016-04-30T19:00:00-05:00"
    bundle.manifest["valid_from"] = NOW.isoformat()
    reseal(bundle)
    install_trust(monkeypatch, load_adapter(), bundle)
    assert load_cli().main(args_for(bundle)) == 0
    result = json.loads((bundle.out / "report.json").read_bytes())
    assert len(result["rows"]) == 96


@pytest.mark.parametrize("case", ["missing-cost-value", "no-coverage", "unknown-source"])
def test_admission_does_not_imply_cost_feasibility(bundle, monkeypatch, case):
    if case == "unknown-source":
        doc = json.loads((bundle.directory / "input.json").read_bytes())
        doc["symbols"]["XAUUSD"]["source_tags"][2050] = None
        replace_artifact(bundle, "input_ref", doc)
        reason = "source_unknown"
    else:
        part = "commission" if case == "missing-cost-value" else "spread"
        path = bundle.directory / f"XAUUSD-{part}.json"
        payload = json.loads(path.read_bytes())
        if case == "missing-cost-value":
            del payload["round_trip_per_lot"]
            reason = "missing_commission_round_trip_per_lot"
        else:
            for value in payload["hours"].values():
                value["n_days"] = 19
            reason = "no_qualified_spread_hours"
        path.write_bytes(encoded(payload))
        group = bundle.manifest["costs"]["XAUUSD"]
        for key in ("artifact_ref", "source_ref"):
            group[part][key]["sha256"] = sha(encoded(payload))
        for component in group.values():
            if part in component["dependencies"]:
                component["dependencies"][part] = copy.deepcopy(group[part]["artifact_ref"])
        reseal(bundle)
    install_trust(monkeypatch, load_adapter(), bundle)
    assert load_cli().main(args_for(bundle)) == 0
    report = json.loads((bundle.out / "report.json").read_bytes())
    row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"]) == ("F1", "XAUUSD", "S1", "A3"))
    assert row["n_triggers"] == 1 and row["cost_r"] is None
    assert reason in row["waiting_reasons"] and row["status"] == "waiting_data"


def test_old_snapshot_cannot_restore_permission_across_restart(bundle, monkeypatch):
    adapter = load_adapter()
    old_raw = bundle.raw
    old_review = (bundle.directory / "review.json").read_bytes()
    bundle.manifest["snapshot_version"] = 2
    reseal(bundle)
    install_trust(monkeypatch, adapter, bundle, version=2)
    assert load_cli().main(args_for(bundle)) == 0
    (bundle.directory / "release.json").write_bytes(old_raw)
    (bundle.directory / "review.json").write_bytes(old_review)
    bundle.raw = old_raw
    bundle.out = bundle.directory / "replay"
    result = subprocess.run(child_command(bundle), cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "old or conflicting" in result.stderr
    assert not bundle.out.exists()


def test_new_release_cannot_reuse_execution_id(bundle, monkeypatch):
    adapter = load_adapter()
    assert load_cli().main(args_for(bundle)) == 0
    bundle.manifest.update(release_id="fixture-release-2", snapshot_version=2)
    bundle.out = bundle.directory / "replay"
    reseal(bundle)
    install_trust(monkeypatch, adapter, bundle, version=2)
    block_data_reads(monkeypatch, bundle)
    assert load_cli().main(args_for(bundle)) == 2
    assert not bundle.out.exists() and len(list(bundle.state.glob("*.claim"))) == 1


def test_legacy_cli_default_mode_uses_temp_sessions_and_real_kernel(bundle):
    from mt5quant.prob.feasibility import input_payload, payload_sha256
    doc = json.loads((bundle.directory / "input.json").read_bytes())
    costs = {}
    for symbol, item in doc["symbols"].items():
        item["evidence"] = {"kind": "synthetic", "purpose": "T121-count-cost", "range_start": START,
            "range_end": END, "source_manifest": {"used_years": [2015], "sources": ["mt5"], "excluded_months": []},
            "payload_sha256": payload_sha256(input_payload(pd.DataFrame(item["bars"]), item["source_tags"], item["gap_spans"]))}
        payload = {p: json.loads((bundle.directory / f"{symbol}-{p}.json").read_bytes()) for p in PARTS}
        costs[symbol] = {"kind": "synthetic", "purpose": "T121-count-cost", "symbol": symbol,
            "account_kind": "Demo", "account_currency": "USD", "quote_currency": "USD", "basis_ref": "fixture",
            "applies_start": START, "applies_end": END, "observed_start": "2014-01-01", "observed_end": "2014-12-31",
            "payload": payload, "payload_sha256": payload_sha256(payload)}
    (bundle.directory / "legacy-input.json").write_bytes(encoded(doc))
    (bundle.directory / "legacy-costs.json").write_bytes(encoded(costs))
    args = ["--input", str(bundle.directory / "legacy-input.json"), "--cost-evidence", str(bundle.directory / "legacy-costs.json"),
            "--sessions", str(bundle.directory / "sessions.toml"), "--output", str(bundle.out)]
    guard = COLD_GUARD[:COLD_GUARD.index("root, bundle_path,")] + '''
root, *args = sys.argv[1:]
cli = runpy.run_path(str(Path(root) / "scripts/pline_feasibility.py"))
raise SystemExit(cli["main"](args))
'''
    result = subprocess.run([sys.executable, "-I", "-c", guard, str(ROOT), *args], cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    report = json.loads((bundle.out / "report.json").read_bytes())
    assert report["artifact_kind"] == "synthetic-engineering-only" and report["production_execution"] == "not_released"
    assert len(report["rows"]) == 96 and len(report["summaries"]) == 8
    row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"]) == ("F1", "XAUUSD", "S1", "A3"))
    assert row["n_triggers"] == 1 and row["cost_r"] == pytest.approx(.244 / (32 / 14))
    before = (bundle.out / "report.json").read_bytes()
    assert load_cli().main(args) == 2 and (bundle.out / "report.json").read_bytes() == before


# fix1: real, temporary Git registration. No trust/Git/admission/claim mocks.
TRUST_FILE = "src/mt5quant/prob/production_releases.py"


def snapshot_doc(bundle=None, **changes):
    value = {"schema": "t121-trust-v1", "initialized": True, "version": 1,
        "valid_from": "2026-11-04T00:00:00Z", "expires_at": "2026-11-06T00:00:00Z",
        "registry": {} if bundle is None else {bundle.manifest["release_id"]: sha(bundle.raw)},
        "states": {} if bundle is None else {bundle.manifest["release_id"]: "active"},
        "context": "isolated-synthetic-fixture"}
    value.update(changes)
    return value


def static_trust_bytes(value):
    return ('"""Isolated generated trust data; never a production approval."""\n'
            'SNAPSHOT_JSON = ' + repr(encoded(value).decode()) + '\n').encode()


def fixture_git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    cmd = ["git", "--no-replace-objects", "-c", "core.hooksPath=" + str(root / "no-hooks"),
           "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "commit.gpgSign=false",
           "-c", "user.name=T121 Fixture", "-c", "user.email=t121-fixture@invalid", *args]
    result = subprocess.run(cmd, cwd=root, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode().strip()


def make_code_repo(root):
    root.mkdir()
    adapter = load_adapter()
    for name in (*adapter.CODE_FILES, adapter.PREREG):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = (ROOT / name).read_bytes()
        target.write_bytes(raw.replace(b"\r\n", b"\n") if name == adapter.PREREG else raw)
    trust = ROOT / TRUST_FILE
    (root / TRUST_FILE).write_bytes(trust.read_bytes() if trust.exists() else static_trust_bytes(
        snapshot_doc(initialized=False, valid_from=None, expires_at=None, context="production")))
    fixture_git(root, "init", "--initial-branch=fixture")
    fixture_git(root, "add", "--", *adapter.CODE_FILES, adapter.PREREG, TRUST_FILE)
    fixture_git(root, "commit", "-m", "[P] isolated implementation I")
    return root


def register_snapshot(root, value):
    (root / TRUST_FILE).write_bytes(static_trust_bytes(value))
    fixture_git(root, "add", "--", TRUST_FILE)
    fixture_git(root, "commit", "-m", "[P] isolated trust registration")
    assert fixture_git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == TRUST_FILE
    return fixture_git(root, "rev-parse", "HEAD")


def make_registered(tmp_path):
    root = make_code_repo(tmp_path / "implementation")
    bundle = make_bundle(tmp_path / "artifacts", load_adapter(), code_root=root)
    bundle.code_root = root
    bundle.implementation = fixture_git(root, "rev-parse", "HEAD")
    bundle.registration = register_snapshot(root, snapshot_doc(bundle))
    return bundle


NORMAL_LAUNCHER = COLD_GUARD[:COLD_GUARD.index("root, bundle_path,")] + '''
import time
root, bundle_path, output, state, action, sync = sys.argv[1:]
sys.path.insert(0, str(Path(root) / "src"))
import mt5quant.prob.production_adapter as adapter
adapter._clock = lambda: datetime(2026, 11, 5, tzinfo=timezone.utc)
adapter._state_root = lambda: Path(state)
materials = Path(bundle_path).resolve()
paused = False
def observe_io(event, args):
    global paused
    if event != "open" or not isinstance(args[0], str):
        return
    path = Path(args[0]).resolve()
    if path.parent != materials:
        return
    if action == "no-material":
        raise AssertionError("rejected release read material: " + path.name)
    if action == "no-data-cost" and path.name not in ("release.json", "review.json"):
        raise AssertionError("consumed execution read data/cost: " + path.name)
    if action == "pause-review" and path.name == "review.json" and not paused:
        paused = True
        Path(sync + ".ready").write_text("initial admission passed")
        deadline = time.monotonic() + 45
        while not Path(sync + ".go").exists():
            if time.monotonic() > deadline:
                raise AssertionError("synchronization timeout")
            time.sleep(.01)
    if action == "pause-review" and path.name not in ("release.json", "review.json"):
        raise AssertionError("old preflight reached data/cost")
sys.addaudithook(observe_io)
if action == "no-data-cost":
    def observe_compute(frame, event, arg):
        if event == "call" and frame.f_code.co_name in ("_run_feasibility_kernel", "_trigger_cost_kernel"):
            raise AssertionError("consumed execution reached count/cost kernel")
    sys.setprofile(observe_compute)
cli = runpy.run_path(str(Path(root) / "scripts/pline_feasibility.py"))
result = cli["main"](["--mode", "production", "--release-id", "fixture-release",
    "--release-manifest", str(materials / "release.json"), "--output", output])
print("NORMAL_LOADER_MAIN", result)
raise SystemExit(result)
'''


def normal_command(bundle, *, root=None, action="run", output=None, sync="unused"):
    return [sys.executable, "-I", "-B", "-c", NORMAL_LAUNCHER, str(root or bundle.code_root),
            str(bundle.directory), str(output or bundle.out), str(bundle.state), action, str(sync)]


def run_normal(bundle, **kwargs):
    result = subprocess.run(normal_command(bundle, **kwargs), cwd=bundle.code_root,
                            capture_output=True, text=True, timeout=90)
    print("NORMAL_PROCESS", result.returncode, result.stdout.strip(), result.stderr.strip())
    return result


def test_fix1_revoked_observation_prevents_legacy_replay_red(bundle, monkeypatch):
    install_trust(monkeypatch, load_adapter(), bundle, version=2, states={"fixture-release": "revoked"})
    block_data_reads(monkeypatch, bundle)
    assert load_cli().main(args_for(bundle)) == 2
    watermark = bundle.state / "snapshot.json"
    assert watermark.exists(), "trusted revoked v2 was forgotten before advancing watermark"
    assert json.loads(watermark.read_bytes())["version"] == 2
    assert not list(bundle.state.glob("*.claim"))
    result = subprocess.run(child_command(bundle), cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 2 and "old or conflicting" in result.stderr


def test_fix1_real_registration_is_acyclic_and_normal_main_counts(tmp_path):
    bundle = make_registered(tmp_path)
    result = run_normal(bundle)
    assert result.returncode == 0, result.stdout + result.stderr
    assert bundle.implementation != bundle.registration
    assert fixture_git(bundle.code_root, "diff", bundle.implementation, bundle.registration, "--", *load_adapter().CODE_FILES) == ""
    report = json.loads((bundle.out / "report.json").read_bytes())
    receipt = json.loads((bundle.out / "receipt.json").read_bytes())
    assert len(report["rows"]) == 96 and len(report["summaries"]) == 8
    row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"]) == ("F1", "XAUUSD", "S1", "A3"))
    assert row["n_triggers"] == 1 and row["cost_r"] == pytest.approx(.244 / (32 / 14))
    assert report["production_execution"] == "not_released"
    assert receipt["runtime"]["head"] == bundle.registration
    assert receipt["runtime"]["implementation_commit"] == bundle.implementation
    assert receipt["trust_source"]["source_commit"] == bundle.registration
    assert receipt["trust_source"]["raw_sha256"] == sha((bundle.code_root / TRUST_FILE).read_bytes())
    assert receipt["snapshot_sha256"] == sha(encoded(snapshot_doc(bundle)))
    print("REGISTRATION_EVIDENCE", json.dumps({"I": bundle.implementation, "H": bundle.registration,
        "manifest_raw_sha256": sha(bundle.raw), "snapshot_sha256": receipt["snapshot_sha256"],
        "rows": len(report["rows"]), "summaries": len(report["summaries"]), "nonzero_cost_r": row["cost_r"]}))
    other = tmp_path / "second-checkout"
    shutil.copytree(bundle.code_root, other)
    claims_before = {p.name: p.read_bytes() for p in bundle.state.glob("*.claim")}
    report_before = (bundle.out / "report.json").read_bytes()
    again = run_normal(bundle, root=other, output=tmp_path / "other-output", action="no-data-cost")
    assert again.returncode == 2 and "consumed" in again.stderr
    assert {p.name: p.read_bytes() for p in bundle.state.glob("*.claim")} == claims_before
    assert len(claims_before) == 1 and not (tmp_path / "other-output").exists()
    assert (bundle.out / "report.json").read_bytes() == report_before


@pytest.mark.parametrize("state", ["revoked", "unknown", "expired", "future"])
def test_fix1_new_rejected_snapshot_persists_and_blocks_old_process(tmp_path, state):
    bundle = make_registered(tmp_path)
    old = tmp_path / "old-copy"
    shutil.copytree(bundle.code_root, old)
    new = snapshot_doc(bundle, version=2)
    if state == "revoked":
        new["states"] = {"fixture-release": "revoked"}
    elif state == "unknown":
        new.update(registry={}, states={})
    elif state == "expired":
        new["expires_at"] = NOW.isoformat()
    else:
        new.update(valid_from="2026-11-06T00:00:00Z", expires_at="2026-11-07T00:00:00Z")
    register_snapshot(bundle.code_root, new)
    result = run_normal(bundle, action="no-material")
    assert result.returncode == 2
    watermark = json.loads((bundle.state / "snapshot.json").read_bytes())
    assert watermark == {"version": 2, "sha256": sha(encoded(new))}
    assert not list(bundle.state.glob("*.claim")) and not bundle.out.exists()
    replay = run_normal(bundle, root=old, action="no-material")
    assert replay.returncode == 2 and "old or conflicting" in replay.stderr
    assert json.loads((bundle.state / "snapshot.json").read_bytes()) == watermark


@pytest.mark.parametrize("bad", ["missing-state", "unknown-state", "bool-version", "invalid-time", "inverted", "extra", "schema"])
def test_fix1_invalid_snapshot_never_advances_watermark(tmp_path, bad):
    bundle = make_registered(tmp_path)
    value = snapshot_doc(bundle, version=2)
    if bad == "missing-state":
        value["states"] = {}
    elif bad == "unknown-state":
        value["states"]["fixture-release"] = "approved"
    elif bad == "bool-version":
        value["version"] = True
    elif bad == "invalid-time":
        value["valid_from"] = "yesterday"
    elif bad == "inverted":
        value["expires_at"] = value["valid_from"]
    elif bad == "extra":
        value["caller_override"] = True
    else:
        value["schema"] = "other"
    register_snapshot(bundle.code_root, value)
    result = run_normal(bundle, action="no-material")
    assert result.returncode == 2
    assert not bundle.state.exists() and not bundle.out.exists()


def test_fix1_same_version_digest_conflict_and_same_snapshot_repeat(tmp_path):
    bundle = make_registered(tmp_path)
    value = snapshot_doc(bundle, version=2, states={"fixture-release": "revoked"})
    register_snapshot(bundle.code_root, value)
    assert run_normal(bundle, action="no-material").returncode == 2
    before = (bundle.state / "snapshot.json").read_bytes()
    assert run_normal(bundle, action="no-material").returncode == 2
    assert (bundle.state / "snapshot.json").read_bytes() == before
    value["expires_at"] = "2026-11-07T00:00:00Z"
    register_snapshot(bundle.code_root, value)
    result = run_normal(bundle, action="no-material")
    assert result.returncode == 2 and "old or conflicting" in result.stderr
    assert (bundle.state / "snapshot.json").read_bytes() == before


def test_fix1_new_revocation_observed_between_preflight_and_claim(tmp_path):
    import time
    bundle = make_registered(tmp_path)
    old = tmp_path / "old-copy"
    shutil.copytree(bundle.code_root, old)
    sync = tmp_path / "synchronization"
    process = subprocess.Popen(normal_command(bundle, root=old, action="pause-review", sync=sync),
        cwd=old, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 45
        while not Path(str(sync) + ".ready").exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "old preflight did not reach review IO"
            time.sleep(.01)
        register_snapshot(bundle.code_root, snapshot_doc(bundle, version=2, states={"fixture-release": "revoked"}))
        assert run_normal(bundle, action="no-material").returncode == 2
        assert json.loads((bundle.state / "snapshot.json").read_bytes())["version"] == 2
        Path(str(sync) + ".go").write_text("new watermark committed before old claim")
        stdout, stderr = process.communicate(timeout=90)
        assert process.returncode == 2 and "old or conflicting" in stderr, stdout + stderr
        assert not list(bundle.state.glob("*.claim")) and not bundle.out.exists()
        print("REVOCATION_BEFORE_CLAIM_PASS")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize("mutation", ["ancestor-code", "dirty-trust", "wrong-source"])
def test_fix1_registered_source_and_exact_implementation_required(tmp_path, mutation):
    bundle = make_registered(tmp_path)
    if mutation == "ancestor-code":
        path = bundle.code_root / "src/mt5quant/prob/feasibility.py"
        path.write_bytes(path.read_bytes() + b"\n# unrelated later code is still different code\n")
        fixture_git(bundle.code_root, "add", "--", "src/mt5quant/prob/feasibility.py")
        fixture_git(bundle.code_root, "commit", "-m", "[P] isolated changed closure")
    elif mutation == "dirty-trust":
        (bundle.code_root / TRUST_FILE).write_bytes(static_trust_bytes(snapshot_doc(bundle, version=2)))
    else:
        # Working trust matches a registered sibling commit, but not actual HEAD.
        previous = fixture_git(bundle.code_root, "rev-parse", "HEAD")
        register_snapshot(bundle.code_root, snapshot_doc(bundle, version=2))
        fixture_git(bundle.code_root, "update-ref", "refs/heads/fixture", previous)
    result = run_normal(bundle)
    assert result.returncode == 2 and not bundle.out.exists()
    assert not list(bundle.state.glob("*.claim"))
    if mutation != "ancestor-code":
        assert not bundle.state.exists()


# fix2: move only the isolated clock at real closure reads / claim fsync calls.
TIME_LAUNCHER = COLD_GUARD[:COLD_GUARD.index("root, bundle_path,")] + '''
import os
root, bundle_path, output, state, phase, advance_to, expected = sys.argv[1:]
sys.path.insert(0, str(Path(root) / "src"))
import mt5quant.prob.production_adapter as adapter
now = datetime(2026, 11, 5, tzinfo=timezone.utc)
advanced = datetime.fromisoformat(advance_to.replace("Z", "+00:00"))
trace = {"clock_reads": [], "jumped": False, "claim_opens": [],
         "claim_fsync_completed": False, "data_cost_reads": [], "kernel_calls": []}
def clock():
    trace["clock_reads"].append(now.isoformat())
    return now
adapter._clock = clock
adapter._state_root = lambda: Path(state)
materials, state_path = Path(bundle_path).resolve(), Path(state).resolve()
closure_path = (Path(root) / "src/mt5quant/prob/feasibility.py").resolve()
def advance():
    global now
    now = advanced
    trace["jumped"] = True

def observe_io(event, args):
    if event != "open" or not isinstance(args[0], str):
        return
    path = Path(args[0]).resolve()
    if phase == "closure" and path == closure_path and not trace["jumped"]:
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_name == "_claim":
                advance()
                break
            frame = frame.f_back
    if path.parent == state_path and path.suffix == ".claim":
        trace["claim_opens"].append(now.isoformat())
    if path.parent == materials and path.name not in ("release.json", "review.json", "closure.json"):
        trace["data_cost_reads"].append(path.name)
        if expected == "refuse":
            raise AssertionError("expired execution read data/cost: " + path.name)

def observe_calls(frame, event, arg):
    if event in ("c_call", "c_return") and arg is os.fsync and frame.f_code.co_name == "_write_exclusive":
        path = frame.f_locals["path"]
        if path.parent == state_path and path.suffix == ".claim":
            if event == "c_call" and phase == "persist":
                advance()
            elif event == "c_return":
                trace["claim_fsync_completed"] = True
    if event == "call" and frame.f_code.co_name in ("_run_feasibility_kernel", "_trigger_cost_kernel"):
        trace["kernel_calls"].append(frame.f_code.co_name)
        if expected == "refuse":
            raise AssertionError("expired execution reached count/cost kernel")
sys.addaudithook(observe_io)
sys.setprofile(observe_calls)
cli = runpy.run_path(str(Path(root) / "scripts/pline_feasibility.py"))
try:
    result = cli["main"](["--mode", "production", "--release-id", "fixture-release",
        "--release-manifest", str(materials / "release.json"), "--output", output])
finally:
    sys.setprofile(None)
    print("TIME_EVIDENCE", json.dumps(trace))
raise SystemExit(result)
'''


def run_time_boundary(bundle, phase, advance_to, *, succeeds=False, output=None):
    command = [sys.executable, "-I", "-B", "-c", TIME_LAUNCHER, str(bundle.code_root),
        str(bundle.directory), str(output or bundle.out), str(bundle.state), phase, advance_to,
        "allow" if succeeds else "refuse"]
    result = subprocess.run(command, cwd=bundle.code_root, capture_output=True, text=True, timeout=90)
    print("TIME_PROCESS", result.returncode, result.stdout.strip(), result.stderr.strip())
    evidence = json.loads(next(line.removeprefix("TIME_EVIDENCE ")
        for line in result.stdout.splitlines() if line.startswith("TIME_EVIDENCE ")))
    return result, evidence


@pytest.mark.parametrize("phase,authority,advance_to,succeeds", [
    ("closure", "manifest", "2026-11-05T00:00:02Z", False),
    ("closure", "snapshot", "2026-11-05T00:00:01Z", False),
    ("persist", "manifest", "2026-11-05T00:00:01Z", False),
    ("persist", "snapshot", "2026-11-05T00:00:02Z", False),
    ("persist", "condition", "2026-11-04T23:59:59.999999Z", False),
    ("closure", "valid", "2026-11-05T00:00:00.999999Z", True),
])
def test_fix2_claim_uses_fresh_time_after_closure_and_persistence(
        tmp_path, phase, authority, advance_to, succeeds):
    root = make_code_repo(tmp_path / "implementation")
    bundle = make_bundle(tmp_path / "artifacts", load_adapter(), code_root=root)
    bundle.code_root = root
    bundle.manifest["snapshot_version"] = 2
    if authority in ("manifest", "valid"):
        bundle.manifest["expires_at"] = "2026-11-05T00:00:01Z"
    closure = ref_file(bundle.directory, "closure.json", {
        "condition_id": "time-boundary", "status": "closed", "closed_at": NOW.isoformat()})
    bundle.manifest["conditions"] = [{"condition_id": "time-boundary", "status": "closed", "evidence_ref": closure}]
    reseal(bundle)
    snapshot = snapshot_doc(bundle, version=2)
    if authority in ("snapshot", "valid"):
        snapshot["expires_at"] = "2026-11-05T00:00:01Z"
    implementation = fixture_git(root, "rev-parse", "HEAD")
    registration = register_snapshot(root, snapshot)
    print("TIME_REGISTRATION", json.dumps({"I": implementation, "H": registration,
        "phase": phase, "authority": authority, "advance_to": advance_to,
        "manifest_raw_sha256": sha(bundle.raw), "snapshot_sha256": sha(encoded(snapshot))}))
    result, trace = run_time_boundary(bundle, phase, advance_to, succeeds=succeeds)
    assert trace["jumped"], "clock did not advance at the actual closure/persistence IO"
    assert result.returncode == (0 if succeeds else 2), result.stdout + result.stderr
    claims = {p.name: p.read_bytes() for p in bundle.state.glob("*.claim")}
    assert len(claims) == (1 if phase == "persist" or succeeds else 0)
    assert trace["claim_fsync_completed"] == bool(claims)
    watermark = (bundle.state / "snapshot.json").read_bytes()
    assert json.loads(watermark) == {"version": 2, "sha256": sha(encoded(snapshot))}
    assert not (bundle.state / "claim.lock").exists()
    if succeeds:
        report = json.loads((bundle.out / "report.json").read_bytes())
        assert len(report["rows"]) == 96 and len(report["summaries"]) == 8
        row = next(r for r in report["rows"] if (r["family"], r["symbol"], r["situation"], r["action"]) == ("F1", "XAUUSD", "S1", "A3"))
        assert row["n_triggers"] == 1 and row["cost_r"] == pytest.approx(.10675)
        assert report["production_execution"] == "not_released"
        assert trace["data_cost_reads"] and trace["kernel_calls"]
    else:
        assert not trace["data_cost_reads"] and not trace["kernel_calls"]
        assert not bundle.out.exists()
        if claims:
            # A fresh interpreter with the original valid clock cannot recover
            # an execution consumed before the post-persistence check refused it.
            retry_out = tmp_path / "retry-output"
            again, retrace = run_time_boundary(bundle, "retry", NOW.isoformat(), output=retry_out)
            assert again.returncode == 2 and "consumed" in again.stderr
            assert not retrace["data_cost_reads"] and not retrace["kernel_calls"]
            assert {p.name: p.read_bytes() for p in bundle.state.glob("*.claim")} == claims
            assert (bundle.state / "snapshot.json").read_bytes() == watermark
            assert not retry_out.exists()
