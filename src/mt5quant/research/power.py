"""夏普比率检验力（Lo 2002 近似；铁律 18"检验力先行"的计算底座）。

**与探针逐位一致**（T62 硬要求）：本模块是
docs/reviews/probes/2026-10-27-round11-power.py 的库化，全网格（真实夏普
× 年数 × 单次/N=20 检验力、最小可检测夏普）浮点逐位相等——探针是该口径
的参考实现，tests/test_research_power.py 直接装载探针模块逐格比对。

口径（与探针同）：
- 年化夏普估计的标准误 SE(SR) ≈ sqrt((1 + SR²/2) / T)，T 为年数
  （Lo 2002 *The Statistics of Sharpe Ratios* 的独立同分布近似；
  收益有自相关、厚尾时真实检验力更低——粗算，用于开卡前的方向性判断）；
- 单侧检验 H0: SR ≤ 0；显著性 alpha 经 Bonferroni 除以同轮试验数
  n_tests（"同一轮测了 n 个独立试验、只认最好的那个"）；
- 临界值 z = Φ⁻¹(1 − alpha/n_tests) **取千分位**、检验力目标
  z_power = Φ⁻¹(power) **取万分位**——探针用的 z=1.645 / 2.807 /
  0.8416 即这一取位约定（0.05→1.645、0.05/20→2.807、0.8→0.8416），
  换算一律走本模块的同一函数，不各处自算分位数产生口径漂移；
- Φ⁻¹（_norm_ppf）用二分法在 Φ 上反解到双精度收敛（仅标准库 math.erf，
  不引入第三方依赖；确定性：同一输入恒同一输出）；
- min_detectable_sharpe 与探针同一二分搜索（lo=0、hi=50、100 步）：
  SR→∞ 时 SR/SE → sqrt(2T)，sqrt(2T) ≤ z_alpha + z_power 即不可达 → None。
"""
from __future__ import annotations

import math
from math import ceil, erf, sqrt

__all__ = ["sharpe_se", "power_one_sided", "min_detectable_sharpe",
           "power_proportion_one_sided", "n_needed_proportion"]


def _phi(x: float) -> float:
    """标准正态分布函数 Φ(x)（与探针 phi 同式）。"""
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分位数 Φ⁻¹(p)：在 Φ 上二分到双精度收敛。

    p ∈ (0, 1)；0/1 与越界 → ValueError（分位数在端点无定义）。
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p 必须在开区间 (0,1)，收到 {p!r}")
    lo, hi = -40.0, 40.0          # Φ(±40) 在双精度下已分别是 0/1
    for _ in range(200):          # 80/2^200 远小于双精度分辨率
        mid = (lo + hi) / 2.0
        if _phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return hi


def _z_alpha(alpha: float, n_tests: int) -> float:
    """Bonferroni 校正后的单侧临界值（千分位，探针口径：0.05→1.645）。"""
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha 必须在开区间 (0,1)，收到 {alpha!r}")
    if isinstance(n_tests, bool) or not isinstance(n_tests, int) \
            or n_tests < 1:
        raise ValueError(f"n_tests 必须是 ≥1 的整数，收到 {n_tests!r}")
    return round(_norm_ppf(1.0 - alpha / n_tests), 3)


def sharpe_se(sr: float, years: float) -> float:
    """年化夏普估计的标准误（Lo 2002 iid 近似）：sqrt((1+SR²/2)/T)。"""
    years = float(years)
    if not math.isfinite(years) or years <= 0.0:
        raise ValueError(f"years 必须是有限正数（年），收到 {years!r}")
    return sqrt((1.0 + sr * sr / 2.0) / years)


def power_one_sided(sr: float, years: float, *, alpha: float = 0.05,
                    n_tests: int = 1) -> float:
    """单侧检验 H0: SR≤0 的检验力（Bonferroni：显著性 alpha/n_tests）。

    与探针表逐位一致：power_one_sided(sr, years) = Φ(sr/SE − 1.645)、
    n_tests=20 = Φ(sr/SE − 2.807)（探针 docstring 的两个临界值）。
    """
    z = _z_alpha(alpha, n_tests)
    return _phi(sr / sharpe_se(sr, years) - z)


def min_detectable_sharpe(years: float, *, alpha: float = 0.05,
                          power: float = 0.8,
                          n_tests: int = 1) -> float | None:
    """给定检验力目标的年化夏普；现有数据长度上不可达 → None。

    反解 power_one_sided(sr, years) ≥ power，即 sr/SE(sr) ≥ z_alpha+z_power
    （z_power 取万分位：power=0.8 → 0.8416，与探针 mde 同）。二分搜索与
    探针同一实现（lo=0、hi=50、100 步、返回 hi），结果逐位相等。
    """
    sr_years = float(years)
    if not math.isfinite(sr_years) or sr_years <= 0.0:
        raise ValueError(f"years 必须是有限正数（年），收到 {years!r}")
    if not math.isfinite(power) or not 0.0 < power < 1.0:
        raise ValueError(f"power 必须在开区间 (0,1)，收到 {power!r}")
    z_a = _z_alpha(alpha, n_tests)
    z_p = round(_norm_ppf(power), 4)          # 探针口径：0.8 → 0.8416
    target = z_a + z_p
    if sqrt(2.0 * sr_years) <= target:        # SR→∞ 时 SR/SE → sqrt(2T)
        return None
    lo, hi = 0.0, 50.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if mid / sharpe_se(mid, sr_years) < target:
            lo = mid
        else:
            hi = mid
    return hi


# --------------------------------------------------------------- 比例检验（T76）
# P 线 Δp 检验（蓝图 P 线 §3.4）：单侧 H0: p = p0（已知基准赢率）对
# H1: p = p0 + dp。正态近似（分子按 H0 方差、分母按 H1 方差，与蓝图
# §3.4 解析表逐格一致——tests/test_research_power.py 全九格复现）：
#   power(n) = Φ((dp·√n − z_a·√(p0q0)) / √(p1q1))
#   n_needed = ceil(((z_a·√(p0q0) + z_p·√(p1q1)) / dp)²)
# z_a 复用 _z_alpha（千分位，Bonferroni alpha/n_tests）、z_p 万分位，
# 与本模块夏普口径同一取位约定。触发会重叠聚集，有效样本更少——按月
# 块自助的方差折算属 T75 标定，此处是解析下界。

def _validate_proportion_args(p0: float, dp: float) -> tuple[float, float]:
    if not (math.isfinite(p0) and 0.0 < p0 < 1.0):
        raise ValueError(f"p0 必须在开区间 (0,1)，收到 {p0!r}")
    if not (math.isfinite(dp) and dp > 0.0):
        raise ValueError(f"dp 必须为正（单侧增量），收到 {dp!r}")
    if p0 + dp >= 1.0:
        raise ValueError(f"p0+dp 必须 < 1，收到 {p0 + dp!r}")
    return float(p0), float(dp)


def power_proportion_one_sided(p0: float, dp: float, n: int, *,
                               alpha: float = 0.05,
                               n_tests: int = 1) -> float:
    """n 个独立触发上单侧 Δp 检验的检验力（正态近似，Bonferroni）。

    以已知基准赢率 p0 为对照：拒绝域 P̂ ≥ p0 + z_a·√(p0q0/n)，真实赢率
    p1 = p0+dp 时的检出概率。n 必须是 ≥1 整数。
    """
    p0, dp = _validate_proportion_args(p0, dp)
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"n 必须是 ≥1 整数，收到 {n!r}")
    z_a = _z_alpha(alpha, n_tests)
    p1 = p0 + dp
    return _phi((dp * sqrt(n) - z_a * sqrt(p0 * (1.0 - p0)))
                / sqrt(p1 * (1.0 - p1)))


def n_needed_proportion(p0: float, dp: float, *, alpha: float = 0.05,
                        power: float = 0.8, n_tests: int = 1) -> int:
    """达到目标检验力所需的独立触发数（解析式取整向上）。

    复现蓝图 P 线 §3.4 全表（p0=0.40；如 dp=0.03/power=0.8 → 1661、
    n_tests=6 → 2808、dp=0.05/power=0.5 → 260）。触发重叠/聚集时有效
    样本少于名义数，实测要求以 T75 标定的样本量规则为准（更严者胜）。
    """
    p0, dp = _validate_proportion_args(p0, dp)
    if not math.isfinite(power) or not 0.0 < power < 1.0:
        raise ValueError(f"power 必须在开区间 (0,1)，收到 {power!r}")
    z_a = _z_alpha(alpha, n_tests)
    z_p = round(_norm_ppf(power), 4)
    p1 = p0 + dp
    raw = ((z_a * sqrt(p0 * (1.0 - p0)) + z_p * sqrt(p1 * (1.0 - p1)))
           / dp) ** 2
    return ceil(raw - 1e-9)
