"""T24 实盘接线集成测试（C1/C2/M9/M10/M1 实盘部分）。

跨组件 + 真实文件系统（tmp_path 的 live 目录）：信号运行器、执行器
（LiveExecutor）、风控进程（RiskProcess + RiskGate）、对账
（scripts/live_reconcile.run_once）在同一套文件契约上一起跑；终端替身
FakeBroker（含账户状态、成交流水、终端 bar、外部平仓）。绝不连真实
MT5、绝不真实下单。

覆盖计划 T24 的集成清单 9 条场景 + 新组件（TerminalBarsSource / RiskGate
/ LiveSizer / LiveExecutor）的行为测试 + `--dry-run --offline` 端到端；
T30 意图语义（C1/P8）、T31 只在开仓时定手数（C2/P9：ATR 逐 bar 变化不
调仓）、T32 全平指令编号协议（C3/P10：连续两次全平、闸门同协议、损坏
指令按有指令处理）与 scripts/live_retire.py 退役；T34 影子盘一条龙
（PaperBroker 全链路 + `--paper --dry-run --offline` 端到端冒烟）。Global
Constraints 的三类必备情形：ATR 逐 bar 变化（test_t31_*）、服务器端 SL/TP
出场（test_t30_* 与 test_t34_*，保持绿）、连续两次全平（test_t32_second_
flatten_*）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from mt5data.sessions import load_session_policy
from mt5data.store import save_bars
from mt5gw.models import (
    DEAL_REASON_CLIENT,
    DEAL_REASON_SL,
    DEAL_REASON_TP,
)
from mt5live.config import LiveConfig
from mt5live.commands import write_ack, write_flatten
from mt5live.executor import (
    ExecutionAdapter,
    LiveExecutor,
    validate_live_strategy,
)
from mt5live.gate import LiveSizer, RiskGate
from mt5live.reconcile import (
    ExpectedStateError,
    Intent,
    load_intents,
    write_intent,
)
from mt5live.riskproc import AccountSnapshot, RiskProcess, load_risk_config
from mt5live.paper import PaperBroker, PaperStateGateway, paper_state_path
from mt5live.runner import (
    MarketClosedError,
    SignalRunner,
    TerminalBarsSource,
    default_heartbeat_path,
    terminal_bars_loader,
    write_heartbeat,
)
from mt5quant.costs.spread import SpreadModel, SpreadStat
from mt5quant.sizing import RiskSizing, lots_for_risk
from mt5quant.strategies.base import ExitRules
from tests.fakes_live import (
    ClosingSignal,
    FakeBroker,
    FakeGateway,
    bars_from_df,
    boundary_open,
    grid_bars,
    mk_decision,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

MAGIC = 20261101
MAGIC_B = 20261102
SYMBOL = "XAUUSD"
TF_S = 900                     # M15
CONTRACT = 100.0
VOL = (0.01, 0.01, 100.0)      # min/step/max

# 轮次时刻：周三 2026-10-28（EDT），网格对齐 M15 + 7 秒容差后
T1 = datetime(2026, 10, 28, 10, 0, 7)
T2 = datetime(2026, 10, 28, 10, 15, 7)
T3 = datetime(2026, 10, 28, 10, 30, 7)
T4 = datetime(2026, 10, 28, 10, 45, 7)
T5 = datetime(2026, 10, 28, 11, 0, 7)

SESSIONS_TOML = """
version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 5
post_minutes = 60
exempt_symbols = ["BTCUSD"]
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
"""

RISK_TOML = """
version = 1
symbols = ["XAUUSD", "BTCUSD", "EURUSD"]
max_lots_per_order = 0.50
max_positions_per_symbol = 3
max_usd_exposure = 100000.0
max_orders_per_minute = 6
daily_loss_cap_usd = 500.0
"""


def write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_script", REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_json(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def heartbeats(live_dir) -> dict:
    return read_json(os.path.join(live_dir, f"heartbeat-{MAGIC}.json"))


def alerts_text(live_dir) -> str:
    p = os.path.join(live_dir, "alerts.log")
    with open(p, encoding="utf-8") as f:
        return f.read()


def pause_file(live_dir, magic: int = MAGIC) -> str:
    """T30/D20：对账暂停按策略隔离 → pause-<magic>.json（全局 pause.json 只留给人工）。"""
    return os.path.join(live_dir, f"pause-{magic}.json")


def pause_state(live_dir, magic: int = MAGIC) -> bool | None:
    p = pause_file(live_dir, magic)
    return read_json(p)["paused"] if os.path.exists(p) else None


def intent_of(live_dir, magic: int = MAGIC) -> dict:
    return read_json(os.path.join(live_dir, f"expected-{magic}.json"))


class LiveStack:
    """装配一个策略的全部实盘组件（FakeBroker 终端 + tmp 文件系统）。"""

    def __init__(self, tmp_path, *, magic: int = MAGIC, symbol: str = SYMBOL,
                 exit_rules: ExitRules | None = None,
                 volume_lots: float | None = None,
                 risk_fraction: float = 0.005,
                 spread_model: SpreadModel | None = None,
                 balance: float = 10_000.0):
        self.magic = magic
        self.symbol = symbol
        self.live_dir = str(tmp_path / "live")
        self.data_root = str(tmp_path / "data")
        os.makedirs(self.live_dir, exist_ok=True)
        os.makedirs(self.data_root, exist_ok=True)
        self.specs_path = write_text(
            tmp_path / "data" / "specs" / "latest.json", json.dumps({
                symbol: {"contract_size": CONTRACT, "volume_min": VOL[0],
                         "volume_step": VOL[1], "volume_max": VOL[2]},
            }))
        self.policy = load_session_policy(
            write_text(tmp_path / "sessions.toml", SESSIONS_TOML))
        self.risk_cfg = load_risk_config(
            write_text(tmp_path / "risk.toml", RISK_TOML))
        self.process = RiskProcess(self.risk_cfg, live_dir=self.live_dir,
                                   session_policy=self.policy)
        self.broker = FakeBroker(symbol, balance=balance,
                                 contract_size=CONTRACT)
        self.gate = RiskGate(
            self.process, account=self.broker, market=self.broker,
            specs_path=self.specs_path, live_dir=self.live_dir,
            spread_model=spread_model, session_policy=self.policy)
        self.cfg = LiveConfig(
            symbol=symbol, timeframe="M15",
            strategy="tests.fakes_live:ClosingSignal", params={}, magic=magic,
            lookback_bars=30, volume_lots=volume_lots,
            risk_fraction=(None if volume_lots is not None else risk_fraction),
            data_root=self.data_root, trigger_delay_s=5.0)
        self.strategy = ClosingSignal(
            exit_rules if exit_rules is not None else DEFAULT_RULES)
        self._bars_df = None
        self.runner = SignalRunner(
            self.cfg, self.strategy,
            bars_loader=lambda root, s, tf: self._bars_df,
            quote_fn=lambda s: (self.broker.bid, self.broker.ask))
        self.adapter = ExecutionAdapter(
            self.broker, self.broker, self.broker, symbol=symbol, magic=magic)
        self.sizer = LiveSizer(
            RiskSizing(risk_fraction or 0.005, basis="equity"),
            account=self.broker, specs_path=self.specs_path)
        self.executor = self.build_executor()
        self.hb_path = os.path.join(self.live_dir, f"heartbeat-{magic}.json")

    def build_executor(self, *, fresh_adapter: bool = False) -> LiveExecutor:
        """新起一个执行器（= 进程重启：去重集清空、状态从文件恢复）。

        fresh_adapter=True 连执行适配器也重建（完全模拟新进程，内存去重
        集、持仓视图全部从零——只剩文件系统状态）。
        """
        if fresh_adapter:
            self.adapter = ExecutionAdapter(
                self.broker, self.broker, self.broker, symbol=self.symbol,
                magic=self.magic)
        return LiveExecutor(
            self.adapter, symbol=self.symbol, magic=self.magic,
            live_dir=self.live_dir, rules=self.strategy.exit_rules,
            gate=self.gate, sizer=self.sizer)

    def touch_heartbeat(self, now: datetime):
        """只刷新心跳（不决策）——模拟运行器在两次决策之间继续轮询存活。"""
        write_heartbeat(self.hb_path, config=self.cfg, decision=None,
                        error=None, dry_run=False, now=now)

    def round(self, now: datetime, last: str = "up",
              *, executor: LiveExecutor | None = None, tr: float = 2.0):
        """一轮 = 运行器决策 + 执行器轮（flatten/闸门/下单/expected）+ 心跳。

        tr：K 线振幅（TR≈tr → 该轮决策 ATR≈tr；T31 逐 bar ATR 变化用）。
        """
        self._bars_df = grid_bars(boundary_open(now), last=last, tr=tr)
        decision = self.runner.decide(now)
        rr = (executor or self.executor).poll(decision, now)
        write_heartbeat(self.hb_path, config=self.cfg, decision=decision,
                        error=rr.error, dry_run=False, now=now)
        return decision, rr

    def risk_cycle(self, now: datetime):
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        snap = AccountSnapshot(
            currency="USD", positions=tuple(self.broker.positions()),
            deals=tuple(self.broker.deals(day_start, now)))
        return self.process.run_cycle(snap, now)

    def reconcile_once(self, now: datetime | None = None):
        mod = load_script("live_reconcile")
        return mod.run_once(self.live_dir, FakeGateway(self.broker),
                            volume_tolerance=1e-6, now=now)


def expected_lots(decision) -> float:
    """T23 同一函数按权益 10000、止损距离 = sl_atr×ATR 复算手数。"""
    return lots_for_risk(10_000.0, 0.005, decision.sl_atr * decision.atr,
                         CONTRACT, VOL[0], VOL[1], VOL[2])


def reconcile_all(*stacks, now: datetime | None = None):
    """跨全部 stack 的对账：真实世界只有一个账户——合并各 FakeBroker 的
    持仓视图再跑 live_reconcile.run_once（多策略共用 live 目录的测试用）。"""
    mod = load_script("live_reconcile")

    class _MergedAccount:
        def __init__(self, brokers):
            self._brokers = brokers

        def positions(self, symbol=None):
            out = []
            for b in self._brokers:
                out.extend(b.positions(symbol))
            return out

    class _MergedGateway:
        def __init__(self, brokers):
            self.account = _MergedAccount(brokers)

        def shutdown(self):
            pass

    gw = _MergedGateway([s.broker for s in stacks])
    return mod.run_once(stacks[0].live_dir, gw, volume_tolerance=1e-6, now=now)


# ================================================================ 场景 1
# 正常开平仓；重复触发不重复下单；进程重启不重复下单

def test_scenario_1_open_close_duplicate_and_restart(tmp_path):
    stack = LiveStack(tmp_path)

    d1, rr1 = stack.round(T1, "up")
    assert d1.signal == 1
    assert rr1.action == "open"
    lots = expected_lots(d1)
    assert lots == pytest.approx(0.16)          # 10000×0.5% / (1.5×2×100)
    pos = stack.broker.positions(SYMBOL)
    assert len(pos) == 1 and pos[0].side == "buy"
    assert pos[0].volume == pytest.approx(lots)
    assert stack.broker.market_orders[0]["magic"] == MAGIC

    # expected-<magic>.json（T30 意图）：对账进程的目标持仓契约
    exp = read_json(os.path.join(stack.live_dir, f"expected-{MAGIC}.json"))
    assert exp["symbol"] == SYMBOL and exp["magic"] == MAGIC
    assert exp["side"] == "buy"
    assert exp["volume"] == pytest.approx(lots)
    assert exp["status"] == "confirmed" and exp["cause"] == d1.decision_id

    # 重复触发（同一 decision 再投递）：duplicate，不再下单
    rr_dup = stack.executor.poll(d1, T1 + timedelta(seconds=30))
    assert rr_dup.action == "duplicate"
    assert len(stack.broker.market_orders) == 1

    # 进程重启（新执行器+新适配器，去重集清空）：净仓已达标 → hold，不重复下单
    ex2 = stack.build_executor(fresh_adapter=True)
    rr_restart = ex2.poll(d1, T1 + timedelta(seconds=60))
    assert rr_restart.action == "hold"
    assert len(stack.broker.market_orders) == 1

    # 平仓：信号归零
    d2, rr2 = stack.round(T2, "flat", executor=ex2)
    assert d2.signal == 0 and rr2.action == "close"
    assert stack.broker.positions(SYMBOL) == []
    exp2 = read_json(os.path.join(stack.live_dir, f"expected-{MAGIC}.json"))
    assert exp2["volume"] == 0.0 and exp2["status"] == "confirmed"
    assert heartbeats(stack.live_dir)["last_error"] is None


# ================================================================ 场景 2
# killSwitch：之后不再开仓，平仓照常

def test_scenario_2_kill_switch_blocks_open_allows_close(tmp_path):
    stack = LiveStack(tmp_path)
    _, rr1 = stack.round(T1, "up")
    assert rr1.action == "open"

    (Path(stack.live_dir) / "killSwitch").write_text("halt", encoding="utf-8")

    # 平仓不受限（禁止时段/停机都只禁开仓）
    _, rr2 = stack.round(T2, "flat")
    assert rr2.action == "close"
    assert stack.broker.positions(SYMBOL) == []

    # 开仓被拒：原因写心跳 last_error 与 alerts.log，不下单
    n_orders = len(stack.broker.market_orders)
    _, rr3 = stack.round(T3, "up")
    assert rr3.action == "risk_rejected"
    assert "kill_switch" in rr3.error
    assert len(stack.broker.market_orders) == n_orders
    hb = heartbeats(stack.live_dir)
    assert "kill_switch" in hb["last_error"]
    assert "RISK_REJECT" in alerts_text(stack.live_dir)
    # killSwitch 只停机不自动平仓（蓝图 §4）：闸门拒绝不得写 flatten 指令
    assert not os.path.exists(os.path.join(stack.live_dir, "commands",
                                           "flatten.json"))

    # 风控进程同时落 halt.json（一键停机的文件级后果）
    stack.risk_cycle(T3)
    assert read_json(os.path.join(stack.live_dir, "halt.json"))[
        "reason"] == "kill_switch"


# ================================================================ 场景 3
# 日亏达到上限：写 flatten 与 halt → 执行器全平并确认 → 不再开仓

def test_scenario_3_daily_loss_flatten_chain(tmp_path):
    stack = LiveStack(tmp_path)
    _, rr1 = stack.round(T1, "up")
    assert rr1.action == "open"
    assert len(stack.broker.positions(SYMBOL)) == 1

    stack.broker.seed_deal(profit=-600.0, magic=MAGIC, time=T2)
    stack.touch_heartbeat(T2)             # 运行器存活（决策间心跳照常刷新）
    res = stack.risk_cycle(T2)
    assert res.daily_loss_breached and res.flatten_written and res.halt_written
    flatten_path = os.path.join(stack.live_dir, "commands", "flatten.json")
    assert read_json(flatten_path)["command"] == "flatten_all"
    cmd_id = read_json(flatten_path)["id"]
    assert read_json(os.path.join(stack.live_dir, "halt.json"))[
        "reason"] == "daily_loss_cap"

    # 执行器下一轮：先消费 flatten → 全平 + 写 flatten.ack-<magic>
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "flatten"
    assert stack.broker.positions(SYMBOL) == []
    ack = read_json(os.path.join(stack.live_dir, "commands",
                                 f"flatten.ack-{MAGIC}"))
    assert ack["magic"] == MAGIC

    # 风控进程：所有登记 magic 都确认 → 归档指令文件
    stack.touch_heartbeat(T2 + timedelta(seconds=30))
    stack.risk_cycle(T2 + timedelta(seconds=30))
    assert not os.path.exists(flatten_path)
    archived = list((Path(stack.live_dir) / "commands" / "archive").glob(
        "flatten-*.json"))
    assert archived, "flatten.json 应被归档而不是留在原地"
    # R10/T58 C-3：自动归档删除前，指令与 ack 副本已保全在
    # drill-archive/<command_id>/（演练证据只增不删）
    drill_dir = Path(stack.live_dir) / "drill-archive" / cmd_id
    assert read_json(drill_dir / "flatten.json")["id"] == cmd_id
    drill_ack = read_json(drill_dir / f"flatten.ack-{MAGIC}")
    assert drill_ack["id"] == cmd_id and drill_ack["magic"] == MAGIC

    # 停机后不再开仓
    n_orders = len(stack.broker.market_orders)
    _, rr3 = stack.round(T3, "up")
    assert rr3.action == "risk_rejected"
    assert "halted" in rr3.error
    assert len(stack.broker.market_orders) == n_orders


# ================================================================ 场景 4
# 冬令时日期 22:10 UTC 禁入、夏令时日期 21:10 UTC 禁入

def test_scenario_4_dst_forbidden_windows(tmp_path):
    # 冬令时（2026-12-15，EST）：换日 22:00 UTC → 22:10 禁入
    stack = LiveStack(tmp_path)
    _, rr = stack.round(datetime(2026, 12, 15, 22, 10, 7), "up")
    assert rr.action == "risk_rejected"
    assert "forbidden_session" in rr.error
    assert stack.broker.positions(SYMBOL) == []

    # 夏令时（2026-07-15，EDT）：换日 21:00 UTC → 21:10 禁入；
    # 且禁入窗内平仓照常（窗前开的仓）
    stack2 = LiveStack(tmp_path)
    _, rr_open = stack2.round(datetime(2026, 7, 15, 20, 30, 7), "up")
    assert rr_open.action == "open"
    _, rr_close = stack2.round(datetime(2026, 7, 15, 21, 10, 7), "flat")
    assert rr_close.action == "close"
    _, rr_reject = stack2.round(datetime(2026, 7, 15, 21, 25, 7), "up")
    assert rr_reject.action == "risk_rejected"
    assert "forbidden_session" in rr_reject.error
    assert stack2.broker.positions(SYMBOL) == []


# ================================================================ 场景 5
# 对账不一致 → pause-<magic>（T30/D20：按策略隔离）→ 不再开仓；恢复一致后自动解除

def test_scenario_5_reconcile_pause_and_resume(tmp_path):
    stack = LiveStack(tmp_path)
    _, rr1 = stack.round(T1, "flat")
    assert rr1.action == "hold"

    # 账户出现不该有的持仓（该 magic 无目标）→ 对账不一致 → pause-<magic>
    stack.broker.seed_position(side="buy", volume=0.05, magic=MAGIC)
    rc, _ = stack.reconcile_once()
    assert rc == 1
    assert read_json(pause_file(stack.live_dir))["paused"]
    assert not os.path.exists(os.path.join(stack.live_dir, "pause.json"))  # 全局只留给人工

    # pause 期间同向信号：T31/D22 持仓不调仓 → 无开仓腿、不下单（hold）
    n_orders = len(stack.broker.market_orders)
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "hold"
    assert len(stack.broker.market_orders) == n_orders

    # pause 期间反向信号：平仓腿不受限（清掉不一致持仓），开仓腿被拒
    _, rr3 = stack.round(T3, "down")
    assert rr3.action == "risk_rejected"
    assert "reconcile_paused" in rr3.error
    assert any(o.kind == "close" for o in rr3.orders)
    assert stack.broker.positions(SYMBOL) == []       # 杂仓已被平仓腿清掉
    assert len(stack.broker.market_orders) == n_orders

    # 反向轮后意图=实际（0 手空仓）→ 对账一致 → pause-<magic>.json 解除
    rc2, _ = stack.reconcile_once()
    assert rc2 == 0
    assert not read_json(pause_file(stack.live_dir))["paused"]
    _, rr4 = stack.round(T4, "up")
    assert rr4.action == "open"


# ================================================================ 场景 6
# 心跳超时 → 全局停机（每策略一个心跳文件，任一超时即停）

def test_scenario_6_heartbeat_timeout_global_halt(tmp_path):
    stack_a = LiveStack(tmp_path)                       # magic A
    stack_b = LiveStack(tmp_path, magic=MAGIC_B)        # magic B，同 live 目录

    stack_b.round(T1, "up")                             # B 心跳停在 T1
    stack_a.round(T5, "up")                             # A 心跳新鲜（T5）
    assert os.path.exists(stack_a.hb_path)
    assert os.path.exists(stack_b.hb_path)

    res = stack_a.risk_cycle(T5)                        # B 距今 3600s > 120s
    assert not res.heartbeat_ok
    assert read_json(os.path.join(stack_a.live_dir, "halt.json"))[
        "reason"] == "heartbeat_timeout"
    # 超时的那个心跳被点名并改写 runner_alive=false
    hb_b = read_json(os.path.join(stack_a.live_dir,
                                  f"heartbeat-{MAGIC_B}.json"))
    assert hb_b["runner_alive"] is False
    assert "RISK_HEARTBEAT_TIMEOUT" in alerts_text(stack_a.live_dir) or \
        "HEARTBEAT_TIMEOUT" in alerts_text(stack_a.live_dir)

    # 全局停机：A 反向信号 → 平仓腿照常、开仓腿被拒（halt 之下不开新仓）
    n_open = len(stack_a.broker.market_orders)
    _, rr = stack_a.round(T5 + timedelta(minutes=15), "down")
    assert rr.action == "risk_rejected"
    assert "halted" in rr.error
    assert stack_a.broker.positions(SYMBOL) == []     # 平仓不受限
    assert len(stack_a.broker.close_calls) == 1       # 只有平仓那一笔
    assert len(stack_a.broker.market_orders) == n_open  # 没有新开仓


# ================================================================ 场景 7
# 周末与每日维护时段：不报错、不下单（报价陈旧 → 休市静默跳过）

def _terminal_runner(stack: LiveStack, source: TerminalBarsSource):
    return SignalRunner(
        stack.cfg, ClosingSignal(),
        bars_loader=terminal_bars_loader(source, n=40),
        quote_fn=lambda s: (stack.broker.bid, stack.broker.ask))


def test_scenario_7_weekend_and_maintenance_silent_skip(tmp_path):
    stack = LiveStack(tmp_path)
    friday_last = pd.Timestamp("2026-10-23 17:45")      # 周五最后一根 M15
    stack.broker.set_bars(SYMBOL, "M15",
                          bars_from_df(grid_bars(friday_last, last="up")))

    for now in (datetime(2026, 10, 25, 12, 0, 7),      # 周日（周末休市）
                datetime(2026, 10, 28, 21, 30, 7)):    # 周三 21:30（每日维护）
        # 报价停在周五最后一根收盘（17:45+15m=18:00），对两个时刻都已陈旧
        stack.broker.quote_time = (friday_last + pd.Timedelta(
            minutes=15)).to_pydatetime()
        source = TerminalBarsSource(stack.broker, stale_quote_s=120.0,
                                    clock=lambda: now)
        runner = _terminal_runner(stack, source)
        decision = runner.decide(now)                   # 休市 → 静默跳过
        assert decision is None
        rr = stack.executor.poll(None, now)
        assert rr.action == "none" and rr.error is None
        assert stack.broker.market_orders == []
        write_heartbeat(stack.hb_path, config=stack.cfg, decision=None,
                        error=rr.error, dry_run=False, now=now)
        assert heartbeats(stack.live_dir)["last_error"] is None


def test_terminal_source_lagging_bars_retry_then_error(tmp_path):
    """有行情但等不到预期 bar：max_wait_s 内重试，仍无 → 报"数据滞后"。"""
    stack = LiveStack(tmp_path)
    now = datetime(2026, 10, 28, 10, 0, 7)
    stack.broker.quote_time = now                       # 行情新鲜
    # 终端只给到上一根（滞后一根）
    lagging = boundary_open(now) - pd.Timedelta(seconds=TF_S)
    stack.broker.set_bars(SYMBOL, "M15", bars_from_df(grid_bars(lagging)))

    calls = {"n": 0}
    orig = stack.broker.bars

    def counting_bars(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    stack.broker.bars = counting_bars
    source = TerminalBarsSource(stack.broker, stale_quote_s=120.0,
                                max_wait_s=0.2, poll_interval_s=0.05,
                                clock=lambda: now)
    with pytest.raises(ValueError, match="数据滞后"):
        source.closed_bars(SYMBOL, "M15", 40)
    assert calls["n"] >= 2                              # 确实在重试


def test_terminal_source_drops_forming_bar(tmp_path):
    stack = LiveStack(tmp_path)
    now = datetime(2026, 10, 28, 10, 0, 7)
    stack.broker.quote_time = now
    df = grid_bars(boundary_open(now), last="up")
    forming = pd.DataFrame([{                       # 正在形成的 10:00 bar
        "time": pd.Timestamp("2026-10-28 10:00"), "open": 100.0,
        "high": 101.0, "low": 99.0, "close": 100.5,
        "tick_volume": 1, "spread": 0, "real_volume": 0}])
    all_bars = bars_from_df(pd.concat([df, forming], ignore_index=True))
    stack.broker.set_bars(SYMBOL, "M15", all_bars)
    source = TerminalBarsSource(stack.broker, stale_quote_s=120.0,
                                clock=lambda: now)
    out = source.closed_bars(SYMBOL, "M15", 40)
    assert out is not None and len(out) == 40
    assert pd.Timestamp(out["time"].iloc[-1]) == boundary_open(now)


# ================================================================ 场景 8
# 点差尖峰：跳过开仓（当前点差 > 倍数 × 模型该纽约小时 p90）

def _spread_model(hour_ny: int) -> SpreadModel:
    # n_days：R6/C4 起点差闸门按覆盖天数判缺（n_days ≥ 20 才算有统计）
    return SpreadModel(
        {SYMBOL: {hour_ny: SpreadStat(median=0.02, p90=0.05, p99=0.08,
                                      n=100, n_days=100)}},
        {SYMBOL: 0.02}, mode="absolute")


def test_scenario_8_spread_spike_skips_open(tmp_path):
    # 2026-10-28 10:00 UTC = 纽约本地 06:00（EDT）
    stack = LiveStack(tmp_path, spread_model=_spread_model(6))

    stack.broker.set_quote(100.0, 100.2)                # 点差 0.2 > 3×0.05
    _, rr1 = stack.round(T1, "up")
    assert rr1.action == "risk_rejected"
    assert "spread_gate" in rr1.error
    assert stack.broker.market_orders == []
    assert "spread_gate" in heartbeats(stack.live_dir)["last_error"]

    stack.broker.set_quote(100.0, 100.03)               # 点差 0.03 ≤ 0.15
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "open"


# ================================================================ 场景 9
# after_signal_change：止损后信号仍为 1 不重入；信号归零再变 1 后重入；
# 重启后状态保持

ASC_RULES = ExitRules(stop_loss_atr=1.5, take_profit_atr=2.5,
                      reentry="after_signal_change")
DEFAULT_RULES = ExitRules(stop_loss_atr=1.5, take_profit_atr=2.5)  # immediate


def test_scenario_9_after_signal_change(tmp_path):
    stack = LiveStack(tmp_path, exit_rules=ASC_RULES)
    d1, rr1 = stack.round(T1, "up")
    assert rr1.action == "open"
    ticket = stack.broker.positions(SYMBOL)[0].ticket

    # 止损触发（外部平仓，不经执行器）
    stack.broker.close_external(ticket, profit=-30.0, comment="[sl]")

    # 信号仍为 +1：不重入（blocked_dir 持久化；下单数停在首开那 1 笔）
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "blocked_reentry"
    assert len(stack.broker.market_orders) == 1
    state = read_json(os.path.join(stack.live_dir, f"state-{MAGIC}.json"))
    assert state["blocked_dir"] == 1

    # 重启（新执行器+新适配器）后状态保持：仍不重入
    ex2 = stack.build_executor(fresh_adapter=True)
    _, rr3 = stack.round(T3, "up", executor=ex2)
    assert rr3.action == "blocked_reentry"
    assert len(stack.broker.market_orders) == 1

    # 信号归零 → 解禁
    _, rr4 = stack.round(T4, "flat", executor=ex2)
    state4 = read_json(os.path.join(stack.live_dir, f"state-{MAGIC}.json"))
    assert state4["blocked_dir"] == 0

    # 信号再变 +1 → 重入
    _, rr5 = stack.round(T5, "up", executor=ex2)
    assert rr5.action == "open"
    assert len(stack.broker.market_orders) == 2


def test_immediate_reentry_reopens_after_stop(tmp_path):
    """对照组：reentry=immediate（默认）止损后同信号照常重开。"""
    stack = LiveStack(tmp_path)                         # 默认 immediate
    _, rr1 = stack.round(T1, "up")
    ticket = stack.broker.positions(SYMBOL)[0].ticket
    stack.broker.close_external(ticket, profit=-30.0, comment="[sl]")
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "open"
    assert len(stack.broker.market_orders) == 2


# ================================================================ T30 意图语义
#（C1/P8 反转：expected-<magic>.json 改为意图——下单前 pending、订单后 confirmed、
# 服务器端 SL/TP/SO 出场按成交原因识别；暂停按策略隔离）

def _run_protective_exit_case(tmp_path, *, reason: int, comment: str,
                              profit: float) -> None:
    """保护性出场（SL/TP/SO）：识别为已解释 → 意图=实际净持仓；对账一致无暂停；
    immediate 下一根照常重开、after_signal_change 禁止重入；另一策略不受影响。"""
    for label, rules, expect in (
            ("immediate", DEFAULT_RULES, "open"),
            ("after_signal_change", ASC_RULES, "blocked_reentry")):
        base = tmp_path / label
        stack = LiveStack(base, exit_rules=rules)
        other = LiveStack(base, magic=MAGIC_B)      # 同 live 目录的另一策略
        _, rr1 = stack.round(T1, "up")
        assert rr1.action == "open"
        ticket = stack.broker.positions(SYMBOL)[0].ticket
        stack.broker.close_external(ticket, profit=profit, comment=comment,
                                    reason=reason)

        # 执行器下一轮开头识别外部出场：reason 已解释 → 意图改为实际净持仓
        _, rr2 = stack.round(T2, "up")
        assert rr2.action == expect
        if expect == "blocked_reentry":
            # 禁入轮不下单：意图保持"已解释的空仓"
            it = intent_of(stack.live_dir)
            assert it["status"] == "confirmed"
            assert it["cause"].startswith("external_exit:")
            assert it["volume"] == 0.0
        # 对账一致、无暂停（本 magic 与全局都没有；P8 死锁反转）
        rc, _ = stack.reconcile_once(now=T2 + timedelta(seconds=30))
        assert rc == 0
        assert pause_state(stack.live_dir) in (None, False)
        assert not os.path.exists(os.path.join(stack.live_dir, "pause.json"))
        # 另一策略照常开仓：一个策略的出场不该停掉别人
        _, rr_b = other.round(T2 + timedelta(minutes=15), "up")
        assert rr_b.action == "open"


def test_t30_sl_exit_reconcile_consistent_no_pause(tmp_path):
    """止损出场（reason=SL）后对账一致、无暂停；两种重入模式行为正确。"""
    _run_protective_exit_case(tmp_path, reason=DEAL_REASON_SL, comment="[sl]",
                              profit=-30.0)


def test_t30_tp_exit_reconcile_consistent_no_pause(tmp_path):
    """止盈出场（reason=TP）同上。"""
    _run_protective_exit_case(tmp_path, reason=DEAL_REASON_TP, comment="[tp]",
                              profit=30.0)


def test_t30_sl_exit_transient_pause_auto_releases(tmp_path):
    """P8 原序：对账抢在执行器识别之前运行 → 只写本 magic 的短暂暂停，
    执行器下一轮识别后自动解除（不再永久死锁）。"""
    stack = LiveStack(tmp_path)
    other = LiveStack(tmp_path, magic=MAGIC_B)
    stack.round(T1, "up")
    ticket = stack.broker.positions(SYMBOL)[0].ticket
    stack.broker.close_external(ticket, profit=-30.0, comment="[sl]",
                                reason=DEAL_REASON_SL)
    # 识别发生前对账：意图（0.16 手）≠ 实际（空仓）→ 只暂停本 magic
    rc, _ = reconcile_all(stack, other, now=T1 + timedelta(seconds=30))
    assert rc == 1
    assert read_json(pause_file(stack.live_dir))["paused"] is True
    assert not os.path.exists(os.path.join(stack.live_dir, "pause.json"))
    # 另一策略不受这个（本 magic 的）暂停影响
    _, rr_b = other.round(T1 + timedelta(minutes=5), "up")
    assert rr_b.action == "open"
    # 执行器轮：识别止损出场 → 意图=空仓；开仓腿被残留暂停挡下（一轮而已）
    _, rr = stack.round(T2, "up")
    assert rr.action == "risk_rejected"
    assert "reconcile_paused" in rr.error
    assert intent_of(stack.live_dir)["cause"].startswith("external_exit:")
    # 意图已解释 → 下一轮对账自动解除暂停
    rc2, _ = reconcile_all(stack, other, now=T2 + timedelta(seconds=30))
    assert rc2 == 0
    assert not read_json(pause_file(stack.live_dir))["paused"]
    # 暂停已解除：下一根照常重开（immediate）
    _, rr3 = stack.round(T3, "up")
    assert rr3.action == "open"


def test_t30_manual_close_pauses_only_this_magic_until_acked(tmp_path, monkeypatch):
    """人工平仓（reason=CLIENT）→ 只写 pause-<本magic>；另一 magic 照常开仓；
    live_ack_external 登记实际持仓为意图后恢复。"""
    stack = LiveStack(tmp_path)
    other = LiveStack(tmp_path, magic=MAGIC_B)
    _, rr1 = stack.round(T1, "up")
    assert rr1.action == "open"
    ticket = stack.broker.positions(SYMBOL)[0].ticket
    stack.broker.close_external(ticket, profit=5.0, comment="manual",
                                reason=DEAL_REASON_CLIENT)

    # 对账（30 秒一轮 << bar 间隔）：意图 0.16 vs 实际 0 → 只暂停本 magic
    rc, _ = stack.reconcile_once(now=T2)
    assert rc == 1
    assert read_json(pause_file(stack.live_dir))["paused"] is True
    assert not os.path.exists(os.path.join(stack.live_dir, "pause.json"))
    # 另一 magic 照常开仓
    _, rr_b = other.round(T2 + timedelta(minutes=5), "up")
    assert rr_b.action == "open"

    # 本 magic 的执行器轮：CLIENT 不在已解释集合 → 不改意图、写告警、记入
    # 未决出场（C2/R6）；重开被**执行器自己的防线**挡下（对账 pause 是叠加
    # 的第二重，不是唯一防线）
    _, rr2 = stack.round(T2 + timedelta(minutes=10), "up")
    assert rr2.action == "blocked_external_exit"
    assert "external_exit_pending" in rr2.error
    assert read_json(pause_file(stack.live_dir))["paused"] is True  # 叠加防线仍在
    it = intent_of(stack.live_dir)
    assert it["volume"] == pytest.approx(0.16)      # 意图未被洗白
    assert not it["cause"].startswith("external_exit:")
    assert "EXTERNAL_EXIT" in alerts_text(stack.live_dir)

    # 人工确认：把当前实际持仓（空仓）登记为意图（cause=ack_external，
    # N-C1/R7 绑定协议：绑定未决事件 ticket + 确认时刻 + 净仓快照）
    ack = load_script("live_ack_external")
    rc_ack = _ack_via_script(ack, monkeypatch, stack,
                             now=T2 + timedelta(minutes=12),
                             reason="人工平仓已核对")
    assert rc_ack == 0
    it2 = intent_of(stack.live_dir)
    assert it2["status"] == "confirmed" and it2["cause"] == "ack_external"
    assert it2["volume"] == 0.0
    assert it2["confirmed_events"]["tickets"] == [ticket]
    assert "ACK_EXTERNAL" in alerts_text(stack.live_dir)

    # 对账一致 → 暂停解除 → 恢复开仓
    rc2, _ = reconcile_all(stack, other, now=T2 + timedelta(minutes=11))
    assert rc2 == 0
    assert not read_json(pause_file(stack.live_dir))["paused"]
    _, rr3 = stack.round(T2 + timedelta(minutes=15), "up")
    assert rr3.action == "open"


def test_t30_reconcile_in_pending_window_does_not_pause(tmp_path):
    """对账恰在"pending 已写、订单未回"时运行（FakeBroker 下单钩子里调用）
    → 宽限期内不判 → 不暂停（C1 的短暂误报反转）。"""
    stack = LiveStack(tmp_path)
    seen = {}
    orig = stack.broker.market_order

    def hooked(*a, **kw):
        # 此刻执行器已写 Intent(0.16, pending)，订单尚未回报，实际仍空仓
        seen["rc"], seen["res"] = stack.reconcile_once(now=T1)
        return orig(*a, **kw)

    stack.broker.market_order = hooked
    _, rr = stack.round(T1, "up")
    assert rr.action == "open"
    assert seen["rc"] == 0                       # pending 宽限 → 不判 → 不暂停
    assert seen["res"].ok
    assert pause_state(stack.live_dir) in (None, False)
    assert not os.path.exists(os.path.join(stack.live_dir, "pause.json"))
    # 订单回报后意图 confirmed=成交量 → 对账仍一致
    rc2, _ = stack.reconcile_once(now=T1 + timedelta(seconds=5))
    assert rc2 == 0


def test_t30_partial_fill_intent_is_filled_volume_with_alert(tmp_path):
    """部分成交 → 意图 = 成交量（confirmed），写告警；下一轮补足。"""
    stack = LiveStack(tmp_path)
    stack.broker.partial_volume = 0.08           # 请求 0.16 只成 0.08
    d1, rr1 = stack.round(T1, "up")
    assert rr1.action == "open"
    assert any(o.partial for o in rr1.orders)
    it = intent_of(stack.live_dir)
    assert it["status"] == "confirmed"
    assert it["cause"] == d1.decision_id
    assert it["volume"] == pytest.approx(0.08)   # 意图=成交量（如实）
    assert "PARTIAL_FILL" in alerts_text(stack.live_dir)
    # 意图=实际 → 对账一致，不暂停
    rc, _ = stack.reconcile_once(now=T1 + timedelta(seconds=5))
    assert rc == 0
    assert pause_state(stack.live_dir) in (None, False)

    # 下一轮（D22/T31：持仓期间不调仓）：差额不再自动补足——hold、不追加
    # 下单；意图=成交量与实际一致，对账照常通过
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "hold"
    assert len(stack.broker.market_orders) == 1
    assert sum(p.volume for p in stack.broker.positions(SYMBOL)) == \
        pytest.approx(0.08)
    assert intent_of(stack.live_dir)["volume"] == pytest.approx(0.08)
    rc, _ = stack.reconcile_once(now=T2 + timedelta(seconds=5))
    assert rc == 0
    assert pause_state(stack.live_dir) in (None, False)


def test_t30_legacy_expected_format_readable_new_writes_new_format(tmp_path):
    """旧格式 expected-*.json 能读（兼容），写出的都是新格式。"""
    live = tmp_path / "live"
    live.mkdir(parents=True)
    ts = datetime(2026, 10, 28, 10, 0)
    # T24 及以前的旧格式（{"ts", "positions": [...]}）
    (live / f"expected-{MAGIC}.json").write_text(json.dumps({
        "ts": "2026-10-28T10:00:00+00:00",
        "positions": [{"symbol": SYMBOL, "magic": MAGIC, "side": "buy",
                       "volume": 0.16}]}), encoding="utf-8")
    # 旧版单文件 expected.json 同样兼容
    (live / "expected.json").write_text(json.dumps({
        "ts": "2026-10-28T10:00:00+00:00",
        "positions": [{"symbol": "EURUSD", "magic": MAGIC_B, "side": "sell",
                       "volume": 0.1}]}), encoding="utf-8")
    intents = load_intents(str(live))
    assert intents == [
        Intent(SYMBOL, MAGIC, "buy", 0.16, "confirmed", "legacy", ts),
        Intent("EURUSD", MAGIC_B, "sell", 0.1, "confirmed", "legacy", ts),
    ]

    # 写出的是新格式（单意图对象，带 status/cause）
    p = str(live / f"expected-{MAGIC}.json")
    write_intent(p, Intent(SYMBOL, MAGIC, "buy", 0.16, "pending",
                           "XAUUSD|M15|d1", ts))
    raw = read_json(p)
    assert set(raw) == {"symbol", "magic", "side", "volume", "status",
                        "cause", "ts"}
    assert raw["status"] == "pending" and raw["cause"] == "XAUUSD|M15|d1"
    # 新格式读回
    again = load_intents(str(live))
    assert Intent(SYMBOL, MAGIC, "buy", 0.16, "pending", "XAUUSD|M15|d1",
                  ts) in again


# ================================================================ T31 定手数
#（C2/P9 反转，决策 D22：实盘只在开仓时定手数——同向持仓保持净手数不调仓；
#  反手/止损后再入场按当时 ATR 与权益重新定手数；固定 volume_lots 行为不变）

def test_t31_no_resize_while_holding_atr_changes(tmp_path):
    """探针 P9 语义：6 根 bar ATR 2.0/2.4/1.8/2.6/2.0/1.7、信号恒 +1
    → 市价单恰 1 笔、平仓 0 笔、持仓 1 张（不再逐 bar 加减仓）。"""
    stack = LiveStack(tmp_path)
    decisions = []
    trs = (2.0, 2.4, 1.8, 2.6, 2.0, 1.7)
    for i, tr in enumerate(trs):
        d, rr = stack.round(T1 + timedelta(minutes=15 * i), "up", tr=tr)
        decisions.append(d)
        assert rr.error is None
    # ATR 确实在逐 bar 变化（否则测不出调仓缺陷）
    assert [round(d.atr, 1) for d in decisions] == list(trs)
    # 首轮开仓、其余 hold；全程 1 笔市价单、0 次平仓、1 张持仓
    assert stack.broker.market_orders and len(stack.broker.market_orders) == 1
    assert stack.broker.close_calls == []
    pos = stack.broker.positions(SYMBOL)
    assert len(pos) == 1 and pos[0].side == "buy"
    assert pos[0].volume == pytest.approx(expected_lots(decisions[0]))
    # 意图 = 入场手数且对账一致
    assert intent_of(stack.live_dir)["volume"] == pytest.approx(
        expected_lots(decisions[0]))
    rc, _ = stack.reconcile_once(now=T1 + timedelta(minutes=90))
    assert rc == 0


def test_t31_actions_hold_after_open(tmp_path):
    """P9 的动作序列：open 之后逐轮 hold（与上例分开，便于失败定位）。"""
    stack = LiveStack(tmp_path)
    actions = []
    for i, tr in enumerate((2.0, 2.4, 1.8, 2.6, 2.0, 1.7)):
        _, rr = stack.round(T1 + timedelta(minutes=15 * i), "up", tr=tr)
        actions.append(rr.action)
    assert actions == ["open"] + ["hold"] * 5


def test_t31_reverse_resizes_at_current_atr_and_equity(tmp_path):
    """反手：平仓后按当时 ATR 与权益重新定手数（不是沿用旧仓手数）。"""
    stack = LiveStack(tmp_path)
    d1, rr1 = stack.round(T1, "up", tr=2.0)
    assert rr1.action == "open"
    stack.broker.set_position_profit(
        stack.broker.positions(SYMBOL)[0].ticket, -100.0)   # 权益 → 9900
    d2, rr2 = stack.round(T2, "down", tr=3.0)               # 反手（ATR 3.0）
    assert rr2.action == "reverse"
    assert len(stack.broker.close_calls) == 1               # 先平旧多仓
    fresh = lots_for_risk(9_900.0, 0.005, d2.sl_atr * d2.atr,
                          CONTRACT, *VOL)
    assert fresh != pytest.approx(expected_lots(d1))        # 手数确实重定
    order = stack.broker.market_orders[-1]
    assert order["side"] == "sell"
    assert order["volume"] == pytest.approx(fresh)
    assert stack.broker.positions(SYMBOL)[0].volume == pytest.approx(fresh)


def test_t31_reentry_after_stop_resizes_fresh(tmp_path):
    """止损后再入场（immediate）：按当时 ATR 与权益重新定手数。"""
    stack = LiveStack(tmp_path)
    d1, rr1 = stack.round(T1, "up", tr=2.0)
    assert rr1.action == "open"
    ticket = stack.broker.positions(SYMBOL)[0].ticket
    stack.broker.close_external(ticket, profit=-300.0, comment="[sl]",
                                reason=DEAL_REASON_SL)      # 权益 → 9700
    d2, rr2 = stack.round(T2, "up", tr=3.0)                 # 重入（ATR 3.0）
    assert rr2.action == "open"
    fresh = lots_for_risk(9_700.0, 0.005, d2.sl_atr * d2.atr,
                          CONTRACT, *VOL)
    assert fresh != pytest.approx(expected_lots(d1))
    assert stack.broker.market_orders[-1]["volume"] == pytest.approx(fresh)
    assert stack.broker.positions(SYMBOL)[0].volume == pytest.approx(fresh)


def test_t31_fixed_volume_lots_behavior_unchanged(tmp_path):
    """固定 volume_lots 配置行为不变：开仓按配置手数、持仓期间不调、
    反手平旧开新（新腿=配置手数）、信号归零平仓。"""
    stack = LiveStack(tmp_path, volume_lots=0.10)
    _, rr1 = stack.round(T1, "up", tr=2.0)
    assert rr1.action == "open"
    assert stack.broker.market_orders[0]["volume"] == pytest.approx(0.10)

    _, rr2 = stack.round(T2, "up", tr=3.5)      # ATR 变化：持仓不动
    assert rr2.action == "hold"
    assert len(stack.broker.market_orders) == 1
    assert stack.broker.close_calls == []

    _, rr3 = stack.round(T3, "down", tr=2.0)    # 反手：先平后按固定手数开空
    assert rr3.action == "reverse"
    assert stack.broker.market_orders[-1]["side"] == "sell"
    assert stack.broker.market_orders[-1]["volume"] == pytest.approx(0.10)

    _, rr4 = stack.round(T4, "flat", tr=2.0)    # 信号归零：平仓
    assert rr4.action == "close"
    assert stack.broker.positions(SYMBOL) == []




# ================================================================ T32 全平编号协议
#（C3/P10 反转，决策 D21：指令带编号；ack 引用编号；归档核对编号并清理 ack）

def test_t32_second_flatten_command_executes_after_archive(tmp_path):
    """探针 P10 语义：第一次全平归档、解除停机后，第二次日亏的新指令
    不被旧 ack 在同一轮吞掉——执行器再次全平。"""
    stack = LiveStack(tmp_path)
    _, rr0 = stack.round(T1, "up")
    assert rr0.action == "open"
    cmds = Path(stack.live_dir) / "commands"
    flatten_path = cmds / "flatten.json"

    # 第一次日亏 → 指令 1 → 执行器全平 + ack(id1) → 风控归档并清理 ack
    stack.broker.seed_deal(profit=-600.0, magic=MAGIC, time=T2)
    stack.touch_heartbeat(T2)
    res1 = stack.risk_cycle(T2)
    assert res1.flatten_written and res1.halt_written
    id1 = read_json(flatten_path)["id"]
    _, rr1 = stack.round(T2, "up")
    assert rr1.action == "flatten"
    assert stack.broker.positions(SYMBOL) == []
    assert read_json(cmds / f"flatten.ack-{MAGIC}")["id"] == id1
    assert intent_of(stack.live_dir)["cause"] == f"flatten:{id1}"  # T30 衔接
    stack.touch_heartbeat(T2 + timedelta(seconds=30))
    res2 = stack.risk_cycle(T2 + timedelta(seconds=30))
    assert res2.flatten_archived
    assert not res2.flatten_written                  # 指令 1 未被重写
    assert not (cmds / f"flatten.ack-{MAGIC}").exists()   # ack 已清理（D21）
    # R10/T58 C-3：归档删除前指令 1 + ack 副本已保全（drill-archive/<id1>/）
    drill1 = Path(stack.live_dir) / "drill-archive" / id1
    assert read_json(drill1 / "flatten.json")["id"] == id1
    assert read_json(drill1 / f"flatten.ack-{MAGIC}")["id"] == id1

    # 运维解除停机 → 重新开仓 → 第二次日亏 → 新编号指令（P10 核心）
    os.remove(os.path.join(stack.live_dir, "halt.json"))
    day2 = T1 + timedelta(days=1)
    _, rr_d2 = stack.round(day2, "up")
    assert rr_d2.action == "open"
    stack.broker.seed_deal(profit=-900.0, magic=MAGIC,
                           time=day2 + timedelta(minutes=5))
    stack.touch_heartbeat(day2 + timedelta(minutes=5))
    res3 = stack.risk_cycle(day2 + timedelta(minutes=5))
    assert res3.flatten_written
    assert not res3.flatten_archived                 # 不再被同一轮归档（C3 反转）
    id2 = read_json(flatten_path)["id"]
    assert id2 != id1
    _, rr2 = stack.round(day2 + timedelta(minutes=15), "up")
    assert rr2.action == "flatten"                   # 执行器真的全平了
    assert stack.broker.positions(SYMBOL) == []
    assert read_json(cmds / f"flatten.ack-{MAGIC}")["id"] == id2

    # 指令归档前：本 magic 已 ack 当前编号 → 不再重复平仓（flatten_acked）
    n_closes = len(stack.broker.close_calls)
    _, rr_ack = stack.round(day2 + timedelta(minutes=30), "up")
    assert rr_ack.action == "flatten_acked"
    assert len(stack.broker.close_calls) == n_closes
    assert len(stack.broker.market_orders) == 2


def test_t32_gate_triggered_flatten_uses_numbered_protocol(tmp_path):
    """闸门（开仓检查）触发的全平走同一编号协议：带 id、执行器 ack 引用、
    风控按编号归档。"""
    stack = LiveStack(tmp_path)
    stack.broker.seed_deal(profit=-600.0, magic=MAGIC, time=T1)  # 当日已亏
    _, rr1 = stack.round(T1, "up")       # 开仓腿过闸门 → 日亏 → 全平+停机+拒开
    assert rr1.action == "risk_rejected"
    assert "daily_loss_cap" in rr1.error
    cmds = Path(stack.live_dir) / "commands"
    cmd = read_json(cmds / "flatten.json")
    assert cmd["command"] == "flatten_all" and cmd["id"]
    assert read_json(os.path.join(stack.live_dir, "halt.json"))["reason"] == \
        "daily_loss_cap"
    assert not (cmds / f"flatten.ack-{MAGIC}").exists()  # 闸门只写指令不代 ack

    # 执行器下一轮消费（同一协议）：全平 + ack 引用编号
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "flatten"
    assert read_json(cmds / f"flatten.ack-{MAGIC}")["id"] == cmd["id"]

    # 风控归档（同一协议）
    stack.touch_heartbeat(T2 + timedelta(seconds=30))
    res = stack.risk_cycle(T2 + timedelta(seconds=30))
    assert res.flatten_archived
    assert not (cmds / "flatten.json").exists()
    assert not (cmds / f"flatten.ack-{MAGIC}").exists()


def test_t32_unreadable_flatten_command_treated_as_present(tmp_path):
    """指令文件损坏：执行器按"有指令"处理（全平）并告警；风控不归档。"""
    stack = LiveStack(tmp_path)
    stack.round(T1, "up")
    flatten_path = os.path.join(stack.live_dir, "commands", "flatten.json")
    os.makedirs(os.path.dirname(flatten_path), exist_ok=True)
    with open(flatten_path, "w", encoding="utf-8") as f:
        f.write("{not json")
    _, rr = stack.round(T2, "up")
    assert rr.action == "flatten"
    assert stack.broker.positions(SYMBOL) == []
    assert "COMMAND_UNREADABLE" in alerts_text(stack.live_dir)
    ack = read_json(os.path.join(stack.live_dir, "commands",
                                 f"flatten.ack-{MAGIC}"))
    assert ack["magic"] == MAGIC and ack["closed"] == 1 and ack["id"] == ""
    assert intent_of(stack.live_dir)["cause"].startswith("flatten")
    # 风控侧：读不出 → 不归档（保守）
    stack.touch_heartbeat(T2 + timedelta(seconds=30))
    res = stack.risk_cycle(T2 + timedelta(seconds=30))
    assert not res.flatten_archived
    assert os.path.exists(flatten_path)


def test_live_retire_archives_registration_files(tmp_path, monkeypatch):
    """live_retire：确认无持仓后归档 heartbeat/expected/state——
    该 magic 不再算作登记策略（风控的 flatten 仲裁不再等它）。"""
    stack = LiveStack(tmp_path)
    stack.round(T1, "up")
    stack.round(T2, "flat")                      # 平仓 → 无持仓
    mod = load_script("live_retire")
    monkeypatch.setattr(mod, "_open_gateway",
                        lambda cfg: FakeGateway(stack.broker))
    rc = mod.main(["--data-dir", str(tmp_path), "--magic", str(MAGIC),
                   "--reason", "策略下线"])
    assert rc == 0
    live = Path(stack.live_dir)
    for name in (f"heartbeat-{MAGIC}.json", f"expected-{MAGIC}.json",
                 f"state-{MAGIC}.json"):
        assert not (live / name).exists(), name
        assert (live / "retired" / name).exists(), name
    from mt5live.riskproc import registered_magics
    assert MAGIC not in registered_magics(stack.live_dir)


def test_live_retire_refuses_with_open_positions(tmp_path, monkeypatch):
    """有持仓 → 拒绝退役（登记文件原样保留）。"""
    stack = LiveStack(tmp_path)
    stack.round(T1, "up")                        # 持仓未平
    mod = load_script("live_retire")
    monkeypatch.setattr(mod, "_open_gateway",
                        lambda cfg: FakeGateway(stack.broker))
    rc = mod.main(["--data-dir", str(tmp_path), "--magic", str(MAGIC),
                   "--reason", "策略下线"])
    assert rc == 2
    assert os.path.exists(os.path.join(stack.live_dir,
                                       f"heartbeat-{MAGIC}.json"))
    assert os.path.exists(os.path.join(stack.live_dir,
                                       f"expected-{MAGIC}.json"))


# ================================================================ M1 启动校验

def test_executor_rejects_time_stop_strategy(tmp_path):
    with pytest.raises(ValueError, match="time_stop"):
        LiveExecutor(
            ExecutionAdapter(FakeBroker(), FakeBroker(), FakeBroker,
                             symbol=SYMBOL, magic=MAGIC),
            symbol=SYMBOL, magic=MAGIC, live_dir=str(tmp_path),
            rules=ExitRules(stop_loss_atr=1.5, time_stop_bars=10))


def test_executor_rejects_week_close_strategy(tmp_path):
    with pytest.raises(ValueError, match="week_close"):
        LiveExecutor(
            ExecutionAdapter(FakeBroker(), FakeBroker(), FakeBroker,
                             symbol=SYMBOL, magic=MAGIC),
            symbol=SYMBOL, magic=MAGIC, live_dir=str(tmp_path),
            rules=ExitRules(week_close_exit=True))


def test_validate_live_strategy_accepts_supported_rules():
    rules = validate_live_strategy(ClosingSignal(ASC_RULES))
    assert rules.reentry == "after_signal_change"


# ================================================================ 组件行为

def test_heartbeat_path_is_per_magic(tmp_path):
    cfg = LiveConfig(symbol=SYMBOL, timeframe="M15",
                     strategy="mod:Cls", magic=MAGIC, lookback_bars=30,
                     risk_fraction=0.005, data_root=str(tmp_path))
    assert default_heartbeat_path(cfg).endswith(f"heartbeat-{MAGIC}.json")


def test_live_sizer_uses_equity_and_stop_distance(tmp_path):
    stack = LiveStack(tmp_path)
    d = mk_decision(volume_lots=None, atr=2.0, sl_atr=1.5)
    lots = stack.sizer.lots_for(d)
    assert lots == pytest.approx(
        lots_for_risk(10_000.0, 0.005, 3.0, CONTRACT, *VOL))
    assert lots == pytest.approx(0.16)


def test_gate_frequency_reads_local_order_log(tmp_path):
    stack = LiveStack(tmp_path)
    log = Path(stack.live_dir) / "orders.jsonl"
    lines = []
    for i in range(6):                                  # 上限 6 笔/分钟
        lines.append(json.dumps({
            "ts": (T1 - timedelta(seconds=10)).isoformat() + "+00:00",
            "magic": MAGIC, "symbol": SYMBOL, "kind": "open", "side": "buy",
            "lots": 0.01, "decision_id": f"d{i}"}))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    d = stack.gate.check_open(SYMBOL, "buy", 0.1, MAGIC, now=T1)
    assert not d.allowed
    assert "max_orders_per_minute" in d.reason


def test_gate_missing_hour_forbids_open(tmp_path):
    """点差模型缺该小时统计（missing_hour_policy=forbid）→ 禁入。"""
    stack = LiveStack(tmp_path, spread_model=_spread_model(6))
    d = stack.gate.check_open(SYMBOL, "buy", 0.1, MAGIC,
                              now=datetime(2026, 10, 28, 12, 0, 7))  # NY 08
    assert not d.allowed
    assert "spread_gate" in d.reason and ("缺" in d.reason or "missing" in d.reason)


def test_risk_process_checks_all_registered_heartbeats(tmp_path):
    live = tmp_path / "live"
    live.mkdir(parents=True)
    process = RiskProcess(load_risk_config(write_text(tmp_path / "r.toml",
                                                      RISK_TOML)),
                          live_dir=str(live), session_policy=load_session_policy(
                              write_text(tmp_path / "s.toml", SESSIONS_TOML)))
    now = datetime(2026, 10, 28, 10, 0, 0)
    # 两个策略心跳：A 新鲜、B 超时
    for magic, age in ((MAGIC, 10), (MAGIC_B, 999)):
        (live / f"heartbeat-{magic}.json").write_text(json.dumps({
            "runner_alive": True,
            "ts": (now - timedelta(seconds=age)).isoformat() + "+00:00"}),
            encoding="utf-8")
    res = process.run_cycle(None, now)
    assert not res.heartbeat_ok
    assert (live / "halt.json").exists()
    assert read_json(live / f"heartbeat-{MAGIC_B}.json")["runner_alive"] is False
    assert read_json(live / f"heartbeat-{MAGIC}.json")["runner_alive"] is True


def test_risk_process_archives_flatten_after_all_acks(tmp_path):
    """T32/D21 编号协议：所有登记 magic 的 ack.id == 当前指令 id → 归档并
    删除这些 ack；ack 缺失或编号不匹配 → 不归档。"""
    live = tmp_path / "live"
    cmds = live / "commands"
    cmds.mkdir(parents=True)
    process = RiskProcess(load_risk_config(write_text(tmp_path / "r.toml",
                                                      RISK_TOML)),
                          live_dir=str(live), session_policy=load_session_policy(
                              write_text(tmp_path / "s.toml", SESSIONS_TOML)))
    now = datetime(2026, 10, 28, 10, 0, 0)
    cmd = write_flatten(str(live), reason="daily_loss_cap", now=now)
    # 登记：心跳 A + expected B；只有 A 确认（编号匹配）→ 不归档
    (live / f"heartbeat-{MAGIC}.json").write_text(json.dumps({
        "runner_alive": True,
        "ts": now.isoformat() + "+00:00"}), encoding="utf-8")
    (live / f"expected-{MAGIC_B}.json").write_text(json.dumps(
        {"ts": now.isoformat() + "+00:00", "positions": []}), encoding="utf-8")
    write_ack(str(live), MAGIC, cmd.id, closed=0, now=now)
    res = process.run_cycle(None, now)
    assert not res.flatten_archived
    assert (cmds / "flatten.json").exists()
    # B 也确认（同编号）→ 归档并删除 ack
    write_ack(str(live), MAGIC_B, cmd.id, closed=0, now=now)
    res2 = process.run_cycle(None, now)
    assert res2.flatten_archived
    assert not (cmds / "flatten.json").exists()
    assert not (cmds / f"flatten.ack-{MAGIC}").exists()
    assert not (cmds / f"flatten.ack-{MAGIC_B}").exists()
    assert list((cmds / "archive").glob("flatten-*.json"))


def test_load_intents_merges_all_magics(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    ts = datetime(2026, 10, 28, 10, 0)
    write_intent(str(live / f"expected-{MAGIC}.json"),
                 Intent(SYMBOL, MAGIC, "buy", 0.16, "confirmed", "d1", ts))
    write_intent(str(live / f"expected-{MAGIC_B}.json"),
                 Intent("EURUSD", MAGIC_B, "sell", 0.1, "confirmed", "d2", ts))
    intents = load_intents(str(live))
    assert {i.magic for i in intents} == {MAGIC, MAGIC_B}
    assert all(i.status == "confirmed" for i in intents)
    with pytest.raises(ExpectedStateError):
        load_intents(str(tmp_path / "nowhere"))


# ================================================================ 日报脚本

def test_live_daily_report_script(tmp_path):
    stack = LiveStack(tmp_path)
    stack.round(T1, "up")
    stack.broker.seed_deal(profit=123.5, magic=MAGIC,
                           time=datetime(2026, 10, 28, 9, 0))
    mod = load_script("live_daily_report")
    rc = mod.main(["--data-dir", str(tmp_path), "--reports-dir",
                   str(tmp_path / "reports"), "--date", "2026-10-28"],
                  gw=FakeGateway(stack.broker))
    assert rc == 0
    out = tmp_path / "reports" / "live" / "daily-2026-10-28.md"
    text = out.read_text(encoding="utf-8")
    assert "2026-10-28" in text
    assert f"{MAGIC}" in text and "123.50" in text


# ================================================================ --dry-run --offline

def test_dry_run_offline_end_to_end(tmp_path):
    mod = load_script("live_runner")
    epoch = int(datetime.now(timezone.utc).timestamp())   # aware→真 UTC 纪元
    boundary = epoch - epoch % TF_S
    if boundary + 8 > epoch:                 # 距网格太近：等容差过期
        _time.sleep(boundary + 8 - epoch)
    last_open = boundary - TF_S
    df = grid_bars(pd.Timestamp(last_open, unit="s"), last="up")

    data_root = tmp_path / "data"
    save_bars(str(data_root), SYMBOL, "M15", df)
    cfg_path = write_text(tmp_path / "live.toml", f"""
symbol = "{SYMBOL}"
timeframe = "M15"
strategy = "mt5quant.strategies.library.buy_hold:BuyHold"
magic = {MAGIC}
lookback_bars = 30
risk_fraction = 0.005
data_root = "{data_root.as_posix()}"
trigger_delay_s = 5.0
""")
    rc = mod.main(["--config", cfg_path, "--once", "--dry-run", "--offline"])
    assert rc == 0
    hb = read_json(os.path.join(str(data_root), "live",
                                f"heartbeat-{MAGIC}.json"))
    assert hb["dry_run"] is True
    assert hb["last_decision"] is not None
    assert hb["last_decision"]["signal"] == 1
    assert hb["last_error"] is None


def test_live_runner_rejects_unsupported_exit_rules(tmp_path):
    """time_stop/week_close 未实现：启动校验拒绝（--dry-run 也一样）。"""
    mod = load_script("live_runner")
    cfg_path = write_text(tmp_path / "live.toml", f"""
symbol = "{SYMBOL}"
timeframe = "M15"
strategy = "tests.fakes_live:ClosingSignal"
magic = {MAGIC}
lookback_bars = 30
risk_fraction = 0.005
data_root = "{(tmp_path / 'data').as_posix()}"
""")
    # 给策略注入 time_stop：无法经 TOML，直接断言校验函数被 main 调用
    # —— 用 monkeypatch 换装载到的策略类
    orig_load = mod.load_strategy

    def patched(path):
        class _TS(ClosingSignal):
            exit_rules = ExitRules(stop_loss_atr=1.5, time_stop_bars=10)
        return _TS

    mod.load_strategy = patched
    try:
        with pytest.raises(ValueError, match="time_stop"):
            mod.main(["--config", cfg_path, "--once", "--dry-run", "--offline"])
    finally:
        mod.load_strategy = orig_load


# ================================================================ T33：跨进程文件契约

def _runner_cfg(tmp_path) -> str:
    return write_text(tmp_path / "live.toml", f"""
symbol = "{SYMBOL}"
timeframe = "M15"
strategy = "mt5quant.strategies.library.buy_hold:BuyHold"
magic = {MAGIC}
lookback_bars = 30
risk_fraction = 0.005
data_root = "{(tmp_path / 'data').as_posix()}"
trigger_delay_s = 5.0
""")


def test_live_runner_refuses_start_without_spread_model(tmp_path, monkeypatch,
                                                        capsys):
    """L3：实盘（非 dry-run）无点差模型且未给 --no-spread-gate → 启动失败
    （rc 2，连网关都不碰）——点差闸门不再静默关闭。"""
    mod = load_script("live_runner")

    def _never(p):
        raise AssertionError("拒绝启动时不得触碰网关")

    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file",
                        staticmethod(_never))
    rc = mod.main(["--config", _runner_cfg(tmp_path), "--once", "--spread-model",
                   str(tmp_path / "missing" / "spread_model.json")])
    assert rc == 2
    out = capsys.readouterr().out
    assert "点差模型" in out
    assert "--no-spread-gate" in out


def test_live_runner_no_spread_gate_flag_warns_red_and_proceeds(tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """--no-spread-gate：启动检查放行（走到网关装配）、启动日志标红警示。"""
    mod = load_script("live_runner")

    class _Sentinel(Exception):
        pass

    class _FakeGW:
        def connect(self):
            raise _Sentinel("已通过点差模型检查，到达网关连接")

    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file",
                        staticmethod(lambda p: _FakeGW()))
    gw_dummy = tmp_path / "gw.json"
    gw_dummy.write_text("{}", encoding="utf-8")
    with pytest.raises(_Sentinel):
        mod.main(["--config", _runner_cfg(tmp_path), "--once", "--no-spread-gate",
                  "--spread-model", str(tmp_path / "missing.json"),
                  "--gateway-config", str(gw_dummy)])
    out = capsys.readouterr().out
    assert "\033[31m" in out                    # 标红
    assert "点差闸门" in out


def test_dry_run_exempt_from_spread_model_refusal(tmp_path, capsys):
    """dry-run 不下单（闸门/执行器本就未接线）：缺模型不拒启动，行为不变。"""
    mod = load_script("live_runner")
    cfg_path = _runner_cfg(tmp_path)
    epoch = int(datetime.now(timezone.utc).timestamp())
    boundary = epoch - epoch % TF_S
    if boundary + 8 > epoch:
        _time.sleep(boundary + 8 - epoch)
    save_bars(str(tmp_path / "data"), SYMBOL, "M15",
              grid_bars(pd.Timestamp(boundary - TF_S, unit="s"), last="up"))
    rc = mod.main(["--config", cfg_path, "--once", "--dry-run", "--offline",
                   "--spread-model", str(tmp_path / "missing.json")])
    assert rc == 0


def test_runner_once_survives_persistent_heartbeat_write_failure(tmp_path,
                                                                 monkeypatch):
    """M1：写心跳持续 PermissionError——运行器不退出（--once 正常返回 0），
    失败记日志、下一轮重试；不再让权限错杀进程、随后心跳超时全局停机。"""
    mod = load_script("live_runner")

    def _boom(*a, **kw):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr("mt5live.runner.write_heartbeat", _boom)
    epoch = int(datetime.now(timezone.utc).timestamp())
    boundary = epoch - epoch % TF_S
    if boundary + 8 > epoch:
        _time.sleep(boundary + 8 - epoch)
    save_bars(str(tmp_path / "data"), SYMBOL, "M15",
              grid_bars(pd.Timestamp(boundary - TF_S, unit="s"), last="up"))
    rc = mod.main(["--config", _runner_cfg(tmp_path), "--once", "--dry-run",
                   "--offline"])
    assert rc == 0
    # 心跳写失败不等于假装写过：文件不存在（风控会按超时停机——那是另一层
    # fail-closed，运行器进程本身活着才是本条修的语义）
    assert not (tmp_path / "data" / "live" / f"heartbeat-{MAGIC}.json").exists()


# ================================================================ T34 影子盘
#（M3/决策 D26：真实行情 + 影子券商，全链路运行——闸门、定手数、意图、对账、
#  风控、全平协议照常，只有成交是模拟的。一条龙：决策 → 闸门 → paper 成交
#  → 意图 → 对账一致（--paper-state 视图）→ 全平指令 → paper 全平 + ack）

class PaperStack:
    """影子栈：FakeBroker 只当行情源（quote/set_quote），PaperBroker 当
    trading + account（同一对象双角色）；闸门/定手数/执行器/风控进程/对账
    与真实链路同一套装配（T34 的组件契约）。"""

    def __init__(self, tmp_path, *, magic: int = MAGIC,
                 exit_rules: ExitRules | None = None,
                 balance: float = 10_000.0):
        from mt5quant.sizing import RiskSizing as _RS

        self.magic = magic
        base = tmp_path / f"paper-{magic}"
        self.live_dir = str(base / "live")
        self.data_root = str(base / "data")
        os.makedirs(self.live_dir, exist_ok=True)
        os.makedirs(self.data_root, exist_ok=True)
        self.specs_path = write_text(
            base / "data" / "specs" / "latest.json", json.dumps({
                SYMBOL: {"contract_size": CONTRACT, "volume_min": VOL[0],
                         "volume_step": VOL[1], "volume_max": VOL[2]},
            }))
        self.policy = load_session_policy(
            write_text(base / "sessions.toml", SESSIONS_TOML))
        self.risk_cfg = load_risk_config(
            write_text(base / "risk.toml", RISK_TOML))
        self.process = RiskProcess(self.risk_cfg, live_dir=self.live_dir,
                                   session_policy=self.policy)
        self.market = FakeBroker(SYMBOL, balance=balance,
                                 contract_size=CONTRACT)
        # R6/C3：影子状态按策略落 paper/state-<magic>.json（多运行器各写
        # 各的）；聚合视图（风控/对账）传 paper 目录
        self.paper_state = paper_state_path(self.live_dir, magic)
        self.paper_dir = os.path.join(self.live_dir, "paper")
        self.paper = PaperBroker(
            self.market, state_path=self.paper_state,
            contract_sizes={SYMBOL: CONTRACT}, balance=balance,
            clock=lambda: T1)
        self.gate = RiskGate(
            self.process, account=self.paper, market=self.market,
            specs_path=self.specs_path, live_dir=self.live_dir,
            session_policy=self.policy)
        self.cfg = LiveConfig(
            symbol=SYMBOL, timeframe="M15",
            strategy="tests.fakes_live:ClosingSignal", params={}, magic=magic,
            lookback_bars=30, risk_fraction=0.005,
            data_root=self.data_root, trigger_delay_s=5.0)
        self.strategy = ClosingSignal(
            exit_rules if exit_rules is not None else DEFAULT_RULES)
        self._bars_df = None
        self.runner = SignalRunner(
            self.cfg, self.strategy,
            bars_loader=lambda root, s, tf: self._bars_df,
            quote_fn=lambda s: (self.market.bid, self.market.ask))
        self.adapter = ExecutionAdapter(
            self.paper, self.paper, self.market, symbol=SYMBOL, magic=magic)
        self.sizer = LiveSizer(_RS(0.005, basis="equity"),
                               account=self.paper, specs_path=self.specs_path)
        self.executor = LiveExecutor(
            self.adapter, symbol=SYMBOL, magic=magic, live_dir=self.live_dir,
            rules=self.strategy.exit_rules, gate=self.gate, sizer=self.sizer)
        self.hb_path = os.path.join(self.live_dir, f"heartbeat-{magic}.json")

    def round(self, now: datetime, last: str = "up"):
        """一轮 = paper 保护性出场轮询 + 运行器决策 + 执行器轮 + 心跳
        （live_runner --paper 的主循环顺序）。"""
        self.market.quote_time = now           # 行情新鲜（fake 的 quote 时刻）
        protective = self.paper.poll_protective(now)
        self._bars_df = grid_bars(boundary_open(now), last=last)
        decision = self.runner.decide(now)
        rr = self.executor.poll(decision, now)
        write_heartbeat(self.hb_path, config=self.cfg, decision=decision,
                        error=rr.error, dry_run=False, now=now)
        return decision, rr, protective

    def risk_cycle_from_paper(self, now: datetime):
        """风控主循环一轮：账户快照来自 --paper-state 只读视图（不碰真实账户；
        R6/C3 起传 paper 目录——聚合全部 state-<magic>.json）。"""
        mod = load_script("risk_process")
        snap = mod.build_snapshot(PaperStateGateway(self.paper_dir), now)
        return self.process.run_cycle(snap, now)

    def reconcile_from_paper(self, now: datetime):
        mod = load_script("live_reconcile")
        return mod.run_once(self.live_dir, PaperStateGateway(self.paper_dir),
                            volume_tolerance=1e-6, now=now)


def test_t34_shadow_full_chain_decision_to_flatten(tmp_path):
    """影子一条龙：决策 → 闸门 → paper 成交 → 意图 → 对账一致 → 全平指令
    → paper 全平 + ack → 风控归档。全程无真实下单（成交只在 paper 账本）。"""
    stack = PaperStack(tmp_path)

    # 1) 决策 → 闸门（RiskGate 全套：日亏/停机/白名单/手数/持仓/暴露/频率/
    #    禁入窗）→ PaperBroker 模拟成交（买按 Ask 100.2）
    d1, rr1, _ = stack.round(T1, "up")
    assert rr1.action == "open"
    lots = expected_lots(d1)
    pos = stack.paper.positions(SYMBOL)
    assert len(pos) == 1 and pos[0].side == "buy"
    assert pos[0].volume == pytest.approx(lots)
    assert pos[0].magic == MAGIC and pos[0].price_open == pytest.approx(100.2)
    assert stack.market.market_orders == []            # 行情源绝未被下单

    # 2) 状态文件落盘（fileio 原子写）；意图 confirmed = 成交手数
    with open(stack.paper_state, encoding="utf-8") as f:
        book = json.load(f)
    assert len(book["positions"]) == 1 and book["balance"] == \
        pytest.approx(10_000.0)
    it = intent_of(stack.live_dir)
    assert it["status"] == "confirmed" and it["cause"] == d1.decision_id
    assert it["volume"] == pytest.approx(lots)

    # 3) 对账（--paper-state 只读视图替代真实账户）：一致、不暂停
    rc, res = stack.reconcile_from_paper(T1 + timedelta(seconds=30))
    assert rc == 0 and res.ok
    assert pause_state(stack.live_dir) in (None, False)

    # 4) 全平指令（风控编号协议）→ 执行器消费 → paper 全平 + ack 引用编号
    cmd = write_flatten(stack.live_dir, reason="daily_loss_cap", now=T2)
    d2, rr2, _ = stack.round(T2, "up")
    assert rr2.action == "flatten"
    assert stack.paper.positions() == []
    ack = read_json(os.path.join(stack.live_dir, "commands",
                                 f"flatten.ack-{MAGIC}"))
    assert ack["id"] == cmd.id and ack["closed"] == 1
    it2 = intent_of(stack.live_dir)
    assert it2["volume"] == 0.0 and it2["cause"] == f"flatten:{cmd.id}"

    # 5) 风控（--paper-state 快照）：全员 ack → 归档指令并清理 ack
    write_heartbeat(stack.hb_path, config=stack.cfg, decision=d2, error=None,
                    dry_run=False, now=T2 + timedelta(seconds=30))
    res2 = stack.risk_cycle_from_paper(T2 + timedelta(seconds=30))
    assert res2.flatten_archived
    assert not os.path.exists(os.path.join(stack.live_dir, "commands",
                                           "flatten.json"))

    # 6) 全平后对账仍一致（意图 0 = paper 空仓）
    rc2, _ = stack.reconcile_from_paper(T2 + timedelta(minutes=1))
    assert rc2 == 0


def test_t34_shadow_protective_exit_explained_in_chain(tmp_path):
    """一条龙中途的 paper SL 出场：触发口径与引擎一致（多头按 Bid），
    执行器同轮识别为 external_exit → 意图=实际 → 对账一致不暂停。"""
    stack = PaperStack(tmp_path)
    _, rr1, _ = stack.round(T1, "up")
    assert rr1.action == "open"
    sl = stack.paper.positions(SYMBOL)[0].sl          # 成交价 100.2 − 1.5×ATR
    assert sl == pytest.approx(100.2 - 1.5 * 2.0)

    # Bid 跌破 SL（Ask 仍在上方——错用 Ask 的实现不会触发）
    stack.market.set_quote(sl - 0.3, sl + 0.5)
    _, rr2, protective = stack.round(T2, "up")
    assert len(protective) == 1
    assert protective[0].reason == DEAL_REASON_SL
    assert protective[0].price == pytest.approx(sl - 0.3)   # 多头按 Bid 成交
    # 亏损入账（影子账本）：(96.9 − 100.2)×0.16×100 = −3.3×16 = −52.8
    assert stack.paper.state().balance == pytest.approx(10_000.0 - 52.8)
    # immediate 重入开新仓；上一笔已按 external_exit 解释（对账一致不暂停）
    assert rr2.action == "open"
    rc, res = stack.reconcile_from_paper(T2 + timedelta(seconds=30))
    assert rc == 0 and res.ok
    assert pause_state(stack.live_dir) in (None, False)


def _paper_cli_env(tmp_path, *,
                   strategy: str = "mt5quant.strategies.library.buy_hold:"
                                   "BuyHold") -> dict:
    """--paper CLI 测试的环境（specs/点差模型/会话/风控/live.toml）。"""
    write_text(tmp_path / "data" / "specs" / "latest.json", json.dumps({
        SYMBOL: {"contract_size": CONTRACT, "volume_min": VOL[0],
                 "volume_step": VOL[1], "volume_max": VOL[2]}}))
    from mt5quant.costs.spread import SpreadStat
    SpreadModel(
        {SYMBOL: {h: SpreadStat(median=0.02, p90=0.5, p99=0.8, n=100,
                                n_days=100)
                  for h in range(24)}},
        {SYMBOL: 0.02}, mode="absolute").save(str(tmp_path / "spread.json"))
    env = {
        "specs": str(tmp_path / "data" / "specs" / "latest.json"),
        "spread": str(tmp_path / "spread.json"),
        "sessions": write_text(tmp_path / "sessions.toml", """
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
"""),
        "risk": write_text(tmp_path / "risk.toml", RISK_TOML),
    }
    env["config"] = write_text(tmp_path / "live.toml", f"""
symbol = "{SYMBOL}"
timeframe = "M15"
strategy = "{strategy}"
magic = {MAGIC}
lookback_bars = 30
risk_fraction = 0.005
data_root = "{(tmp_path / 'data').as_posix()}"
trigger_delay_s = 5.0
""")
    return env


def test_t34_paper_dry_run_offline_end_to_end(tmp_path, capsys):
    """--paper --dry-run --offline 合成数据端到端：不连 MT5、不下真实单，
    全链路（决策 → 闸门 → paper 成交 → 意图/心跳）跑通；随后
    live_reconcile / risk_process 用 --paper-state 复核同一状态文件。"""
    mod = load_script("live_runner")
    epoch = int(datetime.now(timezone.utc).timestamp())   # aware→真 UTC 纪元
    boundary = epoch - epoch % TF_S
    if boundary + 8 > epoch:                 # 距网格太近：等容差过期
        _time.sleep(boundary + 8 - epoch)
    last_open = boundary - TF_S
    save_bars(str(tmp_path / "data"), SYMBOL, "M15",
              grid_bars(pd.Timestamp(last_open, unit="s"), last="up"))
    env = _paper_cli_env(tmp_path)

    rc = mod.main(["--config", env["config"], "--once", "--dry-run", "--offline",
                   "--paper", "--risk-config", env["risk"],
                   "--sessions-config", env["sessions"],
                   "--spread-model", env["spread"]])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[paper offline]" in out and "影子账户" in out

    # 心跳：dry_run=True（显式给了 --dry-run），决策已出、无错误
    hb = read_json(os.path.join(str(tmp_path / "data"), "live",
                                f"heartbeat-{MAGIC}.json"))
    assert hb["dry_run"] is True and hb["last_decision"] is not None
    assert hb["last_error"] is None
    # 影子账本（R6/C3：按策略落 paper/state-<magic>.json）：BuyHold 开多仓
    # （成交=模拟），意图 confirmed。BuyHold 无 SL/TP 规则 → LiveSizer 用
    # fallback_stop_atr=1.0：距离 1.0×ATR(≈2.0) → 10000×0.5%/2.0/100 = 0.25 手
    paper_dir = os.path.join(str(tmp_path / "data"), "live", "paper")
    paper_state = os.path.join(paper_dir, f"state-{MAGIC}.json")
    with open(paper_state, encoding="utf-8") as f:
        book = json.load(f)
    assert len(book["positions"]) == 1
    assert book["positions"][0]["side"] == "buy"
    assert book["positions"][0]["volume"] == pytest.approx(0.25)
    it = read_json(os.path.join(str(tmp_path / "data"), "live",
                                f"expected-{MAGIC}.json"))
    assert it["status"] == "confirmed" and it["volume"] == pytest.approx(0.25)

    # 对账 / 风控复核同一状态目录（--paper-state，不连网关；聚合全部
    # state-<magic>.json）
    rec = load_script("live_reconcile")
    assert rec.main(["--data-dir", str(tmp_path / "data"), "--once",
                     "--paper-state", paper_dir]) == 0
    risk_mod = load_script("risk_process")
    assert risk_mod.main(["--config", env["risk"],
                          "--data-dir", str(tmp_path / "data"), "--once",
                          "--paper-state", paper_dir]) == 0


def test_t34_live_runner_paper_mode_wires_full_stack(tmp_path, monkeypatch,
                                                     capsys):
    """--paper（真终端行情路径）：PaperBroker 替换真实 trading/account——
    网关替身（FakeBroker 只当行情源，绝不连真实 MT5）的 trading 从未被
    调用；闸门/定手数/执行器照常装配，成交进影子账本。"""
    mod = load_script("live_runner")
    broker = FakeBroker(SYMBOL, contract_size=CONTRACT)
    now = datetime(2026, 10, 28, 10, 0, 7)   # 决策墙钟由注入的终端替身给出
    broker.set_bars(SYMBOL, "M15",
                    bars_from_df(grid_bars(boundary_open(now), last="up")))
    broker.quote_time = now
    call_count = {"connect": 0}

    class _FakeGW:
        def __init__(self):
            self.market = broker
            self.account = broker
            self.trading = broker

        def connect(self):
            call_count["connect"] += 1

        def shutdown(self):
            pass

    gw_dummy = tmp_path / "gw.json"
    gw_dummy.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file",
                        staticmethod(lambda p: _FakeGW()))
    monkeypatch.setattr(mod, "now_utc", lambda: now)
    env = _paper_cli_env(tmp_path)

    rc = mod.main(["--config", env["config"], "--once", "--paper",
                   "--risk-config", env["risk"],
                   "--sessions-config", env["sessions"],
                   "--spread-model", env["spread"],
                  "--gateway-config", str(gw_dummy)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[paper]" in out and "影子账户" in out
    assert call_count["connect"] == 1            # 行情来自"网关"（真路径装配）
    assert broker.market_orders == []            # 真实 trading 通道绝未被下单
    assert broker.positions() == []              # 替身账户无持仓
    paper_state = os.path.join(str(tmp_path / "data"), "live", "paper",
                               f"state-{MAGIC}.json")   # R6/C3：按策略落盘
    with open(paper_state, encoding="utf-8") as f:
        book = json.load(f)
    assert len(book["positions"]) == 1           # 成交在影子账本
    it = read_json(os.path.join(str(tmp_path / "data"), "live",
                                f"expected-{MAGIC}.json"))
    assert it["status"] == "confirmed" and it["volume"] == \
        pytest.approx(book["positions"][0]["volume"])


# ================================================================ R6（第六轮审核 C2）
# 外部出场状态机：未决持久化 + 执行器开仓第二道防线（复现 A/B 反转）

def test_r6_manual_close_executor_before_reconcile_no_reopen(tmp_path,
                                                              monkeypatch):
    """C2 复现 B 反转：人工平仓后**执行器先于对账进程运行**——immediate
    也不重开（原缺陷：直接重开并把新仓确认为合法，绕过人工确认）；
    live_ack_external 确认后恢复。"""
    stack = LiveStack(tmp_path)
    _, rr1 = stack.round(T1, "up")
    assert rr1.action == "open"
    ticket = stack.broker.positions(SYMBOL)[0].ticket
    stack.broker.close_external(ticket, profit=5.0, comment="manual",
                                reason=DEAL_REASON_CLIENT)

    # 执行器先跑（对账还没写 pause）：未解释出场 → 不重开
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "blocked_external_exit"
    assert len(stack.broker.market_orders) == 1          # 没有重开
    assert intent_of(stack.live_dir)["volume"] == pytest.approx(0.16)
    state = read_json(os.path.join(stack.live_dir, f"state-{MAGIC}.json"))
    assert str(ticket) in state["pending_external_exits"]
    assert "EXTERNAL_EXIT" in alerts_text(stack.live_dir)

    # 对账此刻才跑：意图 0.16 vs 实际 0 → 不一致 + 暂停（叠加，非唯一防线）
    rc, _ = stack.reconcile_once(now=T2 + timedelta(seconds=30))
    assert rc == 1
    assert read_json(pause_file(stack.live_dir))["paused"] is True

    # 人工确认（绑定该未决事件）→ 未决清空 → 暂停解除 → 恢复开仓
    ack = load_script("live_ack_external")
    rc_ack = _ack_via_script(ack, monkeypatch, stack,
                             now=T2 + timedelta(seconds=45),
                             reason="人工平仓已核对")
    assert rc_ack == 0
    rc2, _ = stack.reconcile_once(now=T2 + timedelta(minutes=1))
    assert rc2 == 0
    assert not read_json(pause_file(stack.live_dir))["paused"]
    _, rr3 = stack.round(T3, "up")
    assert rr3.action == "open"
    assert len(stack.broker.market_orders) == 2


def test_r6_deal_history_delayed_two_rounds_then_explained_recovers(tmp_path):
    """C2 复现 A 反转：成交历史延迟两轮才可见——未决出场持久化、持续重查、
    期间不开新仓（不丢事件、不重开）；第三轮解释为 SL → 意图=实际、
    暂停解除、恢复开仓。"""
    stack = LiveStack(tmp_path)
    stack.broker.deals_delay_polls = 2        # 平仓成交要经 2 次查询才可见
    _, rr1 = stack.round(T1, "up")
    assert rr1.action == "open"
    ticket = stack.broker.positions(SYMBOL)[0].ticket
    stack.broker.close_external(ticket, profit=-30.0, comment="[sl]",
                                reason=DEAL_REASON_SL)

    # 第 1 轮：查无成交 → 未决持久化 + 告警；immediate 也不重开
    _, rr2 = stack.round(T2, "up")
    assert rr2.action == "blocked_external_exit"
    state = read_json(os.path.join(stack.live_dir, f"state-{MAGIC}.json"))
    assert str(ticket) in state["pending_external_exits"]
    assert intent_of(stack.live_dir)["volume"] == pytest.approx(0.16)  # 不洗白
    assert "EXTERNAL_EXIT" in alerts_text(stack.live_dir)

    # 对账：意图 0.16 vs 实际 0 → 失败 + 暂停（复现 A 的症状如实呈现）
    rc, _ = stack.reconcile_once(now=T2 + timedelta(seconds=30))
    assert rc == 1
    assert read_json(pause_file(stack.live_dir))["paused"] is True

    # 第 2 轮：历史仍不可见 → 仍未决、继续挡（事件没有丢失）
    _, rr3 = stack.round(T3, "up")
    assert rr3.action == "blocked_external_exit"
    assert len(stack.broker.market_orders) == 1
    state3 = read_json(os.path.join(stack.live_dir, f"state-{MAGIC}.json"))
    assert str(ticket) in state3["pending_external_exits"]

    # 第 3 轮：历史可见 → SL 解释成功 → 未决清空、意图=实际（空仓）；
    # 开仓腿此刻只被残留的对账暂停挡（叠加防线），不再是外部出场防线
    _, rr4 = stack.round(T4, "up")
    assert rr4.action == "risk_rejected"
    assert "reconcile_paused" in rr4.error
    it = intent_of(stack.live_dir)
    assert it["cause"].startswith("external_exit:")
    assert it["volume"] == 0.0
    state4 = read_json(os.path.join(stack.live_dir, f"state-{MAGIC}.json"))
    assert state4["pending_external_exits"] == {}

    # 对账一致 → 暂停解除 → 恢复开仓
    rc2, _ = stack.reconcile_once(now=T4 + timedelta(seconds=30))
    assert rc2 == 0
    assert not read_json(pause_file(stack.live_dir))["paused"]
    _, rr5 = stack.round(T5, "up")
    assert rr5.action == "open"
    assert len(stack.broker.market_orders) == 2



# ================================================================ R7（第七轮审核）
# N-C1：人工确认必须绑定事件——旧确认不得放行确认之后的新出场（全链路，
# 真脚本 scripts/live_ack_external.py，含确认绑定协议的落盘形状）

def _ack_via_script(mod, monkeypatch, stack, *, now: datetime,
                    magic: int = MAGIC, reason: str = "人工确认外部出场") -> int:
    """真脚本 main() 入口跑一次人工确认，冻结脚本时钟到 now（测试钟面），
    网关替身读 FakeBroker 持仓。"""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.replace(tzinfo=tz) if tz is not None else now

    monkeypatch.setattr(mod, "datetime", _Frozen)
    monkeypatch.setattr(mod, "_open_gateway",
                        lambda cfg: FakeGateway(stack.broker))
    return mod.main(["--data-dir", str(Path(stack.live_dir).parent),
                     "--magic", str(magic), "--reason", reason])


def test_r7_nc1_stale_ack_cannot_release_new_exit_full_chain(tmp_path,
                                                             monkeypatch):
    """N-C1 全链路复现（报告序列）：开 0.16 → 人工减至 0.08 并确认（真脚本
    落盘绑定：空事件集+净仓快照 0.08）→ 无订单轮保留 cause=ack_external →
    再次人工平掉余仓 → 执行器先于对账运行——不得重开（旧实现 action=open
    重开 0.16）；再次人工确认（这次绑定新事件）后经对账恢复开仓。"""
    stack = LiveStack(tmp_path)
    _, rr1 = stack.round(T1, "up")
    assert rr1.action == "open"
    ticket = stack.broker.positions(SYMBOL)[0].ticket

    # 人工减仓至 0.08（ticket 仍在 → 不是出场事件）→ 无订单轮（hold）
    stack.broker.reduce_external(ticket, 0.08, profit=2.0, comment="manual",
                                 reason=DEAL_REASON_CLIENT)
    _, rr_hold = stack.round(T2, "up")
    assert rr_hold.action == "hold"
    assert len(stack.broker.market_orders) == 1

    # 人工确认（真脚本）：绑定此刻的未决出场（空集）+ 净仓快照 0.08
    ack = load_script("live_ack_external")
    rc = _ack_via_script(ack, monkeypatch, stack, now=T2 + timedelta(minutes=2),
                         reason="人工减仓已核对")
    assert rc == 0
    ev = intent_of(stack.live_dir)["confirmed_events"]
    assert ev["tickets"] == []                          # 无未决事件可绑定
    assert ev["net_volume"] == pytest.approx(0.08)      # 确认时净仓快照
    assert ev["positions"][0]["ticket"] == ticket

    # 无订单轮：意图保留 cause=ack_external（hold 不改写意图）
    _, rr_hold2 = stack.round(T3, "up")
    assert rr_hold2.action == "hold"
    assert intent_of(stack.live_dir)["cause"] == "ack_external"

    # 再次人工平掉余仓 → 执行器先于对账运行：新出场事件不被旧确认放行
    stack.broker.close_external(ticket, profit=1.0, comment="manual",
                                reason=DEAL_REASON_CLIENT)
    _, rr = stack.round(T4, "up")
    assert rr.action == "blocked_external_exit"         # 旧实现：open（基线）
    assert len(stack.broker.market_orders) == 1         # 没有重开 0.16
    state = read_json(os.path.join(stack.live_dir, f"state-{MAGIC}.json"))
    assert str(ticket) in state["pending_external_exits"]

    # 对账此刻运行：意图 0.08 vs 实际 0 → 不一致暂停（叠加防线如实呈现）
    rc_rec, _ = stack.reconcile_once(now=T4 + timedelta(seconds=30))
    assert rc_rec == 1
    assert read_json(pause_file(stack.live_dir))["paused"] is True

    # 再次人工确认（这次绑定新事件 ticket、快照=空仓）→ 对账一致 → 恢复
    rc2 = _ack_via_script(ack, monkeypatch, stack, now=T4 + timedelta(minutes=1),
                          reason="余仓人工平仓已核对")
    assert rc2 == 0
    ev2 = intent_of(stack.live_dir)["confirmed_events"]
    assert ev2["tickets"] == [ticket]
    assert ev2["net_volume"] == pytest.approx(0.0)
    rc3, _ = stack.reconcile_once(now=T4 + timedelta(minutes=2))
    assert rc3 == 0
    _, rr3 = stack.round(T5, "up")
    assert rr3.action == "open"
    assert len(stack.broker.market_orders) == 2
