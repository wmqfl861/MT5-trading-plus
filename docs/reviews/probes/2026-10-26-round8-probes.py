"""R8 independent audit: read existing artifacts and call pure repository helpers.

No terminal connection, order submission, G5 execution, market-data loading,
ledger migration, git mutation, or changes to product/config files. Mutated
artifact copies and checker fixtures exist only in memory / TemporaryDirectory.
Run with .venv/Scripts/python.exe docs/reviews/probes/2026-10-26-round8-probes.py.
Output contains counts/hashes only, never account/server/credential fields.
"""
from __future__ import annotations

import collections
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT)]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    obj = importlib.util.module_from_spec(spec)
    sys.modules[name] = obj
    spec.loader.exec_module(obj)
    return obj


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def p14_gate_artifacts():
    merge = module("r8_merge", "scripts/merge_gate_power.py")
    base = ROOT / "data/reports/gates"
    docs = [read_json(base / f"shard8-{i}/gate-power.json") for i in range(5)]
    official = read_json(base / "gate-power-v2/gate-power.json")
    fp = docs[0]["meta"]["fingerprint"]
    git_script = subprocess.run(
        ["git", "show", f"{fp['git_commit']}:scripts/gate_calibration.py"],
        cwd=ROOT, capture_output=True, check=True).stdout
    merged = {"meta": merge.merge_meta(docs),
              "negative": merge.merge_negative(docs),
              "positive": merge.merge_positive(docs),
              "g5": merge.merge_g5(docs)}
    merged["flags"] = merge.recompute_flags(merged)
    distributions = {}
    for setup in official["positive"]["calibration"]:
        for lv, info in official["positive"]["calibration"][setup].items():
            values = [r["sharpe"] for r in official["positive"]["runs"]
                      if r["setup"] == setup and str(r["level"]) == lv]
            distributions[f"{setup}/{lv}"] = {
                "n": len(values), "mean": statistics.mean(values),
                "median": statistics.median(values), "min": min(values),
                "max": max(values), "converged": info.get("converged"),
                "history_length": len(info.get("history", [])),
                "achieved_sharpe": info.get("achieved_sharpe"),
                "target": info.get("target_sharpe"), "tol": info.get("tol")}
    g5_groups = collections.defaultdict(list)
    for r in official["g5"]["runs"]:
        if r["arm"] == "false_positive":
            g5_groups[f"{r['setup']}/{r['null']}"].append(r)
    rates = {k: {"raw_n": len(rows),
                 "unique_seed_n": len({r["seed"] for r in rows}),
                 "passed": sum(r["checks"]["G5.passed"] for r in rows),
                 "p5_min": min(r["p5"] for r in rows),
                 "p5_max": max(r["p5"] for r in rows)}
             for k, rows in g5_groups.items()}
    supplement = merge.render_supplement(merged)
    missing_group = copy.deepcopy(docs)
    missing_group[0]["positive"]["runs"] = [
        r for r in missing_group[0]["positive"]["runs"]
        if not (r["setup"] == "XAUUSD H1" and r["level"] == 1.0)]
    missing_g5 = copy.deepcopy(docs)
    missing_g5[0].pop("g5")
    changed_identity = {}
    for field, value in (("max_bars", 100), ("g5_cutoff", "2024-10-24 00:00:00"),
                         ("g5_window_source", "different")):
        altered = copy.deepcopy(docs)
        altered[1]["meta"][field] = value
        changed_identity[field] = merge._meta_identity_problems(altered)
    altered = copy.deepcopy(docs)
    altered[1]["meta"]["fingerprint"]["script_sha256"] = "mismatch"
    missing_fp = copy.deepcopy(docs)
    for doc in missing_fp:
        doc["meta"].pop("fingerprint")
    # Run the actual merge CLI on disposable copies; this only renders reports.
    with tempfile.TemporaryDirectory(prefix="r8-merge-") as tmp:
        paths = []
        for i, doc in enumerate(missing_g5):
            folder = pathlib.Path(tmp) / str(i)
            folder.mkdir()
            (folder / "gate-power.json").write_text(json.dumps(doc), encoding="utf-8")
            paths.append(str(folder))
        output = pathlib.Path(tmp) / "merged"
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = merge.main(["--shards", ",".join(paths), "--out", str(output)])
        accepted = read_json(output / "gate-power.json") if exit_code == 0 else None
    return {
        "all_fingerprints_equal": all(d["meta"]["fingerprint"] == fp for d in docs),
        "fingerprint": fp,
        "git_script_hash_matches": hashlib.sha256(git_script).hexdigest() == fp["script_sha256"],
        "git_script_matches_after_crlf_normalization": git_script.replace(b"\r\n", b"\n") == (ROOT / "scripts/gate_calibration.py").read_bytes().replace(b"\r\n", b"\n"),
        "current_script_hash_matches": hashlib.sha256((ROOT / "scripts/gate_calibration.py").read_bytes()).hexdigest() == fp["script_sha256"],
        "gates_hash_matches": hashlib.sha256((ROOT / "config/gates.toml").read_bytes()).hexdigest() == fp["gates_sha256"],
        "shard_seeds": [[d["meta"]["seeds_from"], d["meta"]["seeds"]] for d in docs],
        "identity_problems": merge._meta_identity_problems(docs),
        "seed_problems": merge._seed_problems(docs),
        "official_sections_equal": {k: merged[k] == official[k] for k in ("negative", "positive", "g5", "flags")},
        "supplement_matches_official_md": (base / "gate-power-v2/gate-power.md").read_text(encoding="utf-8").endswith(supplement),
        "counts": {k: len(official[k]["runs"]) for k in ("negative", "positive", "g5")},
        "passed_all": {k: sum(r["passed_all"] for r in official[k]["runs"]) for k in ("negative", "positive")},
        "flags": official["flags"], "distributions": distributions, "g5": rates,
        "missing_condition_rejected": bool(merge._seed_problems(missing_group)),
        "changed_fingerprint_rejected": bool(merge._meta_identity_problems(altered)),
        "all_fingerprints_missing_problems": merge._meta_identity_problems(missing_fp),
        "changed_unchecked_identity_fields": changed_identity,
        "missing_whole_g5": {"seed_problems": merge._seed_problems(missing_g5),
                             "merge_exit": exit_code,
                             "g5_runs": len(accepted["g5"]["runs"]) if accepted else None,
                             "reported_seeds": accepted["meta"]["seeds"] if accepted else None}}


def p15_ci_head_ref():
    # Harmless early exit. No branch is created, and no workflow is submitted.
    branch = 'audit";exit${IFS}0;#'
    valid = subprocess.run(["git", "check-ref-format", "--branch", branch],
                           cwd=ROOT, capture_output=True).returncode == 0
    workflow = (ROOT / ".github/workflows/research-allowlist.yml").read_text(encoding="utf-8")
    head_line = next(line.strip() for line in workflow.splitlines()
                     if 'head="${{ github.head_ref }}"' in line)
    bash = pathlib.Path("C:/Program Files/Git/bin/bash.exe")
    if not bash.is_file():
        raise RuntimeError("Git Bash required for this inert shell probe")
    results = {}
    for label, ref in (("ordinary", "audit-normal"), ("injected", branch)):
        script = head_line.replace("${{ github.head_ref }}", ref)
        script += "\nprintf 'CHECKER_REACHED\\n'\nexit 42\n"
        with tempfile.TemporaryDirectory(prefix="r8-ci-") as tmp:
            run = subprocess.run([str(bash), "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                                 cwd=tmp, capture_output=True, text=True)
        results[label] = {"exit": run.returncode, "checker_reached": "CHECKER_REACHED" in run.stdout}
    return {"valid_git_branch": valid, "inert_branch": branch, **results}


def p16_demo_no_evidence():
    demo = module("r8_demo_check", "scripts/demo_check.py")
    with tempfile.TemporaryDirectory(prefix="r8-demo-") as tmp:
        root = pathlib.Path(tmp)
        reports = root / "reports/live"
        reports.mkdir(parents=True)
        # A single report with no transactions or price samples must not prove F.
        (reports / "daily-2026-10-26.md").write_text("当日无点差样本\n", encoding="utf-8")
        live = root / "data/live"
        live.mkdir(parents=True)
        # A stale but syntactically valid flat intent, with no runtime evidence.
        (live / "expected-1.json").write_text(json.dumps({
            "symbol": "XAUUSD", "magic": 1, "side": "buy", "volume": 0,
            "status": "confirmed", "cause": "initial",
            "ts": "2026-10-26T00:00:00+00:00"}), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = demo.main(["--data-dir", str(root / "data"),
                              "--log-dir", str(root / "logs"),
                              "--reports-dir", str(root / "reports"),
                              "--sessions-config", str(ROOT / "config/sessions.toml"),
                              "--summary"])
    return {"exit": code, "output": output.getvalue()}


def p17_reconciliation():
    import numpy as np
    import pandas as pd
    from mt5quant.strategies import ExitRules
    rec = module("r8_reconcile", "scripts/reconcile_ab.py")
    summaries = {}
    total = collections.Counter()
    equivalent = {"stop_gap": "stop", "take_profit_gap": "take_profit"}
    for path in sorted((ROOT / "data/reports/reconciliation-v3").glob("*/diff.csv")):
        rows = pd.read_csv(path)
        paired = rows[rows.match == "paired"].copy()
        summary = read_json(path.with_name("summary.json"))
        cats = collections.Counter()
        for row in paired[paired.net_ok == False].itertuples():
            ra, rb = equivalent.get(row.exit_reason_a, row.exit_reason_a), equivalent.get(row.exit_reason_b, row.exit_reason_b)
            if ra != rb:
                cat = "reason_mismatch"
            elif row.entry_note == "before_ready":
                cat = "cross_bar_skew"
            elif row.exit_reason_a == "take_profit_gap":
                cat = "tp_gap"
            elif row.exit_reason_a == "take_profit":
                cat = "tp_cross"
            else:
                cat = "unclassified"
            cats[cat] += 1
        mismatched_stop = paired[(paired.exit_reason_a == "stop") & (paired.exit_reason_b != "stop")]
        wrong_net_ok = mismatched_stop[(mismatched_stop.net_ok == True) &
            ((mismatched_stop.net_rest + mismatched_stop.net_overshoot_comp).abs() > mismatched_stop.net_tol)]
        # Same entry bar, two genuine stop exits: the report's 2,119-row subset.
        seconds = 3600 if "H1" in path.parent.name else 300
        ea = pd.to_datetime(paired.entry_time_a, format="mixed")
        eb = pd.to_datetime(paired.entry_time_b, format="mixed")
        pure_stop = paired[(paired.exit_reason_a == "stop") & (paired.exit_reason_b == "stop") &
                           (ea.dt.floor(f"{seconds}s") == eb.dt.floor(f"{seconds}s"))]
        bad = paired[paired.net_ok == False]
        result = {
            "paired": len(paired), "only_a": int((rows.match == "only_a").sum()),
            "only_b": int((rows.match == "only_b").sum()),
            "entry_bad": int((paired.entry_time_ok == False).sum()),
            "exit_bad": int((paired.exit_time_ok == False).sum()),
            "reason_bad": int((paired.reason_ok == False).sum()),
            "net_bad": len(bad), "reported_summary_ok": summary["ok"],
            "entry_notes": paired.entry_note.value_counts().to_dict(),
            "exit_notes": paired.exit_note.value_counts().to_dict(),
            "net_bad_categories": dict(cats), "b_favorable_bad_rows": int((bad.net_b > bad.net_a).sum()),
            "pure_stop_rows": len(pure_stop), "pure_stop_net_bad": int((pure_stop.net_ok == False).sum()),
            "stop_to_nonstop_rows": len(mismatched_stop),
            "stop_to_nonstop_overshoot_sum": float(mismatched_stop.net_overshoot_comp.sum()),
            "additional_bad_without_invalid_overshoot": len(wrong_net_ok),
            "summary_counts_match": len(paired) == summary["n_paired"] and len(bad) == summary["violations"]["net"]}
        if len(mismatched_stop):
            worst = mismatched_stop.loc[mismatched_stop.net_overshoot_comp.abs().idxmax()]
            result["largest_invalid_overshoot"] = worst[[
                "entry_time_a", "exit_time_a", "exit_time_b", "exit_reason_b",
                "net_diff", "net_overshoot_comp", "net_rest", "net_tol", "net_ok"]].to_dict()
        summaries[path.parent.name] = result
        total.update({k: result[k] for k in (
            "paired", "only_a", "only_b", "entry_bad", "exit_bad", "reason_bad", "net_bad",
            "pure_stop_rows", "pure_stop_net_bad", "stop_to_nonstop_rows",
            "additional_bad_without_invalid_overshoot", "b_favorable_bad_rows")})
    # Pure function reproduction: A stops at 90, B exits on a later signal at
    # 150. The 60-unit path difference is incorrectly called negative slippage.
    start = pd.Timestamp("2026-01-05T10:00:00")
    ctx = rec.MechanismContext(probe=None, rules=ExitRules(stop_loss_atr=1.0),
        atr=np.array([10.0, 10.0]),
        bar_open_ms=[rec._ms(start), rec._ms(start + pd.Timedelta(hours=1))],
        tf_seconds=3600, costs=None, symbol="TEST", contract_size=1.0)
    a = pd.Series({"side": "long", "entry_time": start + pd.Timedelta(hours=1),
        "entry_price": 100.0, "exit_time": start + pd.Timedelta(hours=1, minutes=1),
        "exit_price": 90.0, "exit_reason": "stop", "net": -10.0,
        "volume_lots": 1.0, "spread_cost": 1.0})
    b = a.copy()
    b["exit_reason"], b["exit_price"], b["net"] = "signal", 150.0, 50.0
    b["exit_time"] = start + pd.Timedelta(hours=2)
    synthetic = rec._decompose_net(a, b, "stop", "stop", ctx=ctx)
    return {"runs": summaries, "totals": dict(total), "nonstop_as_overshoot_probe": synthetic}


def p18_paper_evidence():
    live = ROOT / "data/live"
    out = {}
    for magic, symbol in ((902431, "XAUUSD"), (902432, "EURUSD")):
        state = read_json(live / f"paper/state-{magic}.json")
        deals = state["deals"]
        log = (live / f"paper-{symbol}-M15.log").read_text(encoding="utf-8", errors="replace")
        actions = collections.Counter(re.findall(r"执行 ([a-z_]+)：", log))
        orders = [json.loads(s) for s in (live / f"orders-{magic}.jsonl").read_text(encoding="utf-8").splitlines() if s]
        volume = collections.defaultdict(lambda: [0.0, 0.0])
        for d in deals:
            volume[d["position_id"]][0 if d["entry"] == "in" else 1] += d["volume"]
        still_open = {p["ticket"] for p in state["positions"]}
        mismatches = [pid for pid, (inn, off) in volume.items() if pid not in still_open and abs(inn - off) > 1e-8]
        out[str(magic)] = {
            "decisions": len(re.findall(r"\] 决策 ", log)),
            "deals": len(deals), "entries": sum(d["entry"] == "in" for d in deals),
            "exits": sum(d["entry"] == "out" for d in deals),
            "SL": sum(d.get("reason") == 4 for d in deals),
            "TP": sum(d.get("reason") == 5 for d in deals),
            "balance": state["balance"], "equity": state["equity"],
            "open_positions": len(still_open), "closed_position_volume_mismatches": len(mismatches),
            "execution_actions": dict(actions), "logged_order_kinds": dict(collections.Counter(d["kind"] for d in orders)),
            "protective_times": [d["time"] for d in deals if d.get("reason") in (4, 5)],
            "flatten_close_times": [d["time"] for d in deals if d["entry"] == "out" and d.get("reason") not in (4, 5)]}
    risk = (live / "paper-risk.log").read_text(encoding="utf-8", errors="replace").splitlines()
    recon = (live / "paper-reconcile.log").read_text(encoding="utf-8", errors="replace").splitlines()
    out["risk"] = {"cycles": sum("心跳=" in l for l in risk),
                   "bad_heartbeat_cycles": sum("心跳=" in l and "心跳=ok" not in l for l in risk),
                   "halt_cycles": sum("halt=是" in l for l in risk)}
    out["reconcile"] = {"consistent_cycles": sum("对账一致" in l for l in recon),
                        "inconsistent_cycles": sum("不一致" in l for l in recon)}
    out["flatten_archive"] = {p.name: {k: d[k] for k in ("id", "ts", "closed", "magic") if k in d}
        for p in sorted((live / "commands/archive").glob("*.json")) for d in [read_json(p)]}
    from mt5live.monitor import read_alerts
    alerts = [a for a in read_alerts(str(live)) if a.time.date() == date(2026, 10, 26)]
    out["alert_counts"] = dict(collections.Counter(a.kind for a in alerts))
    out["reject_counts"] = {reason: sum(a.kind == "RISK_REJECT" and reason in a.message for a in alerts)
                            for reason in ("max_lots", "kill_switch")}
    return out


def p19_cost_and_ledger():
    from mt5quant.research.ledger import Ledger
    from run_backtest import load_overshoot_from_cache
    anchor = module("r8_anchor", "scripts/ledger_anchor.py")
    ledger_path = ROOT / "data/research/ledger.sqlite"
    # Bypass Ledger.__init__: even create=False can migrate a production DB.
    # This connection enforces mode=ro and one consistent read transaction.
    conn = sqlite3.connect(f"file:{ledger_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    ledger = Ledger.__new__(Ledger)
    ledger.conn, ledger.path = conn, str(ledger_path)
    try:
        problems = ledger.verify_chain()
        baseline = conn.execute("SELECT run_id, card_id, cost_model_version FROM runs WHERE run_id BETWEEN 364 AND 430").fetchall()
        ops = conn.execute("SELECT run_id, strategy, symbol, timeframe, created_utc FROM runs WHERE actor='ops' AND created_utc LIKE '2026-10-26%'").fetchall()
    finally:
        conn.close()
    cache_results = {}
    for cache in ("data/costs/spread_model.json", "data_research/costs/spread_model.json"):
        cache_results[cache] = {}
        for symbol in ("XAUUSD", "USTEC", "EURUSD", "BTCUSD"):
            model, sha, status = load_overshoot_from_cache(str(ROOT / cache), symbol)
            cache_results[cache][symbol] = {"sha": sha, "status": status, "loaded": model is not None}
    return {"ledger_verify_problems": problems,
            "anchor_verify_problems": anchor.verify_anchor(ROOT / "docs/ops/ledger-anchors.md", ledger_path),
            "baseline_run_count": len(baseline),
            "baseline_with_ov": sum("ov[eca09e24]" in row["cost_model_version"] for row in baseline),
            "baseline_cards": sorted({row["card_id"] for row in baseline}),
            "ops_records": [dict(row) for row in ops], "cache_loads": cache_results}


if __name__ == "__main__":
    results = {"P14_gate_artifacts_and_merge": p14_gate_artifacts(),
               "P15_ci_branch_injection": p15_ci_head_ref(),
               "P16_demo_missing_evidence": p16_demo_no_evidence(),
               "P17_reconciliation": p17_reconciliation(),
               "P18_paper_evidence": p18_paper_evidence(),
               "P19_cost_and_ledger": p19_cost_and_ledger()}
    rendered = json.dumps(results, ensure_ascii=False, indent=2)
    if len(sys.argv) == 3 and sys.argv[1] == "--out":
        pathlib.Path(sys.argv[2]).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
