"""T78: synthetic risk process/gate -> executor/paper -> reconcile chains."""
from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mt5data.sessions import SessionPolicy
from mt5live import riskproc
from mt5live.executor import ExecutionAdapter, LiveExecutor
from mt5live.fileio import write_json_atomic
from mt5live.gate import RiskGate, read_pause
from mt5live.monitor import write_heartbeat, write_role_heartbeat
from mt5live.paper import PaperAccountView, PaperBroker, PaperStateGateway, paper_state_path
from mt5live.reconcile import Intent, write_intent, write_pause
from mt5live.riskproc import AccountSnapshot, OrderRequest, RiskConfig, RiskProcess, RiskState
from mt5quant.strategies.base import ExitRules
from tests.fakes_live import QuoteMarket, mk_decision
from tests.test_mt5live_riskproc import deal, pos

H, P = 902000, 903000
NOW = datetime(2026, 11, 5, 12)
POLICY = SessionPolicy(rollover_local="17:00", rollover_tz="America/New_York",
                       pre_minutes=5, post_minutes=60, exempt_symbols=frozenset())
ROOT = Path(__file__).resolve().parents[1]


def caps():
    # Baseline still reaches its real consumers, so RED is a missing decision,
    # not an ImportError for the new dataclass.
    cls = getattr(riskproc, "LineCap", SimpleNamespace)
    return tuple(cls(name=name, magic_min=lo, magic_max=lo + 999,
                     daily_loss_cap_usd=250.0) for name, lo in (("H", H), ("P", P)))


def config(*, enabled=True):
    cfg = RiskConfig(version=1, symbols=("XAUUSD",), max_lots_per_order=0.01,
                     max_positions_per_symbol=20, max_usd_exposure=1_000_000,
                     max_orders_per_minute=100, daily_loss_cap_usd=500.0)
    if enabled:
        if hasattr(cfg, "line_caps"):
            cfg = replace(cfg, line_caps=caps())
        else:
            object.__setattr__(cfg, "line_caps", caps())
    return cfg


def state(totals=None, *, realized=0.0):
    st = RiskState(positions=(), contract_sizes={"XAUUSD": 100},
                   prices={"XAUUSD": 2000}, today_realized=realized)
    if totals is not None:
        if hasattr(st, "line_pnl"):
            st = replace(st, line_pnl=totals)
        else:
            object.__setattr__(st, "line_pnl", totals)
    return st


def request(magic=H):
    return OrderRequest("XAUUSD", "buy", 0.01, magic)


def script(name):
    spec = importlib.util.spec_from_file_location(f"t78_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Chain:
    """Real file consumers, paper fills and intents; only market/time are fake."""

    def __init__(self, tmp_path, *, enabled=True):
        self.live = tmp_path / "live"
        self.live.mkdir(parents=True)
        self.now = NOW
        self.market = QuoteMarket(bid=2000, ask=2000)
        self.specs = tmp_path / "specs.json"
        write_json_atomic(str(self.specs), {"XAUUSD": {"contract_size": 100}})
        self.cfg = config(enabled=enabled)
        self.brokers = {}
        self.executors = {}
        self.risk = RiskProcess(self.cfg, live_dir=str(self.live), session_policy=POLICY)
        self.add(H)
        self.add(P)
        self.view = PaperAccountView(str(self.live / "paper"))
        self.gateway = PaperStateGateway(str(self.live / "paper"))
        self.gate = self.new_gate()
        for magic in (H, P):
            self.executors[magic] = self.executor(magic)
        self.healthy()

    def healthy(self):
        write_heartbeat(str(self.live), True, ts=self.now)
        write_role_heartbeat(str(self.live), "risk", interval=10, now=self.now)

    def new_gate(self):
        return RiskGate(self.risk, account=self.view, market=self.market,
                        live_dir=str(self.live), specs_path=str(self.specs), clock=lambda: self.now)

    def add(self, magic):
        self.brokers[magic] = PaperBroker(
            self.market, state_path=paper_state_path(str(self.live), magic),
            contract_sizes={"XAUUSD": 100}, magic=magic, clock=lambda: self.now)
        write_intent(str(self.live / f"expected-{magic}.json"),
                     Intent("XAUUSD", magic, "buy", 0, "confirmed", "initial", self.now))
        if hasattr(self, "gate"):
            self.executors[magic] = self.executor(magic)

    def executor(self, magic):
        broker = self.brokers[magic]
        adapter = ExecutionAdapter(broker, broker, self.market, symbol="XAUUSD", magic=magic)
        return LiveExecutor(adapter, symbol="XAUUSD", magic=magic, live_dir=str(self.live),
                            rules=ExitRules(), gate=self.gate)

    def trade(self, magic, signal, tag):
        return self.executors[magic].poll(
            mk_decision(decision_id=f"XAUUSD|M15|{tag}", signal=signal,
                        volume_lots=0.01, sl_atr=None, tp_atr=None), self.now)

    def mark(self, price):
        self.market.set_quote(price, price)
        for broker in self.brokers.values():
            broker.save()

    def cycle(self):
        self.healthy()
        snap = script("risk_process").build_snapshot(self.gateway, self.now)
        return self.risk.run_cycle(snap, self.now, health_clock=lambda: self.now)

    def reconcile(self):
        return script("live_reconcile").run_once(
            str(self.live), self.gateway, volume_tolerance=1e-6, now=self.now)[0]

    def open_check(self, magic):
        return self.gate.check_open("XAUUSD", "buy", 0.01, magic, self.now)

    def restart(self):
        self.risk = RiskProcess(self.cfg, live_dir=str(self.live), session_policy=POLICY)
        self.gate = self.new_gate()
        self.executors = {magic: self.executor(magic) for magic in self.brokers}


def test_monitor_latches_one_line_without_flatten_and_gate_blocks_new_magic(tmp_path):
    c = Chain(tmp_path)
    c.trade(H, 1, "h-open")
    c.trade(P, -1, "p-open")
    c.mark(1750)  # H -250; P +250; account 0.
    res = c.cycle()
    assert not res.flatten_written and not res.halt_active
    assert len(c.view.positions()) == 2
    pause = read_pause(str(c.live), H)
    assert pause.paused and pause.reason == "line_cap"
    assert not read_pause(str(c.live), P).paused
    assert c.reconcile() == 0
    assert read_pause(str(c.live), H).paused
    c.mark(2000)  # Recovery must not reset today's latch.
    c.restart()
    c.add(H + 999)
    denied = c.trade(H + 999, 1, "new-h")
    assert denied.action == "risk_rejected" and "line_cap" in denied.error
    assert not c.brokers[H + 999].positions()
    c.add(P + 999)
    c.trade(P + 999, 1, "new-p")
    assert len(c.brokers[P + 999].positions()) == 1
    c.cycle()
    assert read_pause(str(c.live), H + 999).paused
    assert (c.live / "alerts.log").read_text(encoding="utf-8").count("\tLINE_CAP\t") == 1


@pytest.mark.parametrize("loser,winner,price", [(H, P, 1750), (P, H, 2250)])
def test_gate_first_reconcile_monitor_interleave_and_reverse_close(tmp_path, loser, winner, price):
    c = Chain(tmp_path)
    c.trade(H, 1, "h-open")
    c.trade(P, -1, "p-open")
    c.mark(price)
    verdict = c.open_check(loser)
    assert not verdict.allowed and "line_cap" in verdict.reason
    assert not verdict.flatten_all and not verdict.halt
    assert c.reconcile() == 0
    c.cycle()
    c.restart()
    assert not c.open_check(loser).allowed
    assert c.open_check(winner).allowed
    assert len(c.view.positions()) == 2
    reverse = c.trade(loser, -1 if loser == H else 1, "reverse-loser")
    assert reverse.action == "risk_rejected" and reverse.net_after == 0
    assert [o.kind for o in reverse.orders] == ["close"]
    c.trade(winner, 0, "close-winner")
    assert not c.view.positions()
    assert not (c.live / "halt.json").exists()
    assert not (c.live / "commands" / "flatten.json").exists()


@pytest.mark.parametrize("entrypoint", ["gate", "monitor", "check_order"])
def test_account_cap_is_not_hidden_by_line_pause_and_consumers_flatten(tmp_path, entrypoint):
    c = Chain(tmp_path)
    c.trade(H, 1, "h-open")
    c.trade(P, 1, "p-open")
    c.mark(1875)
    # Earlier line latch must not preempt the subsequent account -500 decision.
    c.risk.check_order(request(), state({"H": -250, "P": 0}, realized=-250), c.now)
    c.mark(1750)
    if entrypoint == "gate":
        verdict = c.open_check(H)
        assert verdict.flatten_all and verdict.halt
    elif entrypoint == "monitor":
        assert c.cycle().flatten_written
    else:
        verdict = c.risk.check_order(request(), state({"H": -250, "P": -250}, realized=-500), c.now)
        assert verdict.flatten_all and verdict.halt
    command = json.loads((c.live / "commands" / "flatten.json").read_text(encoding="utf-8"))
    assert command["id"] and command["reason"] == "daily_loss_cap"
    for magic in (H, P):
        c.executors[magic].poll(None, c.now)
    assert not c.view.positions()
    assert c.cycle().flatten_archived
    assert (c.live / "halt.json").exists()


def test_new_mismatch_survives_line_rollover_but_clean_reconcile_cannot_clear_line(tmp_path):
    c = Chain(tmp_path)
    c.trade(H, 1, "h-open")
    c.mark(1750)
    c.cycle()
    assert c.reconcile() == 0
    assert read_pause(str(c.live), H).paused
    write_intent(str(c.live / f"expected-{H}.json"),
                 Intent("XAUUSD", H, "buy", 0.02, "confirmed", "synthetic-mismatch", NOW))
    assert c.reconcile() == 1
    assert read_pause(str(c.live), H).reason == "reconcile_mismatch"
    c.mark(2000)
    c.now += timedelta(days=1)
    c.cycle()
    assert read_pause(str(c.live), H).reason == "reconcile_mismatch"
    assert not c.open_check(H).allowed
    assert c.open_check(H + 1).allowed


@pytest.mark.parametrize("raw", [
    '{"paused":true,"reason":"manual","owner":"operator"}',
    '{"paused":true,"reason":"unknown"}',
    '{broken', '{"paused":"false","reason":"manual"}', 'null', '[]',
    '{"paused":0,"reason":"unknown"}',
])
def test_foreign_or_broken_pause_bytes_survive_trigger_reconcile_and_rollover(tmp_path, raw):
    c = Chain(tmp_path)
    path = c.live / f"pause-{H}.json"
    path.write_text(raw, encoding="utf-8")
    c.risk.check_order(request(), state({"H": -250, "P": 0}, realized=-250), c.now)
    c.reconcile()
    c.now += timedelta(days=1)
    c.cycle()
    assert path.read_text(encoding="utf-8") == raw
    assert not c.open_check(H).allowed


def test_rollover_clears_only_own_previous_day_and_no_caps_keeps_legacy(tmp_path):
    c = Chain(tmp_path)
    c.risk.check_order(request(), state({"H": -250, "P": 0}, realized=-250), c.now)
    assert read_pause(str(c.live), H).paused
    c.now = c.now.replace(hour=23, minute=59, second=59)
    c.healthy()
    assert not c.open_check(H + 1).allowed
    c.now += timedelta(seconds=1)
    c.healthy()
    assert c.open_check(H).allowed
    assert not read_pause(str(c.live), H).paused
    old = Chain(tmp_path / "old", enabled=False)
    old.trade(H, 1, "open")
    old.mark(1750)
    old.cycle()
    assert old.open_check(H + 1).allowed
    assert not list(old.live.glob("line-cap-*.json"))


@pytest.mark.parametrize("damage", ["json", "null", "future", "owner", "range"])
def test_corrupt_or_foreign_line_state_blocks_restart_and_is_never_cleaned(tmp_path, damage):
    c = Chain(tmp_path)
    c.risk.check_order(request(), state({"H": -250, "P": 0}, realized=-250), c.now)
    paths = list(c.live.glob("line-cap-*.json"))
    assert paths, "a durable line latch is required even before any new magic registers"
    path = next(p for p in paths if json.loads(p.read_text(encoding="utf-8"))["line"] == "H")
    data = json.loads(path.read_text(encoding="utf-8"))
    if damage == "future":
        data["day"] = "2099-01-01"
    elif damage == "owner":
        data["owner"] = "operator"
    elif damage == "range":
        data["magic_max"] += 1
    raw = "{broken" if damage == "json" else "null" if damage == "null" else json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    c.now += timedelta(days=1)
    c.healthy()
    c.restart()
    assert not c.open_check(H + 1).allowed
    assert c.open_check(P + 1).allowed
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("totals", [None, {}, {"H": float("nan"), "P": 0}, {"H": True, "P": 0}])
def test_missing_or_invalid_line_snapshot_fails_closed_in_synchronous_process(tmp_path, totals):
    rp = RiskProcess(config(), live_dir=str(tmp_path), session_policy=POLICY)
    result = rp.check_order(request(), state(totals), NOW)
    assert not result.allowed and not result.flatten_all


def test_fees_floating_magic_boundaries_and_cash_exclusion_share_one_snapshot(tmp_path):
    c = Chain(tmp_path)
    snapshot = SimpleNamespace(state=SimpleNamespace(currency="USD"), positions=(
        pos("XAUUSD", 0.01, magic=H + 999, profit=-90, swap=-10),
        pos("XAUUSD", 0.01, magic=P, profit=20)), deals=(
        deal(magic=H, profit=-140, commission=-5, swap=-3, fee=-2, time=NOW),
        deal(magic=H - 1, profit=-100, time=NOW),
        deal(magic=H, entry=None, profit=10000, time=NOW),
        deal(magic=H, profit=-999, time=NOW - timedelta(days=1)),
        deal(magic=H, profit=-999, time=NOW + timedelta(seconds=1))))
    class SnapshotOnly:
        def snapshot(self):
            return snapshot
        def positions(self):
            raise AssertionError("must use one snapshot")
        def deals(self, *args):
            raise AssertionError("must use one snapshot")
    c.gate._account = SnapshotOnly()
    result = c.open_check(H)
    assert not result.allowed and "line_cap" in result.reason
    assert not result.flatten_all
    assert c.open_check(P).allowed


@pytest.mark.parametrize("bad", [float("nan"), True, "902000", -1])
def test_invalid_snapshot_magic_cannot_hide_loss_from_gate_or_monitor(tmp_path, bad):
    c = Chain(tmp_path)
    snap = AccountSnapshot("USD", (), (deal(magic=bad, profit=-250, time=NOW),))
    class BadAccount:
        def snapshot(self):
            return snap
    c.gate._account = BadAccount()
    try:
        verdict = c.open_check(H)
    except (ValueError, TypeError):
        pass  # Executor translates data errors to a rejection.
    else:
        assert not verdict.allowed
    with pytest.raises((ValueError, TypeError)):
        c.risk.run_cycle(snap, NOW, health_clock=lambda: NOW)


@pytest.mark.parametrize("stop", ["killSwitch", "halt.json", "health"])
def test_halt_and_risk_health_precede_any_account_ipc_with_caps(tmp_path, stop):
    c = Chain(tmp_path)
    class NoIPC:
        def snapshot(self):
            pytest.fail("stop/health must be checked before IPC")
    c.gate._account = NoIPC()
    if stop == "health":
        c.now += timedelta(hours=1)
    else:
        (c.live / stop).write_text('{}', encoding="utf-8")
    assert not c.open_check(H).allowed


@pytest.mark.parametrize("phase", ["trigger", "rollover"])
@pytest.mark.parametrize("reason", ["reconcile_mismatch", "manual"])
def test_pause_owner_is_read_after_lock_and_concurrent_writer_is_preserved(tmp_path, monkeypatch, phase, reason):
    c = Chain(tmp_path)
    if phase == "rollover":
        c.risk.check_order(request(), state({"H": -250, "P": 0}, realized=-250), NOW)
        c.now += timedelta(days=1)
    target = str(c.live / f"pause-{H}.json.lock")
    waiting, resume = Event(), Event()
    real_lock = riskproc.file_lock

    @contextmanager
    def delay_before_lock(path, **kwargs):
        if path == target and not waiting.is_set():
            waiting.set()
            assert resume.wait(5), "controlled writer did not finish"
        with real_lock(path, **kwargs):
            yield

    monkeypatch.setattr(riskproc, "file_lock", delay_before_lock)
    pnl = -250 if phase == "trigger" else 0
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(c.risk.check_order, request(), state({"H": pnl, "P": 0}, realized=pnl), c.now)
        try:
            assert waiting.wait(5)
            if reason == "manual":
                with real_lock(target):
                    write_json_atomic(str(c.live / f"pause-{H}.json"),
                                      {"paused": True, "reason": "manual", "owner": "operator"})
            else:
                write_pause(str(c.live), H, paused=True, reason=reason, now=c.now)
        finally:
            resume.set()
        pending.result(timeout=5)
    pause = read_pause(str(c.live), H)
    assert pause.paused and pause.reason == reason


def test_two_real_writers_serialize_the_pause_read_modify_write(tmp_path, monkeypatch):
    from mt5live import reconcile
    c = Chain(tmp_path)
    path = str(c.live / f"pause-{H}.json")
    writing, release, contender = Event(), Event(), Event()
    real_write, real_lock = riskproc.write_json_atomic, reconcile.file_lock

    def held_write(dest, obj):
        if dest == path:
            writing.set()
            assert release.wait(5)
        return real_write(dest, obj)

    @contextmanager
    def observed_lock(dest, **kwargs):
        if dest == path + ".lock":
            contender.set()
        with real_lock(dest, **kwargs):
            yield

    monkeypatch.setattr(riskproc, "write_json_atomic", held_write)
    monkeypatch.setattr(reconcile, "file_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        risk = pool.submit(c.risk.check_order, request(), state({"H": -250, "P": 0}, realized=-250), NOW)
        try:
            assert writing.wait(5)
            mismatch = pool.submit(write_pause, str(c.live), H, paused=True,
                                   reason="reconcile_mismatch", now=NOW)
            assert contender.wait(5)
            assert not mismatch.done(), "reconcile must wait for the risk writer's lock"
        finally:
            release.set()
        risk.result(timeout=5)
        mismatch.result(timeout=5)
    assert read_pause(str(c.live), H).reason == "reconcile_mismatch"
    assert not c.open_check(H + 1).allowed


@pytest.mark.parametrize("damage", ["future", "owner", "extra"])
def test_line_like_pause_without_proven_ownership_survives_rollover(tmp_path, damage):
    c = Chain(tmp_path)
    c.risk.check_order(request(), state({"H": -250, "P": 0}, realized=-250), NOW)
    path = c.live / f"pause-{H}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if damage == "future":
        data["day"] = "2099-01-01"
    elif damage == "owner":
        data["owner"] = "operator"
    else:
        data["manual_reason"] = "keep-paused"
    raw = json.dumps(data)
    path.write_text(raw, encoding="utf-8")
    c.reconcile()
    c.now += timedelta(days=1)
    c.healthy()
    assert not c.open_check(H).allowed
    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("boundary", [-249.99, -250.0])
def test_synchronous_line_boundary_and_unconfigured_magic(tmp_path, boundary):
    rp = RiskProcess(config(), live_dir=str(tmp_path), session_policy=POLICY)
    st = state({"H": boundary, "P": 0}, realized=boundary)
    assert rp.check_order(request(), st, NOW).allowed == (boundary > -250)
    assert rp.check_order(request(H - 1), st, NOW).allowed


@pytest.mark.parametrize("damage", ["line_state", "pause", "quote", "specs", "magic"])
def test_account_loss_consequence_precedes_subline_and_market_failures(tmp_path, damage):
    c = Chain(tmp_path)
    c.risk.check_order(request(), state({"H": -250, "P": 0}, realized=-250), NOW)
    if damage == "line_state":
        (c.live / "line-cap-H.json").write_text("{broken", encoding="utf-8")
    elif damage == "pause":
        (c.live / f"pause-{H}.json").write_text("{broken", encoding="utf-8")
    elif damage == "quote":
        c.gate._market = SimpleNamespace(quote=lambda _: pytest.fail("must flatten before quotes"))
    elif damage == "specs":
        c.specs.unlink()
    snap = SimpleNamespace(currency="USD", positions=(), deals=(deal(magic="bad" if damage == "magic" else H,
                                                     profit=-500, time=NOW),))
    c.gate._account = SimpleNamespace(snapshot=lambda: snap)
    assert c.open_check(H).flatten_all
    assert (c.live / "commands" / "flatten.json").exists()


def test_pure_check_and_monitor_nan_and_non_usd_are_fail_closed(tmp_path):
    cfg = config()
    assert not riskproc.check(request(), state({"H": -250, "P": 0}), cfg, NOW, POLICY).allowed
    c = Chain(tmp_path)
    c.gate._account = SimpleNamespace(snapshot=lambda: SimpleNamespace(
        positions=(), deals=(), state=SimpleNamespace(currency="EUR")))
    with pytest.raises(ValueError, match="USD"):
        c.open_check(H)
    with pytest.raises(ValueError):
        c.risk.run_cycle(AccountSnapshot("USD", (), (deal(profit=float("nan"), magic=H),)), NOW,
                         health_clock=lambda: NOW)


BASE_TOML = '''version = 1
symbols = ["XAUUSD"]
max_lots_per_order = 0.01
max_positions_per_symbol = 20
max_usd_exposure = 1000000.0
max_orders_per_minute = 100
daily_loss_cap_usd = 500.0
'''
LINE_TOML = '''[[line_caps]]
name = "H"
magic_min = 902000
magic_max = 902999
daily_loss_cap_usd = 250.0
'''


def test_optional_toml_line_caps_roundtrip_and_legacy(tmp_path):
    path = tmp_path / "risk.toml"
    path.write_text(BASE_TOML + LINE_TOML, encoding="utf-8")
    loaded = riskproc.load_risk_config(str(path))
    assert loaded.line_caps[0].magic_max == 902999
    assert loaded.daily_loss_cap_usd == 500
    path.write_text(BASE_TOML, encoding="utf-8")
    assert riskproc.load_risk_config(str(path)).line_caps == ()


@pytest.mark.parametrize("field,value", [
    ("name", '""'), ("name", '"../H"'), ("magic_min", "true"),
    ("magic_min", "-1"), ("magic_max", "902000.5"), ("magic_max", "901999"),
    ("daily_loss_cap_usd", "0"), ("daily_loss_cap_usd", "-1"),
    ("daily_loss_cap_usd", "nan"), ("daily_loss_cap_usd", "inf"),
    ("daily_loss_cap_usd", "true"),
])
def test_line_config_rejects_invalid_values(tmp_path, field, value):
    lines = LINE_TOML.splitlines()
    changed = "\n".join(f"{field} = {value}" if ln.startswith(field + " =") else ln for ln in lines)
    path = tmp_path / "risk.toml"
    path.write_text(BASE_TOML + changed, encoding="utf-8")
    with pytest.raises(riskproc.RiskConfigError):
        riskproc.load_risk_config(str(path))


@pytest.mark.parametrize("extra", [LINE_TOML, LINE_TOML.replace('"H"', '"P"'),
                                    LINE_TOML.replace("902000", "903000").replace("902999", "903999")])
def test_line_config_rejects_overlap_and_duplicate_name(tmp_path, extra):
    path = tmp_path / "risk.toml"
    path.write_text(BASE_TOML + LINE_TOML + extra, encoding="utf-8")
    with pytest.raises(riskproc.RiskConfigError):
        riskproc.load_risk_config(str(path))
