"""T72 概率表：经验贝叶斯收缩、最小样本回退、序列化往返。"""
import math

import pandas as pd
import pytest

from mt5quant.prob.tables import CellEstimate, ProbTable


def make_labels(n_above_win, n_above_loss, n_below_win, n_below_loss,
                *, n_time=0, r_win=1.9, r_loss=-1.05, r_time=0.1,
                situation="S1", action="A3"):
    rows = []
    for _ in range(n_above_win):
        rows.append(("above", "win", r_win))
    for _ in range(n_above_loss):
        rows.append(("above", "loss", r_loss))
    for _ in range(n_time):
        rows.append(("above", "time", r_time))
    for _ in range(n_below_win):
        rows.append(("below", "win", r_win))
    for _ in range(n_below_loss):
        rows.append(("below", "loss", r_loss))
    return pd.DataFrame(rows, columns=["trend_sma200", "outcome", "net_r"]).assign(
        situation=situation, action=action,
        side=1, level=100.0,
        time=pd.date_range("2024-01-01", periods=len(rows), freq="h"))


def test_shrinkage_formula_exact():
    """p_格 = (k + m·p_基准)/(n + m)；r̄_o = (Σr_o + m·p_base_o·r_base_o)/
    (k_o + m·p_base_o)（审核修正 1）；SE 按格内实际 n。"""
    # 格内分布与基准不同（win 均值 2.2 vs 基准 1.9），公式检验才有区分度
    labels = make_labels(30, 30, 0, 0, r_win=2.2)
    labels = labels[labels.trend_sma200 == "above"]
    labels_other = make_labels(0, 0, 40, 60, r_win=1.9)   # below 格（凑基准分布）
    labels_all = pd.concat([labels, labels_other], ignore_index=True)
    m, min_cell = 100, 10
    tbl = ProbTable.fit(labels_all, group_by=["trend_sma200"],
                        prior_strength=m, min_cell=min_cell)
    n_all = len(labels_all)
    base = tbl.base_rate("A3")
    p_bw = 70 / n_all                       # 基准：70 win / 160
    est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
    n = 60
    p_win = (30 + m * p_bw) / (n + m)
    assert est.n == n
    assert est.p_win == pytest.approx(p_win)
    assert est.p_loss == pytest.approx((30 + m * base.p_loss) / (n + m))
    # r̄ 按结局伪样本数收缩（审核修正 1）
    r_win = (30 * 2.2 + m * p_bw * base.r_win) / (30 + m * p_bw)
    assert est.r_win == pytest.approx(r_win)
    assert est.r_loss == pytest.approx(
        (30 * -1.05 + m * base.p_loss * base.r_loss) / (30 + m * base.p_loss))
    assert est.se_win == pytest.approx(math.sqrt(p_win * (1 - p_win) / n))


def test_rbar_same_distribution_equals_base_mean():
    """审核修正 1（a）：格与基准同分布时 r̄ 精确等于基准条件均值。"""
    labels = make_labels(40, 60, 0, 0)          # 全部 above，分布即基准
    labels = labels[labels.trend_sma200 == "above"]
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=10)
    est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
    assert est.r_win == pytest.approx(1.9)       # 基准条件均值，不受 m 影响
    assert est.r_loss == pytest.approx(-1.05)
    assert est.r_time == pytest.approx(0.0)


def test_rbar_large_n_approaches_empirical_conditional_mean():
    """审核修正 1（b）：n 很大时 r̄ 趋近经验条件均值（而非 p×条件均值）。"""
    labels = make_labels(90_000, 10_000, 0, 0, r_win=1.5)
    labels = labels[labels.trend_sma200 == "above"]
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=100)
    est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
    assert est.r_win == pytest.approx(1.5, abs=1e-3)     # 旧式会趋近 0.9×1.5
    assert est.r_loss == pytest.approx(-1.05, abs=1e-3)


def test_ev_from_lookup_matches_hand_calc():
    """审核修正 1（c）：查表估计经 ev_lower 得到的 EV 与手算一致。"""
    from mt5quant.prob.decision import ev_lower
    labels = make_labels(400, 500, 0, 0)
    labels = labels[labels.trend_sma200 == "above"]
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=10)
    est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
    n, m = 900, 100
    base = tbl.base_rate("A3")
    k_w, k_l = 400, 500
    p_win = (k_w + m * base.p_win) / (n + m)
    p_loss = (k_l + m * base.p_loss) / (n + m)
    r_win = (k_w * 1.9 + m * base.p_win * 1.9) / (k_w + m * base.p_win)  # 同分布→1.9
    r_loss = (k_l * -1.05 + m * base.p_loss * -1.05) / (k_l + m * base.p_loss)
    se_win = math.sqrt(p_win * (1 - p_win) / n)
    se_loss = math.sqrt(p_loss * (1 - p_loss) / n)
    z = 1.0
    expected = ((p_win - z * se_win) * r_win + (p_loss + z * se_loss) * r_loss
                + (1 - (p_win - z * se_win) - (p_loss + z * se_loss)) * est.r_time)
    ev = ev_lower(est, z=z)
    assert ev == pytest.approx(expected, rel=1e-12)


def test_empty_cell_falls_back_to_base():
    """n = 0 的格等于基准（单局面单动作时，上一级聚合即全样本基准）。"""
    labels = make_labels(40, 60, 0, 0)
    labels = labels[labels.trend_sma200 == "above"]
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=10)
    est = tbl.lookup("S1", "A3", {"trend_sma200": "below"})   # 从未出现
    base = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
    assert est.n == 100
    assert est.p_win == pytest.approx(base.p_win)
    assert est.p_loss == pytest.approx(base.p_loss)
    # 局面完全未见时同样落到该动作基准
    est2 = tbl.lookup("S9", "A3", {"trend_sma200": "above"})
    assert est2.p_win == pytest.approx(base.p_win)


def test_large_n_approaches_empirical():
    """n 很大时收缩趋近经验频率。"""
    labels = make_labels(90_000, 10_000, 0, 0)
    labels = labels[labels.trend_sma200 == "above"]
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=100)
    est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
    assert est.p_win == pytest.approx(0.9, abs=1e-3)
    assert est.p_loss == pytest.approx(0.1, abs=1e-3)


def test_thin_cell_falls_back_to_situation_action_parent():
    """n < min_cell 的格退回上一级（局面 × 动作聚合，仍向基准收缩）。"""
    # S1 above 样本充足、below 只有 3 个（< min_cell=10）
    labels = make_labels(40, 60, 2, 1)
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=10)
    thin = tbl.lookup("S1", "A3", {"trend_sma200": "below"})
    n_parent = 103
    p_win_parent = (42 + 100 * (42 / 103)) / (n_parent + 100)
    assert thin.n == n_parent
    assert thin.p_win == pytest.approx(p_win_parent)
    # 局面 × 动作 的父级本身不受 min_cell 限制
    assert tbl.lookup("S1", "A3", {"trend_sma200": "above"}).n == 100


def test_fit_ignores_skipped_rows():
    """skipped 行不进概率（只计 win/loss/time）。"""
    labels = make_labels(30, 30, 0, 0)
    labels = labels[labels.trend_sma200 == "above"]
    skipped = pd.DataFrame([
        {"trend_sma200": "above", "outcome": "skipped", "net_r": float("nan"),
         "situation": "S1", "action": "A3", "side": 1, "level": 100.0,
         "time": pd.Timestamp("2024-02-01")},
    ])
    tbl = ProbTable.fit(pd.concat([labels, skipped], ignore_index=True),
                        group_by=["trend_sma200"], prior_strength=100,
                        min_cell=10)
    assert tbl.lookup("S1", "A3", {"trend_sma200": "above"}).n == 60


def test_lookup_missing_feature_key_uses_parent():
    """features 缺分组键 → 无法定位格，回退父级（不抛）。"""
    labels = make_labels(30, 30, 30, 30)
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=10)
    est = tbl.lookup("S1", "A3", {})
    assert est.n == 120


def test_lookup_unknown_action_raises():
    labels = make_labels(10, 10, 0, 0)
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=10)
    with pytest.raises(KeyError):
        tbl.lookup("S1", "A9", {"trend_sma200": "above"})


def test_to_dict_from_dict_roundtrip():
    labels = make_labels(40, 60, 30, 30, n_time=5)
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=100, min_cell=10)
    tbl2 = ProbTable.from_dict(tbl.to_dict())
    for feats in ({"trend_sma200": "above"}, {"trend_sma200": "below"}, {}):
        a = tbl.lookup("S1", "A3", feats)
        b = tbl2.lookup("S1", "A3", feats)
        assert a == b
    assert tbl2.to_dict() == tbl.to_dict()


def test_fit_requires_columns():
    labels = make_labels(10, 10, 0, 0).drop(columns=["situation"])
    with pytest.raises(ValueError):
        ProbTable.fit(labels, group_by=["trend_sma200"],
                      prior_strength=100, min_cell=10)


def test_cell_estimate_is_frozen():
    est = CellEstimate(n=10, p_win=0.4, p_loss=0.5, p_time=0.1, r_win=1.9,
                       r_loss=-1.05, r_time=0.1, se_win=0.05, se_loss=0.05)
    with pytest.raises(Exception):
        est.n = 11


# ------------------------------------------------- P-13（观察级，T98 audit）

class TestP13Observations:
    """P-13 [观察]：稀疏父格 / 全赢全输 / 缺失特征的**当前行为**记录——
    只钉住数值作回归锚，不断言"必须保守"；要改 min_cell/先验/SE 须走
    方法变更并留版本（audit 明示不得以结果选阈值）。"""

    def mixed_labels(self, s1_win, s1_loss, s2_win, s2_loss):
        rows = ([("S1", "win", 1.9)] * s1_win
                + [("S1", "loss", -1.05)] * s1_loss
                + [("S2", "win", 1.9)] * s2_win
                + [("S2", "loss", -1.05)] * s2_loss)
        return pd.DataFrame(
            rows, columns=["situation", "outcome", "net_r"]).assign(
            action="A3", side=1, level=100.0, trend_sma200="above",
            time=pd.date_range("2024-01-01", periods=len(rows), freq="h"))

    def test_parent_n1_n2_shrink_to_base_se_by_actual_n(self):
        """父格可 n=1/2（min_cell 只约束细格）：p̂ 向全样本基准收缩、
        SE 按格内实际 n（n=1 → SE 最大）。"""
        labels = self.mixed_labels(1, 0, 60, 40)
        tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                            prior_strength=100, min_cell=10)
        base = tbl.base_rate("A3")                       # n=101
        est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
        assert est.n == 1                                # 细格 n=1 → 父格
        p1 = (1 + 100 * base.p_win) / 101
        assert est.p_win == pytest.approx(p1)
        assert est.se_win == pytest.approx(math.sqrt(p1 * (1 - p1) / 1))

        labels2 = self.mixed_labels(1, 1, 60, 40)
        tbl2 = ProbTable.fit(labels2, group_by=["trend_sma200"],
                             prior_strength=100, min_cell=10)
        base2 = tbl2.base_rate("A3")                     # n=102
        est2 = tbl2.lookup("S1", "A3", {"trend_sma200": "above"})
        assert est2.n == 2
        p2 = (1 + 100 * base2.p_win) / 102
        assert est2.p_win == pytest.approx(p2)
        assert est2.se_win == pytest.approx(math.sqrt(p2 * (1 - p2) / 2))

    def test_cell_99_falls_back_but_100_used(self):
        """细格 n=99 < min_cell=100 → 回退父格（S1×A3 聚合，n=149）；
        n=100 恰好达标 → 用细格（SE 按格内实际 n；边界行为记录）。"""
        def fit_with(n_above):
            s1_above = self.mixed_labels(n_above, 0, 0, 0)
            s1_above = s1_above[s1_above.situation == "S1"]
            s1_below = self.mixed_labels(0, 50, 0, 0)
            s1_below = s1_below[s1_below.situation == "S1"]
            s2_below = self.mixed_labels(0, 0, 200, 0)
            s2_below = s2_below[s2_below.situation == "S2"]
            both = pd.concat(
                [s1_above.assign(trend_sma200="above"),
                 s1_below.assign(trend_sma200="below"),
                 s2_below.assign(trend_sma200="below")], ignore_index=True)
            return ProbTable.fit(both, group_by=["trend_sma200"],
                                 prior_strength=100, min_cell=100)

        t99 = fit_with(99)
        assert t99.lookup("S1", "A3",
                          {"trend_sma200": "above"}).n == 149   # 回退父格
        assert t99.lookup("S1", "A3",
                          {"trend_sma200": "below"}).n == 149   # 同上
        assert t99.lookup("S2", "A3",
                          {"trend_sma200": "below"}).n == 200   # ≥100 → 细格
        t100 = fit_with(100)
        assert t100.lookup("S1", "A3",
                           {"trend_sma200": "above"}).n == 100  # 恰好达标 → 细格

    def test_all_win_se_zero_ev_lower_hits_full_r(self):
        """全赢样本：基准=格=全赢 → p_win=1、SE(win)=0 → ev_lower 不被
        z·SE 压低，EV 下界 = r_win（P-13 观察：极强下界，如实记录）。"""
        from mt5quant.prob.decision import ev_lower
        labels = self.mixed_labels(100, 0, 0, 0)          # 全赢（含基准）
        tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                            prior_strength=100, min_cell=10)
        est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
        assert est.p_win == pytest.approx(1.0)
        assert est.se_win == pytest.approx(0.0)
        for z in (0.0, 1.0, 2.0):
            assert ev_lower(est, z=z) == pytest.approx(1.9)

    def test_all_loss_and_mixed_base_hand_calc(self):
        """全输格（基准同为全输）：p_win=0、SE=0 → EV 下界 = r_loss；
        混合基准下的全赢格按收缩公式手算（记录 z·SE 的实际压低幅度）。"""
        from mt5quant.prob.decision import ev_lower
        labels = self.mixed_labels(0, 100, 0, 0)
        tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                            prior_strength=100, min_cell=10)
        est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
        assert est.p_win == pytest.approx(0.0)
        assert ev_lower(est, z=1.0) == pytest.approx(-1.05)
        # 混合基准（S2 大样本 win/loss 各半）：S1 全赢 100 格
        labels2 = self.mixed_labels(100, 0, 50, 50)
        tbl2 = ProbTable.fit(labels2, group_by=["trend_sma200"],
                             prior_strength=100, min_cell=10)
        base = tbl2.base_rate("A3")                      # n=200，p=0.75
        est2 = tbl2.lookup("S1", "A3", {"trend_sma200": "above"})
        p = (100 + 100 * 0.75) / 200
        assert est2.p_win == pytest.approx(p)
        assert est2.se_win == pytest.approx(math.sqrt(p * (1 - p) / 100))
        expected = ev_lower(est2, z=1.0)
        assert expected < 1.9                            # 有 SE 压低（记录）
        assert expected == pytest.approx(
            (est2.p_win - est2.se_win) * est2.r_win
            + (est2.p_loss + est2.se_loss) * est2.r_loss
            + (1 - (est2.p_win - est2.se_win)
               - (est2.p_loss + est2.se_loss)) * est2.r_time)

    def test_missing_feature_forms_own_na_cell_not_fallback(self):
        """特征缺值行（"na"）自成细格——与"缺值回退父级"的直觉不同
        （P-13 观察：策略侧把 None 转 "na"，格够大即被直接使用）。"""
        na = self.mixed_labels(30, 10, 0, 0).assign(trend_sma200="na")
        na = na[na.situation == "S1"]
        above = self.mixed_labels(40, 60, 0, 0)
        above = above[above.situation == "S1"]
        labels = pd.concat([above, na], ignore_index=True)
        tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                            prior_strength=100, min_cell=10)
        est_na = tbl.lookup("S1", "A3", {"trend_sma200": "na"})
        assert est_na.n == 40                            # S1 的 na 细格本体
        base = tbl.base_rate("A3")                       # n=140，win 70
        assert est_na.p_win == pytest.approx((30 + 100 * base.p_win) / 140)
        assert est_na.p_win != pytest.approx(
            tbl.lookup("S1", "A3", {"trend_sma200": "above"}).p_win)
