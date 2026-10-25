"""ExecutionAdapter：目标仓位差分六情形、幂等、SL/TP 成交价修正、retcode 分类。

全部走 FakeBroker（mt5gw 服务层替身）；真实下单由 mt5gw 保险丝管辖，不在测试范围。
"""
import pytest

from mt5gw.errors import OrderResultUnknownError, TradeRejectedError

from mt5live.executor import ExecutionAdapter

from tests.fakes_live import FakeBroker, mk_decision

MAGIC = 777


def mk_adapter(broker, **kw):
    return ExecutionAdapter(broker, broker, broker,
                            symbol=broker.symbol, magic=MAGIC, **kw)


# ---- 差分六情形 ----

def test_open_from_flat_attaches_sltp():
    b = FakeBroker()                       # 无持仓
    d = mk_decision(signal=1, volume_lots=0.02, atr=2.0, sl_atr=1.5, tp_atr=2.5)
    rep = mk_adapter(b).execute(d)
    assert rep.action == "open"
    assert len(b.market_orders) == 1
    order = b.market_orders[0]
    assert order["side"] == "buy" and order["volume"] == 0.02
    assert order["magic"] == MAGIC
    # 挂单价口径的 SL/TP：多头参考 Ask=100.2 → 100.2 ∓/± k×ATR
    assert order["sl"] == pytest.approx(100.2 - 1.5 * 2.0)
    assert order["tp"] == pytest.approx(100.2 + 2.5 * 2.0)
    assert rep.net_before == 0.0
    assert rep.net_after == pytest.approx(0.02)


def test_hold_when_already_at_target():
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    rep = mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.02))
    assert rep.action == "hold"
    assert b.market_orders == [] and b.close_calls == []
    assert rep.net_after == pytest.approx(0.02)


def test_add_same_direction():
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.01, magic=MAGIC)
    rep = mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.02))
    assert rep.action == "add"
    assert b.market_orders[0]["side"] == "buy"
    assert b.market_orders[0]["volume"] == pytest.approx(0.01)
    assert b.close_calls == []


def test_reduce_same_direction():
    b = FakeBroker()
    pos = b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    rep = mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.01))
    assert rep.action == "reduce"
    assert b.close_calls == [(pos.ticket, 0.01)]
    assert b.market_orders == []


def test_reverse_closes_before_opening():
    b = FakeBroker()
    pos = b.seed_position(side="buy", volume=0.01, magic=MAGIC)
    rep = mk_adapter(b).execute(mk_decision(signal=-1, volume_lots=0.01))
    assert rep.action == "reverse"
    assert b.close_calls == [(pos.ticket, 0.01)]
    assert b.market_orders[0]["side"] == "sell"
    # 先平后开：close 在全局时序上先于 market
    kinds = [a[0] for a in b.actions]
    assert kinds.index("close") < kinds.index("market")
    assert rep.net_after == pytest.approx(-0.01)


def test_flatten_closes_all():
    b = FakeBroker()
    p1 = b.seed_position(side="buy", volume=0.01, magic=MAGIC)
    p2 = b.seed_position(side="sell", volume=0.01, magic=MAGIC)  # 对冲残仓合计 0
    p3 = b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    rep = mk_adapter(b).execute(mk_decision(signal=0, volume_lots=None))
    assert rep.action == "close"
    assert set(t for t, _ in b.close_calls) == {p1.ticket, p2.ticket, p3.ticket}
    assert b.market_orders == []
    assert rep.net_after == 0.0


# ---- 幂等 ----

def test_same_decision_replay_is_duplicate():
    b = FakeBroker()
    adapter = mk_adapter(b)
    d = mk_decision(signal=1, volume_lots=0.01)
    rep1 = adapter.execute(d)
    assert rep1.action == "open"
    n_actions = len(b.actions)
    rep2 = adapter.execute(d)
    assert rep2.action == "duplicate"
    assert len(b.actions) == n_actions        # 零新增请求


def test_hold_marks_decision_seen():
    b = FakeBroker()
    adapter = mk_adapter(b)
    rep = adapter.execute(mk_decision(signal=0, volume_lots=None))
    assert rep.action == "hold"
    rep2 = adapter.execute(mk_decision(signal=0, volume_lots=None))
    assert rep2.action == "duplicate"


# ---- SL/TP = 实际成交价 ± k×ATR（先随单提交，成交价偏离时修正）----

def test_sltp_corrected_to_actual_fill_price():
    b = FakeBroker(fill_price=100.5)          # 成交价 ≠ 参考 Ask 100.2
    d = mk_decision(signal=1, volume_lots=0.01, atr=2.0, sl_atr=1.5, tp_atr=2.5)
    rep = mk_adapter(b).execute(d)
    # 随单提交的是报价参考口径
    assert b.market_orders[0]["sl"] == pytest.approx(100.2 - 3.0)
    # 成交后修正为实际成交价口径：100.5 − 1.5×2 / 100.5 + 2.5×2
    assert len(b.modify_calls) == 1
    _, sl, tp = b.modify_calls[0]
    assert sl == pytest.approx(100.5 - 3.0)
    assert tp == pytest.approx(100.5 + 5.0)


def test_no_modify_when_fill_matches_reference():
    b = FakeBroker()                          # 默认 buy 按 ask 成交 = 参考价
    mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.01,
                                      atr=2.0, sl_atr=1.5, tp_atr=2.5))
    assert b.modify_calls == []


def test_short_sltp_on_corrected_fill():
    b = FakeBroker(fill_price=99.4)           # 空头成交价 ≠ 参考 Bid 100.0
    mk_adapter(b).execute(mk_decision(signal=-1, volume_lots=0.01,
                                      atr=2.0, sl_atr=1.5, tp_atr=2.5))
    assert b.market_orders[0]["side"] == "sell"
    _, sl, tp = b.modify_calls[0]
    assert sl == pytest.approx(99.4 + 3.0)    # 空：SL 在上方
    assert tp == pytest.approx(99.4 - 5.0)


def test_no_exit_rules_no_sltp():
    b = FakeBroker()
    mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.01,
                                      atr=2.0, sl_atr=None, tp_atr=None))
    assert b.market_orders[0]["sl"] is None
    assert b.market_orders[0]["tp"] is None
    assert b.modify_calls == []


def test_atr_none_no_sltp():
    b = FakeBroker(fill_price=100.5)
    mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.01,
                                      atr=None, sl_atr=1.5, tp_atr=2.5))
    assert b.market_orders[0]["sl"] is None
    assert b.modify_calls == []


# ---- 部分成交与 retcode 分类（复用 mt5gw 异常；执行器不重试）----

def test_partial_fill_reported_no_chase():
    b = FakeBroker(partial_volume=0.01)       # 请求 0.02 只成 0.01（DONE_PARTIAL）
    d = mk_decision(signal=1, volume_lots=0.02)
    rep = mk_adapter(b).execute(d)
    assert len(b.market_orders) == 1          # 不追单
    out = [o for o in rep.orders if o.kind == "open"][0]
    assert out.partial is True
    assert out.requested_volume == 0.02
    assert out.filled_volume == pytest.approx(0.01)
    assert rep.net_after == pytest.approx(0.01)


def test_rejected_propagates_without_retry():
    b = FakeBroker()
    adapter = mk_adapter(b)
    b.error_on_market_order = TradeRejectedError(10019, "no money")
    d = mk_decision(signal=1, volume_lots=0.01)
    with pytest.raises(TradeRejectedError):
        adapter.execute(d)
    assert len(b.market_orders) == 1          # 执行器层零重试（Requote 重试在网关内部）
    # 被拒的决策不标记已执行 → 下次触发允许再试（目标仓位语义自愈）
    b.error_on_market_order = None
    rep = adapter.execute(d)
    assert rep.action == "open"
    assert len(b.market_orders) == 2


def test_unknown_result_propagates_without_retry():
    b = FakeBroker()
    b.error_on_market_order = OrderResultUnknownError("IPC 中断")
    with pytest.raises(OrderResultUnknownError):
        mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.01))
    assert len(b.market_orders) == 1          # Unknown：先对账，绝不盲目重发


# ---- magic 过滤与遗留挂单 ----

def test_only_our_magic_is_managed():
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.05, magic=999)     # 别人的仓
    ours = b.seed_pending(magic=MAGIC)
    theirs = b.seed_pending(magic=999)
    rep = mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.01))
    # 别人的持仓不计入净头寸 → 我们按目标开 0.01
    assert rep.net_before == 0.0
    assert rep.action == "open"
    assert b.market_orders[0]["volume"] == pytest.approx(0.01)
    # 我们 magic 的遗留挂单撤销（目标仓位语义只走市价），别人的不动
    assert b.cancel_calls == [ours.ticket]
    assert theirs in b.pending_orders()
    foreign = [p for p in b.positions() if p.magic == 999]
    assert len(foreign) == 1


# ---- 手数缺失 ----

def test_signal_without_volume_rejected():
    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=None)   # 风控进程未给手数
    with pytest.raises(ValueError, match="volume_lots"):
        mk_adapter(b).execute(d)
    assert b.actions == []                    # 一单未发
