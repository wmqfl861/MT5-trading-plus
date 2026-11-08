"""运行安装脚本的临时副本；schtasks 由本进程函数替代，不安装本机任务。

T182（2026-11-08）增补：数据采集任务改每周六（休市日）触发、日报时刻移入
XAUUSD 换日窗（05:57 本地，窗口推导对 mt5data.sessions 全年逐日复核）、
watch_swap（T35，已过期）不再安装。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mt5data.sessions import NY_TZ, SessionPolicy

ROOT = Path(__file__).resolve().parents[1]

TAIPEI = ZoneInfo("Asia/Taipei")   # 本机时区（安装脚本注释的换算前提：UTC+8 无夏令时）
# 日报计划时刻（scripts/setup_scheduled_tasks.ps1 硬编码；推导注释在该文件头）
DAILY_REPORT_LOCAL = "05:57"


def install_in_sandbox(tmp_path, mode=None, *, paper=False, config_name="gateway.json",
                       with_config=True, include_data=False, with_ticks=False):
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
    driver = tmp_path / "invoke.ps1"
    flags = ""
    if mode:
        flags += " -Mode " + mode
    if include_data:
        flags += " -IncludeDataTasks"
    driver.write_text(
        "function global:schtasks { ($args -join '|') | Add-Content -LiteralPath $env:TASK_CALLS; $global:LASTEXITCODE = 0 }\n"
        "$env:TASK_CALLS = Join-Path $PSScriptRoot 'task-calls.txt'\n"
        "& $PSScriptRoot/scripts/setup_scheduled_tasks.ps1" + flags + "\n",
        encoding="utf-8-sig")
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
