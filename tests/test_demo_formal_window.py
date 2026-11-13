"""D57 offline formal-window contract. Every CLI input is synthetic and immutable."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo_check.py"
SHA = "a" * 40
MAGICS = [902601, 902602]
SESSIONS_TEXT = '''version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 5
post_minutes = 60
exempt_symbols = []
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
min_days_per_hour = 20
'''


def checker():
    spec = importlib.util.spec_from_file_location("formal_checker", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def iso(t):
    return t.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def put(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def get(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def ref(root, path):
    return {"path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def snapshots(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def invoke(root, *flags):
    before = snapshots(root)
    args = [sys.executable, str(SCRIPT), "--data-dir", str(root / "data"),
            "--log-dir", str(root / "logs"), "--reports-dir", str(root / "reports"),
            "--sessions-config", str(root / "sessions.toml"), *flags]
    result = subprocess.run(args, cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"},
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert snapshots(root) == before, "checker modified its evidence"
    return result


def formal(root):
    return invoke(root, "--accept", "--formal-window", str(root / "formal.json"))


def assert_result(result, code, detail=""):
    out = result.stdout + result.stderr
    assert result.returncode == code, out
    assert "historical_identity_verified=false" in out, out
    assert "a5_status=not_evaluated" in out, out
    assert "验收通过" not in out, out
    assert detail in out, out
    return out


def build_evidence(root, trading_count=12):
    """A complete production-shaped retained history, never a real deployment."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day = now.date() - timedelta(days=1)
    trading = []
    while len(trading) < trading_count:
        if day.weekday() < 5:
            trading.append(day)
        day -= timedelta(days=1)
    trading.reverse()
    start = datetime.combine(trading[0], datetime.min.time())
    live, logs, reports = root / "data/live", root / "logs", root / "reports/live"
    for p in (live, logs, reports, root / "retained"):
        p.mkdir(parents=True, exist_ok=True)
    (root / "sessions.toml").write_text(SESSIONS_TEXT, encoding="utf-8")
    deploy, data, logdir = "C:/synthetic/deployment", "C:/synthetic/data", "C:/synthetic/logs"
    entries, processes = [], []
    for i, (kind, magic, script) in enumerate([
        ("runner", MAGICS[0], "live_runner.py"),
        ("runner", MAGICS[1], "live_runner.py"),
        ("risk", None, "risk_process.py"), ("reconcile", None, "live_reconcile.py")]):
        argv = [deploy + "/.venv/Scripts/python.exe", "-u", deploy + "/scripts/" + script,
                "--data-dir", data, "--loop"]
        if kind == "runner":
            argv += ["--config", deploy + f"/configs/{magic}.toml", "--gateway-config",
                     deploy + "/gateway.json", "--risk-config", deploy + "/risk.toml",
                     "--sessions-config", deploy + "/sessions.toml", "--spread-model",
                     data + "/spread.json"]
        else:
            argv += ["--gw-config", deploy + "/gateway.json", "--interval",
                     "10.0" if kind == "risk" else "30.0"]
            if kind == "risk":
                argv += ["--config", deploy + "/risk.toml", "--sessions-config", deploy + "/sessions.toml"]
        name = str(magic) if magic else ("account" if kind == "risk" else "all")
        log = logdir + f"/{kind}-{name}-initial.log"
        entries.append(dict(pid=100 + i, kind=kind, magic=magic, mode="Demo",
                            started_utc=iso(start - timedelta(seconds=5)), log=log, argv=argv,
                            deployment_sha=SHA, deployment_dir=deploy, data_dir=data,
                            gateway_config=deploy + "/gateway.json", account_hint="demo"))
        processes.append(dict(kind=kind, name=name, pid=100 + i, log=log, err_log=log + ".err",
                              started_utc=iso(start - timedelta(seconds=4)), command=" ".join(argv)))
    manifest = dict(schema_version=1, mode="Demo", magics=MAGICS, account_hint="demo",
                    runs=[dict(started_utc=iso(start - timedelta(seconds=3)), log_dir=logdir,
                               kind="start", mode="Demo")])
    launcher = dict(started_utc=iso(start - timedelta(seconds=2)), mode="Demo", repo_root=deploy,
                    config_dir=deploy + "/configs", paper_state_dir=data + "/live/paper",
                    skipped_alive_runners=[], processes=processes)
    put(live / "run-manifest.json", manifest)
    put(live / "instances.json", dict(schema_version=1, instances=entries))
    put(root / "retained/manifest-0.json", manifest)
    put(root / "retained/launcher-0.json", launcher)
    put(root / "retained/instances-0.json", dict(schema_version=1, instances=entries))
    claim_processes = [dict(kind=e["kind"], magic=e["magic"], pid=e["pid"]) for e in entries]
    source = (f"Synthetic deployment {SHA} completed {iso(start)}\r\n" +
              "\r\n".join(f"{e['kind']} magic={e['magic']} pid={e['pid']}" for e in entries) + "\r\n")
    (root / "retained/deployment.txt").write_bytes(source.encode("utf-8"))
    binding = dict(run_index=0, manifest_snapshot=ref(root, root / "retained/manifest-0.json"),
                   launcher_snapshot=ref(root, root / "retained/launcher-0.json"),
                   instances_snapshot=ref(root, root / "retained/instances-0.json"))
    binding["deployment_claim"] = dict(source=ref(root, root / "retained/deployment.txt"),
        source_lines=[1, 5], excerpt_sha256=hashlib.sha256(source.encode()).hexdigest(),
        completed_utc=iso(start), processes=claim_processes)
    dates = [start.date() + timedelta(days=i) for i in range((now.date() - start.date()).days + 1)]
    calendar_source = "".join(f"{d} {'weekday' if d.weekday() < 5 else 'weekend'}\n" for d in dates)
    (root / "retained/calendar.txt").write_text(calendar_source, encoding="utf-8")
    calendar = dict(schema_version=1, timezone="UTC", market="XAUUSD",
                    source=ref(root, root / "retained/calendar.txt"),
                    days=[dict(date=str(d), trading=d.weekday() < 5,
                               reason="weekday" if d.weekday() < 5 else "weekend",
                               source_lines=[i + 1, i + 1]) for i, d in enumerate(dates)])
    put(root / "calendar.json", calendar)
    descriptor = dict(schema_version=1, mode="Demo", deployment_sha=SHA, magics=MAGICS,
                      anchor=binding, run_bindings=[], restart_bindings=[],
                      calendar=ref(root, root / "calendar.json"))
    put(root / "formal.json", descriptor)
    for magic in MAGICS:
        put(live / f"state-{magic}.json", dict(magic=magic, known_positions={},
            pending_external_exits={}, updated_at_utc=iso(start)))
        put(live / f"expected-{magic}.json", dict(magic=magic, symbol="XAUUSD", side="buy",
            volume=0.0, status="confirmed", cause="synthetic", ts=iso(start)))
        put(live / f"pause-{magic}.json", dict(magic=magic, paused=False, reason=None,
            ts=iso(start + timedelta(hours=6)), diffs=[]))
    bar = iso(start + timedelta(hours=10))
    deal = dict(ts=iso(start + timedelta(hours=10, seconds=6)), magic=MAGICS[0], ticket=1,
                event="fill_open", price=4000.4, side="buy", volume=.01, reason=None, decision_bar=bar)
    deals = [deal] + [dict(ts=iso(start + timedelta(hours=11 + i)), magic=MAGICS[0],
        ticket=i + 1, event="external_exit", price=4001., side="sell", volume=.01,
        reason=reason, decision_bar=None) for i, reason in enumerate(["SL", "TP"])]
    lines(live / f"deals-{MAGICS[0]}.jsonl", deals)
    lines(live / f"orders-{MAGICS[0]}.jsonl", [dict(ts=deal["ts"], magic=MAGICS[0], kind="open",
        symbol="XAUUSD", side="buy", lots=.01, decision_id="XAUUSD|M15|" + bar)])
    (live / "alerts.log").write_text("\n".join([
        f"{iso(start + timedelta(hours=1))}\tRISK_HALT\tkill_switch",
        f"{iso(start + timedelta(hours=2))}\tHEARTBEAT_TIMEOUT\theartbeat-{MAGICS[0]}.json",
        f"{iso(start + timedelta(hours=3))}\tRECONCILE_MISMATCH\t已暂停 magic [{MAGICS[0]}]",
        f"{iso(start + timedelta(hours=4))}\tRISK_REJECT\tmagic {MAGICS[0]} forbidden_session",
    ]) + "\n", encoding="utf-8")
    for magic in MAGICS:
        text = f"mt5live 启动：XAUUSD M15 magic={magic} strategy=synthetic [LIVE]\n"
        text += f"[{start + timedelta(hours=2, seconds=30):%Y-%m-%dT%H:%M:%S}] 收到中断，退出\n"
        text += f"[{start + timedelta(hours=2, minutes=1):%Y-%m-%dT%H:%M:%S}] mt5live 启动：XAUUSD M15 magic={magic}\n"
        text += f"[{start + timedelta(hours=5):%Y-%m-%dT%H:%M:%S}] gateway_reconnected\n"
        if magic == MAGICS[0]:
            text += f"[{start + timedelta(hours=10, seconds=5):%Y-%m-%dT%H:%M:%S}] 决策 XAUUSD|M15|{bar}：signal=1 bid=4000.1 ask=4000.3\n"
        text += "".join(f"[{d}T00:00:00] 运行器存活\n" for d in dates)
        (logs / f"runner-{magic}.log").write_text(text, encoding="utf-8")
    risk = []
    for d in dates:
        for h in range(24):
            t = datetime.combine(d, datetime.min.time()) + timedelta(hours=h)
            if t <= now:
                risk.append(f"· {t:%Y-%m-%d %H:%M:%S} UTC [live] 心跳=ok halt=否 flatten=—")
    (logs / "risk.log").write_text("\n".join(risk) + "\n", encoding="utf-8")
    (logs / "reconcile.log").write_text("".join(
        f"· {d} 00:00:00 UTC 对账一致（0 个持仓）\n" for d in dates), encoding="utf-8")
    for d in trading:
        (reports / f"daily-{d}.md").write_text(f"# {d}\n| XAUUSD | 10 | 3 | 0.2 | 0.3 | 正常 |\n", encoding="utf-8")
    for i in range(2):
        t = start + timedelta(hours=14 + i)
        cid = f"synthetic-{i}"
        put(live / f"drill-archive/{cid}/flatten.json", dict(command="flatten_all", id=cid, reason="test", ts=iso(t)))
        for magic in MAGICS:
            put(live / f"drill-archive/{cid}/flatten.ack-{magic}", dict(id=cid, magic=magic, closed=0, ts=iso(t + timedelta(seconds=1))))
    return SimpleNamespace(root=root, live=live, logs=logs, reports=reports, now=now, start=start,
                           trading=trading, descriptor=descriptor)


def save_descriptor(f):
    put(f.root / "formal.json", f.descriptor)


def refresh(f, ref_doc):
    ref_doc.update(ref(f.root, f.root / ref_doc["path"]))


def append_run(f, sha=SHA):
    manifest = get(f.live / "run-manifest.json")
    idx = len(manifest["runs"])
    when = f.start + timedelta(days=idx, hours=1)
    manifest["runs"].append(dict(started_utc=iso(when), mode="Demo", kind="start", log_dir="C:/synthetic/logs"))
    registry = get(f.live / "instances.json")
    launcher = get(f.root / "retained/launcher-0.json")
    launcher["started_utc"] = iso(when + timedelta(seconds=1))
    for e, p in zip(registry["instances"], launcher["processes"]):
        e.update(pid=e["pid"] + 10, started_utc=iso(when - timedelta(seconds=1)), deployment_sha=sha)
        p.update(pid=e["pid"], started_utc=iso(when))
    for key, doc in (("manifest", manifest), ("instances", registry), ("launcher", launcher)):
        put(f.root / f"retained/{key}-{idx}.json", doc)
    put(f.live / "run-manifest.json", manifest)
    put(f.live / "instances.json", registry)
    f.descriptor["run_bindings"].append(dict(run_index=idx, **{
        key + "_snapshot": ref(f.root, f.root / f"retained/{key}-{idx}.json")
        for key in ("manifest", "instances", "launcher")}))
    save_descriptor(f)


def append_restart(f, sha=SHA, component=None):
    component = str(MAGICS[0]) if component is None else component
    journal = f.live / "ops-restarts.jsonl"
    rows = [json.loads(x) for x in journal.read_text().splitlines()] if journal.exists() else []
    n = len(rows) + 1
    before = get(f.live / "instances.json")
    after = copy.deepcopy(before)
    e = next(e for e in after["instances"] if (str(e["magic"]) if e["kind"] == "runner" else e["kind"]) == component)
    old = e["pid"]
    when = f.start + timedelta(hours=7 + n)
    e.update(pid=old + 20, started_utc=iso(when + timedelta(seconds=3)), deployment_sha=sha)
    # supervisor omits launcher-only fields and records observation before launch.
    e.pop("gateway_config", None)
    e.pop("account_hint", None)
    for label, doc in (("before", before), ("after", after)):
        put(f.root / f"retained/restart-{n}-{label}.json", doc)
    rows.append(dict(utc=iso(when), component=component, reason_code="process_missing", old_pid=old,
                     new_pid=e["pid"], deployment_sha=sha))
    lines(journal, rows)
    put(f.live / "instances.json", after)
    f.descriptor["restart_bindings"].append(dict(line_number=n, **{
        label + "_instances": ref(f.root, f.root / f"retained/restart-{n}-{label}.json") for label in ("before", "after")}))
    save_descriptor(f)


def test_formal_complete_is_mechanical_only(tmp_path):
    build_evidence(tmp_path)
    assert_result(formal(tmp_path), 3, "mechanical_checks_passed=true")


def test_bare_accept_rejects_before_default_reads(tmp_path):
    # Running in a synthetic cwd also makes a regression harmless.
    result = subprocess.run([sys.executable, str(SCRIPT), "--accept"], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8")
    assert_result(result, 2, "formal-window")
    assert "会话配置" not in result.stderr
    assert "先启动" not in result.stdout


def test_invalid_descriptor_precedes_any_data_read(tmp_path):
    (tmp_path / "formal.json").write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    assert_result(formal(tmp_path), 2, "descriptor")


def test_formal_requires_explicit_paths_and_accept(tmp_path):
    build_evidence(tmp_path)
    for flags in (["--accept"], ["--summary"]):
        p = subprocess.run([sys.executable, str(SCRIPT), *flags, "--formal-window", str(tmp_path / "formal.json")],
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        assert_result(p, 2)


def test_old_ten_plus_new_nine_and_old_drills_cannot_pass(tmp_path):
    f = build_evidence(tmp_path, 9)
    old = f.start - timedelta(days=20)
    mf = get(f.live / "run-manifest.json")
    mf["runs"].insert(0, dict(started_utc=iso(old), mode="Demo", kind="start", log_dir="C:/old/logs"))
    put(f.live / "run-manifest.json", mf)
    put(f.root / "retained/manifest-0.json", mf)
    f.descriptor["anchor"]["run_index"] = 1
    refresh(f, f.descriptor["anchor"]["manifest_snapshot"])
    save_descriptor(f)
    # Older same-mode activity gives the old reader >10 days, and old SL/TP
    # remains visible to its sample predicate even outside a narrowed run_window.
    for i in range(20):
        d = old.date() + timedelta(days=i)
        (f.reports / f"daily-{d}.md").write_text("| XAUUSD | 10 | 1 | 正常 |\n", encoding="utf-8")
    deals = [json.loads(x) for x in (f.live / f"deals-{MAGICS[0]}.jsonl").read_text().splitlines()]
    for d in deals[1:]:
        d["ts"] = iso(old + timedelta(hours=12))
    lines(f.live / f"deals-{MAGICS[0]}.jsonl", deals)
    out = assert_result(formal(tmp_path), 1)
    assert "9" in out and "SL" in out


@pytest.mark.parametrize("corruption", ["missing", "short_sha", "approximate_time", "pid_substring", "hash", "unknown", "bool"])
def test_identity_evidence_is_required(tmp_path, corruption):
    f = build_evidence(tmp_path)
    a = f.descriptor["anchor"]
    if corruption == "missing":
        (tmp_path / a["instances_snapshot"]["path"]).unlink()
    elif corruption in {"short_sha", "approximate_time", "pid_substring"}:
        p = tmp_path / a["deployment_claim"]["source"]["path"]
        text = p.read_bytes()
        text = text.replace(SHA.encode(), SHA[:8].encode()) if corruption == "short_sha" else (
            text.replace(iso(f.start).encode(), b"13:2x UTC") if corruption == "approximate_time" else text.replace(b"pid=100", b"pid=1009"))
        p.write_bytes(text)
        refresh(f, a["deployment_claim"]["source"])
        a["deployment_claim"]["excerpt_sha256"] = hashlib.sha256(text).hexdigest()
    elif corruption == "hash":
        a["instances_snapshot"]["sha256"] = "0" * 64
    elif corruption == "unknown":
        f.descriptor["approved_by"] = "nobody"
    else:
        a["run_index"] = False
    save_descriptor(f)
    assert_result(formal(tmp_path), 2)


@pytest.mark.parametrize("kind", ["launcher", "supervisor"])
def test_mixed_sha_then_return_is_invalid(tmp_path, kind):
    f = build_evidence(tmp_path)
    add = append_run if kind == "launcher" else append_restart
    add(f, "b" * 40)
    add(f, SHA)
    assert_result(formal(tmp_path), 2)


@pytest.mark.parametrize("kind", ["launcher", "supervisor", "risk"])
def test_same_sha_restart_keeps_original_window(tmp_path, kind):
    f = build_evidence(tmp_path, 10)
    if kind == "launcher":
        append_run(f)
    else:
        append_restart(f, component="risk" if kind == "risk" else None)
    out = assert_result(formal(tmp_path), 3)
    assert str(f.start.date()) in out


def test_moving_anchor_conflicts_with_original_claim(tmp_path):
    f = build_evidence(tmp_path)
    append_run(f)
    old_claim = f.descriptor["anchor"]["deployment_claim"]
    f.descriptor["anchor"] = f.descriptor["run_bindings"].pop()
    f.descriptor["anchor"]["deployment_claim"] = old_claim
    save_descriptor(f)
    assert_result(formal(tmp_path), 2)


@pytest.mark.parametrize("which", ["first", "last"])
def test_missing_required_boundary_day_is_not_hidden_by_ten_middle_days(tmp_path, which):
    f = build_evidence(tmp_path, 12)
    day = f.trading[0 if which == "first" else -1]
    (f.reports / f"daily-{day}.md").unlink()
    for p in f.logs.glob("*.log"):
        p.write_text("\n".join(x for x in p.read_text(encoding="utf-8").splitlines() if str(day) not in x), encoding="utf-8")
    out = assert_result(formal(tmp_path), 1)
    assert str(day) in out


@pytest.mark.parametrize("change", ["gap", "holiday", "duplicate", "timezone"])
def test_calendar_must_be_complete_and_sourced(tmp_path, change):
    f = build_evidence(tmp_path)
    cal = get(tmp_path / "calendar.json")
    if change == "gap":
        cal["days"].pop()
    elif change == "holiday":
        cal["days"][0].update(trading=False, reason="holiday")
    elif change == "duplicate":
        cal["days"].append(cal["days"][0])
    else:
        cal["timezone"] = "Asia/Shanghai"
    put(tmp_path / "calendar.json", cal)
    refresh(f, f.descriptor["calendar"])
    save_descriptor(f)
    assert_result(formal(tmp_path), 2, "calendar")


@pytest.mark.parametrize("change", ["old_sl_tp", "future_recovery", "future_ack", "other_magic", "old_reference", "other_reference"])
def test_event_own_time_and_magic_bound_all_supporting_evidence(tmp_path, change):
    f = build_evidence(tmp_path)
    if change == "old_sl_tp":
        p = f.live / f"deals-{MAGICS[0]}.jsonl"
        ds = [json.loads(x) for x in p.read_text().splitlines()]
        for d in ds[1:]:
            d["ts"] = iso(f.start - timedelta(seconds=1))
        lines(p, ds)
    elif change == "future_recovery":
        p = f.logs / "risk.log"
        p.write_text(p.read_text(encoding="utf-8").replace("halt=否", "halt=是") +
                     f"· {f.now + timedelta(days=1):%Y-%m-%d %H:%M:%S} UTC [live] 心跳=ok halt=否\n", encoding="utf-8")
    elif change == "future_ack":
        for p in (f.live / "drill-archive").rglob("*"):
            if p.is_file():
                doc = get(p)
                doc["ts"] = iso(f.now - timedelta(seconds=5) if "command" in doc else f.now + timedelta(minutes=10))
                put(p, doc)
    elif change == "other_magic":
        put(f.live / f"pause-{MAGICS[0]}.json", dict(magic=MAGICS[0], paused=False, reason=None, ts=iso(f.start), diffs=[]))
    else:
        p = f.logs / f"runner-{MAGICS[0]}.log"
        text = p.read_text(encoding="utf-8")
        quote = next(x for x in text.splitlines() if "决策 " in x)
        p.write_text(text.replace(quote + "\n", ""), encoding="utf-8")
        orders = f.live / f"orders-{MAGICS[0]}.jsonl"
        orders.write_text("", encoding="utf-8")
        dest = p if change == "old_reference" else f.logs / f"runner-{MAGICS[1]}.log"
        if change == "old_reference":
            quote = f"[{f.start - timedelta(seconds=1):%Y-%m-%dT%H:%M:%S}]" + quote.split("]", 1)[1]
        dest.write_text(dest.read_text(encoding="utf-8") + quote + "\n", encoding="utf-8")
    assert_result(formal(tmp_path), 1)


@pytest.mark.parametrize("state", ["pause", "pending", "intent", "killSwitch", "halt.json", "halt-risk-veto.json"])
def test_pre_window_current_protection_blocks_without_latest_activity(tmp_path, state):
    f = build_evidence(tmp_path)
    old = iso(f.start - timedelta(days=20))
    if state == "pause":
        put(f.live / f"pause-{MAGICS[1]}.json", dict(magic=MAGICS[1], paused=True, reason="manual", ts=old))
    elif state == "pending":
        p = f.live / f"state-{MAGICS[1]}.json"
        doc = get(p)
        doc["pending_external_exits"] = {"42": {"ts": old}}
        put(p, doc)
    elif state == "intent":
        p = f.live / f"expected-{MAGICS[1]}.json"
        doc = get(p)
        doc.update(volume=.01, ts=old)
        put(p, doc)
    else:
        (f.live / state).write_text("" if state == "killSwitch" else json.dumps({"ts": old, "reason": "manual"}), encoding="utf-8")
    assert_result(formal(tmp_path), 1, "当前")


@pytest.mark.parametrize("bad", ["journal", "deleted_prefix", "unbound_restart", "missing_ts", "bad_json", "bad_log", "bad_state", "bool_closed"])
def test_malformed_or_unbound_raw_evidence_fails_closed(tmp_path, bad):
    f = build_evidence(tmp_path)
    if bad == "journal":
        (f.live / "ops-restarts.jsonl").write_text("{bad\n", encoding="utf-8")
    elif bad == "deleted_prefix":
        append_run(f)
        doc = get(f.live / "run-manifest.json")
        doc["runs"].pop(0)
        put(f.live / "run-manifest.json", doc)
    elif bad == "unbound_restart":
        append_restart(f)
        f.descriptor["restart_bindings"] = []
        save_descriptor(f)
    elif bad in {"missing_ts", "bad_json"}:
        p = f.live / f"orders-{MAGICS[0]}.jsonl"
        with p.open("a", encoding="utf-8") as out:
            out.write('{"magic":902601,"kind":"open"}\n' if bad == "missing_ts" else '{bad\n')
    elif bad == "bad_log":
        with (f.logs / "risk.log").open("a", encoding="utf-8") as out:
            out.write("Traceback: unassignable error\n")
    elif bad == "bad_state":
        (f.live / f"pause-{MAGICS[0]}.json").write_text("{", encoding="utf-8")
    else:
        p = f.live / f"drill-archive/synthetic-0/flatten.ack-{MAGICS[0]}"
        doc = get(p)
        doc["closed"] = False
        put(p, doc)
    assert_result(formal(tmp_path), 2)


def test_existing_day_and_summary_remain_lenient(tmp_path):
    f = build_evidence(tmp_path)
    for flags in (("--day", str(f.trading[-1])), ("--summary",)):
        p = invoke(tmp_path, *flags)
        assert p.returncode == 0, p.stdout + p.stderr


def test_old_reader_counterexample_has_real_mechanical_difference(tmp_path):
    """RED on the base: old SL/TP and a 10-day middle span are accepted.

    The new interface must first bound both the samples and the calendar span;
    this is deliberately more than an argparse/absent-parameter test.
    """
    f = build_evidence(tmp_path)
    mod = checker()
    raw = mod.build_ctx(SimpleNamespace(data_dir=str(tmp_path / "data"), log_dir=str(f.logs),
        reports_dir=str(tmp_path / "reports"), sessions_config=str(tmp_path / "sessions.toml")))
    raw["run_window"] = (f.start + timedelta(days=1), f.now)
    assert mod.accept_sl_tp_samples(raw).ok is False


def test_fixed_clock_midday_and_midnight_window_boundaries(tmp_path):
    f = build_evidence(tmp_path)
    mod = checker()
    assert hasattr(mod, "resolve_formal_window")
    # Full CLI cases cover current time; this pure boundary helper has no CLI clock override.
    batch = mod.FormalEvidence(tmp_path)
    desc = mod.validate_formal_descriptor(batch.json(tmp_path / "formal.json", "descriptor"))
    window = mod.resolve_formal_window(get(f.live / "run-manifest.json"), desc, batch, now=f.now)
    assert window.start_utc == f.start
    assert len(window.required_trading_days) == 12
    desc["anchor"]["deployment_claim"]["completed_utc"] = iso(f.start + timedelta(hours=1))
    p = tmp_path / "retained/deployment.txt"
    p.write_bytes(p.read_bytes().replace(iso(f.start).encode(), iso(f.start + timedelta(hours=1)).encode()))
    refresh(f, desc["anchor"]["deployment_claim"]["source"])
    desc["anchor"]["deployment_claim"]["excerpt_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    window = mod.resolve_formal_window(get(f.live / "run-manifest.json"), desc, mod.FormalEvidence(tmp_path), now=f.now)
    expected = f.start + timedelta(days=3 if f.start.weekday() == 4 else 1)
    assert window.start_utc == expected


def raw_formal(f):
    mod = checker()
    batch = mod.FormalEvidence(f.root)
    args = SimpleNamespace(data_dir=str(f.root / "data"), log_dir=str(f.logs),
        reports_dir=str(f.root / "reports"), sessions_config=str(f.root / "sessions.toml"))
    raw = mod.load_formal_raw(args, f.descriptor, batch, now=f.now)
    return mod, mod.build_accept_ctx(raw, raw["window"])


def test_retained_restart_time_can_support_recovery_without_inventing_log_time(tmp_path):
    f = build_evidence(tmp_path)
    append_restart(f)
    for p in f.logs.glob("runner-*.log"):
        p.write_text("\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines()
                              if not (ln.startswith("[") and "mt5live 启动" in ln)), encoding="utf-8")
    assert_result(formal(tmp_path), 3)


def test_heartbeat_recovery_must_match_timed_out_magic(tmp_path):
    f = build_evidence(tmp_path)
    p = f.logs / f"runner-{MAGICS[0]}.log"
    p.write_text("\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines()
                          if not (ln.startswith("[") and "mt5live 启动" in ln)), encoding="utf-8")
    assert_result(formal(tmp_path), 1, "心跳")


def test_log_date_and_hour_are_derived_from_own_parsed_utc_timestamp(tmp_path):
    f = build_evidence(tmp_path)
    for p in f.logs.glob("runner-*.log"):
        text = p.read_text(encoding="utf-8")
        import re
        p.write_text(re.sub(r"(\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]", r"\1Z]", text), encoding="utf-8")
    assert_result(formal(tmp_path), 3)


def test_normal_untimed_producer_metadata_is_not_event_evidence(tmp_path):
    f = build_evidence(tmp_path)
    p = f.logs / f"runner-{MAGICS[0]}.log"
    with p.open("a", encoding="utf-8") as out:
        out.write("身份锁已取得：C:/synthetic/locks/identity.lock\n")
        out.write("    执行 hold：net 0.0 → 0.0，0 笔请求\n")
        out.write("      open ticket=1 vol=0.01/0.01 price=4000.4\n")
    assert_result(formal(tmp_path), 3)


@pytest.mark.parametrize("anomaly", ["决策异常", "ACCOUNT_POLL_ERROR", "STATE_CORRUPT", "EXTERNAL_EXIT", "state_corrupt"])
def test_current_or_late_window_anomaly_cannot_hide_after_ten_days(tmp_path, anomaly):
    f = build_evidence(tmp_path)
    t = f.now - timedelta(seconds=2)
    if anomaly == "state_corrupt":
        p = f.live / f"state-{MAGICS[0]}.json"
        doc = get(p)
        doc["state_corrupt"] = True
        put(p, doc)
    elif anomaly == "决策异常":
        with (f.logs / f"runner-{MAGICS[0]}.log").open("a", encoding="utf-8") as out:
            out.write(f"[{t:%Y-%m-%dT%H:%M:%S}] 决策异常：synthetic failure\n")
    else:
        with (f.live / "alerts.log").open("a", encoding="utf-8") as out:
            out.write(f"{iso(t)}\t{anomaly}\tmagic {MAGICS[0]} synthetic failure\n")
    assert_result(formal(tmp_path), 1)


@pytest.mark.parametrize("change", ["wrong_gateway", "future_start", "bool_schema", "missing_binding", "mode_return", "path_return"])
def test_identity_chain_cannot_silently_inherit_or_change_identity(tmp_path, change):
    f = build_evidence(tmp_path)
    if change in {"mode_return", "path_return"}:
        append_run(f)
        append_run(f)
        p = f.root / "retained/instances-1.json"
        doc = get(p)
        for e in doc["instances"]:
            e["mode" if change == "mode_return" else "data_dir"] = "PaperSoak" if change == "mode_return" else "C:/synthetic/other-data"
        put(p, doc)
        refresh(f, f.descriptor["run_bindings"][0]["instances_snapshot"])
    elif change == "missing_binding":
        append_run(f)
        f.descriptor["run_bindings"] = []
    else:
        p = f.root / "retained/instances-0.json"
        doc = get(p)
        if change == "bool_schema":
            doc["schema_version"] = True
        elif change == "wrong_gateway":
            doc["instances"][0]["gateway_config"] = "C:/synthetic/other/gateway.json"
        else:
            doc["instances"][0]["started_utc"] = iso(f.now + timedelta(days=1))
        put(p, doc)
        refresh(f, f.descriptor["anchor"]["instances_snapshot"])
    save_descriptor(f)
    assert_result(formal(tmp_path), 2)


def test_raw_bytes_are_read_once_even_when_paths_alias(tmp_path, monkeypatch):
    f = build_evidence(tmp_path)
    mod = checker()
    calls = {}
    original = Path.read_bytes
    def counted(path):
        key = path.resolve()
        calls[key] = calls.get(key, 0) + 1
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", counted)
    args = SimpleNamespace(data_dir=str(tmp_path / "data"), log_dir=str(f.logs),
        reports_dir=str(tmp_path / "reports"), sessions_config=str(tmp_path / "sessions.toml"))
    batch = mod.FormalEvidence(tmp_path)
    descriptor = mod.validate_formal_descriptor(batch.json(tmp_path / "formal.json", "descriptor"))
    raw = mod.load_formal_raw(args, descriptor, batch, now=f.now)
    ctx = mod.build_accept_ctx(raw, raw["window"])
    assert all(c.ok for c in mod.run_formal_accept(ctx))
    assert calls and max(calls.values()) == 1
    batch.bound(descriptor["calendar"], "calendar")
    changed = dict(descriptor["calendar"], path="./calendar.json", sha256="0" * 64)
    with pytest.raises(mod.FormalInvalid, match="conflicting_hash"):
        batch.bound(changed, "calendar")


@pytest.mark.parametrize("change", ["future_pause", "negative_ack", "bad_halt_time", "bad_quote", "unknown_alert"])
def test_future_or_malformed_support_cannot_create_success(tmp_path, change):
    f = build_evidence(tmp_path)
    code = 2
    if change == "future_pause":
        p = f.live / f"pause-{MAGICS[0]}.json"
        doc = get(p)
        doc["ts"] = iso(f.now + timedelta(days=1))
        put(p, doc)
        code = 1
    elif change == "negative_ack":
        p = f.live / f"drill-archive/synthetic-0/flatten.ack-{MAGICS[0]}"
        doc = get(p)
        doc["closed"] = -1
        put(p, doc)
    elif change == "bad_halt_time":
        put(f.live / "halt.json", dict(reason="synthetic", ts="not a time"))
    elif change == "bad_quote":
        p = f.logs / f"runner-{MAGICS[0]}.log"
        p.write_text(p.read_text(encoding="utf-8").replace("bid=4000.1", "bid=1e999"), encoding="utf-8")
    else:
        with (f.live / "alerts.log").open("a", encoding="utf-8") as out:
            out.write(f"{iso(f.now)}\tUNKNOWN_FAILURE\tunattributable\n")
    assert_result(formal(tmp_path), code)


def test_pending_intent_retains_sixty_second_grace(tmp_path):
    f = build_evidence(tmp_path)
    p = f.live / f"expected-{MAGICS[0]}.json"
    doc = get(p)
    doc.update(status="pending", volume=.01, ts=iso(f.now - timedelta(seconds=10)))
    put(p, doc)
    mod, ctx = raw_formal(f)
    assert mod.formal_current_state(ctx).ok is True
    doc["ts"] = iso(f.now - timedelta(seconds=61))
    put(p, doc)
    mod, ctx = raw_formal(f)
    assert mod.formal_current_state(ctx).ok is False


def test_first_ten_minutes_today_have_no_premature_hour_or_report_requirement(tmp_path):
    f = build_evidence(tmp_path)
    f.now = f.now.replace(hour=0, minute=5, second=0, microsecond=0)
    for p in f.logs.glob("*.log"):
        p.write_text("\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines()
                              if str(f.now.date()) not in ln), encoding="utf-8")
    mod, ctx = raw_formal(f)
    assert mod.formal_process_liveness(ctx).ok is True
    f.now += timedelta(minutes=5)
    mod, ctx = raw_formal(f)
    assert mod.formal_process_liveness(ctx).ok is False


def test_default_reads_are_guarded_before_bad_descriptors(tmp_path, monkeypatch):
    mod = checker()
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy/default data loader was reached")
    monkeypatch.setattr(mod, "build_ctx", forbidden)
    assert mod.main(["--accept"]) == 2
    (tmp_path / "formal.json").write_text("{}", encoding="utf-8")
    assert mod.main(["--accept", "--formal-window", str(tmp_path / "formal.json"),
        "--data-dir", str(tmp_path / "missing"), "--log-dir", str(tmp_path / "missing"),
        "--reports-dir", str(tmp_path / "missing"), "--sessions-config", str(tmp_path / "missing")]) == 2


@pytest.mark.parametrize("name", ["pause.json", "reconcile-block-902601.json"])
def test_current_pause_producer_variants_cannot_be_ignored(tmp_path, name):
    f = build_evidence(tmp_path)
    put(f.live / name, dict(magic=None if name == "pause.json" else MAGICS[0],
        paused=True, reason="manual" if name == "pause.json" else "reconcile_mismatch",
        owner="reconcile", ts=iso(f.start - timedelta(days=2)), diffs=[]))
    assert_result(formal(tmp_path), 1, "当前")


@pytest.mark.parametrize("flags", [["--accept", "--help"], ["--accept", "--now", "2026-11-05"],
                                    ["--accept=true"]])
def test_formal_argument_paths_never_exit_zero_or_echo_raw_inputs(tmp_path, flags):
    p = subprocess.run([sys.executable, str(SCRIPT), *flags], cwd=tmp_path,
                       capture_output=True, text=True, encoding="utf-8")
    assert_result(p, 2)


def test_abbreviated_accept_cannot_enter_default_loader(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--acc"], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 2
    assert "会话配置" not in result.stderr
    assert "先启动" not in result.stdout


def test_untimed_paper_header_is_invalid_identity_not_positive_metadata(tmp_path):
    f = build_evidence(tmp_path)
    p = f.logs / f"runner-{MAGICS[0]}.log"
    p.write_text(p.read_text(encoding="utf-8").replace("[LIVE]", "[paper]"), encoding="utf-8")
    assert_result(formal(tmp_path), 2, "paper")


# fix1: independent review's five counterexamples and their direct controls.
@pytest.mark.parametrize("prefix", ["pause", "state", "expected", "reconcile-block"])
@pytest.mark.parametrize("spelling", ["fullwidth", "leading_zero"])
def test_fix1_noncanonical_alias_cannot_override_canonical_state(tmp_path, prefix, spelling):
    f = build_evidence(tmp_path)
    magic = MAGICS[1]
    canonical = f.live / f"{prefix}-{magic}.json"
    if prefix == "reconcile-block":
        doc = dict(magic=magic, paused=True, reason="reconcile_mismatch", owner="reconcile",
                   ts=iso(f.start - timedelta(days=1)), diffs=[])
    else:
        doc = get(canonical)
        if prefix == "pause":
            doc.update(paused=True, reason="manual", ts=iso(f.start - timedelta(days=1)))
        elif prefix == "state":
            doc["pending_external_exits"] = {"42": {"ts": iso(f.start - timedelta(days=1))}}
        else:
            doc["volume"] = .01
    put(canonical, doc)
    assert_result(formal(tmp_path), 1)
    original = canonical.read_bytes()
    alias_magic = "９０２６０２" if spelling == "fullwidth" else "0902602"
    alias_doc = copy.deepcopy(doc)
    if prefix == "pause":
        alias_doc.update(paused=False, reason=None, ts=iso(f.start + timedelta(hours=6)))
    elif prefix == "state":
        alias_doc["pending_external_exits"] = {}
    elif prefix == "expected":
        alias_doc["volume"] = 0.0
    put(f.live / f"{prefix}-{alias_magic}.json", alias_doc)
    result = formal(tmp_path)
    assert canonical.read_bytes() == original, "canonical protection was changed"
    assert_result(result, 2)


def test_fix1_canonical_state_without_conflicting_alias_is_accepted(tmp_path):
    build_evidence(tmp_path)
    assert_result(formal(tmp_path), 3, "mechanical_checks_passed=true")


@pytest.mark.parametrize("scope", ["strategy", "global"])
@pytest.mark.parametrize("prior_alert", [False, True])
def test_fix1_future_pause_false_cannot_hide_behind_other_magic_drill(tmp_path, scope, prior_alert):
    f = build_evidence(tmp_path)
    magic = MAGICS[1]
    if prior_alert:
        with (f.live / "alerts.log").open("a", encoding="utf-8") as out:
            out.write(f"{iso(f.start - timedelta(days=1))}\tRECONCILE_MISMATCH\t已暂停 magic [{magic}]\n")
    path = f.live / (f"pause-{magic}.json" if scope == "strategy" else "pause.json")
    doc = dict(magic=magic if scope == "strategy" else None, paused=False, reason=None,
               ts=iso(f.now + timedelta(days=1)), diffs=[])
    put(path, doc)
    # Match existing state/intent future-time classification: known anomaly, exit1.
    out = assert_result(formal(tmp_path), 1, "当前")
    assert "future" in out


@pytest.mark.parametrize("scope", ["strategy", "global"])
@pytest.mark.parametrize("state", ["past_false", "past_true", "missing"])
def test_fix1_current_pause_valid_time_controls(tmp_path, scope, state):
    f = build_evidence(tmp_path)
    magic = MAGICS[1]
    path = f.live / (f"pause-{magic}.json" if scope == "strategy" else "pause.json")
    if state == "missing":
        path.unlink(missing_ok=True)
    else:
        put(path, dict(magic=magic if scope == "strategy" else None,
                       paused=state == "past_true", reason="manual" if state == "past_true" else None,
                       ts=iso(f.start - timedelta(days=1)), diffs=[]))
    assert_result(formal(tmp_path), 1 if state == "past_true" else 3)


@pytest.mark.parametrize("release", ["earlier", "equal", "future", "other_magic", "missing", "later"])
def test_fix1_latest_mismatch_requires_own_valid_later_release(tmp_path, release):
    f = build_evidence(tmp_path)
    magic = MAGICS[1]
    latest = f.now - timedelta(seconds=2)
    with (f.live / "alerts.log").open("a", encoding="utf-8") as out:
        # First B alert can have been resolved; the later one is after ten days.
        out.write(f"{iso(f.start + timedelta(hours=4))}\tRECONCILE_MISMATCH\t已暂停 magic [{magic}]\n")
        out.write(f"{iso(latest)}\tRECONCILE_MISMATCH\t已暂停 magic [{magic}]\n")
    path = f.live / f"pause-{magic}.json"
    if release == "missing":
        path.unlink()
    elif release != "earlier":
        doc = get(path)
        doc["ts"] = iso(latest if release == "equal" else
                        f.now + timedelta(days=1) if release == "future" else
                        latest + timedelta(seconds=1))
        if release == "other_magic":
            path = f.live / f"pause-{MAGICS[0]}.json"
            doc["magic"] = MAGICS[0]
        put(path, doc)
    out = assert_result(formal(tmp_path), 3 if release == "later" else 1)
    if release != "later":
        assert "RECONCILE_MISMATCH" in out and str(magic) in out


def test_fix1_multi_magic_mismatch_needs_release_for_each_magic(tmp_path):
    f = build_evidence(tmp_path)
    latest = f.now - timedelta(seconds=2)
    with (f.live / "alerts.log").open("a", encoding="utf-8") as out:
        out.write(f"{iso(latest)}\tRECONCILE_MISMATCH\t已暂停 magic {MAGICS}\n")
    path = f.live / f"pause-{MAGICS[0]}.json"
    doc = get(path)
    doc["ts"] = iso(latest + timedelta(seconds=1))
    put(path, doc)
    assert_result(formal(tmp_path), 1, str(MAGICS[1]))
    doc["magic"] = MAGICS[1]
    put(f.live / f"pause-{MAGICS[1]}.json", doc)
    assert_result(formal(tmp_path), 3)


@pytest.mark.parametrize("remaining", ["none", "untimed_header", "outside_window", "retained_restart"])
def test_fix1_risk_heartbeats_cannot_replace_all_runner_events(tmp_path, remaining):
    f = build_evidence(tmp_path)
    magic = MAGICS[1]
    path = f.logs / f"runner-{magic}.log"
    if remaining in ("none", "retained_restart"):
        path.unlink()
    else:
        text = f"mt5live 启动：XAUUSD M15 magic={magic} strategy=synthetic [LIVE]\n"
        if remaining == "outside_window":
            text += f"[{f.start - timedelta(seconds=1):%Y-%m-%dT%H:%M:%S}] loop_completed magic={magic}\n"
        path.write_text(text, encoding="utf-8")
    if remaining == "retained_restart":
        append_restart(f, component=str(magic))
    assert_result(formal(tmp_path), 1, str(magic))


def test_fix1_one_own_runner_event_preserves_daily_heartbeat_fallback(tmp_path):
    f = build_evidence(tmp_path)
    magic = MAGICS[1]
    (f.logs / f"runner-{magic}.log").write_text(
        f"mt5live 启动：XAUUSD M15 magic={magic} strategy=synthetic [LIVE]\n"
        f"[{f.start + timedelta(hours=10):%Y-%m-%dT%H:%M:%S}] loop_completed magic={magic}\n", encoding="utf-8")
    assert_result(formal(tmp_path), 3)


@pytest.mark.parametrize("flags", [
    ["--accept", "--he"], ["--accept", "--h"], ["--help", "--accept"],
    ["--h", "--acc"], ["--acc", "--hel"], ["--formal-w", "unused.json", "--he"],
    ["--he", "--formal-w", "unused.json"], ["-h", "--accept"],
])
def test_fix1_all_formal_help_exits_are_fail_closed(tmp_path, flags):
    before = snapshots(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT), *flags], cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert snapshots(tmp_path) == before
    out = assert_result(result, 2, "mechanical_checks_passed=false")
    assert "会话配置" not in result.stderr and "先启动" not in result.stdout
    assert "usage:" not in out


@pytest.mark.parametrize("flags", [["--help"], ["--h"], ["--he"], ["--hel"], ["-h"],
                                    ["--day", "2026-11-05", "--he"], ["--summary", "--h"]])
def test_fix1_standalone_and_daily_help_remain_normal(tmp_path, flags):
    result = subprocess.run([sys.executable, str(SCRIPT), *flags], cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"}, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout
    assert "historical_identity_verified" not in result.stdout


# fix2 (T191, 第十八轮 §3.1)：记录类告警、历史范围、窗口内 stderr 与演练 magic。


def add_alerts(f, rows):
    with (f.live / "alerts.log").open("a", encoding="utf-8") as out:
        for t, kind, msg in rows:
            out.write(f"{iso(t)}\t{kind}\t{msg}\n")


def prepend_alerts(f, rows):
    """把行插到 alerts.log 最前（构造早于锚点的历史行）。"""
    p = f.live / "alerts.log"
    old = p.read_text(encoding="utf-8")
    with p.open("w", encoding="utf-8") as out:
        for t, kind, msg in rows:
            out.write(f"{iso(t)}\t{kind}\t{msg}\n")
        out.write(old)


def drill_magic():
    return 902603 if 902603 not in MAGICS else max(MAGICS) + 7


def test_fix2_recovered_recorded_pairs_are_recorded_not_failures(tmp_path):
    # 场景①：窗口内已恢复的 DISCONNECTED 对、ACCOUNT_UNAVAILABLE 对 → 3，
    # 逐日统计照常输出（55 + 40 = 95 秒 < 1800）。
    f = build_evidence(tmp_path)
    t0 = f.start + timedelta(hours=5)
    add_alerts(f, [
        (t0, "DISCONNECTED", "risk 进程与交易服务器断线（NotConnectedError: synthetic）——降级继续"),
        (t0 + timedelta(seconds=55), "DISCONNECTED",
         "risk 进程与交易服务器恢复连接（断线 55 秒）——恢复正常轮询（T141）"),
        (t0 + timedelta(hours=1), "ACCOUNT_UNAVAILABLE",
         "risk 账户取数失败（DataUnavailableError: synthetic）——按时间窗计"),
        (t0 + timedelta(hours=1, seconds=40), "ACCOUNT_UNAVAILABLE",
         "risk 账户取数恢复（此前连续失败 40 秒，未达时间窗上限）——恢复正常轮询（T184）"),
    ])
    out = assert_result(formal(tmp_path), 3)
    assert "记录类 4 次" in out and "降级 95 秒" in out


def test_fix2_disconnect_exit_alert_is_treated_as_anomaly(tmp_path):
    # 场景②："进程连续断线"（T141 rc=78 退出告警）→ 1。
    f = build_evidence(tmp_path)
    add_alerts(f, [(f.start + timedelta(hours=6), "DISCONNECTED",
                    "risk 进程连续断线 15 分钟（≥ 15 分钟上限，最后错误：NotConnectedError）——"
                    "以专用退出码 78 退出交给 supervisor（T141）")])
    out = assert_result(formal(tmp_path), 1)
    assert "DISCONNECTED" in out


def test_fix2_daily_degradation_1860s_fails(tmp_path):
    # 场景③：当日累计降级 1860 秒（>1800）→ 1，失败名"环境降级超限"。
    f = build_evidence(tmp_path)
    t0 = f.start + timedelta(hours=6)
    add_alerts(f, [
        (t0, "DISCONNECTED", "risk 进程与交易服务器断线（NotConnectedError: synthetic）——降级继续"),
        (t0 + timedelta(seconds=1860), "DISCONNECTED",
         "risk 进程与交易服务器恢复连接（断线 1860 秒）——恢复正常轮询（T141）"),
    ])
    out = assert_result(formal(tmp_path), 1)
    assert "环境降级超限" in out and "1860" in out


def test_fix2_degradation_is_union_across_components_per_day(tmp_path):
    # 同日各组件段取并集而非求和：[0,1200] ∪ [600,1801] = 1801 秒（>1800）。
    f = build_evidence(tmp_path)
    t0 = f.start + timedelta(hours=6)
    add_alerts(f, [
        (t0, "DISCONNECTED", "risk 进程与交易服务器断线（NotConnectedError: synthetic）——降级继续"),
        (t0 + timedelta(seconds=1200), "DISCONNECTED",
         "risk 进程与交易服务器恢复连接（断线 1200 秒）——恢复正常轮询（T141）"),
        (t0 + timedelta(seconds=600), "ACCOUNT_UNAVAILABLE",
         "reconcile 账户取数失败（DataUnavailableError: synthetic）——按时间窗计"),
        (t0 + timedelta(seconds=1801), "ACCOUNT_UNAVAILABLE",
         "reconcile 账户取数恢复（此前连续失败 1201 秒，未达时间窗上限）——恢复正常轮询（T184）"),
    ])
    out = assert_result(formal(tmp_path), 1)
    assert "环境降级超限" in out and "1801 秒" in out and "2401" not in out


def test_fix2_unknown_alert_kind_in_window_is_invalid(tmp_path):
    # 场景④：窗口内未知类型 → 2。
    f = build_evidence(tmp_path)
    add_alerts(f, [(f.now - timedelta(seconds=2), "UNCLASSIFIED_KIND", "synthetic failure")])
    assert_result(formal(tmp_path), 2, "alerts.unknown_kind")


def test_fix2_pre_anchor_alerts_are_format_checked_only(tmp_path):
    # 场景⑤：锚点前的未知类型与锚点前演练 magic 的告警 → 只校验行格式 → 3。
    f = build_evidence(tmp_path)
    old = f.start - timedelta(days=1)
    prepend_alerts(f, [
        (old, "LEGACY_KIND", "锚点前的未知类型（只校验行格式）"),
        (old + timedelta(seconds=5), "RISK_REJECT",
         f"magic {drill_magic()} legacy forbidden_session（锚点前演练 magic）"),
    ])
    assert_result(formal(tmp_path), 3)


def test_fix2_pre_anchor_stderr_file_is_not_read(tmp_path):
    # 场景⑥：锚点前修改过的 .log.err 含无时间戳 CRITICAL → 不读 → 3。
    f = build_evidence(tmp_path)
    err = f.logs / "risk-account-20201110-033459.log.err"
    err.write_text("CRITICAL:root:MT5 deadline call=initialize duration=66.422s "
                   "limit=30.000s rc=75\n", encoding="utf-8")
    old = (f.start - timedelta(days=1)).timestamp()
    os.utime(err, (old, old))
    assert_result(formal(tmp_path), 3)


def test_fix2_pre_anchor_log_file_with_untimed_lines_is_not_read(tmp_path):
    f = build_evidence(tmp_path)
    old_log = f.logs / "runner-902601-20260101-000000.log"
    old_log.write_text("mt5live 启动：XAUUSD M15 magic=902601 [LIVE]\n垃圾行（无时间戳）\n",
                       encoding="utf-8")
    old = (f.start - timedelta(days=1)).timestamp()
    os.utime(old_log, (old, old))
    assert_result(formal(tmp_path), 3)


def test_fix2_nonempty_stderr_file_in_window_fails_with_exit_one(tmp_path):
    # 场景⑦：窗口内非空 .log.err → "窗口内异常：stderr 输出 <文件名>"，返回 1。
    f = build_evidence(tmp_path)
    name = "risk-account-20261116-030000.log.err"
    (f.logs / name).write_text(
        "CRITICAL:root:MT5 deadline call=terminal_info duration=31.002s rc=75\n",
        encoding="utf-8")   # mtime=当前时刻，落在窗口内
    out = assert_result(formal(tmp_path), 1)
    assert "窗口内异常" in out and f"stderr 输出 {name}" in out


def test_fix2_supervisor_evidence_in_window_is_invalid(tmp_path):
    # 场景⑧：窗口内 SUPERVISOR_EVIDENCE → 2（evidence.retention_failed）。
    f = build_evidence(tmp_path)
    add_alerts(f, [(f.now - timedelta(seconds=3), "SUPERVISOR_EVIDENCE",
                    "seq=3 component=risk before copy failed: OSError")])
    assert_result(formal(tmp_path), 2, "evidence.retention_failed")


def test_fix2_declared_drill_magic_reject_satisfies_forbidden_session(tmp_path):
    # 场景⑨：声明演练 magic 后其 forbidden_session 拒单 → 3，且"换日禁入"
    # 演练项计满（基线 1 条 + 探针 1 条）。
    f = build_evidence(tmp_path)
    drill = drill_magic()
    f.descriptor["drill_magics"] = [drill]
    save_descriptor(f)
    add_alerts(f, [(f.start + timedelta(hours=4, minutes=30), "RISK_REJECT",
                    f"magic {drill} XAUUSD UTC 21:56 换日窗禁止开仓 forbidden_session（演练探针）")])
    out = assert_result(formal(tmp_path), 3)
    assert "换日禁入拒单×2" in out


@pytest.mark.parametrize("which", ["deals", "orders"])
def test_fix2_drill_magic_trading_evidence_is_invalid(tmp_path, which):
    # 场景⑩：演练 magic 有成交（deals-<d>.jsonl 非空 / orders-<d>.jsonl 有行）→ 2。
    f = build_evidence(tmp_path)
    drill = drill_magic()
    f.descriptor["drill_magics"] = [drill]
    save_descriptor(f)
    if which == "deals":
        lines(f.live / f"deals-{drill}.jsonl",
              [dict(ts=iso(f.start + timedelta(hours=2)), magic=drill, ticket=9,
                    event="fill_open", price=4000.0, side="buy", volume=.01,
                    reason=None, decision_bar=iso(f.start))])
    else:
        lines(f.live / f"orders-{drill}.jsonl",
              [dict(ts=iso(f.start + timedelta(hours=2)), magic=drill, kind="open",
                    symbol="XAUUSD", side="buy", lots=.01, decision_id="XAUUSD|M15|synthetic")])
    assert_result(formal(tmp_path), 2)


def test_fix2_undeclared_drill_magic_in_alert_is_invalid(tmp_path):
    # 场景⑪：未声明的演练 magic 出现在告警里 → 2（alerts.magic）。
    f = build_evidence(tmp_path)
    add_alerts(f, [(f.start + timedelta(hours=4, minutes=30), "RISK_REJECT",
                    f"magic {drill_magic()} XAUUSD forbidden_session（未声明演练 magic）")])
    assert_result(formal(tmp_path), 2, "alerts.magic")


def test_fix2_drill_magic_state_files_do_not_join_current_state(tmp_path):
    # 演练 magic 的 state/pause/expected/heartbeat 与为空的 orders/deals 允许
    # 存在且不参与当前状态判定（演练 pause 置位不得拖垮窗口）。
    f = build_evidence(tmp_path)
    drill = drill_magic()
    f.descriptor["drill_magics"] = [drill]
    save_descriptor(f)
    put(f.live / f"state-{drill}.json", dict(magic=drill, known_positions={},
        pending_external_exits={}, updated_at_utc=iso(f.now)))
    put(f.live / f"pause-{drill}.json", dict(magic=drill, paused=True, reason="drill",
        ts=iso(f.now), diffs=[]))
    put(f.live / f"expected-{drill}.json", dict(magic=drill, symbol="XAUUSD", side="buy",
        volume=0.0, status="confirmed", cause="drill", ts=iso(f.now)))
    put(f.live / f"heartbeat-{drill}.json", dict(magic=drill, ts=iso(f.now)))
    (f.live / f"orders-{drill}.jsonl").write_text("", encoding="utf-8")
    (f.live / f"deals-{drill}.jsonl").write_text("", encoding="utf-8")
    assert_result(formal(tmp_path), 3)


def test_fix2_declared_drill_magic_in_wrong_alert_kind_is_invalid(tmp_path):
    # 演练 magic 只允许出现在 RISK_REJECT/RISK_HALT 告警里。
    f = build_evidence(tmp_path)
    drill = drill_magic()
    f.descriptor["drill_magics"] = [drill]
    save_descriptor(f)
    add_alerts(f, [(f.now - timedelta(seconds=5), "EXTERNAL_EXIT",
                    f"magic {drill} synthetic external exit（非 RISK 类告警）")])
    assert_result(formal(tmp_path), 2, "alerts.magic")


def test_fix2_recoverable_failure_without_exit_is_recorded_only(tmp_path):
    # RECOVERABLE_FAILURE 只有段首、只记录：其后 10 分钟内无同组件 LOOP_EXIT
    # 时降级按 10 分钟封顶（600 秒 < 1800），不判失败。
    f = build_evidence(tmp_path)
    t0 = f.start + timedelta(hours=5)
    add_alerts(f, [(t0, "RECOVERABLE_FAILURE",
                    "risk 主循环已知可恢复异常（PermissionError: synthetic）——记录后继续（T142）")])
    out = assert_result(formal(tmp_path), 3)
    assert "记录类 1 次" in out and "降级 600 秒" in out


def test_fix2_loop_exit_after_recoverable_failure_judges_failure(tmp_path):
    # RECOVERABLE_FAILURE 之后 10 分钟内同组件 LOOP_EXIT：由 LOOP_EXIT 判失败 → 1。
    f = build_evidence(tmp_path)
    t0 = f.start + timedelta(hours=5)
    add_alerts(f, [
        (t0, "RECOVERABLE_FAILURE",
         "risk 主循环已知可恢复异常（PermissionError: synthetic）——记录后继续（T142）"),
        (t0 + timedelta(minutes=2), "LOOP_EXIT",
         "risk 主循环连续 5 轮失败（PermissionError: synthetic）——写退出标记 "
         "loop-exit-risk.json 并以专用退出码 79 退出；已写 halt（fail-closed）（T142）"),
    ])
    out = assert_result(formal(tmp_path), 1)
    assert "LOOP_EXIT" in out


# fix3 (T202, 第十九轮计划 §2.2)：未闭降级段的两种截断（O19-2/D88）。


def test_fix3_unclosed_segment_truncated_by_same_component_restart(tmp_path):
    # ①：未配对 DISCONNECTED 段首 + 3 分钟后同组件 supervisor 重启行
    # （ops-restarts.jsonl）→ 只计 180 秒；新进程不补写恢复，不得计到检查时刻。
    f = build_evidence(tmp_path)
    append_restart(f, component="risk")   # 第 1 条重启行 utc = f.start + 8 小时
    t0 = f.start + timedelta(hours=8) - timedelta(minutes=3)
    add_alerts(f, [(t0, "DISCONNECTED",
                    "risk 进程与交易服务器断线（NotConnectedError: synthetic）——降级继续")])
    out = assert_result(formal(tmp_path), 3)
    assert "降级 180 秒" in out


def test_fix3_unclosed_segment_truncated_by_new_run_start(tmp_path):
    # ②：未配对段首 + 窗口内新运行 → 在新运行 started_utc 截断；截断对所有
    # 组件生效（段属于 reconcile，换运行截的是全部组件）。
    f = build_evidence(tmp_path)
    append_run(f)                          # 新运行起点 = f.start + 1 天 + 1 小时
    t0 = f.start + timedelta(days=1, hours=1) - timedelta(minutes=20)
    add_alerts(f, [(t0, "DISCONNECTED",
                    "reconcile 进程与交易服务器断线（NotConnectedError: synthetic）——降级继续")])
    out = assert_result(formal(tmp_path), 3)
    assert "降级 1200 秒" in out
