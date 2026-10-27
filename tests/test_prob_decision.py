"""T72 决策规则：期望值下界、门槛与并列时的确定性选择。"""
import pytest

from mt5quant.prob.decision import ACTION_ORDER, choose, ev_lower
from mt5quant.prob.tables import CellEstimate


def est(p_win=0.40, p_loss=0.55, p_time=0.05, r_win=1.95, r_loss=-1.05,
        r_time=0.15, se_win=0.02, se_loss=0.02, n=400):
    return CellEstimate(n=n, p_win=p_win, p_loss=p_loss, p_time=p_time,
                        r_win=r_win, r_loss=r_loss, r_time=r_time,
                        se_win=se_win, se_loss=se_loss)


def test_ev_lower_formula():
    """EV_下界 = (p_win−z·se_win)·r_win + (p_loss+z·se_loss)·r_loss + 余下·r_time。"""
    e = est()
    z = 1.0
    pw, pl = 0.40 - z * 0.02, 0.55 + z * 0.02
    expected = pw * 1.95 + pl * -1.05 + (1 - pw - pl) * 0.15
    assert ev_lower(e, z=z) == pytest.approx(expected)


def test_ev_lower_monotone_non_increasing_in_z():
    """样本少 → SE 大 → 下界更低；z 增大 EV 单调不增（r_win > r_time > r_loss）。"""
    e = est()
    vals = [ev_lower(e, z=z) for z in [0.0, 0.5, 1.0, 1.5, 2.0, 3.0]]
    assert all(b <= a + 1e-15 for a, b in zip(vals, vals[1:]))


def test_blueprint_example_sample_rich_vs_poor():
    """蓝图 P 线附录 C 算例：同点估计下，样本少时退回"不做"。"""
    rich = est(se_win=0.02, se_loss=0.02)     # EV≈+0.150R，门槛 0.10R → 入场
    poor = est(se_win=0.05, se_loss=0.05)     # EV≈+0.060R → 不做
    assert ev_lower(rich, z=1.0) == pytest.approx(0.1495, abs=1e-3)
    assert ev_lower(poor, z=1.0) == pytest.approx(0.0595, abs=1e-3)
    assert choose([("A3", rich, 0.05)], z=1.0, threshold_mult=2.0) == "A3"
    assert choose([("A3", poor, 0.05)], z=1.0, threshold_mult=2.0) is None


def test_choose_none_when_all_below_threshold():
    cands = [("A1", est(p_win=0.30), 0.05), ("A3", est(p_win=0.33), 0.05)]
    assert choose(cands, z=1.0, threshold_mult=2.0) is None


def test_choose_highest_ev():
    a1 = est(p_win=0.45)      # EV 更高
    a3 = est(p_win=0.40)
    assert choose([("A3", a3, 0.05), ("A1", a1, 0.05)], z=1.0,
                  threshold_mult=2.0) == "A1"


def test_choose_threshold_uses_per_candidate_cost():
    """门槛 = threshold_mult × 该动作的平均来回成本（各动作成本可不同）。"""
    e = est(p_win=0.40)                       # EV ≈ 0.15R
    assert choose([("A1", e, 0.05), ("A3", e, 0.10)], z=1.0,
                  threshold_mult=2.0) == "A1"   # A3 门槛 0.20R 不过


def test_choose_tie_takes_first_in_action_order():
    e = est()
    # 传入顺序故意倒置；并列（同估计同成本）时取 A1 < A2 < A3 < A4 序最前者
    assert choose([("A4", e, 0.05), ("A1", e, 0.05)], z=1.0,
                  threshold_mult=2.0) == "A1"
    assert choose([("A3", e, 0.05), ("A2", e, 0.05)], z=1.0,
                  threshold_mult=2.0) == "A2"


def test_choose_rejects_unknown_action():
    with pytest.raises(ValueError):
        choose([("A9", est(), 0.05)], z=1.0, threshold_mult=2.0)


def test_action_order_constant():
    assert ACTION_ORDER == ("A1", "A2", "A3", "A4")
