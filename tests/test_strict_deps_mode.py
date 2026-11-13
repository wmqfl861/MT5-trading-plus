"""T206 严格依赖模式（MT5_STRICT_DEPS）的钉住测试。

O19-6：CI 依赖装齐时 T190 的缺包跳过永不触发；但若 CI 环境缺包，
静默 skip 会掩盖安装问题。严格模式（CI 设 MT5_STRICT_DEPS=1）让
缺包跳过失效，缺包按失败暴露：
- 注册端：conftest 加载时该变量置位（非空）→ 兜底 finder 不注册；
- 行为端：即便 finder 已在场（如先加载 conftest 后才设变量），
  被问到也抛普通 ModuleNotFoundError（= 红），不再抛 Skipped。
本地不设变量：行为与 T190 完全一致（缺包=惰性 skip，装齐=零影响）。
"""
from __future__ import annotations

import importlib
import sys

import pytest
from _pytest.outcomes import Skipped

from tests import conftest
from tests.conftest import (
    STRICT_DEPS_ENV,
    _OptionalDependencySkipped,
    _OptionalDepSkipFinder,
    _register_skip_finder,
    _strict_deps_enabled,
)


def test_strict_env_flag_semantics(monkeypatch):
    """标志语义：未设或空串 → 关（本地默认）；任何非空值（CI 用 "1"）→ 开。"""
    monkeypatch.delenv(STRICT_DEPS_ENV, raising=False)
    assert _strict_deps_enabled() is False
    monkeypatch.setenv(STRICT_DEPS_ENV, "")
    assert _strict_deps_enabled() is False
    monkeypatch.setenv(STRICT_DEPS_ENV, "1")
    assert _strict_deps_enabled() is True


def test_strict_mode_blocks_registration(monkeypatch):
    """注册端：严格模式下 _register_skip_finder() 不往 sys.meta_path 挂
    finder（缺包走 Python 原生 ImportError）；未设变量时照常幂等挂载。
    meta_path 做快照/恢复，不影响其他测试看到的进程状态。"""
    saved = list(sys.meta_path)
    try:
        # 用类名匹配剔除既有 finder（monkeypatch 前的注册），从零开始
        sys.meta_path[:] = [f for f in saved
                            if type(f).__name__ != "_OptionalDepSkipFinder"]
        monkeypatch.setenv(STRICT_DEPS_ENV, "1")
        _register_skip_finder()
        assert not any(type(f).__name__ == "_OptionalDepSkipFinder"
                       for f in sys.meta_path), "严格模式下不得注册兜底 finder"
        monkeypatch.delenv(STRICT_DEPS_ENV)
        _register_skip_finder()
        assert any(isinstance(f, _OptionalDepSkipFinder) for f in sys.meta_path)
    finally:
        sys.meta_path[:] = saved


def test_strict_mode_finder_raises_plain_import_error(monkeypatch):
    """行为端：严格模式下被问到的 finder 抛普通 ModuleNotFoundError，
    不是 Skipped——pytest 按 error/failed（红）报告而非 skip。
    （Skipped 派生自 BaseException，接不住 ImportError 的地方也接不住它，
    类型即语义：反查 Skipped 不是 ModuleNotFoundError。）"""
    monkeypatch.setenv(STRICT_DEPS_ENV, "1")
    with pytest.raises(ModuleNotFoundError) as ei:
        _OptionalDepSkipFinder().find_spec("pyarrow")
    assert not isinstance(ei.value, Skipped)
    assert not isinstance(_OptionalDependencySkipped("x"), ModuleNotFoundError)


def test_absent_dep_strict_vs_local_end_to_end(monkeypatch):
    """端到端对比（模拟缺包：假包名注入可选清单，PathFinder 找不到 →
    落到本进程已注册的兜底 finder）：
    - 严格模式：import 抛 ModuleNotFoundError（CI 上=测试失败，红）；
    - 未设变量：import 抛 _OptionalDependencySkipped 且带模块级 skip
      语义（本地=惰性 skip）——与 T190 行为一致。"""
    strict_name = "_t206_absent_strict"
    local_name = "_t206_absent_local"
    from importlib.machinery import PathFinder
    assert PathFinder.find_spec(strict_name) is None
    assert PathFinder.find_spec(local_name) is None
    monkeypatch.setattr(conftest, "OPTIONAL_HEAVY_DEPS",
                        (strict_name, local_name))
    monkeypatch.setenv(STRICT_DEPS_ENV, "1")
    with pytest.raises(ModuleNotFoundError) as strict_exc:
        importlib.import_module(strict_name)
    assert not isinstance(strict_exc.value, Skipped)
    monkeypatch.delenv(STRICT_DEPS_ENV)
    # CI 严格进程里 finder 未注册——本地分支先恢复本地形态（幂等）再验证
    _register_skip_finder()
    with pytest.raises(_OptionalDependencySkipped) as local_exc:
        importlib.import_module(local_name)
    assert isinstance(local_exc.value, Skipped)
    assert local_exc.value.allow_module_level
