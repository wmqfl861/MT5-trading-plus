"""任务 L（2026-11-25）：退避窗口内抛的断线也带上最近一次到点重连失败的原因。

存活检查判"运行器连上之后终端没了"，靠的是运行器心跳 last_error 里的"MT5 终端进程不在"
（scripts/ops/liveness_logic.py 的 _terminal_wait）。这句话原来只在到点重连失败的那一轮出现
（MT5Connection._attempt_reconnect）；退避窗口内 _ready_or_raise 抛的是固定文案加 MT5 自己的
last_error，不带原因。终端进程消失之后按 2 秒一轮，91 轮里认得出的只有 6 轮，稳态每 60 秒一轮——
小时级的采样多半看不到。

真实的 MT5Connection 加替身模块（tests/test_t201_reconnect_fix.py 的 FakeMT5Module）：不 import
MetaTrader5，不连接终端；终端进程在不在的探针和时钟都是注入的。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mt5gw.config import GatewayConfig
from mt5gw.connection import MT5Connection, RECONNECT_BACKOFF_S
from mt5gw.errors import NotConnectedError
from tests.test_liveness_keeper import load
from tests.test_t201_reconnect_fix import FakeMT5Module, Mono

ROOT = Path(__file__).resolve().parents[1]
LOST = "MT5 终端进程不在"                      # 网关在终端进程不在时的报错原文
KIND = "TerminalNotConnectedError"             # 它的异常类名，要一路带到心跳和告警里
IN_WINDOW = "重连退避中"
# 运行器把断线异常写进心跳 last_error 的两种写法（scripts/live_runner.py 的两处断线分支）
RUNNER_FORMS = (f'error = f"NotConnectedError: {{exc}}"', f'error = f"broker_unavailable: NotConnectedError: {{exc}}"')


def connected(fake, mono, running):
    """一个连上了的网关；探针按 running（可变的 holding list）回答终端进程在不在。"""
    return MT5Connection(GatewayConfig(), mt5_module=fake, monotonic=mono,
                         terminal_running=lambda path: running[0])


def rounds(connection, fake, mono, count, step=2.0):
    """像运行器那样每 step 秒一轮；→ 每一轮抛出来的断线异常。"""
    errors = []
    for _ in range(count):
        mono.advance(step)
        with pytest.raises(NotConnectedError) as caught:
            connection.call(fake.symbol_info_tick, "EURUSD")
        errors.append(caught.value)
    return errors


def gone_for_three_minutes():
    """连上之后终端进程消失（探针报不在、IPC 不通），之后 91 轮、每轮 2 秒——跨过 5、10、30、60 四档退避。
    → (每一轮的异常, 网关, 替身模块)。"""
    fake, mono, running = FakeMT5Module(), Mono(), [True]
    connection = connected(fake, mono, running)
    connection.connect()
    running[0], fake.ipc = False, False
    return rounds(connection, fake, mono, 91), connection, fake


def test_every_round_names_the_missing_terminal_process_once_it_is_gone():
    """窗口内的每一轮（91 轮里的 85 轮）也带上"MT5 终端进程不在"和 TerminalNotConnectedError，和到点
    重连失败的那 6 轮一样。"重连退避中（约 N 秒后再试）"的字样保留；窗口内照样不碰 initialize。"""
    errors, _, fake = gone_for_three_minutes()
    assert RECONNECT_BACKOFF_S == (5.0, 10.0, 30.0, 60.0)
    in_window = [error for error in errors if IN_WINDOW in str(error)]
    at_due = [error for error in errors if IN_WINDOW not in str(error)]
    assert (len(in_window), len(at_due)) == (85, 6)            # 到点的是第 1、4、9、24、54、84 轮
    for error in errors:
        text = str(error)
        assert LOST in text and KIND in text, text
    for error in in_window:
        assert "秒后再试）：" in str(error) and str(error).startswith("终端与交易服务器断开，重连退避中（约 "), str(error)
    assert len(fake.of("initialize")) == 1                     # 只有连上时那一次：终端进程不在，到点重连也
    #   不调 initialize（T209），窗口内更不调


def test_a_recovered_connection_does_not_carry_the_old_reason_forward():
    """终端回来、重连成功之后不再留旧原因；接着是"终端在、交易服务器没连上"：到点的和窗口内的文字
    带的都是这一类原因，不是"终端进程不在"。"""
    fake, mono, running = FakeMT5Module(), Mono(), [True]
    connection = connected(fake, mono, running)
    connection.connect()
    running[0], fake.ipc = False, False
    lost = rounds(connection, fake, mono, 4)                    # 两次到点重连失败，窗口内带着"终端进程不在"
    assert sum(IN_WINDOW in str(error) for error in lost) == 2
    for error in lost:
        assert LOST in str(error), str(error)

    running[0], fake.ipc = True, True                           # 终端回来了；到点那一轮重连成功
    mono.advance(10)
    assert connection.call(fake.symbol_info_tick, "EURUSD") == 1.2345
    assert connection._reconnect_failures == 0 and connection._next_reconnect_at == 0.0
    assert connection._last_reconnect_reason == ""               # 旧原因清掉了

    fake.server_connected = False                               # 终端在、交易服务器没连上
    errors = rounds(connection, fake, mono, 4)
    assert sum(IN_WINDOW in str(error) for error in errors) == 2
    for error in errors:
        assert "交易服务器未连接" in str(error) and LOST not in str(error), str(error)
    assert connection._last_reconnect_reason.startswith("终端在线、交易服务器未连接")


def test_the_other_reasons_are_carried_and_do_not_read_as_a_lost_terminal():
    """到点重连失败的三种别的原因——终端在、服务器没连上；初始化了但 terminal_info 还是没有；MT5 的
    initialize 自己失败——窗口内照样带上，但文字里没有"终端进程不在"。"""

    class InitializesButSilent(FakeMT5Module):
        """initialize 说成功，terminal_info 却还是没有（IPC 没重建起来）。"""

        def initialize(self, *args, **kwargs):
            super().initialize(*args, **kwargs)
            return True

    def scenario(make_fake, arrange, expected):
        """连上之后变成 arrange 说的那种断线，跑三轮：第一轮到点，第三轮在退避窗口内。→ 窗口内那一轮的文字。"""
        fake, mono, running = make_fake(), Mono(), [True]
        connection = connected(fake, mono, running)
        connection.connect()
        arrange(fake)
        errors = rounds(connection, fake, mono, 3)
        assert expected in str(errors[0]) and LOST not in str(errors[0]), str(errors[0])
        assert expected in str(errors[2]) and IN_WINDOW in str(errors[2]), str(errors[2])
        return str(errors[2])

    server = scenario(FakeMT5Module, lambda fake: setattr(fake, "server_connected", False),
                      "交易服务器未连接——等待终端自行重连")
    silent = scenario(InitializesButSilent, lambda fake: setattr(fake, "ipc", False),
                      "终端已初始化但交易服务器未连接")
    failed = scenario(FakeMT5Module, lambda fake: setattr(fake, "ipc", False),
                      "NotConnectedError: MT5 终端初始化失败")     # 带的是基类的名字，不是"终端进程不在"
    load()                                                      # liveness_check.py 导入 liveness_logic
    logic = sys.modules["liveness_logic"]
    for window in (server, silent, failed):
        assert LOST not in window
        for form in RUNNER_FORMS:
            assert logic._terminal_wait(form.format(exc=window)) is None, window


def test_the_liveness_check_reads_every_round_the_runner_writes_as_a_lost_terminal():
    """把两边串起来：91 轮里每一轮的异常，按运行器的两种写法变成心跳的 last_error，存活检查的判别器
    每一轮都认成 lost。写法是从 scripts/live_runner.py 原文核对的。"""
    runner = (ROOT / "scripts" / "live_runner.py").read_text(encoding="utf-8")
    for form in RUNNER_FORMS:
        assert form in runner, form
    load()                                                      # liveness_check.py 导入 liveness_logic
    logic = sys.modules["liveness_logic"]
    errors, _, _ = gone_for_three_minutes()
    assert len(errors) == 91
    for error in errors:
        for form in RUNNER_FORMS:
            assert logic._terminal_wait(form.format(exc=error)) == "lost", (form, str(error))
