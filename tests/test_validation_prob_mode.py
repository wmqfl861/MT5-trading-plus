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


# ------------------------------------------------- 审核三（S3/S7/S9）

def test_s3_randomize_matches_atr_quantile():
    """S3：随机时点的 atr_pct_2000 档与原触发一致（桶键含分位档）。"""
    from mt5quant.backtest.engine_a import atr_series
    from mt5quant.strategies.library.prob.features import atr_pct_2000
    n = 9000
    vol = np.where((np.arange(n) // 1500) % 2 == 0, 0.15, 0.60)
    bars = synth_bars(n, seed=5)
    # 高低波动交替（同探针 R2 构造：改用分段 sigma）
    rng2 = np.random.default_rng(9)
    close = 100 + np.cumsum(rng2.normal(0, 1, n) * vol)
    bars = bars.assign(close=close)
    pct = atr_pct_2000(atr_series(bars)).to_numpy()
    hi = np.flatnonzero(pct == "high")
    assert len(hi) > 500
    pick = np.sort(np.random.default_rng(3).choice(hi, size=200,
                                                   replace=False))
    trig = pd.DataFrame({"time": bars["time"].iloc[pick].to_numpy(),
                         "situation": "S1", "action": "A3", "side": 1,
                         "level": 100.0})
    out = randomize_triggers(trig, bars, seed=11)
    idx = pd.Index(bars["time"]).get_indexer(pd.to_datetime(out["time"]))
    share = float(np.mean(pct[idx] == "high"))
    assert share >= 0.99, share


def test_s3_empty_bucket_rejects():
    # 100 根 bar（周四起 ~4.2 天）：周二 12:00 的桶（12, 周二, None）
    # 在池中不存在 → 拒（不退化全域抽样）
    bars = synth_bars(100, seed=2)
    trig = pd.DataFrame({"time": [pd.Timestamp("2020-01-07 12:00")],
                         "situation": "S1", "action": "A3", "side": 1,
                         "level": 100.0})
    with pytest.raises(ValueError, match="无候选"):
        randomize_triggers(trig, bars, seed=1)


def test_s7_skipped_excluded_from_winrate():
    days = pd.bdate_range("2016-01-04", periods=200)
    real = pd.DataFrame({"time": days + pd.Timedelta(hours=10),
                         "outcome": ["win", "skipped"] * 100})
    null = pd.DataFrame({"time": days + pd.Timedelta(hours=11),
                         "outcome": ["win", "loss"] * 100})
    res = delta_p_test(real, null, sims=200, seed=1)
    assert abs(res["delta_p"] - 0.5) < 0.02     # 只按成交计：1.0 − 0.5


def test_s9_p_value_has_plus_one_correction():
    a = labels_df(120, p_win=0.50, seed=1)
    b = labels_df(120, p_win=0.40, seed=2)
    res = delta_p_test(a, b, sims=200, seed=3)
    assert res["p_value"] >= 1.0 / 201           # (1+k)/(1+B) 下界


def test_fpr_fast_smoke():
    """快测（CI 内）：同分布 40 组×50 置换的误报率界（粗检——正式
    误报率以慢测试与 T75 标定为准）。"""
    hits = 0
    for rep in range(40):
        a = labels_df(120, p_win=0.40, seed=1000 + rep)
        b = labels_df(120, p_win=0.40, seed=2000 + rep)
        if delta_p_test(a, b, sims=50, seed=rep)["p_value"] < 0.05:
            hits += 1
    assert hits <= 6, hits


def test_fpr_slow_400x200():
    """审核四 E5（慢测试，CI 外单独跑）：同分布 400 次重复、每次 200
    次置换；名义 5% 下命中 ≤ 32（二项 95% 上界 ≈ 8%）。设环境变量
    P_LINE_SLOW=1 启用。日志随复选框存档。"""
    import os
    if not os.environ.get("P_LINE_SLOW"):
        pytest.skip("慢测试：设 P_LINE_SLOW=1 启用（400×200 置换）")
    hits = 0
    reps, sims = 400, 200
    for rep in range(reps):
        a = labels_df(120, p_win=0.40, seed=10_000 + rep)
        b = labels_df(120, p_win=0.40, seed=20_000 + rep)
        if delta_p_test(a, b, sims=sims, seed=rep)["p_value"] < 0.05:
            hits += 1
    rate = hits / reps
    assert hits <= 32, f"误报率 {rate:.1%}（{hits}/{reps}）超过 8% 界"


def test_t75_missing_detection_rate_90pct():
    """计划缺测：Δp=0.05、触发日数 2,000 → 检出率 > 90%。"""
    det = 0
    reps = 10
    for rep in range(reps):
        a = labels_df(2000, p_win=0.45, seed=3000 + rep)
        b = labels_df(2000, p_win=0.40, seed=4000 + rep)
        if delta_p_test(a, b, sims=400, seed=rep)["p_value"] < 0.05:
            det += 1
    assert det / reps > 0.9, det


# ------------------------------------------------- P-09/P-11（T98 audit）

def test_randomize_eligible_bars_restricts_pool():
    """P-09：eligible_bars 限定抽样池——随机时点只落在合格 bar 上；
    布尔掩码与下标数组同一语义（同 seed 同结果）。"""
    bars = synth_bars(3000, seed=4)
    start = 100
    trig = pd.DataFrame({
        "time": bars["time"].iloc[start::50].iloc[:58],
        "situation": "S1", "action": "A3", "side": 1, "level": 100.0})
    idx_arr = np.arange(start, len(bars))
    out_idx = randomize_triggers(trig, bars, seed=7, eligible_bars=idx_arr)
    pos = pd.Index(bars["time"]).get_indexer(pd.to_datetime(out_idx["time"]))
    assert (pos >= start).all()
    mask = np.zeros(len(bars), dtype=bool)
    mask[start:] = True
    out_mask = randomize_triggers(trig, bars, seed=7, eligible_bars=mask)
    pd.testing.assert_frame_equal(out_idx, out_mask)
    # 下标无需有序/去重——语义仍是集合
    shuffled = np.random.default_rng(1).permutation(idx_arr)
    out_shuf = randomize_triggers(trig, bars, seed=7, eligible_bars=shuffled)
    pd.testing.assert_frame_equal(out_idx, out_shuf)


def test_randomize_eligible_bars_default_none_is_full_domain():
    """缺省 None = 全域（行为逐位不变——探针 03 硬约束）。"""
    bars = synth_bars(2000, seed=3)
    trig = pd.DataFrame({"time": bars["time"].iloc[::40][:50],
                         "situation": "S1", "action": "A3", "side": 1,
                         "level": 100.0})
    a = randomize_triggers(trig, bars, seed=11)
    b = randomize_triggers(trig, bars, seed=11, eligible_bars=None)
    pd.testing.assert_frame_equal(a, b)


def test_randomize_eligible_bars_input_validation():
    bars = synth_bars(100, seed=2)
    trig = pd.DataFrame({"time": [bars["time"].iloc[50]],
                         "situation": "S1", "action": "A3", "side": 1,
                         "level": 100.0})
    with pytest.raises(ValueError, match="等长"):
        randomize_triggers(trig, bars, seed=1,
                           eligible_bars=np.array([True, False]))
    with pytest.raises(ValueError, match="越界"):
        randomize_triggers(trig, bars, seed=1, eligible_bars=[5, 101])
    with pytest.raises(ValueError, match="一维"):
        randomize_triggers(trig, bars, seed=1,
                           eligible_bars=np.array([[0, 1], [2, 3]]))


def test_randomize_eligible_bars_empty_bucket_still_rejects():
    """池限定后桶空 → 仍拒（审核三 S3 语义不变，不退化全域抽样）。"""
    bars = synth_bars(100, seed=2)
    trig = pd.DataFrame({"time": [pd.Timestamp("2020-01-07 12:00")],
                         "situation": "S1", "action": "A3", "side": 1,
                         "level": 100.0})
    with pytest.raises(ValueError, match="无候选"):
        randomize_triggers(trig, bars, seed=1, eligible_bars=[0])


def test_plant_delta_uses_positional_index_not_labels():
    """P-11：flip 是位置数组——非 RangeIndex 上旧 .loc 会改错行/抛错；
    修复后按位置翻转、不增行、net_r 取原 win 均值。"""
    n = 40
    base = pd.DataFrame({
        "time": pd.date_range("2020-01-01", periods=n, freq="D"),
        "outcome": ["loss"] * 25 + ["win"] * 15,
        "net_r": [-1.05] * 25 + [1.9] * 15},
        index=[f"r{k}" for k in range(n)])
    planted = plant_delta(base, 0.10, seed=3)
    assert len(planted) == n                       # 不增行
    assert list(planted.index) == list(base.index)
    rng = np.random.default_rng(3)
    loss_pos = np.flatnonzero((base["outcome"] == "loss").to_numpy())
    n_flip = min(len(loss_pos), int(round(0.10 * n)))
    flipped = set(rng.choice(loss_pos, size=n_flip, replace=False).tolist())
    changed = {k for k in range(n)
               if planted["outcome"].iloc[k] != base["outcome"].iloc[k]}
    assert changed == flipped                      # 恰好翻转选中的位置
    r_val = float(base.loc[base["outcome"] == "win", "net_r"].mean())
    for k in flipped:
        assert planted["outcome"].iloc[k] == "win"
        assert planted["net_r"].iloc[k] == pytest.approx(r_val)
    for k in range(n):
        if k not in flipped:
            assert planted["outcome"].iloc[k] == base["outcome"].iloc[k]
            assert planted["net_r"].iloc[k] == pytest.approx(
                base["net_r"].iloc[k])


def test_delta_p_test_rejects_bad_sims():
    a = labels_df(30, p_win=0.5, seed=1)
    b = labels_df(30, p_win=0.4, seed=2)
    with pytest.raises(ValueError, match="sims"):
        delta_p_test(a, b, sims=0, seed=1)
    with pytest.raises(ValueError, match="sims"):
        delta_p_test(a, b, sims=2.5, seed=1)
    with pytest.raises(ValueError, match="sims"):
        delta_p_test(a, b, sims=True, seed=1)


def test_brier_skill_input_validation():
    y = np.array([0.0, 1.0, 1.0, 0.0])
    assert isinstance(brier_skill([0.1, 0.9, 0.9, 0.1], y, 0.5), float)
    with pytest.raises(ValueError, match="空"):
        brier_skill([], [], 0.5)
    with pytest.raises(ValueError, match="形状"):
        brier_skill([0.5, 0.5], y, 0.5)
    with pytest.raises(ValueError, match="形状"):
        brier_skill(np.zeros((2, 2)), y, 0.5)
    with pytest.raises(ValueError, match="有限"):
        brier_skill([0.5, np.nan, 1.0, 0.0], y, 0.5)
    with pytest.raises(ValueError, match="有限"):
        brier_skill([0.5, np.inf, 1.0, 0.0], y, 0.5)
    with pytest.raises(ValueError, match="\[0,1\]"):
        brier_skill([0.5, 1.2, 1.0, 0.0], y, 0.5)
    with pytest.raises(ValueError, match="\[0,1\]"):
        brier_skill([0.5, 0.5, 0.5, 0.5], y, 1.5)
    with pytest.raises(ValueError, match="\[0,1\]"):
        brier_skill([0.5, 0.5, 0.5, 0.5], y, float("nan"))
    with pytest.raises(ValueError, match="0/1"):
        brier_skill([0.5, 0.5, 0.5, 0.5], [0, 1, 2, 0], 0.5)


def test_holm_input_validation():
    assert holm({}) == {}                       # 空 dict 维持既有约定
    with pytest.raises(ValueError, match="p"):
        holm({"a": float("nan")})
    with pytest.raises(ValueError, match="p"):
        holm({"a": float("inf")})
    with pytest.raises(ValueError, match="p"):
        holm({"a": -0.01})
    with pytest.raises(ValueError, match="p"):
        holm({"a": 1.5})
