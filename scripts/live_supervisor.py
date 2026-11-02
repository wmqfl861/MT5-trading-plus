"""T110 external Demo supervisor. Run once per minute; never imports MetaTrader5.

Only an explicitly pinned, detached and clean deployment may be restarted.
Registry/process ambiguity halts and requires an operator; it never authorizes a
kill or a duplicate worker. All OS actions live in WindowsProcesses for offline
state-machine tests.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw.config import load as load_gateway
from mt5live.config import load as load_live
from mt5live.fileio import append_jsonl, file_lock, write_json_atomic
from mt5live.monitor import (append_alert, health_timeout, heartbeat_status,
                             iso_utc, naive_utc, parse_iso, read_heartbeat_path,
                             read_json_retry, write_halt_once)
from mt5live.riskproc import load_risk_config
from mt5data.sessions import load_session_policy

SCRIPTS = {"runner": "live_runner.py", "risk": "risk_process.py", "reconcile": "live_reconcile.py"}


@dataclass(frozen=True)
class Deployment:
    directory: str
    sha: str
    data_dir: str
    config_dir: str
    python: str
    gateway_config: str
    risk_config: str
    sessions_config: str
    spread_model: str
    log_dir: str


def canonical(path):
    return os.path.normcase(os.path.realpath(path))


def components(cfg):
    roles = {}
    files = sorted(Path(cfg.config_dir).glob("*.toml"))
    if not files:
        raise ValueError("empty config directory")
    for path in files:
        live = load_live(str(path))
        if live.volume_lots != 0.01 or live.risk_fraction is not None:
            raise ValueError("Demo requires fixed 0.01 lots")
        key = str(live.magic)
        if key in roles:
            raise ValueError("duplicate magic")
        roles[key] = dict(kind="runner", magic=live.magic, config=str(path), interval=2.0)
    roles["risk"] = dict(kind="risk", interval=10.0)
    roles["reconcile"] = dict(kind="reconcile", interval=30.0)
    return roles


def command(cfg, role):
    kind = role["kind"]
    argv = [cfg.python, "-u", str(Path(cfg.directory) / "scripts" / SCRIPTS[kind]),
            "--data-dir", cfg.data_dir, "--loop"]
    if kind == "runner":
        argv += ["--config", role["config"], "--gateway-config", cfg.gateway_config,
                 "--risk-config", cfg.risk_config, "--sessions-config", cfg.sessions_config,
                 "--spread-model", cfg.spread_model]
    else:
        argv += ["--gw-config", cfg.gateway_config, "--interval", str(role["interval"])]
        if kind == "risk":
            argv += ["--config", cfg.risk_config, "--sessions-config", cfg.sessions_config]
    return argv


def verify_deployment(cfg):
    if not re.fullmatch(r"[0-9a-f]{40}", cfg.sha):
        raise ValueError("deployment_sha requires full lowercase SHA")
    for field in ("directory", "data_dir", "config_dir", "python", "gateway_config",
                  "risk_config", "sessions_config", "spread_model", "log_dir"):
        path = Path(getattr(cfg, field))
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError(f"unsafe path: {field}")
    if not Path(cfg.python).is_file():
        raise ValueError("explicit Python interpreter missing")
    for name in SCRIPTS.values():
        if not (Path(cfg.directory) / "scripts" / name).is_file():
            raise ValueError("deployment script missing")
    def git(*args):
        return subprocess.run(["git", "-C", cfg.directory, *args], capture_output=True,
                              text=True, timeout=15)
    if git("rev-parse", "HEAD").stdout.strip() != cfg.sha:
        raise ValueError("deployment SHA mismatch")
    if git("symbolic-ref", "-q", "HEAD").returncode != 1:
        raise ValueError("deployment must be detached at fixed SHA")
    dirty = git("status", "--porcelain", "--untracked-files=all")
    if dirty.returncode or dirty.stdout.strip():
        raise ValueError("deployment is not clean/isolated")
    if load_gateway(cfg.gateway_config).allow_live_trading is not False:
        raise ValueError("nonDemo configuration: allow_live_trading")
    load_risk_config(cfg.risk_config)
    load_session_policy(cfg.sessions_config)
    if not Path(cfg.spread_model).is_file():
        raise ValueError("spread model missing")
    components(cfg)


def _argv_matches(actual, expected, *, worker=False):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    for index, (left, right) in enumerate(zip(actual, expected)):
        if index == 0 and worker:
            continue  # verified descendant image is checked separately
        if left == right:
            continue
        if os.path.isabs(left) and os.path.isabs(right) and canonical(left) == canonical(right):
            continue
        return False
    return True


def _identity(process, entry, argv):
    worker = process.get("pid") != entry["pid"]
    if not _argv_matches(process.get("argv"), argv, worker=worker):
        raise ValueError("process script/arguments mismatch")
    if process.get("pid") == entry["pid"]:
        if abs((parse_iso(process["started_utc"]) - parse_iso(entry["started_utc"])).total_seconds()) > 1:
            raise ValueError("PID reused/start-time mismatch")
    # Command argv pins the interpreter as well as script and all deployment
    # paths. The venv worker may have a different image, but must remain a
    # Python worker in the verified descendant tree (never a free-standing PID).
    image = Path(process.get("executable", "")).name.lower()
    if image not in ("python.exe", "pythonw.exe", "python", "python3"):
        raise ValueError("unexpected executable")
    if worker:
        if canonical(process["argv"][0]) != canonical(process["executable"]):
            raise ValueError("worker image/argv mismatch")
        if parse_iso(process["started_utc"]) < parse_iso(entry["started_utc"]):
            raise ValueError("child predates registered parent")


def verified_tree(entry, table, argv):
    root = table.get(entry["pid"])
    if root is None:
        for process in table.values():
            if _argv_matches(process.get("argv"), argv, worker=True):
                raise ValueError("missing launcher with surviving/duplicate worker")
            if process.get("parent_pid") == entry["pid"]:
                age = (parse_iso(process["started_utc"]) - parse_iso(entry["started_utc"])).total_seconds()
                if age >= -1:  # registry creation-time tolerance; unknown remains refusal
                    raise ValueError("missing launcher with surviving/duplicate worker")
        return []
    _identity(root, entry, argv)
    observed_entry = {**entry, "started_utc": root["started_utc"]}
    result, seen = [], set()
    def visit(item):
        if item["pid"] in seen:
            raise ValueError("cyclic process tree")
        seen.add(item["pid"])
        _identity(item, observed_entry, argv)
        for child in table.values():
            if child.get("parent_pid") == item["pid"]:
                if parse_iso(child["started_utc"]) < parse_iso(item["started_utc"]):
                    continue  # recorded parent PID was reused; not this tree
                visit(child)
        result.append(item)  # child first, launcher last
    visit(root)
    if any(_argv_matches(p.get("argv"), argv, worker=True) and p["pid"] not in seen for p in table.values()):
        raise ValueError("unregistered duplicate worker")
    return result


def _clear_owned(live, token):
    """Delete only our exact token. Windows exclusive handle closes the read/delete race."""
    with file_lock(str(live / "halt.lock")):
        if (live / "killSwitch").exists() or (live / "halt-risk-veto.json").exists():
            return False
        path = live / "halt.json"
        if os.name == "nt":
            from ctypes import wintypes as w
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.restype = w.HANDLE
            kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                                          w.DWORD, w.DWORD, w.HANDLE]
            handle = kernel.CreateFileW(str(path), 0x80000000 | 0x10000, 0, None, 3, 0, None)
            if handle == w.HANDLE(-1).value:
                return False
            kernel.CloseHandle.argtypes = [w.HANDLE]
            kernel.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]
            kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
            try:
                buf, count = ctypes.create_string_buffer(65536), w.DWORD()
                if not kernel.ReadFile(handle, buf, len(buf), ctypes.byref(count), None):
                    return False
                record = json.loads(buf.raw[:count.value])
                if record.get("owner") != "supervisor" or record.get("token") != token:
                    return False
                if (live / "killSwitch").exists():
                    return False
                delete = w.BOOL(True)
                return bool(kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)))
            finally:
                kernel.CloseHandle(handle)
        else:
            # Supported production host is Windows; cooperating test/POSIX writers
            # use halt.lock. Never clear a changed/unreadable manual record.
            record = read_json_retry(str(path))
            if not record or record.get("owner") != "supervisor" or record.get("token") != token:
                return False
            path.unlink()
            return True


@contextmanager
def registry_lock(path):
    """Interop with demo_run.ps1's FileShare.None, not a separate byte lock."""
    if os.name != "nt":
        with file_lock(path, attempts=1):
            yield
        return
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(path, 0x80000000 | 0x40000000, 0, None, 4, 0, None)
    if handle == w.HANDLE(-1).value:
        raise TimeoutError("instances.lock held by launcher/supervisor")
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def _registered_trees(cfg, processes, *, allow_missing=False):
    """One deployment/registry/tree contract for launcher, stop and supervisor."""
    roles = components(cfg)
    registry = read_json_retry(str(Path(cfg.data_dir) / "live" / "instances.json"))
    if registry is None and allow_missing:
        registry = {"schema_version": 1, "instances": []}
    if not isinstance(registry, dict) or registry.get("schema_version") != 1 or not isinstance(registry.get("instances"), list):
        raise ValueError("bad/missing registry")
    mapped, trees = {}, {}
    table = processes.snapshot()
    for entry in registry["instances"]:
        if not isinstance(entry, dict) or type(entry.get("pid")) is not int or entry["pid"] <= 0:
            raise ValueError("bad registry identity")
        key = str(entry.get("magic")) if entry.get("kind") == "runner" else entry.get("kind")
        if key not in roles or key in mapped:
            raise ValueError("unexpected/duplicate registry role")
        if (entry.get("mode") != "Demo" or entry.get("deployment_sha") != cfg.sha
                or canonical(entry.get("deployment_dir", "")) != canonical(cfg.directory)
                or canonical(entry.get("data_dir", "")) != canonical(cfg.data_dir)
                or not _argv_matches(entry.get("argv"), command(cfg, roles[key]))):
            raise ValueError("registry deployment/mode/arguments mismatch")
        parse_iso(entry["started_utc"])
        log = Path(entry["log"])
        if not log.is_absolute() or not log.resolve().is_relative_to(Path(cfg.log_dir).resolve()):
            raise ValueError("unsafe registry log path")
        mapped[key] = entry
        trees[key] = verified_tree(entry, table, command(cfg, roles[key]))
    for key, role in roles.items():
        if key not in mapped:
            if any(_argv_matches(p.get("argv"), command(cfg, role), worker=True) for p in table.values()):
                raise ValueError("unregistered live worker")
            trees[key] = []
    return roles, mapped, trees


def terminate_verified_tree(entry, processes, argv):
    tree = verified_tree(entry, processes.snapshot(), argv)
    for process in tree:
        processes.terminate(process)
    if verified_tree(entry, processes.snapshot(), argv):
        raise ValueError("worker tree did not terminate")


def inspect_deployment(cfg, processes, *, verify_deployment=verify_deployment):
    """Read-only; demo_run holds instances.lock through this check and launch."""
    try:
        verify_deployment(cfg)
        _, mapped, trees = _registered_trees(cfg, processes, allow_missing=True)
        return {"refused": False, "alive": {key: mapped[key]["pid"]
                for key, tree in trees.items() if tree}}
    except Exception as exc:
        return {"refused": True, "reason": f"{type(exc).__name__}: {exc}"}


def stop_deployment(cfg, processes, *, verify_deployment=verify_deployment):
    """Explicit maintenance stop: all identities first, child-before-parent, no registry erasure."""
    result = {"refused": False, "stopped": []}
    try:
        live = Path(cfg.data_dir) / "live"
        with registry_lock(str(live / "instances.lock")):
            verify_deployment(cfg)
            if not (live / "killSwitch").is_file():
                raise ValueError("stop requires killSwitch; disable supervisor before maintenance")
            roles, mapped, trees = _registered_trees(cfg, processes)
            for key, tree in trees.items():
                if tree:
                    terminate_verified_tree(mapped[key], processes, command(cfg, roles[key]))
                    result["stopped"].append(key)
    except Exception as exc:
        result.update(refused=True, reason=f"{type(exc).__name__}: {exc}")
    return result


def supervise(cfg, processes, *, now=None, clock=None, verify_deployment=verify_deployment):
    observe = clock or ((lambda: now) if now is not None else lambda: datetime.now(timezone.utc))
    now = observe()
    live = Path(cfg.data_dir) / "live"
    live.mkdir(parents=True, exist_ok=True)
    result = {"restarted": [], "refused": False, "cleared": False}
    state_path = live / "supervisor-state.json"
    try:
        with registry_lock(str(live / "instances.lock")), file_lock(str(live / "supervisor.lock"), attempts=1):
            verify_deployment(cfg)
            roles, mapped, trees = _registered_trees(cfg, processes)
            stale = {}
            clean = False
            for key, role in roles.items():
                entry = mapped.get(key)
                tree = trees[key]
                if not tree:
                    stale[key] = "process_missing"
                    continue
                hb_path = live / f"heartbeat-{key}.json"
                try:
                    data = read_json_retry(str(hb_path))
                    hb = read_heartbeat_path(str(hb_path))
                    log_mtime = Path(entry["log"]).stat().st_mtime
                    observed = observe()  # precheck/scan/reads may span new heartbeats
                    healthy = heartbeat_status(hb, observed, timeout_s=health_timeout(role["interval"])).ok
                    healthy = healthy and data.get("pid") in {p["pid"] for p in tree} and data.get("deployment_sha") == cfg.sha
                    healthy = healthy and parse_iso(data["ts"]) >= parse_iso(entry["started_utc"])
                    age = observed.timestamp() - log_mtime
                    healthy = healthy and 0 <= age <= health_timeout(role["interval"])
                except (OSError, ValueError, TypeError, KeyError):
                    healthy = False
                if not healthy:
                    stale[key] = "heartbeat_or_log_stale"
                if key == "reconcile" and healthy:
                    clean = data.get("reconcile_clean") is True
            now = observe()
            state = read_json_retry(str(state_path)) or {}
            signature = sorted((k, e["pid"], e["started_utc"]) for k, e in mapped.items())
            signature = json.loads(json.dumps(signature))
            if stale:
                write_halt_once(str(live), "risk_lost" if "risk" in stale else "component_lost", now,
                                owner="supervisor", token=uuid.uuid4().hex)
                append_alert(str(live), "SUPERVISOR", f"unhealthy={stale}", now)
                # Persist reset BEFORE any process mutation/crash.
                write_json_atomic(str(state_path), {"healthy_since": None, "signature": signature})
                for key, reason in stale.items():
                    entry = mapped.get(key)
                    if entry:
                        terminate_verified_tree(entry, processes, command(cfg, roles[key]))
                    stamp = now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
                    kind = roles[key]["kind"]
                    log = str(Path(cfg.log_dir) / f"{kind}-{key}-{stamp}.log")
                    argv = command(cfg, roles[key])
                    proc = processes.launch(argv, cfg.directory, log,
                                            {**os.environ, "MT5_DEPLOYMENT_SHA": cfg.sha,
                                             "MT5_LIVE_DIR": str(live), "PYTHONUNBUFFERED": "1"})
                    new = dict(pid=proc["pid"], started_utc=proc["started_utc"], kind=kind,
                               magic=roles[key].get("magic"), mode="Demo", argv=argv, log=log,
                               deployment_sha=cfg.sha, deployment_dir=cfg.directory, data_dir=cfg.data_dir)
                    mapped[key] = new
                    write_json_atomic(str(live / "instances.json"), {"schema_version": 1, "instances": list(mapped.values())})
                    append_jsonl(str(live / "ops-restarts.jsonl"), {
                        "utc": iso_utc(now), "component": key, "reason_code": reason,
                        "old_pid": entry["pid"] if entry else None, "new_pid": proc["pid"],
                        "deployment_sha": cfg.sha})
                    result["restarted"].append(key)
                return result
            # Health must be observed continuously, not inferred from two samples
            # five minutes apart. Missed supervisor polls reset the window.
            gap = None if not state.get("last_checked") else (naive_utc(now) - parse_iso(state["last_checked"])).total_seconds()
            since = state.get("healthy_since")
            if (not clean or state.get("signature") != signature or gap is None or gap < 0 or gap > 90):
                since = iso_utc(now) if clean else None
            halt = read_json_retry(str(live / "halt.json"))
            token = halt.get("token") if isinstance(halt, dict) and halt.get("owner") == "supervisor" else None
            if state.get("halt_token") != token:
                since = iso_utc(now) if clean else None
            if since and (naive_utc(now) - parse_iso(since)).total_seconds() >= 300 and token:
                result["cleared"] = _clear_owned(live, token)
            write_json_atomic(str(state_path), {"healthy_since": since, "last_checked": iso_utc(now),
                                               "signature": signature, "halt_token": token})
    except Exception as exc:
        result["refused"] = True
        result["reason"] = f"{type(exc).__name__}: {exc}"
        write_halt_once(str(live), "supervisor_refused", now, owner="supervisor")
        append_alert(str(live), "SUPERVISOR_REFUSED", result["reason"], now)
        write_json_atomic(str(state_path), {"healthy_since": None})
    return result


class WindowsProcesses:
    """No shell-interpolated input; terminate by a creation-time-bound OS handle."""
    def snapshot(self):
        source = ("Get-CimInstance Win32_Process | ForEach-Object { "
                  "[pscustomobject]@{pid=$_.ProcessId;parent_pid=$_.ParentProcessId;"
                  "started_utc=$_.CreationDate.ToUniversalTime().ToString('o');"
                  "executable=$_.ExecutablePath;command_line=$_.CommandLine} } | ConvertTo-Json -Depth 3 -Compress")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", source],
                             capture_output=True, timeout=30)
        if out.returncode:
            raise RuntimeError("process inventory failed")
        rows = json.loads(out.stdout.decode("utf-8-sig"))
        if isinstance(rows, dict):
            rows = [rows]
        from ctypes import wintypes as w
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.CommandLineToArgvW.restype = ctypes.POINTER(w.LPWSTR)
        shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [w.HLOCAL]
        table = {}
        for row in rows:
            if not row.get("command_line"):
                row.pop("command_line", None)
                row["argv"] = None
                table[row["pid"]] = row  # existing but unreadable is never absent
                continue
            count = ctypes.c_int()
            ptr = shell.CommandLineToArgvW(row.pop("command_line"), ctypes.byref(count))
            if not ptr:
                raise RuntimeError("command line unavailable")
            try:
                row["argv"] = [ptr[i] for i in range(count.value)]
            finally:
                kernel.LocalFree(ptr)
            table[row["pid"]] = row
        return table

    def terminate(self, process):
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = w.HANDLE
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        handle = kernel.OpenProcess(0x1000 | 0x1 | 0x100000, False, process["pid"])
        if not handle:
            raise RuntimeError("cannot acquire process handle")
        try:
            created, exited, kern, user = (w.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kern), ctypes.byref(user)):
                raise RuntimeError("cannot verify process creation")
            seconds = ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 10000000 - 11644473600
            expected = parse_iso(process["started_utc"]).replace(tzinfo=timezone.utc).timestamp()
            if abs(seconds - expected) > 0.01:
                raise RuntimeError("PID reused before terminate")
            if not kernel.TerminateProcess(handle, 75):
                raise RuntimeError("termination failed")
            if kernel.WaitForSingleObject(handle, 10000) != 0:
                raise RuntimeError("termination incomplete")
        finally:
            kernel.CloseHandle(handle)

    def launch(self, argv, cwd, log, env):
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as stdout, open(log + ".err", "ab") as stderr:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        row = self.snapshot().get(proc.pid)
        if row is None:
            raise RuntimeError("launched process vanished before registration")
        return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    for field in Deployment.__dataclass_fields__:
        ap.add_argument("--" + field.replace("_", "-"), required=True)
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--inspect", action="store_true", help="read-only launcher tree precheck")
    action.add_argument("--stop", action="store_true", help="verified maintenance stop; requires killSwitch")
    args = vars(ap.parse_args(argv))
    inspect, stop = args.pop("inspect"), args.pop("stop")
    cfg, processes = Deployment(**args), WindowsProcesses()
    result = (inspect_deployment(cfg, processes) if inspect else
              stop_deployment(cfg, processes) if stop else supervise(cfg, processes))
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 2 if result["refused"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
