"""T180: assemble a FormalWindowV1 descriptor from runtime-retained evidence.

Inputs — nothing is guessed or backfilled; any missing piece is a hard error:
- run snapshots retained by demo_run.ps1: <data>/live/evidence/run-<i>/
  {manifest,launcher,instances}.json, plus the current run-manifest.json;
- the supervisor restart journal <data>/live/ops-restarts.jsonl (checker's
  exact 6-field rows) bound to its retained registry copies through
  <data>/live/evidence/instances/index.jsonl (seq == physical journal line);
- the deployment record: a physical line range in a phase-f-log copy that
  contains exactly one full deployment SHA, exactly one explicit UTC
  completion timestamp, and every anchor launcher PID literally;
- a calendar source file with exactly one dated weekday/weekend/holiday line
  per calendar date from completion to today.

Output: <root>/formal.json and <root>/calendar.json, field-for-field the
demo_check.py FormalWindowV1 contract; the result is re-validated with
demo_check.validate_formal_descriptor before anything is written.

The journal rows themselves stay exactly six fields (checker contract), so the
runtime {path, sha256} evidence binding lives in index.jsonl, written by the
supervisor inside the same restart critical section (see runbook section 4.1).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SHA_RE = re.compile(r"(?<![\w:.+-])[0-9a-f]{40}(?![\w:.+-])")
UTC_RE = re.compile(r"(?<![\w:.+-])\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|\+00:00)(?![\w:.+-])")
REASON_RE = re.compile(r"\b(weekday|weekend|holiday)\b")


def fail(message: str) -> None:
    raise SystemExit(f"formal_descriptor: {message}")


def _utc(value, alias):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|\+00:00)", value):
        fail(f"{alias}: not an explicit UTC timestamp: {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _lines(path: Path, alias):
    try:
        rows = path.read_bytes().splitlines(keepends=True)
    except OSError:
        fail(f"{alias}: unreadable: {path.name}")
    return rows


def _json(path: Path, alias):
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        fail(f"{alias}: unreadable/invalid JSON: {path.name}")


def _ref(root: Path, path: Path):
    return {"path": path.resolve().relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _inside(root: Path, value: str, alias):
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
        path.relative_to(root)
    except (OSError, ValueError):
        fail(f"{alias}: must stay inside the evidence root")
    return path


def _roles(instances_doc, alias):
    if not isinstance(instances_doc, dict) or instances_doc.get("schema_version") != 1 \
            or not isinstance(instances_doc.get("instances"), list):
        fail(f"{alias}: bad instances snapshot schema")
    roles = {}
    for e in instances_doc["instances"]:
        if not isinstance(e, dict) or not isinstance(e.get("pid"), int) or e["pid"] <= 0:
            fail(f"{alias}: bad entry identity")
        key = str(e["magic"]) if e.get("kind") == "runner" else e.get("kind")
        if key in roles:
            fail(f"{alias}: duplicate role {key}")
        roles[key] = e
    return roles


def _run_snapshots(live: Path, index: int, *, required, partial=None):
    """Retained run-i snapshots; None when absent/invalid and not required.

    Pre-anchor runs legitimately have no retained snapshots (older deployments
    append to the same run-manifest); only runs after the anchor are hard
    requirements. A run directory that exists but is incomplete is recorded in
    ``partial`` so the anchor error can name it.
    """
    base = live / "evidence" / f"run-{index}"
    if not base.is_dir():
        return None
    try:
        docs = {name: _json(base / f"{name}.json", f"run-{index}/{name}")
                for name in ("manifest", "launcher", "instances")}
        _roles(docs["instances"], f"run-{index}/instances")
    except SystemExit:
        if required:
            raise
        if partial is not None:
            partial.append(f"run-{index}")
        return None
    return base, docs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="证据根（含 data/、部署记录与日历源）")
    ap.add_argument("--deploy-log", required=True, help="部署记录所在文件（须在根内）")
    ap.add_argument("--deploy-lines", nargs=2, type=int, required=True, metavar=("FIRST", "LAST"),
                    help="部署记录的一基物理行区间")
    ap.add_argument("--calendar", required=True, help="日历源文件（须在根内）")
    ap.add_argument("--drill-magic", action="append", type=int, metavar="MAGIC", default=[],
                    help="演练 magic（可重复；写入描述符可选字段 drill_magics，"
                         "与部署清单 magics 不得相交——换日禁入探针等用）")
    ap.add_argument("--out", help="描述符输出（缺省 <root>/formal.json，须在根内）")
    ap.add_argument("--calendar-out", help="日历 JSON 输出（缺省 <root>/calendar.json，须在根内）")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    live = root / "data" / "live"
    if not live.is_dir():
        fail(f"missing {live.relative_to(root).as_posix()}; the root must contain the retained data tree")
    deploy_log = _inside(root, args.deploy_log, "deploy-log")
    calendar_src = _inside(root, args.calendar, "calendar")
    out_path = _inside(root, args.out or "formal.json", "out")
    calendar_out = _inside(root, args.calendar_out or "calendar.json", "calendar-out")

    manifest = _json(live / "run-manifest.json", "run-manifest")
    if manifest.get("schema_version") != 1 or manifest.get("mode") != "Demo" \
            or not isinstance(manifest.get("runs"), list) or not manifest["runs"]:
        fail("run-manifest: not a Demo manifest with runs")

    # -- deployment claim: exactly one SHA, one UTC completion, all anchor PIDs.
    first, last = args.deploy_lines
    log_lines = _lines(deploy_log, "deploy-log")
    if not (1 <= first <= last <= len(log_lines)):
        fail("deploy-lines: out of range")
    excerpt = b"".join(log_lines[first - 1:last])
    try:
        record = excerpt.decode("utf-8")
    except UnicodeError:
        fail("deploy-log: excerpt is not UTF-8")
    shas = set(SHA_RE.findall(record))
    times = UTC_RE.findall(record)
    if len(shas) != 1:
        fail(f"deploy record must contain exactly one full SHA, found {len(shas)}")
    if len(times) != 1:
        fail(f"deploy record must contain exactly one explicit UTC completion, found {len(times)}")
    completed = _utc(times[0], "deploy record")

    # -- anchor: the unique fully-launched run whose PIDs the record lists.
    candidates, partial = [], []
    for i, run in enumerate(manifest["runs"]):
        snapshots = _run_snapshots(live, i, required=False, partial=partial)
        if snapshots is None:
            continue
        base, docs = snapshots
        roles = _roles(docs["instances"], f"run-{i}/instances")
        shas_i = {e.get("deployment_sha") for e in roles.values()}
        if len(shas_i) != 1 or shas_i != shas:
            continue
        launcher = docs["launcher"]
        processes = launcher.get("processes") if isinstance(launcher, dict) else None
        if not isinstance(processes, list) or launcher.get("skipped_alive_runners"):
            continue
        if {str(p.get("name") if p.get("kind") == "runner" else p.get("kind")) for p in processes
                if isinstance(p, dict)} != set(roles):
            continue
        if _utc(launcher.get("started_utc"), f"run-{i}/launcher") > completed:
            continue
        if not all(re.search(r"(?<!\d)" + str(e["pid"]) + r"(?!\d)", record) for e in roles.values()):
            continue
        candidates.append((i, base, docs, roles))
    if len(candidates) != 1:
        detail = f"; incomplete retained evidence: {', '.join(partial)}" if partial else ""
        fail(f"anchor: expected exactly one fully-launched run listed in the deploy record, "
             f"found {len(candidates)}{detail}")
    anchor_i, anchor_base, anchor_docs, anchor_roles = candidates[0]
    deployment_sha = next(iter(shas))
    magics = sorted(int(k) for k in anchor_roles if k.isdigit())
    if sorted(manifest.get("magics", [])) != magics or not magics:
        fail("anchor: run-manifest magics disagree with the anchor instances snapshot")
    drill_magics = sorted(set(args.drill_magic))
    if any(m <= 0 for m in drill_magics):
        fail("--drill-magic: must be positive integers")
    if set(drill_magics) & set(magics):
        fail(f"--drill-magic: must not overlap the deployment magics {magics}")

    # -- every post-anchor run needs retained snapshots.
    run_bindings = []
    for i, run in enumerate(manifest["runs"][anchor_i + 1:], anchor_i + 1):
        if run.get("mode", manifest["mode"]) != "Demo":
            fail(f"run-{i}: non-Demo run after the anchor cannot be bound")
        snapshots = _run_snapshots(live, i, required=True)
        base, docs = snapshots
        run_bindings.append({"run_index": i, **{
            name + "_snapshot": _ref(root, base / f"{name}.json")
            for name in ("manifest", "launcher", "instances")}})

    # -- restart bindings via the runtime evidence index (seq == journal line).
    restart_bindings = []
    index_rows = {}
    index_path = live / "evidence" / "instances" / "index.jsonl"
    for line in (index_path.read_text(encoding="utf-8").splitlines() if index_path.exists() else []):
        if line.strip():
            row = json.loads(line)
            index_rows[row.get("seq")] = row
    anchor_start = _utc(manifest["runs"][anchor_i]["started_utc"], "anchor run")
    for n, line in enumerate(_lines(live / "ops-restarts.jsonl", "ops-restarts"), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if _utc(row["utc"], "journal") < anchor_start:
            continue
        if row.get("deployment_sha") != deployment_sha:
            fail(f"journal line {n}: deployment_sha differs from the anchor")
        entry = index_rows.get(n)
        if entry is None or entry.get("utc") != row["utc"] or entry.get("component") != row["component"]:
            fail(f"journal line {n}: no matching runtime evidence index entry")
        binding = {"line_number": n}
        for side in ("before_instances", "after_instances"):
            ref = entry.get(side)
            if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
                fail(f"journal line {n}: malformed index entry")
            path = live / ref["path"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if digest != ref["sha256"]:
                fail(f"journal line {n}: retained {side} missing or altered")
            binding[side] = _ref(root, path)
        restart_bindings.append(binding)

    # -- calendar: one sourced line per natural day, completion through today.
    cal_lines = _lines(calendar_src, "calendar")
    days = []
    day = completed.date()
    today = datetime.now(timezone.utc).date()
    while day <= today:
        matches = [ln for ln, text in enumerate(
            (l.decode("utf-8", errors="replace") for l in cal_lines), 1)
            if re.search(r"(?<!\d)" + str(day) + r"(?!\d)", text)]
        if len(matches) != 1:
            fail(f"calendar: {day} needs exactly one source line, found {len(matches)}")
        line = cal_lines[matches[0] - 1].decode("utf-8", errors="replace")
        reasons = REASON_RE.findall(line)
        if len(reasons) != 1:
            fail(f"calendar: {day} line must carry exactly one weekday/weekend/holiday token")
        reason = reasons[0]
        weekday_ok = (reason == "weekday" and day.weekday() < 5) or \
                     (reason == "weekend" and day.weekday() >= 5) or reason == "holiday"
        if not weekday_ok:
            fail(f"calendar: {day} token {reason} contradicts the weekday")
        if reason == "holiday" and not re.sub(r"[\W_]", "", line.replace(str(day), "").replace("holiday", "")):
            fail(f"calendar: {day} holiday line lacks a concrete reason")
        days.append({"date": str(day), "trading": reason == "weekday",
                     "reason": reason, "source_lines": [matches[0], matches[0]]})
        day += timedelta(days=1)
    calendar_doc = {"schema_version": 1, "timezone": "UTC", "market": "XAUUSD",
                    "source": _ref(root, calendar_src), "days": days}

    descriptor = {
        "schema_version": 1, "mode": "Demo", "deployment_sha": deployment_sha, "magics": magics,
        **({"drill_magics": drill_magics} if drill_magics else {}),
        "anchor": {"run_index": anchor_i, **{
            name + "_snapshot": _ref(root, anchor_base / f"{name}.json")
            for name in ("manifest", "launcher", "instances")},
            "deployment_claim": {
                "source": _ref(root, deploy_log), "source_lines": [first, last],
                "excerpt_sha256": hashlib.sha256(excerpt).hexdigest(),
                "completed_utc": times[0],
                "processes": [{"kind": e["kind"], "magic": e.get("magic"), "pid": e["pid"]}
                              for e in anchor_docs["instances"]["instances"]]}},
        "run_bindings": run_bindings, "restart_bindings": restart_bindings,
        "calendar": {"path": calendar_out.relative_to(root).as_posix(), "sha256": ""}}

    spec = importlib.util.spec_from_file_location("demo_check", Path(__file__).with_name("demo_check.py"))
    checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checker  # dataclass needs the module registered
    spec.loader.exec_module(checker)
    calendar_out.write_bytes((json.dumps(calendar_doc, ensure_ascii=False) + "\n").encode("utf-8"))
    descriptor["calendar"]["sha256"] = hashlib.sha256(calendar_out.read_bytes()).hexdigest()
    try:
        checker.validate_formal_descriptor(descriptor)
    except Exception as exc:
        fail(f"internal: assembled descriptor failed demo_check validation ({exc})")
    out_path.write_bytes((json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"anchor run_index={anchor_i} runs_bound={len(run_bindings)} restarts_bound={len(restart_bindings)} "
          f"calendar_days={len(days)} drill_magics={drill_magics or '—'} descriptor={out_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
