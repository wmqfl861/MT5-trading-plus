from datetime import datetime, timedelta

import pandas as pd
import pytest

from mt5data.store import (
    TIMEFRAME_SECONDS,
    drop_unclosed,
    load_bars,
    merge_bars,
    read_watermark,
    save_bars,
)


def bars_df(times, closes):
    return pd.DataFrame({
        "time": pd.to_datetime([datetime(2023, 10, 22) + timedelta(minutes=t) for t in times]),
        "open": 1.0, "high": 1.0, "low": 1.0,
        "close": closes, "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


def test_timeframe_seconds_cover_all_21():
    assert len(TIMEFRAME_SECONDS) == 21
    assert TIMEFRAME_SECONDS["M1"] == 60
    assert TIMEFRAME_SECONDS["H1"] == 3600
    assert TIMEFRAME_SECONDS["D1"] == 86400
    assert TIMEFRAME_SECONDS["W1"] == 7 * 86400
    assert TIMEFRAME_SECONDS["MN1"] == 31 * 86400  # 月线按31天保守判定收盘


def test_merge_bars_dedups_by_time_keeping_newer_and_sorts():
    old = bars_df([0, 1, 2], [1.0, 1.0, 1.0])
    new = bars_df([1, 2, 3], [2.0, 2.0, 2.0])  # 覆盖 1、2，新增 3
    merged = merge_bars(old, new)
    assert list(merged["close"]) == [1.0, 2.0, 2.0, 2.0]  # 旧0 新1新2 新3
    assert merged["time"].is_monotonic_increasing


def test_merge_bars_empty_old():
    merged = merge_bars(None, bars_df([0, 1], [1.0, 1.0]))
    assert len(merged) == 2


def test_drop_unclosed_removes_forming_bar():
    t0 = datetime(2023, 10, 22, 12, 0)
    df = bars_df([0, 1], [1.0, 2.0])
    df["time"] = pd.to_datetime([t0, t0 + timedelta(minutes=1)])
    now = t0 + timedelta(minutes=1, seconds=30)  # 第二根还在形成
    kept = drop_unclosed(df, "M1", now)
    assert len(kept) == 1 and kept.iloc[-1]["close"] == 1.0


def test_drop_unclosed_keeps_bar_closed_at_now():
    t0 = datetime(2023, 10, 22, 12, 0)
    df = bars_df([0, 1], [1.0, 2.0])
    df["time"] = pd.to_datetime([t0, t0 + timedelta(minutes=1)])
    now = t0 + timedelta(minutes=2, seconds=1)  # 两根都已收盘
    kept = drop_unclosed(df, "M1", now)
    assert len(kept) == 2


def test_drop_unclosed_tolerates_frozen_weekend_quote():
    # 周五收盘后行情冻结：最后一根收盘时刻 == 最后 tick 时刻，靠 5 秒宽容保住它
    t0 = datetime(2023, 10, 20, 23, 58)
    df = bars_df([0, 1], [1.0, 2.0])
    df["time"] = pd.to_datetime([t0, t0 + timedelta(minutes=1)])
    now = t0 + timedelta(minutes=1, seconds=59, milliseconds=900)  # 23:59:59.9
    kept = drop_unclosed(df, "M1", now)
    assert len(kept) == 2


def test_save_load_roundtrip_and_watermark(tmp_path):
    df = bars_df([0, 1, 2], [1.0, 2.0, 3.0])
    save_bars(tmp_path, "EURUSD", "M1", df)
    loaded = load_bars(tmp_path, "EURUSD", "M1")
    assert list(loaded["close"]) == [1.0, 2.0, 3.0]
    assert str(loaded["time"].dtype).startswith("datetime64")

    wm = read_watermark(tmp_path, "EURUSD", "M1")
    assert wm["symbol"] == "EURUSD" and wm["timeframe"] == "M1"
    assert wm["rows"] == 3
    assert wm["last_closed_time"].endswith("+00:00") or "T" in wm["last_closed_time"]


def test_load_missing_returns_none(tmp_path):
    assert load_bars(tmp_path, "XXX", "M1") is None
    assert read_watermark(tmp_path, "XXX", "M1") is None
