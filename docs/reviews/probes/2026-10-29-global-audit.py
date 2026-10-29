"""Global audit observations at 7d99a4f (2026-10-29).

Run from the repository root with the project virtual environment. This script
uses synthetic bars, fake brokers, temporary SQLite databases and a temporary
git repository. It never connects to MT5 or reads a real research data store.
Its JSON records observed behavior, not a passing regression-test verdict.
The original three P-line probes and their fixtures remain unchanged.
"""
from __future__ import annotations
import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "src"), str(REPO / "scripts"), str(REPO / "tests"), str(REPO)]
import pandas as pd
from test_prob_labels import FlatCosts, ScriptedStrategy, flat_bars
from mt5quant.strategies.base import ExitRules
from mt5quant.validation.robustness import BacktestSpec, run_engine, ScaledCosts
from mt5quant.backtest.engine_a import run_backtest
from mt5data.sessions import SessionPolicy
from mt5quant.validation.events import EventWindow, event_returns
from mt5quant.costs.swap import SwapRule
from tests.fakes_live import FakeBroker, mk_decision
from mt5live.executor import ExecutionAdapter
from mt5data.store import save_bars
from mt5data.lockbox import write_global_cutoff, research_bars
from mt5data.ext.merge import merged_research_series
from mt5quant.research.ledger import Ledger
from mt5quant.validation.gates import begin_lockbox
from mt5quant.validation.prob_mode import delta_p_test
from mt5quant.strategies.library.prob.level_decision import LevelDecisionV1
import prob_calibration as CAL
import run_backtest as RUN
import bt as BT

out = {}
bars = flat_bars(80)
costs = FlatCosts(half=0, slip=0, commission=0, overnight=0)
script = [0] * 80
script[40] = 1
strategy = ScriptedStrategy(script, ExitRules())
policy = SessionPolicy(forbidden_hours_ny={"XAUUSD": frozenset(range(24))})
baseline = run_backtest(strategy, bars, costs, symbol="XAUUSD", params={},
                        contract_size=100, volume_lots=1, timeframe="H1", session_policy=policy)
replay = run_engine(BacktestSpec(strategy, "XAUUSD", "H1", 100), bars, costs, {})
out["session_policy"] = {"base_trades": len(baseline.trades), "gate_replay_trades": len(replay.trades)}

swap_costs = FlatCosts(half=0, slip=0, commission=0, overnight=-10)
swap_costs.swap = SimpleNamespace(rule=lambda symbol: SwapRule(-10, -5))
e = EventWindow("synthetic", "over_night", pd.Timestamp("2024-01-01 20:00"), pd.Timestamp("2024-01-02 23:00"))
kw = dict(side_fn=lambda event: 1, symbol="XAUUSD", contract_size=100)
a = event_returns(bars, [e], costs=swap_costs, **kw)
b = event_returns(bars, [e], costs=ScaledCosts(swap_costs, 1.5), **kw)
out["event_swap_scaling"] = {"base": float(a.overnight.iloc[0]), "scaled": float(b.overnight.iloc[0])}

broker = FakeBroker(partial_fill=0.5)
broker.seed_position(side="buy", volume=0.02, magic=777)
adapter = ExecutionAdapter(broker, broker, broker, symbol="XAUUSD", magic=777)
adapter.execute(mk_decision(signal=-1, volume_lots=0.02))
out["partial_reverse"] = {"open_orders_after_partial_close": len(broker.market_orders),
                          "positions": [(p.side, p.volume) for p in broker.positions("XAUUSD")]}
flat = FakeBroker()
flat.seed_position(side="buy", volume=0.01, magic=777)
flat.seed_position(side="sell", volume=0.01, magic=777)
rep = ExecutionAdapter(flat, flat, flat, symbol="XAUUSD", magic=777).execute(mk_decision(signal=0, volume_lots=None))
out["hedged_flatten"] = {"action": rep.action, "remaining_positions": len(flat.positions("XAUUSD"))}

with tempfile.TemporaryDirectory(prefix="mt5-audit-") as tmp:
    root = Path(tmp)
    save_bars(root, "XAUUSD", "H1", bars)
    cutoff = pd.Timestamp("2024-01-02")
    write_global_cutoff(root, cutoff.to_pydatetime(), note="synthetic audit")
    regular = research_bars(root, "XAUUSD", "H1")
    merged, *_ = merged_research_series(root, "XAUUSD", "H1")
    out["merged_cutoff"] = {"regular_max": str(regular.time.max()), "merged_max": str(merged.time.max()),
                             "merged_rows_at_or_after_cutoff": int((merged.time >= cutoff).sum())}
    db = root / "ledger.sqlite"
    with Ledger(db) as ledger:
        ledger.lockbox_budget(cutoff, max_uses=1)
    barrier = threading.Barrier(2)
    def start(card):
        with Ledger(db) as ledger:
            ledger.gate_passed = lambda *a, **k: True
            original_budget = ledger.lockbox_budget
            def sync_budget(*a, **k):
                value = original_budget(*a, **k)
                barrier.wait(timeout=15)
                return value
            ledger.lockbox_budget = sync_budget
            begin_lockbox(ledger, card, approved_by="synthetic", cutoff=cutoff)
        return card
    with ThreadPoolExecutor(max_workers=2) as pool:
        starts = list(pool.map(start, ["SYN-A", "SYN-B"]))
    with Ledger(db) as ledger:
        out["lockbox_budget_race"] = {"started": starts, "budget": ledger.lockbox_budget(cutoff)}

days = pd.DataFrame({"day": pd.date_range("2000-01-01", periods=2000).date, "winrate": 0.4})
real = CAL._sample_labels(days, 4000, 7)
null = CAL._sample_labels(days, 4000, 8)
res = delta_p_test(real, null, sims=20, seed=9)
out["calibration_day_count"] = {"nominal": 4000, "real": res["n_days_real"], "null": res["n_days_null"]}

cluster_bars = flat_bars(2500)
triggers = pd.DataFrame({"time": cluster_bars.time.iloc[[100, 500]].to_numpy(),
                         "situation": "S1", "action": "A3", "side": 1, "level": 100.0})
seen = []
def capture_labels(bars, trig, costs, **kw):
    seen.append(list(pd.to_datetime(trig.time)))
    return trig.assign(outcome="win", net_r=1.0)
with contextlib.ExitStack() as stack:
    replacements = [
        (RUN, "load_series", lambda *a, **k: cluster_bars),
        (CAL, "_costs_and_sizes", lambda: (costs, policy, 100, "synthetic")),
        (CAL, "CLUSTER_SHIFT_WEEKS", (3,)), (CAL, "CLUSTER_SEEDS", 1),
        (CAL, "label_triggers", capture_labels),
        (CAL, "delta_p_test", lambda *a, **k: {"p_value": 1.0}),
    ]
    for obj, name, value in replacements:
        stack.enter_context(patch.object(obj, name, value))
    stack.enter_context(patch("mt5quant.prob.counting.level_rows_for_situation", return_value=[]))
    stack.enter_context(patch("mt5quant.strategies.library.prob.situations.find_level_events", return_value=[]))
    stack.enter_context(patch("mt5quant.strategies.library.prob.actions.action_triggers", return_value=triggers))
    CAL.phase_cluster(0, 1, "synthetic")
out["cluster_null_uses_original"] = sorted(seen[1]) == sorted(pd.to_datetime(triggers.time))

class ChangedBracket(LevelDecisionV1):
    SL_ATR = 2.0
    TP_ATR = 3.0
changed = ChangedBracket()
out["strategy_bracket"] = {"declared": [changed.SL_ATR, changed.TP_ATR],
                           "actual_exit_rules": [changed.exit_rules.stop_loss_atr, changed.exit_rules.take_profit_atr]}
split = pd.Timestamp("2024-07-01")
times = pd.date_range(split - pd.Timedelta(days=200), split + pd.Timedelta(days=10), freq="h")
times = times[times.weekday < 5]
sample = pd.DataFrame({"time": times})
sliced = BT._slice_validation_eval(SimpleNamespace(segment="validation", timeframe="H1"),
                                   SimpleNamespace(segments={"split": str(split)}),
                                   {"X": sample}, lookback_bars=2100)
out["validation_warmup"] = {"requested_bars": 2100, "actual_bars": int((sliced["X"].time < split).sum())}

from mt5quant.costs.spread import SpreadModel, SpreadStat
from mt5quant.costs.slippage import SlippageModel
from mt5quant.costs.commission import CommissionModel
from mt5quant.costs.volatility import hourly_stats
from mt5quant.costs.audit import cost_heatmap
vol_bars = flat_bars(20, high=105, low=95)
vol_bars["time"] = pd.date_range("2024-01-01 22:00", periods=20, freq="D")
spread = SpreadModel({"X": {17: SpreadStat(1, 1, 1, 100), 22: SpreadStat(.01, .01, .01, 100)}},
                     {"X": .01}, mode="absolute")
heatmap = cost_heatmap(spread, SlippageModel(spread, spread_fraction=0), CommissionModel(),
                      {"X": 100}, {("X", "H1"): hourly_stats(vol_bars, "H1")}, ["X"], ["H1"])
out["cost_heatmap_timezone"] = {"actual_ratio": float(heatmap.iloc[0, 0]), "expected_ratio": .1,
                                "bar_hour_utc": 22, "bar_hour_ny": 17}

from mt5quant.research import cards as CARDS
import export_research_store as EXPORT
with tempfile.TemporaryDirectory(prefix="mt5-audit-boundary-") as tmp:
    root = Path(tmp)
    save_bars(root, "XAUUSD", "H1", bars)
    (root / "bars" / "XAUUSD" / "H1" / "meta.json").unlink()
    out["verify_orphan_parquet"] = {"violations": EXPORT.verify_research_store(str(root), pd.Timestamp("2024-01-02"))}
    repo = root / "test-repo"
    repo.mkdir()
    cardfile = repo / "research" / "cards" / "H001.toml"
    cardfile.parent.mkdir(parents=True)
    cardfile.write_text("id='H001'\n", encoding="utf-8")
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Synthetic Auditor", "-c", "user.email=audit@example.invalid", "commit", "-qm", "fixture")
    cardfile.write_text("id='H001'\nversion=2\n", encoding="utf-8")
    cwd = Path.cwd()
    try:
        os.chdir(repo)
        relative = Path("research/cards/H001.toml")
        out["git_relative_path"] = {"relative_committed": CARDS.committed_at(relative),
                                    "absolute_committed": CARDS.committed_at(cardfile),
                                    "relative_clean": CARDS.worktree_clean(relative),
                                    "absolute_clean": CARDS.worktree_clean(cardfile)}
    finally:
        os.chdir(cwd)

import mt5quant.strategies.library.prob.level_decision as LD
cross_bars = flat_bars(60)
cross_split = cross_bars.time.iloc[40]
decision = LD.LevelDecisionV1()
decision._table = SimpleNamespace(lookup=lambda *a: object())
decision._cost_r = {("S1", "A3"): 0.0}
one_trig = pd.DataFrame({"bar": [39], "situation": ["S1"], "action": ["A3"], "side": [1]})
with contextlib.ExitStack() as stack:
    for name, value in [
        ("atr_series", lambda b: pd.Series(1., index=b.index)),
        ("trend_sma200", lambda b: pd.Series("up", index=b.index)),
        ("atr_pct_2000", lambda a: pd.Series("high", index=a.index)),
        ("level_rows_for_situation", lambda *a, **k: []),
        ("find_level_events", lambda *a, **k: []),
        ("action_triggers", lambda *a, **k: one_trig),
        ("ev_lower", lambda *a, **k: 1.),
    ]:
        stack.enter_context(patch.object(LD, name, value))
    sig = BT._ZeroedBeforeSplit(decision, cross_split).generate_signals(cross_bars, {})
out["validation_trigger_carry"] = {"triggers_after_split": 0, "signal_at_split": int(sig.iloc[40])}
print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
