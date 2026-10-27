"""scripts/demo_run.ps1 模式边界与生命周期测试（T49/R8-I4/R8-I6）。

用 ``powershell -File`` 跑**拒绝路径与 -PrecheckOnly**（无副作用分支）：

- 裸跑（无 -Mode）拒绝并列出两种模式说明（R8-I4）；
- 未知 -Mode 拒绝；
- Demo 模式配置校验先于网关预检：risk_fraction / 非 0.01 手 → 拒绝
  （不连接终端、不启动任何进程）；
- 生命周期（R8-I6）：pids.txt（新旧两种列格式）里目标 magic 已有存活
  运行器 → -PrecheckOnly 显示跳过清单（runner/<magic>）；死 PID 不视为
  存活（不挡启动、不进跳过清单）；
- -PrecheckOnly 不追加 pids.txt。

不在测试里跑的路径（会连接终端或启动进程）：Demo 模式的 DEMO 保险丝/
网关预检（真终端只读 dry 检查）、任何不带 -PrecheckOnly 的启动。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "demo_run.ps1"

MAGIC = 902601

CONFIG_VOLUME_001 = f'''# Demo 模式测试配置
symbol = "XAUUSD"
timeframe = "M15"
magic = {MAGIC}
volume_lots = 0.01
'''

CONFIG_RISK_FRACTION = f'''# Demo 模式测试配置（按风险定手数——必须拒绝）
symbol = "XAUUSD"
timeframe = "M15"
magic = {MAGIC}
risk_fraction = 0.005
'''

CONFIG_WRONG_LOTS = f'''# Demo 模式测试配置（非 0.01 手——必须拒绝）
symbol = "XAUUSD"
timeframe = "M15"
magic = {MAGIC}
volume_lots = 0.02
'''


def run_ps1(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """powershell -File 跑脚本（拒绝路径/PrecheckOnly，均无副作用）。"""
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(SCRIPT), *args]
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def out_text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or b"").decode("utf-8", errors="replace")


@pytest.fixture(autouse=True)
def _require_powershell():
    if shutil.which("powershell") is None:
        pytest.skip("本机无 powershell（仅 Windows）")


def _dead_pid() -> int:
    """取一个确认已退出的 PID：起一个短命进程等它退出。"""
    p = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Start-Sleep -Milliseconds 100; exit 0"])
    pid = p.pid
    deadline = time.time() + 30
    while p.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    p.wait()
    return pid


# ---------------------------------------------------------------- 模式边界

def test_bare_invocation_refused_with_mode_listing():
    """裸跑（无 -Mode）拒绝：退出码 1 + 两种模式说明（R8-I4）。"""
    proc = run_ps1()
    assert proc.returncode == 1
    out = out_text(proc)
    assert "PaperSoak" in out and "Demo" in out
    assert "-Mode" in out


def test_invalid_mode_refused():
    proc = run_ps1("-Mode", "Bogus")
    assert proc.returncode == 1
    out = out_text(proc)
    assert "PaperSoak" in out and "Demo" in out


def test_demo_mode_rejects_risk_fraction_config(tmp_path):
    """Demo 模式：risk_fraction（按风险定手数）→ 拒绝；且失败发生在网关
    预检之前（输出无 trade_mode 账户核对——绝不连接终端）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_RISK_FRACTION, encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PrecheckOnly")
    assert proc.returncode == 1
    out = out_text(proc)
    assert "risk_fraction" in out
    assert "trade_mode" not in out          # 网关预检未运行（配置校验先行）


def test_demo_mode_rejects_non_001_volume(tmp_path):
    """Demo 模式：volume_lots ≠ 0.01 → 拒绝（阶段 F 手数上限）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_WRONG_LOTS, encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PrecheckOnly")
    assert proc.returncode == 1
    out = out_text(proc)
    assert "volume_lots" in out


def test_demo_mode_rejects_missing_volume(tmp_path):
    """Demo 模式：两个手数来源都没写 → 拒绝（固定 0.01 缺失）。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(
        f'symbol = "XAUUSD"\ntimeframe = "M15"\nmagic = {MAGIC}\n',
        encoding="utf-8")
    proc = run_ps1("-Mode", "Demo", "-ConfigDir", str(cfg_dir),
                   "-PrecheckOnly")
    assert proc.returncode == 1


# ---------------------------------------------------------------- 生命周期（R8-I6）

def test_precheck_only_lists_alive_runner_skip(tmp_path):
    """pids.txt（新 6 列格式）里目标 magic 有存活运行器 → -PrecheckOnly
    显示跳过清单 runner/<magic>；且不追加 pids.txt。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    log_dir = tmp_path / "demo"
    log_dir.mkdir()
    pids = log_dir / "pids.txt"
    pids.write_text(
        f"{os.getpid()}\trunner\t{MAGIC}\tC:\\\\old.log\tPaperSoak\t"
        "2026-10-27T00:00:00.0000000Z\n", encoding="utf-8")
    before = pids.read_bytes()
    proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                   "-LogDir", str(log_dir), "-PrecheckOnly")
    assert proc.returncode == 0, out_text(proc)
    assert f"runner/{MAGIC}" in out_text(proc)      # 将被跳过（绝不并行第二份）
    assert pids.read_bytes() == before              # -PrecheckOnly 不写 pids.txt


def test_precheck_only_parses_legacy_pid_lines(tmp_path):
    """旧 4 列 pids.txt（pid kind name log）同样识别：存活运行器进跳过清单。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    log_dir = tmp_path / "demo"
    log_dir.mkdir()
    (log_dir / "pids.txt").write_text(
        f"{os.getpid()}\trunner\t{MAGIC}\tC:\\\\old.log\n", encoding="utf-8")
    proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                   "-LogDir", str(log_dir), "-PrecheckOnly")
    assert proc.returncode == 0, out_text(proc)
    assert f"runner/{MAGIC}" in out_text(proc)


def test_dead_pid_not_treated_as_alive(tmp_path):
    """死 PID 不视为存活：不进跳过清单（允许补启动），-PrecheckOnly 通过。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    (cfg_dir / "strategy.toml").write_text(CONFIG_VOLUME_001, encoding="utf-8")
    log_dir = tmp_path / "demo"
    log_dir.mkdir()
    dead = _dead_pid()
    (log_dir / "pids.txt").write_text(
        f"{dead}\trunner\t{MAGIC}\tC:\\\\old.log\tPaperSoak\t"
        "2026-10-27T00:00:00.0000000Z\n", encoding="utf-8")
    proc = run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(cfg_dir),
                   "-LogDir", str(log_dir), "-PrecheckOnly")
    assert proc.returncode == 0, out_text(proc)
    assert f"runner/{MAGIC}" not in out_text(proc)  # 不挡启动
