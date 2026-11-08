"""T180: runtime restart evidence -> formal_descriptor.py -> demo_check exit 3.

The synthetic world is production-shaped: demo_run.ps1's run-0 anchor snapshots,
two real ``live_supervisor.supervise`` passes (risk process_missing, runner
heartbeat stale) on a fake process table, then the real generator and the real
checker. Nothing here is a real deployment.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
MAGICS = [902601, 902602]
JOURNAL_FIELDS = {"utc", "component", "reason_code", "old_pid", "new_pid", "deployment_sha"}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def iso(t):
    return t.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def get(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def roles_of(doc):
    return {str(e["magic"]) if e["kind"] == "runner" else e["kind"]: e for e in doc["instances"]}


class Processes:
    """Fake table; the supervisor's verified_tree walks it like the real one."""

    def __init__(self):
        self.items, self.started, self.launch_now = {}, [], None

    def snapshot(self):
        return dict(self.items)

    def terminate(self, process):
        assert self.items[process["pid"]] == process
        del self.items[process["pid"]]

    def launch(self, argv, cwd, log, env):
        now = self.launch_now
        pid = 900 + len(self.started)
        self.started.append(argv)
        self.items[pid] = dict(pid=pid, parent_pid=0, started_utc=iso(now + timedelta(seconds=1)),
                               argv=argv, executable=argv[0])
        return self.items[pid]


def build(tmp_path: Path, trading_count=12):
    """Fixture world (checker side) + a real-path deployment for the supervisor."""
    fixture = _load("t180_fixture", ROOT / "tests" / "test_demo_formal_window.py")
    f = fixture.build_evidence(tmp_path, trading_count)
    data_posix, logs_posix = (tmp_path / "data").as_posix(), (tmp_path / "logs").as_posix()
    # The supervisor stats log/data paths; the deployment dir stays lexical-only.
    for path in [f.live / "instances.json", f.live / "run-manifest.json",
                 tmp_path / "retained/manifest-0.json", tmp_path / "retained/launcher-0.json",
                 tmp_path / "retained/instances-0.json"]:
        path.write_text(path.read_text(encoding="utf-8")
                        .replace("C:/synthetic/data", data_posix)
                        .replace("C:/synthetic/logs", logs_posix), encoding="utf-8")
    run0 = f.live / "evidence/run-0"
    run0.mkdir(parents=True)
    for name, src in (("manifest", "manifest-0"), ("launcher", "launcher-0"),
                      ("instances", "instances-0")):
        (tmp_path / "retained" / f"{src}.json").rename(run0 / f"{name}.json")
    # Deployment record: one table row with exactly one SHA / UTC time / PIDs.
    pids = get(f.live / "instances.json")["instances"]
    pid_text = "、".join(f"{e['kind']}{' ' + str(e['magic']) if e['magic'] else ''} pid={e['pid']}"
                        for e in pids)
    record = (f"| 04:35 | 部署完成：SHA {SHA}，完成时刻 {iso(f.start)}；"
              f"启动器 PID：{pid_text}；心跳解释器 PID 另列（口径：venv 子进程） |")
    (tmp_path / "phase-f-log.md").write_text(
        "| 时刻（UTC） | 事件 |\n| --- | --- |\n" + record + "\n", encoding="utf-8")
    today = datetime.now(timezone.utc).date()
    (tmp_path / "calendar.txt").write_text("".join(
        f"{d} {'weekday' if d.weekday() < 5 else 'weekend'}\n"
        for d in [f.start.date() + timedelta(days=i)
                  for i in range((today - f.start.date()).days + 1)]), encoding="utf-8")
    return f


def supervisor_world(f, tmp_path):
    mod = _load("t180_supervisor", ROOT / "scripts" / "live_supervisor.py")
    mod.components = lambda cfg: {
        **{str(m): dict(kind="runner", magic=m, interval=2.0,
                        config=f"C:/synthetic/deployment/configs/{m}.toml") for m in MAGICS},
        "risk": dict(kind="risk", interval=10.0), "reconcile": dict(kind="reconcile", interval=30.0)}
    cfg = mod.Deployment("C:/synthetic/deployment", SHA, str(tmp_path / "data"),
                         "C:/synthetic/deployment/configs",
                         "C:/synthetic/deployment/.venv/Scripts/python.exe",
                         "C:/synthetic/deployment/gateway.json", "C:/synthetic/deployment/risk.toml",
                         "C:/synthetic/deployment/sessions.toml",
                         (tmp_path / "data").as_posix() + "/spread.json", str(tmp_path / "logs"))
    processes = Processes()
    for key, e in roles_of(get(f.live / "instances.json")).items():
        processes.items[e["pid"]] = dict(pid=e["pid"], parent_pid=0,
                                         started_utc=e["started_utc"], argv=e["argv"],
                                         executable=e["argv"][0])
    return mod, cfg, processes


def make_all_healthy(f, now):
    for key, e in roles_of(get(f.live / "instances.json")).items():
        (f.live / f"heartbeat-{key}.json").write_text(json.dumps(dict(
            runner_alive=True, ts=iso(now), pid=e["pid"], deployment_sha=SHA,
            reconcile_clean=True)))
        log = Path(e["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
        os.utime(log, (now.timestamp(), now.timestamp()))


def two_restarts(f, tmp_path):
    """Deploy + two supervisor restarts on different components (T180 scenario)."""
    mod, cfg, processes = supervisor_world(f, tmp_path)
    now1, now2 = f.start + timedelta(days=5), f.start + timedelta(days=6)
    make_all_healthy(f, now1)
    risk = next(e for e in get(f.live / "instances.json")["instances"] if e["kind"] == "risk")
    del processes.items[risk["pid"]]
    processes.launch_now = now1
    r1 = mod.supervise(cfg, processes, now=now1, verify_deployment=lambda c: None)
    assert r1["restarted"] == ["risk"] and r1["evidence_failures"] == []
    make_all_healthy(f, now2)
    runner = str(MAGICS[0])
    (f.live / f"heartbeat-{runner}.json").write_text(json.dumps(dict(
        runner_alive=True, ts=iso(now2 - timedelta(seconds=300)),
        pid=roles_of(get(f.live / "instances.json"))[runner]["pid"],
        deployment_sha=SHA, reconcile_clean=True)))
    processes.launch_now = now2
    r2 = mod.supervise(cfg, processes, now=now2, verify_deployment=lambda c: None)
    assert r2["restarted"] == [runner] and r2["evidence_failures"] == []
    (f.live / "halt.json").unlink(missing_ok=True)


def generate(tmp_path: Path):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/formal_descriptor.py"),
         "--root", str(tmp_path), "--deploy-log", "phase-f-log.md",
         "--deploy-lines", "3", "3", "--calendar", "calendar.txt"],
        capture_output=True, text=True, encoding="utf-8", timeout=120)


def check(tmp_path: Path):
    def snap():
        return {p: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in tmp_path.rglob("*") if p.is_file()}
    before = snap()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/demo_check.py"),
         "--data-dir", str(tmp_path / "data"), "--log-dir", str(tmp_path / "logs"),
         "--reports-dir", str(tmp_path / "reports"),
         "--sessions-config", str(tmp_path / "sessions.toml"),
         "--accept", "--formal-window", str(tmp_path / "formal.json")],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        env={**os.environ, "PYTHONUTF8": "1"})
    assert snap() == before, "checker modified its evidence"
    return result


def test_deploy_two_restarts_generator_then_checker_returns_3(tmp_path):
    f = build(tmp_path)
    two_restarts(f, tmp_path)
    journal = [json.loads(x) for x in (f.live / "ops-restarts.jsonl").read_text().splitlines()]
    assert [row["component"] for row in journal] == ["risk", str(MAGICS[0])]
    assert all(set(row) == JOURNAL_FIELDS for row in journal)
    index = [json.loads(x) for x in
             (f.live / "evidence/instances/index.jsonl").read_text().splitlines()]
    assert [e["seq"] for e in index] == [1, 2]
    for e in index:
        for side in ("before_instances", "after_instances"):
            p = f.live / e[side]["path"]
            assert hashlib.sha256(p.read_bytes()).hexdigest() == e[side]["sha256"]
    gen = generate(tmp_path)
    assert gen.returncode == 0, gen.stdout + gen.stderr
    descriptor = get(tmp_path / "formal.json")
    assert descriptor["deployment_sha"] == SHA and descriptor["anchor"]["run_index"] == 0
    assert [b["line_number"] for b in descriptor["restart_bindings"]] == [1, 2]
    result = check(tmp_path)
    out = result.stdout + result.stderr
    assert result.returncode == 3, out
    assert "mechanical_checks_passed=true" in out


def test_pre_anchor_runs_without_evidence_are_skipped(tmp_path):
    """Real manifests carry older-deployment runs before the anchor (no copies)."""
    f = build(tmp_path)
    old = dict(started_utc=iso(f.start - timedelta(days=1)), mode="Demo",
               kind="start", log_dir="C:/old/logs")
    for path in (f.live / "run-manifest.json", f.live / "evidence/run-0/manifest.json"):
        doc = get(path)
        doc["runs"].insert(0, old)
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (f.live / "evidence/run-0").rename(f.live / "evidence/run-1")
    two_restarts(f, tmp_path)
    gen = generate(tmp_path)
    assert gen.returncode == 0, gen.stdout + gen.stderr
    assert get(tmp_path / "formal.json")["anchor"]["run_index"] == 1
    result = check(tmp_path)
    assert result.returncode == 3, result.stdout + result.stderr


@pytest.mark.parametrize("victim", [
    "data/live/evidence/run-0/instances.json",
    "data/live/evidence/run-0/launcher.json",
    "data/live/evidence/instances/{}-1-before.json",
])
def test_deleting_any_retained_copy_returns_2(tmp_path, victim):
    f = build(tmp_path)
    two_restarts(f, tmp_path)
    stamp = (f.start + timedelta(days=5)).strftime("%Y%m%d-%H%M%S")
    (tmp_path / victim.format(stamp)).unlink()
    result = check(tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr


@pytest.mark.parametrize("victim, expect", [
    ("data/live/evidence/run-0/launcher.json", "incomplete retained evidence: run-0"),
    ("data/live/evidence/instances/index.jsonl", "no matching runtime evidence index entry"),
    ("calendar.txt", "calendar"),
])
def test_generator_refuses_missing_inputs(tmp_path, victim, expect):
    f = build(tmp_path)
    two_restarts(f, tmp_path)
    (tmp_path / victim).unlink()
    gen = generate(tmp_path)
    assert gen.returncode != 0
    assert expect in gen.stdout + gen.stderr


def test_generator_refuses_calendar_gap(tmp_path):
    f = build(tmp_path)
    two_restarts(f, tmp_path)
    lines = (tmp_path / "calendar.txt").read_text().splitlines()
    (tmp_path / "calendar.txt").write_text(
        "\n".join(l for l in lines if not l.startswith(str(f.trading[3]))) + "\n", encoding="utf-8")
    gen = generate(tmp_path)
    assert gen.returncode != 0
    assert "calendar" in gen.stdout + gen.stderr


def test_copy_failure_still_restarts_and_alerts(tmp_path):
    f = build(tmp_path)
    mod, cfg, processes = supervisor_world(f, tmp_path)
    now1 = f.start + timedelta(days=5)
    make_all_healthy(f, now1)
    risk = next(e for e in get(f.live / "instances.json")["instances"] if e["kind"] == "risk")
    del processes.items[risk["pid"]]
    # Block the before-copy destination with a directory of the exact name.
    stamp = now1.strftime("%Y%m%d-%H%M%S")
    (f.live / "evidence/instances" / f"{stamp}-1-before.json").mkdir(parents=True)
    processes.launch_now = now1
    result = mod.supervise(cfg, processes, now=now1, verify_deployment=lambda c: None)
    assert result["restarted"] == ["risk"]
    assert result["evidence_failures"] == ["risk"]
    alerts = (f.live / "alerts.log").read_text(encoding="utf-8")
    assert "SUPERVISOR_EVIDENCE" in alerts and "before copy failed" in alerts
    row = json.loads((f.live / "ops-restarts.jsonl").read_text().splitlines()[0])
    assert set(row) == JOURNAL_FIELDS and row["component"] == "risk"
    assert not (f.live / "evidence/instances/index.jsonl").exists()
    assert (f.live / "evidence/instances" / f"{stamp}-1-after.json").is_file()
    assert generate(tmp_path).returncode != 0  # incomplete evidence yields no descriptor
