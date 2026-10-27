"""T74 特征 v1：trend_sma200（SMA200 上下）与 atr_pct_2000（ATR 分位高低档）。

只用作概率表的分组特征（蓝图 P 线 §2.4，最多 2 个）；都有界窗口、只用已
收盘 bar。窗口未满 → None（该触发不进细粒度格，由概率表回退父级）。
"""
import pandas as pd
import pytest

from mt5quant.strategies.library.prob.features import atr_pct_2000, trend_sma200


def bars_with(closes):
    n = len(closes)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="h"),
        "open": closes, "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes], "close": closes,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


def test_trend_sma200_above_below_none():
    closes = [100.0] * 5 + [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]   # 窗口=6
    bars = bars_with(closes)
    out = trend_sma200(bars, window=6)
    assert out.iloc[:5].isna().all()          # SMA 未满 → None
    assert out.iloc[5] == "below"             # 1 < mean(100×5,1)=83.5
    assert out.iloc[9] == "below"             # 6 < mean(100,1,2,3,4,5)=19.2
    assert out.iloc[10] == "above"            # 7 > mean(1,2,3,4,5,6)=3.5
    closes2 = [1.0] * 5 + [100.0, 100.0, 100.0]
    out2 = trend_sma200(bars_with(closes2), window=6)
    assert out2.iloc[5] == "above"            # 100 > mean(1,1,1,1,1,100)=17.5
    assert out2.iloc[6] == "above"
    # 等于 SMA 归 above（确定性口径）


def test_trend_sma200_equality_above():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]   # 窗口=3：SMA(4,5,6)=5 < 6
    out = trend_sma200(bars_with(closes), window=3)
    assert out.iloc[5] == "above"
    # close == SMA：窗 (2,4,3)→SMA=3=close → above（闭区间归 above）
    closes_eq = [9.0, 9.0, 9.0, 2.0, 4.0, 3.0]
    out_eq = trend_sma200(bars_with(closes_eq), window=3)
    assert out_eq.iloc[-1] == "above"


def test_atr_pct_2000_high_low_none():
    # 窗口=5：当前值在窗内的分位 ≥0.5 → high；< 0.5 → low；未满 → None
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 1.0]
    atr = pd.Series(vals)
    out = atr_pct_2000(atr, window=5, split=0.5)
    assert out.iloc[:4].isna().all()
    # idx4: rank(5 in [1..5]) = 1.0 ≥ 0.5 → high
    assert out.iloc[4] == "high"
    # idx5: rank(5 in [2,3,4,5,5]) = 4/5? 5 与 5 并列 → pct=1.0（最大）→ high
    assert out.iloc[5] == "high"
    # idx6: rank(1 in [3,4,5,5,1]) = 1/5 = 0.2 < 0.5 → low
    assert out.iloc[6] == "low"


def test_atr_pct_ties_use_average_rank():
    """并列值取平均名次（pandas rolling rank 语义）：5 与 5 并列 → pct 0.9。"""
    atr = pd.Series([2.0, 3.0, 4.0, 5.0, 5.0, 5.0])
    out = atr_pct_2000(atr, window=5, split=0.5)
    assert out.iloc[4] == "high"     # [2,3,4,5,5] 当前 5：名次 (4+5)/2=4.5，pct 0.9
    assert out.iloc[5] == "high"     # [3,4,5,5,5] 当前 5：名次 4，pct 0.8
