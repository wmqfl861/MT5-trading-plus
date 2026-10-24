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
                           timeframe="H1")


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
                         params={}, timeframe="H1")
    res_b = run_backtest_ticks(strat, df, make_loader(ohlc_ticks(df, S_MODEL)),
                               costs, symbol="XAUUSD", params={},
                               contract_size=1.0, timeframe="H1",
                               slippage_price=SLIP_MODEL)
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
                                contract_size=1.0, params={}, timeframe="H1"))
    tb = one_trade(run_backtest_ticks(strat, df, make_loader(ohlc_ticks(df, 0.7)),
                                      costs, symbol="XAUUSD", params={},
                                      contract_size=1.0, timeframe="H1",
                                      slippage_price=SLIP_MODEL))
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
                                     contract_size=1.0, timeframe="H1"))
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
                           symbol="XAUUSD", params={}, timeframe="H1")
    with pytest.raises(ValueError, match="contract_size"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=0,
                           timeframe="H1")
    with pytest.raises(ValueError, match="volume_lots"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=1.0,
                           volume_lots=0.0, timeframe="H1")
    with pytest.raises(ValueError, match="latency_ms"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=1.0,
                           timeframe="H1", latency_ms=-1)
    with pytest.raises(ValueError, match="slippage_price"):
        run_backtest_ticks(BuyHold(), df, make_loader(ticks), FakeCosts(),
                           symbol="XAUUSD", params={}, contract_size=1.0,
                           timeframe="H1", slippage_price=-0.1)
    # timeframe 不可解析（策略 required_data 也没给）
    with pytest.raises(ValueError, match="timeframe"):
        run_backtest_ticks(ScriptStrategy([1], fill=1), df, make_loader(ticks),
                           FakeCosts(), symbol="XAUUSD", params={},
                           contract_size=1.0)
    with pytest.raises(ValueError, match="信号"):
        run_backtest_ticks(ScriptStrategy([1], fill=2), df, make_loader(ticks),
                           FakeCosts(), symbol="XAUUSD", params={},
                           contract_size=1.0, timeframe="H1")


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
                           timeframe="H1")
