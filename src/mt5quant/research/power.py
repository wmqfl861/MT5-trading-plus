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
from math import erf, sqrt

__all__ = ["sharpe_se", "power_one_sided", "min_detectable_sharpe"]


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
