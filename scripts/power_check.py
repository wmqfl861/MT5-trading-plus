"""检验力速查 CLI（T62/D31，铁律 18"检验力先行"）。

mt5quant.research.power 的表格化输出——该模块与第十一轮探针
docs/reviews/probes/2026-10-27-round11-power.py **逐位一致**（Lo 2002
独立同分布近似；tests/test_research_power.py 装载探针模块逐格比对）。

用法（Git Bash）：
    .venv/Scripts/python.exe scripts/power_check.py
    .venv/Scripts/python.exe scripts/power_check.py --n-tests 20 --years 6
    .venv/Scripts/python.exe scripts/power_check.py --sharpe 1.0 --years 4.3,6,10,20

口径：单侧检验 H0: SR ≤ 0，显著性 --alpha（缺省 5%）经 Bonferroni 除以
--n-tests；MDE 表按 --power（缺省 80%）的最小可检测夏普，不可达如实打印。
这是粗算：收益有自相关、厚尾时真实检验力更低。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5quant.research import power  # noqa: E402

DEFAULT_SHARPES = (0.5, 1.0)
DEFAULT_YEARS = (1, 4.3, 6, 10, 20)
DEFAULT_MDE_YEARS = (4.3, 6, 10, 20)


def _floats(text: str) -> list[float]:
    out = []
    for part in text.split(","):
        part = part.strip()
        if part:
            out.append(float(part))
    if not out:
        raise argparse.ArgumentTypeError(f"至少给一个数值：{text!r}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="power_check", description="夏普比率检验力速查（Lo 2002 近似，"
        "与 docs/reviews/probes/2026-10-27-round11-power.py 逐位一致）")
    ap.add_argument("--alpha", type=float, default=0.05,
                    help="单侧显著性（缺省 0.05）")
    ap.add_argument("--n-tests", type=int, default=1,
                    help="同轮试验数（Bonferroni 除之；缺省 1=单次检验）")
    ap.add_argument("--power", type=float, default=0.8,
                    help="MDE 表的检验力目标（缺省 0.8）")
    ap.add_argument("--sharpe", type=_floats,
                    default=list(DEFAULT_SHARPES),
                    help="逗号分隔的真实夏普列表（缺省 0.5,1.0）")
    ap.add_argument("--years", type=_floats, default=list(DEFAULT_YEARS),
                    help="逗号分隔的研究段年数列表（缺省 1,4.3,6,10,20）")
    args = ap.parse_args(argv)

    z_single = power._z_alpha(args.alpha, 1)                       # noqa: SLF001
    z_many = power._z_alpha(args.alpha, args.n_tests)              # noqa: SLF001
    print(f"单侧检验 H0: SR≤0，alpha={args.alpha:g}"
          f"（单次 z={z_single}；N={args.n_tests} Bonferroni z={z_many}）")
    print("真实夏普  年数  标准误  95% 区间          "
          f"检验力(单次)  检验力(N={args.n_tests})")
    for sr in args.sharpe:
        for years in args.years:
            s = power.sharpe_se(sr, years)
            print(f"  {sr:.1f}   {years:>5g}  {s:.2f}   "
                  f"[{sr - 1.96 * s:+.2f}, {sr + 1.96 * s:+.2f}]"
                  f"   {power.power_one_sided(sr, years):6.0%}        "
                  f"{power.power_one_sided(sr, years, alpha=args.alpha, n_tests=args.n_tests):6.0%}")
    print(f"\n{args.power:.0%} 检验力的最小可检测夏普（alpha={args.alpha:g}）：")
    for years in args.years:
        one = power.min_detectable_sharpe(years, alpha=args.alpha)
        many = power.min_detectable_sharpe(years, alpha=args.alpha,
                                           n_tests=args.n_tests)
        print(f"  {years:>4g} 年：单次检验 "
              f"{'不可达' if one is None else f'{one:.2f}'}；N={args.n_tests} "
              f"{'不可达' if many is None else f'{many:.2f}'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
