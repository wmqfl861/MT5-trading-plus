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
