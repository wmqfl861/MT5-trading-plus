"""T110: health is completed work; missing/future/unreadable data fail closed."""
import builtins
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest

from mt5live.monitor import heartbeat_files, heartbeat_status, read_heartbeat_path
from mt5live.executor import ExecutionAdapter, LiveExecutor
from mt5quant.strategies.base import ExitRules
from tests.fakes_live import FakeGateway, mk_decision
from tests.test_mt5live_gate import make_gate, NOW, MAGIC

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("state", ["missing", "stale", "future", "malformed"])
def test_unhealthy_risk_rejects_before_account_calls_and_still_closes(tmp_path, state):
    gate, live = make_gate(tmp_path)
    path = live / "heartbeat-risk.json"
    path.unlink(missing_ok=True)
    if state != "missing":
        ts = NOW + timedelta(seconds=1 if state == "future" else -121)
        path.write_text("{" if state == "malformed" else json.dumps(
            {"runner_alive": True, "ts": ts.isoformat(), "interval_s": 10}), encoding="utf-8")
    broker = gate._account
    original = broker.positions
    def blocked(*a, **k):
        raise AssertionError("health must be checked before gateway assembly")
    broker.positions = blocked
    verdict = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    assert not verdict.allowed and "risk_health" in verdict.reason
    assert "RISK_HEALTH" in (live / "alerts.log").read_text(encoding="utf-8")
    broker.positions = original
    broker.seed_position(magic=MAGIC)
    exe = LiveExecutor(ExecutionAdapter(broker, broker, broker, symbol="XAUUSD", magic=MAGIC),
                       symbol="XAUUSD", magic=MAGIC, live_dir=str(live), gate=gate, rules=ExitRules())
    exe.poll(mk_decision(signal=0), NOW)
    assert not broker.positions()


@pytest.mark.parametrize("name", ["halt.json", "killSwitch"])
def test_halt_checked_before_account_assembly(tmp_path, name):
    gate, live = make_gate(tmp_path)
    (live / name).write_text('{"reason":"manual"}', encoding="utf-8")
    gate._account.positions = lambda: (_ for _ in ()).throw(AssertionError("queried"))
    assert not gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW).allowed


def test_role_heartbeats_are_not_strategy_registration(tmp_path):
    for name in ["heartbeat-risk.json", "heartbeat-reconcile.json", "heartbeat-777.json"]:
        (tmp_path / name).write_text("{}")
    assert [Path(p).name for p in heartbeat_files(str(tmp_path))] == ["heartbeat-777.json"]


def test_future_heartbeat_is_not_healthy(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text(json.dumps({"runner_alive": True, "ts": (NOW + timedelta(seconds=1)).isoformat()}))
    assert not heartbeat_status(read_heartbeat_path(str(p)), NOW).ok


def test_heartbeat_permission_error_has_bounded_retry(tmp_path, monkeypatch):
    p = tmp_path / "hb.json"
    p.write_text(json.dumps({"runner_alive": True, "ts": NOW.isoformat()}))
    real_open = builtins.open
    attempts = []
    def transient(path, *a, **k):
        if str(path) == str(p):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("sharing violation")
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", transient)
    assert read_heartbeat_path(str(p)).runner_alive
    assert len(attempts) == 3


def test_completed_risk_cycle_publishes_heartbeat(tmp_path):
    gate, live = make_gate(tmp_path)
    result = script("risk_process").run_one_cycle(gate._process, FakeGateway(gate._account))
    data = json.loads((live / "heartbeat-risk.json").read_text())
    assert data["runner_alive"] is True
    assert data["component"] == "risk"


def test_completed_weekend_reconcile_publishes_clean_health(tmp_path):
    from mt5live.reconcile import Intent, write_intent
    live = tmp_path / "live"
    live.mkdir()
    write_intent(str(live / "expected-777.json"), Intent(
        symbol="XAUUSD", magic=777, side="buy", volume=0, status="confirmed", cause="test", ts=NOW))
    gate, _ = make_gate(tmp_path)
    rc, res = script("live_reconcile").run_once(str(live), FakeGateway(gate._account), volume_tolerance=1e-6)
    assert rc == 0
    data = json.loads((live / "heartbeat-reconcile.json").read_text())
    assert data["reconcile_clean"] is True


def test_risk_unexpected_exception_is_visible_halt_and_exit(tmp_path, monkeypatch, caplog):
    gate, live = make_gate(tmp_path)
    mod = script("risk_process")
    monkeypatch.setattr(mod, "_open_gateway", lambda _: FakeGateway(gate._account))
    def denied(*a, **k):
        raise PermissionError("heartbeat access denied")
    monkeypatch.setattr(mod, "run_one_cycle", denied)
    rc = mod.main(["--config", str(tmp_path / "r.toml"), "--data-dir", str(tmp_path), "--once"])
    assert rc == 2
    marker = json.loads((live / "exit-risk.json").read_text())
    assert marker["rc"] == 2 and "PermissionError" in marker["reason"]
    assert (live / "halt.json").exists()
    assert "CRITICAL" in caplog.text and "Traceback" in caplog.text


def test_runner_data_dir_override_and_completed_idle_health(tmp_path, monkeypatch):
    mod = script("live_runner")
    from types import SimpleNamespace
    cfg = tmp_path / "live.toml"
    cfg.write_text('symbol="XAUUSD"\ntimeframe="M15"\nstrategy="tests.fakes_live:ClosingSignal"\nmagic=777\nlookback_bars=30\nvolume_lots=0.01\ndata_root="wrong"\nheartbeat_path="wrong/hb.json"\n')
    monkeypatch.setattr(mod, "SignalRunner", lambda *a, **k: SimpleNamespace(
        market_closed_skips=0, decide=lambda: None))
    monkeypatch.setenv("MT5_DEPLOYMENT_SHA", "a" * 40)
    data = tmp_path / "shared"
    rc = mod.main(["--config", str(cfg), "--once", "--offline", "--dry-run",
                   "--data-dir", str(data)])
    assert rc == 0
    hb = json.loads((data / "live" / "heartbeat-777.json").read_text())
    assert hb["pid"] > 0 and hb["deployment_sha"] == "a" * 40
    assert (data / "live" / "exit-runner-777.json").exists()


@pytest.mark.parametrize("role", ["runner", "reconcile"])
def test_other_role_top_level_fatal_evidence(tmp_path, monkeypatch, caplog, role):
    if role == "runner":
        mod = script("live_runner")
        cfg = tmp_path / "live.toml"
        cfg.write_text('symbol="XAUUSD"\ntimeframe="M15"\nstrategy="tests.fakes_live:ClosingSignal"\nmagic=777\nlookback_bars=30\nvolume_lots=0.01\n')
        def broken(*a, **k):
            raise RuntimeError("startup test failure")
        monkeypatch.setattr(mod, "load_strategy", broken)
        argv = ["--config", str(cfg), "--once", "--dry-run", "--offline", "--data-dir", str(tmp_path)]
        component = "runner-777"
    else:
        mod = script("live_reconcile")
        def broken(*a, **k):
            raise RuntimeError("startup test failure")
        monkeypatch.setattr(mod, "_open_gateway", broken)
        argv = ["--once", "--data-dir", str(tmp_path)]
        component = role
    assert mod.main(argv) == 2
    marker = json.loads((tmp_path / "live" / f"exit-{component}.json").read_text())
    assert "RuntimeError" in marker["reason"] and "Traceback" in caplog.text


def test_restart_record_explains_restart_but_does_not_erase_health_gap(tmp_path):
    mod = script("demo_check")
    from mt5live.monitor import Alert
    previous = NOW - timedelta(hours=1)
    ctx = dict(alerts=[], start_events=[
        dict(magic=777, ts=previous, log="old.log"),
        dict(magic=777, ts=NOW, log="new.log")], log_tail_text={},
        ops_restarts=[dict(utc=NOW.isoformat(), component="777", reason_code="process_missing",
                          old_pid=10, new_pid=11, deployment_sha="a" * 40)])
    assert mod.check_heartbeat_continuity(ctx, NOW.date()).ok is True
    ctx["alerts"] = [Alert(NOW + timedelta(seconds=1), "HEARTBEAT_TIMEOUT", "gap")]
    assert mod.check_heartbeat_continuity(ctx, NOW.date()).ok is False


def test_failed_runner_read_preserves_completed_heartbeat(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mt5gw.errors import DataUnavailableError
    mod = script("live_runner")
    cfg = tmp_path / "live.toml"
    cfg.write_text('symbol="XAUUSD"\ntimeframe="M15"\nstrategy="tests.fakes_live:ClosingSignal"\nmagic=777\nlookback_bars=30\nvolume_lots=0.01\n')
    live = tmp_path / "live"
    live.mkdir()
    hb = live / "heartbeat-777.json"
    hb.write_text('{"runner_alive":true,"ts":"2026-10-28T00:00:00Z"}')
    before = hb.read_bytes()
    def missing():
        raise DataUnavailableError("no snapshot")
    monkeypatch.setattr(mod, "SignalRunner", lambda *a, **k: SimpleNamespace(
        market_closed_skips=0, decide=missing))
    assert mod.main(["--config", str(cfg), "--once", "--offline", "--dry-run",
                     "--data-dir", str(tmp_path)]) == 0
    assert hb.read_bytes() == before


@pytest.mark.parametrize("future", [False, True])
def test_gate_reads_health_against_observation_clock(tmp_path, monkeypatch, future):
    import mt5live.monitor as monitor
    gate, live = make_gate(tmp_path)
    current = [NOW]
    gate._clock = lambda: current[0]
    original = monitor.read_heartbeat_path
    def read_after_progress(path):
        current[0] = NOW + timedelta(seconds=4)
        monitor.write_role_heartbeat(str(live), "risk", interval=10.0,
            now=current[0] + timedelta(seconds=1 if future else 0))
        return original(path)
    monkeypatch.setattr(monitor, "read_heartbeat_path", read_after_progress)
    verdict = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    assert verdict.allowed is (not future)
    if future:
        assert "future" in verdict.reason
    assert not (live / "halt.json").exists()


@pytest.mark.parametrize("future", [False, True])
def test_risk_snapshot_delay_does_not_turn_fresh_heartbeat_into_future(tmp_path, monkeypatch, future):
    from types import SimpleNamespace
    from mt5live.monitor import write_heartbeat
    from mt5live.riskproc import AccountSnapshot
    gate, live = make_gate(tmp_path)
    mod = script("risk_process")
    current = [NOW]
    monkeypatch.setattr(mod, "datetime", SimpleNamespace(now=lambda tz: current[0]))
    def query(gw, cutoff):
        assert cutoff == NOW  # business snapshot cutoff remains the cycle start
        current[0] += timedelta(seconds=4)
        write_heartbeat(str(live), True, ts=current[0] + timedelta(seconds=1 if future else 0))
        return AccountSnapshot("USD", (), ())
    monkeypatch.setattr(mod, "build_snapshot", query)
    res = mod.run_one_cycle(gate._process, FakeGateway(gate._account))
    assert res.heartbeat_ok is (not future)
    assert (live / "halt.json").exists() is future
