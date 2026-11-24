"""三个组件对看护状态文件在哪里的说法必须一致（任务 K，2026-11-24）。

安装脚本给看护任务的 -LogPath 是 <数据目录>\\ops-evidence\\terminal_keeper\\terminal_keeper.log；看护脚本把
state.json 写在日志旁边；存活检查带 --expect-keeper 时按缺省路径读它。定版之前这三个地方有两种拼写
（安装脚本和看护脚本是下划线，存活检查的缺省路径、它的说明和 runbook 是连字符），照安装脚本装好之后存活
检查永远读不到状态文件，一直报 keeper_absent——四次复核都是分开看这几个组件，没有人把它们串起来。

这里的契约测试把它们串起来：安装脚本的计划模式给出 log_path → 看护脚本照任务的动作真的跑一次（不是
-DryRun；Get-CimInstance 和 Start-Process 是只记录的替身，不启动任何进程，不查真实的进程表）→ 存活检查用
**缺省**参数读 → 读到的就是看护刚写的那一条。安装脚本在沙箱里运行（驱动先核对替身在位，见
tests/test_install_terminal_keeper.py），这份文件也先过它的静态检查。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_install_terminal_keeper import (  # noqa: F401  (the fixture is picked up by its name)
    located, parameters, run, sandbox, state_lines, text, the_installer_passes_the_static_checks)
from tests.test_keeper_state import run_keeper
from tests.test_liveness_keeper import load

ROOT = Path(__file__).resolve().parents[1]
requires_powershell = pytest.mark.skipif(shutil.which("powershell") is None, reason="requires PowerShell")
# 拼出来写，这份文件自己才不会因为提到这个拼写而不过下面那条静态检查
HYPHENATED = "terminal" + "-" + "keeper"
FOLDER = "terminal_keeper"
# 说得出这个目录的每一个文件
FILES = ("scripts/ops/install_terminal_keeper.ps1", "scripts/ops/terminal_keeper.ps1", "scripts/ops/liveness_logic.py",
         "scripts/ops/liveness_check.py", "docs/ops/demo-runbook.md", "tests/test_liveness_keeper.py",
         "tests/test_keeper_state.py", "tests/test_install_terminal_keeper.py", "tests/test_keeper_state_path.py")


def test_no_file_spells_the_folder_of_the_state_file_with_a_hyphen():
    """拼写只有一种。哪个文件里再出现连字符的写法，这条先失败——缺省路径和实际位置就是这么分家的。"""
    for name in FILES:
        assert HYPHENATED not in (ROOT / name).read_text(encoding="utf-8-sig"), name


@requires_powershell
def test_the_state_file_the_keeper_writes_is_the_one_the_checker_reads_by_default(tmp_path):
    """安装脚本说日志在哪 → 看护脚本把 state.json 写在那里 → 存活检查不带 --keeper-state 就读到它。"""
    deploy, terminal, data, modules = sandbox(tmp_path)

    # 1. 安装脚本的计划模式（不带开关）：任务会带哪个 -LogPath。不查询、不注册。
    done, _, calls = run(tmp_path, modules, *located(deploy, terminal, data))
    assert done.returncode == 0 and calls == [], text(done)
    (plan,) = parameters(done)
    log = Path(plan["log_path"])
    assert log == data / "ops-evidence" / FOLDER / "terminal_keeper.log"

    # 2. 看护脚本照任务的动作跑一次：不是 -DryRun，所以写日志和状态文件；替身不启动任何进程。
    work = tmp_path / "keeper run"
    work.mkdir()
    rc, said, _ = run_keeper(work, terminal, str(log))
    assert rc == 0 and "KEEPER started pid=4321 other_instances=0" in said and "write failed" not in said, said
    assert log.is_file() and (log.parent / "state.json").is_file()

    # 3. 存活检查：缺省路径，不带 keeper_state。
    now = datetime.now(timezone.utc)
    observed = load().observe(str(data / "live"), str(data / "reports"), str(data), now,
                              lambda pid, started_utc: True, expect_keeper=True)
    keeper = observed["keeper"]
    assert keeper["present"] is True, keeper
    assert (keeper["outcome"], keeper["reason"]) == ("started", None)
    assert keeper["age_s"] is not None and 0 <= keeper["age_s"] < 120

    # 连字符那个目录从来不存在；数据目录的 ops-evidence 下只有看护这一个目录。
    assert not (data / "ops-evidence" / HYPHENATED).exists()
    assert [path.name for path in (data / "ops-evidence").iterdir()] == [FOLDER]

    # 4. 安装脚本的 -Status 读的是同一个文件（替身说任务在；只查询）。
    done, _, calls = run(tmp_path, modules, "-Status", *located(deploy, terminal, data), exists=True)
    assert done.returncode == 0, text(done)
    (line,) = state_lines(done)
    assert line["outcome"] == "started" and isinstance(line["age_s"], int)
