"""G4 统计：Deflated Sharpe Ratio 与蒙特卡洛回撤分布（P4/T12）。

Deflated Sharpe Ratio——Bailey & López de Prado（2014）*The Deflated Sharpe
Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality*，
Journal of Portfolio Management 40(5)：

- 每根 bar 的 Sharpe（**非年化**）：SR = mean(r) / std(r, ddof=1)；
- PSR(SR*) = Φ( sqrt(n−1)·(SR − SR*) / sqrt(1 − γ₃·SR + ((γ₄−1)/4)·SR²) )
  （n = 收益观测数，γ₃ = 偏度，γ₄ = **普通**峰度，正态分布 γ₄=3；
  分母项来自 SR 估计量的渐近方差，见论文式 (9)）；
- DSR = PSR(SR₀)：把"无技能零假设下 N 次试验能见到的最大 Sharpe"作为门槛，
  SR₀ = sqrt(V[SR_trials])·((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))
  （论文式 (4)；γ ≈ 0.5772156649 为 Euler–Mascheroni 常数，e = exp(1)；
  N = 1 时无选择效应，SR₀ = 0，DSR 退化为 PSR(0)）；
- V[SR_trials] 取各次试验 Sharpe 的样本方差（同一 bar 频率口径）；
  trial_sharpes 缺省或不足 2 个 → V=0 → SR₀=0（如实：没有可用选择效应估计）。

蒙特卡洛回撤分布：对 bar 收益做 iid 自助重采样（bootstrap），逐条路径算
最大回撤（相对历史峰值，负比例），得到 [p1, p5, p50, p95, p99] 分布；
策略实际回撤落在分布的百分位过高（差于几乎所有重采样路径）→ 路径风险
与收益分布不一致，报警。

种子约定（可复现，写进关卡报告）：所有蒙特卡洛用 numpy default_rng，
种子 = sha256(card_id) 前 8 字节大端整数（rng_seed）。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np
import pandas as pd

EULER_GAMMA = 0.5772156649015329


def rng_seed(card_id: str) -> int:
    """蒙特卡洛种子：sha256(card_id) 前 8 字节大端整数（同一卡可复现）。"""
    digest = hashlib.sha256(card_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _as_float_array(returns) -> np.ndarray:
    arr = np.asarray(pd.Series(returns).astype("float64"), dtype="float64")
    if arr.ndim != 1:
        raise ValueError(f"returns 必须是一维序列，收到 shape={arr.shape}")
    return arr


def sharpe_per_interval(returns) -> float:
    """每根 bar（非年化）的 Sharpe：mean/std(ddof=1)。

    观测 < 2 或 std = 0 → ValueError（SR 无定义，调用方 fail-closed）。
    """
    arr = _as_float_array(returns)
    if len(arr) < 2:
        raise ValueError(f"收益观测不足（{len(arr)} < 2），Sharpe 无定义")
    std = float(arr.std(ddof=1))
    # 相对容差：浮点上"恒定序列"的 std 可能是 1e-18 而非 0，同样无意义
    if std <= 1e-12 * max(1.0, float(np.abs(arr).max())):
        raise ValueError("收益无方差（恒定值），Sharpe 无定义")
    return float(arr.mean()) / std


def sample_moments(returns) -> tuple[float, float]:
    """(偏度 γ₃, 普通峰度 γ₄)：中心矩 m_k/n 的比值（矩估计，正态 → (0, 3)）。"""
    arr = _as_float_array(returns)
    if len(arr) < 2:
        raise ValueError("矩估计需要 ≥2 个观测")
    m2 = float(np.mean((arr - arr.mean()) ** 2))
    if not m2 > 0.0:
        raise ValueError("收益无方差，矩估计无定义")
    m3 = float(np.mean((arr - arr.mean()) ** 3))
    m4 = float(np.mean((arr - arr.mean()) ** 4))
    return m3 / m2 ** 1.5, m4 / (m2 * m2)


def expected_max_sharpe(n_trials: int, variance_of_sharpes: float) -> float:
    """SR₀（论文式 (4)）：N 次试验在无技能零假设下的期望最大 Sharpe。

    N ≤ 1 或 V[SR] = 0 → 0（无选择效应可修正）。
    """
    if n_trials <= 1 or variance_of_sharpes <= 0.0:
        return 0.0
    gamma = EULER_GAMMA
    term = ((1.0 - gamma) * NormalDist().inv_cdf(1.0 - 1.0 / n_trials)
            + gamma * NormalDist().inv_cdf(1.0 - 1.0 / (n_trials * math.e)))
    return math.sqrt(variance_of_sharpes) * term


def deflated_sharpe_probability(returns, *, n_trials: int,
                                trial_sharpes=None) -> float:
    """DSR = PSR(SR₀) ∈ (0, 1)：给定试验次数与各次试验 Sharpe，本收益流
    在校正选择效应与非正态后"真实 SR > 0"的概率。

    - trial_sharpes ≥ 2 个 → V[SR] 用样本方差（ddof=1）；否则 SR₀=0；
    - 分母 sqrt(1 − γ₃SR + ((γ₄−1)/4)SR²) ≤ 0（极端矩，公式失真）→ 返回 0.0
      （fail-closed：宁可拒绝，不猜）；
    - 观测 < 2 或无方差 → ValueError。
    """
    sr = sharpe_per_interval(returns)
    skew, kurt = sample_moments(returns)
    arr = _as_float_array(returns)
    n = len(arr)

    variance = 0.0
    if trial_sharpes is not None and len(trial_sharpes) >= 2:
        variance = float(np.var(np.asarray(trial_sharpes, dtype="float64"),
                                ddof=1))
    sr0 = expected_max_sharpe(n_trials, variance)

    inner = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if inner <= 0.0:
        return 0.0
    z = math.sqrt(n - 1) * (sr - sr0) / math.sqrt(inner)
    return NormalDist().cdf(z)


@dataclass(frozen=True)
class DrawdownDistribution:
    """自助重采样得到的最大回撤分布（负比例；越深越差）。"""

    sims: int
    seed: int
    drawdowns: tuple[float, ...]
    percentiles: dict = field(default_factory=dict)

    @property
    def p1(self) -> float:
        return self.percentiles["p1"]

    @property
    def p5(self) -> float:
        return self.percentiles["p5"]

    @property
    def p50(self) -> float:
        return self.percentiles["p50"]

    @property
    def p95(self) -> float:
        return self.percentiles["p95"]

    @property
    def p99(self) -> float:
        return self.percentiles["p99"]

    def percentile_of(self, max_drawdown: float) -> float:
        """实际回撤比多少比例的重采样路径**更严重**（0=比谁都浅，
        100=比所有路径都深）——高百分位 = 实际路径的回撤异常深。"""
        arr = np.asarray(self.drawdowns)
        return float((arr > max_drawdown).mean() * 100.0)

    def summary(self) -> dict:
        """JSON 可写的摘要（drawdowns 不落盘，凭 seed 可复现）。"""
        return {"sims": self.sims, "seed": self.seed,
                **{k: float(v) for k, v in self.percentiles.items()}}


def max_drawdown_of_returns(returns) -> float:
    """收益序列的最大回撤（相对历史峰值，负比例）。"""
    arr = _as_float_array(returns)
    equity = np.cumprod(1.0 + arr)
    peak = np.maximum.accumulate(np.concatenate(([1.0], equity)))
    dd = np.concatenate(([0.0], equity / peak[1:] - 1.0))
    return float(dd.min())


def mc_drawdown_distribution(returns, *, sims: int, seed: int) -> DrawdownDistribution:
    """自助重采样 bar 收益 → 最大回撤分布（同 seed 逐位可复现）。"""
    arr = _as_float_array(returns)
    if sims < 2:
        raise ValueError(f"sims 必须 ≥2，收到 {sims}")
    if len(arr) < 2:
        raise ValueError("收益观测不足，无法重采样")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(sims, len(arr)))
    sampled = arr[idx]
    equity = np.concatenate(
        [np.ones((sims, 1)), np.cumprod(1.0 + sampled, axis=1)], axis=1)
    peaks = np.maximum.accumulate(equity, axis=1)
    dd = equity / peaks - 1.0
    mdds = dd.min(axis=1)
    pcts = {f"p{q}": float(np.percentile(mdds, q))
            for q in (1, 5, 50, 95, 99)}
    return DrawdownDistribution(sims=int(sims), seed=int(seed),
                                drawdowns=tuple(float(x) for x in mdds),
                                percentiles=pcts)
