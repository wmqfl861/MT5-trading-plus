"""T76 比例检验力：复现蓝图 P 线 §3.4 解析表（p0=0.40，单侧 5%）。"""
import pytest

from mt5quant.research.power import (
    n_needed_proportion, power_proportion_one_sided,
)

# 蓝图 P 线 §3.4 表：Δp × 目标检验力 × 假设数 → 所需独立触发数
TABLE = {
    (0.02, 0.5, 1): 1624, (0.02, 0.8, 1): 3729, (0.02, 0.8, 6): 6306,
    (0.03, 0.5, 1): 722,  (0.03, 0.8, 1): 1661, (0.03, 0.8, 6): 2808,
    (0.05, 0.5, 1): 260,  (0.05, 0.8, 1): 600,  (0.05, 0.8, 6): 1014,
}


@pytest.mark.parametrize("cell,expected", sorted(TABLE.items()))
def test_n_needed_reproduces_blueprint_table(cell, expected):
    dp, power, n_tests = cell
    assert n_needed_proportion(0.40, dp, power=power, n_tests=n_tests) == expected


def test_review_examples():
    assert n_needed_proportion(0.40, 0.03, power=0.8) == 1661
    assert n_needed_proportion(0.40, 0.03, power=0.8, n_tests=6) == 2808
    assert n_needed_proportion(0.40, 0.05, power=0.5) == 260


def test_power_at_table_n_meets_target():
    """表上 n 的检验力恰达目标（与 n_needed 互逆）。"""
    for (dp, power, nt), n in TABLE.items():
        pw = power_proportion_one_sided(0.40, dp, n, n_tests=nt)
        assert pw >= power - 1e-9, (dp, power, nt, pw)
        # n−1 个触发不达标（ceil 的最小性；边界格允许恰好相等）
        pw_minus = power_proportion_one_sided(0.40, dp, n - 1, n_tests=nt)
        assert pw_minus < power


def test_power_monotone_in_n_and_dp():
    assert power_proportion_one_sided(0.40, 0.03, 722) \
        < power_proportion_one_sided(0.40, 0.03, 2000)
    assert power_proportion_one_sided(0.40, 0.03, 1000) \
        < power_proportion_one_sided(0.40, 0.05, 1000)


def test_validations():
    with pytest.raises(ValueError):
        n_needed_proportion(0.0, 0.03)          # p0 越界
    with pytest.raises(ValueError):
        n_needed_proportion(0.40, 0.0)          # dp ≤ 0
    with pytest.raises(ValueError):
        n_needed_proportion(0.40, 0.7)          # p0+dp ≥ 1
    with pytest.raises(ValueError):
        n_needed_proportion(0.40, 0.03, power=1.0)
    with pytest.raises(ValueError):
        power_proportion_one_sided(0.40, 0.03, 0)
    with pytest.raises(ValueError):
        power_proportion_one_sided(0.40, 0.03, 100, n_tests=0)
