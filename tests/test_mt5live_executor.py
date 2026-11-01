"""ExecutionAdapter：目标仓位差分六情形、幂等、SL/TP 成交价修正、retcode 分类。

全部走 FakeBroker（mt5gw 服务层替身）；真实下单由 mt5gw 保险丝管辖，不在测试范围。
T30：LiveExecutor 的意图写入（pending → confirmed / 异常返回 / 被拒轮不洗白）。
T31（D22）：只在开仓时定手数——同向持仓保持净手数不调仓（adapter 的 add/reduce
腿仍在，但 LiveExecutor 正常轮次不再触达；固定手数配置被改动也不调）。
T32（D21）：全平指令编号协议的执行器消费侧——ack 引用指令编号、已 ack 当前
编号则 flatten_acked 不再平仓、新编号指令再次全平。
"""
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mt5gw.errors import RETCode, OrderResultUnknownError, TradeRejectedError

from mt5live.executor import ExecutionAdapter, LiveExecutor
from mt5live.fileio import append_jsonl
from mt5live.monitor import iso_utc
from mt5live.reconcile import STATUS_PENDING, Intent, write_intent

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
    assert len(b.market_orders) == 1          # 非 requote 被拒零重试（requote 见下）
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


# ---- T89/R14-L5：requote(10004) 是确定态拒绝（未成交）——执行器重试一次 ----
# 2026-10-29 交易日 2 事故：平仓腿 10004 网关 3 次重试耗尽后
# TradeRejectedError 上抛，runner 未捕获崩溃退出（docs/ops/phase-f-log.md）。
# requote 语义=服务器给出新价、订单**确定未成交**——与 Unknown（状态未知，
# 先对账）不同，重发安全；执行器对 TradeRejectedError(REQUOTE) 额外重试
# 一次（网关内部重试之外的第二道防线），仍拒则原样上抛（风控停机路径照旧）。

def test_requote_on_open_retried_once_and_succeeds():
    b = FakeBroker()
    calls = {"n": 0}
    orig = b.market_order

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TradeRejectedError(RETCode.REQUOTE, "Requote")
        return orig(*a, **kw)

    b.market_order = flaky
    rep = mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.01))
    assert rep.action == "open"               # 重试后成交，不再崩溃上抛
    assert calls["n"] == 2
    assert len(b.market_orders) == 1          # 第一次未成交，第二次成交


def test_requote_on_close_retried_once_and_succeeds():
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.01, magic=MAGIC)
    calls = {"n": 0}
    orig = b.close_position

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TradeRejectedError(RETCode.REQUOTE, "Requote")
        return orig(*a, **kw)

    b.close_position = flaky
    rep = mk_adapter(b).execute(mk_decision(signal=0, volume_lots=None))
    assert rep.action == "close"              # 事故现场：平仓腿 requote → 重试成功
    assert calls["n"] == 2


def test_requote_persistent_raises_after_single_retry():
    b = FakeBroker()
    b.error_on_market_order = TradeRejectedError(RETCode.REQUOTE, "Requote")
    with pytest.raises(TradeRejectedError):
        mk_adapter(b).execute(mk_decision(signal=1, volume_lots=0.01))
    assert len(b.market_orders) == 2          # 有界：只重试一次，仍拒原样上抛


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


# ================================================================ R6（第六轮审核）
# C1：全平部分成交不得写完成 ack（残仓下一轮继续平，两次后才归档）
# C2：未解释外部出场持久化 + 执行器开仓第二道防线（不依赖对账 pause）

def test_r6_flatten_partial_fill_no_ack_until_flat(tmp_path):
    """C1 复现：0.16 手只平一半 → 残仓 0.08、不写完成 ack、意图如实=残仓；
    下一轮继续消费同一条指令平掉残仓 → 清零后才写 ack；两次后才归档。"""
    from mt5live.commands import archive_if_all_acked, read_ack, write_flatten

    b = FakeBroker()
    b.seed_position(side="buy", volume=0.16, magic=MAGIC)
    ex = _mk_executor(b, tmp_path)
    cmd = write_flatten(str(tmp_path), reason="daily_loss_cap", now=NOW)

    b.partial_fill = 0.5                      # 本轮平仓请求只成一半
    rr1 = ex.poll(mk_decision(signal=1, volume_lots=0.16), NOW)
    assert rr1.action == "flatten"
    assert rr1.net_after == pytest.approx(0.08)          # 残仓 0.08
    assert b.positions()[0].volume == pytest.approx(0.08)
    # 完成条件未满足：不写 ack（读不出 → None，不是旧编号）
    assert read_ack(str(tmp_path), MAGIC) is None
    it = read_intent(tmp_path)                           # 意图如实=残仓净额
    assert it["status"] == "confirmed"
    assert it["volume"] == pytest.approx(0.08)
    assert it["cause"] == f"flatten:{cmd.id}"
    assert "FLATTEN_RESIDUAL" in (tmp_path / "alerts.log").read_text(
        encoding="utf-8")
    # 归档：ack 缺失 → 不归档（指令原地保留）
    assert archive_if_all_acked(str(tmp_path), {MAGIC},
                                NOW.replace(minute=10)) is False
    assert (tmp_path / "commands" / "flatten.json").exists()

    # 下一轮：同一条指令继续消费（不得 flatten_acked 跳过），残仓被平掉
    b.partial_fill = None
    rr2 = ex.poll(mk_decision(signal=1, volume_lots=0.16,
                              decision_id="XAUUSD|M15|d2"),
                  NOW.replace(minute=15))
    assert rr2.action == "flatten"                       # 不是 flatten_acked
    assert b.positions() == []
    ack = read_ack(str(tmp_path), MAGIC)
    assert ack is not None and ack.id == cmd.id          # 完成才写 ack
    it2 = read_intent(tmp_path)
    assert it2["volume"] == 0.0 and it2["status"] == "confirmed"
    # 两次之后才归档成功（"全员真完成"）
    assert archive_if_all_acked(str(tmp_path), {MAGIC},
                                NOW.replace(minute=30)) is True
    assert not (tmp_path / "commands" / "flatten.json").exists()


def test_r6_flatten_cancels_pendings_before_ack(tmp_path):
    """C1 完成判据含挂单：全平腿先撤本 magic 挂单，持仓与挂单均清零才 ack。"""
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    b.seed_pending(magic=MAGIC)
    ex = _mk_executor(b, tmp_path)
    _write_flatten_cmd(str(tmp_path))
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02), NOW)
    assert rr.action == "flatten"
    assert b.positions() == []
    assert b.cancel_calls and b.pending_orders() == []
    from mt5live.commands import read_ack
    assert read_ack(str(tmp_path), MAGIC) is not None


def test_r6_unexplained_exit_blocks_reopen_until_ack(tmp_path):
    """C2 复现 B 反转（执行器层）：人工平仓（CLIENT）→ 未决出场持久化；
    immediate 同向信号也不重开（先于对账运行，不依赖 pause）；
    live_ack_external 确认（绑定该事件）后恢复。平仓腿不受限。"""
    from mt5gw.models import DEAL_REASON_CLIENT

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket
    b.close_external(ticket, profit=5.0, comment="manual",
                     reason=DEAL_REASON_CLIENT)

    # 执行器先于对账运行：消失且无法解释 → 不重开（第二道防线）
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d2"),
                 NOW.replace(minute=15))
    assert rr.action == "blocked_external_exit"
    assert "external_exit_pending" in rr.error
    assert len(b.market_orders) == 1                      # 没有重开
    state = json.loads((tmp_path / f"state-{MAGIC}.json")
                       .read_text(encoding="utf-8"))
    assert str(ticket) in state["pending_external_exits"]  # 未决持久化
    entry = state["pending_external_exits"][str(ticket)]
    assert entry["dir"] == 1 and entry["volume"] == pytest.approx(0.02)
    assert "EXTERNAL_EXIT" in (tmp_path / "alerts.log").read_text(
        encoding="utf-8")
    assert read_intent(tmp_path)["volume"] == pytest.approx(0.02)  # 不洗白

    # 平仓照常：期间出现的持仓，信号归零照常平掉（防线只挡开仓腿）
    b.seed_position(side="buy", volume=0.03, magic=MAGIC)
    rr_close = ex.poll(mk_decision(signal=0, volume_lots=None,
                                   decision_id="XAUUSD|M15|d3"),
                       NOW.replace(minute=30))
    assert rr_close.action == "close"
    assert b.positions() == []
    # 反向信号：平仓腿照常、开仓腿仍被挡
    b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    rr_rev = ex.poll(mk_decision(signal=-1, volume_lots=0.02,
                                 decision_id="XAUUSD|M15|d4"),
                     NOW.replace(minute=45))
    assert rr_rev.action == "blocked_external_exit"
    assert any(o.kind == "close" for o in rr_rev.orders)  # 平仓腿执行了
    assert b.positions() == []
    assert len(b.market_orders) == 1                      # 仍无新开仓

    # 人工确认（live_ack_external 的落盘效果，N-C1/R7 绑定协议：绑定
    # 未决事件 ticket + 确认时刻 + 净仓快照 0——此刻实际空仓）
    _write_ack_external_intent(tmp_path, tickets=(ticket,), net=0.0,
                               positions=[], now=NOW.replace(minute=50))
    rr2 = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                              decision_id="XAUUSD|M15|d5"),
                  NOW.replace(minute=51))
    assert rr2.action == "open"                           # ack 后恢复开仓
    assert len(b.market_orders) == 2
    state2 = json.loads((tmp_path / f"state-{MAGIC}.json")
                        .read_text(encoding="utf-8"))
    assert state2["pending_external_exits"] == {}         # 未决已清


def test_r6_pending_exit_survives_restart_and_deal_query_failure(tmp_path):
    """C2：未决出场跨重启持久化；成交查询持续失败也保持未决（fail-closed），
    人工 ack（绑定协议）仍能解除。"""
    from mt5gw.models import DEAL_REASON_CLIENT

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket
    b.close_external(ticket, profit=5.0, comment="manual",
                     reason=DEAL_REASON_CLIENT)

    def _boom(position_id):
        raise RuntimeError("history_select 不可用")

    b.deals_for_position = _boom
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "blocked_external_exit"
    del b.deals_for_position                     # 恢复真查询（仍查得到 CLIENT）

    # 重启（新执行器，状态从 state-<magic>.json 恢复）：未决仍在、仍挡开仓
    ex2 = _mk_executor(b, tmp_path)
    rr = ex2.poll(mk_decision(signal=1, volume_lots=0.02,
                              decision_id="XAUUSD|M15|d3"),
                  NOW.replace(minute=30))
    assert rr.action == "blocked_external_exit"
    state = json.loads((tmp_path / f"state-{MAGIC}.json")
                       .read_text(encoding="utf-8"))
    assert str(ticket) in state["pending_external_exits"]

    # 人工确认（绑定协议）→ 解除
    _write_ack_external_intent(tmp_path, tickets=(ticket,), net=0.0,
                               positions=[], now=NOW.replace(minute=40))
    assert ex2.poll(mk_decision(signal=1, volume_lots=0.02,
                                decision_id="XAUUSD|M15|d4"),
                    NOW.replace(minute=41)).action == "open"


# ================================================================ R7（第七轮审核）
# C1 残留：净零残仓——多空各留 0.08 时净额恰 0，不得判全平完成
# N-C1：人工确认绑定事件——旧确认不得放行确认之后的新出场

def _write_ack_external_intent(tmp_path, *, tickets, net: float,
                               positions, now) -> None:
    """模拟 scripts/live_ack_external.py 的落盘效果（N-C1/R7 确认绑定协议）。

    与脚本写出同一 JSON 形状（意图 7 字段 + confirmed_events 绑定）——
    集成测试用真脚本核对形状不漂移。"""
    from mt5live.fileio import write_json_atomic
    from mt5live.monitor import iso_utc
    from mt5live.reconcile import STATUS_CONFIRMED

    write_json_atomic(str(tmp_path / f"expected-{MAGIC}.json"), {
        "symbol": "XAUUSD", "magic": MAGIC,
        "side": "buy" if net >= 0 else "sell", "volume": abs(net),
        "status": STATUS_CONFIRMED, "cause": "ack_external",
        "ts": iso_utc(now),
        "confirmed_events": {
            "tickets": sorted(int(t) for t in tickets),
            "confirmed_at": iso_utc(now),
            "net_volume": float(net),
            "positions": list(positions),
        },
    })


def test_r7_flatten_net_zero_residual_positions_not_acked(tmp_path):
    """C1 残留复现：多空各 0.16 对冲仓全平、每笔只成一半 → 多空各残留
    0.08、净额恰为 0——按净额判完成会写完成 ack（归档=全员真完成的语义
    被打破）。正确判据是持仓明细：明细非空 → 不写 ack、写残仓告警，
    下一轮继续平，明细清零后才写 ack。"""
    from mt5live.commands import archive_if_all_acked, read_ack, write_flatten

    b = FakeBroker()
    b.seed_position(side="buy", volume=0.16, magic=MAGIC)
    b.seed_position(side="sell", volume=0.16, magic=MAGIC)
    ex = _mk_executor(b, tmp_path)
    cmd = write_flatten(str(tmp_path), reason="daily_loss_cap", now=NOW)

    b.partial_fill = 0.5                      # 每笔平仓请求只成一半
    rr1 = ex.poll(mk_decision(signal=1, volume_lots=0.16), NOW)
    assert rr1.action == "flatten"
    leftovers = b.positions()
    assert len(leftovers) == 2                # 多空各残留 0.08
    assert sum(p.volume if p.side == "buy" else -p.volume
               for p in leftovers) == pytest.approx(0.0)   # 净额恰为 0
    # 净零但明细非空 → 残仓：不得写完成 ack（读不出 → None）
    assert read_ack(str(tmp_path), MAGIC) is None
    assert "FLATTEN_RESIDUAL" in (tmp_path / "alerts.log").read_text(
        encoding="utf-8")
    assert archive_if_all_acked(str(tmp_path), {MAGIC},
                                NOW.replace(minute=10)) is False
    assert (tmp_path / "commands" / "flatten.json").exists()

    # 下一轮继续消费同一条指令（不得 flatten_acked）：残仓被平掉、
    # 明细清零才写 ack，两次之后才归档
    b.partial_fill = None
    rr2 = ex.poll(mk_decision(signal=1, volume_lots=0.16,
                              decision_id="XAUUSD|M15|d2"),
                  NOW.replace(minute=15))
    assert rr2.action == "flatten"
    assert b.positions() == []
    ack = read_ack(str(tmp_path), MAGIC)
    assert ack is not None and ack.id == cmd.id
    assert archive_if_all_acked(str(tmp_path), {MAGIC},
                                NOW.replace(minute=30)) is True


def test_r7_nc1_old_ack_cannot_release_new_external_exit(tmp_path):
    """N-C1 复现序列：开 0.16 → 人工减至 0.08 并确认（绑定空事件集、
    净仓快照 0.08）→ 无订单轮保留 cause=ack_external → 再次人工平掉余仓
    → 执行器先于对账运行——旧确认不得放行新出场（旧实现 action=open
    重开 0.16 是复现基线），必须 blocked_external_exit；再次人工确认
    （这次绑定新事件）后恢复开仓。"""
    from mt5gw.models import DEAL_REASON_CLIENT

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.16,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket

    # 人工减仓至 0.08（ticket 仍在 → 不是出场事件）→ 无订单轮（hold，
    # 意图不被改写）
    b.reduce_external(ticket, 0.08, profit=2.0, comment="manual",
                      reason=DEAL_REASON_CLIENT)
    rr_hold = ex.poll(mk_decision(signal=1, volume_lots=0.16,
                                  decision_id="XAUUSD|M15|d2"),
                      NOW.replace(minute=15))
    assert rr_hold.action == "hold"
    assert len(b.market_orders) == 1

    # 人工确认（N-C1/R7 协议：绑定被确认事件集合+确认时刻+净仓快照；
    # 此刻无未决出场 → 绑定空集、快照 0.08）
    _write_ack_external_intent(tmp_path, tickets=(), net=0.08,
                               positions=[{"ticket": ticket, "side": "buy",
                                           "volume": 0.08}],
                               now=NOW.replace(minute=20))

    # 无订单轮：意图保留 cause=ack_external（hold 不改写意图）
    rr_hold2 = ex.poll(mk_decision(signal=1, volume_lots=0.16,
                                   decision_id="XAUUSD|M15|d3"),
                       NOW.replace(minute=30))
    assert rr_hold2.action == "hold"
    assert read_intent(tmp_path)["cause"] == "ack_external"

    # 再次人工平掉余仓 → 执行器先于对账运行：新出场事件不被旧确认放行
    b.close_external(ticket, profit=1.0, comment="manual",
                     reason=DEAL_REASON_CLIENT)
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.16,
                             decision_id="XAUUSD|M15|d4"),
                 NOW.replace(minute=45))
    assert rr.action == "blocked_external_exit"
    assert len(b.market_orders) == 1                     # 没有重开 0.16
    state = json.loads((tmp_path / f"state-{MAGIC}.json")
                       .read_text(encoding="utf-8"))
    assert str(ticket) in state["pending_external_exits"]  # 事件回到未决

    # 再次人工确认（这次绑定新事件 ticket、快照=空仓）→ 解除防线恢复开仓
    _write_ack_external_intent(tmp_path, tickets=(ticket,), net=0.0,
                               positions=[], now=NOW.replace(minute=50))
    rr2 = ex.poll(mk_decision(signal=1, volume_lots=0.16,
                              decision_id="XAUUSD|M15|d5"),
                  NOW.replace(minute=51))
    assert rr2.action == "open"
    assert len(b.market_orders) == 2
    state2 = json.loads((tmp_path / f"state-{MAGIC}.json")
                        .read_text(encoding="utf-8"))
    assert state2["pending_external_exits"] == {}


def test_r7_ack_without_event_binding_does_not_release(tmp_path):
    """N-C1 fail-closed：旧格式确认（cause=ack_external 但无 confirmed_events
    绑定）不得放行任何未决出场——永久有效的裸确认字符串正是原缺陷。"""
    from mt5gw.models import DEAL_REASON_CLIENT
    from mt5live.reconcile import STATUS_CONFIRMED, Intent, write_intent

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket
    b.close_external(ticket, profit=5.0, comment="manual",
                     reason=DEAL_REASON_CLIENT)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "blocked_external_exit"

    # 旧格式确认（无绑定）：意图 confirmed + cause=ack_external 落盘
    write_intent(str(tmp_path / f"expected-{MAGIC}.json"),
                 Intent(b.symbol, MAGIC, "buy", 0.0, STATUS_CONFIRMED,
                        "ack_external", NOW.replace(minute=20)))
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d3"),
                 NOW.replace(minute=21))
    assert rr.action == "blocked_external_exit"          # 不放行
    state = json.loads((tmp_path / f"state-{MAGIC}.json")
                       .read_text(encoding="utf-8"))
    assert str(ticket) in state["pending_external_exits"]

    # 绑定协议的确认才解除
    _write_ack_external_intent(tmp_path, tickets=(ticket,), net=0.0,
                               positions=[], now=NOW.replace(minute=30))
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d4"),
                   NOW.replace(minute=31)).action == "open"


# ================================================================ R10/T58（C-2）
# deals-<magic>.jsonl 成交证据流：主动成交 fill_open/fill_close、外部/保护性
# 出场解释 external_exit（reason/price 如实入行；写入失败不阻断交易）。
# 契约见 docs/superpowers/plans/2026-10-27-round10-review-fixes.md §0 C-2。

def _deals_lines(tmp_path) -> list[dict]:
    """读 deals-<magic>.jsonl 全部行（文件不存在即断言失败）。"""
    p = tmp_path / f"deals-{MAGIC}.jsonl"
    assert p.exists(), "deals-<magic>.jsonl 证据文件应存在"
    text = p.read_text(encoding="utf-8")
    return [json.loads(ln) for ln in text.splitlines() if ln]


def test_t58_fill_open_and_close_written_with_decision_bar(tmp_path):
    """主动成交两处：开仓 fill_open（price=成交价、decision_bar=决策 bar
    收盘时刻、reason=null）、平仓 fill_close（side=成交方向）。"""
    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    d1 = mk_decision(signal=1, volume_lots=0.02,
                     decision_id="XAUUSD|M15|d1",
                     bar_close_time=datetime(2026, 10, 20, 18, 0))
    assert ex.poll(d1, NOW).action == "open"
    lines = _deals_lines(tmp_path)
    assert len(lines) == 1                          # 只有开仓成交一行
    rec = lines[0]
    assert set(rec) == {"ts", "magic", "ticket", "event", "reason", "price",
                        "volume", "side", "decision_bar"}
    assert rec["event"] == "fill_open" and rec["reason"] is None
    assert rec["magic"] == MAGIC
    assert rec["price"] == pytest.approx(100.2)     # buy 按 Ask 成交
    assert rec["volume"] == pytest.approx(0.02)
    assert rec["side"] == "buy"
    assert rec["ticket"] == b.positions()[0].ticket
    assert rec["decision_bar"] == "2026-10-20T18:00:00+00:00"
    assert rec["ts"] == "2026-10-20T18:00:00+00:00"  # 本轮时刻（iso_utc）

    d2 = mk_decision(signal=0, volume_lots=None, decision_id="XAUUSD|M15|d2",
                     bar_close_time=datetime(2026, 10, 20, 18, 15))
    assert ex.poll(d2, NOW.replace(minute=15)).action == "close"
    rec2 = _deals_lines(tmp_path)[1]
    assert rec2["event"] == "fill_close" and rec2["reason"] is None
    assert rec2["side"] == "sell"                   # 平多头 → 成交方向 sell
    assert rec2["price"] == pytest.approx(100.0)    # buy 平仓按 Bid
    assert rec2["volume"] == pytest.approx(0.02)
    assert rec2["decision_bar"] == "2026-10-20T18:15:00+00:00"


def test_t58_external_exit_written_with_reason_and_price(tmp_path):
    """外部/保护性出场解释（_explain_exit 的 deal）：external_exit 行带
    reason=SL、price=成交价、volume/side 如实；decision_bar=检测当轮决策 bar。"""
    from mt5gw.models import DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket
    b.close_external(ticket, profit=-30.0, comment="[sl]",
                     reason=DEAL_REASON_SL)

    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d2",
                             bar_close_time=datetime(2026, 10, 20, 18, 15)),
                 NOW.replace(minute=15))
    assert rr.action in ("open", "blocked_reentry")  # immediate 默认 → open
    lines = _deals_lines(tmp_path)
    ext = [r for r in lines if r["event"] == "external_exit"]
    assert len(ext) == 1
    rec = ext[0]
    assert rec["reason"] == "SL"
    assert rec["price"] == pytest.approx(100.2)    # close_external 记 price_current
    assert rec["volume"] == pytest.approx(0.02)
    assert rec["side"] == "sell"                    # 平多头的成交方向
    assert rec["magic"] == MAGIC and rec["ticket"]  # deal ticket 如实
    assert rec["decision_bar"] == "2026-10-20T18:15:00+00:00"  # 检测当轮 bar
    assert any(r["event"] == "fill_open" for r in lines)       # 开仓行仍在


def test_t58_external_exit_decision_bar_null_without_decision(tmp_path):
    """检测轮无决策（decision=None，如休市轮询）→ external_exit 行的
    decision_bar=null（当轮没有 bar 可引用）。"""
    from mt5gw.models import DEAL_REASON_TP

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket
    b.close_external(ticket, profit=30.0, comment="[tp]",
                     reason=DEAL_REASON_TP)
    rr = ex.poll(None, NOW.replace(minute=15))      # 无决策轮：外部出场照常检测
    assert rr.action == "none"
    ext = [r for r in _deals_lines(tmp_path) if r["event"] == "external_exit"]
    assert len(ext) == 1 and ext[0]["reason"] == "TP"
    assert ext[0]["decision_bar"] is None


def test_t58_deals_write_failure_alerts_but_does_not_block(tmp_path,
                                                           monkeypatch):
    """C-2 fail-open 边界：deals 证据写入失败 → 记告警、交易照常（仓位真实
    开出）；orders-<magic>.jsonl 等其他证据流不受牵连。"""
    from mt5live import executor as executor_mod
    from mt5live.fileio import append_jsonl as real_append

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)

    def selective_boom(path, obj):
        if os.path.basename(path).startswith("deals-"):
            raise PermissionError(5, "拒绝访问")
        return real_append(path, obj)

    monkeypatch.setattr(executor_mod, "append_jsonl", selective_boom)
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d1"), NOW)
    assert rr.action == "open"                      # 交易路径不被证据流失败阻断
    assert len(b.positions()) == 1                  # 仓位真实开出
    assert "DEALS_LOG_ERROR" in (tmp_path / "alerts.log").read_text(
        encoding="utf-8")
    orders_log = (tmp_path / f"orders-{MAGIC}.jsonl").read_text(
        encoding="utf-8")
    assert "fill" not in orders_log and '"kind": "open"' in orders_log


# ================================================================ H-04/H-05
# （docs/reviews/2026-10-29-global-code-audit.md）：实盘部分成交/清仓 + 状态
# 损坏保守恢复。每项先写复现旧缺陷的失败测试（红），再修复转绿。

# ---- H-04-1：反向平仓未确认清零前禁止开反向仓（部分成交 → 多空对锁）----

def test_h04_reverse_partial_close_never_opens_hedge():
    """复现：0.02 多仓反手、平仓请求只成一半（DONE_PARTIAL）→ 旧实现残仓
    0.01 多 + 新开 0.02 空 → 多空对锁；修复后未确认残仓清零前绝不开反向仓
    （action=reverse_pending，只平不开），下个决策点再续做反向。"""
    b = FakeBroker()
    pos = b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    adapter = mk_adapter(b)
    b.partial_fill = 0.5                       # 平仓 0.02 只成 0.01
    rep = adapter.execute(mk_decision(signal=-1, volume_lots=0.02))
    assert rep.action == "reverse_pending"     # 旧：reverse（对锁已形成）
    assert b.close_calls == [(pos.ticket, 0.02)]
    assert b.market_orders == []               # 反向开仓腿被禁
    left = b.positions()
    assert [p.side for p in left] == ["buy"]   # 无对锁：残仓留在旧方向
    assert left[0].volume == pytest.approx(0.01)

    # 残仓确认仍在途 → 下个决策点（新 decision_id）续做反向：先平残仓、
    # 确认清零后才开新仓
    b.partial_fill = None
    rep2 = adapter.execute(mk_decision(signal=-1, volume_lots=0.02,
                                       decision_id="XAUUSD|M15|d2"))
    assert rep2.action == "reverse"
    kinds = [a[0] for a in b.actions]
    assert kinds.index("close") < kinds.index("market")   # 先平后开
    assert [p.side for p in b.positions()] == ["sell"]
    assert rep2.net_after == pytest.approx(-0.02)


# ---- H-04-2：signal=0 且净仓恰为 0 的对锁仓按 ticket 逐仓平掉 ----

def test_h04_net_zero_hedge_closes_all_on_flat_signal():
    """复现：signal=0、净仓 0 但有多空各 0.01 对锁 → 旧实现 hold 留两张
    仓位；修复后清仓按持仓 ticket 判断（非净额），逐仓平掉。"""
    b = FakeBroker()
    p1 = b.seed_position(side="buy", volume=0.01, magic=MAGIC)
    p2 = b.seed_position(side="sell", volume=0.01, magic=MAGIC)
    rep = mk_adapter(b).execute(mk_decision(signal=0, volume_lots=None))
    assert rep.action == "close"               # 旧：hold（净 0 == 目标 0）
    assert set(t for t, _ in b.close_calls) == {p1.ticket, p2.ticket}
    assert b.positions() == [] and b.market_orders == []
    assert rep.net_after == 0.0


def test_h04_live_executor_closes_net_zero_hedge(tmp_path):
    """LiveExecutor 同口径：净 0 对锁 + signal=0 → close（旧 hold）。"""
    b = FakeBroker()
    b.seed_position(side="buy", volume=0.01, magic=MAGIC)
    b.seed_position(side="sell", volume=0.01, magic=MAGIC)
    ex = _mk_executor(b, tmp_path)
    rr = ex.poll(mk_decision(signal=0, volume_lots=None,
                             decision_id="XAUUSD|M15|d1"), NOW)
    assert rr.action == "close"                # 旧：hold
    assert b.positions() == []


def test_h04_hedge_close_not_blocked_when_open_defense_active(tmp_path):
    """开仓防线（pending_external_exits / 状态损坏）只挡开仓腿：signal=0
    的净零对锁清仓属于平仓，照常执行（旧实现按净额判 hold，对锁仓留场，
    未决出场期间永远清不掉）。"""
    from mt5gw.models import DEAL_REASON_CLIENT

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket
    b.close_external(ticket, profit=5.0, comment="manual",
                     reason=DEAL_REASON_CLIENT)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "blocked_external_exit"

    b.seed_position(side="buy", volume=0.01, magic=MAGIC)   # 净零对锁残仓
    b.seed_position(side="sell", volume=0.01, magic=MAGIC)
    rr = ex.poll(mk_decision(signal=0, volume_lots=None,
                             decision_id="XAUUSD|M15|d3"),
                 NOW.replace(minute=30))
    assert rr.action == "close"                # 旧：blocked_external_exit
    assert b.positions() == []


# ---- H-04-3：风控取数异常只拒开仓，平仓腿照常 ----

def test_h04_risk_gate_error_reverse_still_closes(tmp_path):
    """复现：反向决策时风控闸门 check_open 抛异常 → 旧实现直接
    risk_rejected 返回，平仓腿也不执行（违反"平仓不受限"）；修复后异常
    fail-closed 只拒开仓腿，平仓腿照常先平后不开。"""
    class _BoomGate:
        def check_open(self, *a, **kw):
            raise RuntimeError("行情快照不可用")

    b = FakeBroker()
    b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    ex = _mk_executor(b, tmp_path, gate=_BoomGate())
    rr = ex.poll(mk_decision(signal=-1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d1"), NOW)
    assert rr.action == "risk_rejected"
    assert "risk_gate_error" in rr.error
    assert any(o.kind == "close" for o in rr.orders)   # 旧：orders 空
    assert b.market_orders == []                        # 开仓腿被拒（fail-closed）
    assert b.positions() == []                          # 平仓腿已执行
    it = read_intent(tmp_path)
    assert it["status"] == "confirmed" and it["volume"] == 0.0

    # 纯开仓（net==0）遇风控异常：拒绝且无订单（fail-closed 不变）
    rr2 = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                              decision_id="XAUUSD|M15|d2"),
                  NOW.replace(minute=15))
    assert rr2.action == "risk_rejected"
    assert rr2.orders == () and b.market_orders == []


# ---- H-05-4：状态文件损坏 → 保守恢复（不清保护 + 保留证据 + 暂停开仓）----

def test_h05_corrupt_state_recovery_is_conservative(tmp_path):
    """复现：状态文件损坏/不可读 → 旧实现从零开始（blocked_dir/
    pending_external_exits 的保护被清空、开仓放行）；修复后区分首次启动
    与损坏恢复——损坏时暂停开新仓（平仓不受影响）、损坏原文抄送
    *.corrupt-* 保留证据，人工核对前跨重启持续封锁。"""
    from mt5gw.models import DEAL_REASON_CLIENT

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)             # 首次启动：无状态文件
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket
    b.close_external(ticket, profit=5.0, comment="manual",
                     reason=DEAL_REASON_CLIENT)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "blocked_external_exit"

    # 截断状态文件（损坏）——原文里有未决出场保护
    state_path = tmp_path / f"state-{MAGIC}.json"
    raw = state_path.read_text(encoding="utf-8")
    truncated = raw[: max(1, len(raw) // 2)]
    state_path.write_text(truncated, encoding="utf-8")

    ex2 = _mk_executor(b, tmp_path)            # 损坏恢复启动
    rr = ex2.poll(mk_decision(signal=1, volume_lots=0.02,
                              decision_id="XAUUSD|M15|d3"),
                  NOW.replace(minute=30))
    assert rr.action == "blocked_state_corrupt"     # 旧：open（保护被清）
    assert "state_corrupt" in rr.error
    assert len(b.market_orders) == 1                # 没有重开
    # 证据保留：损坏原文抄送到 state-<magic>.json.corrupt-*
    corrupt = sorted(tmp_path.glob(f"state-{MAGIC}.json.corrupt-*"))
    assert corrupt and truncated in \
        corrupt[-1].read_text(encoding="utf-8")
    assert "STATE_CORRUPT" in (tmp_path / "alerts.log").read_text(
        encoding="utf-8")

    # 平仓不受影响（允许风险降低操作）：signal=0 照常平掉
    b.seed_position(side="buy", volume=0.03, magic=MAGIC)
    rr_close = ex2.poll(mk_decision(signal=0, volume_lots=None,
                                    decision_id="XAUUSD|M15|d4"),
                        NOW.replace(minute=45))
    assert rr_close.action == "close"
    assert b.positions() == []

    # 封锁跨重启持续（state_corrupt 随新状态文件持久化），人工修复前不放开
    ex3 = _mk_executor(b, tmp_path)
    rr3 = ex3.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d5"),
                   NOW.replace(minute=50))
    assert rr3.action == "blocked_state_corrupt"
    assert len(b.market_orders) == 1


# ---- H-05-5：last_decision_id 用于决策去重（同 bar 重启不重发）----

def test_h05_same_bar_after_restart_not_reordered(tmp_path):
    """复现：last_decision_id 落了盘但没参与去重（adapter._seen 只在内存）
    ——同 bar 决策在重启后再次投递时，期间世界已变（外部又开了仓），旧实现
    按过期决策继续下单（重发一笔平仓）；修复后同 bar 重投递一律 duplicate
    （零新增请求），下个 bar 的新决策照常执行。"""
    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    d1 = mk_decision(signal=1, volume_lots=0.02,
                     decision_id="XAUUSD|M15|2026-10-20T18:00:00")
    assert ex.poll(d1, NOW).action == "open"
    d2 = mk_decision(signal=0, volume_lots=None,
                     decision_id="XAUUSD|M15|2026-10-20T18:15:00")
    assert ex.poll(d2, NOW.replace(minute=15)).action == "close"
    n_closes = len(b.close_calls)

    # 重启窗口期间外部又开了仓（本 magic 的净多 0.03）
    b.seed_position(side="buy", volume=0.03, magic=MAGIC)

    ex2 = _mk_executor(b, tmp_path)            # 重启：同 bar 决策再次投递
    rr = ex2.poll(d2, NOW.replace(minute=16))
    assert rr.action == "duplicate"            # 旧：close（按过期决策重发平仓）
    assert len(b.close_calls) == n_closes      # 零新增请求
    assert len(b.market_orders) == 1

    # 新 bar 的决策不受去重影响：signal=0 照常平掉外部仓
    d3 = mk_decision(signal=0, volume_lots=None,
                     decision_id="XAUUSD|M15|2026-10-20T18:30:00")
    rr2 = ex2.poll(d3, NOW.replace(minute=30))
    assert rr2.action == "close"
    assert b.positions() == []


# ---- H-05-7：出场解释绑定本次事件（非任意历史 SL deal）----

def test_h05_exit_explanation_bound_to_current_event(tmp_path):
    """复现：0.16 仓先被 SL 部分减到 0.08（历史 deal，量变已在先前轮次
    观察），余仓再被人工平掉 → 旧实现拿任意历史 SL deal 解释整个仓位消失
    （改写意图 + 重开 0.16）；修复后解释绑定本次事件（成交时间 ≥ 最后已知
    时刻、累计量 = 最后已知量、全部 reason ∈ SL/TP/SO），人工腿 CLIENT
    未解释 → 未决出场 + 停开仓。"""
    from mt5gw.models import DEAL_REASON_CLIENT, DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.16,
                               decision_id="XAUUSD|M15|d1"), NOW).action == "open"
    ticket = b.positions()[0].ticket
    # 部分 SL：0.16 → 0.08（ticket 仍在；下轮观察到量变，不是出场事件）
    b.reduce_external(ticket, 0.08, profit=-10.0, comment="[sl]",
                      reason=DEAL_REASON_SL,
                      time=datetime(2026, 10, 20, 18, 5))
    assert ex.poll(mk_decision(signal=1, volume_lots=0.16,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "hold"
    assert b.positions()[0].volume == pytest.approx(0.08)   # 量变已入账

    # 人工平掉残余 0.08（本次出场事件）
    b.close_external(ticket, profit=1.0, comment="manual",
                     reason=DEAL_REASON_CLIENT,
                     time=datetime(2026, 10, 20, 18, 20))
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.16,
                             decision_id="XAUUSD|M15|d3"),
                 NOW.replace(minute=30))
    assert rr.action == "blocked_external_exit"     # 旧：open（重开 0.16）
    assert len(b.market_orders) == 1                # 没有重开
    state = json.loads((tmp_path / f"state-{MAGIC}.json")
                       .read_text(encoding="utf-8"))
    assert str(ticket) in state["pending_external_exits"]   # 本次事件未解释
    # deals 证据流不得出现把人工出场记成保护性出场的行（H2-02/R4 起部分
    # 减量的 SL 解释合法入行——此处唯一 external_exit SL 行是 18:05 的部分
    # SL 0.08；人工 CLIENT 成交（18:20 全平）绝不出现）
    deals_path = tmp_path / f"deals-{MAGIC}.jsonl"
    assert deals_path.exists()
    lines = [json.loads(ln) for ln
             in deals_path.read_text(encoding="utf-8").splitlines() if ln]
    sl_lines = [r for r in lines
                if r["event"] == "external_exit" and r["reason"] == "SL"]
    assert len(sl_lines) == 1
    assert sl_lines[0]["volume"] == pytest.approx(0.08)  # 部分 SL（已消费）
    manual_ticket = b._deals[-1].ticket
    assert not any(r["event"] == "external_exit"
                   and r["ticket"] == manual_ticket for r in lines)

    # 人工确认（绑定协议）后恢复；解释行不再产出
    _write_ack_external_intent(tmp_path, tickets=(ticket,), net=0.0,
                               positions=[], now=NOW.replace(minute=40))
    rr2 = ex.poll(mk_decision(signal=1, volume_lots=0.16,
                              decision_id="XAUUSD|M15|d4"),
                  NOW.replace(minute=41))
    assert rr2.action == "open"


# ---- F 阶段停修：decision_id → MT5 安全 comment（端到端） ----
# 首战事故：开仓 comment=decision_id（XAUUSD|M15|2026-10-28T06:30:00）被 MT5
# 拒单（last_error=(-2, 'Invalid "comment" argument')）。开仓 comment 改为
# 压缩安全形态（白名单 [A-Za-z0-9 _-.]、≤31）；完整 decision_id 恒在本地
# 留痕（expected-<magic>.json 的 cause 与 orders-<magic>.jsonl），注释仅
# 辅助定位，可从日志还原原 decision_id。

_SAFE_COMMENT = re.compile(r"[A-Za-z0-9 _\-.]+")


def test_open_comment_safe_and_decision_id_recoverable_from_logs(tmp_path):
    """④ 端到端（FakeBroker）：comment 安全落到订单请求（压缩形态）；
    完整 decision_id 可从本地日志还原。"""
    b = FakeBroker()
    d = mk_decision(decision_id="XAUUSD|M15|2026-10-20T18:00:00",
                    signal=-1, volume_lots=0.01)
    rr = _mk_executor(b, tmp_path).poll(d, NOW)
    assert rr.action == "open"
    sent = b.market_orders[0]
    assert _SAFE_COMMENT.fullmatch(sent["comment"])    # 仅白名单字符
    assert len(sent["comment"]) <= 31
    assert sent["comment"] == "XAUUSD_M15_1020_1800"   # 压缩：月日_时分
    # 完整 decision_id 本地留痕（意图 cause + 下单日志行），可还原
    assert read_intent(tmp_path)["cause"] == d.decision_id
    with open(tmp_path / f"orders-{MAGIC}.jsonl", encoding="utf-8") as f:
        assert json.loads(f.readline())["decision_id"] == d.decision_id


# ================================================================ H2-01（阶段 1）
# docs/reviews/2026-10-30-global-code-audit-2.md H2-01 + 计划
# docs/superpowers/plans/2026-11-01-hline-foundation.md 1A/1B/1D：
# execution journal（发送前 pending → sent → confirmed/rejected/unknown）+
# 同 decision_id 幂等 + 重启恢复核对券商。故障注入手法：先把 journal/券商/
# 意图三者停在"崩溃瞬间"的精确状态（手写 journal 行=崩溃前最后一笔落盘），
# 再用新执行器模拟重启，断言恢复裁决与重投递行为；运行时路径（order_send
# 回报丢失）用钩子注入。

JOURNAL_FMT = "execution-journal-{magic}.jsonl"
SEND_TS = datetime(2026, 10, 20, 17, 59, 58)        # journal pending 行 ts
RESTART_LATE = datetime(2026, 10, 20, 18, 10, 8)    # 发送后 10 分钟（窗口已覆盖）


def _journal_path(tmp_path):
    return tmp_path / JOURNAL_FMT.format(magic=MAGIC)


def _journal_records(tmp_path) -> list:
    p = _journal_path(tmp_path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln
            in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _journal_seed_pending(tmp_path, decision_id, *, ts=SEND_TS,
                          action="open", volume=0.02, side="buy") -> None:
    """手写一条 pending 行（=发送前 journal 已落盘、其后崩溃）。"""
    append_jsonl(str(_journal_path(tmp_path)), {
        "ts": iso_utc(ts), "decision_id": decision_id,
        "request_uuid": "test-uuid", "action": action, "symbol": "XAUUSD",
        "side": side, "volume": volume, "status": "pending"})


def _journal_seed_line(tmp_path, record: dict) -> None:
    append_jsonl(str(_journal_path(tmp_path)), record)


def _seed_pending_intent(tmp_path, decision_id, *, volume=0.02,
                         ts=SEND_TS) -> None:
    """崩溃瞬间的意图文件：下单前写的 pending(目标)。"""
    write_intent(str(tmp_path / f"expected-{MAGIC}.json"),
                 Intent("XAUUSD", MAGIC, "buy", volume, STATUS_PENDING,
                        decision_id, ts))


def _alerts_text(tmp_path) -> str:
    p = tmp_path / "alerts.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _finals(tmp_path, status: str) -> list:
    return [r for r in _journal_records(tmp_path) if r.get("status") == status]


def test_h201_1_crash_before_send_rejected_and_recovered(tmp_path):
    """注入 1：发送前 journal 已写、order_send 未调即崩溃 → 重启核对券商
    （无仓、无挂单、窗口覆盖且无成交）→ rejected + 恢复；重投被幂等跳过，
    新决策照常开仓。"""
    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=0.02)
    _journal_seed_pending(tmp_path, d.decision_id)
    _seed_pending_intent(tmp_path, d.decision_id)

    ex = _mk_executor(b, tmp_path)                       # 重启
    rr = ex.poll(d, RESTART_LATE)
    assert rr.action == "duplicate"                      # journal 幂等跳过
    assert "journal 幂等跳过" in rr.error
    assert b.positions() == []                           # 零新增请求
    rejected = _finals(tmp_path, "rejected")
    assert rejected and rejected[-1]["decision_id"] == d.decision_id
    it = read_intent(tmp_path)                           # 恢复 expected=实际
    assert it["status"] == "confirmed" and it["volume"] == 0.0

    # 恢复完成：新决策照常开仓（未被未决项长期封锁）
    b.error_on_market_order = None
    d2 = mk_decision(signal=1, volume_lots=0.01,
                     decision_id="XAUUSD|M15|2026-10-20T18:15:00")
    assert ex.poll(d2, RESTART_LATE.replace(minute=25)).action == "open"


def test_h201_2_crash_after_fill_before_sent_line_confirmed(tmp_path):
    """注入 2：order_send 已成交（券商有仓+成交）但 journal "sent" 行未写即
    崩溃 → 重启核对券商有成交 → confirmed + expected 恢复为实际持仓。"""
    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=0.02)
    _journal_seed_pending(tmp_path, d.decision_id)
    b.market_order("XAUUSD", "buy", 0.02, magic=MAGIC)   # 券商侧已成交
    _seed_pending_intent(tmp_path, d.decision_id)

    ex = _mk_executor(b, tmp_path)                       # 重启
    rr = ex.poll(d, RESTART_LATE)
    assert rr.action == "duplicate"                      # 不重开（旧实现会再开）
    assert len(b.market_orders) == 1
    confirmed = _finals(tmp_path, "confirmed")
    assert confirmed and confirmed[-1]["decision_id"] == d.decision_id
    it = read_intent(tmp_path)
    assert it["status"] == "confirmed"
    assert it["volume"] == pytest.approx(0.02)           # expected 恢复=实际


def test_h201_3_crash_after_sent_before_state_confirmed(tmp_path):
    """注入 3：order_send 成功 + journal "sent" 行已写、state 写前崩溃 →
    重启 → confirmed + 恢复。"""
    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=0.02)
    _journal_seed_pending(tmp_path, d.decision_id)
    b.market_order("XAUUSD", "buy", 0.02, magic=MAGIC)
    _journal_seed_line(tmp_path, {
        "ts": iso_utc(SEND_TS + timedelta(seconds=2)),
        "decision_id": d.decision_id, "status": "sent",
        "retcode": 10009, "order_ticket": b.positions()[0].ticket})
    _seed_pending_intent(tmp_path, d.decision_id)

    ex = _mk_executor(b, tmp_path)
    rr = ex.poll(d, RESTART_LATE)
    assert rr.action == "duplicate"
    assert len(b.market_orders) == 1
    assert _finals(tmp_path, "confirmed")
    assert read_intent(tmp_path)["volume"] == pytest.approx(0.02)


def test_h201_4_order_send_unknown_intent_pending_restart_confirmed(tmp_path):
    """注入 4：order_send 回报丢失（Unknown）→ R2：意图保持 pending（旧实现
    写 confirmed 洗白）+ journal 记 unknown；重启核对券商有仓 → confirmed。"""
    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    d = mk_decision(signal=1, volume_lots=0.02)
    orig = b.market_order

    def send_then_lose_ack(*a, **kw):
        orig(*a, **kw)                                   # 券商已成交
        raise OrderResultUnknownError("IPC 中断")

    b.market_order = send_then_lose_ack
    with pytest.raises(OrderResultUnknownError):
        ex.poll(d, NOW)
    # R2：未知异常不写 confirmed——意图保持 pending
    assert read_intent(tmp_path)["status"] == "pending"
    recs = _journal_records(tmp_path)
    assert recs[0]["status"] == "pending"                # 发送前登记
    assert recs[-1]["status"] == "unknown"

    ex2 = _mk_executor(b, tmp_path)                      # 重启：券商有仓
    rr = ex2.poll(d, datetime(2026, 10, 20, 18, 0, 30))
    assert rr.action == "duplicate"
    assert _finals(tmp_path, "confirmed")
    assert read_intent(tmp_path)["volume"] == pytest.approx(0.02)
    assert len(b.market_orders) == 1                     # 不重开


def test_h201_5_unknown_no_fill_blocked_until_history_window_covers(tmp_path):
    """注入 5：order_send 回报丢失且实际未成交 → 重启（历史窗口尚未覆盖发送
    时刻）→ 保持 unknown + 暂停开仓（不误判 rejected）；窗口覆盖后 → rejected
    + 恢复（R3）。"""
    from mt5live.executor import JOURNAL_HISTORY_COVER_MARGIN_S

    b = FakeBroker()
    b.error_on_market_order = OrderResultUnknownError("IPC 中断")
    ex = _mk_executor(b, tmp_path)
    d = mk_decision(signal=1, volume_lots=0.02)
    with pytest.raises(OrderResultUnknownError):
        ex.poll(d, NOW)
    assert read_intent(tmp_path)["status"] == "pending"

    # 重启（窗口未覆盖）：未决 → 挡开仓，绝不判 rejected
    b.error_on_market_order = None
    ex2 = _mk_executor(b, tmp_path)
    early = NOW + timedelta(seconds=max(5.0,
                                        JOURNAL_HISTORY_COVER_MARGIN_S / 6))
    rr = ex2.poll(d, early)
    assert rr.action == "blocked_execution_unknown"
    assert "execution_unknown" in rr.error
    assert b.positions() == []                           # 没有开仓
    assert "EXECUTION_UNKNOWN" in _alerts_text(tmp_path)
    assert not _finals(tmp_path, "rejected")             # R3：不误判

    # 窗口覆盖发送时刻（无成交、无仓）→ rejected + 恢复；重投被拒
    late = NOW + timedelta(seconds=JOURNAL_HISTORY_COVER_MARGIN_S * 2)
    rr2 = ex2.poll(d, late)
    assert rr2.action == "duplicate"
    assert _finals(tmp_path, "rejected")
    d2 = mk_decision(signal=1, volume_lots=0.01,
                     decision_id="XAUUSD|M15|2026-10-20T18:15:00")
    assert ex2.poll(d2, late.replace(minute=25)).action == "open"


def test_h201_pending_broker_order_keeps_execution_unknown(tmp_path):
    """P0 regression: an open broker pending order must prevent rejected
    classification even after the history coverage margin has elapsed."""
    from mt5live.executor import JOURNAL_HISTORY_COVER_MARGIN_S

    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=0.02)
    _journal_seed_pending(tmp_path, d.decision_id)
    order = b.seed_pending(magic=MAGIC, volume=0.02)

    ex = _mk_executor(b, tmp_path)  # restart with unresolved broker order
    late = NOW + timedelta(seconds=JOURNAL_HISTORY_COVER_MARGIN_S * 2)
    rr = ex.poll(d, late)
    assert rr.action == "blocked_execution_unknown"
    assert not _finals(tmp_path, "rejected")
    assert order in b.pending_orders("XAUUSD")

    # Once the broker order is no longer pending, a complete negative
    # reconciliation may classify it as rejected.
    b.cancel_order(order.ticket)
    rr2 = ex.poll(d, late + timedelta(seconds=1))
    assert rr2.action == "duplicate"
    assert _finals(tmp_path, "rejected")


def test_h201_6_r3_delayed_broker_query_not_misjudged_rejected(tmp_path):
    """注入 6（R3）：重启时券商核对查询超时/延迟 → 不得误判 rejected（重复
    开仓风险），保持 unknown；下轮重查确认 → confirmed。"""
    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=0.02)
    _journal_seed_pending(tmp_path, d.decision_id)
    b.market_order("XAUUSD", "buy", 0.02, magic=MAGIC)   # 券商有仓
    _seed_pending_intent(tmp_path, d.decision_id)

    calls = {"pos": 0}
    orig_positions = b.positions

    def flaky_positions(symbol=None):
        calls["pos"] += 1
        if calls["pos"] == 1:          # 重启核对的第一次持仓查询超时（延迟可见）
            raise RuntimeError("positions 查询超时（延迟可见）")
        return orig_positions(symbol)

    b.positions = flaky_positions
    ex = _mk_executor(b, tmp_path)
    rr = ex.poll(d, RESTART_LATE)                        # 第 1 轮：查询失败
    assert rr.action in ("hold", "duplicate")            # 净额恰达标，零新增请求
    assert not _finals(tmp_path, "rejected")             # 核心断言：不误判
    assert "EXECUTION_UNKNOWN" in _alerts_text(tmp_path)
    assert b.positions()                                 # 仓位原封未动

    rr2 = ex.poll(d, RESTART_LATE.replace(minute=12))    # 第 2 轮：重查确认
    assert rr2.action == "duplicate"
    assert _finals(tmp_path, "confirmed")
    assert read_intent(tmp_path)["volume"] == pytest.approx(0.02)


def test_h201_7_position_sl_closed_replay_not_reordered(tmp_path):
    """注入 7（审计探针原场景）：成交后 state 写前崩溃 + 重启前仓位已被 SL
    平掉 → 重放同 decision_id 不得再开 1 笔（journal 幂等跳过）。"""
    from mt5gw.models import DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    d = mk_decision(signal=1, volume_lots=0.02)
    assert ex.poll(d, NOW).action == "open"
    ticket = b.positions()[0].ticket
    # 崩溃注入：state 写前进程死亡（状态文件丢失，last_decision_id 不在场）
    (tmp_path / f"state-{MAGIC}.json").unlink()
    # 重启前仓位已被 SL 平掉
    b.close_external(ticket, profit=-30.0, comment="[sl]",
                     reason=DEAL_REASON_SL)

    ex2 = _mk_executor(b, tmp_path)                      # 重启 + 重放同决策
    rr = ex2.poll(d, NOW.replace(minute=5))
    assert rr.action == "duplicate"                      # 旧实现：open 再开 1 笔
    assert "journal 幂等跳过" in rr.error
    assert b.positions() == [] and len(b.market_orders) == 1


def test_h201_8_journal_torn_tail_no_crash_blocks_opening(tmp_path):
    """注入 8（R5）：journal 尾部撕裂行（写一半崩溃）→ 读取不 crash、按
    unknown 处理（挡开仓）；平仓腿不受限。"""
    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=0.02)
    _journal_seed_pending(tmp_path, d.decision_id)
    with open(_journal_path(tmp_path), "a", encoding="utf-8") as f:
        f.write('{"ts": "2026-10-20T18:00:00+00:0')     # 撕裂：无换行+非完整 JSON

    ex = _mk_executor(b, tmp_path)                       # 不得抛
    rr = ex.poll(d, NOW)
    assert rr.action == "blocked_execution_unknown"      # 撕裂行=未知
    assert b.market_orders == [] and b.positions() == []  # 没有开仓
    assert "EXECUTION_UNKNOWN" in _alerts_text(tmp_path)
    # 平仓照常（防线只挡开仓腿）：期间出现的持仓，信号归零照常平掉
    b.seed_position(side="buy", volume=0.02, magic=MAGIC)
    d_close = mk_decision(signal=0, volume_lots=None,
                          decision_id="XAUUSD|M15|2026-10-20T18:15:00")
    assert ex.poll(d_close, NOW.replace(minute=15)).action == "close"
    assert b.positions() == []


def test_h201_9_journal_rejected_decision_replay_refused(tmp_path):
    """注入 9：同 decision_id journal 已有 rejected 终态 → 重投递被拒（语义
    变更：被拒决策不再可重投，重发须先过对账/仲裁）。"""
    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=0.02)
    _journal_seed_pending(tmp_path, d.decision_id)
    _journal_seed_line(tmp_path, {
        "ts": iso_utc(SEND_TS + timedelta(seconds=5)),
        "decision_id": d.decision_id, "status": "rejected",
        "detail": "arbitrated: 人工核对订单确未成交"})

    ex = _mk_executor(b, tmp_path)
    rr = ex.poll(d, NOW)
    assert rr.action == "duplicate"
    assert "拒绝后不可重投" in rr.error
    assert b.market_orders == []


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _ToolGW:
    """仲裁工具的网关替身（只读持仓）。"""

    def __init__(self, broker):
        self.account = broker
        self.market = broker
        self.trading = broker

    def connect(self):
        pass

    def shutdown(self):
        pass


def test_h201_arbitration_tool_resolves_permanent_unknown(tmp_path,
                                                          monkeypatch):
    """1D：永久 unknown（券商核对始终不可用）→ scripts/live_ack_execution.py
    仲裁 → journal 终态落盘 + 解除开仓暂停；重投被拒而非被挡。"""
    b = FakeBroker()
    d = mk_decision(signal=1, volume_lots=0.02)
    # 运行时回报丢失 + 券商实际未成交 → journal unknown
    b.error_on_market_order = OrderResultUnknownError("IPC 中断")
    ex = _mk_executor(b, tmp_path)
    with pytest.raises(OrderResultUnknownError):
        ex.poll(d, NOW)
    b.error_on_market_order = None

    # 券商核对持续不可用（查询一直超时）→ 永久 unknown、开仓被挡
    def boom_deals(since, to):
        raise RuntimeError("history_deals 持续不可用")

    b.deals = boom_deals
    assert ex.poll(d, NOW + timedelta(seconds=10)).action == \
        "blocked_execution_unknown"

    tool = _load_script("live_ack_execution")
    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file",
                        staticmethod(lambda p: _ToolGW(b)))
    # CI 根因修复：直接 patch 工具的 _open_gateway（绕过 from_config_file
    # 的 except fallback——CI 上 fallback 创建真 MT5Gateway 连接失败）
    monkeypatch.setattr(tool, "_open_gateway",
                        lambda gw_config: _ToolGW(b))
    import io, contextlib
    _out = io.StringIO()
    with contextlib.redirect_stdout(_out):
        rc = tool.main(["--live-dir", str(tmp_path), "--magic", str(MAGIC),
                        "--decision-id", d.decision_id, "--status", "rejected",
                        "--reason", "人工核对：订单确未到达服务器",
                        "--symbol", "XAUUSD"])
    assert rc == 0, f"tool rc={rc}; output: {_out.getvalue()}"  # CI 诊断：带工具输出
    rejected = _finals(tmp_path, "rejected")
    assert rejected and rejected[-1]["detail"].startswith("arbitrated")

    # 解除开仓暂停：重投被"拒"（journal 终态幂等），不再被未决挡
    rr = ex.poll(d, NOW.replace(minute=5))
    assert rr.action == "duplicate"
    assert "拒绝后不可重投" in rr.error
    d2 = mk_decision(signal=1, volume_lots=0.01,
                     decision_id="XAUUSD|M15|2026-10-20T18:15:00")
    assert ex.poll(d2, NOW.replace(minute=16)).action == "open"


# ================================================================ H2-02（阶段 2）
# docs/reviews/2026-10-30-global-code-audit-2.md H2-02 + 计划
# 2026-11-01-hline-foundation.md 2A/2B/2C：consumed deal 水位 + 部分减量
# （R4）+ 事件区间窗（R4）+ consumed fail-closed（R5）+ 券商查询 fail-closed。
# 核心复现：旧部分 SL deal 从未被消费 → 迟到的人工平仓被旧 SL"解释"→
# pending 清空、防线解除（审计探针 live 场景）。

CONSUMED_FMT = "consumed-deals-{magic}.jsonl"


def _consumed_path(tmp_path):
    return tmp_path / CONSUMED_FMT.format(magic=MAGIC)


def _consumed_records(tmp_path) -> list:
    p = _consumed_path(tmp_path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln
            in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _state_json(tmp_path) -> dict:
    return json.loads((tmp_path / f"state-{MAGIC}.json")
                      .read_text(encoding="utf-8"))


def test_h202_1_old_partial_sl_plus_delayed_manual_close_pending_holds(tmp_path):
    """注入 1（审计探针原场景）：0.02 仓先部分 SL 0.01（已观察、已消费
    水位），剩余 0.01 被人工平掉且成交延迟可见 → 旧部分 SL 不得解释新
    消失（旧实现按"累计量够了"清空 pending 并重开）。"""
    from mt5gw.models import DEAL_REASON_CLIENT, DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action \
        == "open"
    ticket = b.positions()[0].ticket
    # 部分 SL 0.01（18:05）→ 下一轮观察到量变：解释 + 消费（R4）
    b.reduce_external(ticket, 0.01, profit=-5.0, comment="[sl]",
                      reason=DEAL_REASON_SL, time=datetime(2026, 10, 20, 18, 5))
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "hold"
    consumed = _consumed_records(tmp_path)
    assert len(consumed) == 1
    assert consumed[0]["volume"] == pytest.approx(0.01)
    assert consumed[0]["reason"] == DEAL_REASON_SL

    # 人工平剩余 0.01（18:20，CLIENT）——成交历史延迟可见（6 次查询后）
    b.close_external(ticket, profit=1.0, comment="manual",
                     reason=DEAL_REASON_CLIENT,
                     time=datetime(2026, 10, 20, 18, 20))
    manual = b._deals[-1]
    b._deal_visible_at[b._deals.index(manual)] = b._deal_polls + 6

    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d3"),
                 NOW.replace(minute=30))
    assert rr.action == "blocked_external_exit"    # 旧实现：open（重开 0.02）
    assert len(b.market_orders) == 1               # 没有重开
    assert str(ticket) in _state_json(tmp_path)["pending_external_exits"]
    assert read_intent(tmp_path)["volume"] == pytest.approx(0.01)  # 不洗白

    # 延迟成交可见后（reason=CLIENT）→ 仍未解释：pending 不解除、防线不放
    for i in range(7):
        ex.poll(mk_decision(signal=1, volume_lots=0.02,
                            decision_id=f"XAUUSD|M15|loop{i}"),
                NOW.replace(minute=31 + i))
    assert str(ticket) in _state_json(tmp_path)["pending_external_exits"]
    assert len(b.market_orders) == 1
    # 旧 SL deal 没有被重复消费（水位单调）
    assert len(_consumed_records(tmp_path)) == 1


def test_h202_2_multiple_partial_sl_each_explained_and_consumed(tmp_path):
    """注入 2：多个部分 SL 依次发生 → 各自解释+消费（水位逐笔累进，
    后一笔不冒充前一笔）。"""
    from mt5gw.models import DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.03,
                               decision_id="XAUUSD|M15|d1"), NOW).action \
        == "open"
    ticket = b.positions()[0].ticket
    for i, (minute, event_minute) in enumerate(((5, 15), (20, 30)), start=2):
        b.reduce_external(ticket, 0.01, profit=-5.0, comment="[sl]",
                          reason=DEAL_REASON_SL,
                          time=datetime(2026, 10, 20, 18, event_minute - 10))
        assert ex.poll(mk_decision(signal=1, volume_lots=0.03,
                                   decision_id=f"XAUUSD|M15|d{i}"),
                       NOW.replace(minute=event_minute)).action == "hold"
        assert len(_consumed_records(tmp_path)) == i - 1    # 逐笔累进
    assert _state_json(tmp_path)["pending_external_exits"] == {}

    # 最后一笔 SL 全平 → 整仓消失同样解释 + 消费（immediate 重入场是
    # 解释成功后的正常行为——不挡）
    b.close_external(ticket, profit=-5.0, comment="[sl]",
                     reason=DEAL_REASON_SL,
                     time=datetime(2026, 10, 20, 18, 35))
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.03,
                             decision_id="XAUUSD|M15|d4"),
                 NOW.replace(minute=45))
    assert rr.action in ("open", "blocked_reentry")         # 解释成功不挡
    assert _state_json(tmp_path)["pending_external_exits"] == {}
    consumed = _consumed_records(tmp_path)
    assert len(consumed) == 3
    assert len({c["ticket"] for c in consumed}) == 3        # 三笔各消费一次
    assert all(c["volume"] == pytest.approx(0.01) for c in consumed)
    deals_log = [json.loads(ln) for ln in
                 (tmp_path / f"deals-{MAGIC}.jsonl").read_text(
                     encoding="utf-8").splitlines() if ln]
    assert len([r for r in deals_log if r["event"] == "external_exit"]) == 3


def test_h202_3_repeat_old_deal_not_reconsumed(tmp_path):
    """注入 3：已消费的旧 SL deal 反复返回在成交历史 → 不得再次用于解释/
    消费；人工平仓（CLIENT，立即可见）→ 未解释 pending。"""
    from mt5gw.models import DEAL_REASON_CLIENT, DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action \
        == "open"
    ticket = b.positions()[0].ticket
    b.reduce_external(ticket, 0.01, profit=-5.0, comment="[sl]",
                      reason=DEAL_REASON_SL, time=datetime(2026, 10, 20, 18, 5))
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "hold"
    assert len(_consumed_records(tmp_path)) == 1

    # 人工平剩余 0.01（CLIENT 立即可见）：旧 SL 已消费 → 只剩 CLIENT 候选
    b.close_external(ticket, profit=1.0, comment="manual",
                     reason=DEAL_REASON_CLIENT,
                     time=datetime(2026, 10, 20, 18, 20))
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d3"),
                 NOW.replace(minute=30))
    assert rr.action == "blocked_external_exit"
    assert str(ticket) in _state_json(tmp_path)["pending_external_exits"]
    assert len(_consumed_records(tmp_path)) == 1            # 不重复消费
    deals_log = [json.loads(ln) for ln in
                 (tmp_path / f"deals-{MAGIC}.jsonl").read_text(
                     encoding="utf-8").splitlines() if ln]
    assert not any(r["event"] == "external_exit"
                   and r["ticket"] == b._deals[-1].ticket for r in deals_log)


def test_h202_4_out_of_order_history_and_ancient_deal_filtered(tmp_path):
    """注入 4：成交历史乱序返回（新 deal 排在旧 deal 之前）+ 更古老的
    历史 SL deal 落在事件区间外 → 区间筛选与时间排序仍正确圈定本次事件
    （古老 deal 不得冒充解释）。"""
    from mt5gw.models import DEAL_REASON_CLIENT, DEAL_REASON_SL

    class ReversedHistoryBroker(FakeBroker):
        def deals_for_position(self, position_id):
            return list(reversed(
                super().deals_for_position(position_id)))

    b = ReversedHistoryBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action \
        == "open"
    ticket = b.positions()[0].ticket
    # 更古老的历史 SL deal（开仓之前——永远在事件区间外）
    b._record_deal(entry="out", side="sell", volume=0.01, price=99.0,
                   profit=-1.0, comment="[sl]", magic=MAGIC,
                   position_id=ticket, reason=DEAL_REASON_SL,
                   time=datetime(2026, 10, 20, 17, 50))
    b.reduce_external(ticket, 0.01, profit=-5.0, comment="[sl]",
                      reason=DEAL_REASON_SL, time=datetime(2026, 10, 20, 18, 5))
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "hold"
    consumed = _consumed_records(tmp_path)
    assert len(consumed) == 1 and consumed[0]["volume"] == pytest.approx(0.01)

    # 人工平剩余（CLIENT 延迟可见）：古老 deal 区间外 + 部分 SL 已消费
    # → 无候选 → 未解释（旧实现：部分 SL"累计量够"解释掉新消失并重开）
    b.close_external(ticket, profit=1.0, comment="manual",
                     reason=DEAL_REASON_CLIENT,
                     time=datetime(2026, 10, 20, 18, 20))
    manual = b._deals[-1]
    b._deal_visible_at[b._deals.index(manual)] = b._deal_polls + 6
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d3"),
                 NOW.replace(minute=30))
    assert rr.action == "blocked_external_exit"
    assert len(b.market_orders) == 1
    assert str(ticket) in _state_json(tmp_path)["pending_external_exits"]
    assert len(_consumed_records(tmp_path)) == 1


def test_h202_5_restart_keeps_consumed_watermark(tmp_path):
    """注入 5：重启后 consumed 水位保留（磁盘持久）→ 旧 SL 不因重启被
    重复消费/冒充解释。"""
    from mt5gw.models import DEAL_REASON_CLIENT, DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action \
        == "open"
    ticket = b.positions()[0].ticket
    b.reduce_external(ticket, 0.01, profit=-5.0, comment="[sl]",
                      reason=DEAL_REASON_SL, time=datetime(2026, 10, 20, 18, 5))
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "hold"

    # 重启（新执行器，水位从 consumed-deals-<magic>.jsonl 恢复）
    b.close_external(ticket, profit=1.0, comment="manual",
                     reason=DEAL_REASON_CLIENT,
                     time=datetime(2026, 10, 20, 18, 20))
    ex2 = _mk_executor(b, tmp_path)
    rr = ex2.poll(mk_decision(signal=1, volume_lots=0.02,
                              decision_id="XAUUSD|M15|d3"),
                  NOW.replace(minute=30))
    assert rr.action == "blocked_external_exit"    # 旧实现：SL 解释 → open
    assert str(ticket) in _state_json(tmp_path)["pending_external_exits"]
    assert len(_consumed_records(tmp_path)) == 1   # 水位没有回退


def test_h202_6_consumed_file_corrupt_fail_closed(tmp_path):
    """注入 6（R5）：consumed 水位文件损坏 → fail-closed——一切解释停摆
    （新消失全部 pending、旧 pending 视为仍在）+ STATE_CORRUPT 告警。"""
    from mt5gw.models import DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action \
        == "open"
    ticket = b.positions()[0].ticket
    b.reduce_external(ticket, 0.01, profit=-5.0, comment="[sl]",
                      reason=DEAL_REASON_SL, time=datetime(2026, 10, 20, 18, 5))
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "hold"
    assert len(_consumed_records(tmp_path)) == 1

    # 损坏水位文件（写一半崩溃）→ 重启 → 解释停摆
    _consumed_path(tmp_path).write_text(
        '{"ticket": 900001, "vol', encoding="utf-8")
    b.close_external(ticket, profit=-5.0, comment="[sl]",
                     reason=DEAL_REASON_SL,
                     time=datetime(2026, 10, 20, 18, 20))
    ex2 = _mk_executor(b, tmp_path)
    rr = ex2.poll(mk_decision(signal=1, volume_lots=0.02,
                              decision_id="XAUUSD|M15|d3"),
                  NOW.replace(minute=30))
    assert rr.action == "blocked_external_exit"    # 保护性出场也不自动解释
    assert str(ticket) in _state_json(tmp_path)["pending_external_exits"]
    assert "STATE_CORRUPT" in _alerts_text(tmp_path)


def test_h202_7_broker_query_timeout_fail_closed(tmp_path):
    """注入 7（2C）：券商持仓查询失败/超时 → 本轮不开新仓、不清空
    pending、写告警（blocked_broker_unavailable），不 crash。"""
    from mt5gw.models import DEAL_REASON_CLIENT

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action \
        == "open"
    ticket = b.positions()[0].ticket
    b.close_external(ticket, profit=1.0, comment="manual",
                     reason=DEAL_REASON_CLIENT)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d2"),
                   NOW.replace(minute=15)).action == "blocked_external_exit"

    # 券商持仓查询超时 → fail-closed（旧实现：异常上抛崩掉运行器）
    orig_positions = b.positions

    def boom_positions(symbol=None):
        raise RuntimeError("positions 查询超时")

    b.positions = boom_positions
    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d3"),
                 NOW.replace(minute=30))
    assert rr.action == "blocked_broker_unavailable"
    assert "BROKER_UNAVAILABLE" in _alerts_text(tmp_path)
    assert len(b.market_orders) == 1               # 没有开新仓
    assert str(ticket) in _state_json(tmp_path)["pending_external_exits"]

    # 查询恢复 → pending 仍在（未被本轮清空）、防线照常
    b.positions = orig_positions
    rr2 = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                              decision_id="XAUUSD|M15|d4"),
                  NOW.replace(minute=45))
    assert rr2.action == "blocked_external_exit"
    assert len(b.market_orders) == 1


def test_h202_8_partial_reduction_explained_and_consumed(tmp_path):
    """注入 8（R4 核心行为）：同 ticket volume 0.02→0.01（部分 SL）→
    检测量变 → 解释新 deal + 记入 consumed 水位 + 不产生 pending；
    deals 证据流记 external_exit 行（部分出场也是保护性出场证据）。"""
    from mt5gw.models import DEAL_REASON_SL

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d1"), NOW).action \
        == "open"
    ticket = b.positions()[0].ticket
    b.reduce_external(ticket, 0.01, profit=-5.0, comment="[sl]",
                      reason=DEAL_REASON_SL, time=datetime(2026, 10, 20, 18, 5))

    rr = ex.poll(mk_decision(signal=1, volume_lots=0.02,
                             decision_id="XAUUSD|M15|d2"),
                 NOW.replace(minute=15))
    assert rr.action == "hold"                     # 仓位仍在（量变非出场）
    assert b.positions()[0].volume == pytest.approx(0.01)
    assert _state_json(tmp_path)["pending_external_exits"] == {}
    consumed = _consumed_records(tmp_path)
    assert len(consumed) == 1
    assert consumed[0]["volume"] == pytest.approx(0.01)
    assert consumed[0]["reason"] == DEAL_REASON_SL
    assert consumed[0]["time"].startswith("2026-10-20T18:05")
    deals_log = [json.loads(ln) for ln in
                 (tmp_path / f"deals-{MAGIC}.jsonl").read_text(
                     encoding="utf-8").splitlines() if ln]
    ext = [r for r in deals_log if r["event"] == "external_exit"]
    assert len(ext) == 1 and ext[0]["reason"] == "SL"
    assert ext[0]["volume"] == pytest.approx(0.01)
    assert read_intent(tmp_path)["volume"] == pytest.approx(0.01)  # 意图=实际

    # 下一轮同信号：hold（不因量变重开/加仓）
    assert ex.poll(mk_decision(signal=1, volume_lots=0.02,
                               decision_id="XAUUSD|M15|d3"),
                   NOW.replace(minute=30)).action == "hold"
    assert len(b.market_orders) == 1