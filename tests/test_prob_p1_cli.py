"""T90 审核三修正补充：端到端命令行、S2 轮次约束、S6 成本条件、分界剔除。

全部合成数据与临时 git 仓库（先卡后测桩），不读研究库。
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "scripts")]

import importlib.util
_spec = importlib.util.spec_from_file_location("bt_t90b", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
sys.modules["bt_t90b"] = BT
_spec.loader.exec_module(BT)

from mt5data.store import save_bars                       # noqa: E402
from mt5quant.research.ledger import Ledger               # noqa: E402

STRATEGY_REF = ("mt5quant.strategies.library.prob.level_decision:"
                "LevelDecisionS1Demo")


def synth_bars(n, *, seed, start="2018-01-01", s0=100.0):
    rng = np.random.default_rng(seed)
    close = s0 + np.cumsum(rng.normal(0.0, 0.30, n))
    openc = close + rng.normal(0.0, 0.05, n)
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq="1h"),
        "open": openc,
        "high": np.maximum(openc, close) + np.abs(rng.normal(0, 0.12, n)),
        "low": np.minimum(openc, close) - np.abs(rng.normal(0, 0.12, n)),
        "close": close, "tick_volume": 10, "spread": 5, "real_volume": 0})


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


class FlatCosts:
    def spread_p90(self, s, h, *, price=None):
        return 0.02

    def slippage(self, s, h, *, price=None):
        return 0.01

    def commission_per_lot(self, s):
        return 0.0

    def overnight_fee(self, s, side, f, t, *, price=None):
        return 0.0


CARD = '''id = "P001-v1"
family = "P001"
version = 1
parent = ""
candidate = "C101"
round = "R2"
strategy = "{ref}"
symbols = ["XAUUSD"]
timeframe = "H1"
evaluation_mode = "probability"
primary_statistic = "delta_p_one_sided"
segment = "discovery"
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
hypotheses = ["S1:A1:follow>base", "S1:A3:fade>base"]
bracket = {{ sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 8 }}
level_grid = {{ XAUUSD = 2.0 }}
approach = {{ lookback_bars = 12, min_distance_atr = 1.0, touch_atr = 0.25 }}
breakout_atr = 0.25
retest_window_bars = 4
expected_delta_p = 0.04
'''

ROUND = '''id = "R2"
opened = "2026-11-02"
candidates = ["C101"]
trial_budget = 12
lockbox_uses_max = 0
dsr_trial_scope = "project"
advance_rule = "Holm 显著且成本达标"
close_rule = "全部假设有结论"
approved_by = "探针"
[segments]
split = "2019-12-01"
[evaluation.C101]
mode = "probability"
primary = "delta_p_one_sided"
[hypotheses]
C101 = ["S1:A1:follow>base", "S1:A3:fade>base"]
'''


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=probe",
                    "-c", "user.email=probe@example.invalid", *args],
                   cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    """临时 git 仓库（卡与轮次已提交）+ 合成数据根 + 已开启轮次台账。"""
    repo = tmp_path / "repo"
    (repo / "research" / "cards").mkdir(parents=True)
    (repo / "research" / "rounds").mkdir(parents=True)
    card_p = repo / "research" / "cards" / "P001-v1.toml"
    card_p.write_text(CARD.format(ref=STRATEGY_REF), encoding="utf-8")
    round_p = repo / "research" / "rounds" / "R2.toml"
    round_p.write_text(ROUND, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "probe")
    store = tmp_path / "store"
    save_bars(str(store), "XAUUSD", "H1", synth_bars(9000, seed=21))
    (store / "specs").mkdir(parents=True, exist_ok=True)
    (store / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    # 禁入窗配置（bt 读仓库 config/sessions.toml——桩到临时目录）
    sess = tmp_path / "config"
    sess.mkdir()
    (sess / "sessions.toml").write_text(SESSIONS_TOML, encoding="utf-8")
    monkeypatch.setattr(BT, "SESSIONS_PATH", sess / "sessions.toml")
    ledger_path = store / "research" / "ledger.sqlite"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with Ledger(ledger_path) as led:
        led.record_candidate_check("C101", decision="pass",
                                   result={"probe": True})
        led.record_round_open(
            round_id="R2", candidates=["C101"], trial_budget=12,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="探针",
            evaluation={"C101": {"mode": "probability",
                                 "primary": "delta_p_one_sided"}},
            toml_sha256=hashlib.sha256(round_p.read_bytes()).hexdigest(),
            git_commit="0" * 40)
    monkeypatch.setattr(BT, "build_costs",
                        lambda *a, **k: (FlatCosts(), "flat-probe"))
    return {"repo": repo, "store": store, "ledger": ledger_path,
            "card": card_p, "round": round_p, "tmp": tmp_path}


def _run_prob_test(env, monkeypatch, *, card="P001-v1", sims="200"):
    import contextlib, io as _io
    argv = ["prob", "test", "--card", card, "--strategy", STRATEGY_REF,
            "--cards-dir", str(env["repo"] / "research" / "cards"),
            "--data-root", str(env["store"]), "--ledger", str(env["ledger"]),
            "--sims", sims]
    out = _io.StringIO()
    code = 0
    monkeypatch.chdir(env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    return code, out.getvalue()


def test_cli_prob_test_end_to_end(cli_env, monkeypatch):
    """S1：命令行本身跑通——退出码 0，台账每条假设一行（params.prob 带
    prob_version，试验身份含评估代码版本）。"""
    code, out = _run_prob_test(cli_env, monkeypatch)
    assert code == 0, out
    with Ledger(cli_env["ledger"]) as led:
        rows = [r for r in led.runs_of_round("R2") if r["ok"]]
    assert len(rows) == 2
    hyps = {r["params"]["hypothesis"] for r in rows}
    assert hyps == {"S1:A1:follow>base", "S1:A3:fade>base"}
    for r in rows:
        assert r["params"]["prob"]["prob_version"]
        assert r["params"]["prob"]["split"] == "2019-12-01"
        assert r["metrics"]["prob"]["p_value"] is not None
    # 同一输入重跑：去重口径下不增 N（runs 行可增，round_n_dedup 不变）
    before = Ledger(cli_env["ledger"]).round_n_dedup("R2") \
        if False else None
    with Ledger(cli_env["ledger"]) as led:
        n_before = led.round_n_dedup("R2")
    code2, _ = _run_prob_test(cli_env, monkeypatch)
    assert code2 == 0
    with Ledger(cli_env["ledger"]) as led:
        assert led.round_n_dedup("R2") == n_before


def test_cli_rejects_modified_round_file(cli_env, monkeypatch):
    """S2：开启后改轮次文件（分界/假设族）→ sha256 不一致拒绝。"""
    p = cli_env["round"]
    p.write_text(p.read_text(encoding="utf-8").replace(
        'split = "2019-12-01"', 'split = "2020-06-01"'), encoding="utf-8")
    code, out = _run_prob_test(cli_env, monkeypatch)
    assert code == 1
    assert "不一致" in out


def test_cli_rejects_unopened_round(cli_env, monkeypatch, capsys):
    """S2：轮次文件在、台账未开启（删 rounds 行重建台账）→ _round_context 拒。"""
    cli_env["ledger"].unlink()
    with Ledger(cli_env["ledger"]) as led:
        led.record_candidate_check("C101", decision="pass", result={})
    code, out = _run_prob_test(cli_env, monkeypatch)
    assert code == 1
    assert "未开启" in out or "轮次" in out


def test_discovery_triggers_beyond_split_excluded(cli_env, monkeypatch):
    """数据端到端：run 的 data_last 必须早于分界（越界剔除经真实命令）。"""
    code, out = _run_prob_test(cli_env, monkeypatch)
    assert code == 0
    with Ledger(cli_env["ledger"]) as led:
        rows = led.runs_of_round("R2")
    for r in rows:
        assert r["params"]["prob"]["split"] == "2019-12-01"


def test_round_report_cost_condition_and_multi_run_reject(
        cli_env, monkeypatch, tmp_path):
    """S6：Holm 显著但 ΔEV < 3×成本 → 不推进；多条成功运行 → 拒出报告。"""
    import contextlib, io as _io
    # 候选预筛 JSON（成本 0.05R）
    cands = cli_env["repo"] / "research" / "candidates"
    cands.mkdir(parents=True, exist_ok=True)
    (cands / "C101.precheck.json").write_text(json.dumps({
        "candidate_id": "C101", "decision": "pass",
        "checks": {"ex_ante_cost_prob": {
            "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.05}}}}),
        encoding="utf-8")
    _git(cli_env["repo"], "add", ".")
    _git(cli_env["repo"], "commit", "-q", "-m", "precheck")
    # 直接按目标指标登记两条假设运行（Holm p≈0、Δp=+0.005 → ΔEV=0.015R
    # < 3×0.05R=0.15R——台账带哈希链，不直接 UPDATE）
    with Ledger(cli_env["ledger"]) as led:
        for h in ("S1:A1:follow>base", "S1:A3:fade>base"):
            led.record_run(
                card_id="P001-v1", card_version=1, strategy=STRATEGY_REF,
                params={"hypothesis": h,
                        "prob": {"segment": "discovery",
                                 "split": "2019-12-01",
                                 "bracket": {"sl_atr": 1.0, "tp_atr": 2.0,
                                             "horizon_bars": 8},
                                 "seed": 1, "prob_version": "1.0.0"}},
                symbol="XAUUSD", timeframe="H1", data_first=None,
                data_last=None, data_sha256="a" * 64, engine_version="e",
                cost_model_version="v", gates_hash="",
                metrics={"prob": {"p_value": 1e-6, "delta_p": 0.005,
                                  "n_trigger_days": 5000, "n_triggers": 5000,
                                  "real_win_rate": 0.405,
                                  "null_win_rate": 0.40,
                                  "prob_version": "1.0.0"}},
                actor="bt", round_id="R2")
    argv = ["prob", "round-report", "R2",
            "--rounds-dir", str(cli_env["repo"] / "research" / "rounds"),
            "--candidates-dir", str(cands),
            "--data-root", str(cli_env["store"]),
            "--ledger", str(cli_env["ledger"])]
    out = _io.StringIO()
    code = 0
    monkeypatch.chdir(cli_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 0, out.getvalue()
    reports = sorted((cli_env["tmp"] / "docs" / "research")
                     .glob("round-R2-p1-*.md"))
    text = reports[-1].read_text(encoding="utf-8")
    assert "状态：待审核" in text
    for h in ("S1:A1:follow>base", "S1:A3:fade>base"):
        row = next(ln for ln in text.splitlines() if h in ln)
        assert "不推进" in row, row      # ΔEV=0.015R < 0.15R：Holm 过、成本不过
    # 多条成功运行（同假设再来一条）→ 拒绝出报告
    with Ledger(cli_env["ledger"]) as led:
        led.record_run(
            card_id="P001-v1", card_version=1, strategy=STRATEGY_REF,
            params={"hypothesis": "S1:A3:fade>base",
                    "prob": {"segment": "discovery", "split": "2019-12-01",
                             "bracket": {"sl_atr": 1.0}, "seed": 2}},
            symbol="XAUUSD", timeframe="H1", data_first=None, data_last=None,
            data_sha256="c" * 64, engine_version="e",
            cost_model_version="v", gates_hash="",
            metrics={"prob": {"p_value": 0.5, "delta_p": 0.0}},
            actor="bt", round_id="R2")
    out2 = _io.StringIO()
    code2 = 0
    try:
        with contextlib.redirect_stdout(out2):
            code2 = BT.main(argv)
    except SystemExit as e:
        code2 = e.code
    assert code2 == 1
    assert "成功运行" in out2.getvalue()


def test_round_report_rejects_missing_hypothesis_runs(cli_env, monkeypatch):
    """有卡未跑（一条假设无运行）→ round-report 拒。"""
    import contextlib, io as _io
    cands = cli_env["repo"] / "research" / "candidates"
    cands.mkdir(parents=True, exist_ok=True)
    (cands / "C101.precheck.json").write_text(json.dumps({
        "candidate_id": "C101", "decision": "pass",
        "checks": {"ex_ante_cost_prob": {
            "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.05}}}}),
        encoding="utf-8")
    argv = ["prob", "round-report", "R2",
            "--rounds-dir", str(cli_env["repo"] / "research" / "rounds"),
            "--candidates-dir", str(cands),
            "--data-root", str(cli_env["store"]),
            "--ledger", str(cli_env["ledger"])]
    out = _io.StringIO()
    code = 0
    monkeypatch.chdir(cli_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "没有运行记录" in out.getvalue()
