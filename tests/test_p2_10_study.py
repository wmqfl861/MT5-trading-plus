"""T107 模块 B：P2-10 统计覆盖研究器（scripts/p2_10_study.py）的预注册测试。

研究器为独立脚本（零研究库依赖：不 import mt5data、不触数据根），经
importlib 装载测试。测试钉住：
- 决策层等价物与 level_decision.generate_signals 决策段语义一致（构造候选
  对比两个实现——真实 generate_signals + 冻结模型驱动 vs decide_level）；
- DGP：同日共享单一结局/按日单位、特征缺失降级、K 跨界按 time 计；
- E0 与直接 ProbTable.fit + ev_lower 逐位一致（逐位等价复刻 + 金丝雀）；
- E1/E3 手工公式点值、E2 小计数精确二分对照（全部不依赖 scipy）；
- 覆盖/误开放计量的解析极端例、判定两级门的机器边界；
- run_id 确定性与常量敏感性、结果目录只追加、源码零泄漏断言（AST）；
- CLI run/report 端到端（缩小矩阵）与分片合并的逐位确定性。

注：generate_signals 的信号值只携带方向（±1），并列候选同向时赢家身份
从真实输出不可观测——此类并列的字典序身份在 decide_level 单元级钉住，
真实实现侧验证可观测语义（方向/弃权）一致。
"""
import ast
import hashlib
import importlib.util
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO / "scripts" / "p2_10_study.py"

from mt5quant.prob.decision import ev_lower  # noqa: E402
from mt5quant.prob.tables import ProbTable  # noqa: E402


def load_study():
    spec = importlib.util.spec_from_file_location("p2_10_study_script",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p2_10_study_script"] = mod
    spec.loader.exec_module(mod)
    return mod


p2s = load_study()


# --------------------------------------------------------------- 决策层等价物

class _LevelDecisionEqProbe:
    """用真实 LevelDecisionV1.generate_signals（monkeypatch 触发/特征装配）
    驱动其决策段（~L652-684），与研究器 decide_level 在同一构造候选上对比。"""

    def __init__(self):
        import mt5quant.strategies.library.prob.level_decision as ld
        self.ld = ld

        class _EqProbeStrategy(ld.LevelDecisionV1):
            SITUATIONS = ("S1", "S2")   # 基类只有 S1——跨局面候选需要 S2

        self.Strategy = _EqProbeStrategy
        self.holder = {"triggers": []}

    def run(self, tbl, triggers, *, allowed_hypotheses=(), costs=None,
            z=1.0, threshold_mult=2.0, n_bars=30, feat="f0"):
        """triggers: [(bar, sit, act, side)] → (真实信号在触发 bar 的值,
        decide_level 的决策)。"""
        ld = self.ld
        costs = costs if costs is not None else {
            f"{s}|{a}": 0.05 for s in ("S1", "S2") for a in ("A1", "A2", "A4")}
        model = {
            "prob_table": tbl.to_dict(),
            "cost_r_by_situation_action": costs,
            "symbols": ["XAUUSD"], "timeframe": "H1",
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0,
                        "horizon_bars": 24},   # 基类 HORIZON_BARS 常量
        }
        strat = self.Strategy().with_model(model)
        strat.ALLOWED_HYPOTHESES = tuple(allowed_hypotheses)  # 实例级白名单
        bars = pd.DataFrame(
            {"time": pd.date_range("2026-01-05", periods=n_bars, freq="h")})
        self.holder["triggers"] = list(triggers)
        trig_bar = min(t[0] for t in triggers)

        import unittest.mock as mock
        with mock.patch.object(ld, "atr_series",
                               lambda b: pd.Series(np.ones(len(b)))), \
             mock.patch.object(ld, "trend_sma200",
                               lambda b: pd.Series([feat] * len(b),
                                                   dtype=object)), \
             mock.patch.object(ld, "atr_pct_2000",
                               lambda a: pd.Series(["p0"] * len(a),
                                                   dtype=object)), \
             mock.patch.object(ld, "level_rows_for_situation",
                               lambda *a, **k: None), \
             mock.patch.object(
                 ld, "find_level_events",
                 lambda bars_, levels, *, situation, approach, atr:
                 pd.DataFrame({"situation": [situation]})), \
             mock.patch.object(ld, "action_triggers", self._fake_triggers):
            sig = strat.generate_signals(bars, {})
        # 研究器侧：同表 lookup + 真 ev_lower + decide_level
        cands, evs = [], []
        for _, sit, act, side in triggers:
            try:
                est = tbl.lookup(sit, act, {"trend_sma200": feat})
            except KeyError:
                est = None
            cands.append((sit, act, side, costs.get(f"{sit}|{act}")))
            evs.append(ev_lower(est, z=z) if est is not None else None)
        allowed = ld._parse_allowed_entries(allowed_hypotheses) \
            if allowed_hypotheses else None
        dec = p2s.decide_level(cands, evs, threshold_mult=threshold_mult,
                               allowed_pairs=allowed)
        return int(sig.iloc[trig_bar]), dec, sig

    def _fake_triggers(self, bars, events, *, params, atr):
        sits = set(events["situation"].astype(str))
        rows = [t for t in self.holder["triggers"] if t[1] in sits]
        return pd.DataFrame(rows, columns=["bar", "situation", "action",
                                           "side"])


def eq_table():
    """等价性测试表（group_by=trend_sma200，min_cell=10 使细格生效）：
    S1:A1 与 S2:A1 估计逐位相同（跨局面并列对）；S1:A2 独立强格；S2:A2 弱格。"""
    def rows(sit, act, n_win, n_loss):
        return pd.DataFrame({
            "situation": [sit] * (n_win + n_loss),
            "action": [act] * (n_win + n_loss),
            "trend_sma200": ["f0"] * (n_win + n_loss),
            "outcome": ["win"] * n_win + ["loss"] * n_loss,
            "net_r": [1.9] * n_win + [-1.05] * n_loss})
    df = pd.concat([
        rows("S1", "A1", 40, 10),        # 强（EV_lb≈1.21 过门槛）
        rows("S2", "A1", 40, 10),        # 与 S1:A1 逐位同估计（跨局面并列）
        rows("S1", "A2", 40, 10),        # 强（EV_lb≈0.42 过门槛，A2 基准弱）
        rows("S2", "A2", 5, 45),         # 弱（不过门槛）
    ], ignore_index=True)
    return ProbTable.fit(df, group_by=["trend_sma200"], prior_strength=100,
                         min_cell=10)


class TestDecideLevelEquivalence:
    def setup_method(self):
        self.probe = _LevelDecisionEqProbe()
        self.tbl = eq_table()

    def _check(self, triggers, expected, **kw):
        sig_val, dec, _ = self.probe.run(self.tbl, triggers, **kw)
        if expected is None:
            assert sig_val == 0, "真实实现应弃权（全 0）"
            assert dec is None, "decide_level 应弃权"
        else:
            assert sig_val == expected[2], \
                f"真实信号 {sig_val} 应等于期望赢家 side {expected[2]}"
            assert dec == expected, f"decide_level={dec} 应为 {expected}"
        return dec

    def test_single_pass_takes_it_and_holds(self):
        _, dec, sig = self.probe.run(self.tbl, [(5, "S1", "A1", 1)])
        assert dec == ("S1", "A1", 1)
        assert int(sig.iloc[5]) == 1
        assert all(int(sig.iloc[j]) == 1 for j in range(5, 29))  # K=24 持有
        assert int(sig.iloc[29]) == 0                            # S4 强制归零

    def test_tie_same_side_matches_real_and_lexicographic(self):
        # 并列（S1:A1 ≡ S2:A1 逐位）且同向：真实信号只给方向；
        # decide_level 侧钉字典序身份（S1 < S2），且对输入顺序不变
        self._check([(5, "S1", "A1", 1), (5, "S2", "A1", 1)], ("S1", "A1", 1))
        dec_rev = p2s.decide_level(
            [("S2", "A1", 1, 0.05), ("S1", "A1", 1, 0.05)],
            [1.2, 1.2], threshold_mult=2.0, allowed_pairs=None)
        assert dec_rev == ("S1", "A1", 1), "并列必须按 (sit,action,side) 字典序"

    def test_direction_conflict_abstains_cross_situation(self):
        self._check([(5, "S1", "A1", 1), (5, "S2", "A1", -1)], None)

    def test_direction_conflict_abstains_same_action_two_sides(self):
        self._check([(5, "S1", "A1", 1), (5, "S1", "A1", -1)], None)

    def test_whitelist_filters_otherwise_conflicting(self):
        # 白名单只放 S1:A2 → S1:A1 被过滤（不过滤则方向冲突弃权）→ 赢 S1:A2
        self._check([(5, "S1", "A1", 1), (5, "S1", "A2", -1)],
                    ("S1", "A2", -1), allowed_hypotheses=("S1:A2",))

    def test_wildcard_whitelist(self):
        # 裸动作条目 "A1"（任意局面获准）→ 只 A1 参与 → 无冲突赢 S1:A1
        self._check([(5, "S1", "A1", 1), (5, "S1", "A2", -1)],
                    ("S1", "A1", 1), allowed_hypotheses=("A1",))

    def test_no_sample_candidate_skipped(self):
        # A4 无任何样本（lookup KeyError）→ 不参与决策；单独 A4 → 弃权
        self._check([(5, "S1", "A4", 1), (5, "S1", "A1", 1)], ("S1", "A1", 1))
        self._check([(5, "S1", "A4", 1)], None)

    def test_no_cost_candidate_skipped(self):
        # E4：无成本估计的（局面,动作）不参与决策（有样本也不行）
        costs = {f"{s}|{a}": 0.05 for s in ("S1", "S2")
                 for a in ("A1", "A2", "A4")}
        del costs["S1|A1"]
        self._check([(5, "S1", "A1", 1)], None, costs=costs)
        # 有成本时同触发本应赢 S1:A1——被无成本过滤后落到 S1:A2
        self._check([(5, "S1", "A1", 1), (5, "S1", "A2", -1)],
                    ("S1", "A2", -1), costs=costs)

    def test_below_threshold_abstains(self):
        self._check([(5, "S2", "A2", 1)], None)

    def test_coexist_same_direction_both_pass(self):
        # 同向共存（无冲突）：两强格都过门槛，信号取最高 EV 的方向
        self._check([(5, "S1", "A1", 1), (5, "S1", "A2", 1)], ("S1", "A1", 1))


# ----------------------------------------------------------------------- DGP

def _cond(**kw):
    base = dict(n_days=8, structure="mixed", base_rate=0.34, cluster=3,
                miss=0.0, k_cross=0.0)
    base.update(kw)
    return base


class TestDGP:
    def test_same_day_rows_share_single_outcome(self):
        """同日簇共享当日单一结局（行数≠有效样本；保守角点）。"""
        cond = _cond(n_days=10, cluster=5, k_cross=0.0)
        truth = p2s.draw_truth(cond, p2s.derive_rng("t", 0))
        s = p2s.draw_sample(cond, truth, p2s.derive_rng("r", 0))
        by_day = {}
        for day, oc in zip(s.day, s.outcome):
            by_day.setdefault(int(day), set()).add(int(oc))
        assert all(len(v) == 1 for v in by_day.values()), "同日必须共享结局"
        assert len(by_day) == len(p2s.SITUATIONS) * len(p2s.ACTIONS) * 10

    def test_day_is_statistical_unit(self):
        """行数 = 父格数 × 日数 × 簇大小；结局按日生成后展开。"""
        cond = _cond(n_days=7, cluster=4)
        truth = p2s.draw_truth(cond, p2s.derive_rng("t", 1))
        s = p2s.draw_sample(cond, truth, p2s.derive_rng("r", 1))
        n_parents = len(p2s.SITUATIONS) * len(p2s.ACTIONS)
        assert len(s.outcome) == n_parents * 7 * 4

    def test_feature_missing_fraction(self):
        """预写比例的行特征缺失 → na（父格/基准回退的数据来源）。"""
        cond = _cond(n_days=30, cluster=2, miss=0.3)
        truth = p2s.draw_truth(cond, p2s.derive_rng("t", 2))
        s = p2s.draw_sample(cond, truth, p2s.derive_rng("r", 2))
        na = int((s.feat == 2).sum())          # 2 = na 码
        total = len(s.feat)
        assert na == pytest.approx(0.3 * total, abs=0.06 * total)
        assert set(np.unique(s.feat)) <= {0, 1, 2}

    def test_missing_rows_degrade_via_real_lookup(self):
        """na 行进自身 na 细格；n < min_cell 时真实 lookup 回退父格/基准，
        且研究器复刻表与真表落在同一层。"""
        cond = _cond(n_days=4, cluster=2, miss=0.3)   # na 格必 < min_cell=100
        truth = p2s.draw_truth(cond, p2s.derive_rng("t", 3))
        s = p2s.draw_sample(cond, truth, p2s.derive_rng("r", 3))
        df = p2s.rows_to_labels_df(s)
        assert set(df["outcome"]) <= {"win", "loss", "time"}
        tbl = ProbTable.fit(df, group_by=["feat"], prior_strength=100,
                            min_cell=100)
        rep = p2s.replica_table(s)
        for sit in p2s.SITUATIONS:
            for act in p2s.ACTIONS:
                for fname in p2s.FEAT_VALUES + (p2s.NA_VALUE,):
                    real = tbl.lookup(sit, act, {"feat": fname})
                    level, est = rep.lookup(sit, act, fname)
                    assert est.n == real.n
                    if fname == p2s.NA_VALUE:
                        assert level in ("parent", "base"), \
                            "小样本 na 格必须回退"

    def test_k_cross_rows_become_time(self):
        """K 跨 session 边界的触发按 time 结局计（labels 口径的简化角点）。"""
        cond = _cond(n_days=12, cluster=3, k_cross=0.5)
        truth = p2s.draw_truth(cond, p2s.derive_rng("t", 4))
        s = p2s.draw_sample(cond, truth, p2s.derive_rng("r", 4))
        rows_by_day = defaultdict(list)
        for day, oc in zip(s.day, s.outcome):
            rows_by_day[int(day)].append(int(oc))
        n_crossed = 0
        for outs in rows_by_day.values():
            base = next((o for o in outs if o != 2), 2)   # 日基准结局
            n_crossed += sum(1 for o in outs if o == 2 and base != 2)
        assert n_crossed > 0, "k_cross=0.5 应产生打破同日共享的 time 行"
        # 全跨界角点：同日全变 time
        cond2 = _cond(n_days=4, cluster=3, k_cross=1.0)
        truth2 = p2s.draw_truth(cond2, p2s.derive_rng("t", 4))
        s2 = p2s.draw_sample(cond2, truth2, p2s.derive_rng("r", 4))
        assert (s2.outcome == 2).all()

    def test_truth_structures(self):
        """结构档真值形状：动作级全胜/全负=固定角点；混合=逐 rep 随机。"""
        cond = _cond(structure="action_all_win", base_rate=0.34)
        truth = p2s.draw_truth(cond, p2s.derive_rng("t", 5))
        for p in truth.values():
            assert p[0] == 1.0 and p[1] == 0.0 and p[2] == 0.0
        cond = _cond(structure="action_all_loss", base_rate=0.1)
        truth = p2s.draw_truth(cond, p2s.derive_rng("t", 5))
        for p in truth.values():
            assert p[0] == 0.0
        cond = _cond(structure="mixed", base_rate=0.34)
        t1 = p2s.draw_truth(cond, p2s.derive_rng("t", 6))
        t2 = p2s.draw_truth(cond, p2s.derive_rng("t", 7))
        assert any(not np.array_equal(t1[k], t2[k]) for k in t1), \
            "混合档真值应逐 rep 随机（Dirichlet）"
        for p in t1.values():
            assert abs(float(p.sum()) - 1.0) < 1e-12


# ------------------------------------------------------------- E0 逐位一致

def _assert_est_equal(est, real):
    for f in ("n", "p_win", "p_loss", "p_time", "r_win", "r_loss", "r_time",
              "se_win", "se_loss"):
        assert getattr(est, f) == getattr(real, f), \
            f"字段 {f} 不逐位一致：{getattr(est, f)} != {getattr(real, f)}"


class TestE0Bitwise:
    def test_replica_table_bitwise_equals_real_fit(self):
        """研究器复刻表与真实 ProbTable.fit 在全部（局面×动作×特征值×基准）
        查询上逐位一致（9 字段全 ==）。"""
        for seed in range(6):
            cond = _cond(n_days=int(2 + seed * 7), cluster=1 + seed % 3,
                         miss=0.3 if seed % 2 else 0.0,
                         k_cross=0.5 if seed % 3 == 0 else 0.0,
                         structure=("mixed", "parent_all_win", "mixed",
                                    "action_all_loss", "mixed", "mixed")[seed])
            truth = p2s.draw_truth(cond, p2s.derive_rng("t", seed))
            s = p2s.draw_sample(cond, truth, p2s.derive_rng("r", seed))
            df = p2s.rows_to_labels_df(s)
            tbl = ProbTable.fit(df, group_by=["feat"],
                                prior_strength=p2s.PRIOR_STRENGTH,
                                min_cell=p2s.MIN_CELL)
            rep = p2s.replica_table(s)
            for act in p2s.ACTIONS:
                real_b = tbl.base_rate(act)
                _, est_b = rep.base_rate(act)
                _assert_est_equal(est_b, real_b)
            for sit in p2s.SITUATIONS:
                for act in p2s.ACTIONS:
                    for fname in p2s.FEAT_VALUES + (p2s.NA_VALUE,):
                        real = tbl.lookup(sit, act, {"feat": fname})
                        _, est = rep.lookup(sit, act, fname)
                        _assert_est_equal(est, real)

    def test_e0_ev_bitwise_equals_direct_pipeline(self):
        """E0 的 EV 下界 = 直接 ProbTable.fit → lookup → ev_lower（逐位）。"""
        cond = _cond(n_days=20, cluster=3, miss=0.3)
        truth = p2s.draw_truth(cond, p2s.derive_rng("t", 9))
        s = p2s.draw_sample(cond, truth, p2s.derive_rng("r", 9))
        df = p2s.rows_to_labels_df(s)
        tbl = ProbTable.fit(df, group_by=["feat"], prior_strength=100,
                            min_cell=100)
        rep = p2s.replica_table(s)
        for sit in p2s.SITUATIONS:
            for act in p2s.ACTIONS:
                for fname in p2s.FEAT_VALUES:
                    real_ev = ev_lower(tbl.lookup(sit, act, {"feat": fname}),
                                       z=p2s.LB_Z)
                    _, est = rep.lookup(sit, act, fname)
                    assert p2s.ev_lower(est, z=p2s.LB_Z) == real_ev

    def test_canary_detects_drift(self, monkeypatch):
        """金丝雀：真实 fit 被篡改 → run_condition 立即失败（fail-closed）。"""

        class Poisoned(ProbTable):
            @classmethod
            def fit(cls, *a, **k):
                tbl = super().fit(*a, **k)
                for v in tbl._base.values():
                    object.__setattr__(v, "p_win", v.p_win + 0.01)
                    break
                return tbl

        monkeypatch.setattr(p2s, "ProbTable", Poisoned)
        with pytest.raises(Exception, match="金丝雀"):
            p2s.run_condition(_cond(n_days=3, cluster=1), m_reps=2,
                              canary=True)


# ------------------------------------------------------------ E1/E2/E3 界

ALPHA1 = 0.15865525393145707     # Φ(−1.0)


class TestBounds:
    def test_alpha_for_z(self):
        assert p2s.alpha_for_z(1.0) == pytest.approx(ALPHA1, rel=1e-12)
        assert p2s.alpha_for_z(0.0) == 0.5

    def test_wilson_hand_formula(self):
        """Wilson 下/上界的闭式手工对照（测试侧独立重推公式）。"""
        for k, n, z in [(50, 100, 1.0), (0, 10, 1.0), (10, 10, 1.0),
                        (3, 40, 1.0), (0.5, 7.5, 1.0)]:
            p = k / n
            d = 1 + z * z / n
            center = (p + z * z / (2 * n)) / d
            half = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
            assert p2s.wilson_lb(k, n, z) == pytest.approx(center - half,
                                                           rel=1e-14,
                                                           abs=1e-16)
            assert p2s.wilson_ub(k, n, z) == pytest.approx(center + half,
                                                           rel=1e-14,
                                                           abs=1e-16)
        assert p2s.wilson_lb(0, 5, 1.0) == pytest.approx(0.0, abs=1e-16)
        assert p2s.wilson_ub(5, 5, 1.0) == pytest.approx(1.0, abs=1e-16)

    def test_betai_closed_forms(self):
        """不完全贝塔：arcsine Beta(0.5,0.5) 与 Beta(2,3) 多项式闭式。"""
        for x in (0.1, 0.3, 0.5, 0.8, 0.95):
            assert p2s.betai(0.5, 0.5, x) == pytest.approx(
                2 / math.pi * math.asin(math.sqrt(x)), rel=1e-13)
            assert p2s.betai(2.0, 3.0, x) == pytest.approx(
                6 * x ** 2 - 8 * x ** 3 + 3 * x ** 4, rel=1e-13)

    def test_qbeta_exact_points(self):
        # Beta(0.5,0.5) 的 q 分位 = sin²(qπ/2)；Beta(2,3) 测试侧独立二分
        for q in (0.05, 0.25, 0.5, 0.8413):
            assert p2s.qbeta(q, 0.5, 0.5) == pytest.approx(
                math.sin(q * math.pi / 2) ** 2, rel=1e-12)
        for q in (0.1, 0.5, 0.9):
            lo, hi = 0.0, 1.0
            for _ in range(80):
                mid = 0.5 * (lo + hi)
                if 6 * mid ** 2 - 8 * mid ** 3 + 3 * mid ** 4 < q:
                    lo = mid
                else:
                    hi = mid
            assert p2s.qbeta(q, 2.0, 3.0) == pytest.approx(
                0.5 * (lo + hi), rel=1e-11, abs=1e-13)

    def test_clopper_pearson_vs_exact_bisection(self):
        """E2 小计数精确对照：测试侧用 math.comb 精确二项 CDF 独立二分。"""

        def binom_sf(k, n, p):     # P(X >= k)
            return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j)
                       for j in range(k, n + 1))

        def binom_cdf(k, n, p):    # P(X <= k)
            return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j)
                       for j in range(0, k + 1))

        for k, n, alpha in [(3, 10, 0.05), (0, 8, 0.05), (8, 8, 0.05),
                            (5, 20, ALPHA1), (1, 4, 0.05)]:
            if k == 0:
                expect_lb = 0.0     # P(X>=0)=1 恒>alpha：方程无解，惯例 lb=0
            else:
                lo, hi = 0.0, 1.0
                for _ in range(80):
                    mid = 0.5 * (lo + hi)
                    if binom_sf(k, n, mid) > alpha:
                        hi = mid      # sf 随 p 增：根在更小侧
                    else:
                        lo = mid
                expect_lb = 0.5 * (lo + hi)
            if k == n:
                expect_ub = 1.0     # P(X<=n)=1 恒>=alpha：方程无解，惯例 ub=1
            else:
                lo, hi = 0.0, 1.0
                for _ in range(80):
                    mid = 0.5 * (lo + hi)
                    if binom_cdf(k, n, mid) < alpha:
                        hi = mid      # cdf 随 p 减：值偏小=根在更小侧
                    else:
                        lo = mid
                expect_ub = 0.5 * (lo + hi)
            assert p2s.clopper_pearson_lb(k, n, alpha) == pytest.approx(
                expect_lb, rel=1e-10, abs=1e-12)
            assert p2s.clopper_pearson_ub(k, n, alpha) == pytest.approx(
                expect_ub, rel=1e-10, abs=1e-12)
        assert p2s.clopper_pearson_lb(0, 5, 0.05) == 0.0
        assert p2s.clopper_pearson_ub(5, 5, 0.05) == 1.0

    def test_jeffreys_roundtrip_and_order(self):
        """E3：分位往返 I_x(qbeta)=q，且界夹住点估计。"""
        rng = np.random.default_rng(42)
        for _ in range(40):
            k = float(rng.integers(0, 60))
            n = float(rng.integers(max(int(k), 1), 300))
            a, b = k + 0.5, n - k + 0.5
            for q in (0.05, ALPHA1, 0.5):
                x = p2s.qbeta(q, a, b)
                assert p2s.betai(a, b, x) == pytest.approx(q, rel=1e-9,
                                                           abs=1e-12)
        lb = p2s.jeffreys_lb(30, 100, ALPHA1)
        assert 0.0 < lb < 0.3 < p2s.jeffreys_ub(30, 100, ALPHA1)

    def test_alt_ev_synthesis_no_truncation(self):
        """EV 合成同构式：win 取下界、loss 取上界、time=1−两项（不截断）。"""
        est_r = (1.9, -1.05, 0.1)
        assert p2s.ev_from_bounds(0.3, 0.5, est_r) == pytest.approx(
            0.3 * 1.9 + 0.5 * -1.05 + (1 - 0.3 - 0.5) * 0.1)
        # 负概率/超一允许穿透（不截断，方向保守）
        val = p2s.ev_from_bounds(-0.1, 0.95, est_r)
        assert val == pytest.approx(-0.1 * 1.9 + 0.95 * -1.05
                                    + (1 + 0.1 - 0.95) * 0.1)

    def test_qbeta_batch_matches_scalar_tight(self):
        """批处理反演（预二分+护栏牛顿）与逐个 qbeta（纯二分）一致到
        1e-9（需求 ~1e-9 即足；实测最差 ~1e-10、绝对 ~1e-14）：a,b∈[0.5,1100]、q∈{0.05,α,0.5,0.95}
        批组成按条件固定 → 确定性不受分片影响。"""
        rng = np.random.default_rng(11)
        reqs = [(0.05, 0.5, 0.5), (0.95, 0.5, 0.5), (ALPHA1, 1.0, 1101.0),
                (ALPHA1, 1101.0, 1.0), (0.5, 1050.5, 50.5),
                (0.05, 50.5, 1050.5), (ALPHA1, 0.5, 1.5),
                (0.95, 1.5, 0.5), (0.05, 100.0, 101.0)]
        for _ in range(60):
            k = float(rng.integers(0, 900))
            n = float(rng.integers(max(int(k), 1), 1100))
            reqs.append((float(rng.choice([0.05, ALPHA1, 0.5, 0.95])),
                         k + 0.5, n - k + 0.5))
        batch = p2s.qbeta_batch(np.array(reqs))
        worst = 0.0
        for (q, a, b), got in zip(reqs, batch):
            ref = p2s.qbeta(q, a, b)
            err = abs(got - ref) / max(ref, 1e-300)
            worst = max(worst, err)
            assert got == pytest.approx(ref, rel=1e-9, abs=1e-12), \
                                  f"({q},{a},{b})：{got} vs {ref}"
        assert worst < 1e-9


# ------------------------------------------------------------------- 计量

class TestMetrics:
    def test_coverage_counters_analytic(self):
        """构造解析例：EV_lb 恒大于/恒小于 EV_true 的覆盖计量。"""
        m = p2s.Metrics()
        for _ in range(10):
            m.update(ev_lb=1.0, ev_true=0.5)       # 恒不覆盖
        out = m.finalize()
        assert out["covered"] == 0 and out["coverage"] == 0.0
        m2 = p2s.Metrics()
        for _ in range(10):
            m2.update(ev_lb=0.2, ev_true=0.5)      # 恒覆盖
        out2 = m2.finalize()
        assert out2["coverage"] == 1.0
        assert out2["width_mean"] == pytest.approx(0.3)

    def test_false_open_and_stability_counters(self):
        m = p2s.Metrics()
        m.update(ev_lb=0.0, ev_true=-1.0, null=True, opened=True,
                 dec=("S1", "A1", 1))
        m.update_draw2(("S1", "A1", 1))
        m.update(ev_lb=0.0, ev_true=-1.0, null=True, opened=False, dec=None)
        m.update_draw2(None)
        m.update(ev_lb=0.0, ev_true=-1.0, null=False, opened=True,
                 dec=("S1", "A1", 1))   # 非空真 rep：不计入误开放分母
        m.update_draw2(None)            # 第 3 rep 两次决策不一致
        out = m.finalize()
        assert out["null_reps"] == 2
        assert out["false_opens"] == 1
        assert out["false_open"] == pytest.approx(0.5)
        assert out["stability"] == pytest.approx(2 / 3)


class TestGates:
    def test_coverage_two_tier_boundaries(self):
        v = p2s.coverage_verdict
        assert v(0.95) == "PASS" and v(0.9501) == "PASS" and v(1.0) == "PASS"
        assert v(0.9499) == "WARN" and v(0.9) == "WARN"
        assert v(0.80) == "WARN" and v(0.8001) == "WARN"
        assert v(0.7999) == "RED" and v(0.5) == "RED" and v(0.0) == "RED"

    def test_false_open_two_tier_boundaries(self):
        v = p2s.false_open_verdict
        assert v(0.0) == "PASS" and v(0.05) == "PASS" and v(0.0499) == "PASS"
        assert v(0.0501) == "WARN" and v(0.10) == "WARN"
        assert v(0.0999) == "WARN"
        assert v(0.1001) == "RED" and v(0.5) == "RED"

    def test_condition_gate_combination(self):
        g = p2s.condition_gate
        assert g(0.95, 0.05, null_reps=2000) == "PASS"
        assert g(0.95, 0.06, null_reps=2000) == "WARN"
        assert g(0.90, 0.11, null_reps=2000) == "RED"   # 误开放红牌
        assert g(0.79, 0.05, null_reps=2000) == "RED"   # 覆盖红牌
        # 空真 rep 不足 → 误开放不判（NA），覆盖照判
        assert g(0.95, 0.20, null_reps=10) == "PASS"
        assert g(0.79, 0.20, null_reps=10) == "RED"


# ------------------------------------------------------------- 条件与运行

class TestRunCondition:
    def test_mixed_condition_metrics_complete(self):
        cond = _cond(n_days=2, cluster=1, base_rate=0.1)
        out = p2s.run_condition(cond, m_reps=40, canary=True,
                                collect_failures=True)
        for est in p2s.ESTIMATORS:
            mt = out["metrics"][est]
            assert 0.0 <= mt["coverage"] <= 1.0
            assert 0.0 <= mt["stability"] <= 1.0
            if mt["false_open"] is not None:
                assert 0.0 <= mt["false_open"] <= 1.0
        assert out["metrics"]["E0"]["reps"] == 40
        assert out["canary_ok"] is True

    def test_action_all_win_coverage_collapses(self):
        """预判锚的端到端复现：动作级全胜 → 收缩格 p̂≡1、SE=0 → E0 覆盖塌
        向 ~50%（红牌候选）。"""
        cond = _cond(n_days=50, cluster=3, structure="action_all_win",
                     base_rate=0.34)
        out = p2s.run_condition(cond, m_reps=400, canary=True)
        cov = out["metrics"]["E0"]["coverage"]
        assert cov < 0.70, f"动作级全胜覆盖应塌向 50%，实测 {cov}"

    def test_determinism_same_seeds(self):
        cond = _cond(n_days=3, cluster=2, miss=0.3, k_cross=0.5)
        a = p2s.run_condition(cond, m_reps=25, canary=False)
        b = p2s.run_condition(cond, m_reps=25, canary=False)
        assert a["metrics"] == b["metrics"]

    def test_rep_range_subset_of_full(self):
        """逐 rep 种子派生：子段运行与全跑的对应子列一致（覆盖数不超全跑）。"""
        cond = _cond(n_days=3, cluster=1)
        full = p2s.run_condition(cond, m_reps=30, canary=False)
        part = p2s.run_condition(cond, m_reps=30, canary=False,
                                 rep_range=(10, 30))
        for est in p2s.ESTIMATORS:
            assert part["metrics"][est]["covered"] <= \
                full["metrics"][est]["covered"]


# --------------------------------------------------------- run_id 与目录

class TestRunId:
    def test_deterministic(self):
        assert p2s.compute_run_id() == p2s.compute_run_id()

    def test_sensitive_to_every_frozen_constant(self, monkeypatch):
        base_id = p2s.compute_run_id()
        constants = p2s.frozen_constants()

        def mutate(v):
            if isinstance(v, bool):
                return not v
            if isinstance(v, int):
                return v + 1
            if isinstance(v, float):
                return v + 0.5
            if isinstance(v, str):
                return v + "x"
            if isinstance(v, tuple):
                return v + ("x",)
            raise TypeError(v)

        for name in constants:
            monkeypatch.setattr(p2s, name, mutate(getattr(p2s, name)))
            assert p2s.compute_run_id() != base_id, \
                f"冻结常量 {name} 变化必须改变 run_id"
            monkeypatch.undo()
            assert p2s.compute_run_id() == base_id

    def test_manifest_contents(self):
        man = p2s.build_manifest(shard_n=1)
        for key in ("run_id", "seed_base", "m_reps", "constants", "matrix",
                    "code_sha256", "deps_sha256", "prereg_anchors"):
            assert key in man
        assert set(man["deps_sha256"]) == {
            "src/mt5quant/prob/tables.py",
            "src/mt5quant/prob/decision.py",
            "src/mt5quant/strategies/library/prob/level_decision.py",
            "scripts/p2_10_study.py"}
        h = hashlib.sha256(
            (REPO / "src/mt5quant/prob/tables.py").read_bytes()).hexdigest()
        assert man["deps_sha256"]["src/mt5quant/prob/tables.py"] == h
        assert len(man["matrix"]) == p2s.matrix_size()
        assert len(man["run_id"]) == 16


class TestZeroLeak:
    def test_source_imports_allowlist(self):
        """源码 AST 级断言：只允许 stdlib/numpy/pandas/两个 prob 研究对象
        模块；不得出现 mt5data / data_research / 策略库依赖链。"""
        text = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "mt5data" not in text
        assert "data_research" not in text
        tree = ast.parse(text)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        allowed = {"__future__", "argparse", "ast", "collections", "hashlib",
                   "itertools", "json", "math", "sys", "time", "pathlib",
                   "dataclasses", "typing", "datetime", "functools",
                   "numpy", "pandas",
                   "mt5quant.prob.tables", "mt5quant.prob.decision"}
        bad = imported - allowed
        assert not bad, f"研究器出现未准入 import：{bad}"
        assert "mt5quant.prob.tables" in imported      # 只读使用研究对象
        assert "mt5quant.prob.decision" in imported

    def test_no_imports_on_constant_access(self):
        """常量/矩阵计算纯内存，不触任何数据根（无新模块装载）。"""
        before = set(sys.modules)
        p2s.frozen_constants()
        p2s.matrix_conditions()
        assert not (set(sys.modules) - before)


# -------------------------------------------------------------------- CLI

def _shrink_matrix(monkeypatch):
    monkeypatch.setattr(p2s, "M_REPS", 40)
    monkeypatch.setattr(p2s, "PARENT_N_DAYS", (2,))
    monkeypatch.setattr(p2s, "STRUCTURES", ("mixed", "action_all_win"))
    monkeypatch.setattr(p2s, "BASE_RATES", (0.1,))
    monkeypatch.setattr(p2s, "CLUSTERS", (1,))
    monkeypatch.setattr(p2s, "MISS_FRACS", (0.0,))
    monkeypatch.setattr(p2s, "K_CROSS_FRACS", (0.0,))


class TestCli:
    def test_run_creates_results_and_gate(self, monkeypatch, tmp_path):
        _shrink_matrix(monkeypatch)
        root = tmp_path / "p210"
        assert p2s.main(["run", "--root", str(root)]) == 0
        run_id = p2s.compute_run_id()
        run_dir = root / run_id
        man = json.loads(
            (run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert man["run_id"] == run_id
        assert man["m_reps"] == 40
        assert man["shard_n"] == 1
        res = json.loads(
            (run_dir / "results.json").read_text(encoding="utf-8"))
        assert len(res["conditions"]) == 2
        assert res["run_id"] == run_id
        gate = json.loads((run_dir / "gate.json").read_text(encoding="utf-8"))
        assert gate["overall"] in ("PASS", "WARN", "RED")
        assert len(gate["conditions"]) == 2
        reds = [k for k, v in gate["conditions"].items()
                if v["verdict"] == "RED"]
        assert reds, "缩小矩阵中动作级全胜必须红牌"
        fails = json.loads(
            (run_dir / "failures.json").read_text(encoding="utf-8"))
        assert fails, "红牌条件必须有失败案例"
        for entry in fails:
            assert {"cond", "rep", "ev_lb", "ev_true", "decision",
                    "sample"} <= set(entry)

    def test_run_refuses_existing_dir(self, monkeypatch, tmp_path):
        _shrink_matrix(monkeypatch)
        root = tmp_path / "p210"
        assert p2s.main(["run", "--root", str(root)]) == 0
        assert p2s.main(["run", "--root", str(root)]) == 2, \
            "同 run_id 目录已存在必须拒绝覆盖（只追加）"

    def test_shards_merge_equals_unsharded(self, monkeypatch, tmp_path):
        _shrink_matrix(monkeypatch)
        whole = tmp_path / "whole"
        assert p2s.main(["run", "--root", str(whole)]) == 0
        run_id = p2s.compute_run_id()
        res_whole = json.loads(
            (whole / run_id / "results.json").read_text(encoding="utf-8"))
        sharded = tmp_path / "sharded"
        for i in range(3):
            assert p2s.main(["run", "--root", str(sharded),
                             "--shard-i", str(i), "--shard-n", "3"]) == 0
        res_shard = json.loads(
            (sharded / run_id / "results.json").read_text(encoding="utf-8"))
        assert res_shard == res_whole, "分片合并结果必须与整跑逐位一致"

    def test_report_skeleton(self, monkeypatch, tmp_path):
        _shrink_matrix(monkeypatch)
        root = tmp_path / "p210"
        p2s.main(["run", "--root", str(root)])
        run_id = p2s.compute_run_id()
        assert p2s.main(["report", run_id, "--root", str(root)]) == 0
        md = (root / run_id / "report.md").read_text(encoding="utf-8")
        assert run_id in md
        for token in ("覆盖", "误开放", "E0", "E1", "E3", "红牌", "结论"):
            assert token in md
        assert ("待补" in md) or ("留白" in md), "骨架结论节必须显式留白"
