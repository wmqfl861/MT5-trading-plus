"""P 线审核四的复现探针（2026-10-28）。

用法（仓库根目录）：
    ./.venv/Scripts/python.exe docs/reviews/probes/2026-10-28-pline-probes-04.py

只用合成数据、临时 git 仓库与临时台账，不读研究库、不看任何真实局面结局。
写的是修好之后应有的行为；在审核时的 main（4228926）上 R7 为 FAIL：
- R7 P2 决策卡在 bt screen 里跑通：
  - 卡按蓝图附录 A 写完整的 [prob]（含特征、收缩、门槛等决策配置），与策略类常量一致 → 退出码 0，
    台账一行成功运行，拟合窗止于分界；
  - 同一张卡把 lb_z 改成与策略类不一致 → 拒绝运行（退出码非 0）。

任一探针不符合预期时退出码为 1。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
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
from mt5quant.research.ledger import Ledger  # noqa: E402

_spec = importlib.util.spec_from_file_location("bt_probe04", REPO / "scripts" / "bt.py")
BT = importlib.util.module_from_spec(_spec)
sys.modules["bt_probe04"] = BT
_spec.loader.exec_module(BT)

STRATEGY_REF = "mt5quant.strategies.library.prob.level_decision:LevelDecisionS1Demo"
SPLIT = "2019-12-01"
results: list[tuple[str, bool, str]] = []

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


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=probe", "-c", "user.email=probe@example.invalid",
                    *args], cwd=repo, check=True, capture_output=True)


def _screen(lb_z: float):
    """临时仓库 + 跨分界的合成数据 + 已开启的 R3，跑一次 bt screen。"""
    tmp = Path(tempfile.mkdtemp())
    repo = tmp / "repo"
    (repo / "research" / "cards").mkdir(parents=True)
    (repo / "research" / "rounds").mkdir(parents=True)
    (repo / "research" / "cards" / "P002-v1.toml").write_text(
        CARD.format(ref=STRATEGY_REF, lb_z=lb_z), encoding="utf-8")
    rpath = repo / "research" / "rounds" / "R3.toml"
    rpath.write_text(ROUND, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "probe")
    rng = np.random.default_rng(21)
    n = 17000                                   # 2018-06 起，跨过 2019-12-01 分界
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    op = close + rng.normal(0, 0.05, n)
    bars = pd.DataFrame({
        "time": pd.date_range("2018-06-01", periods=n, freq="1h"), "open": op,
        "high": np.maximum(op, close) + np.abs(rng.normal(0, 0.12, n)),
        "low": np.minimum(op, close) - np.abs(rng.normal(0, 0.12, n)),
        "close": close, "tick_volume": 10, "spread": 5, "real_volume": 0})
    store = tmp / "store"
    save_bars(str(store), "XAUUSD", "H1", bars)
    (store / "specs").mkdir(parents=True, exist_ok=True)
    (store / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    led_path = tmp / "ledger.sqlite"
    with Ledger(led_path) as led:
        led.record_candidate_check("C101", decision="pass", result={"probe": True})
        led.record_round_open(
            round_id="R3", candidates=["C101"], trial_budget=4, lockbox_uses_max=1,
            dsr_trial_scope="project", advance_rule="a", close_rule="c", approved_by="探针",
            evaluation={"C101": {"mode": "standard", "primary": "gates_v3"}},
            toml_sha256=hashlib.sha256(rpath.read_bytes()).hexdigest(), git_commit="0" * 40)
    argv = ["screen", "--card", "P002-v1", "--strategy", STRATEGY_REF,
            "--cards-dir", str(repo / "research" / "cards"), "--data-root", str(store),
            "--ledger", str(led_path), "--costs", "zero", "--volume-lots", "1.0"]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = BT.main(argv)
    except SystemExit as e:
        code = e.code
    with Ledger(led_path) as led:
        rows = [r for r in led.runs_of_round("R3") if r.get("ok")]
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    return code, rows, (lines[-1] if lines else "")


try:
    code_ok, rows, last = _screen(1.0)
    fit_end = str(((rows[0].get("metrics") or {}).get("prob") or {}).get("fit_end", "")) if rows else ""
    good = code_ok == 0 and len(rows) == 1 and fit_end[:10] <= SPLIT and fit_end != ""
    code_bad, _, last_bad = _screen(1.5)
    ok = good and code_bad not in (0, None)
    results.append(("R7 P2 决策卡跑通 screen", ok,
                    f"一致的卡：退出码 {code_ok}（应 0），成功运行 {len(rows)} 行（应 1），"
                    f"拟合窗止于 {fit_end[:10] or '—'}（应 ≤ {SPLIT}）；末行输出：{last[:90]}"
                    f"｜lb_z 不一致的卡：退出码 {code_bad}（应非 0）"))
except Exception as e:  # noqa: BLE001
    tb = traceback.extract_tb(e.__traceback__)[-1]
    results.append(("R7 P2 决策卡跑通 screen", False,
                    f"抛出 {type(e).__name__}: {e}（{Path(tb.filename).name}:{tb.lineno}）"))

for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}：{detail}")
sys.exit(0 if results and all(ok for _, ok, _ in results) else 1)
