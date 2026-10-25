"""ExecutionAdapter：目标仓位差分六情形、幂等、SL/TP 成交价修正、retcode 分类。

全部走 FakeBroker（mt5gw 服务层替身）；真实下单由 mt5gw 保险丝管辖，不在测试范围。
T30：LiveExecutor 的意图写入（pending → confirmed / 异常返回 / 被拒轮不洗白）。
T31（D22）：只在开仓时定手数——同向持仓保持净手数不调仓（adapter 的 add/reduce
腿仍在，但 LiveExecutor 正常轮次不再触达；固定手数配置被改动也不调）。
T32（D21）：全平指令编号协议的执行器消费侧——ack 引用指令编号、已 ack 当前
编号则 flatten_acked 不再平仓、新编号指令再次全平。
"""
import json
from datetime import datetime

import pytest

from mt5gw.errors import OrderResultUnknownError, TradeRejectedError

from mt5live.executor import ExecutionAdapter, LiveExecutor

from tests.fakes_live import FakeBroker, mk_decision

MAGIC = 777
NOW = datetime(2026, 10, 20, 18, 0)


def mk_adapter(broker, **kw):
    return ExecutionAdapter(broker, broker, broker,
                            symbol=broker.symbol, magic=MAGIC, **kw)


def read_intent(tmp_path) -> dict:
    with open(tmp_path / f"expected-{MAGIC}.json", encoding="utf-8") as f:
        return json.load(f)


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


# ---- T30：LiveExecutor 的意图写入（expected-<magic>.json）----

def _mk_executor(broker, tmp_path, *, gate=None):
    from mt5quant.strategies.base import ExitRules

    return LiveExecutor(
        mk_adapter(broker), symbol=broker.symbol, magic=MAGIC,
        live_dir=str(tmp_path), rules=ExitRules(stop_loss_atr=1.5),
        gate=gate)


def test_intent_pending_then_confirmed_across_order(tmp_path):
    """下单前 pending → 订单返回后 confirmed(实际净持仓)（T30 语义 1/2）。"""
    b = FakeBroker()
    seen = {}
    orig = b.market_order

    def hooked(*a, **kw):
        seen["intent"] = dict(read_intent(tmp_path))   # 订单已发、未回
        return orig(*a, **kw)

    b.market_order = hooked
    ex = _mk_executor(b, tmp_path)
    d = mk_decision(signal=1, volume_lots=0.02, atr=2.0)
    rr = ex.poll(d, NOW)
    assert rr.action == "open"
    assert seen["intent"]["status"] == "pending"        # 下单在途
    assert seen["intent"]["volume"] == pytest.approx(0.02)
    assert seen["intent"]["cause"] == d.decision_id
    it = read_intent(tmp_path)                          # 订单返回后
    assert it["status"] == "confirmed"
    assert it["volume"] == pytest.approx(0.02)
    assert it["cause"] == d.decision_id


def test_intent_confirmed_when_order_raises(tmp_path):
    """订单异常返回（被拒）：意图如实写为当前实际净持仓（confirmed），
    绝不留一个等不到回报的 pending。"""
    b = FakeBroker()
    b.error_on_market_order = TradeRejectedError(10019, "no money")
    ex = _mk_executor(b, tmp_path)
    d = mk_decision(signal=1, volume_lots=0.02)
    with pytest.raises(TradeRejectedError):
        ex.poll(d, NOW)
    it = read_intent(tmp_path)
    assert it["status"] == "confirmed"
    assert it["volume"] == 0.0                          # 实际净持仓（空仓）
    assert it["cause"] == d.decision_id


def test_rejected_round_does_not_launder_intent(tmp_path):
    """被拒轮绝不改写意图（防洗白）——开仓腿被闸门拒：无 pending、无洗白。"""
    from mt5live.riskproc import RiskDecision

    class _RejectGate:
        def check_open(self, *a, **kw):
            return RiskDecision(False, "test_reject")

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path, gate=_RejectGate())
    d0 = mk_decision(signal=0, decision_id="XAUUSD|M15|d0")
    assert ex.poll(d0, NOW).action == "hold"
    before = read_intent(tmp_path)               # 首轮登记：意图=实际净持仓

    d1 = mk_decision(signal=1, volume_lots=0.02, decision_id="XAUUSD|M15|d1")
    rr = ex.poll(d1, NOW.replace(minute=15))
    assert rr.action == "risk_rejected"
    assert read_intent(tmp_path) == before       # 原样：无 pending、无洗白


# ---- T31（D22）：实盘只在开仓时定手数，持仓期间不调仓 ----

def test_t31_holding_keeps_net_and_skips_sizer(tmp_path):
    """同向持仓：目标=净手数（保持入场时的手数），不调仓、不查定手数器。"""
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    from mt5quant.strategies.base import ExitRules

    ex = LiveExecutor(
        mk_adapter(b), symbol=b.symbol, magic=MAGIC, live_dir=str(tmp_path),
        rules=ExitRules(stop_loss_atr=1.5))      # 无 sizer：同向轮不应需要它
    rr = ex.poll(mk_decision(signal=1, volume_lots=None, atr=2.0), NOW)
    assert rr.action == "hold"
    assert b.actions == []                       # 不下单、不平仓
    assert read_intent(tmp_path)["volume"] == pytest.approx(0.02)


def test_t31_fixed_lots_config_change_does_not_resize(tmp_path):
    """固定手数配置被人为改动（0.02 → 0.03）且同向持仓 → 仍保持净手数
    （不调仓）；意图不被改写。"""
    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    d1 = mk_decision(signal=1, volume_lots=0.02,
                     decision_id="XAUUSD|M15|d1")
    assert ex.poll(d1, NOW).action == "open"
    after_open = read_intent(tmp_path)

    d2 = mk_decision(signal=1, volume_lots=0.03,
                     decision_id="XAUUSD|M15|d2")
    rr = ex.poll(d2, NOW.replace(minute=15))
    assert rr.action == "hold"
    assert b.market_orders and len(b.market_orders) == 1
    assert b.close_calls == []
    assert read_intent(tmp_path) == after_open


def test_t31_reverse_resizes_fresh_lots(tmp_path):
    """反手：开仓腿按当时决策重定手数（这里固定手数=0.03，非旧仓 0.02）。"""
    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    d1 = mk_decision(signal=1, volume_lots=0.02,
                     decision_id="XAUUSD|M15|d1")
    assert ex.poll(d1, NOW).action == "open"
    d2 = mk_decision(signal=-1, volume_lots=0.03, atr=2.0,
                     decision_id="XAUUSD|M15|d2")
    rr = ex.poll(d2, NOW.replace(minute=15))
    assert rr.action == "reverse"
    assert b.market_orders[-1]["volume"] == pytest.approx(0.03)
    assert rr.net_after == pytest.approx(-0.03)


# ---- T32（D21）：全平指令编号协议——执行器消费侧 ----

def _write_flatten_cmd(live_dir: str, *, reason: str = "daily_loss_cap"):
    from mt5live.commands import write_flatten

    return write_flatten(live_dir, reason=reason, now=NOW)


def test_flatten_round_writes_ack_referencing_command_id(tmp_path):
    """全平：平掉本 magic 全部持仓，ack 引用指令编号；意图 cause 升级为
    flatten:<id>（与 T30 的 Intent cause 衔接）。"""
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    ex = _mk_executor(b, tmp_path)
    cmd = _write_flatten_cmd(str(tmp_path))
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02), NOW)
    assert rr.action == "flatten"
    assert b.positions() == []
    ack = json.loads((tmp_path / "commands" / f"flatten.ack-{MAGIC}")
                     .read_text(encoding="utf-8"))
    assert ack["id"] == cmd.id
    assert ack["magic"] == MAGIC and ack["closed"] == 1
    it = read_intent(tmp_path)
    assert it["volume"] == 0.0 and it["status"] == "confirmed"
    assert it["cause"] == f"flatten:{cmd.id}"


def test_flatten_acked_round_skips_reclosing(tmp_path):
    """本 magic 的 ack 已对应当前指令编号 → 返回 flatten_acked，
    不再重复平仓、不下新仓。"""
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    ex = _mk_executor(b, tmp_path)
    _write_flatten_cmd(str(tmp_path))
    ex.poll(mk_decision(signal=1, volume_lots=0.02), NOW)
    n_closes = len(b.close_calls)

    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d2"),
                 NOW.replace(minute=15))
    assert rr.action == "flatten_acked"
    assert len(b.close_calls) == n_closes
    assert b.market_orders == []


def test_flatten_new_command_id_re_closes_after_archive(tmp_path):
    """旧指令归档、ack 清理后出现新编号指令 → 再次全平并 ack 新编号。"""
    from mt5live.commands import archive_if_all_acked, write_flatten

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    write_flatten(str(tmp_path), reason="r1", now=NOW)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02), NOW).action == \
        "flatten"
    assert archive_if_all_acked(str(tmp_path), {MAGIC}, NOW) is True
    assert not (tmp_path / "commands" / "flatten.json").exists()

    b.seed_position(side="sell", volume=0.03, magic=MAGIC)
    cmd2 = write_flatten(str(tmp_path), reason="r2", now=NOW.replace(minute=30))
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d3"),
                 NOW.replace(minute=30))
    assert rr.action == "flatten"
    assert b.positions() == []
    ack2 = json.loads((tmp_path / "commands" / f"flatten.ack-{MAGIC}")
                      .read_text(encoding="utf-8"))
    assert ack2["id"] == cmd2.id
