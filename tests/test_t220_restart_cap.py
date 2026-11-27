"""T220-C: after three automatic restarts inside a rolling window, a role is latched.

What happened on 11-25 (docs/ops/phase-f-log.md, 2026-11-27, sections 1 and 3.2): a runner
that could not complete one loop wrote no heartbeat, so the supervisor, which runs once a
minute, judged it stale and restarted it every minute, twelve times in a row. From now on,
when a role needs a restart and RESTART_CAP restarts of it are registered inside
RESTART_WINDOW_S, that restart is withheld before anything is ended and the role is
latched. From then on every automatic restart of that role is withheld -- the window
passing does not free it, nor does the role recovering -- until an operator who has checked
the cause lifts the latch with restart-cap-release-<role>.json. The process of the third
restart stays, the halt stays, one SUPERVISOR_RESTART_CAP alert is written per stretch of
withheld restarts, and the supervisor keeps watching the other roles. The count is kept in
supervisor-state.json under the supervisor lock and written before the process is touched.
The first restart after an instance left by itself with exit code 78 is never withheld.

Every round here is one call of supervise() with the process table and the launcher of
test_t110_supervisor.py and an injected clock, one minute at a time. The supervisor is
loaded afresh for every round, as the scheduled task starts a new process every minute, so
the count can only travel through the state file. No MetaTrader5, no real role.
"""
import importlib.util
import json
import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import test_t110_supervisor as t110
from tests.test_t110_supervisor import NOW, fresh

ROOT = Path(__file__).resolve().parents[1]
CAP = "SUPERVISOR_RESTART_CAP"
CONFIRM = "release-restart-cap"


def load():
    path = ROOT / "scripts" / "live_supervisor.py"
    spec = importlib.util.spec_from_file_location("t220_supervisor", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def key_of(entry):
    return str(entry["magic"]) if entry["kind"] == "runner" else entry["kind"]


def names(rows):
    return [row["component"] for row in rows]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rewrite(path, **changes):
    path.write_text(json.dumps({**read(path), **changes}), encoding="utf-8")


class StandIn(t110.Processes):
    """The test_t110 stand-in, with the time of the round: a process it launches is created
    then, as a launched role really is, so that what it leaves behind can be dated after it."""

    def __init__(self):
        super().__init__()
        self.clock = [NOW]

    def launch(self, argv, cwd, log, env):
        item = super().launch(argv, cwd, log, env)
        item["started_utc"] = self.clock[0].isoformat()
        return item


class Site:
    """The test_t110 deployment (runner 777, risk, reconcile) and a second runner, 778."""

    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setattr(t110, "Processes", StandIn)
        self.module, self.cfg, self.p, self.live, _ = t110.setup(tmp_path)
        self.exit_files = (None, None)
        self.add_runner(778, 70, "two.toml")

    def add_runner(self, magic, pid, name):
        """One more runner of the deployment: a configuration file (its name decides the
        order of the roles), a process created ten minutes ago, and its registration."""
        (Path(self.cfg.config_dir) / name).write_text(
            (Path(self.cfg.config_dir) / "one.toml").read_text().replace("magic=777", f"magic={magic}"))
        argv = self.module.command(self.cfg, self.module.components(self.cfg)[str(magic)])
        started = (NOW - timedelta(minutes=10)).isoformat()
        self.p.items[pid] = dict(pid=pid, parent_pid=0, started_utc=started, argv=argv, executable=argv[0])
        log = Path(self.cfg.log_dir) / f"{magic}.log"
        log.write_text("completed loop\n")
        os.utime(log, (NOW.timestamp(), NOW.timestamp()))
        self.write_registry(self.registered() + [dict(
            pid=pid, started_utc=started, kind="runner", magic=magic, mode="Demo",
            deployment_sha=self.cfg.sha, deployment_dir=self.cfg.directory,
            data_dir=self.cfg.data_dir, argv=argv, log=str(log))])
        fresh(self.live, str(magic), pid, self.cfg.sha, NOW)

    # ---- what is on disk ----

    def at(self, minute):
        return NOW + timedelta(minutes=minute)

    def registered(self):
        return json.loads((self.live / "instances.json").read_text(encoding="utf-8"))["instances"]

    def write_registry(self, entries):
        (self.live / "instances.json").write_text(
            json.dumps({"schema_version": 1, "instances": entries}), encoding="utf-8")

    def entry(self, key):
        return next(e for e in self.registered() if key_of(e) == key)

    def pid(self, key):
        return self.entry(key)["pid"]

    def state(self):
        return read(self.live / "supervisor-state.json")

    def write_state(self, state):
        (self.live / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")

    def record(self, key):
        return self.state()["restart_cap"]["roles"][key]

    def restarts(self, key):
        return self.record(key)["restarts"]

    def latch(self, key):
        """When the role was latched, or None."""
        return self.record(key).get("latched_since")

    def journal(self):
        path = self.live / "ops-restarts.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def alerts(self, kind=CAP):
        path = self.live / "alerts.log"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return [line for line in text.splitlines() if line.split("\t")[1:2] == [kind]]

    # ---- the state of the roles before a round ----

    def healthy(self, key, now, last_error=None):
        """A completed loop just now: heartbeat and log are fresh."""
        entry = self.entry(key)
        beat = dict(runner_alive=True, ts=now.isoformat(), pid=entry["pid"],
                    deployment_sha=self.cfg.sha, reconcile_clean=True)
        if last_error is not None:
            beat["last_error"] = last_error
        (self.live / f"heartbeat-{key}.json").write_text(json.dumps(beat))
        log = Path(entry["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"[{now.isoformat()}] loop_completed\n")
        os.utime(log, (now.timestamp(), now.timestamp()))

    def never_completed(self, key, now):
        """The registered instance has not completed one loop since it was started: its log
        is fresh (it keeps trying and failing), the heartbeat on disk is the previous
        instance's -- another pid, and old. What the runner of 11-25 looked like."""
        entry = self.entry(key)
        log = Path(entry["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"[{now.isoformat()}] 决策异常：数据滞后\n")
        os.utime(log, (now.timestamp(), now.timestamp()))
        (self.live / f"heartbeat-{key}.json").write_text(json.dumps(dict(
            runner_alive=True, ts=(now - timedelta(seconds=400)).isoformat(),
            pid=7_000_000 + entry["pid"], deployment_sha=self.cfg.sha, reconcile_clean=True)))

    def exited(self, key, at, rc=78):
        """The registered instance left by itself with `rc` and is gone. With 78 it has left
        the two records of a disconnect exit (T141, T209: fifteen minutes of waiting).
        exit_files is (the marker, the exit record); the marker is None without a 78."""
        entry = self.entry(key)
        component = f"runner-{key}" if entry["kind"] == "runner" else key
        pid = self.p.items.pop(entry["pid"])["pid"]
        marker, record = self.live / f"disconnect-exit-{component}.json", self.live / f"exit-{component}.json"
        if rc == 78:
            marker.write_text(json.dumps(dict(
                component=component, rc=rc, reason="disconnect_timeout",
                first_failure_utc=(at - timedelta(minutes=15)).isoformat(),
                duration_s=900.0, limit_s=900.0, last_error="NotConnectedError: down",
                ts=at.isoformat(), pid=pid)))
        record.write_text(json.dumps(dict(
            component=component, rc=rc, reason=f"exit_code_{rc}", ts=at.isoformat(), pid=pid)))
        self.exit_files = (marker if rc == 78 else None, record)

    def release(self, key, confirm=CONFIRM):
        """What an operator writes after checking the cause."""
        path = self.live / f"restart-cap-release-{key}.json"
        path.write_text(json.dumps({"confirm": confirm}), encoding="utf-8")
        return path

    # ---- one round ----

    def round(self, minute, *, stale=(), gone=(), waiting=None, undeterminable=False, patch=None):
        """One supervisor run at NOW + `minute` minutes. `stale`: roles that need a restart;
        `gone`: roles whose process is missing; every other role is healthy, `waiting` giving
        the last_error of those waiting for a terminal or a trade server. `undeterminable`:
        git cannot answer this round (T212). `patch(module)` sees the supervisor module of
        this round before it runs."""
        stale = [stale] if isinstance(stale, str) else list(stale)
        gone = [gone] if isinstance(gone, str) else list(gone)
        now = self.at(minute)
        for entry in self.registered():
            key = key_of(entry)
            if key in stale:
                self.never_completed(key, now)
            elif key in gone:
                self.p.items.pop(entry["pid"], None)
            else:
                self.healthy(key, now, (waiting or {}).get(key))
        self.p.clock[0] = now
        module = load()
        if patch is not None:
            patch(module)

        def cannot_answer(cfg):
            raise module.DeploymentUndeterminable("git status: TimeoutExpired")
        return module.supervise(self.cfg, self.p, now=now,
                                verify_deployment=cannot_answer if undeterminable else (lambda cfg: None))


def to_the_latch(site, key="777"):
    """Three restarts, one a minute, then the fourth need: withheld, and the role latched."""
    for minute in (0, 1, 2):
        assert site.round(minute, stale=key)["restarted"] == [key]
    assert site.round(3, stale=key)["restarted"] == [] and len(site.journal()) == 3


# ---- the latch itself ----

def test_the_fourth_restart_inside_the_window_is_withheld_before_anything_is_ended_and_latches_the_role(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    first = site.pid("777")
    withheld = []
    for minute in (0, 1, 2):
        result = site.round(minute, stale="777")
        assert result["restarted"] == ["777"] and not result["refused"]
        withheld.append(result.get("restart_latched"))
    third = site.pid("777")                                # the process the third restart started
    assert site.p.killed == [first, 900, 901] and len(site.p.started) == 3
    assert names(site.journal()) == ["777"] * 3

    result = site.round(3, stale="777")
    assert result["restarted"] == [] and not result["refused"]       # the fourth need: not restarted
    assert result["restart_latched"] == ["777"]
    assert site.pid("777") == third and third in site.p.items       # kept: nothing was ended
    assert site.p.killed == [first, 900, 901] and len(site.p.started) == 3   # and nothing started
    assert names(site.journal()) == ["777"] * 3                     # no fourth row
    assert withheld == [[], [], []]
    assert (site.live / "halt.json").exists()
    # Latched from this round on, and said so once: it stays until an operator lifts it.
    assert site.latch("777") == site.at(3).isoformat()
    assert len(site.alerts()) == 1
    for part in ("777", "latched", "3 restarts in the last 600 s", "until an operator lifts it",
                 "restart-cap-release-777.json", CONFIRM):
        assert part in site.alerts()[0], part

    for minute in (4, 5, 6, 7):                              # every minute after: still withheld
        result = site.round(minute, stale="777")
        assert result["restarted"] == [] and result["restart_latched"] == ["777"]
        assert site.pid("777") == third and third in site.p.items
    assert site.p.killed == [first, 900, 901] and len(site.p.started) == 3
    assert names(site.journal()) == ["777"] * 3
    assert len(site.alerts()) == 1                           # said once, not once a minute
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1, 2)]
    assert site.record("777")["suppressed_since"] == site.at(3).isoformat()
    assert site.latch("777") == site.at(3).isoformat()
    # The role is still watched and still named, every minute, as unhealthy and latched.
    unhealthy = site.alerts("SUPERVISOR")
    assert len(unhealthy) == 8 and "restart_latched=['777']" in unhealthy[-1]


def test_each_role_has_its_own_latch(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    everyone = ("777", "778", "risk", "reconcile")
    for minute in (0, 1, 2):
        site.round(minute, stale="777")                    # runner 777 has used its three
    # Its fourth need is withheld while 778 and risk, each on its first, are restarted.
    result = site.round(3, stale=("777", "778", "risk"))
    assert result["restarted"] == ["778", "risk"] and result["restart_latched"] == ["777"]
    assert site.latch("777") == site.at(3).isoformat()
    for minute in (4, 5):
        result = site.round(minute, stale=("777", "778", "risk"))
        assert result["restarted"] == ["778", "risk"] and result["restart_latched"] == ["777"]
    result = site.round(6, stale=("777", "778", "risk"))
    assert result["restarted"] == [] and result["restart_latched"] == ["777", "778", "risk"]
    assert names(site.journal()) == ["777"] * 3 + ["778", "risk"] * 3
    assert site.restarts("778") == [site.at(m).isoformat() for m in (3, 4, 5)]
    assert site.restarts("risk") == [site.at(m).isoformat() for m in (3, 4, 5)]
    assert site.restarts("reconcile") == [] and site.latch("reconcile") is None    # never needed anything
    # Reconcile is restarted three times while three roles are latched, then it is too.
    for minute in (6, 7, 8):
        result = site.round(minute, stale=everyone)
        assert result["restarted"] == ["reconcile"]
        assert result["restart_latched"] == ["777", "778", "risk"]
    result = site.round(9, stale=everyone)
    assert result["restarted"] == [] and result["restart_latched"] == list(everyone)
    assert names(site.journal()) == ["777"] * 3 + ["778", "risk"] * 3 + ["reconcile"] * 3
    assert [site.latch(key) for key in everyone] == [site.at(3).isoformat(), site.at(6).isoformat(),
                                                     site.at(6).isoformat(), site.at(9).isoformat()]
    assert len(site.alerts()) == 4                         # one for each role, each said once


def test_the_latch_does_not_lift_itself_when_the_window_has_passed(tmp_path, monkeypatch):
    """The window only says when a role is latched. Once it is, the registered restarts may
    leave the window one by one: nothing changes until an operator lifts the latch."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        assert site.round(minute, stale="777")["restarted"] == ["777"]
    third = site.pid("777")
    held = [site.round(minute, stale="777") for minute in (3, 10, 11, 12, 13, 30, 31, 120)]
    assert [result["restarted"] for result in held] == [[]] * 8              # never again
    assert site.pid("777") == third
    assert names(site.journal()) == ["777"] * 3             # never a fourth row
    assert [result["restart_latched"] for result in held] == [["777"]] * 8
    assert site.restarts("777") == []                       # the three have left the window ...
    assert site.latch("777") == site.at(3).isoformat()      # ... the latch has not
    assert len(site.alerts()) == 1 and site.p.killed == [10, 900, 901]


def test_a_latched_role_that_recovers_and_needs_a_restart_again_is_still_withheld(tmp_path, monkeypatch):
    """The process that was kept completes loops again: the system runs on. The stretch that
    was said is over, but the role's automatic restarts stay withheld."""
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    third = site.pid("777")
    said = len(site.alerts("SUPERVISOR"))
    for minute in range(4, 16):                 # twelve minutes of health: past the window
        assert site.round(minute)["restarted"] == []
    assert len(site.alerts("SUPERVISOR")) == said            # healthy: nothing withheld, nothing said
    assert site.record("777").get("suppressed_since") is None   # the alert stretch is over ...
    assert site.restarts("777") == [] and site.latch("777") == site.at(3).isoformat()   # the latch is not
    result = site.round(16, stale="777")        # stale again: withheld, and said once more
    assert result["restarted"] == [] and site.pid("777") == third
    assert result["restart_latched"] == ["777"]
    assert site.p.killed == [10, 900, 901]
    assert len(site.journal()) == 3 and len(site.alerts()) == 2
    for part in ("777", "latched since " + site.at(3).isoformat(), "until an operator lifts it"):
        assert part in site.alerts()[1], part


def test_three_restarts_latch_nobody_until_a_fourth_need_comes_inside_the_window(tmp_path, monkeypatch):
    """The window decides when a role is latched, nothing else: a role that was restarted
    three times and then ran past the window starts from nothing when it next needs one."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.round(minute, stale="777")
    for minute in range(3, 13):                 # healthy for ten minutes: past the window
        assert site.round(minute)["restarted"] == []
    assert site.latch("777") is None and site.restarts("777") == [site.at(2).isoformat()]   # 600 s old: the last inside
    for minute in (13, 14, 15):                 # a new storm: three restarts again
        assert site.round(minute, stale="777")["restarted"] == ["777"]
    assert site.latch("777") is None
    assert site.round(16, stale="777")["restarted"] == []
    assert site.round(17, stale="777")["restart_latched"] == ["777"]
    assert site.latch("777") == site.at(16).isoformat() and len(site.journal()) == 6


def test_the_latch_ignores_the_clock(tmp_path, monkeypatch):
    """Three restarts, the fourth need latches. The wall clock jumping forward three hours
    (every restart far outside the window) or back ten minutes changes nothing. (Further
    back would put the clock before the other instances were registered, which makes them
    look stale for a reason of its own.)"""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.round(minute, stale="777")
    held = [site.round(minute, stale="777") for minute in (3, 4, 184, 185, -5, -4)]
    assert [result["restarted"] for result in held] == [[]] * 6   # forward three hours, then back
    assert [result["restart_latched"] for result in held] == [["777"]] * 6
    assert names(site.journal()) == ["777"] * 3 and len(site.alerts()) == 1
    assert site.latch("777") == site.at(3).isoformat()


def test_the_clock_going_back_frees_no_budget(tmp_path, monkeypatch):
    """Two restarts, then the wall clock goes back ten minutes. The two now lie in the
    future and still count: one more restart goes through, the need after it latches. (A
    restart whose time cannot be read counts the same way, see further down.)"""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1):
        site.round(minute, stale="777")
    assert site.round(-5, stale="777")["restarted"] == ["777"]           # the third
    held = [site.round(minute, stale="777") for minute in (-4, -3)]
    assert [result["restarted"] for result in held] == [[], []]
    assert names(site.journal()) == ["777"] * 3
    assert [result["restart_latched"] for result in held] == [["777"]] * 2
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1, -5)]
    assert site.latch("777") == site.at(-4).isoformat()


def test_the_latch_is_the_role_s_whatever_process_is_registered(tmp_path, monkeypatch):
    """Somebody restarts the latched role by hand and registers it, as the launcher would.
    The new process is as stale as the old one was: it is neither ended nor restarted, the
    stretch already said is not said again, and the latch keeps its time."""
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    del site.p.items[site.pid("777")]
    entry = dict(site.entry("777"), pid=750, started_utc=site.at(4).isoformat(),
                 log=str(Path(site.cfg.log_dir) / "by-hand.log"))
    site.p.items[750] = dict(pid=750, parent_pid=0, started_utc=entry["started_utc"],
                             argv=entry["argv"], executable=entry["argv"][0])
    site.write_registry([entry if key_of(e) == "777" else e for e in site.registered()])
    result = site.round(4, stale="777")
    assert result["restarted"] == [] and result["restart_latched"] == ["777"]
    assert 750 in site.p.items and site.p.killed == [10, 900, 901] and len(site.alerts()) == 1
    assert site.latch("777") == site.at(3).isoformat()
    assert site.round(5)["restart_latched"] == []            # healthy: this stretch is over


def test_the_count_and_the_latch_live_in_the_state_file_not_in_the_supervisor(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.round(minute, stale="777")
    stored = site.state()["restart_cap"]
    assert stored["sha"] == site.cfg.sha
    assert stored["roles"]["777"]["restarts"] == [site.at(m).isoformat() for m in (0, 1, 2)]
    assert "latched_since" not in stored["roles"]["777"]      # three restarts latch nobody
    # Without the record on disk the next round knows nothing of the three restarts.
    state = site.state()
    del state["restart_cap"]
    site.write_state(state)
    assert site.round(3, stale="777")["restarted"] == ["777"]
    assert site.restarts("777") == [site.at(3).isoformat()] and len(site.journal()) == 4
    # A record written by hand is what the next round goes by: three inside the window ...
    state = site.state()
    state["restart_cap"]["roles"]["777"]["restarts"] = [site.at(m).isoformat() for m in (3, 4, 5)]
    site.write_state(state)
    assert site.round(6, stale="777")["restart_latched"] == ["777"] and len(site.journal()) == 4
    # ... and a latch written by hand, with nothing left inside the window.
    state = site.state()
    state["restart_cap"]["roles"]["777"] = {"restarts": [], "latched_since": site.at(6).isoformat()}
    site.write_state(state)
    assert site.round(60, stale="777")["restart_latched"] == ["777"] and len(site.journal()) == 4


def test_the_restart_is_registered_before_the_process_is_touched(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    on_disk = {}
    original = site.p.terminate

    def terminate(process):
        on_disk[process["pid"]] = site.state()              # what the state file says at the first end
        original(process)
    site.p.terminate = terminate
    site.round(0, stale=("777", "risk"))
    assert site.p.killed == [10, 11]                        # 777 first, risk second: the order of the roles
    assert on_disk[10]["restart_cap"]["roles"]["777"]["restarts"] == [site.at(0).isoformat()]
    assert on_disk[10]["restart_cap"]["roles"]["risk"]["restarts"] == []
    assert on_disk[11]["restart_cap"]["roles"]["risk"]["restarts"] == [site.at(0).isoformat()]


def test_a_restart_that_was_issued_and_then_failed_stays_counted(tmp_path, monkeypatch):
    """The count says what was issued, not what succeeded: ending the tree may fail after the
    registration was written, and the round ends as a refusal. That restart stays counted."""
    site = Site(tmp_path, monkeypatch)
    original = site.p.terminate

    def failed(process):
        raise RuntimeError("termination failed")
    site.p.terminate = failed
    result = site.round(0, stale="777")
    assert result["refused"] and result["restarted"] == []
    assert site.restarts("777") == [site.at(0).isoformat()]  # on disk, and kept through the refusal
    site.p.terminate = original
    assert site.round(1, stale="777")["restarted"] == ["777"]
    assert site.restarts("777") == [site.at(0).isoformat(), site.at(1).isoformat()]


# ---- what does not count ----

def test_a_refused_round_leaves_the_count_as_it_was(tmp_path, monkeypatch):
    """T211: the identity check refuses the whole round and no process is touched."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1):
        site.round(minute, stale="777")
    risk = site.pid("risk")
    site.p.items[880] = dict(pid=880, parent_pid=risk, started_utc=site.at(2).isoformat(),
                             argv=[sys.executable, "other.py"], executable=sys.executable)
    result = site.round(2, stale="777")
    assert result["refused"] and result["restarted"] == [] and result.get("restart_latched") == []
    assert site.p.killed == [10, 900] and len(site.p.started) == 2
    del site.p.items[880]
    assert site.round(3, stale="777")["restarted"] == ["777"]                 # the third
    assert site.round(4, stale="777")["restart_latched"] == ["777"]           # the fourth is withheld
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1, 3)]   # the refusal neither used nor lost one


def test_a_refusal_does_not_lift_the_latch(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    site.p.items[880] = dict(pid=880, parent_pid=site.pid("risk"), started_utc=site.at(4).isoformat(),
                             argv=[sys.executable, "other.py"], executable=sys.executable)
    assert site.round(4, stale="777")["refused"] is True
    assert site.latch("777") == site.at(3).isoformat()        # the refusal wrote its state; the latch came along
    del site.p.items[880]
    held = [site.round(minute, stale="777") for minute in (5, 30)]
    assert [result["restarted"] for result in held] == [[], []]
    assert [result["restart_latched"] for result in held] == [["777"]] * 2
    assert len(site.alerts()) == 1 and len(site.journal()) == 3


def test_a_deferred_round_leaves_the_count_as_it_was(tmp_path, monkeypatch):
    """T212: git cannot answer, so nothing is restarted that round."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1):
        site.round(minute, stale="777")
    for minute in (2, 3):
        result = site.round(minute, stale="777", undeterminable=True)
        assert result["deferred"] == ["777"] and result["restarted"] == [] and not result["refused"]
        assert site.p.killed == [10, 900] and len(site.p.started) == 2
        assert site.state()["undeterminable"] == minute - 1
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1)]  # the deferrals neither used nor lost one
    assert site.round(4, stale="777")["restarted"] == ["777"]                 # git answers: the third
    assert site.round(5, stale="777")["restart_latched"] == ["777"]
    # A round that cannot verify says nothing new about a latched role: the loop that would
    # say it is not reached, so only the per-minute alert carries the suffix.
    result = site.round(6, stale="777", undeterminable=True)
    assert result["deferred"] == ["777"] and result["restarted"] == [] and result["restart_latched"] == []
    assert "restart_latched=['777']" in site.alerts("SUPERVISOR")[-1]
    assert len(site.alerts()) == 1


def test_a_fourth_need_that_comes_in_a_deferred_round_latches_too(tmp_path, monkeypatch):
    """It is the need that latches, not the restart it would have caused -- T212 withholds
    that round's restarts anyway, and registers nothing. The alert waits for a round that
    can verify and in which the role is still stale."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.round(minute, stale="777")
    result = site.round(3, stale="777", undeterminable=True)
    assert result["deferred"] == ["777"] and result["restarted"] == []
    assert site.latch("777") == site.at(3).isoformat() and site.alerts() == []
    result = site.round(4, stale="777")
    assert result["restarted"] == [] and result["restart_latched"] == ["777"]
    assert len(site.alerts()) == 1 and len(site.journal()) == 3


@pytest.mark.parametrize("key, waiting", [
    ("777", "waiting_for_terminal"),               # T209: started, waiting for the terminal
    ("risk", "NotConnectedError: 终端与交易服务器断开"),   # T141: degraded rounds, heartbeats all along
])
def test_rounds_in_which_the_role_is_alive_and_waiting_do_not_count(tmp_path, monkeypatch, key, waiting):
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1):
        site.round(minute, stale=key)
    alive = site.pid(key)
    for minute in range(2, 12):                    # ten minutes: alive, writing heartbeat and log
        result = site.round(minute, waiting={key: waiting})
        assert result["restarted"] == [] and result["restart_latched"] == []
        assert site.pid(key) == alive and alive in site.p.items
    assert site.p.killed == [10 if key == "777" else 11, 900]   # the instance before, and the first restarted one
    assert site.restarts(key) == [site.at(1).isoformat()]   # of the two, one is 600 s old: the last inside
    assert site.alerts() == []
    assert site.round(12, stale=key)["restarted"] == [key]        # nothing registered: no latch
    assert site.restarts(key) == [site.at(12).isoformat()]


# ---- exit 78 ----

WAITING = [("777", "waiting_for_terminal"),                    # T209: a runner that starts and gets no terminal
           ("risk", "NotConnectedError: down")]                # T141: risk, degraded rounds all along


@pytest.mark.parametrize("key, waiting", WAITING)
def test_a_takeover_after_fifteen_minutes_and_exit_78_is_not_a_restart(tmp_path, monkeypatch, key, waiting):
    """T141/T209: the process started by the third restart connects to nothing for the full
    fifteen minutes, heartbeats all along, then leaves with the dedicated exit code (the
    runner does so from its startup wait, risk and reconcile also from a disconnect). The
    supervisor takes over, and that is not a fourth restart."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.round(minute, stale=key)
    started = site.pid(key)                          # created at minute 2 by the third restart
    for minute in range(3, 17):                      # alive and waiting: nothing to restart
        assert site.round(minute, waiting={key: waiting})["restarted"] == []
        assert site.pid(key) == started
    site.exited(key, site.at(17.5))
    result = site.round(18, gone=key)
    assert result["restarted"] == [key] and result["restart_latched"] == []
    assert names(site.journal()) == [key] * 4 and site.restarts(key) == []
    assert site.alerts() == []
    # Only that one. The process of the takeover is lost without a record of its own: from
    # there on every restart counts, and the old exit record exempts nothing.
    for minute in (19, 20, 21):
        assert site.round(minute, gone=key)["restarted"] == [key]
    assert site.round(22, gone=key)["restart_latched"] == [key]
    assert site.restarts(key) == [site.at(m).isoformat() for m in (19, 20, 21)]
    assert names(site.journal()) == [key] * 7 and len(site.alerts()) == 1


def test_a_takeover_after_exit_78_goes_ahead_while_the_role_is_latched(tmp_path, monkeypatch):
    """The latch withholds automatic restarts. The first takeover after an instance left by
    itself with exit code 78 is not one it withholds, and the latch itself stays."""
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    third = site.pid("777")
    for minute in range(4, 20):                 # the process that was kept runs again, waiting for a terminal
        assert site.round(minute, waiting={"777": "waiting_for_terminal"})["restarted"] == []
        assert site.pid("777") == third
    site.exited("777", site.at(20.5))
    result = site.round(21, gone="777")
    assert result["restarted"] == ["777"] and result["restart_latched"] == []
    assert names(site.journal()) == ["777"] * 4 and site.restarts("777") == []
    assert site.latch("777") == site.at(3).isoformat()
    # The process of the takeover is lost: withheld again, and said once more.
    result = site.round(22, gone="777")
    assert result["restarted"] == [] and result["restart_latched"] == ["777"]
    assert names(site.journal()) == ["777"] * 4 and len(site.alerts()) == 2


@pytest.mark.parametrize("key, _", WAITING)
def test_the_takeover_after_exit_78_is_neither_withheld_nor_counted_while_latched(tmp_path, monkeypatch, key, _):
    """The rule on its own. The record is written by hand: a role latched, nothing left
    inside the window, and a registered instance that left with 78. With the latch the two
    do go together: the process that was kept can run for fifteen minutes and then leave
    with 78."""
    site = Site(tmp_path, monkeypatch)
    site.exited(key, site.at(-0.5))
    state = site.state() if (site.live / "supervisor-state.json").exists() else {}
    state["restart_cap"] = {"sha": site.cfg.sha, "roles": {key: {
        "restarts": [site.at(m).isoformat() for m in (-20, -19, -18)], "latched_since": site.at(-17).isoformat(),
        "suppressed_since": site.at(-17).isoformat()}}}
    site.write_state(state)
    result = site.round(0, gone=key)
    assert result["restarted"] == [key] and result["restart_latched"] == []
    assert site.restarts(key) == []                 # the three have left the window, and none was added
    record = site.record(key)
    assert record.get("suppressed_since") is None                     # the stretch that was said is over ...
    assert record["latched_since"] == site.at(-17).isoformat()        # ... the latch is not
    assert site.alerts() == []


def _other_worker(site, marker, record):
    rewrite(record, pid=read(record)["pid"] + 1)


def _marker_rc(site, marker, record):
    rewrite(marker, rc=79)


def _record_rc(site, marker, record):
    rewrite(record, rc=79)


def _marker_component(site, marker, record):
    rewrite(marker, component="reconcile")


def _record_component(site, marker, record):
    rewrite(record, component="reconcile")


def _marker_reason(site, marker, record):
    rewrite(marker, reason="account_unavailable_window")


def _worker_not_a_pid(site, marker, record):
    rewrite(marker, pid="902")
    rewrite(record, pid="902")


def _lasted_not_a_number(site, marker, record):
    rewrite(marker, duration_s=float("nan"))         # JSON writes NaN, and reads it back


def _record_gone(site, marker, record):
    record.unlink()


def _record_unreadable(site, marker, record):
    record.write_text("{", encoding="utf-8")


def _marker_gone(site, marker, record):
    marker.unlink()


def _marker_unreadable(site, marker, record):
    marker.write_text("not json", encoding="utf-8")


def _exit_before_the_registration(site, marker, record):
    old = (NOW - timedelta(days=1)).isoformat()
    rewrite(marker, ts=old)
    rewrite(record, ts=old)


EXIT_CONDITIONS = [
    ("the two records name one worker", _other_worker),
    ("the marker says rc 78", _marker_rc),
    ("the exit record says rc 78", _record_rc),
    ("the marker names the component", _marker_component),
    ("the exit record names the component", _record_component),
    ("the marker says it was a disconnect", _marker_reason),
    ("the worker is a pid", _worker_not_a_pid),
    ("the disconnect lasted the limit", _lasted_not_a_number),
    ("the exit record is there", _record_gone),
    ("the exit record can be read", _record_unreadable),
    ("the marker is there", _marker_gone),
    ("the marker can be read", _marker_unreadable),
    ("the exit is dated after the registration", _exit_before_the_registration),
]


@pytest.mark.parametrize("condition, damage", EXIT_CONDITIONS, ids=[condition for condition, _ in EXIT_CONDITIONS])
def test_an_exit_that_is_not_a_clean_78_of_the_registered_instance_is_an_ordinary_restart(tmp_path, monkeypatch, condition, damage):
    """The exemption is the only way a restart goes past the latch without an operator, so
    each thing it requires is pinned here: without it the exit is an ordinary one. The
    restart is registered, and the fourth such need is withheld."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.exited("risk", site.at(minute - 0.5))
        damage(site, *site.exit_files)
        assert site.round(minute, gone="risk")["restarted"] == ["risk"]
    assert site.restarts("risk") == [site.at(m).isoformat() for m in (0, 1, 2)]
    assert site.round(3, gone="risk")["restart_latched"] == ["risk"]
    assert names(site.journal()) == ["risk"] * 3


def test_an_exit_78_after_less_than_fifteen_minutes_counts(tmp_path, monkeypatch):
    """Not the normal path: the marker says the disconnect lasted less than the limit."""
    site = Site(tmp_path, monkeypatch)
    site.exited("risk", site.at(-0.5))
    rewrite(site.exit_files[0], duration_s=12.3)
    assert site.round(0, gone="risk")["restarted"] == ["risk"]
    assert site.restarts("risk") == [site.at(0).isoformat()]


# ---- the halts keep their own rules: the latch withholds restarts and nothing else ----

BLOCKERS = ["halt-risk-veto.json", "killSwitch", "manual halt"]


@pytest.mark.parametrize("blocker", BLOCKERS)
def test_releasing_the_latch_touches_no_halt_and_the_halt_keeps_its_release_conditions(tmp_path, monkeypatch, blocker):
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    # While somebody looks into it the risk process puts its veto there, the kill switch is
    # set, or the supervisor's halt is replaced by a manual one.
    if blocker == "halt-risk-veto.json":
        (site.live / blocker).write_text(json.dumps(dict(
            reason="heartbeat_timeout", owner="risk", ts=site.at(3).isoformat())))
    elif blocker == "killSwitch":
        (site.live / blocker).touch()
    else:
        (site.live / "halt.json").write_text(json.dumps(dict(
            reason="operator", owner="manual", token="theirs", ts=site.at(3).isoformat())))
    files = ["halt.json"] if blocker == "manual halt" else ["halt.json", blocker]
    kept = {name: (site.live / name).read_bytes() for name in files}
    site.release("777")
    result = site.round(4, stale="777")
    assert result["restart_cap_released"] == ["777"] and result["restarted"] == ["777"]
    assert not (site.live / "restart-cap-release-777.json").exists()
    # The release lifted the latch and nothing else.
    assert {name: (site.live / name).read_bytes() for name in kept} == kept
    # The role is healthy from now on, well over five minutes: the halt stays while the
    # blocker is there, exactly as without the latch.
    for minute in range(5, 14):
        result = site.round(minute)
        assert (site.live / "halt.json").read_bytes() == kept["halt.json"]
    assert result["cleared"] is False
    if blocker == "manual halt":
        assert "clear_blocked" not in result            # a manual halt is not the supervisor's to judge
        (site.live / "halt.json").unlink()              # removed by hand, as a manual halt is
        assert site.round(14)["cleared"] is False and not (site.live / "halt.json").exists()
    else:
        assert result["clear_blocked"] == blocker
        (site.live / blocker).unlink()                  # the blocker's own procedure, done by hand
        assert site.round(14)["cleared"] is True and not (site.live / "halt.json").exists()


def test_a_latched_role_that_recovers_by_itself_ends_the_alert_stretch_and_the_halt_goes_as_before(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    third = site.pid("777")
    for minute in range(4, 9):                 # like 902610 at 23:15: the third process completes loops again
        result = site.round(minute)
        assert result["restarted"] == [] and result["restart_latched"] == [] and result["cleared"] is False
        assert site.pid("777") == third and site.p.killed == [10, 900, 901]
    assert (site.live / "halt.json").exists()
    # The need that was withheld is over; the three restarts and the latch are not forgotten.
    assert site.record("777").get("suppressed_since") is None
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1, 2)]
    assert site.latch("777") == site.at(3).isoformat()
    # Five minutes of health later the halt goes as it always did.
    assert site.round(9)["cleared"] is True and not (site.live / "halt.json").exists()
    assert len(site.alerts()) == 1 and len(site.journal()) == 3
    # Stale again: withheld at once, and said once more.
    assert site.round(10, stale="777")["restart_latched"] == ["777"]
    assert site.p.killed == [10, 900, 901] and len(site.alerts()) == 2


def test_the_maintenance_stop_still_ends_a_role_that_is_latched(tmp_path, monkeypatch):
    """An explicit stop under the kill switch ends every verified tree, the latched one included."""
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    (site.live / "killSwitch").touch()
    stopped = site.module.stop_deployment(site.cfg, site.p, verify_deployment=lambda cfg: None)
    assert stopped["refused"] is False and sorted(stopped["stopped"]) == ["777", "778", "reconcile", "risk"]
    assert not site.p.items


# ---- the operator's release ----

def test_an_operator_lifts_the_latch_with_a_file_and_that_leaves_a_trace(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    third = site.pid("777")
    site.release("777")
    result = site.round(4, stale="777")
    assert result["restart_cap_released"] == ["777"]
    assert result["restarted"] == ["777"] and result["restart_latched"] == []      # in this very round
    assert site.p.killed == [10, 900, 901, third] and len(site.p.started) == 4
    assert not (site.live / "restart-cap-release-777.json").exists()             # consumed: it cannot lift twice
    assert site.latch("777") is None                                              # the latch went with it ...
    assert site.restarts("777") == [site.at(4).isoformat()]                       # ... and the count starts over
    assert site.record("777")["released"] == {
        "utc": site.at(4).isoformat(), "restarts": 3, "latched_since": site.at(3).isoformat()}
    assert len(site.alerts()) == 2 and names(site.journal()) == ["777"] * 4
    for part in ("released", "777", "restart-cap-release-777.json", "3 restarts", "latch set at " + site.at(3).isoformat()):
        assert part in site.alerts()[1], part
    for minute in (5, 6):                             # two more: with the one after the release that is three
        site.round(minute, stale="777")
    assert site.round(7, stale="777")["restart_latched"] == ["777"] and len(site.alerts()) == 3
    assert site.latch("777") == site.at(7).isoformat() and len(site.journal()) == 6


def test_an_operator_lifts_the_latch_long_after_the_window_has_passed(tmp_path, monkeypatch):
    """The only way back. Nothing is left inside the window, the role is latched all the
    same, and it is restarted again only once the release file is there."""
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    third = site.pid("777")
    held = [site.round(minute, stale="777") for minute in (11, 12, 30)]
    assert [result["restarted"] for result in held] == [[]] * 3
    assert site.restarts("777") == [] and site.latch("777") == site.at(3).isoformat()
    site.release("777")
    result = site.round(31, stale="777")
    assert result["restart_cap_released"] == ["777"] and result["restarted"] == ["777"]
    assert site.latch("777") is None and site.restarts("777") == [site.at(31).isoformat()]
    assert site.record("777")["released"] == {
        "utc": site.at(31).isoformat(), "restarts": 0, "latched_since": site.at(3).isoformat()}
    assert site.p.killed == [10, 900, 901, third] and len(site.journal()) == 4


def test_a_release_for_a_role_that_needs_nothing_restarts_nothing(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1):
        site.round(minute, stale="777")
    site.release("777")
    result = site.round(2)                             # every role healthy
    assert result["restart_cap_released"] == ["777"] and result["restarted"] == []
    assert site.p.killed == [10, 900] and site.restarts("777") == []
    assert site.record("777")["released"] == {"utc": site.at(2).isoformat(), "restarts": 2, "latched_since": None}
    assert len(site.alerts()) == 1 and "released" in site.alerts()[0]


def test_a_release_is_for_one_role(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    for minute in (4, 5, 6):
        site.round(minute, stale=("777", "778"))
    assert site.round(7, stale=("777", "778"))["restart_latched"] == ["777", "778"]
    site.release("777")
    result = site.round(8, stale=("777", "778"))
    assert result["restart_cap_released"] == ["777"]
    assert result["restarted"] == ["777"] and result["restart_latched"] == ["778"]
    assert site.latch("777") is None and site.latch("778") == site.at(7).isoformat()
    assert site.restarts("778") == [site.at(m).isoformat() for m in (4, 5, 6)]


@pytest.mark.parametrize("name, content", [
    ("restart-cap-release-777.json", json.dumps({"confirm": "release"})),     # another phrase
    ("restart-cap-release-999.json", json.dumps({"confirm": CONFIRM})),       # no such role
    ("restart-cap-release-777.json", "release"),                             # not JSON
    ("restart-cap-release-777.json", json.dumps([CONFIRM])),                 # not an object
])
def test_a_release_request_that_is_not_valid_is_left_in_place_and_named_once(tmp_path, monkeypatch, name, content):
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    (site.live / name).write_text(content, encoding="utf-8")
    for minute in (4, 5):
        result = site.round(minute, stale="777")
        assert result["restart_latched"] == ["777"] and result["restart_cap_released"] == []
        assert result["restarted"] == [] and site.p.killed == [10, 900, 901]
    assert (site.live / name).exists()                 # not the supervisor's to remove
    ignored = [line for line in site.alerts() if "ignored" in line]
    assert len(ignored) == 1 and len(site.alerts()) == 2
    for part in (name, CONFIRM):
        assert part in ignored[0], part
    # Corrected, it is honoured.
    (site.live / name).unlink()
    site.release("777")
    assert site.round(6, stale="777")["restart_cap_released"] == ["777"]


def test_each_invalid_release_file_is_named_once_however_many_there_are(tmp_path, monkeypatch):
    """Two files that are not valid requests: one alert for each, in all, not two more
    every round."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.round(minute, stale="777")
    site.round(3, stale="777")                         # withheld: nothing is restarted either way
    files = {"restart-cap-release-778.json": json.dumps({"confirm": "release"}),   # another phrase
             "restart-cap-release-999.json": "release"}                          # not JSON
    for name, content in files.items():
        (site.live / name).write_text(content, encoding="utf-8")

    def ignored():
        return [line for line in site.alerts() if "ignored" in line]
    for minute in (4, 5, 6):
        site.round(minute, stale="777")
    assert len(ignored()) == 2 and len(site.alerts()) == 3
    for name in files:
        assert sum(name in line for line in ignored()) == 1
    # A file whose content changes is named again, once.
    (site.live / "restart-cap-release-778.json").write_text(json.dumps({"confirm": "lift"}), encoding="utf-8")
    for minute in (7, 8):
        site.round(minute, stale="777")
    assert len(ignored()) == 3
    # A file that goes and comes back is named again as well; one that just goes is forgotten.
    (site.live / "restart-cap-release-999.json").unlink()
    site.round(9, stale="777")
    (site.live / "restart-cap-release-999.json").write_text("release", encoding="utf-8")
    for minute in (10, 11):
        site.round(minute, stale="777")
    assert len(ignored()) == 4
    assert site.p.killed == [10, 900, 901] and len(site.journal()) == 3


def test_a_release_in_a_round_that_cannot_restart_takes_effect_in_the_next_that_can(tmp_path, monkeypatch):
    """The release is consumed at once, also by a round that then cannot restart anything.
    The restart waits for a round that can, and the alert says so."""
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    site.release("777")
    result = site.round(4, stale="777", undeterminable=True)      # git cannot answer: nothing is restarted
    assert result["restart_cap_released"] == ["777"] and result["deferred"] == ["777"] and result["restarted"] == []
    assert site.p.killed == [10, 900, 901]
    assert site.latch("777") is None and not (site.live / "restart-cap-release-777.json").exists()
    released = site.alerts()[1]
    assert "in this round" not in released and "next round" in released
    assert site.round(5, stale="777")["restarted"] == ["777"]      # a round that can restart
    assert site.restarts("777") == [site.at(5).isoformat()] and site.p.killed == [10, 900, 901, 902]


def test_a_release_is_not_consumed_by_a_round_that_is_refused_before_it_looks(tmp_path, monkeypatch):
    """T211 refuses the whole round before anything else happens in it: the file stays,
    nothing is said about it, and the next round that is not refused consumes it and
    restarts the role in that round."""
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    site.release("777")
    site.p.items[880] = dict(pid=880, parent_pid=site.pid("risk"), started_utc=site.at(4).isoformat(),
                             argv=[sys.executable, "other.py"], executable=sys.executable)
    result = site.round(4, stale="777")
    assert result["refused"] and result["restart_cap_released"] == [] and result["restarted"] == []
    assert site.p.killed == [10, 900, 901]
    assert (site.live / "restart-cap-release-777.json").exists() and len(site.alerts()) == 1
    assert site.latch("777") == site.at(3).isoformat()
    del site.p.items[880]
    result = site.round(5, stale="777")
    assert result["restart_cap_released"] == ["777"] and result["restarted"] == ["777"]
    assert site.latch("777") is None and site.restarts("777") == [site.at(5).isoformat()]


# ---- the record itself ----

@pytest.mark.parametrize("damage, held", [
    ("roles", ["777", "778", "risk", "reconcile"]),    # the roles of the record are not what they should be
    ("record", ["777"]),                               # one role's entry
    ("entry", ["777"]),                                # one registered time
    ("latch", ["777"]),                                # when the role was latched
])
def test_a_record_that_cannot_be_read_holds_the_role_as_if_latched_until_released(tmp_path, monkeypatch, damage, held):
    """A record that is not of the expected shape says nothing about how many restarts were
    issued; it is not taken as "none"."""
    site = Site(tmp_path, monkeypatch)
    site.round(0, stale=held)
    state = site.state()
    roles = state["restart_cap"]["roles"]
    if damage == "roles":
        state["restart_cap"]["roles"] = "none"
    elif damage == "record":
        roles["777"] = "none"
    elif damage == "entry":
        roles["777"]["restarts"] = [20261101]
    else:
        roles["777"]["latched_since"] = 20261101
    site.write_state(state)
    for minute in (1, 2, 40):
        result = site.round(minute, stale=held)
        assert result["restarted"] == [] and result["restart_latched"] == held
    assert len(site.alerts()) == len(held) and "cannot be read" in site.alerts()[0]   # one for each held role
    for key in held:
        site.release(key)
    result = site.round(41, stale=held)
    assert sorted(result["restart_cap_released"]) == sorted(held) and result["restarted"] == held


def test_a_registered_time_that_cannot_be_read_never_leaves_the_window(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    site.round(0)                                     # a healthy round, so that the record exists
    state = site.state()
    state["restart_cap"]["roles"]["777"]["restarts"] = ["not a time"] * 3
    site.write_state(state)
    assert site.round(1, stale="777")["restart_latched"] == ["777"]   # three of unknown time: inside
    assert site.round(40, stale="777")["restart_latched"] == ["777"]  # and, being latched, for good
    assert site.latch("777") == site.at(1).isoformat()


def test_the_record_of_another_deployment_counts_for_nothing(tmp_path, monkeypatch):
    """Same data directory, another SHA: the deployment was replaced, and with it the past."""
    site = Site(tmp_path, monkeypatch)
    to_the_latch(site)
    state = site.state()
    state["restart_cap"]["sha"] = "b" * 40
    site.write_state(state)
    assert site.round(4, stale="777")["restarted"] == ["777"]
    assert site.state()["restart_cap"]["sha"] == site.cfg.sha
    assert site.restarts("777") == [site.at(4).isoformat()] and site.latch("777") is None


def test_a_role_the_record_has_no_entry_for_starts_with_nothing_registered(tmp_path, monkeypatch):
    """A runner added to the configuration later has no past in the record, and is not
    mistaken for one whose record cannot be read."""
    site = Site(tmp_path, monkeypatch)
    site.round(0, stale="777")                        # the record exists now, without the new role
    site.add_runner(779, 71, "zz-779.toml")
    result = site.round(1, stale="779")
    assert result["restarted"] == ["779"] and result["restart_latched"] == []
    assert site.restarts("779") == [site.at(1).isoformat()] and site.alerts() == []


# ---- a round that was refused: who writes the state file ----

def test_a_round_refused_for_lack_of_the_lock_leaves_the_state_file_to_the_round_that_holds_it(tmp_path, monkeypatch):
    """A round that never got supervisor.lock does not write supervisor-state.json: what it
    would write there is a record read without the lock, and that could be older than what
    the round holding the lock is registering. Its refusal is where it always was, in the
    halt and in alerts.log."""
    site = Site(tmp_path, monkeypatch)
    site.round(0, stale="777")
    state_file = site.live / "supervisor-state.json"
    before = state_file.read_bytes()
    with site.module.file_lock(str(site.live / "supervisor.lock"), attempts=1):   # another round is running
        result = site.round(1, stale="777")
    assert result["refused"] and result["restarted"] == [] and site.p.killed == [10]
    assert len(site.alerts("SUPERVISOR_REFUSED")) == 1 and (site.live / "halt.json").exists()
    assert state_file.read_bytes() == before
    assert site.round(2, stale="777")["restarted"] == ["777"]       # the next round that gets the lock
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 2)]


def test_a_round_refused_for_lack_of_the_lock_cannot_lose_the_restart_the_other_round_registers(tmp_path, monkeypatch):
    """The interleaving of the review. M gets no lock and is refused; N, which holds the
    lock, registers a restart. Were M still to write the state file, it would write the
    record as it read it -- before N's registration -- and N's restart would be forgotten:
    the next round would then issue one restart more before the role is latched."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1):
        site.round(minute, stale="777")
    state_file = site.live / "supervisor-state.json"
    registered = []

    def n_registers():                              # what N puts on disk, under the lock it holds
        state = read(state_file)
        state["restart_cap"]["roles"]["777"]["restarts"].append(site.at(2).isoformat())
        state_file.write_text(json.dumps(state), encoding="utf-8")
        registered.append(1)

    def patch(module):                              # N's write lands after M's read and before M's write, if M writes
        original = module.write_json_atomic

        def write(path, record):
            if "last_refusal" in record and not registered:
                n_registers()
            original(path, record)
        module.write_json_atomic = write

    with site.module.file_lock(str(site.live / "supervisor.lock"), attempts=1):
        result = site.round(2, stale="777", patch=patch)          # M
        assert result["refused"] and result["restarted"] == []
        if not registered:
            n_registers()                                          # M did not write: N's registration lands as N wrote it
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1, 2)]
    result = site.round(3, stale="777")
    assert result["restarted"] == [] and result["restart_latched"] == ["777"]     # no restart more
    assert names(site.journal()) == ["777", "777"] and len(site.alerts()) == 1    # (N's restart was written by hand)


def test_a_round_refused_while_the_launcher_holds_the_registry_lock_still_records_its_refusal(tmp_path, monkeypatch):
    """The usual way a round gets no lock: the launcher holds instances.lock. supervisor.lock
    is free, so the refusal is recorded as before -- now under that lock -- with the record."""
    site = Site(tmp_path, monkeypatch)
    site.round(0, stale="777")
    with site.module.registry_lock(str(site.live / "instances.lock")):
        result = site.round(1, stale="777")
    assert result["refused"] and result["restarted"] == []
    state = site.state()
    assert state["healthy_since"] is None and "TimeoutError" in state["last_refusal"]["reason"]
    assert site.restarts("777") == [site.at(0).isoformat()]


# ---- the formal checker ----

def test_the_formal_checker_reads_the_alerts_of_a_latched_stretch_as_ordinary_alert_rows(tmp_path, monkeypatch):
    """scripts/demo_check.py finds no magic in the `restart_...=` suffix of the per-minute
    alert or in the new alerts, accepts every row and its kind, and keeps the new kind an
    anomaly that nothing reclassifies as recorded."""
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.round(minute, stale="777")
    site.round(3, stale="777")                               # withheld
    site.round(4, stale="777")
    site.release("777")
    site.round(5, stale="777")                               # released, and restarted
    (site.live / "restart-cap-release-999.json").write_text("release", encoding="utf-8")
    site.round(6)                                            # the invalid request is named
    spec = importlib.util.spec_from_file_location("t220_checker", ROOT / "scripts" / "demo_check.py")
    check = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = check
    spec.loader.exec_module(check)
    rows = [line.split("\t", 2) for line in (site.live / "alerts.log").read_text(encoding="utf-8").splitlines()]
    kinds = [kind for _, kind, _ in rows]
    assert kinds.count(CAP) == 3 and kinds.count("SUPERVISOR") >= 5
    # What load_formal_raw requires of every row, then of its kind and of the magics in it.
    for _, kind, message in rows:
        assert re.fullmatch(r"[A-Z][A-Z_0-9]*", kind) and message
        assert kind in check.FORMAL_CLASSIFIED_ALERTS
        assert check._alert_magics(message, set()) == set()
    assert any(re.search(r" restart_\w+=", message) for _, kind, message in rows if kind == "SUPERVISOR")
    for stamp, kind, message in rows:
        if kind == CAP:
            alert = SimpleNamespace(kind=kind, message=message, time=check._utc(stamp, "alerts"))
            assert check._alert_is_recorded_class(alert) is False    # stays what its kind says: an anomaly
    assert CAP in check.FORMAL_ANOMALY_ALERTS and "SUPERVISOR" in check.FORMAL_DRILL_ALERTS
