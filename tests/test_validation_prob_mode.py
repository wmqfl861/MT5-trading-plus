"""T75 概率模式评估：随机化/Holm/植入/Brier——合成数据测试。

全部用合成数据（蓝图 v4 §2 禁止事项允许项）；不触真实局面标签。
"""
import numpy as np
import pandas as pd
import pytest

from mt5quant.backtest.engine_a import atr_series
from mt5quant.validation.prob_mode import (
    brier_skill, delta_p_test, holm, plant_delta, randomize_triggers,
)


def synth_bars(n, *, seed, start="2020-01-02", s0=100.0):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.30, size=n)
    close = s0 + np.cumsum(steps)
    openc = close + rng.normal(0.0, 0.05, size=n)
    high = np.maximum(openc, close) + np.abs(rng.normal(0, 0.12, size=n))
    low = np.minimum(openc, close) - np.abs(rng.normal(0, 0.12, size=n))
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq="1h"),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0})


def labels_df(n_days, *, p_win, seed, start="2020-01-01"):
    """按日一条（触发日单位）合成标签：每日 win/loss。"""
    rng = np.random.default_rng(seed)
    days = pd.date_range(start, periods=n_days, freq="1D")
    wins = rng.random(n_days) < p_win
    rows = []
    for d, w in zip(days, wins):
        for _ in range(rng.integers(1, 4)):     # 同日多触发
            rows.append({"time": d + pd.Timedelta(hours=int(rng.integers(0, 23))),
                         "outcome": "win" if w else "loss",
                         "net_r": 1.9 if w else -1.05,
                         "situation": "S1", "action": "A3", "side": 1,
                         "level": 100.0})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 随机化

def test_randomize_preserves_hour_weekday_buckets():
    bars = synth_bars(3000, seed=5)
    trig = pd.DataFrame({
        "time": bars["time"].iloc[::30].iloc[:80],
        "situation": "S1", "action": "A3", "side": 1, "level": 100.0})
    out = randomize_triggers(trig, bars, seed=7)
    assert len(out) == len(trig)
    orig_hours = pd.to_datetime(trig["time"]).dt.tz_localize("UTC").dt.tz_convert(
        "America/New_York").dt.hour.value_counts().sort_index()
    new_hours = pd.to_datetime(out["time"]).dt.tz_localize("UTC").dt.tz_convert(
        "America/New_York").dt.hour.value_counts().sort_index()
    pd.testing.assert_series_equal(orig_hours, new_hours, check_names=False)
    orig_wd = pd.to_datetime(trig["time"]).dt.tz_localize("UTC").dt.tz_convert(
        "America/New_York").dt.weekday.value_counts().sort_index()
    new_wd = pd.to_datetime(out["time"]).dt.tz_localize("UTC").dt.tz_convert(
        "America/New_York").dt.weekday.value_counts().sort_index()
    pd.testing.assert_series_equal(orig_wd, new_wd, check_names=False)


def test_randomize_deterministic():
    bars = synth_bars(2000, seed=3)
    trig = pd.DataFrame({"time": bars["time"].iloc[::40][:50],
                         "situation": "S1", "action": "A3", "side": 1,
                         "level": 100.0})
    a = randomize_triggers(trig, bars, seed=11)
    b = randomize_triggers(trig, bars, seed=11)
    pd.testing.assert_frame_equal(a, b)


# ---------------------------------------------------------------- Δp 检验

def test_delta_p_test_detects_planted_effect():
    real = labels_df(300, p_win=0.50, seed=1)
    null = labels_df(300, p_win=0.40, seed=2)
    res = delta_p_test(real, null, sims=400, seed=3)
    assert res["n_days_real"] == 300
    assert res["delta_p"] == pytest.approx(0.10, abs=0.03)
    assert res["p_value"] < 0.05      # 300 触发日、Δp≈0.10：z≈2+，单侧显著


def test_delta_p_test_null_effect_p_uniform():
    """同分布两组：p 值不偏小（名义水平附近——单侧中位约 0.5）。"""
    ps = []
    for seed in range(20):
        a = labels_df(120, p_win=0.40, seed=100 + seed)
        b = labels_df(120, p_win=0.40, seed=200 + seed)
        ps.append(delta_p_test(a, b, sims=200, seed=seed)["p_value"])
    assert np.median(ps) > 0.25      # 无效应时 p 不系统偏小
    assert sum(p < 0.05 for p in ps) <= 3   # 误报率接近名义 5%


def test_delta_p_test_deterministic():
    a = labels_df(100, p_win=0.5, seed=9)
    b = labels_df(100, p_win=0.4, seed=8)
    r1 = delta_p_test(a, b, sims=100, seed=42)
    r2 = delta_p_test(a, b, sims=100, seed=42)
    assert r1 == r2


# ---------------------------------------------------------------- Holm

def test_holm_matches_hand_calc():
    pv = {"H1": 0.01, "H2": 0.04, "H3": 0.03}
    adj = holm(pv)
    # 手算：排序 H1(.01)×3=.03；H3(.03)×2=.06；H2(.04)×1=.04→运行最大 .06
    assert adj["H1"] == pytest.approx(0.03)
    assert adj["H3"] == pytest.approx(0.06)
    assert adj["H2"] == pytest.approx(0.06)
    assert all(v <= 1.0 for v in adj.values())


def test_holm_empty():
    assert holm({}) == {}


# ---------------------------------------------------------------- 植入

def test_plant_delta_shifts_winrate():
    base = labels_df(400, p_win=0.40, seed=6)
    planted = plant_delta(base, 0.05, seed=7)
    days = lambda df: (df["outcome"] == "win").groupby(
        pd.to_datetime(df["time"]).dt.date).mean().mean()
    assert days(planted) - days(base) == pytest.approx(0.05, abs=0.01)
    assert len(planted) == len(base)


def test_plant_delta_deterministic():
    base = labels_df(200, p_win=0.4, seed=6)
    pd.testing.assert_frame_equal(plant_delta(base, 0.05, seed=3),
                                  plant_delta(base, 0.05, seed=3))


# ---------------------------------------------------------------- Brier

def test_brier_skill_positive_only_with_information():
    rng = np.random.default_rng(2)
    y = rng.random(500) < 0.4
    # 无信息（预测=基准率）→ 技能分 ≈ 0
    assert abs(brier_skill(np.full(500, 0.4), y, 0.4)) < 1e-9
    # 反向信息 → 负
    assert brier_skill(np.where(y, 0.1, 0.9), y, 0.4) < 0
    # 有信息 → 正
    assert brier_skill(np.where(y, 0.8, 0.2), y, 0.4) > 0.5
