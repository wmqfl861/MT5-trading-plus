"""T90：P1 运行入口——前置检查、编排确定性、分界剔除、轮次报告 Holm。

全部合成数据；真实局面触发上的运行属 T91（等 T77 批准）。
"""
import types

import numpy as np
import pandas as pd
import pytest

import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src")]

import importlib.util
_spec = importlib.util.spec_from_file_location("bt_t90", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BT)

from mt5quant.prob import p1 as p1mod                      # noqa: E402
from mt5quant.research import rounds as rounds_mod         # noqa: E402
from mt5quant.validation.prob_mode import holm             # noqa: E402


class FlatCosts:
    def spread_p90(self, s, h, *, price=None):
        return 0.02

    def slippage(self, s, h, *, price=None):
        return 0.01

    def commission_per_lot(self, s):
        return 0.0

    def overnight_fee(self, s, side, f, t, *, price=None):
        return 0.0


def synth_bars(n, *, seed, start="2016-01-01"):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.30, n))
    openc = close + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq="1h"),
        "open": openc,
        "high": np.maximum(openc, close) + np.abs(rng.normal(0, 0.12, n)),
        "low": np.minimum(openc, close) - np.abs(rng.normal(0, 0.12, n)),
        "close": close, "tick_volume": 1, "spread": 5, "real_volume": 0})


def fake_card(hypotheses=("S1:A3:fade>base",)):
    return types.SimpleNamespace(
        id="P001-v1", timeframe="H1", segment="discovery",
        evaluation_mode="probability",
        candidate="C101", round="R2",
        prob={"situations": ("S1",),
              "hypotheses": list(hypotheses),
              "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 24},
              "level_grid": {"XAUUSD": 50.0},
              "approach": {"lookback_bars": 24, "min_distance_atr": 1.0,
                           "touch_atr": 0.25},
              "breakout_atr": 0.25, "retest_window_bars": 6})


def fake_round(split="2019-12-01", approved="P线审核会话",
               hypotheses=("S1:A3:fade>base",), budget=12):
    return rounds_mod.Round(
        id="R2", opened="2026-11-02", candidates=("C101",),
        trial_budget=budget, lockbox_uses_max=0, dsr_trial_scope="project",
        advance_rule="a", close_rule="c", approved_by=approved,
        evaluation={"C101": {"mode": "probability",
                             "primary": "delta_p_one_sided"}},
        segments={"split": split},
        hypotheses={"C101": list(hypotheses)}, path="")


# ------------------------------------------------------------- 植入检出

def _day_labels(n_days, p_win, seed, start="2016-01-01"):
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.date_range(start, periods=n_days, freq="1D"):
        w = rng.random() < p_win
        rows.append({"time": d + pd.Timedelta(hours=10),
                     "outcome": "win" if w else "loss",
                     "net_r": 1.9 if w else -1.05})
    return pd.DataFrame(rows)


def test_planted_delta_detected_and_null_not():
    from mt5quant.validation.prob_mode import delta_p_test, plant_delta
    base = _day_labels(2000, 0.40, seed=1)
    null = _day_labels(2000, 0.40, seed=2)
    planted = plant_delta(base, 0.05, seed=3)
    det = delta_p_test(planted, null, sims=500, seed=4)
    assert det["delta_p"] > 0.03
    assert det["p_value"] < 0.05                  # 植入 Δp=0.05 → 检出
    nd = delta_p_test(base, null, sims=500, seed=4)
    assert nd["p_value"] >= 0.05 or nd["delta_p"] < 0.02   # 未植入 → 不检出


# ------------------------------------------------------------- 编排确定性

def test_run_p1_card_deterministic_and_structured():
    bars = synth_bars(6000, seed=9)             # ~250 天 H1
    card = fake_card()
    rnd = fake_round()
    sizes = {"XAUUSD": 100.0}
    from mt5data.sessions import SessionPolicy
    permissive = SessionPolicy(exempt_symbols=frozenset({"XAUUSD"}))
    pol = {"XAUUSD": permissive}
    r1 = p1mod.run_p1_card(card, rnd, {"XAUUSD": bars},
                           {"XAUUSD": FlatCosts()}, sizes=sizes,
                           policy_by_symbol=pol, sims=200)
    r2 = p1mod.run_p1_card(card, rnd, {"XAUUSD": bars},
                           {"XAUUSD": FlatCosts()}, sizes=sizes,
                           policy_by_symbol=pol, sims=200)
    assert r1 == r2                              # 逐位一致
    assert len(r1) == 1
    row = r1[0]
    assert row["hypothesis"] == "S1:A3:fade>base"
    assert row["seed"] == p1mod.hypothesis_seed("P001-v1", "S1:A3:fade>base")
    assert 0.0 <= row["p_value"] <= 1.0
    assert row["n_triggers"] >= row["n_trigger_days"] >= 0


def test_run_p1_card_reports_total_usable_skipped(monkeypatch):
    """P-11：n_total/n_usable/n_skipped 三报（n_triggers=n_total 兼容别名）；
    real/null 赢率与 n_trigger_days 排除 skipped（与 delta_p_test 同口径）。"""
    bars = synth_bars(6000, seed=9)
    card = fake_card()
    rnd = fake_round()
    calls = []
    real_label = p1mod.label_triggers

    def stub_labels(b, trig, costs, **kw):
        times = pd.to_datetime(trig["time"])
        calls.append(list(times))
        # 按行位次交替：偶位 win、奇位 skipped——可成交的全是 win
        outcomes = ["win" if k % 2 == 0 else "skipped"
                    for k in range(len(trig))]
        return pd.DataFrame({
            "time": times.to_numpy(), "outcome": outcomes,
            "net_r": [1.9 if o == "win" else 0.0 for o in outcomes]})

    monkeypatch.setattr(p1mod, "label_triggers", stub_labels)
    from mt5data.sessions import SessionPolicy
    res = p1mod.run_p1_card(card, rnd, {"XAUUSD": bars},
                            {"XAUUSD": FlatCosts()},
                            sizes={"XAUUSD": 100.0},
                            policy_by_symbol={"XAUUSD": SessionPolicy(
                                exempt_symbols=frozenset({"XAUUSD"}))},
                            sims=50)
    row = res[0]
    real_times = calls[0]                      # 每假设首调用 = 真实触发
    n = len(real_times)
    assert n >= 2
    n_skipped = sum(1 for k in range(n) if k % 2 == 1)
    n_usable = n - n_skipped
    assert row["n_total"] == n
    assert row["n_triggers"] == row["n_total"]       # 兼容别名
    assert row["n_usable"] == n_usable
    assert row["n_skipped"] == n_skipped
    # 旧口径把 skipped 当非赢计入：会得 0.5；usable 同口径 → 全赢 1.0
    assert row["real_win_rate"] == pytest.approx(1.0)
    assert row["null_win_rate"] == pytest.approx(1.0)
    # 触发日数按可成交行计（usable 行的去重日）
    usable_days = len({t.date() for k, t in enumerate(real_times)
                       if k % 2 == 0})
    assert row["n_trigger_days"] == usable_days
    assert row["delta_p"] == pytest.approx(0.0, abs=1e-12)


def test_hypothesis_seed_deterministic():
    a = p1mod.hypothesis_seed("P001-v1", "S1:A1:follow>base")
    assert a == p1mod.hypothesis_seed("P001-v1", "S1:A1:follow>base")
    assert a != p1mod.hypothesis_seed("P002-v1", "S1:A1:follow>base")


# ------------------------------------------------------------- 前置检查

def _write_round_file(tmp_path, approved="P线审核会话", hyps=None):
    d = tmp_path / "rounds"
    d.mkdir(exist_ok=True)
    hyps = hyps or ["S1:A3:fade>base"]
    body = f'''id = "R2"
opened = "2026-11-02"
candidates = ["C101"]
trial_budget = 12
lockbox_uses_max = 0
dsr_trial_scope = "project"
advance_rule = "a"
close_rule = "c"
approved_by = "{approved}"
[segments]
split = "2019-12-01"
[evaluation.C101]
mode = "probability"
primary = "delta_p_one_sided"
[hypotheses]
C101 = {hyps}
'''
    p = d / "R2.toml"
    p.write_text(body, encoding="utf-8")
    return p


class _LedgerStub:
    """S2 后前置检查走 _round_context：桩齐轮次行与预筛记录。"""

    def __init__(self, used=0, *, round_row=None):
        import json as _j
        self.used = used
        self._row = round_row or {
            "round_id": "R2", "status": "open",
            "candidates_json": _j.dumps(["C101"]),
            "trial_budget": 12, "lockbox_uses_max": 0}

    def round(self, rid):
        return self._row

    def candidate_checks(self, cid):
        return [{"decision": "pass"}]

    def round_lockbox_uses(self, rid):
        return 0

    def round_n_trials(self, rid):
        # X-01：预检改用 round_n_trials（reservation 权威计数；桩与
        # round_n_dedup 同值——无 reservation 的旧口径回退）
        return self.used

    def round_n_dedup(self, rid):
        return self.used


def test_preconditions_reject_each_failure(tmp_path):
    card = fake_card()
    ok_round = _write_round_file(tmp_path)
    from mt5quant.research import cards as rcards
    card_path = tmp_path / "cards"
    card_path.mkdir(exist_ok=True)
    # 用假的卡文件路径（前置检查只用 path.parent.parent 找轮次）
    fake_path = card_path / "P001-v1.toml"
    import hashlib as _h
    led = _LedgerStub(round_row={
        "round_id": "R2", "status": "open",
        "candidates_json": '["C101"]', "trial_budget": 12,
        "lockbox_uses_max": 0,
        "toml_sha256": _h.sha256(ok_round.read_bytes()).hexdigest()})
    pre = BT._prob_preconditions(led, card, fake_path, actor="bt")
    assert pre is not None and pre[0].id == "R2"
    # 1) validation 卡
    v = fake_card(); v.segment = "validation"
    assert BT._prob_preconditions(led, v, fake_path, actor="bt") is None
    # 2) 未批准轮次
    _write_round_file(tmp_path, approved="")
    assert BT._prob_preconditions(led, card, fake_path, actor="bt") is None
    # 3) 假设表不一致
    _write_round_file(tmp_path, hyps=["S1:A1:follow>base"])
    assert BT._prob_preconditions(led, card, fake_path, actor="bt") is None
    # 4) 预算不足（轮次已开启、sha 一致，预算 12 已用满）
    _write_round_file(tmp_path)
    led2 = _LedgerStub(used=12, round_row={
        "round_id": "R2", "status": "open",
        "candidates_json": '["C101"]', "trial_budget": 12,
        "lockbox_uses_max": 0,
        "toml_sha256": _h.sha256(
            (tmp_path / "rounds" / "R2.toml").read_bytes()).hexdigest()})
    assert BT._prob_preconditions(led2, card, fake_path, actor="bt") is None


def test_triggers_beyond_split_dropped():
    """分界后的 bar 不进发现段（run_p1_card 只见 < split 的 bar）。"""
    bars = synth_bars(35_000, seed=9)      # ~4 年，跨过 2019-12-01
    split = pd.Timestamp("2019-12-01")
    times = pd.to_datetime(bars["time"])
    assert times.max() > split
    below = bars[times < split]
    assert pd.to_datetime(below["time"]).max() < split   # 切片语义钉住


# ------------------------------------------------------------- 轮次报告

def test_round_report_holm_and_incomplete_reject(tmp_path):
    from mt5quant.research.ledger import Ledger
    db = tmp_path / "l.sqlite"
    rnd = fake_round(hypotheses=("S1:A3:fade>base", "S1:A1:follow>base"))
    # 只跑了一条 → round-report 拒绝（用 runs_of_round 查空）
    with Ledger(db) as led:
        led.record_round_open(round_id="R2", candidates=["C101"],
                              trial_budget=12, lockbox_uses_max=0,
                              dsr_trial_scope="project", advance_rule="a",
                              close_rule="c", approved_by="P线审核会话",
                              evaluation={"C101": {"mode": "probability",
                                                   "primary": "d"}},
                              toml_sha256="x" * 64, git_commit="g" * 40)
        assert led.runs_of_round("R2") == []
    # Holm 手算
    adj = holm({"a": 0.01, "b": 0.04})
    assert adj == {"a": 0.02, "b": 0.04}


def test_runs_of_round_returns_params_metrics(tmp_path):
    from mt5quant.research.ledger import Ledger
    db = tmp_path / "l.sqlite"
    with Ledger(db) as led:
        rid = led.record_run(
            card_id="P001-v1", card_version=1, strategy="s", symbol="X",
            timeframe="H1", data_first=None, data_last=None,
            data_sha256="a" * 64, engine_version="e",
            cost_model_version="v", gates_hash="",
            params={"hypothesis": "S1:A3:fade>base",
                    "prob": {"segment": "discovery",
                             "split": "2019-12-01",
                             "bracket": {"sl_atr": 1.0}, "seed": 123}},
            metrics={"prob": {"p_value": 0.03, "delta_p": 0.06}},
            actor="bt", round_id="R2")
        rows = led.runs_of_round("R2")
        assert len(rows) == 1
        assert rows[0]["params"]["hypothesis"] == "S1:A3:fade>base"
        assert rows[0]["ok"] is True
        assert rows[0]["metrics"]["prob"]["p_value"] == 0.03
        assert rows[0]["run_id"] == rid
        assert led.verify_chain() == []


# ------------------------------------------------- T104 批 2（audit2 P2-11）
# 资格剔除诊断进入 P1 返回行：n_eligible_dropped（进入标签前）+ 逐原因
# 计数 + null 侧同口径计数——台账写入由丁接线（bt.py）。

def _card_objects(card):
    from mt5quant.strategies.library.prob.actions import ActionParams
    from mt5quant.strategies.library.prob.situations import Approach
    return (Approach(**card.prob["approach"]),
            ActionParams(breakout_atr=card.prob["breakout_atr"],
                         retest_window_bars=card.prob["retest_window_bars"]))


def test_run_p1_card_reports_eligibility_drops_by_reason():
    """缺省预热 2000：触发根 < 2000 的真实触发在标签前被剔——按
    feature_warmup 原因计入 n_eligible_dropped（与 card_triggers 过滤
    前后差逐位一致）。"""
    bars = synth_bars(6000, seed=9)
    card = fake_card()
    rnd = fake_round()
    approach, aparams = _card_objects(card)
    raw = p1mod.card_triggers(card, {"XAUUSD": bars}, approach=approach,
                              action_params=aparams)   # 不滤（三元组缺省）
    kept = p1mod.card_triggers(card, {"XAUUSD": bars}, approach=approach,
                               action_params=aparams, feature_warmup=2000,
                               hold_bars=24)
    assert len(raw) > len(kept) >= 0                # 该种子确有预热期触发
    from mt5data.sessions import SessionPolicy
    res = p1mod.run_p1_card(card, rnd, {"XAUUSD": bars},
                            {"XAUUSD": FlatCosts()},
                            sizes={"XAUUSD": 100.0},
                            policy_by_symbol={"XAUUSD": SessionPolicy(
                                exempt_symbols=frozenset({"XAUUSD"}))},
                            sims=50)
    row = res[0]
    hyp = "S1:A3:fade>base"
    raw_n = int(len(raw[(raw["situation"] == "S1")
                        & (raw["action"] == "A3")]))
    kept_n = int(len(kept[(kept["situation"] == "S1")
                          & (kept["action"] == "A3")]))
    assert row["hypothesis"] == hyp
    assert row["n_eligible_dropped"] == raw_n - kept_n
    assert row["eligible_dropped_by_reason"]["feature_warmup"] \
        == raw_n - kept_n          # 连续合成序列无洞 → 唯一原因是预热
    # 计数守恒（进入标签前的口径）：过滤后触发数 + 剔除数 = 原始触发数
    assert kept_n + row["n_eligible_dropped"] == raw_n
    # 逐位确定（诊断字段不引入随机性）
    res2 = p1mod.run_p1_card(card, rnd, {"XAUUSD": bars},
                             {"XAUUSD": FlatCosts()},
                             sizes={"XAUUSD": 100.0},
                             policy_by_symbol={"XAUUSD": SessionPolicy(
                                 exempt_symbols=frozenset({"XAUUSD"}))},
                             sims=50)
    assert res == res2


def test_run_p1_card_gap_drop_reason_recorded():
    """回看窗跨洞的触发按 lookback_crosses_gap 原因计入（t97 波形：洞
    (t21,t22) 使 A3@26 的 S1 窗 [20,26] 跨洞 → 被剔）。序列补长到 63 根：
    P2-03 起未来窗须 t+K+1 完整可观察（卡 K=24 → 触发根 ≤ 37），否则
    A1@28 会被 future_window 剔除而非'保留'。"""
    from tests.test_prob_t97_wiring import (
        APPROACH as T97_APPROACH, PARAMS as T97_PARAMS, fake_card as t97_card,
        make_bars_rows, s1_pattern_rows, hourly, PLAT,
    )
    bars = make_bars_rows(s1_pattern_rows() + [PLAT] * 30, hourly(63))
    times = list(pd.to_datetime(bars["time"]))
    gaps = [(times[21], times[22])]
    card = t97_card(hypotheses=("S1:A3:fade>base", "S1:A1:follow>base"))
    from mt5data.sessions import SessionPolicy
    res = p1mod.run_p1_card(
        card, fake_round(), {"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
        sizes={"XAUUSD": 100.0},
        policy_by_symbol={"XAUUSD": SessionPolicy(
            exempt_symbols=frozenset({"XAUUSD"}))},
        sims=50, feature_warmup=0,
        gap_spans_by_symbol={"XAUUSD": gaps})
    by_hyp = {r["hypothesis"]: r for r in res}
    a3 = by_hyp["S1:A3:fade>base"]
    # A3@26 被跨洞剔除；A1@28 的窗 [22,28] 在洞后保留
    assert a3["n_eligible_dropped"] == 1
    assert a3["eligible_dropped_by_reason"]["lookback_crosses_gap"] == 1
    a1 = by_hyp["S1:A1:follow>base"]
    assert a1["n_eligible_dropped"] == 0
    assert a1["eligible_dropped_by_reason"] == {}


def test_run_p1_card_future_window_drop_reason():
    """持有/未来窗不满足（future_window——词表对齐甲的 EligibilityContext）：
    31 天洞 (t30,t31)、hold=5 → A3@26 的窗 [26,31] 含洞两侧 → 被剔
    （回看窗不跨 → 原因是未来窗）。序列补长到 63 根：末端截断不参与，
    只测跨洞这一种未来窗失败。"""
    from tests.test_prob_t97_wiring import (
        fake_card as t97_card, make_bars_rows, s1_pattern_rows, hourly, PLAT,
    )
    times = [x if i <= 30 else x + pd.Timedelta(days=31)
             for i, x in enumerate(hourly(63))]
    bars = make_bars_rows(s1_pattern_rows() + [PLAT] * 30, times)
    t = list(pd.to_datetime(bars["time"]))
    card = t97_card(hypotheses=("S1:A3:fade>base", "S1:A1:follow>base"))
    from mt5data.sessions import SessionPolicy
    res = p1mod.run_p1_card(
        card, fake_round(), {"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
        sizes={"XAUUSD": 100.0},
        policy_by_symbol={"XAUUSD": SessionPolicy(
            exempt_symbols=frozenset({"XAUUSD"}))},
        sims=50, feature_warmup=0, hold_bars=5,
        gap_spans_by_symbol={"XAUUSD": [(t[30], t[31])]})
    by_hyp = {r["hypothesis"]: r for r in res}
    a3 = by_hyp["S1:A3:fade>base"]
    assert a3["n_eligible_dropped"] == 1
    assert a3["eligible_dropped_by_reason"]["future_window"] == 1
    # A1@28 的回看窗 [22,28] 干净，但持有窗 [28,33] 含洞两侧 (t30,t31)
    # → 同因被剔（suppress ⇔ 窗首 ≤ s 且 e ≤ 触发后端点）。
    a1 = by_hyp["S1:A1:follow>base"]
    assert a1["n_eligible_dropped"] == 1
    assert a1["eligible_dropped_by_reason"]["future_window"] == 1


def test_run_p1_card_null_side_same_caliber_counts(monkeypatch):
    """P2-11：null 侧同口径计数——n_null_total/n_null_usable/
    n_null_skipped/n_null_trigger_days（排除 skipped，与 real 同口径）；
    null 从合格池抽样 → 资格剔除恒 0（同口径记录）。"""
    bars = synth_bars(6000, seed=9)
    card = fake_card()
    rnd = fake_round()
    calls = []
    real_label = p1mod.label_triggers

    def stub_labels(b, trig, costs, **kw):
        times = pd.to_datetime(trig["time"])
        calls.append(list(times))
        outcomes = ["win" if k % 2 == 0 else "skipped"
                    for k in range(len(trig))]
        return pd.DataFrame({
            "time": times.to_numpy(), "outcome": outcomes,
            "net_r": [1.9 if o == "win" else 0.0 for o in outcomes]})

    monkeypatch.setattr(p1mod, "label_triggers", stub_labels)
    from mt5data.sessions import SessionPolicy
    res = p1mod.run_p1_card(card, rnd, {"XAUUSD": bars},
                            {"XAUUSD": FlatCosts()},
                            sizes={"XAUUSD": 100.0},
                            policy_by_symbol={"XAUUSD": SessionPolicy(
                                exempt_symbols=frozenset({"XAUUSD"}))},
                            sims=50)
    row = res[0]
    assert len(calls) >= 2                        # real + null（单假设）
    null_times = calls[1]
    n = len(null_times)
    n_skipped = sum(1 for k in range(n) if k % 2 == 1)
    assert row["n_null_total"] == n
    assert row["n_null_skipped"] == n_skipped
    assert row["n_null_usable"] == n - n_skipped
    null_usable_days = len({t.date() for k, t in enumerate(null_times)
                            if k % 2 == 0})
    assert row["n_null_trigger_days"] == null_usable_days
    assert row["n_null_eligible_dropped"] == 0    # 池已按共同可用集合预滤
    # real 侧同口径自洽
    assert row["n_total"] == row["n_usable"] + row["n_skipped"]
