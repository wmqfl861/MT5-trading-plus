"""T110 external supervision is testable without inspecting or stopping live PIDs."""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)  # Sunday: no prices/decisions required


def load_supervisor():
    path = ROOT / "scripts" / "live_supervisor.py"
    assert path.exists(), "external supervisor missing"
    spec = importlib.util.spec_from_file_location("t110_supervisor", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Processes:
    def __init__(self):
        self.items = {}
        self.started = []
        self.killed = []

    def snapshot(self):
        return dict(self.items)

    def terminate(self, process):
        assert self.items[process["pid"]] == process
        self.killed.append(process["pid"])
        del self.items[process["pid"]]

    def launch(self, argv, cwd, log, env):
        pid = 900 + len(self.started)
        self.started.append(argv)
        item = dict(pid=pid, parent_pid=0, started_utc=NOW.isoformat(),
                    argv=argv, executable=argv[0])
        self.items[pid] = item
        return item


def setup(tmp_path):
    mod = load_supervisor()
    dep = tmp_path / "deployment"
    dep.mkdir()
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "one.toml").write_text('symbol="XAUUSD"\ntimeframe="M15"\nstrategy="tests.fakes_live:ClosingSignal"\nmagic=777\nlookback_bars=30\nvolume_lots=0.01\n')
    data = tmp_path / "data"
    live = data / "live"
    live.mkdir(parents=True)
    gw = tmp_path / "gateway.json"
    gw.write_text('{"allow_live_trading": false}')
    risk = tmp_path / "risk.toml"
    risk.write_text((ROOT / "config" / "risk-demo.toml").read_text())
    sessions = tmp_path / "sessions.toml"
    sessions.write_text((ROOT / "config" / "sessions.toml").read_text())
    spread = tmp_path / "spread.json"
    spread.write_text('{}')
    cfg = mod.Deployment(str(dep), "a" * 40, str(data), str(configs), sys.executable,
                         str(gw), str(risk), str(sessions), str(spread), str(tmp_path / "logs"))
    processes = Processes()
    entries = []
    for i, (key, role) in enumerate(mod.components(cfg).items(), 10):
        argv = mod.command(cfg, role)
        proc = dict(pid=i, parent_pid=0, started_utc=(NOW - timedelta(minutes=10)).isoformat(),
                    argv=argv, executable=argv[0])
        processes.items[i] = proc
        log = Path(cfg.log_dir) / f"{key}.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("completed loop\n")
        import os
        os.utime(log, (NOW.timestamp(), NOW.timestamp()))
        entries.append(dict(pid=i, started_utc=proc["started_utc"], kind=role["kind"],
                            magic=role.get("magic"), mode="Demo", deployment_sha=cfg.sha,
                            deployment_dir=cfg.directory, data_dir=cfg.data_dir,
                            argv=argv, log=str(log)))
        fresh(live, key, i, cfg.sha, NOW)
    (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": entries}))
    return mod, cfg, processes, live, entries


def fresh(live, key, pid, sha, now):
    (live / f"heartbeat-{key}.json").write_text(json.dumps(dict(
        runner_alive=True, ts=now.isoformat(), pid=pid, deployment_sha=sha,
        reconcile_clean=True)))


def run(mod, cfg, processes, now=NOW):
    return mod.supervise(cfg, processes, now=now, verify_deployment=lambda c: None)


def test_lost_risk_halts_and_restarts_only_missing_role(tmp_path):
    mod, cfg, p, live, entries = setup(tmp_path)
    old = next(e for e in entries if e["kind"] == "risk")
    del p.items[old["pid"]]
    result = run(mod, cfg, p)
    assert result["restarted"] == ["risk"]
    assert len(p.started) == 1 and "risk_process.py" in " ".join(p.started[0])
    assert "--data-dir" in p.started[0] and cfg.data_dir in p.started[0]
    halt = json.loads((live / "halt.json").read_text())
    assert halt["owner"] == "supervisor"
    row = json.loads((live / "ops-restarts.jsonl").read_text())
    assert row["component"] == "risk" and row["old_pid"] == old["pid"]
    assert row["deployment_sha"] == cfg.sha and row["new_pid"] == 900


def test_stall_kills_verified_child_before_parent(tmp_path):
    mod, cfg, p, live, entries = setup(tmp_path)
    e = next(e for e in entries if e["kind"] == "risk")
    p.items[80] = dict(p.items[e["pid"]], pid=80, parent_pid=e["pid"])
    fresh(live, "risk", 80, cfg.sha, NOW - timedelta(seconds=121))
    assert run(mod, cfg, p)["restarted"] == ["risk"]
    assert p.killed == [80, e["pid"]]


@pytest.mark.parametrize("corruption", ["pid_reuse", "script_spoof", "wrong_sha", "malformed_registry", "non_demo", "unknown_child"])
def test_unsafe_identity_refuses_all_restarts(tmp_path, corruption):
    mod, cfg, p, live, entries = setup(tmp_path)
    e = entries[0]
    if corruption == "pid_reuse":
        p.items[e["pid"]]["started_utc"] = NOW.isoformat()
    elif corruption == "script_spoof":
        p.items[e["pid"]]["argv"] = [sys.executable, "-c", "sleep()", "live_runner.py"]
    elif corruption == "unknown_child":
        p.items[88] = dict(p.items[e["pid"]], pid=88, parent_pid=e["pid"], argv=[sys.executable, "other.py"])
    elif corruption == "wrong_sha":
        entries[0]["deployment_sha"] = "b" * 40
    elif corruption == "non_demo":
        entries[0]["mode"] = "Real"
    if corruption == "malformed_registry":
        (live / "instances.json").write_text("{")
    else:
        (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": entries}))
    result = run(mod, cfg, p)
    assert result["refused"]
    assert not p.started and not p.killed
    assert (live / "halt.json").exists()


def test_weekend_progress_does_not_require_decisions_or_prices(tmp_path):
    mod, cfg, p, live, _ = setup(tmp_path)
    assert not run(mod, cfg, p)["restarted"]
    assert not (live / "halt.json").exists()


@pytest.mark.parametrize("manual", [False, True])
def test_auto_clear_requires_five_minutes_and_ownership(tmp_path, manual):
    mod, cfg, p, live, entries = setup(tmp_path)
    mod.write_halt_once(str(live), "lost risk", NOW, owner="supervisor", token="ours")
    run(mod, cfg, p)
    if manual:
        (live / "halt.json").write_text('{"reason":"manual","owner":"manual","token":"theirs"}')
    for seconds in [60, 120, 180, 240, 299, 300]:
        now = NOW + timedelta(seconds=seconds)
        import os
        for e in entries:
            key = str(e["magic"]) if e["kind"] == "runner" else e["kind"]
            fresh(live, key, e["pid"], cfg.sha, now)
            os.utime(e["log"], (now.timestamp(), now.timestamp()))
        run(mod, cfg, p, now)
        if seconds < 300:
            assert (live / "halt.json").exists()
    assert (live / "halt.json").exists() == manual


def test_reconcile_diff_and_killswitch_prevent_clear(tmp_path):
    mod, cfg, p, live, entries = setup(tmp_path)
    mod.write_halt_once(str(live), "lost risk", NOW, owner="supervisor", token="ours")
    (live / "killSwitch").touch()
    run(mod, cfg, p)
    now = NOW + timedelta(minutes=5)
    for e in entries:
        key = str(e["magic"]) if e["kind"] == "runner" else e["kind"]
        fresh(live, key, e["pid"], cfg.sha, now)
    (live / "heartbeat-reconcile.json").write_text(json.dumps(dict(
        runner_alive=True, ts=now.isoformat(), pid=12, deployment_sha=cfg.sha, reconcile_clean=False)))
    run(mod, cfg, p, now)
    assert (live / "halt.json").exists()


def test_missing_root_with_surviving_worker_refuses_duplicate(tmp_path):
    mod, cfg, p, live, entries = setup(tmp_path)
    e = next(e for e in entries if e["kind"] == "risk")
    p.items[80] = dict(p.items[e["pid"]], pid=80, parent_pid=e["pid"])
    del p.items[e["pid"]]
    result = run(mod, cfg, p)
    assert result["refused"] is True
    assert p.started == []
    assert p.killed == []


def test_competing_risk_halt_cannot_be_cleared_with_supervisor_halt(tmp_path):
    mod, cfg, p, live, entries = setup(tmp_path)
    mod.write_halt_once(str(live), "risk lost", NOW, owner="supervisor", token="ours")
    mod.write_halt_once(str(live), "daily_loss_cap", NOW, owner="risk")
    assert json.loads((live / "halt.json").read_text())["reason"] == "risk lost"
    assert not mod._clear_owned(live, "ours")


def test_monitor_data_unavailable_does_not_publish_new_health(tmp_path, monkeypatch):
    from tests.test_t110_health import script
    from tests.test_mt5live_gate import make_gate
    from mt5gw.errors import DataUnavailableError
    from tests.fakes_live import FakeGateway
    mod = script("risk_process")
    gate, live = make_gate(tmp_path)
    path = live / "heartbeat-risk.json"
    before = path.read_bytes()
    def missing(*a, **k):
        raise DataUnavailableError("account unavailable")
    monkeypatch.setattr(mod, "build_snapshot", missing)
    with pytest.raises(DataUnavailableError):
        mod.run_one_cycle(gate._process, FakeGateway(gate._account))
    assert path.read_bytes() == before


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_windows_backend_verifies_fake_launcher_worker_tree(tmp_path):
    """Actual venv launcher shape, but only our sleeping fake is inspected/killed."""
    import subprocess
    import time
    mod = load_supervisor()
    fake = tmp_path / "risk_process.py"
    fake.write_text("import time\ntime.sleep(60)\n")
    argv = [sys.executable, "-u", str(fake)]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backend = mod.WindowsProcesses()
    try:
        table = backend.snapshot()
        entry = dict(pid=proc.pid, started_utc=table[proc.pid]["started_utc"])
        # Registry timestamp has a small allowed tolerance; child age is
        # compared to the observed parent creation, not registry write time.
        entry["started_utc"] = (mod.parse_iso(entry["started_utc"]) + timedelta(milliseconds=100)).isoformat()
        tree = mod.verified_tree(entry, table, argv)
        assert tree and tree[-1]["pid"] == proc.pid
        for item in tree:
            backend.terminate(item)
        assert proc.wait(timeout=5) == 75
        assert not mod.verified_tree(entry, backend.snapshot(), argv)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_supervisor_uses_same_instances_lock_as_launcher(tmp_path):
    import subprocess
    mod, cfg, p, live, entries = setup(tmp_path)
    # Windows exclusive sharing is the launcher's lock contract; emulate only
    # that file handle without touching any business process or scheduler.
    from mt5live.fileio import file_lock
    with mod.registry_lock(str(live / "instances.lock")):
        result = run(mod, cfg, p)
    assert result["refused"] and not p.started


@pytest.mark.skipif(sys.platform != "win32", reason="Windows inventory")
def test_inaccessible_existing_pid_is_preserved_in_inventory(tmp_path, monkeypatch):
    from types import SimpleNamespace
    mod = load_supervisor()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps([dict(pid=123, parent_pid=0,
            started_utc=NOW.isoformat(), executable=None, command_line=None)]).encode()))
    table = mod.WindowsProcesses().snapshot()
    assert 123 in table
    assert table[123]["argv"] is None
    with pytest.raises(ValueError):
        mod.verified_tree(dict(pid=123, started_utc=NOW.isoformat()), table,
                          [sys.executable, "-u", str(tmp_path / "risk_process.py")])


@pytest.mark.parametrize("future", [False, True])
def test_supervisor_observes_health_after_reads_and_scan(tmp_path, monkeypatch, future):
    import os
    from types import SimpleNamespace
    mod, cfg, p, live, entries = setup(tmp_path)
    current = [NOW]
    monkeypatch.setattr(mod, "datetime", SimpleNamespace(now=lambda tz: current[0]))
    original = mod.read_heartbeat_path
    def read_with_progress(path):
        current[0] += timedelta(seconds=1)
        key = Path(path).stem.removeprefix("heartbeat-")
        e = next(e for e in entries if (str(e["magic"]) if e["kind"] == "runner" else e["kind"]) == key)
        ts = current[0] + timedelta(seconds=1 if future else 0)
        fresh(live, key, e["pid"], cfg.sha, ts)
        os.utime(e["log"], (current[0].timestamp(), current[0].timestamp()))
        return original(path)
    monkeypatch.setattr(mod, "read_heartbeat_path", read_with_progress)
    def precheck(_):
        current[0] += timedelta(seconds=4)
    result = mod.supervise(cfg, p, verify_deployment=precheck)
    assert result["refused"] is False
    if future:
        assert result["restarted"]
        assert (live / "halt.json").exists()
    else:
        assert result["restarted"] == []
        assert p.started == [] and p.killed == []
        assert not (live / "halt.json").exists()


@pytest.mark.parametrize("unreadable", ["root", "child", "orphan"])
def test_launcher_inspection_and_stop_refuse_unknown_tree(tmp_path, unreadable):
    mod, cfg, p, live, entries = setup(tmp_path)
    entry = next(e for e in entries if e["kind"] == "risk")
    if unreadable == "root":
        p.items[entry["pid"]]["argv"] = None
    else:
        p.items[80] = dict(p.items[entry["pid"]], pid=80, parent_pid=entry["pid"])
        if unreadable == "child":
            p.items[80]["argv"] = None
        else:
            del p.items[entry["pid"]]
    registry = (live / "instances.json").read_bytes()
    (live / "killSwitch").touch()
    inspected = mod.inspect_deployment(cfg, p, verify_deployment=lambda c: None)
    stopped = mod.stop_deployment(cfg, p, verify_deployment=lambda c: None)
    assert inspected["refused"] is True and stopped["refused"] is True
    assert p.killed == [] and p.started == []
    assert (live / "instances.json").read_bytes() == registry


def test_stop_validates_all_roles_before_any_kill_and_retains_registry(tmp_path):
    mod, cfg, p, live, entries = setup(tmp_path)
    entry = next(e for e in entries if e["kind"] == "risk")
    p.items[80] = dict(p.items[entry["pid"]], pid=80, parent_pid=entry["pid"])
    registry = (live / "instances.json").read_bytes()
    # No stop signal: refuse all side effects, even for fully verified trees.
    assert mod.stop_deployment(cfg, p, verify_deployment=lambda c: None)["refused"]
    assert p.killed == []
    (live / "killSwitch").touch()
    result = mod.stop_deployment(cfg, p, verify_deployment=lambda c: None)
    assert result["refused"] is False
    assert p.killed.index(80) < p.killed.index(entry["pid"])
    assert not p.items and not p.started
    assert (live / "instances.json").read_bytes() == registry
    inspected = mod.inspect_deployment(cfg, p, verify_deployment=lambda c: None)
    assert inspected["refused"] is False and inspected["alive"] == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake process tree")
@pytest.mark.parametrize("orphan", [False, True])
def test_windows_fixed_entrypoints_share_verified_fake_tree(tmp_path, orphan):
    import os
    import subprocess
    import time
    mod, cfg, _, live, entries = setup(tmp_path)
    entry = next(e for e in entries if e["kind"] == "risk")
    script = Path(entry["argv"][2])
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import os,sys,subprocess,time\n"
        "if not os.environ.get('T110_FAKE_WORKER'):\n"
        " subprocess.Popen([sys._base_executable,'-u',*sys.argv],env={**os.environ,'T110_FAKE_WORKER':'1'})\n"
        "else:\n"
        " open(os.environ['T110_READY'],'w').write('ready')\n"
        "time.sleep(60)\n")
    ready = tmp_path / "ready"
    proc = subprocess.Popen(entry["argv"], env={**os.environ, "T110_READY": str(ready)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    backend = mod.WindowsProcesses()
    original_terminate = backend.terminate
    known = []
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "fake worker did not start"
        table = backend.snapshot()
        entry["pid"] = proc.pid
        entry["started_utc"] = table[proc.pid]["started_utc"]
        known = mod.verified_tree(entry, table, entry["argv"])
        assert len(known) >= 2
        (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": [entry]}))
        registry = (live / "instances.json").read_bytes()
        (live / "killSwitch").touch()
        killed = []
        def terminate(item):
            killed.append(item["pid"])
            original_terminate(item)
        backend.terminate = terminate
        if orphan:
            original_terminate(table[proc.pid])  # own fake launcher only, worker deliberately survives
            proc.wait(timeout=5)
            assert any(item["pid"] in backend.snapshot() for item in known[:-1])
        inspected = mod.inspect_deployment(cfg, backend, verify_deployment=lambda c: None)
        stopped = mod.stop_deployment(cfg, backend, verify_deployment=lambda c: None)
        assert inspected["refused"] is orphan
        assert stopped["refused"] is orphan
        if orphan:
            assert killed == []
        else:
            assert killed == [item["pid"] for item in known]
            assert not mod.verified_tree(entry, backend.snapshot(), entry["argv"])
        assert (live / "instances.json").read_bytes() == registry
    finally:
        for item in known:
            try:
                original_terminate(item)
            except RuntimeError:
                pass  # exited fake only; handle creation time guards reused PIDs
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_old_parent_pid_link_is_not_a_descendant_of_reused_root(tmp_path):
    mod, cfg, p, live, entries = setup(tmp_path)
    entry = next(e for e in entries if e["kind"] == "risk")
    p.items[80] = dict(p.items[entry["pid"]], pid=80, parent_pid=entry["pid"],
                       started_utc=(NOW - timedelta(hours=1)).isoformat(), argv=None)
    tree = mod.verified_tree(entry, p.snapshot(), entry["argv"])
    assert [item["pid"] for item in tree] == [entry["pid"]]
    mod.terminate_verified_tree(entry, p, entry["argv"])
    assert p.killed == [entry["pid"]] and 80 in p.items


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell fixed deployment")
def test_powershell_fixed_deployment_inspect_and_stop(tmp_path, monkeypatch):
    """Exercise the real PS1 route against a detached fake repo and sleeping fake role."""
    import os
    import shutil
    import subprocess
    from tests import test_demo_run_ps1 as ps1
    mod, cfg, _, live, entries = setup(tmp_path)
    dep = Path(cfg.directory)
    shutil.copytree(ROOT / "src", dep / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    scripts = dep / "scripts"
    scripts.mkdir()
    for name in ("demo_run.ps1", "live_supervisor.py", "config_check.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    for name in mod.SCRIPTS.values():
        (scripts / name).write_text("import time\ntime.sleep(60)\n")
    def git(*args):
        return subprocess.run(["git", "-C", str(dep), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("add", "src", "scripts")
    git("-c", "user.name=T110 fixture", "-c", "user.email=t110@example.invalid",
        "commit", "-qm", "offline fake deployment")
    sha = git("rev-parse", "HEAD")
    git("checkout", "--detach", "-q", sha)
    from dataclasses import replace
    cfg = replace(cfg, sha=sha)
    entry = next(e for e in entries if e["kind"] == "risk")
    entry["deployment_sha"] = sha
    (Path(cfg.data_dir) / "specs").mkdir()
    (Path(cfg.data_dir) / "specs" / "latest.json").write_text("{}")
    backend = mod.WindowsProcesses()
    proc = subprocess.Popen(entry["argv"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    known = []
    try:
        table = backend.snapshot()
        entry.update(pid=proc.pid, started_utc=table[proc.pid]["started_utc"])
        known = mod.verified_tree(entry, table, entry["argv"])
        (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": [entry]}))
        before = (live / "instances.json").read_bytes()
        (live / "killSwitch").touch()
        monkeypatch.setattr(ps1, "SCRIPT", scripts / "demo_run.ps1")
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
        args = ["-DataDir", cfg.data_dir, "-ConfigDir", cfg.config_dir,
                "-PythonExe", cfg.python, "-GatewayConfig", cfg.gateway_config,
                "-RiskConfig", cfg.risk_config, "-SessionsConfig", cfg.sessions_config,
                "-SpreadModel", cfg.spread_model, "-ResearchCache", cfg.spread_model,
                "-LogDir", cfg.log_dir, "-DeploymentSha", cfg.sha, "-MinFreeGB", "0"]
        checked = ps1.run_ps1("-Mode", "Demo", "-PrecheckOnly", "-SkipGatewayCheck", *args)
        assert checked.returncode == 0, ps1._diag(checked)
        assert str(proc.pid) in ps1.out_text(checked) or "risk" in ps1.out_text(checked)
        assert proc.poll() is None
        stopped = ps1.run_ps1("-Stop", *args)
        assert stopped.returncode == 0, ps1._diag(stopped)
        assert not mod.verified_tree(entry, backend.snapshot(), entry["argv"])
        assert (live / "instances.json").read_bytes() == before
    finally:
        for item in known:
            try:
                backend.terminate(item)
            except RuntimeError:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
