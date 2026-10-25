"""T34 影子券商（PaperBroker）单元测试——失败测试先行。

覆盖计划 T34 的验收点：
- SL/TP 触发口径与引擎一致（engine_a._protective_exit：多头按 Bid、空头按
  Ask 触发；本层成交价 = 触发一侧现价，即真实券商/引擎 B 的报价流口径）——
  造报价序列断言触发时刻与成交价、reason（DEAL_REASON_SL/TP）；
- 账户状态经 fileio 原子落 data/live/paper/state.json，重启可恢复（持仓/
  权益/已实现流水/票据序号不碰撞）；
- PaperBroker 实现执行器（ExecutionAdapter）与闸门（RiskGate）/定手数
  （LiveSizer）用到的 TradingService / AccountService 子集——全部经公开
  接口交互，绝不连真实 MT5、绝不下真实单（成交全模拟）；
- 外部出场 reason 识别闭环：paper 的 SL 平仓被 LiveExecutor 识别为
  external_exit（已解释、不暂停对账）；
- --paper-state：PaperAccountView / PaperStateGateway 从状态文件构造只读
  账户视图（risk_process / live_reconcile 用，不碰真实账户）。

影子模式全链路一条龙（决策→闸门→paper 成交→意图→对账→全平）在
tests/test_mt5live_integration.py（T34 场景）。
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mt5gw.models import DEAL_REASON_EXPERT, DEAL_REASON_SL, DEAL_REASON_TP
from mt5live.executor import ExecutionAdapter, LiveExecutor
from mt5live.paper import (
    BarQuoteMarket,
    PaperAccountView,
    PaperBroker,
    PaperStateGateway,
)
from mt5live.riskproc import RiskProcess, load_risk_config
from mt5quant.strategies.base import ExitRules
from tests.fakes_live import QuoteMarket, mk_decision

REPO_ROOT = Path(__file__).resolve().parents[1]

SYMBOL = "XAUUSD"
CONTRACT = 100.0
MAGIC = 20261101
T0 = datetime(2026, 10, 28, 10, 0, 7)      # 与 integration 测试同一时间锚

RISK_TOML = """
version = 1
symbols = ["XAUUSD", "BTCUSD", "EURUSD"]
max_lots_per_order = 0.50
max_positions_per_symbol = 3
max_usd_exposure = 100000.0
max_orders_per_minute = 6
daily_loss_cap_usd = 50.0
"""


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_script", REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_path(tmp_path) -> str:
    return str(tmp_path / "live" / "paper" / "state.json")


def make_paper(tmp_path, market: QuoteMarket | None = None, *,
               balance: float = 10_000.0, clock=None) -> PaperBroker:
    return PaperBroker(
        market if market is not None else QuoteMarket(),
        state_path=state_path(tmp_path),
        contract_sizes={SYMBOL: CONTRACT},
        balance=balance,
        clock=clock if clock is not None else (lambda: T0))


def read_state(tmp_path) -> dict:
    with open(state_path(tmp_path), encoding="utf-8") as f:
        return json.load(f)


# ================================================================ 服务面
# PaperBroker 实现 ExecutionAdapter / RiskGate / LiveSizer 用到的
# TradingService / AccountService 子集（market 角色由注入的行情源承担）。

def test_market_order_fills_at_quote_and_records_in_deal(tmp_path):
    """市价开仓：买按 Ask、卖按 Bid 成交；in 成交带 reason=EXPERT 与 magic；
    SL/TP 随单登记在持仓上（0 = 未设，MT5 口径）。"""
    paper = make_paper(tmp_path)
    res = paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, tp=102.0,
                             magic=MAGIC)
    assert res.ok and res.filled and not res.partial
    assert res.deal_price == pytest.approx(100.2)          # 买按 Ask
    assert res.volume == pytest.approx(0.1)
    assert res.order_ticket is not None and res.deal_ticket is not None
    pos = paper.positions(SYMBOL)
    assert len(pos) == 1
    p = pos[0]
    assert p.side == "buy" and p.magic == MAGIC
    assert p.volume == pytest.approx(0.1)
    assert p.price_open == pytest.approx(100.2)
    assert p.sl == pytest.approx(99.0) and p.tp == pytest.approx(102.0)
    deals = paper.deals_for_position(p.ticket)
    assert len(deals) == 1
    assert deals[0].entry == "in" and deals[0].reason == DEAL_REASON_EXPERT
    assert deals[0].magic == MAGIC

    res_sell = paper.market_order(SYMBOL, "sell", 0.1, magic=MAGIC + 1)
    assert res_sell.deal_price == pytest.approx(100.0)     # 卖按 Bid
    assert len(paper.positions(SYMBOL)) == 2

    # 无 SL/TP 随单 → 持仓字段为 0（未设）
    res_plain = paper.market_order(SYMBOL, "buy", 0.05, magic=MAGIC)
    plain = [p for p in paper.positions(SYMBOL)
             if p.ticket == res_plain.order_ticket][0]
    assert plain.sl == 0.0 and plain.tp == 0.0


def test_market_order_missing_contract_size_fails_loud(tmp_path):
    """合约大小缺品种 → 如实抛错（盈亏换算无依据，绝不静默按 1 成交）。"""
    from mt5gw.errors import DataUnavailableError
    paper = PaperBroker(QuoteMarket(), state_path=state_path(tmp_path),
                        contract_sizes={"EURUSD": 1.0})
    with pytest.raises(DataUnavailableError, match="contract_size"):
        paper.market_order(SYMBOL, "buy", 0.1, magic=MAGIC)


def test_close_position_fills_at_exit_side_and_books_profit(tmp_path):
    """平仓：多头按 Bid、空头按 Ask 成交；盈亏入余额、out 成交记账、
    deals_for_position 含开平两腿。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    opened = paper.market_order(SYMBOL, "buy", 0.1, magic=MAGIC)
    market.set_quote(101.0, 101.2)                        # 多头平仓按 Bid
    closed = paper.close_position(opened.order_ticket)
    assert closed.deal_price == pytest.approx(101.0)
    # profit = (101.0 - 100.2) × 0.1 × 100 = 8.0
    assert paper.state().balance == pytest.approx(10_000.0 + 8.0)
    assert paper.positions(SYMBOL) == []
    deals = paper.deals_for_position(opened.order_ticket)
    assert [d.entry for d in deals] == ["in", "out"]
    assert deals[1].profit == pytest.approx(8.0)
    assert deals[1].reason == DEAL_REASON_EXPERT
    assert paper.state().equity == pytest.approx(10_008.0)  # 无持仓，浮动 0


def test_modify_position_updates_one_side_only(tmp_path):
    """改持仓 SL/TP：只改一侧、另一侧保持现值；两侧都不给 → 拒绝。"""
    paper = make_paper(tmp_path)
    res = paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, tp=102.0,
                             magic=MAGIC)
    ticket = res.order_ticket
    paper.modify_position(ticket, sl=98.5)
    p = paper.positions(SYMBOL)[0]
    assert p.sl == pytest.approx(98.5) and p.tp == pytest.approx(102.0)
    paper.modify_position(ticket, tp=103.0)
    p = paper.positions(SYMBOL)[0]
    assert p.sl == pytest.approx(98.5) and p.tp == pytest.approx(103.0)
    with pytest.raises(ValueError):
        paper.modify_position(ticket)


def test_pending_orders_empty_and_cancel_raises(tmp_path):
    """影子券商只走市价单：无挂单；cancel 找不到挂单如实抛错。"""
    from mt5gw.errors import DataUnavailableError
    paper = make_paper(tmp_path)
    assert paper.pending_orders(SYMBOL) == []
    with pytest.raises(DataUnavailableError):
        paper.cancel_order(999999)


def test_state_equity_includes_floating(tmp_path):
    """权益 = 余额 + 浮动（按注入行情源的现价算，合约大小换算）。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, magic=MAGIC)   # 开仓 @Ask 100.2
    market.set_quote(102.0, 102.2)
    st = paper.state()
    assert st.currency == "USD"
    # floating = (bid 102.0 − 100.2) × 0.1 × 100 = 18.0
    assert st.equity == pytest.approx(10_000.0 + 18.0)
    assert st.balance == pytest.approx(10_000.0)
    assert paper.positions(SYMBOL)[0].profit == pytest.approx(18.0)


def test_deals_window_filters_by_time(tmp_path):
    """deals(since, to) 按时间窗过滤（risk_process 日亏口径的数据源）。"""
    times = iter([T0, T0 + timedelta(minutes=5), T0 + timedelta(minutes=50)])
    paper = make_paper(tmp_path, clock=lambda: next(times))
    paper.market_order(SYMBOL, "buy", 0.01, magic=MAGIC)
    paper.market_order(SYMBOL, "buy", 0.01, magic=MAGIC)
    paper.market_order(SYMBOL, "buy", 0.01, magic=MAGIC)
    all_deals = paper.deals(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert len(all_deals) == 3
    first_hour = paper.deals(T0, T0 + timedelta(minutes=10))
    assert len(first_hour) == 2


# ================================================================ SL/TP 触发口径
#（engine_a._protective_exit：多头按 Bid、空头按 Ask 触发——逐字一致；
#  成交价 = 触发一侧现价：真实券商按市价平仓，报价流下等价于引擎 B 的
#  "第一笔穿价 tick"；engine_a 的 stop_gap/越幅是回测成本近似，不适用）。

def test_long_sl_triggers_on_bid_not_ask(tmp_path):
    """多头止损：Bid ≤ SL 触发（Ask 仍在 SL 上方也必须触发——错用 Ask 的
    实现测不出来）；成交价 = 触发时的 Bid；reason=SL。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, tp=102.0, magic=MAGIC)

    market.set_quote(99.4, 100.1)                 # Bid > SL：不触发
    assert paper.poll_protective(T0) == []
    assert len(paper.positions(SYMBOL)) == 1

    market.set_quote(98.7, 99.6)                  # Bid ≤ SL（Ask 99.6 > SL）
    now = T0 + timedelta(seconds=30)
    deals = paper.poll_protective(now)
    assert len(deals) == 1
    d = deals[0]
    assert d.reason == DEAL_REASON_SL and d.entry == "out"
    assert d.price == pytest.approx(98.7)         # 多头止损按 Bid 成交
    assert d.time == now                          # 触发时刻 = 本轮轮询时刻
    assert paper.positions(SYMBOL) == []
    # 亏损入账：(98.7 − 100.2) × 0.1 × 100 = −15
    assert paper.state().balance == pytest.approx(10_000.0 - 15.0)


def test_short_sl_triggers_on_ask_not_bid(tmp_path):
    """空头止损：Ask ≥ SL 触发（Bid 仍在 SL 下方也必须触发）；成交价 = Ask。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "sell", 0.1, sl=101.0, tp=98.0, magic=MAGIC)

    market.set_quote(100.5, 100.9)                # Ask < SL：不触发
    assert paper.poll_protective(T0) == []

    market.set_quote(100.5, 101.2)                # Ask ≥ SL（Bid 100.5 < SL）
    deals = paper.poll_protective(T0 + timedelta(seconds=30))
    assert len(deals) == 1
    d = deals[0]
    assert d.reason == DEAL_REASON_SL
    assert d.price == pytest.approx(101.2)        # 空头止损按 Ask 成交
    # 亏损：(100.0 − 101.2) × 0.1 × 100 = −12
    assert paper.state().balance == pytest.approx(10_000.0 - 12.0)


def test_long_tp_triggers_on_bid(tmp_path):
    """多头止盈：Bid ≥ TP 触发，成交价 = Bid，reason=TP。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, tp=102.0, magic=MAGIC)
    market.set_quote(102.3, 102.6)
    deals = paper.poll_protective(T0)
    assert len(deals) == 1
    assert deals[0].reason == DEAL_REASON_TP
    assert deals[0].price == pytest.approx(102.3)
    # 盈利：(102.3 − 100.2) × 0.1 × 100 = 21
    assert paper.state().balance == pytest.approx(10_000.0 + 21.0)


def test_short_tp_triggers_on_ask(tmp_path):
    """空头止盈：Ask ≤ TP 触发，成交价 = Ask，reason=TP。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "sell", 0.1, sl=101.0, tp=98.0, magic=MAGIC)
    market.set_quote(97.6, 97.9)
    deals = paper.poll_protective(T0)
    assert len(deals) == 1
    assert deals[0].reason == DEAL_REASON_TP
    assert deals[0].price == pytest.approx(97.9)
    # 盈利：(100.0 − 97.9) × 0.1 × 100 = 21
    assert paper.state().balance == pytest.approx(10_000.0 + 21.0)


def test_no_trigger_between_levels_or_without_sltp(tmp_path):
    """价位之间不触发；未设 SL/TP（0）的持仓任何报价都不触发。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, tp=102.0, magic=MAGIC)
    market.set_quote(99.5, 100.3)                 # SL < Bid < TP
    assert paper.poll_protective(T0) == []
    assert len(paper.positions(SYMBOL)) == 1

    paper2 = make_paper(tmp_path / "b", market)
    paper2.market_order(SYMBOL, "buy", 0.1, magic=MAGIC)   # 无 SL/TP
    market.set_quote(50.0, 50.2)                  # 极端价也不触发
    assert paper2.poll_protective(T0) == []
    assert len(paper2.positions(SYMBOL)) == 1


def test_poll_protective_closes_multiple_positions_in_one_round(tmp_path):
    """一轮轮询可同时处理多张持仓（各按自身 SL/TP 与方向判定）。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, magic=MAGIC)
    paper.market_order(SYMBOL, "sell", 0.1, sl=101.0, magic=MAGIC + 1)
    market.set_quote(98.5, 101.5)                 # 多头 Bid≤SL、空头 Ask≥SL
    deals = paper.poll_protective(T0)
    assert len(deals) == 2
    assert {d.reason for d in deals} == {DEAL_REASON_SL}
    assert paper.positions() == []


def test_poll_protective_skips_symbol_on_quote_failure(tmp_path):
    """行情取不到：跳过该品种本轮（下轮再判），不让瞬时故障打崩主循环。"""
    market = QuoteMarket()

    class FlakyMarket:
        def quote(self, symbol):
            raise RuntimeError("quote unavailable")

    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, magic=MAGIC)
    paper._market = FlakyMarket()
    market.set_quote(98.0, 99.5)
    assert paper.poll_protective(T0) == []
    paper._market = market
    deals = paper.poll_protective(T0)
    assert len(deals) == 1                        # 行情恢复后照常触发


# ================================================================ 状态文件重启恢复

def test_state_file_written_atomically_with_full_book(tmp_path):
    """每次成交后状态即落盘：持仓/余额/流水/票据序号全在，路径为注入的
    state_path（live/paper/state.json 契约）。"""
    paper = make_paper(tmp_path)
    paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, tp=102.0, magic=MAGIC)
    data = read_state(tmp_path)
    assert data["balance"] == pytest.approx(10_000.0)
    assert len(data["positions"]) == 1
    assert len(data["deals"]) == 1
    assert data["positions"][0]["magic"] == MAGIC
    assert os.path.exists(state_path(tmp_path))


def test_state_restart_recovers_positions_balance_and_deals(tmp_path):
    """进程重启（新 PaperBroker、同状态文件）：持仓逐字段一致、权益一致、
    已实现流水保留、成交序号接着发（不碰撞）。"""
    market = QuoteMarket()
    p1 = make_paper(tmp_path, market)
    r1 = p1.market_order(SYMBOL, "buy", 0.1, sl=99.0, tp=102.0, magic=MAGIC)
    r2 = p1.market_order(SYMBOL, "sell", 0.05, sl=101.0, magic=MAGIC + 1)
    p1.close_position(r2.order_ticket)            # 第二张已平（留已实现流水）
    before = p1.state()
    tickets_before = {p.ticket for p in p1.positions()}

    # 重启：balance 参数被状态文件覆盖（恢复优先）
    p2 = PaperBroker(market, state_path=state_path(tmp_path),
                     contract_sizes={SYMBOL: CONTRACT}, balance=999.0)
    after = p2.state()
    assert after.balance == pytest.approx(before.balance)
    assert after.equity == pytest.approx(before.equity)
    pos = p2.positions(SYMBOL)
    assert len(pos) == 1
    assert pos[0].ticket in tickets_before
    assert pos[0].side == "buy" and pos[0].volume == pytest.approx(0.1)
    assert pos[0].sl == pytest.approx(99.0) and pos[0].tp == pytest.approx(102.0)
    assert pos[0].price_open == pytest.approx(100.2)
    # 已实现流水跨重启可查（含已平仓的两腿）
    deals_closed = p2.deals_for_position(r2.order_ticket)
    assert [d.entry for d in deals_closed] == ["in", "out"]
    # 票据序号接着发：新仓 ticket 不与历史碰撞
    r3 = p2.market_order(SYMBOL, "buy", 0.01, magic=MAGIC)
    assert r3.order_ticket not in tickets_before
    assert r3.order_ticket > max(tickets_before)


def test_state_restart_preserves_deal_times_for_daily_pnl(tmp_path):
    """重启后 deals(since,to) 的时间过滤仍正确（时间往返序列化无损）。"""
    market = QuoteMarket()
    it = iter([T0, T0 + timedelta(hours=3)])
    p1 = PaperBroker(market, state_path=state_path(tmp_path),
                     contract_sizes={SYMBOL: CONTRACT},
                     clock=lambda: next(it))
    p1.market_order(SYMBOL, "buy", 0.01, sl=95.0, magic=MAGIC)  # in @T0
    market.set_quote(90.0, 90.2)
    closed = p1.poll_protective(T0 + timedelta(hours=4))   # out 成交 @T0+4h
    assert len(closed) == 1
    p2 = PaperBroker(market, state_path=state_path(tmp_path),
                     contract_sizes={SYMBOL: CONTRACT})
    assert len(p2.deals(T0, T0 + timedelta(hours=1))) == 1   # 开仓腿在 T0
    assert len(p2.deals(T0 + timedelta(hours=2),
                        T0 + timedelta(hours=5))) == 1       # 平仓腿在 T0+4h


# ================================================================ 外部出场识别闭环
#（paper 的 SL 平仓 = 服务器端保护性出场的替身：reason=SL → LiveExecutor
#  按 external_exit 解释、不改意图为漂移、对账一致不暂停）

RULES = ExitRules(stop_loss_atr=1.5, take_profit_atr=2.5)


def _executor(paper, market, tmp_path, *, fresh=False) -> LiveExecutor:
    adapter = ExecutionAdapter(paper, paper, market, symbol=SYMBOL, magic=MAGIC)
    return LiveExecutor(
        adapter, symbol=SYMBOL, magic=MAGIC,
        live_dir=str(tmp_path / "live"), rules=RULES)


def test_paper_sl_exit_recognized_as_external_exit_after_restart(tmp_path):
    """开仓 → SL 触发（paper.poll_protective）→ 执行器重启 → 识别为
    external_exit（已解释）：意图=实际净持仓、known_positions 对齐、
    对账（--paper-state 视图）一致不暂停。"""
    os.makedirs(tmp_path / "live", exist_ok=True)
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    executor = _executor(paper, market, tmp_path)

    d1 = mk_decision(decision_id=f"{SYMBOL}|M15|2026-10-28T10:00:00",
                     signal=1, volume_lots=0.1)
    rr1 = executor.poll(d1, T0)
    assert rr1.action == "open"
    pos = paper.positions(SYMBOL)[0]
    # 执行器按成交价修正 SL：100.2 − 1.5×2.0 = 97.2
    assert pos.sl == pytest.approx(97.2)
    state = json.loads((tmp_path / "live" / f"state-{MAGIC}.json")
                       .read_text(encoding="utf-8"))
    assert str(pos.ticket) in state["known_positions"]

    # SL 触发：Bid 跌破 97.2（Ask 仍在上方——错侧实现不触发）
    market.set_quote(96.9, 97.4)
    closed = paper.poll_protective(T0 + timedelta(minutes=1))
    assert len(closed) == 1 and closed[0].reason == DEAL_REASON_SL

    # 进程重启（新执行器 + 新适配器 + paper 从状态文件恢复）
    paper2 = PaperBroker(market, state_path=state_path(tmp_path),
                         contract_sizes={SYMBOL: CONTRACT})
    ex2 = _executor(paper2, market, tmp_path)
    # 信号归零的一轮：先识别外部出场（意图=空仓，cause=external_exit:<deal>）
    d2 = mk_decision(decision_id=f"{SYMBOL}|M15|2026-10-28T10:15:00",
                     signal=0, volume_lots=None)
    rr2 = ex2.poll(d2, T0 + timedelta(minutes=16))
    assert rr2.action == "hold"
    intent = json.loads((tmp_path / "live" / f"expected-{MAGIC}.json")
                        .read_text(encoding="utf-8"))
    assert intent["status"] == "confirmed"
    assert intent["cause"].startswith("external_exit:")
    assert intent["volume"] == 0.0
    # known_positions 对齐：重启后执行器状态里不再有已消失的票据
    state2 = json.loads((tmp_path / "live" / f"state-{MAGIC}.json")
                        .read_text(encoding="utf-8"))
    assert state2["known_positions"] == {}

    # 对账（--paper-state 只读视图替代真实账户）：一致、不写暂停
    mod = load_script("live_reconcile")
    rc, _ = mod.run_once(str(tmp_path / "live"),
                         PaperStateGateway(state_path(tmp_path)),
                         volume_tolerance=1e-6,
                         now=T0 + timedelta(minutes=17))
    assert rc == 0
    # 对账一致 → 不暂停（paused=False；文件可存在但不得为 true）
    pause = tmp_path / "live" / f"pause-{MAGIC}.json"
    assert (not pause.exists()
            or not json.loads(pause.read_text(encoding="utf-8"))["paused"])


# ================================================================ --paper-state
#（risk_process / live_reconcile 经 PaperStateGateway 读状态文件，不碰真实账户）

def test_paper_state_view_feeds_risk_cycle_daily_loss(tmp_path):
    """风控进程经 --paper-state 视图组装快照：当日已实现亏够 → flatten。"""
    live = tmp_path / "live"
    live.mkdir(parents=True)
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, sl=95.0, magic=MAGIC)
    market.set_quote(90.0, 90.2)                 # 触发 SL：亏 (90−100.2)×10
    closed = paper.poll_protective(T0)
    assert closed[0].profit == pytest.approx(-102.0)   # > cap 50

    mod = load_script("risk_process")
    gw = PaperStateGateway(state_path(tmp_path))
    snap = mod.build_snapshot(gw, T0)
    assert snap.currency == "USD"
    assert len(snap.deals) == 2 and snap.positions == ()

    (live / f"heartbeat-{MAGIC}.json").write_text(json.dumps({
        "runner_alive": True,
        "ts": T0.isoformat() + "+00:00"}), encoding="utf-8")
    process = RiskProcess(
        load_risk_config(str(_write(live.parent / "risk.toml", RISK_TOML))),
        live_dir=str(live))
    res = process.run_cycle(snap, T0)
    assert res.daily_loss_breached and res.flatten_written
    assert (live / "commands" / "flatten.json").exists()


def test_paper_state_view_feeds_reconcile(tmp_path):
    """对账经 --paper-state 视图读 paper 持仓：意图一致 → 0；漂移 → 1。"""
    from mt5live.reconcile import Intent, write_intent
    live = tmp_path / "live"
    live.mkdir(parents=True)
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, magic=MAGIC)

    mod = load_script("live_reconcile")
    gw = PaperStateGateway(state_path(tmp_path))
    write_intent(str(live / f"expected-{MAGIC}.json"),
                 Intent(SYMBOL, MAGIC, "buy", 0.1, "confirmed", "d1", T0))
    rc, res = mod.run_once(str(live), gw, volume_tolerance=1e-6, now=T0)
    assert rc == 0 and res.ok

    # 意图与 paper 持仓漂移（人工改了意图）→ 暂停本 magic
    write_intent(str(live / f"expected-{MAGIC}.json"),
                 Intent(SYMBOL, MAGIC, "buy", 0.5, "confirmed", "d2", T0))
    rc2, _ = mod.run_once(str(live), gw, volume_tolerance=1e-6, now=T0)
    assert rc2 == 1
    assert json.loads((live / f"pause-{MAGIC}.json")
                      .read_text(encoding="utf-8"))["paused"] is True


def test_risk_process_cli_paper_state_no_gateway(tmp_path, monkeypatch, capsys):
    """risk_process --paper-state：不连网关也能跑完一轮（rc 0）。"""
    live = tmp_path / "live"
    live.mkdir(parents=True)
    market = QuoteMarket()
    make_paper(tmp_path, market)
    (live / f"heartbeat-{MAGIC}.json").write_text(json.dumps({
        "runner_alive": True,
        "ts": datetime.now().isoformat() + "+00:00"}), encoding="utf-8")

    def _never(path):
        raise AssertionError("--paper-state 模式不得连真实网关")

    mod = load_script("risk_process")
    monkeypatch.setattr(mod, "_open_gateway", _never)
    rc = mod.main(["--config", str(_write(tmp_path / "risk.toml", RISK_TOML)),
                   "--data-dir", str(tmp_path), "--once",
                   "--paper-state", state_path(tmp_path)])
    assert rc == 0
    assert "心跳=" in capsys.readouterr().out


def test_live_reconcile_cli_paper_state_no_gateway(tmp_path, monkeypatch):
    """live_reconcile --paper-state：不连网关也能对账（意图一致 → rc 0）。"""
    from mt5live.reconcile import Intent, write_intent
    live = tmp_path / "live"
    live.mkdir(parents=True)
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    paper.market_order(SYMBOL, "buy", 0.1, magic=MAGIC)
    write_intent(str(live / f"expected-{MAGIC}.json"),
                 Intent(SYMBOL, MAGIC, "buy", 0.1, "confirmed", "d1", T0))

    def _never(path):
        raise AssertionError("--paper-state 模式不得连真实网关")

    mod = load_script("live_reconcile")
    monkeypatch.setattr(mod, "_open_gateway", _never)
    rc = mod.main(["--data-dir", str(tmp_path), "--once",
                   "--paper-state", state_path(tmp_path)])
    assert rc == 0


def test_paper_account_view_state_positions_deals(tmp_path):
    """只读视图（PaperAccountView）：state/positions/deals/deals_for_position
    全部来自状态文件，且 pending_orders 恒空。"""
    market = QuoteMarket()
    paper = make_paper(tmp_path, market)
    r = paper.market_order(SYMBOL, "buy", 0.1, sl=99.0, magic=MAGIC)
    view = PaperAccountView(state_path(tmp_path))
    assert view.pending_orders() == []
    st = view.state()
    assert st.currency == "USD"
    assert st.balance == pytest.approx(10_000.0)
    pos = view.positions(SYMBOL)
    assert len(pos) == 1 and pos[0].ticket == r.order_ticket
    assert view.deals_for_position(r.order_ticket)[0].entry == "in"


# ================================================================ 离线合成行情
#（--paper --dry-run --offline 的行情源：报价取 parquet 最新 bar 收盘价 ± 点差）

def test_bar_quote_market_quotes_last_closed_bar(tmp_path):
    """BarQuoteMarket：bid/ask = 最新 bar 收盘价 ∓ spread/2，quote.time 为
    该 bar 收盘时刻；无数据如实抛错。"""
    import pandas as pd
    from mt5data.store import save_bars
    from mt5gw.errors import DataUnavailableError
    from tests.fakes_live import mk_bars

    df = mk_bars([
        (pd.Timestamp("2026-10-28 09:45"), 100.0, 101.0, 99.0, 100.8),
        (pd.Timestamp("2026-10-28 10:00"), 100.8, 102.0, 100.5, 101.0),
    ])
    root = str(tmp_path / "data")
    save_bars(root, SYMBOL, "M15", df)
    market = BarQuoteMarket(root, symbol=SYMBOL, timeframe="M15", spread=0.2)
    q = market.quote(SYMBOL)
    assert q.bid == pytest.approx(100.9)          # 101.0 − 0.1
    assert q.ask == pytest.approx(101.1)          # 101.0 + 0.1
    assert q.time == datetime(2026, 10, 28, 10, 15)   # bar 收盘时刻

    empty = BarQuoteMarket(str(tmp_path / "nowhere"), symbol=SYMBOL,
                           timeframe="M15")
    with pytest.raises(DataUnavailableError):
        empty.quote(SYMBOL)


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)
