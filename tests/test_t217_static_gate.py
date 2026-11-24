"""T217（第二十三轮 C23-2）：静态检查守门。

CI 原先只跑 pytest 和卫生扫描，语法错误、未定义名、没用到的导入和局部变量
都没有人查（C23-2）。这条测试在仓库根用 ``sys.executable -m ruff check .``
跑 ruff，规则集取 ``pyproject.toml`` 的 ``[tool.ruff.lint]``（E9、F401、
F821、F841），与 CI 的 lint job 读的是同一份配置。退出码必须是 0；不是 0
就把 ruff 的输出原样贴进失败信息。

本机的开发 venv 不装 ruff：导入不到时整条跳过并写明原因。CI 的严格依赖
模式（``MT5_STRICT_DEPS``，见 tests/conftest.py）下不跳过——
``requirements-dev.txt`` 钉了 ruff，缺了就是失败，不能靠跳过过关。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import STRICT_DEPS_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ruff_error_rules_are_clean():
    """`ruff check .`（规则集在 pyproject.toml）在仓库根的退出码为 0。"""
    if importlib.util.find_spec("ruff") is None:
        if os.environ.get(STRICT_DEPS_ENV):
            pytest.fail(
                f"{STRICT_DEPS_ENV} 严格模式下 ruff 必须可导入："
                "requirements-dev.txt 钉了 ruff，静态检查不能靠跳过过关")
        pytest.skip(
            "本机开发环境没有装 ruff（共享 venv 不装开发依赖）；"
            "CI 按 requirements-dev.txt 装了 ruff，这一条在 CI 上真的跑")

    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert proc.returncode == 0, (
        f"ruff check . 退出码 {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}")
