"""第十三轮审核探针（H 线）：关卡 v3 的工作点、事件模式的多重校正、C001 的持仓窗口 σ。

运行（仓库根目录，只读，不碰数据目录与台账）：
    ./.venv/Scripts/python.exe docs/reviews/probes/2026-10-28-round13-operating-point.py

A. 方案甲（DSR）在不同 N 下的检出率。gates-v3 标定的实验臂按 N=1 计算；
   正式路径的 N 取项目级研究口径。这里直接调用仓库的
   vstats.deflated_sharpe_probability，用独立同分布正态收益模拟
   XAUUSD H1 / H4 的样本外长度（约 9.8 年），试验夏普方差取两种口径：
   - "零假设抽样方差"：V = 每根 bar 夏普估计的抽样方差 1/(n−1)（最有利于通过的情形）；
   - "异质历史试验"：年化试验夏普的标准差 0.75（混合了日内负期望与趋势正期望的历史台账量级，示意）。
B. 事件模式样本量规则在 Bonferroni α/N 下的变化（T66 §5 的闭式，z 走 power.py 同一约定）。
C. C001 的隐含夏普：σ 取单根 D1 与取 4 个交易日持仓窗口的差别，及其对检验力的影响。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from mt5quant.research import power  # noqa: E402
from mt5quant.validation import stats as vstats  # noqa: E402

REPS = 400
SEED = 20261028


def trials_with_variance(v: float) -> list[float]:
    """两点集 [a, −a]：ddof=1 样本方差 = 2a² = v（确定性，与 N 无关）。"""
    a = math.sqrt(v / 2.0)
    return [a, -a]


def dsr_detection(sr_ann: float, years: float, bpy: float, n_trials: int,
                  v_per_bar: float, rng: np.random.Generator) -> float:
    n = int(round(years * bpy))
    mu = sr_ann / math.sqrt(bpy)
    hits = 0
    for _ in range(REPS):
        r = rng.standard_normal(n) + mu        # σ=1，每根 bar 夏普 = mu
        p = vstats.deflated_sharpe_probability(
            r, n_trials=n_trials,
            trial_sharpes=(trials_with_variance(v_per_bar) if n_trials > 1 else None))
        hits += p >= 0.95
    return hits / REPS


def part_a() -> None:
    print("A. 方案甲（DSR ≥ 0.95）检出率随 N 的变化（独立同分布正态，400 次模拟/格）")
    setups = [("XAUUSD H1", 9.8, 4420.0), ("XAUUSD H4", 9.8, 1265.0)]
    ns = [1, 2, 6, 24, 100, 400]
    for name, years, bpy in setups:
        n_bars = years * bpy
        v_null = 1.0 / (n_bars - 1.0)                 # 每根 bar 夏普的零假设抽样方差
        v_het = (0.75 ** 2) / bpy                     # 年化标准差 0.75 → 每根 bar
        for label, v in (("零假设抽样方差", v_null), ("异质历史试验(年化σ=0.75)", v_het)):
            rng = np.random.default_rng(SEED)
            row = []
            for n_trials in ns:
                sr0_ann = vstats.expected_max_sharpe(n_trials, v) * math.sqrt(bpy)
                fp = dsr_detection(0.0, years, bpy, n_trials, v, rng)
                tp = dsr_detection(1.0, years, bpy, n_trials, v, rng)
                row.append(f"N={n_trials}: SR0≈{sr0_ann:.2f} 误报 {fp:.1%} 检出 {tp:.0%}")
            print(f"  {name} | {label}")
            for cell in row:
                print(f"    {cell}")


def psr_bonferroni_detection(sr_ann: float, years: float, bpy: float,
                             n_tests: int, rng: np.random.Generator) -> float:
    """方案丙（对照用）：PSR(0) ≥ 1 − α/N（Bonferroni），同一收益生成方式。"""
    n = int(round(years * bpy))
    mu = sr_ann / math.sqrt(bpy)
    hits = 0
    for _ in range(REPS):
        r = rng.standard_normal(n) + mu
        p = vstats.deflated_sharpe_probability(r, n_trials=1, trial_sharpes=None)
        hits += p >= 1.0 - 0.05 / n_tests
    return hits / REPS


def part_a2() -> None:
    print("A2. 对照：方案丙 PSR(0) ≥ 1 − α/N（Bonferroni），同样长度与模拟次数")
    for name, years, bpy in (("XAUUSD H1", 9.8, 4420.0), ("XAUUSD H4", 9.8, 1265.0)):
        rng = np.random.default_rng(SEED)
        cells = []
        for n_tests in (1, 2, 6, 24):
            fp = psr_bonferroni_detection(0.0, years, bpy, n_tests, rng)
            tp = psr_bonferroni_detection(1.0, years, bpy, n_tests, rng)
            cells.append(f"N={n_tests}: 误报 {fp:.1%} 检出 {tp:.0%}")
        print(f"  {name}: " + "；".join(cells))


def part_b() -> None:
    print("B. 事件模式样本量规则：n(检验力 50%) = z²·(1+SR²/2)/SR²，z = z_{α/N}")
    for sr in (0.2, 0.3):
        cells = []
        for n_tests in (1, 2, 6, 24):
            z = power._z_alpha(0.05, n_tests)
            n = z * z * (1.0 + sr * sr / 2.0) / (sr * sr)
            cells.append(f"N={n_tests}: z={z:.3f} n≈{math.ceil(n)}")
        print(f"  每事件 SR={sr}: " + "；".join(cells))


def part_c() -> None:
    print("C. C001 隐含夏普：σ 口径（单根 D1 vs 4 个交易日窗口）")
    gross = 0.0024
    sigma_d1 = 0.01338                       # C001.precheck.json 的 USTEC D1 σ
    k_days = 4                               # 末月最后交易日开盘 → 下月第 3 个交易日收盘
    sigma_win = sigma_d1 * math.sqrt(k_days)  # 独立同分布近似；T66 同窗长滚动实测 σ_w≈2.7%
    years = 39.81
    for cost_label, cost in (("快照隔夜费（13.40bp）", 0.001340),
                             ("swap-free（0.81bp）", 0.000081)):
        for s_label, s in (("σ=单根 D1", sigma_d1), ("σ=4 日窗口", sigma_win)):
            sr = (gross - cost) / s * math.sqrt(12)
            p1 = power.power_one_sided(sr, years)
            p2 = power.power_one_sided(sr, years, n_tests=2)
            print(f"  {cost_label} | {s_label}: 隐含夏普 {sr:.3f}，"
                  f"单次检验力 {p1:.1%}，两候选 Bonferroni {p2:.1%}")


if __name__ == "__main__":
    part_a()
    part_a2()
    part_b()
    part_c()
