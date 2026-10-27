"""G4 统计：Deflated Sharpe Ratio（Bailey & López de Prado 2014）与蒙特卡洛回撤分布。

公式（论文 §2/§4，对应实现 stats.deflated_sharpe_probability）：
- PSR(SR*) = Φ( sqrt(n−1)·(SR−SR*) / sqrt(1 − γ₃·SR + ((γ₄−1)/4)·SR²) )
- DSR = PSR(SR₀)，SR₀ = sqrt(V[SR_trials])·((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))
  （γ = Euler–Mascheroni 常数；N=1 时无选择效应，SR₀=0）
- SR/γ₃/γ₄ 全部按**每根 bar**（非年化）口径估计；γ₄ 为普通峰度（正态=3）。
种子约定：所有蒙特卡洛用 numpy default_rng，种子 = sha256(card_id) 前 8 字节。
"""
import math
from statistics import NormalDist

import numpy as np
import pandas as pd
import pytest

from mt5quant.validation import stats as vstats


# ---------------------------------------------------------------- 种子

def test_rng_seed_is_deterministic_and_card_specific():
    assert vstats.rng_seed("H001") == vstats.rng_seed("H001")
    assert vstats.rng_seed("H001") != vstats.rng_seed("H002")
    assert 0 <= vstats.rng_seed("H001") < 2 ** 64


# ---------------------------------------------------------------- SR₀

def test_expected_max_sharpe_single_trial_is_zero():
    assert vstats.expected_max_sharpe(1, 0.5) == 0.0


def test_expected_max_sharpe_zero_variance_is_zero():
    assert vstats.expected_max_sharpe(10, 0.0) == 0.0


def test_expected_max_sharpe_two_trials_known_value():
    """N=2、V[SR]=1：(1−γ)·Φ⁻¹(1/2)=0，只剩 γ·Φ⁻¹(1−1/(2e))≈0.5197。"""
    gamma = vstats.EULER_GAMMA
    expected = gamma * NormalDist().inv_cdf(1.0 - 1.0 / (2.0 * math.e))
    assert vstats.expected_max_sharpe(2, 1.0) == pytest.approx(expected, abs=1e-10)


def test_expected_max_sharpe_grows_with_trials():
    v = 0.02
    assert (vstats.expected_max_sharpe(2, v)
            < vstats.expected_max_sharpe(10, v)
            < vstats.expected_max_sharpe(100, v))


# ---------------------------------------------------------------- DSR

def trending_returns(n=4000, drift=0.0010, vol=0.001, seed=3):
    rng = np.random.default_rng(seed)
    return pd.Series(drift + rng.normal(0.0, vol, n))


def test_deflated_sharpe_matches_manual_formula():
    r = trending_returns(n=600, seed=5)
    sr = vstats.sharpe_per_interval(r)
    skew, kurt = vstats.sample_moments(r)
    trial_sharpes = [sr + 0.02, sr - 0.01, sr + 0.005]
    var = float(np.var(trial_sharpes, ddof=1))
    sr0 = vstats.expected_max_sharpe(3, var)
    n = len(r)
    denom = math.sqrt(1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2)
    expected = NormalDist().cdf(math.sqrt(n - 1) * (sr - sr0) / denom)
    got = vstats.deflated_sharpe_probability(r, n_trials=3,
                                             trial_sharpes=trial_sharpes)
    assert got == pytest.approx(expected, abs=1e-12)


def test_single_trial_equals_psr_at_zero():
    """N=1 → SR₀=0 → DSR 退化为 PSR(0)。"""
    r = trending_returns(n=500, seed=9)
    sr = vstats.sharpe_per_interval(r)
    skew, kurt = vstats.sample_moments(r)
    denom = math.sqrt(1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2)
    expected = NormalDist().cdf(math.sqrt(len(r) - 1) * sr / denom)
    assert vstats.deflated_sharpe_probability(r, n_trials=1) == pytest.approx(
        expected, abs=1e-12)


def test_deflated_sharpe_drops_with_more_trials():
    """同一收益流，试验次数越多（选择余地越大），DSR 越严。"""
    rng = np.random.default_rng(4)
    r = pd.Series(0.0002 + rng.normal(0.0, 0.002, 2000))   # 每 bar SR≈0.1
    trials = [float(x) for x in np.linspace(-0.05, 0.05, 50)]
    p2 = vstats.deflated_sharpe_probability(r, n_trials=2, trial_sharpes=trials)
    p50 = vstats.deflated_sharpe_probability(r, n_trials=50, trial_sharpes=trials)
    assert p50 < p2 < 1.0


def test_deflated_sharpe_overfit_noise_is_low():
    """计划失败测试 2（G4 部分）：噪声上挑出来的"最优"收益流，DSR 必须显著低于 0.95。"""
    rng = np.random.default_rng(42)
    n = 2000
    trial_sharpes = []
    best_sr, best_returns = -9.0, None
    for _ in range(20):                      # 20 次试验，全部是噪声
        r = pd.Series(rng.normal(0.0, 0.004, n))
        sr = vstats.sharpe_per_interval(r)
        trial_sharpes.append(sr)
        if sr > best_sr:
            best_sr, best_returns = sr, r
    p = vstats.deflated_sharpe_probability(best_returns, n_trials=20,
                                           trial_sharpes=trial_sharpes)
    assert p < 0.95


def test_deflated_sharpe_strong_trend_is_high():
    r = trending_returns(n=8000, seed=6)
    p = vstats.deflated_sharpe_probability(r, n_trials=1)
    assert p > 0.999


def test_deflated_sharpe_rejects_degenerate_inputs():
    with pytest.raises(ValueError):
        vstats.deflated_sharpe_probability(pd.Series([0.01]), n_trials=1)
    with pytest.raises(ValueError):
        vstats.deflated_sharpe_probability(pd.Series([0.01] * 10), n_trials=1)


# ------------------------------------------------------- 块自助夏普（T21a/C3）

def test_block_bootstrap_sharpes_shape_and_determinism():
    idx = pd.date_range("2024-01-01", periods=500, freq="h")
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0.0005, 0.004, 500), index=idx)
    a = vstats.block_bootstrap_sharpes(r, window_len=200, block_len=24,
                                       sims=50, seed=11,
                                       bars_per_year=6000.0)
    b = vstats.block_bootstrap_sharpes(r, window_len=200, block_len=24,
                                       sims=50, seed=11,
                                       bars_per_year=6000.0)
    assert a.shape == (50,)
    assert np.array_equal(a, b)


def test_block_bootstrap_sharpes_drift_sign():
    """正漂移收益的自助夏普分布 p5 也显著为正；零漂移分布的中位数在 0 附近。"""
    idx = pd.date_range("2024-01-01", periods=1200, freq="h")
    rng = np.random.default_rng(7)
    drift = pd.Series(0.0015 + rng.normal(0.0, 0.003, 1200), index=idx)
    noise = pd.Series(rng.normal(0.0, 0.003, 1200), index=idx)
    d = vstats.block_bootstrap_sharpes(drift, window_len=300, block_len=24,
                                       sims=100, seed=5, bars_per_year=6000.0)
    n = vstats.block_bootstrap_sharpes(noise, window_len=300, block_len=24,
                                       sims=100, seed=5, bars_per_year=6000.0)
    assert float(np.percentile(d, 5)) > 5.0
    assert abs(float(np.median(n))) < 5.0


def test_block_bootstrap_sharpes_annualization_scales_with_bpy():
    """年化口径与 report 一致：bars_per_year ×4 → 夏普 ×2（sqrt 年化）。"""
    idx = pd.date_range("2024-01-01", periods=400, freq="h")
    r = pd.Series(np.random.default_rng(9).normal(0.0004, 0.003, 400),
                  index=idx)
    s1 = vstats.block_bootstrap_sharpes(r, window_len=200, block_len=24,
                                        sims=20, seed=2, bars_per_year=1000.0)
    s4 = vstats.block_bootstrap_sharpes(r, window_len=200, block_len=24,
                                        sims=20, seed=2, bars_per_year=4000.0)
    assert np.allclose(s4, s1 * 2.0)


def test_block_bootstrap_sharpes_derives_bpy_from_datetime_index():
    """未传 bars_per_year 时按 DatetimeIndex 实测（与 observed_bars_per_year
    同口径）；跨度 ≥30 天才能实测。"""
    from mt5quant.backtest.report import observed_bars_per_year
    idx = pd.date_range("2024-01-01", periods=24 * 60, freq="h")   # 60 天
    r = pd.Series(np.random.default_rng(2).normal(0.0005, 0.003, len(idx)),
                  index=idx)
    a = vstats.block_bootstrap_sharpes(r, window_len=500, block_len=24,
                                       sims=10, seed=4)
    b = vstats.block_bootstrap_sharpes(
        r, window_len=500, block_len=24, sims=10, seed=4,
        bars_per_year=observed_bars_per_year(r.index))
    assert np.array_equal(a, b)


def test_block_bootstrap_sharpes_rejects_bad_args():
    idx = pd.date_range("2024-01-01", periods=100, freq="h")
    r = pd.Series(0.001, index=idx)
    with pytest.raises(ValueError):   # 窗口太短：夏普无定义
        vstats.block_bootstrap_sharpes(r, window_len=1, block_len=24,
                                       sims=5, seed=1)
    with pytest.raises(ValueError):   # 块长超过样本
        vstats.block_bootstrap_sharpes(r, window_len=50, block_len=101,
                                       sims=5, seed=1)
    with pytest.raises(ValueError):   # 非时间索引且未给 bars_per_year
        vstats.block_bootstrap_sharpes(pd.Series(np.linspace(1.0, 2.0, 300)),
                                       window_len=100, block_len=24,
                                       sims=5, seed=1)
    with pytest.raises(ValueError):   # 时间索引但跨度 < 30 天，无法实测
        vstats.block_bootstrap_sharpes(r, window_len=50, block_len=24,
                                       sims=5, seed=1)


# ---------------------------------------------------------------- 蒙特卡洛回撤

def test_mc_drawdown_reproducible_with_same_seed():
    r = trending_returns(n=300, seed=8)
    a = vstats.mc_drawdown_distribution(r, sims=200, seed=vstats.rng_seed("H001"))
    b = vstats.mc_drawdown_distribution(r, sims=200, seed=vstats.rng_seed("H001"))
    assert a.p50 == b.p50 and a.p95 == b.p95


def test_mc_drawdown_all_nonpositive_and_percentile_centered():
    r = trending_returns(n=300, seed=8)
    dist = vstats.mc_drawdown_distribution(r, sims=200, seed=vstats.rng_seed("H002"))
    assert all(d <= 0.0 for d in dist.drawdowns)
    assert dist.percentile_of(dist.p50) == pytest.approx(50.0, abs=1.0)
    # 零回撤（比谁都浅）→ 0 分位；比 p5 还深一倍 → 几乎所有路径都更浅
    assert dist.percentile_of(0.0) == 0.0
    assert dist.percentile_of(dist.p5 * 2.0) >= 95.0


def test_mc_drawdown_summary_is_jsonable():
    import json
    r = trending_returns(n=200, seed=10)
    dist = vstats.mc_drawdown_distribution(r, sims=100, seed=1)
    payload = json.dumps(dist.summary())
    assert "p95" in payload


# ------------------------------------------- T21c/H6：分批 + 块自助重采样

def test_mc_drawdown_block_len_one_matches_legacy_iid_stream():
    """block_len=1（缺省）与旧一次性 iid 实现同 seed 逐位一致（回归保护）。"""
    r = trending_returns(n=300, seed=8)
    seed = vstats.rng_seed("H001")
    dist = vstats.mc_drawdown_distribution(r, sims=200, seed=seed)
    # 手工按旧实现复算（一次性矩阵、iid 抽样、含前置 1 的权益）
    arr = np.asarray(r, dtype="float64")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(200, len(arr)))
    sampled = arr[idx]
    equity = np.concatenate(
        [np.ones((200, 1)), np.cumprod(1.0 + sampled, axis=1)], axis=1)
    peaks = np.maximum.accumulate(equity, axis=1)
    dd = equity / peaks - 1.0
    assert list(dist.drawdowns) == [float(x) for x in dd.min(axis=1)]


def test_mc_drawdown_block_bootstrap_reproducible_and_sane():
    r = trending_returns(n=2000, seed=4)
    a = vstats.mc_drawdown_distribution(r, sims=100, seed=9, block_len=24)
    b = vstats.mc_drawdown_distribution(r, sims=100, seed=9, block_len=24)
    assert a.p50 == b.p50 and a.p95 == b.p95
    assert all(d <= 0.0 for d in a.drawdowns)
    assert a.summary()["block_len"] == 24


def test_mc_drawdown_batched_memory_under_500mb():
    """H6/探针 P4：37 万根 × 1000 次重采样的峰值内存 < 500 MB（tracemalloc）。

    旧实现一次性物化 sims×n 矩阵（约 3 GB×多个）；分批（每批 ≤20）+
    就地运算后应远低于 500 MB。下限断言确认 tracemalloc 确实看到了
    批矩阵（防止 numpy 分配不被追踪导致空断言）。
    """
    import tracemalloc
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.0002, 0.004, 370_000))
    tracemalloc.start()
    try:
        dist = vstats.mc_drawdown_distribution(r, sims=1000, seed=3,
                                               block_len=288)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(dist.drawdowns) == 1000
    assert peak > 50_000_000        # ≈ 每批 2×60 MB 的矩阵确实被追踪
    assert peak < 500_000_000       # H6 验收线


def test_mc_drawdown_block_len1_memory_under_500mb():
    """T70/F6：缺省 block_len=1（iid）时**起点矩阵**也须逐批抽取。

    旧实现把 (sims, n_blocks) 起点一次性抽完：block_len=288 时 n_blocks 只有
    ~1285 尚可，但缺省 block_len=1 时 n_blocks = n，37 万根 × 1000 sims 的
    int64 起点矩阵约 3 GB——F6 指出的内存陷阱。逐批抽取后峰值须 < 500 MB。
    """
    import tracemalloc
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.0002, 0.004, 370_000))
    tracemalloc.start()
    try:
        dist = vstats.mc_drawdown_distribution(r, sims=1000, seed=3)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(dist.drawdowns) == 1000
    assert peak > 50_000_000        # 批矩阵确实被追踪（防空断言）
    assert peak < 500_000_000       # F6 验收线


def test_mc_drawdown_batched_starts_match_oneshot_block_stream():
    """T70/F6：起点改逐批抽取后，与旧实现（一次性抽完 sims×n_blocks 起点、
    再分批 gather）同种子**逐位一致**。

    rng.integers 按行主序填充且 numpy Generator 的每次调用共享同一条随机流
    （同一 bounds/dtype 下分批拼接与一次抽完逐值相同），故分批只改变分配
    时机、不改变随机流——这是不更新任何快照的依据。
    """
    r = trending_returns(n=5000, seed=6)
    seed = vstats.rng_seed("H003")
    sims, bl = 100, 288
    dist = vstats.mc_drawdown_distribution(r, sims=sims, seed=seed,
                                           block_len=bl)
    # 旧实现复算：一次性抽 (sims, n_blocks) 起点，再按同批大小 gather + 就地回撤
    arr = np.asarray(r, dtype="float64")
    n = len(arr)
    n_blocks = -(-n // bl)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - bl + 1, size=(sims, n_blocks))
    mdds = []
    for b0 in range(0, sims, vstats._MC_SIMS_PER_BATCH):
        b1 = min(b0 + vstats._MC_SIMS_PER_BATCH, sims)
        sampled = vstats._gather_blocks(arr, starts[b0:b1], bl)[:, :n]
        np.add(sampled, 1.0, out=sampled)
        np.cumprod(sampled, axis=1, out=sampled)
        peaks = np.maximum.accumulate(sampled, axis=1)
        np.maximum(peaks, 1.0, out=peaks)
        np.divide(sampled, peaks, out=sampled)
        sampled -= 1.0
        mdds.extend(float(x) for x in sampled.min(axis=1))
    assert list(dist.drawdowns) == mdds
