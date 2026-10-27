"""G4 统计：Deflated Sharpe Ratio 与蒙特卡洛回撤分布（P4/T12；T21a/T21c 修订）。

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

蒙特卡洛回撤分布（T21c/H6 + T70/F6 修订）：**块**自助重采样（block_len =
一个交易日的 bar 数，保留日级波动聚集；缺省 1 = 旧 iid 自助，且随机流与
旧实现逐位一致）+ **分批**模拟（起点逐批抽取、每批 ≤ _MC_SIMS_PER_BATCH
条路径；缺省 block_len=1 时 n_blocks = n，一次性抽 sims×n 个起点会在
37 万根 × 1000 sims 下物化约 3 GB 的起点矩阵）。逐条路径算最大回撤
（相对历史峰值，负比例），得到 [p1, p5, p50, p95, p99] 分布；策略实际回撤
落在分布的百分位过高（差于几乎所有重采样路径）→ 报警。

块自助夏普分布（T21a/C3）：block_bootstrap_sharpes 对收益序列整块重采样
出指定长度窗口，逐窗算**年化** Sharpe（口径与 report.summarize 一致）——
G5 用它把策略自身样本外收益变成"锁箱长度"下的参照分布。

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

# H6/探针 P4 + T70/F6：M5 全史（约 37 万根）块路径单矩阵约 3 GB——起点与
# 路径都逐批物化，每批 ≤20 条路径（起点、批矩阵、峰值行合计约 180 MB）
_MC_SIMS_PER_BATCH = 20
# 块自助夏普的分块规模（窗口 × 2000 sims 在长窗时会到数百 MB）
_BOOTSTRAP_CHUNK = 256


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
    block_len: int = 1

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
                "block_len": int(self.block_len),
                **{k: float(v) for k, v in self.percentiles.items()}}


def max_drawdown_of_returns(returns) -> float:
    """收益序列的最大回撤（相对历史峰值，负比例）。"""
    arr = _as_float_array(returns)
    equity = np.cumprod(1.0 + arr)
    peak = np.maximum.accumulate(np.concatenate(([1.0], equity)))
    dd = np.concatenate(([0.0], equity / peak[1:] - 1.0))
    return float(dd.min())


def _sample_block_starts(rng, n: int, block_len: int, size: tuple) -> np.ndarray:
    """块起点（有放回，均匀）：与旧 iid 实现共用同一次 rng 调用
    （block_len=1、size=(sims, n) 时随机流逐位一致）。"""
    return rng.integers(0, n - block_len + 1, size=size)


def _gather_blocks(arr: np.ndarray, starts: np.ndarray,
                   block_len: int) -> np.ndarray:
    """starts[..., None] + 块内偏移 → 展平为 (行数, 块数×块长) 的重采样矩阵。"""
    idx = starts[:, :, None] + np.arange(block_len)[None, None, :]
    return arr[idx].reshape(starts.shape[0], -1)


def block_bootstrap_sharpes(returns, *, window_len: int, block_len: int,
                            sims: int, seed: int,
                            bars_per_year: float | None = None) -> np.ndarray:
    """块自助重采样 → 各重采样窗的**年化** Sharpe 分布（T21a/C3，G5 参照分布）。

    - 从 returns 整块抽取长度 block_len 的连续片段（块起点均匀、有放回），
      拼接出 window_len 根 bar 的窗口，算一个年化 Sharpe；重复 sims 次。
      块长 = 一个交易日的 bar 数（保留日内到日级的相关结构）；
    - 年化口径与 report.summarize 一致：mean/std(ddof=1) × sqrt(bars_per_year)。
      bars_per_year 缺省时按 returns 的 DatetimeIndex 实测
      （observed_bars_per_year，与 report 同一函数）；跨度 < 30 天无法实测
      → ValueError：调用方必须显式传入，绝不静默换日历口径；
    - 无方差窗的夏普无定义 → 该窗丢弃；全部无方差 → ValueError。
    """
    from mt5quant.backtest.report import observed_bars_per_year
    arr = _as_float_array(returns)
    if window_len < 2:
        raise ValueError(f"window_len 必须 ≥2（夏普无定义），收到 {window_len}")
    if block_len < 1:
        raise ValueError(f"block_len 必须 ≥1，收到 {block_len}")
    if sims < 2:
        raise ValueError(f"sims 必须 ≥2，收到 {sims}")
    if len(arr) < block_len:
        raise ValueError(
            f"returns 长度 {len(arr)} 不足一个块（block_len={block_len}）")
    if bars_per_year is None:
        idx = getattr(returns, "index", None)
        if not isinstance(idx, pd.DatetimeIndex):
            raise ValueError(
                "returns 无 DatetimeIndex 且未传 bars_per_year——年化口径"
                "无法确定（与 report 一致的实测口径需要时间索引）")
        bars_per_year = observed_bars_per_year(idx)
        if bars_per_year is None:
            raise ValueError(
                "returns 时间跨度 < 30 天，bars_per_year 无法实测——请显式传入"
                "（锁箱段的 annualization_bars_per_year）")
    if not float(bars_per_year) > 0:
        raise ValueError(f"bars_per_year 必须为正，收到 {bars_per_year!r}")

    n_blocks = -(-int(window_len) // int(block_len))     # ceil
    rng = np.random.default_rng(seed)
    factor = math.sqrt(float(bars_per_year))
    sharpes: list[float] = []
    done = 0
    while done < sims:
        b = min(_BOOTSTRAP_CHUNK, sims - done)
        starts = _sample_block_starts(rng, len(arr), block_len, (b, n_blocks))
        sampled = _gather_blocks(arr, starts, block_len)[:, :window_len]
        mean = sampled.mean(axis=1)
        std = sampled.std(axis=1, ddof=1)
        ok = std > 0.0
        sharpes.extend(
            float(x) for x in mean[ok] / std[ok] * factor)
        done += b
    if not sharpes:
        raise ValueError("全部重采样窗都无方差——夏普分布无定义")
    return np.asarray(sharpes, dtype="float64")


def mc_drawdown_distribution(returns, *, sims: int, seed: int,
                             block_len: int = 1) -> DrawdownDistribution:
    """块自助重采样 bar 收益 → 最大回撤分布（同 seed 逐位可复现）。

    T21c/H6 + T70/F6：起点**逐批**抽取（每批 ≤ _MC_SIMS_PER_BATCH 条路径，
    就地运算控峰值内存——缺省 block_len=1 时 n_blocks = n，一次性抽
    sims×n 个起点在 37 万根 × 1000 sims 下是约 3 GB 的 int64 矩阵）；
    重采样按块（block_len = 一个交易日的 bar 数；缺省 1 = 旧 iid 自助）。
    numpy Generator 的随机流与调用切分无关（同一 bounds/dtype 下，
    rng.integers 行主序逐值填充，分批拼接与一次抽完逐位相同），故逐批
    抽取不改变任何 seed 的结果——与旧实现（一次性抽完起点）逐位一致。
    """
    arr = _as_float_array(returns)
    if sims < 2:
        raise ValueError(f"sims 必须 ≥2，收到 {sims}")
    if block_len < 1:
        raise ValueError(f"block_len 必须 ≥1，收到 {block_len}")
    if len(arr) < 2:
        raise ValueError("收益观测不足，无法重采样")
    if len(arr) < block_len:
        raise ValueError(
            f"收益长度 {len(arr)} 不足一个块（block_len={block_len}）")
    n = len(arr)
    n_blocks = -(-n // int(block_len))
    rng = np.random.default_rng(seed)
    mdds = np.empty(int(sims), dtype="float64")
    for b0 in range(0, int(sims), _MC_SIMS_PER_BATCH):
        b1 = min(b0 + _MC_SIMS_PER_BATCH, int(sims))
        starts = _sample_block_starts(rng, n, block_len,
                                      (b1 - b0, n_blocks))
        sampled = _gather_blocks(arr, starts, block_len)[:, :n]
        # 就地算回撤：sampled ← 权益 ← 回撤（峰值内存 ≈ 2×批矩阵）
        np.add(sampled, 1.0, out=sampled)
        np.cumprod(sampled, axis=1, out=sampled)
        peaks = np.maximum.accumulate(sampled, axis=1)
        np.maximum(peaks, 1.0, out=peaks)    # 峰值不低于初始权益 1
        np.divide(sampled, peaks, out=sampled)
        sampled -= 1.0
        mdds[b0:b1] = sampled.min(axis=1)
    pcts = {f"p{q}": float(np.percentile(mdds, q))
            for q in (1, 5, 50, 95, 99)}
    return DrawdownDistribution(sims=int(sims), seed=int(seed),
                                drawdowns=tuple(float(x) for x in mdds),
                                percentiles=pcts, block_len=int(block_len))
