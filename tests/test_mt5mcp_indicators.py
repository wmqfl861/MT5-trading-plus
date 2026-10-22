import numpy as np
import pandas as pd
import pytest

from mt5mcp.indicators import atr, bbands, ema, macd, rsi, sma


def test_sma_hand_computed():
    out = sma(pd.Series([1.0, 2, 3, 4, 5]), 3)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert list(out.iloc[2:]) == [2.0, 3.0, 4.0]


def test_ema_matches_recursive_definition():
    # 种子=首值，k=2/(span+1)=0.5
    out = ema(pd.Series([1.0, 2, 3, 4, 5]), 3)
    assert list(out) == pytest.approx([1.0, 1.5, 2.25, 3.125, 4.0625])


def test_rsi_hand_computed_period2():
    closes = pd.Series([1.0, 2, 3, 2, 3])
    out = rsi(closes, 2)
    assert list(out) == pytest.approx([np.nan, 100, 100, 50, 75], nan_ok=True)


def test_rsi_extremes():
    up = rsi(pd.Series([1.0, 2, 3, 4, 5, 6, 7]), 3)
    down = rsi(pd.Series([7.0, 6, 5, 4, 3, 2, 1]), 3)
    assert up.iloc[-1] == 100  # 只涨不跌
    assert down.iloc[-1] == 0  # 只跌不涨


def test_macd_constant_series_is_zero():
    closes = pd.Series([5.0] * 40)
    line, signal, hist = macd(closes)
    assert line.iloc[-1] == 0 and signal.iloc[-1] == 0 and hist.iloc[-1] == 0


def test_macd_shapes_aligned():
    closes = pd.Series(np.linspace(1, 50, 60))
    line, signal, hist = macd(closes)
    assert len(line) == len(signal) == len(hist) == 60
    assert (hist == line - signal).all()


def test_bbands_constant_series_collapses():
    closes = pd.Series([5.0] * 30)
    mid, upper, lower = bbands(closes, 20, 2)
    assert mid.iloc[-1] == 5 and upper.iloc[-1] == 5 and lower.iloc[-1] == 5


def test_bbands_envelope_around_middle():
    closes = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    mid, upper, lower = bbands(closes, 5, 2)
    assert upper.iloc[-1] > mid.iloc[-1] > lower.iloc[-1]
    # 均值±2倍样本标准差（ddof=1）
    win = closes.iloc[-5:]
    assert mid.iloc[-1] == win.mean()
    assert upper.iloc[-1] == win.mean() + 2 * win.std(ddof=1)


def test_atr_constant_range_bars():
    n = 30
    highs = pd.Series([11.0] * n)
    lows = pd.Series([10.0] * n)
    closes = pd.Series([10.5] * n)  # 前收与区间端点距离都 ≤ 1 → TR 恒为 1
    out = atr(highs, lows, closes, 14)
    assert out.iloc[-1] == pytest.approx(1.0)


def test_atr_zero_range():
    n = 20
    s = pd.Series([5.0] * n)
    out = atr(s, s, s, 5)
    assert (out.iloc[1:] == 0).all()
