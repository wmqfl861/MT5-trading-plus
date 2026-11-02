"""运行安装脚本的临时副本；schtasks 由本进程函数替代，不安装本机任务。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def install_in_sandbox(tmp_path, mode=None, *, paper=False, config_name="gateway.json", with_config=True):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts" / "setup_scheduled_tasks.ps1",
                    scripts / "setup_scheduled_tasks.ps1")
    if with_config:
        (tmp_path / config_name).write_text(
            json.dumps({"allow_live_trading": False}), encoding="utf-8")
    if paper:
        (tmp_path / "data" / "live" / "paper").mkdir(parents=True)
    driver = tmp_path / "invoke.ps1"
    driver.write_text(
        "function global:schtasks { ($args -join '|') | Add-Content -LiteralPath $env:TASK_CALLS; $global:LASTEXITCODE = 0 }\n"
        "$env:TASK_CALLS = Join-Path $PSScriptRoot 'task-calls.txt'\n"
        "& $PSScriptRoot/scripts/setup_scheduled_tasks.ps1 "
        + ("-Mode " + mode if mode else "") + "\n",
        encoding="utf-8-sig")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
        capture_output=True, timeout=30)
    return result, scripts / "task_live_daily_report.cmd"


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
