"""T190 缺包跳过机制（tests/conftest.py）的单元与惰性验证。

本机装齐全部依赖，这里验证机制的两端：
- 惰性端：已安装时 import 直接成功、finder 不介入（行为零变化）；
- 生效端：依赖真实缺席时，import 抛 Skipped（pytest 报 skipped）；
  唯独 pandas 的降级探测（pandas/compat/pyarrow.py）看到普通
  ModuleNotFoundError，import pandas 照常降级、不连坐别的测试。
端到端（缺 pyarrow/duckdb/mcp 的整文件跳过、退出码 0）由外部模拟
验证：把包从 PathFinder 可见范围屏蔽后跑 pytest。
"""
from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest
from _pytest.outcomes import Skipped

from tests.conftest import (
    OPTIONAL_HEAVY_DEPS,
    _OptionalDependencySkipped,
    _OptionalDepSkipFinder,
    _in_pandas_compat_probe,
)


def test_all_optional_deps_installed_and_finder_inert():
    """本机（全依赖环境）机制必须完全惰性：import 直接成功。"""
    for name in OPTIONAL_HEAVY_DEPS:
        assert importlib.util.find_spec(name) is not None, (
            f"本机应装有 {name}（T190 的前提：装齐时零影响）")
    import pyarrow  # noqa: F401  已安装时正常导入（兜底 finder 不介入）
    assert _OptionalDepSkipFinder().find_spec("json") is None  # 非可选依赖


def test_finder_raises_for_optional_dep_names_by_contract():
    """兜底 finder 的契约：被问到（= 前面 finder 都没找到）即抛 skip。

    它不检查安装状态——正确性来自挂在 meta_path 末位；直接用可选依赖
    名调用必然抛 _OptionalDependencySkipped（含已安装的场景，此时正常
    import 流程根本不会问到它）。"""
    finder = _OptionalDepSkipFinder()
    with pytest.raises(_OptionalDependencySkipped):
        finder.find_spec("pyarrow")
    with pytest.raises(_OptionalDependencySkipped):
        finder.find_spec("duckdb.sub.mod")   # 子模块按根名命中


def test_finder_registered_in_meta_path():
    """兜底 finder 已挂在 sys.meta_path（末位）。"""
    assert any(isinstance(f, _OptionalDepSkipFinder) for f in sys.meta_path)


def test_absent_dep_import_raises_skip_import(monkeypatch):
    """真实缺席的包：import 抛 _OptionalDependencySkipped——是 Skipped
    且带模块级 skip 语义（收集期 → 整文件 skipped），并验证子模块按
    根名命中。前提检查用 PathFinder：importlib.util.find_spec 会走
    meta_path 问到兜底 finder 自己。"""
    absent = "_t190_absent_pkg_for_test"
    monkeypatch.setattr("tests.conftest.OPTIONAL_HEAVY_DEPS", (absent,))
    from importlib.machinery import PathFinder
    assert PathFinder.find_spec(absent) is None
    with pytest.raises(_OptionalDependencySkipped) as ei:
        importlib.import_module(absent + ".sub")
    assert isinstance(ei.value, Skipped)
    assert ei.value.allow_module_level


def test_pandas_compat_probe_detected_and_gets_import_error():
    """pandas 降级探测例外：源自 pandas/compat/pyarrow.py 的 import
    得到普通 ModuleNotFoundError（pandas 的 except ImportError 接得住，
    import pandas 降级不炸）；其他来源（如本测试）不会被误判。"""
    assert _in_pandas_compat_probe() is False   # 本测试模块不在探测点内
    probe_ns = {
        "__name__": "pandas.compat.pyarrow",
        "_in_pandas_compat_probe": _in_pandas_compat_probe,
        "_OptionalDepSkipFinder": _OptionalDepSkipFinder,
        "ModuleNotFoundError": ModuleNotFoundError,
        "_OptionalDependencySkipped": _OptionalDependencySkipped,
    }
    exec(
        "def _call():\n"
        "    return _in_pandas_compat_probe()\n"
        "def _find():\n"
        "    return _OptionalDepSkipFinder().find_spec('pyarrow')\n"
        "probe_hit = _call()\n"
        "try:\n"
        "    _find()\n"
        "    raised = None\n"
        "except ModuleNotFoundError as e:\n"
        "    raised = e\n",
        probe_ns)
    assert probe_ns["probe_hit"] is True
    assert isinstance(probe_ns["raised"], ModuleNotFoundError)
    assert not isinstance(probe_ns["raised"], _OptionalDependencySkipped)
