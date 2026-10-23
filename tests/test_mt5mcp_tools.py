from datetime import datetime, timedelta

import pandas as pd
import pytest

from mt5data.store import save_bars
from mt5mcp.tools import (
    MAX_BARS_LIMIT,
    compute_indicator,
    describe_series,
    get_bars,
    list_series,
)


def make_db(tmp_path, n=300):
    t0 = datetime(2023, 10, 22, 12, 0)
    df = pd.DataFrame({
        "time": pd.to_datetime([t0 + timedelta(minutes=i) for i in range(n)]),
        "open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n,
        "close": [10.0 + (i % 2) for i in range(n)],
        "tick_volume": [5] * n, "spread": [1] * n, "real_volume": [0] * n,
    })
    save_bars(tmp_path, "EURUSD", "M1", df)
    save_bars(tmp_path, "XAUUSD", "H1", df)
    return tmp_path


def test_list_series_reports_each_series(tmp_path):
    rows = list_series(make_db(tmp_path))
    assert {(r["symbol"], r["timeframe"]) for r in rows} == {("EURUSD", "M1"), ("XAUUSD", "H1")}
    row = next(r for r in rows if r["symbol"] == "EURUSD")
    assert row["rows"] == 300
    assert row["first_time"].startswith("2023-10-22T12:00")
    assert row["last_time"].startswith("2023-10-22T16:59")


def test_get_bars_returns_records_with_iso_time(tmp_path):
    out = get_bars(make_db(tmp_path), "EURUSD", "M1", limit=3)
    assert len(out["bars"]) == 3
    b = out["bars"][-1]
    assert set(b) == {"time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"}
    assert b["time"].startswith("2023-10-22T")
    assert out["rows_returned"] == 3 and out["truncated"] is True  # 300 根只取 3 根


def test_get_bars_limit_is_capped(tmp_path):
    out = get_bars(make_db(tmp_path, n=600), "EURUSD", "M1", limit=10_000)
    assert len(out["bars"]) == MAX_BARS_LIMIT  # 600 根被截到上限
    assert out["truncated"] is True


def test_get_bars_time_window_filter(tmp_path):
    root = make_db(tmp_path)
    since = datetime(2023, 10, 22, 13, 0)
    until = datetime(2023, 10, 22, 13, 10)
    out = get_bars(root, "EURUSD", "M1", since=since, until=until, limit=500)
    times = [datetime.fromisoformat(b["time"]) for b in out["bars"]]
    assert len(times) == 11
    assert all(since <= t <= until for t in times)


def test_get_bars_unknown_symbol_raises(tmp_path):
    with pytest.raises(ValueError, match="NOAUD"):
        get_bars(make_db(tmp_path), "NOAUD", "M1", limit=10)


def test_describe_series_gives_gap_stats(tmp_path):
    out = describe_series(make_db(tmp_path), "EURUSD", "M1")
    assert out["rows"] == 300
    assert out["max_gap_seconds"] == 60  # 连续 M1：最大间隔恰为步长
    assert out["expected_step_minutes"] == 1


def test_symbol_traversal_rejected(tmp_path):  # I12：路径穿越/UNC/点号/换行一律拒绝
    root = make_db(tmp_path)
    evils = [".." + chr(92) + "x", chr(92) * 2 + "attacker" + chr(92) + "share",
             "EUR/USD", "XAUUSD;moo", "x" * 40, ".", "..",
             "EURUSD" + chr(10)]
    for evil in evils:
        with pytest.raises(ValueError):
            get_bars(root, evil, "M1", limit=5)
        with pytest.raises(ValueError):
            compute_indicator(root, evil, "M1", "rsi", {}, limit=5)


def test_indicator_period_capped(tmp_path):  # m4：超大 period 不得让读取失去上界
    root = make_db(tmp_path)
    with pytest.raises(ValueError, match="500"):
        compute_indicator(root, "EURUSD", "M1", "rsi", {"period": 10 ** 9}, limit=5)
    with pytest.raises(ValueError, match="500"):
        compute_indicator(root, "EURUSD", "M1", "macd", {"slow": 10 ** 6}, limit=5)


def test_indicator_window_invariance(tmp_path):  # m3：limit 变化不得改变同时间戳的值
    root = make_db(tmp_path, n=300)
    small = compute_indicator(root, "EURUSD", "M1", "rsi", {"period": 30}, limit=5)
    large = compute_indicator(root, "EURUSD", "M1", "rsi", {"period": 30}, limit=50)
    by_time = {v["time"]: v["value"] for v in large["values"]}
    for v in small["values"]:
        assert v["value"] == by_time[v["time"]]


def test_list_series_raises_when_data_dir_missing(tmp_path):  # M12：不再静默空表
    import os
    with pytest.raises(ValueError, match="数据目录"):
        list_series(os.path.join(tmp_path, "nope"))


def test_compute_macd_multi_output(tmp_path):
    out = compute_indicator(make_db(tmp_path), "EURUSD", "M1", "macd",
                            {"fast": 3, "slow": 6, "signal": 3}, limit=5)
    v = out["values"][-1]
    assert set(v) == {"time", "macd", "signal", "hist"}
    assert abs(v["hist"] - (v["macd"] - v["signal"])) < 1e-6  # 值域按 8 位小数舍入


def test_compute_bbands_multi_output(tmp_path):
    out = compute_indicator(make_db(tmp_path), "EURUSD", "M1", "bbands",
                            {"period": 20}, limit=5)
    v = out["values"][-1]
    assert set(v) == {"time", "mid", "upper", "lower"}
    assert v["upper"] > v["mid"] > v["lower"]


def test_compute_indicator_bounded_and_aligned(tmp_path):
    out = compute_indicator(make_db(tmp_path), "EURUSD", "M1", "rsi",
                            {"period": 2}, limit=5)
    assert out["indicator"] == "rsi" and out["params"] == {"period": 2}
    assert len(out["values"]) == 5
    v = out["values"][-1]
    assert set(v) == {"time", "value"}
    # 严格交替行情：Wilder 平滑下 avg_up:avg_down → 2/3:1/3 → RSI≈66.67
    assert v["value"] == pytest.approx(200 / 3)


def test_compute_indicator_rejects_unknown_name(tmp_path):
    with pytest.raises(ValueError, match="unicorn"):
        compute_indicator(make_db(tmp_path), "EURUSD", "M1", "unicorn", {}, limit=5)


def test_compute_indicator_list_names_in_error(tmp_path):
    with pytest.raises(ValueError, match="sma"):
        compute_indicator(make_db(tmp_path), "EURUSD", "M1", "unicorn", {}, limit=5)
    with pytest.raises(ValueError, match="macd"):
        compute_indicator(make_db(tmp_path), "EURUSD", "M1", "unicorn", {}, limit=5)
