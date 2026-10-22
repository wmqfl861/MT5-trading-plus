"""与 MT5 终端的连接：串行化、懒连接、有界自动重连。

MetaTrader5 包为同步调用且非线程安全——本类是它唯一的入口，
所有调用经 call() 在同一把锁下执行，对上层呈现线程安全。
"""
import threading

from mt5gw.config import GatewayConfig
from mt5gw.errors import NotConnectedError


class MT5Connection:
    def __init__(self, config: GatewayConfig | None = None, mt5_module=None):
        self.config = config or GatewayConfig()
        if mt5_module is None:
            import MetaTrader5 as mt5_module  # 延迟导入：纯逻辑测试不触终端
        self._mt5 = mt5_module
        self._lock = threading.RLock()
        self._initialized = False

    def connect(self) -> None:
        self._ensure_initialized()

    def shutdown(self) -> None:
        with self._lock:
            if self._initialized:
                self._mt5.shutdown()
                self._initialized = False

    @property
    def connected(self) -> bool:
        return self._initialized

    def call(self, fn, *args, **kwargs):
        with self._lock:
            if not self._initialized or not self._mt5.terminal_connected():
                self._ensure_initialized()
            return fn(*args, **kwargs)

    def last_error(self) -> str:
        return str(self._mt5.last_error())

    def _ensure_initialized(self) -> None:
        ok = self._mt5.initialize(self.config.terminal_path)
        if not ok:
            self._initialized = False
            raise NotConnectedError(f"MT5 终端初始化失败：{self.last_error()}")
        self._initialized = True

    def __enter__(self) -> "MT5Connection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
