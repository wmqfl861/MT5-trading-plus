# -*- coding: utf-8 -*-
"""Offline deterministic replication of the interrupted run5 V3 evaluation,
plus the A1-D (15/213 month-end subset) and E7 (per-event nights) obligations.

Background: run5 (2026-11-07 08:47:13 +08) posted run#451 and gates rows 79-83
(G0..G4) and was computing the V3 event branch (randomization, 1999 draws,
seed 10640155159524715549 = vstats.rng_seed('H102')) when the process was
externally terminated (~09:36-09:47 +08; harness killed the background shell
wrapper, exit 137). No V3 gate row exists in the ledger. The ledger is the
authoritative record and is NOT modified by this script.

The randomization test is fully deterministic (seeded child draws over the
frozen inputs), so replicating the identical production computation here
yields exactly the numbers the engine would have written. This script:

 1. re-assembles the same frozen inputs through production functions
    (bt.py in-memory from main-repo HEAD 72d1014 worktree; ledger TEMP COPY;
    _load_h_research_inputs -> merged fred series; build_costs research-store
    cache; _h_event_setup with the frozen FOMC artifact binding);
 2. verifies assembly identity against run#451's recorded data_sha256 /
    cost_model_version / strategy fingerprint (read-only production ledger);
 3. replicates randomization_test draws with IDENTICAL seeding and per-draw
    computation (production function body, chunk-executed with a JSONL
    checkpoint so an external kill cannot lose progress; aggregation formula
    copied verbatim);
 4. runs the rest of _v3_gate_evaluate's event branch through production
    functions (cost x1.5 veto via vrob.ScaledCosts, not-applicable vetoes,
    v3_event_diagnostics, evaluate_v3 verdict, n_composition);
 5. extracts A1-D subset stats and E7 per-event nights from the observed frame.

Outputs (this directory): v3-offline-replication.json, a1d-e7-obligations.txt,
v3-draws-checkpoint.jsonl (resumable).
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
OUT = RESEARCH / ".superpowers/sdd/2026-11-07-r3-main-test-run5"
DATA_ROOT = RESEARCH / "data_research"
SIMS = 1999
SEED = 10640155159524715549          # vstats.rng_seed("H102") — verified == G4 seed
MONTH_END_15 = ["1996-01-31", "1997-09-30", "1998-03-31", "1999-06-30",
                "2001-01-31", "2004-06-30", "2005-06-30", "2006-01-31",
                "2007-01-31", "2007-10-31", "2008-04-30", "2013-07-31",
                "2014-04-30", "2018-01-31", "2019-07-31"]

def log(*a):
    print(*a, flush=True)

log("V3 offline deterministic replication + A1-D + E7 (started", time.strftime("%H:%M:%S"), ")")
log("=" * 96)

# ------------------------------------------------------------------ assembly
sys.path.insert(0, str(MAIN / "src"))
spec = importlib.util.spec_from_file_location("bt_rep", MAIN / "scripts/bt.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)
from mt5quant.research import cards  # noqa: E402
from mt5quant.research import ledger as ledger_mod  # noqa: E402
from mt5quant.validation import events as vevents  # noqa: E402
from mt5quant.validation import gates as vgate  # noqa: E402
from mt5quant.validation import robustness as vrob  # noqa: E402
from mt5quant.validation import stats as vstats  # noqa: E402
from mt5data.ext.merge import merged_research_series  # noqa: E402
from mt5data.sessions import load_session_policy  # noqa: E402
from mt5data.quality import read_known_gaps  # noqa: E402

assert vstats.rng_seed("H102") == SEED, "seed derivation mismatch"

tmp = Path(tempfile.mkdtemp(prefix="run5-v3rep-"))
tmp_led = tmp / "ledger.sqlite"
shutil.copy2(LEDGER, tmp_led)
ledger = ledger_mod.Ledger(tmp_led, create=False)

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
sess_policy = load_session_policy(str(bt.SESSIONS_PATH))
sess_sha = bt.sessions_sha256(str(bt.SESSIONS_PATH))
costs, cost_version = bt.build_costs(
    str(DATA_ROOT), symbol, 0.1, 0.0, sessions_sha=sess_sha,
    research_store=True, swap_ref_price=float(bars["close"].iloc[-1]))
events, days, side_fn = bt._h_event_setup(
    h_ctx, symbol, bars, "fomc_calendar", {}, fomc=fomc_binding)
cfg, gsha = vgate.load_gates()
dsha = bt.data_sha256(bars)
log(f"assembly: bars={len(bars)} merged={h_ctx['merged']} events={len(events)} "
    f"pool_days={len(days)} contract_size={sizes[symbol]}")
log(f"identity : cost_model_version={cost_version}")
log(f"          data_sha256={dsha}")
log(f"          gates_sha256={gsha}")

# --------------------------------------------- identity vs run#451 (read-only)
con = sqlite3.connect(f"file:{LEDGER.as_posix()}?mode=ro", uri=True)
r451 = con.execute("SELECT data_sha256, cost_model_version, strategy_sha, params_json"
                   " FROM runs WHERE run_id=451").fetchone()
g0det = json.loads(con.execute(
    "SELECT details_json FROM gates WHERE run_id=451 AND gate='G0'").fetchone()[0])
con.close()
ident = {"data_sha256": r451[0] == dsha, "cost_model_version": r451[1] == cost_version,
         "params": r451[3] == "{}"}
log("identity vs run#451:", ident, "; strategy_sha =", r451[2][:24], "(8742cd4f... prefix:",
    r451[2].startswith("8742cd4f"), ")")
assert all(ident.values()), "replication inputs differ from the real run!"

# ------------------------------------------------------ observed frame (+A1D/E7)
t0 = time.time()
obs_frame = vevents.event_returns(bars, events, side_fn=side_fn, costs=costs,
                                  symbol=symbol, contract_size=sizes[symbol])
observed_mean = float(pd.to_numeric(obs_frame["net"]).mean())
log(f"observed frame: {len(obs_frame)} events, dropped={obs_frame.attrs['dropped']}, "
    f"observed_mean={observed_mean!r} ({time.time()-t0:.1f}s)")

# A1-D
full_mean = observed_mean
sub_mask = obs_frame["exit_time"].dt.strftime("%Y-%m-%d").isin(MONTH_END_15)
sub = obs_frame[sub_mask]
sub_mean = float(pd.to_numeric(sub["net"]).mean())
others_mean = float(pd.to_numeric(obs_frame.loc[~sub_mask, "net"]).mean())
a1d = {
    "full_set_n": int(len(obs_frame)), "full_set_net_mean": full_mean,
    "subset_n": int(len(sub)), "subset_net_mean": sub_mean,
    "subset_dates": sorted(sub["exit_time"].dt.strftime("%Y-%m-%d").unique().tolist()),
    "others_n": int(len(obs_frame) - len(sub)), "others_net_mean": others_mean,
    "subset_weight": len(sub) / len(obs_frame),
    "subset_weighted_contribution": (len(sub) / len(obs_frame)) * sub_mean,
    "subset_share_of_net_sum": float(pd.to_numeric(sub["net"]).sum()
                                     / pd.to_numeric(obs_frame["net"]).sum()),
    "note": "A1 D-section obligation: mechanical breakdown, NOT a veto item",
}

# E7
nights = obs_frame["nights"]
swap_rule = costs.swap.rule(symbol) if getattr(costs, "swap", None) is not None else None
worst_rate = max(abs(float(swap_rule.long_per_day)), abs(float(swap_rule.short_per_day))) if swap_rule else None
e7 = {
    "nights_value_counts": {str(k): int(v) for k, v in nights.value_counts().items()},
    "all_one_night": bool((nights == 1).all()),
    "total_nights": int(nights.sum()), "n_events": int(len(obs_frame)),
    "overnight_by_entry_weekday": {k: int(v) for k, v in
                                   obs_frame["entry_bar_time"].dt.day_name().value_counts().items()},
    "announcement_day_weekday": {k: int(v) for k, v in
                                 obs_frame["exit_bar_time"].dt.day_name().value_counts().items()},
    "swap_rule": {"long_per_day": str(swap_rule.long_per_day),
                  "short_per_day": str(swap_rule.short_per_day)} if swap_rule else None,
    "worst_side_rate": worst_rate,
    "per_event_overnight_relprice": float(obs_frame["overnight"].iloc[0]) if len(obs_frame) else None,
    "declared_basis": "swap_free (registration swap_basis.decision_basis)",
    "registered_swap_free": {"multiple": 21.386366454794768,
                             "implied_sharpe": 0.39646021117234875,
                             "power": 0.5948863577998579, "decision": "pass"},
    "registered_charged_reference": {
        "multiple": 4.073989795394991, "implied_sharpe": 0.31381902600128775,
        "power": 0.4474553469118876, "decision": "fail",
        "reported_as": "4.0740 / 0.31382 / 0.4475",
        "note": "charged = swap_free round-trip + point-snapshot worst side x 1 night "
                 "(spec_snapshot_not_deal_measured, non-gating reference)"},
}

# ------------------------------------------ chunked randomization draws (replica)
# Production body (events.py randomization_test) with checkpoint/resume added;
# seeding + per-draw computation + aggregation copied verbatim:
master = np.random.default_rng(int(SEED))
child_seeds = master.integers(0, 2**63 - 1, size=int(SIMS))
ckpt = OUT / "v3-draws-checkpoint.jsonl"
done: dict[int, float] = {}
if ckpt.exists():
    for line in ckpt.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            done[rec["k"]] = rec["mean"]   # mean may be NaN -> stored as null
log(f"checkpoint: {len(done)}/{SIMS} draws already computed; resuming")

CHUNK = 100
t0 = time.time()
with open(ckpt, "a", encoding="utf-8") as fh:
    for k0 in range(0, SIMS, CHUNK):
        ks = [k for k in range(k0, min(k0 + CHUNK, SIMS)) if k not in done]
        if not ks:
            continue
        for k in ks:
            cs = int(child_seeds[k])
            rand = vevents.randomize_event_times(events, days, seed=cs)
            frame = vevents.event_returns(bars, rand, side_fn=side_fn, costs=costs,
                                          symbol=symbol, contract_size=sizes[symbol])
            mean = (float(pd.to_numeric(frame["net"]).mean())
                    if len(frame) else float("nan"))
            done[k] = mean
            fh.write(json.dumps({"k": k, "mean": None if np.isnan(mean) else mean}) + "\n")
        fh.flush()
        el = time.time() - t0
        log(f"  draws {k0 + CHUNK}/{SIMS} ({el:.0f}s elapsed, "
            f"{el / max(1, len([x for x in done if x >= k0 - CHUNK and x < k0 + CHUNK])):.2f}s/draw)")

null_stats = np.array([done[k] for k in range(SIMS)], dtype="float64")
valid = null_stats[~np.isnan(null_stats)]
p_value = float((1 + int(np.sum(valid >= observed_mean))) / (len(valid) + 1))
null_mean = float(valid.mean()) if len(valid) else float("nan")
null_std = float(valid.std(ddof=1)) if len(valid) > 1 else 0.0
log(f"randomization replica complete: sims={len(valid)}/{SIMS} valid, "
    f"observed={observed_mean!r}, null_mean={null_mean!r}, null_std={null_std!r}, p={p_value!r}")

# ----------------------------------------------- rest of _v3_gate_evaluate (event)
comp = bt._v3_n_composition(ledger, card, Path(str(MAIN / "research/cards")))
n_raw = int(comp["n"])
threshold = float(cfg.alpha) / max(1, n_raw)
primary = {
    "passed": bool(p_value <= threshold), "test": "randomization",
    "p_value": p_value, "threshold": threshold, "alpha": float(cfg.alpha),
    "n_tests": n_raw, "sims": SIMS, "draws_min": 1999,
    "n_events": int(len(obs_frame)), "observed_mean": observed_mean,
    "null_mean": null_mean, "null_std": null_std,
    "event_kind": "fomc_calendar", "event_side": "static_long",
    "side_rule": "static_long",
    "note": "随机化参照（D35 修改 1）；N 口径同 [v3] 主检验（rounds_lineage），不吃 n_min",
}
frame15 = vevents.event_returns(
    bars, events, side_fn=side_fn,
    costs=vrob.ScaledCosts(costs, float(cfg.cost_scale_veto)),
    symbol=symbol, contract_size=sizes[symbol])
cost_scale = {
    "passed": bool(len(frame15) and float(pd.to_numeric(frame15["net"]).mean()) > 0),
    "net_mean_scaled": (float(pd.to_numeric(frame15["net"]).mean())
                        if len(frame15) else None),
    "scale": float(cfg.cost_scale_veto), "n_events": int(len(frame15)),
    "note": "事件模式否决=成本 ×1.5 后净均值 > 0（D35 修改 2）"}
not_applicable = ("事件模式不适用（D35 修改 2：[v3.event].{0}_veto = false——"
                  "按时钟定义的事件窗上'{1}'会误杀）")
delay = {"passed": True, "applicable": False,
         "note": not_applicable.format("delay_bars", "延迟一根落到定盘后反转段")}
buyhold = {"passed": True, "applicable": False,
           "note": not_applicable.format("buyhold_excess", "真实月末效应夏普通常低于全程持有")}
known_gaps = list(read_known_gaps(str(DATA_ROOT), symbol, card.timeframe)) + [
    {"start": str(a), "end": str(b), "kind": "merged_gap", "input": key}
    for key, spans in h_ctx["gaps"].items() for a, b in spans]
t0 = time.time()
diagnostics = vevents.v3_event_diagnostics(
    bars, events, side_fn=side_fn, costs=costs, symbol=symbol,
    contract_size=sizes[symbol], known_gaps=known_gaps,
    sims=int(cfg.bootstrap_sims), seed=SEED)
log(f"diagnostics computed ({time.time()-t0:.0f}s): "
    + ", ".join(sorted(diagnostics.keys())))
g0 = {"passed": bool(g0det["passed"])}
verdict = vgate.evaluate_v3(g0=g0, primary=primary, cost_scale=cost_scale,
                            delay=delay, buyhold=buyhold, diagnostics=diagnostics)
result = {
    "passed": bool(verdict["passed"]), "mode": "event", "primary": primary,
    "vetoes": {"cost_scale_x15": cost_scale, "delay_1_bar": delay,
               "buyhold_excess": buyhold},
    "n_composition": comp, "veto": verdict["veto"],
    "failed_vetoes": verdict["failed_vetoes"], "diagnostics": diagnostics,
    "checks": {"G0": {"passed": True}, "primary": {"passed": primary["passed"]},
               "cost_scale_x15": {"passed": cost_scale["passed"]},
               "delay_1_bar": {"passed": True}, "buyhold_excess": {"passed": True}},
    "_replication_note": (
        "offline deterministic replication of the run5 V3 event evaluation that "
        "was interrupted by external process termination before its ledger row "
        "was written; identical frozen inputs (identity verified vs run#451: "
        "data_sha256/cost_model_version), identical seed (rng_seed('H102')="
        f"{SEED}), production functions; draws chunk-executed with checkpoint, "
        "seeding/aggregation verbatim from events.randomization_test. NOT a "
        "ledger gates row; the authoritative ledger has no V3 row for run#451."),
}

out = {"identity": ident, "seed": SEED, "sims": SIMS,
       "result": result, "a1d": a1d, "e7": e7}
(OUT / "v3-offline-replication.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
log("archived -> v3-offline-replication.json")

with open(OUT / "a1d-e7-obligations.txt", "w", encoding="utf-8") as fh:
    fh.write("A1-D / E7 obligations — run5 (2026-11-07; same frozen inputs as run#451)\n")
    fh.write("=" * 88 + "\n\n[A1-D] month-end 15/213 subset breakdown (mechanical, NOT a veto)\n")
    fh.write(f"  full-set   : n={a1d['full_set_n']}  net mean = {a1d['full_set_net_mean']!r}\n")
    fh.write(f"  subset-15  : n={a1d['subset_n']}  net mean = {a1d['subset_net_mean']!r}\n")
    fh.write(f"  others-198 : n={a1d['others_n']}  net mean = {a1d['others_net_mean']!r}\n")
    fh.write(f"  weighted contribution (15/213 x subset mean) = {a1d['subset_weighted_contribution']!r}\n")
    fh.write(f"  share of full-set net sum = {a1d['subset_share_of_net_sum']!r}\n")
    fh.write("  subset dates matched: " + ", ".join(a1d["subset_dates"]) + "\n\n")
    fh.write("[E7] per-event nights (fee-engine measured) + swap calibers\n")
    fh.write(f"  nights value counts: {e7['nights_value_counts']}\n")
    fh.write(f"  all events exactly 1 night: {e7['all_one_night']}; total nights = {e7['total_nights']} over {e7['n_events']} events\n")
    fh.write(f"  overnight by entry-day weekday: {e7['overnight_by_entry_weekday']}\n")
    fh.write(f"  announcement-day weekday: {e7['announcement_day_weekday']}\n")
    fh.write(f"  swap rule: {e7['swap_rule']}; worst-side rate = {e7['worst_side_rate']}\n")
    fh.write(f"  per-event overnight (relative price): {e7['per_event_overnight_relprice']!r}\n")
    fh.write("  declared caliber = swap_free; registered swap_free: multiple 21.386366454794768 implied_sharpe 0.39646021117234875 power 0.5948863577998579 decision=pass\n")
    fh.write("  charged parallel reference (registered): multiple 4.073989795394991 (4.0740), implied_sharpe 0.31381902600128775 (0.31382), power 0.4474553469118876 (0.4475), decision=fail — non-gating reference\n")
log("archived -> a1d-e7-obligations.txt")

ledger.conn.close()
shutil.rmtree(tmp, ignore_errors=True)
log("VERDICT (offline replication): V3 passed =", result["passed"],
    "| primary passed =", primary["passed"], f"(p={p_value!r} vs threshold {threshold!r})",
    "| failed vetoes =", verdict["failed_vetoes"])
log("done", time.strftime("%H:%M:%S"))
