"""T75 概率模式评估：随机化/Holm/植入/Brier——合成数据测试。

全部用合成数据（蓝图 v4 §2 禁止事项允许项）；不触真实局面标签。
"""
import numpy as np
import pandas as pd
import pytest

from mt5quant.backtest.engine_a import atr_series
from mt5quant.validation.prob_mode import (
    CLUSTER_GAP_DAYS, MIN_CLUSTERS_PER_GROUP, cluster_permutation_test,
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


# ------------------------------------------- 季度块置换（T101/P-02 处置）

def _day_labels(days):
    """显式构造逐日一条的标签表：days = [(日期, 'win'|'loss'), ...]。"""
    rows = [{"time": pd.Timestamp(d) + pd.Timedelta(hours=10),
             "outcome": o, "net_r": 1.9 if o == "win" else -1.05,
             "situation": "S1", "action": "A3", "side": 1, "level": 100.0}
            for d, o in days]
    return pd.DataFrame(rows)


_Q1_MIXED_REAL = [("2021-01-04", "win"), ("2021-01-05", "loss"),
                  ("2021-02-01", "win"), ("2021-02-02", "win")]
_Q1_MIXED_NULL = [("2021-01-06", "loss"), ("2021-01-07", "win"),
                  ("2021-02-03", "loss"), ("2021-02-04", "loss")]


def test_delta_p_test_quarter_differs_from_month_on_cross_month():
    """T101：同季跨月（1+2 月）数据上，季度块与月块的置换空间不同——
    p 值应不同（当前实现 quarter 静默按月处理，二者相同 → 红）。"""
    real = _day_labels(_Q1_MIXED_REAL)
    null = _day_labels(_Q1_MIXED_NULL)
    pm = delta_p_test(real, null, block="month", sims=500, seed=7)
    pq = delta_p_test(real, null, block="quarter", sims=500, seed=7)
    assert pq["p_value"] != pm["p_value"]


def test_delta_p_test_quarter_equals_month_within_single_months():
    """T101：季度从不拆分月份——数据只落在互不同月的月份里时，月块与
    季度块的池逐月一一对应，结果除 block 字段外逐位相同。两个用例：
    单月数据；1 月+4 月（不同季度、各自单月）。"""
    for days_r, days_n in (
            ([("2021-01-04", "win"), ("2021-01-05", "loss"),
              ("2021-01-06", "win")],
             [("2021-01-07", "loss"), ("2021-01-08", "loss"),
              ("2021-01-09", "win")]),
            ([("2021-01-04", "win"), ("2021-01-05", "loss"),
              ("2021-04-05", "win"), ("2021-04-06", "win")],
             [("2021-01-06", "loss"), ("2021-01-07", "win"),
              ("2021-04-07", "loss"), ("2021-04-08", "loss")])):
        real = _day_labels(days_r)
        null = _day_labels(days_n)
        pm = delta_p_test(real, null, block="month", sims=300, seed=11)
        pq = delta_p_test(real, null, block="quarter", sims=300, seed=11)
        assert {**pm, "block": None} == {**pq, "block": None}


def test_delta_p_test_rejects_bad_block():
    """T101：block 只认 month/quarter，入口校验（空标签也不得静默
    返回 p=1.0）。"""
    real = _day_labels(_Q1_MIXED_REAL)
    null = _day_labels(_Q1_MIXED_NULL)
    with pytest.raises(ValueError):
        delta_p_test(real, null, block="bogus", sims=100, seed=1)
    empty = pd.DataFrame(columns=["time", "outcome"])
    with pytest.raises(ValueError):
        delta_p_test(empty, empty, block="bogus", sims=100, seed=1)


def test_delta_p_test_quarter_deterministic_and_echoed():
    """T101：季度块同 seed 确定、block 字段回显。"""
    real = _day_labels(_Q1_MIXED_REAL)
    null = _day_labels(_Q1_MIXED_NULL)
    r1 = delta_p_test(real, null, block="quarter", sims=200, seed=5)
    r2 = delta_p_test(real, null, block="quarter", sims=200, seed=5)
    assert r1 == r2
    assert r1["block"] == "quarter"
    assert delta_p_test(real, null, sims=100, seed=5)["block"] == "month"


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


# ------------------------------------- T104 批 2（P2-09 方法批准门，丁-6）

def test_method_registry_statuses():
    """P2-09：方法登记——月内置换在 v2 成簇负对照失败（w7=11%>5%）→
    failed_cluster_control；季度块为 v2 §6 预写选项一，v3 试验 w7=8%>5%
    按预写规则不采纳 → not_adopted。两者均未获批。"""
    from mt5quant.validation.prob_mode import METHOD_REGISTRY
    assert METHOD_REGISTRY["month-within"]["status"] == "failed_cluster_control"
    assert METHOD_REGISTRY["quarter"]["status"] == "not_adopted"
    for entry in METHOD_REGISTRY.values():
        assert entry.get("adopted") is False
        assert entry.get("note")            # 人读依据（含报告出处）


def test_primary_method_approved_false_while_cluster_control_fails():
    """P2-09：主检验方法未获批 → (False, reason)——reason 写明成簇负对照
    失败（消费方：bt prob round-report 的推进结论门）。"""
    from mt5quant.validation.prob_mode import primary_method_approved
    approved, reason = primary_method_approved()
    assert approved is False
    assert "成簇" in reason and "未批准" in reason


# ------------------------------------- T106 簇单位置换（cluster-permutation-v1）
#
# 预注册设计（docs/superpowers/plans/2026-10-30-t106-cluster-permutation.md
# §一）：CLUSTER_GAP_DAYS=10 **日历日** 邻接聚簇（跨月不拆）、混合簇多数
# 归属计数（平局计 real）、整簇交换、任一组簇数 < MIN_CLUSTERS_PER_GROUP=5
# 或总簇数 < 10 → 不可检验（不打碎簇）、审计输出（簇数/簇大小分布/两组
# 日数观测与置换均值/min_achievable_p）、同 seed 确定、method_id 回显。


def _pairs_labels(rows):
    """rows = [(日期串, 'real'|'null', 'win'|'loss'|None), ...] → 两张标签表。

    None = 该组该日无触发（构造混合簇/纯簇用）。"""
    sel = {"real": [], "null": []}
    for d, grp, o in rows:
        if o is not None:
            sel[grp].append((d, o))
    return _day_labels(sel["real"]), _day_labels(sel["null"])


def _blocks_rows(n_clusters, days_per, spacing, *, real_days, null_days,
                 outcome, start="2021-01-04"):
    """n_clusters 个块（各 days_per 个连续日历日、块起点间隔 spacing 天），
    每簇按 real_days/null_days 取该块前若干天，outcome(day, grp) 给标签。"""
    from datetime import timedelta
    t0 = pd.Timestamp(start)
    rows = []
    for c in range(n_clusters):
        base = t0 + timedelta(days=c * spacing)
        for j in range(days_per):
            d = (base + timedelta(days=j)).strftime("%Y-%m-%d")
            if j < real_days:
                rows.append((d, "real", outcome(d, j, "real", c)))
            if j < null_days:
                rows.append((d, "null", outcome(d, j, "null", c)))
    return rows


def test_cluster_constants_preregistered():
    """T106 §一：设计常量按预注册值（不得偏离）。"""
    assert CLUSTER_GAP_DAYS == 10
    assert MIN_CLUSTERS_PER_GROUP == 5


def test_cluster_ids_calendar_days_cross_month_not_split():
    """簇构造：排序后间隔 ≤10 **日历日** 归同簇（跨月不拆）；>10 开新簇。
    恰好 10 日历日仍同簇（≤ 含等号）。"""
    from mt5quant.validation.prob_mode import _cluster_ids_for_days
    d = lambda s: pd.Timestamp(s).date()          # noqa: E731
    # 跨 1 月末的连续触发（1/25 与 2/2 间隔 8 日历日）→ 同簇
    assert list(_cluster_ids_for_days([d("2021-01-25"), d("2021-02-02")])) == [0, 0]
    # 间隔恰 10 日历日 → 同簇；11 → 新簇
    assert list(_cluster_ids_for_days([d("2021-01-01"), d("2021-01-11")])) == [0, 0]
    assert list(_cluster_ids_for_days([d("2021-01-01"), d("2021-01-12")])) == [0, 1]
    # 多日链式：1,5,9,13,20 —— 间隔 4,4,4 均 ≤10 同簇；13→20 间隔 7 同簇
    # （链式按相邻间隔，非首尾距离）→ 全同簇
    assert list(_cluster_ids_for_days(
        [d("2021-01-01"), d("2021-01-05"), d("2021-01-09"), d("2021-01-13"),
         d("2021-01-20")])) == [0, 0, 0, 0, 0]


def test_cluster_cross_month_cluster_reported_and_small_n_untestable():
    """跨月连续触发不被 GAP 拆散（n_clusters 如实=2）——簇太少 → 不可检验
    返回（不抛异常、p=None、不打碎簇凑样本）。"""
    rows = _blocks_rows(2, 12, 25, real_days=8, null_days=8,
                        outcome=lambda d, j, g, c: "win" if (j % 2 == 0) else "loss")
    # 块 0 覆盖 2021-01-04..01-15、块 1 覆盖 2021-01-29..02-09（块间隔 25 天
    # >10 → 两簇；块 1 本身跨 1 月末）
    real, null = _pairs_labels(rows)
    res = cluster_permutation_test(real, null, sims=200, seed=5)
    assert res["testable"] is False
    assert res["p_value"] is None
    assert res["n_clusters"] == 2              # 跨月块没有被拆
    assert res["reason"] and "不可检验" in res["reason"]


def test_cluster_majority_attribution_and_tie_counts_real():
    """多数归属+平局规则：real 日数>null → real 簇；< → null；**平局计
    real**（确定性）。12 簇 = 6 纯 real + 4 纯 null + 1 平局 + 1 null 多数
    → k_real=7、k_null=5（可检验）。"""
    rows = []
    # 6 纯 real 簇（4 天）、4 纯 null 簇、1 平局簇（2+2 → 计 real）、
    # 1 null 多数簇（1+3）
    specs = ([(4, 0)] * 6 + [(0, 4)] * 4 + [(2, 2), (1, 3)])
    for c, (nr, nn) in enumerate(specs):
        base = pd.Timestamp("2021-01-04") + pd.Timedelta(days=c * 40)
        for j in range(max(nr, nn)):
            d = (base + pd.Timedelta(days=j)).strftime("%Y-%m-%d")
            if j < nr:
                rows.append((d, "real", "win" if j % 2 == 0 else "loss"))
            if j < nn:
                rows.append((d, "null", "loss" if j % 2 == 0 else "win"))
    real, null = _pairs_labels(rows)
    res = cluster_permutation_test(real, null, sims=100, seed=9)
    assert res["testable"] is True
    assert res["n_clusters"] == 12
    assert res["k_real"] == 7                  # 6 纯 + 1 平局
    assert res["k_null"] == 5                  # 4 纯 + 1 null 多数


def test_cluster_min_group_clusters_untestable():
    """任一组目标簇数 <5 → 不可检验（12 簇但 null 侧仅 3 簇）。"""
    rows = []
    for c in range(12):
        base = pd.Timestamp("2021-01-04") + pd.Timedelta(days=c * 40)
        nr = 4 if c < 9 else 0                 # 9 real 簇 + 3 null 簇
        nn = 0 if c < 9 else 4
        for j in range(4):
            d = (base + pd.Timedelta(days=j)).strftime("%Y-%m-%d")
            if j < nr:
                rows.append((d, "real", "win" if j % 2 == 0 else "loss"))
            if j < nn:
                rows.append((d, "null", "loss" if j % 2 == 0 else "win"))
    real, null = _pairs_labels(rows)
    res = cluster_permutation_test(real, null, sims=100, seed=3)
    assert res["testable"] is False
    assert res["k_real"] == 9 and res["k_null"] == 3
    assert res["p_value"] is None
    assert "5" in res["reason"]                # 写明门槛


def test_cluster_empty_side_untestable():
    real, _ = _pairs_labels([("2021-01-04", "real", "win")])
    empty = pd.DataFrame(columns=["time", "outcome"])
    res = cluster_permutation_test(real, empty, sims=50, seed=1)
    assert res["testable"] is False and res["p_value"] is None


def _correlated_cluster_frames(rep):
    """簇相关结构（w7 型定向构造）：40 簇（real/null 交替）× 10 连续日、
    周期 25 天（簇间隔 15 天 > GAP → 分簇；任何 31 天月份必跨 ≥2 个相邻
    簇 → 每月两组并存——月块 day 置换确实在打散簇）；每簇共享热/冷状态
    （触发赢率 0.85/0.15，组内强正相关），每日 5 个触发独立取标签。
    real 侧 20 簇中 12 热、null 侧 8 热（簇级失衡 0.2）：日级（独立日近
    似）z≈3.6 拒绝、簇级 z≈1.26 不拒绝——day 置换 null 过窄被纠正。"""
    rng = np.random.default_rng(5000 + rep)
    rows_r: list[tuple] = []
    rows_n: list[tuple] = []
    for c in range(40):
        grp = "real" if c % 2 == 0 else "null"
        hot = (c // 2) < (12 if grp == "real" else 8)
        p_win = 0.85 if hot else 0.15
        base = pd.Timestamp("2021-01-04") + pd.Timedelta(days=c * 25)
        for j in range(10):
            d = base + pd.Timedelta(days=j)
            for k in range(5):
                win = rng.random() < p_win
                (rows_r if grp == "real" else rows_n).append(
                    (d + pd.Timedelta(hours=3 * k + 1),
                     "win" if win else "loss", 1.9 if win else -1.05))
    real = pd.DataFrame([{"time": t, "outcome": o, "net_r": nr,
                          "situation": "S1", "action": "A3", "side": 1,
                          "level": 100.0} for t, o, nr in rows_r])
    null = pd.DataFrame([{"time": t, "outcome": o, "net_r": nr,
                          "situation": "S1", "action": "A3", "side": 1,
                          "level": 100.0} for t, o, nr in rows_n])
    return real, null


def test_day_permutation_overrejects_cluster_correlated_cluster_wider():
    """定向（T106 计划二）：簇相关下 day（月块）置换 null 过窄（过度拒绝）
    ——cluster 置换整簇交换，null 分布更宽、拒绝率回到名义附近。"""
    rej_day = rej_clu = 0
    reps = 10
    for rep in range(reps):
        real, null = _correlated_cluster_frames(rep)
        p_day = delta_p_test(real, null, block="month", sims=300,
                             seed=rep)["p_value"]
        p_clu = cluster_permutation_test(real, null, sims=300,
                                         seed=rep)["p_value"]
        assert p_clu is not None               # 30 簇、15/15 → 可检验
        rej_day += p_day < 0.05
        rej_clu += p_clu < 0.05
    assert rej_day >= 8, (rej_day, rej_clu)    # day 置换过窄 → 高拒绝
    assert rej_clu <= 3, (rej_day, rej_clu)    # cluster 置换 null 更宽


def test_cluster_audit_outputs_resolution_determinism_method_id():
    """审计输出键完备（簇数/簇大小分布/两组日数观测与置换均值/
    min_achievable_p=max(1/C(n,k), 1/(B+1))/k_real/k_null）；同 seed 确定；
    p ≥ 1/(1+B)；method_id 回显。"""
    rng = np.random.default_rng(21)
    rows = []
    for c in range(20):
        base = pd.Timestamp("2021-01-04") + pd.Timedelta(days=c * 40)
        real_major = c < 10                    # 10 real 多数簇 + 10 null 多数簇
        nr, nn = (3, 2) if real_major else (2, 3)
        for j in range(3):
            d = (base + pd.Timedelta(days=j)).strftime("%Y-%m-%d")
            if j < nr:
                rows.append((d, "real", "win" if rng.random() < 0.5 else "loss"))
            if j < nn:
                rows.append((d, "null", "win" if rng.random() < 0.4 else "loss"))
    real = pd.DataFrame(
        [{"time": d, "outcome": o} for d, g, o in rows if g == "real"])
    null = pd.DataFrame(
        [{"time": d, "outcome": o} for d, g, o in rows if g == "null"])
    r1 = cluster_permutation_test(real, null, sims=300, seed=7)
    r2 = cluster_permutation_test(real, null, sims=300, seed=7)
    assert r1 == r2                                   # 同 seed 逐位确定
    # 审计键完备（B1 复审补断言）
    for key in ("n_clusters", "cluster_size_summary", "n_days_real",
                "n_days_null", "perm_mean_days_real", "perm_mean_days_null",
                "min_achievable_p", "k_real", "k_null", "method_id"):
        assert key in r1, key
    assert r1["method_id"] == "cluster-permutation-v1"
    assert r1["testable"] is True
    # 构造：20 簇（10 real 多数 + 10 null 多数，各含对方少量日）
    assert r1["k_real"] == 10 and r1["k_null"] == 10
    assert r1["n_clusters"] == 20
    # 分辨率下限 = max(1/C(n,k), 1/(B+1))；p ≥ 1/(B+1)
    import math as _m
    comb = _m.comb(20, 10)
    assert r1["min_achievable_p"] == pytest.approx(
        max(1.0 / comb, 1.0 / 301), rel=1e-12)
    assert r1["p_value"] >= 1.0 / 301
    # 多数归属平局计 real 的口径由簇构造间接覆盖（无平局簇：3/2 与 2/3）
    assert r1["n_days_real"] == 3 * 10 + 2 * 10
    assert r1["n_days_null"] == 2 * 10 + 3 * 10
