"""B5 纳指日内动量（USTEC M5）信号语义测试。

语义（Gao, Han, Li & Zhou 2018, Market Intraday Momentum）：开盘头半小时
（NY 09:30-10:00）的方向 → 尾盘半小时（NY 15:30-16:00）同向持有。
信号恰落在收盘本地时刻 ∈ [15:30, 16:00) 的 bar 上——引擎在 15:30 整点
开盘价入场、16:00 整点开盘价离场（窗口完整覆盖尾盘半小时）。
"""
import inspect

import pandas as pd
import pytest

from mt5quant.strategies.library.b5_intraday_momentum import IntradayMomentumNy
from mt5quant.validation.lookahead import (
    check_window_invariance, scan_forbidden_patterns,
)
from mt5quant.backtest.truncation import check_no_lookahead

P = {"min_ret_atr": 0.0}
EDT_DAY = "2026-10-26"      # EDT（NY=UTC-4）：09:30 NY=13:30 UTC，15:30 NY=19:30 UTC
EST_DAY = "2026-11-02"      # EST（NY=UTC-5）：09:30 NY=14:30 UTC，15:30 NY=20:30 UTC


def build_m5(day, start_utc, n, base=100.0):
    times = pd.date_range(f"{day} {start_utc}", periods=n, freq="5min")
    return pd.DataFrame({
        "time": times,
        "open": base, "high": base + 0.2, "low": base - 0.2, "close": base,
    })


def at(bars, day, hhmm):
    mask = bars["time"] == pd.Timestamp(f"{day} {hhmm}")
    assert mask.any(), f"bar {day} {hhmm} 不存在"
    return int(mask.idxmax())


def opening_half_hour_up(bars, day, first_open=100.0, last_close=105.0,
                         utc_or=("13:30", "13:55")):
    """开盘头半小时（EDT 日 13:30-13:55 UTC）线性走出 first_open→last_close。"""
    i0, i1 = at(bars, day, utc_or[0]), at(bars, day, utc_or[1])
    nseg = i1 - i0 + 1
    for j, i in enumerate(range(i0, i1 + 1)):
        v = first_open + (last_close - first_open) * j / (nseg - 1)
        bars.loc[i, ["open", "close"]] = v
        bars.loc[i, "high"] = v + 0.1
        bars.loc[i, "low"] = v - 0.1
    return bars


def make_edt_day(day=EDT_DAY, first_open=100.0, last_close=105.0):
    """13:00-20:10 UTC 的 M5 日：头半小时给定方向，其余平在 100。"""
    bars = build_m5(day, "13:00", 87)
    return opening_half_hour_up(bars, day, first_open, last_close)


def signals(bars, params=P):
    return IntradayMomentumNy().generate_signals(bars, params).tolist()


# ---------------------------------------------------------------- 方向与时点

def test_up_opening_carries_long_into_last_half_hour():
    bars = make_edt_day(last_close=105.0)
    sig = signals(bars)
    # 开盘半小时与全天其余时段：0
    assert sig[at(bars, EDT_DAY, "13:55")] == 0
    assert sig[at(bars, EDT_DAY, "19:20")] == 0     # 收于 NY 15:25：窗口未开
    # 收盘本地时刻 ∈ [15:30, 16:00)：恰为开盘 19:25..19:50 的 6 根
    assert sig[at(bars, EDT_DAY, "19:25")] == 1     # 收于 NY 15:30 → 引擎 15:30 开盘入场
    assert sig[at(bars, EDT_DAY, "19:50")] == 1     # 收于 NY 15:55
    assert sig[at(bars, EDT_DAY, "19:55")] == 0     # 收于 NY 16:00：窗口关 → 16:00 开盘离场
    assert sig[at(bars, EDT_DAY, "20:00")] == 0


def test_down_opening_carries_short():
    bars = make_edt_day(last_close=95.0)
    sig = signals(bars)
    assert sig[at(bars, EDT_DAY, "19:25")] == -1
    assert sig[at(bars, EDT_DAY, "19:50")] == -1
    assert sig[at(bars, EDT_DAY, "19:55")] == 0


def test_flat_opening_no_trade():
    bars = make_edt_day(last_close=100.0)           # 头半小时收益恰为 0
    assert set(signals(bars)) == {0}


def test_no_signal_outside_last_half_hour():
    bars = make_edt_day(last_close=105.0)
    sig = signals(bars)
    for i, s in enumerate(sig):
        t = bars["time"].iloc[i]
        if t < pd.Timestamp(f"{EDT_DAY} 19:25") or t > pd.Timestamp(f"{EDT_DAY} 19:50"):
            assert s == 0, f"bar {t} 不在尾盘窗口内却有信号 {s}"


# ---------------------------------------------------------------- 门槛过滤

def test_min_ret_atr_blocks_weak_opening():
    """头半小时收益 0.5 < 1.5×ATR（量级 ≈1.2，由盘前大幅振荡提供）：弱方向不交易。"""
    day = EDT_DAY
    bars = build_m5(day, "12:00", 111)              # 12:00 起：保证 ATR14 有预热
    for j, i in enumerate(range(0, at(bars, day, "13:30"))):
        v = 100.0 + (1.0 if j % 2 == 0 else -1.0)
        bars.loc[i, ["open", "close"]] = v
        bars.loc[i, "high"] = v + 1.0
        bars.loc[i, "low"] = v - 1.0
    bars = opening_half_hour_up(bars, day, 100.0, 100.5)
    sig = signals(bars, {"min_ret_atr": 1.5})
    assert set(sig) == {0}


def test_min_ret_atr_zero_trades_any_direction():
    bars = make_edt_day(last_close=100.5)
    assert signals(bars, {"min_ret_atr": 0.0})[at(bars, EDT_DAY, "19:25")] == 1


# ---------------------------------------------------------------- DST 与多日

def test_dst_fallback_shifts_both_windows():
    """EST 日：头半小时在 14:30-14:55 UTC，尾盘窗口在 20:25-20:50 UTC。"""
    day = EST_DAY
    bars = build_m5(day, "14:00", 85)
    bars = opening_half_hour_up(bars, day, 100.0, 104.0,
                                utc_or=("14:30", "14:55"))
    sig = signals(bars)
    assert sig[at(bars, day, "14:30")] == 0        # 头半小时本身：无信号
    assert sig[at(bars, day, "20:20")] == 0        # NY 15:25：窗口未开
    assert sig[at(bars, day, "20:25")] == 1        # 收于 NY 15:30
    assert sig[at(bars, day, "20:50")] == 1        # 收于 NY 15:55
    assert sig[at(bars, day, "20:55")] == 0        # 收于 NY 16:00


def test_direction_resets_daily():
    bars = pd.concat([
        make_edt_day(EDT_DAY, last_close=105.0),
        make_edt_day("2026-10-27", last_close=95.0),
        make_edt_day("2026-10-28", last_close=105.0),
    ], ignore_index=True)
    sig = signals(bars)
    assert sig[at(bars, EDT_DAY, "19:25")] == 1
    assert sig[at(bars, "2026-10-27", "19:25")] == -1
    assert sig[at(bars, "2026-10-28", "19:25")] == 1


# ---------------------------------------------------------------- 只用已收盘 bar

def test_truncation_and_window_invariance_and_static_scan():
    bars = pd.concat([
        make_edt_day(EDT_DAY, last_close=105.0),
        make_edt_day("2026-10-27", last_close=95.0),
        make_edt_day("2026-10-28", last_close=102.0),
    ], ignore_index=True)
    s = IntradayMomentumNy()
    assert check_no_lookahead(s, bars, params=P, probe_points=5) == []
    assert check_window_invariance(s, bars, P, probes=5) == []
    from mt5quant.strategies.library import b5_intraday_momentum as mod
    assert scan_forbidden_patterns(inspect.getsource(mod)) == []


# ---------------------------------------------------------------- 声明与约束

def test_required_data_and_exit_rules_declared():
    rd = IntradayMomentumNy.required_data
    assert rd.timeframe == "M5"
    assert rd.symbols == ("USTEC",)
    assert rd.lookback_bars >= 288 + 8       # M5 一日 288 根 + 缓冲
    assert IntradayMomentumNy.exit_rules.stop_loss_atr == 2.0


def test_grid_within_limits():
    s = IntradayMomentumNy()
    for k in (0.0, 0.5, 1.0, 1.5):
        s.validate_params({"min_ret_atr": k})
    with pytest.raises(ValueError):
        s.validate_params({"min_ret_atr": 2.0})


def test_signal_series_valid_for_engine():
    bars = make_edt_day(last_close=105.0)
    sig = IntradayMomentumNy().generate_signals(bars, P)
    assert isinstance(sig, pd.Series)
    assert sig.index.equals(bars.index)
    assert set(sig.unique()) <= {-1, 0, 1}


def test_single_bar_returns_zero():
    bars = pd.DataFrame({
        "time": [pd.Timestamp("2026-10-26 13:30")],
        "open": [100.0], "high": [100.5], "low": [99.5], "close": [100.2],
    })
    assert IntradayMomentumNy().generate_signals(bars, P).tolist() == [0]
