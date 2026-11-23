"""T209（第二十三轮施工计划 §2.2，D111）：网关不拉起终端。

- 调 initialize 之前先确认终端进程在运行；不在就抛 TerminalNotConnectedError
  （NotConnectedError 的子类），initialize 一次也不调。启动的 connect() 和
  重连走同一条路。
- 探测在进程内完成（ctypes），不起子进程；做成可注入的接口。
- 启动时终端没就绪按断线处理：有心跳、有 DISCONNECTED 段，不写 fatal halt，
  连续 15 分钟以 78 退出。
- 运行器每个决策轮读 terminal_info()；终端不允许算法交易时本轮不决策、不开仓。
- 告警定类：TerminalNotConnectedError 归记录类；ALGO_TRADING_DISABLED 归异常类。
- 日报这类一次性脚本用同一个网关，终端不在时不调 initialize。

真实的进程探测只在标了 real_terminal_probe 的测试里跑；其余测试的终端探针由
tests/conftest.py 换成"在运行"。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mt5gw import connection as gw_connection
from mt5gw.config import GatewayConfig
from mt5gw.connection import MT5Connection
from mt5gw.errors import NotConnectedError, TerminalNotConnectedError
from mt5gw.terminal import _same_image, terminal_running
from mt5live import terminal_guard
from mt5live.terminal_guard import (ALERT_ALGO_TRADING_DISABLED, ALERT_ALGO_TRADING_RESTORED,
                                    AlgoTradingWatch, wait_for_gateway)
from mt5live.monitor import DISCONNECT_EXIT_RC, read_alerts, write_role_heartbeat

from tests.fakes import FakeMT5, TerminalInfo
from tests.fakes_live import FakeBroker, bars_from_df, boundary_open, grid_bars
from tests.test_t141_disconnect import (MAGIC, RISK_TOML, SESSIONS_TOML, VirtClock, _DownGateway,
                                        _fake_datetime, _keep_runner_alive, _real_mode_env, _write,
                                        virt0)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
ABSENT = "MT5 终端进程不在：未调用 initialize（终端只由看护任务或维护窗内的人工操作启动，T209）"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name[:-3]}_t209", ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def kinds(live_dir) -> list[str]:
    return [alert.kind for alert in read_alerts(str(live_dir))]


# --------------------------------------------------------------- 进程内探针

def test_same_image_allows_for_case_separators_and_short_names_only():
    assert _same_image(r"C:\MT5 Demo\terminal64.exe", r"c:\mt5 demo\TERMINAL64.EXE")
    assert _same_image(r"C:\MT5 Demo\terminal64.exe", "C:/MT5 Demo/./terminal64.exe")
    assert not _same_image(r"C:\MT5\terminal64.exe", r"C:\MT5_B1\terminal64.exe")
    assert not _same_image("", r"C:\MT5\terminal64.exe") and not _same_image(None, "x")


@pytest.mark.real_terminal_probe
def test_real_probe_recognizes_this_process_and_no_other_path(tmp_path):
    from mt5gw import terminal

    kernel, wintypes = terminal._kernel()
    image = terminal._image_path(kernel, wintypes, os.getpid())
    assert image                                       # the test process can be queried
    assert terminal_running(image) is True
    assert terminal_running(image.upper()) is True     # case-insensitive
    assert terminal_running(image.replace("\\", "/")) is True
    assert terminal_running(str(tmp_path / "terminal64.exe")) is False   # nothing runs from here
    assert terminal_running("") is False and terminal_running(None) is False


def test_probe_says_no_when_it_cannot_tell(monkeypatch):
    from mt5gw import terminal

    monkeypatch.setattr(terminal.os, "name", "posix")
    assert terminal_running(sys.executable) is False   # not Windows
    monkeypatch.undo()
    monkeypatch.setattr(terminal, "_pids_named", lambda name: [1, 2])
    monkeypatch.setattr(terminal, "_kernel", lambda: (_ for _ in ()).throw(OSError("no kernel32")))
    assert terminal_running(sys.executable) is False   # the query itself failed


def test_probe_stays_inside_the_process_and_touches_no_terminal_api():
    tree = ast.parse((ROOT / "src" / "mt5gw" / "terminal.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {"__future__", "ctypes", "ntpath", "os"}, imported
    called = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}
    assert not called & {"Popen", "system", "run", "check_output", "initialize"}, called


# ------------------------------------------------- 网关：终端不在就不 initialize

def make_conn(probe, fake=None, monotonic=None, **options):
    options = {"terminal_path": r"C:\MT5 Demo\terminal64.exe", **options}
    fake = fake or FakeMT5()
    return MT5Connection(GatewayConfig(**options), mt5_module=fake, terminal_running=probe,
                         monotonic=monotonic or time.monotonic), fake


def test_initialize_is_not_called_while_the_terminal_process_is_absent():
    conn, fake = make_conn(lambda path: False)
    with pytest.raises(TerminalNotConnectedError) as stopped:
        conn.connect()
    assert isinstance(stopped.value, NotConnectedError)
    assert "NotConnectedError" in type(stopped.value).__name__    # T210 的定类按这个子串
    assert fake.calls("initialize") == 0 and conn.connected is False


def test_initialize_is_called_once_with_the_configured_path_when_the_terminal_is_there():
    seen = []
    conn, fake = make_conn(lambda path: seen.append(path) or True)
    conn.connect()
    assert seen == [r"C:\MT5 Demo\terminal64.exe"]
    assert fake.calls("initialize") == 1 and conn.connected is True


def test_a_failing_probe_counts_as_absent():
    def broken(path):
        raise OSError("snapshot failed")
    conn, fake = make_conn(broken)
    with pytest.raises(TerminalNotConnectedError):
        conn.connect()
    assert fake.calls("initialize") == 0


@pytest.mark.real_terminal_probe
def test_the_real_probe_blocks_initialize_for_a_terminal_that_is_not_running(tmp_path):
    fake = FakeMT5()
    conn = MT5Connection(GatewayConfig(terminal_path=str(tmp_path / "terminal64.exe")), mt5_module=fake)
    with pytest.raises(TerminalNotConnectedError):
        conn.connect()
    assert fake.calls("initialize") == 0


def test_reconnect_neither_initializes_while_absent_nor_hammers_inside_the_backoff_window():
    state, clock = {"running": True}, {"t": 1000.0}
    conn, fake = make_conn(lambda path: state["running"], monotonic=lambda: clock["t"])
    conn.connect()
    fake.terminal_info_value = None                     # IPC 不通：终端进程退出了
    state["running"] = False
    with pytest.raises(NotConnectedError) as stopped:
        conn.call(fake.symbol_info_tick, "EURUSD")
    assert "TerminalNotConnectedError" in str(stopped.value)
    assert fake.calls("initialize") == 1                 # 还是启动那一次
    with pytest.raises(NotConnectedError) as waiting:
        conn.call(fake.symbol_info_tick, "EURUSD")       # 退避窗口内：如实抛断线
    assert "退避" in str(waiting.value) and fake.calls("initialize") == 1
    clock["t"] += 61.0                                   # 过了退避窗口
    state["running"] = True                              # 看护任务把终端拉起来了
    fake.terminal_info_value = TerminalInfo(connected=True, trade_allowed=True)
    fake.set_behavior("symbol_info_tick", value=1.5)
    assert conn.call(fake.symbol_info_tick, "EURUSD") == 1.5
    assert fake.calls("initialize") == 2


# ------------------------------------------------------- 启动时终端还没起来

class _LateGateway(_DownGateway):
    """前 failures 次 connect() 抛 TerminalNotConnectedError，之后连上。"""

    def __init__(self, broker, failures=0):
        super().__init__(broker)
        self._failures = failures
        self.connect_calls = 0
        self._connected = False

    @property
    def connected(self):
        return self._connected

    def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self._failures:
            raise TerminalNotConnectedError(ABSENT)
        self._connected = True


def test_startup_wait_keeps_the_heartbeat_and_the_disconnected_segment_then_connects(tmp_path):
    gw = _LateGateway(FakeBroker("XAUUSD"), failures=2)
    beats, waits, logs = [], [], []
    rc = wait_for_gateway(gw, str(tmp_path), f"runner-{MAGIC}", lambda: beats.append(1),
                          retry_s=7, sleep=waits.append, log=logs.append)
    assert rc is None and gw.connected and gw.connect_calls == 3
    assert len(beats) == 2 and waits == [7, 7]          # 每次连不上：写一次心跳、等一个间隔
    alerts = read_alerts(str(tmp_path))
    assert [alert.kind for alert in alerts] == ["DISCONNECTED", "DISCONNECTED"]
    assert "TerminalNotConnectedError" in alerts[0].message and "恢复连接" in alerts[1].message
    assert all("等待交易终端" in line for line in logs) and len(logs) == 2
    assert not (tmp_path / "halt.json").exists()


def test_startup_wait_gives_up_with_the_dedicated_exit_code_and_no_halt(tmp_path):
    gw = _LateGateway(FakeBroker("XAUUSD"), failures=10 ** 6)
    clock = {"t": 1000.0}

    def sleep(seconds):
        clock["t"] += seconds

    rc = wait_for_gateway(gw, str(tmp_path), "risk", lambda: None, retry_s=60,
                          clock=lambda: clock["t"], sleep=sleep, log=lambda line: None)
    assert rc == DISCONNECT_EXIT_RC and gw.connect_calls == 16
    marker = json.loads((tmp_path / "disconnect-exit-risk.json").read_text(encoding="utf-8"))
    assert (marker["component"], marker["rc"], marker["reason"]) == ("risk", 78, "disconnect_timeout")
    assert marker["duration_s"] >= 900 and "TerminalNotConnectedError" in marker["last_error"]
    assert not (tmp_path / "halt.json").exists()
    assert "退出码" in read_alerts(str(tmp_path))[-1].message


def test_startup_wait_connects_once_and_writes_nothing_when_the_terminal_is_there(tmp_path):
    gw = _LateGateway(FakeBroker("XAUUSD"), failures=0)
    rc = wait_for_gateway(gw, str(tmp_path), "risk", lambda: pytest.fail("no heartbeat needed"),
                          sleep=lambda seconds: pytest.fail("no sleep"))
    assert rc is None and gw.connected and gw.connect_calls == 1
    assert read_alerts(str(tmp_path)) == ()                 # 一次就连上：没有断线段，不写告警


def test_any_other_startup_exception_still_propagates(tmp_path):
    class _Sentinel(Exception):
        pass

    class _BrokenGateway(_LateGateway):
        def connect(self):
            raise _Sentinel("已通过点差模型检查，到达网关连接")

    with pytest.raises(_Sentinel):
        wait_for_gateway(_BrokenGateway(FakeBroker("XAUUSD")), str(tmp_path), "risk", lambda: None)
    assert read_alerts(str(tmp_path)) == ()                 # 不是断线：不进降级段，照旧是启动失败


def test_startup_wait_takes_the_sleep_function_at_call_time(tmp_path, monkeypatch):
    gw = _LateGateway(FakeBroker("XAUUSD"), failures=1)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert wait_for_gateway(gw, str(tmp_path), "risk", lambda: None, retry_s=0, log=lambda line: None) is None


def _sleeper(virt, stop_after, live=None):
    """替换 time.sleep：每睡一次推进虚拟钟 30 秒；到第 stop_after 次用 KeyboardInterrupt 收尾。
    给了 live 就按虚拟墙钟写新鲜的运行器心跳（风控据此判活）。"""
    real_sleep, step = time.sleep, {"n": 0}

    def fake_sleep(seconds):
        step["n"] += 1
        virt.advance(30)
        if live is not None:
            _keep_runner_alive(str(live), virt)
        if step["n"] >= stop_after:
            raise KeyboardInterrupt
        real_sleep(0)

    return fake_sleep


def test_risk_process_waits_for_the_terminal_at_startup_without_a_fatal_halt(tmp_path, monkeypatch):
    mod = load_script("risk_process.py")
    live = tmp_path / "live"
    gw = _LateGateway(FakeBroker("XAUUSD"), failures=2)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    beats = []
    monkeypatch.setattr(mod, "write_role_heartbeat", lambda *a, **k: beats.append(virt.mono))
    sleeper = _sleeper(virt, stop_after=5, live=live)
    monkeypatch.setattr(time, "sleep", sleeper)
    rc = mod.main(["--config", _write(tmp_path / "risk.toml", RISK_TOML), "--data-dir", str(tmp_path),
                   "--loop", "--interval", "30",
                   "--sessions-config", _write(tmp_path / "sessions.toml", SESSIONS_TOML)])
    assert rc == 0                                       # 不是 78，也不是 fatal
    assert gw.connect_calls == 3 and beats[:2] == [1000.0, 1030.0]   # 等待期间每 30 秒写一次角色心跳
    assert len(beats) > 2                                # 连上以后主循环照写
    assert kinds(live)[:2] == ["DISCONNECTED", "DISCONNECTED"]
    assert not (live / "halt.json").exists() and not (live / "disconnect-exit-risk.json").exists()
    assert not list(live.glob("loop-exit-*.json"))


def test_risk_process_exits_with_78_when_the_terminal_never_comes_at_startup(tmp_path, monkeypatch):
    mod = load_script("risk_process.py")
    live = tmp_path / "live"
    gw = _LateGateway(FakeBroker("XAUUSD"), failures=10 ** 6)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    monkeypatch.setattr(mod, "write_role_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(time, "sleep", _sleeper(virt, stop_after=10 ** 6))
    rc = mod.main(["--config", _write(tmp_path / "risk.toml", RISK_TOML), "--data-dir", str(tmp_path),
                   "--loop", "--interval", "30",
                   "--sessions-config", _write(tmp_path / "sessions.toml", SESSIONS_TOML)])
    assert rc == DISCONNECT_EXIT_RC and gw.connect_calls == 31
    marker = json.loads((live / "disconnect-exit-risk.json").read_text(encoding="utf-8"))
    assert (marker["rc"], marker["duration_s"] >= 900) == (78, True)
    assert json.loads((live / "exit-risk.json").read_text(encoding="utf-8"))["rc"] == 78
    assert not (live / "halt.json").exists()


def test_reconcile_waits_for_the_terminal_at_startup_without_a_fatal_halt(tmp_path, monkeypatch):
    from mt5live.reconcile import Intent, write_intent

    mod = load_script("live_reconcile.py")
    live = tmp_path / "live"
    live.mkdir(parents=True)
    write_intent(str(live / f"expected-{MAGIC}.json"), Intent(
        symbol="XAUUSD", magic=MAGIC, side="buy", volume=0.0, status="confirmed", cause="test", ts=virt0()))
    gw = _LateGateway(FakeBroker("XAUUSD"), failures=2)
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    beats = []
    monkeypatch.setattr(mod, "write_role_heartbeat", lambda *a, **k: beats.append(1))
    monkeypatch.setattr(time, "sleep", _sleeper(virt, stop_after=5))
    rc = mod.main(["--data-dir", str(tmp_path), "--loop", "--interval", "30"])
    assert rc == 0 and gw.connect_calls == 3 and beats[:2] == [1, 1]
    assert len(beats) > 2                                # 连上以后主循环照写
    assert kinds(live)[:2] == ["DISCONNECTED", "DISCONNECTED"]
    assert not (live / "halt.json").exists() and not (live / "disconnect-exit-reconcile.json").exists()


def test_runner_waits_for_the_terminal_at_startup_without_a_fatal_halt(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lockshome"))
    mod = load_script("live_runner.py")
    t1 = datetime(2026, 10, 28, 10, 0, 7)
    broker = FakeBroker("XAUUSD")
    broker.set_bars("XAUUSD", "M1", bars_from_df(grid_bars(boundary_open(t1, tf_seconds=60),
                                                           tf_seconds=60, last="up")))
    broker.quote_time = t1
    gw = _LateGateway(broker, failures=2)
    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file", staticmethod(lambda p: gw))
    monkeypatch.setattr(mod, "now_utc", lambda: t1)
    env = _real_mode_env(tmp_path)
    live = Path(env["live_dir"])
    write_role_heartbeat(str(live), "risk", interval=600.0, now=t1)
    monkeypatch.setattr(time, "sleep", _sleeper(VirtClock(t1), stop_after=3))   # 两次等待 + 第一轮
    rc = mod.main(["--config", env["config"], "--loop", "--no-spread-gate",
                   "--risk-config", env["risk"], "--sessions-config", env["sessions"],
                   "--gateway-config", _write(tmp_path / "gw.json", "{}"), "--poll-interval", "0.1"])
    assert rc == 0 and gw.connect_calls == 3
    assert kinds(live)[:2] == ["DISCONNECTED", "DISCONNECTED"]
    heartbeat = json.loads((live / f"heartbeat-{MAGIC}.json").read_text(encoding="utf-8"))
    assert heartbeat["runner_alive"] is True
    assert not (live / "halt.json").exists()


# --------------------------------------------- 终端不允许算法交易：本轮不开仓

class _InfoGateway:
    def __init__(self, infos):
        self.infos = list(infos)
        self.reads = 0

    def terminal_info(self):
        self.reads += 1
        item = self.infos.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _terminal(trade_allowed=True, tradeapi_disabled=False):
    return SimpleNamespace(trade_allowed=trade_allowed, tradeapi_disabled=tradeapi_disabled)


def test_algo_trading_watch_blocks_the_round_and_announces_only_changes(tmp_path):
    gw = _InfoGateway([_terminal(), _terminal(trade_allowed=False), _terminal(trade_allowed=False),
                       _terminal(tradeapi_disabled=True), _terminal()])
    watch = AlgoTradingWatch(gw, str(tmp_path))
    assert [watch.allowed(NOW) for _ in range(5)] == [True, False, False, False, True]
    assert gw.reads == 5                                  # 每个决策轮读一次
    assert kinds(tmp_path) == [ALERT_ALGO_TRADING_DISABLED, ALERT_ALGO_TRADING_RESTORED]


def test_algo_trading_watch_announces_a_terminal_that_starts_forbidden(tmp_path):
    watch = AlgoTradingWatch(_InfoGateway([_terminal(trade_allowed=False), _terminal()]), str(tmp_path))
    assert [watch.allowed(NOW), watch.allowed(NOW)] == [False, True]
    assert kinds(tmp_path) == [ALERT_ALGO_TRADING_DISABLED, ALERT_ALGO_TRADING_RESTORED]


@pytest.mark.parametrize("dead", [None, NotConnectedError("terminal_info 无返回")])
def test_algo_trading_watch_hands_a_dead_ipc_to_the_caller_as_not_connected(tmp_path, dead):
    watch = AlgoTradingWatch(_InfoGateway([dead, _terminal()]), str(tmp_path))
    with pytest.raises(NotConnectedError):
        watch.allowed(NOW)
    assert kinds(tmp_path) == []                         # 不误报成"算法交易被关"


def test_algo_trading_watch_lets_a_gateway_without_terminal_info_pass(tmp_path):
    class Bare:
        pass
    assert AlgoTradingWatch(Bare(), str(tmp_path)).allowed(NOW) is True
    assert kinds(tmp_path) == []


class _AlgoGateway(_LateGateway):
    def terminal_info(self):
        return _terminal(self.state.get("algo", True))


def test_runner_neither_decides_nor_opens_while_the_terminal_forbids_algo_trading(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lockshome"))
    mod = load_script("live_runner.py")
    t1 = datetime(2026, 10, 28, 10, 0, 7)
    clock = {"now": t1}
    broker = FakeBroker("XAUUSD")
    broker.set_bars("XAUUSD", "M1", bars_from_df(grid_bars(boundary_open(t1, tf_seconds=60),
                                                           tf_seconds=60, last="up")))
    broker.quote_time = t1
    gw = _AlgoGateway(broker)
    gw._connected = True
    gw.state["algo"] = False
    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file", staticmethod(lambda p: gw))
    monkeypatch.setattr(mod, "now_utc", lambda: clock["now"])
    env = _real_mode_env(tmp_path)
    live = Path(env["live_dir"])
    write_role_heartbeat(str(live), "risk", interval=600.0, now=t1)
    real_sleep, step = time.sleep, {"n": 0}
    t2 = t1 + timedelta(seconds=80)

    def fake_sleep(seconds):
        step["n"] += 1
        if step["n"] == 1:
            assert broker.market_orders == []             # 被拦的那一轮没有发单
            clock["now"] = t2
            gw.state["algo"] = True
            broker.set_bars("XAUUSD", "M1", bars_from_df(grid_bars(boundary_open(t2, tf_seconds=60),
                                                                    tf_seconds=60, last="up")))
            broker.quote_time = t2
        elif step["n"] == 2:
            raise KeyboardInterrupt
        real_sleep(0)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    rc = mod.main(["--config", env["config"], "--loop", "--no-spread-gate",
                   "--risk-config", env["risk"], "--sessions-config", env["sessions"],
                   "--gateway-config", _write(tmp_path / "gw.json", "{}"), "--poll-interval", "0.1"])
    assert rc == 0
    assert len(broker.market_orders) == 1                 # 下一轮恢复正常开仓
    out = capsys.readouterr().out
    assert "algo_trading_disabled" in out
    assert kinds(live) == [ALERT_ALGO_TRADING_DISABLED, ALERT_ALGO_TRADING_RESTORED]
    assert json.loads((live / f"heartbeat-{MAGIC}.json").read_text(encoding="utf-8"))["last_error"] is None


# --------------------------------------------------------------- 告警定类

def test_terminal_absent_alerts_are_recorded_and_algo_disabled_is_an_anomaly():
    mod = load_script("demo_check.py")
    assert "TerminalNotConnectedError" in mod.BROKER_UNAVAILABLE_RECORDED_MARKS
    assert mod._broker_unavailable_recorded(f"blocked_broker_unavailable：TerminalNotConnectedError: {ABSENT}")
    assert "ALGO_TRADING_DISABLED" in mod.FORMAL_ANOMALY_ALERTS
    assert "ALGO_TRADING_RESTORED" in mod.FORMAL_RECORDED_ALERTS
    assert mod.FORMAL_ANOMALY_ALERTS.isdisjoint(mod.FORMAL_RECORDED_ALERTS)


def test_the_subclass_name_keeps_the_substring_the_classifier_matches_on():
    assert issubclass(TerminalNotConnectedError, NotConnectedError)
    assert "NotConnectedError" in TerminalNotConnectedError.__name__


# ------------------------------------------- 一次性脚本：同一个网关，不拉起终端

def test_daily_report_never_launches_the_terminal(tmp_path, monkeypatch):
    fake = FakeMT5()
    fake.set_behavior("account_info", SimpleNamespace(
        login=123456, server="Fake-Server", company="Fake-Broker", currency="USD", balance=10000.0,
        equity=10000.0, margin=0.0, margin_free=10000.0, profit=0.0, leverage=100, margin_mode=2,
        trade_mode=0))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    report = load_script("live_daily_report.py")
    config = tmp_path / "gateway.json"
    config.write_text(json.dumps({"allow_live_trading": False}), encoding="utf-8")
    monkeypatch.setattr(gw_connection, "TERMINAL_PROBE", lambda terminal_path: False)   # 终端进程不在
    with pytest.raises(TerminalNotConnectedError):
        report.build_gw(None, str(config), str(tmp_path), demo=True)
    assert fake.calls("initialize") == 0 and fake.calls("account_info") == 0
    monkeypatch.setattr(gw_connection, "TERMINAL_PROBE", lambda terminal_path: True)
    gw, owned = report.build_gw(None, str(config), str(tmp_path), demo=True)
    assert owned and fake.calls("initialize") == 1       # 终端在：走的是同一个网关
    gw.shutdown()
