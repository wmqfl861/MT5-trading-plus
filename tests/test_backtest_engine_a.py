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

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        f, t = pd.Timestamp(from_ts), pd.Timestamp(to_ts)
        if t <= f:
            return 0.0
        days = (t.normalize() - f.normalize()).days  # (f,t] 内午夜数（t 恰为午夜也计）
        return self.overnight * days


class ChargeHourCosts(FlatCosts):
    """按 21:00 计费时刻数夜（I2 回归）：每夜 −3.0。"""

    def overnight_fee(self, symbol, side, from_ts, to_ts):
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
                       symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")
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
        res = run_backtest(BuyHold(), df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                           params={}, volume_lots=lots, timeframe="H1")
        t = one_trade(res)
        assert t["net"] == pytest.approx((exit_ - entry) * lots - comm)


def test_short_round_trip_hand_computed():
    df = base4()
    res = run_backtest(ScriptStrategy([-1], fill=-1), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")
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
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1"))
    tb = one_trade(run_backtest(ScriptStrategy([1], fill=1), b, FlatCosts(),
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1"))
    assert ta["entry_price"] == tb["entry_price"]
    assert ta["entry_time"] == a["time"].iloc[1] == tb["entry_time"]


def test_later_signal_change_does_not_affect_earlier_entry():
    df = base4()
    t1 = one_trade(run_backtest(ScriptStrategy([1, 0], fill=0), df, FlatCosts(),
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1"))
    t2 = one_trade(run_backtest(ScriptStrategy([1, 1], fill=1), df, FlatCosts(),
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1"))
    assert t1["entry_price"] == t2["entry_price"]
    assert t1["exit_reason"] == "signal" and t1["exit_time"] == df["time"].iloc[2]


def test_execution_uses_execution_bar_hour():
    df = base4()  # 执行 bar 为 11:00 UTC
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, HourlySlipCosts(),
                               symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1"))
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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
                               timeframe="H1"))
    assert t["exit_reason"] == "take_profit"
    assert t["exit_price"] == pytest.approx(100.6 + 2.0 - SLIP)


def test_stop_loss_single_touch():
    df = sl_tp_bars()
    df.loc[19, "low"] = 98.0    # 只触 SL
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=5.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
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
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={},
                               timeframe="H1"))
    assert t["exit_reason"] == "week_close"
    assert t["exit_time"] == df["time"].iloc[3]  # 20:00 UTC 那根
    assert t["exit_price"] == pytest.approx(102.0 - SLIP)


def test_without_week_close_holds_over_weekend():
    df = week_bars("2026-01-09")
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, FlatCosts(),
                               symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1"))
    assert t["exit_reason"] == "end_of_data"
    assert t["exit_time"] == df["time"].iloc[4]  # 次周一 00:00 那根
    assert t["swap"] == pytest.approx(4 * ONIGHT)  # 周五19:00→(次日)周二00:00：跨 4 个午夜（模型整段计夜）


# ------------------------------------------------- 隔夜费

def test_overnight_fee_charged_when_crossing_utc_date():
    df = flat_bars(pd.Timestamp("2026-01-05 22:00"), 4)  # 22,23,00,01 点
    res = run_backtest(ScriptStrategy([1, 1, 1, 0]), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")
    t = one_trade(res)
    assert t["entry_time"] == df["time"].iloc[1]  # 23:00 入场
    assert t["exit_time"] == df["time"].iloc[3]   # 次日 01:00 出场
    assert t["swap"] == pytest.approx(ONIGHT)
    assert t["net"] == pytest.approx(t["gross"] - COMM + ONIGHT)


# ------------------------------------------------- 同 bar 反向：先平后开

def test_reversal_closes_then_opens_at_same_bar_open():
    df = base4()
    res = run_backtest(ScriptStrategy([1, -1], fill=-1), df, FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")
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
                       symbol="XAUUSD", contract_size=1.0, params={}, initial_capital=10_000.0,
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
    res = run_backtest(BuyHold(), df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
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
                     symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")


def test_rejects_non_monotonic_bars():
    df = base4().iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError, match="升序"):
        run_backtest(BuyHold(), df, FlatCosts(),
                     symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")


def test_rejects_unknown_params():
    df = base4()
    with pytest.raises(ValueError, match="未知参数"):
        run_backtest(BuyHold(), df, FlatCosts(),
                     symbol="XAUUSD", contract_size=1.0, params={"k": 1}, timeframe="H1")


def test_rejects_invalid_exit_rules():
    df = base4()
    with pytest.raises(ValueError, match="time_stop_bars"):
        run_backtest(ScriptStrategy([1], fill=1,
                                    exit_rules=ExitRules(time_stop_bars=0)),
                     df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")
    with pytest.raises(ValueError, match="stop_loss_atr"):
        run_backtest(ScriptStrategy([1], fill=1,
                                    exit_rules=ExitRules(stop_loss_atr=-1.0)),
                     df, FlatCosts(), symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")


def test_overnight_fee_counts_intraday_charge_hour_crossing():
    """I2 回归：00:30 起持仓到当日 22:00——不跨 UTC 日界但跨 21:00 计费时刻，
    必须记 1 夜（旧实现只在跨日界时才调用模型，恒漏计日内跨计费时刻）。"""
    df = flat_bars(pd.Timestamp("2026-01-05 00:30"), 44, tf="M30")  # 00:30…22:00
    t = one_trade(run_backtest(ScriptStrategy([1], fill=1), df, ChargeHourCosts(),
                               symbol="XAUUSD", contract_size=1.0, params={}, timeframe="M30"))
    assert t["entry_time"] == pd.Timestamp("2026-01-05 01:00")
    assert t["exit_time"] == pd.Timestamp("2026-01-05 22:00")  # 末根强平
    assert t["swap"] == pytest.approx(-3.0)                    # 恰跨 21:00 一次

    ctrl = flat_bars(pd.Timestamp("2026-01-05 00:30"), 24, tf="M30")  # 00:30…12:00 不跨
    t2 = one_trade(run_backtest(ScriptStrategy([1], fill=1), ctrl, ChargeHourCosts(),
                                symbol="XAUUSD", contract_size=1.0, params={}, timeframe="M30"))
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
                     params={}, timeframe="H1")


def test_rejects_invalid_contract_size_and_volume():
    for bad in (0, -100.0, float("nan"), float("inf"), True, "100"):
        with pytest.raises(ValueError, match="contract_size"):
            run_backtest(BuyHold(), base4(), FlatCosts(), symbol="XAUUSD",
                         contract_size=bad, params={}, timeframe="H1")
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
                       initial_capital=10_000.0, timeframe="H1")
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
                               params={}, timeframe="H1"))
    assert t["gross"] == pytest.approx((98.5 - 100.6) * 100)
    assert t["r_multiple"] == pytest.approx(t["net"] / (2.0 * 1.0 * 100))


def test_long_stop_gap_fills_at_open_not_stop_price():
    # 入场 bar19 开盘 100.6，SL=98.6；bar20 开盘直接跳到 95（周末/数据跳空）
    df = gap_bars(95.0, 95.5, 94.5, 95.0)
    rules = ExitRules(stop_loss_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], fill=1, exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1"))
    assert t["exit_reason"] == "stop_gap"
    assert t["exit_price"] == pytest.approx(95.0 - SLIP)    # 按开盘价，不是 98.5
    assert t["bars_held"] == 1


def test_long_take_profit_gap_fills_at_tp_conservatively():
    df = gap_bars(104.0, 104.5, 103.5, 104.0)               # TP=102.6，开盘已越过
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [1], fill=1, exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1"))
    assert t["exit_reason"] == "take_profit_gap"
    assert t["exit_price"] == pytest.approx(102.6 - SLIP)


def test_short_stop_triggers_on_ask():
    # 空头入场 bid 99.9，SL=101.9；bar20 bid 高点 101.6 未到 SL，但 Ask=102.1 已到
    df = gap_bars(100.0, 101.6, 99.5, 100.0)
    rules = ExitRules(stop_loss_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1"))
    assert t["entry_price"] == pytest.approx(100.0 - SLIP)
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(101.9 + SLIP)   # 价位即 Ask 口径，+slip


def test_short_take_profit_needs_ask_to_reach_level():
    rules = ExitRules(stop_loss_atr=2.0, take_profit_atr=2.0)  # TP=97.9（Ask 口径）
    miss = gap_bars(100.0, 100.5, 97.5, 100.0)   # bid 低点 97.5，Ask 低点 98.0 > 97.9
    t1 = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                                miss, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                                params={}, timeframe="H1"))
    assert t1["exit_reason"] == "end_of_data"
    hit = gap_bars(100.0, 100.5, 97.3, 100.0)    # Ask 低点 97.8 ≤ 97.9
    t2 = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                                hit, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                                params={}, timeframe="H1"))
    assert t2["exit_reason"] == "take_profit"
    assert t2["exit_price"] == pytest.approx(97.9 + SLIP)


def test_short_stop_gap_fills_at_open_ask():
    df = gap_bars(103.0, 103.5, 102.5, 103.0)    # 开盘 Ask=103.5 ≥ SL 101.9
    rules = ExitRules(stop_loss_atr=2.0)
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                               df, FlatCosts(), symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1"))
    assert t["exit_reason"] == "stop_gap"
    assert t["exit_price"] == pytest.approx(103.0 + HALF + SLIP)


def test_equity_marks_short_at_ask():
    res = run_backtest(ScriptStrategy([-1], fill=-1), base4(), FlatCosts(),
                       symbol="XAUUSD", contract_size=1.0, params={},
                       initial_capital=10_000.0, timeframe="H1")
    # 入场 99.9；bar1/2 收盘 Ask=100.5 → 浮亏 0.6；末根平仓 103.6 → 净 −3.7−1.0
    assert list(res.equity) == pytest.approx([10_000.0, 9_999.4, 9_999.4, 9_995.3])


# ------------------------------------------------- 成本接口 v2（R1 / 计划 T3）

def test_engine_version_bumped_for_cost_interface_v2():
    assert ENGINE_VERSION == "A-1.2"


def test_engine_passes_price_to_cost_models_long_fills():
    """多头：入场 spr/slip 的 price=执行 bar 开盘；平仓 price=该根收盘。"""
    df = base4()  # bar1 11:00 开盘入场（open=100），末根 13:00 收盘 103 强平
    rec = RecordingCosts()
    run_backtest(ScriptStrategy([1], fill=1), df, rec,
                 symbol="XAUUSD", contract_size=1.0, params={}, timeframe="H1")
    # 多头估值按 Bid（close），不调点差模型 → 调用序列完全确定
    assert rec.calls == [("spread", 11, 100.0), ("slip", 11, 100.0),
                         ("spread", 13, 103.0), ("slip", 13, 103.0)]


def test_engine_passes_bar_prices_for_short_ask_trigger_and_marking():
    """空头：Ask 触发判断 price=当根 high/low（跳空用 open）；逐 bar 估值 price=close。"""
    df = gap_bars(100.0, 101.6, 99.5, 100.0)
    df.loc[19, "close"] = 100.4      # 与 open=100 区分，验证估值基准是 close
    rules = ExitRules(stop_loss_atr=2.0)
    rec = RecordingCosts()
    t = one_trade(run_backtest(ScriptStrategy([0] * 18 + [-1], fill=-1, exit_rules=rules),
                               df, rec, symbol="XAUUSD", contract_size=1.0,
                               params={}, timeframe="H1"))
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
                       symbol="XAUUSD", contract_size=100.0, params={}, timeframe="H1")
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
                       symbol="XAUUSD", contract_size=100.0, params={}, timeframe="H1")
    t2 = one_trade(res)
    assert t2["spread_cost"] == pytest.approx(rel * 200.0 * 100.0)   # 入场基准 200
    assert t2["slippage_cost"] == pytest.approx(
        (0.1 * rel * 200.0 + 0.1 * rel * 206.0) * 100.0)             # 出场基准 206
