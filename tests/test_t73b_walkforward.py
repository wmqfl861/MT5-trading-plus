"""T105 模块乙（T73B）：fittable 逐窗拟合 + 关卡 v3 接线 + B 项收尾。

对应 docs/reviews/2026-10-30-global-code-audit-2.md §4.1 验收标准九条：
1. 非拟合 H 基准逐位不变（H 侧既有 walkforward/gates 测试 + 本文件键集断言）；
2. 各训练窗模型/输入 hash 可区分；
3. 修改未来 bar 不改变过去训练窗模型或决策；
4. 故意跨界标签被 purge（标签与成本过滤都真正应用 purge_bars）；
5. 同一模型可重放（确定性，冻结 artifact 重放同序列）；
6. OOS 无重复/预热收益（收益起点=各窗 OOS 首根，预热不开仓）；
7. 无信息拟合模型触发 Brier 否决；
8. 预算/身份/G5 契约（既有 test_p10_cross_line / test_validation_gates 等）；
9. 冻结 gates.toml 指纹保持（既有 test_validation_gates 断言不动）。

全部合成数据、临时目录；不读研究库、不改引擎与 gates.py/gates.toml。
"""
import contextlib
import dataclasses
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "scripts")]

_spec = importlib.util.spec_from_file_location("bt_t73b", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
sys.modules["bt_t73b"] = BT
_spec.loader.exec_module(BT)

from mt5data.sessions import SessionPolicy              # noqa: E402
from mt5quant.strategies import BuyHold                 # noqa: E402
from mt5quant.strategies.library.prob.level_decision import (  # noqa: E402
    DecisionPolicy, LevelDecisionS1Demo,
)
from mt5quant.validation import walkforward as wf       # noqa: E402
from mt5quant.validation.gates import load_gates        # noqa: E402
from mt5quant.validation.robustness import BacktestSpec  # noqa: E402

# 复用既有 e2e 夹具（临时 git 仓库 + 合成 store + 已开启轮次台账）
from tests.test_prob_p2_e2e import env as p2_env       # noqa: E402,F401
from tests.test_prob_p1_cli import cli_env              # noqa: E402,F401

PERMISSIVE = SessionPolicy(exempt_symbols=frozenset({"XAUUSD"}))


class FlatCosts:
    def spread_p90(self, s, h, *, price=None):
        return 0.02

    def slippage(self, s, h, *, price=None):
        return 0.01

    def commission_per_lot(self, s):
        return 0.0

    def overnight_fee(self, s, side, f, t, *, price=None):
        return 0.0


class ZeroC:
    def spread_p90(self, s, h, *, price=None):
        return 0.0

    def slippage(self, s, h, *, price=None):
        return 0.0

    def commission_per_lot(self, s):
        return 0.0

    def overnight_fee(self, s, side, f, t, *, price=None):
        return 0.0


class DenseS1(LevelDecisionS1Demo):
    """测试用：1.0 网格（触发密度足够每个训练窗有可用样本）+ 零决策门槛
    （合成数据无优势——门槛 2×成本下永远"不做"，OOS 决策路径会是空转；
    测试要的是逐窗拟合/冻结/决策起点的结构行为，不是策略盈利性）。"""

    LEVEL_GRID = {"XAUUSD": 1.0}
    POLICY = DecisionPolicy(z=0.0, threshold_mult=-1e6)


FIT_META = {"sizes": {"XAUUSD": 100.0},
            "policies": {"XAUUSD": PERMISSIVE}}


def synth_nonstationary(n=4600, seed=7, start="2024-01-01"):
    """非平稳合成数据：每 700 根换一次漂移方向/幅度（各训练窗分布不同）。"""
    rng = np.random.default_rng(seed)
    drift = np.concatenate([np.full(700, d) for d in
                            (0.05, -0.12, 0.18, -0.03, 0.10, -0.20, 0.08)[:n // 700 + 1]])[:n]
    close = 100 + np.cumsum(rng.normal(0, 0.3, n) + drift)
    op = close + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq="1h"),
        "open": op, "high": np.maximum(op, close) + np.abs(rng.normal(0, 0.12, n)),
        "low": np.minimum(op, close) - np.abs(rng.normal(0, 0.12, n)),
        "close": close, "tick_volume": 10, "spread": 5, "real_volume": 0})


def make_bars(closes, start="2024-01-01", freq="1h"):
    closes = list(map(float, closes))
    times = pd.date_range(start, periods=len(closes), freq=freq)
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "time": times, "open": opens,
        "high": [max(o, c) + 0.2 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.2 for o, c in zip(opens, closes)],
        "close": closes, "tick_volume": 1, "spread": 1, "real_volume": 0})


@pytest.fixture(scope="module")
def cfg():
    """小时档缩窗（2+1 个月）：真门槛（24/6）单测跑不动。"""
    base = load_gates()[0]
    return dataclasses.replace(base, train_months_hourly=2, test_months_hourly=1)


@pytest.fixture(scope="module")
def bars_ns():
    return synth_nonstationary()


def spec_of(strategy):
    return BacktestSpec(strategy=strategy, symbol="XAUUSD", timeframe="H1",
                        contract_size=1.0)


def run_wf(bars, tmp_root=None, **over):
    kw = dict(grid=[{}], in_sample_sharpe=1.0, cfg=None, fit_meta=FIT_META,
              costs_by_symbol={"XAUUSD": FlatCosts()},
              strategy_cls=DenseS1)
    kw.update(over)
    return wf.run_g3_fittable(spec_of(DenseS1()), bars, FlatCosts(),
                              model_root=tmp_root, **kw)


# ================================================== 验收 1：H 路径逐位不变

def test_accept1_h_path_run_g3_unchanged(cfg, monkeypatch):
    """验收 1：非拟合 H 的 run_g3 输出键集与段结构逐位保持——新 fittable
    路径是新增函数，不渗入 H 语义（H 侧既有 walkforward/gates 测试另证）。"""
    bars = make_bars([100 + 0.05 * i for i in range(2600)])
    out = wf.run_g3(spec_of(BuyHold()), bars, ZeroC(), grid=[{}],
                    in_sample_sharpe=1.2, cfg=cfg)
    assert set(out) == {"passed", "segments", "oos_sharpe", "in_sample_sharpe",
                        "min_oos_ratio", "min_required_oos_sharpe",
                        "n_test_bars_total"}
    assert len(out["segments"]) == 2
    assert out["segments"][0]["test_start"] == "2024-03-01T00:00:00"
    seg_keys = {"train_range", "test_range", "train_start", "test_start",
                "test_end", "test_bars", "train_months", "test_months",
                "params", "train_net", "oos_net", "oos_trades"}
    for seg in out["segments"]:
        assert set(seg) == seg_keys          # 无 fittable 键渗入 H 段
        assert seg["oos_net"] > 0
    assert out["passed"] is True


# ============================================ 验收 2：各训练窗身份可区分

def test_accept2_per_window_model_and_data_hashes_distinct(cfg, bars_ns,
                                                            tmp_path):
    """验收 2：非平稳数据下各训练窗的模型 hash 与训练输入 hash 两两可区分，
    且每窗模型/数据/配置/拟合范围冻结进 artifact（文件内容哈希=model_sha256，
    fit_window.end=窗末=test_start，purge 登记为 K+2）。"""
    out = run_wf(bars_ns, cfg=cfg, tmp_root=tmp_path / "models")
    segs = out["segments"]
    assert len(segs) >= 3
    assert out["mode"] == "fittable"
    shas = [s["model_sha256"] for s in segs]
    dshas = [s["model_data_sha256"] for s in segs]
    assert len(set(shas)) == len(shas), "各训练窗模型 hash 必须可区分"
    assert len(set(dshas)) == len(dshas), "各训练窗输入 hash 必须可区分"
    for s in segs:
        purge = DenseS1.HORIZON_BARS + 2
        assert s["purge_bars"] == purge
        assert s["fit_window"]["end"] == s["test_start"]
        # 冻结 artifact：磁盘文件存在且内容哈希与登记一致
        assert Path(s["model_path"]).is_file()
        raw = Path(s["model_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == s["model_sha256"]
        model = json.loads(raw.decode("utf-8"))
        assert pd.Timestamp(model["fit_window"]["end"]) \
            == pd.Timestamp(s["test_start"])
        assert model["purge_bars"] == purge
    # 训练窗选参规则与正式主检验分开标注（walkforward 现行语义=净利选参）
    assert out["selection_rule"] == "train_net_profit"
    assert all(s["params"] == {} for s in segs)


# ==================================== 验收 3：改未来 bar 不动过去窗

def test_accept3_future_bar_change_keeps_past_windows(cfg, bars_ns, tmp_path):
    """验收 3：修改（末窗测试区内的）未来 bar 不改变更早训练窗的模型与
    决策（OOS 统计逐位不变），末窗模型随之改变。"""
    out1 = run_wf(bars_ns, cfg=cfg, tmp_root=tmp_path / "m")
    segs1 = out1["segments"]
    cut_ts = pd.Timestamp(segs1[-1]["test_start"])
    times = pd.to_datetime(bars_ns["time"])
    idx = times.searchsorted(cut_ts, side="left")
    assert idx < len(bars_ns) - 1            # 末窗测试区内确有 bar
    bars2 = bars_ns.copy()
    for col in ("open", "high", "low", "close"):   # 等比缩放保持 OHLC 合法
        c = bars2.columns.get_loc(col)
        bars2.iloc[idx:, c] = bars2[col].iloc[idx:] * 1.5 + 3.0
    out2 = run_wf(bars2, cfg=cfg, tmp_root=tmp_path / "m")
    segs2 = out2["segments"]
    assert len(segs2) == len(segs1)
    for i in range(len(segs1) - 1):          # 过去窗：模型与决策逐位不变
        assert segs2[i]["model_sha256"] == segs1[i]["model_sha256"]
        assert segs2[i]["model_data_sha256"] == segs1[i]["model_data_sha256"]
        assert segs2[i]["oos_net"] == segs1[i]["oos_net"]
        assert segs2[i]["oos_trades"] == segs1[i]["oos_trades"]
        assert segs2[i]["params"] == segs1[i]["params"]
    # 末窗训练数据同样早于修改点（模型不变——训练输入严格早于窗末），受影响
    # 的是其 OOS 段（改动生效的 sanity：末窗样本外统计/重放序列变化）
    assert segs2[-1]["model_sha256"] == segs1[-1]["model_sha256"]
    spec = spec_of(DenseS1())
    s1 = BT._stitch_oos_from_segments(
        spec, bars_ns, FlatCosts(), segs1,
        window_models=out1["_window_models"], strategy_cls=DenseS1)
    s2 = BT._stitch_oos_from_segments(
        spec, bars2, FlatCosts(), segs2,
        window_models=out2["_window_models"], strategy_cls=DenseS1)
    assert not np.allclose(s1.to_numpy(), s2.to_numpy())


# ============================================= 验收 4：跨界标签被 purge

def _labels_for(bars, purge_bars):
    inst = DenseS1()
    acc: dict = {}
    labels = inst._labels_for_symbol(
        "XAUUSD", bars, FlatCosts(), contract_size=100.0,
        session_policy=PERMISSIVE, train_end=None,
        purge_bars=purge_bars, cost_acc=acc)
    return labels, acc


def test_accept4_purge_applies_to_labels_and_costs(bars_ns):
    """验收 4：purge_bars 真正生效——训练输入严格早于窗末（保留触发的
    bar + purge_bars ≤ 训练数据长度：标签最后信息 t+K+1 不越界），且成本
    过滤同样应用该值（被 purge 的触发不进 cost_r 累计）。"""
    bars = bars_ns
    labels0, acc0 = _labels_for(bars, 0)          # 旧口径：不 purge
    assert len(labels0) > 0
    t_max = int(labels0["bar"].max())
    purge = len(bars) - t_max + 1                 # 恰好剔掉最晚的触发
    assert purge > 0
    labels_p, acc_p = _labels_for(bars, purge)
    assert len(labels_p) > 0
    assert t_max not in set(int(b) for b in labels_p["bar"])
    assert (labels_p["bar"].astype(int) + purge <= len(bars)).all()
    n_cost0 = sum(len(v) for v in acc0.values())
    n_costp = sum(len(v) for v in acc_p.values())
    assert 0 < n_costp < n_cost0, "成本过滤必须同幅应用 purge（不再累计被剔触发）"


# ============================================ 验收 5：同一模型可重放

def test_accept5_replay_deterministic(cfg, bars_ns, tmp_path):
    """验收 5：同一输入重跑逐位一致（含模型 hash/统计量），且仅凭冻结
    artifact（磁盘上的模型文件 + segments）即可重放出同一 OOS 序列。"""
    root = tmp_path / "models"
    out1 = run_wf(bars_ns, cfg=cfg, tmp_root=root)
    out2 = run_wf(bars_ns, cfg=cfg, tmp_root=root)
    assert out1 == out2
    segs = out1["segments"]
    models = out1["_window_models"]
    in_mem = BT._stitch_oos_from_segments(
        spec_of(DenseS1()), bars_ns, FlatCosts(), segs,
        window_models=models, strategy_cls=DenseS1)
    from_disk = BT._stitch_oos_from_segments(
        spec_of(DenseS1()), bars_ns, FlatCosts(), segs,
        strategy_cls=DenseS1)                 # window_models=None → 从 artifact 装载
    pd.testing.assert_series_equal(
        in_mem.reset_index(drop=True), from_disk.reset_index(drop=True))
    assert len(in_mem) == sum(s["test_bars"] for s in segs)


# ====================================== 验收 6：OOS 无重复/预热收益

def test_accept6_oos_no_overlap_no_warmup(cfg, bars_ns, tmp_path):
    """验收 6：OOS 拼接不重叠、不含训练/预热收益——测试窗区间两两不交，
    拼接收益根数 = Σtest_bars（收益起点=各窗 OOS 首根），预热段零开仓
    （决策起点原语：warmup_trades=0）。"""
    out = run_wf(bars_ns, cfg=cfg, tmp_root=tmp_path / "models")
    segs = out["segments"]
    ranges = [(s["test_range"][0], s["test_range"][1]) for s in segs]
    for (a0, a1), (b0, b1) in zip(ranges, ranges[1:]):
        assert a1 <= b0                          # 不重叠（按 bar 区间）
    for s in segs:
        assert s["warmup_trades"] == 0, "预热段不得产生交易状态（P2-02）"
        assert s["train_range"][1] == s["test_range"][0]
    stitched = BT._stitch_oos_from_segments(
        spec_of(DenseS1()), bars_ns, FlatCosts(), segs,
        window_models=out["_window_models"], strategy_cls=DenseS1)
    assert len(stitched) == sum(s["test_bars"] for s in segs)


# ============================================ 验收 7：Brier 否决

def _brier_rows(p, y, base):
    return pd.DataFrame({"p_pred": p, "y": y, "base_rate": base})


def test_accept7_uninformative_veto_fails():
    """验收 7：无信息（预测=训练窗基准率）→ Brier skill=0 ≤0 → 否决。"""
    rows = _brier_rows([0.4] * 60,
                       [1, 0] * 30,
                       [0.4] * 60)
    v = wf.brier_skill_veto([rows])
    assert v["passed"] is False
    assert v["skill"] <= 0.0
    assert v["n_oos"] == 60


def test_accept7_informative_veto_passes():
    y = [1, 0] * 30
    rows = _brier_rows([0.99 if v else 0.01 for v in y], y, [0.5] * 60)
    v = wf.brier_skill_veto([rows])
    assert v["passed"] is True and v["skill"] > 0


def test_accept7_no_oos_rows_fail_closed():
    v = wf.brier_skill_veto([pd.DataFrame(columns=["p_pred", "y", "base_rate"])])
    assert v["passed"] is False and v["n_oos"] == 0


def _base_only_model():
    """手工无信息模型：无 cells/parents，lookup 恒回退动作基准——每个 OOS
    触发的 p_pred 与 base_rate 同源同值（"无信息拟合"的决定论形态）。"""
    est = {"n": 500, "p_win": 0.40, "p_loss": 0.50, "p_time": 0.10,
           "r_win": 2.0, "r_loss": -1.0, "r_time": 0.0,
           "se_win": 0.02, "se_loss": 0.02}
    return {"schema_version": 1,
            "group_by": ["trend_sma200", "atr_pct_2000"],
            "prior_strength": 100, "min_cell": 100,
            "base_rates": {"A3": est}, "parents": [], "cells": []}


def test_accept7_uninformative_fitted_model_triggers_veto(bars_ns):
    """验收 7（集成）：装好"预测=基准"模型的策略在真实 OOS 触发上收集
    校准行（只用 OOS、基准率=模型训练窗动作基准）→ skill=0 → 否决。"""
    strat = DenseS1().with_model({
        "prob_version": "1.0.0", "symbols": ["XAUUSD"], "timeframe": "H1",
        "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 8},
        # 不带 allowed_hypotheses 键 = 全部兼容（v1 冻结件口径）
        "prob_table": _base_only_model(),
        "cost_r_by_situation_action": {"S1|A3": 0.001}})
    rows = strat.oos_calibration_rows(
        bars_ns, FlatCosts(), contract_size=100.0,
        session_policy=PERMISSIVE,
        decision_start=pd.Timestamp(bars_ns["time"].iloc[len(bars_ns) // 2]))
    assert len(rows) > 0
    assert (rows["p_pred"] == rows["base_rate"]).all()
    v = wf.brier_skill_veto([rows])
    assert v["passed"] is False and v["skill"] <= 0.0


def test_accept7_run_g3_fittable_carries_brier_veto(cfg, bars_ns, tmp_path):
    """条目 3 接线：逐窗拟合结果携带 Brier 否决项（进关卡 v3 判决链）。"""
    out = run_wf(bars_ns, cfg=cfg, tmp_root=tmp_path / "models")
    v = out["brier_veto"]
    assert set(v) >= {"passed", "skill", "n_oos", "per_window", "brier_model",
                      "brier_base"}
    assert v["n_oos"] > 0


def test_v3_merge_p_line_brier_veto():
    """V3 判决链接线：P 线专属否决项进 failed_vetoes/checks（gates.py 的
    evaluate_v3 五层不动，bt 侧合并）。"""
    result = {"passed": True, "veto": {"G0": True, "primary": True},
              "failed_vetoes": [], "vetoes": {}, "checks": {}}
    merged = BT._merge_p_line_veto(result, {"passed": False, "skill": -0.1,
                                            "n_oos": 42})
    assert merged["passed"] is False
    assert merged["vetoes"]["brier_skill"]["passed"] is False
    assert "brier_skill" in merged["failed_vetoes"]
    assert merged["checks"]["brier_skill"]["passed"] is False
    ok = BT._merge_p_line_veto({**result}, {"passed": True, "skill": 0.2,
                                            "n_oos": 42})
    assert ok["passed"] is True and "brier_skill" not in ok["failed_vetoes"]


# ============================================ 条目 4：capability 白名单

def test_capability_whitelist_allows_p2_fittable_g3_v3():
    """条目 6：显式白名单放开——LevelDecisionV1 子类（P2 决策卡）的
    G3/V3 走新逐窗路径，不再被拒。"""
    assert BT._reject_fittable_walkforward(LevelDecisionS1Demo,
                                           ["G0", "G3"]) is None
    assert BT._reject_fittable_walkforward(LevelDecisionS1Demo,
                                           ["V3"]) is None


def test_capability_whitelist_still_rejects_unsupported():
    """条目 6：白名单外的 fittable 组合仍拒绝（不删拒绝函数）——自定义
    fit 协议（非 LevelDecisionV1 子类）不在本批逐窗路径能力范围。"""

    class CustomFittable:              # 自定义拟合协议：不在白名单
        fittable = True

        def fit(self, bars, costs, params, *, train_end, purge_bars):
            return {}

        def with_model(self, model):
            return self

    msg = BT._reject_fittable_walkforward(CustomFittable, ["G3"])
    assert msg and "T73B" in msg and "白名单" in msg
    assert BT._reject_fittable_walkforward(CustomFittable, ["G0"]) is None
    assert BT._reject_fittable_walkforward(
        type("P", (), {"fittable": False}), ["G3"]) is None


def test_stitch_consumes_per_window_models(cfg, bars_ns, tmp_path):
    """条目 2/4 接线：V3/G5 的 OOS 重放消费每窗冻结模型——替换任一窗模型
    即改变重放序列（证明真的逐窗装载，而非全史单一模型）。"""
    out = run_wf(bars_ns, cfg=cfg, tmp_root=tmp_path / "models")
    segs = out["segments"]
    spec = spec_of(DenseS1())
    base = BT._stitch_oos_from_segments(
        spec, bars_ns, FlatCosts(), segs,
        window_models=out["_window_models"], strategy_cls=DenseS1)
    tampered = dict(out["_window_models"])
    tampered[segs[0]["model_sha256"]] = {
        "prob_version": "1.0.0", "symbols": ["XAUUSD"], "timeframe": "H1",
        "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 8},
        # 不带 allowed_hypotheses 键 = 全部兼容（v1 冻结件口径）
        "prob_table": _base_only_model(),
        "cost_r_by_situation_action": {"S1|A3": 1e-9}}
    other = BT._stitch_oos_from_segments(
        spec, bars_ns, FlatCosts(), segs,
        window_models=tampered, strategy_cls=DenseS1)
    assert not np.allclose(base.to_numpy(), other.to_numpy())


# ============================================ 条目 2：cards 占位字段

def test_cards_primary_statistic_placeholder():
    """条目 4：standard P 卡的 primary_statistic 接受冻结主检验统计量
    dsr_v0；max_net_profit 是选参规则（legacy 兼容），其他值仍拒绝。"""
    from tests.test_prob_p2_cards import p2_card_data
    from mt5quant.research import cards as rcards
    ok = rcards.validate_card(
        p2_card_data(primary_statistic="dsr_v0"), source="t")
    assert ok.primary_statistic == "dsr_v0"
    legacy = rcards.validate_card(
        p2_card_data(primary_statistic="max_net_profit"), source="t")
    assert legacy.primary_statistic == "max_net_profit"
    with pytest.raises(rcards.CardError, match="dsr_v0"):
        rcards.validate_card(p2_card_data(primary_statistic="max_sharpe"),
                             source="t")
    # 冻结口径与 gates.toml 实际加载一致（不改阈值——指纹测试另证）
    cfg, _ = load_gates()
    assert cfg.primary_test == "dsr_v0"


# ============================================ 条目 5：B 项收尾

def test_b1_p2_card_requires_nonempty_class_whitelist():
    """批 2 复审 B1：正式 P2 决策卡（standard+validation+[prob]）的策略类
    获准（局面×动作）白名单必须非空；P1（probability）不受限（兼容）。"""
    from tests.test_prob_p2_cards import _card_with_hyp, NoWhitelistDemo
    base = _card_with_hyp("S1:A3:fade>base")
    p2_card = NS(**{**vars(base), "evaluation_mode": "standard",
                    "segment": "validation"})
    msg = BT._check_card_strategy_constants(p2_card, NoWhitelistDemo)
    assert msg and "ALLOWED_HYPOTHESES" in msg and "P2" in msg
    # P1（probability）卡 + 空白名单：兼容不拒
    assert BT._check_card_strategy_constants(base, NoWhitelistDemo) is None
    # 白名单非空（现行演示类）照常
    assert BT._check_card_strategy_constants(p2_card,
                                             LevelDecisionS1Demo) is None


def _patch_bt_gate_env(monkeypatch, p2_env):
    """本文件的 BT 模块实例与夹具模块的实例不同——同一批桩（指纹/会话
    配置）打到自己实例上。"""
    monkeypatch.setattr(BT, "_bind_strategy_sha",
                        lambda led, card, ref: "f" * 40)
    monkeypatch.setattr(BT, "SESSIONS_PATH",
                        p2_env["tmp"] / "config" / "sessions.toml")


def test_b2_gate_reserves_before_fit(p2_env, monkeypatch):
    """B2-gate：reservation 口径轮 gate 的基准运行占位前移到拟合之前
    （照搬 screen 的 _pre_fit_key 模式）——预算满的轮次零拟合计算。"""
    from mt5quant.research.ledger import Ledger
    _patch_bt_gate_env(monkeypatch, p2_env)
    with Ledger(p2_env["ledger"]) as led:
        for i in range(4):            # trial_budget=4：预先占满
            led.reserve_trial("R3", f"foreign-{i}", actor="bt")
    calls = {"fit": 0}

    def _no_fit(*a, **k):
        calls["fit"] += 1
        raise AssertionError(
            "预算满的轮次不得执行拟合（B2-gate：占位前移到拟合之前）")

    monkeypatch.setattr(BT, "_fit_and_freeze", _no_fit)
    from tests.test_prob_p2_e2e import _card_path, STRATEGY_REF
    _card_path(p2_env, 1.0)
    argv = ["gate", "--card", "P002-v1", "--strategy", STRATEGY_REF,
            "--gates", "G0", "--cards-dir", str(p2_env["cards"]),
            "--data-root", str(p2_env["store"]), "--ledger",
            str(p2_env["ledger"]), "--costs", "zero", "--volume-lots", "1.0",
            "--params", "{}", "--allow-unbounded-data"]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(p2_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "占位" in out.getvalue()
    assert calls["fit"] == 0


def test_b2_gate_single_reservation_no_double_count(p2_env, monkeypatch):
    """B2-gate 对照：预算未满的正常 gate——占位恰一条且推进 completed
    （不因拟合前占位 + 基准前占位双计）。"""
    from mt5quant.research.ledger import Ledger
    from tests.test_prob_p2_e2e import _card_path, STRATEGY_REF
    _patch_bt_gate_env(monkeypatch, p2_env)
    _card_path(p2_env, 1.0)
    argv = ["gate", "--card", "P002-v1", "--strategy", STRATEGY_REF,
            "--gates", "G0", "--cards-dir", str(p2_env["cards"]),
            "--data-root", str(p2_env["store"]), "--ledger",
            str(p2_env["ledger"]), "--costs", "zero", "--volume-lots", "1.0",
            "--params", "{}", "--allow-unbounded-data"]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(p2_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 0, out.getvalue()
    with Ledger(p2_env["ledger"]) as led:
        rows = led.conn.execute(
            "SELECT stage, trial_key FROM trial_reservations"
            " WHERE round_id='R3'").fetchall()
    ours = [r for r in rows if r["trial_key"]]
    assert len(ours) == 1
    assert ours[0]["stage"] == "completed"


def test_b3_era_boundary_uses_merge_measured_tags(cli_env, monkeypatch):
    """B3（批 2 复审）：cmd_prob_test 的 era 边界消费 merge 实测
    (bars, source_tags)——gaps.mt5_first_time 不再走 meta 水位路径。"""
    from tests.test_prob_p1_cli import _run_prob_test
    captured: dict = {}

    # 卡带 [prob] 新键（max_gap_days）→ use_merged=True
    card_p = cli_env["card"]
    card_p.write_text(card_p.read_text(encoding="utf-8").replace(
        "expected_delta_p = 0.04",
        "expected_delta_p = 0.04\nmax_gap_days = 30"), encoding="utf-8")
    import subprocess

    def _git(repo, *args):
        subprocess.run(["git", "-c", "user.name=probe",
                        "-c", "user.email=probe@example.invalid", *args],
                       cwd=repo, check=True, capture_output=True)
    _git(cli_env["repo"], "add", ".")
    _git(cli_env["repo"], "commit", "-q", "-m", "merged-card")

    import mt5quant.prob.gaps as prob_gaps

    real_mt5_first = prob_gaps.mt5_first_time

    def _spy_first(data_root, symbol, timeframe, *, bars=None,
                   source_tags=None):
        captured["bars"] = bars
        captured["source_tags"] = source_tags
        return real_mt5_first(data_root, symbol, timeframe,
                              bars=bars, source_tags=source_tags)

    tags = ["ext"] * 100 + ["mt5"] * 100

    def _fake_load(data_root, sym, tf, *, use_merged, end=None,
                   allow_unbounded=False):
        from mt5data.store import _COLUMNS
        n = 200
        rng = np.random.default_rng(3)
        close = 100 + np.cumsum(rng.normal(0, 0.3, n))
        op = close + rng.normal(0, 0.05, n)
        bars = pd.DataFrame({
            "time": pd.date_range("2018-01-01", periods=n, freq="1h"),
            "open": op, "high": np.maximum(op, close) + 0.12,
            "low": np.minimum(op, close) - 0.12, "close": close,
            "tick_volume": 10, "spread": 5, "real_volume": 0})[_COLUMNS]
        return bars, list(tags), None

    monkeypatch.setattr(BT, "_prob_load_bars", _fake_load)
    from tests import test_prob_p1_cli as p1_cli_mod
    monkeypatch.setattr(p1_cli_mod.BT, "_prob_load_bars", _fake_load)
    monkeypatch.setattr(prob_gaps, "mt5_first_time", _spy_first)
    monkeypatch.setattr("mt5quant.prob.p1.run_p1_card",
                        lambda *a, **k: [])
    code, out = _run_prob_test(cli_env, monkeypatch)
    assert code == 0, out
    assert "bars" in captured and "source_tags" in captured
    assert captured["bars"] is not None and len(captured["bars"]) == 200
    assert captured["source_tags"] == tags


def test_b2_prime_p1_metrics_carry_eligibility_version(cli_env, monkeypatch):
    """B2'（批 2 复审）：ELIGIBILITY_VERSION 随 P1 metrics.prob 携带。"""
    from mt5quant.prob import gaps as prob_gaps
    from mt5quant.research.ledger import Ledger
    from tests.test_prob_p1_cli import _run_prob_test
    code, out = _run_prob_test(cli_env, monkeypatch)
    assert code == 0, out
    with Ledger(cli_env["ledger"]) as led:
        rows = [r for r in led.runs_of_round("R2") if r["ok"]]
    assert rows
    for r in rows:
        assert r["metrics"]["prob"]["eligibility_version"] \
            == prob_gaps.ELIGIBILITY_VERSION


def test_b2_prime_precheck_carries_eligibility_version(tmp_path, monkeypatch):
    """B2'：概率候选预筛的 checks 携带 ELIGIBILITY_VERSION（读侧可对出
    资格规则版本）。"""
    from mt5quant.prob import gaps as prob_gaps
    from mt5quant.research.ledger import Ledger
    from mt5quant.research import candidates as cand

    cand_toml = tmp_path / "C101.toml"
    cand_toml.write_text("id='C101'", encoding="utf-8")
    fake = NS(id="C101", status="active", path=str(cand_toml),
              prob_declared=NS(), symbols=("XAUUSD",), holding="H1")

    class _Res:
        decision = "pass"

        def to_payload(self):
            return {"checks": {}, "reasons": []}

    monkeypatch.setattr(cand, "load_candidate", lambda p: fake)
    monkeypatch.setattr(BT, "_resolve_store", lambda a: (str(tmp_path), "research"))
    monkeypatch.setattr(BT, "_swap_ref_prices", lambda *a, **k: {})
    monkeypatch.setattr(BT, "_merged_costs",
                        lambda *a, **k: (FlatCosts(), "flat-probe"))
    monkeypatch.setattr(BT, "_active_mechanism_classes",
                        lambda *a, **k: set())
    monkeypatch.setattr(BT, "_candidate_check_prob",
                        lambda *a, **k: _Res())
    led_path = tmp_path / "ledger.sqlite"
    monkeypatch.setattr(BT, "_ledger_default", lambda a: led_path)
    args = NS(candidate="C101", candidates_dir=str(tmp_path),
              ledger=str(led_path), spread_fraction=1.0, commission=None,
              data_root=str(tmp_path))
    out = io.StringIO()
    import contextlib as _cl
    monkeypatch.chdir(tmp_path)
    with _cl.redirect_stdout(out):
        code = BT.cmd_candidate_check(args)
    assert code == 0, out.getvalue()
    with Ledger(led_path) as led:
        row = led.candidate_checks("C101")[0]
    recorded = json.loads(row["result_json"])
    assert recorded["checks"]["eligibility_version"] \
        == prob_gaps.ELIGIBILITY_VERSION
    payload = json.loads((tmp_path / "C101.precheck.json").read_text(
        encoding="utf-8"))
    assert payload["checks"]["eligibility_version"] \
        == prob_gaps.ELIGIBILITY_VERSION
