"""T220-C: the supervisor restarts one role at most RESTART_CAP times in a rolling window.

What happened on 11-25 (docs/ops/phase-f-log.md, 2026-11-27, sections 1 and 3.2): a runner
that could not complete one loop wrote no heartbeat, so the supervisor, which runs once a
minute, judged it stale and restarted it every minute, twelve times in a row. From now on
the fourth restart of one role inside RESTART_WINDOW_S is withheld before anything is
ended: the process of the third restart stays, the halt stays, one SUPERVISOR_RESTART_CAP
alert is written, and the supervisor keeps watching the other roles. The count is kept in
supervisor-state.json under the supervisor lock and written before the process is touched.
An operator who has checked the cause lifts it with restart-cap-release-<role>.json.

Every round here is one call of supervise() with the process table and the launcher of
test_t110_supervisor.py and an injected clock, one minute at a time. The supervisor is
loaded afresh for every round, as the scheduled task starts a new process every minute, so
the count can only travel through the state file. No MetaTrader5, no real role.
"""
import importlib.util
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

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
        return json.loads((self.live / "supervisor-state.json").read_text(encoding="utf-8"))

    def write_state(self, state):
        (self.live / "supervisor-state.json").write_text(json.dumps(state), encoding="utf-8")

    def restarts(self, key):
        return self.state()["restart_cap"]["roles"][key]["restarts"]

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
        the two records of a disconnect exit (T141, T209: fifteen minutes of waiting)."""
        entry = self.entry(key)
        component = f"runner-{key}" if entry["kind"] == "runner" else key
        pid = self.p.items.pop(entry["pid"])["pid"]
        if rc == 78:
            (self.live / f"disconnect-exit-{component}.json").write_text(json.dumps(dict(
                component=component, rc=rc, reason="disconnect_timeout",
                first_failure_utc=(at - timedelta(minutes=15)).isoformat(),
                duration_s=900.0, limit_s=900.0, last_error="NotConnectedError: down",
                ts=at.isoformat(), pid=pid)))
        (self.live / f"exit-{component}.json").write_text(json.dumps(dict(
            component=component, rc=rc, reason=f"exit_code_{rc}", ts=at.isoformat(), pid=pid)))

    def release(self, key, confirm=CONFIRM):
        """What an operator writes after checking the cause."""
        path = self.live / f"restart-cap-release-{key}.json"
        path.write_text(json.dumps({"confirm": confirm}), encoding="utf-8")
        return path

    # ---- one round ----

    def round(self, minute, *, stale=(), gone=(), waiting=None, undeterminable=False):
        """One supervisor run at NOW + `minute` minutes. `stale`: roles that need a restart;
        `gone`: roles whose process is missing; every other role is healthy, `waiting` giving
        the last_error of those waiting for a terminal or a trade server. `undeterminable`:
        git cannot answer this round (T212)."""
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

        def cannot_answer(cfg):
            raise module.DeploymentUndeterminable("git status: TimeoutExpired")
        return module.supervise(self.cfg, self.p, now=now,
                                verify_deployment=cannot_answer if undeterminable else (lambda cfg: None))


def to_the_cap(site, key="777"):
    """Three restarts, one a minute, then the fourth need that is withheld."""
    for minute in (0, 1, 2):
        assert site.round(minute, stale=key)["restarted"] == [key]
    result = site.round(3, stale=key)
    assert result["restarted"] == [] and len(site.journal()) == 3
    assert result["restart_capped"] == [key] and len(site.alerts()) == 1


# ---- the cap itself ----

def test_the_fourth_restart_in_ten_minutes_is_withheld_before_anything_is_ended(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    first = site.pid("777")
    withheld = []
    for minute in (0, 1, 2):
        result = site.round(minute, stale="777")
        assert result["restarted"] == ["777"] and not result["refused"]
        withheld.append(result.get("restart_capped"))
    third = site.pid("777")                                # the process the third restart started
    assert site.p.killed == [first, 900, 901] and len(site.p.started) == 3
    assert names(site.journal()) == ["777"] * 3

    result = site.round(3, stale="777")
    assert result["restarted"] == [] and not result["refused"]       # the fourth need: not restarted
    assert result["restart_capped"] == ["777"]
    assert site.pid("777") == third and third in site.p.items       # kept: nothing was ended
    assert site.p.killed == [first, 900, 901] and len(site.p.started) == 3   # and nothing started
    assert names(site.journal()) == ["777"] * 3                     # no fourth row
    assert withheld == [[], [], []]
    assert site.alerts("SUPERVISOR")[0].count("restart_capped") == 0   # the three restarts: nothing was withheld
    assert (site.live / "halt.json").exists()
    assert len(site.alerts()) == 1
    for part in ("777", "3 in the last 600 s", "restart-cap-release-777.json", CONFIRM):
        assert part in site.alerts()[0], part

    for minute in (4, 5, 6, 7):                              # every minute after: still remembered
        result = site.round(minute, stale="777")
        assert result["restarted"] == [] and result["restart_capped"] == ["777"]
        assert site.pid("777") == third and third in site.p.items
    assert site.p.killed == [first, 900, 901] and len(site.p.started) == 3
    assert names(site.journal()) == ["777"] * 3
    assert len(site.alerts()) == 1                           # said once, not once a minute
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1, 2)]
    assert site.state()["restart_cap"]["roles"]["777"]["suppressed_since"] == site.at(3).isoformat()
    # The role is still watched and still named, every minute, as unhealthy and capped.
    unhealthy = site.alerts("SUPERVISOR")
    assert len(unhealthy) == 8 and "restart_capped=['777']" in unhealthy[-1]


def test_each_role_has_its_own_budget(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    everyone = ("777", "778", "risk", "reconcile")
    for minute in (0, 1, 2):
        site.round(minute, stale="777")                    # runner 777 has used its three
    # Its fourth need is withheld while 778 and risk, each on its first, are restarted.
    result = site.round(3, stale=("777", "778", "risk"))
    assert result["restarted"] == ["778", "risk"] and result["restart_capped"] == ["777"]
    for minute in (4, 5):
        result = site.round(minute, stale=("777", "778", "risk"))
        assert result["restarted"] == ["778", "risk"] and result["restart_capped"] == ["777"]
    result = site.round(6, stale=("777", "778", "risk"))
    assert result["restarted"] == [] and result["restart_capped"] == ["777", "778", "risk"]
    assert names(site.journal()) == ["777"] * 3 + ["778", "risk"] * 3
    assert site.restarts("778") == [site.at(m).isoformat() for m in (3, 4, 5)]
    assert site.restarts("risk") == [site.at(m).isoformat() for m in (3, 4, 5)]
    assert site.restarts("reconcile") == []                # never needed anything
    # Reconcile keeps its full budget while three roles are held.
    for minute in (6, 7, 8):
        result = site.round(minute, stale=everyone)
        assert result["restarted"] == ["reconcile"]
        assert result["restart_capped"] == ["777", "778", "risk"]
    result = site.round(9, stale=everyone)
    assert result["restarted"] == [] and result["restart_capped"] == list(everyone)
    assert names(site.journal()) == ["777"] * 3 + ["778", "risk"] * 3 + ["reconcile"] * 3
    assert len(site.alerts()) == 4                         # one for each role, each said once


def test_a_restart_stops_counting_ten_minutes_after_it_was_registered(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        assert site.round(minute, stale="777")["restarted"] == ["777"]
    held = [site.round(minute, stale="777") for minute in range(3, 11)]
    assert [result["restarted"] for result in held] == [[]] * 8
    assert [result["restart_capped"] for result in held] == [["777"]] * 8
    # At minute 10 the first restart is exactly 600 s old: still inside the window. At
    # minute 11 it is 660 s old and gone; two are left, so the next restart may proceed.
    assert site.round(11, stale="777")["restarted"] == ["777"]
    assert site.restarts("777") == [site.at(m).isoformat() for m in (1, 2, 11)]
    # Each further one pushes the oldest out of the window: two more go through ...
    for minute in (12, 13):
        assert site.round(minute, stale="777")["restarted"] == ["777"]
    assert site.restarts("777") == [site.at(m).isoformat() for m in (11, 12, 13)]
    # ... then the window is full again: withheld, and said once more.
    for minute in range(14, 22):
        assert site.round(minute, stale="777")["restart_capped"] == ["777"]
    assert len(site.alerts()) == 2 and len(site.journal()) == 6
    assert site.round(22, stale="777")["restarted"] == ["777"]      # the one at minute 11 is 660 s old


def test_the_count_lives_in_the_state_file_not_in_the_supervisor(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1, 2):
        site.round(minute, stale="777")
    stored = site.state()["restart_cap"]
    assert stored["sha"] == site.cfg.sha
    assert stored["roles"]["777"]["restarts"] == [site.at(m).isoformat() for m in (0, 1, 2)]
    # Without the record on disk the next round knows nothing of the three restarts.
    state = site.state()
    del state["restart_cap"]
    site.write_state(state)
    assert site.round(3, stale="777")["restarted"] == ["777"]
    assert site.restarts("777") == [site.at(3).isoformat()] and len(site.journal()) == 4
    # A record written by hand is what the next round goes by.
    state = site.state()
    state["restart_cap"]["roles"]["777"]["restarts"] = [site.at(m).isoformat() for m in (3, 4, 5)]
    site.write_state(state)
    assert site.round(6, stale="777")["restart_capped"] == ["777"] and len(site.journal()) == 4


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
    assert result["refused"] and result["restarted"] == [] and result.get("restart_capped") == []
    assert site.p.killed == [10, 900] and len(site.p.started) == 2
    del site.p.items[880]
    assert site.round(3, stale="777")["restarted"] == ["777"]                 # the third
    assert site.round(4, stale="777")["restart_capped"] == ["777"]           # the fourth is withheld
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1, 3)]   # the refusal neither used nor lost one


def test_a_refusal_does_not_lift_a_suppression(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    to_the_cap(site)
    site.p.items[880] = dict(pid=880, parent_pid=site.pid("risk"), started_utc=site.at(4).isoformat(),
                             argv=[sys.executable, "other.py"], executable=sys.executable)
    assert site.round(4, stale="777")["refused"] is True
    del site.p.items[880]
    assert site.round(5, stale="777")["restart_capped"] == ["777"]
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
    assert site.round(5, stale="777")["restart_capped"] == ["777"]
    # A round that cannot verify says nothing new about a role that is already held.
    result = site.round(6, stale="777", undeterminable=True)
    assert result["deferred"] == ["777"] and result["restarted"] == []
    assert len(site.alerts()) == 1


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
        assert result["restarted"] == [] and result["restart_capped"] == []
        assert site.pid(key) == alive and alive in site.p.items
    assert site.p.killed == [10 if key == "777" else 11, 900]   # the instance before, and the first restarted one
    assert site.restarts(key) == [site.at(1).isoformat()]   # of the two, one is 600 s old: the last inside
    assert site.alerts() == []
    assert site.round(12, stale=key)["restarted"] == [key]        # a full budget
    assert site.restarts(key) == [site.at(12).isoformat()]


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
    assert result["restarted"] == [key] and result["restart_capped"] == []
    assert names(site.journal()) == [key] * 4 and site.restarts(key) == []
    assert site.alerts() == []
    # Only that one. The process of the takeover is lost without a record of its own: from
    # there on every restart counts, and the old exit record exempts nothing.
    for minute in (19, 20, 21):
        assert site.round(minute, gone=key)["restarted"] == [key]
    assert site.round(22, gone=key)["restart_capped"] == [key]
    assert site.restarts(key) == [site.at(m).isoformat() for m in (19, 20, 21)]
    assert names(site.journal()) == [key] * 7 and len(site.alerts()) == 1


@pytest.mark.parametrize("key, _", WAITING)
def test_the_takeover_after_exit_78_goes_ahead_with_three_restarts_still_inside_the_window(tmp_path, monkeypatch, key, _):
    """The rule on its own. The record is written by hand: three restarts still inside the
    window and a registered instance that left with 78. The two do not really go together
    (fifteen minutes of waiting put every earlier restart outside the window), so this is
    what pins that such a takeover is neither withheld nor counted."""
    site = Site(tmp_path, monkeypatch)
    site.exited(key, site.at(-0.5))
    state = site.state() if (site.live / "supervisor-state.json").exists() else {}
    state["restart_cap"] = {"sha": site.cfg.sha, "roles": {key: {
        "restarts": [site.at(m).isoformat() for m in (-8, -7, -6)], "suppressed_since": site.at(-5).isoformat()}}}
    site.write_state(state)
    result = site.round(0, gone=key)
    assert result["restarted"] == [key] and result["restart_capped"] == []
    assert site.restarts(key) == [site.at(m).isoformat() for m in (-8, -7, -6)]    # not a fourth
    assert site.state()["restart_cap"]["roles"][key].get("suppressed_since") is None
    assert site.alerts() == []


def test_an_exit_record_that_is_not_a_clean_78_of_the_registered_instance_exempts_nothing(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    site.exited("risk", site.at(-0.5), rc=79)       # T142: five failed rounds in a row
    assert site.round(0, gone="risk")["restarted"] == ["risk"]
    assert site.restarts("risk") == [site.at(0).isoformat()]
    site.exited("risk", site.at(0.5), rc=2)         # a fatal exit
    assert site.round(1, gone="risk")["restarted"] == ["risk"]
    site.exited("risk", site.at(1.5))               # 78, but dated before the instance registered next
    marker = site.live / "disconnect-exit-risk.json"
    stale = json.loads(marker.read_text())
    marker.write_text(json.dumps({**stale, "ts": (NOW - timedelta(days=1)).isoformat(), "pid": 1}))
    record = site.live / "exit-risk.json"
    record.write_text(json.dumps({**json.loads(record.read_text()),
                                  "ts": (NOW - timedelta(days=1)).isoformat(), "pid": 1}))
    assert site.round(2, gone="risk")["restarted"] == ["risk"]
    assert site.restarts("risk") == [site.at(m).isoformat() for m in (0, 1, 2)]
    assert site.round(3, gone="risk")["restart_capped"] == ["risk"]


def test_an_exit_78_after_less_than_fifteen_minutes_counts(tmp_path, monkeypatch):
    """Not the normal path: the marker says the disconnect lasted less than the limit."""
    site = Site(tmp_path, monkeypatch)
    site.exited("risk", site.at(-0.5))
    marker = site.live / "disconnect-exit-risk.json"
    marker.write_text(json.dumps({**json.loads(marker.read_text()), "duration_s": 12.3}))
    assert site.round(0, gone="risk")["restarted"] == ["risk"]
    assert site.restarts("risk") == [site.at(0).isoformat()]


# ---- the halts keep their own rules: the cap withholds restarts and nothing else ----

BLOCKERS = ["halt-risk-veto.json", "killSwitch", "manual halt"]


@pytest.mark.parametrize("blocker", BLOCKERS)
def test_releasing_the_cap_touches_no_halt_and_the_halt_keeps_its_release_conditions(tmp_path, monkeypatch, blocker):
    site = Site(tmp_path, monkeypatch)
    to_the_cap(site)
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
    # The release lifted the cap and nothing else.
    assert {name: (site.live / name).read_bytes() for name in kept} == kept
    # The role is healthy from now on, well over five minutes: the halt stays while the
    # blocker is there, exactly as without the cap.
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


def test_a_held_role_that_recovers_by_itself_ends_the_suppression_and_the_halt_goes_as_before(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    to_the_cap(site)
    third = site.pid("777")
    for minute in range(4, 9):                 # like 902610 at 23:15: the third process completes loops again
        result = site.round(minute)
        assert result["restarted"] == [] and result["restart_capped"] == [] and result["cleared"] is False
        assert site.pid("777") == third and site.p.killed == [10, 900, 901]
    assert (site.live / "halt.json").exists()
    # The need that was withheld is over; the three restarts are not forgotten.
    assert site.state()["restart_cap"]["roles"]["777"].get("suppressed_since") is None
    assert site.restarts("777") == [site.at(m).isoformat() for m in (0, 1, 2)]
    # Five minutes of health later the halt goes as it always did.
    assert site.round(9)["cleared"] is True and not (site.live / "halt.json").exists()
    assert len(site.alerts()) == 1 and len(site.journal()) == 3
    # Stale again while the three are still inside the window: held at once, and said once more.
    assert site.round(10, stale="777")["restart_capped"] == ["777"]
    assert site.p.killed == [10, 900, 901] and len(site.alerts()) == 2


def test_the_maintenance_stop_still_ends_a_role_that_is_held(tmp_path, monkeypatch):
    """An explicit stop under the kill switch ends every verified tree, the held one included."""
    site = Site(tmp_path, monkeypatch)
    to_the_cap(site)
    (site.live / "killSwitch").touch()
    stopped = site.module.stop_deployment(site.cfg, site.p, verify_deployment=lambda cfg: None)
    assert stopped["refused"] is False and sorted(stopped["stopped"]) == ["777", "778", "reconcile", "risk"]
    assert not site.p.items


# ---- the operator's release ----

def test_an_operator_lifts_the_cap_with_a_file_and_that_leaves_a_trace(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    to_the_cap(site)
    third = site.pid("777")
    site.release("777")
    result = site.round(4, stale="777")
    assert result["restart_cap_released"] == ["777"]
    assert result["restarted"] == ["777"] and result["restart_capped"] == []      # in this very round
    assert site.p.killed == [10, 900, 901, third] and len(site.p.started) == 4
    assert not (site.live / "restart-cap-release-777.json").exists()             # consumed: it cannot lift twice
    assert site.restarts("777") == [site.at(4).isoformat()]                       # and the budget starts over
    assert site.state()["restart_cap"]["roles"]["777"]["released"] == {
        "utc": site.at(4).isoformat(), "restarts": 3}
    assert len(site.alerts()) == 2 and names(site.journal()) == ["777"] * 4
    for part in ("released", "777", "restart-cap-release-777.json", "3 restarts"):
        assert part in site.alerts()[1], part
    for minute in (5, 6, 7):                           # three more, then held again
        site.round(minute, stale="777")
    assert site.round(8, stale="777")["restart_capped"] == ["777"] and len(site.alerts()) == 3


def test_a_release_for_a_role_that_needs_nothing_restarts_nothing(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    for minute in (0, 1):
        site.round(minute, stale="777")
    site.release("777")
    result = site.round(2)                             # every role healthy
    assert result["restart_cap_released"] == ["777"] and result["restarted"] == []
    assert site.p.killed == [10, 900] and site.restarts("777") == []
    assert len(site.alerts()) == 1 and "released" in site.alerts()[0]


def test_a_release_is_for_one_role(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    to_the_cap(site)
    for minute in (4, 5, 6):
        site.round(minute, stale=("777", "778"))
    assert site.round(7, stale=("777", "778"))["restart_capped"] == ["777", "778"]
    site.release("777")
    result = site.round(8, stale=("777", "778"))
    assert result["restart_cap_released"] == ["777"]
    assert result["restarted"] == ["777"] and result["restart_capped"] == ["778"]
    assert site.restarts("778") == [site.at(m).isoformat() for m in (4, 5, 6)]


@pytest.mark.parametrize("name, content", [
    ("restart-cap-release-777.json", json.dumps({"confirm": "release"})),     # another phrase
    ("restart-cap-release-999.json", json.dumps({"confirm": CONFIRM})),       # no such role
    ("restart-cap-release-777.json", "release"),                             # not JSON
    ("restart-cap-release-777.json", json.dumps([CONFIRM])),                 # not an object
])
def test_a_release_request_that_is_not_valid_is_left_in_place_and_named_once(tmp_path, monkeypatch, name, content):
    site = Site(tmp_path, monkeypatch)
    to_the_cap(site)
    (site.live / name).write_text(content, encoding="utf-8")
    for minute in (4, 5):
        result = site.round(minute, stale="777")
        assert result["restart_capped"] == ["777"] and result["restart_cap_released"] == []
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


# ---- the record itself ----

@pytest.mark.parametrize("damage, held", [
    ("roles", ["777", "778", "risk", "reconcile"]),    # the roles of the record are not what they should be
    ("record", ["777"]),                               # one role's entry
    ("entry", ["777"]),                                # one registered time
])
def test_a_record_that_cannot_be_read_holds_the_role_at_the_cap_until_released(tmp_path, monkeypatch, damage, held):
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
    else:
        roles["777"]["restarts"] = [20261101]
    site.write_state(state)
    for minute in (1, 2):
        result = site.round(minute, stale=held)
        assert result["restarted"] == [] and result["restart_capped"] == held
    assert len(site.alerts()) == len(held) and "cannot be read" in site.alerts()[0]   # one for each held role
    for key in held:
        site.release(key)
    result = site.round(3, stale=held)
    assert sorted(result["restart_cap_released"]) == sorted(held) and result["restarted"] == held


def test_a_registered_time_that_cannot_be_read_never_leaves_the_window(tmp_path, monkeypatch):
    site = Site(tmp_path, monkeypatch)
    site.round(0)                                     # a healthy round, so that the record exists
    state = site.state()
    state["restart_cap"]["roles"]["777"]["restarts"] = ["not a time"] * 3
    site.write_state(state)
    for minute in (1, 40):                            # forty minutes later it has not expired
        assert site.round(minute, stale="777")["restart_capped"] == ["777"]
    assert site.restarts("777") == ["not a time"] * 3


def test_the_record_of_another_deployment_counts_for_nothing(tmp_path, monkeypatch):
    """Same data directory, another SHA: the deployment was replaced, and with it the past."""
    site = Site(tmp_path, monkeypatch)
    to_the_cap(site)
    state = site.state()
    state["restart_cap"]["sha"] = "b" * 40
    site.write_state(state)
    assert site.round(4, stale="777")["restarted"] == ["777"]
    assert site.state()["restart_cap"]["sha"] == site.cfg.sha
    assert site.restarts("777") == [site.at(4).isoformat()]


def test_a_role_the_record_has_no_entry_for_starts_with_nothing_registered(tmp_path, monkeypatch):
    """A runner added to the configuration later has no past in the record, and is not
    mistaken for one whose record cannot be read."""
    site = Site(tmp_path, monkeypatch)
    site.round(0, stale="777")                        # the record exists now, without the new role
    site.add_runner(779, 71, "zz-779.toml")
    result = site.round(1, stale="779")
    assert result["restarted"] == ["779"] and result["restart_capped"] == []
    assert site.restarts("779") == [site.at(1).isoformat()] and site.alerts() == []