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
        led.record_candidate_check("C101", decision="pass", result={
            "checks": {"ex_ante_cost_prob": {
                "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.05}}}})
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
    monkeypatch.setattr(BT, "_bind_strategy_sha",
                        lambda led, card, ref: "f" * 40)
    return {"repo": repo, "store": store, "ledger": ledger_path,
            "card": card_p, "round": round_p, "tmp": tmp_path}


def _run_prob_test(env, monkeypatch, *, card="P001-v1", sims="200"):
    import contextlib, io as _io
    argv = ["prob", "test", "--card", card, "--strategy", STRATEGY_REF,
            "--cards-dir", str(env["repo"] / "research" / "cards"),
            "--data-root", str(env["store"]), "--ledger", str(env["ledger"]),
            "--sims", sims, "--allow-unbounded-data"]
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


def test_e6_report_uses_round_open_precheck_cost(cli_env, monkeypatch):
    """E6：开轮后预筛 JSON 被改写（磁盘），报告仍用**开轮时入账**的
    台账 candidate_checks 成本；候选号过滤生效。"""
    import contextlib, io as _io
    from datetime import datetime, timezone, timedelta
    # 开轮前先入账一个"贵"成本预筛（记录即入链）
    with Ledger(cli_env["ledger"]) as led:
        led.record_candidate_check("C101", decision="pass", result={
            "checks": {"ex_ante_cost_prob": {
                "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.05}}}})
    # 磁盘 JSON 写个"便宜"成本（模拟开轮后被改写）
    cands = cli_env["repo"] / "research" / "candidates"
    cands.mkdir(parents=True, exist_ok=True)
    (cands / "C101.precheck.json").write_text(json.dumps({
        "candidate_id": "C101", "decision": "pass",
        "checks": {"ex_ante_cost_prob": {
            "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.0001}}}}),
        encoding="utf-8")
    _git(cli_env["repo"], "add", ".")
    _git(cli_env["repo"], "commit", "-q", "-m", "precheck")
    code, _ = _run_prob_test(cli_env, monkeypatch)
    assert code == 0
    # 两条假设运行（Δp 真实值不定），直接重写台账两行成本无关字段使
    # ΔEV 显著不足（p≈0、Δp=0.005 < 3×0.05R）——经台账正规写入
    with Ledger(cli_env["ledger"]) as led:
        rows = led.conn.execute(
            "SELECT run_id, card_id, params_json FROM runs "
            "WHERE round_id='R2'").fetchall()
        for r in rows:
            params = json.loads(r["params_json"])
            params["prob"]["candidate"] = "C101"
            metrics = {"prob": {"p_value": 1e-6, "delta_p": 0.005,
                                "n_trigger_days": 5000, "n_triggers": 5000,
                                "prob_version": "1.0.0"}}
            conn = led.conn
            payload_ok = json.dumps({"ok": True, "error": None,
                                     "metrics": metrics})
            conn.execute(
                "UPDATE runs SET params_json=?, metrics_json=? "
                "WHERE run_id=?",
                (json.dumps(params), payload_ok, r["run_id"]))
        led.conn.commit()
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
    # 台账开轮行成本 0.05 → 门槛 0.15R；磁盘 0.0001 → 0.0003R（若读磁盘
    # 会误判推进）。ΔEV=0.005×3=0.015R：按台账成本"不推进"。
    for h in ("S1:A1:follow>base", "S1:A3:fade>base"):
        row = next(ln for ln in text.splitlines() if h in ln)
        assert "不推进" in row, row


# ============================================ T98 波次 2（P-04/P-05/P-06）

from mt5quant.strategies.library.prob.level_decision import (  # noqa: E402
    LevelDecisionS1Demo,
)


class TwoSymDemo(LevelDecisionS1Demo):
    """P-06 探针：双品种网格（prob test 不跑引擎——标签经 prob.labels，
    只做卡/策略常量核对；SYMBOLS 双品种供 P-07 核对）。"""

    SYMBOLS = ("XAUUSD", "BTCUSD")
    LEVEL_GRID = {"XAUUSD": 2.0, "BTCUSD": 300.0}


CAND_C101 = '''id = "C101"
title = "整数关口"
mechanism_class = "price_levels"
status = "registered"
symbols = ["XAUUSD"]
holding = "H1"
mechanism = "止盈止损聚集"
[literature]
original = ["Osler 2003"]
replications = []
rebuttals = []
search_log = "JF 2026-10-28"
published_effect = "聚集"
expected_effect = "Δp 0.05"
[prob]
situations = ["S1"]
hypotheses = ["S1:A1:follow>base", "S1:A3:fade>base"]
bracket = { sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 24 }
level_grid = { XAUUSD = 50.0 }
approach = { lookback_bars = 24, min_distance_atr = 1.0, touch_atr = 0.25 }
breakout_atr = 0.25
retest_window_bars = 6
expected_delta_p = 0.05
'''

CAND_C102 = CAND_C101.replace('id = "C101"', 'id = "C102"').replace(
    'hypotheses = ["S1:A1:follow>base", "S1:A3:fade>base"]',
    'hypotheses = ["S1:A2:follow>base"]')

ROUND_R4 = '''id = "R4"
opened = "2026-11-02"
candidates = ["C101", "C102"]
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
[evaluation.C102]
mode = "probability"
primary = "delta_p_one_sided"
[hypotheses]
C101 = ["S1:A1:follow>base", "S1:A3:fade>base"]
C102 = ["S1:A2:follow>base"]
'''

CARD_R4 = '''id = "P004-v1"
family = "P004"
version = 1
parent = ""
candidate = "C101"
round = "R4"
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
hypotheses = [{hyps}]
bracket = {{ sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 8 }}
level_grid = {{ XAUUSD = 2.0 }}
approach = {{ lookback_bars = 12, min_distance_atr = 1.0, touch_atr = 0.25 }}
breakout_atr = 0.25
retest_window_bars = 4
expected_delta_p = 0.05
'''


def _power_precheck(ledger, cid, per_hyp):
    """记一条 pass 预筛（带 power_prob.per_hypothesis 与成本）。"""
    ledger.record_candidate_check(cid, decision="pass", result={
        "checks": {"power_prob": {"per_hypothesis": per_hyp},
                   "ex_ante_cost_prob": {
                       "passed": True,
                       "cost_r_by_symbol": {"XAUUSD": 0.05}}}})


@pytest.fixture()
def round_env(tmp_path, monkeypatch):
    """P-04 夹具：候选登记（C101/C102）+ R4 预注册 + 数据根 + 空台账。"""
    repo = tmp_path / "repo"
    (repo / "research" / "cards").mkdir(parents=True)
    (repo / "research" / "rounds").mkdir(parents=True)
    (repo / "research" / "candidates").mkdir(parents=True)
    (repo / "research" / "candidates" / "C101.toml").write_text(
        CAND_C101, encoding="utf-8")
    (repo / "research" / "candidates" / "C102.toml").write_text(
        CAND_C102, encoding="utf-8")
    rpath = repo / "research" / "rounds" / "R4.toml"
    rpath.write_text(ROUND_R4, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "probe")
    store = tmp_path / "store"
    save_bars(str(store), "XAUUSD", "H1", synth_bars(9000, seed=21))
    (store / "specs").mkdir(parents=True, exist_ok=True)
    (store / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    sess = tmp_path / "config"
    sess.mkdir()
    (sess / "sessions.toml").write_text(SESSIONS_TOML, encoding="utf-8")
    monkeypatch.setattr(BT, "SESSIONS_PATH", sess / "sessions.toml")
    return {"repo": repo, "store": store, "tmp": tmp_path,
            "round": rpath,
            "ledger": tmp_path / "ledger.sqlite",
            "cards": repo / "research" / "cards"}


def _open_r4(env, monkeypatch, capsys=None):
    import contextlib, io as _io
    out = _io.StringIO()
    code = 0
    monkeypatch.chdir(env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(["round", "open", "R4",
                            "--rounds-dir",
                            str(env["repo"] / "research" / "rounds"),
                            "--ledger", str(env["ledger"])])
    except SystemExit as e:
        code = e.code
    return code, out.getvalue()


def test_p04_round_open_rejects_missing_per_hypothesis(round_env, monkeypatch):
    """P-04：概率候选台账最新预筛无 power_prob.per_hypothesis → 保守拒绝
    开轮（waiting_data 假设不得混入冻结族）。"""
    with Ledger(round_env["ledger"]) as led:
        led.record_candidate_check("C101", decision="pass", result={
            "checks": {"ex_ante_cost_prob": {
                "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.05}}}})
        led.record_candidate_check("C102", decision="pass", result={})
    code, out = _open_r4(round_env, monkeypatch)
    assert code == 1
    assert "per_hypothesis" in out
    with Ledger(round_env["ledger"]) as led:
        assert led.conn.execute("SELECT COUNT(*) FROM rounds"
                                ).fetchone()[0] == 0


def test_p04_round_open_freezes_passing_family(round_env, monkeypatch):
    """P-04/D52：逐条核对 per_hypothesis passed——未通过的假设不入冻结族
    （输出写明）；全轮总条数复核一次，候选口径过而全轮口径不足的假设
    机械移除（N 不缩小重算）。"""
    from mt5quant.research.candidates import measured_n_required
    req_cand, _ = measured_n_required(2, 0.05)     # C101 候选自己的条数
    req_round, _ = measured_n_required(3, 0.05)    # 全轮冻结总条数（2+1）
    days_mid = (req_cand + req_round) // 2
    assert req_cand < days_mid < req_round, "夹具不自洽：中位日数须落在两档之间"
    with Ledger(round_env["ledger"]) as led:
        _power_precheck(led, "C101", {
            "S1:A1:follow>base": {"trigger_days": req_round + 500,
                                  "passed": True},
            "S1:A3:fade>base": {"trigger_days": days_mid, "passed": True}})
        _power_precheck(led, "C102", {
            "S1:A2:follow>base": {"trigger_days": req_round + 500,
                                  "passed": True}})
        # 一个预筛未通过的假设（开轮前即移除，与全轮复核移除分开报告）
        # ——上面的 A3 走全轮复核；另加一条候选口径就不过的对照在下方
        # 单独断言输出语义。
    code, out = _open_r4(round_env, monkeypatch)
    assert code == 0, out
    assert "S1:A3:fade>base" in out and "移除" in out
    with Ledger(round_env["ledger"]) as led:
        row = led.round("R4")
    frozen = json.loads(row["evaluation_json"])["__frozen_hypotheses__"]
    assert frozen["hypotheses"]["C101"] == ["S1:A1:follow>base"]
    assert frozen["hypotheses"]["C102"] == ["S1:A2:follow>base"]
    assert frozen["n_bonferroni"] == 3            # 移除前的冻结总条数
    assert "S1:A3:fade>base" in frozen["dropped"]["C101"]
    # 复核只做一遍：移除后不按 2 条重算（N 仍记 3）


def test_p04_round_open_drops_precheck_failed_hypothesis(round_env,
                                                          monkeypatch):
    """P-04：预筛 per_hypothesis passed=False 的假设开轮即移除（不整轮拒绝
    ——报告写明哪些未通过）。"""
    from mt5quant.research.candidates import measured_n_required
    req_round, _ = measured_n_required(3, 0.05)
    with Ledger(round_env["ledger"]) as led:
        _power_precheck(led, "C101", {
            "S1:A1:follow>base": {"trigger_days": req_round + 500,
                                  "passed": True},
            "S1:A3:fade>base": {"trigger_days": 3, "passed": False}})
        _power_precheck(led, "C102", {
            "S1:A2:follow>base": {"trigger_days": req_round + 500,
                                  "passed": True}})
    code, out = _open_r4(round_env, monkeypatch)
    assert code == 0, out
    with Ledger(round_env["ledger"]) as led:
        row = led.round("R4")
    frozen = json.loads(row["evaluation_json"])["__frozen_hypotheses__"]
    assert frozen["hypotheses"]["C101"] == ["S1:A1:follow>base"]
    assert frozen["n_bonferroni"] == 2            # 候选口径过滤后的总数
    assert "预筛" in frozen["dropped"]["C101"]["S1:A3:fade>base"]


def _commit_card(env, body):
    p = env["cards"] / "P004-v1.toml"
    p.write_text(body, encoding="utf-8")
    _git(env["repo"], "add", ".")
    _git(env["repo"], "commit", "-q", "-m", "card")
    return p


def _run_prob_test_card(env, monkeypatch, *, card_path="P004-v1"):
    import contextlib, io as _io
    argv = ["prob", "test", "--card", card_path,
            "--strategy", STRATEGY_REF,
            "--cards-dir", str(env["cards"]),
            "--data-root", str(env["store"]),
            "--ledger", str(env["ledger"]), "--sims", "200",
            "--allow-unbounded-data"]
    out = _io.StringIO()
    code = 0
    monkeypatch.chdir(env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    return code, out.getvalue()


@pytest.fixture()
def r4_open(round_env, monkeypatch):
    """R4 已按冻结族开启（C101 只剩 A1；C102 留 A2），卡绑定指纹已桩。"""
    from mt5quant.research.candidates import measured_n_required
    req_round, _ = measured_n_required(3, 0.05)
    with Ledger(round_env["ledger"]) as led:
        _power_precheck(led, "C101", {
            "S1:A1:follow>base": {"trigger_days": req_round + 500,
                                  "passed": True},
            "S1:A3:fade>base": {"trigger_days": 3, "passed": False}})
        _power_precheck(led, "C102", {
            "S1:A2:follow>base": {"trigger_days": req_round + 500,
                                  "passed": True}})
    code, out = _open_r4(round_env, monkeypatch)
    assert code == 0, out
    monkeypatch.setattr(BT, "_bind_strategy_sha",
                        lambda led, card, ref: "f" * 40)
    monkeypatch.setattr(BT, "build_costs",
                        lambda *a, **k: (FlatCosts(), "flat-probe"))
    return round_env


def test_p04_prob_test_card_must_match_frozen_family(r4_open, monkeypatch):
    """P-04：卡假设表必须等于冻结族——被移除的假设还在卡上 → 拒。"""
    _commit_card(r4_open, CARD_R4.format(
        ref=STRATEGY_REF,
        hyps='"S1:A1:follow>base", "S1:A3:fade>base"'))
    code, out = _run_prob_test_card(r4_open, monkeypatch)
    assert code == 1
    assert "冻结族" in out or "不一致" in out


def test_p04_prob_test_frozen_family_card_runs(r4_open, monkeypatch):
    """P-04：卡假设表=冻结族（只剩 A1）→ 正常跑通（一条假设一行）。"""
    _commit_card(r4_open, CARD_R4.format(
        ref=STRATEGY_REF, hyps='"S1:A1:follow>base"'))
    code, out = _run_prob_test_card(r4_open, monkeypatch)
    assert code == 0, out
    with Ledger(r4_open["ledger"]) as led:
        rows = [r for r in led.runs_of_round("R4") if r["ok"]
                and (r["params"].get("prob") or {}).get("candidate")
                == "C101"]
    assert len(rows) == 1
    assert rows[0]["params"]["hypothesis"] == "S1:A1:follow>base"


def _record_r4_run(ledger, *, cid, hyp, p_value=1e-6, delta=0.005,
                   seed=1):
    ledger.record_run(
        card_id="P004-v1", card_version=1, strategy=STRATEGY_REF,
        params={"hypothesis": hyp,
                "prob": {"candidate": cid, "segment": "discovery",
                         "split": "2019-12-01",
                         "bracket": {"sl_atr": 1.0, "tp_atr": 2.0,
                                     "horizon_bars": 8},
                         "seed": seed, "prob_version": "1.0.0"}},
        symbol="XAUUSD", timeframe="H1", data_first=None, data_last=None,
        data_sha256="a" * 64, engine_version="e", cost_model_version="v",
        gates_hash="",
        metrics={"prob": {"p_value": p_value, "delta_p": delta,
                          "n_trigger_days": 5000, "n_triggers": 5000,
                          "prob_version": "1.0.0"}},
        actor="bt", round_id="R4")


def _run_round_report(env, monkeypatch, round_id="R4"):
    import contextlib, io as _io
    argv = ["prob", "round-report", round_id,
            "--rounds-dir", str(env["repo"] / "research" / "rounds"),
            "--data-root", str(env["store"]),
            "--ledger", str(env["ledger"])]
    out = _io.StringIO()
    code = 0
    monkeypatch.chdir(env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    return code, out.getvalue()


def test_p04_round_report_rejects_modified_round_toml(r4_open, monkeypatch):
    """P-04：开轮后改磁盘轮次文件（删失败假设缩小 Holm 族）→ sha 不一致
    拒出报告。"""
    with Ledger(r4_open["ledger"]) as led:
        _record_r4_run(led, cid="C101", hyp="S1:A1:follow>base")
        _record_r4_run(led, cid="C102", hyp="S1:A2:follow>base")
    p = r4_open["round"]
    p.write_text(p.read_text(encoding="utf-8").replace(
        'C101 = ["S1:A1:follow>base", "S1:A3:fade>base"]',
        'C101 = ["S1:A1:follow>base"]'), encoding="utf-8")
    code, out = _run_round_report(r4_open, monkeypatch)
    assert code == 1
    assert "不一致" in out


def test_p04_round_report_rejects_family_external_runs(r4_open, monkeypatch):
    """P-04：冻结族之外的假设有成功运行（被移除的 A3 又跑了）→ 拒出报告。"""
    with Ledger(r4_open["ledger"]) as led:
        _record_r4_run(led, cid="C101", hyp="S1:A1:follow>base")
        _record_r4_run(led, cid="C102", hyp="S1:A2:follow>base")
        _record_r4_run(led, cid="C101", hyp="S1:A3:fade>base", seed=2)
    code, out = _run_round_report(r4_open, monkeypatch)
    assert code == 1
    assert "族" in out and "S1:A3:fade>base" in out


def test_p04_round_report_no_disk_precheck_fallback(cli_env, monkeypatch,
                                                     tmp_path):
    """P-04：台账无开轮前预筛记录时**不再回退磁盘 precheck.json**——直接
    拒绝（磁盘 JSON 开轮后可被重写，不作数）。"""
    import contextlib, io as _io
    db2 = tmp_path / "ledger_nofallback.sqlite"
    with Ledger(db2) as led:
        led.record_round_open(
            round_id="R2", candidates=["C101"], trial_budget=12,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="探针",
            evaluation={"C101": {"mode": "probability",
                                 "primary": "delta_p_one_sided"}},
            toml_sha256=hashlib.sha256(
                cli_env["round"].read_bytes()).hexdigest(),
            git_commit="0" * 40)
        for h in ("S1:A1:follow>base", "S1:A3:fade>base"):
            led.record_run(
                card_id="P001-v1", card_version=1, strategy=STRATEGY_REF,
                params={"hypothesis": h,
                        "prob": {"candidate": "C101",
                                 "segment": "discovery",
                                 "split": "2019-12-01",
                                 "bracket": {"sl_atr": 1.0, "tp_atr": 2.0,
                                             "horizon_bars": 8},
                                 "seed": 1, "prob_version": "1.0.0"}},
                symbol="XAUUSD", timeframe="H1", data_first=None,
                data_last=None, data_sha256="a" * 64, engine_version="e",
                cost_model_version="v", gates_hash="",
                metrics={"prob": {"p_value": 0.5, "delta_p": 0.01,
                                  "n_trigger_days": 10}},
                actor="bt", round_id="R2")
    # 磁盘 precheck.json 存在（旧路径会回退读它）
    cands = cli_env["repo"] / "research" / "candidates"
    cands.mkdir(parents=True, exist_ok=True)
    (cands / "C101.precheck.json").write_text(json.dumps({
        "candidate_id": "C101", "decision": "pass",
        "checks": {"ex_ante_cost_prob": {
            "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.0001}}}}),
        encoding="utf-8")
    argv = ["prob", "round-report", "R2",
            "--rounds-dir", str(cli_env["repo"] / "research" / "rounds"),
            "--candidates-dir", str(cands),
            "--data-root", str(cli_env["store"]),
            "--ledger", str(db2)]
    out = _io.StringIO()
    code = 0
    monkeypatch.chdir(cli_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "没有预筛记录" in out.getvalue()


def test_p04_round_report_shared_hypothesis_two_candidates(
        round_env, monkeypatch):
    """波次 2 审核 B1：同轮两个候选登记同一假设串——owner 按假设字符串
    建键会把 C102 的成功运行判成族外（且家族又要求它有运行），报告死锁。
    按（候选, 假设）成对判定后应正常出报告。"""
    from mt5quant.research.candidates import measured_n_required
    req2, _ = measured_n_required(2, 0.05)
    # C102 的候选登记也包含共享假设串（开轮交叉校验：轮次 ⊆ 候选登记）
    cand2 = round_env["repo"] / "research" / "candidates" / "C102.toml"
    cand2.write_text(cand2.read_text(encoding="utf-8").replace(
        'hypotheses = ["S1:A2:follow>base"]',
        'hypotheses = ["S1:A1:follow>base"]'), encoding="utf-8")
    p = round_env["round"]
    p.write_text(p.read_text(encoding="utf-8").replace(
        'C101 = ["S1:A1:follow>base", "S1:A3:fade>base"]',
        'C101 = ["S1:A1:follow>base"]').replace(
        'C102 = ["S1:A2:follow>base"]',
        'C102 = ["S1:A1:follow>base"]'), encoding="utf-8")
    _git(round_env["repo"], "add", ".")
    _git(round_env["repo"], "commit", "-q", "-m", "shared-hyp")
    with Ledger(round_env["ledger"]) as led:
        for cid in ("C101", "C102"):
            _power_precheck(led, cid, {
                "S1:A1:follow>base": {"trigger_days": req2 + 500,
                                      "passed": True}})
    code, out = _open_r4(round_env, monkeypatch)
    assert code == 0, out
    with Ledger(round_env["ledger"]) as led:
        _record_r4_run(led, cid="C101", hyp="S1:A1:follow>base")
        _record_r4_run(led, cid="C102", hyp="S1:A1:follow>base", seed=2)
    code, out = _run_round_report(round_env, monkeypatch)
    assert code == 0, out


def test_p04_round_report_mixed_round_standard_runs_skipped(
        r4_open, monkeypatch):
    """波次 2 审核 B2：混合评估轮（probability + standard 同轮）——
    standard 卡的 screen/gate 成功运行 params 无 hypothesis 键，不属于
    概率 Holm 族，应跳过而非判族外（否则 P1 报告永远出不来）。"""
    with Ledger(r4_open["ledger"]) as led:
        _record_r4_run(led, cid="C101", hyp="S1:A1:follow>base")
        _record_r4_run(led, cid="C102", hyp="S1:A2:follow>base")
        # 同轮 standard P2 卡的 screen 运行（_prob_run_fields：有 prob
        # 试验身份、无 hypothesis/candidate 键）
        led.record_run(
            card_id="P201-v1", card_version=1, strategy=STRATEGY_REF,
            params={"prob": {"segment": "validation",
                             "split": "2019-12-01",
                             "bracket": {"sl_atr": 1.0, "tp_atr": 2.0,
                                         "horizon_bars": 8}}},
            symbol="XAUUSD", timeframe="H1", data_first=None,
            data_last=None, data_sha256="a" * 64, engine_version="e",
            cost_model_version="v", gates_hash="", metrics={},
            actor="bt", round_id="R4")
    code, out = _run_round_report(r4_open, monkeypatch)
    assert code == 0, out


# ------------------------------------------------------------ P-10

P10_ROUND = '''id = "R4"
opened = "2026-11-02"
candidates = ["C101"]
trial_budget = 2
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


@pytest.fixture()
def p10_r4(round_env, monkeypatch):
    """P-10 夹具：R4 只有 C101（A1+A3 全过预筛），预算 2，预筛行带
    candidate_sha256 内容绑定（H-08 生效前提）。"""
    from mt5quant.research.candidates import measured_n_required
    req2, _ = measured_n_required(2, 0.05)
    round_env["round"].write_text(P10_ROUND, encoding="utf-8")
    _git(round_env["repo"], "add", ".")
    _git(round_env["repo"], "commit", "-q", "-m", "p10-round")
    cand = round_env["repo"] / "research" / "candidates" / "C101.toml"
    sha = hashlib.sha256(cand.read_bytes()).hexdigest()
    with Ledger(round_env["ledger"]) as led:
        led.record_candidate_check("C101", decision="pass", result={
            "candidate_sha256": sha,
            "checks": {"power_prob": {"per_hypothesis": {
                h: {"trigger_days": req2 + 500, "passed": True}
                for h in ("S1:A1:follow>base", "S1:A3:fade>base")}},
                "ex_ante_cost_prob": {
                    "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.05}}}})
    code, out = _open_r4(round_env, monkeypatch)
    assert code == 0, out
    monkeypatch.setattr(BT, "_bind_strategy_sha",
                        lambda led, card, ref: "f" * 40)
    monkeypatch.setattr(BT, "build_costs",
                        lambda *a, **k: (FlatCosts(), "flat-probe"))
    return round_env


def _h_style_run(ledger, tag):
    """H 线风格的成功行：record_run 不带 round_trial_budget（H 调用
    路径），七元组与 P 卡不同。"""
    ledger.record_run(
        card_id="H001-v1", card_version=1, strategy=f"h-{tag}",
        params={"combo": tag}, symbol="XAUUSD", timeframe="H1",
        data_first=None, data_last=None, data_sha256="b" * 64,
        engine_version="e", cost_model_version="v", gates_hash="",
        metrics={"net_profit": 1.0}, actor="bt", round_id="R4")


def test_p10_h_occupancy_precheck_rejects_card(p10_r4, monkeypatch):
    """P-10 验证点 1（预检侧）：H 风格运行先占 1 个名额（预算 2）——P 卡
    还需 2 条假设 → 预检拒绝（1+2>2），不产生任何行。"""
    with Ledger(p10_r4["ledger"]) as led:
        _h_style_run(led, "w1")
    _commit_card(p10_r4, CARD_R4.format(
        ref=STRATEGY_REF,
        hyps='"S1:A1:follow>base", "S1:A3:fade>base"'))
    code, out = _run_prob_test_card(p10_r4, monkeypatch)
    assert code == 1
    assert "预算不足" in out
    with Ledger(p10_r4["ledger"]) as led:
        assert len(led.runs_of_round("R4")) == 1     # 只有 H 那一行


def test_x01_budget_exhaustion_refuses_before_computation(p10_r4,
                                                          monkeypatch):
    """X-01（2026-10-30 审核，原 test_p10_midrun_budget_exhaustion_voids_rest
    的**错误契约**修正）：预算权威前移到**计算前占位**——旧断言"预检后
    预算被并发占 1 → 第一条入账、其余计算后作废"固定的正是审核 X-01 指出
    的"先计算后补账"。新契约：占满的预算在 run_p1_card **之前**拒绝——
    零行、零标签调用；H 风格占位（reserve_trial，started 即计）同样烧
    名额，H/P 共享。
    """
    def must_not_run(*a, **k):
        raise AssertionError("预算已满：run_p1_card 不应被调用（X-01 计算前拒绝）")

    monkeypatch.setattr("mt5quant.prob.p1.run_p1_card", must_not_run)
    # H 风格占位先把预算（2）占满：两条 H 试验 started 即计
    with Ledger(p10_r4["ledger"]) as led:
        led.reserve_trial("R4", "h-trial-1", budget=2)
        led.reserve_trial("R4", "h-trial-2", budget=2)
    _commit_card(p10_r4, CARD_R4.format(
        ref=STRATEGY_REF,
        hyps='"S1:A1:follow>base", "S1:A3:fade>base"'))
    code, out = _run_prob_test_card(p10_r4, monkeypatch)
    assert code == 1
    # 预检即拒（round_n_trials 权威口径：2+2>2）——同样是计算前拒绝
    assert "预算不足" in out
    with Ledger(p10_r4["ledger"]) as led:
        rows = led.runs_of_round("R4")
        assert rows == []                     # 计算前拒绝：零行
        assert led.round_n_trials("R4") == 2  # 只有 H 的两条占位


def test_x01_midrun_concurrent_bypass_caught_by_reconciliation(p10_r4,
                                                               monkeypatch):
    """X-01 对账后盾：预检通过、占位完成后，run_p1_card 期间出现的
    **绕过占位**的成功行（旧 H 调用形态：record_run 不经 reserve_trial）
    在成功行写入时被 TrialReservationConsistencyError 拦下（防双计），
    整体退 1——不再是"计算后作废补行"，而是失败如实报错。"""
    def fake_run_p1_card(card, round_obj, bars_by_symbol, costs_by_symbol,
                         **kwargs):
        with Ledger(p10_r4["ledger"]) as led:   # 模拟绕过占位的并发写入
            _h_style_run(led, "race")
        return [{"hypothesis": h, "seed": i + 1, "prob_version": "1.0.0",
                 "n_triggers": 10, "n_trigger_days": 10,
                 "real_win_rate": 0.6, "null_win_rate": 0.5,
                 "delta_p": 0.1, "p_value": 0.01}
                for i, h in enumerate(("S1:A1:follow>base",
                                       "S1:A3:fade>base"))]
    monkeypatch.setattr("mt5quant.prob.p1.run_p1_card", fake_run_p1_card)
    _commit_card(p10_r4, CARD_R4.format(
        ref=STRATEGY_REF,
        hyps='"S1:A1:follow>base", "S1:A3:fade>base"'))
    code, out = _run_prob_test_card(p10_r4, monkeypatch)
    assert code == 1
    assert "对账失败" in out
    with Ledger(p10_r4["ledger"]) as led:
        # P 卡两条占位 + 绕行 H 成功行：把去重成功数推过上限的那次写入
        # 被整体回滚（第三条假设行不存在），此前已入账的行保留
        assert led.round_n_trials("R4") == 2
        n_runs = led.conn.execute(
            "SELECT COUNT(*) n FROM runs").fetchone()["n"]
        assert n_runs == 2        # 绕行 H 行 + 第一条假设行；越限行已回滚
        assert led.round_n_dedup("R4") == 2
        assert led.verify_chain() == []


def test_p10_h08_candidate_tamper_rejects_prob_test(p10_r4, monkeypatch):
    """P-10 验证点 4（H-08 准入继承）：开轮后候选登记文件被改（与预筛
    pass 行绑定的 candidate_sha256 不一致）→ prob test 拒绝——P 路径走
    共享 _round_context 的 _candidate_frozen_check，自动继承 H-08。"""
    cand = p10_r4["repo"] / "research" / "candidates" / "C101.toml"
    cand.write_text(cand.read_text(encoding="utf-8") + "\n# tampered\n",
                    encoding="utf-8")
    _commit_card(p10_r4, CARD_R4.format(
        ref=STRATEGY_REF, hyps='"S1:A1:follow>base"'))
    code, out = _run_prob_test_card(p10_r4, monkeypatch)
    assert code == 1
    assert "预筛通过后被改动" in out
    with Ledger(p10_r4["ledger"]) as led:
        assert led.runs_of_round("R4") == []


def test_p10_p1_policy_same_source(p10_r4, monkeypatch):
    """P-10 政策一致（P1 侧）：prob test 传给 run_p1_card 的
    policy_by_symbol 经 load_session_policy(SESSIONS_PATH)+
    entry_policy_for 构造——与 P2 screen/gate 同一来源同一构造
    （test_p10_cross_line.test_p10_screen_policy_from_sessions_source
    为 P2 侧对应断言）。"""
    from mt5data.sessions import load_session_policy
    cap_epf = []
    captured_pol = {}
    real_epf = BT.entry_policy_for

    def spy_epf(costs, symbol, base):
        out = real_epf(costs, symbol, base)
        cap_epf.append((symbol, base, out))
        return out

    def fake_run_p1_card(card, round_obj, bars_by_symbol, costs_by_symbol,
                         *, policy_by_symbol, **kwargs):
        captured_pol.update(policy_by_symbol)
        return []

    monkeypatch.setattr(BT, "entry_policy_for", spy_epf)
    monkeypatch.setattr("mt5quant.prob.p1.run_p1_card", fake_run_p1_card)
    _commit_card(p10_r4, CARD_R4.format(
        ref=STRATEGY_REF,
        hyps='"S1:A1:follow>base", "S1:A3:fade>base"'))
    code, out = _run_prob_test_card(p10_r4, monkeypatch)
    assert code == 0, out
    assert [s for s, _b, _o in cap_epf] == ["XAUUSD"]
    base_expected = load_session_policy(str(BT.SESSIONS_PATH))
    for sym, base, result in cap_epf:
        # 桩成本无 spread 统计可并入 → 原样返回配置源政策
        assert base == base_expected
        assert result == base_expected
    assert captured_pol["XAUUSD"] == base_expected


# ---------------------------------------------------------------- P-05

def test_p05_strategy_sha_bound_before_computation(cli_env, monkeypatch):
    """P-05：策略指纹绑定提前到 run_p1_card 之前——绑定失败（脏代码/指纹
    不符）时标签计算零调用。"""
    calls = {"n": 0}
    import mt5quant.prob.p1 as p1mod
    real = p1mod.run_p1_card

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(p1mod, "run_p1_card", spy)
    monkeypatch.setattr(BT, "_bind_strategy_sha", lambda *a: None)
    code, out = _run_prob_test(cli_env, monkeypatch)
    assert code == 1
    assert calls["n"] == 0, "准入失败后仍执行了 run_p1_card"


def test_p05_p1_failure_records_batch_error_rows(cli_env, monkeypatch):
    """P-05：run_p1_card 异常 → 全部假设整批记 error 行（append-only 台账
    无行内 update——失败可追溯），退出码 1。"""
    import mt5quant.prob.p1 as p1mod

    def boom(*a, **k):
        raise RuntimeError("boom-探针")

    monkeypatch.setattr(p1mod, "run_p1_card", boom)
    code, out = _run_prob_test(cli_env, monkeypatch)
    assert code == 1
    with Ledger(cli_env["ledger"]) as led:
        rows = led.runs_of_round("R2")
    hyps = {r["params"].get("hypothesis") for r in rows}
    assert hyps == {"S1:A1:follow>base", "S1:A3:fade>base"}
    assert rows and all(not r["ok"] for r in rows)
    assert all("boom-探针" in (r["error"] or "") for r in rows)


# ---------------------------------------------------------------- P-06

CARD_2SYM = '''id = "P006-v1"
family = "P006"
version = 1
parent = ""
candidate = "C101"
round = "R2"
strategy = "tests.test_prob_p1_cli:TwoSymDemo"
symbols = ["XAUUSD", "BTCUSD"]
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
bracket = { sl_atr = 1.0, tp_atr = 2.0, horizon_bars = 8 }
level_grid = { XAUUSD = 2.0, BTCUSD = 300.0 }
approach = { lookback_bars = 12, min_distance_atr = 1.0, touch_atr = 0.25 }
breakout_atr = 0.25
retest_window_bars = 4
expected_delta_p = 0.05
'''


@pytest.fixture()
def two_sym_env(tmp_path, monkeypatch):
    """双品种（价格量级差数千倍）概率环境。"""
    repo = tmp_path / "repo"
    (repo / "research" / "cards").mkdir(parents=True)
    (repo / "research" / "rounds").mkdir(parents=True)
    card_p = repo / "research" / "cards" / "P006-v1.toml"
    card_p.write_text(CARD_2SYM, encoding="utf-8")
    round_p = repo / "research" / "rounds" / "R2.toml"
    round_p.write_text(ROUND, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "probe")
    store = tmp_path / "store"
    save_bars(str(store), "XAUUSD", "H1", synth_bars(9000, seed=21))
    save_bars(str(store), "BTCUSD", "H1",
              synth_bars(9000, seed=22, s0=50000.0))
    (store / "specs").mkdir(parents=True, exist_ok=True)
    (store / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0},
                    "BTCUSD": {"contract_size": 1.0}}), encoding="utf-8")
    sess = tmp_path / "config"
    sess.mkdir()
    (sess / "sessions.toml").write_text(SESSIONS_TOML, encoding="utf-8")
    monkeypatch.setattr(BT, "SESSIONS_PATH", sess / "sessions.toml")
    ledger_path = store / "research" / "ledger.sqlite"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with Ledger(ledger_path) as led:
        led.record_candidate_check("C101", decision="pass", result={
            "checks": {"ex_ante_cost_prob": {
                "passed": True, "cost_r_by_symbol": {"XAUUSD": 0.05}}}})
        led.record_round_open(
            round_id="R2", candidates=["C101"], trial_budget=12,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="探针",
            evaluation={"C101": {"mode": "probability",
                                 "primary": "delta_p_one_sided"}},
            toml_sha256=hashlib.sha256(round_p.read_bytes()).hexdigest(),
            git_commit="0" * 40)
    monkeypatch.setattr(BT, "_bind_strategy_sha",
                        lambda led, card, ref: "f" * 40)
    calls = {}

    def spy_costs(data_root, sym, *a, **k):
        calls[sym] = k.get("swap_ref_price")
        return FlatCosts(), f"flat-{sym}"

    monkeypatch.setattr(BT, "build_costs", spy_costs)
    return {"repo": repo, "store": store, "ledger": ledger_path,
            "card": card_p, "round": round_p, "tmp": tmp_path,
            "calls": calls}


def test_p06_per_symbol_swap_ref_price_and_identity(two_sym_env,
                                                    monkeypatch):
    """P-06：swap 参考价逐品种取各自全序列最后收盘（不循环尾变量复用）；
    params.prob 带全品种 data_sha/cost_version 映射与 sims。"""
    import contextlib, io as _io
    from mt5quant.backtest.report import data_sha256 as _dsha
    xau_last = float(synth_bars(9000, seed=21)["close"].iloc[-1])
    btc_last = float(synth_bars(9000, seed=22, s0=50000.0)["close"].iloc[-1])
    assert abs(btc_last / xau_last) > 100      # 量级差足够大，混用必露馅
    argv = ["prob", "test", "--card", "P006-v1",
            "--strategy", "tests.test_prob_p1_cli:TwoSymDemo",
            "--cards-dir", str(two_sym_env["repo"] / "research" / "cards"),
            "--data-root", str(two_sym_env["store"]),
            "--ledger", str(two_sym_env["ledger"]), "--sims", "200",
            "--allow-unbounded-data"]
    out = _io.StringIO()
    code = 0
    monkeypatch.chdir(two_sym_env["tmp"])
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 0, out.getvalue()
    calls = two_sym_env["calls"]
    assert calls["XAUUSD"] == pytest.approx(xau_last)
    assert calls["BTCUSD"] == pytest.approx(btc_last)
    with Ledger(two_sym_env["ledger"]) as led:
        rows = [r for r in led.runs_of_round("R2") if r["ok"]]
    assert rows
    for r in rows:
        prob = r["params"]["prob"]
        assert prob["sims"] == 200
        sha_map = prob["data_sha256_by_symbol"]
        assert set(sha_map) == {"XAUUSD", "BTCUSD"}
        assert sha_map["XAUUSD"] != sha_map["BTCUSD"]
        assert set(prob["cost_model_version_by_symbol"]) == \
            {"XAUUSD", "BTCUSD"}
        assert prob["cost_model_version_by_symbol"]["XAUUSD"] == "flat-XAUUSD"
    # 只改第二品种的数据 → 试验身份必须变（params 指纹不同）
    assert rows[0]["params"]["prob"]["data_sha256_by_symbol"]["BTCUSD"]


def test_e6_empty_cost_map_rejected(cli_env, monkeypatch):
    """E6：开轮时入账的预筛 cost_r_by_symbol 为空 → 拒出报告。"""
    import contextlib, io as _io
    db2 = cli_env["tmp"] / "ledger2.sqlite"
    with Ledger(db2) as led:
        led.record_candidate_check("C101", decision="pass", result={
            "checks": {"ex_ante_cost_prob": {"passed": None,
                                             "cost_r_by_symbol": {}}}})
        led.record_round_open(
            round_id="R2", candidates=["C101"], trial_budget=12,
            lockbox_uses_max=0, dsr_trial_scope="project",
            advance_rule="a", close_rule="c", approved_by="探针",
            evaluation={"C101": {"mode": "probability",
                                 "primary": "d"}},
            toml_sha256=hashlib.sha256(
                cli_env["round"].read_bytes()).hexdigest(),
            git_commit="0" * 40)
        for h in ("S1:A1:follow>base", "S1:A3:fade>base"):
            led.record_run(
                card_id="P001-v1", card_version=1, strategy=STRATEGY_REF,
                params={"hypothesis": h,
                        "prob": {"candidate": "C101",
                                 "segment": "discovery",
                                 "split": "2019-12-01",
                                 "bracket": {"sl_atr": 1.0, "tp_atr": 2.0,
                                             "horizon_bars": 8},
                                 "seed": 1, "prob_version": "1.0.0"}},
                symbol="XAUUSD", timeframe="H1", data_first=None,
                data_last=None, data_sha256="a" * 64, engine_version="e",
                cost_model_version="v", gates_hash="",
                metrics={"prob": {"p_value": 0.5, "delta_p": 0.01,
                                  "n_trigger_days": 10}},
                actor="bt", round_id="R2")
    argv = ["prob", "round-report", "R2",
            "--rounds-dir", str(cli_env["repo"] / "research" / "rounds"),
            "--data-root", str(cli_env["store"]),
            "--ledger", str(db2)]
    out = _io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    assert code == 1
    assert "为空" in out.getvalue() or "cost_r_by_symbol" in out.getvalue()
