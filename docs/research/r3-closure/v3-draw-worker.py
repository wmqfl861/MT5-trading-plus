# -*- coding: utf-8 -*-
"""Parallel draw worker for the run5 V3 offline replication.

Computes null-draw means for k in [k0, k1) with the IDENTICAL per-draw
computation as production events.randomization_test (same seeded child draws:
master = default_rng(seed); child_seeds = master.integers(0, 2**63-1, size=sims);
per k: randomize_event_times(events, days, seed=child_seeds[k]) then
event_returns(...).net.mean). Appends {"k":k,"mean":m} lines to its own shard
file (resume-safe: skips ks already present). Usage: v3-draw-worker.py K0 K1.
"""
import importlib.util
import json
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np
import pandas as pd

MAIN = Path(r"<workspace>/demo-next-20261105")
RESEARCH = Path(r"<workspace>/664d5945-068c-46aa-ba82-7141f134aa13")
OUT = RESEARCH / ".superpowers/sdd/2026-11-07-r3-main-test-run5"
DATA_ROOT = RESEARCH / "data_research"
SIMS = 1999
SEED = 10640155159524715549

k0, k1 = int(sys.argv[1]), int(sys.argv[2])
shard = OUT / f"v3-draws-shard-{k0:04d}-{k1:04d}.jsonl"

def log(*a):
    print(f"[worker {k0}-{k1}]", *a, flush=True)

sys.path.insert(0, str(MAIN / "src"))
spec = importlib.util.spec_from_file_location("bt_w", MAIN / "scripts/bt.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)
from mt5quant.research import cards  # noqa: E402
from mt5quant.research import ledger as ledger_mod  # noqa: E402
from mt5quant.validation import events as vevents  # noqa: E402
from mt5data.ext.merge import merged_research_series  # noqa: E402
from mt5data.sessions import load_session_policy  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix=f"run5-w{k0}-"))
shutil.copy2(RESEARCH / "data/research/ledger.sqlite", tmp / "ledger.sqlite")
ledger = ledger_mod.Ledger(tmp / "ledger.sqlite", create=False)
card = cards.load_card(MAIN / "research/cards/H102.toml")
declared = bt._h_declared(ledger, card, actor="bt-gate", event_side=None)
args = types.SimpleNamespace(
    event_kind="fomc_calendar",
    fomc_calendar=str(MAIN / ".superpowers/h-new-candidates/n1-fomc-calendar/fomc_meetings.json"),
    fomc_calendar_sha256="a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e88c1e76ec971c5",
    event_days_start="1986-01-02", event_days_end="2021-05-17")
fomc_binding = bt._fomc_event_binding(args)
h_ctx = bt._load_h_research_inputs(
    ledger, card, candidate_id="C005", declared=declared,
    data_root=str(DATA_ROOT), end=None, allow_unbounded=False,
    load_plain=bt.load_series, load_merged=merged_research_series)
symbol = card.symbols[0]
bars = h_ctx["bars"][symbol]
sizes = {sym: bt.resolve_contract_size(str(DATA_ROOT), sym, None) for sym in card.symbols}
sess_sha = bt.sessions_sha256(str(bt.SESSIONS_PATH))
costs, _cv = bt.build_costs(
    str(DATA_ROOT), symbol, 0.1, 0.0, sessions_sha=sess_sha,
    research_store=True, swap_ref_price=float(bars["close"].iloc[-1]))
events, days, side_fn = bt._h_event_setup(
    h_ctx, symbol, bars, "fomc_calendar", {}, fomc=fomc_binding)
ledger.conn.close()
shutil.rmtree(tmp, ignore_errors=True)

master = np.random.default_rng(int(SEED))
child_seeds = master.integers(0, 2**63 - 1, size=int(SIMS))

done: set[int] = set()
if shard.exists():
    for line in shard.read_text(encoding="utf-8").splitlines():
        if line.strip():
            done.add(json.loads(line)["k"])
todo = [k for k in range(k0, k1) if k not in done]
log(f"assembly done ({len(bars)} bars, {len(events)} events); shard {shard.name}: "
    f"{len(done)} done, {len(todo)} todo")
t0 = time.time()
with open(shard, "a", encoding="utf-8") as fh:
    for n, k in enumerate(todo, 1):
        cs = int(child_seeds[k])
        rand = vevents.randomize_event_times(events, days, seed=cs)
        frame = vevents.event_returns(bars, rand, side_fn=side_fn, costs=costs,
                                      symbol=symbol, contract_size=sizes[symbol])
        mean = (float(pd.to_numeric(frame["net"]).mean())
                if len(frame) else float("nan"))
        fh.write(json.dumps({"k": k, "mean": None if np.isnan(mean) else mean}) + "\n")
        if n % 10 == 0 or n == len(todo):
            fh.flush()
            el = time.time() - t0
            log(f"{n}/{len(todo)} draws ({el:.0f}s, {el/n:.2f}s/draw, ETA {(len(todo)-n)*el/n/60:.0f}m)")
log("worker complete")
