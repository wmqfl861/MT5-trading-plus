"""resample 测试：M1→M5 手算全等、label/closed=left、spread 取 min、与经纪商对账。"""
from datetime import datetime

import pandas as pd
import pytest

from mt5data.resample import ResampleCompare, compare_with_broker, resample_bars
from mt5data.store import _COLUMNS

T0 = datetime(2026, 10, 23, 0, 0)


def m1_df() -> pd.DataFrame:
    """12 根 M1（00:00–00:11），逐项手算友好。"""
    return pd.DataFrame({
        "time": [T0 + pd.Timedelta(minutes=i) for i in range(12)],
        #            桶1(00:00-04)              桶2(00:05-09)            桶3(00:10-11)
        "open": [100.0, 100.5, 101.0, 101.5, 102.0, 103.0, 103.5, 104.0, 104.5, 105.0, 106.0, 106.5],
        "high": [100.8, 101.0, 101.6, 102.0, 102.4, 103.8, 104.0, 104.6, 105.0, 105.4, 106.8, 107.0],
        "low": [99.7, 100.2, 100.8, 101.2, 101.6, 102.7, 103.2, 103.7, 104.2, 104.6, 105.6, 106.1],
        "close": [100.5, 101.0, 101.5, 102.0, 102.3, 103.5, 104.0, 104.5, 105.0, 105.3, 106.5, 107.0],
        "tick_volume": [10, 11, 12, 13, 14, 20, 21, 22, 23, 24, 30, 31],
        "spread": [12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
        "real_volume": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    })


def test_resample_m1_to_m5_matches_hand_computed():
    out = resample_bars(m1_df(), "M5")
    assert len(out) == 3
    assert list(out["time"]) == [T0, T0 + pd.Timedelta(minutes=5),
                                 T0 + pd.Timedelta(minutes=10)]
    assert list(out["open"]) == [100.0, 103.0, 106.0]        # first
    assert list(out["high"]) == [102.4, 105.4, 107.0]        # max（桶 3 = max(106.8, 107.0)）
    assert list(out["low"]) == [99.7, 102.7, 105.6]          # min
    assert list(out["close"]) == [102.3, 105.3, 107.0]       # last
    assert list(out["tick_volume"]) == [60, 110, 61]         # sum: 10+11+12+13+14 等
    assert list(out["spread"]) == [8, 3, 1]                  # min：MQL5 语义（bar 内最小点差）
    assert list(out["real_volume"]) == [15, 40, 23]          # sum


def test_resample_columns_and_dtypes_match_store():
    out = resample_bars(m1_df(), "M5")
    assert list(out.columns) == _COLUMNS
    assert str(out["time"].dtype).startswith("datetime64")
    for c in ("tick_volume", "spread", "real_volume"):
        assert str(out[c].dtype) == "int64"
    for c in ("open", "high", "low", "close"):
        assert str(out[c].dtype) == "float64"


def test_resample_label_closed_left_boundary_bar_opens_new_bucket():
    # 第 6 根（00:05）必须属于第二个桶而非第一个（closed=left 边界）
    out = resample_bars(m1_df(), "M5")
    assert out.iloc[0]["close"] == 102.3  # 00:04 收在桶 1
    assert out.iloc[1]["open"] == 103.0   # 00:05 自成桶 2 之首
    assert out.iloc[1]["high"] == 105.4


def test_resample_to_m15_single_bucket():
    out = resample_bars(m1_df(), "M15")
    assert len(out) == 1
    row = out.iloc[0]
    assert row["open"] == 100.0 and row["close"] == 107.0
    assert row["high"] == 107.0 and row["low"] == 99.7
    assert row["tick_volume"] == 231  # 10+11+...+31
    assert row["spread"] == 1


def test_resample_rejects_unknown_or_not_higher_timeframe():
    df = m1_df()
    with pytest.raises(ValueError):
        resample_bars(df, "M1")  # 必须高于 M1
    with pytest.raises(ValueError):
        resample_bars(df, "XXL")  # 未知周期


def _two_m5_bars() -> pd.DataFrame:
    return pd.DataFrame({
        "time": [T0, T0 + pd.Timedelta(minutes=5)],
        "open": [100.0, 103.0], "high": [102.4, 105.4],
        "low": [99.7, 102.7], "close": [102.3, 105.3],
        "tick_volume": [60, 110], "spread": [8, 3], "real_volume": [15, 40],
    })


def test_compare_identical_series_ok():
    df = _two_m5_bars()
    c = compare_with_broker(df, df.copy())
    assert isinstance(c, ResampleCompare)
    assert c.ok and c.common == 2 and c.only_res == 0 and c.only_broker == 0
    assert c.price_mismatches == []
    assert c.max_volume_diff == 0


def test_compare_single_high_mismatch():
    res = _two_m5_bars()
    broker = res.copy()
    broker.loc[0, "high"] = 103.4  # 差 1 点
    c = compare_with_broker(res, broker)
    assert not c.ok
    assert len(c.price_mismatches) == 1
    t, field, v_res, v_broker = c.price_mismatches[0]
    assert t == T0 and field == "high"
    assert v_res == pytest.approx(102.4) and v_broker == pytest.approx(103.4)
    assert c.common == 2 and c.only_res == 0 and c.only_broker == 0


def test_compare_only_counts_each_direction():
    res = _two_m5_bars()
    broker_missing_tail = res.iloc[[0]].copy()          # broker 缺末根
    c = compare_with_broker(res, broker_missing_tail)
    assert c.common == 1 and c.only_res == 1 and c.only_broker == 0 and c.ok

    broker_extra = pd.concat([res, res.iloc[[0]].assign(
        time=[T0 + pd.Timedelta(minutes=10)])], ignore_index=True)
    c2 = compare_with_broker(res, broker_extra)
    assert c2.common == 2 and c2.only_res == 0 and c2.only_broker == 1 and c2.ok


def test_compare_volume_diff_recorded_but_not_failing():
    res = _two_m5_bars()
    broker = res.copy()
    broker.loc[1, "tick_volume"] = 105  # 差 5
    broker.loc[0, "real_volume"] = 25   # 差 10
    c = compare_with_broker(res, broker)
    assert c.ok  # 成交量差异只记录，不判失败
    assert c.max_volume_diff == 10
    assert c.price_mismatches == []
