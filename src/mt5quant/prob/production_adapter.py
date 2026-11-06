"""T121 byte admission, one durable execution, and the existing count/cost kernel.

No production release exists. Only a separately reviewed code change can populate
its fixed registry/current revocation snapshot. JSON, CLI and environment never
supply trust. Private seams are patched only by isolated synthetic tests.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tomllib

from mt5data.sessions import SessionPolicy, _session_policy_from_dict
from mt5quant.prob.feasibility import (
    RULES, _input_reasons, _run_feasibility_kernel, input_payload, payload_sha256,
)

ROOT = Path(__file__).resolve().parents[3]
PARTS = ("spread", "slippage", "commission", "contract", "overshoot", "swap")
# Scope review B/E (2026-11-06): non-measured evidence never claims an
# observation interval inside the discovery segment (observed=null); its
# authority is basis/evidence_refs, and scenario components additionally
# carry a positive multiplier. Measured keeps the v1 discovery-bounded
# observation window unchanged.
OBSERVATION_KINDS = ("measured", "declared_terms", "spec_snapshot", "scenario")
COMPONENT_FIELDS = ("artifact_ref source_ref component symbol purpose account_scope_id "
                    "account_purpose currency observed_start observed_end_exclusive "
                    "applies_start applies_end_exclusive basis units conversion_basis dependencies")
SYMBOLS = ("XAUUSD", "EURUSD")
PURPOSE = "T121-count-cost"
CUTOFF = datetime(2016, 5, 1, tzinfo=timezone.utc)
PREREG = "docs/research/pline-feasibility-prereg-2026-11-02.md"
PREREG_SHA256 = "c78a78d13ed81c611b5f9599c96235d7bbcefceac6fda54566b103f3e8cb3235"
CODE_FILES = (
    "scripts/pline_feasibility.py", "src/mt5quant/prob/production_adapter.py",
    "src/mt5quant/prob/feasibility.py", "src/mt5quant/prob/counting.py", "src/mt5quant/prob/gaps.py",
    "src/mt5quant/strategies/library/prob/situations.py", "src/mt5quant/strategies/library/prob/actions.py",
    "src/mt5data/sessions.py", "src/mt5data/store.py", "src/mt5data/__init__.py",
    "src/mt5quant/costs/volatility.py", "src/mt5quant/costs/spread.py", "src/mt5quant/costs/slippage.py",
    "src/mt5quant/costs/commission.py", "src/mt5quant/costs/overshoot.py", "src/mt5quant/costs/swap.py",
    "src/mt5quant/costs/audit.py", "src/mt5quant/costs/__init__.py", "src/mt5quant/__init__.py",
    "src/mt5quant/prob/__init__.py", "src/mt5quant/strategies/__init__.py",
    "src/mt5quant/strategies/base.py", "src/mt5quant/strategies/library/__init__.py",
    "src/mt5quant/strategies/library/buy_hold.py",
    "src/mt5quant/strategies/library/b1_donchian_breakout.py",
    "src/mt5quant/strategies/library/b2_opening_range_breakout.py",
    "src/mt5quant/strategies/library/b3_london_breakout.py",
    "src/mt5quant/strategies/library/b4_mean_reversion.py",
    "src/mt5quant/strategies/library/b5_intraday_momentum.py",
    "src/mt5quant/strategies/library/b6_vol_target_long.py",
    "src/mt5quant/strategies/library/prob/__init__.py",
    "src/mt5quant/strategies/library/prob/features.py",
)


TRUST_FILE = "src/mt5quant/prob/production_releases.py"
# Capture this process's implementation before any admission IO. Recheck disk,
# Git I/H and these bytes before claim; a changed checkout cannot rebind a process.
_LOADED_CODE = {name: (ROOT / name).read_bytes() for name in CODE_FILES}


@dataclass(frozen=True)
class _Snapshot:
    version: int
    valid_from: str | None
    expires_at: str | None
    registry: object
    states: object
    context: str
    schema: str
    initialized: bool
    source: object


def _git(*args):
    """Read raw Git objects only; no filters, hooks, replace refs or env overrides."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    return subprocess.check_output(["git", "--no-replace-objects", "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=" + os.devnull, *args], cwd=ROOT, env=env, stderr=subprocess.DEVNULL)


def _same_source(raw, blob):
    # Only exact bytes or complete LF -> CRLF checkout mapping on Windows.
    return raw == blob or (os.name == "nt" and b"\r" not in blob and
                           raw == blob.replace(b"\n", b"\r\n"))


def _trust_snapshot():
    """Load only committed fixed-path literal data; never import/execute trust code."""
    head = _git("rev-parse", "HEAD").decode().strip()
    blob = _git("cat-file", "blob", head + ":" + TRUST_FILE)
    raw = (ROOT / TRUST_FILE).read_bytes()
    _require(_same_source(raw, blob), "unregistered trust bytes")
    source = _git("log", "-1", "--format=%H", head, "--", TRUST_FILE).decode().strip()
    _hash(source, 40)
    _require(_git("cat-file", "blob", source + ":" + TRUST_FILE) == blob, "trust source identity mismatch")
    tree = ast.parse(raw.decode("utf-8"))
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    _require(len(body) == 1 and isinstance(body[0], ast.Assign) and len(body[0].targets) == 1 and
             isinstance(body[0].targets[0], ast.Name) and body[0].targets[0].id == "SNAPSHOT_JSON" and
             isinstance(body[0].value, ast.Constant) and type(body[0].value.value) is str, "invalid static trust envelope")
    data = _json(body[0].value.value.encode())
    _keys(data, "schema initialized version valid_from expires_at registry states context")
    return _Snapshot(**data, source={"head": head, "source_commit": source,
        "git_blob_id": _git("rev-parse", head + ":" + TRUST_FILE).decode().strip(),
        "git_blob_sha256": _sha(blob), "raw_sha256": _sha(raw),
        "git_digest_domain": "git-blob-bytes", "raw_digest_domain": "raw-bytes"})


def _clock():
    return datetime.now(timezone.utc)


def _state_root():
    """OS user profile, not HOME/APPDATA/XDG/env, output, data root or worktree."""
    if os.name == "nt":
        import ctypes
        profile = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x28, None, 0, profile) != 0:
            raise ValueError("OS user profile unavailable")
        home = Path(profile.value)
    else:
        import pwd
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute():
        raise ValueError("OS user profile unavailable")
    return home / ".mt5quant" / "t121-executions-v1"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class AdmissionRefused(ValueError):
    """A constant, non-sensitive admission reason suitable for CLI diagnostics."""


def _require(ok, reason):
    if not ok:
        raise AdmissionRefused(reason)


def _keys(value, keys):
    _require(type(value) is dict and set(value) == set(keys.split()), "invalid or missing fields")


def _id(value):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value), "invalid identity")
    return value


def _hash(value, length=64):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{%d}" % length, value), "invalid digest")
    return value


def _time(value):
    _require(type(value) is str and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value), "invalid aware timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _window(start, end):
    a, b = _time(start), _time(end)
    _require(a < b, "empty or inverted interval")
    return a, b


def _finite(value):
    if type(value) is float:
        _require(math.isfinite(value), "nonfinite number")
    elif isinstance(value, dict):
        for child in value.values():
            _finite(child)
    elif isinstance(value, list):
        for child in value:
            _finite(child)


def _json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            _require(key not in out, "duplicate JSON key")
            out[key] = value
        return out
    def invalid_constant(value):
        raise ValueError("nonfinite JSON number")
    obj = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    _require(type(obj) is dict, "JSON object required")
    _finite(obj)
    return obj


def _ref(ref):
    _keys(ref, "artifact_id artifact_version path format digest_domain sha256")
    _id(ref["artifact_id"])
    _id(ref["artifact_version"])
    _hash(ref["sha256"])
    _require(ref["digest_domain"] == "raw-bytes", "artifact digest domain must be raw-bytes")
    _require(ref["format"] in ("json", "toml"), "unsupported artifact format")
    path = ref["path"]
    _require(type(path) is str and path and "\\" not in path and ":" not in path,
             "invalid relative artifact path")
    p = PurePosixPath(path)
    _require(not p.is_absolute() and all(x not in ("", ".", "..") for x in path.split("/")),
             "invalid relative artifact path")


def verify_artifact_bytes(raw: bytes, expected_ref):
    """Validate a RAW digest before parsing exactly these bytes, never a path.

    canonical-json-v1 is reserved for explicit in-memory rule/review bindings;
    it is never accepted as the identity of a file or release manifest.
    """
    _ref(expected_ref)
    _require(type(raw) is bytes, "bytes required")
    _require(_sha(raw) == expected_ref["sha256"], "artifact bytes differ")
    obj = _json(raw) if expected_ref["format"] == "json" else tomllib.loads(raw.decode("utf-8"))
    _finite(obj)
    return obj


def check_t121_admission(input_meta, cost_meta, review_binding):
    """Pure declaration only: cannot verify an authority, read or release anything."""
    missing = ["trusted_external_verification", "independent_execution_release"]
    conflicts = []
    for name, value in (("input_meta", input_meta), ("cost_meta", cost_meta), ("review_binding", review_binding)):
        if type(value) is not dict or not value:
            missing.append(name)
        elif value.get("purpose", PURPOSE) != PURPOSE:
            conflicts.append(name + ".purpose")
    return {"contract_version": "t121-production-admission/draft-1",
            "material_state": "conflicted" if conflicts else "incomplete",
            "missing": missing, "conflicts": conflicts, "identity_bindings": [],
            "review_validity": "unverified", "pending_decisions": ["independent_external_verification"],
            "execution_status": "not_released"}


def _complete_snapshot(snapshot):
    _require(snapshot.schema == "t121-trust-v1" and type(snapshot.initialized) is bool,
             "invalid trust schema")
    _require(type(snapshot.version) is int and snapshot.version > 0, "invalid trust version")
    _require(type(snapshot.registry) is dict and type(snapshot.states) is dict and
             snapshot.registry.keys() == snapshot.states.keys(), "invalid registry/states")
    _require(snapshot.context in ("production", "isolated-synthetic-fixture"), "unknown execution context")
    for release_id, digest in snapshot.registry.items():
        _id(release_id)
        _hash(digest)
        _require(snapshot.states[release_id] in ("active", "revoked"), "invalid release state")
    if not snapshot.initialized:
        _require(not snapshot.registry and snapshot.valid_from is None and snapshot.expires_at is None,
                 "invalid uninitialized snapshot")
        return False
    _window(snapshot.valid_from, snapshot.expires_at)
    return True


def _trusted(snapshot, release_id, now):
    _id(release_id)
    _require(release_id in snapshot.registry, "unknown release")
    _require(snapshot.states[release_id] == "active", "unknown or revoked release")
    _require(isinstance(now, datetime) and now.tzinfo is not None, "trusted time unavailable")
    a, b = _window(snapshot.valid_from, snapshot.expires_at)
    _require(a <= now < b, "trust snapshot not current")


def _scope(scope):
    _keys(scope, "scope_id purpose account_scope_id account_purpose currency quote_currency symbols source_timeframe target_timeframes range_start range_end_exclusive discovery_start discovery_end_exclusive")
    for key in ("scope_id", "account_scope_id"):
        _id(scope[key])
    _require(scope["purpose"] == PURPOSE and scope["account_purpose"] == "Demo" and
             scope["currency"] == scope["quote_currency"] == "USD", "purpose/account scope mismatch")
    _require(type(scope["symbols"]) is list and scope["symbols"] == list(SYMBOLS) and
             scope["source_timeframe"] == "H1" and scope["target_timeframes"] == ["H1", "H4"], "frozen pool/timeframes mismatch")
    start, end = _window(scope["range_start"], scope["range_end_exclusive"])
    discovery, cutoff = _window(scope["discovery_start"], scope["discovery_end_exclusive"])
    _require(discovery <= start < end <= cutoff == CUTOFF, "outside discovery scope")
    return start, end


def _validate_manifest(m, snapshot, release_id, now):
    _keys(m, "schema release_id execution_id snapshot_version valid_from expires_at scope input_ref source_manifest_ref sessions_ref costs code rules conditions review_ref")
    _require(m["schema"] == "t121-release-v1" and m["release_id"] == release_id, "release identity mismatch")
    _id(m["execution_id"])
    _require(type(m["snapshot_version"]) is int and m["snapshot_version"] == snapshot.version, "old trust version")
    a, b = _window(m["valid_from"], m["expires_at"])
    _require(a <= now < b, "release not current")
    start, end = _scope(m["scope"])
    for key, fmt in (("input_ref", "json"), ("source_manifest_ref", "json"), ("sessions_ref", "toml"), ("review_ref", "json")):
        _ref(m[key])
        _require(m[key]["format"] == fmt, "artifact format mismatch")
    _keys(m["costs"], "XAUUSD EURUSD")
    for symbol in SYMBOLS:
        group = m["costs"][symbol]
        _keys(group, " ".join(PARTS))
        for name in PARTS:
            part = group[name]
            kind = part.get("observation_kind", "measured") if type(part) is dict else "measured"
            _require(kind in OBSERVATION_KINDS, "unknown observation kind")
            fields = set(COMPONENT_FIELDS.split())
            if type(part) is dict and "observation_kind" in part:
                fields.add("observation_kind")
            if kind != "measured":
                fields |= {"evidence_refs"}
                if kind == "scenario":
                    fields.add("multiplier")
            _require(type(part) is dict and set(part) == fields, "invalid or missing fields")
            _ref(part["artifact_ref"])
            _ref(part["source_ref"])
            # v1 admits only the reviewed source bytes themselves, not transforms
            # whose source cannot be verified without another format decision.
            _require(part["artifact_ref"] == part["source_ref"] and part["artifact_ref"]["format"] == "json", "unbound component source")
            _require(part["component"] == name and part["symbol"] == symbol and part["purpose"] == PURPOSE,
                     "component purpose/symbol mismatch")
            _require(part["account_scope_id"] == m["scope"]["account_scope_id"] and
                     part["account_purpose"] == "Demo" and part["currency"] == "USD", "component account mismatch")
            applies_start, applies_end = _window(part["applies_start"], part["applies_end_exclusive"])
            _require(applies_start <= start < end <= applies_end <= CUTOFF,
                     "component application outside discovery")
            if kind == "measured":
                observed_start, observed_end = _window(part["observed_start"], part["observed_end_exclusive"])
                _require(observed_start < observed_end <= CUTOFF,
                         "component observation outside discovery")
            else:
                # B: the account did not exist in 2007-2016; declared terms,
                # spec snapshots and scenarios must never be registered as
                # observation intervals ("非观测型记不适用").
                _require(part["observed_start"] is None and part["observed_end_exclusive"] is None,
                         "non-measured components cannot declare observation intervals")
                refs = part["evidence_refs"]
                if type(refs) is str:
                    _require(refs.strip(), "invalid evidence references")
                elif type(refs) is list:
                    _require(refs and all(type(item) is str and item.strip() for item in refs),
                             "invalid evidence references")
                else:
                    _require(False, "invalid evidence references")
                if kind == "scenario":
                    multiplier = part["multiplier"]
                    _require(type(multiplier) in (int, float) and math.isfinite(multiplier) and
                             multiplier > 0, "invalid scenario multiplier")
            for key in ("basis", "units", "conversion_basis"):
                _id(part[key])
            allowed_basis = {"spread": ("absolute", "relative"), "slippage": ("spread-fraction-v1",),
                             "commission": ("round-trip-per-lot",), "contract": ("size-per-lot",),
                             "overshoot": ("stop-level-v2",), "swap": ("free",)}
            _require(part["basis"] in allowed_basis[name], "unreviewed component basis")
            required = {"slippage": ("spread",), "commission": ("contract",), "swap": ("contract",)}.get(name, ())
            _keys(part["dependencies"], " ".join(required))
            for dep in required:
                _require(part["dependencies"][dep] == group[dep]["artifact_ref"], "component dependency mismatch")
    _keys(m["rules"], "version digest_domain sha256 prereg_sha256 prereg_digest_domain")
    _require(m["rules"] == {"version": RULES["version"], "digest_domain": "canonical-json-v1",
             "sha256": _sha(_canonical(RULES)), "prereg_sha256": PREREG_SHA256,
             "prereg_digest_domain": "git-blob-bytes"}, "frozen rule identity mismatch")
    _keys(m["code"], "repo_commit files")
    _hash(m["code"]["repo_commit"], 40)
    _keys(m["code"]["files"], " ".join(CODE_FILES))
    for identity in m["code"]["files"].values():
        _keys(identity, "raw_sha256 git_blob_sha256")
        _hash(identity["raw_sha256"])
        _hash(identity["git_blob_sha256"])
    _require(type(m["conditions"]) is list, "condition list required")
    ids = set()
    for condition in m["conditions"]:
        _keys(condition, "condition_id status evidence_ref")
        _id(condition["condition_id"])
        _require(condition["condition_id"] not in ids and condition["status"] == "closed", "condition not closed")
        ids.add(condition["condition_id"])
        _ref(condition["evidence_ref"])
        _require(condition["evidence_ref"]["format"] == "json", "condition format mismatch")
    _implementation(m)


def _implementation(m):
    head = _git("rev-parse", "HEAD").decode().strip()
    implementation = m["code"]["repo_commit"]
    _git("merge-base", "--is-ancestor", implementation, head)
    for path in CODE_FILES:
        blob = _git("cat-file", "blob", implementation + ":" + path)
        actual_blob = _git("cat-file", "blob", head + ":" + path)
        raw = (ROOT / path).read_bytes()
        identity = m["code"]["files"][path]
        _require(actual_blob == blob and _sha(blob) == identity["git_blob_sha256"] and
                 _same_source(raw, blob) and _sha(raw) == identity["raw_sha256"] and
                 raw == _LOADED_CODE[path], "implementation closure differs")
    prereg_bytes = _git("cat-file", "blob", implementation + ":" + PREREG)
    _require(_sha(prereg_bytes) == PREREG_SHA256, "preregistration bytes mismatch")
    return {"head": head, "implementation_commit": implementation,
            "raw_digest_domain": "raw-bytes", "git_digest_domain": "git-blob-bytes"}


def _read_verified(base, ref, cache):
    _ref(ref)
    path = (base / ref["path"]).resolve()
    _require(path.is_relative_to(base), "artifact escapes manifest directory")
    if path not in cache:
        cache[path] = path.read_bytes()
    return verify_artifact_bytes(cache[path], ref)


def _review(m, base, cache, now):
    review = _read_verified(base, m["review_ref"], cache)
    _keys(review, "schema record_id decision authority_ref reviewer_id independence_basis decided_at valid_from expires_at binding_domain binding_sha256")
    _require(review["schema"] == "t121-review-v1" and review["decision"] == "approved", "review not approved")
    for name in ("record_id", "authority_ref", "reviewer_id"):
        _id(review[name])
    _require(type(review["independence_basis"]) is str and review["independence_basis"].strip(), "independence basis missing")
    a, b = _window(review["valid_from"], review["expires_at"])
    _require(_time(review["decided_at"]) <= a <= now < b and a <= _time(m["valid_from"]) and
             _time(m["expires_at"]) <= b, "review time mismatch")
    _require(review["binding_domain"] == "canonical-json-v1" and
             review["binding_sha256"] == _sha(_canonical({k: v for k, v in m.items() if k != "review_ref"})), "review binding mismatch")
    valid_from = a
    for condition in m["conditions"]:
        closure = _read_verified(base, condition["evidence_ref"], cache)
        _keys(closure, "condition_id status closed_at")
        closed_at = _time(closure["closed_at"])
        _require(closure["condition_id"] == condition["condition_id"] and closure["status"] == "closed" and
                 closed_at <= now, "condition closure mismatch")
        valid_from = max(valid_from, closed_at)
    return valid_from, b


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_exclusive(path, raw):
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _snapshot_bytes(snapshot):
    return _canonical({"schema": snapshot.schema, "initialized": snapshot.initialized,
        "version": snapshot.version, "valid_from": snapshot.valid_from,
        "expires_at": snapshot.expires_at, "registry": snapshot.registry,
        "states": snapshot.states, "context": snapshot.context})


@contextmanager
def _state_lock():
    root = _state_root()
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "claim.lock"
    try:
        _write_exclusive(lock, b"locked\n")
    except FileExistsError:
        raise AdmissionRefused("execution state busy or interrupted; no automatic reclaim") from None
    try:
        yield root
    finally:
        lock.unlink()
        _sync_directory(root)


def _observe_locked(root, snapshot):
    """Record complete trusted snapshots even when no release may execute."""
    current = {"version": snapshot.version, "sha256": _sha(_snapshot_bytes(snapshot))}
    watermark = root / "snapshot.json"
    if watermark.exists():
        previous = _json(watermark.read_bytes())
        _keys(previous, "version sha256")
        _require(type(previous["version"]) is int and previous["version"] > 0, "invalid persistent snapshot")
        _hash(previous["sha256"])
        _require(previous["version"] <= current["version"] and
                 (previous["version"] != current["version"] or previous == current), "old or conflicting trust snapshot")
        if previous == current:
            return
    pending = root / "snapshot.pending"
    _write_exclusive(pending, _canonical(current))
    os.replace(pending, watermark)
    _sync_directory(root)


def _observe(release_id):
    # Validate before creating state, then reload under the shared exclusion.
    snapshot = _trust_snapshot()
    _require(_complete_snapshot(snapshot), "unknown release: trust uninitialized")
    with _state_lock() as root:
        snapshot = _trust_snapshot()
        _require(_complete_snapshot(snapshot), "unknown release: trust uninitialized")
        _observe_locked(root, snapshot)
        now = _clock()
        _trusted(snapshot, release_id, now)
        return snapshot, now


def _claim(snapshot, m, manifest_hash, base, cache):
    """The authorization point is the final validation + durable claim under lock."""
    with _state_lock() as root:
        latest = _trust_snapshot()
        _require(_complete_snapshot(latest), "trust uninitialized")
        _observe_locked(root, latest)  # newer revoked/unknown/expired also persists
        _require(_snapshot_bytes(latest) == _snapshot_bytes(snapshot), "trust changed during admission")
        now = _clock()
        _trusted(latest, m["release_id"], now)
        _require(latest.registry[m["release_id"]] == manifest_hash, "manifest binding changed")
        _require(_time(m["valid_from"]) <= now < _time(m["expires_at"]), "release not current")
        review_start, review_end = _review(m, base, cache, now)
        runtime = _implementation(m)
        _require(runtime["head"] == latest.source["head"], "code changed during claim")
        claim = root / (_sha(m["execution_id"].encode()) + ".claim")
        claim_bytes = _canonical({"release_id": m["release_id"], "execution_id": m["execution_id"],
            "manifest_raw_sha256": manifest_hash, "snapshot_version": latest.version,
            "snapshot_sha256": _sha(_snapshot_bytes(latest))})
        # These bounds come from the verified snapshot and cached metadata;
        # condition closure times are included in review_start. Finish all
        # hashing/Git/IO before taking the final clock sample.
        valid_from = max(_time(latest.valid_from), _time(m["valid_from"]), review_start)
        expires_at = min(_time(latest.expires_at), _time(m["expires_at"]), review_end)

        def require_current_time():
            current_time = _clock()
            _require(isinstance(current_time, datetime) and current_time.tzinfo is not None,
                     "trusted time unavailable")
            _require(valid_from <= current_time < expires_at, "execution authorization not current")

        require_current_time()
        try:
            _write_exclusive(claim, claim_bytes)
        except FileExistsError:
            raise AdmissionRefused("execution already consumed") from None
        _sync_directory(root)
        # Persistence may itself cross expiry. Refuse data/cost, but never
        # remove the durable claim or roll back the observed snapshot.
        require_current_time()
        return latest, runtime


def _inputs(doc, sources, scope):
    import pandas as pd
    _keys(doc, "schema symbols")
    _keys(sources, "schema symbols")
    _require(doc["schema"] == "t121-input-v1" and sources["schema"] == "t121-sources-v1", "input/source schema mismatch")
    _keys(doc["symbols"], "XAUUSD EURUSD")
    _keys(sources["symbols"], "XAUUSD EURUSD")
    start, end = _scope(scope)
    bars, tags, gaps, evidence = {}, {}, {}, {}
    for symbol in SYMBOLS:
        item, source = doc["symbols"][symbol], sources["symbols"][symbol]
        _keys(item, "bars source_tags gap_spans")
        _keys(source, "used_years sources excluded_months segments")
        _require(type(source["used_years"]) is list and source["used_years"] and all(type(y) is int and 1 <= y <= 2016 for y in source["used_years"]), "invalid source years")
        _require(type(source["sources"]) is list and source["sources"] and all(x in ("mt5", "ext:HistData") for x in source["sources"]), "invalid declared sources")
        _require(type(source["excluded_months"]) is list and all(type(x) is str and re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", x) for x in source["excluded_months"]), "invalid excluded months")
        segments = source["segments"]
        _require(type(segments) is list and segments, "source segments required")
        spans, cursor = [], start
        for segment in segments:
            _keys(segment, "source start end_exclusive")
            a, b = _window(segment["start"], segment["end_exclusive"])
            _require(a == cursor and b <= end and segment["source"] in source["sources"], "source segment scope mismatch")
            spans.append((a, b, segment["source"]))
            cursor = b
        _require(cursor == end, "source segments do not cover scope")
        _require(type(item["bars"]) is list and type(item["source_tags"]) is list and len(item["bars"]) == len(item["source_tags"]), "invalid bars/tags container")
        _require(type(item["gap_spans"]) is list, "explicit gaps required")
        for span in item["gap_spans"]:
            _require(type(span) is list and len(span) == 2, "invalid gap")
            a, b = _window(*span)
            _require(start <= a < b < end, "gap outside scope")
        previous_time = None
        for row, tag in zip(item["bars"], item["source_tags"]):
            _require(type(row) is dict and set(row) in ({"time", "open", "high", "low", "close"}, {"time", "open", "high", "low", "close", "closed"}), "invalid bar fields")
            t = _time(row["time"])
            _require(previous_time is None or previous_time < t, "H1 roots must be unique and ordered")
            previous_time = t
            _require(start <= t < end and t.minute == t.second == t.microsecond == 0, "bar outside hourly scope")
            _require(all(type(row[k]) in (int, float) and math.isfinite(row[k]) and row[k] > 0 for k in ("open", "high", "low", "close")), "invalid OHLC")
            _require(row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"], "invalid OHLC range")
            _require("closed" not in row or type(row["closed"]) is bool, "invalid closed flag")
            # Unknown tags remain explicit waiting_data under the frozen kernel.
            _require(tag is None or type(tag) is str, "invalid source tag")
            known = "mt5" if tag == "mt5" else "ext:HistData" if isinstance(tag, str) and tag.startswith("ext:HistData:") else None
            _require(known is None or any(a <= t < b and known == src for a, b, src in spans), "bar/source segment mismatch")
        frame = pd.DataFrame(item["bars"], columns=["time", "open", "high", "low", "close"] + (["closed"] if any("closed" in row for row in item["bars"]) else []))
        bars[symbol], tags[symbol], gaps[symbol] = frame, item["source_tags"], item["gap_spans"]
        e = {"kind": "production", "purpose": PURPOSE, "range_start": scope["range_start"], "range_end": scope["range_end_exclusive"],
             "source_manifest": source, "payload_sha256": payload_sha256(input_payload(frame, tags[symbol], gaps[symbol]))}
        _require(not _input_reasons(frame, tags[symbol], gaps[symbol], e, kind="production"), "input scope/identity mismatch")
        evidence[symbol] = e
    return bars, tags, gaps, evidence


def run_production(release_id, manifest_path, output):
    """Only executable entry: fixed trust → review → durable claim → bytes → kernel."""
    snapshot, now = _observe(release_id)  # no material read until observation + eligibility
    manifest_path, output = Path(manifest_path), Path(output)
    raw = manifest_path.read_bytes()
    _require(_sha(raw) == snapshot.registry[release_id], "release manifest raw bytes differ")
    m = _json(raw)
    _validate_manifest(m, snapshot, release_id, now)
    base, cache = manifest_path.resolve().parent, {}
    _review(m, base, cache, now)
    _require(not output.exists(), "output directory already exists; refusing overwrite")
    snapshot, runtime = _claim(snapshot, m, _sha(raw), base, cache)
    # Every failure from this point leaves the execution occupied permanently.
    doc = _read_verified(base, m["input_ref"], cache)
    sources = _read_verified(base, m["source_manifest_ref"], cache)
    session_doc = _read_verified(base, m["sessions_ref"], cache)
    _keys(session_doc, "version rollover forbidden costs")
    _keys(session_doc["rollover"], "local_time tz")
    _keys(session_doc["forbidden"], "pre_minutes post_minutes exempt_symbols")
    _keys(session_doc["costs"], "spread_gate_multiplier missing_hour_policy min_days_per_hour")
    _require(type(session_doc["version"]) is int, "invalid sessions version")
    sessions = _session_policy_from_dict(session_doc, "verified sessions bytes")
    _require(sessions == SessionPolicy(exempt_symbols=frozenset({"BTCUSD"})), "unfrozen sessions")
    bars, tags, gaps, evidence = _inputs(doc, sources, m["scope"])
    costs = {}
    for symbol in SYMBOLS:
        group = m["costs"][symbol]
        payload = {name: _read_verified(base, group[name]["artifact_ref"], cache) for name in PARTS}
        _require(payload["swap"].get("basis") == "free", "charged production format not released")
        _require(payload["spread"].get("mode") == group["spread"]["basis"], "spread basis mismatch")
        observed_start = min(_time(p["observed_start"]) for p in group.values())
        observed_end = max(_time(p["observed_end_exclusive"]) for p in group.values())
        # The old kernel checks an inclusive observed endpoint; retain the half-open
        # scope above and supply its last representable instant without expanding it.
        from datetime import timedelta
        costs[symbol] = {"kind": "production", "purpose": PURPOSE, "symbol": symbol,
            "account_kind": "Demo", "account_currency": "USD", "quote_currency": "USD",
            "basis_ref": m["review_ref"]["artifact_id"], "applies_start": m["scope"]["range_start"],
            "applies_end": m["scope"]["range_end_exclusive"], "observed_start": observed_start.isoformat(),
            "observed_end": (observed_end - timedelta(microseconds=1)).isoformat(),
            "payload": payload, "payload_sha256": payload_sha256(payload)}
    report = _run_feasibility_kernel(bars, source_tags=tags, gap_spans=gaps, sessions=sessions,
        input_evidence=evidence, cost_evidence=costs, kind="production")
    identities = {"release_id": release_id, "execution_id": m["execution_id"], "context": snapshot.context,
                  "scope_id": m["scope"]["scope_id"], "snapshot_version": snapshot.version,
                  "manifest_raw_sha256": _sha(raw)}
    report.update(identities)
    report["artifact_kind"] = "t121-admitted-count-cost"
    report["production_execution"] = "consumed" if snapshot.context == "production" else "not_released"
    report_bytes = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    refs = [m[k] for k in ("input_ref", "source_manifest_ref", "sessions_ref", "review_ref")]
    refs += [m["costs"][s][p]["artifact_ref"] for s in SYMBOLS for p in PARTS]
    refs += [c["evidence_ref"] for c in m["conditions"]]
    receipt = {**identities, "runtime": runtime, "trust_source": snapshot.source,
        "snapshot_sha256": _sha(_snapshot_bytes(snapshot)), "snapshot_digest_domain": "canonical-json-v1",
        "production_execution": report["production_execution"], "code": m["code"], "rules": m["rules"],
        "artifacts": [{k: ref[k] for k in ("artifact_id", "artifact_version", "digest_domain", "sha256")} for ref in refs],
        "report_sha256": _sha(report_bytes)}
    output.mkdir(parents=True, exist_ok=False)
    _write_exclusive(output / "report.json", report_bytes)
    _write_exclusive(output / "receipt.json", _canonical(receipt) + b"\n")
    return report
