from collections import namedtuple
from datetime import datetime

import pytest

from mt5gw.account import AccountService
from mt5gw.connection import MT5Connection
from mt5gw.errors import DataUnavailableError
from mt5gw.models import AccountState, Deal, HistoryOrder, PendingOrder, Position

from tests.fakes import FakeMT5

AccRow = namedtuple(
    "AccRow",
    "login server company currency leverage balance equity margin margin_free margin_mode trade_mode",
)
PosRow = namedtuple(
    "PosRow",
    "ticket symbol type volume price_open price_current sl tp swap profit time comment magic",
)
OrdRow = namedtuple(
    "OrdRow", "ticket symbol type volume_current price_open price_current sl tp "
             "time_setup comment magic type_time price_stoplimit time_expiration"
)
DealRow = namedtuple(
    "DealRow",
    "ticket order position_id symbol type entry volume price profit commission swap fee time comment magic",
)
HistRow = namedtuple(
    "HistRow", "ticket symbol type volume_initial price_open state time_setup time_done comment"
)

DEMO_ACC = AccRow(
    login=123456, server="MetaQuotes-Demo", company="MetaQuotes Software Corp.",
    currency="USD", leverage=100, balance=10000.0, equity=10012.5,
    margin=100.0, margin_free=9912.5, margin_mode=2, trade_mode=0,
)
LONG = PosRow(
    ticket=9001, symbol="EURUSD", type=0, volume=0.5, price_open=1.1000,
    price_current=1.1050, sl=1.0900, tp=1.1200, swap=-0.02, profit=12.5,
    time=1698000000, comment="smoke", magic=20261022,
)
SHORT = PosRow(
    ticket=9002, symbol="XAUUSD", type=1, volume=0.1, price_open=1950.0,
    price_current=1945.0, sl=1970.0, tp=1900.0, swap=0.0, profit=-3.0,
    time=1698000060, comment="", magic=0,
)
PEND = OrdRow(8001, "EURUSD", 2, 0.5, 1.0900, 1.1000, 1.0800, 1.1200,
              1698000000, "bot", 20261022, 0, 0.0, 0)


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


def test_state_none_raises_with_last_error():
    acct, fake = make()
    fake.last_error_value = (-6, "terminal not connected")
    with pytest.raises(DataUnavailableError, match="terminal not connected"):
        acct.state()


def test_positions_map_all_fields():
    acct, fake = make()
    fake.set_behavior("positions_get", value=lambda symbol=None, ticket=None: (LONG, SHORT))
    poss = acct.positions()
    assert [p.side for p in poss] == ["buy", "sell"]
    p = poss[0]
    assert p.ticket == 9001 and p.symbol == "EURUSD" and p.volume == 0.5
    assert p.price_open == 1.1000 and p.price_current == 1.1050
    assert p.sl == 1.0900 and p.tp == 1.1200 and p.swap == -0.02
    assert p.profit == 12.5 and p.magic == 20261022 and p.comment == "smoke"
    assert p.time == datetime(2023, 10, 22, 18, 40, 0)


def test_positions_none_with_success_is_empty():
    acct, fake = make()
    fake.set_behavior("positions_get", value=None)
    fake.last_error_value = (1, "Success")
    assert acct.positions() == []


def test_positions_none_with_error_raises():  # I5：查询失败绝不伪装成空仓
    acct, fake = make()
    fake.set_behavior("positions_get", value=None)
    fake.last_error_value = (-6, "IPC failed")
    with pytest.raises(DataUnavailableError, match="IPC failed"):
        acct.positions()


def test_positions_symbol_filter_passed_through():
    acct, fake = make()
    fake.set_behavior(
        "positions_get",
        value=lambda symbol=None, ticket=None: (LONG,) if symbol == "EURUSD" else (),
    )
    assert [p.symbol for p in acct.positions("EURUSD")] == ["EURUSD"]
    assert acct.positions("GBPUSD") == []


def test_position_by_ticket_exact():
    acct, fake = make()
    fake.set_behavior(
        "positions_get",
        value=lambda symbol=None, ticket=None: (LONG, SHORT) if ticket == 9001 else (),
    )
    pos = acct.position(9001)
    assert pos.ticket == 9001


def test_position_by_ticket_not_found_raises():
    acct, fake = make()
    fake.set_behavior("positions_get", value=None)
    fake.last_error_value = (1, "Success")
    with pytest.raises(DataUnavailableError, match="不存在"):
        acct.position(9999)


def test_pending_orders_map_kind():
    acct, fake = make()
    lim = PEND
    stp = OrdRow(8002, "XAUUSD", 5, 0.1, 1980.0, 1950.0, 0.0, 0.0, 1698000060, "",
                 20261022, 0, 0.0, 0)
    fake.set_behavior("orders_get", value=lambda symbol=None, ticket=None: (lim, stp))
    orders = acct.pending_orders()
    assert [o.kind for o in orders] == ["buy_limit", "sell_stop"]
    assert orders[0].ticket == 8001 and orders[0].volume == 0.5
    assert orders[0].price_open == 1.0900 and orders[0].price_current == 1.1000
    assert orders[1].time_setup == datetime(2023, 10, 22, 18, 41, 0)


def test_pending_order_by_ticket():
    acct, fake = make()
    fake.set_behavior(
        "orders_get", value=lambda symbol=None, ticket=None: (PEND,) if ticket == 8001 else (),
    )
    order = acct.pending_order(8001)
    assert order.ticket == 8001 and order.kind == "buy_limit"


def test_orders_none_with_error_raises():
    acct, fake = make()
    fake.set_behavior("orders_get", value=None)
    fake.last_error_value = (-6, "IPC failed")
    with pytest.raises(DataUnavailableError, match="IPC failed"):
        acct.pending_orders()


def test_deals_pass_epoch_window_and_map_fields():
    acct, fake = make()
    deal = DealRow(7001, 9001, 9001, "EURUSD", 0, 0, 0.5, 1.1002, 12.5, 0.0, 0.0, -0.35,
                   1698000000, "smoke", 20261022)

    def check(from_dt, to_dt, **kw):
        assert from_dt == 1697986800 and to_dt == 1698000000  # naive UTC → 整数秒
        return (deal,)

    fake.set_behavior("history_deals_get", value=check)
    deals = acct.deals(datetime(2023, 10, 22, 15, 0), datetime(2023, 10, 22, 18, 40))
    d = deals[0]
    assert isinstance(d, Deal)
    assert d.side == "buy" and d.entry == "in"
    assert d.position_id == 9001 and d.magic == 20261022
    assert d.volume == 0.5 and d.price == 1.1002
    assert d.commission == 0.0 and d.fee == -0.35 and d.comment == "smoke"


def test_deals_map_reason_field():
    """T30/C1：deal.reason 原值透传（SL/TP/SO 识别的依据）。"""
    acct, fake = make()
    DealRowR = namedtuple("DealRowR", DealRow._fields + ("reason",))
    sl = DealRowR(7002, 9001, 9001, "XAUUSD", 1, 1, 0.1, 1945.0, -30.0, 0.0,
                  0.0, 0.0, 1698000100, "[sl]", 20261022, 4)
    client = DealRowR(7003, 9002, 9002, "XAUUSD", 1, 1, 0.1, 1950.0, 5.0, 0.0,
                      0.0, 0.0, 1698000200, "manual", 20261022, 0)
    fake.set_behavior("history_deals_get", value=(sl, client))
    deals = acct.deals(datetime(2023, 10, 22, 15, 0), datetime(2023, 10, 22, 19, 0))
    assert [d.reason for d in deals] == [4, 0]   # 0=CLIENT 是合法值，不是缺字段


def test_deals_reason_missing_is_none():
    """旧式 row（无 reason 字段）→ None，不冒充任何原因。"""
    acct, fake = make()
    deal = DealRow(7001, 9001, 9001, "EURUSD", 0, 0, 0.5, 1.1002, 12.5, 0.0,
                   0.0, -0.35, 1698000000, "smoke", 20261022)
    fake.set_behavior("history_deals_get", value=(deal,))
    deals = acct.deals(datetime(2023, 10, 22, 15, 0), datetime(2023, 10, 22, 19, 0))
    assert deals[0].reason is None


def test_deal_reason_constants_match_package():
    """常量与本机 MetaTrader5 包逐字核对过（2026-10-24 只读打印）——
    防手滑改动破坏 SL/TP/SO 识别。"""
    from mt5gw import models
    assert models.DEAL_REASON_CLIENT == 0
    assert models.DEAL_REASON_MOBILE == 1
    assert models.DEAL_REASON_WEB == 2
    assert models.DEAL_REASON_EXPERT == 3
    assert models.DEAL_REASON_SL == 4
    assert models.DEAL_REASON_TP == 5
    assert models.DEAL_REASON_SO == 6
    assert models.DEAL_REASON_ROLLOVER == 7
    assert models.DEAL_REASON_VMARGIN == 8
    assert models.DEAL_REASON_SPLIT == 9


def test_deals_for_position_passes_position_kwarg():
    """T30：按持仓 id 查成交（外部出场识别）——position= 形态透传。

    FakeMT5.history_deals_get 只实现了 (date_from, date_to) 形态，这里
    用其子类覆盖成 position= 关键字形态（真包文档：or filtered by
    position id）。
    """
    deal = DealRow(7004, 9001, 9001, "XAUUSD", 1, 1, 0.1, 1945.0, -30.0, 0.0,
                   0.0, 0.0, 1698000100, "[sl]", 20261022)

    class _PosFake(FakeMT5):
        def __init__(self):
            super().__init__()
            self.kwargs = None

        def history_deals_get(self, **kwargs):   # 只接受 position= 形态
            self.kwargs = kwargs
            return (deal,)

    stub = _PosFake()
    conn = MT5Connection(mt5_module=stub)
    conn.connect()
    acct = AccountService(conn, stub)
    deals = acct.deals_for_position(9001)
    assert stub.kwargs == {"position": 9001}
    assert len(deals) == 1 and deals[0].position_id == 9001


def test_deals_include_balance_operations():
    acct, fake = make()
    deposit = DealRow(7000, 0, 0, "", 2, None, 0.0, 0.0, 10000.0, 0.0, 0.0, 0.0,
                      1697000000, "deposit", 0)
    fake.set_behavior("history_deals_get", value=(deposit,))
    deals = acct.deals(datetime(2023, 10, 1), datetime(2023, 10, 23))
    assert deals[0].side == "balance"  # 出入金流水不是买卖方向
    assert deals[0].entry is None
    assert deals[0].profit == 10000.0


def test_deals_none_with_error_raises():
    acct, fake = make()
    fake.set_behavior("history_deals_get", value=None)
    fake.last_error_value = (-6, "IPC failed")
    with pytest.raises(DataUnavailableError, match="IPC failed"):
        acct.deals(datetime(2023, 10, 1), datetime(2023, 10, 23))


def test_order_history_maps_fields():
    acct, fake = make()
    h = HistRow(8001, "EURUSD", 2, 0.5, 1.0900, 3, 1698000000, 1698000100, "done")
    fake.set_behavior("history_orders_get", value=(h,))
    rows = acct.order_history(datetime(2023, 10, 22, 15, 0), datetime(2023, 10, 22, 19, 0))
    r = rows[0]
    assert isinstance(r, HistoryOrder)
    assert r.kind == "buy_limit" and r.state == 3
    assert r.time_done == datetime(2023, 10, 22, 18, 41, 40)


def test_order_history_maps_market_and_unknown_types():
    acct, fake = make()
    mk = HistRow(8002, "EURUSD", 0, 0.02, 0.0, 3, 1698000000, 1698000100, "market")
    exotic = HistRow(8003, "XAUUSD", 6, 0.1, 1960.0, 3, 1698000000, 1698000100, "")
    fake.set_behavior("history_orders_get", value=(mk, exotic))
    rows = acct.order_history(datetime(2023, 10, 22, 15, 0), datetime(2023, 10, 22, 19, 0))
    assert rows[0].kind == "buy"        # 市价单：最常见的类型
    assert rows[1].kind == "type6"      # 未知类型不崩溃，如实标注


def test_order_history_unfinished_done_is_none():  # M6：0 顶替等于伪造
    acct, fake = make()
    h = HistRow(8004, "EURUSD", 3, 0.5, 1.0900, 0, 1698000000, 0, "cancelled")
    fake.set_behavior("history_orders_get", value=(h,))
    r = acct.order_history(datetime(2023, 10, 22, 15, 0), datetime(2023, 10, 22, 19, 0))[0]
    assert r.time_done is None
