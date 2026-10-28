from collections import namedtuple
import logging
import re

import pytest

from mt5gw.account import AccountService
from mt5gw.config import GatewayConfig, LiveTradingBlockedError
from mt5gw.connection import MT5Connection
from mt5gw.errors import RETCode, DataUnavailableError, OrderResultUnknownError, TradeRejectedError
from mt5gw.symbols import SymbolCatalog
from mt5gw.trading import TradingService, clamp_volume, pick_filling

from tests.fakes import FakeMT5, ok_send

AccRow = namedtuple(
    "AccRow",
    "login server company currency leverage balance equity margin margin_free margin_mode trade_mode",
)
TickInfo = namedtuple("TickInfo", "time bid ask last volume")  # 包的 tick 无 symbol 字段
SymRow = namedtuple(
    "SymRow",
    "name description digits point spread trade_mode currency_profit currency_margin "
    "trade_contract_size volume_min volume_max volume_step filling_mode visible",
)
PosRow = namedtuple(
    "PosRow",
    "ticket symbol type volume price_open price_current sl tp swap profit time comment magic",
)
OrdRow = namedtuple(
    "OrdRow", "ticket symbol type volume price_open price_current sl tp time_setup comment"
)

ACC = AccRow(1, "s", "c", "USD", 100, 10000, 10000, 0, 10000, 2, 0)

# 多头：有 SL 也有 TP（改单"现值回填"测试依赖这两个现值）
LONG = PosRow(
    ticket=9001, symbol="EURUSD", type=0, volume=0.5, price_open=1.1000,
    price_current=1.1050, sl=1.0900, tp=1.1200, swap=-0.02, profit=12.5,
    time=1698000000, comment="", magic=20261022,
)
PEND = OrdRow(
    ticket=8001, symbol="EURUSD", type=2, volume=0.5, price_open=1.0900,
    price_current=1.1000, sl=1.0800, tp=1.1200, time_setup=1698000000, comment="bot",
)


def sym(filling=1, v_min=0.01, v_max=100.0, step=0.01):
    return SymRow(
        name="EURUSD", description="", digits=5, point=0.00001, spread=12,
        trade_mode=4, currency_profit="USD", currency_margin="USD",
        trade_contract_size=100000.0, volume_min=v_min, volume_max=v_max,
        volume_step=step, filling_mode=filling, visible=True,
    )


def make(trade_mode=0, filling=1, allow_live=False, expected_login=None, **sym_kw):
    fake = FakeMT5()
    conn = MT5Connection(mt5_module=fake)
    config = GatewayConfig(allow_live_trading=allow_live,
                           expected_login=expected_login)
    fake.set_behavior("account_info", value=ACC._replace(trade_mode=trade_mode))
    fake.set_behavior("symbol_info", value=sym(filling=filling, **sym_kw))
    fake.set_behavior("symbol_select", value=True)
    fake.set_behavior(
        "symbol_info_tick", value=TickInfo(1698000000, 1.10450, 1.10462, 1.10460, 7)
    )
    fake.set_behavior(
        "positions_get",
        value=lambda symbol=None, ticket=None: (LONG,) if ticket in (None, 9001) else (),
    )
    fake.set_behavior(
        "orders_get",
        value=lambda symbol=None, ticket=None: (PEND,) if ticket == 8001 else (),
    )
    svc = TradingService(conn, config, AccountService(conn, fake),
                         SymbolCatalog(conn, fake), fake)
    return svc, fake


def capture_request(fake, result=None):
    """配置 order_send：捕获请求字典并返回给定结果。"""
    box = {}

    def send(req):
        box["req"] = dict(req)
        return result if result is not None else ok_send()

    fake.set_behavior("order_send", value=send)
    return box


# ---- 纯函数 ----

def test_pick_filling_first_supported_mode():
    assert pick_filling(1) == FakeMT5.ORDER_FILLING_FOK
    assert pick_filling(2) == FakeMT5.ORDER_FILLING_IOC
    assert pick_filling(3) == FakeMT5.ORDER_FILLING_FOK
    assert pick_filling(0) == FakeMT5.ORDER_FILLING_RETURN


def test_clamp_volume_rounds_down_to_step():
    assert clamp_volume(0.015, 0.01, 100.0, 0.01) == 0.01
    with pytest.raises(ValueError):
        clamp_volume(0.005, 0.01, 100.0, 0.01)
    with pytest.raises(ValueError):
        clamp_volume(0.0, 0.01, 100.0, 0.01)
    with pytest.raises(ValueError):
        clamp_volume(-1.0, 0.01, 100.0, 0.01)
    with pytest.raises(ValueError):
        clamp_volume(float("nan"), 0.01, 100.0, 0.01)
    with pytest.raises(ValueError):
        clamp_volume(float("inf"), 0.01, 100.0, 0.01)
    with pytest.raises(ValueError):
        clamp_volume(0.05, 0.01, 100.0, 0.0)  # 步长非法


def test_clamp_rejects_over_max_instead_of_silent_shrink():
    with pytest.raises(ValueError, match="最大交易量"):
        clamp_volume(150.0, 0.01, 100.0, 0.01)  # 要 150 手不许静默下 100 手


# ---- 保险丝 ----

def test_fuse_blocks_every_trading_entry_on_real_account():
    tr, fake = make(trade_mode=2)
    capture_request(fake)
    with pytest.raises(LiveTradingBlockedError):
        tr.market_order("EURUSD", "buy", 0.01)
    with pytest.raises(LiveTradingBlockedError):
        tr.pending_order("EURUSD", "buy", "limit", 0.01, 1.0900)
    with pytest.raises(LiveTradingBlockedError):
        tr.close_position(9001)
    with pytest.raises(LiveTradingBlockedError):
        tr.modify(8001, sl=1.08)
    with pytest.raises(LiveTradingBlockedError):
        tr.modify_position(9001, sl=1.08)
    with pytest.raises(LiveTradingBlockedError):
        tr.cancel_order(8001)
    assert fake.calls("order_send") == 0  # 全部在触达 order_send 之前被拦


def test_fuse_fails_closed_when_account_unreadable():
    tr, fake = make()
    fake.set_behavior("account_info", value=None)  # 下单前读不到账户
    fake.last_error_value = (-6, "terminal not connected")
    capture_request(fake)
    from mt5gw.errors import DataUnavailableError
    with pytest.raises(DataUnavailableError, match="拒绝发送"):
        tr.market_order("EURUSD", "buy", 0.01)
    assert fake.calls("order_send") == 0


def test_fuse_blocks_contest_account_by_default():  # 白名单：只有模拟盘放行
    from mt5gw.config import ensure_trade_allowed
    with pytest.raises(LiveTradingBlockedError):
        ensure_trade_allowed(1, False)
    ensure_trade_allowed(0, False)
    ensure_trade_allowed(1, True)


def test_fuse_immune_to_truthy_string_unlock():  # I1：非严格布尔解锁一律无效
    from mt5gw.config import ensure_trade_allowed
    with pytest.raises(LiveTradingBlockedError):
        ensure_trade_allowed(2, "false")  # 字符串真值不解锁
    with pytest.raises(ValueError):
        GatewayConfig(allow_live_trading="false")  # 构造时当场拒绝
    import dataclasses
    with pytest.raises(ValueError):
        dataclasses.replace(GatewayConfig(), allow_live_trading=1)


def test_fuse_allows_real_when_unlocked_and_pinned():
    tr, fake = make(trade_mode=2, allow_live=True, expected_login=ACC.login)
    box = capture_request(fake)
    res = tr.market_order("EURUSD", "buy", 0.01)
    assert res.ok and fake.calls("order_send") == 1


def test_pinned_login_mismatch_rejects():  # 解锁后被切到别的实盘账号 → 拒单
    from mt5gw.config import GatewayConfig as C
    tr, fake = make(trade_mode=2, allow_live=True, expected_login=999999)
    capture_request(fake)
    with pytest.raises(LiveTradingBlockedError, match="expected_login"):
        tr.market_order("EURUSD", "buy", 0.01)
    assert fake.calls("order_send") == 0


# ---- 市价单 ----

def test_market_order_sends_correct_request():
    tr, fake = make(filling=1)
    box = capture_request(fake)
    res = tr.market_order("EURUSD", "buy", 0.02, sl=1.09, tp=1.12,
                          deviation=30, comment="hello")
    req = box["req"]
    assert req["action"] == FakeMT5.TRADE_ACTION_DEAL
    assert req["symbol"] == "EURUSD"
    assert req["type"] == FakeMT5.ORDER_TYPE_BUY
    assert req["volume"] == 0.02
    assert req["sl"] == 1.09 and req["tp"] == 1.12
    assert req["deviation"] == 30
    assert req["magic"] == 20261022
    assert req["comment"] == "hello"
    assert req["type_filling"] == FakeMT5.ORDER_FILLING_FOK  # bits=1：仅 FOK 可用
    assert req["price"] == 1.10462  # 买入带当前 ask（Exness 要求显式参考价）
    assert res.ok and res.filled and not res.partial
    assert res.deal_ticket == 777 and res.order_ticket == 888 and res.deal_price == 1.1010
    assert res.requested_volume == 0.02


def test_market_order_sell_uses_bid_price():
    tr, fake = make()
    box = capture_request(fake)
    tr.market_order("EURUSD", "sell", 0.01)
    assert box["req"]["type"] == FakeMT5.ORDER_TYPE_SELL
    assert box["req"]["price"] == 1.10450  # 卖出带当前 bid


def test_invalid_fill_uses_symbol_supported_mode():
    tr, fake = make(filling=2)  # 仅 IOC
    box = capture_request(fake)
    tr.market_order("EURUSD", "buy", 0.01)
    assert box["req"]["type_filling"] == FakeMT5.ORDER_FILLING_IOC

    tr2, fake2 = make(filling=0)  # 都不支持 → RETURN
    box2 = capture_request(fake2)
    tr2.market_order("EURUSD", "buy", 0.01)
    assert box2["req"]["type_filling"] == FakeMT5.ORDER_FILLING_RETURN


def test_volume_clamped_in_market_order():
    tr, fake = make()
    box = capture_request(fake)
    tr.market_order("EURUSD", "buy", 0.015)
    assert box["req"]["volume"] == 0.01


def test_over_max_volume_rejected_not_shrunk():
    tr, fake = make()
    capture_request(fake)
    with pytest.raises(TradeRejectedError, match="最大交易量"):
        tr.market_order("EURUSD", "buy", 150.0)
    assert fake.calls("order_send") == 0


def test_below_min_volume_raises_invalid_volume():
    tr, fake = make()
    capture_request(fake)
    with pytest.raises(TradeRejectedError) as ei:
        tr.market_order("EURUSD", "buy", 0.005)
    assert ei.value.retcode == RETCode.INVALID_VOLUME
    assert fake.calls("order_send") == 0


def test_rejected_retcode_raises_with_readable_message():
    tr, fake = make()
    capture_request(fake, result=ok_send()._replace(retcode=10018, comment="closed"))
    with pytest.raises(TradeRejectedError) as ei:
        tr.market_order("EURUSD", "buy", 0.01)
    assert ei.value.retcode == 10018
    assert "市场关闭" in str(ei.value)


# ---- 成交结果语义（I3）----

def test_done_partial_is_success_and_filled():
    tr, fake = make()
    capture_request(fake, result=ok_send()._replace(retcode=10010, volume=0.006))
    res = tr.market_order("EURUSD", "buy", 0.01)
    assert res.ok and res.filled and res.partial  # 部分成交：仓位已建立，绝不能当"被拒"
    assert res.volume == 0.006 and res.requested_volume == 0.01


def test_placed_is_ok_but_not_filled():
    tr, fake = make()
    capture_request(fake, result=ok_send()._replace(retcode=10008, order=999, deal=0, price=0.0))
    res = tr.pending_order("EURUSD", "buy", "limit", 0.01, 1.0900)
    assert res.ok and res.filled is False and res.partial is False
    assert res.deal_ticket is None and res.deal_price is None


def test_requote_retried_with_fresh_reference_price():
    # 真机发现：参考价与发送之间行情变动 → 10004 Requote。
    # 执行层应有界重试（每次重取参考价），这是传输鲁棒性而非策略逻辑。
    tr, fake = make()
    prices = [1.10462, 1.10470, 1.10480]  # 行情在走
    fake.set_behavior(
        "symbol_info_tick",
        value=lambda symbol=None: TickInfo(1698000000, prices[0] - 0.0001,
                                           prices[fake.state.get("i", 0)], 0, 7)
        if not fake.state.get("i") else TickInfo(1698000000, 0, prices[fake.state["i"]], 0, 7),
    )
    fake.state = {"i": 0}
    calls = {"n": 0}

    def send(req):
        calls["n"] += 1
        fake.state["i"] = min(calls["n"], len(prices) - 1)
        if calls["n"] < 3:
            return ok_send()._replace(retcode=10004, comment="Requote")
        return ok_send(price=req.get("price", 1.1))

    fake.set_behavior("order_send", value=send)
    res = tr.market_order("EURUSD", "buy", 0.01)
    assert res.ok and calls["n"] == 3  # 两次 Requote 后第三次成功


def test_requote_exhausts_retries_and_raises():
    tr, fake = make()
    fake.state = {}
    fake.set_behavior("order_send",
                      value=ok_send()._replace(retcode=10004, comment="Requote"))
    with pytest.raises(TradeRejectedError, match="重新报价"):
        tr.market_order("EURUSD", "buy", 0.01)
    assert fake.calls("order_send") == 3  # 有界：至多 3 次


def test_order_send_none_raises_unknown_result():
    tr, fake = make()
    fake.set_behavior("order_send", value=None)  # order_send 无返回
    with pytest.raises(OrderResultUnknownError, match="核对持仓"):
        tr.market_order("EURUSD", "buy", 0.01)


# ---- 挂单 / 撤单 ----

def test_pending_order_sends_correct_request():
    tr, fake = make()
    box = capture_request(fake)
    res = tr.pending_order("EURUSD", "buy", "limit", 0.02, 1.0900, sl=1.08, tp=1.10)
    req = box["req"]
    assert req["action"] == FakeMT5.TRADE_ACTION_PENDING
    assert req["type"] == FakeMT5.ORDER_TYPE_BUY_LIMIT
    assert req["price"] == 1.0900
    assert req["volume"] == 0.02
    assert req["sl"] == 1.08 and req["tp"] == 1.10
    assert res.ok


def test_cancel_order_sends_remove_action():
    tr, fake = make()
    box = capture_request(fake)
    res = tr.cancel_order(8001)
    req = box["req"]
    assert req["action"] == FakeMT5.TRADE_ACTION_REMOVE
    assert req["order"] == 8001
    assert res.ok


def test_cancel_unknown_order_raises():
    tr, fake = make()
    capture_request(fake)
    with pytest.raises(DataUnavailableError, match="8002"):
        tr.cancel_order(8002)
    assert fake.calls("order_send") == 0


# ---- 改单：未传字段回填现值（C1）----

def test_modify_position_only_tp_keeps_current_sl():
    tr, fake = make()
    box = capture_request(fake)
    res = tr.modify_position(9001, tp=1.1300)
    req = box["req"]
    assert req["action"] == FakeMT5.TRADE_ACTION_SLTP
    assert req["position"] == 9001
    assert req["symbol"] == "EURUSD"
    assert req["sl"] == 1.0900  # 未传的 SL 保持现值，绝不发 0
    assert req["tp"] == 1.1300
    assert res.ok


def test_modify_position_only_sl_keeps_current_tp():
    tr, fake = make()
    box = capture_request(fake)
    tr.modify_position(9001, sl=1.0800)
    assert box["req"]["sl"] == 1.0800
    assert box["req"]["tp"] == 1.1200  # 未传的 TP 保持现值


def test_modify_position_requires_sl_or_tp():
    tr, fake = make()
    with pytest.raises(ValueError):
        tr.modify_position(9001)


def test_modify_position_unknown_ticket_raises():
    tr, fake = make()
    capture_request(fake)
    with pytest.raises(DataUnavailableError, match="9002"):
        tr.modify_position(9002, sl=1.08)
    assert fake.calls("order_send") == 0


def test_modify_pending_fills_current_values():
    tr, fake = make()
    box = capture_request(fake)
    res = tr.modify(8001, tp=1.1500)
    req = box["req"]
    assert req["action"] == FakeMT5.TRADE_ACTION_MODIFY
    assert req["order"] == 8001
    assert req["symbol"] == "EURUSD"
    assert req["price"] == 1.0900  # 未传 price 用现挂单价
    assert req["sl"] == 1.0800     # 未传 sl 保持现值
    assert req["tp"] == 1.1500
    assert req["type_time"] == 0 and req["stoplimit"] == 0.0  # 有效期/触发价也回填
    assert res.ok


# ---- 平仓 ----

def test_close_position_full_uses_opposite_side_with_position_ticket():
    tr, fake = make()
    box = capture_request(fake)
    res = tr.close_position(9001)
    req = box["req"]
    assert req["action"] == FakeMT5.TRADE_ACTION_DEAL
    assert req["symbol"] == "EURUSD"
    assert req["type"] == FakeMT5.ORDER_TYPE_SELL  # 多头平仓 → 卖出
    assert req["position"] == 9001
    assert req["volume"] == 0.5  # 缺省全平
    assert req["price"] == 1.10450  # 平多卖出 → bid
    assert res.ok


def test_partial_close_sends_clamped_volume():
    tr, fake = make()
    box = capture_request(fake)
    tr.close_position(9001, 0.037)
    assert box["req"]["volume"] == 0.03


def test_partial_close_over_remaining_rejected():
    tr, fake = make()
    capture_request(fake)
    with pytest.raises(TradeRejectedError) as ei:
        tr.close_position(9001, 0.7)
    assert ei.value.retcode == RETCode.INVALID_VOLUME
    assert fake.calls("order_send") == 0


def test_close_position_deviation_configurable():
    tr, fake = make()
    box = capture_request(fake)
    tr.close_position(9001, deviation=50)
    assert box["req"]["deviation"] == 50


def test_close_unknown_position_raises():
    tr, fake = make()
    fake.set_behavior("positions_get", value=lambda symbol=None, ticket=None: ())
    capture_request(fake)
    with pytest.raises(DataUnavailableError, match="9002"):
        tr.close_position(9002)
    assert fake.calls("order_send") == 0


# ---- 每策略 magic（P0）----

def test_magic_defaults_to_config_everywhere():
    # 缺省不传：市价 / 挂单 / 平仓三处一律用 config.magic
    tr, fake = make()
    box = capture_request(fake)
    tr.market_order("EURUSD", "buy", 0.01)
    assert box["req"]["magic"] == 20261022  # GatewayConfig.magic 缺省值
    tr.pending_order("EURUSD", "buy", "limit", 0.01, 1.0900)
    assert box["req"]["magic"] == 20261022
    tr.close_position(9001)
    assert box["req"]["magic"] == 20261022


def test_explicit_magic_overrides_config_everywhere():
    # 逐单覆盖：同一网关下多策略各自标识互不串号
    tr, fake = make()
    box = capture_request(fake)
    tr.market_order("EURUSD", "buy", 0.01, magic=777)
    assert box["req"]["magic"] == 777
    tr.pending_order("EURUSD", "buy", "limit", 0.01, 1.0900, magic=777)
    assert box["req"]["magic"] == 777
    tr.close_position(9001, magic=777)
    assert box["req"]["magic"] == 777


# ---- F 阶段停修：MT5 订单 comment 安全化 ----
# 2026-10-28 首战真实事故：comment="XAUUSD|M15|2026-10-28T06:30:00" 被拒，
# last_error=(-2, 'Invalid "comment" argument')（data/demo/runner-902601-
# 20261028-143730.log.err）。不猜黑名单：保守白名单 [A-Za-z0-9 _-.] + 31 截断。

_SAFE_COMMENT = re.compile(r"[A-Za-z0-9 _\-.]+")


def test_sanitize_comment_real_incident_shape():
    """① 真实失败形态（XAUUSD|M15|ISO 冒号时间戳，30 字符）→ ≤31 且仅白名单。"""
    from mt5gw.trading import sanitize_comment   # TDD 红→绿同形态（延迟导入）
    raw = "XAUUSD|M15|2026-10-28T06:30:00"       # 首战原始 comment
    safe = sanitize_comment(raw)
    assert len(safe) <= 31
    assert _SAFE_COMMENT.fullmatch(safe)
    assert "|" not in safe and ":" not in safe
    assert safe.startswith("XAUUSD_M15_")        # 非法字符替换为 '_'：信息保留


def test_sanitize_comment_35_chars_with_pipe_truncated():
    """①补：35 字符带 '|' → 截 31 且无 '|'。"""
    from mt5gw.trading import sanitize_comment
    raw = "EURUSD|" + "x" * 29                   # 35 字符、含 '|'
    safe = sanitize_comment(raw)
    assert len(safe) == 31
    assert _SAFE_COMMENT.fullmatch(safe)


def test_sanitize_comment_empty_and_none_untouched():
    """③ 空 / None 不受影响（空串是合法缺省，None 归一为空串）。"""
    from mt5gw.trading import sanitize_comment
    assert sanitize_comment("") == ""
    assert sanitize_comment(None) == ""


def test_market_order_sanitizes_comment_before_send_with_warning(caplog):
    """② 35 字符带 '|' 的 comment 发送前被安全化；告警记录 原文→安全文。"""
    from mt5gw.trading import sanitize_comment  # noqa: F401  红阶段锚点
    tr, fake = make()
    box = capture_request(fake)
    raw = "EURUSD|" + "x" * 29                   # 35 字符、含 '|'
    with caplog.at_level(logging.WARNING, logger="mt5gw.trading"):
        tr.market_order("EURUSD", "buy", 0.01, comment=raw)
    sent = box["req"]["comment"]
    assert len(sent) == 31 and "|" not in sent   # 发送前已安全化
    assert _SAFE_COMMENT.fullmatch(sent)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(raw in r.getMessage() and sent in r.getMessage()
               for r in warnings)                # 不静默：原文→安全文都有


def test_market_order_clean_comment_passes_through_silently(caplog):
    """③ 合法/缺省 comment 原样透传，不告警（含空串）。"""
    tr, fake = make()
    box = capture_request(fake)
    with caplog.at_level(logging.WARNING, logger="mt5gw.trading"):
        tr.market_order("EURUSD", "sell", 0.01)  # 缺省空 comment
        assert box["req"]["comment"] == ""
        tr.market_order("EURUSD", "buy", 0.01, comment="ok cmt-1.0_x")
    assert box["req"]["comment"] == "ok cmt-1.0_x"
    assert [r for r in caplog.records
            if r.levelno == logging.WARNING] == []
