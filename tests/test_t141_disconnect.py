"""T141（第十六轮 O16-3；计划 docs/handoff/2026-11-08-round16.md §2）：断线不退出。

- 下单路径上的 NotConnectedError 与查询路径同样处理：本轮
  blocked_broker_unavailable、不开新仓、写告警（BROKER_UNAVAILABLE）。
- 网关内有界重连：指数退避 5、10、30、60 秒（此后封顶 60），每次尝试
  本身仍受 T110 期限约束（_serialized/_invoke 既有超时，不改期限常量）。
- 风控、对账进程同样处理断线（降级继续：文件级检查照常、角色心跳照写）；
  连续 15 分钟连不上才以**专用退出码 78** 退出，交给 supervisor，并留
  disconnect-exit-<component>.json 标记（T143 演练与取证读它）。
- 计划逐字 TDD 场景：
  1) 假网关断线 3 分钟后恢复 → 三个进程（runner/risk/reconcile）都不
     退出，恢复后照常；
  2) 断线 20 分钟 → 风控、对账以专用码 78 退出并留标记。

边界（逐字）：不动 T140 已交付的拒单语义（TradeRejectedError → rejected
回报、连拒 pause、Unknown 仍退出），只叠加断线维度；T110 期限常量不改。
"""
import importlib.util
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mt5gw.config import GatewayConfig
from mt5gw.errors import NotConnectedError

from tests.fakes import FakeMT5
from tests.fakes_live import FakeBroker, bars_from_df, boundary_open, \
    grid_bars, mk_decision

REPO_ROOT = Path(__file__).resolve().parents[1]
MAGIC = 779
NOW = datetime(2026, 10, 20, 18, 0)
DISCONNECT_EXIT_RC = 78
DISCONNECT_LIMIT_S = 15 * 60.0


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _alerts_text(live_dir) -> str:
    path = os.path.join(str(live_dir), "alerts.log")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name.replace('.', '_')}_t141", REPO_ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


# --------------------------------------------------------------- 虚拟时钟

class VirtClock:
    """测试用双钟：墙钟（naive UTC）+ 单调钟。sleep 即推进。"""

    def __init__(self, wall: datetime, mono: float = 1000.0):
        self.wall = wall.replace(tzinfo=None)
        self.mono = mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def mono_fn(self) -> float:
        return self.mono


def _fake_datetime(virt: VirtClock):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            wall = virt.wall
            return wall.replace(tzinfo=tz) if tz is not None else wall

    return _DT


# ------------------------------------------------- 网关：有界重连（T141-2）

class _Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _down_terminal(fake: FakeMT5) -> None:
    fake.initialize_result = False
    fake.terminal_info_value = type(fake.terminal_info_value)(
        connected=False, trade_allowed=False)
    fake.last_error_value = (-6, "terminal is not connected")


def test_t141_gateway_backoff_schedule_5_10_30_60_capped():
    """断线后重连尝试的间隔：5、10、30、60，此后封顶 60 秒；退避窗口内
    reconnect() 不再触终端（不砸初始化）。"""
    from mt5gw.connection import MT5Connection, RECONNECT_BACKOFF_S

    assert RECONNECT_BACKOFF_S == (5.0, 10.0, 30.0, 60.0)
    fake = FakeMT5()
    _down_terminal(fake)
    mono = _Mono()
    conn = MT5Connection(GatewayConfig(), mt5_module=fake, monotonic=mono)
    assert conn.reconnect() is False                 # 第 1 次尝试（立即）
    assert fake.calls("initialize") == 1
    assert conn.reconnect() is False                 # 退避 5 秒内：不重复尝试
    assert fake.calls("initialize") == 1
    mono.advance(5)
    assert conn.reconnect() is False                 # 第 2 次（+5）
    assert fake.calls("initialize") == 2
    mono.advance(9)
    assert conn.reconnect() is False                 # 10 秒窗口内
    assert fake.calls("initialize") == 2
    mono.advance(1)
    assert conn.reconnect() is False                 # 第 3 次（+10）
    assert fake.calls("initialize") == 3
    mono.advance(30)
    assert conn.reconnect() is False                 # 第 4 次（+30）
    assert fake.calls("initialize") == 4
    mono.advance(60)
    assert conn.reconnect() is False                 # 第 5 次（+60）
    assert fake.calls("initialize") == 5
    mono.advance(60)
    assert conn.reconnect() is False                 # 第 6 次（封顶 60）
    assert fake.calls("initialize") == 6
    mono.advance(60)
    conn.reconnect()
    assert fake.calls("initialize") == 7             # 仍是 60 秒节奏


def test_t141_gateway_reconnect_success_resets_backoff():
    """连上后退避清零：下一次失败再从 5 秒起。"""
    from mt5gw.connection import MT5Connection

    fake = FakeMT5()
    _down_terminal(fake)
    mono = _Mono()
    conn = MT5Connection(GatewayConfig(), mt5_module=fake, monotonic=mono)
    assert conn.reconnect() is False
    mono.advance(5)
    fake.initialize_result = True     # FakeMT5.initialize 成功即恢复 connected
    assert conn.reconnect() is True
    assert conn.connected
    # 再次断线：退避从头（5 秒）开始，而不是延续 60 秒
    _down_terminal(fake)
    assert conn.reconnect() is False
    assert fake.calls("initialize") == 3
    mono.advance(4)
    assert conn.reconnect() is False
    assert fake.calls("initialize") == 3             # 5 秒窗口内不试
    mono.advance(1)
    conn.reconnect()
    assert fake.calls("initialize") == 4             # +5 秒就试


def test_t141_gateway_no_hammer_inside_backoff_window():
    """退避窗口内普通调用（call）直接抛 NotConnectedError，不再触
    initialize——断线期间按 5/10/30/60 节奏重连，不逐调用砸终端。"""
    from mt5gw.connection import MT5Connection

    fake = FakeMT5()
    _down_terminal(fake)
    mono = _Mono()
    conn = MT5Connection(GatewayConfig(), mt5_module=fake, monotonic=mono)
    with pytest.raises(NotConnectedError):
        conn.call(fake.symbol_info_tick, "EURUSD")
    assert fake.calls("initialize") == 1             # 只试了一次
    with pytest.raises(NotConnectedError, match="退避"):
        conn.call(fake.symbol_info_tick, "EURUSD")
    assert fake.calls("initialize") == 1             # 窗口内零新增尝试
    assert fake.calls("symbol_info_tick") == 0       # fn 从未被触达


def test_t141_gateway_recovers_through_call_after_backoff():
    """到点的隐式重连（call 内）成功后照常出数——恢复后照常的最小钉。"""
    from mt5gw.connection import MT5Connection

    fake = FakeMT5()
    _down_terminal(fake)
    mono = _Mono()
    conn = MT5Connection(GatewayConfig(), mt5_module=fake, monotonic=mono)
    with pytest.raises(NotConnectedError):
        conn.call(fake.symbol_info_tick, "EURUSD")
    mono.advance(5)
    fake.initialize_result = True
    fake.set_behavior("symbol_info_tick", value=1.2345)
    assert conn.call(fake.symbol_info_tick, "EURUSD") == 1.2345
    assert fake.calls("initialize") == 2


# ------------------------------------------------- 执行器：下单路径（T141-1）

def _mk_executor(broker, tmp_path, *, magic: int = MAGIC):
    from mt5quant.strategies.base import ExitRules
    from mt5live.executor import ExecutionAdapter, LiveExecutor

    return LiveExecutor(
        ExecutionAdapter(broker, broker, broker,
                         symbol=broker.symbol, magic=magic),
        symbol=broker.symbol, magic=magic, live_dir=str(tmp_path),
        rules=ExitRules(stop_loss_atr=1.5))


def _journal_lines(live_dir, magic: int = MAGIC):
    path = os.path.join(str(live_dir), f"execution-journal-{magic}.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f.read().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_t141_order_path_disconnect_blocked_not_exit(tmp_path):
    """下单路径 NotConnectedError：与查询路径同样处理——poll 不重抛，返回
    blocked_broker_unavailable；零订单发出；写告警 BROKER_UNAVAILABLE。"""
    b = FakeBroker()
    b.error_on_market_order = NotConnectedError("终端与交易服务器断开")
    ex = _mk_executor(b, tmp_path)
    d = mk_decision("XAUUSD|M15|2026-10-20T18:00:00", signal=1,
                    volume_lots=0.02)
    rr = ex.poll(d, NOW)                       # 不再 pytest.raises
    assert rr.action == "blocked_broker_unavailable"
    assert rr.decision_id == d.decision_id
    assert "NotConnectedError" in (rr.error or "")
    assert b.positions() == []                 # 未成交、无持仓（请求未到达服务器）
    alerts = _alerts_text(tmp_path)
    assert "BROKER_UNAVAILABLE" in alerts
    assert "断线" in alerts


def test_t141_order_path_disconnect_journal_rejected_intent_confirmed(tmp_path):
    """断线 = 请求从未发出（网关在 order_send 之前抛出）：journal 落终态
    rejected（detail 注明 not_connected，不留未决项挡恢复后的正常开仓）；
    意图 confirmed（实际净持仓=0，空仓即真相）。"""
    b = FakeBroker()
    b.error_on_market_order = NotConnectedError("终端与交易服务器断开")
    ex = _mk_executor(b, tmp_path)
    d = mk_decision("XAUUSD|M15|2026-10-20T18:00:00", signal=1,
                    volume_lots=0.02)
    assert ex.poll(d, NOW).action == "blocked_broker_unavailable"
    lines = _journal_lines(tmp_path)
    rejected = [r for r in lines if r.get("status") == "rejected"]
    assert rejected and rejected[-1]["decision_id"] == d.decision_id
    assert "not_connected" in rejected[-1]["detail"]
    # 无未决项（journal append-only：该决策不得出现 sent/unknown 行，
    # 终态之后内存未决表为空）——恢复后不被 execution_unknown 挡板拦住
    assert not [r for r in lines if r.get("decision_id") == d.decision_id
                and r.get("status") in ("sent", "unknown")]
    assert ex._undecided == {}
    intent = _read_json(tmp_path / f"expected-{MAGIC}.json")
    assert intent["status"] == "confirmed"
    assert intent["volume"] == 0.0


def test_t141_disconnect_does_not_touch_t140_rejection_semantics(tmp_path):
    """边界钉：断线轮不计入 T140 的连拒计数（不是服务器拒单）——断线后
    成功一轮，连拒暂停逻辑照旧由 TradeRejectedError 驱动。"""
    from mt5gw.errors import TradeRejectedError

    b = FakeBroker()
    b.error_on_market_order = NotConnectedError("断线演练")
    ex = _mk_executor(b, tmp_path)
    d1 = mk_decision("XAUUSD|M15|2026-10-20T18:00:00", signal=1,
                     volume_lots=0.02)
    assert ex.poll(d1, NOW).action == "blocked_broker_unavailable"
    assert not (tmp_path / f"pause-{MAGIC}.json").exists()   # 无 pause
    alerts = _alerts_text(tmp_path)
    assert "ORDER_REJECTED" not in alerts                     # 非拒单告警
    # 恢复后同 bar 重投被 journal 终态幂等拦住（duplicate，零新增请求）
    b.error_on_market_order = None
    rr = ex.poll(d1, NOW + timedelta(seconds=30))
    assert rr.action == "duplicate"
    assert len(b.market_orders) == 1           # 仅断线前那次尝试，无重发
    assert b.positions() == []


def test_t141_flatten_path_disconnect_blocked(tmp_path):
    """flatten 轮的网关调用断线：poll 级兜底同样返回 blocked（不重抛），
    下一轮继续消费同一指令。"""
    from mt5live.commands import write_flatten

    b = FakeBroker()
    write_flatten(str(tmp_path), reason="drill", now=NOW)
    orig = b.positions

    def down_positions(symbol=None):
        raise NotConnectedError("终端与交易服务器断开")

    b.positions = down_positions
    ex = _mk_executor(b, tmp_path)
    rr = ex.poll(None, NOW)                    # 不再抛 NotConnectedError
    assert rr.action == "blocked_broker_unavailable"
    assert "NotConnectedError" in (rr.error or "")
    assert _alerts_text(tmp_path).count("BROKER_UNAVAILABLE") >= 1
    b.positions = orig
    # 恢复后下一轮照常消费全平指令
    rr2 = ex.poll(None, NOW + timedelta(seconds=30))
    assert rr2.action in ("flatten", "flatten_acked")


# ------------------------------------- 进程级断线计时（monitor.T141 常量）

def test_t141_constants_dedicated_exit_code():
    """专用退出码 78（区别于 75=T110 期限、2=fatal、3=单实例拒绝）与
    15 分钟上限、标记文件名——T143 supervisor 演练依赖这些契约。"""
    from mt5live import monitor

    assert monitor.DISCONNECT_EXIT_RC == DISCONNECT_EXIT_RC
    assert monitor.DISCONNECT_LIMIT_S == DISCONNECT_LIMIT_S
    assert monitor.ALERT_DISCONNECTED == "DISCONNECTED"
    assert monitor.DISCONNECT_EXIT_FILE_FMT == "disconnect-exit-{component}.json"


def test_t141_disconnect_watch_expiry_reset_and_marker(tmp_path):
    """DisconnectWatch：段首一次告警、恢复告警并清零；连续超 15 分钟
    expired；exit_code 落标记文件（含 reason/rc/时长/首末错误）并返回 78。"""
    from mt5live.monitor import DisconnectWatch

    mono = _Mono()
    live = tmp_path
    watch = DisconnectWatch(live, "risk", clock=mono)
    assert not watch.expired()
    watch.note_failure(NotConnectedError("down"))
    assert watch.disconnected_s == 0.0
    text = _alerts_text(live)
    assert text.count("DISCONNECTED") == 1          # 段首一次，不刷屏
    watch.note_failure(NotConnectedError("still down"))
    assert _alerts_text(live).count("DISCONNECTED") == 1
    mono.advance(DISCONNECT_LIMIT_S - 1)
    assert not watch.expired()
    mono.advance(1)
    assert watch.expired()
    rc = watch.exit_code(last_error="NotConnectedError: down")
    assert rc == DISCONNECT_EXIT_RC
    marker = _read_json(live / "disconnect-exit-risk.json")
    assert marker["rc"] == DISCONNECT_EXIT_RC
    assert marker["reason"] == "disconnect_timeout"
    assert marker["component"] == "risk"
    assert marker["duration_s"] >= DISCONNECT_LIMIT_S
    assert marker["last_error"]
    assert "DISCONNECTED" in _alerts_text(live)     # 退出也留告警
    # 恢复路径：重新计时、恢复告警
    mono.advance(60)
    watch2 = DisconnectWatch(live, "risk", clock=mono)
    watch2.note_failure(NotConnectedError("down again"))
    mono.advance(30)
    watch2.note_ok()                            # 恢复告警并清零
    assert not watch2.expired()
    assert "恢复" in _alerts_text(live)
    mono.advance(DISCONNECT_LIMIT_S + 1)
    assert not watch2.expired()                     # 已清零，不再累计


# ------------------------------------------------------ 三进程：TDD 场景 1
# 假网关断线 3 分钟后恢复 → runner / risk / reconcile 都不退出，恢复后照常。

RISK_TOML = """
version = 1
symbols = ["XAUUSD"]
max_lots_per_order = 0.50
max_positions_per_symbol = 3
max_usd_exposure = 100000.0
max_orders_per_minute = 6
daily_loss_cap_usd = 500.0
"""

SESSIONS_TOML = """
version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 0
post_minutes = 0
exempt_symbols = ["BTCUSD"]
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
"""


class _DownService:
    """网关服务替身：state['down']=True 时一切调用抛 NotConnectedError。"""

    def __init__(self, broker, state):
        self._broker = broker
        self._state = state

    def __getattr__(self, name):
        target = getattr(self._broker, name)
        if not callable(target):
            return target

        def call(*a, **k):
            if self._state["down"]:
                raise NotConnectedError("终端与交易服务器断开（演练）")
            return target(*a, **k)

        return call


class _DownGateway:
    """带断线开关的网关替身（对齐 MT5Gateway 的 market/account/trading）。"""

    def __init__(self, broker):
        self._state = {"down": False, "reconnect_tries": 0}
        svc = _DownService(broker, self._state)
        self.market = svc
        self.account = svc
        self.trading = svc

    @property
    def state(self):
        return self._state

    def connect(self):
        pass

    def shutdown(self):
        pass

    def reconnect(self):
        if self._state["down"]:
            self._state["reconnect_tries"] += 1
            return False
        return True


def _keep_runner_alive(live_dir, virt):
    """演练期间运行器活着：按虚拟墙钟写新鲜心跳（风控据此判活）。"""
    from mt5live.monitor import write_heartbeat
    write_heartbeat(live_dir, True, ts=virt.wall)


def test_t141_risk_3min_disconnect_survives(tmp_path, monkeypatch, capsys):
    """风控进程：断线 3 分钟 → 降级轮（不查账户、文件级检查照常、角色心跳
    照写）不退出；恢复后照常轮询；无 halt、无专用退出标记。"""
    from mt5live.monitor import write_role_heartbeat

    mod = _load_script("risk_process.py")
    live = tmp_path / "live"
    risk_toml = _write(tmp_path / "risk.toml", RISK_TOML)
    gw = _DownGateway(FakeBroker("XAUUSD"))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    _keep_runner_alive(str(live), virt)

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)                      # 每轮 30 秒（interval 同步）
        _keep_runner_alive(str(live), virt)
        if step["n"] == 4:                    # 第 5 轮起断线 ~3 分钟
            gw.state["down"] = True
        elif step["n"] == 10:                 # 3 分钟后恢复
            gw.state["down"] = False
        elif step["n"] >= 12:                 # 恢复后跑满 2 轮健康轮
            raise KeyboardInterrupt
        real_sleep(0)

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    rc = mod.main(["--config", risk_toml, "--data-dir", str(tmp_path),
                   "--loop", "--interval", "30",
                   "--sessions-config",
                   _write(tmp_path / "sessions.toml", SESSIONS_TOML)])
    assert rc == 0                            # 不退出（手动停止收尾）
    out = capsys.readouterr().out
    assert out.count("断线降级") >= 1
    assert (live / "heartbeat-risk.json").exists()      # 角色心跳持续
    assert not (live / "disconnect-exit-risk.json").exists()
    assert not (live / "halt.json").exists()            # 未触发心跳停机
    alerts = _alerts_text(live)
    assert "DISCONNECTED" in alerts and "恢复" in alerts


def test_t141_risk_20min_disconnect_dedicated_exit(tmp_path, monkeypatch,
                                                   capsys):
    """风控进程：断线 20 分钟 → 以专用码 78 退出并留
    disconnect-exit-risk.json（T143 演练/取证）；不写 halt（交给 supervisor）。"""
    mod = _load_script("risk_process.py")
    live = tmp_path / "live"
    risk_toml = _write(tmp_path / "risk.toml", RISK_TOML)
    gw = _DownGateway(FakeBroker("XAUUSD"))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)
    _keep_runner_alive(str(live), virt)

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        _keep_runner_alive(str(live), virt)
        if step["n"] == 2:
            gw.state["down"] = True           # 之后一直断线（≥20 分钟）
        real_sleep(0)

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    rc = mod.main(["--config", risk_toml, "--data-dir", str(tmp_path),
                   "--loop", "--interval", "30",
                   "--sessions-config",
                   _write(tmp_path / "sessions.toml", SESSIONS_TOML)])
    assert rc == DISCONNECT_EXIT_RC
    marker = _read_json(live / "disconnect-exit-risk.json")
    assert marker["reason"] == "disconnect_timeout"
    assert marker["rc"] == DISCONNECT_EXIT_RC
    assert marker["duration_s"] >= DISCONNECT_LIMIT_S
    assert marker["duration_s"] <= 20 * 60 + 60
    exit_doc = _read_json(live / "exit-risk.json")
    assert exit_doc["rc"] == DISCONNECT_EXIT_RC      # guarded_main 落的证据
    assert not (live / "halt.json").exists()         # 交给 supervisor，不自作主张
    assert "DISCONNECTED" in _alerts_text(live)
    assert gw.state["reconnect_tries"] >= 1          # 断线期间确实在重连


def test_t141_reconcile_3min_disconnect_survives(tmp_path, monkeypatch,
                                                 capsys):
    """对账进程：断线 3 分钟 → 本轮跳过核对（降级继续、角色心跳照写）不
    退出；恢复后照常对账一致；无专用退出标记。"""
    from mt5live.reconcile import Intent, write_intent

    mod = _load_script("live_reconcile.py")
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    write_intent(str(live / f"expected-{MAGIC}.json"), Intent(
        symbol="XAUUSD", magic=MAGIC, side="buy", volume=0.0,
        status="confirmed", cause="test", ts=virt0()))
    gw = _DownGateway(FakeBroker("XAUUSD"))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        if step["n"] == 1:
            gw.state["down"] = True
        elif step["n"] == 7:                  # ~3 分钟后恢复
            gw.state["down"] = False
        elif step["n"] >= 9:
            raise KeyboardInterrupt
        real_sleep(0)

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    rc = mod.main(["--data-dir", str(tmp_path), "--loop", "--interval", "30"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "断线" in out
    assert (live / "heartbeat-reconcile.json").exists()
    assert not (live / "disconnect-exit-reconcile.json").exists()
    alerts = _alerts_text(live)
    assert "DISCONNECTED" in alerts and "恢复" in alerts
    # 恢复后照常：pause 文件按一致结果写回 paused=false
    assert _read_json(live / f"pause-{MAGIC}.json")["paused"] is False


def test_t141_reconcile_20min_disconnect_dedicated_exit(tmp_path, monkeypatch):
    """对账进程：断线 20 分钟 → 专用码 78 退出 + disconnect-exit-reconcile.json
    标记 + exit-reconcile.json（rc=78）。"""
    from mt5live.reconcile import Intent, write_intent

    mod = _load_script("live_reconcile.py")
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    write_intent(str(live / f"expected-{MAGIC}.json"), Intent(
        symbol="XAUUSD", magic=MAGIC, side="buy", volume=0.0,
        status="confirmed", cause="test", ts=virt0()))
    gw = _DownGateway(FakeBroker("XAUUSD"))
    monkeypatch.setattr(mod, "_open_gateway", lambda cfg: gw)
    virt = VirtClock(datetime(2026, 11, 9, 12, 0, 0))
    monkeypatch.setattr(mod, "datetime", _fake_datetime(virt))
    monkeypatch.setattr(mod, "_mono", virt.mono_fn)

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        virt.advance(30)
        if step["n"] == 1:
            gw.state["down"] = True
        real_sleep(0)

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    rc = mod.main(["--data-dir", str(tmp_path), "--loop", "--interval", "30"])
    assert rc == DISCONNECT_EXIT_RC
    marker = _read_json(live / "disconnect-exit-reconcile.json")
    assert marker["reason"] == "disconnect_timeout"
    assert marker["duration_s"] >= DISCONNECT_LIMIT_S
    assert _read_json(live / "exit-reconcile.json")["rc"] == DISCONNECT_EXIT_RC
    assert not (live / "halt.json").exists()


def virt0() -> datetime:
    return datetime(2026, 11, 9, 12, 0, 0)


# ------------------------------------------------------- 运行器（TDD 场景 1）

def _real_mode_env(tmp_path) -> dict:
    _write(tmp_path / "data" / "specs" / "latest.json", json.dumps({
        "XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                   "volume_step": 0.01, "volume_max": 100.0}}))
    return {
        "risk": _write(tmp_path / "risk.toml", RISK_TOML),
        "sessions": _write(tmp_path / "sessions.toml", SESSIONS_TOML),
        "config": _write(tmp_path / "live.toml", f"""
symbol = "XAUUSD"
timeframe = "M1"
strategy = "mt5quant.strategies.library.buy_hold:BuyHold"
magic = {MAGIC}
lookback_bars = 30
volume_lots = 0.02
data_root = "{(tmp_path / 'data').as_posix()}"
trigger_delay_s = 5.0
"""),
        "live_dir": str(tmp_path / "data" / "live"),
    }


def test_t141_runner_3min_disconnect_survives_and_recovers(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """运行器：断线 3 分钟 → 进程不退出、心跳照写（降级在途，风控不误判
    死亡触发 halt）；恢复后下一网格点照常决策/执行。"""
    from mt5live.monitor import write_role_heartbeat

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lockshome"))
    mod = _load_script("live_runner.py")
    t1 = datetime(2026, 10, 28, 10, 0, 7)
    t2 = t1 + timedelta(minutes=3, seconds=40)
    clock = {"now": t1}
    broker = FakeBroker("XAUUSD")
    broker.set_bars("XAUUSD", "M1",
                    bars_from_df(grid_bars(boundary_open(t1, tf_seconds=60),
                                           tf_seconds=60, last="up")))
    broker.quote_time = t1
    gw = _DownGateway(broker)

    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file",
                        staticmethod(lambda p: gw))

    def mutable_now():
        return clock["now"]

    monkeypatch.setattr(mod, "now_utc", mutable_now)
    env = _real_mode_env(tmp_path)
    live = env["live_dir"]
    write_role_heartbeat(live, "risk", interval=10.0, now=t1)

    hb_seen = {}

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        if step["n"] == 1:                    # 第 1 轮决策+开仓完成后断线
            gw.state["down"] = True
            clock["now"] = t1 + timedelta(seconds=100)
        elif step["n"] == 2:
            clock["now"] = t1 + timedelta(seconds=160)
            hb = _read_json(os.path.join(live, f"heartbeat-{MAGIC}.json"))
            hb_seen["mid"] = hb               # 断线期中心跳仍在更新
        elif step["n"] == 3:                  # ~3 分钟后恢复
            clock["now"] = t2
            gw.state["down"] = False
            broker.set_bars("XAUUSD", "M1",
                            bars_from_df(grid_bars(
                                boundary_open(t2, tf_seconds=60),
                                tf_seconds=60, last="up")))
            broker.quote_time = t2
            write_role_heartbeat(live, "risk", interval=10.0, now=t2)
        else:
            raise KeyboardInterrupt
        real_sleep(0)

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    rc = mod.main(["--config", env["config"], "--loop", "--no-spread-gate",
                   "--risk-config", env["risk"],
                   "--sessions-config", env["sessions"],
                   "--gateway-config", _write(tmp_path / "gw.json", "{}"),
                   "--poll-interval", "0.1"])
    assert rc == 0                            # 不退出
    out = capsys.readouterr().out
    assert "断线" in out
    assert out.count("loop_completed") >= 4   # 断线轮也完整收尾（心跳照写）
    assert "blocked_broker_unavailable" in out
    # 断线期中心跳新鲜且活着（否则风控 120 秒就会 halt）
    assert hb_seen["mid"]["runner_alive"] is True
    # 恢复后照常：第 1 轮的开仓仍在，恢复轮正常决策（hold，不加单）
    assert len(broker.market_orders) == 1
    assert len(broker.positions()) == 1
    hb = _read_json(os.path.join(live, f"heartbeat-{MAGIC}.json"))
    assert hb["runner_alive"] is True
    assert hb["last_error"] is None           # 恢复轮覆盖，无残留错误
    assert not os.path.exists(os.path.join(live, "halt.json"))
    exit_doc = _read_json(os.path.join(live, f"exit-runner-{MAGIC}.json"))
    assert exit_doc.get("reason") in ("completed", "interrupted")
    assert "BROKER_UNAVAILABLE" in _alerts_text(live)
    # 意图/journal 一致：断线轮没洗白任何状态
    assert _read_json(os.path.join(
        live, f"expected-{MAGIC}.json"))["status"] == "confirmed"
