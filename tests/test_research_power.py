"""T62/D31：检验力函数（mt5quant.research.power，Lo 2002 近似）。

铁律 18（检验力先行）的计算底座。**与第十一轮探针逐位一致**是硬要求：
docs/reviews/probes/2026-10-27-round11-power.py 是参考实现，本测试直接
装载探针模块，对探针输出表的全网格（真实夏普 × 年数 × 单次/N=20）逐格
比较——标准误、检验力、最小可检测夏普都要求**浮点完全相等**，不是近似。
"""
import importlib.util
from pathlib import Path

import pytest

from mt5quant.research import power

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = (REPO_ROOT / "docs" / "reviews" / "probes"
              / "2026-10-27-round11-power.py")

# 探针输出表的全网格（__main__ 里两层 for 的同一组值）
PROBE_GRID = [(sr, years) for sr in (0.5, 1.0)
              for years in (1, 4.3, 6, 10, 20)]
PROBE_MDE_YEARS = (4.3, 6, 10, 20)


def load_probe():
    """装载探针脚本（有 __main__ 守卫，import 安全）。"""
    spec = importlib.util.spec_from_file_location("round11_power_probe",
                                                  PROBE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------ 与探针逐位一致（核心）

def test_sharpe_se_bitwise_identical_to_probe():
    probe = load_probe()
    for sr, years in PROBE_GRID:
        assert power.sharpe_se(sr, years) == probe.se(sr, years), \
            f"se({sr}, {years}) 与探针不一致"


def test_power_bitwise_identical_to_probe_table():
    """探针表的"检验力(单次)"（z=1.645）与"检验力(N=20)"（z=2.807）两列。

    power_one_sided 缺省（alpha=0.05, n_tests=1）与 Bonferroni N=20 的
    结果必须与探针按其口径（phi(sr/se − z)）逐位相等。
    """
    probe = load_probe()
    for sr, years in PROBE_GRID:
        s = probe.se(sr, years)
        single = probe.phi(sr / s - 1.645)
        many = probe.phi(sr / s - 2.807)
        assert power.power_one_sided(sr, years) == single, \
            f"power({sr}, {years}) 单次与探针不一致"
        assert power.power_one_sided(sr, years, n_tests=20) == many, \
            f"power({sr}, {years}, n_tests=20) 与探针不一致"


def test_min_detectable_sharpe_bitwise_identical_to_probe():
    probe = load_probe()
    for years in PROBE_MDE_YEARS:
        assert power.min_detectable_sharpe(years) == probe.mde(years, 1.645), \
            f"mde({years}) 单次与探针不一致"
        assert power.min_detectable_sharpe(years, n_tests=20) \
            == probe.mde(years, 2.807), \
            f"mde({years}, n_tests=20) 与探针不一致"


def test_min_detectable_sharpe_unreachable_matches_probe():
    probe = load_probe()
    # sqrt(2×6) ≈ 3.464 ≤ 2.807+0.8416 → 探针判不可达；6 年 N=20 同为 None
    assert probe.mde(6, 2.807) is None
    assert power.min_detectable_sharpe(6, n_tests=20) is None


# ------------------------------------------------ 计划里的失败测试先行值

@pytest.mark.parametrize("sr, years, n_tests, expected", [
    (1.0, 6, 1, 0.64),           # 6 年、真实夏普 1：单次检验力 ≈ 64%
    (1.0, 4.3, 20, 0.13),        # 4.3 年、同轮 20 个试验：≈ 13%
])
def test_power_reference_values(sr, years, n_tests, expected):
    assert power.power_one_sided(sr, years, n_tests=n_tests) \
        == pytest.approx(expected, abs=0.01)


def test_min_detectable_sharpe_reference_values():
    assert power.min_detectable_sharpe(6) == pytest.approx(1.46, abs=0.01)
    # 蓝图附录 E：4.3/10/20 年的单次最小可检测夏普 2.26/0.95/0.60
    assert power.min_detectable_sharpe(4.3) == pytest.approx(2.26, abs=0.01)
    assert power.min_detectable_sharpe(10) == pytest.approx(0.95, abs=0.01)
    assert power.min_detectable_sharpe(20) == pytest.approx(0.60, abs=0.01)


def test_min_detectable_sharpe_six_years_twenty_tests_unreachable():
    assert power.min_detectable_sharpe(6, n_tests=20) is None


# ------------------------------------------------ 行为与参数校验

def test_bonferroni_reduces_power_monotonically():
    single = power.power_one_sided(1.0, 6)
    for n in (2, 5, 20, 100):
        assert power.power_one_sided(1.0, 6, n_tests=n) <= single
    # 夏普越高检验力越高；年数越长检验力越高（同口径下单调）
    assert power.power_one_sided(0.5, 6) < power.power_one_sided(1.5, 6)
    assert power.power_one_sided(1.0, 4.3) < power.power_one_sided(1.0, 20)


def test_alpha_effect_and_validation():
    # 更严的显著性 → 更低检验力
    assert power.power_one_sided(1.0, 6, alpha=0.01) \
        < power.power_one_sided(1.0, 6, alpha=0.05)
    with pytest.raises(ValueError):
        power.power_one_sided(1.0, 0.0)
    with pytest.raises(ValueError):
        power.power_one_sided(1.0, -6.0)
    with pytest.raises(ValueError):
        power.power_one_sided(1.0, 6, alpha=0.0)
    with pytest.raises(ValueError):
        power.power_one_sided(1.0, 6, alpha=1.0)
    with pytest.raises(ValueError):
        power.power_one_sided(1.0, 6, n_tests=0)
    with pytest.raises(ValueError):
        power.sharpe_se(1.0, 0.0)
    with pytest.raises(ValueError):
        power.min_detectable_sharpe(0.0)
    with pytest.raises(ValueError):
        power.min_detectable_sharpe(6, power=0.0)
    with pytest.raises(ValueError):
        power.min_detectable_sharpe(6, power=1.0)


def test_min_detectable_sharpe_feasible_long_years_many_tests():
    # 20 年、N=20：sqrt(40) ≈ 6.32 > 3.6486 → 可达且高于单次检验
    m = power.min_detectable_sharpe(20, n_tests=20)
    assert m is not None
    assert m > power.min_detectable_sharpe(20)
