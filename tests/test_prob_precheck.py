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
    """rows: (symbol, situation, action, ny_hour, atr, close, time)。

    time 同一天多行只记一个触发日（D52：检验力按触发日数计）。
    """
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
    # Δp=0.04/power=0.5/1 条假设 → 需 406 个触发日 → 500 个不同日过
    days = pd.date_range("2018-01-01", periods=500, freq="1D")
    rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, str(d))
            for d in days]
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(rows),
                             split=pd.Timestamp("2025-01-01"))
    assert res.decision == "pass", res.reasons
    assert res.checks["power_prob"]["n_required_analytic"] == 406
    assert res.checks["power_prob"]["per_hypothesis"]["S1:A3:fade>base"][
        "trigger_days"] == 500


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
    # Q6：100 次触发都在同一天 → 触发日数 1（不是 100），逐条列出
    ph = res.checks["power_prob"]["per_hypothesis"]["S1:A3:fade>base"]
    assert ph["trigger_days"] == 1
    assert res.checks["power_prob"]["insufficient"] == ["S1:A3:fade>base"]
    assert res.checks["power_prob"]["unit"] == "trigger_days"
    assert res.checks["power_prob"]["t75_rule_pending"] is True


def test_precheck_prob_no_counted_waiting(data_root):
    c = cand.validate_candidate(prob_data(), source="t")
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=None, split=pd.Timestamp("2025-01-01"))
    assert res.decision == "waiting_data"
    pp = res.checks["power_prob"]
    assert pp["per_hypothesis"] == {} and pp["passed"] is False
    assert pp["note"] == "无触发计数"
    assert res.checks["ex_ante_cost_prob"]["passed"] is None


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


# ------------------------------------------------- 审核二修正（Q1/Q2/Q6）

class TwoSymCosts:
    """Q1 实验：两品种一来回成本一贵一贱（价格单位）。"""

    def __init__(self, cheap=0.05, dear=0.50):
        self.spreads = {"CHEAP": cheap, "DEAR": dear}

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return self.spreads[symbol]

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0


def _two_sym_setup(tmp_path, delta_p=0.02):
    (tmp_path / "specs").mkdir(exist_ok=True)
    (tmp_path / "specs" / "latest.json").write_text(
        json.dumps({"CHEAP": {"contract_size": 100.0},
                    "DEAR": {"contract_size": 100.0}}), encoding="utf-8")
    data = prob_data(symbols=["CHEAP", "DEAR"])
    data["prob"] = {**data["prob"],
                    "level_grid": {"CHEAP": 50.0, "DEAR": 50.0},
                    "expected_delta_p": delta_p}
    return cand.validate_candidate(data, source="t")


def test_q1_cost_per_symbol_cheap_and_dear(tmp_path):
    """Q1：一品种便宜（0.005R）一品种贵（0.05R）——贵的不满足 → 整体 fail。

    atr=10、sl_atr=1 → R=10 价格单位；CHEAP 0.05/10=0.005R、DEAR 0.5/10
    =0.05R；Δp=0.02、tp_R=2 → ΔEV=0.06R：CHEAP 过（0.015），DEAR 不过
    （0.15）。"""
    c = _two_sym_setup(tmp_path)
    rows = [(s, "S1", "A3", 10, 10.0, 2000.0, "2018-01-02")
            for s in ("CHEAP", "DEAR") for _ in range(50)]
    res = cand.precheck_prob(c, data_root=tmp_path,
                             cost_model=TwoSymCosts(),
                             counted=counted_df(rows), split="2020-01-01")
    ec = res.checks["ex_ante_cost_prob"]
    assert ec["passed"] is False
    assert ec["failing_symbols"] == ["DEAR"]
    assert ec["binding_symbol"] == "DEAR"
    assert ec["cost_r_by_symbol"]["CHEAP"] == pytest.approx(0.005)
    assert ec["cost_r_by_symbol"]["DEAR"] == pytest.approx(0.05)
    assert res.decision == "fail"
    # 不短路：成本 fail 时检验力条件照样给出逐条结果
    assert "per_hypothesis" in res.checks["power_prob"]


def test_q2_nonzero_commission_hand_calc(tmp_path):
    """Q2：佣金按 commission_per_lot/cs 折成价格单位再换 R（手算对账）。"""
    class CommCosts(TwoSymCosts):
        def spread_p90(self, symbol, hour_ny, *, price=None):
            return 0.10

        def commission_per_lot(self, symbol):
            return 2.0            # 每手来回 2 账户货币

    root = data_root_of(tmp_path)
    data = prob_data()
    # Δp=0.20（tp_R=2 → ΔEV=0.6R）：0.6 ≥ 3×0.12=0.36 → 成本条件过；
    # 再用 Δp=0.03（ΔEV=0.09R < 0.36R）→ 不过。两档手算对账。
    data["prob"] = {**data["prob"], "expected_delta_p": 0.20}
    c = cand.validate_candidate(data, source="t")
    rows = [("TEST", "S1", "A3", 9, 1.0, 2000.0, "2018-01-02")]
    res = cand.precheck_prob(c, data_root=root, cost_model=CommCosts(),
                             counted=counted_df(rows), split="2020-01-01")
    ec = res.checks["ex_ante_cost_prob"]
    # 手算：comm_price = 2.0/100 = 0.02；成本 = (0.10 + 0 + 0.02)/1.0 = 0.12R
    assert ec["cost_r_by_symbol"]["TEST"] == pytest.approx(0.12)
    assert ec["passed"] is True and ec["failing_symbols"] == []

    data2 = prob_data()
    data2["prob"] = {**data2["prob"], "expected_delta_p": 0.03}
    c2 = cand.validate_candidate(data2, source="t")
    res2 = cand.precheck_prob(c2, data_root=root, cost_model=CommCosts(),
                              counted=counted_df(rows), split="2020-01-01")
    ec2 = res2.checks["ex_ante_cost_prob"]
    assert ec2["cost_r_by_symbol"]["TEST"] == pytest.approx(0.12)
    assert ec2["passed"] is False      # ΔEV = 0.03×3 = 0.09R < 0.36R


def data_root_of(tmp_path):
    (tmp_path / "specs").mkdir(exist_ok=True)
    (tmp_path / "specs" / "latest.json").write_text(
        json.dumps({"TEST": {"contract_size": 100.0}}), encoding="utf-8")
    return tmp_path

def data_root_of(tmp_path):
    (tmp_path / "specs").mkdir(exist_ok=True)
    (tmp_path / "specs" / "latest.json").write_text(
        json.dumps({"TEST": {"contract_size": 100.0}}), encoding="utf-8")
    return tmp_path


def test_q6_days_unit_and_per_hypothesis(data_root):
    """Q6：样本单位=触发日数；逐条判定（一条过候选即过，不足的列出）。"""
    c = cand.validate_candidate(prob_data(), source="t")
    days = pd.date_range("2018-01-01", periods=650, freq="1D")
    a3_rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, str(d)) for d in days]
    # 同一天 300 次 A1 触发——按次计 300 会过（需 406），按日计只有 1 日
    a1_rows = [("TEST", "S1", "A1", 9, 20.0, 2000.0, "2018-06-01")] * 300
    data2 = prob_data()
    data2["prob"] = {**data2["prob"],
                     "hypotheses": ["S1:A3:fade>base", "S1:A1:follow>base"]}
    c2 = cand.validate_candidate(data2, source="t")
    res = cand.precheck_prob(c2, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(a3_rows + a1_rows),
                             split=pd.Timestamp("2025-01-01"))
    pp = res.checks["power_prob"]
    # 两条假设 → 需 577 触发日（z_a(2)=1.96）；A3 650 日过；A1 1 日不足
    assert pp["n_required_analytic"] == 577
    assert pp["per_hypothesis"]["S1:A3:fade>base"]["passed"] is True
    assert pp["per_hypothesis"]["S1:A1:follow>base"]["trigger_days"] == 1
    assert pp["insufficient"] == ["S1:A1:follow>base"]
    assert pp["passing_count"] == 1
    assert pp["passed"] is True            # 至少一条通过 → 候选过
    assert res.decision == "pass"
    assert any("S1:A1:follow>base" in r for r in res.reasons)


def test_q6_all_hypotheses_insufficient_waiting(data_root):
    """Q6：全部假设都不足 → 候选 waiting_data，逐条列出。"""
    data = prob_data()
    data["prob"] = {**data["prob"],
                    "hypotheses": ["S1:A3:fade>base", "S1:A1:follow>base"]}
    c = cand.validate_candidate(data, source="t")
    rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, "2018-01-02")] * 30
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(rows),
                             split=pd.Timestamp("2025-01-01"))
    assert res.decision == "waiting_data"
    assert set(res.checks["power_prob"]["insufficient"]) == {
        "S1:A3:fade>base", "S1:A1:follow>base"}
    assert res.checks["power_prob"]["passing_count"] == 0


def test_q3_bonferroni_uses_registered_hypothesis_count(data_root):
    """Q3：n_tests = 候选登记的假设条数（不是 1）。"""
    data = prob_data()
    data["prob"] = {**data["prob"],
                    "hypotheses": ["S1:A1:follow>base", "S1:A2:follow>base",
                                   "S1:A3:fade>base", "S1:A4:fade>base"]}
    c = cand.validate_candidate(data, source="t")
    rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, "2018-01-02")]
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(rows),
                             split=pd.Timestamp("2025-01-01"))
    pp = res.checks["power_prob"]
    assert pp["n_tests"] == 4
    # Δp=0.04、4 条假设（z_a(4)=2.241）→ (2.241×√0.24/0.04)² = 754 日
    assert pp["n_required_analytic"] == 754
