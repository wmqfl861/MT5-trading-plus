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
    """powershell 跑脚本（拒绝路径/PrecheckOnly，均无副作用）。

    经 -Command 包一层设置 [Console]::OutputEncoding=UTF-8：PS 5.1 管道
    输出缺省取系统 OEM 代码页，会把中文输出转码。**try/catch 包裹**——
    CI 运行器是无控制台会话，[Console] 句柄无效时赋值会抛异常导致整个
    -Command 失败（stdout 全空）；此时回退默认编码，断言用的都是 ASCII
    标记（PaperSoak/Demo/-Mode/runner/<magic>/risk_fraction 等），任何
    代码页下都可读。参数名（-Xxx 形式）不加引号（-Command 上下文里带
    引号的 '-Mode' 会被当作位置实参）。"""
    def _q(s: str) -> str:
        if s.startswith("-") and all(c.isalnum() or c == "-" for c in s):
            return s
        return "'" + s.replace("'", "''") + "'"
    inner = ("try { [Console]::OutputEncoding=[Text.Encoding]::UTF8 } "
             "catch { }; & " + _q(str(SCRIPT)) + " "
             + " ".join(_q(a) for a in args)
             + " 6>&1 2>&1")
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-Command", inner]
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def out_text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or b"").decode("utf-8", errors="replace")


def _diag(proc: subprocess.CompletedProcess) -> str:
    """断言失败时的诊断文本。GitHub 注解只显示首行——首行带上 rc 与
    预检 [X] 失败项（哪个工件/检查没过），正文附完整 stdout，尾部附
    stderr（CI 无控制台会话的报错只在 stderr）。"""
    out = out_text(proc)
    failed_items = [ln.strip() for ln in out.splitlines()
                    if ln.strip().startswith("[X]")]
    first = f"rc={proc.returncode}"
    if failed_items:
        first += " 预检未过：" + "；".join(failed_items)[:200]
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    tail = ("\n[stderr] " + err[-600:]) if err else ""
    return first + "\n" + out + tail


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


def precheck_artifact_args(tmp: Path) -> list[str]:
    """-PrecheckOnly 断言 rc==0 的测试须自备三个数据工件（点差缓存/研究库
    缓存/合约快照）并经参数显式指向——它们只存在于实机 data/（gitignore），
    此前借开发机本地文件通过、干净克隆/CI 上预检 3 项未通过假红。"""
    data = tmp / "precheck-data"
    (data / "specs").mkdir(parents=True, exist_ok=True)
    (data / "specs" / "latest.json").write_text("{}", encoding="utf-8")
    spread = tmp / "precheck-spread.json"
    spread.write_text("{}", encoding="utf-8")
    research = tmp / "precheck-research.json"
    research.write_text("{}", encoding="utf-8")
    return ["-DataDir", str(data), "-SpreadModel", str(spread),
            "-ResearchCache", str(research)]


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
                   "-LogDir", str(log_dir), *precheck_artifact_args(tmp_path),
                   "-PrecheckOnly")
    assert proc.returncode == 0, _diag(proc)
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
                   "-LogDir", str(log_dir), *precheck_artifact_args(tmp_path),
                   "-PrecheckOnly")
    assert proc.returncode == 0, _diag(proc)
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
                   "-LogDir", str(log_dir), *precheck_artifact_args(tmp_path),
                   "-PrecheckOnly")
    assert proc.returncode == 0, _diag(proc)
    assert f"runner/{MAGIC}" not in out_text(proc)  # 不挡启动


def test_ci_powershell_behavior_probe():
    """临时诊断探针（CI 红灯定位用，绿后删除）：直接探测 CI 上
    powershell 子进程的流行为——stdout/stderr 长度与内容、Write-Host 与
    Write-Output 的去向、脚本调用的退出码传播。断言恒假，把全部信息
    写进失败消息首行（注解按行截断）。"""
    inner = ("try { [Console]::OutputEncoding=[Text.Encoding]::UTF8 } "
             "catch { }; Write-Output 'PROBE-OUT'; "
             "try { Write-Host 'PROBE-HOST' } catch { }; "
             "& " + "'" + str(SCRIPT).replace("'", "''") + "' -BogusMode")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", inner], capture_output=True, timeout=90)
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    first = (f"PROBE rc={proc.returncode} stdout[{len(out)}]="
             + out[:180].replace("\n", "⏎")
             + f" stderr[{len(err)}]=" + err[:180].replace("\n", "⏎"))
    assert False, first + "\n---full stdout---\n" + out + "\n---full stderr---\n" + err
