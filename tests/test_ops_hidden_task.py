"""scripts/ops/hidden_task.vbs and scripts/ops/supervisor_once.ps1.

Why they exist: a scheduled task whose action is a console program puts a console window
on the interactive desktop every time it runs. The launcher is wscript's part -- no console,
the child started hidden and waited for, the exit code handed back. The wrapper runs the
supervisor of a pinned deployment once and puts what it writes into a log.

The launcher is run here with cscript //B //Nologo (the other Windows Script Host, same
engine as wscript) against a stand-in script, and the wrapper with powershell -File against
a stand-in "supervisor", all in temp directories. No scheduled task is registered or run
and no terminal is touched. What cannot be checked this way -- that nothing shows up on
the interactive desktop -- is left to whoever installs the task.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "ops" / "hidden_task.vbs"
ONCE = ROOT / "scripts" / "ops" / "supervisor_once.ps1"
NO_WINDOW = 0x08000000            # CREATE_NO_WINDOW: the test itself must not flash a console either
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows Script Host and PowerShell")

POWERSHELL_STANDIN = """\
$lines = @($args | ForEach-Object { '<' + $_ + '>' })
[System.IO.File]::WriteAllLines($env:STANDIN_OUT, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
exit ([int]$env:STANDIN_EXIT)
"""

# The stand-in "supervisor": records what it was given, writes one Chinese line to each
# stream, and exits with FAKE_EXIT. With FAKE_CHILD it first starts a child that stays up
# for three minutes -- what the real supervisor does when it restarts a role, only shorter.
FAKE_SUPERVISOR = """\
import json, os, subprocess, sys
with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as handle:
    json.dump({"argv": sys.argv, "cwd": os.getcwd()}, handle)
if os.environ.get("FAKE_CHILD"):
    child = subprocess.Popen([sys._base_executable, "-I", "-c", "import time; time.sleep(180)"])
    with open(os.environ["FAKE_CHILD"], "w") as handle:
        handle.write(str(child.pid))
print("stdout 一行中文")
print("stderr 一行中文", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_EXIT", "75")))
"""
STDOUT_LINE = "stdout 一行中文"
STDERR_LINE = "stderr 一行中文"
STAMP = r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]"


# ------------------------------------------------------------------ the launcher

def powershell_standin(tmp_path):
    """A .ps1 that writes its arguments to STANDIN_OUT and exits with STANDIN_EXIT. Its own
    path has spaces and characters that need quoting."""
    folder = tmp_path / "stand in (x) & y"
    folder.mkdir()
    script = folder / "stand-in.ps1"
    script.write_text(POWERSHELL_STANDIN, encoding="utf-8-sig")
    return script, tmp_path / "arguments.txt"


def launch(*args, env=None):
    done = subprocess.run(["cscript.exe", "//B", "//Nologo", str(LAUNCHER), *(str(a) for a in args)],
                          capture_output=True, timeout=120, env=env, creationflags=NO_WINDOW)
    return done.returncode, (done.stdout + done.stderr).decode("mbcs", "replace")


@windows_only
@pytest.mark.parametrize("code", [0, 75])
def test_launcher_hands_the_exit_code_of_the_script_back(tmp_path, code):
    script, out = powershell_standin(tmp_path)
    rc, noise = launch(script, env=dict(os.environ, STANDIN_OUT=str(out), STANDIN_EXIT=str(code)))
    assert rc == code and out.exists()               # the script ran, and its code came back
    assert noise == ""                               # nothing is printed: under wscript that would be a box


@windows_only
def test_launcher_hands_arguments_over_as_they_are(tmp_path):
    script, out = powershell_standin(tmp_path)
    spaced = tmp_path / "a b" / "x & y (z)"
    spaced.mkdir(parents=True)
    chinese = tmp_path / "中文 目录"
    chinese.mkdir()
    arguments = ["-DataDir", str(spaced), "--sha", "a" * 40, "-Note", str(chinese),
                 "it's", "a;b", "a,b", ""]
    rc, _ = launch(script, *arguments, env=dict(os.environ, STANDIN_OUT=str(out), STANDIN_EXIT="0"))
    assert rc == 0
    assert out.read_text(encoding="utf-8").splitlines() == [f"<{a}>" for a in arguments]


@windows_only
def test_launcher_refuses_what_it_cannot_pass_and_starts_nothing(tmp_path):
    script, out = powershell_standin(tmp_path)

    def refused(*args):
        out.unlink(missing_ok=True)
        rc, noise = launch(*args, env=dict(os.environ, STANDIN_OUT=str(out), STANDIN_EXIT="0"))
        assert not out.exists() and noise == "", args       # the script was not started
        return rc

    assert refused() == 2                                       # no arguments at all
    assert refused(tmp_path / "no such script.ps1") == 3
    assert refused(script, "%TEMP%") == 4                      # WScript.Shell.Run expands %NAME%
    assert refused(script, "100%") == 4
    assert refused(script, "D:\\dir with space\\") == 5        # the closing quote would be escaped
    assert refused(script, "-DataDir", "D:\\") == 5


@windows_only
def test_a_quote_on_the_command_line_never_reaches_the_script_so_the_launcher_cannot_refuse_it(tmp_path):
    """Pre-freeze review S6, as measured: WSH takes a quote on the command line for a delimiter and
    hands the script the argument without it -- a"b arrives as ab, a quoted path with a quote inside it
    arrives shortened. The launcher therefore cannot refuse a double quote: it never sees one. Such a
    path has to be refused where it is still visible, by the installer
    (tests/test_install_terminal_keeper.py)."""
    folder = tmp_path / "probe"
    folder.mkdir()
    show = folder / "show.vbs"
    show.write_bytes(b'For i = 0 To WScript.Arguments.Count - 1\r\n  WScript.Echo "[" & WScript.Arguments(i) & "]"\r\nNext\r\n')
    for tail in ('a"b', '"C:\\x"y\\z.ps1"', '-Name "a"b"'):
        done = subprocess.run(f'cscript.exe //Nologo "{show}" {tail}', capture_output=True, timeout=60,
                              creationflags=NO_WINDOW)
        received = done.stdout.decode("mbcs", "replace").split()
        assert received and all('"' not in argument for argument in received), (tail, received)


def test_launcher_is_plain_ascii_with_crlf_lines_and_no_bom():
    """wscript reads the file in the system ANSI code page and would take a BOM for code.
    The line endings are pinned in .gitattributes, so the file is the same on every machine."""
    raw = LAUNCHER.read_bytes()
    assert raw.isascii() and not raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\n") == raw.count(b"\r\n") > 10


@windows_only
def test_a_failing_statement_of_the_launcher_is_exit_code_6_not_0(tmp_path):
    """A run-time error leaves the host's exit code at 0: under the task that reads as a
    successful run. The launcher has to notice its own failures. Checked on a copy in which
    WScript.Shell cannot be created; the launcher in the tree is not modified."""
    source = LAUNCHER.read_text(encoding="ascii")
    broken = source.replace('CreateObject("WScript.Shell")', 'CreateObject("WScript.NoSuchHost")')
    assert broken != source
    copy = tmp_path / "hidden_task.vbs"
    copy.write_bytes(broken.replace("\n", "\r\n").encode("ascii"))
    script, out = powershell_standin(tmp_path)
    done = subprocess.run(["cscript.exe", "//B", "//Nologo", str(copy), str(script)],
                          capture_output=True, timeout=60, creationflags=NO_WINDOW,
                          env=dict(os.environ, STANDIN_OUT=str(out), STANDIN_EXIT="0"))
    assert done.returncode == 6 and not out.exists()


# ------------------------------------------------------------------ the wrapper

# The wrapper's parameters, in the order live_supervisor.py declares its options, and the
# option each one becomes.
OPTIONS = [("Directory", "directory"), ("Sha", "sha"), ("DataDir", "data-dir"), ("ConfigDir", "config-dir"),
           ("Python", "python"), ("GatewayConfig", "gateway-config"), ("RiskConfig", "risk-config"),
           ("SessionsConfig", "sessions-config"), ("SpreadModel", "spread-model"), ("LogDir", "log-dir")]


class FakeDeployment:
    """What the wrapper needs to run: a directory with scripts/live_supervisor.py (the
    stand-in), and the ten options -- several of them paths with spaces and characters
    that have to survive the quoting."""

    def __init__(self, tmp_path):
        self.directory = tmp_path / "deploy with spaces"
        (self.directory / "scripts").mkdir(parents=True)
        (self.directory / "scripts" / "live_supervisor.py").write_text(FAKE_SUPERVISOR, encoding="utf-8")
        self.data = tmp_path / "data dir"
        self.log = self.data / "ops-evidence" / "supervisor" / "supervisor.log"
        self.record = tmp_path / "what-the-stand-in-got.json"

    def options(self):
        return {"Directory": str(self.directory), "Sha": "a" * 40, "DataDir": str(self.data),
                "ConfigDir": "C:\\configs", "Python": sys.executable,
                "GatewayConfig": "C:\\gateway & co (demo)\\gateway.json",
                "RiskConfig": "C:\\configs\\risk-demo.toml", "SessionsConfig": "C:\\configs\\sessions.toml",
                "SpreadModel": "C:\\data dir\\costs\\spread_model.json", "LogDir": "C:\\data dir\\demo"}


def wrapper_environment(tmp_path, extra=None):
    """PYTHONIOENCODING=utf-8: the stand-in writes UTF-8 whatever the machine's code page is,
    so "the bytes arrive as they were written" can be checked anywhere."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", FAKE_RECORD=str(tmp_path / "what-the-stand-in-got.json"))
    for name in ("FAKE_CHILD", "FAKE_EXIT"):
        env.pop(name, None)
    env.update(extra or {})
    return env


def flattened(options):
    return [part for name, value in options.items() for part in (f"-{name}", str(value))]


def run_wrapper(tmp_path, options, extra_env=None, timeout=120):
    """The wrapper the way the launcher runs it (minus the hiding)."""
    done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ONCE),
                           *flattened(options)],
                          capture_output=True, timeout=timeout, env=wrapper_environment(tmp_path, extra_env),
                          creationflags=NO_WINDOW)
    return done.returncode, (done.stdout + done.stderr).decode("mbcs", "replace")


def log_lines(fake):
    return fake.log.read_bytes().decode("utf-8").splitlines()


@windows_only
def test_wrapper_passes_the_ten_options_through_and_runs_from_the_deployment_directory(tmp_path):
    """The task's working directory is not inherited through wscript: the wrapper has to go
    to the deployment directory itself."""
    fake = FakeDeployment(tmp_path)
    rc, noise = run_wrapper(tmp_path, fake.options())
    assert rc == 75, noise
    got = json.loads(fake.record.read_text(encoding="utf-8"))
    options = fake.options()
    expected = [str(fake.directory / "scripts" / "live_supervisor.py")]
    for name, option in OPTIONS:
        expected += ["--" + option, options[name]]
    assert got["argv"] == expected
    assert got["cwd"] == str(fake.directory)


@windows_only
@pytest.mark.parametrize("code", [0, 75])
def test_wrapper_puts_stdout_and_stderr_in_the_log_and_keeps_the_exit_code(tmp_path, code):
    """A crash traceback goes to stderr. Piping the interpreter's output through PowerShell
    (2>&1) turns its first stderr line into an error of the wrapper's under
    ErrorActionPreference = Stop, and the exit code is lost with it. The wrapper appends
    the bytes the interpreter wrote instead: a Chinese line is not re-encoded into "?"."""
    fake = FakeDeployment(tmp_path)
    rc, noise = run_wrapper(tmp_path, fake.options(), {"FAKE_EXIT": str(code)})
    assert rc == code, noise
    raw = fake.log.read_bytes()
    lines = raw.decode("utf-8").splitlines()              # what the stand-in wrote, byte for byte
    assert lines.index(STDOUT_LINE) < lines.index(STDERR_LINE)       # stdout first, then stderr
    assert re.fullmatch(STAMP + r" start", lines[0])
    assert re.fullmatch(STAMP + rf" exit {code} \(\d+[.,]\d s\)", lines[-1])
    assert not raw.startswith(b"\xef\xbb\xbf")           # no BOM in an appended log
    assert [p.name for p in fake.log.parent.iterdir()] == ["supervisor.log"]    # capture files removed


@windows_only
def test_wrapper_does_not_wait_for_a_process_the_supervisor_leaves_behind(tmp_path):
    """The supervisor restarts roles, which then stay up for days. Start-Process -Wait waits
    for the whole process tree: with it the wrapper -- and with it the task's instance --
    would never end, and "do not start a new instance" would keep every later minute from
    running. The child here also keeps the capture files open: the log still gets both lines."""
    fake = FakeDeployment(tmp_path)
    child_file = tmp_path / "child.pid"
    try:
        rc, noise = run_wrapper(tmp_path, fake.options(), {"FAKE_CHILD": str(child_file)}, timeout=90)
        assert child_file.exists()                       # the stand-in did leave one behind
        assert rc == 75, noise
        lines = log_lines(fake)
        assert STDOUT_LINE in lines and STDERR_LINE in lines
    finally:
        if child_file.exists():
            os.kill(int(child_file.read_text()), signal.SIGTERM)


@windows_only
def test_wrapper_appends_to_the_log_and_rotates_it_once_it_is_over_five_megabytes(tmp_path):
    fake = FakeDeployment(tmp_path)
    fake.log.parent.mkdir(parents=True)
    fake.log.write_bytes(b"earlier run\n")
    rc, noise = run_wrapper(tmp_path, fake.options())
    assert rc == 75, noise
    assert fake.log.read_bytes().startswith(b"earlier run\n")          # a small log is appended to

    five_megabytes = 5 * 1024 * 1024
    with open(fake.log, "ab") as handle:
        handle.write(b"x" * (five_megabytes + 1))
    older = Path(str(fake.log) + ".1")
    older.write_bytes(b"the generation before that")
    rc, noise = run_wrapper(tmp_path, fake.options())
    assert rc == 75, noise
    assert older.read_bytes().startswith(b"earlier run\n") and older.stat().st_size > five_megabytes
    assert b"earlier run" not in fake.log.read_bytes()                  # a new log: this run only
    assert STDOUT_LINE.encode("utf-8") in fake.log.read_bytes()


@windows_only
@pytest.mark.parametrize("change, reason", [
    ({"Sha": None}, "-sha was not given"),
    ({"ConfigDir": "C:\\configs\\"}, "-config-dir ends with a backslash"),
    ({"GatewayConfig": 'C:\\a"b.json'}, "-gateway-config contains a double quote"),
    ({"Directory": "<nowhere>"}, "deployment directory not found"),
    ({"Python": "<no interpreter here>"}, "interpreter not found"),
    # pre-freeze review S5: live_supervisor.py refuses relative paths (verify_deployment)
    ({"ConfigDir": "configs"}, "-config-dir is not an absolute path"),
    ({"SpreadModel": "spread_model.json"}, "-spread-model is not an absolute path"),
    ({"LogDir": "logs"}, "-log-dir is not an absolute path"),
])
def test_wrapper_refuses_what_it_cannot_run_and_says_why_in_the_log(tmp_path, change, reason):
    """Under the launcher nobody sees the wrapper's error stream, so the reason goes to the
    log. A missing parameter must not be asked for (a prompt under a hidden window would
    keep the task hanging until its time limit): it is refused."""
    fake = FakeDeployment(tmp_path)
    options = fake.options()
    for name, value in change.items():
        options[name] = value if value is None else (
            str(tmp_path / value[1:-1]) if value.startswith("<") else value)
    options = {name: value for name, value in options.items() if value is not None}
    rc, noise = run_wrapper(tmp_path, options, timeout=60)
    assert rc == 3, noise
    assert reason in fake.log.read_bytes().decode("ascii")
    assert not fake.record.exists()                      # nothing was started


@windows_only
def test_wrapper_refuses_a_relative_data_dir_and_logs_nothing_anywhere(tmp_path):
    """Pre-freeze review S5: a relative -DataDir does not say where the log is. It used to be
    taken: the reason was logged under the directory that happened to be current, and after
    Set-Location the supervisor's output would have gone under the deployment directory."""
    fake = FakeDeployment(tmp_path)
    done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ONCE),
                           *flattened({**fake.options(), "DataDir": "relative-data"})],
                          capture_output=True, timeout=60, cwd=tmp_path, creationflags=NO_WINDOW,
                          env=wrapper_environment(tmp_path))
    noise = (done.stdout + done.stderr).decode("mbcs", "replace")
    assert done.returncode == 3 and "-data-dir is not an absolute path" in noise, noise
    assert not (tmp_path / "relative-data").exists()      # no log under the current directory
    assert not fake.log.exists() and not fake.record.exists()


@windows_only
def test_through_the_launcher_the_exit_code_and_both_output_lines_survive(tmp_path):
    """The whole chain of the task's action: launcher, wrapper, interpreter."""
    fake = FakeDeployment(tmp_path)
    done = subprocess.run(["cscript.exe", "//B", "//Nologo", str(LAUNCHER), str(ONCE), *flattened(fake.options())],
                          capture_output=True, timeout=120, env=wrapper_environment(tmp_path),
                          creationflags=NO_WINDOW)
    assert done.returncode == 75, (done.stdout + done.stderr).decode("mbcs", "replace")
    lines = log_lines(fake)
    assert STDOUT_LINE in lines and STDERR_LINE in lines
    got = json.loads(fake.record.read_text(encoding="utf-8"))
    assert got["argv"][1:3] == ["--directory", str(fake.directory)] and got["cwd"] == str(fake.directory)
