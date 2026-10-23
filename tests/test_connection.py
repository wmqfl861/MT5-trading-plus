import threading

import pytest

from mt5gw.config import GatewayConfig
from mt5gw.connection import MT5Connection
from mt5gw.errors import NotConnectedError

from tests.fakes import FakeMT5, TerminalInfo


def make_conn(fake=None, **cfg):
    fake = fake or FakeMT5()
    return MT5Connection(GatewayConfig(**cfg) if cfg else GatewayConfig(), mt5_module=fake), fake


def test_call_returns_raw_result():
    conn, fake = make_conn()
    sentinel = object()
    fake.set_behavior("symbol_info_tick", value=sentinel)
    conn.connect()
    assert conn.call(fake.symbol_info_tick, "EURUSD") is sentinel


def test_reconnects_once_when_terminal_disconnected():
    conn, fake = make_conn()
    fake.terminal_info_value = TerminalInfo(connected=False, trade_allowed=False)  # 终端重启/掉线场景
    fake.initialize_result = True
    fake.set_behavior("symbol_info_tick", value=1.1)
    result = conn.call(fake.symbol_info_tick, "EURUSD")
    assert result == 1.1
    assert fake.calls("initialize") == 1   # 恰好重连一次
    assert fake.calls("symbol_info_tick") == 1  # fn 恰被调一次


def test_raises_not_connected_when_reconnect_fails():
    conn, fake = make_conn()
    fake.terminal_info_value = TerminalInfo(connected=False, trade_allowed=False)
    fake.initialize_result = False
    fake.last_error_value = (-6, "terminal is not connected")
    with pytest.raises(NotConnectedError):
        conn.call(fake.symbol_info_tick, "EURUSD")
    assert fake.calls("symbol_info_tick") == 0  # fn 从未被触达


def test_connect_failure_raises_not_connected():
    conn, fake = make_conn()
    fake.initialize_result = False
    with pytest.raises(NotConnectedError):
        conn.connect()
    assert conn.connected is False


def test_shutdown_is_idempotent():
    conn, fake = make_conn()
    conn.connect()
    conn.shutdown()
    conn.shutdown()
    assert fake.calls("shutdown") == 1
    assert conn.connected is False


def test_concurrent_calls_are_serialized():
    conn, fake = make_conn()
    fake.set_behavior("symbol_info_tick", value=1.2345, delay=0.02)
    conn.connect()

    errors = []

    def worker():
        try:
            conn.call(fake.symbol_info_tick, "EURUSD")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert fake.max_concurrent == 1  # 任意时刻至多一个调用在 mt5 侧执行


def test_last_error_is_serialized():
    conn, fake = make_conn()
    fake.set_behavior("symbol_info_tick", value=1.0, delay=0.05)
    conn.connect()
    errors = []

    def poller():
        try:
            for _ in range(10):
                conn.last_error()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=poller)
    t.start()
    conn.call(fake.symbol_info_tick, "EURUSD")
    t.join()
    assert errors == []
    assert fake.max_concurrent == 1  # last_error 与 call 互斥进入 mt5 侧


def test_reconnect_ok_but_still_disconnected_raises():  # I10：重连≠服务器在线
    conn, fake = make_conn()
    fake.terminal_info_value = TerminalInfo(connected=False, trade_allowed=False)
    fake.initialize_result = True
    fake.initialize_restores_connection = False  # initialize 成功但服务器仍掉线
    fake.set_behavior("symbol_info_tick", value=1.1)
    with pytest.raises(NotConnectedError, match="未连接"):
        conn.call(fake.symbol_info_tick, "EURUSD")
    assert fake.calls("symbol_info_tick") == 0  # 陈旧数据路径拒绝执行


def test_lazy_connect_on_first_call():
    conn, fake = make_conn()
    fake.set_behavior("symbol_info_tick", value=2.0)
    assert conn.call(fake.symbol_info_tick, "EURUSD") == 2.0  # 未显式 connect 也自动初始化
    assert fake.calls("initialize") == 1
