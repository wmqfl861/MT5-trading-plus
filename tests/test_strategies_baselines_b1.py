"""B1 唐奇安通道突破（H1）信号语义测试：手造 bars 断言突破方向与时点。

铁律：只用已收盘 bar——信号在 bar i 收盘产生，i 之前的信号不得依赖 i 之后
的任何 bar（截断测试）；窗口不变性：声明 lookback 内重算末位信号一致。
"""
import pandas as pd
import pytest

from mt5quant.strategies import ParamSpec
from mt5quant.strategies.library.b1_donchian_breakout import (
    BARS_PER_DAY, MAX_HOLD_BARS, DonchianBreakout,
)
from mt5quant.validation.lookahead import (
    check_window_invariance, scan_forbidden_patterns,
)
from mt5quant.backtest.truncation import check_no_lookahead

P = {"entry_days": 10, "exit_days": 2}      # 最小网格组合：240 根 / 48 根


def make_bars(closes, highs=None, lows=None, start="2026-01-05", freq="h"):
    closes = pd.Series(closes, dtype="float64")
    highs = closes + 0.5 if highs is None else pd.Series(highs, dtype="float64")
    lows = closes - 0.5 if lows is None else pd.Series(lows, dtype="float64")
    opens = closes.shift(1).fillna(closes.iloc[0])
    return pd.DataFrame({
        "time": pd.date_range(start, periods=len(closes), freq=freq),
        "open": opens, "high": highs, "low": lows, "close": closes,
    })


def flat_then(close_break, hold=6, base=100.0, n=240):
    """n 根横盘 base，第 n 根收盘 close_break（触发或不触发突破），再 hold 根。"""
    closes = [base] * n + [close_break] + [close_break] * hold
    return make_bars(closes)


def signals(bars):
    return DonchianBreakout().generate_signals(bars, P).tolist()


# ---------------------------------------------------------------- 突破方向与时点

def test_long_breakout_first_signal_at_breakout_bar():
    sig = signals(flat_then(101.0))
    assert sig[:240] == [0] * 240            # 回看不足/未突破：全 0
    assert sig[240] == 1                     # 收盘首次高于前 240 根最高：当根信号 1
    assert all(s == 1 for s in sig[241:])    # 无出场事件：粘性持有


def test_short_breakout_on_low_break():
    sig = signals(flat_then(99.0))
    assert sig[:240] == [0] * 240
    assert sig[240] == -1
    assert all(s == -1 for s in sig[241:])


def test_breakout_requires_close_beyond_channel_not_touch():
    """盘中高点触到通道上沿但收盘未超过：不算突破（只用收盘判定，保守）。"""
    bars = flat_then(100.4, hold=3)
    bars.loc[240, "high"] = 100.6            # 盘中越过 100.5，收盘 100.4 未越过
    sig = signals(bars)
    assert sig[240] == 0


def test_no_breakup_inside_lookback_even_if_close_above_older_highs():
    """前 240 根内出现 close>更早高点：整体窗口仍含更高高点 → 不触发。"""
    closes = [100.0] * 100 + [100.3] * 140   # 窗口内最高 100.8（100.3+0.5）
    closes += [100.4] * 6                    # close 100.4 < 100.8 → 无信号
    sig = signals(make_bars(closes))
    assert set(sig) == {0}


# ---------------------------------------------------------------- 出场语义

def _exit_scenario():
    """240 横盘 → bar240 突破做多 → bar300 深下影（不破入场通道低点）→
    bar480 收盘跌破 48 根低点通道（但未破 240 根低点）→ 仅出场不反手。"""
    n = 487
    closes = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    closes[240] = 101.0; highs[240] = 101.5; lows[240] = 100.5   # 突破入场
    for i in range(241, 480):
        closes[i], highs[i], lows[i] = 101.0, 101.5, 100.5
    lows[300] = 95.0                                              # 深下影，只影响入场通道低点
    closes[480] = 100.0; highs[480] = 101.5; lows[480] = 99.8     # 破 48 根低点 100.5
    for i in range(481, n):
        closes[i], highs[i], lows[i] = 100.0, 101.5, 99.8
    return make_bars(closes, highs, lows)


def test_exit_channel_closes_long_without_reversal():
    sig = signals(_exit_scenario())
    assert sig[240] == 1 and sig[479] == 1
    assert sig[480] == 0                       # 跌破短通道：平多
    assert sig[481] == 0                       # 未破长通道低点 95：不反手


def test_opposite_breakout_reverses_to_short():
    closes = [100.0] * 240 + [101.0]
    closes += [101.0] * 59                      # bars 241..299
    closes += [99.0] * 5                        # bar 300：跌破 240 根低点 99.5 → 反手
    sig = signals(make_bars(closes))
    assert sig[299] == 1
    assert sig[300] == -1


def test_max_hold_flattens_after_fixed_bars():
    """信号层时间止损：入场后 MAX_HOLD_BARS 根仍未出场 → 信号归零。"""
    closes = [100.0] * 240 + [101.0] * (MAX_HOLD_BARS + 6)
    sig = signals(make_bars(closes))
    entry = 240
    assert sig[entry] == 1
    assert sig[entry + MAX_HOLD_BARS - 1] == 1
    assert sig[entry + MAX_HOLD_BARS] == 0     # 720 根（30 天）后强制离场


# ---------------------------------------------------------------- 只用已收盘 bar

def test_truncation_no_lookahead_on_mixed_regime():
    closes = [100.0] * 240
    closes += [101.0] * 60 + [99.0] * 60       # 突破→反手
    closes += [102.0] * 300 + [100.0] * 300    # 再突破→出场
    closes += [103.0] * 500                    # 再突破
    bars = make_bars(closes)
    assert check_no_lookahead(DonchianBreakout(), bars, params=P,
                              probe_points=6) == []


def test_window_invariance_on_mixed_regime():
    closes = [100.0] * 240
    closes += [101.0] * 60 + [99.0] * 60
    closes += [102.0] * 300 + [100.0] * 300
    closes += [103.0] * 700                    # 覆盖 lookback（960+720+缓冲）
    bars = make_bars(closes)
    assert check_window_invariance(DonchianBreakout(), bars, P, probes=6) == []


def test_static_scan_source_is_clean():
    import inspect
    from mt5quant.strategies.library import b1_donchian_breakout as mod
    src = inspect.getsource(mod)
    assert scan_forbidden_patterns(src) == []


# ---------------------------------------------------------------- 声明与约束

def test_required_data_and_exit_rules_declared():
    rd = DonchianBreakout.required_data
    assert rd.timeframe == "H1"
    # lookback 必须覆盖最大入场通道（40 天）+ 最大持仓（720 根），否则窗口不变性不成立
    assert rd.lookback_bars >= 40 * BARS_PER_DAY + MAX_HOLD_BARS
    assert DonchianBreakout.exit_rules.stop_loss_atr == 3.0
    assert DonchianBreakout.exit_rules.reentry == "immediate"   # 缺省


def test_grid_within_limits_and_all_combos_exit_shorter_than_entry():
    s = DonchianBreakout()
    combos = [{"entry_days": e, "exit_days": x}
              for e in (10, 20, 40) for x in (2, 4, 8)]
    assert len(combos) == 9 <= 100
    for c in combos:
        s.validate_params(c)
        assert c["exit_days"] < c["entry_days"]


def test_exit_channel_longer_than_entry_rejected():
    class BadGrid(DonchianBreakout):
        params_schema = (
            ParamSpec("entry_days", "int", (2,)),
            ParamSpec("exit_days", "int", (4,)),
        )

    bars = make_bars([100.0] * 60)
    with pytest.raises(ValueError, match="exit_days"):
        BadGrid().generate_signals(bars, {"entry_days": 2, "exit_days": 4})


def test_signal_series_valid_for_engine():
    bars = flat_then(101.0)
    sig = DonchianBreakout().generate_signals(bars, P)
    assert isinstance(sig, pd.Series)
    assert sig.index.equals(bars.index)
    assert set(sig.unique()) <= {-1, 0, 1}
