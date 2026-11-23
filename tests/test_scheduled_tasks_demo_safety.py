"""运行安装脚本的临时副本；schtasks 由本进程函数替代，不安装本机任务。

T182（2026-11-08）增补：数据采集任务改每周六（休市日）触发、日报时刻移入
XAUUSD 换日窗（05:57 本地，窗口推导对 mt5data.sessions 全年逐日复核）、
watch_swap（T35，已过期）不再安装。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mt5data.sessions import NY_TZ, SessionPolicy

ROOT = Path(__file__).resolve().parents[1]

TAIPEI = ZoneInfo("Asia/Taipei")   # 本机时区（安装脚本注释的换算前提：UTC+8 无夏令时）
# 日报计划时刻（scripts/setup_scheduled_tasks.ps1 硬编码；推导注释在该文件头）
DAILY_REPORT_LOCAL = "05:57"


def entries(root):
    """root 下的全部文件和目录（相对路径集合）——断言"拒绝安装时什么都没有新建"。"""
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def install_in_sandbox(tmp_path, mode=None, *, paper=False, config_name="gateway.json",
                       with_config=True, include_data=False, with_ticks=False,
                       with_python=True, with_sessions=True, with_spread=True,
                       paths=None, code_pages=None, before=None):
    """paths（T213）：显式参数 {参数名: 路径}，逐个变成 -参数名 '路径'。

    with_spread：在点差模型的位置（显式路径或缺省的 data\\costs 下）放一个空文件占位
    ——评审 R6 起安装脚本先核对这个文件。
    code_pages：把安装脚本读到的两个系统代码页固定成 (ANSI, OEM)。驱动脚本里事先定义
    一个同名的 Get-SystemCodePages 函数，安装脚本就用它——和 schtasks 的替身是同一个
    方式，不改系统设置。
    before（字典）：运行安装脚本之前把沙箱里已有的条目记到 before["entries"]。
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts" / "setup_scheduled_tasks.ps1",
                    scripts / "setup_scheduled_tasks.ps1")
    if with_config:
        (tmp_path / config_name).write_text(
            json.dumps({"allow_live_trading": False}), encoding="utf-8")
    if paper:
        (tmp_path / "data" / "live" / "paper").mkdir(parents=True)
    if with_ticks:
        (tmp_path / "data" / "ticks" / "XAUUSD").mkdir(parents=True)
    if with_python:
        # T213：安装脚本要求任务命令里的解释器存在；沙箱里放一个空文件占位。
        (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
        (tmp_path / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    if with_sessions:
        (tmp_path / "config").mkdir()
        shutil.copyfile(ROOT / "config" / "sessions.toml",
                        tmp_path / "config" / "sessions.toml")
    spread = (Path(paths["SpreadModel"]) if paths and "SpreadModel" in paths
              else tmp_path / "data" / "costs" / "spread_model.json")
    if with_spread:
        spread.parent.mkdir(parents=True, exist_ok=True)
        spread.write_bytes(b"")
    driver = tmp_path / "invoke.ps1"
    flags = ""
    if mode:
        flags += " -Mode " + mode
    if include_data:
        flags += " -IncludeDataTasks"
    for name, value in (paths or {}).items():
        flags += f" -{name} '{value}'"
    driver.write_text(
        "function global:schtasks { ($args -join '|') | Add-Content -LiteralPath $env:TASK_CALLS; $global:LASTEXITCODE = 0 }\n"
        + (f"function global:Get-SystemCodePages {{ [pscustomobject]@{{ ACP = {code_pages[0]}; OEMCP = {code_pages[1]} }} }}\n"
           if code_pages else "")
        + "$env:TASK_CALLS = Join-Path $PSScriptRoot 'task-calls.txt'\n"
        "& $PSScriptRoot/scripts/setup_scheduled_tasks.ps1" + flags + "\n",
        encoding="utf-8-sig")
    if before is not None:
        before["entries"] = entries(tmp_path)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True, timeout=30)
    return result, scripts / "task_live_daily_report.cmd"


def schtasks_calls(tmp_path):
    """捕获的 schtasks 调用 → [（任务名, {开关: 值}）]。

    行格式 = 替身把 $args 用 '|' 连接：'/Create|/F|/TN|MT5Quant\\x|/SC|WEEKLY|...'。
    /F 是无值开关：下一个 token 以 '/' 开头即视为裸开关。
    """
    calls = []
    for line in (tmp_path / "task-calls.txt").read_text(
            encoding="utf-8-sig").splitlines():
        toks = line.split("|")
        assert toks[0] == "/Create", f"测试替身只应看到 /Create：{line}"
        args, i = {}, 1
        while i < len(toks):
            key, val = toks[i], None
            if i + 1 < len(toks) and not toks[i + 1].startswith("/"):
                val = toks[i + 1]
                i += 1
            args[key] = val
            i += 1
        calls.append((args.get("/TN"), args))
    return calls


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_task_install_requires_mode_before_writing_wrappers(tmp_path):
    result, wrapper = install_in_sandbox(tmp_path)
    assert result.returncode != 0
    assert not wrapper.exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_demo_daily_task_uses_explicit_mode_config_and_costs(tmp_path):
    result, wrapper = install_in_sandbox(tmp_path, "Demo", paper=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    command = wrapper.read_text(encoding="ascii")
    assert " --demo" in command
    assert " --gw-config " in command
    assert " --config " not in command
    assert " --paper-state " not in command
    assert " --spread-model " in command
    assert " --log-dir " in command


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_paper_daily_task_keeps_missing_state_path_without_terminal_fallback(tmp_path):
    result, wrapper = install_in_sandbox(tmp_path, "PaperSoak")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    command = wrapper.read_text(encoding="ascii")
    assert " --paper-state " in command
    assert " --demo" not in command


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
@pytest.mark.parametrize("mode", ["Demo", "PaperSoak"])
def test_default_install_only_touches_daily_and_weekly_tasks(tmp_path, mode):
    result, _ = install_in_sandbox(tmp_path, mode)
    assert result.returncode == 0
    calls = (tmp_path / "task-calls.txt").read_text(encoding="utf-8-sig").splitlines()
    names = [line.split("|/TN|")[1].split("|")[0] for line in calls]
    assert names == ["MT5Quant\\live_daily_report", "MT5Quant\\demo_weekly_check"]
    assert not (tmp_path / "scripts" / "task_snapshot_specs.cmd").exists()
    assert not (tmp_path / "scripts" / "task_refresh_bars.cmd").exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_missing_demo_config_has_no_task_install_side_effects(tmp_path):
    result, wrapper = install_in_sandbox(tmp_path, "Demo", with_config=False)
    assert result.returncode != 0
    assert not wrapper.exists()
    assert not (tmp_path / "task-calls.txt").exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_task_uses_canonical_config_when_only_config_json_exists(tmp_path):
    result, wrapper = install_in_sandbox(tmp_path, "Demo", config_name="config.json")
    assert result.returncode == 0
    command = wrapper.read_text(encoding="ascii")
    assert str(tmp_path / "config.json") in command
    assert "gateway.json" not in command


# ------------------------------------------------ T182：周六数据任务/换日窗日报/无 watch_swap

# T182：三个数据任务改每周六本机白天（周六全天休市，避开纽约盘中），错峰排列。
SATURDAY_TASKS = {
    "MT5Quant\\snapshot_specs": "10:00",
    "MT5Quant\\refresh_bars": "11:00",
    "MT5Quant\\refresh_ticks": "13:00",
}


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_data_tasks_with_include_data_run_weekly_on_saturday(tmp_path):
    result, _ = install_in_sandbox(tmp_path, "Demo", include_data=True, with_ticks=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    calls = dict(schtasks_calls(tmp_path))
    for tn, st in SATURDAY_TASKS.items():
        assert tn in calls, f"缺 {tn}"
        assert calls[tn]["/SC"] == "WEEKLY", f"{tn} 须为每周任务"
        assert calls[tn]["/D"] == "SAT", f"{tn} 须在周六触发"
        assert calls[tn]["/ST"] == st, f"{tn} 计划时刻应为 {st}"
        hour = int(st.split(":")[0])
        assert 9 <= hour <= 16, f"{tn} 须在本机白天运行（当前 {st}）"
    # tick 刷新沿用品种目录探测：包装器里带 data\ticks 下发现的品种
    wrapper = (tmp_path / "scripts" / "task_refresh_ticks.cmd").read_text(
        encoding="ascii")
    assert "--mode refresh --symbols \"XAUUSD\"" in wrapper


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_include_data_tasks_without_ticks_dir_skips_refresh_ticks(tmp_path):
    result, _ = install_in_sandbox(tmp_path, "Demo", include_data=True, with_ticks=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    names = [tn for tn, _ in schtasks_calls(tmp_path)]
    assert "MT5Quant\\refresh_ticks" not in names
    assert "MT5Quant\\snapshot_specs" in names and "MT5Quant\\refresh_bars" in names


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_no_watch_swap_task_is_installed_any_more(tmp_path):
    # T35 已过期（原定 2026-10-31 后删除）：即使带 -IncludeDataTasks 也不再安装。
    result, _ = install_in_sandbox(tmp_path, "Demo", include_data=True, with_ticks=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    names = [tn for tn, _ in schtasks_calls(tmp_path)]
    assert not any("watch_swap" in n for n in names)
    # 结构断言：脚本里已无 watch_swap 的包装器/安装调用（头部留给主会话的
    # 手动清理命令不算安装逻辑，故只查可执行代码模式）
    script = (ROOT / "scripts" / "setup_scheduled_tasks.ps1").read_text(
        encoding="utf-8-sig")
    assert "New-TaskWrapper \"watch_swap\"" not in script
    assert "watch_swap.py" not in script


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_daily_report_moves_into_rollover_window(tmp_path):
    # T182：日报（Demo 模式仍连终端读模拟账户）从 00:20 移入 XAUUSD 换日窗，
    # 避开纽约盘中与实盘三进程抢终端。
    result, _ = install_in_sandbox(tmp_path, "Demo", include_data=True, with_ticks=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    calls = dict(schtasks_calls(tmp_path))
    rep = calls["MT5Quant\\live_daily_report"]
    assert rep["/SC"] == "DAILY"
    assert rep["/ST"] == DAILY_REPORT_LOCAL


def _daily_report_fire_utc(day: date) -> datetime:
    """日报计划时刻 → naive-UTC（本机台北 UTC+8；与 sessions.py 的库内口径一致）。"""
    hh, mm = map(int, DAILY_REPORT_LOCAL.split(":"))
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=TAIPEI)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def test_daily_report_time_inside_rollover_window_year_round():
    """05:57 必须全年（EST 与 EDT 两侧）落在换日禁入窗内，且距两端 ≥1 分钟。

    窗口唯一来源 = mt5data.sessions.SessionPolicy 缺省（= config/sessions.toml：
    换日 17:00 America/New_York、前 5 分后 60 分 → 16:55–18:00 纽约）。安装脚本
    里的硬编码时刻（推导注释见其文件头）在此逐日复核一年，防 DST 漂移回归。
    """
    policy = SessionPolicy()
    day = date(2026, 11, 1)   # EST 起；跨 EST→EDT→EST 一整年
    end = date(2027, 10, 31)
    while day <= end:
        fire = _daily_report_fire_utc(day)
        ny_day = fire.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo(NY_TZ)).date()
        start, close = policy.rollover_window(ny_day)
        assert start <= fire <= close, (
            f"{day} 台北 {DAILY_REPORT_LOCAL}（UTC {fire}）不在换日窗 "
            f"[{start}, {close}] 内")
        assert fire - start >= timedelta(minutes=1), f"{day} 距窗起点不足 1 分钟"
        assert close - fire >= timedelta(minutes=1), f"{day} 距窗终点不足 1 分钟"
        day += timedelta(days=1)


def _taipei_minutes(utc_naive: datetime) -> int:
    """naive-UTC 时刻 → 台北本地当日分钟数（本机固定 UTC+8、无夏令时）。"""
    local = utc_naive.replace(tzinfo=timezone.utc).astimezone(TAIPEI)
    return local.hour * 60 + local.minute


def test_daily_report_time_is_the_year_round_window_intersection():
    """两季换日窗的全年交集只有 05:55–06:00 本地——钉死时刻选择没有别的余地。

    若 sessions.toml 改动换日锚点/前后缓冲导致交集漂移，任何一天 05:57 出窗
    时上一条测试会先失败；本条把"冬窗 05:55–07:00、夏窗 04:55–06:00"写成
    文档级断言——改配置的人会先撞到这里，并须同步改计划时刻与脚本头注释。
    """
    policy = SessionPolicy()
    expected = {  # (窗起点本地分钟, 窗终点本地分钟)：冬 EST / 夏 EDT
        date(2027, 1, 20): (5 * 60 + 55, 7 * 60),   # EST 代表日
        date(2026, 7, 15): (4 * 60 + 55, 6 * 60),   # EDT 代表日
    }
    for day, want in expected.items():
        # 与实际触发同口径：先换 UTC，再取纽约日（21:57 UTC → 纽约仍是同日）
        fire = _daily_report_fire_utc(day)
        ny_day = fire.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo(NY_TZ)).date()
        start, close = policy.rollover_window(ny_day)
        got = (_taipei_minutes(start), _taipei_minutes(close))
        assert got == want, f"{day} 换日窗本地分钟应为 {want}，实得 {got}"


# ------------------------------------------------ T213：解释器与目录显式给出

def _output(result):
    return result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_install_refuses_when_the_interpreter_is_missing(tmp_path):
    """11-17 起日报和周检任务每次都以 rc=1 失败：命令指向部署树里不存在的 .venv。"""
    before = {}
    result, wrapper = install_in_sandbox(tmp_path, "Demo", with_python=False, before=before)
    assert result.returncode != 0
    assert ".venv\\Scripts\\python.exe" in _output(result)
    assert not wrapper.exists()
    assert not (tmp_path / "scripts" / "task_demo_weekly_check.cmd").exists()
    assert not (tmp_path / "task-calls.txt").exists()
    assert entries(tmp_path) == before["entries"]     # nothing was created either


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_install_refuses_when_the_sessions_config_is_missing(tmp_path):
    result, wrapper = install_in_sandbox(tmp_path, "Demo", with_sessions=False)
    assert result.returncode != 0
    assert "sessions.toml" in _output(result)
    assert not wrapper.exists() and not (tmp_path / "task-calls.txt").exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_explicit_locations_outside_the_repo_root_reach_the_task_commands(tmp_path):
    """部署树是纯检出：解释器和运行数据在别处，包装器里只能写给出的路径。"""
    runtime = tmp_path / "runtime"
    python = runtime / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    gateway = runtime / "gateway.json"
    gateway.write_text(json.dumps({"allow_live_trading": False}), encoding="utf-8")
    paths = {"PythonExe": python, "GatewayConfig": gateway,
             "DataDir": runtime / "data", "RunnerLogDir": runtime / "demo",
             "TaskLogDir": runtime / "tasklogs", "ReportsDir": runtime / "reports",
             "SpreadModel": runtime / "costs" / "spread_model.json",
             "SessionsConfig": tmp_path / "config" / "sessions.toml"}
    result, wrapper = install_in_sandbox(tmp_path, "Demo", with_config=False, paths=paths)
    assert result.returncode == 0, _output(result)
    command = wrapper.read_text(encoding="ascii")
    for name in paths:
        assert str(paths[name]) in command, name
    assert "--sessions-config" in command
    assert str(tmp_path / "data") not in command        # not the old default location
    weekly = (tmp_path / "scripts" / "task_demo_weekly_check.cmd").read_text(encoding="ascii")
    assert str(python) in weekly and str(paths["DataDir"]) in weekly
    assert (runtime / "tasklogs").is_dir()              # task output goes where told
    assert not (tmp_path / "data").exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_install_prints_the_task_parameters_for_the_deployment_record(tmp_path):
    result, wrapper = install_in_sandbox(tmp_path, "Demo")
    assert result.returncode == 0, _output(result)
    lines = [line for line in result.stdout.decode(errors="replace").splitlines()
             if line.startswith("TASK_PARAMETERS ")]
    assert len(lines) == 1
    parameters = json.loads(lines[0].removeprefix("TASK_PARAMETERS "))
    assert parameters["mode"] == "Demo"
    assert parameters["python"] == str(tmp_path / ".venv" / "Scripts" / "python.exe")
    assert parameters["data_dir"] == str(tmp_path / "data")
    assert parameters["gateway_config"] == str(tmp_path / "gateway.json")
    assert parameters["sessions_config"] == str(tmp_path / "config" / "sessions.toml")
    assert parameters["daily_report"] == {
        "task": "MT5Quant\\live_daily_report", "local_time": "05:57", "wrapper": str(wrapper)}


# ------------------------------------------------ T213 评审（2026-11-23）：安装前核对与包装器代码页

@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_missing_demo_config_creates_no_directory(tmp_path):
    """评审 R5：Demo 模式下没有任何网关配置时，旧脚本先建了 <数据目录>\\logs 才拒绝。"""
    before = {}
    result, wrapper = install_in_sandbox(tmp_path, "Demo", with_config=False, before=before)
    assert result.returncode != 0
    assert "gateway.json" in _output(result) and "-GatewayConfig" in _output(result)
    assert entries(tmp_path) == before["entries"]       # 一个目录都没建
    assert not (tmp_path / "data" / "logs").exists()
    assert not wrapper.exists()
    assert not (tmp_path / "task-calls.txt").exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_install_refuses_when_the_spread_model_is_missing(tmp_path):
    """评审 R6：点差模型不存在也照样安装，装出来的日报任务每天退出码 2、不重试。"""
    before = {}
    result, wrapper = install_in_sandbox(tmp_path, "Demo", with_spread=False, before=before)
    assert result.returncode != 0
    assert "spread_model.json" in _output(result) and "-SpreadModel" in _output(result)
    assert entries(tmp_path) == before["entries"]       # 一个目录都没建
    assert not wrapper.exists()
    assert not (tmp_path / "task-calls.txt").exists()


WESTERN = (1252, 1252)      # ANSI 和控制台两个代码页相同（都是西欧的 1252）：表示得了 é，表示不了汉字
SPLIT = (1252, 437)         # ANSI 是 1252、控制台是 437：多数西欧系统的样子


def system_code_pages():
    """系统的 ANSI 与 OEM 代码页（读注册表）。安装脚本用的是 GetACP / GetOEMCP，两边互不依赖。"""
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r"SYSTEM\CurrentControlSet\Control\Nls\CodePage") as key:
        return (int(winreg.QueryValueEx(key, "ACP")[0]),
                int(winreg.QueryValueEx(key, "OEMCP")[0]))


def representable(text, encoding):
    try:
        return text.encode(encoding).decode(encoding) == text
    except UnicodeEncodeError:
        return False


def non_ascii_char(encoding):
    """该代码页表示得了的一个非 ASCII 字符（各种系统代码页各备一个候选）。"""
    return next((c for c in "éüäñд中日한" if representable(c, encoding)), None)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_wrappers_keep_non_ascii_paths_in_the_system_ansi_code_page(tmp_path):
    """评审 R7：包装器按系统 ANSI 代码页写。按 ASCII 写时中文等字符变成 "?"，
    任务装得上、跑不起来，安装脚本却报告成功。两个代码页不同的机器上这种路径会被
    拒绝（见 test_non_ascii_paths_are_refused_where_console_and_ansi_code_pages_differ），
    所以这里按本机真实的两个代码页分两种情形核对。"""
    acp, oemcp = system_code_pages()
    encoding = f"cp{acp}"
    mark = non_ascii_char(encoding)
    if mark is None:
        pytest.skip(f"系统代码页 {acp} 表示不了任何非 ASCII 字符")
    deploy = tmp_path / f"{mark} deploy"
    deploy.mkdir()
    before = {}
    result, wrapper = install_in_sandbox(deploy, "Demo", before=before)
    if acp != oemcp:
        assert result.returncode != 0 and entries(deploy) == before["entries"]
        return
    assert result.returncode == 0, _output(result)
    for path in (wrapper, deploy / "scripts" / "task_demo_weekly_check.cmd"):
        raw = path.read_bytes()
        assert str(deploy) in raw.decode(encoding)         # 真实的路径，不是 "?"
        assert b"?" not in raw and not raw.startswith(b"\xef\xbb\xbf")


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_install_refuses_paths_the_ansi_code_page_cannot_represent(tmp_path):
    """评审 R7：两个代码页相同的机器上，路径里有这个代码页表示不了的字符时，在副作用
    之前拒绝安装并说明原因。本机的系统代码页多半是 UTF-8（什么都表示得了），所以把
    两个代码页都固定成 1252。"""
    before = {}
    deploy = tmp_path / "部署 tree"
    deploy.mkdir()
    result, wrapper = install_in_sandbox(deploy, "Demo", code_pages=WESTERN, before=before)
    assert result.returncode != 0
    assert "1252" in _output(result) and "tree" in _output(result)
    assert entries(deploy) == before["entries"]            # 一个目录都没建
    assert not wrapper.exists() and not (deploy / "task-calls.txt").exists()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_wrapper_bytes_follow_the_ansi_code_page(tmp_path):
    """两个代码页都是 1252 时，é 写成 0xE9（UTF-8 会是 C3 A9，ASCII 会是 "?"），文件头没有 BOM。"""
    deploy = tmp_path / "déploy"
    deploy.mkdir()
    result, wrapper = install_in_sandbox(deploy, "Demo", code_pages=WESTERN)
    assert result.returncode == 0, _output(result)
    raw = wrapper.read_bytes()
    assert b"d\xe9ploy" in raw and b"?" not in raw
    assert str(deploy) in raw.decode("cp1252")
    assert not raw.startswith(b"\xef\xbb\xbf")             # BOM 会被 cmd 当成命令的一部分


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_non_ascii_paths_are_refused_where_console_and_ansi_code_pages_differ(tmp_path):
    """评审 R7 的最终处置。cmd.exe 按控制台代码页读 .cmd，新开控制台的缺省值是系统 OEM
    代码页。它和 ANSI 代码页不同的机器上，不去猜哪种编码对 cmd 有效：路径里只要有一个
    非 ASCII 字符就拒绝安装，一个目录都不建、一个任务都不建；纯 ASCII 路径照常安装。
    é 在 1252 里表示得了，所以这里拒绝它的是"两个代码页不同"这一条，不是"表示不了"那一条。"""
    before = {}
    foreign = tmp_path / "déploy tree"
    foreign.mkdir()
    result, wrapper = install_in_sandbox(foreign, "Demo", code_pages=SPLIT, before=before)
    assert result.returncode != 0
    assert "OEM 437" in _output(result) and "1252" in _output(result) and "ASCII" in _output(result)
    assert "tree" in _output(result)
    assert entries(foreign) == before["entries"]           # 一个目录都没建
    assert not wrapper.exists() and not (foreign / "task-calls.txt").exists()

    before = {}
    plain = tmp_path / "plain tree"
    plain.mkdir()
    result, wrapper = install_in_sandbox(plain, "Demo", code_pages=SPLIT, before=before)
    assert result.returncode == 0, _output(result)
    assert str(plain) in wrapper.read_text(encoding="ascii")   # 照常写、照常装
    assert [tn for tn, _ in schtasks_calls(plain)] == [
        "MT5Quant\\live_daily_report", "MT5Quant\\demo_weekly_check"]

    # 别的位置也一样算：部署树的名字是纯 ASCII，数据目录的名字里有 é，同样拒绝，也不建目录。
    plain = tmp_path / "ascii deploy"
    (plain / "données" / "costs").mkdir(parents=True)    # 点差模型的缺省位置跟着数据目录走，先放好占位文件
    (plain / "données" / "costs" / "spread_model.json").write_bytes(b"")
    before = {}
    result, wrapper = install_in_sandbox(plain, "Demo", code_pages=SPLIT,
                                         paths={"DataDir": plain / "données"}, before=before)
    assert result.returncode != 0 and "1252" in _output(result) and "OEM" in _output(result)
    assert entries(plain) == before["entries"] and not wrapper.exists()


STANDIN = (
    "import json, os, sys\n"
    "with open(os.environ['STANDIN_OUT'], 'w', encoding='utf-8') as f:\n"
    "    json.dump(sys.argv, f)\n"
)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
def test_installed_wrappers_start_under_non_ascii_paths(tmp_path):
    """真正用 cmd.exe 跑一遍装出来的两份包装器：沙箱里的两个脚本换成只记录参数的
    替身，核对它收到的路径和 TASK_PARAMETERS 行逐项一致。cmd 按控制台代码页读 .cmd，
    新开控制台的缺省值是系统 OEM 代码页；它和 ANSI 代码页不同的机器上，安装脚本拒绝
    非 ASCII 路径（见 test_non_ascii_paths_are_refused_where_console_and_ansi_code_pages_differ）。"""
    acp, oemcp = system_code_pages()
    if acp != oemcp:
        pytest.skip(f"系统 ANSI（{acp}）与 OEM（{oemcp}）代码页不同：安装脚本拒绝非 ASCII 路径")
    mark = non_ascii_char(f"cp{acp}")
    if mark is None:
        pytest.skip(f"系统代码页 {acp} 表示不了任何非 ASCII 字符")
    deploy = tmp_path / f"{mark} deploy"
    deploy.mkdir()
    result, wrapper = install_in_sandbox(deploy, "Demo", paths={"PythonExe": sys.executable})
    assert result.returncode == 0, _output(result)
    lines = [line for line in result.stdout.decode(errors="replace").splitlines()
             if line.startswith("TASK_PARAMETERS ")]
    assert len(lines) == 1
    parameters = json.loads(lines[0].removeprefix("TASK_PARAMETERS "))
    for script in ("live_daily_report", "demo_check"):
        (deploy / "scripts" / f"{script}.py").write_text(STANDIN, encoding="utf-8")

    def run(task, script):
        out = tmp_path / f"{task}.argv.json"
        env = dict(os.environ, STANDIN_OUT=str(out))
        done = subprocess.run(["cmd.exe", "/d", "/c", f"task_{task}.cmd"],
                              cwd=deploy / "scripts", env=env, capture_output=True, timeout=60)
        assert done.returncode == 0, done.stdout + done.stderr      # Python 启动并跑完
        return json.loads(out.read_text(encoding="utf-8"))

    assert run("live_daily_report", "live_daily_report") == [
        str(deploy / "scripts" / "live_daily_report.py"),
        "--date", "yesterday", "--data-dir", parameters["data_dir"],
        "--reports-dir", parameters["reports_dir"],
        "--spread-model", parameters["spread_model"],
        "--log-dir", parameters["runner_log_dir"],
        "--sessions-config", parameters["sessions_config"],
        "--demo", "--gw-config", parameters["gateway_config"]]
    assert run("demo_weekly_check", "demo_check") == [
        str(deploy / "scripts" / "demo_check.py"), "--summary",
        "--data-dir", parameters["data_dir"], "--log-dir", parameters["runner_log_dir"],
        "--reports-dir", parameters["reports_dir"]]
    assert (Path(parameters["task_log_dir"]) / "task_live_daily_report.log").is_file()
    assert (Path(parameters["runner_log_dir"]) / "weekly-check.md").is_file()
