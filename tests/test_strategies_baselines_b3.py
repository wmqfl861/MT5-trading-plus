"""B3 伦敦时段突破亚盘区间（EURUSD/XAUUSD M15）信号语义测试。

伦敦本地时间经 zoneinfo 换算：2026-10-20 为 BST（UTC+1，08:00 伦敦=07:00 UTC）；
2026-10-26 为 GMT（UTC+0，冬令时 10-25 01:00 UTC 起生效）。
"""
import pandas as pd
import pytest

from mt5quant.strategies.library.b3_london_breakout import LondonAsianBreakout
from mt5quant.validation.lookahead import (
    check_window_invariance, scan_forbidden_patterns,
)
from mt5quant.backtest.truncation import check_no_lookahead

P = {"exit_hour_ldn": 11, "min_range_atr": 0.0}
GMT_DAY = "2026-10-26"     # 冬令时（GMT，UTC+0）
BST_DAY = "2026-10-20"     # 夏令时（BST，UTC+1）


def build_m15(day, start_utc, n, base=1.1000, hi=0.0003, lo=0.0003):
    times = pd.date_range(f"{day} {start_utc}", periods=n, freq="15min")
    return pd.DataFrame({
        "time": times,
        "open": base, "high": base + hi, "low": base - lo, "close": base,
    })


def at(bars, day, hhmm):
    mask = bars["time"] == pd.Timestamp(f"{day} {hhmm}")
    assert mask.any(), f"bar {day} {hhmm} 不存在"
    return int(mask.idxmax())


def set_from(bars, day, hhmm, close, high=None, low=None):
    """从 day hhmm 起全部 bar 设为同一收盘。"""
    start = at(bars, day, hhmm)
    for i in range(start, len(bars)):
        bars.loc[i, ["open", "close"]] = close
        bars.loc[i, "high"] = close + 0.0002 if high is None else high
        bars.loc[i, "low"] = close - 0.0002 if low is None else low


def make_gmt_day(day=GMT_DAY, asian_hi=1.1050, asian_lo=1.0950,
                 breakout_close=1.1060):
    """GMT 日：00:00-07:45 UTC 为亚盘（伦敦 00:00-07:45 本地），高低
    [asian_lo, asian_hi]；08:30 UTC（伦敦 08:30）收盘突破。"""
    bars = build_m15(day, "00:00", 49)        # 00:00 → 12:00 UTC
    for i in range(0, at(bars, day, "08:00")):
        bars.loc[i, "high"] = asian_hi
        bars.loc[i, "low"] = asian_lo
    set_from(bars, day, "08:00", 1.1001)      # 区间内：无信号
    set_from(bars, day, "08:30", breakout_close)
    return bars


def signals(bars, params=P):
    return LondonAsianBreakout().generate_signals(bars, params).tolist()


# ---------------------------------------------------------------- 突破方向与时点

def test_long_breakout_after_london_open_gmt():
    bars = make_gmt_day()
    sig = signals(bars)
    assert sig[at(bars, GMT_DAY, "07:45")] == 0    # 亚盘最后一根：仍在构筑
    assert sig[at(bars, GMT_DAY, "08:00")] == 0    # 收盘在区间内
    assert sig[at(bars, GMT_DAY, "08:15")] == 0
    assert sig[at(bars, GMT_DAY, "08:30")] == 1    # 收盘 1.1060 > 亚盘高 1.1050


def test_short_breakout_below_asian_low():
    bars = make_gmt_day(breakout_close=1.0940)
    sig = signals(bars)
    assert sig[at(bars, GMT_DAY, "08:30")] == -1


def test_held_until_exit_hour_then_flat():
    bars = make_gmt_day()                          # exit_hour_ldn=11
    sig = signals(bars)
    assert sig[at(bars, GMT_DAY, "10:30")] == 1    # 伦敦 10:30 开、10:45 收
    assert sig[at(bars, GMT_DAY, "10:45")] == 0    # 收于伦敦 11:00 → 归零
    assert sig[at(bars, GMT_DAY, "11:00")] == 0


def test_exit_hour_12_flattens_one_hour_later():
    bars = make_gmt_day()
    sig = signals(bars, {"exit_hour_ldn": 12, "min_range_atr": 0.0})
    assert sig[at(bars, GMT_DAY, "11:45")] == 0    # 收于伦敦 12:00 → 归零
    assert sig[at(bars, GMT_DAY, "11:30")] == 1


def test_opposite_side_close_flips_direction():
    bars = make_gmt_day()
    set_from(bars, GMT_DAY, "09:30", 1.0940)       # 收盘破亚盘低 → 反手
    sig = signals(bars)
    assert sig[at(bars, GMT_DAY, "09:15")] == 1
    assert sig[at(bars, GMT_DAY, "09:30")] == -1


def test_no_breakout_inside_range_all_zero():
    bars = make_gmt_day()
    set_from(bars, GMT_DAY, "08:30", 1.1005)       # 全天收盘都在区间内
    sig = signals(bars)
    assert set(sig) == {0}


# ---------------------------------------------------------------- BST（夏令时）

def test_bst_day_asian_window_shifts_by_one_hour():
    """BST：伦敦 00:00-08:00 = 前一 UTC 日 23:00 起；突破须在 07:00 UTC 后。"""
    bars = build_m15("2026-10-19", "22:00", 60)    # 10-19 22:00 → 10-20 13:00 UTC
    d1, d2 = "2026-10-19", BST_DAY
    # 10-19 23:00 UTC（伦敦 10-20 00:00）到 10-20 06:45（伦敦 07:45）：亚盘
    for i in range(at(bars, d1, "23:00"), at(bars, d2, "07:00")):
        bars.loc[i, "high"] = 1.1050
        bars.loc[i, "low"] = 1.0950
    set_from(bars, d2, "07:00", 1.1001)            # 伦敦 08:00 起仍在区间内
    set_from(bars, d2, "07:30", 1.1060)            # 伦敦 08:30 收盘突破
    sig = signals(bars)
    assert sig[at(bars, d2, "06:45")] == 0         # 伦敦 07:45：亚盘内
    assert sig[at(bars, d2, "07:00")] == 0
    assert sig[at(bars, d2, "07:30")] == 1
    assert sig[at(bars, d2, "09:30")] == 1         # 伦敦 10:30 开、10:45 收
    assert sig[at(bars, d2, "09:45")] == 0         # 收于伦敦 11:00 → 归零
    assert sig[at(bars, d2, "10:00")] == 0


def test_bst_premidnight_bar_belongs_to_previous_london_day():
    """10-19 22:00 UTC（伦敦 10-19 23:00）：属于伦敦 10-19 日，不进 10-20 亚盘。"""
    bars = build_m15("2026-10-19", "22:00", 60)
    bars.loc[at(bars, "2026-10-19", "22:00"), "high"] = 1.2000   # 干扰值
    bars.loc[at(bars, "2026-10-19", "22:00"), "low"] = 1.0000
    for i in range(at(bars, "2026-10-19", "23:00"),
                   at(bars, BST_DAY, "07:00")):
        bars.loc[i, "high"] = 1.1050
        bars.loc[i, "low"] = 1.0950
    set_from(bars, BST_DAY, "07:30", 1.1060)
    sig = signals(bars)
    # 亚盘高低不含 10-19 22:00 bar 的极值：1.1060 > 1.1050 仍触发
    assert sig[at(bars, BST_DAY, "07:30")] == 1


# ---------------------------------------------------------------- 区间宽度过滤

def test_min_range_filter_blocks_narrow_asian_range():
    """亚盘宽 0.0003 < 1.0×ATR14（=0.0004，由 bar 影线决定）：当日禁交易。"""
    bars = make_gmt_day(asian_hi=1.1002, asian_lo=1.0999)
    set_from(bars, GMT_DAY, "08:30", 1.1030)
    sig = signals(bars, {"exit_hour_ldn": 11, "min_range_atr": 1.0})
    assert set(sig) == {0}


def test_min_range_filter_allows_wide_asian_range():
    bars = make_gmt_day()                          # 宽 0.01 >> ATR
    sig = signals(bars, {"exit_hour_ldn": 11, "min_range_atr": 1.0})
    assert sig[at(bars, GMT_DAY, "08:30")] == 1


# ---------------------------------------------------------------- 只用已收盘 bar

def test_truncation_and_window_invariance_and_static_scan():
    bars = pd.concat([make_gmt_day(day=d) for d in
                      ("2026-10-26", "2026-10-27", "2026-10-28")],
                     ignore_index=True)
    s = LondonAsianBreakout()
    assert check_no_lookahead(s, bars, params=P, probe_points=5) == []
    assert check_window_invariance(s, bars, P, probes=5) == []
    import inspect
    from mt5quant.strategies.library import b3_london_breakout as mod
    assert scan_forbidden_patterns(inspect.getsource(mod)) == []


def test_next_day_range_is_independent():
    bars = pd.concat([make_gmt_day(), make_gmt_day(day="2026-10-27")],
                     ignore_index=True)
    sig = signals(bars)
    assert sig[at(bars, "2026-10-26", "11:00")] == 0     # day1 出场后空仓
    assert sig[at(bars, "2026-10-27", "08:15")] == 0     # day2 突破前一根：0
    assert sig[at(bars, "2026-10-27", "08:30")] == 1     # day2 自身突破


# ---------------------------------------------------------------- 声明与约束

def test_required_data_and_exit_rules_declared():
    rd = LondonAsianBreakout.required_data
    assert rd.timeframe == "M15"
    assert rd.symbols == ("EURUSD", "XAUUSD")
    assert rd.lookback_bars >= 96 + 8               # M15 一日 96 根 + 缓冲
    assert LondonAsianBreakout.exit_rules.stop_loss_atr == 1.0


def test_grid_within_limits():
    s = LondonAsianBreakout()
    combos = [{"exit_hour_ldn": h, "min_range_atr": k}
              for h in (11, 12) for k in (0.0, 0.5, 1.0)]
    assert len(combos) == 6 <= 100
    for c in combos:
        s.validate_params(c)


def test_signal_series_valid_for_engine():
    bars = make_gmt_day()
    sig = LondonAsianBreakout().generate_signals(bars, P)
    assert isinstance(sig, pd.Series)
    assert sig.index.equals(bars.index)
    assert set(sig.unique()) <= {-1, 0, 1}


def test_single_bar_returns_zero():
    bars = pd.DataFrame({
        "time": [pd.Timestamp("2026-10-26 08:00")],
        "open": [1.1], "high": [1.1005], "low": [1.0995], "close": [1.1],
    })
    assert LondonAsianBreakout().generate_signals(bars, P).tolist() == [0]
