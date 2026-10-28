"""T73A 轮次文件扩展：[segments].split、[hypotheses]、lockbox_uses_max=0。"""
import pytest

from mt5quant.research import rounds


def round_data(**over):
    d = {
        "id": "R2", "opened": "2026-11-02", "candidates": ["C101"],
        "trial_budget": 12, "lockbox_uses_max": 0,
        "dsr_trial_scope": "project", "advance_rule": "Holm 显著且成本达标",
        "close_rule": "全部假设有结论", "approved_by": "P线审核会话",
        "evaluation": {"C101": {"mode": "probability",
                                "primary": "delta_p_one_sided"}},
        "segments": {"split": "2019-12-01"},
        "hypotheses": {"C101": ["S1:A1:follow>base", "S1:A2:follow>base",
                                "S1:A3:fade>base", "S1:A4:fade>base"]},
    }
    d.update(over)
    return d


def h_round_data(**over):
    d = {
        "id": "R1", "opened": "2026-11-01", "candidates": ["C001"],
        "trial_budget": 40, "lockbox_uses_max": 1,
        "dsr_trial_scope": "project", "advance_rule": "过 G3",
        "close_rule": "预算用尽", "approved_by": "用户甲",
        "evaluation": {"C001": {"mode": "event",
                                "primary": "event_mean_return_one_sided"}},
    }
    d.update(over)
    return d


def test_prob_round_with_zero_lockbox_and_segments_loads():
    r = rounds.validate_round(round_data(), source="t")
    assert r.segments == {"split": "2019-12-01"}
    assert r.hypotheses["C101"][0] == "S1:A1:follow>base"
    assert r.lockbox_uses_max == 0


def test_split_must_be_month_start():
    with pytest.raises(rounds.RoundError, match="月初"):
        rounds.validate_round(round_data(
            segments={"split": "2019-12-15"}), source="t")
    with pytest.raises(rounds.RoundError, match="split"):
        rounds.validate_round(round_data(
            segments={"split": "2019-12"}), source="t")


def test_zero_lockbox_only_for_all_probability():
    d = round_data(candidates=["C101", "C001"],
                   evaluation={"C101": {"mode": "probability",
                                        "primary": "delta_p_one_sided"},
                               "C001": {"mode": "event",
                                        "primary": "event_mean_return_one_sided"}},
                   hypotheses={"C101": ["S1:A3:fade>base"]})
    with pytest.raises(rounds.RoundError, match="lockbox_uses_max = 0"):
        rounds.validate_round(d, source="t")


def test_lockbox_above_one_still_rejected():
    with pytest.raises(rounds.RoundError, match="D16"):
        rounds.validate_round(round_data(lockbox_uses_max=2), source="t")


def test_prob_candidate_requires_hypotheses():
    d = round_data()
    del d["hypotheses"]
    with pytest.raises(rounds.RoundError, match="hypotheses"):
        rounds.validate_round(d, source="t")
    d2 = round_data(hypotheses={"C102": ["S1:A3:fade>base"]})
    with pytest.raises(rounds.RoundError, match="不在候选名单内"):
        rounds.validate_round(d2, source="t")


def test_hypotheses_forbidden_for_non_prob_candidates():
    d = round_data(hypotheses={"C101": ["S1:A3:fade>base"]},
                   evaluation={"C101": {"mode": "standard",
                                        "primary": "max_net_profit"}},
                   lockbox_uses_max=1)
    with pytest.raises(rounds.RoundError, match="hypotheses"):
        rounds.validate_round(d, source="t")


def test_hypothesis_entry_format_validated():
    with pytest.raises(rounds.RoundError, match="必须形如"):
        rounds.validate_round(round_data(
            hypotheses={"C101": ["X9:A3:fade>base"]}), source="t")


def test_h_round_loads_unchanged():
    """H 线现有轮次文件（无新段）装载结果逐字段不变。"""
    r = rounds.validate_round(h_round_data(), source="t")
    assert r.segments is None and r.hypotheses == {}
    assert r.lockbox_uses_max == 1
