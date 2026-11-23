"""T209 终端看护脚本：终端进程不在就启动它，别的什么都不做。

多数用例在 PowerShell 驱动脚本里把 Get-CimInstance 和 Start-Process 换成记录
调用的函数：不启动任何进程，也不查真实的进程表。最后两个用例看真实情况：
一个只做 -DryRun（脚本结构上不可能启动）；一个用 python.exe 的副本冒充
terminal64.exe，真的启动一次、再认出它在运行，结束时只结束这个副本——
先核对映像路径，再动它。
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEEPER = ROOT / "scripts" / "ops" / "terminal_keeper.ps1"
MUTEX = "Local\\MT5Quant_terminal_keeper"
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


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_a_second_instance_backs_off_instead_of_starting_twice(tmp_path, terminal):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.ReleaseMutex.argtypes = kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = ctypes.c_void_p(kernel.CreateMutexW(None, False, MUTEX))
    assert handle.value and kernel.WaitForSingleObject(handle, 0) in (0, 0x80)   # WAIT_OBJECT_0 / WAIT_ABANDONED
    try:
        rc, out, trace = run_keeper(tmp_path, terminal)
        assert rc == 0 and "KEEPER busy" in out and "START" not in trace
    finally:
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


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


def _stand_in_processes(work, exe):
    """映像路径就是 exe 的进程：[(pid, 命令行)]。只找这一个替身，不碰别的。"""
    out = powershell(work, (
        "Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | ForEach-Object {"
        f" if ($_.ExecutablePath -ieq {quote(exe)}) {{ \"$($_.ProcessId)|$($_.CommandLine)\" }} }}\n"))
    return [(int(line.split("|", 1)[0]), line.split("|", 1)[1]) for line in out.splitlines() if "|" in line]


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
        assert [pid for pid, _ in started] == [int(found.group(1))]
        assert started[0][1].strip().strip('"').lower() == str(exe).lower()     # 不带任何参数
        rc, out, _ = run_keeper(work, exe, prelude="", env=env)                 # 真实的进程表，没有替身
        assert rc == 0 and f"KEEPER running pids={found.group(1)} other_instances=" in out
    finally:
        for pid, _ in _stand_in_processes(work, exe):                           # 只结束映像路径核对过的替身
            try:
                os.kill(pid, 9)
            except OSError:
                pass
