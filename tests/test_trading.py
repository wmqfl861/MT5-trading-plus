from collections import namedtuple

import pytest

from mt5gw.account import AccountService
from mt5gw.config import GatewayConfig, LiveTradingBlockedError
from mt5gw.connection import MT5Connection
from mt5gw.errors import RETCode, TradeRejectedError
from mt5gw.symbols import SymbolCatalog
from mt5gw.trading import TradingService, clamp_volume, pick_filling

from tests.fakes import FakeMT5, ok_send

AccRow = namedtuple(
    "AccRow",
    "login server company currency leverage balance equity margin margin_free margin_mode trade_mode",
)
SymRow = namedtuple(
    "SymRow",
    "name description digits point spread trade_mode currency_profit currency_margin "
    "contract_size volume_min volume_max volume_step filling_mode visible",
)
PosRow = namedtuple(
    "PosRow", "ticket symbol type volume price_open sl tp profit time comment magic"
)

ACC = AccRow(1, "s", "c", "USD", 100, 10000, 10000, 0, 10000, 2, 0)


def sym(filling=1, v_min=0.01, v_max=100.0, step=0.01):
    return SymRow(
        name="EURUSD", description="", digits=5, point=0.00001, spread=12,
        trade_mode=4, currency_profit="USD", currency_margin="USD",
        contract_size=100000.0, volume_min=v_min, volume_max=v_max,
        volume_step=step, filling_mode=filling, visible=True,
    )


LONG = PosRow(9001, "EURUSD", 0, 0.5, 1.1000, 1.09, 1.12, 12.5, 1698000000, "", 20261022)


def make(trade_mode=0, filling=1, allow_live=False, **sym_kw):
    fake = FakeMT5()
    conn = MT5Connection(mt5_module=fake)
    config = GatewayConfig(allow_live_trading=allow_live)
    fake.set_behavior("account_info", value=ACC._replace(trade_mode=trade_mode))
    fake.set_behavior("symbol_info", value=sym(filling=filling, **sym_kw))
    fake.set_behavior("symbol_select", value=True)
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
    assert pick_filling(1) == FakeMT5.ORDER_FILLING_FOK      # 仅 FOK
    assert pick_filling(2) == FakeMT5.ORDER_FILLING_IOC      # 仅 IOC
    assert pick_filling(3) == FakeMT5.ORDER_FILLING_FOK      # 两者都有 → FOK 优先
    assert pick_filling(0) == FakeMT5.ORDER_FILLING_RETURN   # 都不支持 → RETURN


def test_clamp_volume_rounds_down_to_step_and_bounds():
    assert clamp_volume(0.015, 0.01, 100.0, 0.01) == 0.01
    assert clamp_volume(150.0, 0.01, 100.0, 0.01) == 100.0
    with pytest.raises(ValueError):
        clamp_volume(0.005, 0.01, 100.0, 0.01)  # 取整后低于最小量
    with pytest.raises(ValueError):
        clamp_volume(0.0, 0.01, 100.0, 0.01)
    with pytest.raises(ValueError):
        clamp_volume(-1.0, 0.01, 100.0, 0.01)


# ---- 保险丝 ----

def test_fuse_blocks_before_order_send_on_real_account():
    tr, fake = make(trade_mode=2)  # 实盘
    capture_request(fake)
    with pytest.raises(LiveTradingBlockedError):
        tr.market_order("EURUSD", "buy", 0.01)
    assert fake.calls("order_send") == 0  # 请求从未触达 order_send


def test_fuse_blocks_close_and_modify_too():
    tr, fake = make(trade_mode=2)
    fake.set_behavior("positions_get", value=(LONG,))
    capture_request(fake)
    with pytest.raises(LiveTradingBlockedError):
        tr.close_position(9001)
    with pytest.raises(LiveTradingBlockedError):
        tr.modify(9001, sl=1.08)
    with pytest.raises(LiveTradingBlockedError):
        tr.pending_order("EURUSD", "buy", "limit", 0.01, 1.0900)
    assert fake.calls("order_send") == 0


def test_fuse_allows_real_when_unlocked():
    tr, fake = make(trade_mode=2, allow_live=True)
    box = capture_request(fake)
    res = tr.market_order("EURUSD", "buy", 0.01)
    assert res.ok and fake.calls("order_send") == 1


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
    assert res.ok and res.retcode == RETCode.DONE
    assert res.deal_ticket == 777 and res.order_ticket == 888 and res.deal_price == 1.1010


def test_market_order_sell_type():
    tr, fake = make()
    box = capture_request(fake)
    tr.market_order("EURUSD", "sell", 0.01)
    assert box["req"]["type"] == FakeMT5.ORDER_TYPE_SELL


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


def test_order_send_none_raises_with_last_error():
    tr, fake = make()
    fake.last_error_value = (-6, "send failed")
    with pytest.raises(TradeRejectedError, match="send failed"):
        tr.market_order("EURUSD", "buy", 0.01)


# ---- 挂单 ----

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


def test_pending_order_sell_stop_type():
    tr, fake = make()
    box = capture_request(fake)
    tr.pending_order("EURUSD", "sell", "stop", 0.02, 1.0900)
    assert box["req"]["type"] == FakeMT5.ORDER_TYPE_SELL_STOP


# ---- 平仓 / 改单 ----

def test_close_position_full_uses_opposite_side_with_position_ticket():
    tr, fake = make()
    fake.set_behavior("positions_get", value=(LONG,))
    box = capture_request(fake)
    res = tr.close_position(9001)
    req = box["req"]
    assert req["action"] == FakeMT5.TRADE_ACTION_DEAL
    assert req["symbol"] == "EURUSD"
    assert req["type"] == FakeMT5.ORDER_TYPE_SELL  # 多头平仓 → 卖出
    assert req["position"] == 9001
    assert req["volume"] == 0.5  # 缺省全平
    assert res.ok


def test_partial_close_sends_clamped_volume():
    tr, fake = make()
    fake.set_behavior("positions_get", value=(LONG,))
    box = capture_request(fake)
    tr.close_position(9001, 0.037)
    assert box["req"]["volume"] == 0.03


def test_close_unknown_position_raises():
    tr, fake = make()
    fake.set_behavior("positions_get", value=())
    capture_request(fake)
    with pytest.raises(Exception, match="9001"):
        tr.close_position(9001)
    assert fake.calls("order_send") == 0


def test_modify_updates_sl_tp_price():
    tr, fake = make()
    box = capture_request(fake)
    res = tr.modify(8001, sl=1.08, tp=1.13, price=1.0950)
    req = box["req"]
    assert req["action"] == FakeMT5.TRADE_ACTION_MODIFY
    assert req["order"] == 8001
    assert req["sl"] == 1.08 and req["tp"] == 1.13 and req["price"] == 1.0950
    assert res.ok
