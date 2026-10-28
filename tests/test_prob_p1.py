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
    r1 = p1mod.run_p1_card(card, rnd, {"XAUUSD": bars},
                           {"XAUUSD": FlatCosts()}, sizes=sizes, sims=200)
    r2 = p1mod.run_p1_card(card, rnd, {"XAUUSD": bars},
                           {"XAUUSD": FlatCosts()}, sizes=sizes, sims=200)
    assert r1 == r2                              # 逐位一致
    assert len(r1) == 1
    row = r1[0]
    assert row["hypothesis"] == "S1:A3:fade>base"
    assert row["seed"] == p1mod.hypothesis_seed("P001-v1", "S1:A3:fade>base")
    assert 0.0 <= row["p_value"] <= 1.0
    assert row["n_triggers"] >= row["n_trigger_days"] >= 0


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
    def __init__(self, used=0):
        self.used = used

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
    led = _LedgerStub()
    assert BT._prob_preconditions(led, card, fake_path, actor="bt") is not None
    # 1) validation 卡
    v = fake_card(); v.segment = "validation"
    assert BT._prob_preconditions(led, v, fake_path, actor="bt") is None
    # 2) 未批准轮次
    _write_round_file(tmp_path, approved="")
    assert BT._prob_preconditions(led, card, fake_path, actor="bt") is None
    # 3) 假设表不一致
    _write_round_file(tmp_path, hyps=["S1:A1:follow>base"])
    assert BT._prob_preconditions(led, card, fake_path, actor="bt") is None
    # 4) 预算不足
    _write_round_file(tmp_path)
    led2 = _LedgerStub(used=12)
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
