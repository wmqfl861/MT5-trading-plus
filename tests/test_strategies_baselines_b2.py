"""B2 NY 开盘区间突破（USTEC M5/M15）信号语义测试。

时间口径：库内时间为 naive-UTC 服务器时间（Exness GMT+0）；NY 本地时间用
zoneinfo 换算。测试日用 2026-10-26（EDT，NY=UTC-4，09:30 NY=13:30 UTC）与
2026-11-02（EST fallback 后，NY=UTC-5，09:30 NY=14:30 UTC）钉死 DST 行为。
"""
import pandas as pd
import pytest

from mt5quant.strategies.library.b2_opening_range_breakout import (
    NyOpeningRangeBreakout,
)
from mt5quant.validation.lookahead import (
    check_window_invariance, scan_forbidden_patterns,
)
from mt5quant.backtest.truncation import check_no_lookahead

P = {"exit_hour_ny": 15, "min_range_atr": 0.0}
EDT_DAY = "2026-10-26"      # 周一，EDT（UTC-4）
EST_DAY = "2026-11-02"      # fallback（11-01）后的周一，EST（UTC-5）


def build_m5(day, start_utc, n, base=100.0, hi=0.3, lo=0.3):
    """从 day start_utc 起 n 根 5 分钟 bar，默认平盘、给定高低影。"""
    times = pd.date_range(f"{day} {start_utc}", periods=n, freq="5min")
    return pd.DataFrame({
        "time": times,
        "open": base, "high": base + hi, "low": base - lo, "close": base,
    })


def set_bar(bars, day, hhmm, *, close=None, high=None, low=None):
    mask = bars["time"] == pd.Timestamp(f"{day} {hhmm}")
    assert mask.any(), f"bar {day} {hhmm} 不存在"
    i = mask.idxmax()
    if close is not None:
        bars.loc[i, "close"] = close
        bars.loc[i, "open"] = close          # 简化：开盘=收盘，只关心收盘突破
    if high is not None:
        bars.loc[i, "high"] = high
    if low is not None:
        bars.loc[i, "low"] = low
    return i


def at(bars, day, hhmm):
    mask = bars["time"] == pd.Timestamp(f"{day} {hhmm}")
    assert mask.any(), f"bar {day} {hhmm} 不存在"
    return int(mask.idxmax())


def signals(bars, params=P):
    return NyOpeningRangeBreakout().generate_signals(bars, params).tolist()


def make_edt_day_with_breakout(day=EDT_DAY, or_hi=101.0, or_lo=99.0,
                               breakout_close=101.5):
    """12:00-20:00 UTC（NY 08:00-16:00）。OR 13:30-13:55 UTC（NY 09:30-09:55）
    高低 [or_lo, or_hi]；14:10 UTC（NY 10:10）收盘突破。"""
    bars = build_m5(day, "12:00", 97)
    for hhmm in ("13:30", "13:35", "13:40", "13:45", "13:50", "13:55"):
        set_bar(bars, day, hhmm, high=or_hi, low=or_lo)
    set_bar(bars, day, "14:00", close=100.2, high=100.7, low=99.7)
    set_bar(bars, day, "14:05", close=100.2, high=100.7, low=99.7)
    set_bar(bars, day, "14:10", close=breakout_close, high=breakout_close + 0.2,
            low=100.2)
    for i in range(at(bars, day, "14:15"), len(bars)):
        bars.loc[i, ["open", "close"]] = breakout_close
        bars.loc[i, "high"] = breakout_close + 0.2
        bars.loc[i, "low"] = breakout_close - 0.3
    return bars


# ---------------------------------------------------------------- 突破方向与时点

def test_long_breakout_after_range_completes():
    bars = make_edt_day_with_breakout()
    sig = signals(bars)
    # OR 未完成（09:30-09:55 NY）与完成初期收盘仍在区间内：0
    assert sig[at(bars, EDT_DAY, "13:30")] == 0
    assert sig[at(bars, EDT_DAY, "14:00")] == 0
    assert sig[at(bars, EDT_DAY, "14:05")] == 0
    # 14:10 UTC（NY 10:10）收盘 101.5 > OR 高点 101：当根信号 1
    assert sig[at(bars, EDT_DAY, "14:10")] == 1


def test_long_held_until_exit_hour_then_flat():
    bars = make_edt_day_with_breakout()
    sig = signals(bars)                     # exit_hour_ny=15 → 15:00 NY 平仓
    assert sig[at(bars, EDT_DAY, "18:50")] == 1   # NY 14:50 开、14:55 收：仍持有
    assert sig[at(bars, EDT_DAY, "18:55")] == 0   # NY 14:55 开、15:00 收：信号归零
    assert sig[at(bars, EDT_DAY, "19:00")] == 0   # NY 15:00 开盘价平仓（引擎）


def test_short_breakout_below_range_low():
    bars = make_edt_day_with_breakout(breakout_close=98.5)
    set_bar(bars, EDT_DAY, "14:10", close=98.5, high=100.2, low=98.3)
    sig = signals(bars)
    assert sig[at(bars, EDT_DAY, "14:05")] == 0
    assert sig[at(bars, EDT_DAY, "14:10")] == -1   # 收盘 98.5 < OR 低点 99
    assert sig[at(bars, EDT_DAY, "18:50")] == -1


def test_no_breakout_inside_range_all_zero():
    bars = make_edt_day_with_breakout()
    # 14:10 起所有收盘都压回区间内（99, 101) 之间：无突破
    start = at(bars, EDT_DAY, "14:10")
    for i in range(start, len(bars)):
        bars.loc[i, ["open", "close"]] = 100.5
        bars.loc[i, "high"] = 100.7
        bars.loc[i, "low"] = 99.7
    sig = signals(bars)
    assert set(sig) == {0}


def test_opposite_side_close_flips_direction():
    bars = make_edt_day_with_breakout()
    set_bar(bars, EDT_DAY, "15:00", close=98.5, high=100.5, low=98.3)
    sig = signals(bars)
    assert sig[at(bars, EDT_DAY, "14:55")] == 1
    assert sig[at(bars, EDT_DAY, "15:00")] == -1   # 收盘跌破 OR 低点 → 反手


def test_exit_hour_14_flattens_one_hour_earlier():
    bars = make_edt_day_with_breakout()
    sig = signals(bars, {"exit_hour_ny": 14, "min_range_atr": 0.0})
    assert sig[at(bars, EDT_DAY, "17:50")] == 1    # NY 13:50 开、13:55 收
    assert sig[at(bars, EDT_DAY, "17:55")] == 0    # NY 13:55 收于 14:00 → 归零


# ---------------------------------------------------------------- 区间宽度过滤

def test_min_range_filter_blocks_narrow_range():
    """OR 宽 0.1 < 1.0×ATR14(≈0.4+)：当日禁交易。"""
    bars = make_edt_day_with_breakout(or_hi=100.05, or_lo=99.95,
                                      breakout_close=100.5)
    sig = signals(bars, {"exit_hour_ny": 15, "min_range_atr": 1.0})
    assert set(sig) == {0}


def test_min_range_filter_allows_wide_range():
    bars = make_edt_day_with_breakout(or_hi=101.0, or_lo=99.0)
    sig = signals(bars, {"exit_hour_ny": 15, "min_range_atr": 1.0})
    assert sig[at(bars, EDT_DAY, "14:10")] == 1


# ---------------------------------------------------------------- DST 与换日

def test_dst_fallback_shifts_opening_range_to_1430_utc():
    """2026-11-01 回拨后 NY=UTC-5：OR 在 14:30-14:55 UTC，突破按时出现。"""
    day = EST_DAY
    bars = build_m5(day, "13:00", 85)
    for hhmm in ("14:30", "14:35", "14:40", "14:45", "14:50", "14:55"):
        set_bar(bars, day, hhmm, high=101.0, low=99.0)
    set_bar(bars, day, "15:05", close=100.2, high=100.7, low=99.7)
    set_bar(bars, day, "15:10", close=101.5, high=101.7, low=100.2)
    sig = signals(bars)
    # 13:30 UTC 在 EST 下是 NY 08:30（盘前）：不在 OR 内
    assert sig[at(bars, day, "13:30")] == 0
    # OR 最后一根（14:55 UTC = NY 09:55）与 15:05（区间内）：0
    assert sig[at(bars, day, "14:55")] == 0
    assert sig[at(bars, day, "15:05")] == 0
    assert sig[at(bars, day, "15:10")] == 1     # NY 10:10 收盘突破
    assert sig[at(bars, day, "19:50")] == 1     # NY 14:50
    assert sig[at(bars, day, "19:55")] == 0     # 收于 NY 15:00 → 归零


def test_next_day_forms_new_range_and_does_not_inherit_position():
    """两天连续数据：第二天重新构筑 OR，突破信号只源于当天 OR。"""
    day1 = make_edt_day_with_breakout()
    day2 = make_edt_day_with_breakout(day="2026-10-27")
    bars = pd.concat([day1, day2], ignore_index=True)
    sig = signals(bars)
    # day1 尾部（NY 15:00 后）空仓
    assert sig[at(bars, "2026-10-26", "19:00")] == 0
    # day2 突破 bar 仍触发（OR 按天重算）
    assert sig[at(bars, "2026-10-27", "14:10")] == 1
    # day2 突破前一根：0（不继承 day1 的任何状态）
    assert sig[at(bars, "2026-10-27", "14:05")] == 0


# ---------------------------------------------------------------- 只用已收盘 bar

def test_truncation_and_window_invariance_and_static_scan():
    bars = pd.concat([make_edt_day_with_breakout(day=d) for d in
                      ("2026-10-26", "2026-10-27", "2026-10-28")],
                     ignore_index=True)
    s = NyOpeningRangeBreakout()
    assert check_no_lookahead(s, bars, params=P, probe_points=5) == []
    assert check_window_invariance(s, bars, P, probes=5) == []
    import inspect
    from mt5quant.strategies.library import b2_opening_range_breakout as mod
    assert scan_forbidden_patterns(inspect.getsource(mod)) == []


def test_bar_spacing_gap_does_not_break_time_logic():
    """缺一根 bar（数据孔）：bar 周期仍按 5 分钟推导，OR/突破时刻不受影响。"""
    bars = make_edt_day_with_breakout()
    i = at(bars, EDT_DAY, "12:30")
    bars = bars.drop(index=i).reset_index(drop=True)
    sig = signals(bars)
    assert sig[at(bars, EDT_DAY, "14:10")] == 1


# ---------------------------------------------------------------- 声明与约束

def test_required_data_and_exit_rules_declared():
    rd = NyOpeningRangeBreakout.required_data
    assert rd.timeframe == "M5"
    assert rd.lookback_bars >= 288 + 8       # M5 一天 288 根 + 缓冲（窗口不变性）
    assert NyOpeningRangeBreakout.exit_rules.stop_loss_atr == 1.0
    assert NyOpeningRangeBreakout.exit_rules.reentry == "immediate"


def test_grid_within_limits():
    s = NyOpeningRangeBreakout()
    combos = [{"exit_hour_ny": h, "min_range_atr": k}
              for h in (14, 15) for k in (0.0, 0.5, 1.0)]
    assert len(combos) == 6 <= 100
    for c in combos:
        s.validate_params(c)


def test_off_grid_params_rejected():
    with pytest.raises(ValueError):
        NyOpeningRangeBreakout().validate_params(
            {"exit_hour_ny": 16, "min_range_atr": 0.0})


def test_signal_series_valid_for_engine():
    bars = make_edt_day_with_breakout()
    sig = NyOpeningRangeBreakout().generate_signals(bars, P)
    assert isinstance(sig, pd.Series)
    assert sig.index.equals(bars.index)
    assert set(sig.unique()) <= {-1, 0, 1}


def test_single_bar_returns_zero():
    bars = pd.DataFrame({
        "time": [pd.Timestamp("2026-10-26 13:30")],
        "open": [100.0], "high": [100.5], "low": [99.5], "close": [100.0],
    })
    sig = NyOpeningRangeBreakout().generate_signals(bars, P)
    assert sig.tolist() == [0]
