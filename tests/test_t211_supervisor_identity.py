"""T211 supervisor identity verification: the failure-mode table of runbook §6.3.

Each row of the table is one case over a synthetic process table, driven through
the public entry point with the system root supplied the way the OS boundary
reads it. The decision itself (``_decide_tree``) takes the root as an argument
and the console-host comparison is pure string work. The two production
launch paths are exercised in test_t110_supervisor.py.
"""
import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_t110_supervisor import NOW, fresh, run, setup

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 21, 1, 0, tzinfo=timezone.utc)
SYSTEM = r"C:\Windows"
CONHOST = SYSTEM + r"\System32\conhost.exe"
LAUNCHER = "C:/prod/venv/Scripts/python.exe"
BASE_PY = "C:/Python312/python.exe"
ARGV = [LAUNCHER, "-u", "C:/prod/deploy/scripts/risk_process.py",
        "--data-dir", "C:/prod/data", "--loop"]
ENTRY = {"pid": 100, "started_utc": T0.isoformat()}


@pytest.fixture(scope="module")
def mod():
    path = ROOT / "scripts" / "live_supervisor.py"
    spec = importlib.util.spec_from_file_location("t211_supervisor", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def launcher(pid=100, start=0):
    return dict(pid=pid, parent_pid=1, started_utc=at(start), argv=list(ARGV), executable=LAUNCHER)


def worker(pid=101, parent=100, start=1):
    return dict(pid=pid, parent_pid=parent, started_utc=at(start),
                argv=[BASE_PY, *ARGV[1:]], executable=BASE_PY)


def console(pid=102, parent=101, start=1, image=CONHOST):
    return dict(pid=pid, parent_pid=parent, started_utc=at(start),
                argv=["\\??\\" + image, "0x4"], executable=image)


def other(pid, parent, start, image="C:/Windows/System32/ctfmon.exe"):
    return dict(pid=pid, parent_pid=parent, started_utc=at(start), argv=[image], executable=image)


@contextmanager
def environment(system_root):
    """What the OS boundary would read. None is modelled by an empty value
    rather than by deleting the variable from the real process environment."""
    saved = os.environ.get("SystemRoot")
    os.environ["SystemRoot"] = system_root or ""
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SystemRoot", None)
        else:
            os.environ["SystemRoot"] = saved


def decide(module, *rows, system_root=SYSTEM):
    with environment(system_root):
        return module.verified_tree(ENTRY, {row["pid"]: row for row in rows}, ARGV)


def pids(tree):
    return [item["pid"] for item in tree]


def test_row_1_launcher_and_worker_pass(mod):
    assert pids(decide(mod, launcher(), worker())) == [101, 100]


@pytest.mark.parametrize("parent", [100, 101])
def test_row_2_system_console_leaf_passes_and_stays_out_of_the_tree(mod, parent):
    tree = decide(mod, launcher(), worker(), console(parent=parent))
    assert pids(tree) == [101, 100]


def test_row_3_older_process_citing_the_console_pid_is_a_stale_link(mod):
    day = 86400
    tree = decide(mod, launcher(), worker(), console(), other(103, parent=102, start=-day))
    assert pids(tree) == [101, 100]


def test_row_4_console_host_with_a_process_started_under_it_is_refused(mod):
    with pytest.raises(mod.IdentityError) as caught:
        decide(mod, launcher(), worker(), console(), other(103, parent=102, start=2))
    assert caught.value.detail() == {"role": None, "pid": 102, "parent_pid": 101, "image": "conhost.exe"}


def test_row_5_console_host_outside_the_system_directory_is_refused(mod):
    with pytest.raises(mod.IdentityError, match="script/arguments mismatch"):
        decide(mod, launcher(), worker(), console(image=r"C:\Tools\conhost.exe"))


def test_row_6_launcher_gone_with_only_its_console_host_counts_as_missing(mod):
    assert decide(mod, console(parent=100, start=0)) == []


def test_row_7_unrelated_process_on_the_launcher_pid_counts_as_missing(mod):
    day = 86400
    occupant = other(100, parent=4, start=2 * day)
    assert decide(mod, occupant) == []
    # A child of the process that owns the PID now is not ours.
    assert decide(mod, occupant, other(104, parent=100, start=2 * day + 5)) == []
    # Our launcher's console host outliving it owns nothing.
    assert decide(mod, occupant, console(parent=100, start=0)) == []
    # Something started while our launcher could still have been its parent is an orphan.
    with pytest.raises(mod.IdentityError, match="surviving/duplicate worker"):
        decide(mod, occupant, other(105, parent=100, start=3600))


def test_row_8_matching_arguments_with_another_creation_time_is_refused(mod):
    with pytest.raises(mod.IdentityError, match="PID reused/start-time mismatch"):
        decide(mod, launcher(start=3600), worker(parent=100, start=3601))


def test_row_9_launcher_gone_with_a_surviving_worker_is_refused(mod):
    with pytest.raises(mod.IdentityError, match="surviving/duplicate worker"):
        decide(mod, worker())


def test_row_10_terminal_under_a_worker_is_refused_and_named(mod):
    terminal = other(110, parent=101, start=60, image="C:/Program Files/MetaTrader 5/terminal64.exe")
    with pytest.raises(mod.IdentityError) as caught:
        decide(mod, launcher(), worker(), terminal)
    assert caught.value.detail()["image"] == "terminal64.exe"
    assert "pid=110" in caught.value.describe() and "parent_pid=101" in caught.value.describe()
    assert str(caught.value) == "process script/arguments mismatch"  # the reason itself stays bare


def test_row_11_unregistered_worker_with_matching_arguments_is_refused(mod):
    with pytest.raises(mod.IdentityError, match="unregistered duplicate worker"):
        decide(mod, launcher(), worker(), worker(pid=120, parent=1, start=5))


def test_unknown_system_root_exempts_nothing(mod):
    with pytest.raises(mod.IdentityError, match="script/arguments mismatch"):
        decide(mod, launcher(), worker(), console(), system_root=None)


def test_console_host_path_ignores_case_separators_and_extended_prefix(mod):
    tree = decide(mod, launcher(), worker(),
                  console(image="\\\\?\\c:/WINDOWS/system32/conhost.exe"), system_root="c:/windows/")
    assert pids(tree) == [101, 100]


def test_entry_point_reads_the_system_root_at_the_os_boundary_only(mod, monkeypatch):
    table = {row["pid"]: row for row in (launcher(), worker(), console())}
    monkeypatch.setenv("SystemRoot", SYSTEM)
    assert pids(mod.verified_tree(ENTRY, table, ARGV)) == [101, 100]
    monkeypatch.setenv("SystemRoot", r"D:\Elsewhere")
    with pytest.raises(mod.IdentityError):
        mod.verified_tree(ENTRY, table, ARGV)
    assert pids(mod.verified_tree(ENTRY, table, ARGV, SYSTEM)) == [101, 100]  # explicit wins


def test_console_host_decision_reads_no_environment_and_no_filesystem(mod, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("console host decision reached the OS boundary")
    table = {row["pid"]: row for row in (launcher(), worker(), console(), other(103, parent=102, start=-86400))}
    monkeypatch.setattr(mod, "os_system_root", forbidden)  # the root is an argument here
    assert pids(mod._decide_tree(ENTRY, table, ARGV, SYSTEM)) == [101, 100]
    # The console-host comparison itself is string work only (worker identity
    # checks keep their own path normalization; that is not this decision).
    monkeypatch.setattr(mod, "canonical", forbidden)
    monkeypatch.setattr(mod.os.path, "realpath", forbidden)
    assert mod._console_leaf(console(), table, SYSTEM) is True
    assert mod._console_leaf(console(image=r"C:\Tools\conhost.exe"), table, SYSTEM) is False
    assert mod._console_leaf(console(), table, None) is False
    monkeypatch.undo()
    with environment(None):
        assert mod.os_system_root() is None  # unknown root exempts nothing


# ---- through supervise(): alerts, state file and restart decisions ----

def role(entries, kind):
    return next(e for e in entries if e["kind"] == kind)


def alerts(live, kind):
    text = (live / "alerts.log").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if "\t" + kind + "\t" in line]


def test_refusal_names_role_pid_image_and_parent_in_alert_and_state(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    e = role(entries, "risk")
    p.items[88] = dict(pid=88, parent_pid=e["pid"], started_utc=p.items[e["pid"]]["started_utc"],
                       argv=["C:/Program Files/MetaTrader 5/terminal64.exe"],
                       executable="C:/Program Files/MetaTrader 5/terminal64.exe")
    result = run(module, cfg, p)
    assert result["refused"] and not p.started and not p.killed
    refused = alerts(live, "SUPERVISOR_REFUSED")
    assert len(refused) == 1
    for part in ("role=risk", "pid=88", "image=terminal64.exe", f"parent_pid={e['pid']}"):
        assert part in refused[0], part
    terminal = alerts(live, "SUPERVISOR_TERMINAL_CHILD")
    assert len(terminal) == 1 and "risk" in terminal[0]
    state = json.loads((live / "supervisor-state.json").read_text(encoding="utf-8"))
    assert state["healthy_since"] is None
    assert state["last_refusal"]["reason"] == result["reason"]
    assert (state["last_refusal"]["role"], state["last_refusal"]["pid"],
            state["last_refusal"]["image"], state["last_refusal"]["parent_pid"]) == ("risk", 88, "terminal64.exe", e["pid"])


def test_reused_launcher_pid_restarts_only_that_role(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    e = role(entries, "risk")
    p.items[e["pid"]] = dict(pid=e["pid"], parent_pid=4, started_utc=NOW.isoformat(),
                             argv=["C:/Windows/System32/ctfmon.exe"],
                             executable="C:/Windows/System32/ctfmon.exe")
    result = run(module, cfg, p)
    assert result["restarted"] == ["risk"] and result["refused"] is False
    assert p.killed == []  # the unrelated process on the old PID is never touched


def test_console_host_under_a_live_or_dead_launcher_changes_nothing(tmp_path, monkeypatch):
    module, cfg, p, live, entries = setup(tmp_path)
    monkeypatch.setattr(module, "os_system_root", lambda: SYSTEM)
    e = role(entries, "risk")
    p.items[80] = dict(pid=80, parent_pid=e["pid"], started_utc=p.items[e["pid"]]["started_utc"],
                       argv=["\\??\\" + CONHOST, "0x4"], executable=CONHOST)
    result = run(module, cfg, p)
    assert result["refused"] is False and result["restarted"] == []
    del p.items[e["pid"]]  # launcher exits first, its console host lingers
    result = run(module, cfg, p)
    assert result["restarted"] == ["risk"] and p.killed == []


def healthy_at(live, cfg, entries, now):
    import os
    for e in entries:
        key = str(e["magic"]) if e["kind"] == "runner" else e["kind"]
        fresh(live, key, e["pid"], cfg.sha, now)
        os.utime(e["log"], (now.timestamp(), now.timestamp()))


def test_halt_that_cannot_clear_itself_names_its_blocker(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    module.write_halt_once(str(live), "component_lost", NOW, owner="supervisor", token="ours")
    veto = live / "halt-risk-veto.json"
    veto.write_text(json.dumps(dict(reason="heartbeat_timeout", owner="risk", ts=NOW.isoformat())))
    run(module, cfg, p)
    # Continuous health for five minutes, polled every minute.
    for seconds in (60, 120, 180, 240):
        now = NOW + timedelta(seconds=seconds)
        healthy_at(live, cfg, entries, now)
        run(module, cfg, p, now)
    now = NOW + timedelta(seconds=300)
    healthy_at(live, cfg, entries, now)
    result = run(module, cfg, p, now)
    assert result["cleared"] is False and (live / "halt.json").exists()
    assert result["clear_blocked"] == "halt-risk-veto.json"
    state = json.loads((live / "supervisor-state.json").read_text(encoding="utf-8"))
    assert state["clear_blocked"] == "halt-risk-veto.json"
    veto.unlink()  # an operator removed the stale veto: the next round clears the halt
    now = NOW + timedelta(seconds=360)
    healthy_at(live, cfg, entries, now)
    assert run(module, cfg, p, now)["cleared"] is True
    assert not (live / "halt.json").exists()


def test_stale_alert_names_the_checks_that_failed(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    e = role(entries, "reconcile")
    (live / "heartbeat-reconcile.json").write_text(json.dumps(dict(
        runner_alive=True, ts=(NOW - timedelta(seconds=400)).isoformat(),
        pid=e["pid"], deployment_sha=cfg.sha, reconcile_clean=True)))
    assert run(module, cfg, p)["restarted"] == ["reconcile"]
    line = alerts(live, "SUPERVISOR")[-1]
    assert "heartbeat_or_log_stale" in line and "checks={'reconcile': '" in line
    for check in ("heartbeat=timeout", "pid_in_tree=True", "deployment_sha=ok",
                  "heartbeat_not_before_registry=True", "log_age_s="):
        assert check in line, check


def test_stale_checks_are_kept_in_the_state_file(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    e = role(entries, "reconcile")
    (live / "heartbeat-reconcile.json").write_text(json.dumps(dict(
        runner_alive=True, ts=(NOW - timedelta(seconds=400)).isoformat(),
        pid=e["pid"], deployment_sha=cfg.sha, reconcile_clean=True)))
    assert run(module, cfg, p)["restarted"] == ["reconcile"]
    kept = json.loads((live / "supervisor-state.json").read_text(encoding="utf-8"))["stale_checks"]
    assert set(kept) == {"reconcile"} and "heartbeat=timeout" in kept["reconcile"]


def test_a_halt_behind_a_kill_switch_names_the_blocker(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    module.write_halt_once(str(live), "component_lost", NOW, owner="supervisor", token="ours")
    (live / "killSwitch").touch()
    run(module, cfg, p)
    now = NOW + timedelta(seconds=300)
    healthy_at(live, cfg, entries, now)
    result = run(module, cfg, p, now)
    assert result["cleared"] is False and (live / "halt.json").exists()
    assert result["clear_blocked"] == "killSwitch"
    assert json.loads((live / "supervisor-state.json").read_text(encoding="utf-8"))["clear_blocked"] == "killSwitch"


def test_the_terminal_child_alert_is_an_anomaly():
    spec = importlib.util.spec_from_file_location("t211_demo_check", ROOT / "scripts" / "demo_check.py")
    check = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = check
    spec.loader.exec_module(check)
    assert "SUPERVISOR_TERMINAL_CHILD" in check.FORMAL_ANOMALY_ALERTS


# ---- independent review of 506e57c: a refusal raised while a tree is being
# ended must carry the role, because that tree is looked at again from a new
# snapshot and may differ from the one that passed the registry check ----

def tree_changes_after_the_registry_check(processes, target_pid):
    original, calls = processes.snapshot, []

    def snapshot():
        table = original()
        calls.append(1)
        if len(calls) >= 2:                      # every look after the registry check
            table[target_pid * 10] = dict(table[target_pid], pid=target_pid * 10, parent_pid=target_pid,
                                          argv=[sys.executable, "other.py"])
        return table
    processes.snapshot = snapshot


def test_refusal_while_restarting_names_the_role(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    e = role(entries, "reconcile")
    (live / "heartbeat-reconcile.json").write_text(json.dumps(dict(
        runner_alive=True, ts=(NOW - timedelta(seconds=400)).isoformat(),
        pid=e["pid"], deployment_sha=cfg.sha, reconcile_clean=True)))
    tree_changes_after_the_registry_check(p, e["pid"])
    result = run(module, cfg, p)
    assert result["refused"] is True and f"role=reconcile" in result["reason"]
    assert f"pid={e['pid'] * 10}" in result["reason"]
    refused = alerts(live, "SUPERVISOR_REFUSED")
    assert len(refused) == 1 and "role=reconcile" in refused[0]
    assert json.loads((live / "supervisor-state.json").read_text(encoding="utf-8"))["last_refusal"]["role"] == "reconcile"
    assert p.started == []


def test_refusal_while_stopping_names_the_role(tmp_path):
    module, cfg, p, live, entries = setup(tmp_path)
    tree_changes_after_the_registry_check(p, role(entries, "risk")["pid"])
    (live / "killSwitch").touch()
    stopped = module.stop_deployment(cfg, p, verify_deployment=lambda c: None)
    assert stopped["refused"] is True and "role=risk" in stopped["reason"]
