"""T110 external Demo supervisor. Run once per minute; never imports MetaTrader5.

Only an explicitly pinned, detached and clean deployment may be restarted.
Registry/process ambiguity halts and requires an operator; it never authorizes a
kill or a duplicate worker. Process-table access and termination live in
WindowsProcesses for offline state-machine tests; identity checks resolve paths
on disk (``canonical()``) exactly as the launcher does.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager, nullcontext
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


class DeploymentUndeterminable(Exception):
    """git could not answer this round (timed out or failed to start).

    That says nothing about the deployment: it is neither clean nor dirty here.
    """


UNDETERMINABLE_LIMIT = 3         # consecutive rounds; the third escalates to a refusal
CLEAN_REUSE_S = 15 * 60          # a full "git status" runs at most this often
_PID_GONE = 87                   # ERROR_INVALID_PARAMETER: no process has this pid any more
# A role's log cannot really be newer than "now", but right after the role wrote it the
# file's last write time can read a little ahead of the clock taken later: for up to one
# step of the clock, about a millisecond here. Requiring "0 <=" called a healthy role
# stale whenever a check fell in that moment. This much "from the future" is tolerated;
# beyond it the role is still unhealthy.
LOG_AGE_TOLERANCE_S = 2.0


def _absolute(program: str, preferred=()) -> str:
    """Absolute path of an external program. The supervisor may end processes, so
    nothing it runs is looked up through PATH at call time (C23-5)."""
    for candidate in preferred:
        if candidate and os.path.isabs(candidate) and Path(candidate).is_file():
            return candidate
    found = shutil.which(program)
    if not found or not os.path.isabs(found):
        raise DeploymentUndeterminable(f"{program} not found")
    return found


def powershell_exe() -> str:
    return _absolute("powershell", (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                                "System32", "WindowsPowerShell", "v1.0",
                                                "powershell.exe"),))


def _git(directory, *args) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([_absolute("git"), "-C", directory, *args],
                              capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise DeploymentUndeterminable(f"git {args[0]}: {type(exc).__name__}") from exc


def _git_dir(directory) -> Path | None:
    """The directory holding HEAD and the index: .git itself, or where the .git
    file of a linked worktree points. None when it cannot be told."""
    dot = Path(directory) / ".git"
    try:
        if dot.is_dir():
            return dot
        text = dot.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:"):].strip())
    return target if target.is_absolute() else Path(directory) / target


def _checkout_fingerprint(directory) -> list | None:
    """What any change to the checkout alters sooner or later: the text of HEAD
    and the size and mtime of the index. None when they cannot be read."""
    git_dir = _git_dir(directory)
    if git_dir is None:
        return None
    try:
        index = (git_dir / "index").stat()
        return [(git_dir / "HEAD").read_text(encoding="utf-8").strip(), index.st_size, index.st_mtime_ns]
    except (OSError, UnicodeError):
        return None


def _verdict_age(remembered) -> float | None:
    """Seconds since a remembered clean verdict. None when that cannot be told:
    no verdict, no timestamp, or one that does not parse. A record that cannot
    be read is no verdict against the deployment, only a reason to verify in full."""
    if not isinstance(remembered, dict) or not isinstance(remembered.get("at"), str):
        return None
    try:
        return (naive_utc(datetime.now(timezone.utc)) - parse_iso(remembered["at"])).total_seconds()
    except (ValueError, OverflowError):
        # OverflowError: a zoned timestamp whose conversion to UTC leaves the range
        return None


def verify_deployment(cfg, state=None, *, full=False):
    """Only an explicitly pinned, detached and clean deployment may be touched.

    The SHA and the detached state are checked on every call. The expensive
    "git status" runs when `full` is set, when no clean verdict is remembered,
    when HEAD or the index changed since, or when that verdict is older than
    CLEAN_REUSE_S. Returns the record to remember from a full clean verdict,
    else None. A real mismatch raises ValueError; git not answering raises
    DeploymentUndeterminable, which is not a verdict about the deployment.
    Whatever cannot be read out of the remembered verdict (its timestamp, the
    fingerprint on either side) only means verifying in full, never refusing.
    """
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
    if _git(cfg.directory, "rev-parse", "HEAD").stdout.strip() != cfg.sha:
        raise ValueError("deployment SHA mismatch")
    if _git(cfg.directory, "symbolic-ref", "-q", "HEAD").returncode != 1:
        raise ValueError("deployment must be detached at fixed SHA")
    remembered = (state or {}).get("clean")
    age = _verdict_age(remembered)
    fingerprint = _checkout_fingerprint(cfg.directory)
    # None == None would pass: a fingerprint that could not be read never matches.
    reuse = (not full and isinstance(remembered, dict) and age is not None
             and 0 <= age < CLEAN_REUSE_S and fingerprint is not None
             and remembered.get("fingerprint") == fingerprint)
    if not reuse:
        dirty = _git(cfg.directory, "status", "--porcelain", "--untracked-files=all")
        if dirty.returncode or dirty.stdout.strip():
            raise ValueError("deployment is not clean/isolated")
    if load_gateway(cfg.gateway_config).allow_live_trading is not False:
        raise ValueError("nonDemo configuration: allow_live_trading")
    load_risk_config(cfg.risk_config)
    load_session_policy(cfg.sessions_config)
    if not Path(cfg.spread_model).is_file():
        raise ValueError("spread model missing")
    components(cfg)
    if reuse:
        return None
    # Read after "git status": it may itself rewrite the index.
    return {"fingerprint": _checkout_fingerprint(cfg.directory), "at": iso_utc(datetime.now(timezone.utc))}


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


class IdentityError(ValueError):
    """A refusal that can name the offending process; the caller attaches the role.

    The message stays the bare reason (what the refusal is); who was refused is
    carried separately for alerts and the state file, see describe().
    """
    def __init__(self, reason, process=None):
        super().__init__(reason)
        self.reason = reason
        self.role = None
        self.process = process

    def detail(self):
        process = self.process
        image = ntpath.basename(process.get("executable") or "") if process else ""
        return {"role": self.role,
                "pid": process.get("pid") if process else None,
                "parent_pid": process.get("parent_pid") if process else None,
                "image": (image or "unknown") if process else None}

    def describe(self):
        text = self.reason
        if self.process is not None:
            item = self.detail()
            text += f": pid={item['pid']} image={item['image']} parent_pid={item['parent_pid']}"
        return f"role={self.role} {text}" if self.role else text


def refusal_text(exc):
    if isinstance(exc, IdentityError):
        return f"{type(exc).__name__}: {exc.describe()}"
    return f"{type(exc).__name__}: {exc}"


def _identity(process, entry, argv):
    worker = process.get("pid") != entry["pid"]
    if not _argv_matches(process.get("argv"), argv, worker=worker):
        raise IdentityError("process script/arguments mismatch", process)
    if process.get("pid") == entry["pid"]:
        if abs((parse_iso(process["started_utc"]) - parse_iso(entry["started_utc"])).total_seconds()) > 1:
            raise IdentityError("PID reused/start-time mismatch", process)
    # Command argv pins the interpreter as well as script and all deployment
    # paths. The venv worker may have a different image, but must remain a
    # Python worker in the verified descendant tree (never a free-standing PID).
    image = ntpath.basename(process.get("executable") or "").lower()
    if image not in ("python.exe", "pythonw.exe", "python", "python3"):
        raise IdentityError("unexpected executable", process)
    if worker:
        if canonical(process["argv"][0]) != canonical(process["executable"]):
            raise IdentityError("worker image/argv mismatch", process)
        if parse_iso(process["started_utc"]) < parse_iso(entry["started_utc"]):
            raise IdentityError("child predates registered parent", process)


def os_system_root():
    """OS boundary: the only place the trusted system root is read. None = unknown."""
    return os.environ.get("SystemRoot") or None


def _same_path(left, right):
    """Pure string comparison after case and separator normalization."""
    def spell(path):
        path = str(path)
        if path.startswith("\\\\?\\"):  # extended-length spelling of the same path
            path = path[4:]
        return ntpath.normcase(ntpath.normpath(path))
    return bool(left) and bool(right) and spell(left) == spell(right)


def _console_leaf(process, table, system_root):
    """The system's own console host with nothing started under it.

    Trusted only by full image path under the caller-supplied system root (an
    unknown root exempts nothing), and only while no process created at or after
    it records it as parent; an older process citing the PID is a stale link.
    """
    if not system_root or not _same_path(
            process.get("executable") or "", ntpath.join(str(system_root), "System32", "conhost.exe")):
        return False
    return not any(item.get("parent_pid") == process["pid"] and item["pid"] != process["pid"]
                   and parse_iso(item["started_utc"]) >= parse_iso(process["started_utc"])
                   for item in table.values())


def _require_launcher_gone(entry, table, argv, system_root):
    """The launcher is missing: restart only when nothing of ours survives."""
    registered = parse_iso(entry["started_utc"])
    occupant = table.get(entry["pid"])
    reused_at = parse_iso(occupant["started_utc"]) if occupant is not None else None
    for process in table.values():
        if _argv_matches(process.get("argv"), argv, worker=True):
            raise IdentityError("missing launcher with surviving/duplicate worker", process)
        if process.get("parent_pid") != entry["pid"]:
            continue
        started = parse_iso(process["started_utc"])
        if (started - registered).total_seconds() < -1:
            continue  # registry creation-time tolerance; older means a reused parent PID
        if reused_at is not None and started >= reused_at:
            continue  # belongs to the process that owns the PID now
        if _console_leaf(process, table, system_root):
            continue  # the launcher's console host outliving it owns nothing
        raise IdentityError("missing launcher with surviving/duplicate worker", process)


def _decide_tree(entry, table, argv, system_root):
    """Decide over a process table with the system root as an argument.

    The console-host decision reads no environment and no filesystem; the
    worker identity checks keep their own path normalization (`canonical()`),
    as they always have. Unknown descendants are refused. An exempted process
    is never verified, so it never enters the result and can never be
    terminated. Children come before their parent in the result.
    """
    root = table.get(entry["pid"])
    if root is not None and not _argv_matches(root.get("argv"), argv):
        # Neither the launcher's arguments nor its registered creation time:
        # the launcher is gone and an unrelated process owns the PID.
        if abs((parse_iso(root["started_utc"]) - parse_iso(entry["started_utc"])).total_seconds()) > 1:
            root = None
    if root is None:
        _require_launcher_gone(entry, table, argv, system_root)
        return []
    _identity(root, entry, argv)
    observed_entry = {**entry, "started_utc": root["started_utc"]}
    result, seen = [], set()
    def visit(item):
        if item["pid"] in seen:
            raise ValueError("cyclic process tree")
        seen.add(item["pid"])
        if _console_leaf(item, table, system_root):
            return
        _identity(item, observed_entry, argv)
        for child in table.values():
            if child.get("parent_pid") == item["pid"]:
                if parse_iso(child["started_utc"]) < parse_iso(item["started_utc"]):
                    continue  # recorded parent PID was reused; not this tree
                visit(child)
        result.append(item)  # child first, launcher last
    visit(root)
    for process in table.values():
        if process["pid"] not in seen and _argv_matches(process.get("argv"), argv, worker=True):
            raise IdentityError("unregistered duplicate worker", process)
    return result


_UNSET = object()


def verified_tree(entry, table, argv, system_root=_UNSET):
    """Entry point for callers that own the OS boundary; pass an explicit
    system_root (None = unknown) to keep the call free of environment reads."""
    return _decide_tree(entry, table, argv,
                        os_system_root() if system_root is _UNSET else system_root)


def _next_journal_line(path):
    """One-based physical line number the next append_jsonl row will occupy.

    The formal-window generator binds retained registry copies to ops-restarts
    rows by physical line number; seq must be counted the same way (non-empty
    lines, append-only one row per restart).
    """
    try:
        with open(path, "rb") as fh:
            return sum(1 for line in fh if line.strip()) + 1
    except FileNotFoundError:
        return 1


def _retain_registry_copy(src, dst):
    """Raw-byte copy of a registry file for formal-window evidence (T180).

    Returns the sha256 of the copied bytes so the runtime index records the
    digest observed at restart time. The caller decides how failures surface;
    a retention failure must never block the restart itself.
    """
    data = Path(src).read_bytes()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as fh:
        fh.write(data)
    return hashlib.sha256(data).hexdigest()


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
        try:
            trees[key] = verified_tree(entry, table, command(cfg, roles[key]))
        except IdentityError as exc:
            exc.role = key  # the refusal names role, PID, image and parent
            raise
    for key, role in roles.items():
        if key not in mapped:
            if any(_argv_matches(p.get("argv"), command(cfg, role), worker=True) for p in table.values()):
                raise ValueError("unregistered live worker")
            trees[key] = []
    return roles, mapped, trees


def terminate_verified_tree(entry, processes, argv, role=None):
    """End a verified tree, children first.

    The tree is looked at again from a fresh snapshot, so it can differ from the
    one that passed the registry check; a refusal raised here carries the role.
    The whole tree is verified before the first member is ended, and each member
    once more through its handle right before it is terminated: nothing that did
    not pass is ever terminated. A member that has left by itself since counts as
    ended; the last look at the tree is what says whether anything survived.
    """
    try:
        tree = verified_tree(entry, processes.snapshot(), argv)
        # The Windows backend sends TerminateProcess to the whole tree before it waits
        # for any of it (WindowsProcesses.terminate_without_waiting); stand-ins end each
        # process as it comes.
        with getattr(processes, "terminate_without_waiting", nullcontext)():
            for process in tree:
                processes.terminate(process)
        if verified_tree(entry, processes.snapshot(), argv):
            raise ValueError("worker tree did not terminate")
    except IdentityError as exc:
        exc.role = role or exc.role
        raise


def inspect_deployment(cfg, processes, *, verify_deployment=verify_deployment):
    """Read-only; demo_run holds instances.lock through this check and launch."""
    try:
        verify_deployment(cfg)
        _, mapped, trees = _registered_trees(cfg, processes, allow_missing=True)
        return {"refused": False, "alive": {key: mapped[key]["pid"]
                for key, tree in trees.items() if tree}}
    except Exception as exc:
        return {"refused": True, "reason": refusal_text(exc)}


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
                    terminate_verified_tree(mapped[key], processes, command(cfg, roles[key]), role=key)
                    result["stopped"].append(key)
    except Exception as exc:
        result.update(refused=True, reason=refusal_text(exc))
    return result


def supervise(cfg, processes, *, now=None, clock=None, verify_deployment=verify_deployment):
    observe = clock or ((lambda: now) if now is not None else lambda: datetime.now(timezone.utc))
    now = observe()
    live = Path(cfg.data_dir) / "live"
    live.mkdir(parents=True, exist_ok=True)
    result = {"restarted": [], "refused": False, "cleared": False, "evidence_failures": [],
              "deferred": []}
    state_path = live / "supervisor-state.json"
    # Test doubles take only cfg; the real verifier also takes the remembered state.
    real_verifier = verify_deployment is globals()["verify_deployment"]

    def check(state, *, full=False):
        try:
            if real_verifier:
                return globals()["verify_deployment"](cfg, state, full=full)
            return verify_deployment(cfg)
        except subprocess.TimeoutExpired as exc:
            # A git call that ran out of time behind the verifier: not a verdict.
            raise DeploymentUndeterminable(f"git call: {type(exc).__name__}") from exc
    try:
        with registry_lock(str(live / "instances.lock")), file_lock(str(live / "supervisor.lock"), attempts=1):
            state = read_json_retry(str(state_path)) or {}
            # Rounds in a row in which git could not answer. A round counts once
            # wherever in it that happens, so the full check before a restart
            # counts like the first check. A reused verdict alone neither counts
            # nor clears: only a round in which git answered every time clears it.
            carried = int(state.get("undeterminable") or 0)   # the rounds before this one
            undeterminable = 0          # 1 once git could not answer in this round

            def could_not_answer(exc):
                # One count per round, and the same limit for both of its checks.
                nonlocal undeterminable
                undeterminable = 1
                result["undeterminable"] = carried + 1
                result["deferred_reason"] = f"git could not answer: {exc}"
                if carried + 1 >= UNDETERMINABLE_LIMIT:
                    raise ValueError(f"deployment undeterminable {carried + 1} times in a row "
                                     f"({exc})") from exc

            try:
                verified = check(state)
            except DeploymentUndeterminable as exc:
                # Not a verdict about the deployment: nothing is restarted this round.
                could_not_answer(exc)
            else:
                if verified:
                    state["clean"] = verified
            roles, mapped, trees = _registered_trees(cfg, processes)
            stale, checks = {}, {}
            clean = False
            for key, role in roles.items():
                entry = mapped.get(key)
                tree = trees[key]
                if not tree:
                    stale[key] = "process_missing"
                    continue
                hb_path = live / f"heartbeat-{key}.json"
                seen = []
                try:
                    data = read_json_retry(str(hb_path))
                    hb = read_heartbeat_path(str(hb_path))
                    log_mtime = Path(entry["log"]).stat().st_mtime
                    observed = observe()  # precheck/scan/reads may span new heartbeats
                    status = heartbeat_status(hb, observed, timeout_s=health_timeout(role["interval"]))
                    seen.append(f"heartbeat={status.reason}")
                    in_tree = data.get("pid") in {p["pid"] for p in tree}
                    seen.append(f"pid_in_tree={in_tree}")
                    sha_ok = data.get("deployment_sha") == cfg.sha
                    seen.append(f"deployment_sha={'ok' if sha_ok else 'mismatch'}")
                    not_before = parse_iso(entry["started_utc"]) <= parse_iso(data["ts"])
                    seen.append(f"heartbeat_not_before_registry={not_before}")
                    age = observed.timestamp() - log_mtime
                    seen.append(f"log_age_s={age:.1f}")
                    # A negative log age is recorded as it is; see LOG_AGE_TOLERANCE_S.
                    healthy = (status.ok and in_tree and sha_ok and not_before
                               and -LOG_AGE_TOLERANCE_S <= age <= health_timeout(role["interval"]))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    healthy = False
                    seen.append(f"read_error={type(exc).__name__}")
                if not healthy:
                    stale[key] = "heartbeat_or_log_stale"
                    checks[key] = " ".join(seen)
                if key == "reconcile" and healthy:
                    clean = data.get("reconcile_clean") is True
            now = observe()
            signature = sorted((k, e["pid"], e["started_utc"]) for k, e in mapped.items())
            signature = json.loads(json.dumps(signature))
            if stale:
                write_halt_once(str(live), "risk_lost" if "risk" in stale else "component_lost", now,
                                owner="supervisor", token=uuid.uuid4().hex)
                append_alert(str(live), "SUPERVISOR",
                             f"unhealthy={stale}" + (f" checks={checks}" if checks else ""), now)
                # Persist reset BEFORE any process mutation/crash. The count is
                # written as known so far: a reused verdict has not cleared it.
                reset = {"healthy_since": None, "signature": signature,
                         "undeterminable": carried + undeterminable, "clean": state.get("clean")}
                if checks:
                    reset["stale_checks"] = checks   # same sub-conditions as the alert, for forensics
                write_json_atomic(str(state_path), reset)
                if undeterminable:
                    # The halt just written is the protection; it stays until a round
                    # can verify the deployment again.
                    result["deferred"] = sorted(stale)
                    append_alert(str(live), "SUPERVISOR_DEFERRED",
                                 f"{result['deferred_reason']}; restarts deferred for {result['deferred']}; "
                                 f"consecutive={result['undeterminable']}", now)
                    return result
                # Only a clean, pinned deployment may be restarted. A round that restarts
                # anything verifies it in full first, never from the cache.
                try:
                    verified = check(None, full=True)
                except DeploymentUndeterminable as exc:
                    could_not_answer(exc)     # at the limit this is a refusal, as above
                    result["deferred"] = sorted(stale)
                    write_json_atomic(str(state_path), {**reset, "undeterminable": carried + 1})
                    append_alert(str(live), "SUPERVISOR_DEFERRED",
                                 f"{result['deferred_reason']}; restarts deferred for {result['deferred']}; "
                                 f"consecutive={result['undeterminable']}", now)
                    return result
                if verified:
                    state["clean"] = verified
                # Git answered every check of this round: the count starts over, and
                # the verdict just verified in full is the one to remember.
                reset.update(undeterminable=0, clean=state.get("clean"))
                write_json_atomic(str(state_path), reset)
                for key, reason in stale.items():
                    entry = mapped.get(key)
                    # T180: retain pre-restart registry bytes inside the same lock.
                    # The journal row itself keeps the checker's exact 6-field
                    # schema; the copy paths/digests go to the runtime evidence
                    # index, which is what the descriptor generator consumes.
                    seq = _next_journal_line(live / "ops-restarts.jsonl")
                    stamp = now.strftime("%Y%m%d-%H%M%S")
                    before_rel = f"evidence/instances/{stamp}-{seq}-before.json"
                    after_rel = f"evidence/instances/{stamp}-{seq}-after.json"
                    before_hash = None
                    try:
                        before_hash = _retain_registry_copy(live / "instances.json", live / before_rel)
                    except Exception as exc:
                        append_alert(str(live), "SUPERVISOR_EVIDENCE",
                                     f"seq={seq} component={key} before copy failed: {type(exc).__name__}", now)
                        result["evidence_failures"].append(key)
                    if entry:
                        terminate_verified_tree(entry, processes, command(cfg, roles[key]), role=key)
                    log_stamp = stamp + "-" + uuid.uuid4().hex[:8]
                    kind = roles[key]["kind"]
                    log = str(Path(cfg.log_dir) / f"{kind}-{key}-{log_stamp}.log")
                    argv = command(cfg, roles[key])
                    proc = processes.launch(argv, cfg.directory, log,
                                            {**os.environ, "MT5_DEPLOYMENT_SHA": cfg.sha,
                                             "MT5_LIVE_DIR": str(live), "PYTHONUNBUFFERED": "1"})
                    new = dict(pid=proc["pid"], started_utc=proc["started_utc"], kind=kind,
                               magic=roles[key].get("magic"), mode="Demo", argv=argv, log=log,
                               deployment_sha=cfg.sha, deployment_dir=cfg.directory, data_dir=cfg.data_dir)
                    mapped[key] = new
                    write_json_atomic(str(live / "instances.json"), {"schema_version": 1, "instances": list(mapped.values())})
                    after_hash = None
                    try:
                        after_hash = _retain_registry_copy(live / "instances.json", live / after_rel)
                    except Exception as exc:
                        append_alert(str(live), "SUPERVISOR_EVIDENCE",
                                     f"seq={seq} component={key} after copy failed: {type(exc).__name__}", now)
                        result["evidence_failures"].append(key)
                    append_jsonl(str(live / "ops-restarts.jsonl"), {
                        "utc": iso_utc(now), "component": key, "reason_code": reason,
                        "old_pid": entry["pid"] if entry else None, "new_pid": proc["pid"],
                        "deployment_sha": cfg.sha})
                    if before_hash and after_hash:
                        try:
                            append_jsonl(str(live / "evidence/instances/index.jsonl"), {
                                "seq": seq, "utc": iso_utc(now), "component": key,
                                "before_instances": {"path": before_rel, "sha256": before_hash},
                                "after_instances": {"path": after_rel, "sha256": after_hash}})
                        except Exception as exc:
                            append_alert(str(live), "SUPERVISOR_EVIDENCE",
                                         f"seq={seq} component={key} index append failed: {type(exc).__name__}", now)
                            result["evidence_failures"].append(key)
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
            if undeterminable:
                # Healthy roles, unverifiable deployment: no halt is written, and an
                # existing one is not lifted until the deployment can be verified again.
                append_alert(str(live), "SUPERVISOR_DEFERRED",
                             f"{result['deferred_reason']}; all roles healthy, no halt written; "
                             f"consecutive={result['undeterminable']}", now)
            elif since and (naive_utc(now) - parse_iso(since)).total_seconds() >= 300 and token:
                result["cleared"] = _clear_owned(live, token)
            # Nothing is restarted below, so no other check can fail: unless git
            # could not answer the first check, this round was answerable throughout.
            state = {"healthy_since": since, "last_checked": iso_utc(now),
                     "signature": signature, "halt_token": token,
                     "undeterminable": carried + undeterminable if undeterminable else 0,
                     "clean": state.get("clean")}
            if token and not result["cleared"]:
                # _clear_owned refuses silently; name the blocker so a halt that
                # cannot clear by itself is visible without reading the directory.
                blocker = next((name for name in ("halt-risk-veto.json", "killSwitch")
                                if (live / name).exists()), None)
                if blocker:
                    state["clear_blocked"] = result["clear_blocked"] = blocker
            write_json_atomic(str(state_path), state)
    except Exception as exc:
        detail = exc.detail() if isinstance(exc, IdentityError) else None
        result["refused"] = True
        result["reason"] = refusal_text(exc)
        write_halt_once(str(live), "supervisor_refused", now, owner="supervisor")
        append_alert(str(live), "SUPERVISOR_REFUSED", result["reason"], now)
        if detail and (detail.get("image") or "").lower() == "terminal64.exe":
            # A terminal under a worker tree means some worker launched it.
            append_alert(str(live), "SUPERVISOR_TERMINAL_CHILD",
                         f"terminal64.exe pid={detail['pid']} parent_pid={detail['parent_pid']} "
                         f"runs under the {detail['role']} worker tree", now)
        write_json_atomic(str(state_path), {"healthy_since": None, "last_refusal": {
            "utc": iso_utc(now), "reason": result["reason"], **(detail or {})}})
    return result


class WindowsProcesses:
    """No shell-interpolated input; terminate by a creation-time-bound OS handle."""

    def __init__(self):
        # (kernel32, handle) of the TerminateProcess calls that nobody has waited for yet
        self._issued = None

    @contextmanager
    def terminate_without_waiting(self):
        """Inside the block terminate() sends TerminateProcess and returns at once; the
        handles are waited for when the block ends.

        A tree is ended this way so that each member is still the process that was
        verified when its turn comes. A launcher whose worker has died starts leaving
        by itself within a fraction of a millisecond; waiting for the worker first
        meant the launcher was already on its way out when TerminateProcess reached it.
        """
        previous, self._issued = self._issued, []
        issued = self._issued
        try:
            yield
        except BaseException:
            self._issued = previous
            for kernel, handle in issued:
                try:
                    self._wait_ended(kernel, handle)
                except RuntimeError:
                    pass  # the refusal on its way up is what the caller must see
            raise
        self._issued = previous
        failure = None
        for kernel, handle in issued:
            try:
                self._wait_ended(kernel, handle)
            except RuntimeError as error:
                # A member that has not ended is a refusal, but the members after it are
                # still waited for and their handles closed (pre-freeze review S1): the first
                # failing wait used to raise at once and leave the rest with handles open.
                failure = failure or error
        if failure is not None:
            raise failure

    def snapshot(self):
        source = ("Get-CimInstance Win32_Process | ForEach-Object { "
                  "[pscustomobject]@{pid=$_.ProcessId;parent_pid=$_.ParentProcessId;"
                  "started_utc=$_.CreationDate.ToUniversalTime().ToString('o');"
                  "executable=$_.ExecutablePath;command_line=$_.CommandLine} } | ConvertTo-Json -Depth 3 -Compress")
        out = subprocess.run([powershell_exe(), "-NoProfile", "-Command", source],
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
        """End `process` and wait until it has exited; inside terminate_without_waiting()
        only the first half.

        A process that has already left by itself counts as ended: it has exited, or
        its process object is gone. It is still recognised first, by its creation time
        read through the handle: a pid that now belongs to another process, or a process
        that cannot be opened at all, is refused and nothing is terminated.
        """
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
            # pid 0 gets the same answer although the System Idle Process is always there
            if process["pid"] > 0 and ctypes.get_last_error() == _PID_GONE:
                return
            raise RuntimeError("cannot acquire process handle")
        try:
            created, exited, kern, user = (w.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kern), ctypes.byref(user)):
                raise RuntimeError("cannot verify process creation")
            seconds = ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 10000000 - 11644473600
            expected = parse_iso(process["started_utc"]).replace(tzinfo=timezone.utc).timestamp()
            if abs(seconds - expected) > 0.01:
                raise RuntimeError("PID reused before terminate")
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        # FALSE means the process is already on its way out. This is the process verified
        # a moment ago and the handle carries PROCESS_TERMINATE; the wait decides whether
        # it ended.
        kernel.TerminateProcess(handle, 75)
        if self._issued is None:
            self._wait_ended(kernel, handle)
        else:
            self._issued.append((kernel, handle))

    @staticmethod
    def _wait_ended(kernel, handle):
        try:
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
