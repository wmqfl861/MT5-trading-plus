from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "t111b_prepare.py"


SNAPSHOT_FIELDS = {
    "name": "XAUUSD",
    "description": "Gold vs USD",
    "digits": 2,
    "point": 0.01,
    "spread": 20,
    "trade_mode": 4,
    "currency_profit": "USD",
    "currency_margin": "USD",
    "contract_size": 100.0,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
    "filling_mode": 1,
    "trade_stops_level": 30,
    "swap_long": -18.4,
    "swap_short": 9.2,
    "trade_freeze_level": 15,
    "tick_size": 0.01,
    "tick_value": 1.0,
    "trade_calc_mode": 1,
    "margin_initial": 0.005,
    "margin_maintenance": 0.0025,
    "swap_mode": 1,
    "swap_rollover3days": 3,
    "captured_at_utc": "2026-11-03T00:00:00+00:00",
}


def _run_git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
         "-c", "commit.gpgsign=false", "-C", str(path), *args],
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def make_release(
    tmp_path: Path, *, missing: str | None = None, crlf: bool = False,
) -> tuple[Path, str]:
    release = tmp_path / "release"
    release.mkdir(parents=True)
    _run_git(release, "init", "--template=", "-b", "main")
    _run_git(release, "config", "user.email", "test@example.invalid")
    _run_git(release, "config", "user.name", "Test User")
    _run_git(release, "config", "core.autocrlf", "false")
    required = (
        "scripts/live_runner.py",
        "scripts/risk_process.py",
        "scripts/live_reconcile.py",
    )
    for name in required:
        if name != missing:
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"# synthetic {name}\n# offline fixture only\n".encode("utf-8"))
    module = release / "src" / "mt5live" / "gate.py"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"# synthetic non-entry module\n")
    _run_git(release, "add", ".")
    _run_git(release, "commit", "-m", "synthetic release")
    sha = _run_git(release, "rev-parse", "HEAD").stdout.strip()
    _run_git(release, "checkout", "--detach", sha)
    if crlf:
        _run_git(release, "config", "core.autocrlf", "true")
        for name in required:
            if name != missing:
                (release / name).unlink()
        _run_git(release, "checkout-index", "--force", "--all", "--index")
    return release, sha


def write_snapshot(path: Path, snapshot: dict) -> bytes:
    data = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(data)
    return data


def snapshots(*, before_currency: str | None = "__missing__") -> tuple[dict, dict]:
    before_entry = copy.deepcopy(SNAPSHOT_FIELDS)
    if before_currency != "__missing__":
        before_entry["currency_base"] = before_currency
    after_entry = copy.deepcopy(SNAPSHOT_FIELDS)
    after_entry["captured_at_utc"] = "2026-11-03T01:00:00+00:00"
    after_entry["currency_base"] = "XAU"
    return {"XAUUSD": before_entry}, {"XAUUSD": after_entry}


def invoke(
    release: Path,
    sha: str,
    before: Path,
    after: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--release-dir",
            str(release),
            "--expected-sha",
            sha,
            "--before-snapshot",
            str(before),
            "--after-snapshot",
            str(after),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def parse_output(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def prepare_inputs(tmp_path: Path, *, crlf: bool = False) -> tuple[Path, str, Path, Path]:
    release, sha = make_release(tmp_path, crlf=crlf)
    before_data, after_data = snapshots()
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    write_snapshot(before, before_data)
    write_snapshot(after, after_data)
    return release, sha, before, after


def test_help_is_available():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--release-dir" in result.stdout
    assert "--before-snapshot" in result.stdout


def test_accepts_fixed_detached_clean_release_and_currency_base_addition(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)

    assert result.returncode == 0, result.stderr
    assert manifest["schema_version"] == 1
    assert manifest["static_checks_passed"] is True
    assert manifest["release"]["sha"] == sha
    assert manifest["release"]["detached"] is True
    assert manifest["release"]["clean"] is True
    assert [item["path"] for item in manifest["release"]["artifacts"]] == [
        "scripts/live_reconcile.py",
        "scripts/live_runner.py",
        "scripts/risk_process.py",
    ]
    assert all(len(item["sha256"]) == 64 for item in manifest["release"]["artifacts"])
    assert manifest["snapshots"]["allowed_diff"] == {
        "field": "currency_base",
        "added_records": 1,
    }
    assert manifest["runtime_checks_pending"]
    assert manifest["deployment_prerequisites_complete"] is False
    assert {item["name"] for item in manifest["static_prerequisites_pending"]} == {
        "data_dir", "config_dir", "python", "gateway_config", "risk_config",
        "sessions_config", "spread_model", "log_dir",
    }
    for item in manifest["release"]["artifacts"]:
        assert item["sha256"] == hashlib.sha256((release / item["path"]).read_bytes()).hexdigest()


def test_output_is_deterministic_and_does_not_mutate_inputs(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    before_bytes = before.read_bytes()
    after_bytes = after.read_bytes()
    first = invoke(release, sha, before, after)
    second = invoke(release, sha, before, after)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert before.read_bytes() == before_bytes
    assert after.read_bytes() == after_bytes


def test_rejects_same_tree_different_commit_sha(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    _run_git(release, "commit", "--allow-empty", "-m", "same tree different sha")
    other_sha = _run_git(release, "rev-parse", "HEAD").stdout.strip()
    assert other_sha != sha
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert manifest["static_checks_passed"] is False
    assert "release_sha_mismatch" in manifest["errors"]


@pytest.mark.parametrize("change", ["attached", "dirty", "untracked"])
def test_rejects_attached_dirty_or_untracked_release(tmp_path, change):
    release, sha, before, after = prepare_inputs(tmp_path)
    if change == "attached":
        _run_git(release, "checkout", "-B", "release")
    elif change == "dirty":
        (release / "scripts" / "live_runner.py").write_text("dirty\n", encoding="utf-8")
    else:
        (release / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert manifest["static_checks_passed"] is False
    expected = {
        "attached": "release_attached",
        "dirty": "release_not_clean",
        "untracked": "release_not_clean",
    }[change]
    assert expected in manifest["errors"]


def test_rejects_nonworktree_and_missing_required_artifact(tmp_path):
    nonworktree = tmp_path / "plain"
    nonworktree.mkdir()
    before_data, after_data = snapshots()
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    write_snapshot(before, before_data)
    write_snapshot(after, after_data)
    result = invoke(nonworktree, "0" * 40, before, after)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert "release_not_git_worktree" in manifest["errors"]

    release, sha = make_release(tmp_path / "missing", missing="scripts/risk_process.py")
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert "required_artifact_missing:scripts/risk_process.py" in manifest["errors"]


def test_checks_supplied_paths_without_echoing_absolute_values(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    data_dir = tmp_path / "private-account-data"
    data_dir.mkdir()
    result = invoke(release, sha, before, after, "--path", f"data_dir={data_dir}")
    manifest = parse_output(result)
    assert result.returncode == 0, result.stderr
    assert manifest["paths"] == [{"name": "data_dir", "status": "present", "kind": "directory"}]
    assert str(tmp_path) not in result.stdout
    assert "private-account-data" not in result.stdout

    missing = tmp_path / "missing-secret-path"
    result = invoke(release, sha, before, after, "--path", f"log_dir={missing}")
    manifest = parse_output(result)
    assert result.returncode != 0
    assert "path_missing:log_dir" in manifest["errors"]
    assert str(missing) not in result.stdout


def test_rejects_before_after_path_alias(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    after.write_bytes(before.read_bytes())
    result = invoke(release, sha, before, before)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert "snapshot_paths_alias" in manifest["errors"]


def test_rejects_changed_existing_field(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    data = json.loads(after.read_text(encoding="utf-8"))
    data["XAUUSD"]["swap_long"] = -25.0
    write_snapshot(after, data)
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert "snapshot_diff_not_allowed" in manifest["errors"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda before, after: before.pop("XAUUSD"),
        lambda before, after: after.update({"BTCUSD": copy.deepcopy(after["XAUUSD"])}),
        lambda before, after: before["XAUUSD"].update({"currency_base": "USD"}),
        lambda before, after: after["XAUUSD"].update({"unexpected": 1}),
        lambda before, after: after["XAUUSD"].update({"currency_base": ""}),
        lambda before, after: after["XAUUSD"].update({"spread": True}),
    ],
    ids=["deleted-symbol", "added-symbol", "changed-prior-field", "extra-field", "empty-currency", "bool-int-type"],
)
def test_rejects_snapshot_schema_or_disallowed_diff(tmp_path, mutate):
    release, sha, before_path, after_path = prepare_inputs(tmp_path)
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    mutate(before, after)
    write_snapshot(before_path, before)
    write_snapshot(after_path, after)
    result = invoke(release, sha, before_path, after_path)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert manifest["static_checks_passed"] is False
    assert any(error.startswith("snapshot_") for error in manifest["errors"])


def test_rejects_malformed_duplicate_and_nonfinite_snapshot_json(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    before.write_text('{"XAUUSD": {"name": "XAUUSD"}, "XAUUSD": {}}', encoding="utf-8")
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert "snapshot_before_duplicate_json_key" in manifest["errors"]

    before_data, _ = snapshots()
    write_snapshot(before, before_data)
    after.write_text('{"XAUUSD": NaN}', encoding="utf-8")
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert "snapshot_after_nonfinite_number" in manifest["errors"]


def test_snapshot_hashes_are_sha256_and_privacy_safe(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    data = json.loads(before.read_text(encoding="utf-8"))
    data["XAUUSD"]["description"] = "account=secret-host-token"
    before_bytes = write_snapshot(before, data)
    after_data = json.loads(after.read_text(encoding="utf-8"))
    after_data["XAUUSD"]["description"] = data["XAUUSD"]["description"]
    write_snapshot(after, after_data)
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode == 0, result.stderr
    assert manifest["snapshots"]["before_sha256"] == hashlib.sha256(before_bytes).hexdigest()
    assert "account=secret-host-token" not in result.stdout
    assert str(before) not in result.stdout
    assert "secret-host-token" not in result.stdout


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_rejects_artifact_modified_behind_clean_git_status(tmp_path, flag):
    release, sha, before, after = prepare_inputs(tmp_path)
    artifact = "scripts/live_runner.py"
    _run_git(release, "update-index", flag, artifact)
    (release / artifact).write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    assert not _run_git(release, "status", "--porcelain").stdout.strip()
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode != 0
    assert f"required_artifact_not_at_head:{artifact}" in manifest["errors"]


def test_git_checks_do_not_execute_repository_fsmonitor_hook(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    sentinel = tmp_path / "hook-executed"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    _run_git(release, "config", "core.fsmonitor", str(hook))
    result = invoke(release, sha, before, after)
    assert not sentinel.exists()
    assert result.returncode == 0, result.stdout


def test_empty_snapshots_are_not_valid_authorized_snapshots(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path)
    write_snapshot(before, {})
    write_snapshot(after, {})
    result = invoke(release, sha, before, after)
    assert result.returncode != 0
    assert parse_output(result)["static_checks_passed"] is False


@pytest.mark.parametrize("flags", [
    ("--assume-unchanged",), ("--skip-worktree",),
    ("--assume-unchanged", "--skip-worktree"),
], ids=["assume-unchanged", "skip-worktree", "both"])
@pytest.mark.parametrize("modified", [False, True], ids=["unchanged", "modified"])
def test_rejects_hidden_index_flags_on_non_entry_module(tmp_path, flags, modified):
    release, sha, before, after = prepare_inputs(tmp_path)
    module = "src/mt5live/gate.py"
    for flag in flags:
        _run_git(release, "update-index", flag, module)
    if modified:
        (release / module).write_bytes(b"raise RuntimeError('must not run')\n")
    assert not _run_git(release, "status", "--porcelain").stdout.strip()
    index = release / ".git" / "index"
    index_before = index.read_bytes()
    module_before = (release / module).read_bytes()

    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode == 1
    assert manifest["static_checks_passed"] is False
    assert manifest["release"]["clean"] is False
    assert "release_hidden_index_flags" in manifest["errors"]
    assert index.read_bytes() == index_before
    assert (release / module).read_bytes() == module_before


def test_accepts_lf_blobs_with_crlf_text_artifacts_and_hashes_actual_bytes(tmp_path):
    release, sha, before, after = prepare_inputs(tmp_path, crlf=True)
    eol = _run_git(release, "ls-files", "--eol", "--", "scripts").stdout.splitlines()
    assert len(eol) == 3
    assert all(line.split()[:2] == ["i/lf", "w/crlf"] for line in eol)
    assert not _run_git(release, "status", "--porcelain").stdout.strip()
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode == 0, manifest["errors"]
    assert manifest["static_checks_passed"] is True
    assert manifest["release"]["clean"] is True
    for item in manifest["release"]["artifacts"]:
        actual = (release / item["path"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(actual).hexdigest()
        assert item["sha256"] != hashlib.sha256(actual.replace(b"\r\n", b"\n")).hexdigest()


@pytest.mark.parametrize("artifact", [
    "scripts/live_reconcile.py", "scripts/live_runner.py", "scripts/risk_process.py",
])
def test_crlf_equivalence_rejects_substantive_artifact_edit(tmp_path, artifact):
    release, sha, before, after = prepare_inputs(tmp_path, crlf=True)
    path = release / artifact
    path.write_bytes(path.read_bytes() + b"raise RuntimeError('must not run')\r\n")
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode == 1
    assert manifest["static_checks_passed"] is False
    assert f"required_artifact_not_at_head:{artifact}" in manifest["errors"]


@pytest.mark.parametrize("change", ["bare-cr", "mixed-eol", "bom", "utf16", "trailing-space"])
def test_crlf_equivalence_does_not_allow_other_text_changes(tmp_path, change):
    release, sha, before, after = prepare_inputs(tmp_path, crlf=True)
    artifact = "scripts/live_runner.py"
    path = release / artifact
    raw = path.read_bytes()
    changed = {
        "bare-cr": raw.replace(b"\r\n", b"\r"),
        "mixed-eol": raw.replace(b"\r\n", b"\n", 1),
        "bom": b"\xef\xbb\xbf" + raw,
        "utf16": raw.decode("utf-8").encode("utf-16"),
        "trailing-space": raw.replace(b"\r\n", b" \r\n", 1),
    }[change]
    path.write_bytes(changed)
    result = invoke(release, sha, before, after)
    manifest = parse_output(result)
    assert result.returncode == 1
    assert f"required_artifact_not_at_head:{artifact}" in manifest["errors"]


@pytest.mark.parametrize("case", [
    "missing-required", "unknown-option", "unknown-value", "missing-value",
    "malformed-help", "end-of-options-value",
])
def test_cli_parse_failures_are_fixed_redacted_json(tmp_path, case):
    # Parsing failures must finish without touching even the synthetic paths.
    private = str(tmp_path / "SYNTHETIC-PRIVATE-ACCOUNT" / "gateway.json")
    valid = [
        "--release-dir", private, "--expected-sha", "0" * 40,
        "--before-snapshot", private + ".before", "--after-snapshot", private + ".after",
    ]
    argv = {
        "missing-required": ["--release-dir", private],
        "unknown-option": valid + ["--gateway-config", private],
        "unknown-value": valid + [private],
        "missing-value": valid + ["--path"],
        "malformed-help": valid + ["--help=" + private],
        "end-of-options-value": valid + ["--", private],
    }[case]
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *argv], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 2
    assert result.stderr == ""
    assert parse_output(result) == {
        "schema_version": 1, "static_checks_passed": False,
        "errors": ["cli_arguments_invalid"],
    }
    for text in (result.stdout, result.stderr):
        assert "SYNTHETIC-PRIVATE-ACCOUNT" not in text
        assert private not in text
        assert str(SCRIPT) not in text


@pytest.mark.parametrize("label", ["before", "after"])
def test_deep_json_returns_stable_snapshot_error_without_traceback(tmp_path, label):
    release, sha, before, after = prepare_inputs(tmp_path)
    path = before if label == "before" else after
    raw = b"[" * 10000 + b"0" + b"]" * 10000
    path.write_bytes(raw)
    first = invoke(release, sha, before, after)
    second = invoke(release, sha, before, after)
    assert first.returncode == second.returncode == 1
    assert first.stderr == second.stderr == ""
    assert first.stdout == second.stdout
    manifest = parse_output(first)
    assert manifest["static_checks_passed"] is False
    assert manifest["errors"] == [f"snapshot_{label}_invalid_json"]
    assert path.read_bytes() == raw
    assert str(path) not in first.stdout
    assert str(SCRIPT) not in first.stdout
