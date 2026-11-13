"""可选重依赖缺失时的跳过机制（T190：缺包 skip，而非 error/失败）。

背景：requirements.txt 声明的重依赖并非每台机器都装齐——本地缺
pyarrow 时，src/mt5data/ticks.py 的顶层 import 与 pandas to_parquet
的引擎装载曾造成数百条 collection error / 失败。

机制：把可选重依赖装进 sys.meta_path **末位**兜底 finder。它只在
"前置 finder（PathFinder 等）都没找到该包"——即真正缺席——时被问到，
此时抛 ``_OptionalDependencySkipped``（pytest 的 ``Skipped``，带
allow_module_level）：

- 收集期（模块顶层 import，如 tests/test_mt5data_ticks.py 顶层
  ``import mt5data.ticks``、tests/test_h_time_audit.py 顶层
  ``import pyarrow``）：pytest 按模块级 skip 报告——整文件 skipped；
- 运行期（测试体内 ``from mt5data.ticks import save_ticks``、
  ``df.to_parquet`` 引擎装载）：pytest 按该条测试 skipped 报告。
  pandas 的 ``import_optional_dependency`` 只 ``except ImportError``，
  接不住 ``Skipped``（OutcomeException 派生自 BaseException），
  透传到测试即 skip，无需逐测试处理。

唯一的例外：``import pandas`` 自身。pandas/compat/pyarrow.py 顶层有
兼容探测 ``try: import pyarrow … except ImportError:``（缺席则降级），
skip 异常若在这里逃逸会让所有 import pandas 的测试文件被连坐跳过。
因此当 import 请求源自该探测模块（按栈帧 ``__name__`` 精确识别）时，
改抛普通 ``ModuleNotFoundError``，让 pandas 按缺席正常降级。

机器装齐全部依赖时本机制完全惰性：finder 挂在末位，已安装的包由
PathFinder 先行解析，永远不会走到这里——行为零变化。
"""
from __future__ import annotations

import sys

from _pytest.outcomes import Skipped

# 可选重依赖（与 src/ 的顶层 import 对齐）：
# - pyarrow：src/mt5data/ticks.py 顶层 + pandas parquet 引擎（bars 全套）；
# - duckdb：src/mt5mcp/tools.py 顶层；
# - mcp：src/mt5mcp/server.py 顶层；
# - MetaTrader5：mt5gw/mt5live 的延迟导入（只有连真终端的路径才触发，
#   测试全走 FakeMT5 依赖注入，缺席不影响现有行为）。
OPTIONAL_HEAVY_DEPS: tuple[str, ...] = ("MetaTrader5", "pyarrow", "duckdb", "mcp")

# pandas 对可选依赖的降级探测点：缺席时它需要看到 ImportError。
_PANDAS_COMPAT_PROBE = "pandas.compat.pyarrow"


class _OptionalDependencySkipped(Skipped):
    """缺可选依赖时的 import 异常：pytest 按模块级/单条 skip 报告。"""

    def __init__(self, msg: str):
        Skipped.__init__(self, msg, allow_module_level=True)


def _in_pandas_compat_probe() -> bool:
    """本次 import 是否发自 pandas 的可选依赖降级探测模块。

    只在 finder 真被问到（包缺席）时才走这段栈帧检查，频率极低；
    精确匹配模块名 ``pandas.compat.pyarrow``——这是 pandas 里唯一
    "缺席属正常、必须降级"的 pyarrow import 点（import_optional_dependency
    的 errors="raise" 路径按缺席失败是对的，交给 Skipped 透传）。
    """
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_globals.get("__name__") == _PANDAS_COMPAT_PROBE:
            return True
        frame = frame.f_back
    return False


class _OptionalDepSkipFinder:
    """meta_path 兜底 finder：只在可选依赖真实缺席时被问到。"""

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in OPTIONAL_HEAVY_DEPS:
            msg = f"可选重依赖 {root} 未安装（T190：缺包跳过，不算失败）"
            if _in_pandas_compat_probe():
                # pandas 的降级探测：给它普通缺席异常，import pandas 不炸
                raise ModuleNotFoundError(msg)
            raise _OptionalDependencySkipped(msg)
        return None


# 末尾追加（幂等）：已安装时前置 PathFinder 已解析，永远走不到这里。
if not any(isinstance(f, _OptionalDepSkipFinder) for f in sys.meta_path):
    sys.meta_path.append(_OptionalDepSkipFinder())
