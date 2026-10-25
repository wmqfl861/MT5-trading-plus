"""B4 带趋势过滤的均值回归（M15/H1）信号语义测试。

语义：z-score 极值反转 + SMA 趋势过滤——只在趋势同侧逆向入场
（上升趋势中买深度回落、下降趋势中卖冲高），z 回归中值或趋势过滤失效
或持有满 MAX_HOLD_BARS 根时信号归零。

构造说明：单根离群 bar 在 20 根窗口里的 |z| 恒为 19/√2≈4.25（与深度
无关），因此用多根回落/冲高序列构造 |z| 落在 2–3 区间的可控场景；
测试侧用独立重算的 z/SMA 验证触发条件，再断言信号方向与时点。
"""
import inspect

import numpy as np
import pandas as pd
import pytest

from mt5quant.strategies.library.b4_mean_reversion import (
    MAX_HOLD_BARS, TrendFilteredMeanReversion,
)
from mt5quant.validation.lookahead import (
    check_window_invariance, scan_forbidden_patterns,
)
from mt5quant.backtest.truncation import check_no_lookahead

P = {"z_n": 20, "z_entry": 2.0, "trend_n": 50}


def make_bars(closes, start="2026-01-05", freq="15min"):
    closes = pd.Series(closes, dtype="float64")
    return pd.DataFrame({
        "time": pd.date_range(start, periods=len(closes), freq=freq),
        "open": closes, "high": closes + 0.2, "low": closes - 0.2,
        "close": closes,
    })


def zspec(bars, z_n, trend_n):
    """测试侧独立重算：z 与 SMA（与规格同义、独立于实现的写法）。"""
    close = bars["close"]
    m = close.rolling(z_n).mean()
    s = close.rolling(z_n).std()
    z = (close - m) / s
    sma = close.rolling(trend_n).mean()
    return z, sma


def signals(bars, params=P):
    return TrendFilteredMeanReversion().generate_signals(bars, params).tolist()


def gap_up_dip(dip_tail=(133.0, 130.0, 128.0, 125.0), after="ramp"):
    """61 根 100 → 16 根 140（跳升平台）→ 4 根回落序列（bar 77..80）→ after。

    after：ramp=回到 140；hold=停在末位回落值；crash=跌到 105（破 SMA）。
    默认回落尾 125：z(80)≈-2.86<-2、close 125>SMA50≈115 → 触发做多。
    """
    closes = [100.0] * 61 + [140.0] * 16 + list(dip_tail)
    last = dip_tail[-1]
    if after == "ramp":
        closes += [140.0] * 6
    elif after == "hold":
        closes += [last] * (MAX_HOLD_BARS + 8)
    elif after == "crash":
        closes += [105.0] * 6
    return make_bars(closes)


def gap_down_spike(spike_tail=(67.0, 70.0, 72.0, 74.0), after="ramp"):
    """61 根 100 → 16 根 60（跳降平台）→ 4 根冲高序列（bar 77..80）→ after。

    默认冲高尾 74：z(80)≈+2.59>2、close 74<SMA50≈84.9 → 触发做空。
    """
    closes = [100.0] * 61 + [60.0] * 16 + list(spike_tail)
    if after == "ramp":
        closes += [60.0] * 6
    return make_bars(closes)


# ---------------------------------------------------------------- 多头：趋势中买回落

def test_long_entry_on_deep_dip_in_uptrend():
    bars = gap_up_dip()
    z, sma = zspec(bars, 20, 50)
    assert z.iloc[80] < -2.0 and z.iloc[80] > -3.0      # 手验：z(80)≈-2.86
    assert bars["close"].iloc[80] > sma.iloc[80]         # close 125 > SMA50≈115
    sig = signals(bars)
    assert all(s == 0 for s in sig[:80])                 # 回落途中 z 不足：无信号
    assert sig[80] == 1                                  # 第 4 根回落收盘触发做多
    assert sig[81] == 0                                  # 次根收盘回到均值上方（z≥0）→ 离场


def test_dip_below_sma_is_blocked_by_trend_filter():
    bars = gap_up_dip(dip_tail=(113.0, 110.0, 108.0, 105.0))  # 回落击穿 SMA
    z, sma = zspec(bars, 20, 50)
    assert z.iloc[80] < -2.0 and bars["close"].iloc[80] < sma.iloc[80]
    sig = signals(bars)
    assert set(sig) == {0}                                # 逆势接刀：禁入


def test_exit_when_close_falls_below_sma():
    bars = gap_up_dip(after="crash")                      # bar81 起收盘 105<SMA
    sig = signals(bars)
    assert sig[80] == 1
    assert sig[81] == 0                                   # 趋势过滤失效 → 离场
    assert all(s == 0 for s in sig[82:])                  # z<-2 但 close<SMA：不再入场


def test_max_hold_exits_even_if_z_stays_low():
    """构造：200 根 100 底座 → 16 根 140 平台 → 回落触发（z≈-2.7），此后
    以 0.02/bar 缓跌——close 恒低于 20 均值（z<0 不回归）、恒高于 SMA200
    （过滤不出场），只有 MAX_HOLD 时间止损能离场。"""
    closes = [100.0] * 200 + [140.0] * 16 + [133.0, 130.0, 128.0, 125.0]
    entry = len(closes) - 1                       # bar 219
    closes += [125.0 - 0.02 * k for k in range(1, MAX_HOLD_BARS + 9)]
    bars = make_bars(closes)
    z, sma = zspec(bars, 20, 200)
    assert z.iloc[entry] < -2.0                   # 触发条件成立
    assert bars["close"].iloc[entry] > sma.iloc[entry]
    for k in (10, 50, 95):                        # 持有期内两个出场条件都不成立
        i = entry + k
        assert z.iloc[i] < 0 and bars["close"].iloc[i] > sma.iloc[i]
    params = {"z_n": 20, "z_entry": 2.0, "trend_n": 200}
    sig = signals(bars, params)
    assert sig[entry] == 1
    assert sig[entry + MAX_HOLD_BARS - 1] == 1
    assert sig[entry + MAX_HOLD_BARS] == 0        # 持满 MAX_HOLD 根：信号归零


def test_wider_z_entry_needs_deeper_dip():
    """z_entry=3.0 的更深门槛：z(80)≈-2.86 不再触发。"""
    bars = gap_up_dip()
    sig = signals(bars, {"z_n": 20, "z_entry": 3.0, "trend_n": 50})
    assert sig[80] == 0


# ---------------------------------------------------------------- 空头：趋势中卖冲高

def test_short_entry_on_spike_in_downtrend():
    bars = gap_down_spike()
    z, sma = zspec(bars, 20, 50)
    assert z.iloc[80] > 2.0 and bars["close"].iloc[80] < sma.iloc[80]  # z≈+2.59
    sig = signals(bars)
    assert all(s == 0 for s in sig[:80])
    assert sig[80] == -1                                  # 下降趋势中冲高 → 做空
    assert sig[81] == 0                                   # z 回到 ≤0 → 离场


def test_spike_above_sma_is_blocked_by_trend_filter():
    bars = gap_down_spike(spike_tail=(83.0, 86.0, 88.0, 90.0))  # 冲高越过 SMA
    z, sma = zspec(bars, 20, 50)
    assert z.iloc[80] > 2.0 and bars["close"].iloc[80] > sma.iloc[80]
    sig = signals(bars)
    assert set(sig) == {0}


# ---------------------------------------------------------------- 网格与邻档

@pytest.mark.parametrize("z_n,z_entry,trend_n",
                         [(20, 2.0, 50), (40, 2.5, 100), (80, 3.0, 200)])
def test_grid_combos_all_valid(z_n, z_entry, trend_n):
    TrendFilteredMeanReversion().validate_params(
        {"z_n": z_n, "z_entry": z_entry, "trend_n": trend_n})


# ---------------------------------------------------------------- 只用已收盘 bar

def _mixed_closes():
    """趋势段 + 回落/冲高 + 震荡段（确定性伪随机）。"""
    rng = np.random.default_rng(42)
    up = [100.0 + 0.4 * i for i in range(200)]
    chop = [180.0 + 6.0 * np.sin(i / 5.0) + float(rng.normal(0, 1.5))
            for i in range(200)]
    dn = [180.0 - 0.3 * (i - 200) for i in range(200)]
    mix = [60.0 - 0.2 * (i - 400) + 4.0 * np.sin(i / 3.0) for i in range(200)]
    return up + chop + dn + mix


def test_truncation_and_window_invariance_and_static_scan():
    bars = make_bars(_mixed_closes())
    s = TrendFilteredMeanReversion()
    for params in (P, {"z_n": 80, "z_entry": 3.0, "trend_n": 200}):
        assert check_no_lookahead(s, bars, params=params, probe_points=5) == []
        assert check_window_invariance(s, bars, params, probes=5) == []
    from mt5quant.strategies.library import b4_mean_reversion as mod
    assert scan_forbidden_patterns(inspect.getsource(mod)) == []


# ---------------------------------------------------------------- 声明与约束

def test_required_data_and_exit_rules_declared():
    rd = TrendFilteredMeanReversion.required_data
    assert rd.timeframe == "M15"
    assert rd.lookback_bars >= 200 + MAX_HOLD_BARS   # 网格最大 trend_n + 持有上限
    assert TrendFilteredMeanReversion.exit_rules.stop_loss_atr == 2.0


def test_grid_size_within_limits():
    s = TrendFilteredMeanReversion()
    combos = [(z, e, t) for z in (20, 40, 80) for e in (2.0, 2.5, 3.0)
              for t in (50, 100, 200)]
    assert len(combos) == 27 <= 100
    for z, e, t in combos:
        s.validate_params({"z_n": z, "z_entry": e, "trend_n": t})


def test_signal_series_valid_for_engine():
    bars = gap_up_dip()
    sig = TrendFilteredMeanReversion().generate_signals(bars, P)
    assert isinstance(sig, pd.Series)
    assert sig.index.equals(bars.index)
    assert set(sig.unique()) <= {-1, 0, 1}
