"""R9 review evidence: read existing artifacts; exercise copies in temporary dirs.

Never starts a runner, connects to MT5, reads bars/ticks, runs G5, or writes a
ledger. The PowerShell native-argument probe only ast.parse()s received text.
Usage: .venv/Scripts/python.exe <this file> --out <review-evidence.json>
"""
from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT)]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def git_blob(ref, path):
    return subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=ROOT)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def bytes_and_assertions():
    script = "scripts/gate_calibration.py"
    a, b = git_blob("82be091", script), git_blob("0d24729", script)
    bom = {}
    for path in ("scripts/demo_run.ps1", "scripts/setup_scheduled_tasks.ps1"):
        old, new = git_blob("0d24729^", path), git_blob("0d24729", path)
        bom[path] = {"only_utf8_bom_added": new == b"\xef\xbb\xbf" + old,
                     "size_delta": len(new) - len(old)}

    def assertions(src):
        return {n.name: [ast.dump(a.test) for a in ast.walk(n)
                         if isinstance(a, ast.Assert)]
                for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)
                and n.name.startswith("test_")}

    paths = ("tests/test_demo_run_ps1.py", "tests/test_p6_docs.py")
    return {"gate_calibration_git_bytes_equal": a == b,
            "git_blob_sha256": hashlib.sha256(b).hexdigest(), "bom": bom,
            "assertion_expressions_unchanged_since_T49": {
                p: assertions(git_blob("bcb831c", p)) ==
                   assertions(git_blob("0d24729", p)) for p in paths}}


def ci_shell_probe():
    mod = module("r9_p6_tests", "tests/test_p6_docs.py")
    scripts = {
        "before": git_blob("82be091", ".github/workflows/research-allowlist.yml").decode(),
        "after": (ROOT / ".github/workflows/research-allowlist.yml").read_text(encoding="utf-8")}
    results = {}
    with tempfile.TemporaryDirectory(prefix="r9-ci-") as temp:
        root = Path(temp)
        hook = root / "baseline/scripts/hooks/pre-commit-readonly-paths"
        hook.parent.mkdir(parents=True)
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        for name, source in scripts.items():
            script = mod._run_block_scalars(source)[0].split("python -", 1)[0]
            script = script.replace("${{ github.head_ref }}", mod.INJECTED_BRANCH)
            script += "\nprintf 'CHECKER_REACHED'\nexit 42\n"
            scenarios = ["injected"] if name == "before" else [
                "injected", "missing_env", "empty_env", "missing_baseline"]
            for case in scenarios:
                env = dict(os.environ, HEAD_REF=mod.INJECTED_BRANCH)
                if case == "missing_env":
                    env.pop("HEAD_REF", None)
                if case == "empty_env":
                    env["HEAD_REF"] = ""
                if case == "missing_baseline":
                    hook.unlink()  # only the file created above in this temp dir
                proc = subprocess.run([mod._bash_exe(), "--noprofile", "--norc",
                                       "-eo", "pipefail", "-c", script],
                                      cwd=root, env=env, capture_output=True, text=True)
                results[f"{name}/{case}"] = {
                    "exit": proc.returncode,
                    "checker_reached": "CHECKER_REACHED" in proc.stdout}
    return results


def fingerprint_edges():
    merge = module("r9_merge_extra", "scripts/merge_gate_power.py")
    originals = [read_json(ROOT / f"data/reports/gates/shard8-{i}/gate-power.json")
                 for i in range(5)]
    results = {}
    for case in ("missing", "missing_with_allow_legacy", "null", "empty_object",
                 "missing_negative", "missing_positive", "missing_g5"):
        docs = copy.deepcopy(originals)
        if case in ("missing_negative", "missing_positive", "missing_g5"):
            docs[0].pop(case.removeprefix("missing_"))
        else:
            for doc in docs:
                if case.startswith("missing"):
                    doc["meta"].pop("fingerprint")
                else:
                    doc["meta"]["fingerprint"] = None if case == "null" else {}
        with tempfile.TemporaryDirectory(prefix="r9-identity-") as temp:
            root = Path(temp)
            shards = []
            for i, doc in enumerate(docs):
                folder = root / str(i)
                folder.mkdir()
                (folder / "gate-power.json").write_text(json.dumps(doc), encoding="utf-8")
                shards.append(str(folder))
            argv = ["--shards", ",".join(shards), "--out", str(root / "out")]
            if case.endswith("allow_legacy"):
                argv.append("--allow-legacy")
            with contextlib.redirect_stdout(io.StringIO()):
                code = merge.main(argv)
            out = read_json(root / "out/gate-power.json") if code == 0 else {}
            results[case] = {"exit": code,
                "legacy_no_fingerprint": out.get("meta", {}).get("legacy_no_fingerprint", False)}
    merged = {"meta": merge.merge_meta(originals),
              "negative": merge.merge_negative(originals),
              "positive": merge.merge_positive(originals),
              "g5": merge.merge_g5(originals)}
    merged["flags"] = merge.recompute_flags(merged)
    rendered = merge.annotate_structural_flags(
        merge.gc.render_report(merged) + merge.render_supplement(merged), merged["flags"])
    official = (ROOT / "data/reports/gates/gate-power-v2/gate-power.md").read_text(encoding="utf-8")
    results["structural_annotation"] = {
        "fresh_render_contains_note": merge.STRUCTURAL_FLAG_NOTE in rendered,
        "existing_official_md_contains_note": merge.STRUCTURAL_FLAG_NOTE in official}
    return results


def demo_evidence_edges():
    fixture = module("r9_demo_tests", "tests/test_demo_check.py")
    check = fixture.load_demo_check()
    results = {}
    for case in ("p16", "paper_only_fixture", "orphan_acks_closed_zero",
                 "headings_only_reports", "paper_logs_without_deal_records"):
        with tempfile.TemporaryDirectory(prefix="r9-accept-") as temp:
            root = Path(temp)
            if case == "p16":
                fixture.build_p16_evidence(root)
            else:
                paths = fixture.build_full_evidence(root)
                if case == "orphan_acks_closed_zero":
                    arch = paths["live"] / "commands/archive"
                    for file in arch.glob("*.json"):
                        file.unlink()  # only synthetic files in this temp dir
                    for cid in ("a" * 32, "b" * 32):
                        (arch / f"flatten.ack-{cid}.json").write_text(
                            json.dumps({"id": cid, "closed": 0}), encoding="utf-8")
                elif case == "headings_only_reports":
                    for file in paths["reports"].glob("daily-*.md"):
                        file.write_text("# Daily report\n", encoding="utf-8")
                elif case == "paper_logs_without_deal_records":
                    paths["paper_state"].unlink()
                    with paths["runner_log"].open("a", encoding="utf-8") as f:
                        f.write("[2026-11-02T12:00:00] [paper] 保护性出场 1 笔：#101 reason=4\n")
                        f.write("[2026-11-03T12:00:00] [paper] 保护性出场 1 笔：#102 reason=5\n")
            code, output = fixture.run_cli(check, root, "--accept")
            results[case] = {"accept_exit": code,
                "acceptance_claimed": "结论：验收通过" in output,
                "output": output}
            if case == "p16":
                results[case]["day_exit"] = fixture.run_cli(
                    check, root, "--day", "2026-10-26")[0]
    return results


def powershell_native_argument():
    source = (ROOT / "scripts/demo_run.ps1").read_text(encoding="utf-8-sig")
    body = source.split("$demoFusePy = @'", 1)[1].split("'@", 1)[0]
    ast.parse(body)  # original source must be syntactically valid
    with tempfile.TemporaryDirectory(prefix="r9-ps-argv-") as temp:
        root = Path(temp)
        parse_only = root / "parse_only.py"
        parse_only.write_text(
            'import ast,sys,json\ns=sys.argv[1]\ntry:\n'
            ' ast.parse(s); print(json.dumps({"parsed":True}))\n'
            'except SyntaxError as e:\n'
            ' print(json.dumps({"parsed":False,"line":e.lineno,"text":e.text,"msg":e.msg}));sys.exit(1)\n',
            encoding="utf-8")
        script = root / "probe.ps1"
        q = lambda s: "'" + str(s).replace("'", "''") + "'"
        script.write_text("$demoFusePy = @'" + body + "'@\n& " + q(sys.executable)
                          + " " + q(parse_only) + " $demoFusePy\nexit $LASTEXITCODE\n",
                          encoding="utf-8-sig")
        proc = subprocess.run(["powershell", "-NoProfile", "-File", str(script)],
                              capture_output=True, text=True)
        return {"source_parses": True, "received_argument_parse_exit": proc.returncode,
                "detail": json.loads(proc.stdout),
                "gateway_code_executed": False}


def volume_validation():
    from mt5live.config import load
    sample = (ROOT / "demo-configs/DEMO-MA-XAUUSD-M15.toml").read_text(encoding="utf-8")
    sample = sample.replace("volume_lots = 0.01", "volume_lots = 0.02")
    sample += "\ncreated_utc = '''\nvolume_lots = 0.01\n'''\n"
    with tempfile.TemporaryDirectory(prefix="r9-volume-") as temp:
        path = Path(temp) / "config.toml"
        path.write_text(sample, encoding="utf-8")
        cfg = load(str(path))
        # Only the launcher's two Select-String predicates; never its gateway block.
        cmd = "$c=Get-Item -LiteralPath '" + str(path).replace("'", "''") + "'; "
        cmd += r"$riskFrac=@(Select-String -Path $c.FullName -Pattern '^\s*risk_fraction\s*='); "
        cmd += r"$lots=@(Select-String -Path $c.FullName -Pattern '^\s*volume_lots\s*=\s*0\.01\s*$'); "
        cmd += "if ($riskFrac.Count -eq 0 -and $lots.Count -eq 1) { 'ACCEPT'; exit 0 } else { 'REJECT'; exit 1 }"
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                              capture_output=True, text=True)
        return {"TOML_loaded_volume_lots": cfg.volume_lots,
                "launcher_volume_validation_exit": proc.returncode,
                "stdout": proc.stdout.strip(), "no_gateway_or_runner_executed": True}


def lifecycle_logdir():
    fixture = module("r9_ps_tests", "tests/test_demo_run_ps1.py")
    results = {}
    with tempfile.TemporaryDirectory(prefix="r9-lifecycle-") as temp:
        root = Path(temp)
        configs = root / "configs"
        configs.mkdir()
        (configs / "demo.toml").write_text(
            (ROOT / "demo-configs/DEMO-MA-XAUUSD-M15.toml").read_text(encoding="utf-8"),
            encoding="utf-8")
        log_a, log_b = root / "logs-a", root / "logs-b"
        log_a.mkdir()
        (log_a / "pids.txt").write_text(
            f"{os.getpid()}\trunner\t902601\tsynthetic.log\tDemo\t2026-10-27T00:00:00Z\n",
            encoding="utf-8")
        args = fixture.precheck_artifact_args(root)
        for label, logs in (("recorded_logdir_other_mode", log_a), ("changed_logdir_same_magic", log_b)):
            proc = fixture.run_ps1("-Mode", "PaperSoak", "-ConfigDir", str(configs),
                                   "-LogDir", str(logs), *args, "-PrecheckOnly")
            results[label] = {"exit": proc.returncode,
                "existing_runner_detected": "runner/902601" in fixture.out_text(proc)}
    return results


def recalc_comparison():
    import numpy as np
    import pandas as pd
    base = ROOT / "data/reports/reconciliation-v3"
    rec = module("r9_recalc_check", "scripts/reconcile_ab.py")
    results = {}
    for path in sorted(base.glob("*_r8i3-*/diff.csv")):
        summary = read_json(path.with_name("summary.json"))
        source = summary["meta"]["source_run"]
        old = pd.read_csv(base / source / "diff.csv")
        new = pd.read_csv(path)
        paired = old["match"] == "paired"
        seconds = 3600 if "H1" in source else 300
        same = (pd.to_datetime(old.entry_time_a, format="mixed").dt.floor(f"{seconds}s") ==
                pd.to_datetime(old.entry_time_b, format="mixed").dt.floor(f"{seconds}s"))
        keep = paired & same & (old.exit_reason_a == "stop") & (old.exit_reason_b == "stop")
        ov = old.net_overshoot_comp.where(keep, 0.0)
        rest = old.net_diff - old.net_spread_comp - ov
        net_ok = rest.abs() <= old.net_tol
        cols = [c for c in old.columns if c not in (
            "net_overshoot_comp", "net_rest", "net_ok", "row_ok")]
        bad = new[paired & ~new.net_ok.astype(bool)]
        listing = pd.read_csv(path.with_name("net_violations.csv"))
        computed_summary = rec.summarize_net_diffs(new)
        try:
            pd.testing.assert_frame_equal(old[cols], new[cols], check_exact=False,
                                          rtol=1e-12, atol=1e-12)
            equivalent = True
        except AssertionError:
            equivalent = False
        expected_row_ok = (old.loc[paired, "entry_time_ok"] & old.loc[paired, "exit_time_ok"]
                           & old.loc[paired, "reason_ok"] & net_ok[paired])
        results[source] = {
            "non_net_columns_equal_with_csv_float_roundoff": equivalent,
            "overshoot_matches": bool(np.allclose(ov[paired], new.loc[paired, "net_overshoot_comp"])),
            "rest_matches": bool(np.allclose(rest[paired], new.loc[paired, "net_rest"])),
            "net_ok_matches": bool((net_ok[paired] == new.loc[paired, "net_ok"]).all()),
            "row_ok_matches": bool((expected_row_ok == new.loc[paired, "row_ok"]).all()),
            "kept_stop_exit_checks_all_true": bool(old.loc[keep, "exit_time_ok"].all()),
            "summary_violations_match": computed_summary["violations"] == summary["violations"],
            "violation_listing_count": len(listing),
            "violation_listing_classes_match": listing.net_class.value_counts().to_dict() == bad.net_class.value_counts().to_dict(),
            "classes": bad.net_class.value_counts().to_dict(),
            "recalc": summary["meta"].get("recalc"),
            "inherited_ops_ack": summary["meta"].get("ops_ack"),
            "old_bad": int((~old.loc[paired, "net_ok"]).sum()),
            "new_bad": len(bad), "paired": int(paired.sum()),
            "stop_nonstop_old_overshoot_sum": float(old.loc[paired & (old.exit_reason_a == "stop") &
                (old.exit_reason_b != "stop"), "net_overshoot_comp"].sum())}
    return results


def main():
    old = module("r9_original_probes", "docs/reviews/probes/2026-10-26-round8-probes.py")
    p17_source = inspect.getsource(old.p17_reconciliation)
    adapted = p17_source.replace('"stop", "stop", ctx=ctx)', '"stop", "stop", "signal", ctx=ctx)')
    assert adapted != p17_source
    exec(adapted, old.__dict__)
    result = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()}
    for name, call in (
        ("bytes_and_assertions", bytes_and_assertions),
        ("ci_shell", ci_shell_probe), ("p14", old.p14_gate_artifacts),
        ("fingerprint_edges", fingerprint_edges), ("p16_and_accept_edges", demo_evidence_edges),
        ("demo_ps51_native_argument", powershell_native_argument),
        ("demo_volume_validation", volume_validation),
        ("lifecycle", lifecycle_logdir), ("p17", old.p17_reconciliation),
        ("p18", old.p18_paper_evidence), ("p19", old.p19_cost_and_ledger)):
        result[name] = call()
        print(f"completed {name}", flush=True)
    result["recalc_comparison"] = recalc_comparison()
    import sqlite3
    ledger = ROOT / "data/research/ledger.sqlite"
    with sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True) as conn:
        result["ops_rows_after_historical_445"] = conn.execute(
            "SELECT run_id, actor, created_utc FROM runs WHERE actor='ops' AND run_id>445").fetchall()
    result["old_rate_percent"] = 100 * (8364 - 158) / 8364
    result["new_rate_percent"] = 100 * (8364 - 213) / 8364
    if len(sys.argv) != 3 or sys.argv[1] != "--out":
        raise SystemExit("usage: --out review-evidence.json")
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"review evidence saved: {sys.argv[2]}")


if __name__ == "__main__":
    main()
