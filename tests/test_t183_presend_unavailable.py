"""T183（第十七轮 O17-3；计划 docs/handoff/2026-11-08-round17-plan.md §2）：
发送前失败按"未发出"处理。

- `MT5Connection.trade_send` 在 `account_info` 为空时抛专用异常
  `PreSendUnavailableError(NotConnectedError)`（含义：发送前无法确认账户
  状态，请求未发出），替换原来的 `DataUnavailableError`——后者会把
  "确定未发出"误导成"回报未知"（执行器兜底记 unknown 后重抛，运行器
  经 guarded_main 退出并写 halt，O17-3/P17-3）。
- 保险丝与账号不符（`LiveTradingBlockedError`）不变，仍然上抛退出。
- 执行器不改：`PreSendUnavailableError` 是 `NotConnectedError`，自动落进
  T141 的 blocked 路径（journal 终态 rejected/not_connected、本轮不开新
  仓、写告警 BROKER_UNAVAILABLE、运行器不退出）——用测试钉住。
- 计划逐字 TDD 场景：假终端在发送前返回空账户 → 运行器不退出、下一
  bar 正常决策、`order_send` 从未被调用（断言调用计数 0）。

边界（逐字）：不动 T140 拒单语义与 T141 断线路径本身，只把"发送前
空账户"归入 T141 类别；`LiveTradingBlockedError` 不在归并范围。
"""
import importlib.util
import json
import os
import time
from collections import namedtuple
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mt5gw.config import GatewayConfig
from mt5gw.connection import MT5Connection

from tests.fakes import FakeMT5
from tests.fakes_live import FakeBroker, bars_from_df, boundary_open, \
    grid_bars, mk_decision

REPO_ROOT = Path(__file__).resolve().parents[1]
MAGIC = 783
NOW = datetime(2026, 10, 20, 18, 0)

AccRow = namedtuple(
    "AccRow",
    "login server company currency leverage balance equity margin margin_free margin_mode trade_mode",
)
TickInfo = namedtuple("TickInfo", "time bid ask last volume")
SymRow = namedtuple(
    "SymRow",
    "name description digits point spread trade_mode currency_profit currency_margin "
    "trade_contract_size volume_min volume_max volume_step filling_mode visible",
)

ACC = AccRow(1, "s", "c", "USD", 100, 10000, 10000, 0, 10000, 2, 0)
SYM = SymRow(
    name="XAUUSD", description="", digits=2, point=0.01, spread=20,
    trade_mode=4, currency_profit="USD", currency_margin="USD",
    trade_contract_size=100.0, volume_min=0.01, volume_max=100.0,
    volume_step=0.01, filling_mode=1, visible=True,
)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _alerts_text(live_dir) -> str:
    path = os.path.join(str(live_dir), "alerts.log")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


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


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name.replace('.', '_')}_t183", REPO_ROOT / "scripts" / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------- 1) 异常层级（计划第 1 条）

def test_t183_presend_error_is_notconnected_not_dataunavailable():
    """PreSendUnavailableError(NotConnectedError)：发送前无法确认账户状态，
    请求未发出——归入断线类别（T141 blocked 路径），不再与
    DataUnavailableError（回报未知兜底）混同。"""
    from mt5gw.errors import (DataUnavailableError, GatewayError,
                              NotConnectedError, PreSendUnavailableError)

    assert issubclass(PreSendUnavailableError, NotConnectedError)
    assert issubclass(PreSendUnavailableError, GatewayError)
    assert not issubclass(PreSendUnavailableError, DataUnavailableError)
    assert not issubclass(DataUnavailableError, NotConnectedError)   # 旧类别不并


# --------------------------------------- 2) trade_send 分类（计划第 2 条）

def test_t183_trade_send_empty_account_raises_presend_order_never_sent():
    """account_info 为空 → trade_send 抛 PreSendUnavailableError（NotConnected
    子类、非 DataUnavailable），order_send 从未被调用（断言计数 0）。"""
    from mt5gw.errors import (DataUnavailableError, NotConnectedError,
                              PreSendUnavailableError)

    fake = FakeMT5()                       # account_info 无 behavior → None
    fake.last_error_value = (-6, "terminal not connected")
    conn = MT5Connection(GatewayConfig(), mt5_module=fake)
    conn.connect()
    with pytest.raises(PreSendUnavailableError) as ei:
        conn.trade_send({"action": 1}, lambda mode: None)
    assert isinstance(ei.value, NotConnectedError)
    assert not isinstance(ei.value, DataUnavailableError)
    assert "请求未发出" in str(ei.value)
    assert fake.calls("order_send") == 0   # 发送前拦下，order_send 零调用
    assert fake.calls("account_info") == 1
    conn.shutdown()


def test_t183_fuse_real_account_still_blocks_and_propagates():
    """保险丝不变：trade_mode=2（真实盘）且未解锁 → trade_send 仍抛
    LiveTradingBlockedError（非 NotConnected 子类——不落 blocked 路径，
    运行器照旧退出）；order_send 零调用。"""
    from mt5gw.account import AccountService
    from mt5gw.errors import LiveTradingBlockedError, NotConnectedError
    from mt5gw.symbols import SymbolCatalog
    from mt5gw.trading import TradingService

    fake = FakeMT5()
    fake.set_behavior("account_info", value=ACC._replace(trade_mode=2))
    fake.set_behavior("symbol_info", value=SYM)
    fake.set_behavior("symbol_select", value=True)
    fake.set_behavior("symbol_info_tick",
                      value=TickInfo(1698000000, 2648.50, 2649.20, 2648.8, 7))
    conn = MT5Connection(GatewayConfig(), mt5_module=fake)   # 未解锁实盘
    conn.connect()
    trading = TradingService(conn, GatewayConfig(),
                             AccountService(conn, fake),
                             SymbolCatalog(conn, fake), fake)
    with pytest.raises(LiveTradingBlockedError) as ei:
        trading.market_order("XAUUSD", "buy", 0.01)
    assert not isinstance(ei.value, NotConnectedError)   # 不并入 blocked 类别
    assert fake.calls("order_send") == 0
    conn.shutdown()


def test_t183_login_mismatch_still_blocks_and_propagates():
    """账号钉死不变：expected_login 不符 → trade_send 仍抛
    LiveTradingBlockedError（模拟盘账户读得到、fuse 也过，唯独账号不符）。"""
    from mt5gw.account import AccountService
    from mt5gw.errors import LiveTradingBlockedError
    from mt5gw.symbols import SymbolCatalog
    from mt5gw.trading import TradingService

    fake = FakeMT5()
    fake.set_behavior("account_info", value=ACC)          # login=1
    fake.set_behavior("symbol_info", value=SYM)
    fake.set_behavior("symbol_select", value=True)
    fake.set_behavior("symbol_info_tick",
                      value=TickInfo(1698000000, 2648.50, 2649.20, 2648.8, 7))
    cfg = GatewayConfig(expected_login=999)               # 钉死别的账号
    conn = MT5Connection(cfg, mt5_module=fake)
    conn.connect()
    trading = TradingService(conn, cfg, AccountService(conn, fake),
                             SymbolCatalog(conn, fake), fake)
    with pytest.raises(LiveTradingBlockedError):
        trading.market_order("XAUUSD", "buy", 0.01)
    assert fake.calls("order_send") == 0
    conn.shutdown()


# --------------------------- 3) 执行器不改：落 T141 blocked 路径（计划第 3 条）

def _mk_executor(broker, tmp_path, *, magic: int = MAGIC):
    from mt5quant.strategies.base import ExitRules
    from mt5live.executor import ExecutionAdapter, LiveExecutor

    return LiveExecutor(
        ExecutionAdapter(broker, broker, broker,
                         symbol=broker.symbol, magic=magic),
        symbol=broker.symbol, magic=magic, live_dir=str(tmp_path),
        rules=ExitRules(stop_loss_atr=1.5))


def test_t183_executor_presend_error_lands_blocked_path(tmp_path):
    """执行器下单路径收到 PreSendUnavailableError：与断线同样处理——poll
    不重抛，返回 blocked_broker_unavailable；本轮不开新仓；写告警。"""
    from mt5gw.errors import PreSendUnavailableError

    b = FakeBroker()
    b.error_on_market_order = PreSendUnavailableError(
        "发送前无法确认账户状态，请求未发出")
    ex = _mk_executor(b, tmp_path)
    d = mk_decision("XAUUSD|M15|2026-10-20T18:00:00", signal=1,
                    volume_lots=0.02)
    rr = ex.poll(d, NOW)                       # 不抛 = 运行器不退出
    assert rr.action == "blocked_broker_unavailable"
    assert rr.decision_id == d.decision_id
    # 执行器不改：error/告警按 T141 既有文案呈现（NotConnectedError 前缀
    # + 异常消息）；PreSendUnavailableError 的消息即"请求未发出"
    assert "NotConnectedError" in (rr.error or "")
    assert "请求未发出" in (rr.error or "")
    assert b.positions() == []                 # 请求未发出，零持仓
    assert "BROKER_UNAVAILABLE" in _alerts_text(tmp_path)


def test_t183_executor_presend_journal_rejected_not_connected(tmp_path):
    """journal 落终态 rejected（detail 注明 not_connected、请求未发出），
    不留 sent/unknown 未决项——下一 bar 的开仓不被挡板拦住。"""
    from mt5gw.errors import PreSendUnavailableError

    b = FakeBroker()
    b.error_on_market_order = PreSendUnavailableError(
        "发送前无法确认账户状态，请求未发出")
    ex = _mk_executor(b, tmp_path)
    d = mk_decision("XAUUSD|M15|2026-10-20T18:00:00", signal=1,
                    volume_lots=0.02)
    assert ex.poll(d, NOW).action == "blocked_broker_unavailable"
    lines = _journal_lines(tmp_path)
    rejected = [r for r in lines if r.get("status") == "rejected"
                and r.get("decision_id") == d.decision_id]
    assert rejected
    assert "not_connected" in rejected[-1]["detail"]
    assert "请求未发出" in rejected[-1]["detail"]
    assert not [r for r in lines if r.get("decision_id") == d.decision_id
                and r.get("status") in ("sent", "unknown")]
    assert ex._undecided == {}
    intent = _read_json(tmp_path / f"expected-{MAGIC}.json")
    assert intent["status"] == "confirmed"
    assert intent["volume"] == 0.0


def test_t183_presend_round_not_counted_as_t140_rejection(tmp_path):
    """边界钉：发送前空账户不是服务器拒单——不计 T140 连拒、不写
    ORDER_REJECTED/pause；同决策重投被 journal 终态幂等拦住（duplicate）。"""
    from mt5gw.errors import PreSendUnavailableError

    b = FakeBroker()
    b.error_on_market_order = PreSendUnavailableError(
        "发送前无法确认账户状态，请求未发出")
    ex = _mk_executor(b, tmp_path)
    d1 = mk_decision("XAUUSD|M15|2026-10-20T18:00:00", signal=1,
                     volume_lots=0.02)
    assert ex.poll(d1, NOW).action == "blocked_broker_unavailable"
    assert not (tmp_path / f"pause-{MAGIC}.json").exists()
    alerts = _alerts_text(tmp_path)
    assert "ORDER_REJECTED" not in alerts
    # 下一 bar：新决策照常进入执行（blocked 类别不挡后续轮次）
    d2 = mk_decision("XAUUSD|M15|2026-10-20T18:15:00", signal=1,
                     volume_lots=0.02)
    rr2 = ex.poll(d2, NOW + timedelta(minutes=15))
    assert rr2.action == "blocked_broker_unavailable"
    # 同决策重投：journal 终态幂等（duplicate，零新增请求）
    rr3 = ex.poll(d1, NOW + timedelta(seconds=30))
    assert rr3.action == "duplicate"
    assert len(b.market_orders) == 2           # 仅两次尝试记录，无重发
    assert b.positions() == []


# ------------------------- 4) 运行器不退出（计划逐字 TDD 场景，计划第 5 条）

class _RealTradingGW:
    """混合网关：trading 走真实 TradingService + 真实 MT5Connection（假终端
    account_info 恒空 = 发送前取不到账户），market/account 用 FakeBroker
    （行情/闸门数据照常——失败精确发生在发送前的账户检查）。"""

    def __init__(self, broker: FakeBroker, config: GatewayConfig):
        from mt5gw.account import AccountService
        from mt5gw.symbols import SymbolCatalog
        from mt5gw.trading import TradingService

        self._fake = FakeMT5()              # account_info 无 behavior → None
        self._fake.set_behavior("symbol_info", value=SYM)
        self._fake.set_behavior("symbol_select", value=True)
        self._fake.last_error_value = (-6, "terminal not connected")
        self._conn = MT5Connection(config, mt5_module=self._fake)
        self._conn.connect()
        self.account = broker
        self.market = broker
        self.trading = TradingService(
            self._conn, config, AccountService(self._conn, self._fake),
            SymbolCatalog(self._conn, self._fake), self._fake)

    @property
    def fake_terminal(self) -> FakeMT5:
        return self._fake

    def connect(self):
        pass

    def shutdown(self):
        self._conn.shutdown()


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


def test_t183_runner_presend_empty_account_no_exit_next_bar_decides(
        tmp_path, monkeypatch, capsys):
    """假终端发送前返回空账户（真实 trade_send 路径）→ 运行器不退出、
    下一 bar 正常决策（新决策继续进入执行）、order_send 从未被调用
    （断言调用计数 0）；journal 逐决策落 rejected/not_connected；无 halt。"""
    from mt5live.monitor import write_role_heartbeat

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lockshome"))
    mod = _load_script("live_runner.py")
    t1 = datetime(2026, 10, 28, 10, 0, 7)
    clock = {"now": t1}
    broker = FakeBroker("XAUUSD")
    broker.set_bars("XAUUSD", "M1",
                    bars_from_df(grid_bars(boundary_open(t1, tf_seconds=60),
                                           tf_seconds=60, last="up")))
    broker.quote_time = t1
    gw = _RealTradingGW(broker, GatewayConfig())

    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file",
                        staticmethod(lambda p: gw))

    def mutable_now():
        return clock["now"]

    monkeypatch.setattr(mod, "now_utc", mutable_now)
    env = _real_mode_env(tmp_path)
    live = env["live_dir"]
    write_role_heartbeat(live, "risk", interval=10.0, now=t1)

    def refresh_bars(now):
        broker.set_bars("XAUUSD", "M1",
                        bars_from_df(grid_bars(boundary_open(now, tf_seconds=60),
                                               tf_seconds=60, last="up")))
        broker.quote_time = now

    real_sleep = time.sleep
    step = {"n": 0}

    def fake_sleep(s):
        step["n"] += 1
        if step["n"] == 1:                    # 下一网格点：新 bar、新决策
            clock["now"] = t1 + timedelta(seconds=60)
            refresh_bars(clock["now"])
            write_role_heartbeat(live, "risk", interval=10.0,
                                 now=clock["now"])
        elif step["n"] == 2:                  # 再下一网格点：照常决策
            clock["now"] = t1 + timedelta(seconds=120)
            refresh_bars(clock["now"])
            write_role_heartbeat(live, "risk", interval=10.0,
                                 now=clock["now"])
        else:
            raise KeyboardInterrupt
        real_sleep(0)

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    rc = mod.main(["--config", env["config"], "--loop", "--no-spread-gate",
                   "--risk-config", env["risk"],
                   "--sessions-config", env["sessions"],
                   "--gateway-config", _write(tmp_path / "gw.json", "{}"),
                   "--poll-interval", "0.1"])
    assert rc == 0                            # 不退出（手动停止收尾）
    out = capsys.readouterr().out
    blocked_rounds = out.count("blocked_broker_unavailable")
    assert blocked_rounds >= 2               # 第 1 bar 与下一 bar 都在决策
    # 执行器不改：blocked 回报按 T141 文案呈现（异常消息透传）
    assert "发送前无法确认账户状态" in out
    assert "请求未发出" in out
    assert gw.fake_terminal.calls("order_send") == 0   # 从未被调用
    # journal：每个决策都落终态 rejected（not_connected、请求未发出），
    # 无 sent/unknown 未决项（不洗白、不挡下一 bar）
    lines = _journal_lines(live)
    rejected = [r for r in lines if r.get("status") == "rejected"]
    assert len(rejected) >= 2
    assert all("not_connected" in r["detail"] for r in rejected)
    assert not [r for r in lines if r.get("status") in ("sent", "unknown")]
    # 运行器活着、无 halt（发送前失败绝不升级成停机）
    hb = _read_json(os.path.join(live, f"heartbeat-{MAGIC}.json"))
    assert hb["runner_alive"] is True
    assert not os.path.exists(os.path.join(live, "halt.json"))
    assert "BROKER_UNAVAILABLE" in _alerts_text(live)
    assert broker.positions() == []           # 本轮不开新仓（净持仓为 0）
