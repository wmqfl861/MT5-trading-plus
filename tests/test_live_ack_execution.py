"""Arbitration must not release the journal gate before durable intent repair.

All accounts are fakes. No terminal, credentials, orders, or market data are used.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mt5live.fileio import append_jsonl, file_lock
from mt5live.runner import instance_lock_key, runner_lock_path

DID = "XAUUSD|M15|2026-10-20T18:00:00"
REASON = "broker backend verified not sent"
MAGIC = 777
NOW = datetime(2026, 10, 20, 18, 5)


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_ack_execution.py"
    spec = importlib.util.spec_from_file_location("live_ack_execution_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mod = _module()


class Account:
    def __init__(self):
        self.identity = SimpleNamespace(server="Audit-Demo", login=123456,
                                        trade_mode=0)
        self.positions_data = []
        self.pending_data = []

    def state(self):
        return self.identity

    def positions(self):
        return self.positions_data

    def pending_orders(self):
        return self.pending_data


@pytest.fixture(autouse=True)
def isolated_locks(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "locks-home"))


@pytest.fixture
def env(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    journal = live / mod.JOURNAL_FILE_FMT.format(magic=MAGIC)
    append_jsonl(str(journal), {
        "ts": "2026-10-20T18:00:00Z", "decision_id": DID,
        "action": "open", "symbol": "XAUUSD", "status": "pending",
    })
    return live, journal, SimpleNamespace(account=Account())


def call(env, **over):
    live, _, gw = env
    kwargs = dict(symbol="XAUUSD", now=NOW)
    kwargs.update(over)
    return mod.ack(str(live), gw, MAGIC, DID, "rejected", REASON, **kwargs)


def unresolved(env, did=DID):
    live, _, _ = env
    state = mod._journal_state(str(live), MAGIC)
    assert did in state["undecided"]
    assert did not in state["finals"]


@pytest.mark.parametrize("raw", ["[]", "null", "true", "7", '"text"'])
def test_non_object_json_is_torn_not_a_crash(env, raw):
    live, journal, _ = env
    journal.write_text(raw + "\n", encoding="utf-8")
    state = mod._journal_state(str(live), MAGIC)
    assert state["undecided"] == {"torn@0": "torn"}
    assert state["finals"] == {}
    assert mod.main(["--live-dir", str(live), "--magic", str(MAGIC),
                     "--list"]) == 0


@pytest.mark.parametrize("tail", [b'{"truncated":', b'[]', b'\xff{"broken":'])
def test_unterminated_tail_preserved_and_final_is_separate(env, tail):
    live, journal, gw = env
    journal.write_bytes(tail)
    assert mod.ack(str(live), gw, MAGIC, "torn@0", "rejected", REASON,
                   symbol="XAUUSD", now=NOW) == 0
    raw = journal.read_bytes()
    assert raw[:len(tail)] == tail
    suffix = raw[len(tail):]
    assert suffix.startswith((b"\n", b"\r\n"))
    final = json.loads(suffix.lstrip(b"\r\n").splitlines()[0])
    assert final["decision_id"] == "torn@0"
    assert final["status"] == "rejected"
    state = mod._journal_state(str(live), MAGIC)
    assert state["undecided"] == {}
    assert state["finals"]["torn@0"]["status"] == "rejected"


def test_broker_position_query_failure_does_not_consume_arbitration(env, capsys):
    _, journal, gw = env
    original = journal.read_bytes()

    def unavailable():
        raise RuntimeError("terminal IPC unavailable")

    gw.account.positions = unavailable
    assert call(env) == 2
    unresolved(env)
    assert journal.read_bytes() == original
    assert "未写入 journal 终态，可重试" in capsys.readouterr().out


def test_intent_failure_keeps_unresolved_then_retry_succeeds(env, monkeypatch):
    live, journal, _ = env
    original = journal.read_bytes()
    writer = mod.write_intent

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "write_intent", fail)
    assert call(env) == 2
    unresolved(env)
    assert journal.read_bytes() == original
    assert not (live / mod.INTENT_FILE_FMT.format(magic=MAGIC)).exists()
    monkeypatch.setattr(mod, "write_intent", writer)
    assert call(env) == 0
    finals = [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines()
              if json.loads(x).get("status") in ("confirmed", "rejected")]
    assert len(finals) == 1


def test_commit_happens_after_intent_is_durable(env, monkeypatch):
    live, _, _ = env
    writer = mod.append_line

    def inspect(path, line):
        intent = live / mod.INTENT_FILE_FMT.format(magic=MAGIC)
        assert intent.is_file()
        assert json.loads(intent.read_text(encoding="utf-8"))["status"] == "confirmed"
        return writer(path, line)

    monkeypatch.setattr(mod, "append_line", inspect)
    assert call(env) == 0


def test_final_write_failure_keeps_gate_and_can_retry(env, monkeypatch):
    live, journal, _ = env
    original = journal.read_bytes()
    writer = mod.append_line

    def fail(*args, **kwargs):
        raise OSError("journal fsync failed before write")

    monkeypatch.setattr(mod, "append_line", fail)
    assert call(env) == 2
    unresolved(env)
    assert journal.read_bytes() == original
    assert (live / mod.INTENT_FILE_FMT.format(magic=MAGIC)).is_file()
    monkeypatch.setattr(mod, "append_line", writer)
    assert call(env) == 0


def test_existing_legacy_arbitration_retry_is_exact_and_does_not_append(env):
    live, journal, gw = env
    append_jsonl(str(journal), {
        "decision_id": DID, "status": "rejected", "detail": f"arbitrated: {REASON}"})
    original = journal.read_bytes()
    assert mod.ack(str(live), gw, MAGIC, DID, "confirmed", REASON,
                   symbol="XAUUSD", now=NOW) == 2
    assert mod.ack(str(live), gw, MAGIC, DID, "rejected", "changed reason",
                   symbol="XAUUSD", now=NOW) == 2
    assert journal.read_bytes() == original
    assert call(env) == 0
    assert journal.read_bytes() == original
    assert (live / mod.INTENT_FILE_FMT.format(magic=MAGIC)).is_file()


@pytest.mark.parametrize("kind", ["identity", "legacy"])
def test_running_process_or_competing_operator_lock_blocks_commit(env, kind):
    live, journal, _ = env
    path = (runner_lock_path(instance_lock_key("Audit-Demo", 123456, MAGIC))
            if kind == "identity" else str(live / f"runner-{MAGIC}.lock"))
    original = journal.read_bytes()
    with file_lock(path, attempts=1, retry_s=0):
        assert call(env) == 2
        assert journal.read_bytes() == original
    # Both locks must be released after every failure/refusal.
    assert call(env) == 0


@pytest.mark.parametrize("mode", [None, False, 1, 2])
def test_unknown_or_non_demo_account_is_rejected(env, mode):
    _, journal, gw = env
    gw.account.identity.trade_mode = mode
    original = journal.read_bytes()
    assert call(env) == 2
    assert journal.read_bytes() == original


def test_pending_order_blocks_arbitration(env):
    _, journal, gw = env
    gw.account.pending_data = [SimpleNamespace(magic=MAGIC)]
    original = journal.read_bytes()
    assert call(env) == 2
    unresolved(env)
    assert journal.read_bytes() == original


@pytest.mark.parametrize("field", ["positions_data", "pending_data"])
def test_incomplete_broker_snapshot_fails_closed(env, field):
    _, journal, gw = env
    setattr(gw.account, field, None)
    original = journal.read_bytes()
    assert call(env) == 2
    assert journal.read_bytes() == original


@pytest.mark.parametrize("volume,side", [(float("nan"), "buy"),
    (float("inf"), "buy"), (-1, "buy"), (0, "buy"), (True, "buy"), (1, "bad")])
def test_invalid_position_fails_before_final(env, volume, side):
    _, journal, gw = env
    gw.account.positions_data = [SimpleNamespace(
        magic=MAGIC, symbol="XAUUSD", volume=volume, side=side)]
    original = journal.read_bytes()
    assert call(env) == 2
    assert journal.read_bytes() == original


def test_explicit_symbol_must_match_broker_position(env):
    _, journal, gw = env
    gw.account.positions_data = [SimpleNamespace(
        magic=MAGIC, symbol="EURUSD", volume=0.02, side="buy")]
    original = journal.read_bytes()
    assert call(env) == 2
    assert journal.read_bytes() == original


def test_account_change_during_reconciliation_refuses_final(env):
    _, journal, gw = env
    original = journal.read_bytes()

    def change_account():
        gw.account.identity.login += 1
        return []

    gw.account.positions = change_account
    assert call(env) == 2
    assert journal.read_bytes() == original


def test_concurrent_journal_change_is_not_overwritten(env):
    _, journal, gw = env

    def change_journal():
        append_jsonl(str(journal), {"decision_id": "other", "status": "pending"})
        return []

    gw.account.positions = change_journal
    assert call(env) == 2
    unresolved(env)
    assert "other" in mod._journal_state(str(env[0]), MAGIC)["undecided"]


def test_missing_configuration_never_opens_default_terminal(tmp_path, monkeypatch):
    class NeverConnect:
        @staticmethod
        def from_config_file(path):
            pytest.fail("missing configuration must fail before gateway creation")

        def __init__(self, *args):
            pytest.fail("must not construct a default gateway")

    monkeypatch.setattr(mod, "MT5Gateway", NeverConnect)
    with pytest.raises(FileNotFoundError):
        mod._open_gateway(str(tmp_path / "missing-config.json"))
