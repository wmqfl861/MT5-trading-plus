"""T217（第二十三轮 C23-2）：静态检查守门。

CI 原先只跑 pytest 和卫生扫描，语法错误、未定义名、没用到的导入和局部变量
都没有人查（C23-2）。这条测试在仓库根用 ``sys.executable -m ruff check .``
跑 ruff，规则集取 ``pyproject.toml`` 的 ``[tool.ruff.lint]``（E9、F401、
F821、F841），与 CI 的 lint job 读的是同一份配置。退出码必须是 0；不是 0
就把 ruff 的输出原样贴进失败信息。

门自己灵不灵敏也有人查（T217 评审的建议 J-1、J-2）：

- ``pyproject.toml`` 里选中的规则恰好是上面四条。把 ``select`` 改空，违规
  就成了 ``All checks passed!``，此前套件里没有东西会发现。
- 四条规则各自真的会报：四类违规各一处的四个小文件，配上仓库的
  ``pyproject.toml`` 跑 ruff，四类都要报出来。
- 被 per-file-ignores 豁免的 5 个研究侧文件不带项目的忽略单跑，结果恰好是
  已知的 6 处。这些文件里再出现死导入，这里失败。

本机的开发 venv 不装 ruff：导入不到时整条跳过并写明原因。CI 的严格依赖
模式（``MT5_STRICT_DEPS``，见 tests/conftest.py）下不跳过——
``requirements-dev.txt`` 钉了 ruff，缺了就是失败，不能靠跳过过关。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.conftest import STRICT_DEPS_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
RULES = ("E9", "F401", "F821", "F841")
# ruff 卡住时让这几条自己超时失败，看到的是超时，不是等到 CI 的 job 超时
# （T217 评审的建议 J-4）。全仓一遍本机一两秒。
RUFF_TIMEOUT = 300

# 四类违规各一处，一个文件一处：文件名 → （内容, 这个文件必须报出的规则）。语法错误在
# ruff 0.17.6 里不带规则码，标签是 invalid-syntax。规则是 None 的那个必须不被报：裸 except 是
# E722，不在这四条里。ruff 不带配置时的缺省规则会报它，而缺省规则同样会报上面四类——它不被报，
# 才说明跑的是仓库的规则集，不是 ruff 的缺省规则。
VIOLATIONS = {
    "unused_import.py": ("import os\nimport sys\n\nprint(sys.argv)\n", "F401"),
    "unused_variable.py": ("def f():\n    unused = 1\n    return 2\n", "F841"),
    "undefined_name.py": ("def f():\n    return not_defined_anywhere\n", "F821"),
    "syntax_error.py": ("def f(:\n    pass\n", "invalid-syntax"),
    "bare_except.py": ("try:\n    pass\nexcept:\n    pass\n", None),
}

# pyproject.toml 的 per-file-ignores：研究侧的 5 个文件，各豁免一条规则（理由写在那里；文件本身
# 一个字节不动）。豁免的是"这个文件里的这条规则整体不查"，所以另外钉一个基线。
EXEMPTED = {
    "src/mt5quant/validation/prob_mode.py": "F841",
    "src/mt5quant/strategies/library/prob/level_decision.py": "F401",
    "src/mt5quant/strategies/library/b6_vol_target_long.py": "F401",
    "src/mt5quant/strategies/library/prob/situations.py": "F401",
    "src/mt5quant/validation/events.py": "F401",
}
# 这 5 个文件不带豁免单跑时已知的发现：(文件, 规则)，按出现次数计，共 6 处（events.py 里两处）。
KNOWN_FINDINGS = sorted([(path, rule) for path, rule in EXEMPTED.items()]
                        + [("src/mt5quant/validation/events.py", "F401")])


def lint_settings() -> dict:
    """pyproject.toml 的 [tool.ruff.lint]，规则集只定在这一处。"""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]


def require_ruff() -> None:
    """ruff 可导入才往下走。导入不到：严格模式下失败，否则跳过并写明原因（见模块说明）。"""
    if importlib.util.find_spec("ruff") is None:
        if os.environ.get(STRICT_DEPS_ENV):
            pytest.fail(
                f"{STRICT_DEPS_ENV} 严格模式下 ruff 必须可导入："
                "requirements-dev.txt 钉了 ruff，静态检查不能靠跳过过关")
        pytest.skip(
            "本机开发环境没有装 ruff（共享 venv 不装开发依赖）；"
            "CI 按 requirements-dev.txt 装了 ruff，这一条在 CI 上真的跑")


def run_ruff(args, cwd=REPO_ROOT) -> subprocess.CompletedProcess:
    """在 cwd 跑 ruff check（规则集取 cwd 下的 pyproject.toml），输出原样带回。"""
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=RUFF_TIMEOUT,
    )


def findings(args, cwd=REPO_ROOT) -> list[dict]:
    """ruff 的发现（JSON 输出，一处一个字典：filename、code……）。退出码不是 0（没有发现）也不是
    1（有发现）就失败：那是 ruff 自己出了错。"""
    proc = run_ruff([*args, "--output-format", "json"], cwd)
    assert proc.returncode in (0, 1), (
        f"ruff 退出码 {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_ruff_error_rules_are_clean():
    """`ruff check .`（规则集在 pyproject.toml）在仓库根的退出码为 0。"""
    require_ruff()
    proc = run_ruff(["."])
    assert proc.returncode == 0, (
        f"ruff check . 退出码 {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}")


def test_the_rule_set_is_exactly_the_four_error_rules():
    """门的灵敏度全系于 pyproject.toml 不被动：把 select 改空，违规就成了 All checks passed!，
    没有别的测试会发现（T217 评审的建议 J-1）。[tool.ruff.lint] 选中的规则必须恰好是 E9、F401、
    F821、F841——select 里的，加上 extend-select 的，减去 ignore、extend-ignore 的。"""
    lint = lint_settings()
    chosen = ((set(lint.get("select", [])) | set(lint.get("extend-select", [])))
              - set(lint.get("ignore", [])) - set(lint.get("extend-ignore", [])))
    assert sorted(chosen) == sorted(RULES), lint


def test_each_of_the_four_rules_really_reports(tmp_path):
    """四条规则各自真的会报：没用的导入、赋值后没用的局部变量、没定义的名字、语法错误，配上仓库
    的 pyproject.toml 跑 ruff，四类都要报出来，各报各的；不在这四条里的不报。规则集被改空、被
    ignore 减掉，或者跑的根本不是这份配置，这里失败（T217 评审的建议 J-1）。"""
    require_ruff()
    for name, (source, _) in VIOLATIONS.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    shutil.copyfile(PYPROJECT, tmp_path / "pyproject.toml")
    reported = {(Path(item["filename"]).name, item["code"]) for item in findings(["."], cwd=tmp_path)}
    assert reported == {(name, rule) for name, (_, rule) in VIOLATIONS.items() if rule}, reported


def test_the_exempt_research_files_hold_exactly_their_known_six():
    """per-file-ignores 是"这个文件里的这条规则整体不查"，这 5 个文件里再多出死导入也不会有人
    知道（T217 评审的建议 J-2）。豁免的清单必须还是这 5 个文件、各一条规则；它们不带项目的忽略
    单跑（--isolated，规则集显式给同样的四条），发现必须恰好是已知的 6 处，按"文件 + 规则"计：
    多一处、少一处都失败。"""
    require_ruff()
    exempt = {path: rules for path, rules in lint_settings().get("per-file-ignores", {}).items()}
    assert exempt == {path: [rule] for path, rule in EXEMPTED.items()}, exempt
    found = sorted((os.path.relpath(item["filename"], REPO_ROOT).replace(os.sep, "/"), item["code"])
                   for item in findings(["--isolated", "--select", ",".join(RULES), *EXEMPTED]))
    assert found == KNOWN_FINDINGS, found
