"""T76 概率模式预筛：[prob] 装载校验、五条件决定、分界提案。"""
import json

import pandas as pd
import pytest

from mt5quant.research import candidates as cand

# ------------------------------------------------------------ 测试夹具

LIT = {
    "original": ["Osler (2003), Journal of Finance 58(5)"],
    "replications": [],
    "rebuttals": [],
    "search_log": "JSTOR/JF 2026-10-28: currency orders price levels",
    "published_effect": "止盈/止损单在整数位聚集",
    "expected_effect": "按发表后 −58% 折算：Δp 0.04",
}


def prob_data(**over):
    d = {
        "id": "C101", "title": "整数关口反转", "mechanism_class": "price_levels",
        "status": "registered", "symbols": ["TEST"], "holding": "H1",
        "mechanism": "止盈止损单聚集",
        "literature": dict(LIT),
        "prob": {
            "situations": ["S1"],
            "hypotheses": ["S1:A3:fade>base"],
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 24},
            "level_grid": {"TEST": 50.0},
            "approach": {"lookback_bars": 24, "min_distance_atr": 1.0,
                         "touch_atr": 0.25},
            "breakout_atr": 0.25,
            "retest_window_bars": 6,
            "expected_delta_p": 0.04,
        },
    }
    d.update(over)
    return d


class FlatCosts:
    """点差/滑点/佣金恒定（价格单位）。"""

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.30

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.05

    def commission_per_lot(self, symbol):
        return 0.0


def counted_df(rows):
    """rows: (symbol, situation, action, ny_hour, atr, close, time)。"""
    return pd.DataFrame(
        [{"symbol": s, "situation": sit, "action": a, "side": 1,
          "level": 2000.0, "kind": "resistance", "anchor": i,
          "bar": i, "time": pd.Timestamp(t), "ny_hour": h, "atr": atr,
          "close": c}
         for i, (s, sit, a, h, atr, c, t) in enumerate(rows)])


@pytest.fixture()
def data_root(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "latest.json").write_text(
        json.dumps({"TEST": {"contract_size": 100.0}}), encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------ [prob] 校验

def test_prob_section_parses():
    c = cand.validate_candidate(prob_data(), source="t")
    assert c.prob_declared is not None
    assert c.prob_declared.expected_delta_p == 0.04
    assert c.declared is None


def test_price_levels_requires_prob():
    d = prob_data()
    del d["prob"]
    with pytest.raises(cand.CandidateError, match="必须带 \\[prob\\]"):
        cand.validate_candidate(d, source="t")


def test_price_levels_rejects_declared():
    d = prob_data()
    d["declared"] = {"events_per_year": 10, "gross_return_per_event": 0.001,
                     "entry_hour_ny": 9, "exit_hour_ny": 10,
                     "overnight_nights": 0.0, "expected_sharpe": 0.8}
    with pytest.raises(cand.CandidateError, match="不带 \\[declared\\]"):
        cand.validate_candidate(d, source="t")


def test_prob_validations():
    with pytest.raises(cand.CandidateError, match="situations"):
        cand.validate_candidate(prob_data(
            prob={**prob_data()["prob"], "situations": ["S9"]}), source="t")
    with pytest.raises(cand.CandidateError, match="hypotheses"):
        cand.validate_candidate(prob_data(
            prob={**prob_data()["prob"],
                  "hypotheses": ["S1:A9:fade>base"]}), source="t")
    with pytest.raises(cand.CandidateError, match="假设"):
        cand.validate_candidate(prob_data(
            prob={**prob_data()["prob"], "situations": ["S2"]}), source="t")
    with pytest.raises(cand.CandidateError, match="level_grid"):
        cand.validate_candidate(prob_data(
            prob={**prob_data()["prob"], "level_grid": {}}), source="t")
    d = prob_data(symbols=["TEST", "OTHER"])
    d["prob"] = {**d["prob"], "level_grid": {"TEST": 50.0}}
    with pytest.raises(cand.CandidateError, match="每个品种"):
        cand.validate_candidate(d, source="t")
    with pytest.raises(cand.CandidateError, match="expected_delta_p"):
        cand.validate_candidate(prob_data(
            prob={**prob_data()["prob"], "expected_delta_p": 0.9}),
            source="t")


def test_mechanism_enum_includes_price_levels():
    assert "price_levels" in cand.MECHANISM_CLASSES


# ------------------------------------------------------------ precheck_prob

def test_precheck_prob_pass(data_root):
    c = cand.validate_candidate(prob_data(), source="t")
    # 成本：spread0.3 + 2×slip0.1 = 0.5 价格单位；R = 1.0×atr
    # cost_R = 0.5/20 = 0.025R；ΔEV = 0.04×3 = 0.12R ≥ 3×0.025R ✓
    # Δp=0.04/power=0.5 的解析样本量 = (1.645×√0.24/0.04)² = 406 → 500 行过
    rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, "2024-01-02 14:00")] * 500
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(rows),
                             split=pd.Timestamp("2025-01-01"))
    assert res.decision == "pass", res.reasons
    assert res.checks["power_prob"]["n_required_analytic"] == 406


def test_precheck_prob_cost_fail(data_root):
    """预期 Δp 不够覆盖 3 倍成本 → fail（计划 T76 测试项）。"""
    c = cand.validate_candidate(prob_data(), source="t")
    rows = [("TEST", "S1", "A3", 9, 1.0, 2000.0, "2024-01-02 14:00")] * 300
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(rows),
                             split=pd.Timestamp("2025-01-01"))
    # cost_R = 0.5/1.0 = 0.5R；ΔEV = 0.12R < 1.5R → fail
    assert res.decision == "fail"
    assert res.checks["ex_ante_cost_prob"]["passed"] is False


def test_precheck_prob_insufficient_triggers_waiting(data_root):
    """触发数不足解析样本量 → waiting_data（计划 T76 测试项）。"""
    c = cand.validate_candidate(prob_data(), source="t")
    rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, "2024-01-02 14:00")] * 100
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(rows),
                             split=pd.Timestamp("2025-01-01"))
    assert res.decision == "waiting_data"
    assert res.checks["power_prob"]["n_available_min"] == 100
    assert res.checks["power_prob"]["t75_rule_pending"] is True


def test_precheck_prob_no_counted_waiting(data_root):
    c = cand.validate_candidate(prob_data(), source="t")
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=None, split=pd.Timestamp("2025-01-01"))
    assert res.decision == "waiting_data"
    assert res.checks["power_prob"]["n_available_min"] is None


def test_precheck_prob_literature_fail(data_root):
    # 装载器会拒绝空 search_log——预筛侧的复核用直接构造的候选对象验证
    ok = cand.validate_candidate(prob_data(), source="t")
    c = cand.Candidate(
        id=ok.id, title=ok.title, mechanism_class=ok.mechanism_class,
        status=ok.status, symbols=ok.symbols, holding=ok.holding,
        mechanism=ok.mechanism,
        literature=cand.Literature(
            original=ok.literature.original,
            replications=ok.literature.replications,
            rebuttals=ok.literature.rebuttals, search_log="",
            published_effect=ok.literature.published_effect,
            expected_effect=ok.literature.expected_effect),
        prob_declared=ok.prob_declared)
    rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, "2024-01-02 14:00")] * 300
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(rows),
                             split=pd.Timestamp("2025-01-01"))
    assert res.decision == "fail"


# ------------------------------------------------------------ propose_split

def store_with_span(tmp_path, sym, first, last, tf="H1"):
    d = tmp_path / "bars" / sym / tf
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps({"first_time": first, "last_closed_time": last}),
        encoding="utf-8")


def test_propose_split_deterministic_and_month_floored(tmp_path):
    store_with_span(tmp_path, "AAA", "2010-01-04T00:00:00",
                    "2026-01-02T00:00:00")
    store_with_span(tmp_path, "BBB", "2014-06-01T00:00:00",
                    "2026-01-02T00:00:00")
    s1 = cand.propose_split(tmp_path, ["AAA", "BBB"], "H1")
    s2 = cand.propose_split(tmp_path, ["AAA", "BBB"], "H1")
    assert s1 == s2                      # 同样的输入 → 相同分界
    # 共同覆盖 2014-06-01..2026-01-02，中点 ≈ 2020-03-17 → 月初 2020-03-01
    assert s1 == pd.Timestamp("2020-03-01")
    assert s1.day == 1


def test_propose_split_no_common_coverage(tmp_path):
    store_with_span(tmp_path, "AAA", "2010-01-04T00:00:00",
                    "2012-01-02T00:00:00")
    store_with_span(tmp_path, "BBB", "2014-06-01T00:00:00",
                    "2016-01-02T00:00:00")
    with pytest.raises(ValueError, match="共同覆盖"):
        cand.propose_split(tmp_path, ["AAA", "BBB"], "H1")
