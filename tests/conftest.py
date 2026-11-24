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

严格模式（T206/O19-6）：设 ``MT5_STRICT_DEPS``（任何非空值，CI 用
``1``）后该跳过机制整体失效——finder 不注册；若 conftest 已在变量
置位前加载（finder 已在场），被问到时也改抛普通 ``ModuleNotFoundError``
（缺包 = 测试失败，红）。用途：CI 依赖装齐是前提，缺包静默 skip 会
掩盖安装问题；本地不设该变量，行为与上面描述完全一致。
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _pytest.outcomes import Skipped

# 严格模式开关（T206/O19-6）：任何非空值开启；CI 的 pytest job 置 1。
STRICT_DEPS_ENV = "MT5_STRICT_DEPS"

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


def _strict_deps_enabled() -> bool:
    """严格依赖模式是否开启：``MT5_STRICT_DEPS`` 置任何非空值即开。

    CI 用它关掉缺包跳过——依赖装齐是 CI 的前提，缺包必须红；本地
    不设该变量，走 T190 的惰性 skip。
    """
    return bool(os.environ.get(STRICT_DEPS_ENV))


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
    """meta_path 兜底 finder：只在可选依赖真实缺席时被问到。

    严格模式（MT5_STRICT_DEPS 置位）下被问到时抛普通
    ModuleNotFoundError 而非 Skipped——正常情况下严格模式根本不会注册
    它（见 _register_skip_finder），这里兜住"先加载 conftest 后设变量"
    的进程形态，保证严格模式下缺包永远是失败。
    """

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in OPTIONAL_HEAVY_DEPS:
            if _strict_deps_enabled():
                # 严格模式（CI）：缺包按原生 ImportError 失败（T206/O19-6）
                raise ModuleNotFoundError(
                    f"可选重依赖 {root} 未安装"
                    f"（{STRICT_DEPS_ENV} 严格模式：缺包即失败）")
            msg = f"可选重依赖 {root} 未安装（T190：缺包跳过，不算失败）"
            if _in_pandas_compat_probe():
                # pandas 的降级探测：给它普通缺席异常，import pandas 不炸
                raise ModuleNotFoundError(msg)
            raise _OptionalDependencySkipped(msg)
        return None


def _register_skip_finder() -> None:
    """幂等把兜底 finder 挂到 sys.meta_path 末位；严格模式下不挂。

    已安装时前置 PathFinder 已解析，finder 永远不会被问到。
    """
    if _strict_deps_enabled():
        return  # 严格模式（CI）：不注册，缺包走 Python 原生 ImportError
    if not any(isinstance(f, _OptionalDepSkipFinder) for f in sys.meta_path):
        sys.meta_path.append(_OptionalDepSkipFinder())


_register_skip_finder()


# ---- T209：测试默认假定交易终端进程在运行 ----------------------------------
#
# 真实的 MetaTrader5.initialize() 在终端进程不在时会把终端拉起来，终端从此是
# 调用者的子进程（11-21 的根因）。网关因此在 initialize 之前先查终端进程在不在
# （mt5gw.terminal.terminal_running，进程内探测），不在就不调 initialize。
# 测试里的 MetaTrader5 全是替身，起不了任何进程；测试的结果不应取决于这台机器上
# 恰好有没有 terminal64.exe 在运行（开发和生产的机器上就有）。所以除了标了
# real_terminal_probe 的测试，这里把缺省探针换成"在运行"。真实探针由
# tests/test_t209_terminal_guard.py 对真实进程验证。
#
# 这个替换够不到子进程：在子进程源码里构造 MT5Connection 或 MT5Gateway 的测试，
# 必须显式传 terminal_running=（见 tests/test_t110_deadline.py 和钉住这一点的
# tests/test_t209_terminal_guard.py），否则只会在有终端运行的机器上通过。

REAL_TERMINAL_PROBE_MARKER = "real_terminal_probe"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{REAL_TERMINAL_PROBE_MARKER}: keep the real in-process terminal probe (T209)")


@pytest.fixture(autouse=True)
def _terminal_assumed_running(request, monkeypatch):
    if request.node.get_closest_marker(REAL_TERMINAL_PROBE_MARKER) is not None:
        return
    from mt5gw import connection

    monkeypatch.setattr(connection, "TERMINAL_PROBE", lambda terminal_path: True)


# ---- 子进程把结果交回 Python：走文件，不走标准输出 --------------------------------
#
# 上面说测试的结果不应取决于这台机器。子进程（PowerShell 脚本、驱动脚本、别的解释器）把结构化的
# 结果交给 Python 时，写进测试临时目录里的 UTF-8 文件，不经标准输出；经标准输出传的只放 ASCII。
# 原因：Windows PowerShell 5.1 按控制台的代码页编码它被重定向的输出。CI 的机器是 437，"§"在其中
# 是字节 0x15，一个控制字符；开发和生产的机器是 65001。同一段输出，本机读出来是对的，CI 上是
# 坏的——定版前的 CI #420 就是这样：安装脚本的测试把一段含"§"的 JSON 经标准输出传回，本机通过，
# CI 上解析失败。
#
# 在子进程里先把 [Console]::OutputEncoding 设成 UTF-8 也可以（tests/test_install_terminal_keeper.py
# 的驱动脚本就是这么做的），但那要每写一个子进程都记得。PowerShell 脚本交回报告用下面的
# powershell_report()，规矩由它来守；别的子进程照同样的规矩写。

CHILD_REPORT_ENV = "MT5_CHILD_REPORT"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def powershell_report(folder, script) -> str:
    """跑一段测试自己的 PowerShell（不是被测的脚本），→ 它交回的报告文本（UTF-8）。

    script 经 -File 运行（Windows PowerShell 5.1，不加载配置）；一次性的脚本写在 folder 下。报告
    文件的路径在子进程的环境变量 MT5_CHILD_REPORT 里——folder 下的 report.txt，子进程这样写：
    [System.IO.File]::WriteAllText($env:MT5_CHILD_REPORT, $text, (New-Object System.Text.UTF8Encoding($false)))
    旧的报告先删掉：后一次没有写出报告时，读不到前一次的。子进程的退出码不是 0、或者没有写出
    报告，都按失败处理，标准输出和标准错误贴在失败信息里。标准输出不是返回值：要断言的内容
    都走报告。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "report.ps1"
    report = folder / "report.txt"
    path.write_bytes(b"\xef\xbb\xbf" + script.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    report.unlink(missing_ok=True)                 # 先删旧的：路径按目录固定
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(path)],
                          capture_output=True, timeout=120, creationflags=NO_WINDOW,
                          env={**os.environ, CHILD_REPORT_ENV: str(report)})
    shown = done.stdout.decode("utf-8", errors="replace") + done.stderr.decode("utf-8", errors="replace")
    assert done.returncode == 0, shown[-800:]
    assert report.is_file(), "the child wrote no report\n" + shown[-800:]
    return report.read_text(encoding="utf-8-sig")


# ---- T209：测试不得加载真实的 MetaTrader5 包 ----------------------------------
#
# 开发和生产的机器上装着真实的包，交易终端也常常在运行：测试里一次真实的
# import 接着一次 connect()，就连到了交易终端（D82）。替身一律显式注入——构造
# MT5Connection/MT5Gateway 时传 mt5_module=，或者把替身放进
# sys.modules["MetaTrader5"]（那样 import 根本不问 meta_path）。这里把真实包的加载
# 拦下来：包确实装着时，import 它的测试直接失败（不是 skip）；没装时不管，照旧交给
# 后面的 finder（T190 的缺包跳过）。只管本进程：子进程不加载 conftest，那一半靠
# tests/test_t209_terminal_guard.py 里的扫描和 CI。

REAL_PACKAGE_ROOT = "MetaTrader5"


class _RefusedRealPackage:
    """一加载就失败的 loader。抛 RuntimeError 而不是 ImportError：pytest.importorskip
    之类只接 ImportError，会把它变成一次不声不响的 skip。"""

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise RuntimeError(
            f"测试加载了真实的 {module.__name__}（D82：交易终端专用）。用 tests/fakes.py 的替身，"
            "构造 MT5Connection/MT5Gateway 时显式传 mt5_module=")


class _RealPackageGuard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] != REAL_PACKAGE_ROOT or path is not None:
            return None
        if importlib.machinery.PathFinder.find_spec(fullname) is None:
            return None                       # 包没装：不是这里的事
        return importlib.util.spec_from_loader(fullname, _RefusedRealPackage())


def _register_real_package_guard() -> None:
    """幂等地把守卫放在 meta_path 最前面：sys.modules 里有替身时 import 不问 meta_path，
    替身照常可用；没有替身而真实包被 import 时，先问到的是守卫。"""
    if not any(isinstance(finder, _RealPackageGuard) for finder in sys.meta_path):
        sys.meta_path.insert(0, _RealPackageGuard())


_register_real_package_guard()
