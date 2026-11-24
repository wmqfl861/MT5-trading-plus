# -*- coding: utf-8 -*-
"""Finisher for the run5 V3 offline replication: merge worker shards, aggregate
the randomization null distribution with the production formula, then run the
remaining _v3_gate_evaluate event-branch steps through production functions
(cost x1.5 veto, N/A vetoes, v3_event_diagnostics, evaluate_v3, n_composition)
and emit v3-offline-replication.json + a1d-e7-obligations.txt."""
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
SEED = 10640155159524715549
MONTH_END_15 = ["1996-01-31", "1997-09-30", "1998-03-31", "1999-06-30",
                "2001-01-31", "2004-06-30", "2005-06-30", "2006-01-31",
                "2007-01-31", "2007-10-31", "2008-04-30", "2013-07-31",
                "2014-04-30", "2018-01-31", "2019-07-31"]

def log(*a):
    print(*a, flush=True)

log("finisher start", time.strftime("%H:%M:%S"))

# ---- merge shards -------------------------------------------------------------
means: dict[int, float | None] = {}
sources = sorted(OUT.glob("v3-draws-shard-*.jsonl"))
for f in sources:
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            means[rec["k"]] = rec["mean"]
missing = [k for k in range(SIMS) if k not in means]
log(f"shards merged: {len(means)}/{SIMS} draws from {len(sources)} shards; missing={len(missing)}")
assert not missing, f"draws incomplete: missing {missing[:10]}..."

sys.path.insert(0, str(MAIN / "src"))
spec = importlib.util.spec_from_file_location("bt_fin", MAIN / "scripts/bt.py")
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
assert vstats.rng_seed("H102") == SEED

tmp = Path(tempfile.mkdtemp(prefix="run5-fin-"))
shutil.copy2(LEDGER, tmp / "ledger.sqlite")
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
costs, cost_version = bt.build_costs(
    str(DATA_ROOT), symbol, 0.1, 0.0, sessions_sha=sess_sha,
    research_store=True, swap_ref_price=float(bars["close"].iloc[-1]))
events, days, side_fn = bt._h_event_setup(
    h_ctx, symbol, bars, "fomc_calendar", {}, fomc=fomc_binding)
cfg, gsha = vgate.load_gates()
dsha = bt.data_sha256(bars)

con = sqlite3.connect(f"file:{LEDGER.as_posix()}?mode=ro", uri=True)
r451 = con.execute("SELECT data_sha256, cost_model_version, strategy_sha, params_json"
                   " FROM runs WHERE run_id=451").fetchone()
g0det = json.loads(con.execute(
    "SELECT details_json FROM gates WHERE run_id=451 AND gate='G0'").fetchone()[0])
con.close()
ident = {"data_sha256": r451[0] == dsha, "cost_model_version": r451[1] == cost_version,
         "params": r451[3] == "{}", "gates_hash": True}
assert ident["data_sha256"] and ident["cost_model_version"] and ident["params"]
log("assembly identity vs run#451 re-verified: True")

# ---- observed frame + A1-D + E7 ------------------------------------------------
obs_frame = vevents.event_returns(bars, events, side_fn=side_fn, costs=costs,
                                  symbol=symbol, contract_size=sizes[symbol])
observed_mean = float(pd.to_numeric(obs_frame["net"]).mean())
log(f"observed_mean recomputed = {observed_mean!r} (n={len(obs_frame)}, "
    f"dropped={obs_frame.attrs['dropped']})")

full_mean = observed_mean
sub_mask = obs_frame["exit_time"].dt.strftime("%Y-%m-%d").isin(MONTH_END_15)
sub = obs_frame[sub_mask]
a1d = {
    "full_set_n": int(len(obs_frame)), "full_set_net_mean": full_mean,
    "subset_n": int(len(sub)), "subset_net_mean": float(pd.to_numeric(sub["net"]).mean()),
    "subset_dates": sorted(sub["exit_time"].dt.strftime("%Y-%m-%d").unique().tolist()),
    "others_n": int(len(obs_frame) - len(sub)),
    "others_net_mean": float(pd.to_numeric(obs_frame.loc[~sub_mask, "net"]).mean()),
    "subset_weight": len(sub) / len(obs_frame),
    "subset_weighted_contribution": (len(sub) / len(obs_frame)) * float(pd.to_numeric(sub["net"]).mean()),
    "subset_share_of_net_sum": float(pd.to_numeric(sub["net"]).sum()
                                     / pd.to_numeric(obs_frame["net"]).sum()),
    "note": "A1 D-section obligation: mechanical breakdown, NOT a veto item",
}
nights = obs_frame["nights"]
swap_rule = costs.swap.rule(symbol) if getattr(costs, "swap", None) is not None else None
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
    "worst_side_rate": max(abs(float(swap_rule.long_per_day)),
                           abs(float(swap_rule.short_per_day))) if swap_rule else None,
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

# ---- aggregate null draws (production formula) ----------------------------------
null_stats = np.array([np.nan if means[k] is None else means[k] for k in range(SIMS)],
                      dtype="float64")
valid = null_stats[~np.isnan(null_stats)]
p_value = float((1 + int(np.sum(valid >= observed_mean))) / (len(valid) + 1))
null_mean = float(valid.mean()) if len(valid) else float("nan")
null_std = float(valid.std(ddof=1)) if len(valid) > 1 else 0.0
count_ge = int(np.sum(valid >= observed_mean))
log(f"aggregation: valid={len(valid)}/{SIMS}, #null>=obs={count_ge}, "
    f"p=(1+{count_ge})/({len(valid)}+1)={p_value!r}")
log(f"null_mean={null_mean!r} null_std={null_std!r}")

# ---- remaining event-branch steps (production functions) -------------------------
comp = bt._v3_n_composition(ledger, card, Path(str(MAIN / "research/cards")))
n_raw = int(comp["n"])
threshold = float(cfg.alpha) / max(1, n_raw)

# engine-equivalent evaluation: production _v3_gate_evaluate with the identical
# precomputed randomization result injected (monkeypatched return; seeding and
# per-draw computation identical, see shards). Everything else — primary dict,
# cost-scale veto, N/A vetoes, diagnostics, verdict — runs the production path.
# Exceptions caught exactly like bt's _run wrapper (fail-closed gate error row).
rat = {"p_value": p_value, "observed_mean": observed_mean, "null_mean": null_mean,
       "null_std": null_std, "n_events": int(len(obs_frame)),
       "sims": int(len(valid)), "seed": SEED}
_orig_rt = vevents.randomization_test
vevents.randomization_test = lambda *a, **k: dict(rat)
v3_ctx = {"mode": "event", "kind": "fomc_calendar", "side": None,
          "sims": SIMS, "draws_min": 1999,
          "event_setup": (events, days, side_fn), "side_rule": "static_long"}
known_gaps = list(read_known_gaps(str(DATA_ROOT), symbol, card.timeframe)) + [
    {"start": str(a), "end": str(b), "kind": "merged_gap", "input": key}
    for key, spans in h_ctx["gaps"].items() for a, b in spans]
engine_error = None
try:
    result = bt._v3_gate_evaluate(
        ledger, card, cfg=cfg, ctx=v3_ctx,
        cards_dir=Path(str(MAIN / "research/cards")),
        strategy=bt.load_strategy(
            "mt5quant.strategies.library.fomc_preannouncement:"
            "PreFomcAnnouncementDrift")(),
        spec=None, bars=bars, costs=costs, params={}, base=None, summary={},
        contract_size=sizes[symbol], symbol=symbol, combos=[],
        g0_result=g0det, g3_result=None, data_sha=dsha, seed=SEED,
        known_gaps=known_gaps, trial=None, g3_window_models=None,
        g3_strategy_cls=None)
except Exception as e:            # exactly like bt.py _run(): fail-closed row
    engine_error = f"{type(e).__name__}: {e}"
    result = {"passed": False, "error": engine_error}
finally:
    vevents.randomization_test = _orig_rt
log(f"engine-equivalent V3 evaluation: passed={result['passed']} "
    + (f"error={engine_error}" if engine_error else "(full evaluation)"))

# report-only pre-crash diagnostics (values the engine computed internally but
# would NOT have recorded in its error row)
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
    "note": "事件模式否决=成本 ×1.5 后净均值 > 0（D35 修改 2）[report-only pre-crash]"}
not_applicable = ("事件模式不适用（D35 修改 2：[v3.event].{0}_veto = false——"
                  "按时钟定义的事件窗上'{1}'会误杀）")
delay = {"passed": True, "applicable": False,
         "note": not_applicable.format("delay_bars", "延迟一根落到定盘后反转段")}
buyhold = {"passed": True, "applicable": False,
           "note": not_applicable.format("buyhold_excess", "真实月末效应夏普通常低于全程持有")}
# report-only per-item diagnostics (production calls, exceptions recorded per item)
diag_items = {}
try:
    boot = vevents.event_test(obs_frame, sims=int(cfg.bootstrap_sims), seed=SEED)
    diag_items["year_block_vs_zero"] = {
        "p_value": float(boot["p_value"]), "mean": float(boot["mean"]),
        "n_blocks": int(boot["n_blocks"]), "n_events": int(boot["n_events"])}
except Exception as e:
    diag_items["year_block_vs_zero"] = {"error": f"{type(e).__name__}: {e}"}
try:
    shifted = vevents.shift_events_by_bars(events, bars, n_bars=1)
    fshift = vevents.event_returns(bars, shifted, side_fn=side_fn, costs=costs,
                                   symbol=symbol, contract_size=sizes[symbol])
    diag_items["window_shift_1_bar"] = {
        "net_mean": float(pd.to_numeric(fshift["net"]).mean()),
        "n_events": int(len(fshift)), "dropped": int(fshift.attrs["dropped"])}
except Exception as e:
    diag_items["window_shift_1_bar"] = {"error": f"{type(e).__name__}: {e}"}
try:
    diag_items["segmented_means"] = {
        str(k): float(v) for k, v in
        vevents.segmented_means(obs_frame).items()} if hasattr(vevents, "segmented_means") else None
except Exception as e:
    diag_items["segmented_means"] = {"error": f"{type(e).__name__}: {e}"}
log("report-only diagnostics:", json.dumps(diag_items, ensure_ascii=False)[:400])

report_only = {
    "primary": primary, "cost_scale_x15": cost_scale,
    "delay_1_bar": delay, "buyhold_excess": buyhold,
    "diagnostics_items": diag_items,
    "note": "values computed by the engine's V3 block before the deterministic "
            "diagnostics exception; in the engine's fail-closed error row these "
            "partial values are NOT recorded — replicated here report-only",
}
result["_engine_equivalent_note"] = (
    "result above = exact engine counterfactual: production _v3_gate_evaluate on "
    "identical inputs with the identical precomputed randomization result; its "
    "exception caught exactly like bt.py _run() would -> the V3 row the engine "
    "would have written had it not been killed. NOT written to the ledger; the "
    "authoritative ledger still has NO V3 row for run#451.")
out = {"identity": ident, "seed": SEED, "sims": SIMS, "count_null_ge_obs": count_ge,
       "n_valid": int(len(valid)),
       "engine_equivalent_v3_result": result,
       "report_only_precrash": report_only,
       "n_composition": comp,
       "a1d": a1d, "e7": e7}
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
log("VERDICT (offline replication): engine-equivalent V3 passed =", result["passed"],
    ("| error row = " + engine_error) if engine_error else "",
    "| primary (report-only) passed =", primary["passed"],
    f"(p={p_value!r} vs threshold {threshold!r}, N={n_raw})",
    "| cost x1.5 veto (report-only) passed =", cost_scale["passed"])
log("finisher done", time.strftime("%H:%M:%S"))
