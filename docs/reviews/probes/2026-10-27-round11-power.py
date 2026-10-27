"""第十一轮审核：夏普比率检验力速查（纯计算，不读仓库数据）。

运行：python docs/reviews/probes/2026-10-27-round11-power.py

口径：Lo（2002）独立同分布近似，年化夏普估计的标准误
    SE(SR) ≈ sqrt((1 + SR²/2) / T)，T 为年数。
单侧检验 H0: SR ≤ 0，显著性 5%（z = 1.645）；
"N=20" 列按 Bonferroni 把显著性降到 5%/20（z ≈ 2.807），
近似"同一轮测了 20 个独立试验、只认最好的那个"。
这是粗算：收益有自相关、厚尾时真实检验力更低。
"""
from math import erf, sqrt


def phi(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def se(sr: float, years: float) -> float:
    return sqrt((1.0 + sr * sr / 2.0) / years)


def mde(years: float, z_alpha: float, z_power: float = 0.8416) -> float | None:
    """80% 检验力下的最小可检测夏普；不可达返回 None。"""
    target = z_alpha + z_power
    if sqrt(2.0 * years) <= target:        # SR→∞ 时 SR/SE → sqrt(2T)
        return None
    lo, hi = 0.0, 50.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if mid / se(mid, years) < target:
            lo = mid
        else:
            hi = mid
    return hi


if __name__ == "__main__":
    print("真实夏普  年数  标准误  95% 区间          检验力(单次)  检验力(N=20)")
    for sr in (0.5, 1.0):
        for years in (1, 4.3, 6, 10, 20):
            s = se(sr, years)
            print(f"  {sr:.1f}   {years:>5}  {s:.2f}   [{sr - 1.96 * s:+.2f}, {sr + 1.96 * s:+.2f}]"
                  f"   {phi(sr / s - 1.645):6.0%}        {phi(sr / s - 2.807):6.0%}")
    print("\n80% 检验力的最小可检测夏普：")
    for years in (4.3, 6, 10, 20):
        one = mde(years, 1.645)
        many = mde(years, 2.807)
        print(f"  {years:>4} 年：单次检验 {one:.2f}；N=20 "
              f"{'不可达' if many is None else f'{many:.2f}'}")
