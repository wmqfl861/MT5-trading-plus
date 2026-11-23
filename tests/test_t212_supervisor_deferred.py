"""T212: a deployment that cannot be verified is "undeterminable", not refused.

git timing out says nothing about the deployment. Such a round restarts nothing,
keeps the read-only checks, writes a halt only when a role is really down, and
escalates to a refusal only after three rounds in a row. A round that would
restart a role verifies the deployment in full, never from the cache.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_t110_supervisor import NOW, fresh, setup

ROOT = Path(__file__).resolve().parents[1]
REAL_NOW = datetime.now(timezone.utc)


def load():
    path = ROOT / "scripts" / "live_supervisor.py"
    spec = importlib.util.spec_from_file_location("t212_supervisor", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git_cannot_answer(module):
    def verify(cfg):
        raise module.DeploymentUndeterminable("git status: TimeoutExpired")
    return verify


def lines(live, kind):
    path = live / "alerts.log"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [line for line in text.splitlines() if "\t" + kind + "\t" in line]


def role(entries, kind):
    return next(e for e in entries if e["kind"] == kind)


def state_of(live):
    return json.loads((live / "supervisor-state.json").read_text(encoding="utf-8"))


# ---- through supervise(), with a verifier that cannot answer ----

def test_one_unverifiable_round_with_healthy_roles_writes_no_halt(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    result = module.supervise(cfg, p, now=NOW, verify_deployment=git_cannot_answer(module))
    assert result["refused"] is False and result["restarted"] == [] and result["deferred"] == []
    assert result["undeterminable"] == 1 and "TimeoutExpired" in result["deferred_reason"]
    assert not (live / "halt.json").exists()
    assert p.started == [] and p.killed == []
    assert len(lines(live, "SUPERVISOR_DEFERRED")) == 1 and lines(live, "SUPERVISOR_REFUSED") == []
    assert "all roles healthy" in lines(live, "SUPERVISOR_DEFERRED")[0]
    assert state_of(live)["undeterminable"] == 1


def test_a_verifier_that_runs_out_of_time_is_treated_as_undeterminable(tmp_path):
    """What a git timeout looks like if it surfaces from the verifier itself (probe P23-4)."""
    module, cfg, p, live, entries = setup(tmp_path)
    def timed_out(cfg):
        raise subprocess.TimeoutExpired(cmd=["git", "-C", "<deploy>", "status"], timeout=15)
    result = module.supervise(cfg, p, now=NOW, verify_deployment=timed_out)
    assert result["refused"] is False and result["undeterminable"] == 1
    assert not (live / "halt.json").exists() and p.started == [] and p.killed == []
    assert len(lines(live, "SUPERVISOR_DEFERRED")) == 1 and lines(live, "SUPERVISOR_REFUSED") == []


def test_three_unverifiable_rounds_in_a_row_escalate_to_a_refusal(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    verify = git_cannot_answer(module)
    for minute in (0, 1):
        result = module.supervise(cfg, p, now=NOW + timedelta(minutes=minute), verify_deployment=verify)
        assert result["refused"] is False and not (live / "halt.json").exists()
    third = module.supervise(cfg, p, now=NOW + timedelta(minutes=2), verify_deployment=verify)
    assert third["refused"] is True and "3 times in a row" in third["reason"]
    assert json.loads((live / "halt.json").read_text(encoding="utf-8"))["reason"] == "supervisor_refused"
    assert len(lines(live, "SUPERVISOR_REFUSED")) == 1 and p.started == []


def test_a_round_that_could_verify_resets_the_count(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    verify = git_cannot_answer(module)
    for minute, verifier in enumerate((verify, verify, lambda cfg: None, verify, verify)):
        result = module.supervise(cfg, p, now=NOW + timedelta(minutes=minute), verify_deployment=verifier)
        assert result["refused"] is False
    assert result["undeterminable"] == 2                     # 2, then 0, then 1, 2: never three in a row


def test_unverifiable_round_with_a_missing_role_halts_but_does_not_restart(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    del p.items[role(entries, "risk")["pid"]]
    result = module.supervise(cfg, p, now=NOW, verify_deployment=git_cannot_answer(module))
    assert result["refused"] is False and result["deferred"] == ["risk"]
    assert p.started == [] and p.killed == []
    assert json.loads((live / "halt.json").read_text(encoding="utf-8"))["reason"] == "risk_lost"
    deferred = lines(live, "SUPERVISOR_DEFERRED")
    assert len(deferred) == 1 and "restarts deferred for ['risk']" in deferred[0]
    assert lines(live, "SUPERVISOR_REFUSED") == []
    # The next round that can verify the deployment performs the restart.
    later = module.supervise(cfg, p, now=NOW + timedelta(minutes=1), verify_deployment=lambda cfg: None)
    assert later["restarted"] == ["risk"] and later["deferred"] == []


def test_an_existing_halt_is_not_lifted_while_the_deployment_is_unverifiable(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    module.write_halt_once(str(live), "component_lost", NOW, owner="supervisor", token="ours")
    run = module.supervise(cfg, p, now=NOW + timedelta(minutes=10),
                           verify_deployment=git_cannot_answer(module))
    assert run["refused"] is False and run["cleared"] is False and (live / "halt.json").exists()


# ---- the real verifier: what is checked when, and the cache ----

def configured(tmp_path):
    """The supervisor fixture, with its deployment directory turned into a detached,
    clean one-commit git repository holding the three role scripts."""
    module, cfg, p, live, entries = setup(tmp_path)
    repo = Path(cfg.directory)
    (repo / "scripts").mkdir(parents=True)
    for name in ("live_runner.py", "risk_process.py", "live_reconcile.py"):
        (repo / "scripts" / name).write_text("", encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    git("init", "-q")
    git("add", "scripts")
    git("-c", "user.name=T212 fixture", "-c", "user.email=t212@example.invalid", "commit", "-qm", "fixture")
    sha = git("rev-parse", "HEAD")
    git("checkout", "--detach", "-q", sha)
    cfg = replace(cfg, sha=sha)
    for entry in entries:
        entry["deployment_sha"] = sha
        key = str(entry["magic"]) if entry["kind"] == "runner" else entry["kind"]
        fresh(live, key, entry["pid"], sha, NOW)
    (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": entries}), encoding="utf-8")
    return module, cfg, p, live, entries, git


class GitCalls:
    """Records every git call the supervisor module makes and can make one subcommand fail."""
    def __init__(self, module, monkeypatch, fail=None):
        self.calls, self.programs = [], []
        self.fail = fail or (lambda subcommand: None)
        real = module.subprocess.run
        outer = self

        def run(argv, *args, **kwargs):
            if argv and Path(str(argv[0])).name.lower() in ("git", "git.exe"):
                outer.programs.append(str(argv[0]))
                outer.calls.append(argv[3])                 # [git, -C, <directory>, <subcommand>, …]
                outcome = outer.fail(argv[3])
                if outcome is not None:
                    raise outcome
            return real(argv, *args, **kwargs)
        monkeypatch.setattr(module.subprocess, "run", run)

    def count(self, subcommand):
        return self.calls.count(subcommand)


def test_clean_verdict_is_reused_only_while_head_and_index_are_unchanged(tmp_path, monkeypatch):
    module, cfg, _, _, _, _ = configured(tmp_path)
    calls = GitCalls(module, monkeypatch)
    record = module.verify_deployment(cfg)
    assert calls.count("status") == 1 and isinstance(record["fingerprint"], list)
    assert module.verify_deployment(cfg, {"clean": record}) is None
    assert module.verify_deployment(cfg, {"clean": record}) is None
    assert calls.count("status") == 1                       # reused, not re-run
    assert calls.count("rev-parse") == 3 and calls.count("symbolic-ref") == 3   # every round, as before
    again = module.verify_deployment(cfg, {"clean": record}, full=True)
    assert calls.count("status") == 2                       # a full check never uses the cache
    assert module.verify_deployment(cfg, {"clean": again}) is None
    assert calls.count("status") == 2                       # the fresh verdict is reusable in turn
    head, size, mtime = again["fingerprint"]
    module.verify_deployment(cfg, {"clean": {**again, "fingerprint": ["another commit", size, mtime]}})
    module.verify_deployment(cfg, {"clean": {**again, "fingerprint": [head, size + 8, mtime + 1]}})
    assert calls.count("status") == 4                       # HEAD or the index changed: verify again
    old = (REAL_NOW - timedelta(seconds=module.CLEAN_REUSE_S)).isoformat()
    module.verify_deployment(cfg, {"clean": {**again, "at": old}})
    assert calls.count("status") == 5                       # older than the reuse window
    assert calls.programs and all(os.path.isabs(program) for program in calls.programs)


def test_a_dirty_tree_is_caught_by_the_full_check_and_only_there(tmp_path, monkeypatch):
    module, cfg, _, _, _, git = configured(tmp_path)
    calls = GitCalls(module, monkeypatch)
    record = module.verify_deployment(cfg)
    (Path(cfg.directory) / "stray.txt").write_text("not tracked", encoding="utf-8")
    module.verify_deployment(cfg, {"clean": record})        # inside the window the cache still says clean
    with pytest.raises(ValueError, match="not clean"):
        module.verify_deployment(cfg, {"clean": record}, full=True)
    with pytest.raises(ValueError, match="SHA mismatch"):
        module.verify_deployment(replace(cfg, sha="0" * 40), {"clean": record})
    # HEAD attached to the branch: refused whatever the cache says.
    git("checkout", "-q", "-")
    with pytest.raises(ValueError, match="detached"):
        module.verify_deployment(cfg, {"clean": record})
    git("checkout", "--detach", "-q", cfg.sha)


def test_git_not_answering_is_undeterminable_not_a_verdict(tmp_path, monkeypatch):
    module, cfg, _, _, _, _ = configured(tmp_path)
    GitCalls(module, monkeypatch,
             fail=lambda sub: subprocess.TimeoutExpired(["git"], 15) if sub == "status" else None)
    with pytest.raises(module.DeploymentUndeterminable, match="^git status: TimeoutExpired$"):
        module.verify_deployment(cfg)
    with pytest.raises(module.DeploymentUndeterminable, match="^git status: TimeoutExpired$"):
        module.verify_deployment(cfg, {"clean": None}, full=True)
    assert not issubclass(module.DeploymentUndeterminable, ValueError)   # never mistaken for a mismatch


def test_external_programs_are_called_by_absolute_path(tmp_path, monkeypatch):
    module, cfg, _, _, _, _ = configured(tmp_path)
    calls = GitCalls(module, monkeypatch)
    module.verify_deployment(cfg)
    assert calls.programs and all(os.path.isabs(program) for program in calls.programs)
    assert os.path.isabs(module.powershell_exe())
    monkeypatch.setattr(module.shutil, "which", lambda program: None)     # nothing on PATH
    with pytest.raises(module.DeploymentUndeterminable, match="not found"):
        module.verify_deployment(cfg)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "no-such-windows"))   # and no known location
    with pytest.raises(module.DeploymentUndeterminable, match="powershell not found"):
        module.powershell_exe()


def test_restart_round_that_cannot_verify_in_full_halts_and_defers(tmp_path, monkeypatch):
    """The cache says clean, but the full check before the restart cannot complete."""
    module, cfg, p, live, entries, _ = configured(tmp_path)
    record = module.verify_deployment(cfg)
    (live / "supervisor-state.json").write_text(json.dumps({"clean": record}), encoding="utf-8")
    del p.items[role(entries, "risk")["pid"]]
    calls = GitCalls(module, monkeypatch,
                     fail=lambda sub: subprocess.TimeoutExpired(["git"], 15) if sub == "status" else None)
    result = module.supervise(cfg, p, now=NOW)
    assert result["refused"] is False and result["restarted"] == [] and result["deferred"] == ["risk"]
    assert p.started == [] and calls.count("status") == 1    # the cached check passed, the full one ran
    assert json.loads((live / "halt.json").read_text(encoding="utf-8"))["reason"] == "risk_lost"
    assert len(lines(live, "SUPERVISOR_DEFERRED")) == 1 and lines(live, "SUPERVISOR_REFUSED") == []
    state = state_of(live)
    assert state["undeterminable"] == 1 and state["healthy_since"] is None


def test_supervisor_deferred_is_a_recorded_alert_and_stays_classified():
    module = load()
    spec = importlib.util.spec_from_file_location("t212_demo_check", ROOT / "scripts" / "demo_check.py")
    check = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = check
    spec.loader.exec_module(check)
    assert "SUPERVISOR_DEFERRED" in check.FORMAL_RECORDED_ALERTS
    assert "SUPERVISOR_DEFERRED" not in check.FORMAL_ANOMALY_ALERTS
    assert "SUPERVISOR_REFUSED" in check.FORMAL_ANOMALY_ALERTS     # a real mismatch still fails the window
    from tests.test_formal_alert_classification_guard import runtime_alert_kinds
    assert "live_supervisor.py" in runtime_alert_kinds().get("SUPERVISOR_DEFERRED", set())
