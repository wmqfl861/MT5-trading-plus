"""Manual execution arbitration must remain recoverable across file failures."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from mt5live.fileio import append_jsonl
from tests.fakes_live import FakeBroker


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_ack_execution.py"
    spec = importlib.util.spec_from_file_location("live_ack_execution_test", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _seed_pending(live_dir: Path, magic: int, decision_id: str) -> Path:
    live_dir.mkdir(parents=True, exist_ok=True)
    path = live_dir / mod.JOURNAL_FILE_FMT.format(magic=magic)
    append_jsonl(str(path), {
        "ts": "2026-10-20T18:00:00Z",
        "decision_id": decision_id,
        "action": "open",
        "status": "pending",
    })
    return path


mod = _module()


def test_broker_position_query_failure_does_not_consume_arbitration(tmp_path, capsys):
    live_dir = tmp_path / "live"
    decision_id = "XAUUSD|M15|2026-10-20T18:00:00"
    journal = _seed_pending(live_dir, 777, decision_id)

    class Unavailable:
        def positions(self):
            raise RuntimeError("terminal IPC unavailable")

    rc = mod.ack(
        str(live_dir), SimpleNamespace(account=Unavailable()), 777,
        decision_id, "rejected", "broker backend verified not sent",
        symbol="XAUUSD", now=datetime(2026, 10, 20, 18, 5),
    )

    assert rc == 2
    state = mod._journal_state(str(live_dir), 777)
    assert decision_id in state["undecided"]
    assert decision_id not in state["finals"]
    assert "未写入 journal 终态，可重试" in capsys.readouterr().out
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 1


def test_same_arbitration_can_retry_after_intent_write_failure(
        tmp_path, monkeypatch):
    live_dir = tmp_path / "live"
    decision_id = "XAUUSD|M15|2026-10-20T18:00:00"
    journal = _seed_pending(live_dir, 777, decision_id)
    broker = FakeBroker()
    gw = SimpleNamespace(account=broker)
    original_write_intent = mod.write_intent
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated disk-full during intent write")
        return original_write_intent(*args, **kwargs)

    monkeypatch.setattr(mod, "write_intent", fail_once)
    now = datetime(2026, 10, 20, 18, 5)
    first = mod.ack(
        str(live_dir), gw, 777, decision_id, "rejected",
        "broker backend verified not sent", symbol="XAUUSD", now=now,
    )
    assert first == 2
    state = mod._journal_state(str(live_dir), 777)
    assert state["finals"][decision_id]["status"] == "rejected"
    assert not (live_dir / mod.INTENT_FILE_FMT.format(magic=777)).exists()

    # Same status with a changed reason is not the same arbitration and must
    # not silently reuse the existing terminal record.
    conflict = mod.ack(
        str(live_dir), gw, 777, decision_id, "rejected",
        "different reason", symbol="XAUUSD", now=now,
    )
    assert conflict == 2
    assert calls["n"] == 1

    # Same conclusion and same reason may repair the intent; no second final.
    second = mod.ack(
        str(live_dir), gw, 777, decision_id, "rejected",
        "broker backend verified not sent", symbol="XAUUSD", now=now,
    )
    assert second == 0
    assert calls["n"] == 2
    lines = [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines()]
    finals = [x for x in lines if x.get("status") in ("confirmed", "rejected")]
    assert len(finals) == 1
    intent_path = live_dir / mod.INTENT_FILE_FMT.format(magic=777)
    assert intent_path.is_file()
    assert "ACK_EXECUTION" in (live_dir / "alerts.log").read_text(encoding="utf-8")
