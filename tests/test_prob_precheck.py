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
                     "window_bars": 1,
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
    # Δp=0.04/power=0.5/1 条假设 → 解析 406 / T75 实测 1500（插值段，
    # 更严）→ 需 1500 个触发日 → 1600 个不同日过
    days = pd.date_range("2018-01-01", periods=1600, freq="1D")
    rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, str(d))
            for d in days]
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(rows),
                             split=pd.Timestamp("2025-01-01"))
    assert res.decision == "pass", res.reasons
    assert res.checks["power_prob"]["n_required_analytic"] == 406
    assert res.checks["power_prob"]["n_required_used"] == 1500
    assert res.checks["power_prob"]["per_hypothesis"]["S1:A3:fade>base"][
        "trigger_days"] == 1600


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
    assert res.checks["power_prob"]["t75_rule_pending"] is False


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
    days = pd.date_range("2018-01-01", periods=2200, freq="1D")
    a3_rows = [("TEST", "S1", "A3", 9, 20.0, 2000.0, str(d)) for d in days]
    # 同一天 300 次 A1 触发——按次计 300 次、按日计只有 1 日（远低于 2130）
    a1_rows = [("TEST", "S1", "A1", 9, 20.0, 2000.0, "2018-06-01")] * 300
    data2 = prob_data()
    data2["prob"] = {**data2["prob"],
                     "hypotheses": ["S1:A3:fade>base", "S1:A1:follow>base"]}
    c2 = cand.validate_candidate(data2, source="t")
    res = cand.precheck_prob(c2, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(a3_rows + a1_rows),
                             split=pd.Timestamp("2025-01-01"))
    pp = res.checks["power_prob"]
    # 两条假设 → 解析 577（z_a(2)=1.96）/ T75 实测 1500×1.420=2130
    # （更严）→ 需 2130 触发日；A3 2200 日过；A1 1 日不足
    assert pp["n_required_analytic"] == 577
    assert pp["n_required_used"] == 2130
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


# ------------------------------------------- T75 实测样本量规则（T76 收尾）
# 计划 docs/superpowers/plans/2026-10-29-p-line-t76-finalize.md 模块 A §5：
# 解析 D52 与 T75 实测（月内置换标定，2026-10-29 批准）取更严者。

_FOUR_HYPOTHESES = ["S1:A1:follow>base", "S1:A2:follow>base",
                    "S1:A3:fade>base", "S1:A4:fade>base"]


def _prob_candidate(**prob_over):
    """单品种候选（覆盖 [prob] 字段），4 假设档位默认带满。"""
    data = prob_data()
    over = {"hypotheses": list(_FOUR_HYPOTHESES)}
    over.update(prob_over)
    data["prob"] = {**data["prob"], **over}
    return cand.validate_candidate(data, source="t")


def _rows_for_days(action, n_days, atr=20.0):
    """A3 在 n_days 个**不同**日各触发一次（atr 抬高可压低 R 成本）。"""
    days = pd.date_range("2018-01-01", periods=n_days, freq="1D")
    return [("TEST", "S1", action, 9, atr, 2000.0, str(d)) for d in days]


def test_measured_rule_stricter_wins(data_root):
    """实测更严以实测为准：4 假设 Δp=0.03 → 解析 1340 vs 实测
    2000×1.856=3712 → required 3712（2000 日按旧解析口径会过、现在待数据）。"""
    c = _prob_candidate(expected_delta_p=0.03)
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(_rows_for_days("A3", 2000)),
                             split=pd.Timestamp("2025-01-01"))
    pp = res.checks["power_prob"]
    assert pp["n_required_analytic"] == 1340
    assert pp["n_required_measured"] == 3712    # 2000×(2.241/1.645)²=3711.8→3712
    assert pp["n_required_used"] == 3712
    assert pp["rule_source"] == "t75-measured+z2"
    assert pp["per_hypothesis"]["S1:A3:fade>base"]["passed"] is False
    assert res.decision == "waiting_data"


def test_analytic_stricter_in_extrapolation_region(data_root):
    """外推区解析更严 → 取解析：4 假设 Δp=0.20 → 实测沿段斜率外推为负、
    max(0,·) 截断到 0；解析 31 → required 31，50 日过。"""
    c = _prob_candidate(expected_delta_p=0.20)
    res = cand.precheck_prob(c, data_root=data_root, cost_model=FlatCosts(),
                             counted=counted_df(_rows_for_days("A3", 50)),
                             split=pd.Timestamp("2025-01-01"))
    pp = res.checks["power_prob"]
    assert pp["n_required_measured"] == 0     # 2000−50000×(0.20−0.03)=−6500→0
    assert pp["n_required_analytic"] == 31
    assert pp["n_required_used"] == 31
    assert pp["open_ended"] is False
    assert pp["per_hypothesis"]["S1:A3:fade>base"]["passed"] is True
    assert res.decision == "pass"


def test_open_ended_interval_requires_strictly_more(data_root):
    """开区间档（Δp=0.02 端继承开区间语义）：4 假设 4000×1.856=7424、
    open_ended=True——days=7424 不算过（须 >），days=7425 过。"""
    c = _prob_candidate(expected_delta_p=0.02)
    kwargs = dict(data_root=data_root, cost_model=FlatCosts(),
                  split=pd.Timestamp("2025-01-01"))
    res_7424 = cand.precheck_prob(
        c, counted=counted_df(_rows_for_days("A3", 7424, atr=100.0)), **kwargs)
    pp = res_7424.checks["power_prob"]
    assert pp["open_ended"] is True
    assert pp["n_required_measured"] == 7424     # 4000×(2.241/1.645)²=7423.6→7424
    assert pp["n_required_used"] == 7424         # 解析 3014（D52）不敌实测
    assert pp["per_hypothesis"]["S1:A3:fade>base"]["passed"] is False
    assert res_7424.decision == "waiting_data"
    res_7425 = cand.precheck_prob(
        c, counted=counted_df(_rows_for_days("A3", 7425, atr=100.0)), **kwargs)
    assert res_7425.checks["power_prob"][
        "per_hypothesis"]["S1:A3:fade>base"]["passed"] is True
    assert res_7425.decision == "pass"


def test_measured_none_below_calibration_propagates_waiting(data_root):
    """Δp=0.015（< 标定下界 0.02）实测未覆盖 → None 传播：4 假设恒待数据
    （6000 日已超解析要求，仍被实测 None 卡住——不随触发日数增加改变）。"""
    c = _prob_candidate(expected_delta_p=0.015)
    res = cand.precheck_prob(
        c, counted=counted_df(_rows_for_days("A3", 6000, atr=100.0)),
        data_root=data_root, cost_model=FlatCosts(),
        split=pd.Timestamp("2025-01-01"))
    pp = res.checks["power_prob"]
    assert pp["n_required_measured"] is None
    assert pp["n_required_used"] is None
    ph = pp["per_hypothesis"]["S1:A3:fade>base"]
    assert ph["required_days"] is None and ph["passed"] is False
    assert pp["n_required_analytic"] < 6000     # 旧解析口径下会过 → 决定翻转
    assert res.decision == "waiting_data"


def test_measured_rule_numeric_self_checks():
    """数值自检（T75 报告 §3 推导式，计划模块 A）：
    4 假设 Δp=0.03 → 2000×1.856=3712；2 假设 Δp=0.04 → 单假设 1500×
    (1.960/1.645)²=2130；12 假设 Δp=0.05 → 1000×2.572=2572。"""
    assert cand.measured_n_required(4, 0.03) == (3712, True)
    assert cand.measured_n_required(2, 0.04) == (2130, False)
    assert cand.measured_n_required(12, 0.05) == (2572, False)
    # 单假设档（z² 换算系数=1）：锚点原样，0.02 端开、0.03 起闭
    assert cand.measured_n_required(1, 0.02) == (4000, True)
    assert cand.measured_n_required(1, 0.03) == (2000, True)
    assert cand.measured_n_required(1, 0.05) == (1000, False)
    assert cand.measured_n_required(1, 0.015) == (None, False)


def test_measured_rule_monotonicity():
    """性质测试（两维，扫描覆盖插值/外推全部档位——外推方向反了会被
    Δp 扫描抓住）：n_required 对 Δp 非增（同假设数）、对假设数非减（同 Δp）。"""
    dps = [0.02, 0.025, 0.029, 0.03, 0.031, 0.04, 0.049, 0.05,
           0.06, 0.08, 0.10, 0.15, 0.20]
    for n_hyp in (1, 2, 4, 12):
        vals = [cand.measured_n_required(n_hyp, dp)[0] for dp in dps]
        for dp, v, v_next in zip(dps, vals, vals[1:]):
            assert v is not None and v_next is not None
            assert v_next <= v, (
                f"n_hyp={n_hyp}：dp {dp}→{dps[dps.index(dp) + 1]} 实测样本量 "
                f"{v}→{v_next} 递增（应对 Δp 非增）")
    for dp in dps:
        vals = [cand.measured_n_required(k, dp)[0] for k in range(1, 13)]
        assert all(b >= a for a, b in zip(vals, vals[1:])), (
            f"dp={dp}：假设数 1..12 的实测样本量 {vals} 应非减")


# ------------------------------------------- T97 数据解锁批（M2：登记与文档）

from pathlib import Path  # noqa: E402  （追加节：不动上方既有模块级定义）

_T97_CANDIDATES_DIR = Path(__file__).resolve().parents[1] / \
    "research" / "candidates"


def test_c104_loads_with_new_prob_keys():
    """C104 装载：三新键读出正确（tuple 化）+ 逐品种网格 + D51 前驱逐字。"""
    c = cand.load_candidate(_T97_CANDIDATES_DIR / "C104.toml")
    assert c.id == "C104" and c.status == "screened_fail"  # T97 实运行后回填（2026-10-29）
    assert c.symbols == ("XAUUSD", "EURUSD")
    p = c.prob_declared
    assert p.level_grid == {"XAUUSD": 50.0, "EURUSD": 0.0050}
    assert p.max_gap_days == 30
    assert p.cost_scenario_mult == (1.0, 1.5)      # list → tuple
    assert p.cost_scenario_reference == 3.0
    # D51：approach/bracket/Δp 与前驱 C101 逐字相同
    p101 = cand.load_candidate(_T97_CANDIDATES_DIR / "C101.toml").prob_declared
    assert p.approach == p101.approach
    assert p.bracket == p101.bracket
    assert p.breakout_atr == p101.breakout_atr
    assert p.retest_window_bars == p101.retest_window_bars
    assert p.expected_delta_p == p101.expected_delta_p


def test_prob_without_new_keys_defaults_none():
    """缺省兼容（硬约束）：不带新键的 prob_data() 候选装载后三新字段 None。"""
    c = cand.validate_candidate(prob_data(), source="t")
    p = c.prob_declared
    assert p.max_gap_days is None
    assert p.cost_scenario_mult is None
    assert p.cost_scenario_reference is None


def test_prob_new_key_validations():
    """三个新键各自的非法形态被拒：max_gap_days=0 / 首元素非 1 /
    reference=-1。"""
    base = prob_data()["prob"]
    with pytest.raises(cand.CandidateError, match="max_gap_days"):
        cand.validate_candidate(
            prob_data(prob={**base, "max_gap_days": 0}), source="t")
    with pytest.raises(cand.CandidateError, match="cost_scenario_mult"):
        cand.validate_candidate(
            prob_data(prob={**base, "cost_scenario_mult": [0.5, 1.5]}),
            source="t")
    with pytest.raises(cand.CandidateError, match="cost_scenario_reference"):
        cand.validate_candidate(
            prob_data(prob={**base, "cost_scenario_reference": -1}),
            source="t")


def test_c101_to_c103_status_backfilled():
    """C101–C103 状态回填守护：TOML 文本 grep screened_fail + 行内注释
    引报告 §4；装载后 status 枚举值正确。"""
    for cid in ("C101", "C102", "C103"):
        text = (_T97_CANDIDATES_DIR / f"{cid}.toml").read_text(
            encoding="utf-8")
        assert 'status = "screened_fail"' in text, cid
        assert "# 2026-10-29 预筛 fail（报告 §4" in text, cid
        assert "C104-C106" in text, cid
        assert cand.load_candidate(
            _T97_CANDIDATES_DIR / f"{cid}.toml").status == "screened_fail"
