"""A 层引擎测试：成交模型逐项手算对账（half=0.5、slip=0.1、佣金 1.0/手）。"""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
from mt5mcp.indicators import atr as mcp_atr
from mt5quant.strategies import BaseStrategy, BuyHold, ExitRules
from mt5quant.backtest.engine_a import (
    BacktestResult,
    Costs,
    atr_series,
    run_backtest,
)

HALF, SLIP, COMM, ONIGHT = 0.5, 0.1, 1.0, -2.0


class FlatCosts:
    """手算假成本：常数 half/slip/佣金、隔夜费每次调用恒 −2.0。"""

    def __init__(self, half=HALF, slip=SLIP, commission=COMM, overnight=ONIGHT):
        self.half, self.slip = half, slip
        self.commission, self.overnight = commission, overnight

    def half_spread(self, symbol, hour_utc):
        return self.half

    def slippage(self, symbol, hour_utc):
        return self.slip

    def commission_per_lot(self, symbol):
        return self.commission

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return self.overnight


class HourlySlipCosts(FlatCosts):
    """滑点随 UTC 小时变化，验证引擎用的是执行 bar 的小时。"""

    def slippage(self, symbol, hour_utc):
        return SLIP + 0.01 * hour_utc


class ScriptStrategy(BaseStrategy):
    """按位置取脚本信号（fill 补齐）。仅引擎测试用：前缀不一致。"""

    def __init__(self, script, fill=0, exit_rules=None):
        self.script = list(script)
        self.fill = fill
        self.exit_rules = exit_rules or ExitRules()

    def generate_signals(self, bars, params):
        vals = [self.script[i] if i < len(self.script) else self.fill
                for i in range(len(bars))]
        return pd.Series(vals, index=bars.index, dtype="int64")


class BadValuesStrategy(BaseStrategy):
    def generate_signals(self, bars, params):
        return pd.Series(2, index=bars.index, dtype="int64")  # 非法值 2


def flat_bars(start, n, tf="H1", *, openc=100.0, high=100.5, low=99.5, close=100.0):
    secs = TIMEFRAME_SECONDS[tf]
    times = [start + timedelta(seconds=secs * i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


def base4():
    """4 根 H1（周一 2026-01-05 10:00 起），末根收盘 103。"""
    df = flat_bars(pd.Timestamp("2026-01-05 10:00"), 4)
    df.loc[3, "close"] = 103.0
    return df


def one_trade(result):
    assert len(result.trades) == 1
    return result.trades.iloc[0]


# ---------------------------------------------------------------- ATR

def test_atr_series_wilder_hand_values():
    closes = [10, 12, 11, 15, 14]
    highs = [11, 13, 12, 16, 15]
    lows = [9, 11, 9, 14, 13]
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    out = atr_series(df, period=3)
    # TR = [2,3,3,5,2]；种子=mean(2,3,3)=8/3，其后 Wilder 递推 (n-1)/3
    expected = [np.nan, np.nan, 8 / 3, 8 / 3 + (5 - 8 / 3) / 3,
                8 / 3 + (5 - 8 / 3) / 3 + (2 - (8 / 3 + (5 - 8 / 3) / 3)) / 3]
    assert np.allclose(out.to_numpy(), expected, equal_nan=True)


def test_atr_series_insufficient_data_all_nan():
    df = pd.DataFrame({"high": [1.0, 2.0], "low": [0.5, 1.0], "close": [1.0, 1.5]})
    assert atr_series(df, period=3).isna().all()


def test_atr_series_parity_with_mt5mcp_indicators():
    rng = np.random.default_rng(42)
    n = 60
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + rng.uniform(0.1, 0.6, n)
    low = close - rng.uniform(0.1, 0.6, n)
    df = pd.DataFrame({"high": high, "low": low, "close": close})
    mine = atr_series(df, period=14)
    ref = mcp_atr(df["high"], df["low"], df["close"], period=14)
    assert np.allclose(mine.to_numpy(), ref.to_numpy(), equal_nan=True)


# ------------------------------------------------- 手算单笔往返（用例 a/b）

def test_long_round_trip_hand_computed():
    df = base4()
    res = run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(),
                       symbol="XAUUSD", params={}, timeframe="H1")
    t = one_trade(res)
    # 入场：bar1 开盘 100 + half 0.5 + slip 0.1；出场：末根收盘 103 − slip 0.1
    assert t["side"] == "long"
    assert t["entry_price"] == pytest.approx(100 + HALF + SLIP)
    assert t["exit_price"] == pytest.approx(103 - SLIP)
    assert t["entry_time"] == df["time"].iloc[1]
    assert t["exit_time"] == df["time"].iloc[3]
    assert t["gross"] == pytest.approx((103 - SLIP) - (100 + HALF + SLIP))
    assert t["commission"] == pytest.approx(COMM)
    assert t["slippage_cost"] == pytest.approx(2 * SLIP)
    assert t["spread_cost"] == pytest.approx(HALF)
    assert t["swap"] == pytest.approx(0.0)  # 同日 10–13 点，不跨日界
    assert t["net"] == pytest.approx((103 - SLIP) - (100 + HALF + SLIP) - COMM)
    assert t["bars_held"] == 2
    assert t["exit_reason"] == "end_of_data"


def test_buy_hold_net_identity():
    df = base4()
    entry = 100 + HALF + SLIP
    exit_ = 103 - SLIP
    for lots, comm in ((1.0, COMM), (0.5, 0.5)):
        res = run_backtest(BuyHold(), df, FlatCosts(), symbol="XAUUSD",
                           params={}, volume_lots=lots, timeframe="H1")
        t = one_trade(res)
        assert t["net"] == pytest.approx((exit_ - entry) * lots - comm)


def test_short_round_trip_hand_computed():
    df = base4()
    res = run_backtest(ScriptStrategy([-1], fill=-1), df, FlatCosts(),
                       symbol="XAUUSD", params={}, timeframe="H1")
    t = one_trade(res)
    assert t["side"] == "short"
    assert t["entry_price"] == pytest.approx(100 - SLIP)      # 空头 entry=bid−slip
    assert t["exit_price"] == pytest.approx(103 + HALF + SLIP)  # 空头 exit=价+half+slip
    assert t["gross"] == pytest.approx((100 - SLIP) - (103 + HALF + SLIP))
    assert t["net"] == pytest.approx((100 - SLIP) - (103 + HALF + SLIP) - COMM)


# ------------------------------------------------- 无前视成交（用例 d）

def test_signal_bar_close_does_not_affect_next_open_fill():
    a = base4()
    b = base4()
    b.loc[0, "close"] = 111.0  # 大改信号 bar 的收盘价
    ta = one_trade(run_backtest(ScriptStrategy([1], fill=1), a, FlatCosts(),
                                symbol="XAUUSD", params={}, timeframe="H1"))
    tb = one_trade(run_backtest(ScriptStrategy([1], fill=1), b, FlatCosts(),
                                symbol="XAUUSD", params={}, timeframe="H1"))
    assert ta["entry_price"] == tb["entry_price"]
    assert ta["entry_time"] == a["time"].iloc[1] == tb["entry_time"]


def test_later_signal_change_does_not_affect_earlier_entry():
    df = base4()
    t1 = one_trade(run_backtest(ScriptStrategy([1, 0], fill=0), df, FlatCosts(),
                                symbol="XAUUSD", params={}, timeframe="H1"))
    t2 = one_trade(run_backtest(ScriptStrategy([1, 1], fill=1), df, FlatCosts(),
                                symbol="XAUUSD", params={}, timeframe="H1"))
    assert t1["entry_price"] == t2["entry_price"]
    assert t1["exit_reason"] == "signal" and t1["exit_time"] == df["time"].iloc[2]


def test_execution_uses_execution_bar_hour():
    df = base4()  # 执行 bar 为 11:00 UTC
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, HourlySlipCosts(),
                               symbol="XAUUSD", params={}, timeframe="H1"))
    assert t["entry_price"] == pytest.approx(100 + HALF + (SLIP + 0.01 * 11))


# ------------------------------------------------- SL/TP（用例 c）

def sl_tp_bars():
    """20 根恒定波幅 H1：TR 恒 1.0 → ATR14 从 bar13 起恒 1.0。"""
    return flat_bars(pd.Timestamp("2026-01-05 00:00"), 20)


def test_sl_tp_double_touch_fills_at_stop():
    df = sl_tp_bars()
    df.loc[19, "high"] = 103.0   # 触及 TP=100.6+2.0=102.6
    df.loc[19, "low"] = 98.0     # 同时触及 SL=100.6−2.0=98.6
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", params={},
                               timeframe="H1"))
    entry = 100 + HALF + SLIP  # bar19 开盘 100 入场
    assert t["entry_price"] == pytest.approx(entry)
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(entry - 2.0 - SLIP)  # 按止损价 −slip
    assert t["bars_held"] == 0  # 入场当根即止损


def test_take_profit_single_touch():
    df = sl_tp_bars()
    df.loc[19, "high"] = 103.0
    df.loc[19, "low"] = 99.5    # 未触及 SL=98.6
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", params={},
                               timeframe="H1"))
    assert t["exit_reason"] == "take_profit"
    assert t["exit_price"] == pytest.approx(100.6 + 2.0 - SLIP)


def test_stop_loss_single_touch():
    df = sl_tp_bars()
    df.loc[19, "low"] = 98.0    # 只触 SL
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=5.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", params={},
                               timeframe="H1"))
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(100.6 - 2.0 - SLIP)


# ------------------------------------------------- time_stop / week_close

def test_time_stop_exits_at_close():
    df = sl_tp_bars()
    rules = ExitRules(time_stop_bars=2)
    # 信号 bar5..7 = 1：bar6 开盘入场，bar8 收盘（持有满 2 根）time_stop 出场，
    # bar8 信号已归 0 → 不会重新入场
    t = one_trade(run_backtest(ScriptStrategy([0] * 5 + [1, 1, 1], fill=0,
                                             exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", params={},
                               timeframe="H1"))
    assert t["entry_time"] == df["time"].iloc[6]
    assert t["exit_time"] == df["time"].iloc[8]  # 持有满 2 根，第 8 根收盘出
    assert t["exit_reason"] == "time_stop"
    assert t["exit_price"] == pytest.approx(100.0 - SLIP)  # 按到期 bar 收盘价
    assert t["bars_held"] == 2


def week_bars(day):
    """周五 18:00 起四根 + 次周一 00:00 一根（冬令时收盘=22:00 UTC）。"""
    start = pd.Timestamp(f"{day} 18:00")
    df = flat_bars(start, 4)
    for i, c in enumerate([100.0, 100.0, 101.0, 102.0]):
        df.loc[i, "close"] = c
    nxt = flat_bars(pd.Timestamp(f"{day} 18:00") + pd.Timedelta(days=3, hours=6), 1)
    nxt.loc[0, "close"] = 103.0
    return pd.concat([df, nxt], ignore_index=True)


def test_week_close_exit_january_est():
    # 2026-01-09 周五，EST：17:00 NY = 22:00 UTC → 21:00 bar 是收盘前最后一根
    df = week_bars("2026-01-09")
    rules = ExitRules(week_close_exit=True)
    # 信号 bar0..2 = 1：bar1 开盘入场，bar3（周五 21:00 UTC）收盘前强平；
    # bar3 信号归 0 → 周一不再重新入场
    t = one_trade(run_backtest(ScriptStrategy([1, 1, 1, 0], fill=0,
                                             exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", params={},
                               timeframe="H1"))
    assert t["exit_reason"] == "week_close"
    assert t["exit_time"] == df["time"].iloc[3]  # 21:00 UTC 那根
    assert t["exit_price"] == pytest.approx(102.0 - SLIP)
    assert t["swap"] == pytest.approx(0.0)  # 同一交易日内入场出场


def test_week_close_exit_july_edt():
    # 2026-07-10 周五，EDT：17:00 NY = 21:00 UTC → 20:00 bar 是收盘前最后一根
    start = pd.Timestamp("2026-07-10 17:00")
    df = flat_bars(start, 4)
    for i, c in enumerate([100.0, 100.0, 101.0, 102.0]):
        df.loc[i, "close"] = c
    nxt = flat_bars(start + pd.Timedelta(days=3, hours=7), 1)
    nxt.loc[0, "close"] = 103.0
    df = pd.concat([df, nxt], ignore_index=True)
    rules = ExitRules(week_close_exit=True)
    t = one_trade(run_backtest(ScriptStrategy([1, 1, 1, 0], fill=0,
                                             exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", params={},
                               timeframe="H1"))
    assert t["exit_reason"] == "week_close"
    assert t["exit_time"] == df["time"].iloc[3]  # 20:00 UTC 那根
    assert t["exit_price"] == pytest.approx(102.0 - SLIP)


def test_without_week_close_holds_over_weekend():
    df = week_bars("2026-01-09")
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(),
                               symbol="XAUUSD", params={}, timeframe="H1"))
    assert t["exit_reason"] == "end_of_data"
    assert t["exit_time"] == df["time"].iloc[4]  # 次周一 00:00 那根
    assert t["swap"] == pytest.approx(ONIGHT)    # 周五 19:00 → 周一 00:00 跨日


# ------------------------------------------------- 隔夜费

def test_overnight_fee_charged_when_crossing_utc_date():
    df = flat_bars(pd.Timestamp("2026-01-05 22:00"), 4)  # 22,23,00,01 点
    res = run_backtest(ScriptStrategy([1, 1, 1, 0]), df, FlatCosts(),
                       symbol="XAUUSD", params={}, timeframe="H1")
    t = one_trade(res)
    assert t["entry_time"] == df["time"].iloc[1]  # 23:00 入场
    assert t["exit_time"] == df["time"].iloc[3]   # 次日 01:00 出场
    assert t["swap"] == pytest.approx(ONIGHT)
    assert t["net"] == pytest.approx(t["gross"] - COMM + ONIGHT)


# ------------------------------------------------- 同 bar 反向：先平后开

def test_reversal_closes_then_opens_at_same_bar_open():
    df = base4()
    res = run_backtest(ScriptStrategy([1, -1], fill=-1), df, FlatCosts(),
                       symbol="XAUUSD", params={}, timeframe="H1")
    assert len(res.trades) == 2
    t1, t2 = res.trades.iloc[0], res.trades.iloc[1]
    assert t1["side"] == "long" and t1["exit_reason"] == "signal"
    assert t1["exit_price"] == pytest.approx(100 - SLIP)   # bar2 开盘平多
    assert t2["side"] == "short"
    assert t2["entry_price"] == pytest.approx(100 - SLIP)  # 同一根开盘开空
    assert t2["entry_time"] == t1["exit_time"] == df["time"].iloc[2]


# ------------------------------------------------- equity 曲线

def test_equity_mark_to_market_per_bar():
    df = base4()
    res = run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(),
                       symbol="XAUUSD", params={}, initial_capital=10_000.0,
                       timeframe="H1")
    eq = res.equity
    assert list(eq.index) == list(df.index)
    entry = 100 + HALF + SLIP
    assert list(eq) == pytest.approx([
        10_000.0,                # bar0：无仓
        10_000.0 + (100.0 - entry),  # bar1 收盘浮亏（含入场成本）
        10_000.0 + (100.0 - entry),  # bar2
        10_000.0 + 1.3,          # bar3 已实现净利
    ])
    assert eq.iloc[-1] == pytest.approx(10_000.0 + one_trade(res)["net"])


def test_result_carries_context():
    df = base4()
    res = run_backtest(BuyHold(), df, FlatCosts(), symbol="XAUUSD",
                       params={}, timeframe="H1")
    assert isinstance(res, BacktestResult)
    assert res.symbol == "XAUUSD" and res.timeframe == "H1"
    assert res.params == {}
    assert list(res.trades.columns)[:4] == ["entry_time", "exit_time", "side",
                                            "volume_lots"]


# ------------------------------------------------- 失败模式（fail-fast）

def test_rejects_signal_values_outside_allowed_set():
    df = base4()
    with pytest.raises(ValueError, match="信号"):
        run_backtest(BadValuesStrategy(), df, FlatCosts(),
                     symbol="XAUUSD", params={}, timeframe="H1")


def test_rejects_non_monotonic_bars():
    df = base4().iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError, match="升序"):
        run_backtest(BuyHold(), df, FlatCosts(),
                     symbol="XAUUSD", params={}, timeframe="H1")


def test_rejects_unknown_params():
    df = base4()
    with pytest.raises(ValueError, match="未知参数"):
        run_backtest(BuyHold(), df, FlatCosts(),
                     symbol="XAUUSD", params={"k": 1}, timeframe="H1")


def test_rejects_invalid_exit_rules():
    df = base4()
    with pytest.raises(ValueError, match="time_stop_bars"):
        run_backtest(ScriptStrategy([1], fill=1,
                                    exit_rules=ExitRules(time_stop_bars=0)),
                     df, FlatCosts(), symbol="XAUUSD", params={}, timeframe="H1")
    with pytest.raises(ValueError, match="stop_loss_atr"):
        run_backtest(ScriptStrategy([1], fill=1,
                                    exit_rules=ExitRules(stop_loss_atr=-1.0)),
                     df, FlatCosts(), symbol="XAUUSD", params={}, timeframe="H1")
