"""A 层引擎测试：成交模型逐项手算对账（half=0.5、slip=0.1、佣金 1.0/手）；
成本接口 v2（R1/T3）：price 传参断言 + 相对点差模型手算。"""
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
    ENGINE_VERSION,
    atr_series,
    run_backtest,
)

HALF, SLIP, COMM, ONIGHT = 0.5, 0.1, 1.0, -2.0


class FlatCosts:
    """手算假成本：常数 half/slip/佣金；隔夜费按 (from,to] 跨过的 UTC 午夜数计。

    绝对口径（成本接口 v2 的 price 参数被忽略），与旧版行为逐位一致。
    """

    def __init__(self, half=HALF, slip=SLIP, commission=COMM, overnight=ONIGHT):
        self.half, self.slip = half, slip
        self.commission, self.overnight = commission, overnight

    def spread_p90(self, symbol, hour_utc, *, price=None):
        return self.half

    def slippage(self, symbol, hour_utc, *, price=None):
        return self.slip

    def commission_per_lot(self, symbol):
        return self.commission

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        # T82：price 形参保持成本接口兼容（绝对假成本忽略）
        f, t = pd.Timestamp(from_ts), pd.Timestamp(to_ts)
        if t <= f:
            return 0.0
        days = (t.normalize() - f.normalize()).days  # (f,t] 内午夜数（t 恰为午夜也计）
        return self.overnight * days


class ChargeHourCosts(FlatCosts):
    """按 21:00 计费时刻数夜（I2 回归）：每夜 −3.0。"""

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        f, t = pd.Timestamp(from_ts), pd.Timestamp(to_ts)
        nights = 0
        day = f.normalize()
        while day <= t:
            mark = day + pd.Timedelta(hours=21)
            if f < mark <= t:
                nights += 1
            day += pd.Timedelta(days=1)
        return -3.0 * nights


class HourlySlipCosts(FlatCosts):
    """滑点随 UTC 小时变化，验证引擎用的是执行 bar 的小时。"""

    def slippage(self, symbol, hour_utc, *, price=None):
        return SLIP + 0.01 * hour_utc


class RecordingCosts(FlatCosts):
    """记录每次 spread_p90/slippage 调用参数的假成本（price 传参断言用）。"""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, int, float | None]] = []

    def spread_p90(self, symbol, hour_utc, *, price=None):
        self.calls.append(("spread", int(hour_utc), price))
        return self.half

    def slippage(self, symbol, hour_utc, *, price=None):
        self.calls.append(("slip", int(hour_utc), price))
        return self.slip

    def prices(self, kind: str) -> list[float | None]:
        return [p for k, _, p in self.calls if k == kind]


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
                       symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
    t = one_trade(res)
    # 入场：bar1 开盘 100 + half 0.5 + slip 0.1；出场：末根收盘 103 − slip 0.1
    assert t["side"] == "long"
    assert t["entry_price"] == pytest.approx(100 + HALF + SLIP)
    assert t["exit_price"] == pytest.approx(103 - SLIP)
    assert t["entry_time"] == df["time"].iloc[1]
    assert t["exit_time"] == df["time"].iloc[3]
    assert t["exit_fill_time"] == df["time"].iloc[3] + pd.Timedelta(hours=1)  # 收盘价出场=bar 收盘
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
        res = run_backtest(BuyHold(), df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                           params={}, volume_lots=lots, timeframe="H1")
        t = one_trade(res)
        assert t["net"] == pytest.approx((exit_ - entry) * lots - comm)


def test_short_round_trip_hand_computed():
    df = base4()
    res = run_backtest(ScriptStrategy([-1], fill=-1), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
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
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0))
    tb = one_trade(run_backtest(ScriptStrategy([1], fill=1), b, FlatCosts(),
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0))
    assert ta["entry_price"] == tb["entry_price"]
    assert ta["entry_time"] == a["time"].iloc[1] == tb["entry_time"]


def test_later_signal_change_does_not_affect_earlier_entry():
    df = base4()
    t1 = one_trade(run_backtest(ScriptStrategy([1, 0], fill=0), df, FlatCosts(),
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0))
    t2 = one_trade(run_backtest(ScriptStrategy([1, 1], fill=1), df, FlatCosts(),
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0))
    assert t1["entry_price"] == t2["entry_price"]
    assert t1["exit_reason"] == "signal" and t1["exit_time"] == df["time"].iloc[2]


def test_execution_uses_execution_bar_hour():
    df = base4()  # 执行 bar 为 11:00 UTC = 纽约本地 6 点（EST，T20 分桶口径）
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, HourlySlipCosts(),
                               symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0))
    assert t["entry_price"] == pytest.approx(100 + HALF + (SLIP + 0.01 * 6))


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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
                               timeframe="H1", volume_lots=1.0))
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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
                               timeframe="H1", volume_lots=1.0))
    assert t["exit_reason"] == "take_profit"
    assert t["exit_price"] == pytest.approx(100.6 + 2.0 - SLIP)


def test_stop_loss_single_touch():
    df = sl_tp_bars()
    df.loc[19, "low"] = 98.0    # 只触 SL
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=5.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
                               timeframe="H1", volume_lots=1.0))
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(100.6 - 2.0 - SLIP)
    assert t["exit_fill_time"] == t["exit_time"]   # 盘中出场：成交时刻=bar 时间


# ------------------------------------------------- time_stop / week_close

def test_time_stop_exits_at_close():
    df = sl_tp_bars()
    rules = ExitRules(time_stop_bars=2)
    # 信号 bar5..7 = 1：bar6 开盘入场，bar8 收盘（持有满 2 根）time_stop 出场，
    # bar8 信号已归 0 → 不会重新入场
    t = one_trade(run_backtest(ScriptStrategy([0] * 5 + [1, 1, 1], fill=0,
                                             exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
                               timeframe="H1", volume_lots=1.0))
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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
                               timeframe="H1", volume_lots=1.0))
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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
                               timeframe="H1", volume_lots=1.0))
    assert t["exit_reason"] == "week_close"
    assert t["exit_time"] == df["time"].iloc[3]  # 20:00 UTC 那根
    assert t["exit_price"] == pytest.approx(102.0 - SLIP)


def test_without_week_close_holds_over_weekend():
    df = week_bars("2026-01-09")
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(),
                               symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0))
    assert t["exit_reason"] == "end_of_data"
    assert t["exit_time"] == df["time"].iloc[4]  # 次周一 00:00 那根
    assert t["swap"] == pytest.approx(4 * ONIGHT)  # 周五19:00→(次日)周二00:00：跨 4 个午夜（模型整段计夜）


# ------------------------------------------------- 隔夜费

def test_overnight_fee_charged_when_crossing_utc_date():
    df = flat_bars(pd.Timestamp("2026-01-05 22:00"), 4)  # 22,23,00,01 点
    res = run_backtest(ScriptStrategy([1, 1, 1, 0]), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
    t = one_trade(res)
    assert t["entry_time"] == df["time"].iloc[1]  # 23:00 入场
    assert t["exit_time"] == df["time"].iloc[3]   # 次日 01:00 出场
    assert t["swap"] == pytest.approx(ONIGHT)
    assert t["net"] == pytest.approx(t["gross"] - COMM + ONIGHT)


# ------------------------------------------------- 同 bar 反向：先平后开

def test_reversal_closes_then_opens_at_same_bar_open():
    df = base4()
    res = run_backtest(ScriptStrategy([1, -1], fill=-1), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
    assert len(res.trades) == 2
    t1, t2 = res.trades.iloc[0], res.trades.iloc[1]
    assert t1["side"] == "long" and t1["exit_reason"] == "signal"
    assert t1["exit_price"] == pytest.approx(100 - SLIP)   # bar2 开盘平多
    assert t2["side"] == "short"
    assert t2["entry_price"] == pytest.approx(100 - SLIP)  # 同一根开盘开空
    assert t2["entry_time"] == t1["exit_time"] == df["time"].iloc[2]
    assert t1["exit_fill_time"] == t1["exit_time"]  # signal 出场：成交时刻=bar 时间


# ------------------------------------------------- equity 曲线

def test_equity_mark_to_market_per_bar():
    df = base4()
    res = run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={}, initial_capital=10_000.0,
                       timeframe="H1", volume_lots=1.0)
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
    assert res.ruined is False and res.ruined_at is None   # 权益未穿透 0：无爆仓标记


def test_result_carries_context():
    df = base4()
    res = run_backtest(BuyHold(), df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                       params={}, timeframe="H1", volume_lots=1.0)
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
                     symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)


def test_rejects_non_monotonic_bars():
    df = base4().iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError, match="升序"):
        run_backtest(BuyHold(), df, FlatCosts(),
                     symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)


def test_rejects_unknown_params():
    df = base4()
    with pytest.raises(ValueError, match="未知参数"):
        run_backtest(BuyHold(), df, FlatCosts(),
                     symbol="XAUUSD", contract_size=1.0, params={"k": 1}, timeframe="H1", volume_lots=1.0)


def test_rejects_invalid_exit_rules():
    df = base4()
    with pytest.raises(ValueError, match="time_stop_bars"):
        run_backtest(ScriptStrategy([1], fill=1,
                                    exit_rules=ExitRules(time_stop_bars=0)),
                     df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
    with pytest.raises(ValueError, match="stop_loss_atr"):
        run_backtest(ScriptStrategy([1], fill=1,
                                    exit_rules=ExitRules(stop_loss_atr=-1.0)),
                     df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)


def test_overnight_fee_counts_intraday_charge_hour_crossing():
    """I2 回归：00:30 起持仓到当日 22:00——不跨 UTC 日界但跨 21:00 计费时刻，
    必须记 1 夜（旧实现只在跨日界时才调用模型，恒漏计日内跨计费时刻）。"""
    df = flat_bars(pd.Timestamp("2026-01-05 00:30"), 44, tf="M30")  # 00:30…22:00
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, ChargeHourCosts(),
                               symbol="XAUUSD", contract_size=1.0, params={}, timeframe="M30", volume_lots=1.0))
    assert t["entry_time"] == pd.Timestamp("2026-01-05 01:00")
    assert t["exit_time"] == pd.Timestamp("2026-01-05 22:00")  # 末根强平
    assert t["swap"] == pytest.approx(-3.0)                    # 恰跨 21:00 一次

    ctrl = flat_bars(pd.Timestamp("2026-01-05 00:30"), 24, tf="M30")  # 00:30…12:00 不跨
    t2 = one_trade(run_backtest(ScriptStrategy([1], fill=1), ctrl, ChargeHourCosts(),
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="M30", volume_lots=1.0))
    assert t2["swap"] == pytest.approx(0.0)


# ------------------------------------------------- 2026-10-25 修复轮回归
# C1 货币口径 / H1 跳空穿止损 / H2 空头按 Ask 触发 / swap × 手数

def gap_bars(open_, high, low, close):
    """sl_tp_bars（ATR14 恒 1.0）后追加第 20 根：用于入场后的跳空/盘中触发。"""
    df = sl_tp_bars()
    extra = flat_bars(pd.Timestamp("2026-01-05 20:00"), 1,
                      openc=open_, high=high, low=low, close=close)
    return pd.concat([df, extra], ignore_index=True)


def test_contract_size_is_required_keyword():
    with pytest.raises(TypeError, match="contract_size"):
        run_backtest(BuyHold(), base4(), FlatCosts(), symbol="XAUUSD",
                     params={}, timeframe="H1", volume_lots=1.0)


def test_rejects_invalid_contract_size_and_volume():
    for bad in (0, -100.0, float("nan"), float("inf"), True, "100"):
        with pytest.raises(ValueError, match="contract_size"):
            run_backtest(BuyHold(), base4(), FlatCosts(), symbol="XAUUSD",
                         contract_size=bad, params={}, timeframe="H1", volume_lots=1.0)
    with pytest.raises(ValueError, match="volume_lots"):
        run_backtest(BuyHold(), base4(), FlatCosts(), symbol="XAUUSD",
                     contract_size=100.0, params={}, volume_lots=0.0, timeframe="H1")


def test_money_fields_are_account_currency():
    """XAUUSD 式合约（1 手 = 100 盎司）：价差 2.3 → 毛利 230 美元，佣金 1 美元/手。"""
    for lots in (1.0, 2.0):
        t = one_trade(run_backtest(ScriptStrategy([1], fill=1), base4(), FlatCosts(),
                                   symbol="XAUUSD", contract_size=100.0, params={},
                                   volume_lots=lots, timeframe="H1"))
        assert t["entry_price"] == pytest.approx(100.6)      # 价格字段仍是价格单位
        assert t["exit_price"] == pytest.approx(102.9)
        assert t["gross"] == pytest.approx(230.0 * lots)
        assert t["commission"] == pytest.approx(1.0 * lots)
        assert t["net"] == pytest.approx(230.0 * lots - 1.0 * lots)
        assert t["spread_cost"] == pytest.approx(0.5 * 100 * lots)
        assert t["slippage_cost"] == pytest.approx(0.2 * 100 * lots)


def test_equity_in_account_currency():
    res = run_backtest(ScriptStrategy([1], fill=1), base4(), FlatCosts(),
                       symbol="XAUUSD", contract_size=100.0, params={},
                       initial_capital=10_000.0, timeframe="H1", volume_lots=1.0)
    assert list(res.equity) == pytest.approx([10_000.0, 9_940.0, 9_940.0, 10_229.0])
    assert res.contract_size == 100.0 and res.volume_lots == 1.0
    assert res.initial_capital == 10_000.0


def test_swap_scales_with_lots():
    df = week_bars("2026-01-09")
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(),
                               symbol="XAUUSD", contract_size=1.0, params={},
                               volume_lots=3.0, timeframe="H1"))
    assert t["swap"] == pytest.approx(4 * ONIGHT * 3.0)   # 每手 4 夜 × 3 手


def test_r_multiple_uses_risk_in_account_currency():
    df = sl_tp_bars()
    df.loc[19, "low"] = 98.0
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=5.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=100.0,
                               params={}, timeframe="H1", volume_lots=1.0))
    assert t["gross"] == pytest.approx((98.5 - 100.6) * 100)
    assert t["r_multiple"] == pytest.approx(t["net"] / (2.0 * 1.0 * 100))


def test_long_stop_gap_fills_at_open_not_stop_price():
    # 入场 bar19 开盘 100.6，SL=98.6；bar20 开盘直接跳到 95（周末/数据跳空）
    df = gap_bars(95.0, 95.5, 94.5, 95.0)
    rules = ExitRules(stop_loss_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], fill=1, exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1", volume_lots=1.0))
    assert t["exit_reason"] == "stop_gap"
    assert t["exit_price"] == pytest.approx(95.0 - SLIP)    # 按开盘价，不是 98.5
    assert t["bars_held"] == 1


def test_long_take_profit_gap_fills_at_tp_conservatively():
    df = gap_bars(104.0, 104.5, 103.5, 104.0)               # TP=102.6，开盘已越过
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], fill=1, exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1", volume_lots=1.0))
    assert t["exit_reason"] == "take_profit_gap"
    assert t["exit_price"] == pytest.approx(102.6 - SLIP)


def test_short_stop_triggers_on_ask():
    # 空头入场 bid 99.9，SL=101.9；bar20 bid 高点 101.6 未到 SL，但 Ask=102.1 已到
    df = gap_bars(100.0, 101.6, 99.5, 100.0)
    rules = ExitRules(stop_loss_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1", volume_lots=1.0))
    assert t["entry_price"] == pytest.approx(100.0 - SLIP)
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(101.9 + SLIP)   # 价位即 Ask 口径，+slip


def test_short_take_profit_needs_ask_to_reach_level():
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=2.0)  # TP=97.9（Ask 口径）
    miss = gap_bars(100.0, 100.5, 97.5, 100.0)   # bid 低点 97.5，Ask 低点 98.0 > 97.9
    t1 = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                                miss, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                                params={}, timeframe="H1", volume_lots=1.0))
    assert t1["exit_reason"] == "end_of_data"
    hit = gap_bars(100.0, 100.5, 97.3, 100.0)    # Ask 低点 97.8 ≤ 97.9
    t2 = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                                hit, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                                params={}, timeframe="H1", volume_lots=1.0))
    assert t2["exit_reason"] == "take_profit"
    assert t2["exit_price"] == pytest.approx(97.9 + SLIP)


def test_short_stop_gap_fills_at_open_ask():
    df = gap_bars(103.0, 103.5, 102.5, 103.0)    # 开盘 Ask=103.5 ≥ SL 101.9
    rules = ExitRules(stop_loss_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1", volume_lots=1.0))
    assert t["exit_reason"] == "stop_gap"
    assert t["exit_price"] == pytest.approx(103.0 + HALF + SLIP)


# ------------------------------------------------- T36（H2）：止损越幅 A-1.6

class OvershootCosts(FlatCosts):
    """带 stop_overshoot 的假成本：恒定越幅 OV（p50 档）/ OV_P90（p90 档）。"""

    def __init__(self, ov=0.3, ov_p90=0.8, **kw):
        super().__init__(**kw)
        self.ov, self.ov_p90 = ov, ov_p90

    def stop_overshoot(self, symbol, hour_utc, *, price=None, level="p50"):
        return self.ov_p90 if level == "p90" else self.ov


def test_stop_overshoot_added_to_long_stop_exit_only():
    """失败测试清单第 3 条：越幅只加在 stop——多头 stop = SL − slip − ov。"""
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=2.0)
    df = sl_tp_bars()
    df.loc[19, "high"] = 103.0     # 触及 TP=102.6
    df.loc[19, "low"] = 98.0       # 同根触及 SL=98.6（双触 → 止损优先）
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, OvershootCosts(ov=0.3), symbol="XAUUSD",
                               contract_size=1.0, params={}, timeframe="H1",
                               volume_lots=1.0))
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(98.6 - SLIP - 0.3)   # 越幅恶化成交价


def test_stop_overshoot_not_added_to_take_profit_or_gap_or_signal():
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=2.0)
    # take_profit：不加越幅（102.6 − slip）
    df = sl_tp_bars()
    df.loc[19, "high"] = 103.0
    df.loc[19, "low"] = 99.5
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, OvershootCosts(ov=0.3), symbol="XAUUSD",
                               contract_size=1.0, params={}, timeframe="H1",
                               volume_lots=1.0))
    assert t["exit_reason"] == "take_profit"
    assert t["exit_price"] == pytest.approx(102.6 - SLIP)
    # stop_gap：按开盘价成交（跳空已含真实越幅），不重复加
    dg = gap_bars(95.0, 95.5, 94.5, 95.0)
    t2 = one_trade(run_backtest(
        ScriptStrategy([0] * 18 + [1], fill=1, exit_rules=ExitRules(stop_loss_atr=2.0)),
        dg, OvershootCosts(ov=0.3), symbol="XAUUSD", contract_size=1.0,
        params={}, timeframe="H1", volume_lots=1.0))
    assert t2["exit_reason"] == "stop_gap"
    assert t2["exit_price"] == pytest.approx(95.0 - SLIP)
    # signal 出场：不加越幅（bar2 开盘 100 平仓）
    t3 = one_trade(run_backtest(ScriptStrategy([1, 0], fill=0), base4(),
                                OvershootCosts(ov=0.3), symbol="XAUUSD",
                                contract_size=1.0, params={}, timeframe="H1",
                                volume_lots=1.0))
    assert t3["exit_reason"] == "signal"
    assert t3["exit_price"] == pytest.approx(100.0 - SLIP)


def test_stop_overshoot_added_to_short_stop_and_rejects_negative():
    # 空头 stop：exit = SL + slip + ov（Ask 口径价位本身已含点差）
    df = gap_bars(100.0, 101.6, 99.5, 100.0)
    rules = ExitRules(stop_loss_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                               df, OvershootCosts(ov=0.25), symbol="XAUUSD",
                               contract_size=1.0, params={}, timeframe="H1",
                               volume_lots=1.0))
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(101.9 + SLIP + 0.25)
    # 负越幅 = 有利方向成本，拒绝（fail-fast，不静默取 0）
    with pytest.raises(ValueError, match="越幅"):
        run_backtest(ScriptStrategy([0] * 14 + [1] * 5, fill=1, exit_rules=rules),
                     reentry_bars(), NegativeOvershootCosts(), symbol="XAUUSD",
                     contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)


class NegativeOvershootCosts(OvershootCosts):
    def stop_overshoot(self, symbol, hour_utc, *, price=None, level="p50"):
        return -0.1


def test_costs_without_stop_overshoot_keep_a15_behavior():
    """成本模型未提供 stop_overshoot（旧口径/测试替身）→ 不加越幅（A-1.5 行为）。"""
    rules = ExitRules(stop_loss_atr=2.0)
    df = sl_tp_bars()
    df.loc[19, "low"] = 98.0
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1", volume_lots=1.0))
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(98.6 - SLIP)


def test_equity_marks_short_at_ask():
    res = run_backtest(ScriptStrategy([-1], fill=-1), base4(), FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={},
                       initial_capital=10_000.0, timeframe="H1", volume_lots=1.0)
    # 入场 99.9；bar1/2 收盘 Ask=100.5 → 浮亏 0.6；末根平仓 103.6 → 净 −3.7−1.0
    assert list(res.equity) == pytest.approx([10_000.0, 9_999.4, 9_999.4, 9_995.3])


# ------------------------------------------------- 成本接口 v2（R1 / 计划 T3）

def test_engine_version_bumped_for_t82_swap_price_basis():
    # A-1.6 → A-1.7（T82）：overnight_fee 增 keyword-only price=（出场侧
    # 成交基准价）——点数模式费率按参考价折相对口径随 price 缩放，缺 price
    # 的相对模型 fail-fast；货币模式忽略 price（绝对额语义不变）。
    # A-1.6 = T36 stop 出场成交价加止损越幅 p50；A-1.5 = T23 RiskSizing
    # 按风险定手数；A-1.4 = T20 禁入窗推迟开仓
    assert ENGINE_VERSION == "A-1.7"


def test_engine_passes_price_to_cost_models_long_fills():
    """多头：入场 spr/slip 的 price=执行 bar 开盘；平仓 price=该根收盘。"""
    df = base4()  # bar1 11:00 开盘入场（open=100），末根 13:00 收盘 103 强平
    rec = RecordingCosts()
    run_backtest(ScriptStrategy([1], fill=1), df, rec,
                 symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
    # 多头估值按 Bid（close），不调点差模型 → 调用序列完全确定。
    # 小时为纽约本地（2026-01-05 EST：UTC−5）——11:00 UTC→6 点、13:00 UTC→8 点
    assert rec.calls == [("spread", 6, 100.0), ("slip", 6, 100.0),
                         ("spread", 8, 103.0), ("slip", 8, 103.0)]


def test_engine_passes_bar_prices_for_short_ask_trigger_and_marking():
    """空头：Ask 触发判断 price=当根 high/low（跳空用 open）；逐 bar 估值 price=close。"""
    df = gap_bars(100.0, 101.6, 99.5, 100.0)
    df.loc[19, "close"] = 100.4      # 与 open=100 区分，验证估值基准是 close
    rules = ExitRules(stop_loss_atr=2.0)
    rec = RecordingCosts()
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                               df, rec, symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1", volume_lots=1.0))
    spread_prices = rec.prices("spread")
    assert all(p is not None for p in spread_prices)
    assert 100.4 in spread_prices    # bar19 收盘估值 spr 的 price=close
    assert 101.6 in spread_prices    # bar20 SL 触发判断 spr 的 price=当根 high
    assert 100.0 in spread_prices    # 入场 open / bar20 开盘跳空判断
    # 绝对假成本下成交口径不变：exit = SL 价位 + slip
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(101.9 + SLIP)


def _relative_costs():
    """真实相对口径成本束：rel 恒 0.2/100.1（bid 100/ask 100.2），滑点=0.1×rel×price。"""
    from mt5quant.costs import (
        CommissionModel, Costs as Bundle, SlippageModel, SpreadModel, SwapModel,
        SwapRule,
    )

    rows = [(f"2026-01-05 {h:02d}:30:00", 0.2) for h in (10, 11, 12, 13)]
    df = pd.DataFrame(
        {"time_msc": [pd.Timestamp(t).value // 10**6 for t, _ in rows],
         "bid": [100.0] * 4, "ask": [100.2] * 4}
    )
    spread = SpreadModel.from_ticks("XAUUSD", df)          # 默认 relative
    slip = SlippageModel(spread, spread_fraction=0.1)
    swap = SwapModel(per_symbol={"XAUUSD": SwapRule(long_per_day=0.0,
                                                    short_per_day=0.0)})
    return Bundle(spread=spread, slippage_model=slip,
                  commission=CommissionModel(default_per_lot=0.0), swap=swap)


def test_engine_relative_costs_hand_computed_contract_size_100():
    """相对点差 × contract_size=100 的手算往返：点差随成交基准价缩放。"""
    rel = 0.2 / 100.1
    df = base4()  # bar1 开盘 100 入场，末根收盘 103 强平
    res = run_backtest(ScriptStrategy([1], fill=1), df, _relative_costs(),
                       symbol="XAUUSD", contract_size=100.0, params={}, timeframe="H1", volume_lots=1.0)
    t = one_trade(res)
    e_spr, e_slip = rel * 100.0, 0.1 * rel * 100.0        # 入场基准价=开盘 100
    x_slip = 0.1 * rel * 103.0                            # 出场基准价=收盘 103
    assert t["entry_price"] == pytest.approx(100.0 + e_spr + e_slip)
    assert t["exit_price"] == pytest.approx(103.0 - x_slip)
    assert t["gross"] == pytest.approx((103.0 - x_slip - 100.0 - e_spr - e_slip) * 100.0)
    assert t["spread_cost"] == pytest.approx(e_spr * 100.0)
    assert t["slippage_cost"] == pytest.approx((e_slip + x_slip) * 100.0)
    assert t["commission"] == 0.0 and t["swap"] == 0.0
    assert t["net"] == pytest.approx(t["gross"])
    # 手数只放大金额字段，价格字段仍是价格单位
    res2 = run_backtest(ScriptStrategy([1], fill=1), df, _relative_costs(),
                        symbol="XAUUSD", contract_size=100.0, params={},
                        volume_lots=2.0, timeframe="H1")
    t2x = one_trade(res2)
    assert t2x["entry_price"] == pytest.approx(t["entry_price"])
    assert t2x["spread_cost"] == pytest.approx(2 * t["spread_cost"])


def test_engine_relative_costs_double_price_doubles_spread_cost():
    """价格整体 ×2 → 相对模型的点差/滑点成本（价格单位）恰 ×2（R1 核心性质）。"""
    rel = 0.2 / 100.1
    df2 = base4()
    for col in ("open", "high", "low", "close"):
        df2[col] = df2[col] * 2.0
    rows = [(f"2026-01-05 {h:02d}:30:00", 0.4) for h in (10, 11, 12, 13)]
    from mt5quant.costs import (
        CommissionModel, Costs as Bundle, SlippageModel, SpreadModel, SwapModel,
        SwapRule,
    )
    ticks2 = pd.DataFrame(
        {"time_msc": [pd.Timestamp(t).value // 10**6 for t, _ in rows],
         "bid": [200.0] * 4, "ask": [200.4] * 4}          # rel 仍 = 0.2/100.1
    )
    spread2 = SpreadModel.from_ticks("XAUUSD", ticks2)
    costs2 = Bundle(spread=spread2,
                    slippage_model=SlippageModel(spread2, spread_fraction=0.1),
                    commission=CommissionModel(default_per_lot=0.0),
                    swap=SwapModel(per_symbol={"XAUUSD": SwapRule(
                        long_per_day=0.0, short_per_day=0.0)}))
    res = run_backtest(ScriptStrategy([1], fill=1), df2, costs2,
                       symbol="XAUUSD", contract_size=100.0, params={}, timeframe="H1", volume_lots=1.0)
    t2 = one_trade(res)
    assert t2["spread_cost"] == pytest.approx(rel * 200.0 * 100.0)   # 入场基准 200
    assert t2["slippage_cost"] == pytest.approx(
        (0.1 * rel * 200.0 + 0.1 * rel * 206.0) * 100.0)             # 出场基准 206


# ------------------------------------------------- T7：R7 / R8 / R9 / R16

def reentry_bars():
    """sl_tp_bars 基础上把 bar16 的 low 打到 98.0（≤ 多头 SL 98.6）。"""
    df = sl_tp_bars()
    df.loc[16, "low"] = 98.0
    return df


def test_reentry_immediate_reenters_next_bar_after_stop():
    """R7 默认（immediate）：止损后信号仍为 1 → 下一根开盘立即重入（目标仓位语义）。"""
    rules = ExitRules(stop_loss_atr=2.0)          # reentry 默认 "immediate"
    res = run_backtest(ScriptStrategy([0] * 14 + [1] * 5, fill=1, exit_rules=rules),
                       reentry_bars(), FlatCosts(), symbol="XAUUSD",
                       contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
    assert len(res.trades) == 2
    t1, t2 = res.trades.iloc[0], res.trades.iloc[1]
    assert t1["exit_reason"] == "stop" and t1["exit_time"] == res.bars["time"].iloc[16]
    assert t2["entry_time"] == res.bars["time"].iloc[17]   # bar16 止损 → bar17 开盘重入
    assert t2["exit_reason"] == "end_of_data"


def test_reentry_after_signal_change_blocks_until_signal_leaves_direction():
    """R7：after_signal_change——止损后信号仍 1 不重入；归 0（bar17）再回 1（bar18）
    后，bar19 开盘才重新入场。"""
    rules = ExitRules(stop_loss_atr=2.0, reentry="after_signal_change")
    df = reentry_bars()
    res = run_backtest(ScriptStrategy([0] * 14 + [1, 1, 1, 0, 1], fill=0,
                                      exit_rules=rules),
                       df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                       params={}, timeframe="H1", volume_lots=1.0)
    assert len(res.trades) == 2
    t1, t2 = res.trades.iloc[0], res.trades.iloc[1]
    assert t1["exit_reason"] == "stop"
    assert t2["entry_time"] == df["time"].iloc[19]
    assert t2["exit_reason"] == "end_of_data"


def test_reentry_after_signal_change_no_reentry_while_signal_holds():
    """R7：信号始终为 1 时 after_signal_change 止损后不再重入（对照 immediate 用例）。"""
    rules = ExitRules(stop_loss_atr=2.0, reentry="after_signal_change")
    res = run_backtest(ScriptStrategy([0] * 14 + [1] * 5, fill=1, exit_rules=rules),
                       reentry_bars(), FlatCosts(), symbol="XAUUSD",
                       contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["exit_reason"] == "stop"


def test_engine_rejects_invalid_reentry_value():
    """引擎侧防御：绕过 ExitRules 构造校验的鸭子类型规则也在运行前被拒。"""
    from types import SimpleNamespace
    rules = SimpleNamespace(stop_loss_atr=None, take_profit_atr=None,
                            time_stop_bars=None, week_close_exit=False,
                            reentry="sometimes")
    with pytest.raises(ValueError, match="reentry"):
        run_backtest(ScriptStrategy([1], fill=1, exit_rules=rules), base4(),
                     FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                     params={}, timeframe="H1", volume_lots=1.0)


def test_week_close_exit_rejects_timeframes_above_h1():
    """R8：week_close_exit 按 bar 收盘时刻判定，D1/H4 会提前一整根平仓 → 只许 ≤H1。"""
    rules = ExitRules(week_close_exit=True)
    for tf in ("H4", "D1"):
        df = flat_bars(pd.Timestamp("2026-01-05 10:00"), 6, tf=tf)
        with pytest.raises(ValueError, match="week_close"):
            run_backtest(ScriptStrategy([1], fill=1, exit_rules=rules), df,
                         FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                         params={}, timeframe=tf, volume_lots=1.0)
    m30 = flat_bars(pd.Timestamp("2026-01-09 17:00"), 8, tf="M30")
    run_backtest(ScriptStrategy([1], fill=1, exit_rules=rules), m30,
                 FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                 params={}, timeframe="M30", volume_lots=1.0)   # ≤H1 的周期不受影响


def test_time_stop_exit_fill_time_is_bar_close_and_swap_counts_extra_night():
    """R9：按收盘价出场的成交时刻 = bar 收盘（bar 时间 + 周期），隔夜费按
    (entry_time, exit_fill_time] 计——到期 bar 20:30（收盘 21:00）恰跨 21:00
    计费时刻，比按 bar 开盘时刻（旧口径）多计 1 夜。"""
    df = flat_bars(pd.Timestamp("2026-01-05 00:30"), 41, tf="M30")  # bar40 = 20:30
    rules = ExitRules(time_stop_bars=2)
    t = one_trade(run_backtest(ScriptStrategy([0] * 37 + [1, 1, 1], fill=0,
                                              exit_rules=rules),
                               df, ChargeHourCosts(), symbol="XAUUSD",
                               contract_size=1.0, params={}, timeframe="M30", volume_lots=1.0))
    assert t["entry_time"] == pd.Timestamp("2026-01-05 19:30")
    assert t["exit_reason"] == "time_stop"
    assert t["exit_time"] == pd.Timestamp("2026-01-05 20:30")        # bar 时间（原义）
    assert t["exit_fill_time"] == pd.Timestamp("2026-01-05 21:00")   # bar 收盘时刻
    assert t["swap"] == pytest.approx(-3.0)  # (19:30, 21:00] 含 21:00 计费时刻


def test_ruin_stops_new_entries_and_marks_result():
    """R16：权益 ≤ 0 后不再开新仓；BacktestResult.ruined / ruined_at 记首次时刻，
    标记粘滞（time_stop 出场后权益回升也不清除）。"""
    df = flat_bars(pd.Timestamp("2026-01-05 10:00"), 10)
    df.loc[1, "close"] = 95.0    # bar1 收盘浮亏 (95−100.6)×1000 = −5600 < −本金 1000
    rules = ExitRules(time_stop_bars=2)
    res = run_backtest(ScriptStrategy([1], fill=1, exit_rules=rules), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1000.0, params={},
                       initial_capital=1000.0, timeframe="H1", volume_lots=1.0)
    assert len(res.trades) == 1                    # bar3 time_stop 出场后信号仍 1，但已爆仓
    t = res.trades.iloc[0]
    assert t["exit_reason"] == "time_stop"
    assert res.ruined is True
    assert res.ruined_at == df["time"].iloc[1]     # 首次权益 ≤ 0 的 bar
    # 出场净利 −701：bar3 起权益 = 1000 − 701 = 299（回升，但 ruined 已粘滞）
    assert res.equity.iloc[-1] == pytest.approx(299.0)


# ------------------------------------------------- T20（H2）：禁入窗推迟开仓

from mt5data.sessions import SessionPolicy  # noqa: E402


def _pol_window(local_hhmm: str, tz: str = "UTC", *, pre: int = 0, post: int = 60,
                **kw) -> SessionPolicy:
    return SessionPolicy(rollover_local=local_hhmm, rollover_tz=tz,
                         pre_minutes=pre, post_minutes=post, **kw)


def test_session_policy_defers_entry_to_next_allowed_bar():
    """信号在禁入时段开仓 → 推迟到下一个允许的 bar 开盘（A-1.4）。"""
    df = base4()  # 2026-01-05 10/11/12/13 UTC
    pol = _pol_window("11:00")           # 禁入 [11:00, 12:00) UTC
    res = run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(), symbol="XAUUSD",
                       contract_size=1.0, params={}, timeframe="H1",
                       session_policy=pol, volume_lots=1.0)
    t = one_trade(res)
    assert t["entry_time"] == df["time"].iloc[2]   # bar1 禁入 → bar2（12:00）开盘
    assert t["exit_reason"] == "end_of_data"
    # 对照：无策略（None）行为不变——bar1 开盘即入
    res0 = run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(), symbol="XAUUSD",
                        contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0)
    assert one_trade(res0)["entry_time"] == df["time"].iloc[1]


def test_session_policy_defers_while_window_persists():
    # 禁入 [11:00, 14:00)：11、12、13 三根 bar 全禁 → 数据末尾前一根（14:00 允许）
    # 没有第 5 根 → 整段无交易（信号持续到末尾也没有允许的 bar）
    df = flat_bars(pd.Timestamp("2026-01-05 10:00"), 4)
    pol = _pol_window("11:00", post=180)
    res = run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(), symbol="XAUUSD",
                       contract_size=1.0, params={}, timeframe="H1",
                       session_policy=pol, volume_lots=1.0)
    assert len(res.trades) == 0


def test_session_policy_never_blocks_exit():
    """平仓不受限：禁入时段内的 signal 出场照常执行。"""
    df = base4()
    pol = _pol_window("12:00")           # bar2（12:00）在禁入窗内
    res = run_backtest(ScriptStrategy([1, 0], fill=0), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={},
                       timeframe="H1", session_policy=pol, volume_lots=1.0)
    t = one_trade(res)
    assert t["entry_time"] == df["time"].iloc[1]    # 11:00 入场（允许）
    assert t["exit_time"] == df["time"].iloc[2]     # 12:00 平仓照常（不受限）
    assert t["exit_reason"] == "signal"


def test_session_policy_reversal_closes_but_defers_new_side():
    # 反向信号落在禁入窗：先平旧仓（不受限），新仓推迟到下一个允许 bar
    df = base4()
    pol = _pol_window("12:00")
    res = run_backtest(ScriptStrategy([1, -1], fill=-1), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={},
                       timeframe="H1", session_policy=pol, volume_lots=1.0)
    assert len(res.trades) == 2
    t1, t2 = res.trades.iloc[0], res.trades.iloc[1]
    assert t1["exit_time"] == df["time"].iloc[2]    # 12:00 平多（允许）
    assert t2["entry_time"] == df["time"].iloc[3]   # 空头推迟到 13:00 开盘


def test_session_policy_dst_anchors_summer_and_winter():
    # 真实 17:00 纽约策略：夏令时 21:00 UTC 禁入、冬令时 22:00 UTC 禁入
    pol = SessionPolicy()   # 默认 17:00 America/New_York，前 5 后 60
    july = flat_bars(pd.Timestamp("2026-07-15 20:00"), 3)   # 20/21/22 UTC（EDT）
    res = run_backtest(ScriptStrategy([1], fill=1), july, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={},
                       timeframe="H1", session_policy=pol, volume_lots=1.0)
    t = one_trade(res)
    # bar0 信号 → bar1（21:00 UTC）在 [20:55, 22:00) 内禁入 → bar2（22:00）开盘
    assert t["entry_time"] == pd.Timestamp("2026-07-15 22:00")

    dec = flat_bars(pd.Timestamp("2026-12-15 21:00"), 3)    # 21/22/23 UTC（EST）
    res2 = run_backtest(ScriptStrategy([1], fill=1), dec, FlatCosts(),
                        symbol="XAUUSD", contract_size=1.0, params={},
                        timeframe="H1", session_policy=pol, volume_lots=1.0)
    t2 = one_trade(res2)
    # 22:00（冬令时换日）禁入 → 推迟到 23:00 开盘
    assert t2["entry_time"] == pd.Timestamp("2026-12-15 23:00")


# ------------------------------------------------- T23（M2）：RiskSizing 定手数

from mt5quant.sizing import RiskSizing  # noqa: E402

XAU_VOL = dict(vol_min=0.01, vol_step=0.01, vol_max=200.0)


def sizing_bars(n=30):
    """恒定波幅 H1：TR 恒 1.0 → ATR14 从 bar13 起恒 1.0（SL 距离=系数×1.0）。"""
    return flat_bars(pd.Timestamp("2026-01-05 10:00"), n)


def test_sizing_volume_lots_mutually_exclusive():
    bars = sizing_bars()
    strat = ScriptStrategy([0] * 14 + [1] * 15)
    # 同给 → 拒
    with pytest.raises(ValueError, match="二选一"):
        run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                     contract_size=100.0, timeframe="H1", volume_lots=1.0,
                     sizing=RiskSizing(risk_fraction=0.005), **XAU_VOL)
    # 同缺 → 拒（引擎不给默认手数：静默 1 手黄金首次止损即爆仓，M2）
    with pytest.raises(ValueError, match="volume_lots"):
        run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                     contract_size=100.0, timeframe="H1")
    # sizing 缺 vol 三元组 → 拒
    with pytest.raises(ValueError, match="vol_min"):
        run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                     contract_size=100.0, timeframe="H1", volume_lots=None,
                     sizing=RiskSizing(risk_fraction=0.005))
    # 零步长等非法 vol → 拒（开跑前校验，不是入场时才炸）
    with pytest.raises(ValueError, match="vol_step"):
        run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                     contract_size=100.0, timeframe="H1", volume_lots=None,
                     sizing=RiskSizing(risk_fraction=0.005),
                     vol_min=0.01, vol_step=0.0, vol_max=1.0)


def test_sizing_uses_strategy_sl_distance_plan_case():
    """计划逐条：合约 100、资金 10,000、风险 0.5%、SL 距离 5.0（ATR=1.0 ×
    stop_loss_atr=5.0）→ 0.10 手；Trade.volume_lots 如实记录。"""
    bars = sizing_bars()
    strat = ScriptStrategy([0] * 14 + [1] * 15,
                           exit_rules=ExitRules(stop_loss_atr=5.0))
    res = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1", volume_lots=None,
                       initial_capital=10_000.0,
                       sizing=RiskSizing(risk_fraction=0.005), **XAU_VOL)
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["volume_lots"] == pytest.approx(0.10)
    assert res.volume_lots is None          # 固定手数字段不冒充 sizing 结果
    assert res.skipped_zero_lots == 0


def test_sizing_fallback_stop_when_no_sl():
    """策略无 SL：风险距离 = fallback_stop_atr×ATR——fallback 2.0 的手数
    恰为 1.0 的一半（同一资金同一 ATR）。"""
    bars = sizing_bars()
    sig = [0] * 14 + [1] * 15

    def run(fallback):
        strat = ScriptStrategy(sig)          # ExitRules()：无 SL/TP
        return run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD",
                            params={}, contract_size=100.0, timeframe="H1",
                            volume_lots=None, initial_capital=10_000.0,
                            sizing=RiskSizing(risk_fraction=0.005,
                                              fallback_stop_atr=fallback),
                            **XAU_VOL)

    lots1 = run(1.0).trades.iloc[0]["volume_lots"]
    lots2 = run(2.0).trades.iloc[0]["volume_lots"]
    assert lots1 == pytest.approx(0.50)      # 50 / (1.0×100) = 0.50
    assert lots2 == pytest.approx(0.25)      # 50 / (2.0×100) = 0.25


def test_sizing_skips_entry_when_atr_unavailable():
    """ATR 回看不足（入场太早）→ 无风险距离 → 手数 0：跳过并计数；
    信号持续到 ATR 可得后照常入场（逐 bar 重试）。"""
    bars = sizing_bars(30)
    strat = ScriptStrategy([1], fill=1)      # bar0 信号 → bar1 就想入场
    res = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1", volume_lots=None,
                       initial_capital=10_000.0,
                       sizing=RiskSizing(risk_fraction=0.005), **XAU_VOL)
    # bar1..14 共 14 次尝试因 ATR 不可得跳过（sig_bar=0..13；bar13 起有 ATR，
    # 即 bar14 入场）→ 之后成交 1 笔
    assert res.skipped_zero_lots == 13       # sig_bar 0..12 的 13 次尝试
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["entry_time"] == pd.Timestamp("2026-01-06 00:00")


# --------------------------------------------- T82：隔夜费相对价格基准

class SwapRecordingCosts(FlatCosts):
    """记录 overnight_fee 的 price 传参（T82 出场侧 bar 价断言用）。"""

    def __init__(self):
        super().__init__()
        self.swap_calls: list[float | None] = []

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        self.swap_calls.append(price)
        return super().overnight_fee(symbol, side, from_ts, to_ts)


def test_t82_engine_passes_exit_side_price_to_swap():
    """T82 失败测试：引擎调 overnight_fee 必须带 price=出场侧成交基准价
    （end_of_data 出场 = 末根收盘 103；相对口径模型缺 price 即 fail-fast，
    引擎接线是 price 的唯一供给了）。"""
    rec = SwapRecordingCosts()
    run_backtest(ScriptStrategy([1], fill=1), base4(), rec,
                 symbol="XAUUSD", contract_size=1.0, params={},
                 timeframe="H1", volume_lots=1.0)
    assert rec.swap_calls == [103.0]   # 出场侧基准价（末根收盘强平）


class EarlyPriceSwapCosts(FlatCosts):
    """T82：点数模式相对口径隔夜费（USTEC 快照费率 -6.338/手/夜、ref=25097）。"""

    def __init__(self):
        super().__init__(commission=0.0)
        from mt5quant.costs.swap import SwapModel, SwapRule
        self._swap = SwapModel(per_symbol={"USTEC": SwapRule(
            long_per_day=-633.8 * 0.01 * 1.0, short_per_day=0.0,
            triple_weekdays=frozenset({4}),          # USTEC 三倍日=周五
            ref_price=25097.0, contract_size=1.0)})

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        return self._swap.overnight_fee(symbol, side, from_ts, to_ts,
                                        price=price)


def test_t82_early_1986_prices_equity_not_ruined():
    """T82 失败测试（T65 §5.4 场景复算）：131 价位（1986 年 USTEC）21 根 D1
    持仓、100 手——旧绝对口径 swap = 6.338×20 夜×100 ≈ −1.27 万（1 万本金
    权益爆掉）；相对口径每夜 ≈ −2.5bp 名义额，权益不再爆炸。"""
    from mt5quant.costs.swap import charge_nights

    df = flat_bars(pd.Timestamp("2026-01-05"), 21, tf="D1",
                   openc=131.0, high=131.5, low=130.5, close=131.0)
    costs = EarlyPriceSwapCosts()
    res = run_backtest(ScriptStrategy([1], fill=1), df, costs,
                       symbol="USTEC", contract_size=1.0, params={},
                       timeframe="D1", volume_lots=100.0,
                       initial_capital=10_000.0)
    assert not res.ruined
    t = one_trade(res)
    entry = df["time"].iloc[1]
    exit_fill = df["time"].iloc[20] + pd.Timedelta(days=1)   # D1 收盘时刻
    nights = charge_nights(entry, exit_fill, costs._swap.rule("USTEC"))
    # 相对口径：swap = 6.338 × (131/25097) × 夜数 × 100 手（每夜 ≈ −2.5bp）
    assert t["swap"] == pytest.approx(
        -6.338 * (131.0 / 25097.0) * nights * 100.0, rel=1e-9)
    # 量级对照：旧绝对口径同窗口是 6.338×夜数×100——约为新口径的
    # 25097/131 ≈ 192 倍（T65 实测 6 笔爆仓的根因）
    old_abs = 6.338 * nights * 100.0
    assert old_abs / abs(t["swap"]) == pytest.approx(25097.0 / 131.0, rel=1e-9)
    assert old_abs > 10_000.0            # 旧口径足以爆掉 1 万本金
    assert res.equity.min() > 9_000.0    # 新口径权益不爆
