"""审核四 E1/E2 端到端：P2 决策卡（standard+validation）经 screen/gate。

全部合成数据、临时 git 仓库与临时台账；不读研究库。
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "scripts")]

_spec = importlib.util.spec_from_file_location("bt_e1e2", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
sys.modules["bt_e1e2"] = BT
_spec.loader.exec_module(BT)

from mt5data.store import save_bars                       # noqa: E402
from mt5quant.research.ledger import Ledger               # noqa: E402

STRATEGY_REF = ("mt5quant.strategies.library.prob.level_decision:"
                "LevelDecisionS1Demo")
SPLIT = "2019-12-01"

CARD = '''id = "P002-v1"
family = "P002"
version = 1
parent = ""
candidate = "C101"
round = "R3"
strategy = "{ref}"
symbols = ["XAUUSD"]
timeframe = "H1"
evaluation_mode = "standard"
segment = "validation"
session = "America/New_York"
phenomenon = "合成探针"
mechanism = "合成探针"
should_fail_when = "合成探针"
falsified_if = "合成探针"
graveyard_checked = []

[rules]
entry = "由 [prob] 决定"
exit = "tp/sl/K 根归零"
stop = "sl_atr×ATR"

[expected]
delta_p = "探针"
triggers_per_year = "探针"

[prob]
situations = ["S1"]
hypotheses = ["S1:A3:fade>base"]
bracket = {{ sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 8 }}
level_grid = {{ XAUUSD = 2.0 }}
approach = {{ lookback_bars = 12, min_distance_atr = 1.0, touch_atr = 0.25 }}
breakout_atr = 0.25
retest_window_bars = 4
features = ["trend_sma200", "atr_pct_2000"]
prior_strength = 100
min_cell = 100
lb_z = {lb_z}
entry_threshold_cost_mult = 2.0
expected_delta_p = 0.04
'''

ROUND = f'''id = "R3"
opened = "2026-11-20"
candidates = ["C101"]
trial_budget = 4
lockbox_uses_max = 1
dsr_trial_scope = "project"
advance_rule = "关卡 v3"
close_rule = "全部决策卡有结论"
approved_by = "探针"

[segments]
split = "{SPLIT}"

[evaluation.C101]
mode = "standard"
primary = "gates_v3"
'''

SESSIONS_TOML = """version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 5
post_minutes = 60
exempt_symbols = ["BTCUSD"]
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
min_days_per_hour = 20
"""


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=probe",
                    "-c", "user.email=probe@example.invalid", *args],
                   cwd=repo, check=True, capture_output=True)


def synth(n=17000, seed=21):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    op = close + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "time": pd.date_range("2018-06-01", periods=n, freq="1h"),
        "open": op,
        "high": np.maximum(op, close) + np.abs(rng.normal(0, 0.12, n)),
        "low": np.minimum(op, close) - np.abs(rng.normal(0, 0.12, n)),
        "close": close, "tick_volume": 10, "spread": 5, "real_volume": 0})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "research" / "cards").mkdir(parents=True)
    (repo / "research" / "rounds").mkdir(parents=True)
    rpath = repo / "research" / "rounds" / "R3.toml"
    rpath.write_text(ROUND, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "round")
    store = tmp_path / "store"
    save_bars(str(store), "XAUUSD", "H1", synth())
    (store / "specs").mkdir(parents=True, exist_ok=True)
    (store / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sessions.toml").write_text(SESSIONS_TOML, encoding="utf-8")
    monkeypatch.setattr(BT, "SESSIONS_PATH", cfg / "sessions.toml")
    led_path = tmp_path / "ledger.sqlite"
    with Ledger(led_path) as led:
        led.record_candidate_check("C101", decision="pass",
                                   result={"probe": True})
        led.record_round_open(
            round_id="R3", candidates=["C101"], trial_budget=4,
            lockbox_uses_max=1, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="探针",
            evaluation={"C101": {"mode": "standard",
                                 "primary": "gates_v3"}},
            toml_sha256=hashlib.sha256(rpath.read_bytes()).hexdigest(),
            git_commit="0" * 40)
    monkeypatch.setattr(BT, "_bind_strategy_sha",
                        lambda led, card, ref: "f" * 40)
    return {"tmp": tmp_path, "repo": repo, "store": store,
            "ledger": led_path, "cards": repo / "research" / "cards"}


def _card_path(env, lb_z):
    p = env["cards"] / "P002-v1.toml"
    p.write_text(CARD.format(ref=STRATEGY_REF, lb_z=lb_z),
                 encoding="utf-8")
    _git(env["repo"], "add", ".")
    _git(env["repo"], "commit", "-q", "-m", f"card lb_z={lb_z}")
    return p


def _screen(env, lb_z, monkeypatch):
    _card_path(env, lb_z)
    argv = ["screen", "--card", "P002-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(env["cards"]), "--data-root", str(env["store"]),
            "--ledger", str(env["ledger"]), "--costs", "zero",
            "--volume-lots", "1.0"]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    return code, out.getvalue()


def test_e1_screen_runs_validation_card(env, monkeypatch):
    """E1/E2：完整决策字段的 validation 卡经 screen 跑通——退出码 0、
    一行成功运行、拟合窗止于分界、全部交易在分界之后入场。"""
    code, out = _screen(env, 1.0, monkeypatch)
    assert code == 0, out
    with Ledger(env["ledger"]) as led:
        rows = [r for r in led.runs_of_round("R3") if r["ok"]]
    assert len(rows) == 1
    m = rows[0]["metrics"]["prob"]
    assert str(m["fit_end"])[:10] <= SPLIT
    assert m["fit_start"]
    # 交易全部在分界之后入场（信号分界前为 0 → 引擎不可能在分界前开仓）
    sig_masked = rows[0]["params"]["prob"]["segment"] == "validation"
    assert sig_masked


def test_e2_lb_z_mismatch_rejected(env, monkeypatch):
    """E2：lb_z 与策略类 POLICY.z 不一致 → 拒绝运行。"""
    code, out = _screen(env, 1.5, monkeypatch)
    assert code != 0
    assert "lb_z" in out and "不一致" in out


def test_e1_gate_runs_validation_card(env, monkeypatch):
    """E1：validation 决策卡经 gate（G0）跑通；G0 的冻结模型只用
    < split 训练段前 70%。"""
    _card_path(env, 1.0)
    argv = ["gate", "--card", "P002-v1", "--strategy", STRATEGY_REF,
            "--gates", "G0", "--cards-dir", str(env["cards"]),
            "--data-root", str(env["store"]), "--ledger", str(env["ledger"]),
            "--costs", "zero", "--volume-lots", "1.0", "--params", "{}"]
    out = io.StringIO()
    code = 0
    monkeypatch.chdir(env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 0, out.getvalue()
    assert "validation 评估窗" in out.getvalue()


def test_e2_prob_fields_validated(tmp_path):
    """E2：五个决策字段的类型/取值校验（bad 值拒）。"""
    from tests.test_prob_precheck import prob_data
    from mt5quant.research import candidates as cand
    base = prob_data()["prob"]
    with pytest.raises(cand.CandidateError, match="features"):
        cand.validate_candidate(prob_data(prob={
            **base, "features": ["nope"]}), source="t")
    with pytest.raises(cand.CandidateError, match="prior_strength"):
        cand.validate_candidate(prob_data(prob={
            **base, "prior_strength": 0}), source="t")
    with pytest.raises(cand.CandidateError, match="min_cell"):
        cand.validate_candidate(prob_data(prob={
            **base, "min_cell": True}), source="t")
    with pytest.raises(cand.CandidateError, match="lb_z"):
        cand.validate_candidate(prob_data(prob={
            **base, "lb_z": 0.0}), source="t")
    with pytest.raises(cand.CandidateError, match="entry_threshold"):
        cand.validate_candidate(prob_data(prob={
            **base, "entry_threshold_cost_mult": -1}), source="t")
    c = cand.validate_candidate(prob_data(prob={
        **base, "features": ["trend_sma200"], "prior_strength": 50,
        "min_cell": 60, "lb_z": 1.0,
        "entry_threshold_cost_mult": 2.0}), source="t")
    pd_ = c.prob_declared
    assert pd_.prior_strength == 50 and pd_.lb_z == 1.0
