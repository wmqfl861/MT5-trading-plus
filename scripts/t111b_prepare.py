#!/usr/bin/env python3
"""T111b offline release and authorized snapshot pre-check.

This command only reads the supplied Git tree, files, and JSON snapshots.  It
never creates a worktree, executes a release script, reads deployment config or
runtime data, inspects processes, or connects to MT5.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REQUIRED_ARTIFACTS = (
    "scripts/live_reconcile.py",
    "scripts/live_runner.py",
    "scripts/risk_process.py",
)

# This is the public whitelist in src/mt5gw/snapshot.py.  The timestamp is
# metadata and is intentionally excluded from the comparison, as in
# mt5gw.snapshot.diff_snapshots.
SNAPSHOT_FIELDS = (
    "name",
    "description",
    "digits",
    "point",
    "spread",
    "trade_mode",
    "currency_profit",
    "currency_margin",
    "contract_size",
    "volume_min",
    "volume_max",
    "volume_step",
    "filling_mode",
    "trade_stops_level",
    "swap_long",
    "swap_short",
    "trade_freeze_level",
    "tick_size",
    "tick_value",
    "trade_calc_mode",
    "margin_initial",
    "margin_maintenance",
    "swap_mode",
    "swap_rollover3days",
    "currency_base",
)
TIMESTAMP_FIELD = "captured_at_utc"

STRING_FIELDS = {
    "name",
    "description",
    "currency_profit",
    "currency_margin",
}
INT_FIELDS = {
    "digits",
    "spread",
    "trade_mode",
    "filling_mode",
    "trade_stops_level",
    "trade_freeze_level",
    "trade_calc_mode",
    "swap_mode",
    "swap_rollover3days",
}
FLOAT_FIELDS = {
    "point",
    "contract_size",
    "volume_min",
    "volume_max",
    "volume_step",
    "swap_long",
    "swap_short",
    "tick_size",
    "tick_value",
    "margin_initial",
    "margin_maintenance",
}

# These names mirror live_supervisor.Deployment's path fields, excluding the
# release directory (which has its own --release-dir argument).
PATH_SPECS = (
    ("data_dir", "directory"),
    ("config_dir", "directory"),
    ("python", "file"),
    ("gateway_config", "file"),
    ("risk_config", "file"),
    ("sessions_config", "file"),
    ("spread_model", "file"),
    ("log_dir", "directory"),
)
PATH_KINDS = dict(PATH_SPECS)

RUNTIME_CHECKS_PENDING = (
    "maintenance_window_approval",
    "demo_safety_and_runtime_health",
    "process_identity_and_supervisor",
    "official_snapshot_authorization",
    "deployment_and_10_day_window",
)


class _DuplicateKey(ValueError):
    pass


class _NonFinite(ValueError):
    pass


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _NonFinite(value)


def _canonical(path: Path) -> Path:
    return path.resolve(strict=False)


def _has_symlink_component(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _same_file_or_path(left: Path, right: Path) -> bool:
    if _canonical(left) == _canonical(right):
        return True
    try:
        return left.exists() and right.exists() and left.is_file() and right.is_file() and left.samefile(right)
    except OSError:
        return False


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git_blob_id(value: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(value)).encode("ascii") + b"\0" + value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(release: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Ignore ambient Git redirection; disable status hooks, index refresh writes,
    # replace objects and lazy fetching. No input executable is ever invoked.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1",
               GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-c", "core.untrackedCache=false", "-C", str(release), *args],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Keep the exception private: its text may contain the caller's path.
        return subprocess.CompletedProcess(["git"], 125, "", "")


def _check_release(release: Path, expected_sha: str) -> tuple[dict[str, Any], list[str]]:
    info: dict[str, Any] = {
        "sha": "",
        "detached": False,
        "clean": False,
        "artifacts": [],
    }
    errors: list[str] = []

    if not release.exists():
        errors.append("release_missing")
        return info, errors
    if not release.is_dir():
        errors.append("release_not_directory")
        return info, errors
    if _has_symlink_component(release):
        errors.append("release_path_unsafe")
        return info, errors

    inside = _git(release, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        errors.append("release_not_git_worktree")
    else:
        top = _git(release, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or _canonical(Path(top.stdout.strip())) != _canonical(release):
            errors.append("release_not_worktree_root")

    head = _git(release, "rev-parse", "HEAD")
    actual_sha = head.stdout.strip() if head.returncode == 0 else ""
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", actual_sha):
        errors.append("release_head_unavailable")
    else:
        info["sha"] = actual_sha
        if re.fullmatch(r"[0-9a-f]{40}", expected_sha):
            if actual_sha != expected_sha:
                errors.append("release_sha_mismatch")
        else:
            errors.append("expected_sha_invalid")

    symbolic = _git(release, "symbolic-ref", "-q", "HEAD")
    if symbolic.returncode == 1 and not symbolic.stdout.strip():
        info["detached"] = True
    else:
        errors.append("release_attached")

    # Inspect the whole index: status can hide changes outside the entry scripts.
    # -v lowercases assume-unchanged entries; S/s denotes skip-worktree.
    index_flags = _git(release, "ls-files", "-v", "-z")
    hidden_flags = any(
        entry[:1].islower() or entry[:1] == "S"
        for entry in index_flags.stdout.split("\0") if entry
    )
    if index_flags.returncode != 0:
        errors.append("release_index_flags_unavailable")
    elif hidden_flags:
        errors.append("release_hidden_index_flags")

    # Git status may run an attribute clean/process filter. Disable configured
    # filter commands for this query as well as fsmonitor in _git.
    filters = _git(release, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process)$")
    filter_options = []
    for key in filters.stdout.splitlines():
        filter_options.extend(("-c", key + "="))
    status = _git(release, *filter_options, "status", "--porcelain", "--untracked-files=all",
                  "--ignore-submodules=none")
    if status.returncode == 0 and not status.stdout.strip():
        info["clean"] = index_flags.returncode == 0 and not hidden_flags
    else:
        errors.append("release_not_clean")

    for relative in REQUIRED_ARTIFACTS:
        artifact = release / relative
        if not artifact.is_file() or artifact.is_symlink() or _has_symlink_component(artifact):
            errors.append(f"required_artifact_missing:{relative}")
            continue
        try:
            raw = artifact.read_bytes()
            digest = _sha256_bytes(raw)
        except OSError:
            errors.append(f"required_artifact_unreadable:{relative}")
            continue
        # Only these three UTF-8 text scripts may match an LF HEAD blob after
        # uniform CRLF checkout conversion. Keep the digest of the actual bytes;
        # reject mixed/bare CR, binary encodings and all other content changes.
        blob_ids = {_git_blob_id(raw)}
        lf = raw.replace(b"\r\n", b"\n")
        if (b"\r\n" in raw and b"\r" not in lf and b"\0" not in raw
                and raw == lf.replace(b"\n", b"\r\n")):
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                blob_ids.add(_git_blob_id(lf))
        tree = _git(release, "ls-tree", actual_sha, "--", relative)
        entry = tree.stdout.strip().split("\t", 1)
        metadata = entry[0].split()
        if (tree.returncode or len(entry) != 2 or entry[1] != relative
                or len(metadata) != 3 or metadata[0] not in ("100644", "100755")
                or metadata[1] != "blob" or metadata[2] not in blob_ids):
            errors.append(f"required_artifact_not_at_head:{relative}")
        info["artifacts"].append({"path": relative, "sha256": digest})

    return info, errors


def _parse_path_arguments(values: list[str]) -> tuple[dict[str, Path], list[str]]:
    supplied: dict[str, Path] = {}
    errors: list[str] = []
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or name not in PATH_KINDS:
            errors.append("path_argument_invalid")
            continue
        if name in supplied:
            errors.append("path_argument_duplicate")
            continue
        if not raw_path:
            errors.append(f"path_missing:{name}")
            continue
        supplied[name] = Path(raw_path)
    return supplied, errors


def _check_paths(values: list[str]) -> tuple[list[dict[str, str]], list[str], list[str]]:
    supplied, errors = _parse_path_arguments(values)
    checked: list[dict[str, str]] = []
    for name in sorted(supplied):
        path = supplied[name]
        kind = PATH_KINDS[name]
        if not path.is_absolute():
            errors.append(f"path_not_absolute:{name}")
            continue
        if _has_symlink_component(path):
            errors.append(f"path_unsafe:{name}")
            continue
        if not path.exists():
            errors.append(f"path_missing:{name}")
            continue
        if kind == "directory" and not path.is_dir():
            errors.append(f"path_wrong_kind:{name}")
            continue
        if kind == "file" and not path.is_file():
            errors.append(f"path_wrong_kind:{name}")
            continue
        checked.append({"name": name, "status": "present", "kind": kind})

    pending = [name for name, _ in PATH_SPECS if name not in supplied]
    return checked, pending, errors


def _read_json_snapshot(path: Path, label: str) -> tuple[Any, str | None]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None, f"snapshot_{label}_unreadable"
    digest = _sha256_bytes(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, f"snapshot_{label}_invalid_json"
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey:
        return None, f"snapshot_{label}_duplicate_json_key"
    except _NonFinite:
        return None, f"snapshot_{label}_nonfinite_number"
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return None, f"snapshot_{label}_invalid_json"
    return (value, digest), None


def _is_finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_is_finite(key) and _is_finite(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_is_finite(item) for item in value)
    return True


def _type_ok(field: str, value: Any) -> bool:
    if field in STRING_FIELDS:
        return type(value) is str
    if field in INT_FIELDS:
        return type(value) is int
    if field in FLOAT_FIELDS:
        return type(value) is float and math.isfinite(value)
    if field == "currency_base":
        return type(value) is str and bool(value.strip())
    if field == TIMESTAMP_FIELD:
        return type(value) is str and bool(value)
    return False


def _validate_record(symbol: str, record: Any, *, label: str, allow_missing_currency: bool) -> str | None:
    if type(record) is not dict:
        return f"snapshot_{label}_record_invalid"
    allowed = set(SNAPSHOT_FIELDS) | {TIMESTAMP_FIELD}
    required = set(SNAPSHOT_FIELDS) | {TIMESTAMP_FIELD}
    if allow_missing_currency:
        required.remove("currency_base")
    keys = set(record)
    if not required.issubset(keys):
        return f"snapshot_{label}_record_missing_field"
    unknown = keys - allowed
    if unknown:
        return f"snapshot_{label}_unknown_field"
    if record.get("name") != symbol:
        return f"snapshot_{label}_symbol_name_mismatch"
    for field in sorted(keys & allowed):
        if field == "currency_base" and allow_missing_currency and field not in record:
            continue
        if not _type_ok(field, record[field]):
            return f"snapshot_{label}_field_type_invalid"
    if not _is_finite(record):
        return f"snapshot_{label}_nonfinite_number"
    return None


def _validate_snapshot_shape(snapshot: Any, *, label: str, allow_missing_currency: bool) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    symbols: list[str] = []
    if type(snapshot) is not dict or not snapshot:
        return [f"snapshot_{label}_root_invalid"], symbols
    folded: set[str] = set()
    for symbol in sorted(snapshot):
        if type(symbol) is not str or not symbol or symbol.casefold() in folded:
            errors.append(f"snapshot_{label}_duplicate_symbol_name")
            continue
        folded.add(symbol.casefold())
        symbols.append(symbol)
        error = _validate_record(
            symbol,
            snapshot[symbol],
            label=label,
            allow_missing_currency=allow_missing_currency,
        )
        if error:
            errors.append(error)
    return errors, symbols


def _same_typed_value(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return (
            set(left) == set(right)
            and all(_same_typed_value(left[key], right[key]) for key in left)
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_typed_value(a, b) for a, b in zip(left, right)
        )
    return left == right


def _compare_snapshots(before: Any, after: Any) -> tuple[dict[str, Any] | None, list[str]]:
    before_errors, before_symbols = _validate_snapshot_shape(
        before, label="before", allow_missing_currency=True
    )
    after_errors, after_symbols = _validate_snapshot_shape(
        after, label="after", allow_missing_currency=False
    )
    errors = before_errors + after_errors
    if errors:
        return None, errors
    if before_symbols != after_symbols:
        return None, ["snapshot_symbol_set_changed"]

    added_currency = 0
    for symbol in before_symbols:
        old = before[symbol]
        new = after[symbol]
        for field in SNAPSHOT_FIELDS:
            if field == "currency_base":
                if field not in old:
                    added_currency += 1
                elif not _same_typed_value(old[field], new[field]):
                    errors.append("snapshot_diff_not_allowed")
                continue
            if not _same_typed_value(old[field], new[field]):
                errors.append("snapshot_diff_not_allowed")
        # captured_at_utc is the only differing metadata field permitted by the
        # public comparison contract; its type and presence were validated.
    if errors:
        return None, sorted(set(errors))
    return {"field": "currency_base", "added_records": added_currency}, []


def build_manifest(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    expected_sha = args.expected_sha
    release = Path(args.release_dir)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "static_checks_passed": False,
        "deployment_prerequisites_complete": False,
        "static_prerequisites_pending": [],
        "runtime_checks_pending": list(RUNTIME_CHECKS_PENDING),
        "release": {
            "sha": "",
            "detached": False,
            "clean": False,
            "artifacts": [],
        },
        "paths": [],
        "snapshots": {
            "before_sha256": None,
            "after_sha256": None,
            "allowed_diff": None,
        },
        "errors": [],
    }
    errors: list[str] = []

    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        errors.append("expected_sha_invalid")

    release_info, release_errors = _check_release(release, expected_sha)
    manifest["release"] = release_info
    errors.extend(release_errors)

    checked_paths, pending_paths, path_errors = _check_paths(args.paths)
    manifest["paths"] = checked_paths
    manifest["static_prerequisites_pending"] = [
        {"name": name, "status": "not_supplied"} for name in pending_paths
    ]
    errors.extend(path_errors)

    before_path = Path(args.before_snapshot)
    after_path = Path(args.after_snapshot)
    if _same_file_or_path(before_path, after_path):
        errors.append("snapshot_paths_alias")
    else:
        before_result, before_error = _read_json_snapshot(before_path, "before")
        after_result, after_error = _read_json_snapshot(after_path, "after")
        if before_result is not None:
            before_value, before_digest = before_result
            manifest["snapshots"]["before_sha256"] = before_digest
        else:
            before_value = None
        if after_result is not None:
            after_value, after_digest = after_result
            manifest["snapshots"]["after_sha256"] = after_digest
        else:
            after_value = None
        if before_error:
            errors.append(before_error)
        if after_error:
            errors.append(after_error)
        if before_error is None and after_error is None:
            allowed_diff, snapshot_errors = _compare_snapshots(before_value, after_value)
            manifest["snapshots"]["allowed_diff"] = allowed_diff
            errors.extend(snapshot_errors)

    # Preserve deterministic error ordering while retaining the first occurrence.
    manifest["errors"] = sorted(set(errors))
    manifest["deployment_prerequisites_complete"] = (
        not pending_paths and not path_errors
    )
    manifest["static_checks_passed"] = not manifest["errors"]
    return manifest, 0 if not manifest["errors"] else 1


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's message can contain raw private arguments; never emit it.
        failure = {
            "schema_version": SCHEMA_VERSION,
            "static_checks_passed": False,
            "errors": ["cli_arguments_invalid"],
        }
        print(json.dumps(failure, sort_keys=True, separators=(",", ":")))
        self.exit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        description="Read-only T111b fixed-SHA and authorized snapshot pre-check"
    )
    parser.add_argument("--release-dir", required=True, help="existing detached release tree")
    parser.add_argument("--expected-sha", required=True, help="full 40-character lowercase Git SHA")
    parser.add_argument("--before-snapshot", required=True, help="authorized before snapshot JSON")
    parser.add_argument("--after-snapshot", required=True, help="authorized after snapshot JSON")
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="optional existing deployment path check; may be repeated",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest, status = build_manifest(args)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    sys.exit(main())
