"""与 MT5 终端的连接：串行化、懒连接、有界自动重连。

MetaTrader5 包为同步调用且非线程安全——本类是它唯一的入口，
所有调用在同一把锁下执行，对上层呈现线程安全。

实测（见 README）：该 C 扩展对"单个位置参数经 *args 解包传入"的形态报
(-2, 'Unnamed arguments not allowed')，单参数调用一律用直接位置调用。
"""
from __future__ import annotations

import threading

from mt5gw.config import GatewayConfig
from mt5gw.errors import DataUnavailableError, NotConnectedError


def _is_success(error_tuple) -> bool:
    """last_error 的成功形态是 (1, 'Success')；兼容旧 fake 的 (0, 'success')。"""
    if not error_tuple:
        return False
    code = error_tuple[0]
    desc = str(error_tuple[1]).lower() if len(error_tuple) > 1 else ""
    return code in (0, 1) and "success" in desc


class MT5Connection:
    def __init__(self, config: GatewayConfig | None = None, mt5_module=None):
        self.config = config or GatewayConfig()
        if mt5_module is None:
            import MetaTrader5 as mt5_module  # 延迟导入：纯逻辑测试不触终端
        self._mt5 = mt5_module
        self._lock = threading.RLock()
        self._initialized = False

    def connect(self) -> None:
        with self._lock:
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
            self._ready_or_raise()
            if len(args) == 1 and not kwargs:
                return fn(args[0])
            return fn(*args, **kwargs)

    def call_status(self, fn, *args, **kwargs):
        """原子地执行调用并取回 last_error（None 歧义由调用方按成功码裁决）。"""
        with self._lock:
            self._ready_or_raise()
            if kwargs or len(args) != 1:
                result = fn(*args, **kwargs)
            else:
                result = fn(args[0])
            error = self._mt5.last_error()
            return result, error

    def trade_send(self, request: dict, fuse_check, login_check=None):
        """交易唯一出口：同一次持锁内完成 重连检查→保险丝→order_send。

        fuse_check(trade_mode) 在 order_send 之前执行；账户状态取不到时
        fail-closed（拒单），杜绝"账户切换竞态"与"漏调保险丝"两类绕过。
        login_check(login) 非空时同时校验账号（解锁实盘时强制钉死）。
        返回 (result, last_error)：order_send 无返回时的诊断与发送同锁原子。
        """
        with self._lock:
            self._ready_or_raise()
            account = self._mt5.account_info()
            if account is None:
                raise DataUnavailableError(
                    f"下单前无法确认账户状态，拒绝发送：{self._mt5.last_error()}"
                )
            fuse_check(account.trade_mode)
            if login_check is not None:
                login_check(account.login)
            result = self._mt5.order_send(request)  # 直接位置调用（C 扩展解包限制）
            return result, self._mt5.last_error()

    def last_error(self) -> str:
        # 同样要过锁：C 扩展非线程安全，错误路径也不能与在途调用并发
        with self._lock:
            return str(self._mt5.last_error())

    def _ensure_initialized(self) -> None:
        ok = self._mt5.initialize(self.config.terminal_path)
        if not ok:
            self._initialized = False
            raise NotConnectedError(f"MT5 终端初始化失败：{self.last_error()}")
        self._initialized = True

    def _terminal_ok(self) -> bool:
        # Python 包没有 terminal_connected()（MQL5 专有）；等价物是 terminal_info().connected
        info = self._mt5.terminal_info()
        return bool(info is not None and info.connected)

    def _ready_or_raise(self) -> None:
        if not self._initialized or not self._terminal_ok():
            self._ensure_initialized()
            # 重连成功 ≠ 交易服务器在线；仍掉线必须如实暴露（陈旧数据比无数据更危险）
            if not self._terminal_ok():
                self._initialized = False
                raise NotConnectedError(
                    f"终端已初始化但交易服务器未连接：{self.last_error()}"
                )

    def __enter__(self) -> "MT5Connection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
