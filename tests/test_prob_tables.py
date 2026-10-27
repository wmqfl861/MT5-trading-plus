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
    """p_格 = (k + m·p_基准)/(n + m)；r̄ 同法；SE 按格内实际 n。"""
    labels = make_labels(30, 30, 0, 0)          # 只用 above 填格；below 空
    labels = labels[labels.trend_sma200 == "above"]
    m, min_cell = 100, 10
    tbl = ProbTable.fit(labels, group_by=["trend_sma200"],
                        prior_strength=m, min_cell=min_cell)
    est = tbl.lookup("S1", "A3", {"trend_sma200": "above"})
    n = 60
    p_base_win, p_base_loss = 30 / n, 30 / n
    p_win = (30 + m * p_base_win) / (n + m)
    p_loss = (30 + m * p_base_loss) / (n + m)
    assert est.n == n
    assert est.p_win == pytest.approx(p_win)
    assert est.p_loss == pytest.approx(p_loss)
    assert est.p_time == pytest.approx((0 + m * 0.0) / (n + m))
    assert est.r_win == pytest.approx((30 * 1.9 + m * 1.9) / (n + m))
    assert est.r_loss == pytest.approx((30 * -1.05 + m * -1.05) / (n + m))
    assert est.se_win == pytest.approx(math.sqrt(p_win * (1 - p_win) / n))


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
