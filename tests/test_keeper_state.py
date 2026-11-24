"""看护脚本的状态文件（定版前跟进 D）：terminal_keeper.ps1 每次做过判断的运行把结果写进
-LogPath 所在目录下的 state.json，小时级存活检查（liveness_check.py --expect-keeper）和安装脚本的
-Status 读它：看护任务没在跑、看护被拒绝、终端一直不在，靠这个文件发现。

和 T209 的测试（tests/test_t209_terminal_keeper.py）同一个做法：PowerShell 驱动脚本里把
Get-CimInstance 和 Start-Process 换成记录调用的函数——不启动任何进程，也不查真实的进程表。
terminal64.exe 是一个空文件。这里没有计划任务，也不碰任务计划程序。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEEPER = ROOT / "scripts" / "ops" / "terminal_keeper.ps1"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
requires_powershell = pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
STATE_FILE = Path("logs") / "state.json"
LOG = STATE_FILE.with_name("terminal_keeper.log")
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

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
    Add-Content -LiteralPath $env:KEEPER_TRACE -Value ("START " + $FilePath)
    [pscustomobject]@{ Id = 4321 }
}
"""
FAILING_START = FAKES.replace("    [pscustomobject]@{ Id = 4321 }",
                              "    throw [System.InvalidOperationException]::new('refused by the stand-in')")
SLOW_START = FAKES.replace("    [pscustomobject]@{ Id = 4321 }",
                           "    Start-Sleep -Seconds 5; [pscustomobject]@{ Id = 4321 }   # hold the lock while starting")


def quote(text) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def run_keeper(work, terminal_path, log=None, *, dry_run=False, procs=(), prelude=FAKES, env=None):
    """跑一次看护脚本。→ (退出码, 标准输出加标准错误, 调用记录)."""
    trace = work / "trace.txt"
    trace.write_text("", encoding="utf-8")
    flags = ["-TerminalPath", quote(terminal_path)]
    if log is not None:
        flags += ["-LogPath", quote(log)]
    if dry_run:
        flags.append("-DryRun")
    driver = work / "driver.ps1"
    driver.write_text("\n".join([prelude, "& " + quote(KEEPER) + " " + " ".join(flags), "exit $LASTEXITCODE"]),
                      encoding="utf-8-sig")
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                          capture_output=True, timeout=120, creationflags=NO_WINDOW,
                          env=dict(os.environ, KEEPER_TRACE=str(trace), KEEPER_PROCS=";".join(procs), **(env or {})))
    said = done.stdout.decode("utf-8", errors="replace") + done.stderr.decode("utf-8", errors="replace")
    return done.returncode, said, trace.read_text(encoding="utf-8")


def epoch_of(stamp: str) -> float:
    """状态文件里的 ts（系统时钟的 UTC）换成时间戳，和测试进程自己的时钟比。"""
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture
def terminal(tmp_path):
    """一个存在的 terminal64.exe（空文件就够：脚本只启动它，不读它）。"""
    path = tmp_path / "MT5" / "terminal64.exe"
    path.parent.mkdir()
    path.write_bytes(b"")
    return path


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


def state(tmp_path) -> dict:
    return json.loads((tmp_path / STATE_FILE).read_text(encoding="utf-8-sig"))


def facts(record: dict) -> dict:
    return {key: record[key] for key in ("outcome", "reason", "running", "others", "pid")}


@requires_powershell
def test_running_and_started_are_written_with_the_facts_of_that_run(tmp_path, terminal, work):
    """在运行：outcome running，两个计数。启动了：outcome started，带启动的 pid。每次整份替换，目录里
    不留临时文件；ts 是系统时钟的 UTC；文件里没有本机路径。"""
    log = tmp_path / LOG
    before = time.time()
    rc, said, _ = run_keeper(work, terminal, log, procs=[f"4242={str(terminal).upper()}", "7=elsewhere"])
    assert rc == 0 and "KEEPER running pids=4242 other_instances=1" in said, said
    running = state(tmp_path)
    assert facts(running) == {"outcome": "running", "reason": "", "running": 1, "others": 1, "pid": 0}
    assert sorted(running) == ["others", "outcome", "pid", "reason", "running", "ts"]
    assert TS.match(running["ts"]) and before - 5 <= epoch_of(running["ts"]) <= time.time() + 5
    assert str(terminal) not in (tmp_path / STATE_FILE).read_text(encoding="utf-8")

    rc, said, _ = run_keeper(work, terminal, log)
    assert rc == 0 and "KEEPER started pid=4321 other_instances=0" in said, said
    assert facts(state(tmp_path)) == {"outcome": "started", "reason": "", "running": 0, "others": 0, "pid": 4321}
    assert {path.name for path in (tmp_path / "logs").iterdir()} == {"terminal_keeper.log", "state.json"}


@requires_powershell
def test_a_refusal_and_a_failed_start_are_written_with_their_reason(tmp_path, terminal, work):
    """拒绝——拿锁之后读不到映像路径，和拿锁之前的参数检查——和启动失败都写：outcome 和 reason 是代码，
    不是异常原文。"""
    log = tmp_path / LOG
    rc, said, _ = run_keeper(work, terminal, log, procs=["4242=<null>"])
    assert rc == 1 and "KEEPER refused reason=cannot_tell" in said, said
    assert facts(state(tmp_path)) == {"outcome": "refused", "reason": "cannot_tell", "running": 0, "others": 0, "pid": 0}

    rc, said, _ = run_keeper(work, tmp_path / "notepad.exe", log)
    assert rc == 1 and "reason=not_terminal64" in said, said
    written = state(tmp_path)
    assert (written["outcome"], written["reason"]) == ("refused", "not_terminal64")

    rc, said, _ = run_keeper(work, terminal, log, prelude=FAILING_START)
    assert rc == 1 and "KEEPER start-failed error=InvalidOperationException" in said, said
    written = (tmp_path / STATE_FILE).read_text(encoding="utf-8")
    assert facts(json.loads(written)) == {"outcome": "start_failed", "reason": "InvalidOperationException",
                                          "running": 0, "others": 0, "pid": 0}
    assert "refused by the stand-in" not in written


@requires_powershell
def test_nothing_is_written_on_a_dry_run_or_without_a_log_path(tmp_path, terminal, work):
    log = tmp_path / LOG
    rc, said, _ = run_keeper(work, terminal, log, dry_run=True)
    assert rc == 0 and "KEEPER would-start" in said, said
    rc, said, _ = run_keeper(work, terminal, log, dry_run=True, procs=[f"4242={terminal}"])
    assert rc == 0 and "KEEPER running" in said, said
    assert not log.parent.exists()                                  # -DryRun：日志目录都不建

    rc, said, _ = run_keeper(work, terminal)                       # 没给 -LogPath：启动了，什么都不写
    assert rc == 0 and "KEEPER started" in said and "write failed" not in said, said
    assert not log.parent.exists()


@requires_powershell
@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_a_busy_keeper_does_not_write_over_the_one_holding_the_lock(tmp_path, terminal, work):
    """第一个看护正停在"正在启动"里（持锁 5 秒），同一个终端的第二个看护退让：它没做判断，不写状态
    文件——之前那一条原样留着。持锁的那个启动完才写 started。"""
    log = tmp_path / LOG
    rc, said, _ = run_keeper(work, terminal, log, procs=[f"4242={terminal}"])
    assert rc == 0, said
    earlier = (tmp_path / STATE_FILE).read_bytes()

    slow_trace = work / "slow-trace.txt"
    slow_trace.write_text("", encoding="utf-8")
    driver = work / "slow.ps1"
    driver.write_text("\n".join([SLOW_START, "& " + quote(KEEPER) + " -TerminalPath " + quote(terminal)
                                 + " -LogPath " + quote(log), "exit $LASTEXITCODE"]), encoding="utf-8-sig")
    slow = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(driver)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW, env=dict(os.environ, KEEPER_TRACE=str(slow_trace), KEEPER_PROCS=""))
    try:
        deadline = time.monotonic() + 30
        while "START " not in slow_trace.read_text(encoding="utf-8"):      # the first keeper is starting the terminal
            assert time.monotonic() < deadline and slow.poll() is None, "the slow keeper never reached Start-Process"
            time.sleep(0.1)
        rc, said, _ = run_keeper(work, terminal, log)                     # the same terminal, meanwhile
        assert rc == 0 and "KEEPER busy" in said, said
        assert (tmp_path / STATE_FILE).read_bytes() == earlier            # busy 没有写
    finally:
        started, _ = slow.communicate(timeout=60)
    assert b"KEEPER started pid=4321" in started
    assert facts(state(tmp_path)) == {"outcome": "started", "reason": "", "running": 0, "others": 0, "pid": 4321}


@requires_powershell
def test_a_state_file_that_cannot_be_written_only_warns(tmp_path, terminal, work):
    """日志目录建不出来（它的上一级是一个文件）：日志和状态文件都只警告，看护的结论和退出码不变。"""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    log = blocker / "state" / "terminal_keeper.log"
    rc, said, _ = run_keeper(work, terminal, log)
    assert rc == 0 and "KEEPER started pid=4321 other_instances=0" in said, said
    assert "state write failed: " in said and "log write failed: " in said
    assert str(tmp_path) not in said                                   # 警告里只有异常类名，没有路径
    assert blocker.is_file()
