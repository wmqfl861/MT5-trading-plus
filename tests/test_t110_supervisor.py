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
        assert inspected["refused"] is orphan, inspected  # show the refusal reason if this flakes
        assert stopped["refused"] is orphan, stopped
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


# ---- T211: the two production launch paths (a real console host is required) ----

def sleeping_role(tmp_path):
    fake = tmp_path / "scripts" / "risk_process.py"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("import time\ntime.sleep(120)\n")
    return [sys.executable, "-u", str(fake)]


def console_hosts(table, launcher_pid):
    import ntpath
    return [row for row in table.values() if row.get("parent_pid") == launcher_pid
            and ntpath.basename(row.get("executable") or "").lower() == "conhost.exe"]


def await_console_host(backend, launcher_pid, seconds=15):
    """Fail with the launcher's tree when no console host shows up: the test would prove nothing."""
    import time
    table, deadline = {}, time.monotonic() + seconds
    while time.monotonic() < deadline:
        table = backend.snapshot()
        if console_hosts(table, launcher_pid):
            return table
        time.sleep(0.1)
    tree = {pid: row for pid, row in table.items()
            if pid == launcher_pid or row.get("parent_pid") == launcher_pid}
    pytest.fail(f"no console host under the launched role; launcher tree = {tree}")


def verify_and_terminate(mod, backend, launcher_pid, started_utc, argv):
    entry = dict(pid=launcher_pid, started_utc=started_utc)
    table = await_console_host(backend, launcher_pid)
    hosts = {row["pid"] for row in console_hosts(table, launcher_pid)}
    tree = mod.verified_tree(entry, table, argv)
    assert tree[-1]["pid"] == launcher_pid and not hosts & {item["pid"] for item in tree}
    mod.terminate_verified_tree(entry, backend, argv)
    assert not mod.verified_tree(entry, backend.snapshot(), argv)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console host")
def test_launch_path_windows_processes_launch_is_verified_and_terminated(tmp_path):
    """WindowsProcesses.launch() is how the supervisor restarts a role."""
    import os
    import subprocess
    mod = load_supervisor()
    argv = sleeping_role(tmp_path)
    backend = mod.WindowsProcesses()
    row = backend.launch(argv, str(tmp_path), str(tmp_path / "role.log"),
                         {**os.environ, "PYTHONUNBUFFERED": "1"})
    try:
        verify_and_terminate(mod, backend, row["pid"], row["started_utc"], argv)
    finally:
        subprocess.run(["taskkill", "/PID", str(row["pid"]), "/T", "/F"], capture_output=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console host")
def test_launch_path_demo_run_start_process_hidden_is_verified_and_terminated(tmp_path):
    """The Start-Process form demo_run.ps1 uses to launch every role."""
    import subprocess
    mod = load_supervisor()
    script = (ROOT / "scripts" / "demo_run.ps1").read_text(encoding="utf-8")
    for token in ("Start-Process -FilePath $PythonExeAbs -ArgumentList $quotedArgs",
                  "-WindowStyle Hidden -PassThru",
                  "-RedirectStandardOutput $log -RedirectStandardError $errLog"):
        assert token in script, f"demo_run.ps1 launch form changed: {token!r}"
    argv = sleeping_role(tmp_path)
    log = str(tmp_path / "role.log")
    launch = tmp_path / "launch.ps1"
    launch.write_text(
        "param([string]$PythonExe, [string]$Script, [string]$WorkDir, [string]$Log)\n"
        "$argList = @('-u', $Script)\n"
        "$quotedArgs = @($argList | ForEach-Object {\n"
        "    if ($_.Contains('\"')) { throw \"Unsupported quote in process argument\" }\n"
        "    '\"' + $_.TrimEnd('\\') + '\"'\n"
        "})\n"
        "$p = Start-Process -FilePath $PythonExe -ArgumentList $quotedArgs `\n"
        "    -WorkingDirectory $WorkDir -WindowStyle Hidden -PassThru `\n"
        "    -RedirectStandardOutput $Log -RedirectStandardError \"$Log.err\"\n"
        "$p.Id\n")
    # The launched role inherits what PowerShell holds, so its output goes to
    # files: with pipes the caller would wait for the role itself to exit.
    pid_file, err_file = tmp_path / "launcher.pid", tmp_path / "launcher.err"
    with open(pid_file, "w") as out, open(err_file, "w") as err:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launch),
                        sys.executable, argv[2], str(tmp_path), log],
                       stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=120)
    launcher_pid = pid_file.read_text().strip()
    assert launcher_pid.isdigit(), err_file.read_text()
    launcher_pid = int(launcher_pid)
    try:
        backend = mod.WindowsProcesses()
        verify_and_terminate(mod, backend, launcher_pid,
                             backend.snapshot()[launcher_pid]["started_utc"], argv)
    finally:
        subprocess.run(["taskkill", "/PID", str(launcher_pid), "/T", "/F"], capture_output=True)


# ---- C2 (2026-11-24): a tree whose launcher leaves by itself
#
# A role is two processes: the venv's python.exe (the launcher), which waits for its worker
# and exits the moment the worker dies, and the interpreter running the role. Ending the
# worker first and the launcher right after worked only when the backend reached the
# launcher within a fraction of a millisecond. Otherwise the launcher was already on its
# way out, TerminateProcess failed, and the round was refused ("termination failed").

def launcher_with_worker(tmp_path):
    """The risk role as a real two-level tree: the venv's launcher and an interpreter
    that runs a script doing nothing but sleep."""
    mod, cfg, _, live, entries = setup(tmp_path)
    entry = next(e for e in entries if e["kind"] == "risk")
    script = Path(entry["argv"][2])
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import time\ntime.sleep(120)\n")
    return mod, cfg, live, entry


def start_role(entry):
    import subprocess
    return subprocess.Popen(entry["argv"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def verified_real_tree(mod, backend, entry, launcher, seconds=15):
    """The registry entry of a real launcher, and its tree once verified_tree has seen
    the worker too (the launcher starts it a fraction of a second after it comes up)."""
    import time
    deadline = time.monotonic() + seconds
    while True:
        table = backend.snapshot()
        entry = dict(entry, pid=launcher, started_utc=table[launcher]["started_utc"])
        tree = mod.verified_tree(entry, table, entry["argv"])
        if len(tree) >= 2:
            return entry, tree
        if time.monotonic() > deadline:
            pytest.fail(f"the tree under pid {launcher} never came up whole: {tree}")
        time.sleep(0.1)


def late_for_the_launcher(backend, launcher, seconds=0.1):
    """Make the backend reach the launcher only after a pause, so that it has left by
    itself by then. Returns the backend's own terminate, for the clean-up."""
    import time
    original = backend.terminate

    def terminate(process):
        if process["pid"] == launcher:
            time.sleep(seconds)
        original(process)

    backend.terminate = terminate
    return original


def end_fake(terminate, tree):
    for item in tree:
        try:
            terminate(item)
        except RuntimeError:
            pass  # already gone


def kernel32_as_the_supervisor_sees_it(monkeypatch, mod, wrap):
    """Replace the ctypes module as live_supervisor names it: WinDLL gives the real
    library, except that kernel32 is wrap(real library)."""
    import ctypes
    import types
    real = ctypes.WinDLL
    namespace = types.SimpleNamespace(**vars(ctypes))
    namespace.WinDLL = lambda name, *args, **kwargs: (
        wrap(real(name, *args, **kwargs)) if name.lower() == "kernel32"
        else real(name, *args, **kwargs))
    monkeypatch.setattr(mod, "ctypes", namespace)


def record_kernel_calls(monkeypatch, mod, calls):
    """kernel32 with TerminateProcess and WaitForSingleObject reporting every call. The
    real call is made as well."""

    def wrap(real):
        class Function:
            def __init__(self, name, fn):
                self._name, self._fn = name, fn

            def __getattr__(self, attr):
                return getattr(self._fn, attr)

            def __setattr__(self, attr, value):    # restype and argtypes go to the real function
                if attr.startswith("_"):
                    object.__setattr__(self, attr, value)
                else:
                    setattr(self._fn, attr, value)

            def __call__(self, *args):
                calls.append(self._name)
                return self._fn(*args)

        class Kernel:
            def __getattr__(self, name):
                if name in ("TerminateProcess", "WaitForSingleObject"):
                    return Function(name, getattr(real, name))
                return getattr(real, name)

        return Kernel()

    kernel32_as_the_supervisor_sees_it(monkeypatch, mod, wrap)


def refuse_to_open(monkeypatch, mod, error):
    """kernel32 whose OpenProcess finds nothing and leaves `error` as the last error."""

    def wrap(real):
        class Kernel:
            def __getattr__(self, name):
                return (lambda access, inherit, pid: None) if name == "OpenProcess" else getattr(real, name)

        return Kernel()

    kernel32_as_the_supervisor_sees_it(monkeypatch, mod, wrap)
    monkeypatch.setattr(mod.ctypes, "get_last_error", lambda: error)


def wait_until_gone(pid, seconds=30):
    """Wait until no process object has this pid any more: OpenProcess then fails with 87."""
    import ctypes
    import time
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = w.HANDLE
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)   # query and wait only
        if not handle:
            assert ctypes.get_last_error() == 87
            return
        kernel.CloseHandle(handle)
        time.sleep(0.05)
    pytest.fail(f"the process object of pid {pid} is still there after {seconds} s")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake process tree")
def test_stop_ends_a_tree_whose_launcher_has_left_by_itself(tmp_path):
    """C2, the stop path. The worker is ended first and the launcher follows at once; here
    the backend reaches the launcher 0.1 s late, so that it has already left. The stop
    used to be refused with RuntimeError("termination failed")."""
    mod, cfg, live, entry = launcher_with_worker(tmp_path)
    proc = start_role(entry)
    backend = mod.WindowsProcesses()
    original, tree = backend.terminate, []
    try:
        entry, tree = verified_real_tree(mod, backend, entry, proc.pid)
        (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": [entry]}))
        (live / "killSwitch").touch()
        original = late_for_the_launcher(backend, proc.pid)
        stopped = mod.stop_deployment(cfg, backend, verify_deployment=lambda c: None)
        assert stopped["refused"] is False, stopped
        assert stopped["stopped"] == ["risk"]
        assert not mod.verified_tree(entry, backend.snapshot(), entry["argv"])
    finally:
        backend.terminate = original
        end_fake(original, tree)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake process tree")
def test_supervise_restarts_a_role_whose_launcher_has_left_by_itself(tmp_path):
    """C2, the restart path: the same tree under supervise(). The role is restarted because
    its heartbeat names a pid that is not in the tree. The restart used to end the round
    as a refusal (halt supervisor_refused, the alert SUPERVISOR_REFUSED) and nothing after
    it was restarted. Roles are not really relaunched here: launch only answers."""
    mod, cfg, live, entry = launcher_with_worker(tmp_path)
    proc = start_role(entry)
    backend = mod.WindowsProcesses()
    launched = []

    def launch(argv, cwd, log, env):
        pid = 900 + len(launched)
        launched.append(argv)
        return dict(pid=pid, started_utc=NOW.isoformat(), argv=argv, executable=argv[0])

    backend.launch = launch
    original, tree = backend.terminate, []
    try:
        entry, tree = verified_real_tree(mod, backend, entry, proc.pid)
        (live / "instances.json").write_text(json.dumps({"schema_version": 1, "instances": [entry]}))
        original = late_for_the_launcher(backend, proc.pid)
        result = mod.supervise(cfg, backend, now=NOW, verify_deployment=lambda c: None)
        assert result["refused"] is False, result.get("reason")
        assert result["restarted"] == ["777", "risk", "reconcile"]
        rows = [json.loads(line) for line in (live / "ops-restarts.jsonl").read_text().splitlines()]
        assert [row["component"] for row in rows] == ["777", "risk", "reconcile"]
        assert rows[1]["old_pid"] == proc.pid and rows[1]["new_pid"] == 901
    finally:
        backend.terminate = original
        end_fake(original, tree)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows fake process tree")
def test_a_tree_is_terminated_in_full_before_any_member_is_waited_for(tmp_path, monkeypatch):
    """C2: every member gets its TerminateProcess while it is still the process that was
    verified, and only then is anything waited for. Waiting for the worker first is what
    left the launcher already leaving when its turn came."""
    mod, cfg, live, entry = launcher_with_worker(tmp_path)
    proc = start_role(entry)
    backend = mod.WindowsProcesses()
    original, tree = backend.terminate, []
    try:
        entry, tree = verified_real_tree(mod, backend, entry, proc.pid)
        calls = []
        record_kernel_calls(monkeypatch, mod, calls)
        mod.terminate_verified_tree(entry, backend, entry["argv"])
        assert calls == ["TerminateProcess", "TerminateProcess", "WaitForSingleObject", "WaitForSingleObject"]
        assert not mod.verified_tree(entry, backend.snapshot(), entry["argv"])
    finally:
        end_fake(original, tree)
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_terminate_counts_an_exited_process_as_ended(tmp_path):
    """C2: a process that left by itself has nothing left to end. While some handle keeps
    its process object alive OpenProcess still finds it; that used to be "termination failed"."""
    import subprocess
    mod = load_supervisor()
    backend = mod.WindowsProcesses()
    short = subprocess.Popen([sys._base_executable, "-I", "-c", "import sys; sys.stdin.readline()"],
                             stdin=subprocess.PIPE)
    try:
        row = backend.snapshot()[short.pid]        # its creation time, read while it runs
        short.stdin.close()                        # end of input: it leaves by itself
        short.wait(timeout=30)                     # this handle keeps its process object
        backend.terminate(row)
    finally:
        if short.poll() is None:
            short.kill()
        short.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_terminate_counts_a_vanished_process_as_ended(tmp_path):
    """C2: the same once the process object is gone and OpenProcess fails; that used to be
    "cannot acquire process handle". Nothing here holds a handle: the launcher was started
    by the backend itself and left when its worker did."""
    import os
    mod = load_supervisor()
    backend = mod.WindowsProcesses()
    argv = sleeping_role(tmp_path)
    launched = backend.launch(argv, str(tmp_path), str(tmp_path / "role.log"),
                               {**os.environ, "PYTHONUNBUFFERED": "1"})
    _, tree = verified_real_tree(mod, backend, dict(launched, argv=argv), launched["pid"])
    backend.terminate(tree[0])                    # the worker
    wait_until_gone(launched["pid"])
    backend.terminate(launched)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_terminate_still_refuses_a_pid_that_now_belongs_to_another_process(tmp_path):
    """C2: "already gone" is not "somebody else is there now". The creation time is still
    checked through the handle; when it differs nothing is terminated."""
    import subprocess
    mod = load_supervisor()
    backend = mod.WindowsProcesses()
    other = subprocess.Popen([sys._base_executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        row = dict(backend.snapshot()[other.pid])
        row["started_utc"] = (mod.parse_iso(row["started_utc"]) + timedelta(seconds=1)).isoformat()
        with pytest.raises(RuntimeError, match="PID reused before terminate"):
            backend.terminate(row)
        assert other.poll() is None                # left alone
    finally:
        other.kill()
        other.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_terminate_still_refuses_what_cannot_be_identified(tmp_path, monkeypatch):
    """C2: "already gone" is not "could not be opened". A process that cannot be opened
    (access denied here) is still a refusal and is left alone. So is pid 0, for which
    OpenProcess answers 87 just as it does for a pid that no longer exists."""
    import subprocess
    mod = load_supervisor()
    backend = mod.WindowsProcesses()
    other = subprocess.Popen([sys._base_executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        row = backend.snapshot()[other.pid]
        refuse_to_open(monkeypatch, mod, error=5)
        with pytest.raises(RuntimeError, match="cannot acquire process handle"):
            backend.terminate(row)
        assert other.poll() is None                # left alone

        refuse_to_open(monkeypatch, mod, error=87)
        with pytest.raises(RuntimeError, match="cannot acquire process handle"):
            backend.terminate(dict(row, pid=0))
    finally:
        other.kill()
        other.wait(timeout=5)


# ---- C3 (2026-11-24): the log age may read a little negative
#
# A role is called stale when its log is too old: 0 <= now - log_mtime <= timeout. The file's
# last write time can read a little ahead of the clock taken right after it (Python's clock
# moves in steps of about a millisecond), so for a moment after a role wrote its log the age
# was a tiny negative number and a healthy role was called stale: halt, restart. The heartbeats
# are written here with the same clock the supervisor reads, as in production: only the log
# age is negative.

def run_with_the_log_written_just_after_the_clock(tmp_path, early):
    """setup(), the heartbeats stamped with the supervisor's clock, and supervise() with
    that clock reading `early` seconds before the roles' logs were last written."""
    import os
    mod, cfg, p, live, entries = setup(tmp_path)
    observed = datetime.fromtimestamp(os.stat(entries[0]["log"]).st_mtime - early, tz=timezone.utc)
    for e in entries:
        key = str(e["magic"]) if e["kind"] == "runner" else e["kind"]
        fresh(live, key, e["pid"], cfg.sha, observed)
    return mod, cfg, p, live, run(mod, cfg, p, now=observed)


@pytest.mark.parametrize("early", [0.0005, 0.001, 1.5])
def test_a_log_written_moments_after_the_clock_does_not_make_a_healthy_role_stale(tmp_path, early):
    """C3: inside LOG_AGE_TOLERANCE_S a negative log age is not staleness. All three values
    used to halt and restart every role."""
    mod, cfg, p, live, result = run_with_the_log_written_just_after_the_clock(tmp_path, early)
    assert result["restarted"] == [], result
    assert result["refused"] is False and p.started == [] and p.killed == []
    assert not (live / "halt.json").exists()
    assert mod.LOG_AGE_TOLERANCE_S == 2.0


@pytest.mark.parametrize("early", [2.5, 3.0])
def test_a_log_from_farther_ahead_than_the_tolerance_still_makes_the_role_stale(tmp_path, early):
    """C3: the tolerance is 2 seconds, not "any log time in the future will do". A log from
    farther ahead is still unhealthy: halt and restart, as before."""
    mod, cfg, p, live, result = run_with_the_log_written_just_after_the_clock(tmp_path, early)
    assert result["restarted"] == ["777", "risk", "reconcile"]
    assert (live / "halt.json").exists()


def test_the_alert_records_a_negative_log_age_as_it_is(tmp_path):
    """C3: `log_age_s=` in the alert's checks is the number that was computed. A negative one
    stays negative, outside the tolerance and inside it."""
    # Farther ahead than the tolerance: stale, and the check says by how much.
    mod, cfg, p, live, result = run_with_the_log_written_just_after_the_clock(tmp_path, 3.0)
    assert result["restarted"] == ["777", "risk", "reconcile"]
    assert "log_age_s=-3.0" in (live / "alerts.log").read_text(encoding="utf-8")

    # Inside the tolerance the log is not what makes the role stale; a stale heartbeat is,
    # and the negative log age is still written as it was.
    import os
    work = tmp_path / "stale-heartbeat"
    work.mkdir()
    mod, cfg, p, live, entries = setup(work)
    observed = datetime.fromtimestamp(os.stat(entries[0]["log"]).st_mtime - 1.0, tz=timezone.utc)
    for e in entries:
        key = str(e["magic"]) if e["kind"] == "runner" else e["kind"]
        fresh(live, key, e["pid"], cfg.sha, observed - timedelta(seconds=300))
    result = run(mod, cfg, p, now=observed)
    assert result["restarted"] == ["777", "risk", "reconcile"]
    alert = (live / "alerts.log").read_text(encoding="utf-8")
    assert "log_age_s=-1.0" in alert and "heartbeat=timeout" in alert
