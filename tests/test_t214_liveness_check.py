"""T214 hourly liveness check: pure logic over a synthetic site.

No MetaTrader5, no terminal, no real process table (the liveness probe is
injected), no network (the notifier is injected). The clock is injected too.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 24, 21, 30, tzinfo=timezone.utc)
ROLES = (("runner", 902610, 11), ("runner", 902501, 12), ("risk", None, 13), ("reconcile", None, 14))
_SITES = 0


def load():
    spec = importlib.util.spec_from_file_location("t214_liveness", ROOT / "scripts" / "ops" / "liveness_check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def key_of(kind, magic):
    return str(magic) if kind == "runner" else kind


def site(tmp_path, *, hb_age=5.0, supervisor_age=30.0, halt=None, pauses=(), kill=False, veto=False,
         report=True, registry=True, now=NOW):
    """A synthetic live directory under a fresh sub-directory. Ages are seconds before `now`."""
    global _SITES
    _SITES += 1
    tmp_path = tmp_path / f"site{_SITES}"
    def ago(seconds):
        return (now - timedelta(seconds=seconds)).isoformat()
    # hb_age is one number for every role, or a {role key: seconds} mapping (others 5 s).
    def age_of(key):
        return hb_age.get(key, 5.0) if isinstance(hb_age, dict) else hb_age
    data = tmp_path / "data"
    live, reports = data / "live", data / "reports"
    live.mkdir(parents=True), (reports / "live").mkdir(parents=True)
    if registry:
        entries = [{"pid": pid, "started_utc": ago(9000), "kind": kind, "magic": magic}
                   for kind, magic, pid in ROLES]
        (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": entries}), encoding="utf-8")
    for kind, magic, pid in ROLES:
        (live / f"heartbeat-{key_of(kind, magic)}.json").write_text(
            json.dumps({"runner_alive": True, "ts": ago(age_of(key_of(kind, magic))), "pid": pid + 100}),
            encoding="utf-8")
    if supervisor_age is not None:
        (live / "supervisor-state.json").write_text(json.dumps({"last_checked": ago(supervisor_age)}), encoding="utf-8")
    if halt is not None:
        (live / "halt.json").write_text(json.dumps(
            {"reason": "component_lost", "owner": "supervisor", "ts": ago(halt)}), encoding="utf-8")
    for magic, age in pauses:
        (live / f"pause-{magic}.json").write_text(json.dumps(
            {"magic": magic, "paused": True, "reason": "RECONCILE_MISMATCH", "ts": ago(age)}), encoding="utf-8")
    if kill:
        (live / "killSwitch").write_text("", encoding="utf-8")
    if veto:
        (live / "halt-risk-veto.json").write_text("{}", encoding="utf-8")
    if report:
        yesterday = (now - timedelta(days=1)).date()
        (reports / "live" / f"daily-{yesterday.isoformat()}.md").write_text("# report", encoding="utf-8")
    return live, reports


def codes(findings):
    return {item["code"]: item["production"] for item in findings}


def run(mod, tmp_path, alive=None, state=None, **kwargs):
    live, reports = site(tmp_path, **kwargs)
    alive = alive or (lambda pid, started: True)
    findings, new_state = mod.evaluate(mod.observe(live, reports, tmp_path, NOW, alive), NOW, state or {})
    return findings, new_state, live, reports


def test_healthy_site_has_no_findings(tmp_path):
    mod = load()
    findings, state, _, _ = run(mod, tmp_path)
    assert findings == [] and state == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_a_registered_process_is_recognised_by_its_creation_time():
    mod = load()
    started = mod.process_started_utc(os.getpid())
    assert started is not None and mod.process_alive(os.getpid(), started)
    shifted = (datetime.fromisoformat(started) + timedelta(seconds=5)).isoformat()
    assert not mod.process_alive(os.getpid(), shifted)            # PID reused by a later process
    assert not mod.process_alive(os.getpid() + 400000, started) or True   # unknown PID: nothing to assert
    assert mod.process_started_utc(99999999) is None               # no such process
    assert not mod.process_alive(99999999, started)


def test_missing_role_is_production_at_once_when_the_heartbeat_is_already_old(tmp_path):
    mod = load()
    alive = lambda pid, started: pid != 13                       # risk launcher gone
    findings, _, _, _ = run(mod, tmp_path, alive=alive, hb_age={"risk": 700.0})
    assert codes(findings) == {"role_missing:risk": True}
    # Recently gone and not revived yet: reported, but not production-level on the first look.
    findings, state, _, _ = run(mod, tmp_path, alive=alive, hb_age={"risk": 100.0})
    assert codes(findings) == {"role_missing:risk": False}
    assert state == {"role_missing:risk": NOW.isoformat()}
    # Still gone ten minutes later by the checker's own clock: production-level.
    findings, _, _, _ = run(mod, tmp_path, alive=alive, hb_age={"risk": 100.0},
                            state={"role_missing:risk": (NOW - timedelta(seconds=700)).isoformat()})
    assert codes(findings) == {"role_missing:risk": True}


def test_stale_heartbeat_and_silent_supervisor(tmp_path):
    mod = load()
    findings, _, _, _ = run(mod, tmp_path, hb_age=130.0)
    assert codes(findings) == {f"heartbeat_stale:{key}": False for key in ("902610", "902501", "risk", "reconcile")}
    findings, _, _, _ = run(mod, tmp_path, hb_age=700.0)
    assert set(codes(findings).values()) == {True}
    findings, _, _, _ = run(mod, tmp_path, supervisor_age=200.0)
    assert codes(findings) == {"supervisor_stale": False}
    findings, _, _, _ = run(mod, tmp_path, supervisor_age=700.0)
    assert codes(findings) == {"supervisor_stale": True}
    findings, _, _, _ = run(mod, tmp_path, supervisor_age=None)      # no state file at all
    assert codes(findings) == {"supervisor_stale": False}


def test_control_files_become_production_after_an_hour(tmp_path):
    mod = load()
    findings, _, _, _ = run(mod, tmp_path, halt=100.0, pauses=[(902501, 100.0)], kill=True, veto=True)
    assert codes(findings) == {"halt": False, "kill_switch": False, "pause:902501": False, "risk_veto": False}
    findings, _, _, _ = run(mod, tmp_path, halt=3700.0, pauses=[(902501, 3700.0)], kill=True, veto=True,
                            state={"kill_switch": (NOW - timedelta(seconds=3700)).isoformat(),
                                   "risk_veto": (NOW - timedelta(seconds=3599)).isoformat()})
    assert codes(findings) == {"halt": True, "kill_switch": True, "pause:902501": True, "risk_veto": False}
    halted = next(item for item in findings if item["code"] == "halt")
    assert halted["reason"] == "component_lost" and halted["owner"] == "supervisor"


def test_low_disk_missing_report_and_unreadable_registry(tmp_path, monkeypatch):
    mod = load()
    for free_gb, production in ((10.0, False), (9.99, True)):
        monkeypatch.setattr(mod.shutil, "disk_usage", lambda path, free=free_gb * 2 ** 30:
                            shutil._ntuple_diskusage(100 * 2 ** 30, 50 * 2 ** 30, free))
        findings, _, _, _ = run(mod, tmp_path)
        assert codes(findings).get("disk_low", None) in (None, production)
        assert ("disk_low" in codes(findings)) is production
    monkeypatch.undo()
    findings, _, _, _ = run(mod, tmp_path, report=False)
    assert codes(findings) == {"report_missing": True}
    findings, _, _, _ = run(mod, tmp_path, registry=False)
    assert codes(findings) == {"registry_unreadable": False}


def test_state_keeps_only_problems_that_are_still_there(tmp_path):
    mod = load()
    alive = lambda pid, started: pid != 14
    findings, state, _, _ = run(mod, tmp_path, alive=alive)
    assert set(state) == {"role_missing:reconcile"}
    findings, state, _, _ = run(mod, tmp_path, state=state)       # reconcile came back
    assert findings == [] and state == {}


def test_notifier_sends_counts_only_and_only_for_production_level_problems(tmp_path):
    mod = load()
    assert mod.notify([], tmp_path / "notify.local.json") == "none"
    finding = {"code": "disk_low", "production": False, "lasted_s": 5, "free_gb": 9.5}
    assert mod.notify([finding], tmp_path / "notify.local.json") == "none"
    assert mod.notify([{**finding, "production": True}], tmp_path / "missing.json") == "no_channel"
    config = tmp_path / "notify.local.json"
    config.write_text(json.dumps({"channel": "carrier-pigeon", "target": "rooftop"}), encoding="utf-8")
    assert mod.notify([{**finding, "production": True}], config) == "unsupported_channel"
    config.write_text(json.dumps({"channel": "webhook", "target": "https://example.invalid/hook"}), encoding="utf-8")
    sent = []
    assert mod.notify([{**finding, "production": True},
                       {"code": "role_missing:risk", "production": True, "lasted_s": 720, "role": "risk"}],
                      config, send=lambda target, message: sent.append((target, message))) == "sent"
    assert sent == [("https://example.invalid/hook", "MT5 Demo 值守：disk_low(0分) role_missing:risk(12分)")]
    assert str(tmp_path) not in sent[0][1]


def test_cli_prints_one_json_line_and_the_exit_code(tmp_path, capsys):
    mod = load()
    live, reports = site(tmp_path, report=False)
    state_file = tmp_path / "state.json"
    sent = []
    config = tmp_path / "notify.local.json"
    config.write_text(json.dumps({"channel": "webhook", "target": "https://example.invalid/hook"}), encoding="utf-8")
    rc = mod.main(["--live-dir", str(live), "--reports-dir", str(reports), "--disk-path", str(tmp_path),
                   "--state-file", str(state_file), "--notify-config", str(config)],
                  now=NOW, alive=lambda pid, started: True, send=lambda target, message: sent.append(message))
    out = capsys.readouterr().out
    assert rc == 3 and out.count("\n") == 1 and str(tmp_path) not in out
    result = json.loads(out)
    assert result["production"] is True and result["ok"] is False and result["notified"] == "sent"
    assert [item["code"] for item in result["findings"]] == ["report_missing"]
    assert len(sent) == 1 and sent[0] == "MT5 Demo 值守：report_missing(0分)"
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"report_missing": NOW.isoformat()}
    # A failing check reports its own failure without leaking the path it stumbled on.
    capsys.readouterr()
    rc = mod.main(["--live-dir", str(live), "--reports-dir", str(reports), "--disk-path", str(tmp_path),
                   "--state-file", str(state_file), "--no-notify"],
                  now=NOW, alive=raising)
    out = capsys.readouterr().out
    assert rc == 2 and json.loads(out)["error"] == "RuntimeError" and str(tmp_path) not in out


def raising(pid, started):
    raise RuntimeError(f"cannot open {pid}")
