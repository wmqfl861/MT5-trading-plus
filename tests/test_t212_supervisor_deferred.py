"""T212: a deployment that cannot be verified is "undeterminable", not refused.

git timing out says nothing about the deployment. Such a round restarts nothing,
keeps the read-only checks, writes a halt only when a role is really down, and
escalates to a refusal only after three rounds in a row. A round counts once
wherever in it git failed to answer, the full check before a restart included.
A round that would restart a role verifies the deployment in full, never from
the cache.
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
    # Staging the file rewrites the index, so the next round no longer reuses the verdict.
    git("add", "stray.txt")
    with pytest.raises(ValueError, match="not clean"):
        module.verify_deployment(cfg, {"clean": record})
    assert calls.count("status") == 3
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


# ---- after the review of 9281257: the count, and a record that cannot be read ----

def status_times_out(subcommand):
    return subprocess.TimeoutExpired(["git"], 15) if subcommand == "status" else None


# A round that reuses the cached verdict, then verifies in full before a restart.
REUSED_THEN_FULL = ["rev-parse", "symbolic-ref", "rev-parse", "symbolic-ref", "status"]


def cached_verdict(module, cfg, live):
    """One full clean verdict, remembered the way a healthy round remembers it."""
    record = module.verify_deployment(cfg)
    (live / "supervisor-state.json").write_text(json.dumps({"clean": record}), encoding="utf-8")
    return record


def healthy_roles(live, cfg, now):
    """A fresh heartbeat and log for every registered role, as a healthy deployment has."""
    for entry in json.loads((live / "instances.json").read_text(encoding="utf-8"))["instances"]:
        key = str(entry["magic"]) if entry["kind"] == "runner" else entry["kind"]
        fresh(live, key, entry["pid"], cfg.sha, now)
        log = Path(entry["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("completed loop\n")
        os.utime(log, (now.timestamp(), now.timestamp()))


def test_rounds_whose_check_before_the_restart_cannot_finish_count_toward_the_limit(tmp_path, monkeypatch):
    """The real verifier over a real repository: nothing stands in for it, only
    "git status" runs out of time. The cached verdict passes the first check of
    every round; it is the full check before the restart that cannot finish.
    Each such round counts, so the third in a row is a refusal."""
    module, cfg, p, live, entries, _ = configured(tmp_path)
    cached_verdict(module, cfg, live)
    del p.items[role(entries, "risk")["pid"]]
    calls = GitCalls(module, monkeypatch, fail=status_times_out)
    for count in (1, 2):
        healthy_roles(live, cfg, NOW + timedelta(minutes=count - 1))
        seen = len(calls.calls)
        result = module.supervise(cfg, p, now=NOW + timedelta(minutes=count - 1))
        assert calls.calls[seen:] == REUSED_THEN_FULL
        assert result["refused"] is False and result["undeterminable"] == count
        assert result["deferred"] == ["risk"] and result["restarted"] == []
        assert state_of(live)["undeterminable"] == count         # the reused verdict has not cleared it
    halt = (live / "halt.json").read_bytes()
    healthy_roles(live, cfg, NOW + timedelta(minutes=2))
    seen = len(calls.calls)
    third = module.supervise(cfg, p, now=NOW + timedelta(minutes=2))
    assert calls.calls[seen:] == REUSED_THEN_FULL
    assert third["refused"] is True and "3 times in a row" in third["reason"]
    assert third["undeterminable"] == 3 and third["deferred"] == [] and third["restarted"] == []
    assert p.started == [] and p.killed == []
    assert len(lines(live, "SUPERVISOR_DEFERRED")) == 2 and len(lines(live, "SUPERVISOR_REFUSED")) == 1
    assert (live / "halt.json").read_bytes() == halt            # a halt already there is left as it is
    assert "undeterminable" not in state_of(live)               # a refusal starts the count over


def test_a_round_that_restarts_after_a_full_check_starts_the_count_over(tmp_path, monkeypatch):
    """Two rounds cannot finish the check before the restart; then git answers and
    the role is restarted. That round was answerable throughout, so the count
    starts over instead of turning the next failure into a refusal."""
    module, cfg, p, live, entries, _ = configured(tmp_path)
    cached_verdict(module, cfg, live)
    del p.items[role(entries, "risk")["pid"]]
    calls = GitCalls(module, monkeypatch, fail=status_times_out)
    for minute in (0, 1):
        healthy_roles(live, cfg, NOW + timedelta(minutes=minute))
        result = module.supervise(cfg, p, now=NOW + timedelta(minutes=minute))
        assert result["undeterminable"] == minute + 1 and result["refused"] is False
    calls.fail = lambda subcommand: None                       # git answers again
    restarted_at = NOW + timedelta(minutes=2)
    healthy_roles(live, cfg, restarted_at)                    # only the missing role is unhealthy
    seen = len(calls.calls)
    restarted = module.supervise(cfg, p, now=restarted_at)
    assert restarted["restarted"] == ["risk"] and restarted["refused"] is False
    assert calls.calls[seen:] == REUSED_THEN_FULL              # exactly one full check before the restart
    assert state_of(live)["undeterminable"] == 0
    registered = json.loads((live / "instances.json").read_text(encoding="utf-8"))["instances"]
    del p.items[role(registered, "risk")["pid"]]               # the restarted role is lost again
    calls.fail = status_times_out
    for minute in (3, 4):
        now = NOW + timedelta(minutes=minute)
        healthy_roles(live, cfg, now)
        result = module.supervise(cfg, p, now=now)
        assert result["refused"] is False and result["undeterminable"] == minute - 2
    healthy_roles(live, cfg, NOW + timedelta(minutes=5))
    again = module.supervise(cfg, p, now=NOW + timedelta(minutes=5))
    assert again["refused"] is True and "3 times in a row" in again["reason"]


def test_the_verdict_verified_in_full_before_a_restart_is_the_one_remembered(tmp_path, monkeypatch):
    """A round that restarts a role verifies in full. That verdict, not the older
    one the round started from, is what the next round finds in the state file."""
    module, cfg, p, live, entries, _ = configured(tmp_path)
    record = module.verify_deployment(cfg)
    older = (datetime.now(timezone.utc) - timedelta(minutes=14)).isoformat()   # still inside the reuse window
    (live / "supervisor-state.json").write_text(json.dumps({"clean": {**record, "at": older}}), encoding="utf-8")
    del p.items[role(entries, "risk")["pid"]]
    calls = GitCalls(module, monkeypatch)
    result = module.supervise(cfg, p, now=NOW)
    assert result["restarted"] == ["risk"] and calls.count("status") == 1   # the full check before the restart
    kept = datetime.fromisoformat(state_of(live)["clean"]["at"])
    assert timedelta(0) <= datetime.now(timezone.utc) - kept < timedelta(minutes=1)   # written by this round


@pytest.mark.parametrize("at", ["garbage", "", "2026-13-45T99:99:99+00:00", "2026-11-23T19:00:00+00:00 later",
                               "0001-01-01T00:00:00+14:00"])     # the last one overflows when converted to UTC
def test_a_timestamp_that_cannot_be_read_only_means_verifying_in_full(tmp_path, monkeypatch, at):
    """A damaged record in the state file is no verdict against the deployment:
    it is neither reused nor a refusal."""
    module, cfg, p, live, _, _ = configured(tmp_path)
    calls = GitCalls(module, monkeypatch)
    record = module.verify_deployment(cfg)
    assert module.verify_deployment(cfg, {"clean": {**record, "at": at}})
    assert calls.count("status") == 2                           # verified in full again
    (live / "supervisor-state.json").write_text(json.dumps({"clean": {**record, "at": at}}), encoding="utf-8")
    result = module.supervise(cfg, p, now=NOW)
    assert result["refused"] is False and result["restarted"] == []
    assert calls.count("status") == 3
    assert not (live / "halt.json").exists() and lines(live, "SUPERVISOR_REFUSED") == []
    assert lines(live, "SUPERVISOR_DEFERRED") == []             # git answered: nothing to defer
    assert state_of(live)["clean"]["at"] != at                  # the damaged record is replaced


def test_no_verdict_is_reused_when_the_fingerprint_cannot_be_read(tmp_path, monkeypatch):
    """None on both sides would compare equal: a fingerprint that cannot be read
    never matches, so the deployment is verified in full."""
    module, cfg, _, _, _, _ = configured(tmp_path)
    calls = GitCalls(module, monkeypatch)
    monkeypatch.setattr(module, "_checkout_fingerprint", lambda directory: None)
    remembered = {"clean": {"fingerprint": None, "at": REAL_NOW.isoformat()}}
    again = module.verify_deployment(cfg, remembered)
    assert calls.count("status") == 1 and isinstance(again, dict)


def test_a_real_change_of_head_is_verified_in_full(tmp_path, monkeypatch):
    """HEAD really moves to another commit; the record is not rewritten to fake it."""
    module, cfg, _, _, _, git = configured(tmp_path)
    git("-c", "user.name=T212 fixture", "-c", "user.email=t212@example.invalid",
        "commit", "--allow-empty", "-qm", "moved")
    moved = replace(cfg, sha=git("rev-parse", "HEAD"))          # the deployment is pinned to the new commit
    calls = GitCalls(module, monkeypatch)
    record = module.verify_deployment(moved)
    assert module.verify_deployment(moved, {"clean": record}) is None
    assert calls.count("status") == 1                           # HEAD and index unchanged: reused
    git("-c", "user.name=T212 fixture", "-c", "user.email=t212@example.invalid",
        "commit", "--allow-empty", "-qm", "moved again")
    moved = replace(cfg, sha=git("rev-parse", "HEAD"))
    assert module.verify_deployment(moved, {"clean": record})
    assert calls.count("status") == 2                           # HEAD changed: verified in full


def test_after_a_refusal_the_rounds_cycle_deferred_deferred_refused(tmp_path):
    """A refusal starts the count over: while git stays silent, every third round refuses."""
    module, cfg, p, live, entries = setup(tmp_path)
    verify = git_cannot_answer(module)
    for minute in range(7):
        module.supervise(cfg, p, now=NOW + timedelta(minutes=minute), verify_deployment=verify)
    kinds = [line.split("\t")[1] for line in (live / "alerts.log").read_text(encoding="utf-8").splitlines()]
    cycle = [kind for kind in kinds if kind in ("SUPERVISOR_DEFERRED", "SUPERVISOR_REFUSED")]
    assert cycle == ["SUPERVISOR_DEFERRED", "SUPERVISOR_DEFERRED", "SUPERVISOR_REFUSED"] * 2 + ["SUPERVISOR_DEFERRED"]
