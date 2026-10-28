"""P 线审核三的复现探针（2026-10-28）。

用法（仓库根目录）：
    ./.venv/Scripts/python.exe docs/reviews/probes/2026-10-28-pline-probes-03.py

全部只用合成数据、临时目录与临时台账，不读研究库、不看任何真实局面结局。
每个探针写的是修好之后应有的行为，在审核时的 main（0c76de9）上 R1–R6 均为 FAIL：
- R1 bt prob test 命令行跑通：退出码 0，台账每条假设一行
- R2 随机化匹配 ATR 分位（atr_pct_2000）：随机时点与原触发同档
- R3 持有上限：信号连续同向段不超过 K 根（不链式续持）
- R4 同一实例重复拟合：第二次 fit 不报错，模型与第一次相同
- R5 轮次报告推进规则：Holm 显著但 Δp 覆盖不了 3 倍成本的假设不得推进
- R6 Δp 检验剔除未成交的标签（skipped 不计入赢率）

任一探针不符合预期时退出码为 1。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "src"), str(REPO / "scripts")]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from mt5data.store import save_bars  # noqa: E402
from mt5quant.backtest.engine_a import atr_series, run_backtest  # noqa: E402
from mt5quant.prob.tables import ProbTable  # noqa: E402
from mt5quant.research.ledger import Ledger  # noqa: E402
from mt5quant.strategies.library.prob.features import atr_pct_2000  # noqa: E402
from mt5quant.strategies.library.prob.level_decision import (  # noqa: E402
    LevelDecisionS1Demo,
)
from mt5quant.strategies.library.prob.situations import Approach  # noqa: E402
from mt5quant.validation.prob_mode import (  # noqa: E402
    delta_p_test, randomize_triggers,
)

_spec = importlib.util.spec_from_file_location("bt_probe03", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
sys.modules["bt_probe03"] = BT
_spec.loader.exec_module(BT)

STRATEGY_REF = "mt5quant.strategies.library.prob.level_decision:LevelDecisionS1Demo"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))


def guarded(name):
    """探针本身抛异常 → 记 FAIL（异常类型写进说明），不中断其余探针。"""
    def deco(fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            tb = traceback.extract_tb(e.__traceback__)[-1]
            record(name, False, f"抛出 {type(e).__name__}: {e}"
                   f"（{Path(tb.filename).name}:{tb.lineno}）")
        return fn
    return deco


class FlatCosts:
    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.02

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.01

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        return 0.0


def synth_bars(n, *, seed, start="2018-01-01", s0=100.0, vol=None):
    rng = np.random.default_rng(seed)
    sig = np.full(n, 0.30) if vol is None else np.asarray(vol, dtype=float)
    close = s0 + np.cumsum(rng.normal(0.0, 1.0, n) * sig)
    openc = close + rng.normal(0.0, 0.05, n) * sig / 0.30
    high = np.maximum(openc, close) + np.abs(rng.normal(0, 0.12, n)) * sig / 0.30
    low = np.minimum(openc, close) - np.abs(rng.normal(0, 0.12, n)) * sig / 0.30
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq="1h"),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0})


class SmallGrid(LevelDecisionS1Demo):
    """事件密度高的合成配置（网格 2.0、K = 8）。"""

    LEVEL_GRID = {"XAUUSD": 2.0}
    HORIZON_BARS = 8
    APPROACH = Approach(lookback_bars=12, min_distance_atr=1.0, touch_atr=0.25)
    RETEST_WINDOW_BARS = 4


def strong_model(k):
    """手工高优势模型：S1×A3 赢率 0.9，保证频繁入场。"""
    rows = [("win", 1.9)] * 900 + [("loss", -1.05)] * 90 + [("time", 0.1)] * 10
    labels = pd.DataFrame({"situation": "S1", "action": "A3",
                           "outcome": [o for o, _ in rows],
                           "net_r": [r for _, r in rows],
                           "trend_sma200": "na", "atr_pct_2000": "na"})
    tbl = ProbTable.fit(labels, group_by=["trend_sma200", "atr_pct_2000"],
                        prior_strength=100, min_cell=100)
    return {"prob_version": "1.0.0", "prob_table": tbl.to_dict(),
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": k},
            "cost_r_by_situation_action": {"S1|A3": 0.001}}


# ---------------------------------------------------------------- R1
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
phenomenon = "合成数据探针"
mechanism = "合成数据探针"
should_fail_when = "合成数据探针"
falsified_if = "合成数据探针"
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
advance_rule = "Holm 校正后显著，且 Δp 点估计满足事前成本要求"
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
    subprocess.run(["git", "-c", "user.name=probe", "-c", "user.email=probe@example.invalid",
                    *args], cwd=repo, check=True, capture_output=True)


@guarded("R1 bt prob test 命令行")
def probe_cli():
    tmp = Path(tempfile.mkdtemp())
    repo = tmp / "repo"
    (repo / "research" / "cards").mkdir(parents=True)
    (repo / "research" / "rounds").mkdir(parents=True)
    (repo / "research" / "cards" / "P001-v1.toml").write_text(
        CARD.format(ref=STRATEGY_REF), encoding="utf-8")
    rpath = repo / "research" / "rounds" / "R2.toml"
    rpath.write_text(ROUND, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "probe")
    store = tmp / "store"
    save_bars(str(store), "XAUUSD", "H1", synth_bars(9000, seed=21))
    (store / "specs").mkdir(parents=True, exist_ok=True)
    (store / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    ledger_path = store / "research" / "ledger.sqlite"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    import hashlib
    with Ledger(ledger_path) as led:     # 修好后若要求"轮次已开启、候选预筛 pass"，这里已备齐
        led.record_candidate_check("C101", decision="pass", result={"probe": True})
        led.record_round_open(
            round_id="R2", candidates=["C101"], trial_budget=12, lockbox_uses_max=0,
            dsr_trial_scope="project", advance_rule="a", close_rule="c",
            approved_by="探针", evaluation={"C101": {"mode": "probability",
                                                    "primary": "delta_p_one_sided"}},
            toml_sha256=hashlib.sha256(rpath.read_bytes()).hexdigest(),
            git_commit="0" * 40)
    BT.build_costs = lambda *a, **k: (FlatCosts(), "flat-probe")
    argv = ["prob", "test", "--card", "P001-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(repo / "research" / "cards"),
            "--data-root", str(store), "--sims", "200"]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:          # argparse 拒绝参数时
        code = e.code
    with Ledger(ledger_path) as led:
        rows = [r for r in led.runs_of_round("R2") if r.get("ok")]
    tail = out.getvalue().strip().splitlines()[-1:] or [""]
    record("R1 bt prob test 命令行", code == 0 and len(rows) == 2,
           f"退出码 {code}（应 0），台账 {len(rows)} 行（应 2）；末行输出：{tail[0][:120]}")


# ---------------------------------------------------------------- R2
@guarded("R2 随机化匹配 ATR 分位")
def probe_atr_match():
    n = 9000
    vol = np.where((np.arange(n) // 1500) % 2 == 0, 0.15, 0.60)   # 高低波动交替
    bars = synth_bars(n, seed=5, vol=vol)
    pct = atr_pct_2000(atr_series(bars)).to_numpy()
    rng = np.random.default_rng(3)
    hi = np.flatnonzero(pct == "high")
    pick = np.sort(rng.choice(hi, size=200, replace=False))
    trig = pd.DataFrame({"time": bars["time"].iloc[pick].to_numpy(), "situation": "S1",
                         "action": "A3", "side": 1, "level": 100.0})
    out = randomize_triggers(trig, bars, seed=11)
    idx = pd.Index(bars["time"]).get_indexer(pd.to_datetime(out["time"]))
    share = float(np.mean(pct[idx] == "high"))
    record("R2 随机化匹配 ATR 分位", share >= 0.99,
           f"原触发全在 atr_pct_2000 高档；随机时点在高档的比例 {share:.2f}（应 ≥ 0.99）")


# ---------------------------------------------------------------- R3
def _chain_bars():
    """确定性序列：第 40 根触及 100 的阻力带（A3 做空），恰好 K = 8 根后
    第 48 根触及 102 的阻力带（又一个 A3 做空）。"""
    rows = ([(98.5, 99.0, 98.0, 98.5)] * 40 + [(99.5, 99.9, 99.0, 99.0)]
            + [(100.8, 101.3, 100.5, 101.0)] * 7 + [(101.5, 101.9, 101.2, 101.4)]
            + [(101.2, 101.4, 100.9, 101.1)] * 30)
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time", pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    return bars


def _longest_same_side_run(sig) -> int:
    best = cur = 0
    prev = 0
    for v in sig:
        cur = cur + 1 if (v != 0 and v == prev) else (1 if v != 0 else 0)
        best = max(best, cur)
        prev = v
    return best


@guarded("R3 持有上限不链式续持")
def probe_hold():
    k = SmallGrid.HORIZON_BARS
    strat = SmallGrid().with_model(strong_model(k))
    longest, over, n_trades = 0, 0, 0
    for bars in (_chain_bars(), synth_bars(4000, seed=42)):
        sig = strat.generate_signals(bars, {}).to_numpy()
        longest = max(longest, _longest_same_side_run(sig))
        res = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                           contract_size=100.0, timeframe="H1", volume_lots=1.0)
        pos = pd.Index(bars["time"])
        held = (pos.get_indexer(pd.to_datetime(res.trades["exit_time"]))
                - pos.get_indexer(pd.to_datetime(res.trades["entry_time"])))
        over += int(np.sum(held > k))
        n_trades += len(res.trades)
    record("R3 持有上限不链式续持", longest <= k and over == 0,
           f"同向信号最长连续 {longest} 根（应 ≤ {k}）；持有超过 {k} 根的交易 {over} 笔（应 0），"
           f"共 {n_trades} 笔")


# ---------------------------------------------------------------- R4
@guarded("R4 同一实例重复拟合")
def probe_refit():
    bars = synth_bars(4000, seed=42)
    s = SmallGrid()
    kw = dict(train_end=pd.Timestamp("2018-06-01"), purge_bars=10)
    meta = {"__fit__": {"sizes": {"XAUUSD": 100.0}}}
    m1 = s.fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta, **kw)
    m2 = s.fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()}, meta, **kw)
    record("R4 同一实例重复拟合", m1 == m2, "两次拟合的模型相同" if m1 == m2 else "两次拟合的模型不同")


# ---------------------------------------------------------------- R5
@guarded("R5 轮次报告的成本条件")
def probe_round_report():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "research" / "rounds").mkdir(parents=True)
    (tmp / "research" / "candidates").mkdir(parents=True)
    rpath = tmp / "research" / "rounds" / "R2.toml"
    rpath.write_text(ROUND.replace(
        'C101 = ["S1:A1:follow>base", "S1:A3:fade>base"]', 'C101 = ["S1:A3:fade>base"]'),
        encoding="utf-8")
    (tmp / "research" / "candidates" / "C101.precheck.json").write_text(json.dumps(
        {"candidate_id": "C101", "decision": "pass",
         "checks": {"ex_ante_cost_prob": {"passed": True,
                                          "cost_r_by_symbol": {"XAUUSD": 0.05}}}}),
        encoding="utf-8")
    store = tmp / "store"
    store.mkdir()
    db = tmp / "ledger.sqlite"
    with Ledger(db) as led:
        led.record_run(
            card_id="P001-v1", card_version=1, strategy=STRATEGY_REF, symbol="XAUUSD",
            timeframe="H1", data_first=None, data_last=None, data_sha256="a" * 64,
            engine_version="e", cost_model_version="v", gates_hash="",
            params={"hypothesis": "S1:A3:fade>base",
                    "prob": {"segment": "discovery", "split": "2019-12-01",
                             "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 24},
                             "seed": 1}},
            metrics={"prob": {"p_value": 1e-6, "delta_p": 0.005, "n_trigger_days": 5000,
                              "n_triggers": 5000, "real_win_rate": 0.405,
                              "null_win_rate": 0.40, "prob_version": "1.0.0"}},
            actor="bt", round_id="R2")
    argv = ["prob", "round-report", "R2", "--data-root", str(store), "--ledger", str(db),
            "--rounds-dir", str(tmp / "research" / "rounds")]
    cwd = os.getcwd()
    out = io.StringIO()
    try:
        os.chdir(tmp)
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:          # argparse 拒绝参数时
        code = e.code
    finally:
        os.chdir(cwd)
    reports = sorted((tmp / "docs" / "research").glob("round-R2-p1-*.md"))
    text = reports[-1].read_text(encoding="utf-8") if reports else ""
    row = next((ln for ln in text.splitlines() if "S1:A3:fade>base" in ln), "")
    advanced = "推进" in row and "不推进" not in row
    record("R5 轮次报告的成本条件", code == 0 and bool(row) and not advanced,
           f"Δp=+0.005、Holm p≈1e-6、成本 0.05R（需 ΔEV ≥ 0.15R，实际 0.015R）；"
           f"报告行：{row.strip()[:100] or '（无报告）'}（应判不推进）")


# ---------------------------------------------------------------- R6
@guarded("R6 Δp 检验剔除未成交")
def probe_skipped():
    days = pd.bdate_range("2016-01-04", periods=200)
    real = pd.DataFrame({"time": days + pd.Timedelta(hours=10),
                         "outcome": ["win", "skipped"] * 100})
    null = pd.DataFrame({"time": days + pd.Timedelta(hours=11),
                         "outcome": ["win", "loss"] * 100})
    res = delta_p_test(real, null, sims=200, seed=1)
    ok = abs(res["delta_p"] - 0.5) < 0.02
    record("R6 Δp 检验剔除未成交", ok,
           f"成交的触发全赢、零假设赢率 0.5；Δp={res['delta_p']:+.3f}（应 +0.500）")


# ---------------------------------------------------------------- 汇总
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}：{detail}")
sys.exit(0 if results and all(ok for _, ok, _ in results) else 1)
