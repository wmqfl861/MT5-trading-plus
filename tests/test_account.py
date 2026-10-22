from collections import namedtuple
from datetime import datetime

import pytest

from mt5gw.account import AccountService
from mt5gw.connection import MT5Connection
from mt5gw.models import AccountState, Deal, HistoryOrder, PendingOrder, Position

from tests.fakes import FakeMT5

AccRow = namedtuple(
    "AccRow",
    "login server company currency leverage balance equity margin margin_free margin_mode trade_mode",
)
PosRow = namedtuple(
    "PosRow", "ticket symbol type volume price_open sl tp profit time comment magic"
)
OrdRow = namedtuple(
    "OrdRow", "ticket symbol type volume price_open price_current sl tp time_setup comment"
)
DealRow = namedtuple(
    "DealRow", "ticket order symbol type volume price profit commission swap fee time comment"
)
HistRow = namedtuple(
    "HistRow", "ticket symbol type volume price_open state time_setup time_done comment"
)

DEMO_ACC = AccRow(
    login=123456, server="MetaQuotes-Demo", company="MetaQuotes Software Corp.",
    currency="USD", leverage=100, balance=10000.0, equity=10012.5,
    margin=100.0, margin_free=9912.5, margin_mode=2, trade_mode=0,
)
LONG = PosRow(
    ticket=9001, symbol="EURUSD", type=0, volume=0.5, price_open=1.1000,
    sl=1.0900, tp=1.1200, profit=12.5, time=1698000000, comment="smoke", magic=20261022,
)
SHORT = PosRow(
    ticket=9002, symbol="XAUUSD", type=1, volume=0.1, price_open=1950.0,
    sl=1970.0, tp=1900.0, profit=-3.0, time=1698000060, comment="", magic=0,
)


def make():
    fake = FakeMT5()
    conn = MT5Connection(mt5_module=fake)
    return AccountService(conn, fake), fake


def test_state_maps_all_fields_with_trade_mode():
    acct, fake = make()
    fake.set_behavior("account_info", value=DEMO_ACC)
    st = acct.state()
    assert isinstance(st, AccountState)
    assert st.login == 123456 and st.server == "MetaQuotes-Demo"
    assert st.company == "MetaQuotes Software Corp." and st.currency == "USD"
    assert st.leverage == 100
    assert st.balance == 10000.0 and st.equity == 10012.5
    assert st.margin == 100.0 and st.margin_free == 9912.5
    assert st.margin_mode == 2
    assert st.trade_mode == 0  # 保险丝依赖此值


def test_state_none_raises_not_connected_style_error():
    acct, fake = make()
    fake.last_error_value = (-6, "terminal not connected")
    with pytest.raises(Exception, match="terminal not connected"):
        acct.state()


def test_positions_map_type_to_side():
    acct, fake = make()
    fake.set_behavior("positions_get", value=(LONG, SHORT))
    poss = acct.positions()
    assert [p.side for p in poss] == ["buy", "sell"]
    p = poss[0]
    assert p.ticket == 9001 and p.symbol == "EURUSD" and p.volume == 0.5
    assert p.price_open == 1.1000 and p.sl == 1.0900 and p.tp == 1.1200
    assert p.profit == 12.5 and p.magic == 20261022 and p.comment == "smoke"
    assert p.time == datetime(2023, 10, 22, 18, 40, 0)


def test_positions_none_returns_empty():  # 包的 None 歧义：按空集处理（README 约定）
    acct, fake = make()
    fake.set_behavior("positions_get", value=None)
    assert acct.positions() == []


def test_positions_symbol_filter_passed_through():
    acct, fake = make()
    fake.set_behavior("positions_get", value=lambda symbol=None: (LONG,) if symbol == "EURUSD" else ())
    assert [p.symbol for p in acct.positions("EURUSD")] == ["EURUSD"]
    assert acct.positions("GBPUSD") == []


def test_pending_orders_map_kind():
    acct, fake = make()
    lim = OrdRow(8001, "EURUSD", 2, 0.5, 1.0900, 1.1000, 0.0, 1.1200, 1698000000, "bot")
    stp = OrdRow(8002, "XAUUSD", 5, 0.1, 1980.0, 1950.0, 0.0, 0.0, 1698000060, "")
    fake.set_behavior("orders_get", value=(lim, stp))
    orders = acct.pending_orders()
    assert [o.kind for o in orders] == ["buy_limit", "sell_stop"]
    assert orders[0].ticket == 8001 and orders[0].volume == 0.5
    assert orders[0].price_open == 1.0900 and orders[0].price_current == 1.1000
    assert orders[1].time_setup == datetime(2023, 10, 22, 18, 41, 0)


def test_pending_orders_none_returns_empty():
    acct, fake = make()
    fake.set_behavior("orders_get", value=None)
    assert acct.pending_orders() == []


def test_deals_pass_epoch_window_and_map_fields():
    acct, fake = make()
    deal = DealRow(7001, 9001, "EURUSD", 0, 0.5, 1.1002, 12.5, 0.0, 0.0, -0.35, 1698000000, "smoke")

    def check(from_dt, to_dt, **kw):
        assert from_dt == 1697986800 and to_dt == 1698000000  # naive UTC → 整数秒
        return (deal,)

    fake.set_behavior("history_deals_get", value=check)
    deals = acct.deals(datetime(2023, 10, 22, 15, 0), datetime(2023, 10, 22, 18, 40))
    d = deals[0]
    assert isinstance(d, Deal)
    assert d.side == "buy" and d.volume == 0.5 and d.price == 1.1002
    assert d.commission == 0.0 and d.fee == -0.35 and d.comment == "smoke"


def test_order_history_maps_fields():
    acct, fake = make()
    h = HistRow(8001, "EURUSD", 2, 0.5, 1.0900, 3, 1698000000, 1698000100, "done")
    fake.set_behavior("history_orders_get", value=(h,))
    rows = acct.order_history(datetime(2023, 10, 22, 15, 0), datetime(2023, 10, 22, 19, 0))
    r = rows[0]
    assert isinstance(r, HistoryOrder)
    assert r.kind == "buy_limit" and r.state == 3
    assert r.time_done == datetime(2023, 10, 22, 18, 41, 40)
