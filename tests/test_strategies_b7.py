"""B7 月末对冲调仓（EURUSD H1，C002/H101）信号语义测试。

事件模式策略：bar 级 generate_signals 只在事件窗锚定 bar（月末最后
工作日、伦敦 14:00 开盘的 H1 bar——收盘恰为定盘前一小时窗起点 15:00）
给出方向信号；研究评估走 validation.events 事件路径（london_fix_events
15:00 入、16:00 出），与本测试的 bar 级语义同一窗定义。
"""
import inspect

import pandas as pd
import pytest

from mt5quant.strategies.library.b7_monthend_hedge_rebalance import (
    LOOKBACK_BARS, MonthEndHedgeRebalance, _is_last_business_day,
)
from mt5quant.validation.lookahead import (
    check_window_invariance, scan_forbidden_patterns,
)
from mt5quant.backtest.truncation import check_no_lookahead

P0 = {"threshold_pct": 0.0}
P2 = {"threshold_pct": 0.02}


def make_bars(start, end, daily_growth, base=1.10):
    """工作日逐小时的合成 H1（naive UTC）：daily_growth[(年, 月)] 为该月
    每日复利增幅，月内方向可控。"""
    times = pd.DatetimeIndex(
        t for t in pd.date_range(start, end, freq="h")
        if t.dayofweek < 5)
    closes, v = [], float(base)
    for t in times:
        v *= daily_growth.get((t.year, t.month), 1.0) ** (1.0 / 24.0)
        closes.append(v)
    closes = pd.Series(closes, dtype="float64")
    return pd.DataFrame({
        "time": times, "open": closes, "high": closes * 1.0002,
        "low": closes * 0.9998, "close": closes,
    })


def signals(bars, params=P0):
    return MonthEndHedgeRebalance().generate_signals(bars, params)


def sig_at(sig, bars, ts):
    return int(sig.iloc[bars.index[bars["time"] == pd.Timestamp(ts)][0]])


# ---------------------------------------------------------------- 日历判定

def test_last_business_day_calendar_arithmetic():
    assert _is_last_business_day(2024, 2, 29)      # 2024-02-29 周四（闰月）
    assert _is_last_business_day(2024, 1, 31)      # 周三
    assert _is_last_business_day(2024, 3, 29)      # 3 月 30/31 是周末
    assert _is_last_business_day(2024, 7, 31)      # 周三（DST 段）
    assert not _is_last_business_day(2024, 1, 30)  # 31 日还有工作日
    assert not _is_last_business_day(2024, 3, 31)  # 周日不是工作日
    assert not _is_last_business_day(2024, 6, 30)  # 周日


# ---------------------------------------------------------------- 信号语义

def test_rising_month_longs_on_last_bday_14ldn_bar_only():
    """2 月上涨（冬令时伦敦=UTC）：仅 2/29 14:00（伦敦）bar 信号 +1，
    事件窗下一根（15:00）即归零——引擎 15:00 开盘入场、16:00 开盘平仓。"""
    bars = make_bars("2024-01-01", "2024-02-29 23:00",
                     {(2024, 1): 1.0, (2024, 2): 1.004})
    sig = signals(bars)
    assert sig_at(sig, bars, "2024-02-29 14:00") == 1
    assert sig_at(sig, bars, "2024-02-29 15:00") == 0
    assert int(sig.abs().sum()) == 1              # 全样本唯一事件信号
    # 1 月事件日（1/31）缺上月锚点 → 不交易
    assert sig_at(sig, bars, "2024-01-31 14:00") == 0


def test_falling_month_signals_short():
    bars = make_bars("2024-01-01", "2024-02-29 23:00",
                     {(2024, 1): 1.0, (2024, 2): 0.996})
    sig = signals(bars)
    assert sig_at(sig, bars, "2024-02-29 14:00") == -1


def test_amplitude_below_threshold_no_trade():
    """2 月约 +1.2%：阈值 0.02 → 不交易；阈值 0.0 → 方向仓。"""
    bars = make_bars("2024-01-01", "2024-02-29 23:00",
                     {(2024, 1): 1.0, (2024, 2): 1.0004})
    assert int(signals(bars, P2).abs().sum()) == 0
    assert sig_at(signals(bars, P0), bars, "2024-02-29 14:00") == 1


def test_first_calendar_month_has_no_anchor_and_later_months_do():
    """样本首月事件无上月锚点（不交易）；次月起锚点在场、信号正常。"""
    bars = make_bars("2024-02-01", "2024-03-29 23:00",
                     {(2024, 2): 1.0, (2024, 3): 1.005})
    sig = signals(bars)
    assert sig_at(sig, bars, "2024-02-29 14:00") == 0
    assert sig_at(sig, bars, "2024-03-29 14:00") == 1


def test_dst_london_1400_moves_with_utc_offset():
    """夏令时（BST=UTC+1）：伦敦 14:00 = 13:00 UTC——DST 由 zoneinfo 处理。"""
    bars = make_bars("2024-06-01", "2024-07-31 23:00",
                     {(2024, 6): 1.0, (2024, 7): 1.004})
    sig = signals(bars)
    assert sig_at(sig, bars, "2024-07-31 13:00") == 1   # 伦敦 14:00（BST）
    assert sig_at(sig, bars, "2024-07-31 12:00") == 0
    assert sig_at(sig, bars, "2024-07-31 14:00") == 0   # 伦敦 15:00（定盘前窗内非决策 bar）


# ---------------------------------------------------------------- 只用已收盘 bar

def test_truncation_window_invariance_and_static_scan():
    """四个月随机月度方向：任意前缀信号一致（截断）＋声明回看窗内复算
    末位信号一致（窗口不变性）＋源码无高危写法。"""
    import numpy as np
    rng = np.random.default_rng(5)
    growth = {(2024, m): float(1.0 + rng.normal(0.0, 0.004))
              for m in (1, 2, 3, 4)}
    bars = make_bars("2024-01-01", "2024-04-30 23:00", growth)
    assert len(bars) > LOOKBACK_BARS
    s = MonthEndHedgeRebalance()
    for params in (P0, P2):
        assert check_no_lookahead(s, bars, params=params,
                                  probe_points=5) == []
        assert check_window_invariance(s, bars, params, probes=12) == []
    from mt5quant.strategies.library import b7_monthend_hedge_rebalance as mod
    assert scan_forbidden_patterns(inspect.getsource(mod)) == []


# ---------------------------------------------------------------- 声明与约束

def test_required_data_and_exit_rules_declared():
    rd = MonthEndHedgeRebalance.required_data
    assert rd.timeframe == "H1"
    assert rd.symbols == ("EURUSD",)
    assert rd.lookback_bars == LOOKBACK_BARS >= 720   # 容得下上一日历月锚点
    rules = MonthEndHedgeRebalance.exit_rules
    assert rules.stop_loss_atr == 1.0                 # 日内保护性止损
    assert rules.time_stop_bars is None               # 时间出场=事件窗信号归零
    assert rules.week_close_exit is False             # 日内 1 小时，不过夜


def test_params_grid_and_validation():
    s = MonthEndHedgeRebalance()
    for thr in (0.0, 0.02):
        s.validate_params({"threshold_pct": thr})
    with pytest.raises(ValueError):
        s.validate_params({"threshold_pct": 0.01})    # 越网格
    with pytest.raises(ValueError):
        s.validate_params({})                          # 缺参


def test_signal_series_valid_for_engine():
    bars = make_bars("2024-01-01", "2024-02-29 23:00",
                     {(2024, 1): 1.0, (2024, 2): 1.004})
    sig = signals(bars)
    assert isinstance(sig, pd.Series)
    assert sig.index.equals(bars.index)
    assert set(sig.unique()) <= {-1, 0, 1}
