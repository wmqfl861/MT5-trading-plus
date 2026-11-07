"""Serialized MT5 access with terminal deadlines (T110).

A deadline includes lock contention and every raw API entry. The dedicated
worker cannot be joined after a timeout: native IPC may never return. Evidence
is flushed before os._exit(75); an external supervisor covers native GIL hangs.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import datetime, timezone

from mt5gw.config import GatewayConfig
from mt5gw.errors import DataUnavailableError, NotConnectedError
from mt5gw.snapshot import write_json_atomic

_TIMEOUT_EVIDENCE = {}
_EVIDENCE_LOCK = threading.Lock()
_FATAL_LOCK = threading.Lock()
_TERMINATING = threading.Event()

# T141（第十六轮计划 §2）：网关内有界重连的退避序列（秒）——指数退避
# 5、10、30、60，此后封顶 60。等待在锁外、由调用方按轮询节奏驱动
# （reconnect()/隐式重连到点才真试）；每次尝试本身走 T110 期限
# （_serialized/_invoke 的既有超时——本任务不改期限常量）。
RECONNECT_BACKOFF_S = (5.0, 10.0, 30.0, 60.0)


def _await_terminal_exit():
    if _TERMINATING.is_set():
        # Another thread is persisting unknown/markers. Never overtake it with
        # an order, a final journal status or an early os._exit.
        threading.Event().wait()


@contextmanager
def timeout_evidence(callback):
    """Register in-flight evidence process-wide, across connections and threads."""
    token = object()
    with _EVIDENCE_LOCK:
        _await_terminal_exit()
        _TIMEOUT_EVIDENCE[token] = callback
    try:
        yield
    finally:
        with _EVIDENCE_LOCK:
            terminating = _TERMINATING.is_set()
            if not terminating:
                _TIMEOUT_EVIDENCE.pop(token, None)
        if terminating:
            _await_terminal_exit()


def _is_success(error_tuple) -> bool:
    if not error_tuple:
        return False
    code = error_tuple[0]
    desc = str(error_tuple[1]).lower() if len(error_tuple) > 1 else ""
    return code in (0, 1) and "success" in desc


class MT5Connection:
    def __init__(self, config: GatewayConfig | None = None, mt5_module=None,
                 monotonic=time.monotonic):
        self.config = config or GatewayConfig()
        if mt5_module is None:
            import MetaTrader5 as mt5_module
        self._mt5 = mt5_module
        self._lock = threading.RLock()
        self._initialized = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5-deadline")
        # T141：有界重连的退避状态（单调钟可注入，供测试虚拟时间）。
        # 读（_reconnect_due）在锁外、写在 _attempt_reconnect 的锁内——
        # 最坏竞态是多一次 initialize 尝试，无正确性影响。
        self._monotonic = monotonic
        self._reconnect_failures = 0
        self._next_reconnect_at = 0.0

    def _fatal_timeout(self, name: str, started: float, timeout: float):
        if not _FATAL_LOCK.acquire(blocking=False):
            threading.Event().wait()  # the first failure owns evidence and exit
        # Evidence I/O must not leave a stuck process indefinitely either. This
        # timer also needs the GIL; native GIL stalls require the supervisor.
        killer = threading.Timer(5.0, lambda: os._exit(75))
        killer.daemon = True
        killer.start()
        record = {"rc": 75, "reason": "gateway_deadline", "call": name,
                  "duration_s": time.monotonic() - started, "timeout_s": timeout,
                  "ts": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}
        logging.critical("MT5 deadline call=%s duration=%.3fs limit=%.3fs rc=75",
                         name, record["duration_s"], timeout)
        with _EVIDENCE_LOCK:
            _TERMINATING.set()
            callbacks = tuple(_TIMEOUT_EVIDENCE.values())
        for callback in callbacks:
            try:
                callback(record)
            except BaseException:
                logging.critical("Cannot persist execution unknown", exc_info=True)
        try:
            live = os.environ.get("MT5_LIVE_DIR", os.path.join("data", "live"))
            write_json_atomic(os.path.join(live, "gateway-stall.json"), record)
            component = os.environ.get("MT5_COMPONENT", "gateway")
            write_json_atomic(os.path.join(live, f"exit-{component}.json"), record)
        except BaseException:
            logging.critical("Cannot persist deadline marker", exc_info=True)
        finally:
            for stream in (sys.stderr, sys.stdout):
                try:
                    stream.flush()
                except Exception:
                    pass
            os._exit(75)  # never wait for ThreadPoolExecutor's atexit join

    @contextmanager
    def _serialized(self, name, timeout=None):
        _await_terminal_exit()
        timeout = self.config.query_timeout_s if timeout is None else timeout
        started = time.monotonic()
        if not self._lock.acquire(timeout=timeout):
            self._fatal_timeout(f"lock:{name}", started, timeout)
        try:
            yield
        finally:
            self._lock.release()

    def _invoke(self, name, fn, *args, timeout=None, **kwargs):
        _await_terminal_exit()
        timeout = self.config.query_timeout_s if timeout is None else timeout
        started = time.monotonic()
        # Direct one-position invocation preserves the C extension's ABI quirk.
        def invoke():
            if len(args) == 1 and not kwargs:
                return fn(args[0])
            return fn(*args, **kwargs)
        future = self._executor.submit(invoke)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            # A function may itself raise TimeoutError. That is not a stuck IPC.
            if future.done():
                return future.result()
            self._fatal_timeout(name, started, timeout)

    def _raw(self, name, *args, **kwargs):
        return self._invoke(name, getattr(self._mt5, name), *args, **kwargs)

    def connect(self) -> None:
        with self._serialized("initialize"):
            self._ensure_initialized()

    def shutdown(self) -> None:
        with self._serialized("shutdown"):
            if self._initialized:
                self._raw("shutdown")
                self._initialized = False

    @property
    def connected(self) -> bool:
        return self._initialized

    def call(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", "call")
        with self._serialized(name):
            self._ready_or_raise()
            return self._invoke(name, fn, *args, **kwargs)

    def call_status(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", "call_status")
        with self._serialized(name):
            self._ready_or_raise()
            result = self._invoke(name, fn, *args, **kwargs)
            return result, self._raw("last_error")

    def trade_send(self, request: dict, fuse_check, login_check=None):
        # This entire transaction retains one lock: no interleaved reconnect,
        # account check, fuse or order is allowed between these operations.
        with self._serialized("order_send", self.config.order_timeout_s):
            self._ready_or_raise()
            account = self._raw("account_info")
            if account is None:
                raise DataUnavailableError(
                    f"下单前无法确认账户状态，拒绝发送：{self._raw('last_error')}")
            fuse_check(account.trade_mode)
            if login_check is not None:
                login_check(account.login)
            result = self._raw("order_send", request, timeout=self.config.order_timeout_s)
            return result, self._raw("last_error")

    def last_error(self) -> str:
        with self._serialized("last_error"):
            return str(self._raw("last_error"))

    def _ensure_initialized(self) -> None:
        ok = self._raw("initialize", self.config.terminal_path)
        if not ok:
            self._initialized = False
            raise NotConnectedError(f"MT5 终端初始化失败：{self.last_error()}")
        self._initialized = True

    def _terminal_ok(self) -> bool:
        info = self._raw("terminal_info")
        return bool(info is not None and info.connected)

    # ---- T141：网关内有界重连（指数退避 5/10/30/60，封顶 60） ----

    def _reconnect_due(self) -> bool:
        return self._monotonic() >= self._next_reconnect_at

    def _attempt_reconnect(self) -> tuple[bool, str]:
        """一次重连尝试（受 T110 期限约束：_serialized/_invoke 既有超时）。

        成功 → 重置退避并返回 (True, "")；失败 → 推进退避计数、安排下次
        到点，返回 (False, 供上抛/日志的原因文案)。
        """
        try:
            with self._serialized("reconnect"):
                self._ensure_initialized()
                if self._terminal_ok():
                    self._reconnect_failures = 0
                    self._next_reconnect_at = 0.0
                    return True, ""
                self._initialized = False
                reason = f"终端已初始化但交易服务器未连接：{self.last_error()}"
        except NotConnectedError as exc:
            reason = str(exc)
        self._reconnect_failures += 1
        delay = RECONNECT_BACKOFF_S[
            min(self._reconnect_failures - 1, len(RECONNECT_BACKOFF_S) - 1)]
        self._next_reconnect_at = self._monotonic() + delay
        return False, reason

    def reconnect(self) -> bool:
        """T141 有界重连的到点单次尝试：退避窗口内不触终端（防逐调用砸
        initialize），到点即试一次。返回是否已连上。

        调用方（风控/对账/运行器的轮询循环）按自己的间隔反复调用即可得到
        5/10/30/60（封顶 60）的重连节奏；连续断线的总容忍（15 分钟专用
        退出码）由进程层把关，不在网关层。
        """
        if not self._reconnect_due():
            return False
        ok, _ = self._attempt_reconnect()
        return ok

    def _ready_or_raise(self) -> None:
        if not self._initialized or not self._terminal_ok():
            if not self._reconnect_due():
                # T141：退避窗口内不再重复 initialize——立即如实抛断线，
                # 调用方（执行器/运行器）按 blocked_broker_unavailable 降级。
                wait = self._next_reconnect_at - self._monotonic()
                raise NotConnectedError(
                    f"终端与交易服务器断开，重连退避中（约 {max(wait, 0.0):.0f} "
                    f"秒后再试）：{self.last_error()}")
            ok, reason = self._attempt_reconnect()
            if not ok:
                raise NotConnectedError(reason)

    def __enter__(self) -> "MT5Connection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown()
