"""D57 / T45/T49: read-only Demo checks, without MT5 or process operations.

``--day`` / ``--summary`` retain their lenient operational behavior: 0 means
no observed anomaly, 1 means an observed failure, 2 means unusable arguments/data.
They do not claim final acceptance.

``--accept --formal-window DESCRIPTOR`` requires explicit offline data, logs,
reports and sessions paths. FormalWindowV1 binds retained manifest, per-launch
inventory, instances, deployment-source excerpt and a complete sourced UTC
calendar. It checks the preserved identity chain, uses one observation T, and
starts S at the first trading-day boundary at or after claimed completion C.
All finished trading days (at least ten) and all natural days in [S,T] are
required; same-deployment restarts do not reset S. Every positive event and its
supporting reference is filtered by its own timestamp and strategy magic.
Current protections are checked separately, including pre-window state.

Formal exit codes: 2 invalid/incomplete evidence, 1 known unmet checks,
3 mechanical checks satisfied. Never 0. Every formal result fixes
historical_identity_verified=false and a5_status=not_evaluated: byte hashes and
literal source matches do not prove historical authenticity or grant A5.
See docs/ops/demo-runbook.md section 4 for the exact evidence schema and usage.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import math
import ntpath
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5live.monitor import parse_iso, read_alerts  # noqa: E402
from mt5live.reconcile import (  # noqa: E402
    DEFAULT_VOLUME_TOLERANCE,
    ExpectedStateError,
    diff_positions,
    load_intents,
)

# mt5gw.models.DEAL_REASON_*（这里不 import mt5gw，保持脚本只读轻依赖）
REASON_SL, REASON_TP, REASON_SO = 4, 5, 6
PROTECTIVE_REASONS = frozenset({REASON_SL, REASON_TP, REASON_SO})
TRADE_ENTRIES = frozenset({"in", "out", "inout", "out_by"})

# ---- R10 契约（T53/T54）：Demo 验收的证据源 ----
RUN_MANIFEST_FILE = "run-manifest.json"        # C-1：运行清单（A 组写入）
DRILL_ARCHIVE_SUBDIR = "drill-archive"         # C-3：演练证据保全（E 组写入）
FLATTEN_COMMAND = "flatten_all"
# C-2 event → 统一成交形状（entry/comment 供 §7 日常检查复用）
DEAL_EVENT_ENTRY = {"fill_open": "in", "fill_close": "out",
                    "external_exit": "out"}
REASON_CODE_BY_NAME = {"SL": REASON_SL, "TP": REASON_TP, "SO": REASON_SO}
PAPER_LOG_MARK = "[paper"                      # 影子盘日志行标记（含 [paper offline]）
GATEWAY_RECONNECTED_MARK = "gateway_reconnected"   # C-4 重连标记（E 组写入）
KILL_SWITCH_MARK = "kill_switch"               # RISK_HALT 告警的 killSwitch 语义
ACK_LINK_WINDOW_S = 3600.0                     # T54：ack 与指令的时间关联窗（秒）

# 心跳连续性：进程日志里的标记行
RUNNER_START_MARK = "mt5live 启动"
EXIT_MARKS = ("收到中断，退出", "手动停止", "Traceback", "执行异常")
TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T[0-9:.]+)\]")
# 风控/对账进程行格式："· 2026-10-26 11:00:40 UTC [live] 心跳=ok …"
DOT_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC")
HEARTBEAT_OK_MARK = "心跳=ok"          # 风控该轮全部登记心跳新鲜
# 运行器决策行的参考报价（滑点判据：决策价 vs C-2 成交价）
DECISION_QUOTE_RE = re.compile(
    r"决策 (\S+?)：.*?\bbid=([0-9.eE+-]+) ask=([0-9.eE+-]+)")
# 日报点差表的结构化行（monitor.render_daily_report 表格式）
SPREAD_ROW_RE = re.compile(
    r"^\|\s*([A-Za-z0-9_.-]+)\s*\|\s*(\d{1,2})\s*\|\s*(\d+)\s*\|", re.M)


@dataclass
class Check:
    """单项验收结果：ok=None 表示当日无样本/无法判定（不算不通过）。"""

    name: str
    ok: bool | None
    detail: str


@dataclass
class DayReport:
    day: date
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]


# ---------------------------------------------------------------- D57 offline formal evidence

class FormalInvalid(ValueError):
    """Only logical aliases/codes may cross the formal CLI boundary."""


def _require(condition, code):
    if not condition:
        raise FormalInvalid(code)


def _fields(doc, names, alias):
    _require(isinstance(doc, dict) and set(doc) == set(names.split()), alias + ".schema")


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _utc(value, alias, *, naive=False):
    _require(isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|\+00:00)?", value), alias + ".time")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(naive or dt.tzinfo is not None, alias + ".timezone")
        return dt.replace(tzinfo=None)
    except ValueError:
        raise FormalInvalid(alias + ".time") from None


def _json_bytes(raw, alias):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, alias + ".duplicate_key")
            result[key] = value
        return result
    def constant(_):
        raise FormalInvalid(alias + ".number")
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, ValueError):
        raise FormalInvalid(alias + ".json") from None


def _ref_schema(doc, alias):
    _fields(doc, "path sha256", alias)
    _require(isinstance(doc["path"], str) and bool(doc["path"]) and "\x00" not in doc["path"], alias + ".path")
    _require(_hex(doc["sha256"], 64), alias + ".sha256")


def _line_range(value, alias):
    _require(isinstance(value, list) and len(value) == 2
             and all(_integer(v, 1) for v in value) and value[0] <= value[1], alias + ".lines")


def _role(doc, magics, alias):
    kind, magic = doc.get("kind"), doc.get("magic")
    _require(kind in ("runner", "risk", "reconcile"), alias + ".role")
    if kind == "runner":
        _require(_integer(magic, 1) and magic in magics, alias + ".magic")
        return str(magic)
    _require(magic is None, alias + ".magic")  # both actual producers write null
    return kind


def validate_formal_descriptor(doc):
    alias = "descriptor"
    _fields(doc, "schema_version mode deployment_sha magics anchor run_bindings restart_bindings calendar", alias)
    _require(type(doc["schema_version"]) is int and doc["schema_version"] == 1, alias + ".version")
    _require(doc["mode"] == "Demo" and _hex(doc["deployment_sha"], 40), alias + ".identity")
    magics = doc["magics"]
    _require(isinstance(magics, list) and magics and all(_integer(m, 1) for m in magics)
             and len(set(magics)) == len(magics), alias + ".magics")
    _require(isinstance(doc["run_bindings"], list) and isinstance(doc["restart_bindings"], list), alias + ".bindings")
    for n, binding in enumerate([doc["anchor"], *doc["run_bindings"]]):
        _fields(binding, "run_index manifest_snapshot launcher_snapshot instances_snapshot" +
                (" deployment_claim" if n == 0 else ""), alias)
        _require(_integer(binding["run_index"]), alias + ".run_index")
        for name in ("manifest_snapshot", "launcher_snapshot", "instances_snapshot"):
            _ref_schema(binding[name], alias)
    claim = doc["anchor"]["deployment_claim"]
    _fields(claim, "source source_lines excerpt_sha256 completed_utc processes", alias)
    _ref_schema(claim["source"], alias)
    _line_range(claim["source_lines"], alias)
    _require(_hex(claim["excerpt_sha256"], 64), alias + ".excerpt_sha256")
    _utc(claim["completed_utc"], alias)
    _require(isinstance(claim["processes"], list), alias + ".processes")
    roles, pids = set(), set()
    for proc in claim["processes"]:
        _fields(proc, "kind magic pid", alias)
        role = _role(proc, magics, alias)
        _require(_integer(proc["pid"], 1) and proc["pid"] not in pids and role not in roles, alias + ".pid")
        roles.add(role)
        pids.add(proc["pid"])
    _require(roles == {str(m) for m in magics} | {"risk", "reconcile"}, alias + ".roles")
    for binding in doc["restart_bindings"]:
        _fields(binding, "line_number before_instances after_instances", alias)
        _require(_integer(binding["line_number"], 1), alias + ".line_number")
        _ref_schema(binding["before_instances"], alias)
        _ref_schema(binding["after_instances"], alias)
    _ref_schema(doc["calendar"], alias)
    return doc


class FormalEvidence:
    """One read per actual artifact; hashes and parsers consume the same bytes."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.cache = {}
        self.hashes = {}

    def read(self, path, alias):
        try:
            path = Path(path).resolve()
            st = path.stat()
            key = (st.st_dev, st.st_ino) if st.st_ino else str(path).casefold()
            if key not in self.cache:
                self.cache[key] = path.read_bytes()
            return key, self.cache[key]
        except (OSError, ValueError):
            raise FormalInvalid(alias + ".unreadable") from None

    def json(self, path, alias):
        return _json_bytes(self.read(path, alias)[1], alias)

    def text(self, path, alias):
        try:
            return self.read(path, alias)[1].decode("utf-8-sig")
        except UnicodeError:
            raise FormalInvalid(alias + ".encoding") from None

    def bound(self, reference, alias):
        _ref_schema(reference, alias)
        try:
            path = (self.root / reference["path"]).resolve()
            _require(path.is_relative_to(self.root), alias + ".outside_root")
        except (OSError, ValueError):
            raise FormalInvalid(alias + ".path") from None
        key, raw = self.read(path, alias)
        wanted = reference["sha256"]
        _require(key not in self.hashes or self.hashes[key] == wanted, alias + ".conflicting_hash")
        self.hashes[key] = wanted
        _require(hashlib.sha256(raw).hexdigest() == wanted, alias + ".hash")
        return raw

    def bound_json(self, reference, alias):
        return _json_bytes(self.bound(reference, alias), alias)

    def excerpt(self, reference, selected, alias):
        raw = self.bound(reference, alias)
        try:
            raw.decode("utf-8")  # validate the entire original, not just selected lines
        except UnicodeError:
            raise FormalInvalid(alias + ".encoding") from None
        _line_range(selected, alias)
        rows = raw.splitlines(keepends=True)
        _require(selected[1] <= len(rows), alias + ".lines")
        return b"".join(rows[selected[0] - 1:selected[1]])


def _identity_path(value):
    # Lexical only: never stat/resolve any deployment/config path in a record.
    _require(isinstance(value, str) and bool(value) and "\x00" not in value, "identity.path")
    _require(ntpath.isabs(value) or value.startswith("/"), "identity.path")
    return ntpath.normcase(ntpath.normpath(value))


def _argv_identity(entry):
    argv, kind = entry.get("argv"), entry["kind"]
    _require(isinstance(argv, list) and len(argv) >= 6
             and all(isinstance(x, str) and x for x in argv), "instances.argv")
    _require(argv[1] == "-u", "instances.argv")
    script = {"runner": "live_runner.py", "risk": "risk_process.py", "reconcile": "live_reconcile.py"}[kind]
    dep = _identity_path(entry.get("deployment_dir"))
    _require(_identity_path(argv[2]) == ntpath.join(dep, "scripts", script), "instances.script")
    _identity_path(argv[0])
    options = {}
    i = 3
    while i < len(argv):
        key = argv[i]
        _require(key.startswith("--") and key not in options, "instances.argv")
        if key == "--loop":
            options[key] = True
            i += 1
        else:
            _require(i + 1 < len(argv) and not argv[i + 1].startswith("--"), "instances.argv")
            options[key] = argv[i + 1]
            i += 2
    required = {"--data-dir", "--loop"}
    if kind == "runner":
        required |= {"--config", "--gateway-config", "--risk-config", "--sessions-config", "--spread-model"}
    else:
        required |= {"--gw-config", "--interval"}
        if kind == "risk":
            required |= {"--config", "--sessions-config"}
    _require(set(options) == required, "instances.argv")
    _require(_identity_path(options["--data-dir"]) == _identity_path(entry.get("data_dir")), "instances.data_dir")
    if "gateway_config" in entry:
        gateway = options["--gateway-config" if kind == "runner" else "--gw-config"]
        _require(_identity_path(entry["gateway_config"]) == _identity_path(gateway), "instances.gateway_config")
    normalized = []
    for key, value in sorted(options.items()):
        if key == "--interval":
            _require(value == ("10.0" if kind == "risk" else "30.0"), "instances.interval")
        elif key != "--loop":
            value = _identity_path(value)
        normalized.append((key, value))
    return (_identity_path(argv[0]), _identity_path(argv[2]), tuple(normalized))


def _instances(doc, descriptor, baseline=None):
    _require(isinstance(doc, dict) and type(doc.get("schema_version")) is int
             and doc["schema_version"] == 1 and isinstance(doc.get("instances"), list), "instances.schema")
    roles, pids = {}, set()
    for e in doc["instances"]:
        _require(isinstance(e, dict), "instances.schema")
        role = _role(e, descriptor["magics"], "instances")
        _require(role not in roles and _integer(e.get("pid"), 1) and e["pid"] not in pids, "instances.pid")
        _require(e.get("mode") == "Demo" and e.get("deployment_sha") == descriptor["deployment_sha"], "instances.deployment")
        _utc(e.get("started_utc"), "instances")
        signature = (_identity_path(e.get("deployment_dir")), _identity_path(e.get("data_dir")), _argv_identity(e))
        _identity_path(e.get("log"))
        if baseline is not None:
            b = baseline[role]
            _require(signature == (b["deployment_dir"], b["data_dir"], b["argv"]), "instances.identity_changed")
        roles[role] = dict(pid=e["pid"], started_utc=_utc(e["started_utc"], "instances"),
                           deployment_dir=signature[0], data_dir=signature[1], argv=signature[2],
                           log=_identity_path(e["log"]))
        pids.add(e["pid"])
    _require(set(roles) == {str(m) for m in descriptor["magics"]} | {"risk", "reconcile"}, "instances.roles")
    _require(len({e["deployment_dir"] for e in roles.values()}) == 1
             and len({e["data_dir"] for e in roles.values()}) == 1, "instances.paths")
    return roles


def _manifest(doc, descriptor):
    _require(isinstance(doc, dict) and type(doc.get("schema_version")) is int
             and doc["schema_version"] == 1 and doc.get("mode") == "Demo", "manifest.schema")
    _require(isinstance(doc.get("magics"), list) and all(_integer(m, 1) for m in doc["magics"])
             and sorted(doc["magics"]) == sorted(descriptor["magics"]), "manifest.magics")
    _require(isinstance(doc.get("runs"), list) and doc["runs"], "manifest.runs")
    last = None
    for run in doc["runs"]:
        _require(isinstance(run, dict) and run.get("kind") in ("start", "restart"), "manifest.run")
        ts = _utc(run.get("started_utc"), "manifest")
        _require(last is None or ts >= last, "manifest.order")
        last = ts
        _identity_path(run.get("log_dir"))
    return doc["runs"]


def _launcher(doc, run, registry, descriptor, instances_doc):
    _require(isinstance(doc, dict) and doc.get("mode") == "Demo"
             and isinstance(doc.get("processes"), list), "launcher.schema")
    t = _utc(doc.get("started_utc"), "launcher")
    _require(t >= _utc(run["started_utc"], "manifest"), "launcher.time_order")
    _require(_identity_path(doc.get("repo_root")) == registry["risk"]["deployment_dir"], "launcher.deployment_dir")
    config_dir = _identity_path(doc.get("config_dir"))
    _require(_identity_path(doc.get("paper_state_dir")) == ntpath.join(registry["risk"]["data_dir"], "live", "paper"), "launcher.data_dir")
    skipped = doc.get("skipped_alive_runners")
    _require(isinstance(skipped, list) and all(_integer(m, 1) and m in descriptor["magics"] for m in skipped)
             and len(set(skipped)) == len(skipped), "launcher.skipped")
    raw_by_pid = {e["pid"]: e for e in instances_doc["instances"]}
    launched = set()
    for p in doc["processes"]:
        _require(isinstance(p, dict) and _integer(p.get("pid"), 1), "launcher.process")
        kind = p.get("kind")
        name = p.get("name")
        role = name if kind == "runner" else kind
        _require(role in registry and role not in launched, "launcher.role")
        _require(kind == ("runner" if role.isdigit() else role), "launcher.role")
        _require(kind == "runner" or name == ("account" if kind == "risk" else "all"), "launcher.name")
        e = registry[role]
        _require(p["pid"] == e["pid"] and _identity_path(p.get("log")) == e["log"], "launcher.pid_log")
        _require(ntpath.dirname(e["log"]) == _identity_path(run["log_dir"]), "launcher.log_dir")
        _require(_identity_path(p.get("err_log")) == e["log"] + ".err", "launcher.err_log")
        _require(e["started_utc"] <= _utc(p.get("started_utc"), "launcher") <= t, "launcher.process_time")
        # The actual PS producer joins argv without quoting. Compare that exact
        # record; do not try to execute it or split it on whitespace.
        raw = raw_by_pid[p["pid"]]
        _require(p.get("command") == " ".join(raw["argv"]), "launcher.command")
        if kind == "runner":
            opts = dict(e["argv"][2])
            _require(ntpath.dirname(opts["--config"]) == config_dir, "launcher.config_dir")
        launched.add(role)
    _require({str(m) for m in skipped} == {str(m) for m in descriptor["magics"]} - launched,
             "launcher.skipped")
    return t, launched


@dataclass(frozen=True)
class FormalWindow:
    deployment_sha: str
    anchor: dict
    start_utc: datetime
    end_utc: datetime
    required_trading_days: tuple[date, ...]
    required_calendar_days: tuple[date, ...]
    registry: dict = field(repr=False)
    restart_rows: tuple[dict, ...] = field(default=(), repr=False)
    registry_starts: tuple[dict, ...] = field(default=(), repr=False)


def resolve_formal_window(manifest, descriptor, bound_records, *, now):
    """Bind retained identity first; no current identity can fill historic holes."""
    d = validate_formal_descriptor(descriptor)
    batch = bound_records
    now = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    runs = _manifest(manifest, d)
    anchor = d["anchor"]
    idx = anchor["run_index"]
    claim = anchor["deployment_claim"]
    completed = _utc(claim["completed_utc"], "deployment_claim")
    _require(completed <= now, "deployment_claim.future")
    source = batch.excerpt(claim["source"], claim["source_lines"], "deployment_claim")
    _require(hashlib.sha256(source).hexdigest() == claim["excerpt_sha256"], "deployment_claim.excerpt_hash")
    text = source.decode("utf-8")
    for literal in (d["deployment_sha"], claim["completed_utc"]):
        _require(re.search(r"(?<![\w:.+-])" + re.escape(literal) + r"(?![\w:.+-])", text), "deployment_claim.literal")
    for p in claim["processes"]:
        _require(re.search(r"(?<!\d)" + str(p["pid"]) + r"(?!\d)", text), "deployment_claim.pid_literal")
    bindings = [anchor, *d["run_bindings"]]
    _require([b["run_index"] for b in bindings] == list(range(idx, len(runs))), "manifest.bindings")
    events, baseline, registry_starts = [], None, []
    for b in bindings:
        i = b["run_index"]
        snap = batch.bound_json(b["manifest_snapshot"], "manifest_snapshot")
        prefix = _manifest(snap, d)
        _require(len(prefix) == i + 1 and prefix == runs[:i + 1], "manifest.prefix")
        run = prefix[-1]
        _require(run.get("mode", snap["mode"]) == "Demo", "manifest.mode")
        ts = _utc(run["started_utc"], "manifest")
        _require(ts <= now, "manifest.future")
        instances = batch.bound_json(b["instances_snapshot"], "instances_snapshot")
        registry = _instances(instances, d, baseline)
        launcher = batch.bound_json(b["launcher_snapshot"], "launcher_snapshot")
        launch_ts, launched = _launcher(launcher, run, registry, d, instances)
        _require(launch_ts <= now, "launcher.future")
        if baseline is None:
            baseline = registry
            _require(launched == set(registry) and launch_ts <= completed, "anchor.initial_deployment")
            claimed = {_role(p, d["magics"], "claim"): p["pid"] for p in claim["processes"]}
            _require(claimed == {r: e["pid"] for r, e in registry.items()}, "anchor.pid")
        else:
            _require(ts >= completed, "launcher.before_anchor")
            events.append((ts, "launcher", registry, launched))
            registry_starts.extend(dict(magic=int(role), ts=registry[role]["started_utc"],
                                        log="launcher-retained", retained=True)
                                   for role in launched if role.isdigit())
    # The CLI supplies the complete raw physical journal and current registry.
    journal = getattr(batch, "journal", [])
    relevant = [(n, row) for n, row in journal
                if _utc(row["utc"], "journal") >= _utc(runs[idx]["started_utc"], "manifest")]
    restarts = d["restart_bindings"]
    _require([b["line_number"] for b in restarts] == [n for n, _ in relevant], "journal.bindings")
    for b, (_, row) in zip(restarts, relevant):
        _require(row["deployment_sha"] == d["deployment_sha"], "journal.deployment")
        t = _utc(row["utc"], "journal")
        _require(completed <= t <= now, "journal.time")
        before = _instances(batch.bound_json(b["before_instances"], "restart.before"), d, baseline)
        after = _instances(batch.bound_json(b["after_instances"], "restart.after"), d, baseline)
        role = row["component"]
        _require(role in before and row["old_pid"] == before[role]["pid"]
                 and row["new_pid"] == after[role]["pid"] and row["new_pid"] != row["old_pid"], "journal.pid")
        _require(t <= after[role]["started_utc"] <= now, "journal.launch_time")
        _require(all(before[k] == after[k] for k in before if k != role), "journal.unrelated_change")
        events.append((t, "supervisor", before, after))
        if role.isdigit():
            registry_starts.append(dict(magic=int(role), ts=after[role]["started_utc"],
                                        log="supervisor-retained", retained=True))
    current = baseline
    for _, kind, left, right in sorted(events, key=lambda e: e[0]):
        if kind == "supervisor":
            _require(current == left, "journal.chain")
            current = right
        else:
            changed = {k for k in current if current[k] != left[k]}
            _require(changed == right and all(current[k]["pid"] != left[k]["pid"] for k in changed), "launcher.chain")
            current = left
    if hasattr(batch, "current_instances"):
        _require(_instances(batch.current_instances, d, baseline) == current, "instances.current_chain")
    calendar = batch.bound_json(d["calendar"], "calendar")
    _fields(calendar, "schema_version timezone market source days", "calendar")
    _require(type(calendar["schema_version"]) is int and calendar["schema_version"] == 1
             and calendar["timezone"] == "UTC" and calendar["market"] == "XAUUSD", "calendar.schema")
    _ref_schema(calendar["source"], "calendar")
    _require(isinstance(calendar["days"], list), "calendar.days")
    dates, trading = [], []
    for row in calendar["days"]:
        _fields(row, "date trading reason source_lines", "calendar")
        _require(isinstance(row["date"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]), "calendar.date")
        try:
            day = date.fromisoformat(row["date"])
        except ValueError:
            raise FormalInvalid("calendar.date") from None
        _require(type(row["trading"]) is bool and row["reason"] in ("weekday", "weekend", "holiday"), "calendar.reason")
        reason = row["reason"]
        _require((reason == "weekday" and row["trading"] and day.weekday() < 5)
                 or (reason == "weekend" and not row["trading"] and day.weekday() >= 5)
                 or (reason == "holiday" and not row["trading"]), "calendar.reason")
        excerpt = batch.excerpt(calendar["source"], row["source_lines"], "calendar").decode("utf-8")
        matching = [ln for ln in excerpt.splitlines() if re.search(r"(?<!\d)" + str(day) + r"(?!\d)", ln)]
        _require(any(re.search(r"\b" + reason + r"\b", ln) for ln in matching), "calendar.source")
        if reason == "holiday":
            _require(any(re.sub(r"[\W_]", "", ln.replace(str(day), "").replace("holiday", ""))
                         for ln in matching), "calendar.holiday_reason")
        dates.append(day)
        if row["trading"]:
            trading.append(day)
    expected = [completed.date() + timedelta(days=i) for i in range((now.date() - completed.date()).days + 1)]
    _require(dates == expected, "calendar.coverage")
    starts = [datetime.combine(day, datetime.min.time()) for day in trading
              if datetime.combine(day, datetime.min.time()) >= completed]
    _require(starts and starts[0] <= now, "calendar.window_not_started")
    start = starts[0]
    return FormalWindow(d["deployment_sha"], anchor, start, now,
        tuple(day for day in trading if start.date() <= day < now.date()),
        tuple(day for day in dates if day >= start.date()), current,
        tuple(row for _, row in relevant), tuple(registry_starts))


def _formal_jsonl(batch, path, alias):
    rows = []
    for n, line in enumerate(batch.text(path, alias).splitlines(), 1):
        if not line.strip():
            continue
        doc = _json_bytes(line.encode("utf-8"), alias)
        _require(isinstance(doc, dict), alias + ".row")
        rows.append((n, doc))
    return rows


def _formal_glob(directory, pattern, alias):
    try:
        return sorted(Path(directory).glob(pattern))
    except OSError:
        raise FormalInvalid(alias + ".unreadable") from None


def _formal_exists(path, alias):
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise FormalInvalid(alias + ".unreadable") from None


def _number(value, *, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def _alert_magics(message, magics):
    found = set()
    for group in re.findall(r"magic\s*\[([^]]+)\]", message):
        _require(re.fullmatch(r"\s*\d+(?:\s*,\s*\d+)*\s*", group), "alerts.magic")
        found.update(int(x.strip()) for x in group.split(","))
    for a, b in re.findall(r"\bmagic[= ]+(\d+)\b|heartbeat-(\d+)\.json", message):
        found.add(int(a or b))
    _require(found <= set(magics), "alerts.magic")
    return found


def _formal_log_time(line):
    match = re.match(r"\[(\d{4}-\d{2}-\d{2}T[^]]+)\]", line)
    if match:
        return _utc(match.group(1), "logs", naive=True)
    match = re.match(r"[·\s]*(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC\b", line)
    if match:
        return _utc(match.group(1) + "T" + match.group(2) + "Z", "logs")
    return None


FORMAL_ANOMALY_ALERTS = frozenset({
    "ACCOUNT_POLL_ERROR", "HEARTBEAT_WRITE_ERROR", "PARTIAL_FILL", "EXTERNAL_EXIT",
    "UNEXPECTED_RESIZE", "FLATTEN_RESIDUAL", "DEALS_LOG_ERROR", "STATE_CORRUPT",
    "EXECUTION_UNKNOWN", "BROKER_PENDING_ORDER", "BROKER_UNAVAILABLE", "EXECUTION_RECOVERED",
    "PAUSE_UNREADABLE", "ORDER_LOG_BAD_LINE", "RISK_HEALTH", "SUPERVISOR_REFUSED",
    "TRADE_MODE", "ORDER_ERROR", "LINE_CAP_STATE",
})
FORMAL_DRILL_ALERTS = frozenset({"RISK_HALT", "RISK_FLATTEN", "HEARTBEAT_TIMEOUT",
                               "RECONCILE_MISMATCH", "RISK_REJECT", "SUPERVISOR"})


def _normal_log_metadata(line):
    """Only actual, non-evidence producer lines; never guess a missing time."""
    number = r"-?\d+(?:\.\d+)?"
    return any(re.fullmatch(pattern, line) for pattern in (
        r"mt5live 启动：[^\r\n]+",
        r"身份锁已取得：[^\r\n]+",
        r"    执行 (?:hold|none|duplicate|open|close|reverse|reverse_pending|add|reduce|flatten|flatten_acked|blocked_reentry|skipped_zero_lots)：net "
        + number + r" → " + number + r"，\d+ 笔请求",
        r"      (?:open|close) ticket=\d+ vol=" + number + "/" + number + " price=" + number,
    ))


def load_formal_raw(args, descriptor, batch, *, now):
    """Strict raw reader. No calls into the forgiving day/summary loaders."""
    from mt5live.monitor import Alert
    from mt5live.reconcile import Intent
    from mt5data.sessions import _session_policy_from_dict
    import tomllib

    live = Path(args.data_dir) / "live"
    for directory in (live, Path(args.log_dir), Path(args.reports_dir)):
        _require(_formal_exists(directory, "inputs") and directory.is_dir(), "inputs.directory")
    manifest = batch.json(live / RUN_MANIFEST_FILE, "manifest")
    batch.current_instances = batch.json(live / "instances.json", "instances.current")
    batch.journal = []
    journal_path = live / "ops-restarts.jsonl"
    if _formal_exists(journal_path, "journal"):
        batch.journal = _formal_jsonl(batch, journal_path, "journal")
        last = None
        for _, row in batch.journal:
            _require(set(row) == {"utc", "component", "reason_code", "old_pid", "new_pid", "deployment_sha"}, "journal.schema")
            t = _utc(row["utc"], "journal")
            _require(last is None or last <= t, "journal.order")
            _require(isinstance(row["component"], str) and bool(row["component"])
                     and isinstance(row["reason_code"], str) and bool(row["reason_code"])
                     and (row["old_pid"] is None or _integer(row["old_pid"], 1))
                     and _integer(row["new_pid"], 1) and _hex(row["deployment_sha"], 40), "journal.schema")
            last = t
    window = resolve_formal_window(manifest, descriptor, batch, now=now)
    magics = set(descriptor["magics"])
    try:
        policy = _session_policy_from_dict(tomllib.loads(batch.text(args.sessions_config, "sessions")), "sessions")
    except Exception:
        raise FormalInvalid("sessions.invalid") from None
    states, pauses, intents = {}, {}, []
    for prefix in ("state", "pause", "expected"):
        seen_magics = set()
        for path in _formal_glob(live, prefix + "-*.json", prefix):
            m = re.fullmatch(prefix + r"-([0-9]+)\.json", path.name)
            _require(m and int(m[1]) in magics, prefix + ".magic")
            magic = int(m[1])
            _require(path.name == f"{prefix}-{magic}.json", prefix + ".filename")
            _require(magic not in seen_magics, prefix + ".duplicate_magic")
            seen_magics.add(magic)
            doc = batch.json(path, prefix)
            _require(isinstance(doc, dict) and type(doc.get("magic")) is int
                     and doc["magic"] == magic, prefix + ".schema")
            if prefix == "state":
                known, pending = doc.get("known_positions"), doc.get("pending_external_exits")
                _require(isinstance(known, dict) and isinstance(pending, dict), "state.schema")
                _utc(doc.get("updated_at_utc"), "state")
                _require(type(doc.get("state_corrupt", False)) is bool, "state.state_corrupt")
                for ticket, item in known.items():
                    _require(ticket.isdigit() and isinstance(item, dict)
                             and type(item.get("dir")) is int and item["dir"] in (-1, 1)
                             and _number(item.get("volume")), "state.position")
                for ticket, item in pending.items():
                    _require(ticket.isdigit() and isinstance(item, dict), "state.pending")
                    _utc(item.get("ts"), "state.pending")
                states[magic] = doc
            elif prefix == "pause":
                _require(type(doc.get("paused")) is bool, "pause.schema")
                _utc(doc.get("ts"), "pause")
                pauses[magic] = doc
            else:
                _require(isinstance(doc.get("symbol"), str) and bool(doc["symbol"])
                         and _number(doc.get("volume")), "expected.schema")
                try:
                    intents.append(Intent(symbol=doc["symbol"], magic=magic, side=doc["side"],
                        volume=doc["volume"], status=doc["status"], cause=doc["cause"],
                        ts=_utc(doc.get("ts"), "expected")))
                except (KeyError, TypeError, ValueError):
                    raise FormalInvalid("expected.schema") from None
    _require(set(states) == magics and {it.magic for it in intents} == magics, "current.missing_state")
    _require(not _formal_exists(live / "expected.json", "expected"), "expected.legacy_unbound")
    controls, seen_blocks = [], set()
    for path in [live / "pause.json", *_formal_glob(live, "reconcile-block-*.json", "current.pause")]:
        if not _formal_exists(path, "current.pause"):
            continue
        doc = batch.json(path, "current.pause")
        _require(isinstance(doc, dict) and type(doc.get("paused")) is bool, "current.pause.schema")
        pause_ts = _utc(doc.get("ts"), "current.pause")
        if path.name == "pause.json":
            _require(doc.get("magic") is None, "current.pause.magic")
        else:
            m = re.fullmatch(r"reconcile-block-([0-9]+)\.json", path.name)
            _require(m and int(m[1]) in magics and type(doc.get("magic")) is int
                     and doc["magic"] == int(m[1]) and doc["paused"] is True
                     and doc.get("owner") == "reconcile" and doc.get("reason") == "reconcile_mismatch",
                     "current.pause.schema")
            _require(path.name == f"reconcile-block-{doc['magic']}.json", "current.pause.filename")
            _require(doc["magic"] not in seen_blocks, "current.pause.duplicate_magic")
            seen_blocks.add(doc["magic"])
        control = "global_pause" if path.name == "pause.json" else "reconcile_block"
        if pause_ts > window.end_utc:
            controls.append("future " + control)
        if doc["paused"]:
            controls.append(control)
    for name in ("killSwitch", "halt.json", "halt-risk-veto.json"):
        path = live / name
        if _formal_exists(path, "current"):
            if name == "killSwitch":
                batch.read(path, "current.killSwitch")
            else:
                doc = batch.json(path, "current.halt")
                _require(isinstance(doc, dict) and isinstance(doc.get("reason"), str), "current.halt.schema")
                _utc(doc.get("ts"), "current.halt")
            controls.append(name)
    deals, orders = [], []
    for prefix in ("deals", "orders"):
        for path in _formal_glob(live, prefix + "-*.jsonl", prefix):
            m = re.fullmatch(prefix + r"-(\d+)\.jsonl", path.name)
            _require(m and int(m[1]) in magics, prefix + ".magic")
            for _, row in _formal_jsonl(batch, path, prefix):
                _require(type(row.get("magic")) is int and row["magic"] == int(m[1]), prefix + ".magic")
                row["_t"] = _utc(row.get("ts"), prefix, naive=True)
                if prefix == "orders":
                    _require(row.get("kind") in ("open", "close") and isinstance(row.get("decision_id"), str), "orders.schema")
                    orders.append(row)
                else:
                    event = row.get("event")
                    _require(event in DEAL_EVENT_ENTRY and _integer(row.get("ticket"), 1)
                             and _number(row.get("price")) and _number(row.get("volume"))
                             and row.get("side") in ("buy", "sell")
                             and (row.get("reason") is None or row["reason"] in ("SL", "TP", "SO")), "deals.schema")
                    if event == "fill_open":
                        _require(_bar_close_key(row.get("decision_bar")) is not None, "deals.decision_bar")
                    row.update(entry=DEAL_EVENT_ENTRY[event], position_id=row["ticket"],
                               comment=row.get("decision_bar") if event == "fill_open" else "close" if event == "fill_close" else "",
                               reason=REASON_CODE_BY_NAME.get(row.get("reason")))
                    deals.append(row)
    alerts = []
    for line in batch.text(live / "alerts.log", "alerts").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        _require(len(parts) == 3 and bool(re.fullmatch(r"[A-Z][A-Z_0-9]*", parts[1])) and bool(parts[2]), "alerts.row")
        _utc(parts[0], "alerts", naive=True)
        _require(parts[1] in FORMAL_ANOMALY_ALERTS | FORMAL_DRILL_ALERTS, "alerts.unknown_kind")
        if parts[1] in ("RECONCILE_MISMATCH", "EXTERNAL_EXIT", "HEARTBEAT_TIMEOUT", "RISK_REJECT"):
            _require(_alert_magics(parts[2], magics), "alerts.unattributed")
        alerts.append(Alert(_utc(parts[0], "alerts", naive=True), parts[1], parts[2]))
    logs = []
    seen = set()
    for directory in (Path(args.log_dir), live):
        for path in _formal_glob(directory, "*.log", "logs") + _formal_glob(directory, "*.log.err", "logs"):
            if path == live / "alerts.log":
                continue
            key, raw = batch.read(path, "logs")
            if key in seen:
                continue
            seen.add(key)
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                raise FormalInvalid("logs.encoding") from None
            if not text.strip():
                continue
            _require(PAPER_LOG_MARK not in text, "logs.paper_mode_conflict")
            magic_matches = {int(x) for x in re.findall(r"mt5live 启动：.*?magic=(\d+)", text)}
            _require(len(magic_matches) <= 1 and magic_matches <= magics, "logs.magic")
            magic = next(iter(magic_matches), None)
            kind = "runner" if magic is not None else ("risk" if "心跳=" in text else
                   "reconcile" if "对账一致" in text or "持仓不一致" in text else "other")
            events = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                ts = _formal_log_time(line)
                if ts is None:
                    _require(_normal_log_metadata(line), "logs.missing_event_time")
                    continue
                if "决策 " in line:
                    decision = re.search(r"决策 (\S+?)：", line)
                    _require(kind == "runner" and decision is not None
                             and _bar_close_key(decision[1]) is not None, "logs.decision")
                    quote = DECISION_QUOTE_RE.search(line)
                    if "bid=" in line or "ask=" in line:
                        _require(quote is not None and all(_number(float(v)) for v in quote.groups()[1:]), "logs.quote")
                events.append((ts, line))
            logs.append((path.name, magic, kind, events))
    rounds = {}
    paths = set(_formal_glob(live / DRILL_ARCHIVE_SUBDIR, "*/*.json", "drills")
                + _formal_glob(live / DRILL_ARCHIVE_SUBDIR, "*/flatten.ack-*", "drills")
                + _formal_glob(live / "commands", "flatten.json", "drills")
                + _formal_glob(live / "commands", "flatten.ack-*", "drills")
                + _formal_glob(live / "commands/archive", "flatten-*.json", "drills"))
    for path in sorted(paths):
        doc = batch.json(path, "drills")
        _require(isinstance(doc, dict) and isinstance(doc.get("id"), str) and doc["id"], "drills.id")
        t = _utc(doc.get("ts"), "drills", naive=True)
        cid = doc["id"]
        r = rounds.setdefault(cid, dict(command=None, command_ts=None, acks=[]))
        if "command" in doc:
            _require(doc["command"] == FLATTEN_COMMAND, "drills.command")
            _require(r["command"] is None or r["command"] == doc, "drills.conflicting_command")
            r.update(command=doc, command_ts=t)
        else:
            _require(_integer(doc.get("magic"), 1) and doc["magic"] in magics
                     and _integer(doc.get("closed")), "drills.ack")
            r["acks"].append(dict(magic=doc["magic"], id=cid, closed=doc["closed"], ts=t))
    reports = {}
    for path in _formal_glob(Path(args.reports_dir) / "live", "daily-*.md", "reports"):
        m = re.fullmatch(r"daily-(\d{4}-\d{2}-\d{2})\.md", path.name)
        _require(m is not None, "reports.name")
        try:
            day = date.fromisoformat(m[1])
        except ValueError:
            raise FormalInvalid("reports.date") from None
        reports[day] = batch.text(path, "reports")
    return dict(manifest=manifest, window=window, deals=deals, orders=orders, alerts=alerts,
                log_events=logs, pauses=pauses, executor_states=states, intents=intents,
                current_controls=controls, reports=reports, drill_rounds=rounds, session_policy=policy,
                paper_state_files=_formal_glob(live / "paper", "state-*.json", "paper"))


def build_accept_ctx(raw_ctx, formal_window):
    """Derive every positive event view only after its own time was validated."""
    raw, w = raw_ctx, formal_window
    inside = lambda t: w.start_utc <= t <= w.end_utc
    logs, starts = [], []
    for name, magic, kind, events in raw["log_events"]:
        selected = [(ts, ln) for ts, ln in events if inside(ts)]
        if not selected:
            continue
        times, lines = zip(*selected)
        logs.append(LogFile(name, list(lines), magic, kind, min(times), max(times), w.end_utc))
        for ts, ln in selected:
            if kind == "runner" and RUNNER_START_MARK in ln:
                starts.append(dict(magic=magic, ts=ts, log=name))
    starts.extend(s for s in w.registry_starts if inside(s["ts"]))
    evidence = {"runner_days": {}, "risk_days": set(), "reconcile_days": set(),
                "heartbeat_ok_days": set(), "risk_hb_hours": {}}
    for _, magic, kind, events in raw["log_events"]:
        for ts, ln in events:
            if not inside(ts):
                continue
            day = ts.date()
            if kind == "runner":
                evidence["runner_days"].setdefault(magic, set()).add(day)
            elif kind == "risk":
                evidence["risk_days"].add(day)
                if "心跳=" in ln:
                    evidence["risk_hb_hours"].setdefault(day, set()).add(ts.hour)
                if HEARTBEAT_OK_MARK in ln:
                    evidence["heartbeat_ok_days"].add(day)
            elif kind == "reconcile":
                evidence["reconcile_days"].add(day)
    deals = [r for r in raw["deals"] if inside(r["_t"])]
    orders = [r for r in raw["orders"] if inside(r["_t"])]
    alerts = [a for a in raw["alerts"] if inside(a.time)]
    report_days = {day for day in raw["reports"] if w.start_utc.date() <= day <= w.end_utc.date()}
    activity = report_days | {d["_t"].date() for d in deals} | {a.time.date() for a in alerts}
    activity |= {d for ds in evidence["runner_days"].values() for d in ds}
    rounds = {}
    for cid, r in raw["drill_rounds"].items():
        rounds[cid] = dict(command=r["command"] if r["command_ts"] and inside(r["command_ts"]) else None,
                           command_ts=r["command_ts"], acks=[a for a in r["acks"] if inside(a["ts"])])
    return {**raw, "formal_window": w, "now": w.end_utc, "run_window": (w.start_utc, w.end_utc),
            "deals": deals, "deals_log": deals, "orders": orders, "alerts": alerts, "logs": logs,
            "start_events": sorted(starts, key=lambda s: s["ts"]), "drill_rounds": rounds,
            "states": {}, "intent_symbols": {i.magic: i.symbol for i in raw["intents"]},
            "manifest_error": None, "manifest_magics": raw["manifest"]["magics"],
            "runner_magics": set(raw["manifest"]["magics"]), "evidence_magics": set(raw["manifest"]["magics"]),
            "all_days": activity, "activity_days": activity, "latest_day": None,
            "runner_magic_days": evidence["runner_days"], **{k: v for k, v in evidence.items() if k != "runner_days"},
            "paper_log_count": sum(PAPER_LOG_MARK in ln for lf in logs for ln in lf.lines),
            "deals_log_bad": 0, "ops_restarts": list(w.restart_rows),
            "log_tail_text": {(lf.magic, lf.path): "\n".join(lf.lines) for lf in logs}}


def formal_current_state(ctx):
    problems = list(ctx["current_controls"])
    for magic, pause in ctx["pauses"].items():
        if pause["paused"]:
            problems.append(f"pause magic={magic}")
        if _utc(pause["ts"], "pause") > ctx["now"]:
            problems.append(f"future pause magic={magic}")
    # A completed drill is an existence check, not proof that every later
    # mismatch was resolved. Check each magic's latest in-window alert here.
    latest_mismatch = {}
    for alert in ctx["alerts"]:
        if alert.kind == "RECONCILE_MISMATCH":
            for magic in _alert_magics(alert.message, ctx["manifest_magics"]):
                latest_mismatch[magic] = max(alert.time, latest_mismatch.get(magic, alert.time))
    for magic, alert_ts in sorted(latest_mismatch.items()):
        pause = ctx["pauses"].get(magic)
        if (pause is None or pause["paused"]
                or not alert_ts < _utc(pause["ts"], "pause") <= ctx["now"]):
            problems.append(f"RECONCILE_MISMATCH magic={magic} 未有同策略后续有效解除")
    for magic, state in ctx["executor_states"].items():
        if state.get("state_corrupt"):
            problems.append(f"state_corrupt magic={magic}")
        if state["pending_external_exits"]:
            problems.append(f"pending magic={magic}")
        if _utc(state["updated_at_utc"], "state") > ctx["now"]:
            problems.append(f"future state magic={magic}")
    for it in ctx["intents"]:
        if it.ts > ctx["now"]:
            problems.append(f"future intent magic={it.magic}")
    result = diff_positions(ctx["intents"], actual_positions({}, ctx["executor_states"], ctx["intent_symbols"]),
                            now=ctx["now"], grace_s=60.0, volume_tolerance=DEFAULT_VOLUME_TOLERANCE)
    if result.diffs:
        problems.append("known_positions 与当前意图不一致")
    return Check("当前持续保护与持仓差分", not problems, "；".join(problems) if problems else "当前保护未置位且持仓一致")


def formal_day_coverage(ctx):
    w = ctx["formal_window"]
    missing = [d for d in w.required_trading_days if d not in ctx["activity_days"]]
    problems = []
    if len(w.required_trading_days) < 10:
        problems.append(f"已结束交易日 {len(w.required_trading_days)}（<10）")
    if missing:
        problems.append("缺失交易日 " + ",".join(map(str, missing)))
    return Check("连续交易日覆盖（完整日历）", not problems,
                 "；".join(problems) if problems else f"全部 {len(w.required_trading_days)} 已结束交易日有证据")


def formal_process_liveness(ctx):
    w = ctx["formal_window"]
    problems = [f"magic={magic} 全窗口无有效自身运行器日志事件"
                for magic in ctx["manifest_magics"] if not ctx["runner_magic_days"].get(magic)]
    for day in w.required_calendar_days:
        # Same hourly grace as the daily checker; at 00:00..00:09 no hour
        # has fallen due yet, so do not invent a requirement for hour zero.
        hi = 23 if day < w.end_utc.date() else w.end_utc.hour - (w.end_utc.minute < 10)
        hours = set(range(hi + 1))
        if not hours:
            continue
        for label, days in (("风控", ctx["risk_days"]), ("对账", ctx["reconcile_days"])):
            if day not in days:
                problems.append(f"{day} 无{label}进程日志")
        if hours - ctx["risk_hb_hours"].get(day, set()):
            problems.append(f"{day} 风控逐小时心跳缺失")
        for magic in ctx["manifest_magics"]:
            if day not in ctx["runner_magic_days"].get(magic, set()) and day not in ctx["heartbeat_ok_days"]:
                problems.append(f"{day} magic={magic} 无运行器存活证据")
    return Check("各进程存活（完整自然日及已到期小时）", not problems,
                 "；".join(problems) if problems else "完整自然日存活证据齐全")


def formal_report(ctx, day):
    text = ctx["reports"].get(day)
    ok = bool(text and text.strip())
    detail = f"{day} 日报缺失/为空"
    if ok:
        ok = "**超标**" not in text and "当日无点差样本" not in text and any(
            sym == "XAUUSD" and 0 <= int(hour) <= 23 and int(n) > 0
            for sym, hour, n in SPREAD_ROW_RE.findall(text))
        detail = f"{day} " + ("结构化点差样本正常" if ok else "点差超标或无结构化样本")
    return Check("日报和点差样本", ok, detail)


def run_formal_accept(ctx):
    checks = [accept_evidence_source(ctx), formal_current_state(ctx), formal_day_coverage(ctx),
              formal_process_liveness(ctx), accept_sl_tp_samples(ctx), accept_drills(ctx),
              accept_double_flatten(ctx), accept_slippage(ctx)]
    w = ctx["formal_window"]
    for day in w.required_calendar_days:
        for fn in (check_deal_attribution, check_zero_rebalance, check_protective_exit,
                   check_heartbeat_continuity, check_forbidden_session):
            c = fn(ctx, day)
            if c.ok is not True:
                checks.append(Check(c.name, False, f"{day}: {c.detail}"))
        if day in w.required_trading_days:
            checks.append(formal_report(ctx, day))
        elif day == w.end_utc.date() and day in ctx["reports"] and "**超标**" in ctx["reports"][day]:
            checks.append(Check("当日点差", False, f"{day} 点差超标"))
    anomalies = [(a.time, a.kind) for a in ctx["alerts"] if a.kind in FORMAL_ANOMALY_ALERTS]
    anomalies += [(ts, "日志异常") for _, _, _, events in ctx["log_events"]
                  for ts, ln in events if _in_window(ctx, ts)
                  and any(mark in ln for mark in ("Traceback", "执行异常", "决策异常", "CRITICAL", "ERROR"))]
    if anomalies:
        checks.append(Check("窗口内异常", False, "；".join(f"{t} {kind}" for t, kind in anomalies)))
    return checks


def _formal_result(code, *, checks=(), window=None, error=None):
    print("historical_identity_verified=false")
    print("a5_status=not_evaluated")
    print("mechanical_checks_passed=" + ("true" if code == 3 else "false"))
    if error:
        print("invalid_evidence=" + error)
    if window:
        print(f"正式窗口机械预检 [S,T]=[{window.start_utc.isoformat()}Z,{window.end_utc.isoformat()}Z]")
        print("deployment_sha=" + window.deployment_sha)
    for c in checks:
        print(f"[{'满足' if c.ok is True else '不满足'}] {c.name}：{c.detail}")
    print("仅机械核对；历史真实性及独立 A5 尚未审定。")
    return code


# ---------------------------------------------------------------- 数据装载

def _read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_paper_states(live_dir: str) -> dict[int, dict]:
    """{magic: state 文档}（paper/state-<magic>.json；缺失文件跳过）。"""
    out: dict[int, dict] = {}
    for path in sorted(glob.glob(os.path.join(live_dir, "paper", "state-*.json"))):
        try:
            doc = _read_json(path)
        except (OSError, ValueError) as e:
            print(f"× 影子状态不可读（该策略按无数据处理，人工核对）：{path}：{e}",
                  file=sys.stderr)
            continue
        out[int(doc.get("magic", 0))] = doc
    return out


def all_deals(states: dict[int, dict]) -> list[dict]:
    """全部成交（含 time 解析后的 naive UTC）。"""
    out = []
    for magic, doc in states.items():
        for d in doc.get("deals", []):
            rec = dict(d)
            rec["magic"] = int(d.get("magic", magic))
            try:
                rec["_t"] = parse_iso(str(d["time"]))
            except (KeyError, ValueError, TypeError):
                continue
            out.append(rec)
    return out


def _parse_known_position(v) -> dict | None:
    """known_positions 单项（executor._parse_known 同款宽容解析）：
    {"dir": ±1, "volume": 手数}；纯方向整数的旧格式 volume=0。坏值 → None。"""
    try:
        if isinstance(v, dict):
            return {"dir": int(v.get("dir", 0)),
                    "volume": float(v.get("volume") or 0.0)}
        return {"dir": int(v), "volume": 0.0}
    except (TypeError, ValueError):
        return None


def actual_positions(states: dict[int, dict],
                     executor_states: dict[int, dict] | None = None,
                     intent_symbols: dict[int, str] | None = None) -> list:
    """意图 vs 实际差分用的 actual 持仓（最小鸭子类型：symbol/magic/side/volume）。

    两源合成（T53 持仓判据源的选择）：
    - paper 影子账本 positions（PaperSoak 日常诊断）；
    - **state-<magic>.json 的 known_positions**（executor 每轮 ``_finish``
      已持久化——Demo 运行唯一的现场持仓记录；不连 MT5）。仅补 paper 未
      覆盖的 magic（两源并存即配置错误，--accept 由证据源判据另行拒绝）。
    known_positions 无 symbol 字段——取同 magic 意图文件的 symbol。"""

    class _P:
        __slots__ = ("symbol", "magic", "side", "volume")

        def __init__(self, d):
            self.symbol = d["symbol"]
            self.magic = int(d.get("magic", 0))
            self.side = d["side"]
            self.volume = float(d["volume"])

    out = [_P(p) for doc in states.values() for p in doc.get("positions", [])]
    symbols = intent_symbols or {}
    for magic, st in (executor_states or {}).items():
        if magic in states:
            continue
        known = st.get("known_positions")
        if not isinstance(known, dict):
            continue
        for v in known.values():
            kv = _parse_known_position(v)
            if kv is None or kv["volume"] <= 0:
                continue
            out.append(_P({"symbol": symbols.get(magic, ""),
                           "magic": magic,
                           "side": "buy" if kv["dir"] > 0 else "sell",
                           "volume": kv["volume"]}))
    return out


def load_pause_files(live_dir: str) -> dict[int, dict]:
    """{magic: pause 文档}（对账进程写的 pause-<magic>.json）。"""
    out: dict[int, dict] = {}
    for path in sorted(glob.glob(os.path.join(live_dir, "pause-*.json"))):
        try:
            doc = _read_json(path)
        except (OSError, ValueError):
            continue
        m = re.search(r"pause-(\d+)\.json$", os.path.basename(path))
        if m:
            out[int(m.group(1))] = doc
    return out


def load_executor_states(live_dir: str) -> dict[int, dict]:
    """{magic: 执行器 state 文档}（live/state-<magic>.json，pending_external_exits）。"""
    out: dict[int, dict] = {}
    for path in sorted(glob.glob(os.path.join(live_dir, "state-*.json"))):
        m = re.search(r"state-(\d+)\.json$", os.path.basename(path))
        if not m:
            continue
        try:
            out[int(m.group(1))] = _read_json(path)
        except (OSError, ValueError):
            continue
    return out


def load_order_log(live_dir: str) -> list[dict]:
    """orders-<magic>.jsonl 全部行（坏行跳过并计数由调用方汇总）。"""
    out = []
    for path in sorted(glob.glob(os.path.join(live_dir, "orders-*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                        rec["_t"] = parse_iso(str(rec["ts"]))
                    except (ValueError, KeyError, TypeError):
                        continue
                    out.append(rec)
        except OSError:
            continue
    return out


# ------------------------------------------------ R10 契约装载（T53/T54）

def _parse_any_ts(value) -> datetime | None:
    """宽容解析 ISO 时间（接受尾缀 Z；坏值返回 None 而非抛错）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _bar_close_key(value) -> datetime | None:
    """滑点/归因关联键（T59/P2，C-2 契约）：decision_id（``SYMBOL|TF|bar``）
    的后缀或裸 bar ISO → 归一 naive UTC bar 收盘时刻；坏值 None。

    生产端两侧形态不同：runner 的 decision_id 后缀是 naive isoformat
    （无时区，runner.py:267-268）、executor 的 deals decision_bar 是
    iso_utc（带 +00:00，executor.py:471）——**字符串相等比较会以同样
    方式断链**，关联一律经 ``_parse_any_ts`` 归一为 naive UTC datetime
    再作 dict 键比较。"""
    if not isinstance(value, str) or not value:
        return None
    return _parse_any_ts(value.rsplit("|", 1)[-1])


def load_run_manifest(live_dir: str) -> tuple[dict | None, str | None]:
    """读 C-1 run-manifest.json。返回 (文档, 错误说明)——文件缺失 →
    (None, None)；存在但读不出/不是对象 → (None, 原因)。"""
    path = os.path.join(live_dir, RUN_MANIFEST_FILE)
    if not os.path.exists(path):
        return None, None
    try:
        doc = _read_json(path)
    except (OSError, ValueError) as e:
        return None, f"读不出（{e}）"
    if not isinstance(doc, dict):
        return None, f"顶层须为对象，收到 {type(doc).__name__}"
    return doc, None


def manifest_run_start(doc: dict | None) -> datetime | None:
    """运行窗起点 = runs 中**当前 mode** 的最早 started_utc（C-1：追加式
    runs、重启不清窗；run 条目自带 mode 字段才按其过滤，否则全体属于
    manifest 声明的当前 mode）。无 runs/无合法时间 → None。"""
    if not isinstance(doc, dict):
        return None
    mode = doc.get("mode")
    starts = []
    for r in doc.get("runs") or []:
        if not isinstance(r, dict):
            continue
        if r.get("mode", mode) != mode:   # 条目声明了别的 mode（跨模式清单）
            continue
        ts = _parse_any_ts(r.get("started_utc"))
        if ts is not None:
            starts.append(ts)
    return min(starts) if starts else None


def load_deals_log(live_dir: str) -> tuple[list[dict], int]:
    """读 C-2 deals-<magic>.jsonl 全部行 → （统一成交列表, 坏行数）。

    统一成交形状与 paper 影子账本兼容（§7 日常检查复用同一形状）：
    - ``event``/``decision_bar``/``price``/``_t`` 原样保留；
    - ``entry``：fill_open→"in"、fill_close/external_exit→"out"；
    - ``comment``：fill_open→decision_bar（开仓归因键=决策 bar 收盘时刻，
      消费侧经 ``_bar_close_key`` 归一 naive UTC 比较——T59/P2，两侧
      ISO 形态不同不得字符串相等）、
      fill_close→"close"（主动平仓，须 orders 台账 close 记录支撑）、
      external_exit→""（保护性出场已由 reason 解释）；
    - ``reason``：字符串名称按 mt5gw 语义数值化（SL→4/TP→5/SO→6，null→None）；
    - ``position_id``=ticket（C-2 不含持仓号，加仓腿检测以票号为键——
      重复开仓检测由幂等演练判据承担）。

    external_exit 的 ``decision_bar`` 语义=检测当轮 bar 或 null（按此解析、
    不作归因键——保护性出场与决策无关）。"""
    out: list[dict] = []
    bad = 0
    for path in sorted(glob.glob(os.path.join(live_dir, "deals-*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                        ts = _parse_any_ts(rec.get("ts"))
                        if ts is None:
                            raise ValueError("ts")
                        event = str(rec.get("event", ""))
                        if event not in DEAL_EVENT_ENTRY:
                            raise ValueError(f"event={event!r}")
                        reason = rec.get("reason")
                        doc = {
                            "ticket": rec.get("ticket"),
                            "position_id": rec.get("ticket"),
                            "magic": int(rec.get("magic", 0)),
                            "event": event,
                            "entry": DEAL_EVENT_ENTRY[event],
                            "comment": (str(rec["decision_bar"])
                                        if event == "fill_open"
                                        and rec.get("decision_bar")
                                        else "close" if event == "fill_close"
                                        else ""),
                            "reason": REASON_CODE_BY_NAME.get(reason)
                            if isinstance(reason, str) else None,
                            "price": rec.get("price"),
                            "volume": rec.get("volume"),
                            "side": rec.get("side"),
                            "decision_bar": rec.get("decision_bar"),
                            "_t": ts,
                        }
                        out.append(doc)
                    except (ValueError, KeyError, TypeError):
                        bad += 1
        except OSError:
            continue
    return out, bad


def load_drill_rounds(live_dir: str) -> dict[str, dict]:
    """读 C-3 drill-archive/<command_id>/（主源）+ commands 目录（兜底）。

    返回 {command_id: {"command": 指令文档|None, "command_ts": dt|None,
    "acks": [{"magic","id","closed","ts"}]}}。**文件名不作关联依据**（C-3
    只钉死目录名=编号）——命令/ack 按内容字段分类：
    - 命令：``command=="flatten_all"`` 且有 id（drill-archive 目录内任意
      *.json、commands/flatten.json 当前指令、commands/archive/flatten-*
      人工归档指令）；
    - ack：含 magic/id/closed/ts 四字段（drill-archive 目录内任意 *.json
      与 ``flatten.ack-*``——**T59/P1：生产端归档副本保留原名
      flatten.ack-<magic>，无 .json 扩展名（commands.py:48/191-194），
      两种模式都匹配**；commands/flatten.ack-<magic> 未删 ack）。
    三点关联（编号+magic+时间窗）由 accept_double_flatten 判定，这里只收集。
    """
    rounds: dict[str, dict] = {}

    def _round(cid: str) -> dict:
        return rounds.setdefault(
            cid, {"command": None, "command_ts": None, "acks": []})

    def _feed(doc) -> None:
        try:
            if doc.get("command") == FLATTEN_COMMAND and doc.get("id"):
                r = _round(str(doc["id"]))
                r["command"] = doc
                r["command_ts"] = (r["command_ts"]
                                   or _parse_any_ts(doc.get("ts")))
            elif all(k in doc for k in ("magic", "id", "closed")):
                ts = _parse_any_ts(doc.get("ts"))
                if ts is not None:
                    _round(str(doc["id"]))["acks"].append({
                        "magic": int(doc["magic"]), "id": str(doc["id"]),
                        "closed": int(doc["closed"]), "ts": ts})
        except (TypeError, ValueError):
            return

    root = os.path.join(live_dir, DRILL_ARCHIVE_SUBDIR)
    if os.path.isdir(root):
        for cid in sorted(os.listdir(root)):
            d = os.path.join(root, cid)
            if not os.path.isdir(d):
                continue
            # T59/P1：生产端（commands._copy_drill_evidence）把 ack 按原名
            # flatten.ack-<magic> 复制进来（无扩展名）——只 glob *.json 会
            # 漏掉全部 ack（消费端-生产端断链）
            for pattern in ("*.json", "flatten.ack-*"):
                for path in sorted(glob.glob(os.path.join(d, pattern))):
                    try:
                        _feed(_read_json(path))
                    except (OSError, ValueError):
                        continue
    cmds = os.path.join(live_dir, "commands")
    for path in [os.path.join(cmds, "flatten.json")]:
        if os.path.exists(path):
            try:
                _feed(_read_json(path))
            except (OSError, ValueError):
                pass
    for pattern in ("flatten.ack-*", os.path.join("archive", "flatten-*.json")):
        for path in sorted(glob.glob(os.path.join(cmds, pattern))):
            try:
                _feed(_read_json(path))
            except (OSError, ValueError):
                continue
    return rounds


@dataclass
class LogFile:
    path: str
    lines: list[str]
    magic: int | None       # 运行器日志：启动行里的 magic；其余 None
    kind: str               # runner / risk / reconcile / other
    first_ts: datetime | None
    last_ts: datetime | None
    mtime: datetime


def scan_logs(log_dirs: list[str]) -> list[LogFile]:
    """读全部 *.log（demo_run.ps1 的 <kind>-<name>-<stamp>.log 与既有命名都认）。"""
    out: list[LogFile] = []
    seen: set[str] = set()
    for d in log_dirs:
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.log"))):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            if not lines:
                continue
            text = "\n".join(lines)
            magic = None
            m = re.search(r"mt5live 启动：.*?magic=(\d+)", text)
            if m:
                magic = int(m.group(1))
                kind = "runner"
            elif "心跳=" in text:
                kind = "risk"
            elif ("对账一致" in text) or ("持仓不一致" in text):
                kind = "reconcile"
            else:
                kind = "other"
            tss = [parse_iso(m) for m in TS_RE.findall(text)]
            # mtime 归一化为 naive UTC（与 parse_iso 同口径，可比较）
            mtime = datetime.fromtimestamp(
                os.path.getmtime(path), timezone.utc).replace(tzinfo=None)
            out.append(LogFile(
                path=path, lines=lines, magic=magic, kind=kind,
                first_ts=min(tss) if tss else None,
                last_ts=max(tss) if tss else None,
                mtime=mtime))
    return out


def log_start_events(logs: list[LogFile]) -> list[dict]:
    """运行器启动事件（每个"mt5live 启动"行一个；ts 取其后第一条带时间戳行，
    无则用文件 mtime——最近刚启动的进程日志可能还没打出时间戳行）。"""
    events = []
    for lf in logs:
        if lf.kind != "runner":
            continue
        for i, ln in enumerate(lf.lines):
            if RUNNER_START_MARK not in ln:
                continue
            ts = None
            for later in lf.lines[i + 1:]:
                m = TS_RE.search(later)
                if m:
                    ts = parse_iso(m.group(1))
                    break
            events.append({"magic": lf.magic, "ts": ts or lf.mtime,
                           "log": os.path.basename(lf.path)})
    return sorted(events, key=lambda e: e["ts"])


def _dot_line_ts(ln: str) -> datetime | None:
    """风控/对账进程行（"· YYYY-MM-DD HH:MM:SS UTC …"）→ naive UTC；无时
    间戳行返回 None。"""
    m = DOT_TS_RE.search(ln)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}",
                                 "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def scan_log_days(logs: list[LogFile]) -> dict:
    """逐进程"哪些天有日志行"证据（--accept 进程存活判据用）。

    返回 {runner_days: {magic: set(date)}, risk_days, reconcile_days,
    heartbeat_ok_days, risk_hb_hours}——运行器行带 [ISO] 括号时间戳，风控/
    对账行是"· YYYY-MM-DD HH:MM:SS UTC"格式，两种都认；风控行含"心跳=ok"
    另计入 heartbeat_ok_days（该轮全部登记策略心跳新鲜的证据）；T53 加强：
    风控行含"心跳="的（日期, 小时）计入 risk_hb_hours——逐小时存活证据。"""
    runner_days: dict[int | None, set] = {}
    risk_days: set = set()
    reconcile_days: set = set()
    heartbeat_ok_days: set = set()
    risk_hb_hours: dict[date, set] = {}

    def _days_in_line(ln: str) -> set:
        out = set()
        for m in TS_RE.findall(ln):
            try:
                out.add(parse_iso(m).date())
            except ValueError:
                continue
        for m in DOT_TS_RE.findall(ln):
            try:
                out.add(date.fromisoformat(m[0]))
            except ValueError:
                continue
        return out

    for lf in logs:
        if lf.kind == "runner":
            for ln in lf.lines:
                runner_days.setdefault(lf.magic, set()).update(_days_in_line(ln))
        elif lf.kind == "risk":
            for ln in lf.lines:
                ds = _days_in_line(ln)
                risk_days.update(ds)
                if HEARTBEAT_OK_MARK in ln:
                    heartbeat_ok_days.update(ds)
                if "心跳=" in ln:
                    ts = _dot_line_ts(ln)
                    if ts is not None:
                        risk_hb_hours.setdefault(ts.date(), set()).add(ts.hour)
        elif lf.kind == "reconcile":
            for ln in lf.lines:
                reconcile_days.update(_days_in_line(ln))
    return {"runner_days": runner_days, "risk_days": risk_days,
            "reconcile_days": reconcile_days,
            "heartbeat_ok_days": heartbeat_ok_days,
            "risk_hb_hours": risk_hb_hours}


def _span_days(days: list[date]) -> list[date]:
    """覆盖跨度内的全部日历日（min→max，含周末——风控/对账 24/7 不间断）。"""
    if not days:
        return []
    out, cur = [], days[0]
    while cur <= days[-1]:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _missing_weekdays(days: list[date]) -> list[date]:
    """连续覆盖日之间缺失的工作日（周六日不算缺；假日历不可得，按周一至
    五近似——跨假日段须人工在验收报告注明例外）。"""
    missing: list[date] = []
    for a, b in zip(days, days[1:]):
        cur = a + timedelta(days=1)
        while cur < b:
            if cur.isoweekday() <= 5:
                missing.append(cur)
            cur += timedelta(days=1)
    return missing


# ---------------------------------------------------------------- §7 检查项

def check_unexplained_diffs(ctx, day: date) -> Check:
    """§7-2：无法解释的持仓差异 = 0（意图 vs 实际 + 未解除告警）。"""
    name = "无法解释持仓差异=0（意图 vs 实际）"
    unresolved: list[str] = []
    if day == ctx["latest_day"]:
        # 最新日：现场差分（pending 宽限 60 秒与对账进程同口径）
        try:
            intents = load_intents(ctx["live_dir"])
            res = diff_positions(
                intents, actual_positions(ctx["states"], ctx["executor_states"],
                                          ctx["intent_symbols"]),
                now=ctx["now"],
                grace_s=60.0, volume_tolerance=DEFAULT_VOLUME_TOLERANCE)
            for d in res.diffs:
                unresolved.append(f"当前差分 {d.detail}")
        except ExpectedStateError as e:
            return Check(name, False, f"意图文件不可用：{e}")
    # 对账暂停写了却至今未解除（pause 仍 paused；ts ≤ 当日 → 与该日相关）
    for mg, doc in sorted(ctx["pauses"].items()):
        if not doc.get("paused") or doc.get("reason") != "reconcile_mismatch":
            continue
        try:
            ts = parse_iso(str(doc.get("ts", "")))
        except (ValueError, TypeError):
            ts = None
        if ts is None or ts.date() <= day:
            unresolved.append(f"magic {mg} 对账不一致暂停未解除")
    # 未解释外部出场至今未决（pending 条目 ts ≤ 当日；条目时间不可判按未决）
    for mg, st in sorted(ctx["executor_states"].items()):
        for ticket, item in (st.get("pending_external_exits") or {}).items():
            try:
                its = parse_iso(str(item.get("ts", "")))
            except (ValueError, TypeError):
                its = None
            if its is None or its.date() <= day:
                unresolved.append(
                    f"magic {mg} 未解释外部出场（ticket {ticket}）仍未决")
                break
    n_day = sum(1 for a in ctx["alerts"]
                if a.time.date() == day and a.kind in
                ("RECONCILE_MISMATCH", "EXTERNAL_EXIT"))
    if not unresolved:
        extra = f"（当日相关告警 {n_day} 条，均已解除）" if n_day else ""
        return Check(name, True, "意图与实际一致，无未解除的对账暂停/未决出场" + extra)
    return Check(name, False, "；".join(unresolved))


def check_deal_attribution(ctx, day: date) -> Check:
    """§7-2 后半：每笔成交可在台账或日志中找到对应决策。

    开仓成交的归因键 = **解析后的 bar 收盘时刻**（T59/P2）：comment/
    decision_id（含 paper 旧夹具的全 id comment）取 ``|`` 后缀经
    ``_bar_close_key`` 归一为 naive UTC datetime 再比较——decision_id
    后缀（naive）与 C-2 decision_bar（iso_utc 带 +00:00）字符串形态
    不同，禁止字符串相等比较（会断链）。"""
    name = "每笔成交可归因（台账/日志有对应决策）"
    formal = "formal_window" in ctx
    decision_ids = {(r.get("magic") if formal else None, r["decision_id"]) for r in ctx["orders"]
                    if r.get("kind") == "open" and r.get("decision_id", "-") != "-"}
    for lf in ctx["logs"]:
        for ln in lf.lines:
            m = re.search(r"决策 (\S+?)：", ln)
            if m:
                decision_ids.add((lf.magic if formal else None, m.group(1)))
    decision_keys = {(mg, k) for mg, did in decision_ids
                     if (k := _bar_close_key(did)) is not None}
    closes_available: dict[int, int] = {}
    for r in ctx["orders"]:
        if r.get("kind") == "close":
            closes_available[r.get("magic", 0)] = (
                closes_available.get(r.get("magic", 0), 0) + 1)
    bad_in, closes_needed = [], {}
    for d in ctx["deals"]:
        if d["_t"].date() != day or d.get("entry") not in TRADE_ENTRIES:
            continue
        if d["entry"] == "in":
            key = _bar_close_key(d.get("comment", ""))
            if key is None or (d["magic"] if formal else None, key) not in decision_keys:
                bad_in.append(f"#{d.get('ticket')} comment={d.get('comment')!r}")
        else:
            if d.get("reason") in PROTECTIVE_REASONS:
                continue                      # SL/TP/SO：服务器端出场，已解释
            if str(d.get("comment", "")) == "close":
                closes_needed[d["magic"]] = closes_needed.get(d["magic"], 0) + 1
            else:
                bad_in.append(f"#{d.get('ticket')} 出场无解释（comment="
                              f"{d.get('comment')!r} reason={d.get('reason')}）")
    short_close = {m: n for m, n in closes_needed.items()
                   if closes_available.get(m, 0) < n}
    problems = []
    if bad_in:
        problems.append(f"{len(bad_in)} 笔无对应决策：" + "；".join(bad_in[:5]))
    if short_close:
        problems.append(f"平仓成交多于台账 close 记录：{short_close}")
    if problems:
        return Check(name, False, "；".join(problems))
    n_day = sum(1 for d in ctx["deals"] if d["_t"].date() == day)
    return Check(name, True, f"当日 {n_day} 笔成交全部可归因")


def check_zero_rebalance(ctx, day: date) -> Check:
    """§7-3：持仓期间零调仓（市价单数 = 入场数 + 出场数）。"""
    name = "持仓期间零调仓（市价单数=入场+出场）"
    resize = [a for a in ctx["alerts"]
              if a.time.date() == day and a.kind == "UNEXPECTED_RESIZE"]
    bad_kind, double_in = [], []
    seen_in: set[tuple[int, int]] = set()
    for d in ctx["deals"]:
        if d["_t"].date() != day:
            continue
        if d.get("entry") not in ("in", "out"):
            bad_kind.append(f"#{d.get('ticket')} entry={d.get('entry')!r}")
            continue
        if d["entry"] == "in":
            key = (d["magic"], int(d.get("position_id", 0)))
            if key in seen_in:
                double_in.append(f"position {key[1]} 第二笔开仓（加仓腿）")
            seen_in.add(key)
    problems = []
    if resize:
        problems.append(f"UNEXPECTED_RESIZE 告警 {len(resize)} 条")
    if bad_kind:
        problems.append("非 in/out 成交：" + "；".join(bad_kind[:5]))
    if double_in:
        problems.append("；".join(sorted(set(double_in))[:5]))
    if problems:
        return Check(name, False, "；".join(problems))
    n_in = sum(1 for k in seen_in)
    n_out = sum(1 for d in ctx["deals"]
                if d["_t"].date() == day and d.get("entry") == "out")
    return Check(name, True, f"开仓 {n_in} + 出场 {n_out}，无加/减仓腿、无调仓告警")


def check_protective_exit(ctx, day: date) -> Check:
    """§7-4：服务器端 SL/TP 出场后不触发暂停（同策略按重入规则继续）。"""
    name = "SL/TP 出场后无未解除暂停"
    magics = sorted({d["magic"] for d in ctx["deals"]
                     if d["_t"].date() == day and d.get("entry") != "in"
                     and d.get("reason") in PROTECTIVE_REASONS})
    if not magics:
        return Check(name, True, "当日无保护性出场（无样本）")
    problems = []
    for mg in magics:
        pause = ctx["pauses"].get(mg)
        if pause is not None and pause.get("paused"):
            problems.append(f"magic {mg} 仍处对账暂停")
        pend = ctx["executor_states"].get(mg, {}).get("pending_external_exits")
        if pend:
            problems.append(f"magic {mg} 未决外部出场 {len(pend)} 项")
    if problems:
        return Check(name, False, "；".join(problems))
    return Check(name, True,
                 f"保护性出场 {len(magics)} 个 magic（{magics}），暂停均已自动解除")


def load_ops_restarts(live_dir: str) -> list[dict]:
    """Restart explanations only, never synthetic liveness/coverage evidence."""
    path = os.path.join(live_dir, "ops-restarts.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for row in rows:
            parse_iso(row["utc"])
            if (not row.get("reason_code") or not isinstance(row.get("component"), str)
                    or not re.fullmatch(r"[0-9a-f]{40}", row.get("deployment_sha", ""))
                    or type(row.get("new_pid")) is not int or row["new_pid"] <= 0):
                raise ValueError("invalid ops restart")
        return rows
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError, KeyError):
        return []  # unreadable explanations cannot excuse a restart


def check_heartbeat_continuity(ctx, day: date) -> Check:
    """§7-1：进程不间断、重启有日志说明（心跳超时后有恢复记录）。"""
    name = "心跳连续性（中断有日志说明）"
    timeouts = [a for a in ctx["alerts"]
                if a.time.date() == day and a.kind == "HEARTBEAT_TIMEOUT"]
    starts = ctx["start_events"]
    problems = []
    # 1) 心跳超时之后须有运行器重启（新启动事件 ts > 告警 ts）
    for a in timeouts:
        timed_out = (_alert_magics(a.message, ctx["manifest_magics"])
                     if "formal_window" in ctx else {None})
        if not all(any(s["ts"] > a.time and (m is None or s["magic"] == m)
                       for s in starts) for m in timed_out):
            problems.append(f"{a.time:%H:%M} 心跳超时后无同策略重启恢复记录")
    # 2) 每次重启须在上一段日志留有说明（退出标记或当期停机类告警）；
    #    只判"重启发生在当日"的事件（历史日的重启归历史日）
    by_magic: dict[int | None, list[dict]] = {}
    for s in starts:
        by_magic.setdefault(s["magic"], []).append(s)
    for mg, evs in by_magic.items():
        for prev, cur in zip(evs, evs[1:]):
            if cur["ts"].date() != day:
                continue
            if cur.get("retained"):
                continue  # checked launcher/journal identity explains this restart, not liveness
            tail = ctx["log_tail_text"].get((mg, prev["log"]), "")
            explained = any(mark in tail for mark in EXIT_MARKS)
            if not explained:
                explained = any(
                    r.get("component") == str(mg) and r.get("reason_code")
                    and prev["ts"] <= parse_iso(r["utc"]) <= cur["ts"]
                    and (cur["ts"] - parse_iso(r["utc"])).total_seconds() <= 120
                    for r in ctx.get("ops_restarts", []))
            if not explained:
                covered = [a for a in ctx["alerts"]
                           if a.kind in ("HEARTBEAT_TIMEOUT", "RISK_HALT")
                           and prev["ts"] <= a.time <= cur["ts"]]
                if not covered:
                    problems.append(
                        f"magic {mg} 重启（{prev['log']} → {cur['log']}）"
                        "无退出说明（taskkill 强杀不留日志——停止请用 killSwitch "
                        "或 Ctrl-C，并在日志留一行说明）")
    if problems:
        return Check(name, False, "；".join(problems))
    if timeouts:
        return Check(name, True,
                     f"心跳超时 {len(timeouts)} 次，均有重启恢复；重启均有说明")
    return Check(name, True, "无心跳超时告警；无未说明的重启")


def check_forbidden_session(ctx, day: date) -> Check:
    """§7-5 演练项：换日禁止时段（含冬令时 22:00）内不得开仓。"""
    name = "换日禁入时段（含冬令时 22:00）"
    if ctx["session_policy"] is None:
        return Check(name, None, "会话配置不可用，无法判定禁入窗")
    violations = []
    for d in ctx["deals"]:
        if d["_t"].date() != day or d.get("entry") != "in":
            continue
        if not ctx["session_policy"].entry_allowed(d.get("symbol", ""), d["_t"]):
            violations.append(f"#{d.get('ticket')} {d.get('symbol')} "
                              f"{d['_t']:%H:%M} 开仓落在禁入窗")
    rejects = [a for a in ctx["alerts"]
               if a.time.date() == day and a.kind == "RISK_REJECT"
               and "forbidden_session" in a.message]
    if violations:
        return Check(name, False, "禁入窗内开仓 " + "；".join(violations[:5]))
    note = f"禁入窗内零开仓；拒单记录 {len(rejects)} 条"
    if not rejects:
        note += "（当日无禁入窗开仓尝试——§7 演练项要求覆盖换日窗，见 --summary）"
    return Check(name, True, note)


def check_daily_report(ctx, day: date) -> Check:
    """§7-6/§7-7：每日日报存在；点差按小时比对无超标（由日报承担）。"""
    name = "日报存在且点差无超标"
    path = os.path.join(ctx["reports_dir"], "live", f"daily-{day.isoformat()}.md")
    if not os.path.exists(path):
        return Check(name, False, f"日报缺失：{path}")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return Check(name, False, f"日报不可读：{e}")
    if not text.strip():
        return Check(name, False, f"日报为空：{path}")
    n_breach = text.count("**超标**")
    if n_breach:
        return Check(name, False, f"日报含 {n_breach} 行点差超标（按小时比对）")
    if "当日无点差样本" in text:
        return Check(name, True, "日报存在；当日无点差样本（无法比对，如实标注）")
    return Check(name, True, "日报存在，点差比对无超标行")


CHECKS = (
    check_unexplained_diffs,
    check_deal_attribution,
    check_zero_rebalance,
    check_protective_exit,
    check_heartbeat_continuity,
    check_forbidden_session,
    check_daily_report,
)


# ---------------------------------------------------------------- --accept 判据
# 阶段 F 最终验收（R8-I5 + R10/T53/T54）：任何"无样本/未覆盖"= 不通过
# （未验收）。证据源钉死 Demo 运行契约（C-1/C-2/C-3）。

def _in_window(ctx, ts: datetime) -> bool:
    """ts 是否落在 C-1 运行窗（当前 mode 最早 started_utc..now）内。"""
    start, end = ctx["run_window"]
    return start is not None and start <= ts <= end


def accept_evidence_source(ctx) -> Check:
    """判据 0（T53/C-1）：证据来源与运行窗——run-manifest.json 存在且
    mode=="Demo"、magics 与证据一致；存在 paper 状态文件/[paper] 日志行 =
    "证据来源可疑"拒绝（PaperSoak 证据不被 --accept 采信）。"""
    name = "证据来源与运行窗（C-1 run-manifest，mode=Demo）"
    doc, err = ctx["manifest"], ctx["manifest_error"]
    if doc is None:
        reason = f"：{err}" if err else "（文件不存在）"
        return Check(name, False,
                     "证据来源可疑——未读到可用的 run-manifest.json" + reason
                     + "；纯 paper/无运行清单证据不被 --accept 采信（R9-I3）")
    problems = []
    if str(doc.get("mode")) != "Demo":
        problems.append(f"manifest mode={doc.get('mode')!r} ≠ Demo")
    # T59/P3：schema_version 缺失同样拒绝（fail-closed，与生产端写入器对齐
    # ——demo_run.ps1 恒写该字段；缺字段=契约不符，不得静默按 1 处理）
    version = doc.get("schema_version")
    if version != 1:
        problems.append(f"schema_version={version!r} 不受支持（当前 1；"
                        "缺失同样拒绝——fail-closed）")
    magics = set(ctx["manifest_magics"])
    if not magics:
        problems.append("manifest magics 为空")
    else:
        evidence = ctx["evidence_magics"]
        if evidence != magics:
            extra = sorted(evidence - magics)
            lacking = sorted(magics - evidence)
            problems.append(
                f"magics 与证据不一致（manifest {sorted(magics)} vs 证据 "
                f"{sorted(evidence)}；多出 {extra}、缺证据 {lacking}）")
    if ctx["paper_state_files"]:
        problems.append(
            f"证据来源可疑：存在 paper 状态文件 {len(ctx['paper_state_files'])}"
            f" 个（{os.path.basename(ctx['paper_state_files'][0])} 等）")
    if ctx["paper_log_count"]:
        problems.append(
            f"证据来源可疑：日志含 [paper 影子盘行 {ctx['paper_log_count']} 行")
    if ctx["run_window"][0] is None:
        problems.append("manifest 无当前 mode 的合法 runs（运行窗不可定）")
    if problems:
        return Check(name, False, "；".join(problems))
    start = ctx["run_window"][0]
    return Check(name, True,
                 f"mode=Demo magics={sorted(magics)}，运行窗 "
                 f"{start:%Y-%m-%d %H:%M} UTC → now（runs "
                 f"{len(doc.get('runs') or [])} 条，重启不清窗）")


def accept_positions_state(ctx) -> Check:
    """判据 1（T53 持仓判据源的选择）：state-<magic>.json 的 known_positions
    （executor 每轮 ``_finish`` 已持久化）——不连 MT5、不读 paper。逐 magic
    要求：状态文件存在且含 known_positions 字段；其净手数（Σ dir×volume）
    与 confirmed 意图一致（pending 意图在途不判）。"""
    name = "持仓状态可核（state-<magic>.json known_positions）"
    magics = set(ctx["manifest_magics"]) or ctx["runner_magics"]
    problems = []
    for m in sorted(magics):
        st = ctx["executor_states"].get(m)
        if st is None:
            problems.append(f"magic {m} 无 state 文件（执行器未持久化"
                            " known_positions）")
        elif not isinstance(st.get("known_positions"), dict):
            problems.append(f"magic {m} state 无/坏 known_positions 字段")
    nets: dict[int, float] = {}
    for m, st in ctx["executor_states"].items():
        net = 0.0
        for v in (st.get("known_positions") or {}).values():
            kv = _parse_known_position(v)
            if kv is not None:
                net += kv["dir"] * kv["volume"]
        nets[m] = net
    try:
        intents = load_intents(ctx["live_dir"])
    except ExpectedStateError as e:
        problems.append(f"意图不可读（{e}）")
        intents = []
    for it in intents:
        if getattr(it, "status", None) != "confirmed":
            continue                    # pending 在途：对账进程另有宽限口径
        net = nets.get(it.magic)
        if net is None:
            problems.append(
                f"magic {it.magic} 有 confirmed 意图但无 known_positions 状态"
                "可核")
        elif abs(net - it.net_volume) > 1e-6:
            problems.append(
                f"magic {it.magic} known_positions 净 {net:+.2f} 手与 confirmed"
                f" 意图 {it.net_volume:+.2f} 手不一致")
    if problems:
        return Check(name, False, "；".join(problems[:6])
                     + ("…" if len(problems) > 6 else ""))
    known_n = sum(len(st.get("known_positions") or {})
                  for st in ctx["executor_states"].values())
    return Check(name, True,
                 f"magics {sorted(magics)} 状态齐全（known_positions 共 "
                 f"{known_n} 项），净额与 confirmed 意图一致")


def accept_day_coverage(ctx) -> Check:
    """判据 2：连续交易日覆盖 ≥10 个、全齐（工作日无缺日）。"""
    name = "连续交易日覆盖（≥10 个交易日全齐）"
    days = sorted(ctx["activity_days"])
    if not days:
        return Check(name, False,
                     "无活动日证据（成交/告警/运行器日志/日报任一日皆无）"
                     "——未覆盖")
    n_trading = sum(1 for d in days if d.isoweekday() <= 5)
    problems = []
    if n_trading < 10:
        problems.append(f"仅覆盖 {n_trading} 个交易日（<10）")
    missing = _missing_weekdays(days)
    if missing:
        shown = "、".join(d.isoformat() for d in missing[:5])
        problems.append("覆盖不连续，缺失交易日 " + shown
                        + (f" 等 {len(missing)} 天" if len(missing) > 5 else ""))
    if problems:
        return Check(name, False,
                     "；".join(problems) + f"（{days[0]} → {days[-1]}）")
    return Check(name, True,
                 f"{days[0]} → {days[-1]} 共 {n_trading} 个交易日全齐，"
                 "其间无缺失工作日（周六日不计；假日按工作日近似，跨假日段"
                 "须人工注明例外）")


def _required_hours(day: date, win_start: datetime, now: datetime) -> range:
    """逐小时存活要求的 hour 集合：窗首日从窗起点小时起；now 所在日到当前
    小时止（小时刚开头 10 分钟内还等不到本小时首条日志，宽限到上一小时）；
    其余日整日 0..23。"""
    lo, hi = 0, 23
    if day == win_start.date():
        lo = win_start.hour
    if day == now.date():
        hi = now.hour if now.minute >= 10 else max(now.hour - 1, 0)
        hi = max(hi, lo)
    return range(lo, hi + 1)


def accept_process_liveness(ctx) -> Check:
    """判据 3：各进程存活证据——覆盖跨度内逐日（含周末，进程 24/7 不间断）：
    风控/对账有当日日志行；每个 magic 的运行器有当日日志行，或当日风控
    日志有"心跳=ok"行佐证（风控每轮核对全部登记心跳）；且**风控心跳日志
    覆盖当日每小时**（T53 加强：一条日志不再能证明整日存活）。"""
    name = "各进程存活证据（逐日风控/对账/运行器+逐小时心跳）"
    days = sorted(ctx["activity_days"])
    magics = ctx["runner_magics"]
    if not days:
        return Check(name, False, "无活动日，无从判定进程存活")
    no_log = sorted(m for m in magics if not ctx["runner_magic_days"].get(m))
    if not magics or no_log:
        return Check(name, False,
                     f"magic {no_log or magics or '?'} 没有任何运行器日志"
                     "证据——无法证明运行器存活过")
    problems = []
    win_start = ctx["run_window"][0] or ctx["now"]
    for d in _span_days(days):
        if d not in ctx["risk_days"]:
            problems.append(f"{d.isoformat()} 无风控进程日志行")
        if d not in ctx["reconcile_days"]:
            problems.append(f"{d.isoformat()} 无对账进程日志行")
        missing_h = sorted(set(_required_hours(d, win_start, ctx["now"]))
                           - ctx["risk_hb_hours"].get(d, set()))
        if missing_h:
            problems.append(
                f"{d.isoformat()} 风控心跳日志缺 {len(missing_h)} 个小时"
                f"（如 {missing_h[:6]}）——逐小时存活证据不全")
        if d in ctx["heartbeat_ok_days"]:
            continue                     # 该轮全部登记心跳新鲜＝全员佐证
        for m in magics:
            if d not in ctx["runner_magic_days"].get(m, set()):
                problems.append(
                    f"{d.isoformat()} magic {m} 无运行器日志行"
                    "（且当日无风控 心跳=ok 佐证）")
    if problems:
        shown = "；".join(problems[:6])
        if len(problems) > 6:
            shown += f" 等 {len(problems)} 项"
        return Check(name, False, shown)
    span = _span_days(days)
    n_hours = sum(len(ctx["risk_hb_hours"].get(d, set())) for d in span)
    return Check(name, True,
                 f"{span[0]} → {span[-1]} 逐日（含周末 {len(span)} 天）风控/"
                 f"对账/运行器（{sorted(magics)}）均有存活证据，风控心跳逐小时"
                 f"覆盖 {n_hours} 小时样本")


def accept_sl_tp_samples(ctx) -> Check:
    """判据 4（T53/C-2）：SL 与 TP 样本均存在——唯一采信源=C-2
    deals-*.jsonl 的 external_exit（reason∈{SL,TP}）。paper 影子账本与
    "[paper] 保护性出场"日志不再作为验收证据（R9-I3：该日志仅 paper 分支
    产生，Demo 真实成交路径不落此格式）。"""
    name = "SL 与 TP 样本均存在（C-2 deals external_exit）"
    sl = [d for d in ctx["deals_log"]
          if d.get("event") == "external_exit" and d.get("reason") == REASON_SL
          and _in_window(ctx, d["_t"])]
    tp = [d for d in ctx["deals_log"]
          if d.get("event") == "external_exit" and d.get("reason") == REASON_TP
          and _in_window(ctx, d["_t"])]
    if not sl or not tp:
        return Check(name, False,
                     f"C-2 成交流水 SL 样本 {len(sl)} 笔、TP 样本 {len(tp)} 笔"
                     "——无样本=未验收（§7 要求 SL/TP 均有实测出场证据；"
                     "paper 保护性出场日志不构成 Demo 证据）")
    return Check(name, True,
                 f"SL {len(sl)} 笔、TP {len(tp)} 笔（C-2 deals external_exit）"
                 "均已有实测样本")


def accept_drills(ctx) -> Check:
    """判据 5（T53）：§7 六项演练逐项判据（运行窗内），每项缺失=未验收。

    - killSwitch：RISK_HALT 告警消息含 kill_switch（停机）+ 其后有 halt=否
      的风控行（恢复对——恢复不单设标记，由风控日志 halt=否 推导）；
    - 心跳超时：HEARTBEAT_TIMEOUT 告警（恢复记录由逐日复核
      check_heartbeat_continuity 把关）；
    - 分策略暂停：RECONCILE_MISMATCH 告警 + pause-<magic>.json 解除
      （paused=false 且 ts 晚于告警——对账进程一致后写回）；
    - 重连：运行器日志 C-4 ``gateway_reconnected`` 行（E 组网关失败后
      首次成功时写）；
    - 幂等（推导，不设标记）：orders-*.jsonl 运行窗内开仓请求按
      （magic, decision_id=bar）分组无重复，且至少有 1 笔样本；
    - 换日禁入：RISK_REJECT 告警含 forbidden_session（禁入窗拒单记录）。"""
    name = "§7 六项演练证据（运行窗内逐项）"
    if ctx["run_window"][0] is None:
        return Check(name, False, "运行窗不可定（C-1 manifest 缺失/无 runs）"
                     "——演练证据无从核")
    missing, notes = [], []
    # 1) killSwitch 停机 + 恢复对
    halts = [a for a in ctx["alerts"]
             if a.kind == "RISK_HALT" and KILL_SWITCH_MARK in a.message
             and _in_window(ctx, a.time)]
    recovered = any(
        lf.kind == "risk" and "halt=否" in ln
        and (ts := _dot_line_ts(ln)) is not None
        and _in_window(ctx, ts) and any(ts > a.time for a in halts)
        for lf in ctx["logs"] for ln in lf.lines)
    if halts and recovered:
        notes.append(f"killSwitch×{len(halts)}（停机告警+恢复 halt=否）")
    else:
        why = "无 kill_switch 停机告警" if not halts else "停机后无 halt=否 恢复行"
        missing.append(f"killSwitch 停机+恢复对（{why}）")
    # 2) 心跳超时
    hb = [a for a in ctx["alerts"]
          if a.kind == "HEARTBEAT_TIMEOUT" and _in_window(ctx, a.time)]
    if hb:
        notes.append(f"心跳超时×{len(hb)}")
    else:
        missing.append("心跳超时（HEARTBEAT_TIMEOUT 告警）")
    # 3) 分策略暂停 + 解除
    mism = [a for a in ctx["alerts"]
            if a.kind == "RECONCILE_MISMATCH" and _in_window(ctx, a.time)]
    released = any(
        not doc.get("paused")
        and (ts := _parse_any_ts(doc.get("ts"))) is not None and _in_window(ctx, ts)
        and any(ts > a.time and ("formal_window" not in ctx or
                mg in _alert_magics(a.message, ctx["manifest_magics"])) for a in mism)
        for mg, doc in ctx["pauses"].items())
    if mism and released:
        notes.append(f"分策略暂停×{len(mism)}+解除")
    else:
        why = "无 RECONCILE 暂停告警" if not mism else "暂停未解除（pause 仍 paused）"
        missing.append(f"分策略暂停+解除（{why}）")
    # 4) 重连（C-4）
    reconnected = any(
        lf.kind == "runner" and GATEWAY_RECONNECTED_MARK in ln
        and (ts := (_formal_log_time(ln) if "formal_window" in ctx else
                    parse_iso(m.group(1)) if (m := TS_RE.search(ln)) else None)) is not None
        and _in_window(ctx, ts)
        for lf in ctx["logs"] for ln in lf.lines)
    if reconnected:
        notes.append("重连 gateway_reconnected")
    else:
        missing.append("重连（运行器日志 gateway_reconnected 行）")
    # 5) 幂等（推导）
    opens = [r for r in ctx["orders"]
             if r.get("kind") == "open" and r.get("decision_id", "-") != "-"
             and _in_window(ctx, r["_t"])]
    groups: dict[tuple[int, str], int] = {}
    for r in opens:
        key = (int(r.get("magic", 0)), str(r.get("decision_id")))
        groups[key] = groups.get(key, 0) + 1
    dup = {k: v for k, v in groups.items() if v > 1}
    if opens and not dup:
        notes.append(f"幂等（{len(opens)} 笔开仓请求同 bar 同 magic 无重复）")
    elif dup:
        shown = "、".join(f"magic {m} {did}×{n}"
                          for (m, did), n in sorted(dup.items())[:3])
        missing.append(f"幂等（同 bar 同 magic 重复开仓请求：{shown}）")
    else:
        missing.append("幂等（运行窗内无开仓请求样本可推导）")
    # 6) 换日禁入拒单
    rejects = [a for a in ctx["alerts"]
               if a.kind == "RISK_REJECT" and "forbidden_session" in a.message
               and _in_window(ctx, a.time)]
    if rejects:
        notes.append(f"换日禁入拒单×{len(rejects)}")
    else:
        missing.append("换日禁入（forbidden_session 拒单记录）")
    if missing:
        return Check(name, False, "演练证据缺失 " + "；".join(missing)
                     + "——每项缺失=未验收")
    return Check(name, True, "六项齐全：" + "，".join(notes))


def accept_double_flatten(ctx) -> Check:
    """判据 6（T54/C-3）：双全平演练真实完成——两点收口：

    - 证据源=C-3 drill-archive/<command_id>/（归档删除**前**复制的指令与
      全部 ack 副本）或 commands 目录兜底；孤儿 ack（无指令文档/无 magic/
      编号不匹配）不计；
    - 两轮指令（不同 uuid）均在**运行窗内**（窗外旧指令——D′ 10 月演练
      的指令——不计）；每轮须有全部参与 magic（manifest 集合）的**关联**
      ack：command_id 匹配 + magic + ack 时间在指令后 ACK_LINK_WINDOW_S 内
      + closed>0 或附空仓证明——编号协议的完成 ack 本身即空仓证明
      （executor 仅在重查确认本 magic 持仓与挂单均清零后才写 ack，
      mt5live.commands.write_ack 契约），closed=0 的关联 ack 计入并标注；
    - 一轮缺任一 magic 的 ack=该轮未完成；不足两轮完整=未验收。"""
    name = "双全平演练真实完成（两轮指令×全部 magic 关联 ack）"
    start, end = ctx["run_window"]
    if start is None:
        return Check(name, False, "运行窗不可定（C-1 manifest 缺失/无 runs）")
    magics = set(ctx["manifest_magics"]) or ctx["runner_magics"]
    rounds = ctx["drill_rounds"] if "formal_window" in ctx else load_drill_rounds(ctx["live_dir"])
    complete: list[tuple[str, datetime, list[str]]] = []
    incomplete: list[tuple[str, list[int]]] = []
    n_stale = 0
    for cid, r in sorted(rounds.items()):
        ts = r["command_ts"]
        if r["command"] is None or ts is None:
            continue                     # 无指令文档：孤儿 ack 不计
        if not (start <= ts <= end):
            n_stale += 1                 # 窗外旧指令（D′）不计
            continue
        missing: list[int] = []
        detail: list[str] = []
        for m in sorted(magics):
            acks = [a for a in r["acks"]
                    if a["id"] == cid and a["magic"] == m
                    and _integer(a["closed"])
                    and ts <= a["ts"]
                    <= min(end, ts + timedelta(seconds=ACK_LINK_WINDOW_S))]
            if not acks:
                missing.append(m)
                continue
            best = max(acks, key=lambda a: a["ts"])
            proof = (f"{m}:closed={best['closed']}" +
                     ("（空仓证明）" if best["closed"] <= 0 else ""))
            detail.append(proof)
        if missing:
            incomplete.append((cid, missing))
        else:
            complete.append((cid, ts, detail))
    if len(complete) < 2:
        parts = [f"完整轮 {len(complete)}（<2）"]
        if incomplete:
            parts.append("未完成轮：" + "；".join(
                f"{cid[:8]} 缺 magic {[m for m in mg]}" for cid, mg in incomplete))
        if n_stale:
            parts.append(f"窗外旧指令 {n_stale} 个（D′，不计）")
        orphans = sum(1 for r in rounds.values() if r["command"] is None)
        if orphans:
            parts.append(f"孤儿 ack（无指令文档）{orphans} 组不计")
        return Check(name, False,
                     "；".join(parts) + "——§7 要求连续两次、每轮全部参与 "
                     f"magic（{sorted(magics)}）均有三点关联的完成 ack"
                     "（RISK_FLATTEN 告警条数/孤立 ack 文件不能代替）")
    detail = "；".join(f"{cid[:8]}@{ts:%m-%d %H:%M}（{'，'.join(d)}）"
                       for cid, ts, d in complete[:2])
    return Check(name, True,
                 f"{len(complete)} 轮完整（两轮以上、不同 uuid、窗内、全员关联"
                 f" ack）：{detail}" + (f"；另有完整轮 {len(complete) - 2} 轮"
                                       if len(complete) > 2 else ""))


def accept_spread_samples(ctx) -> Check:
    """判据 7（T53 结构化解析）：实际点差样本存在且无超标小时——覆盖工作日
    逐日：日报存在、非"当日无点差样本"、无"**超标**"行，且含**可解析的结构
    化点差行**（品种|小时|样本，monitor.render_daily_report 表格式；只查
    非空文本/否定词会被标题日报蒙混——R9-I3 反例）。"""
    name = "实际点差样本存在且无超标小时（结构化行）"
    days = sorted(ctx["activity_days"])
    if not days:
        return Check(name, False, "无活动日——无从要求点差样本")
    problems = []
    weekdays = [d for d in _span_days(days) if d.isoweekday() <= 5]
    for d in weekdays:
        path = os.path.join(ctx["reports_dir"], "live",
                            f"daily-{d.isoformat()}.md")
        if not os.path.exists(path):
            problems.append(f"{d.isoformat()} 日报缺失")
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            problems.append(f"{d.isoformat()} 日报不可读：{e}")
            continue
        if not text.strip():
            problems.append(f"{d.isoformat()} 日报为空")
            continue
        if "当日无点差样本" in text:
            problems.append(f"{d.isoformat()} 当日无点差样本（无样本=未验收）")
            continue
        n_breach = text.count("**超标**")
        if n_breach:
            problems.append(f"{d.isoformat()} 点差超标 {n_breach} 行"
                            "（超限小时已由日报列出，超出模型区间）")
            continue
        rows = [(sym, int(h), int(n)) for sym, h, n in SPREAD_ROW_RE.findall(text)
                if 0 <= int(h) <= 23 and int(n) > 0]
        if not rows:
            problems.append(
                f"{d.isoformat()} 日报无结构化点差行（须含 品种|小时|样本 "
                "可解析行——标题/纯文本不算样本）")
    if problems:
        shown = "；".join(problems[:6])
        if len(problems) > 6:
            shown += f" 等 {len(problems)} 项"
        return Check(name, False, shown)
    return Check(name, True,
                 f"覆盖 {len(weekdays)} 个交易日日报均有结构化点差样本行且无"
                 "**超标** 小时（PaperSoak 不产生经纪商点差/滑点证据）")


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    """线性插值分位数（无 numpy 依赖；空表 None）。"""
    if not sorted_vals:
        return None
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def accept_slippage(ctx) -> Check:
    """判据 8（T53）：滑点样本——C-2 fill_open 成交价 vs 决策参考价
    （buy=ask / sell=bid，取自运行器决策行的 bid=/ask=）的差值分布：
    运行窗内有样本 + min/p50/p95/max 分位数入报告（无阈值判定，如实呈现
    分布；PaperSoak 影子盘按决策价成交、不产生经纪商滑点——无样本=未验收）。

    关联键 = **解析后的 bar 收盘时刻**（T59/P2）：决策行 decision_id 后缀
    （naive isoformat）与 C-2 decision_bar（iso_utc 带 +00:00）经
    ``_bar_close_key`` 归一为 naive UTC datetime——字符串相等比较在两种
    真实形态之间恒 miss（C-2 断链根因）。"""
    name = "滑点样本（deals 成交价 vs 决策参考价）"
    if ctx["run_window"][0] is None:
        return Check(name, False, "运行窗不可定（C-1 manifest 缺失/无 runs）")
    formal = "formal_window" in ctx
    refs: dict[tuple, tuple[float, float]] = {}
    for lf in ctx["logs"]:
        if lf.kind != "runner":
            continue
        for ln in lf.lines:
            m = DECISION_QUOTE_RE.search(ln)
            if m:
                key = _bar_close_key(m.group(1))
                if key is None:
                    continue
                try:
                    refs[(lf.magic if formal else None, key)] = (float(m.group(2)), float(m.group(3)))
                except ValueError:
                    continue
    samples: list[float] = []
    for d in ctx["deals_log"]:
        if d.get("event") != "fill_open" or not _in_window(ctx, d["_t"]):
            continue
        bar_key = _bar_close_key(d.get("decision_bar"))
        ref = refs.get((d["magic"] if formal else None, bar_key)) if bar_key is not None else None
        if ref is None or not isinstance(d.get("price"), (int, float)):
            continue
        bid, ask = ref
        samples.append(float(d["price"]) - ask if d.get("side") == "buy"
                       else bid - float(d["price"]))
    if not samples:
        return Check(name, False,
                     "无滑点样本（C-2 fill_open 无运行窗内样本，或决策行缺 "
                     "bid/ask 参考价）——无样本=未验收（滑点证据须 Demo 真实"
                     "成交，PaperSoak 影子盘不产生经纪商滑点）")
    s = sorted(samples)

    def _f(v: float | None) -> str:
        return "—" if v is None else f"{v:+.2f}"

    return Check(name, True,
                 f"{len(s)} 样本：min {_f(s[0])} / p50 {_f(_quantile(s, .5))} / "
                 f"p95 {_f(_quantile(s, .95))} / max {_f(s[-1])}（价格单位，"
                 "正=劣于决策参考价）")


ACCEPT_CHECKS = (
    accept_evidence_source,
    accept_positions_state,
    accept_day_coverage,
    accept_process_liveness,
    accept_sl_tp_samples,
    accept_drills,
    accept_double_flatten,
    accept_spread_samples,
    accept_slippage,
)


# ---------------------------------------------------------------- 汇总渲染

def build_ctx(args) -> dict:
    live_dir = os.path.join(args.data_dir, "live")
    reports_dir = args.reports_dir
    log_dirs = [args.log_dir, os.path.join(args.data_dir, "live")]
    logs = scan_logs(log_dirs)
    states = load_paper_states(live_dir)
    deals = all_deals(states)
    try:
        alerts = list(read_alerts(live_dir))
    except (OSError, ValueError) as e:
        print(f"× alerts.log 不可读：{e}", file=sys.stderr)
        sys.exit(2)
    report_days = _report_days(reports_dir)
    all_days = ({a.time.date() for a in alerts}
                | {d["_t"].date() for d in deals}
                | report_days)
    session_policy = None
    try:
        from mt5data.sessions import load_session_policy
        session_policy = load_session_policy(args.sessions_config)
    except Exception as e:  # SessionConfigError 等：如实标注为无法判定
        print(f"× 会话配置加载失败：{e}", file=sys.stderr)
    start_events = log_start_events(logs)
    log_tail_text: dict[tuple[int | None, str], str] = {}
    for lf in logs:
        if lf.kind == "runner":
            log_tail_text[(lf.magic, os.path.basename(lf.path))] = \
                "\n".join(lf.lines[-80:])
    day_evidence = scan_log_days(logs)
    runner_magic_days = day_evidence["runner_days"]
    # 运行器 magic 全集：日志（含启动行 magic）∪ 影子状态 ∪ 意图文件——
    # 有状态/意图却无日志 = 无法证明运行器存活过（--accept 不通过）
    intent_symbols: dict[int, str] = {}
    expected_magics = set()
    for path in glob.glob(os.path.join(live_dir, "expected-*.json")):
        m = re.search(r"expected-(\d+)\.json$", os.path.basename(path))
        if not m:
            continue
        magic = int(m.group(1))
        expected_magics.add(magic)
        try:
            doc = _read_json(path)
            sym = doc.get("symbol")
            if isinstance(sym, str) and sym:
                intent_symbols[magic] = sym
        except (OSError, ValueError):
            pass
    runner_magics = ({m for m in runner_magic_days if m is not None}
                     | set(states.keys()) | expected_magics)
    activity_days = (all_days
                     | {d for ds in runner_magic_days.values() for d in ds})
    # ---- R10 契约证据（T53/T54）----
    executor_states = load_executor_states(live_dir)
    manifest, manifest_error = load_run_manifest(live_dir)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    win_start = manifest_run_start(manifest)
    # T59/P4：activity_days 的四个消费点（连续覆盖 accept_day_coverage、
    # 逐日存活 accept_process_liveness、点差样本覆盖 accept_spread_samples、
    # --accept 逐日严格复核 sweep）在此统一按 C-1 运行窗裁剪——窗外旧证据
    # （D′ 演练期遗留的告警/日志/日报）既不得撑长覆盖跨度，也不得反过来
    # 被要求补齐窗内才需要的证据。窗不可得（无 manifest/无合法 runs）保持
    # 原样；--day/--summary 宽松档消费 all_days，本就不受影响。
    if win_start is not None:
        lo_day, hi_day = win_start.date(), now.date()
        activity_days = {d for d in activity_days if lo_day <= d <= hi_day}
    deals_log, deals_log_bad = load_deals_log(live_dir)
    paper_state_files = sorted(glob.glob(
        os.path.join(live_dir, "paper", "state-*.json")))
    paper_log_count = sum(1 for lf in logs for ln in lf.lines
                          if PAPER_LOG_MARK in ln)
    manifest_magics: list[int] = []
    if isinstance(manifest, dict):
        try:
            manifest_magics = [int(m) for m in (manifest.get("magics") or [])]
        except (TypeError, ValueError):
            manifest_magics = []
    evidence_magics = ({d["magic"] for d in deals_log}
                       | {m for m in runner_magic_days if m is not None}
                       | expected_magics | set(executor_states))
    return {
        "live_dir": live_dir, "reports_dir": reports_dir,
        "states": states, "deals": deals + deals_log, "alerts": alerts,
        "orders": load_order_log(live_dir),
        "pauses": load_pause_files(live_dir),
        "executor_states": executor_states,
        "intent_symbols": intent_symbols,
        "logs": logs, "start_events": start_events,
        "log_tail_text": log_tail_text,
        "ops_restarts": load_ops_restarts(live_dir),
        "session_policy": session_policy,
        "now": now,
        "all_days": all_days, "latest_day": max(all_days) if all_days else None,
        "activity_days": activity_days,
        "runner_magic_days": runner_magic_days,
        "runner_magics": runner_magics,
        "risk_days": day_evidence["risk_days"],
        "reconcile_days": day_evidence["reconcile_days"],
        "heartbeat_ok_days": day_evidence["heartbeat_ok_days"],
        "risk_hb_hours": day_evidence["risk_hb_hours"],
        # T53/T54 契约证据
        "manifest": manifest, "manifest_error": manifest_error,
        "manifest_magics": manifest_magics,
        "run_window": (win_start, now),
        "deals_log": deals_log, "deals_log_bad": deals_log_bad,
        "paper_state_files": paper_state_files,
        "paper_log_count": paper_log_count,
        "evidence_magics": evidence_magics,
    }


def _report_days(reports_dir: str) -> set[date]:
    out = set()
    for path in glob.glob(os.path.join(reports_dir, "live", "daily-*.md")):
        m = re.search(r"daily-(\d{4}-\d{2}-\d{2})\.md$", os.path.basename(path))
        if m:
            try:
                out.add(date.fromisoformat(m.group(1)))
            except ValueError:
                continue
    return out


def run_day(ctx, day: date) -> DayReport:
    rep = DayReport(day)
    for fn in CHECKS:
        rep.checks.append(fn(ctx, day))
    return rep


def print_day(rep: DayReport) -> None:
    print(f"== 模拟盘演练验收检查 {rep.day.isoformat()}（UTC 日） ==")
    for c in rep.checks:
        if c.ok is True:
            mark = "通过"
        elif c.ok is False:
            mark = "不通过"
        else:
            mark = "无样本"
        print(f"[{mark}] {c.name}：{c.detail}")
    print()


def print_summary(ctx, reps: list[DayReport]) -> None:
    print("== 模拟盘演练检查汇总（日常口径，蓝图 §7） ==")
    for rep in reps:
        n_ok = sum(1 for c in rep.checks if c.ok is True)
        n_bad = len(rep.failed)
        n_na = sum(1 for c in rep.checks if c.ok is None)
        status = "未见异常" if (n_bad == 0 and n_na == 0) else (
            f"{n_bad} 项不通过" if n_bad else "无未决样本")
        failed_names = "、".join(c.name for c in rep.failed) or "—"
        print(f"{rep.day.isoformat()}  通过 {n_ok} / 不通过 {n_bad} / "
              f"无样本 {n_na}（{status}）不通过项：{failed_names}")
    n_fail = sum(len(r.failed) for r in reps)
    print()
    print("演练证据（§7 演练项，全期累计展示——最终验收以 --accept 为准）：")
    kinds = {}
    for a in ctx["alerts"]:
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
    flatten = kinds.get("RISK_FLATTEN", 0)
    print(f"  一键停机/风控停机（RISK_HALT）：{kinds.get('RISK_HALT', 0)} 次")
    print(f"  日亏全平（RISK_FLATTEN）：{flatten} 次"
          + ("" if flatten >= 2 else "——§7 要求连续触发两次（≥2），未达标"))
    print(f"  心跳超时停机（HEARTBEAT_TIMEOUT）："
          f"{kinds.get('HEARTBEAT_TIMEOUT', 0)} 次（每次须有重启恢复记录）")
    rejects = sum(1 for a in ctx["alerts"]
                  if a.kind == "RISK_REJECT" and "forbidden_session" in a.message)
    print(f"  换日禁入拒单（forbidden_session）：{rejects} 条"
          + ("" if rejects >= 1 else "——§7 要求覆盖换日窗（含冬令时 22:00），未达标"))
    print(f"  覆盖天数：{len(reps)}（§7 要求连续 10 个交易日）")
    print()
    if n_fail:
        print(f"合计 {n_fail} 项不通过。")
    else:
        print("未发现异常证据（日常口径）——阶段 F 最终验收须另跑 --accept："
              "Demo 运行清单（C-1）/持仓状态/连续覆盖/逐小时进程存活/SL 与 TP "
              "样本（C-2）/六项演练/双全平（两轮指令×全部 magic 关联 ack，"
              "C-3）/结构化点差/滑点样本任一无证据 = 未验收。")


def run_accept(ctx) -> tuple[list[Check], list[tuple[date, list[str], list[str]]]]:
    """--accept：九条全期判据 + 逐日严格复核（ok=None 无样本=不通过）。

    逐日复核只跑覆盖跨度内的工作日（周末无交易/无日报，进程存活由判据 2
    的逐日风控/对账/心跳证据覆盖）。"""
    checks = [fn(ctx) for fn in ACCEPT_CHECKS]
    problems: list[tuple[date, list[str], list[str]]] = []
    days = sorted(ctx["activity_days"])
    if days:
        for d in _span_days(days):
            if d.isoweekday() > 5:
                continue
            rep = run_day(ctx, d)
            failed = [c.name for c in rep.checks if c.ok is False]
            nas = [c.name for c in rep.checks if c.ok is None]
            if failed or nas:
                problems.append((d, failed, nas))
    return checks, problems


def print_accept(ctx, checks: list[Check],
                 problems: list[tuple[date, list[str], list[str]]]) -> None:
    print("== 阶段 F 最终验收（--accept 严格口径：无样本/未覆盖=未验收） ==")
    days = sorted(ctx["activity_days"])
    if days:
        print(f"数据范围：{days[0].isoformat()} → {days[-1].isoformat()}")
    for c in checks:
        mark = "通过" if c.ok is True else "不通过"
        print(f"[{mark}] {c.name}：{c.detail}")
    print()
    print("逐日严格复核（覆盖跨度内交易日；无样本/无法判定项按不通过）：")
    weekdays = [d for d in _span_days(days) if d.isoweekday() <= 5]
    if not weekdays:
        print("  （无覆盖日——跳过）")
    else:
        bad = {d: (f, n) for d, f, n in problems}
        for d in weekdays:
            if d not in bad:
                print(f"  {d.isoformat()}：通过")
            else:
                failed, nas = bad[d]
                parts = []
                if failed:
                    parts.append("不通过：" + "、".join(failed))
                if nas:
                    parts.append("无样本（未验收）：" + "、".join(nas))
                print(f"  {d.isoformat()}：" + "；".join(parts))
    n_bad = sum(1 for c in checks if c.ok is not True)
    print()
    if n_bad == 0 and not problems:
        print("结论：验收通过——阶段 F 证据齐全（蓝图 §7，严格口径）。")
    else:
        print(f"结论：未通过（未验收）——判据不满足 {n_bad} 项、逐日复核异常 "
              f"{len(problems)} 天；日常检查用 --day/--summary（宽松口径）。")


def main(argv=None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # argparse accepts unambiguous long-option abbreviations. Recognize the
    # same formal prefixes before parsing so they cannot fall into build_ctx.
    formal_requested = any(
        a.startswith("--") and len(a.split("=", 1)[0]) > 2
        and any(option.startswith(a.split("=", 1)[0]) for option in ("--accept", "--formal-window"))
        for a in raw_argv)
    class Parser(argparse.ArgumentParser):
        def print_help(self, file=None):
            # Help actions (including every argparse abbreviation) run before
            # normal argument validation. Keep formal calls on the same exit2
            # result path instead of printing help and raising SystemExit(0).
            if formal_requested:
                raise FormalInvalid("arguments.help_requires_standalone_invocation")
            super().print_help(file)

        def exit(self, status=0, message=None):
            if formal_requested:
                raise FormalInvalid("arguments.invalid")
            super().exit(status, message)

        def error(self, message):
            if formal_requested:
                raise FormalInvalid("arguments.invalid")
            super().error(message)

    ap = Parser(description="阶段 F 日常检查 / 离线正式窗口机械预检（不审定历史真实性或 A5）")
    ap.add_argument("--data-dir", default="data", help="数据根（live=data/live）")
    ap.add_argument("--log-dir", default="data/demo",
                    help="进程日志目录（另自动并入 <data-dir>/live/*.log）")
    ap.add_argument("--reports-dir", default="data/reports",
                    help="日报根（daily-*.md 在 <reports-dir>/live/）")
    ap.add_argument("--sessions-config", default="config/sessions.toml",
                    help="会话配置（换日禁入窗唯一定义源）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", metavar="YYYY-MM-DD",
                   help="单日日常检查（宽松：无异常证据即 0，无样本如实标注）")
    g.add_argument("--summary", action="store_true",
                   help="全期逐日汇总（日常口径，同 --day 宽松）")
    ap.add_argument("--formal-window", help="FormalWindowV1 离线证据索引（必须搭配 --accept 和四个显式路径）")
    g.add_argument("--accept", action="store_true", help="正式窗口机械预检：满足 exit3，不满足 exit1，无效 exit2")
    try:
        args = ap.parse_args(raw_argv)
        if formal_requested:
            _require(args.accept and args.formal_window, "arguments.formal-window_required")
            explicit = {a.split("=", 1)[0] for a in raw_argv}
            _require({"--data-dir", "--log-dir", "--reports-dir", "--sessions-config"} <= explicit,
                     "arguments.explicit_offline_paths_required")
            descriptor_path = Path(args.formal_window)
            batch = FormalEvidence(descriptor_path.parent)
            descriptor = validate_formal_descriptor(batch.json(descriptor_path, "descriptor"))
            now = datetime.now(timezone.utc).replace(tzinfo=None)  # single production observation
            raw = load_formal_raw(args, descriptor, batch, now=now)
            ctx = build_accept_ctx(raw, raw["window"])
            checks = run_formal_accept(ctx)
            code = 3 if all(c.ok is True for c in checks) else 1
            return _formal_result(code, checks=checks, window=raw["window"])
    except FormalInvalid as exc:
        return _formal_result(2, error=str(exc))
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        if formal_requested:
            return _formal_result(2, error="evidence.invalid")
        raise

    ctx = build_ctx(args)
    if not ctx["all_days"]:
        print("× 未发现任何演练数据（成交/告警/日报均无）——先启动 demo_run.ps1")
        return 2

    if args.day:
        try:
            day = date.fromisoformat(args.day)
        except ValueError:
            print("× --day 须为 YYYY-MM-DD")
            return 2
        rep = run_day(ctx, day)
        print_day(rep)
        return 1 if rep.failed else 0

    reps = [run_day(ctx, d) for d in sorted(ctx["all_days"])]
    for rep in reps:
        print_day(rep)
    print_summary(ctx, reps)
    return 1 if any(r.failed for r in reps) else 0


if __name__ == "__main__":
    sys.exit(main())
