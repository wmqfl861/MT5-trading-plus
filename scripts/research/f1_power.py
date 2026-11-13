"""F1 跨资产 TSMOM 组合：合并检验力正式测算（T155 结论指定的下一步；D79/D76）。

纯离线公式测算：不读仓库数据、不下载、不连终端、不开研究轮次、不计算任何
策略收益。全部单品种夏普锚为 T155（docs/research/d79-feasibility-2026-11-13.md，
提交 b76bb90）登记的**文献折算值**（出处逐个见该文件 §1 与候选登记
research/candidates/C003.toml、C004.toml 的 expected_effect 折算链），本脚本只做
转录与组合数学。

公式（与 docs/reviews/probes/2026-10-27-round11-power.py 及
src/mt5quant/research/power.py 同口径——Lo 2002 独立同分布近似）：

    组合夏普        SR_p = SR_a * sqrt(M / (1 + (M-1) * rho))
    年化夏普标准误  SE(SR_p) = sqrt((1 + SR_p^2 / 2) / T)      T 为年数
    单侧检验力      power = Phi(SR_p / SE - z)，z = Phi^{-1}(1 - alpha/N)
    达 50% 所需年数 T50  = z^2 * (1 + SR_p^2/2) / SR_p^2
                        （power = 0.5 当且仅当 SR_p/SE = z）
    分散化上限      SR_p(M->inf) = SR_a / sqrt(rho)   （品种再多也不过此值）

z 取千分位（power.py 同约定）：N=1 -> 1.645、N=2 -> 1.960、N=3 -> 2.128。
alpha = 0.05（单侧，蓝图 §6.3 / [v3] 同值）。

N 口径（报告 §2 详述）：
- N=2：现行轮次预算口径（R1/R2/R3 预注册 trial_budget=2：1 主规格+1 修复重跑）；
- N=3：若 F1 单候选轮开轮，主检验 rounds_lineage 累计口径
  （R1=1 + R3=1 + F1 主规格=1，R3.toml advance_rule 同一算式先例）；
- N=1：单次检验口径（蓝图 §6.2 条件 4 预筛口径 / 开轮候选数复核口径）。

运行：python scripts/research/f1_power.py [--check]
  --check 只跑内置自测（与蓝图 §6.3 速查表抽样比对 + 公式恒等式），不打印表格。

依赖：仅标准库（math/sys）——不超过"只依赖 numpy"的约束，无需第三方包。
确定性：同一输入恒同一输出。
"""
from __future__ import annotations

import sys
from math import ceil, erf, sqrt

ALPHA = 0.05

# 单品种年化夏普锚（T155 登记；出处见报告 §3，逐个给文献）。
ANCHORS: dict[str, float] = {
    # C003 声明（XAUUSD 12 个月 TSMOM）：MOP 2012 单资产毛夏普锚 0.4 × (1-0.58)
    # 衰减（McLean & Pontiff 2016）≈ 0.17；取 min(声明, 隐含)。
    "C003声明0.17": 0.17,
    # T155 的 C003/C004 均值口径（精确算术均值 0.23，T155 表记 0.25；两者对
    # 结论的差异本脚本在 --check 与报告 §5 核过：检验力差 < 5pp、判定不翻转）。
    "T155均值0.25": 0.25,
    # C004 声明（BTCUSD 周度 TSMOM）：Borri et al. (2025) 2020 年后动量超额锚
    # × 0.42 衰减、按 LTW (2022) 负面证据向下取整后经 σ_周 折算 ≈ 0.29。
    "C004声明0.29": 0.29,
}
ANCHOR_MEAN_EXACT = 0.23  # (0.17+0.29)/2，敏感性核对用

# 品种间策略收益平均两两相关档（任务指定的三档敏感性；非实测——实测属
# 候选预筛 T84 口径，本测算不开轮不预筛）。
RHOS = (0.3, 0.5, 0.7)

# 各 M 的研究窗年数 T（T155 登记 / ext-data §9.5 覆盖表；报告 §2 注明绑定约束）。
# 主口径（T155）：M=4 含 BTC 公共窗；M=5/6/8 为"新增 FX 对置换 BTC"的
# 不平衡面板窗（前段以可得品种子集运行，SR_p 按满配 M 计——早期年份高估
# 分散化，正式预筛须按逐月实际品种数重算）。
WINDOWS: dict[int, float] = {4: 10.90, 5: 16.5, 6: 15.6, 8: 15.6}
WINDOW_BTC_STACK = 10.90  # 含 BTC 叠加变体：任何 M 公共窗都由 BTC 2014-11 绑定

N_BUDGET = 2      # 现行轮次预算口径（trial_budget=2）
N_LINEAGE = 3     # F1 若开轮的主检验 rounds_lineage 口径
NS = (1, N_BUDGET, N_LINEAGE)


def phi(x: float) -> float:
    """标准正态分布函数 Phi(x)（探针同式）。"""
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """标准正态分位数（power.py 同款二分，双精度收敛）。"""
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return hi


def z_alpha(n_tests: int) -> float:
    """Bonferroni 后单侧临界值，千分位（power.py 同约定）。"""
    return round(norm_ppf(1.0 - ALPHA / n_tests), 3)


def portfolio_sharpe(sr_a: float, m: int, rho: float) -> float:
    """分散化近似：SR_p = SR_a * sqrt(M/(1+(M-1)rho))。"""
    return sr_a * sqrt(m / (1.0 + (m - 1) * rho))


def sharpe_se(sr: float, years: float) -> float:
    """Lo 2002 iid 近似：SE(SR) = sqrt((1+SR^2/2)/T)。"""
    return sqrt((1.0 + sr * sr / 2.0) / years)


def power_one_sided(sr: float, years: float, n_tests: int = 1) -> float:
    """单侧 H0: SR<=0 检验力（与 power.py 同式同取位）。"""
    return phi(sr / sharpe_se(sr, years) - z_alpha(n_tests))


def years_to_50(sr: float, n_tests: int) -> float:
    """该夏普下达到 50% 检验力所需年数 T50 = z^2(1+SR^2/2)/SR^2。"""
    z = z_alpha(n_tests)
    return z * z * (1.0 + sr * sr / 2.0) / (sr * sr)


def sr_p_needed(years: float, n_tests: int) -> float | None:
    """给定窗长与 N，达到 50% 检验力所需的组合夏普（解析解）。

    SR^2 (T - z^2/2) = z^2；T <= z^2/2 时任何夏普都达不到（SR/SE 上限 < z）。
    """
    z = z_alpha(n_tests)
    denom = years - z * z / 2.0
    if denom <= 0.0:
        return None
    return z / sqrt(denom)


def min_viable_m(sr_a: float, years: float, rho: float,
                 n_tests: int) -> int | None:
    """给定 rho 与窗长，达到 50% 检验力的最小品种数；不可达返回 None。

    需 M/(1+(M-1)rho) >= K，K = (SR_p*/SR_a)^2；解 M = K(1-rho)/(1-K*rho)。
    K*rho >= 1 时分散化上限 SR_a/sqrt(rho) 不够，任何 M 都不可达。
    """
    star = sr_p_needed(years, n_tests)
    if star is None:
        return None
    k = (star / sr_a) ** 2
    if k * rho >= 1.0:
        return None
    return max(2, ceil(k * (1.0 - rho) / (1.0 - k * rho)))


def rho_max_feasible(sr_a: float, years: float, n_tests: int) -> float | None:
    """M->inf 也刚能过 50% 的临界平均相关 rho* = (SR_a/SR_p*)^2。"""
    star = sr_p_needed(years, n_tests)
    if star is None:
        return None
    return (sr_a / star) ** 2


def _selfcheck() -> None:
    """内置自测：与蓝图 §6.3 速查表抽样比对 + 公式恒等式。"""
    # 蓝图 §6.3（探针同表）：SR=1.0/4.3 年 -> 52%、10 年 -> 83%、20 年 -> 98%；
    # SR=0.5/20 年 -> 68%（N=1）；同轮 20 试验（N=20 -> z=2.807）SR=1.0/
    # 10 年 -> 41%。
    cases = [
        (1.0, 4.3, 1, 0.52), (1.0, 10.0, 1, 0.83), (1.0, 20.0, 1, 0.98),
        (0.5, 20.0, 1, 0.68), (1.0, 10.0, 20, 0.41),
    ]
    for sr, years, n, expect in cases:
        got = power_one_sided(sr, years, n)
        assert abs(got - expect) <= 0.01, (sr, years, n, got, expect)
    assert z_alpha(1) == 1.645 and z_alpha(20) == 2.807, z_alpha(1)
    # T50 恒等式：power(T=T50) == 0.5（数值容差）
    for n in NS:
        for sr in (0.2, 0.4, 0.6):
            t50 = years_to_50(sr, n)
            assert abs(power_one_sided(sr, t50, n) - 0.5) <= 1e-9
    # SR_p* 恒等式：power(SR=SR_p*) == 0.5
    for n in NS:
        for years in (10.9, 15.6, 16.5):
            star = sr_p_needed(years, n)
            assert star is not None
            assert abs(power_one_sided(star, years, n) - 0.5) <= 1e-9
    # 组合公式退化：rho=0 -> SR_a*sqrt(M)；M=1 -> SR_a
    assert abs(portfolio_sharpe(0.25, 4, 0.0) - 0.5) < 1e-12
    assert abs(portfolio_sharpe(0.25, 1, 0.5) - 0.25) < 1e-12
    # T155 转录核对：M=5、SR_a=0.25、rho=0.1-0.3 -> SR_p 0.38-0.47，
    # T=16.5 单次检验力 43-57%（T155 表原格）
    lo, hi = portfolio_sharpe(0.25, 5, 0.3), portfolio_sharpe(0.25, 5, 0.1)
    assert round(lo, 2) == 0.38 and round(hi, 2) == 0.47, (lo, hi)
    p_lo = power_one_sided(lo, 16.5, 1)
    p_hi = power_one_sided(hi, 16.5, 1)
    assert 0.42 <= p_lo <= 0.45 and 0.55 <= p_hi <= 0.59, (p_lo, p_hi)
    # 锚 0.23（精确均值）与 0.25：检验力差 < 5pp，且任何 (M, rho, T, N)
    # 组合的 50% 判定不翻转（结论对 T155 的 0.25 取整不敏感；差额在 N=1、
    # 检验力靠近 50% 的格最大，那里曲线最陡）
    for m, years in WINDOWS.items():
        for rho in RHOS:
            for n in NS:
                a = power_one_sided(portfolio_sharpe(0.25, m, rho), years, n)
                b = power_one_sided(portfolio_sharpe(ANCHOR_MEAN_EXACT, m, rho),
                                    years, n)
                assert abs(a - b) < 0.05, (m, rho, n, a, b)
                assert (a >= 0.5) == (b >= 0.5), (m, rho, n, a, b)
    print("自测通过：§6.3 速查表抽样 5 格、T50/SR_p* 恒等式、组合公式退化、"
          "T155 转录格、锚 0.23 vs 0.25 差 < 5pp 且判定不翻转。")


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:.0f}%"


def grid_table(sr_a: float, label: str, *, years_override: float | None = None,
               nsShown: tuple[int, ...] = (N_BUDGET, N_LINEAGE)) -> None:
    """打印一个锚 × (M, rho) 网格：SR_p、各 N 检验力、T50、判定。"""
    print(f"\n== 锚 SR_a={sr_a:.2f}（{label}）==")
    hdr = ["M", "T(年)", "rho", "SR_p"]
    hdr += [f"检验力(N={n})" for n in nsShown]
    hdr += [f"T50(N={n})" for n in nsShown] + ["D76条件2(N=3)"]
    print(" | ".join(hdr))
    for m in (4, 5, 6, 8):
        years = years_override if years_override is not None else WINDOWS[m]
        for rho in RHOS:
            srp = portfolio_sharpe(sr_a, m, rho)
            row = [str(m), f"{years:.2f}", f"{rho:.1f}", f"{srp:.3f}"]
            row += [_fmt_pct(power_one_sided(srp, years, n)) for n in nsShown]
            row += [f"{years_to_50(srp, n):.1f}" for n in nsShown]
            ok = power_one_sided(srp, years, N_LINEAGE) >= 0.5
            row += ["满足" if ok else "不满足"]
            print(" | ".join(row))


def feasibility_tables() -> None:
    """盈亏平衡相关 rho* 与最小可行品种数（回答"哪些 rho 档下 M=? 过 50%"）。"""
    print("\n== 盈亏平衡：临界相关 rho*（M→∞ 刚过 50%）与最小可行 M ==")
    for name, sr_a in ANCHORS.items():
        for years, wlabel in ((10.90, "T=10.9(M=4/含BTC)"),
                              (16.5, "T=16.5(XAU绑定)"),
                              (15.6, "T=15.6(USDJPY绑定)")):
            parts = []
            for n in NS:
                rstar = rho_max_feasible(sr_a, years, n)
                if rstar is None:
                    parts.append(f"N={n}:不可达")
                    continue
                mv = {rho: min_viable_m(sr_a, years, rho, n) for rho in RHOS}
                mvTxt = ", ".join(
                    f"rho={rho:.1f}:M={v if v is not None else '∞不可达'}"
                    for rho, v in mv.items())
                parts.append(f"N={n}:rho*={rstar:.2f}[{mvTxt}]")
            print(f"SR_a={sr_a:.2f}({name}) {wlabel} -> " + "；".join(parts))


def main(argv: list[str]) -> int:
    if "--check" in argv:
        _selfcheck()
        return 0
    _selfcheck()
    print(f"\nz 临界值（千分位）：N=1:{z_alpha(1)}、N=2:{z_alpha(2)}、"
          f"N=3:{z_alpha(3)}（alpha={ALPHA} 单侧）")
    grid_table(ANCHORS["T155均值0.25"], "主表：T155 的 C003/C004 均值口径")
    grid_table(ANCHORS["C003声明0.17"], "锚敏感性下界：C003 声明")
    grid_table(ANCHORS["C004声明0.29"], "锚敏感性上界：C004 声明")
    grid_table(ANCHORS["T155均值0.25"], "含 BTC 叠加变体（M 在现有 4 品种之上"
              "叠加新 FX 对，公共窗恒由 BTC 2014-11 绑定）",
               years_override=WINDOW_BTC_STACK)
    feasibility_tables()
    # N=1 参照（预筛单次口径）：报各锚下 N=1 过 50% 而 N=2 不过的格子——
    # 即"T155 单次口径结论在 α/N 校正后失守"的精确位置（主窗 + 含 BTC 叠加窗）
    print("\n== N 口径敏感性：N=1 过 50% 而 N=2 不过的格子 ==")
    for sr_a_name in ("T155均值0.25", "C004声明0.29"):
        sr_a = ANCHORS[sr_a_name]
        flips = []
        for m in (4, 5, 6, 8):
            for years in sorted({WINDOWS[m], WINDOW_BTC_STACK}):
                for rho in RHOS:
                    srp = portfolio_sharpe(sr_a, m, rho)
                    p1 = power_one_sided(srp, years, 1)
                    if p1 >= 0.5 and power_one_sided(srp, years, N_BUDGET) < 0.5:
                        flips.append(f"M={m},rho={rho:.1f},T={years:.1f}"
                                     f"(N=1:{_fmt_pct(p1)})")
        print(f"SR_a={sr_a:.2f}: " + ("；".join(flips) if flips
                                     else "（无——N=1 与 N=2 判定一致）"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
