from collections import namedtuple
from datetime import datetime, timezone

import pytest

from mt5gw.connection import MT5Connection
from mt5gw.errors import DataUnavailableError
from mt5gw.market import MarketData
from mt5gw.models import Bar, Quote, SymbolSpec, Tick
from mt5gw.symbols import SymbolCatalog

from tests.fakes import FakeMT5

SymRow = namedtuple(
    "SymRow",
    "name description digits point spread trade_mode currency_profit currency_margin "
    "trade_contract_size volume_min volume_max volume_step filling_mode visible",
)
TickInfo = namedtuple("TickInfo", "time bid ask last volume")  # 包的 tick 无 symbol 字段
RateRow = namedtuple("RateRow", "time open high low close tick_volume spread real_volume")
TickRec = namedtuple("TickRec", "time_msc bid ask last volume")

EUR = SymRow(
    name="EURUSD", description="Euro vs US Dollar", digits=5, point=0.00001, spread=12,
    trade_mode=4, currency_profit="USD", currency_margin="USD", trade_contract_size=100000.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01, filling_mode=1, visible=True,
)


def make():
    fake = FakeMT5()
    conn = MT5Connection(mt5_module=fake)
    return conn, SymbolCatalog(conn, fake), MarketData(conn, fake), fake


def test_all_symbols_mapped_to_spec():
    conn, cat, _, fake = make()
    gbp = EUR._replace(name="GBPUSD")
    fake.set_behavior("symbols_get", value=(EUR, gbp))
    specs = cat.all()
    assert [s.name for s in specs] == ["EURUSD", "GBPUSD"]
    spec = specs[0]
    assert isinstance(spec, SymbolSpec)
    assert spec.description == "Euro vs US Dollar"
    assert spec.digits == 5 and spec.point == 0.00001 and spec.spread == 12
    assert spec.contract_size == 100000.0
    assert spec.volume_min == 0.01 and spec.volume_max == 100.0 and spec.volume_step == 0.01
    assert spec.currency_profit == "USD" and spec.currency_margin == "USD"
    assert spec.trade_mode == 4 and spec.filling_mode == 1
    assert spec.raw is EUR  # 原始行完整保留


def test_symbols_get_none_raises_data_unavailable():
    conn, cat, _, fake = make()
    fake.last_error_value = (1, "terminal: no symbols")
    with pytest.raises(DataUnavailableError, match="terminal: no symbols"):
        cat.all()


def test_get_unknown_symbol_raises():
    conn, cat, _, fake = make()
    fake.set_behavior("symbol_info", value=None)
    with pytest.raises(DataUnavailableError, match="EURUSD"):
        cat.get("EURUSD")


def test_get_single_symbol_maps_fields():
    conn, cat, _, fake = make()
    fake.set_behavior("symbol_info", value=EUR)
    spec = cat.get("EURUSD")
    assert spec.name == "EURUSD" and spec.volume_step == 0.01


def test_quote_returns_fresh_tick_fields():
    conn, _, mkt, fake = make()
    fake.set_behavior("symbol_info", value=EUR)
    fake.set_behavior("symbol_info_tick", value=TickInfo(1698000000, 1.10450, 1.10462, 1.10460, 7))
    q = mkt.quote("EURUSD")
    assert isinstance(q, Quote)
    assert q.symbol == "EURUSD"
    assert q.bid == 1.10450 and q.ask == 1.10462 and q.last == 1.10460 and q.volume == 7
    assert q.time == datetime(2023, 10, 22, 18, 40, 0)  # naive UTC 标签（1698000000）


def test_quote_none_raises_data_unavailable():
    conn, _, mkt, fake = make()
    fake.last_error_value = (2, "unknown symbol")
    with pytest.raises(DataUnavailableError, match="unknown symbol"):
        mkt.quote("EURUSD")


def test_bars_none_raises_data_unavailable_with_last_error():
    conn, _, mkt, fake = make()
    fake.set_behavior("symbol_info", value=EUR)
    fake.last_error_value = (13, "no history")
    with pytest.raises(DataUnavailableError, match="no history"):
        mkt.bars("EURUSD", "M1", 100)


def test_bars_converts_rates_rows_to_bar_list():
    conn, _, mkt, fake = make()
    fake.set_behavior("symbol_info", value=EUR)
    rows = (
        RateRow(1698000000, 1.1000, 1.1050, 1.0990, 1.1020, 123, 12, 456),
        RateRow(1698000060, 1.1020, 1.1060, 1.1010, 1.1050, 100, 11, 400),
    )
    fake.set_behavior("copy_rates_from_pos", value=rows)
    bars = mkt.bars("EURUSD", "M1", 2)
    assert len(bars) == 2 and all(isinstance(b, Bar) for b in bars)
    b = bars[0]
    assert b.time == datetime(2023, 10, 22, 18, 40, 0)
    assert (b.open, b.high, b.low, b.close) == (1.1000, 1.1050, 1.0990, 1.1020)
    assert b.tick_volume == 123 and b.spread == 12 and b.real_volume == 456


def test_bad_timeframe_string_raises_valueerror():
    conn, _, mkt, fake = make()
    with pytest.raises(ValueError, match="XX9"):
        mkt.bars("EURUSD", "XX9", 10)
    assert fake.calls("copy_rates_from_pos") == 0  # 参数校验先于任何终端调用


def test_symbol_select_called_before_copy():
    conn, _, mkt, fake = make()
    hidden = EUR._replace(visible=False)
    fake.set_behavior("symbol_info", value=hidden)

    def copy_checked(*args):
        assert fake.calls("symbol_select") >= 1, "copy 前必须先 symbol_select"
        return ()

    fake.set_behavior("copy_rates_from_pos", value=copy_checked)
    fake.set_behavior("symbol_select", value=True)
    mkt.bars("EURUSD", "M1", 10)
    assert fake.calls("symbol_select") == 1


def test_ticks_explicit_since_passed_as_epoch_seconds():
    conn, _, mkt, fake = make()
    fake.set_behavior("symbol_info", value=EUR)
    fake.set_behavior("symbol_info_tick", value=TickInfo(1698000000, 1.1, 1.2, 1.15, 7))
    since = datetime(2023, 10, 22, 15, 0, 0)

    def check_range(symbol, from_time, to_time, flags):
        assert from_time == 1697986800  # 2023-10-22 15:00:00 UTC，无时区偏移
        assert to_time == 1698000060    # 最新报价 + 60s 上界
        assert flags == FakeMT5.COPY_TICKS_ALL
        return (TickRec(1697986800000, 1.1044, 1.1045, 1.1045, 1),)

    fake.set_behavior("copy_ticks_range", value=check_range)
    ticks = mkt.ticks("EURUSD", 50, since=since)
    assert len(ticks) == 1 and isinstance(ticks[0], Tick)
    assert ticks[0].bid == 1.1044


def test_bars_end_pos_forwarded_for_backfill():
    conn, _, mkt, fake = make()
    fake.set_behavior("symbol_info", value=EUR)

    def check_pos(symbol, timeframe, start_pos, count):
        assert start_pos == 5000  # 回填时从更早的位置取
        assert count == 1000
        return ()

    fake.set_behavior("copy_rates_from_pos", value=check_pos)
    mkt.bars("EURUSD", "M1", 1000, end_pos=5000)
    # 缺省仍从 0（最新）取
    fake.set_behavior("copy_rates_from_pos", value=())
    mkt.bars("EURUSD", "M1", 100)


def test_ticks_returns_latest_n_of_window():
    # I9：窗口 80 笔取 count=50 → 必须是最后 50 笔（旧实现给的是最早 50 笔）
    conn, _, mkt, fake = make()
    fake.set_behavior("symbol_info", value=EUR)
    fake.set_behavior("symbol_info_tick", value=TickInfo(1698000000, 1.1, 1.2, 1.15, 7))
    window = tuple(
        TickRec(1697999700000 + i * 1000, 1.10 + i * 0.0001, 1.11, 1.10, i)
        for i in range(80)
    )
    fake.set_behavior("copy_ticks_range", value=window)
    ticks = mkt.ticks("EURUSD", 50)
    assert len(ticks) == 50
    assert ticks[-1].time == datetime(2023, 10, 22, 18, 41, 39)  # 窗口最后一笔
    assert ticks[0].time == datetime(2023, 10, 22, 18, 40, 30)   # 而非窗口第一笔


def test_symbol_select_failure_raises():
    conn, _, mkt, fake = make()
    hidden = EUR._replace(visible=False)
    fake.set_behavior("symbol_info", value=hidden)
    fake.set_behavior("symbol_select", value=False)  # M7：加入 Market Watch 失败
    fake.last_error_value = (-2, "select failed")
    with pytest.raises(DataUnavailableError, match="Market Watch"):
        mkt.bars("EURUSD", "M1", 10)
