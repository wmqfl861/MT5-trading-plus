"""T4 锁箱测试：cutoff 计算、配置读写、research_bars 边界、状态统计。

语义钉死（Review Focus #4——锁箱旁路）：
- research_bars 只返回 time < cutoff 的行；
- 显式 end > cutoff → LockboxError，绝不静默截断；
- 无配置 → 行为与 store.load_bars 完全一致。
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from mt5data.lockbox import (
    LockboxError,
    compute_cutoff,
    load_config,
    lockbox_status,
    research_bars,
    write_config,
)
from mt5data.store import save_bars


def make_series(root, symbol="XAUUSD", tf="H1",
                start=datetime(2026, 1, 1), n=48):
    """n 根 H1：2026-01-01T00 .. 2026-01-02T23（供 cutoff=01-02T00 切半）。"""
    df = pd.DataFrame({
        "time": pd.to_datetime([start + timedelta(hours=i) for i in range(n)]),
        "open": 1.0, "high": 1.0, "low": 1.0,
        "close": [float(i) for i in range(n)],
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })
    save_bars(root, symbol, tf, df)
    return df


# ---- compute_cutoff：历史跨度决定回扣月数，截到日 00:00 ----

def test_cutoff_long_history_uses_12_months():
    # 2014 年起：跨度 >5 年 → now−12 个月，且截到日 00:00
    c = compute_cutoff(datetime(2014, 1, 1), datetime(2026, 10, 24, 15, 30))
    assert c == datetime(2025, 10, 24, 0, 0)


def test_cutoff_short_history_uses_6_months():
    # 2024 年中起：跨度 <5 年 → now−6 个月
    c = compute_cutoff(datetime(2024, 6, 1), datetime(2026, 10, 24, 15, 30))
    assert c == datetime(2026, 4, 24, 0, 0)


def test_cutoff_exactly_5_years_takes_6_months():
    first = datetime(2021, 10, 24, 9, 0)
    now = datetime(2026, 10, 24, 9, 0)
    assert compute_cutoff(first, now) == datetime(2026, 4, 24)  # 恰 5 年 → 6 个月
    assert compute_cutoff(first, now + timedelta(seconds=1)) == \
        datetime(2025, 10, 24)  # 晚 1 秒才越过边界 → 12 个月


def test_cutoff_feb29_first_year_boundary():
    # 闰日起步：5 年边界收进 02-28（2029 非闰年）
    first = datetime(2024, 2, 29)
    assert compute_cutoff(first, datetime(2029, 2, 28)) == datetime(2028, 8, 28)
    assert compute_cutoff(first, datetime(2029, 3, 1)) == datetime(2028, 3, 1)


def test_cutoff_month_end_clamps_day():
    # 3/31 −6 个月 → 9/30（不存在 9/31，收进当月最后一天）
    c = compute_cutoff(datetime(2025, 1, 1), datetime(2026, 3, 31, 12, 0))
    assert c == datetime(2025, 9, 30)


# ---- 配置读写 ----

def test_write_load_config_roundtrip(tmp_path):
    write_config(tmp_path, {"XAUUSD/H1": "2026-04-24T00:00:00",
                            "EURUSD/M1": "2026-04-24T00:00:00"})
    assert load_config(tmp_path) == {
        "XAUUSD/H1": datetime(2026, 4, 24),
        "EURUSD/M1": datetime(2026, 4, 24),
    }


def test_write_config_normalizes_keys_upper(tmp_path):
    write_config(tmp_path, {"xauusd/h1": "2026-04-24T00:00:00"})
    assert load_config(tmp_path) == {"XAUUSD/H1": datetime(2026, 4, 24)}


def test_write_config_rejects_unparsable_value(tmp_path):
    with pytest.raises(ValueError):
        write_config(tmp_path, {"XAUUSD/H1": "不是时间"})


def test_load_config_missing_returns_none(tmp_path):
    assert load_config(tmp_path) is None


# ---- research_bars：每条读取路径一条"越过即报错/约束"测试 ----

def test_research_bars_no_config_returns_all(tmp_path):
    make_series(tmp_path)
    df = research_bars(tmp_path, "XAUUSD", "H1")
    assert len(df) == 48  # 无配置 → 与 store.load_bars 完全一致


def test_research_bars_returns_only_rows_before_cutoff(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    df = research_bars(tmp_path, "XAUUSD", "H1")
    assert len(df) == 24
    assert df["time"].max() < pd.Timestamp(2026, 1, 2)


def test_research_bars_end_beyond_cutoff_raises(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    with pytest.raises(LockboxError, match="锁箱"):
        research_bars(tmp_path, "XAUUSD", "H1", end=datetime(2026, 1, 3))
    # 越界 1 秒也算越过——显式请求禁止静默截断
    with pytest.raises(LockboxError, match="锁箱"):
        research_bars(tmp_path, "XAUUSD", "H1",
                      end=datetime(2026, 1, 2, 0, 0, 1))


def test_research_bars_end_equals_cutoff_allowed_but_strict(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    df = research_bars(tmp_path, "XAUUSD", "H1", end=datetime(2026, 1, 2))
    assert len(df) == 24  # 不报错，但结果仍严格 < cutoff


def test_research_bars_legal_end_applies_as_upper_bound(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    df = research_bars(tmp_path, "XAUUSD", "H1", end=datetime(2026, 1, 1, 10, 0))
    assert len(df) == 11  # 00:00..10:00 两端含
    assert df["time"].max() == pd.Timestamp(2026, 1, 1, 10, 0)


def test_research_bars_end_without_config_filters_plainly(tmp_path):
    make_series(tmp_path)
    df = research_bars(tmp_path, "XAUUSD", "H1", end=datetime(2026, 1, 1, 5, 0))
    assert len(df) == 6


def test_research_bars_only_configured_series_locked(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"EURUSD/M1": "2026-01-02T00:00:00"})
    assert len(research_bars(tmp_path, "XAUUSD", "H1")) == 48  # 键按序列生效


def test_research_bars_accepts_explicit_lockbox_dict(tmp_path):
    make_series(tmp_path)
    df = research_bars(tmp_path, "XAUUSD", "H1",
                       lockbox={"XAUUSD/H1": datetime(2026, 1, 2)})
    assert len(df) == 24  # 显式传入等价于读盘


def test_research_bars_missing_series_returns_none(tmp_path):
    assert research_bars(tmp_path, "NOAUD", "M1") is None  # 对齐 load_bars


def test_research_bars_end_beyond_cutoff_raises_even_without_data(tmp_path):
    # 非法请求先于数据存在性判定（fail-fast）
    write_config(tmp_path, {"NOAUD/M1": "2026-01-02T00:00:00"})
    with pytest.raises(LockboxError):
        research_bars(tmp_path, "NOAUD", "M1", end=datetime(2026, 1, 3))


# ---- lockbox_status ----

def test_lockbox_status_counts_open_and_locked(tmp_path):
    make_series(tmp_path)
    write_config(tmp_path, {"XAUUSD/H1": "2026-01-02T00:00:00"})
    st = lockbox_status(tmp_path, "XAUUSD", "H1")
    assert st["cutoff"] == datetime(2026, 1, 2)
    assert st["open_rows"] == 24 and st["locked_rows"] == 24


def test_lockbox_status_without_config_all_open(tmp_path):
    make_series(tmp_path)
    st = lockbox_status(tmp_path, "XAUUSD", "H1")
    assert st["cutoff"] is None
    assert st["open_rows"] == 48 and st["locked_rows"] == 0
