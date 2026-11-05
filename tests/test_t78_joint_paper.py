"""Synthetic joint T78 acceptance; BuyHold H is NOT an approved C002 candidate.

Only tmp data/config/artifacts are used. P is the real LevelDecisionS1Demo;
models are handwritten, never fitted. Experiment identities are assembled in
this checkout (the existing fingerprint includes absolute code-closure paths).
PaperBroker has no fee schedule: selected *real paper fills* receive explicitly
synthetic fees in their persisted snapshots, before fresh consumers reload them.
"""
from __future__ import annotations

import json
import socket
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mt5data.store import load_bars, save_bars
from mt5live import monitor, paper, runner as runner_module
from mt5live.commands import read_ack, read_flatten
from mt5live.fileio import write_json_atomic
from mt5live.gate import read_pause
from mt5live.monitor import write_role_heartbeat
from mt5live.paper import PaperAccountView, PaperStateGateway
from mt5live.reconcile import Intent, write_intent
from mt5live.riskproc import RiskProcess, load_default_session_policy, load_risk_config
from mt5quant.strategies.library.prob.level_decision import LevelDecisionS1Demo
from tests.test_t78_line_caps import BASE_TOML, H, P, script
from tests.test_t78_model_loading import (
    H_STRATEGY, NOW, frozen_model, make_config, offline_args, paper_inputs,
    read_json, runner, snapshot, update_model, write, write_config,
)


MODEL_KEYS = ("model_path", "model_sha256", "card_path", "experiment_sha256", "experiment")


class JointPaper:
    """Test orchestration only: real CLI, broker files, monitor and reconcile."""

    def __init__(self, root, cli):
        self.root, self.cli, self.now = root, cli, NOW
        path, cfg, _ = make_config(root)
        self.data = Path(cfg["data_root"])
        self.live = self.data / "live"
        self.args = paper_inputs(root, self.data) + ["--paper-spread", "0"]
        self.bars = load_bars(str(self.data), "XAUUSD", "H1")
        cfg["magic"] = P
        self.models = {True: dict(cfg)}
        weak = dict(cfg)
        update_model(path, weak, frozen_model(strong=False), rebind=True)
        self.models[False] = weak
        self.risk_path = write(root / "risk.toml", BASE_TOML + "".join(
            f'[[line_caps]]\nname = "{name}"\nmagic_min = {lo}\n'
            f'magic_max = {lo + 999}\ndaily_loss_cap_usd = 250.0\n'
            for name, lo in (("H", H), ("P", P))))
        self.risk_config = load_risk_config(str(self.risk_path))
        assert self.risk_config.daily_loss_cap_usd == 500
        assert [(c.name, c.magic_min, c.magic_max, c.daily_loss_cap_usd)
                for c in self.risk_config.line_caps] == [
                    ("H", 902000, 902999, 250), ("P", 903000, 903999, 250)]
        self.policy = load_default_session_policy(str(root / "sessions.toml"))
        self.risk_script = script("risk_process")
        self.reconcile_script = script("live_reconcile")

    def config(self, magic, *, strong=True):
        cfg = dict(self.models[strong], magic=magic)
        assert cfg["volume_lots"] == 0.01
        if H <= magic <= H + 999:
            cfg["strategy"] = H_STRATEGY
            for key in MODEL_KEYS:
                cfg.pop(key)
        else:
            assert P <= magic <= P + 999
        path = self.root / "config" / f"run-{magic}.toml"
        write_config(path, cfg)
        return path, cfg

    def run(self, magic, *, strong=True):
        path, _ = self.config(magic, strong=strong)
        write_role_heartbeat(str(self.live), "risk", interval=10, now=self.now)
        assert self.cli.main(offline_args(path) + self.args) == 0
        return read_json(self.live / f"heartbeat-{magic}.json")

    def book_path(self, magic):
        return self.live / "paper" / f"state-{magic}.json"

    def book(self, magic):
        return read_json(self.book_path(magic))

    def orders(self, magic):
        path = self.live / f"orders-{magic}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] \
            if path.exists() else []

    def intent(self, magic):
        return read_json(self.live / f"expected-{magic}.json")

    def account(self):
        return PaperAccountView(str(self.live / "paper")).snapshot()

    def fees(self, magic, *, commission, swap, fee):
        """Amend fees on a real fill, preserving tickets, volume and position.

        There is no broker/decision stub. Update balance/equity by the same fee
        delta so the persisted account remains internally consistent. Every CLI
        invocation constructs a new PaperBroker and reads these actual bytes.
        """
        book = self.book(magic)
        fill = book["deals"][0]
        assert fill["entry"] == "in" and fill["volume"] == 0.01
        before = sum(fill[k] for k in ("commission", "swap", "fee"))
        fill.update(commission=commission, swap=swap, fee=fee)
        delta = commission + swap + fee - before
        book["balance"] += delta
        book["equity"] += delta
        write_json_atomic(str(self.book_path(magic)), book)

    def advance(self, *, hours=1, mirror=False):
        self.now += timedelta(hours=hours)
        bars = self.bars.copy()
        bars["time"] += self.now - NOW
        if mirror:
            # Reflection around 99 preserves the quote and SL/TP safety while
            # changing the genuine S1:A3 fade from sell to buy at the 98 grid.
            bars["open"] = 198 - self.bars["open"]
            bars["close"] = 198 - self.bars["close"]
            bars["high"] = 198 - self.bars["low"]
            bars["low"] = 198 - self.bars["high"]
        save_bars(str(self.data), "XAUUSD", "H1", bars)

    def cycle(self):
        # A fresh monitor models restart; preserve real runner heartbeat content
        # and refresh only the clock through the existing writer (no waiting).
        for path in self.live.glob("heartbeat-*.json"):
            if path.stem.removeprefix("heartbeat-").isdigit():
                hb = read_json(path)
                hb.update(ts=self.now.isoformat() + "+00:00",
                          updated_at_utc=self.now.isoformat() + "+00:00")
                write_json_atomic(str(path), hb)
        write_role_heartbeat(str(self.live), "risk", interval=10, now=self.now)
        gateway = PaperStateGateway(str(self.live / "paper"))
        snap = self.risk_script.build_snapshot(gateway, self.now)
        risk = RiskProcess(self.risk_config, live_dir=str(self.live), session_policy=self.policy)
        return risk.run_cycle(snap, self.now, health_clock=lambda: self.now)

    def reconcile(self):
        gateway = PaperStateGateway(str(self.live / "paper"))
        return self.reconcile_script.run_once(
            str(self.live), gateway, volume_tolerance=1e-6, now=self.now)[0]

    def open_pair(self):
        for magic, signal, side in ((H, 1, "buy"), (P, -1, "sell")):
            hb = self.run(magic)
            assert hb["last_error"] is None
            assert hb["last_decision"]["signal"] == signal
            assert [(p["side"], p["volume"]) for p in self.book(magic)["positions"]] == [(side, 0.01)]
            assert [(o["kind"], o["lots"]) for o in self.orders(magic)] == [("open", 0.01)]
        assert self.reconcile() == 0

    def latch_p(self):
        self.fees(P, commission=-220, swap=-20, fee=-10)
        snap = self.account()
        assert sum(d.profit + d.commission + d.swap + d.fee for d in snap.deals) == -250
        assert sum(p.profit + p.swap for p in snap.positions) == 0
        result = self.cycle()
        assert result.heartbeat_ok and not result.halt_active
        assert not result.flatten_written and not result.daily_loss_breached
        assert read_pause(str(self.live), P).reason == "line_cap"
        assert len(self.account().positions) == 2
        assert read_flatten(str(self.live)) is None


@pytest.fixture
def joint(tmp_path, monkeypatch, runner):
    def forbidden(*args, **kwargs):
        pytest.fail("joint paper must not use network, MT5, fit or SQLite")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr("mt5gw.MT5Gateway.connect", forbidden)
    # The imported runner fixture also refuses gateway configuration and fit.
    monkeypatch.chdir(tmp_path)
    c = JointPaper(tmp_path, runner)
    monkeypatch.setattr(runner, "now_utc", lambda: c.now)
    monkeypatch.setattr(paper, "_now_utc", lambda: c.now)
    original_heartbeat = runner_module.write_heartbeat

    def heartbeat(*args, **kwargs):
        return original_heartbeat(*args, **dict(kwargs, now=c.now))

    monkeypatch.setattr(runner_module, "write_heartbeat", heartbeat)

    class FrozenMonitorTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.now.replace(tzinfo=tz)

    monkeypatch.setattr(monitor, "datetime", FrozenMonitorTime)
    return c


def test_main_models_change_real_p_signals_and_fills_in_shared_h_account(joint, monkeypatch):
    # Catches discarding with_model's new instance or ignoring model configuration.
    attached, consumed = [], []
    original_attach = LevelDecisionS1Demo.with_model
    original_runner = joint.cli.SignalRunner

    def attach(self, model):
        loaded = original_attach(self, model)
        assert loaded is not self
        attached.append(loaded)
        return loaded

    def consume(cfg, strategy, **kwargs):
        if P <= cfg.magic <= P + 999:
            assert strategy is attached[-1]
            consumed.append(strategy)
        return original_runner(cfg, strategy, **kwargs)

    monkeypatch.setattr(LevelDecisionS1Demo, "with_model", attach)
    monkeypatch.setattr(joint.cli, "SignalRunner", consume)
    joint.open_pair()
    weak = joint.run(P + 1, strong=False)
    assert weak["last_error"] is None and weak["last_decision"]["signal"] == 0
    assert joint.models[True]["model_sha256"] != joint.models[False]["model_sha256"]
    assert len(attached) == len(consumed) == 2
    assert joint.book(P + 1)["positions"] == joint.book(P + 1)["deals"] == []
    assert joint.orders(P + 1) == []
    assert joint.intent(P)["status"] == joint.intent(P + 1)["status"] == "confirmed"
    assert joint.intent(P)["volume"] == 0.01 and joint.intent(P + 1)["volume"] == 0
    assert {(p.magic, p.side, p.volume) for p in joint.account().positions} == {
        (H, "buy", 0.01), (P, "sell", 0.01)}
    assert joint.reconcile() == 0


@pytest.mark.parametrize("exit_kind", ["flat", "reverse"])
def test_p_line_boundary_preserves_stock_allows_close_and_h_new_opens(joint, exit_kind):
    # A line cap must neither force-close stock nor stop H/reducing P legs.
    joint.open_pair()
    ticket = joint.book(P)["positions"][0]["ticket"]
    joint.latch_p()
    joint.advance()
    held = joint.run(P)
    assert held["last_error"] is None and held["last_decision"]["signal"] == -1
    assert joint.book(P)["positions"][0]["ticket"] == ticket
    assert len(joint.orders(P)) == 1
    denied = joint.run(P + 999)
    assert "line_cap" in denied["last_error"]
    assert joint.book(P + 999)["deals"] == joint.orders(P + 999) == []
    allowed = joint.run(H + 999)
    assert allowed["last_error"] is None
    assert [(o["kind"], o["lots"]) for o in joint.orders(H + 999)] == [("open", 0.01)]
    joint.advance(mirror=exit_kind == "reverse")
    closed = joint.run(P, strong=exit_kind == "reverse")
    assert closed["last_decision"]["signal"] == (1 if exit_kind == "reverse" else 0)
    if exit_kind == "reverse":
        assert "line_cap" in closed["last_error"]
    else:
        assert closed["last_error"] is None
    assert [(o["kind"], o["lots"]) for o in joint.orders(P)] == [("open", 0.01), ("close", 0.01)]
    assert joint.book(P)["positions"] == []
    assert [(d["entry"], d["position_id"]) for d in joint.book(P)["deals"]] == [
        ("in", ticket), ("out", ticket)]
    assert joint.intent(P)["status"] == "confirmed" and joint.intent(P)["volume"] == 0
    assert joint.reconcile() == 0
    assert read_pause(str(joint.live), P).paused
    assert len(joint.account().positions) == 2  # Both H magics remain open.
    assert not (joint.live / "halt.json").exists()
    assert read_flatten(str(joint.live)) is None


def test_same_day_recovery_restart_and_clean_reconcile_do_not_release_p(joint):
    # Current PnL, a clean reconcile and a fresh magic cannot bypass the latch.
    joint.open_pair()
    joint.latch_p()
    latch = (joint.live / "line-cap-P.json").read_bytes()
    joint.fees(P, commission=0, swap=0, fee=0)  # Synthetic fee rebate: PnL recovers.
    assert joint.reconcile() == 0
    assert not joint.cycle().halt_active  # New RiskProcess, same persisted state.
    assert (joint.live / "line-cap-P.json").read_bytes() == latch
    joint.advance()
    assert joint.run(P, strong=False)["last_decision"]["signal"] == 0
    assert joint.reconcile() == 0
    joint.advance()
    for magic in (P, P + 999):
        hb = joint.run(magic)  # Fresh runner, broker and RiskGate on each launch.
        assert hb["last_decision"]["signal"] == -1
        assert "line_cap" in hb["last_error"]
        assert joint.book(magic)["positions"] == []
    assert [o["kind"] for o in joint.orders(P)] == ["open", "close"]
    assert joint.orders(P + 999) == []
    assert joint.run(H + 999)["last_error"] is None
    assert joint.reconcile() == 0
    assert read_pause(str(joint.live), P + 999).paused
    assert (joint.live / "line-cap-P.json").read_bytes() == latch


def test_rollover_preserves_manual_and_reconcile_pauses_on_real_paper_state(joint):
    # Rollover may remove only its own line state; it cannot erase other causes.
    joint.open_pair()
    manual_path = joint.live / f"pause-{H + 999}.json"
    manual = b'{"paused":true,"reason":"manual","owner":"operator"}'
    manual_path.write_bytes(manual)
    assert "manual" in joint.run(H + 999)["last_error"]
    joint.latch_p()
    assert joint.reconcile() == 0
    assert manual_path.read_bytes() == manual
    write_intent(str(joint.live / f"expected-{P}.json"),
                 Intent("XAUUSD", P, "sell", 0.02, "confirmed", "synthetic-mismatch", joint.now))
    assert joint.reconcile() == 1
    mismatch_path = joint.live / f"pause-{P}.json"
    mismatch = mismatch_path.read_bytes()
    assert read_json(mismatch_path)["reason"] == "reconcile_mismatch"
    joint.fees(P, commission=0, swap=0, fee=0)
    joint.advance(hours=24)
    assert not joint.cycle().halt_active
    assert not (joint.live / "line-cap-P.json").exists()
    assert mismatch_path.read_bytes() == mismatch
    assert manual_path.read_bytes() == manual
    assert "manual" in joint.run(H + 999)["last_error"]
    assert joint.orders(H + 999) == []
    assert joint.run(P + 999)["last_error"] is None
    assert joint.book(P + 999)["positions"][0]["volume"] == 0.01
    assert joint.reconcile() == 1  # Original mismatch still visible and paused.
    assert read_pause(str(joint.live), P).reason == "reconcile_mismatch"
    assert manual_path.read_bytes() == manual


@pytest.mark.parametrize("trigger", ["monitor", "gate"])
def test_account_boundary_overrides_p_pause_and_real_executors_ack_flatten(joint, trigger):
    # A prior line rejection must not hide the account -500 consequence.
    joint.open_pair()
    joint.latch_p()
    joint.fees(H, commission=-220, swap=-20, fee=-10)
    snap = joint.account()
    assert sum(d.profit + d.commission + d.swap + d.fee for d in snap.deals) == -500
    assert sum(p.profit + p.swap for p in snap.positions) == 0
    magics = [H, P]
    if trigger == "monitor":
        result = joint.cycle()
        assert result.daily_loss_breached and result.flatten_written and result.halt_active
    else:
        # New magic is also subject to the existing P latch. Real CLI calls gate.
        denied = joint.run(P + 999)
        assert "daily_loss_cap" in denied["last_error"]
        assert joint.orders(P + 999) == []
        magics.append(P + 999)
    command = read_flatten(str(joint.live))
    assert command is not None and command.id and command.reason == "daily_loss_cap"
    assert read_json(joint.live / "halt.json")["reason"] == "daily_loss_cap"
    assert all(read_ack(str(joint.live), magic) is None for magic in magics)
    joint.run(H)  # Even an already consumed signal must first consume flatten.
    assert read_ack(str(joint.live), H).id == command.id
    assert not joint.cycle().flatten_archived  # P has not acknowledged yet.
    assert read_flatten(str(joint.live)).id == command.id
    for magic in magics[1:]:
        joint.run(magic)
    for magic in magics:
        ack = read_ack(str(joint.live), magic)
        assert ack.id == command.id and ack.closed == (1 if magic in (H, P) else 0)
        assert joint.intent(magic)["volume"] == 0
        assert joint.intent(magic)["status"] == "confirmed"
        assert joint.intent(magic)["cause"] == f"flatten:{command.id}"
        if magic in (H, P):
            assert [(o["kind"], o["lots"]) for o in joint.orders(magic)] == [
                ("open", 0.01), ("close", 0.01)]
    assert joint.account().positions == ()
    assert joint.reconcile() == 0
    assert joint.cycle().flatten_archived
    assert read_flatten(str(joint.live)) is None
    archived = joint.live / "drill-archive" / command.id
    assert read_json(archived / "flatten.json")["id"] == command.id
    assert {read_json(archived / f"flatten.ack-{m}")["magic"] for m in magics} == set(magics)
    assert (joint.live / "halt.json").exists()
    joint.advance()
    assert "halted" in joint.run(H + 1)["last_error"]
    assert joint.orders(H + 1) == []


def test_invalid_model_and_online_fittable_refuse_before_runtime_reads_or_writes(joint, monkeypatch):
    # Protect an existing paper account, not just an initially empty directory.
    joint.open_pair()
    for online in (False, True):
        path, cfg = joint.config(P + 999)
        if not online:
            cfg["model_sha256"] = "0" * 64
            write_config(path, cfg)
        before = snapshot(joint.root)
        reached = []

        def forbidden(*args, **kwargs):
            reached.append(True)
            raise AssertionError("model preflight crossed a runtime side-effect boundary")

        with monkeypatch.context() as guard:
            for name in ("guarded_main", "MagicInstanceLock", "HeartbeatWriter",
                         "PaperBroker", "load_bars", "load_contract_sizes"):
                guard.setattr(joint.cli, name, forbidden)
            guard.setattr(PaperAccountView, "snapshot", forbidden)
            if online:
                guard.setattr("mt5quant.prob.artifact.load_model", forbidden)
                original_read = Path.read_bytes
                artifacts = {(path.parent / cfg[k]).resolve() for k in ("model_path", "card_path")}

                def no_artifact_read(self):
                    if self.resolve() in artifacts:
                        return forbidden()
                    return original_read(self)

                guard.setattr(Path, "read_bytes", no_artifact_read)
            argv = (["--config", str(path), "--once", "--paper"] if online
                    else offline_args(path) + joint.args)
            assert joint.cli.main(argv) == 2
        assert reached == []
        assert snapshot(joint.root) == before
