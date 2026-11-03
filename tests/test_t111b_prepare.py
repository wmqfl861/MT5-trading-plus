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
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def make_release(tmp_path: Path, *, missing: str | None = None) -> tuple[Path, str]:
    release = tmp_path / "release"
    release.mkdir(parents=True)
    _run_git(release, "init", "-b", "main")
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
            path.write_text(f"# synthetic {name}\n", encoding="utf-8")
    _run_git(release, "add", ".")
    _run_git(release, "commit", "-m", "synthetic release")
    sha = _run_git(release, "rev-parse", "HEAD").stdout.strip()
    _run_git(release, "checkout", "--detach", sha)
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


def prepare_inputs(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    release, sha = make_release(tmp_path)
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
