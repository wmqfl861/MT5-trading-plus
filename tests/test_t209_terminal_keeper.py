"""T209 终端看护脚本：终端进程不在就启动它，别的什么都不做。

多数用例在 PowerShell 驱动脚本里把 Get-CimInstance 和 Start-Process 换成记录
调用的函数：不启动任何进程，也不查真实的进程表。最后几条用例看真实情况：
一条只做 -DryRun（脚本结构上不可能启动）；两条用 python.exe 的副本冒充
terminal64.exe，真的启动一次（其中一条经目录联接启动）、再认出它在运行，
结束时只结束这个副本——先核对映像路径，再动它；一条让第一个看护停在"正在
启动"里，看同一个终端的第二个看护会不会退让。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEEPER = ROOT / "scripts" / "ops" / "terminal_keeper.ps1"
DEFAULT_INSTALL = Path(r"C:\Program Files\MetaTrader 5\terminal64.exe")     # GatewayConfig 的缺省值
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

requires_powershell = pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")

FAKES = """
function global:Get-CimInstance {
    param($ClassName, $Filter)
    Add-Content -LiteralPath $env:KEEPER_TRACE -Value ("CIM " + $Filter)
    foreach ($item in @($env:KEEPER_PROCS -split ';' | Where-Object { $_ })) {
        $pair = $item -split '=', 2
        $path = $pair[1]
        if ($path -eq '<null>') { $path = $null }
        [pscustomobject]@{ ProcessId = [int]$pair[0]; ExecutablePath = $path }
    }
}
function global:Start-Process {
    param($FilePath, $WorkingDirectory, $ArgumentList, [switch]$PassThru)
    Add-Content -LiteralPath $env:KEEPER_TRACE -Value ("START " + $FilePath + " | " + $WorkingDirectory + " | " + ($ArgumentList -join ' '))
    [pscustomobject]@{ Id = 4321 }
}
"""
FAILING_START = """
function global:Start-Process {
    param($FilePath, $WorkingDirectory, $ArgumentList, [switch]$PassThru)
    throw [System.InvalidOperationException]::new('refused by the stand-in')
}
"""
HIDDEN_START = """
function global:Start-Process {
    param($FilePath, $WorkingDirectory, $ArgumentList, [switch]$PassThru)
    Microsoft.PowerShell.Management\\Start-Process -FilePath $FilePath -WorkingDirectory $WorkingDirectory -PassThru:$PassThru -WindowStyle Hidden
}
"""


def quote(text) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def run_keeper(work, terminal_path, *options, procs=(), env=None, prelude=FAKES):
    """跑一次看护脚本。返回 (退出码, 标准输出, 调用记录)。"""
    trace = work / "trace.txt"
    trace.write_text("", encoding="utf-8")
    flags = ["-TerminalPath", quote(terminal_path)]
    for option in options:
        if option == "-DryRun":
            flags.append(option)
        else:
            name, _, value = option.partition("=")
            flags += [name, quote(value)]
    driver = work / "driver.ps1"
    driver.write_text("\n".join([prelude, "& " + quote(KEEPER) + " " + " ".join(flags), "exit $LASTEXITCODE"]),
                      encoding="utf-8-sig")
    done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                          capture_output=True, timeout=120, creationflags=NO_WINDOW,
                          env=dict(os.environ, KEEPER_TRACE=str(trace), KEEPER_PROCS=";".join(procs), **(env or {})))
    return done.returncode, done.stdout.decode("utf-8", errors="replace"), trace.read_text(encoding="utf-8")


def powershell(work, script: str) -> str:
    path = work / "query.ps1"
    path.write_text(script, encoding="utf-8-sig")
    done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                          capture_output=True, timeout=120, creationflags=NO_WINDOW)
    return done.stdout.decode("utf-8", errors="replace").strip()


@pytest.fixture
def terminal(tmp_path):
    """一个存在的 terminal64.exe（空文件就够：脚本只启动它，不读它）。"""
    path = tmp_path / "MT5" / "terminal64.exe"
    path.parent.mkdir()
    path.write_bytes(b"")
    return path


def test_the_keeper_only_starts_a_process_and_names_nothing_else():
    raw = KEEPER.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf"                      # UTF-8 with BOM: PS5.1 reads the Chinese help
    code = raw.decode("utf-8-sig").split("#>", 1)[1]       # drop the comment-based help
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("MetaTrader5", "python", "Stop-Process", "taskkill", "schtasks", "Remove-",
                      "New-NetFirewallRule", "Restart-Computer", "Get-Content", "Initialize"):
        assert forbidden not in code, forbidden
    assert "Start-Process" in code and "[DateTime]::UtcNow" in code and "if ($DryRun)" in code
    # 启动只在 -DryRun 判断之后：报告在先，启动在后
    assert code.index("if ($DryRun)") < code.index("Start-Process -FilePath")


@requires_powershell
@pytest.mark.parametrize("make, reason", [
    (lambda root: Path("terminal64.exe"), "not_absolute"),          # 相对路径：不替它解析
    (lambda root: root / "notepad.exe", "not_terminal64"),          # 只启动交易终端
    (lambda root: root / "missing" / "terminal64.exe", "file_missing"),   # 不存在：不创建、不猜
])
def test_it_refuses_a_path_it_cannot_trust(tmp_path, make, reason):
    target = make(tmp_path)
    if target.is_absolute() and target.name == "notepad.exe":
        target.write_bytes(b"")
    rc, out, trace = run_keeper(tmp_path, target)
    assert rc == 1 and f"KEEPER refused reason={reason}" in out
    assert "START" not in trace and "CIM" not in trace


@requires_powershell
def test_it_leaves_a_running_terminal_alone_and_starts_only_the_right_one(tmp_path, terminal):
    rc, out, trace = run_keeper(tmp_path, terminal, procs=[f"4242={str(terminal).upper()}"])   # 大小写不同
    assert rc == 0 and "KEEPER running pids=4242 other_instances=0" in out
    assert "START" not in trace
    other = tmp_path / "B1" / "terminal64.exe"
    rc, out, trace = run_keeper(tmp_path, terminal, procs=[f"4242={str(terminal).upper()}", f"7={other}"])
    assert rc == 0 and "KEEPER running pids=4242 other_instances=1" in out and "START" not in trace
    # 这一个不在：另一个 terminal64.exe 是别的实例，照常启动这一个，不带参数
    rc, out, trace = run_keeper(tmp_path, terminal, procs=[f"7={other}"])
    assert rc == 0 and "KEEPER started pid=4321 other_instances=1" in out
    assert trace.splitlines() == ["CIM Name='terminal64.exe'", f"START {terminal} | {terminal.parent} | "]


@requires_powershell
def test_dry_run_reports_but_never_starts(tmp_path, terminal):
    rc, out, trace = run_keeper(tmp_path, terminal, "-DryRun")
    assert rc == 0 and "KEEPER would-start other_instances=0" in out and "START" not in trace
    rc, out, trace = run_keeper(tmp_path, terminal, "-DryRun", procs=[f"4242={terminal}"])
    assert rc == 0 and "KEEPER running pids=4242 other_instances=0" in out and "START" not in trace


@requires_powershell
def test_it_refuses_to_guess_when_a_terminal_path_cannot_be_read(tmp_path, terminal):
    rc, out, trace = run_keeper(tmp_path, terminal, procs=["4242=<null>"])
    assert rc == 1 and "KEEPER refused reason=cannot_tell" in out and "pids=4242" in out
    assert "START" not in trace


@requires_powershell
def test_a_failed_start_is_reported(tmp_path, terminal):
    rc, out, trace = run_keeper(tmp_path, terminal, prelude=FAKES + FAILING_START)
    assert rc == 1 and "KEEPER start-failed error=InvalidOperationException" in out
    assert "refused by the stand-in" not in out                # 异常原文不进输出


@requires_powershell
def test_state_changes_go_to_the_log_and_no_path_is_ever_printed(tmp_path, terminal):
    log = tmp_path / "logs" / "keeper.log"
    rc, out, _ = run_keeper(tmp_path, terminal, f"-LogPath={log}")
    assert rc == 0 and str(terminal) not in out and str(tmp_path) not in out
    lines = log.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 1 and lines[0].endswith("\tstarted pid=4321 other_instances=0")
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\t", lines[0])     # 系统时钟的 UTC
    assert str(terminal) not in lines[0]
    rc, out, _ = run_keeper(tmp_path, terminal, f"-LogPath={log}", procs=[f"4242={terminal}"])
    assert rc == 0 and "KEEPER running" in out
    assert len(log.read_text(encoding="utf-8-sig").splitlines()) == 1     # "在运行"不是状态变化，不记


SLOW_START = FAKES.replace("    [pscustomobject]@{ Id = 4321 }",
                           "    Start-Sleep -Seconds 10; [pscustomobject]@{ Id = 4321 }   # hold the lock while starting")


@requires_powershell
@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_the_lock_is_per_terminal_and_a_second_keeper_of_the_same_terminal_backs_off(tmp_path, terminal):
    other = tmp_path / "Other" / "terminal64.exe"
    other.parent.mkdir()
    other.write_bytes(b"")
    work = tmp_path / "work"
    work.mkdir()
    slow_trace = work / "slow-trace.txt"
    slow_trace.write_text("", encoding="utf-8")
    driver = work / "slow.ps1"
    driver.write_text("\n".join([SLOW_START, "& " + quote(KEEPER) + " -TerminalPath " + quote(terminal),
                                 "exit $LASTEXITCODE"]), encoding="utf-8-sig")
    slow = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW,
                            env=dict(os.environ, KEEPER_TRACE=str(slow_trace), KEEPER_PROCS=""))
    try:
        deadline = time.monotonic() + 30
        while "START " not in slow_trace.read_text(encoding="utf-8"):      # the first keeper is starting the terminal
            assert time.monotonic() < deadline and slow.poll() is None, "the slow keeper never reached Start-Process"
            time.sleep(0.1)
        rc, out, _ = run_keeper(work, terminal)                              # the same terminal, meanwhile
        assert rc == 0 and "KEEPER busy" in out
        rc, out, _ = run_keeper(work, other)                                 # another terminal has a lock of its own
        assert rc == 0 and "KEEPER started pid=4321" in out
    finally:
        started, _ = slow.communicate(timeout=60)
    assert b"KEEPER started pid=4321" in started


@requires_powershell
@pytest.mark.skipif(not DEFAULT_INSTALL.exists(), reason="no terminal at the default install path")
def test_dry_run_against_a_real_installation_only_reports(tmp_path):
    rc, out, trace = run_keeper(tmp_path, DEFAULT_INSTALL, "-DryRun", prelude="")
    assert rc == 0 and out.startswith("KEEPER "), out
    assert " started " not in out and "start-failed" not in out


# ------------------------------------------------ 真的启动一次（用替身程序）

def _stand_in(tmp_path):
    """python.exe 的副本，名字叫 terminal64.exe：没有参数就停在交互提示符上。"""
    base = Path(sys.base_prefix)
    exe = tmp_path / "stand-in" / "terminal64.exe"
    exe.parent.mkdir()
    shutil.copyfile(base / "python.exe", exe)
    env = {"PATH": str(base) + os.pathsep + os.environ.get("PATH", ""), "PYTHONHOME": str(base)}
    return exe, env


def _stand_in_runs(exe, env) -> bool:
    try:
        done = subprocess.run([str(exe), "-c", "import sys"], env=dict(os.environ, **env),
                              capture_output=True, timeout=120, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _stand_in_processes(work, *images):
    """映像路径是 images 之一的进程：[(pid, 命令行)]。只找这些替身，不碰别的。"""
    spelled = ",".join(quote(str(image)) for image in images)
    out = powershell(work, (
        "Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | ForEach-Object {"
        f" if (@({spelled}) -contains $_.ExecutablePath) {{ \"$($_.ProcessId)|$($_.ExecutablePath)|$($_.CommandLine)\" }} }}\n"))
    found = []
    for line in out.splitlines():
        if line.count("|") >= 2:
            pid, image, command = line.split("|", 2)
            found.append((int(pid), image, command))
    return found


@requires_powershell
@pytest.mark.skipif(os.name != "nt", reason="Windows processes")
def test_it_really_starts_the_terminal_with_no_arguments_and_then_leaves_it_alone(tmp_path):
    exe, env = _stand_in(tmp_path)
    if not _stand_in_runs(exe, env):
        pytest.skip("python.exe 的副本在这里换一个目录跑不起来")
    work = tmp_path / "work"
    work.mkdir()
    try:
        rc, out, _ = run_keeper(work, exe, prelude=HIDDEN_START, env=env)
        found = re.search(r"KEEPER started pid=(\d+)", out)
        assert rc == 0 and found, out
        started = _stand_in_processes(work, exe)
        assert [pid for pid, _, _ in started] == [int(found.group(1))]
        assert started[0][2].strip().strip('"').lower() == str(exe).lower()     # 不带任何参数
        rc, out, _ = run_keeper(work, exe, prelude="", env=env)                 # 真实的进程表，没有替身
        assert rc == 0 and f"KEEPER running pids={found.group(1)} other_instances=" in out
    finally:
        for pid, _, _ in _stand_in_processes(work, exe):                        # 只结束映像路径核对过的替身
            try:
                os.kill(pid, 9)
            except OSError:
                pass


@requires_powershell
@pytest.mark.skipif(os.name != "nt", reason="Windows directory junctions")
def test_a_terminal_started_through_a_directory_junction_counts_as_running(tmp_path):
    """The gateway probe resolves links on both sides; the keeper must too. Started through a
    junction, the process is listed under the junction spelling: a keeper that compared spellings
    would start a second copy every minute and report `started` each time."""
    exe, env = _stand_in(tmp_path)
    if not _stand_in_runs(exe, env):
        pytest.skip("python.exe 的副本在这里换一个目录跑不起来")
    link = tmp_path / "junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(exe.parent)],
                          capture_output=True, timeout=60, creationflags=NO_WINDOW)
    assert made.returncode == 0 and (link / exe.name).is_file(), made.stdout.decode(errors="replace")
    work = tmp_path / "work"
    work.mkdir()
    via_link = link / exe.name
    try:
        rc, out, _ = run_keeper(work, via_link, prelude=HIDDEN_START, env=env)  # started through the junction
        found = re.search(r"KEEPER started pid=(\d+)", out)
        assert rc == 0 and found, out
        for spelled in (via_link, exe):
            rc, out, _ = run_keeper(work, spelled, prelude="", env=env)         # either spelling: it is running
            assert rc == 0 and f"KEEPER running pids={found.group(1)} other_instances=" in out, (spelled, out)
        rc, out, _ = run_keeper(work, exe, "-DryRun", prelude="", env=env)
        assert rc == 0 and "would-start" not in out
    finally:
        for pid, _, _ in _stand_in_processes(work, exe, via_link):              # 只结束映像路径核对过的替身
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        os.rmdir(link)                                                           # the junction only, not what it points to
    assert exe.is_file() and not link.exists()
