"""B 层 tick 引擎测试：全部手造 tick 序列逐项手算对账。

佣金 1.0/手、隔夜费每跨一个 UTC 午夜 −2.0/手；A/B 对照用例里
A 层点差模型全幅 S_MODEL=0.5、滑点 0.1，与 tick 实际点差对齐或故意错开。
"""
from datetime import timedelta

import pandas as pd
import pytest

from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
from mt5quant.strategies import BaseStrategy, BuyHold, ExitRules
from mt5quant.backtest.engine_a import run_backtest
from mt5quant.backtest.engine_b import (
    TickCoverageError,
    run_backtest_ticks,
)

COMM, ONIGHT = 1.0, -2.0
S_MODEL, SLIP_MODEL = 0.5, 0.1


class FakeCosts:
    """A/B 通吃：spread_p90/slippage 供 A 层调用（带 price= 兼容成本接口 v2），
    B 层只消费 commission_per_lot / overnight_fee。"""

    def __init__(self, half=S_MODEL, slip=SLIP_MODEL, commission=COMM,
                 overnight=ONIGHT):
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
        return self.overnight * (t.normalize() - f.normalize()).days


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


def flat_bars(start, n, tf="H1", *, openc=100.0, high=100.5, low=99.5, close=100.0):
    secs = TIMEFRAME_SECONDS[tf]
    times = [pd.Timestamp(start) + timedelta(seconds=secs * i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


def bars_at(times, **kw):
    """按给定时间列表建 flat bars（跨周末/跨月场景）。"""
    return pd.DataFrame({
        "time": pd.to_datetime([pd.Timestamp(t) for t in times]),
        "open": kw.get("openc", 100.0), "high": kw.get("high", 100.5),
        "low": kw.get("low", 99.5), "close": kw.get("close", 100.0),
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


def base4():
    """4 根 H1（周一 2026-01-05 10:00 起），末根收盘 103。"""
    df = flat_bars(pd.Timestamp("2026-01-05 10:00"), 4)
    df.loc[3, "close"] = 103.0
    return df


def sl_tp_bars():
    """20 根恒定波幅 H1：TR 恒 1.0 → ATR14 从 bar13 起恒 1.0。"""
    return flat_bars(pd.Timestamp("2026-01-05 00:00"), 20)


def msc(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def tick(ts, bid, ask):
    return (msc(ts), float(bid), float(ask))


def make_loader(ticks):
    """记录调用窗口的假 ticks_loader（契约同 mt5data.ticks.load_ticks 区间参数）。"""
    calls = []

    def load(start_msc=None, end_msc=None):
        calls.append((start_msc, end_msc))
        rows = [t for t in ticks
                if (start_msc is None or t[0] >= start_msc)
                and (end_msc is None or t[0] <= end_msc)]
        return pd.DataFrame(rows, columns=["time_msc", "bid", "ask"])

    load.calls = calls
    return load


def run_b(bars, ticks, strategy=None, costs=None, **kw):
    kw.setdefault("symbol", "XAUUSD")
    kw.setdefault("params", {})
    kw.setdefault("contract_size", 1.0)
    kw.setdefault("timeframe", "H1")
    kw.setdefault("volume_lots", 1.0)   # T23：引擎不再给默认手数（二选一）
    return run_backtest_ticks(strategy or BuyHold(), bars, make_loader(ticks),
                               costs or FakeCosts(), **kw)


def one_trade(result):
    assert len(result.trades) == 1
    return result.trades.iloc[0]


def ts(text) -> pd.Timestamp:
    return pd.Timestamp(text)


# --------------------------------------------- 入场：信号 bar 收盘后第一笔 tick

def test_entry_fills_at_first_tick_ask_after_signal_close():
    df = base4()  # bar0 10:00 收盘于 11:00；10:30 的 tick 早于收盘，绝不可成交
    ticks = [
        tick("2026-01-05 10:30:00", 99.0, 99.2),          # 收盘前：忽略
        tick("2026-01-05 11:00:00.500", 99.9, 100.4),     # 第一笔可用 tick
        tick("2026-01-05 11:20:00", 99.7, 100.1),
        tick("2026-01-05 11:45:00", 99.6, 100.0),
        tick("2026-01-05 12:10:00", 99.5, 99.9),
        tick("2026-01-05 12:59:59.999", 100.0, 100.4),
        tick("2026-01-05 13:30:00", 102.0, 102.4),
        tick("2026-01-05 13:59:59.999", 103.0, 103.2),    # 末根收盘前最后一笔
    ]
    t = one_trade(run_b(df, ticks, ScriptStrategy([1], fill=1)))
    assert t["side"] == "long"
    assert t["entry_price"] == pytest.approx(100.4)   # 该 tick 的 ask
    assert t["entry_time"] == ts("2026-01-05 11:00:00.500")
    assert t["exit_reason"] == "end_of_data"
    assert t["exit_price"] == pytest.approx(103.0)    # 收盘前最后一笔 tick 的 bid
    assert t["exit_time"] == ts("2026-01-05 13:59:59.999")
    assert t["bars_held"] == 2                         # 入场 bar1、出场 bar3
    assert t["gross"] == pytest.approx((103.0 - 100.4))
    assert t["commission"] == pytest.approx(COMM)
    assert t["swap"] == pytest.approx(0.0)             # 同日不跨午夜
    assert t["net"] == pytest.approx(2.6 - COMM)
    assert t["spread_cost"] == pytest.approx(0.5)      # 入场 tick 的 ask−bid
    assert t["slippage_cost"] == pytest.approx(0.0)


def test_slippage_price_is_adverse_on_both_legs():
    df = base4()
    ticks = [
        tick("2026-01-05 11:00:00.500", 99.9, 100.4),
        tick("2026-01-05 11:59:59.999", 100.0, 100.4),
        tick("2026-01-05 12:59:59.999", 100.0, 100.4),
        tick("2026-01-05 13:59:59.999", 103.0, 103.2),
    ]
    t = one_trade(run_b(df, ticks, ScriptStrategy([1], fill=1),
                        slippage_price=0.2))
    assert t["entry_price"] == pytest.approx(100.4 + 0.2)  # 买入多付
    assert t["exit_price"] == pytest.approx(103.0 - 0.2)   # 卖出少收
    assert t["slippage_cost"] == pytest.approx(0.4)


def test_money_fields_are_account_currency_contract_size_100():
    """XAUUSD 式合约：价差 0.5、滑点 0.2、2.6 价差毛利 → 手算美元数。"""
    df = base4()
    ticks = [
        tick("2026-01-05 11:00:00.500", 99.9, 100.4),
        tick("2026-01-05 11:59:59.999", 100.0, 100.4),
        tick("2026-01-05 12:59:59.999", 100.0, 100.4),
        tick("2026-01-05 13:59:59.999", 103.0, 103.2),
    ]
    t = one_trade(run_b(df, ticks, ScriptStrategy([1], fill=1),
                        slippage_price=0.2, contract_size=100.0,
                        volume_lots=2.0))
    assert t["entry_price"] == pytest.approx(100.6)
    assert t["exit_price"] == pytest.approx(102.8)
    assert t["gross"] == pytest.approx((102.8 - 100.6) * 2.0 * 100.0)  # 440
    assert t["commission"] == pytest.approx(2.0)
    assert t["net"] == pytest.approx(440.0 - 2.0)                      # 438
    assert t["spread_cost"] == pytest.approx(0.5 * 2.0 * 100.0)
    assert t["slippage_cost"] == pytest.approx(0.4 * 2.0 * 100.0)


# --------------------------------------------- 空头止损由 Ask 触发

def test_short_stop_triggers_on_ask():
    df = sl_tp_bars()
    ticks_hit = [
        tick("2026-01-05 19:00:00.001", 100.0, 100.1),  # 入场：bid 100.0
        tick("2026-01-05 19:30:00", 101.5, 102.3),      # bid<SL 但 ask≥SL
        tick("2026-01-05 19:59:59.999", 101.0, 101.1),
    ]
    rules = ExitRules(stop_loss_atr=2.0)                 # SL = 100 + 2×ATR1 = 102
    t = one_trade(run_b(df, ticks_hit, ScriptStrategy([0] * 18 + [-1], fill=-1,
                                                      exit_rules=rules)))
    assert t["side"] == "short"
    assert t["entry_price"] == pytest.approx(100.0)
    assert t["exit_reason"] == "stop"
    assert t["exit_time"] == ts("2026-01-05 19:30:00")
    assert t["exit_price"] == pytest.approx(102.3)       # 触发 tick 的 ask

    # 对照：ask 峰值 101.9 < 102 → 不触发，末根收盘前最后一笔 tick 强平
    ticks_miss = [
        tick("2026-01-05 19:00:00.001", 100.0, 100.1),
        tick("2026-01-05 19:30:00", 101.5, 101.9),
        tick("2026-01-05 19:59:59.999", 101.0, 101.1),
    ]
    t2 = one_trade(run_b(df, ticks_miss, ScriptStrategy([0] * 18 + [-1], fill=-1,
                                                        exit_rules=rules)))
    assert t2["exit_reason"] == "end_of_data"
    assert t2["exit_price"] == pytest.approx(101.1)      # 空头平仓按 ask


# --------------------------------------------- tick 级跳空按触发 tick 成交

def test_tick_gap_fills_at_trigger_tick_price():
    df = sl_tp_bars()
    rules = ExitRules(stop_loss_atr=2.0)  # 入场 100.1（ask）→ SL = 98.1
    ticks = [
        tick("2026-01-05 19:00:00.001", 100.0, 100.1),
        tick("2026-01-05 19:10:00", 95.0, 95.2),   # 跳空直穿 SL
    ]
    t = one_trade(run_b(df, ticks, ScriptStrategy([0] * 18 + [1], fill=1,
                                                  exit_rules=rules)))
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(95.0)   # 触发 tick 的 bid，不是 98.1
    assert t["gross"] == pytest.approx(95.0 - 100.1)

    # 跳空越过 TP：按触发 tick 的更好价格成交（真实可达价，不保守截断）
    tp_rules = ExitRules(take_profit_atr=2.0)       # TP = 102.1
    ticks_tp = [
        tick("2026-01-05 19:00:00.001", 100.0, 100.1),
        tick("2026-01-05 19:10:00", 103.0, 103.2),
    ]
    t2 = one_trade(run_b(df, ticks_tp, ScriptStrategy([0] * 18 + [1], fill=1,
                                                      exit_rules=tp_rules)))
    assert t2["exit_reason"] == "take_profit"
    assert t2["exit_price"] == pytest.approx(103.0)  # 优于 TP 价 102.1


# --------------------------------------------- latency_ms 推迟到下一笔 tick

def test_latency_defers_fill_to_next_tick():
    df = base4()
    ticks = [
        tick("2026-01-05 11:00:00.000", 99.0, 99.5),    # 恰在收盘时刻
        tick("2026-01-05 11:00:02.000", 99.8, 100.3),
        tick("2026-01-05 11:00:12.000", 100.1, 100.6),
        tick("2026-01-05 11:30:00", 100.0, 100.5),
        tick("2026-01-05 12:59:59.999", 100.0, 100.4),
        tick("2026-01-05 13:59:59.999", 103.0, 103.2),
    ]
    strat = ScriptStrategy([1], fill=1)
    # latency 0：恰在收盘时刻的 tick 即第一笔（含边界）
    t0 = one_trade(run_b(df, ticks, strat, latency_ms=0))
    assert t0["entry_time"] == ts("2026-01-05 11:00:00.000")
    assert t0["entry_price"] == pytest.approx(99.5)
    # latency 2s：就绪时刻恰等于某 tick 时刻 → 该 tick 成交（含边界）
    t2 = one_trade(run_b(df, ticks, strat, latency_ms=2_000))
    assert t2["entry_time"] == ts("2026-01-05 11:00:02.000")
    assert t2["entry_price"] == pytest.approx(100.3)
    # latency 6s：跳过 11:00:02，推迟到下一笔
    t6 = one_trade(run_b(df, ticks, strat, latency_ms=6_000))
    assert t6["entry_time"] == ts("2026-01-05 11:00:12.000")
    assert t6["entry_price"] == pytest.approx(100.6)


# --------------------------------------------- tick 覆盖不足报错

def test_coverage_error_reports_interval_and_loaded_range():
    df = base4()
    ticks = [  # bar3 区间 [13:00, 14:00) 一笔 tick 都没有
        tick("2026-01-05 11:00:00.001", 99.9, 100.4),
        tick("2026-01-05 12:15:00", 100.0, 100.5),
    ]
    with pytest.raises(TickCoverageError) as ei:
        run_b(df, ticks, ScriptStrategy([1], fill=1))
    msg = str(ei.value)
    assert "2026-01-05 13:00" in msg        # 缺口区间
    assert "2026-01-05 11:00:00.001" in msg  # 已加载覆盖范围（首）
    assert "2026-01-05 12:15:00" in msg      # 已加载覆盖范围（末）


def test_coverage_error_when_no_ticks_loaded():
    def none_loader(start_msc=None, end_msc=None):
        return None

    with pytest.raises(TickCoverageError, match="覆盖"):
        run_backtest_ticks(BuyHold(), base4(), none_loader, FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=1.0,
                           timeframe="H1", volume_lots=1.0)


def test_coverage_error_pending_order_in_empty_interval():
    df = base4()
    ticks = [  # bar2 收盘（13:00）产生信号，bar3 区间空 → 订单无法成交
        tick("2026-01-05 11:00:00.001", 99.9, 100.4),
        tick("2026-01-05 11:59:59.999", 100.0, 100.4),
        tick("2026-01-05 12:59:59.999", 100.0, 100.4),
    ]
    with pytest.raises(TickCoverageError, match="13:00"):
        run_b(df, ticks, ScriptStrategy([0, 0, 1], fill=0))


def test_coverage_error_order_ready_beyond_data_end():
    df = base4()
    ticks = [
        tick("2026-01-05 11:00:00.001", 99.9, 100.4),
        tick("2026-01-05 11:59:59.999", 100.0, 100.4),
        tick("2026-01-05 12:59:59.999", 100.0, 100.4),
        tick("2026-01-05 13:59:59.999", 103.0, 103.2),
    ]
    # latency 5h：订单就绪时刻 16:00 超出数据末尾（14:00），无法成交
    with pytest.raises(TickCoverageError, match="就绪"):
        run_b(df, ticks, ScriptStrategy([1], fill=1), latency_ms=5 * 3600 * 1000)


# --------------------------------------------- A/B 对照：tick 复现 bar OHLC

def ohlc_ticks(bars, spread, tf="H1"):
    """每根 bar 四笔 tick 恰好复现其 OHLC（bid=价、ask=bid+spread）。"""
    secs = TIMEFRAME_SECONDS[tf]
    out = []
    for _, row in bars.iterrows():
        t = pd.Timestamp(row["time"])
        out.append(tick(t, row["open"], row["open"] + spread))
        out.append(tick(t + pd.Timedelta(minutes=20), row["high"],
                        row["high"] + spread))
        out.append(tick(t + pd.Timedelta(minutes=40), row["low"],
                        row["low"] + spread))
        out.append(tick(t + pd.Timedelta(seconds=secs, milliseconds=-1),
                        row["close"], row["close"] + spread))
    return out


def test_ab_parity_ohlc_reproducing_ticks():
    df = base4()
    costs = FakeCosts(half=S_MODEL, slip=SLIP_MODEL)
    strat = ScriptStrategy([1], fill=1)
    res_a = run_backtest(strat, df, costs, symbol="XAUUSD", contract_size=1.0,
                         params={}, timeframe="H1", volume_lots=1.0)
    res_b = run_backtest_ticks(strat, df, make_loader(ohlc_ticks(df, S_MODEL)),
                               costs, symbol="XAUUSD", params={},
                               contract_size=1.0, timeframe="H1",
                               slippage_price=SLIP_MODEL, volume_lots=1.0)
    ta, tb = one_trade(res_a), one_trade(res_b)
    assert len(res_a.trades) == len(res_b.trades) == 1
    assert ta["side"] == tb["side"] == "long"
    assert ta["entry_time"] == tb["entry_time"]          # 信号类成交时间完全一致
    assert tb["entry_time"] == ts("2026-01-05 11:00:00")  # 即下一根 bar 开盘 tick
    assert ta["entry_price"] == pytest.approx(tb["entry_price"])   # 100.6
    assert ta["exit_price"] == pytest.approx(tb["exit_price"])     # 102.9
    assert ta["exit_reason"] == tb["exit_reason"] == "end_of_data"
    assert ta["net"] == pytest.approx(tb["net"])
    assert ta["commission"] == pytest.approx(tb["commission"])
    assert ta["swap"] == pytest.approx(tb["swap"])
    assert ta["bars_held"] == tb["bars_held"] == 2
    # 收盘价类出场：B 层在收盘前最后一笔 tick 成交，比 A 层的 bar 标签早 1ms 于收盘
    assert tb["exit_time"] - ta["exit_time"] == (
        pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1))
    assert list(res_a.equity) == pytest.approx(list(res_b.equity))


def test_ab_price_diff_only_from_spread_modeling():
    """tick 实际点差 0.7 vs A 层模型 0.5：价差只差在多头入场腿，其余逐项相等。"""
    df = base4()
    costs = FakeCosts(half=S_MODEL, slip=SLIP_MODEL)
    strat = ScriptStrategy([1], fill=1)
    ta = one_trade(run_backtest(strat, df, costs, symbol="XAUUSD",
                                contract_size=1.0, params={}, timeframe="H1", volume_lots=1.0))
    tb = one_trade(run_backtest_ticks(strat, df, make_loader(ohlc_ticks(df, 0.7)),
                                      costs, symbol="XAUUSD", params={},
                                      contract_size=1.0, timeframe="H1",
                                      slippage_price=SLIP_MODEL, volume_lots=1.0))
    assert tb["entry_price"] - ta["entry_price"] == pytest.approx(0.2)  # 0.7−0.5
    assert tb["exit_price"] == pytest.approx(ta["exit_price"])          # bid 腿不受影响
    assert tb["net"] - ta["net"] == pytest.approx(-0.2)
    assert tb["spread_cost"] - ta["spread_cost"] == pytest.approx(0.2)


# --------------------------------------------- 按月流式读取

def test_monthly_streaming_windows():
    bars = bars_at(["2026-01-31 22:00", "2026-01-31 23:00",
                    "2026-02-01 00:00", "2026-02-01 01:00"])
    bars.loc[3, "close"] = 103.0
    ticks = [
        tick("2026-01-31 23:00:00.001", 100.0, 100.5),
        tick("2026-01-31 23:59:59.999", 100.0, 100.5),
        tick("2026-02-01 00:00:00.001", 100.0, 100.5),
        tick("2026-02-01 01:59:59.999", 103.0, 103.2),
    ]
    loader = make_loader(ticks)
    t = one_trade(run_backtest_ticks(BuyHold(), bars, loader, FakeCosts(),
                                     symbol="XAUUSD", params={},
                                     contract_size=1.0, timeframe="H1", volume_lots=1.0))
    # 请求区间 [bar0 收盘 23:00, 末根收盘前 01:59:59.999]，按月切两个窗口
    assert loader.calls == [
        (msc("2026-01-31 23:00:00"), msc("2026-01-31 23:59:59.999")),
        (msc("2026-02-01 00:00:00"), msc("2026-02-01 01:59:59.999")),
    ]
    assert t["entry_price"] == pytest.approx(100.5)
    assert t["entry_time"] == ts("2026-01-31 23:00:00.001")
    assert t["exit_price"] == pytest.approx(103.0)
    assert t["exit_time"] == ts("2026-02-01 01:59:59.999")


# --------------------------------------------- 隔夜费 × 手数

def test_swap_scales_with_lots():
    bars = bars_at(["2026-01-05 22:00", "2026-01-05 23:00",
                    "2026-01-06 00:00", "2026-01-06 01:00"])
    bars.loc[3, "close"] = 103.0
    ticks = [
        tick("2026-01-05 23:00:00.001", 100.0, 100.5),
        tick("2026-01-05 23:30:00", 100.0, 100.5),
        tick("2026-01-06 00:30:00", 100.0, 100.5),
        tick("2026-01-06 01:30:00", 100.0, 100.5),
        tick("2026-01-06 01:59:59.999", 103.0, 103.5),
    ]
    t = one_trade(run_b(bars, ticks, BuyHold(), volume_lots=3.0))
    assert t["entry_time"] == ts("2026-01-05 23:00:00.001")
    assert t["exit_time"] == ts("2026-01-06 01:59:59.999")
    assert t["swap"] == pytest.approx(ONIGHT * 3.0)     # 跨 1 个午夜 × 3 手
    assert t["gross"] == pytest.approx((103.0 - 100.5) * 3.0)
    assert t["commission"] == pytest.approx(COMM * 3.0)
    assert t["net"] == pytest.approx(7.5 - 3.0 - 6.0)


# --------------------------------------------- 同 tick 反向：先平后开

def test_reversal_closes_and_opens_at_same_tick():
    df = base4()
    ticks = [
        tick("2026-01-05 11:00:00.001", 100.0, 100.5),
        tick("2026-01-05 12:00:00.001", 100.2, 100.7),
        tick("2026-01-05 13:59:59.999", 103.0, 103.5),
    ]
    res = run_b(df, ticks, ScriptStrategy([1, -1], fill=-1))
    assert len(res.trades) == 2
    t1, t2 = res.trades.iloc[0], res.trades.iloc[1]
    assert t1["side"] == "long" and t1["exit_reason"] == "signal"
    assert t1["exit_price"] == pytest.approx(100.2)      # 平多按 bid
    assert t1["exit_time"] == ts("2026-01-05 12:00:00.001")
    assert t2["side"] == "short"
    assert t2["entry_price"] == pytest.approx(100.2)     # 同一笔 tick 开空按 bid
    assert t2["entry_time"] == t1["exit_time"]
    assert t2["exit_reason"] == "end_of_data"
    assert t2["exit_price"] == pytest.approx(103.5)      # 空头平仓按 ask
    assert t1["bars_held"] == 1


# --------------------------------------------- time_stop：收盘前最后一笔 tick

def test_time_stop_fills_at_last_tick_before_close():
    df = sl_tp_bars()
    rules = ExitRules(time_stop_bars=2)
    ticks = [  # bar6 开盘 tick 入场；bar8 收盘前最后一笔 tick 出场
        tick("2026-01-05 06:00:00.001", 100.0, 100.5),
        tick("2026-01-05 07:30:00", 100.0, 100.5),
        tick("2026-01-05 08:59:59.999", 100.0, 100.5),
    ]
    t = one_trade(run_b(df, ticks, ScriptStrategy([0] * 5 + [1, 1, 1], fill=0,
                                                  exit_rules=rules)))
    assert t["entry_time"] == ts("2026-01-05 06:00:00.001")
    assert t["exit_reason"] == "time_stop"
    assert t["exit_time"] == ts("2026-01-05 08:59:59.999")
    assert t["exit_price"] == pytest.approx(100.0)
    assert t["bars_held"] == 2                            # bar6 入场、bar8 出场


# --------------------------------------------- equity 逐 bar 收盘市值

def test_equity_marked_at_last_tick_bid():
    df = base4()
    ticks = [
        tick("2026-01-05 11:00:00.001", 100.0, 100.5),
        tick("2026-01-05 11:59:59.999", 100.0, 100.5),
        tick("2026-01-05 12:59:59.999", 100.0, 100.5),
        tick("2026-01-05 13:59:59.999", 103.0, 103.5),
    ]
    res = run_b(df, ticks, ScriptStrategy([1], fill=1), contract_size=100.0,
                initial_capital=10_000.0)
    # 入场 100.5（ask）；bar1/2 收盘按最后 tick bid 100.0 估值 → −50；
    # bar3 强平净利 (103−100.5)×100 − 1 = 249
    assert list(res.equity) == pytest.approx([10_000.0, 9_950.0, 9_950.0,
                                              10_249.0])
    assert res.contract_size == 100.0 and res.timeframe == "H1"


# --------------------------------------------- 校验与损坏数据（fail-fast）

def test_rejects_invalid_inputs():
    df = base4()
    ticks = [tick("2026-01-05 11:00:00.001", 100.0, 100.5),
             tick("2026-01-05 13:59:59.999", 103.0, 103.5)]
    with pytest.raises(TypeError, match="contract_size"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, timeframe="H1", volume_lots=1.0)
    with pytest.raises(ValueError, match="contract_size"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=0,
                           timeframe="H1", volume_lots=1.0)
    with pytest.raises(ValueError, match="volume_lots"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=1.0,
                           volume_lots=0.0, timeframe="H1")
    with pytest.raises(ValueError, match="latency_ms"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=1.0,
                           timeframe="H1", latency_ms=-1, volume_lots=1.0)
    with pytest.raises(ValueError, match="slippage_price"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=1.0,
                           timeframe="H1", slippage_price=-0.1, volume_lots=1.0)
    # timeframe 不可解析（策略 required_data 也没给）
    with pytest.raises(ValueError, match="timeframe"):
        run_backtest_ticks(ScriptStrategy([1], fill=1), df, make_loader(ticks),
                           FakeCosts(), symbol="XAUUSD", params={},
                           contract_size=1.0, volume_lots=1.0)
    with pytest.raises(ValueError, match="信号"):
        run_backtest_ticks(ScriptStrategy([1], fill=2), df, make_loader(ticks),
                           FakeCosts(), symbol="XAUUSD", params={},
                           contract_size=1.0, timeframe="H1", volume_lots=1.0)


def test_rejects_corrupt_ticks():
    df = base4()
    # time_msc 倒退（两笔都在请求窗口内，后一笔更早）
    bad_order = [tick("2026-01-05 11:30:00", 100.0, 100.5),
                 tick("2026-01-05 11:00:30", 100.0, 100.5)]
    with pytest.raises(ValueError, match="倒退"):
        run_b(df, bad_order, ScriptStrategy([1], fill=1))
    # ask < bid
    bad_spread = [tick("2026-01-05 11:00:00.001", 100.5, 100.0)]
    with pytest.raises(ValueError, match="ask"):
        run_b(df, bad_spread, ScriptStrategy([1], fill=1))
    # 缺列
    def col_loader(start_msc=None, end_msc=None):
        return pd.DataFrame({"time_msc": [msc("2026-01-05 11:00:00.001")]})
    with pytest.raises(ValueError, match="列"):
        run_backtest_ticks(BuyHold(), df, col_loader, FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=1.0,
                           timeframe="H1", volume_lots=1.0)


# --------------------------------------------- T20（H2）：禁入窗推迟开仓（B-0.2）

from mt5data.sessions import SessionPolicy  # noqa: E402
from mt5quant.backtest.engine_b import ENGINE_VERSION_B  # noqa: E402


def _pol_window(local_hhmm: str, tz: str = "UTC", *, pre: int = 0, post: int = 60,
                **kw) -> SessionPolicy:
    return SessionPolicy(rollover_local=local_hhmm, rollover_tz=tz,
                         pre_minutes=pre, post_minutes=post, **kw)


def test_engine_b_version_bumped_for_t23_risk_sizing():
    # T23（M2）：RiskSizing 按风险定手数（B-0.4，与 A-1.5 同一函数）；
    # B-0.3 = T28/M8 性能改写（时间行为与 B-0.2 逐位一致）。
    assert ENGINE_VERSION_B == "B-0.4"


def test_session_policy_defers_entry_to_first_allowed_tick():
    """禁入时段的开仓订单保留，等下一笔允许时刻的 tick 成交（B-0.2）。"""
    df = base4()  # bar 10/11/12/13 UTC，收盘 11/12/13/14
    ticks = [
        tick("2026-01-05 10:30:00", 99.0, 99.2),
        tick("2026-01-05 11:00:00", 99.9, 100.4),     # 订单就绪但 11:00 禁入
        tick("2026-01-05 11:30:00", 99.8, 100.3),     # 仍禁入
        tick("2026-01-05 12:00:00", 99.7, 100.1),     # 12:00 恢复 → 在此成交
        tick("2026-01-05 12:59:59.999", 100.0, 100.4),
        tick("2026-01-05 13:59:59.999", 103.0, 103.2),
    ]
    pol = _pol_window("11:00")                        # 禁入 [11:00, 12:00)
    t = one_trade(run_b(df, ticks, ScriptStrategy([1], fill=1), session_policy=pol))
    assert t["entry_time"] == ts("2026-01-05 12:00:00")
    assert t["entry_price"] == pytest.approx(100.1)   # 12:00 tick 的 Ask
    assert t["exit_reason"] == "end_of_data"


def test_session_policy_never_blocks_close_and_reversal_closes():
    # 反向信号落在禁入窗：旧仓照平（12:00 tick），新仓推迟到允许 tick
    df = base4()
    ticks = [
        tick("2026-01-05 11:00:00", 99.9, 100.4),     # bar0 收盘 → 多头入场（11:00 允许）
        tick("2026-01-05 11:59:59.999", 100.1, 100.5),
        tick("2026-01-05 12:00:00", 99.7, 100.1),     # bar1 信号 -1 → 平多（不受限）
        tick("2026-01-05 12:30:00", 99.8, 100.2),     # 空头禁入（[12:00,13:00)）
        tick("2026-01-05 13:00:00", 99.6, 100.0),     # 恢复 → 空头入场
        tick("2026-01-05 13:59:59.999", 103.0, 103.2),
    ]
    pol = _pol_window("12:00")
    res = run_b(df, ticks, ScriptStrategy([1, -1], fill=-1), session_policy=pol)
    assert len(res.trades) == 2
    t1, t2 = res.trades.iloc[0], res.trades.iloc[1]
    assert t1["exit_time"] == ts("2026-01-05 12:00:00") and t1["exit_reason"] == "signal"
    assert t2["side"] == "short"
    assert t2["entry_time"] == ts("2026-01-05 13:00:00")
    assert t2["entry_price"] == pytest.approx(99.6)   # 空头按 Bid


def test_session_policy_pending_until_data_end_is_cancelled_not_coverage_error():
    """订单因禁入窗一直无法成交到数据末尾：作废（非 tick 覆盖问题），不抛错。"""
    df = flat_bars(pd.Timestamp("2026-01-05 10:00"), 3)   # 收盘 11/12/13
    ticks = [
        tick("2026-01-05 11:00:00", 99.9, 100.4),     # 就绪但全程禁入 [11:00, 13:00)
        tick("2026-01-05 12:00:00", 99.8, 100.3),
    ]
    pol = _pol_window("11:00", post=120)
    res = run_b(df, ticks, ScriptStrategy([1], fill=1), session_policy=pol)
    assert len(res.trades) == 0
    assert list(res.equity) == pytest.approx([10_000.0] * 3)


# --------------------------------------------- T28（M8）：跳读不改语义 + 性能

def test_idle_jump_dense_ticks_keep_results_bit_identical():
    """空仓区间的稠密 tick（跳读段）不改变任何结果：trades 与 equity
    与稀疏 tick 版逐位一致（prev 按逐笔消费语义模拟）。

    信号 [0,0,1,1]：[11:00, 13:00)（bar1/bar2 区间）空仓无订单——稠密 idle 段。
    """
    df = base4()
    sparse = [
        tick("2026-01-05 13:00:00.500", 99.9, 100.4),   # bar2 收盘后的首笔 → 入场
        tick("2026-01-05 13:30:00", 102.0, 102.4),
        tick("2026-01-05 13:59:59.999", 103.0, 103.2),
    ]
    dense = list(sparse)
    for m in range(0, 120):                             # 每分钟一笔 idle tick
        dense.append(tick(pd.Timestamp("2026-01-05 11:00") + timedelta(minutes=m),
                          98.0 + m * 0.01, 98.2 + m * 0.01))
    dense.sort(key=lambda t: t[0])
    res_sparse = run_b(df, sparse, ScriptStrategy([0, 0, 1], fill=1))
    res_dense = run_b(df, dense, ScriptStrategy([0, 0, 1], fill=1))
    assert len(res_sparse.trades) == len(res_dense.trades) == 1
    pd.testing.assert_frame_equal(res_sparse.trades, res_dense.trades)
    assert list(res_sparse.equity) == pytest.approx(list(res_dense.equity))


def test_idle_jump_still_raises_coverage_error_for_empty_interval():
    """跳读后 prev 的覆盖语义不变：跳过稠密 idle 段之后，下一根 bar 区间
    仍无 tick → TickCoverageError 照报（缺口区间与已加载范围都在报文里，
    跳读段的 tick 计入已加载覆盖统计）。"""
    df = base4()   # bar 10/11/12/13，收盘 11/12/13/14
    ticks = [tick(pd.Timestamp("2026-01-05 11:00") + timedelta(minutes=m),
                  99.0, 99.2) for m in range(120)]      # [11:00, 13:00) 稠密 idle
    # bar3 区间 [13:00, 14:00) 一笔 tick 都没有
    with pytest.raises(TickCoverageError) as ei:
        run_b(df, ticks, ScriptStrategy([0, 0, 1], fill=1))   # bar2 收盘（13:00）下单
    msg = str(ei.value)
    assert "2026-01-05 13:00" in msg            # bar3 区间无 tick 被查出
    assert "2026-01-05 12:59" in msg            # 跳读段末笔计入已加载覆盖
    assert "120 笔" in msg


def test_engine_b_perf_smoke_no_regression():
    """性能冒烟（宽限断言）：200 根 H1 × 每 30 秒一笔 tick（2.4 万笔，
    BuyHold 全程持仓——最重路径）应在数秒内完成；防 O(bar×tick) 级回退。"""
    import time

    df = flat_bars(pd.Timestamp("2026-03-02 00:00"), 200)
    ticks = []
    for b in range(200):
        t0 = pd.Timestamp("2026-03-02 00:00") + timedelta(hours=b)
        for s in range(0, 3600, 30):
            ticks.append(tick(t0 + timedelta(seconds=s),
                              100.0 + (s % 60) * 0.01, 100.4 + (s % 60) * 0.01))
    t_start = time.perf_counter()
    res = run_b(df, ticks, BuyHold())
    elapsed = time.perf_counter() - t_start
    assert len(res.trades) == 1
    assert elapsed < 10.0, f"engine B 性能冒烟超时：{elapsed:.1f}s（2.4 万 tick）"


# ------------------------------------------------- T23（M2）：RiskSizing 定手数

from mt5quant.sizing import RiskSizing  # noqa: E402

XAU_VOL = dict(vol_min=0.01, vol_step=0.01, vol_max=200.0)


def _sizing_bars(n=30):
    """恒定波幅 H1（sl_tp_bars 同款）：TR 恒 1.0 → ATR14 从 bar13 起恒 1.0。"""
    return flat_bars(pd.Timestamp("2026-01-05 00:00"), n)


def test_sizing_volume_lots_mutually_exclusive():
    df = _sizing_bars()
    ticks = ohlc_ticks(df, 0.5)
    # 同给 → 拒
    with pytest.raises(ValueError, match="二选一"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=100.0,
                           timeframe="H1", volume_lots=1.0,
                           sizing=RiskSizing(risk_fraction=0.005), **XAU_VOL)
    # 同缺 → 拒
    with pytest.raises(ValueError, match="volume_lots"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=100.0,
                           timeframe="H1")
    # sizing 缺 vol 三元组 → 拒
    with pytest.raises(ValueError, match="vol_min"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=100.0,
                           timeframe="H1", volume_lots=None,
                           sizing=RiskSizing(risk_fraction=0.005))


def test_sizing_lots_recorded_and_equal_to_shared_function():
    """成交 tick 时点按风险定手数：合约 100、资金 10,000、风险 0.5%、
    距离 = fallback 1.0×ATR 1.0 → 0.50 手（与 A 层同一函数同一结果）。"""
    df = _sizing_bars()
    ticks = ohlc_ticks(df, 0.5)
    strat = ScriptStrategy([0] * 14 + [1] * 10, fill=0)
    res = run_b(df, ticks, strat, contract_size=100.0,
                initial_capital=10_000.0, volume_lots=None,
                sizing=RiskSizing(risk_fraction=0.005), **XAU_VOL)
    t = one_trade(res)
    assert t["volume_lots"] == pytest.approx(0.50)
    assert res.volume_lots is None
    assert res.skipped_zero_lots == 0


def test_sizing_zero_lots_cancels_order_and_counts():
    """手数 0（资金 150：风险 0.75 → 0.0075 手 < 0.01）→ 订单作废、
    不开仓；信号持续的每个 bar 收盘都重新下单 → 每次计数。"""
    df = _sizing_bars(30)
    ticks = ohlc_ticks(df, 0.5)
    sig = [0] * 14 + [1] * 6 + [0] * 10      # bar14..19 信号为 1
    strat = ScriptStrategy(sig)
    res = run_b(df, ticks, strat, contract_size=100.0,
                initial_capital=150.0, volume_lots=None,
                sizing=RiskSizing(risk_fraction=0.005), **XAU_VOL)
    assert len(res.trades) == 0
    assert res.skipped_zero_lots == 6         # bar14..19 各一次订单


def test_sizing_equity_basis_changes_lots_after_loss():
    """basis="equity"：第一笔净亏后权益下降 → 第二笔 0.49 手；
    basis="initial" 两笔同为 0.50（与 A 层语义一致）。"""
    df = _sizing_bars(40)
    ticks = ohlc_ticks(df, 0.5)
    sig = [0] * 40
    sig[14] = 1
    sig[26] = 1
    strat = ScriptStrategy(sig)
    common = dict(contract_size=100.0, initial_capital=10_000.0,
                  volume_lots=None)
    res_i = run_b(df, ticks, strat, sizing=RiskSizing(risk_fraction=0.005,
                                                      basis="initial"),
                  **common, **XAU_VOL)
    res_e = run_b(df, ticks, strat, sizing=RiskSizing(risk_fraction=0.005,
                                                      basis="equity"),
                  **common, **XAU_VOL)
    lots_i = res_i.trades["volume_lots"].tolist()
    lots_e = res_e.trades["volume_lots"].tolist()
    assert lots_i == [pytest.approx(0.50), pytest.approx(0.50)]
    # 第一笔：入场 ask 100.5、出场 bid 100.0 → (−0.5)×0.5×100 − 0.5 佣金
    assert lots_e[0] == pytest.approx(0.50)
    assert lots_e[1] == pytest.approx(0.49)
