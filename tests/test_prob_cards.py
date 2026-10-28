"""T73A P 卡字段：segment / [prob] / evaluation_mode / primary_statistic。"""
import pytest

from mt5quant.research import cards


def p_card_data(**over):
    d = {
        "id": "P001-v1", "family": "P001", "version": 1, "parent": "",
        "candidate": "C101", "round": "R2",
        "strategy": "mt5quant.strategies.library.prob.level_decision:P001",
        "symbols": ["XAUUSD"], "timeframe": "H1",
        "evaluation_mode": "probability",
        "primary_statistic": "delta_p_one_sided",
        "segment": "discovery",
        "session": "America/New_York",
        "phenomenon": "整数关口处反转赢率更高",
        "mechanism": "止盈/止损单在整数位聚集（Osler 2003）",
        "should_fail_when": "整数位无聚集时",
        "falsified_if": "Holm 校正后不显著",
        "graveyard_checked": [],
        "rules": {"entry": "由 [prob] 决定", "exit": "tp/sl/K 根归零",
                  "stop": "sl_atr×ATR"},
        "expected": {"delta_p": "文献折算 0.04（Osler 2003，无复现按 −58%）",
                     "triggers_per_year": "只数触发，不看结果"},
        "prob": {
            "situations": ["S1"],
            "hypotheses": ["S1:A1:follow>base", "S1:A2:follow>base",
                           "S1:A3:fade>base", "S1:A4:fade>base"],
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 24},
            "level_grid": {"XAUUSD": 50.0},
            "approach": {"lookback_bars": 24, "min_distance_atr": 1.0,
                         "touch_atr": 0.25},
            "breakout_atr": 0.25,
            "retest_window_bars": 6,
            "expected_delta_p": 0.04,
        },
    }
    d.update(over)
    return d


def test_p_card_loads_with_prob_and_segment():
    c = cards.validate_card(p_card_data(), source="t")
    assert c.evaluation_mode == "probability"
    assert c.primary_statistic == "delta_p_one_sided"
    assert c.segment == "discovery"
    assert c.prob["bracket"]["horizon_bars"] == 24
    assert len(c.prob["hypotheses"]) == 4


def test_p_card_id_prefix_required():
    with pytest.raises(cards.CardError, match="P###"):
        d = p_card_data()
        d["id"] = "H999-v1"; d["family"] = "H999"
        cards.validate_card(d, source="t")


def test_probability_mode_requires_segment_prob_and_statistic():
    d = p_card_data()
    del d["segment"]
    with pytest.raises(cards.CardError, match="segment"):
        cards.validate_card(d, source="t")
    d = p_card_data()
    del d["prob"]
    with pytest.raises(cards.CardError, match="\\[prob\\]"):
        cards.validate_card(d, source="t")
    d = p_card_data()
    d["primary_statistic"] = "max_net_profit"
    with pytest.raises(cards.CardError, match="delta_p_one_sided"):
        cards.validate_card(d, source="t")


def test_segment_enum_and_prob_fields_validated():
    with pytest.raises(cards.CardError, match="segment"):
        cards.validate_card(p_card_data(segment="middle"), source="t")
    with pytest.raises(cards.CardError, match="bracket"):
        bad = p_card_data()
        bad["prob"] = {**bad["prob"],
                       "bracket": {"sl_atr": 1.0, "tp_atr": 2.0}}
        cards.validate_card(bad, source="t")


def _h_card_data():
    """最小 H 卡（附录 A v1 旧卡形态，与 tests/test_research_cards.VALID 同构）。"""
    return {
        "id": "H097", "version": 1, "symbols": ["XAUUSD"], "timeframe": "H1",
        "session": "Europe/London 08:00-10:00", "phenomenon": "伦敦开盘动量",
        "mechanism": "亚盘区间积累的止损单被开盘流吃掉",
        "should_fail_when": "亚盘无区间的趋势日",
        "falsified_if": "样本内净利为负", "graveyard_checked": [],
        "rules": {"entry": "突破上沿", "exit": "收盘反向", "stop": "区间对侧"},
        "params": {"fast": [2, 3, 4], "slow": [10, 20]},
        "expected": {"trades_per_year": "50-200", "win_rate": "45-60%",
                     "payoff_ratio": "1.2-2.0"},
    }


def test_h_card_unchanged_and_prob_forbidden():
    """非 probability 卡：新键 evaluation_mode 缺省 standard；带 [prob] → 拒。"""
    c = cards.validate_card(_h_card_data(), source="t")
    assert c.evaluation_mode == "standard" and c.segment == ""
    assert c.prob is None
    bad = _h_card_data()
    bad["prob"] = p_card_data()["prob"]
    with pytest.raises(cards.CardError, match="P###"):
        cards.validate_card(bad, source="t")


def test_probability_forbidden_with_declared_grid_params():
    """probability 卡不开参数网格（P1 不设网格，蓝图附录 A）。"""
    d = p_card_data()
    d["params"] = {"x": (1, 2)}
    with pytest.raises(cards.CardError, match="网格"):
        cards.validate_card(d, source="t")


# ------------------------------------------------- 审核三 D1：P2 决策卡

def test_p2_standard_validation_card_loads():
    """D1：P 卡 standard + segment=validation + [prob]——P2 决策卡可写。"""
    d = p_card_data()
    d["evaluation_mode"] = "standard"
    d["segment"] = "validation"
    d.pop("primary_statistic", None)
    c = cards.validate_card(d, source="t")
    assert c.evaluation_mode == "standard"
    assert c.segment == "validation"
    assert c.prob is not None and len(c.prob["hypotheses"]) == 4


def test_p2_standard_requires_validation_segment():
    d = p_card_data()
    d["evaluation_mode"] = "standard"
    d["segment"] = "discovery"
    d.pop("primary_statistic", None)
    with pytest.raises(cards.CardError, match="validation"):
        cards.validate_card(d, source="t")


def test_p2_card_wrong_primary_statistic_rejected():
    d = p_card_data()
    d["evaluation_mode"] = "standard"
    d["segment"] = "validation"
    d["primary_statistic"] = "delta_p_one_sided"
    with pytest.raises(cards.CardError, match="关卡 v3"):
        cards.validate_card(d, source="t")


def test_p2_card_without_segment_rejected():
    d = p_card_data()
    d["evaluation_mode"] = "standard"
    del d["segment"]
    with pytest.raises(cards.CardError, match="segment 必须"):
        cards.validate_card(d, source="t")
