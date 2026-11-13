"""T201（第十九轮 O19-1/D87；计划 docs/handoff/2026-11-13-round19-plan.md §2.1）：
网关重连修复。

背景（11-10 / 11-13 取证）：终端进程在线、只是与交易服务器断开时，T141
重连路径调 mt5.initialize()，且不带 MT5 自己的 timeout（官方缺省 60 秒），
挂 66 秒撞 T110 的 30 秒期限 → rc=75 有界退出，三进程全灭。修复后：

1. IPC 通、服务器断（terminal_info 有对象、connected=False）：零次
   initialize、抛 NotConnectedError（原因写明等待终端自行重连）、退避推进；
2. 随后服务器恢复：重连通过且不调 initialize、退避重置；
3. IPC 不通（terminal_info 返回 None）：恰一次 initialize，假模块收到
   timeout 关键字（毫秒整数，≤ query_timeout_s×1000−10000）；
4. 启动 connect() 的 initialize 同样带 timeout；
5. 配置 initialize_timeout_ms：缺省 15000，边界 1000 与上界均合法、
   越界/类型不符拒绝。

本文件用自带假 MT5 模块（ipc 开关区分"终端进程不在"），不复用
tests/fakes.py 的 FakeMT5——后者 terminal_info 恒返回对象，表达不了
IPC 不通的真实包形态。
"""
import json
from types import SimpleNamespace

import pytest

from mt5gw.config import GatewayConfig, load
from mt5gw.connection import MT5Connection, RECONNECT_BACKOFF_S
from mt5gw.errors import NotConnectedError


class Mono:
    """可推进的单调钟（同 test_t141_disconnect 的 _Mono）。"""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeMT5Module:
    """T201 假 MT5：记录 (名, args, kwargs)；ipc=False 表示终端进程不在
    （terminal_info 返回 None、initialize 失败）——对齐真实包形态。"""

    def __init__(self):
        self.calls = []
        self.ipc = True
        self.server_connected = True

    def initialize(self, *args, **kwargs):
        self.calls.append(("initialize", args, kwargs))
        return bool(self.ipc)

    def terminal_info(self):
        self.calls.append(("terminal_info", (), {}))
        if not self.ipc:
            return None
        return SimpleNamespace(connected=self.server_connected)

    def last_error(self):
        if not self.ipc:
            return (-10004, "No IPC connection")
        if not self.server_connected:
            return (-6, "terminal is not connected")
        return (1, "Success")

    def shutdown(self):
        self.calls.append(("shutdown", (), {}))
        return True

    def symbol_info_tick(self, symbol):
        self.calls.append(("symbol_info_tick", (symbol,), {}))
        return 1.2345

    def of(self, name):
        return [c for c in self.calls if c[0] == name]


def make_conn(fake, mono):
    return MT5Connection(GatewayConfig(), mt5_module=fake, monotonic=mono)


def test_t201_terminal_alive_server_down_never_initializes():
    """①IPC 通+服务器断：零次 initialize、抛 NotConnectedError、退避推进、
    _initialized 保持不变（11-10 形态）。"""
    fake, mono = FakeMT5Module(), Mono()
    conn = make_conn(fake, mono)
    conn.connect()
    startup = len(fake.of("initialize"))
    assert startup == 1
    fake.server_connected = False        # 终端在线、交易服务器断开
    with pytest.raises(NotConnectedError, match="等待终端自行重连"):
        conn.call(fake.symbol_info_tick, "EURUSD")
    assert len(fake.of("initialize")) == startup          # 零次新增 initialize
    assert conn._initialized is True                      # 保持不变
    assert conn._reconnect_failures == 1                  # 退避推进
    assert conn._next_reconnect_at == mono.t + RECONNECT_BACKOFF_S[0]
    assert fake.of("symbol_info_tick") == []              # fn 从未被触达


def test_t201_server_recovery_reconnects_without_initialize_and_resets_backoff():
    """②随后服务器恢复：到点的重连尝试探活即成功——通过、不调 initialize、
    退避重置；窗口未到点时普通调用也照常通过并重置退避。"""
    fake, mono = FakeMT5Module(), Mono()
    conn = make_conn(fake, mono)
    conn.connect()
    fake.server_connected = False
    with pytest.raises(NotConnectedError):
        conn.call(fake.symbol_info_tick, "EURUSD")        # 第 1 次尝试失败
    assert conn._reconnect_failures == 1
    mono.advance(RECONNECT_BACKOFF_S[0])                  # 到点
    fake.server_connected = True                          # 终端自行重连成功
    assert conn.reconnect() is True
    assert len(fake.of("initialize")) == 1                # 不调 initialize
    assert conn._reconnect_failures == 0
    assert conn._next_reconnect_at == 0.0
    assert conn.call(fake.symbol_info_tick, "EURUSD") == 1.2345
    # 再次断线后退避中恢复：普通调用不等退避到点即通过，退避一并重置
    fake.server_connected = False
    with pytest.raises(NotConnectedError):
        conn.call(fake.symbol_info_tick, "EURUSD")
    assert conn._reconnect_failures == 1
    fake.server_connected = True
    assert conn.call(fake.symbol_info_tick, "EURUSD") == 1.2345
    assert conn._reconnect_failures == 0 and conn._next_reconnect_at == 0.0
    assert len(fake.of("initialize")) == 1                # 全程零新增 initialize


def test_t201_ipc_dead_initializes_once_with_mt5_timeout():
    """③IPC 不通（终端进程不在）：恰一次 initialize，假模块收到 timeout
    关键字（毫秒整数，≤ query_timeout_s×1000−10000）；_initialized 置假、
    退避推进。"""
    fake, mono = FakeMT5Module(), Mono()
    conn = make_conn(fake, mono)
    conn.connect()
    fake.ipc = False                     # terminal_info → None
    with pytest.raises(NotConnectedError):
        conn.call(fake.symbol_info_tick, "EURUSD")
    inits = fake.of("initialize")
    assert len(inits) == 2               # 启动 1 次 + 本次恰 1 次
    upper = int(GatewayConfig().query_timeout_s * 1000) - 10_000
    timeout = inits[-1][2].get("timeout")
    assert isinstance(timeout, int) and 1000 <= timeout <= upper
    assert inits[-1][1] == (GatewayConfig().terminal_path,)   # 路径仍传
    assert conn._initialized is False    # IPC 不通：置假
    assert conn._reconnect_failures == 1


def test_t201_connect_startup_initialize_carries_timeout():
    """④启动 connect() 的 initialize 也带 MT5 timeout（缺省 15000 毫秒，
    显式配置同样透传）。"""
    fake = FakeMT5Module()
    conn = MT5Connection(GatewayConfig(), mt5_module=fake)
    conn.connect()
    inits = fake.of("initialize")
    assert len(inits) == 1
    assert inits[0][1] == (GatewayConfig().terminal_path,)
    assert inits[0][2].get("timeout") == 15000
    assert conn.connected is True
    fake2 = FakeMT5Module()
    MT5Connection(GatewayConfig(initialize_timeout_ms=10000),
                  mt5_module=fake2).connect()
    assert fake2.of("initialize")[0][2].get("timeout") == 10000


def test_t201_initialize_timeout_ms_config_validation(tmp_path):
    """⑤配置校验：缺省 15000；边界 1000 与 20000（30 秒期限−10 秒）合法；
    越界/非整数/布尔拒绝；上界随 query_timeout_s 联动；亚秒级期限的测试
    配置仍可构造；JSON 装载同一条底线。"""
    assert GatewayConfig().initialize_timeout_ms == 15000
    assert GatewayConfig(initialize_timeout_ms=1000).initialize_timeout_ms == 1000
    assert GatewayConfig(initialize_timeout_ms=20000).initialize_timeout_ms == 20000
    with pytest.raises(ValueError, match="initialize_timeout_ms"):
        GatewayConfig(initialize_timeout_ms=999)
    with pytest.raises(ValueError, match="initialize_timeout_ms"):
        GatewayConfig(initialize_timeout_ms=20001)
    with pytest.raises(ValueError, match="initialize_timeout_ms"):
        GatewayConfig(initialize_timeout_ms=15000.0)      # 必须整数毫秒
    with pytest.raises(ValueError, match="initialize_timeout_ms"):
        GatewayConfig(initialize_timeout_ms=True)         # 布尔拒绝
    # 上界联动：40 秒期限 → 30000 合法；20 秒期限 → 缺省 15000 越界
    assert GatewayConfig(query_timeout_s=40.0,
                         initialize_timeout_ms=30000).initialize_timeout_ms == 30000
    with pytest.raises(ValueError, match="initialize_timeout_ms"):
        GatewayConfig(query_timeout_s=20.0)
    # T110 期限测试的亚秒级 query_timeout_s 仍可构造（上界对其不适用）
    GatewayConfig(query_timeout_s=0.15, order_timeout_s=0.2)
    # JSON 装载同一条底线
    p = tmp_path / "gw.json"
    p.write_text(json.dumps({"initialize_timeout_ms": 999}), encoding="utf-8")
    with pytest.raises(ValueError, match="initialize_timeout_ms"):
        load(str(p))
    p.write_text(json.dumps({"initialize_timeout_ms": "15000"}), encoding="utf-8")
    with pytest.raises(ValueError, match="initialize_timeout_ms"):
        load(str(p))
    p.write_text(json.dumps({"initialize_timeout_ms": 12000}), encoding="utf-8")
    assert load(str(p)).initialize_timeout_ms == 12000
