from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

from mt5gw.connection import MT5Connection
from mt5gw.market import MarketData
from mt5gw.symbols import SymbolCatalog
from mt5data.downloader import ALL_TIMEFRAMES, backfill_series, refresh_series

from tests.fakes import FakeMT5

SymRow = namedtuple(
    "SymRow",
    "name description digits point spread trade_mode currency_profit currency_margin "
    "trade_contract_size volume_min volume_max volume_step filling_mode visible",
)
TickInfo = namedtuple("TickInfo", "time bid ask last volume")
RateRow = namedtuple("RateRow", "time open high low close tick_volume spread real_volume")

EUR = SymRow(
    name="EURUSD", description="", digits=5, point=0.00001, spread=12,
    trade_mode=4, currency_profit="USD", currency_margin="USD",
    trade_contract_size=100000.0, volume_min=0.01, volume_max=100.0,
    volume_step=0.01, filling_mode=1, visible=True,
)

BASE = datetime(2023, 10, 22, 12, 0)


def rate(ts_min):
    t = int((BASE + timedelta(minutes=ts_min)).timestamp())
    return RateRow(t, 1.0, 1.0, 1.0, 1.0, 10, 1, 0)


def make_gateway(total_history, now_min, now_sec=0):
    """伪造 total_history 根连续 M1 历史；'现在' 在 now_min 分 now_sec 秒处。

    真实语义：pos 0 是最新一根，越大越老；p 号 bar 时间 = now - p 分钟。
    now 可被测试推进（refresh 场景）。
    """
    fake = FakeMT5()
    conn = MT5Connection(mt5_module=fake)
    state = {
        "now": BASE + timedelta(minutes=now_min, seconds=now_sec),
        "total": total_history,
    }

    def copy_rates(symbol, timeframe, start_pos, count):
        rows = []
        for p in range(start_pos, start_pos + count):
            if p >= state["total"]:  # 越过历史最老端 → 无行
                continue
            t = state["now"] - timedelta(minutes=p)
            # naive 时间戳必须锚定 UTC，否则被按宿主时区（UTC+8）解释
            rows.append(RateRow(
                int(t.replace(tzinfo=timezone.utc).timestamp()),
                1.0, 1.0, 1.0, 1.0, 10, 1, 0))
        return tuple(rows)

    def copy_rates_refresh(symbol, timeframe, start_pos, count):
        return copy_rates(symbol, timeframe, start_pos, count)

    fake.set_behavior("copy_rates_from_pos", value=copy_rates)
    fake.set_behavior("symbol_info", value=EUR)
    fake.set_behavior("symbol_select", value=True)
    def now_epoch():
        return int(state["now"].replace(tzinfo=timezone.utc).timestamp())

    fake.set_behavior("symbol_info_tick", value=lambda symbol=None: TickInfo(
        now_epoch(), 1.1, 1.2, 1.15, 7))
    fake.state = state  # 测试推进时间用：fake.state["now"] = ...
    return fake, MarketData(conn, fake), SymbolCatalog(conn, fake)


def test_all_timeframes_is_21_canonical_ordered():
    assert len(ALL_TIMEFRAMES) == 21
    assert ALL_TIMEFRAMES[:3] == ["M1", "M2", "M3"]
    assert ALL_TIMEFRAMES[-3:] == ["D1", "W1", "MN1"]


def test_backfill_walks_chunks_and_drops_forming_bar(tmp_path):
    fake, market, symbols = make_gateway(total_history=250, now_min=249, now_sec=30)
    stats = backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    # 250 根历史，最后一根（第 250 根）正在形成 → 只落库 249
    assert stats["rows"] == 249
    assert stats["chunks_fetched"] >= 3  # 100+100+50 分块
    assert stats["first_time"].startswith("2023-10-22T12:00")
    assert stats["last_time"].startswith("2023-10-22T16:08")  # 12:00 起第 248 分钟


def test_backfill_merges_overlaps_without_duplicates(tmp_path):
    fake, market, symbols = make_gateway(total_history=120, now_min=119, now_sec=30)
    stats = backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    assert stats["rows"] == 119  # 120 根去掉形成中的最后一根
    wm_stats = backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    assert wm_stats["rows"] == 119  # 重跑不重复


def test_refresh_appends_new_bars(tmp_path):
    fake, market, symbols = make_gateway(total_history=100, now_min=99, now_sec=30)
    backfill_series(tmp_path, market, "EURUSD", "M1", chunk=100)
    # 时间前进 10 分钟、历史多出 10 根新 bar（第 110 根形成中被丢弃）
    fake.state["now"] = BASE + timedelta(minutes=109, seconds=30)
    fake.state["total"] = 110
    stats = refresh_series(tmp_path, market, "EURUSD", "M1", lookback=50)
    assert stats["rows"] == 109  # 110 根收盘
    assert stats["new_bars"] == 10
