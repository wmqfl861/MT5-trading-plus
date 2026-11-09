# -*- coding: utf-8 -*-
"""T150 closure-report segment means (offline, deterministic, report-layer only).

Purpose: the R3 closure report (T150) must list segmented means for the three
T150 segments 1994-2003 / 2004-2013 / 2014-2021-05 and the diagnostic segment
after 2021-05 (registered non-blind, never in the main test). The ledger V3
row (run#452, rowid 89) carries segmented_means in fixed 10-year buckets
(1990-1999 / ... / 2020-2029) — a different segmentation. Per the T150 task
instruction, the three segments are mechanically recomputed from the same
per-event returns (entry_time-year convention of validation/events.py
segmented_means), and the 10-year buckets are reproduced bit-exactly as an
equivalence check against the ledger row.

Assembly = exact run5 v3-replication.py pattern (same frozen inputs through
production functions; bt.py in-memory from the main-repo worktree, whose
bt.py blob == 065abf7 == the engine that wrote run#452). Identity is verified
against run#452 (read-only production ledger). The ledger is NOT modified:
the Ledger object opens a TEMP COPY only. Zero runs, zero gates, zero
reservations. Output: t150-closure-segments.json (+ stdout log captured by
the caller).
"""
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

import numpy as np
import pandas as pd

MAIN = Path(r"<workspace>/demo-next-20261105")
RESEARCH = Path(r"<workspace>/664d5945-068c-46aa-ba82-7141f134aa13")
LEDGER = RESEARCH / "data/research/ledger.sqlite"
DATA_ROOT = RESEARCH / "data_research"
CAL = MAIN / ".superpowers/h-new-candidates/n1-fomc-calendar/fomc_meetings.json"
CAL_SHA = "a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e88c1e76ec971c5"
POOL_START, POOL_END = "1986-01-02", "2021-05-17"       # frozen main-segment pool
BOUNDARY = pd.Timestamp("2021-05-17")                    # main/diagnostic boundary

def log(*a):
    print(*a, flush=True)

t_start = time.time()
log("T150 closure segment means (offline, report-layer; started",
    time.strftime("%H:%M:%S"), ")")

# ------------------------------------------------------------------ assembly
sys.path.insert(0, str(MAIN / "src"))
spec = importlib.util.spec_from_file_location("bt_t150", MAIN / "scripts/bt.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)
from mt5quant.research import cards                              # noqa: E402
from mt5quant.research import ledger as ledger_mod                # noqa: E402
from mt5quant.validation import events as vevents                 # noqa: E402
from mt5data.ext.merge import merged_research_series              # noqa: E402
from mt5data.sessions import load_session_policy                  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="t150-segments-"))
tmp_led = tmp / "ledger.sqlite"
shutil.copy2(LEDGER, tmp_led)
ledger = ledger_mod.Ledger(tmp_led, create=False)

card = cards.load_card(MAIN / "research/cards/H102.toml")
declared = bt._h_declared(ledger, card, actor="bt-gate", event_side=None)
args = types.SimpleNamespace(
    event_kind="fomc_calendar", fomc_calendar=str(CAL),
    fomc_calendar_sha256=CAL_SHA,
    event_days_start=POOL_START, event_days_end=POOL_END)
fomc_binding = bt._fomc_event_binding(args)
h_ctx = bt._load_h_research_inputs(
    ledger, card, candidate_id="C005", declared=declared,
    data_root=str(DATA_ROOT), end=None, allow_unbounded=False,
    load_plain=bt.load_series, load_merged=merged_research_series)
symbol = card.symbols[0]
bars = h_ctx["bars"][symbol]
sizes = {sym: bt.resolve_contract_size(str(DATA_ROOT), sym, None) for sym in card.symbols}
sess_policy = load_session_policy(str(bt.SESSIONS_PATH))
sess_sha = bt.sessions_sha256(str(bt.SESSIONS_PATH))
costs, cost_version = bt.build_costs(
    str(DATA_ROOT), symbol, 0.1, 0.0, sessions_sha=sess_sha,
    research_store=True, swap_ref_price=float(bars["close"].iloc[-1]))
events, days, side_fn = bt._h_event_setup(
    h_ctx, symbol, bars, "fomc_calendar", {}, fomc=fomc_binding)
dsha = bt.data_sha256(bars)
log(f"assembly: bars={len(bars)} events={len(events)} pool_days={len(days)}")

# ------------------------------------------- identity vs run#452 (read-only)
con = sqlite3.connect(f"file:{LEDGER.as_posix()}?mode=ro", uri=True)
r452 = con.execute("SELECT data_sha256, cost_model_version, strategy_sha,"
                   " params_json, gates_hash FROM runs WHERE run_id=452").fetchone()
v3 = json.loads(con.execute(
    "SELECT details_json FROM gates WHERE run_id=452 AND gate='V3'").fetchone()[0])
con.close()
ident = {
    "data_sha256": r452[0] == dsha,
    "cost_model_version": r452[1] == cost_version,
    "params": r452[3] == "{}",
    "strategy_sha_prefix": r452[2].startswith("8742cd4f"),
}
log("identity vs run#452:", ident)
assert all(ident.values()), "assembly inputs differ from run#452!"

# --------------------------------------- observed frame (main segment, 213)
obs = vevents.event_returns(bars, events, side_fn=side_fn, costs=costs,
                            symbol=symbol, contract_size=sizes[symbol])
observed_mean = float(pd.to_numeric(obs["net"]).mean())
led = v3["primary"]
log(f"main segment: n={len(obs)} dropped={obs.attrs['dropped']} "
    f"observed_mean={observed_mean!r}")
assert len(obs) == led["n_events"] == 213
assert observed_mean == led["observed_mean"], "observed_mean drift vs ledger!"

# equivalence check: reproduce the ledger 10-year buckets bit-exactly
ten = {k: float(v) for k, v in
       vevents.segmented_means(obs, years=10).items()}
led_ten = {k: float(v) for k, v in
           v3["diagnostics"]["segmented_means"]["segments"].items()}
log("10-year buckets recomputed == ledger:", ten == led_ten, ten)
assert ten == led_ten, "10-year bucket reproduction differs from ledger V3 row!"

# --------------------------------------------- T150 three-segment recompute
# Convention: same as validation/events.py segmented_means — per-event net
# grouped by entry_time year; T150 boundaries 1994-2003 / 2004-2013 /
# 2014-2021-05 (main-segment pool ends 2021-05-17 exclusive).
entry = pd.DatetimeIndex(pd.to_datetime(obs["entry_time"]))
nets = pd.to_numeric(obs["net"])
def seg(lo_year, hi_exclusive_ts):
    m = (entry.year >= lo_year) & (entry < hi_exclusive_ts)
    return {"n": int(m.sum()),
            "mean": (float(nets[m].mean()) if int(m.sum()) else None)}
assert int(entry.year.min()) == 1994 and int(entry.year.max()) <= 2021
t150 = {
    "1994-2003": seg(1994, pd.Timestamp("2004-01-01")),
    "2004-2013": seg(2004, pd.Timestamp("2014-01-01")),
    "2014-2021-05": seg(2014, BOUNDARY),
}
uncovered = int(len(obs) - sum(s["n"] for s in t150.values()))
log("T150 three segments:", json.dumps(t150), "; events outside segments:",
    uncovered)
assert uncovered == 0

# ----------------------- diagnostic segment after 2021-05-17 (non-blind)
# Registered non-blind: R3.toml advance_rule — "主段外（MT5 段与 FRED
# 2021-05-17 之后段）只作诊断、标非盲". Same calendar artifact + same
# assembly; pool extended past the boundary; events with announcement day
# (exit_time) >= 2021-05-17. event_returns drops windows past the data end
# (dropped counted, never extrapolated).
raw = h_ctx["calendars"][symbol]
cal = vevents.trading_calendar(raw)
all_days = pd.DatetimeIndex(cal.day)
ext_days = all_days[(all_days >= pd.Timestamp(POOL_START))
                    & (all_days < pd.Timestamp("2026-01-01"))]
ext_events, ext_report = vevents.fomc_calendar_events(str(CAL), CAL_SHA, ext_days)
post = [e for e in ext_events if e.exit_time >= BOUNDARY]
post_frame = vevents.event_returns(bars, post, side_fn=side_fn, costs=costs,
                                   symbol=symbol, contract_size=sizes[symbol])
post_n = int(len(post_frame))
post_mean = (float(pd.to_numeric(post_frame["net"]).mean())
             if post_n else None)
log(f"diagnostic post-boundary: calendar post events={len(post)} "
    f"resolved={post_n} dropped_by_returns={post_frame.attrs['dropped']} "
    f"mean={post_mean!r}")
first_post = post_frame["exit_time"].dt.strftime("%Y-%m-%d").min() if post_n else None
last_post = post_frame["exit_time"].dt.strftime("%Y-%m-%d").max() if post_n else None
by_sub_segment = {}
if post_n:
    pexit = pd.DatetimeIndex(pd.to_datetime(post_frame["exit_time"]))
    pnets = pd.to_numeric(post_frame["net"])
    fred_tail = np.asarray(pexit <= pd.Timestamp("2021-07-01"))  # FRED tail
    by_sub_segment["fred_2021-05-17..2021-07-01"] = {
        "n": int(fred_tail.sum()),
        "mean": (float(pnets[fred_tail].mean()) if int(fred_tail.sum()) else None)}
    mt5 = ~fred_tail
    by_sub_segment["mt5_2021-07-02..2025-10-23"] = {
        "n": int(mt5.sum()),
        "mean": (float(pnets[mt5].mean()) if int(mt5.sum()) else None)}

out = {
    "purpose": "T150 closure report segment means; report-layer only, "
               "ledger untouched; identity verified vs run#452",
    "identity_vs_run452": ident,
    "main_segment": {
        "n": int(len(obs)), "dropped": int(obs.attrs["dropped"]),
        "observed_mean": observed_mean,
        "ledger_observed_mean": led["observed_mean"],
        "equal_bitexact": observed_mean == led["observed_mean"],
        "ten_year_buckets_reproduced": ten,
        "ledger_ten_year_buckets": led_ten,
        "ten_year_equal_bitexact": ten == led_ten,
        "t150_segments": t150,
        "convention": "per-event net grouped by entry_time year (engine "
                      "segmented_means convention); T150 boundaries "
                      "1994-2003 / 2004-2013 / 2014-2021-05 (pool end "
                      "2021-05-17 exclusive)",
    },
    "diagnostic_post_boundary": {
        "registered_status": "非盲诊断段（R3.toml advance_rule：主段外只作"
                             "诊断、标非盲；不进主检验、不进 N、不进判定）",
        "calendar_post_events": len(post), "resolved": post_n,
        "dropped_by_returns": int(post_frame.attrs["dropped"]),
        "mean": post_mean,
        "first_announcement": first_post, "last_announcement": last_post,
        "by_sub_segment": by_sub_segment,
        "ext_report": ext_report,
    },
    "elapsed_s": round(time.time() - t_start, 1),
}
dest = Path(__file__).with_name("t150-closure-segments.json")
dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
log("written:", dest)
