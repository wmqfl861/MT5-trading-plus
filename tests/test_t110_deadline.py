"""T110 deadlines: every raw MT5 entry and lock wait must fail terminally."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mt5gw.config import GatewayConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("field", ["query_timeout_s", "order_timeout_s"])
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "30"])
def test_deadlines_reject_unsafe_config(field, value):
    with pytest.raises(ValueError, match=field):
        GatewayConfig(**{field: value})


@pytest.mark.parametrize("blocked", ["initialize", "terminal_info", "account_info",
                                     "last_error", "shutdown", "query", "order_send",
                                     "lock"])
def test_blocked_mt5_exits_75_with_durable_evidence(tmp_path, blocked):
    source = r'''
import os, sys, threading
from pathlib import Path
from types import SimpleNamespace
from mt5gw.config import GatewayConfig
from mt5gw.connection import MT5Connection
blocked = sys.argv[1]
def stuck(*a, **k):
    threading.Event().wait()
class Fake:
    def initialize(self, *a, **k): return True  # T201：initialize 带 MT5 timeout 关键字
    def terminal_info(self): return SimpleNamespace(connected=True)
    def account_info(self): return SimpleNamespace(trade_mode=0, login=123)
    def last_error(self): return (1, 'Success')
    def shutdown(self): pass
    def query(self): return 1
    def order_send(self, request): return 1
fake = Fake()
if blocked != 'lock':
    stuck.__name__ = blocked
    setattr(fake, blocked, stuck)
conn = MT5Connection(GatewayConfig(query_timeout_s=0.15, order_timeout_s=0.2), fake)
if blocked == 'initialize': conn.connect()
elif blocked == 'last_error': conn.last_error()
elif blocked == 'shutdown':
    conn.connect()
    conn.shutdown()
elif blocked in ('order_send', 'account_info'):
    conn.trade_send({}, lambda mode: None)
elif blocked == 'lock':
    acquired = threading.Event()
    def own():
        with conn._lock:
            acquired.set()
            stuck()
    threading.Thread(target=own, daemon=True).start()
    acquired.wait()
    conn.call(fake.query)
else: conn.call(fake.query)
raise AssertionError('continued after stall')
'''
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
               MT5_LIVE_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-u", "-c", source, blocked],
                          cwd=ROOT, env=env, capture_output=True, timeout=10)
    assert proc.returncode == 75, proc.stderr.decode(errors="replace")
    marker = json.loads((tmp_path / "gateway-stall.json").read_text())
    assert marker["rc"] == 75
    assert marker["duration_s"] >= 0.1
    assert blocked in marker["call"]
    assert b"CRITICAL" in proc.stderr


def test_order_deadline_persists_executor_unknown_before_hard_exit(tmp_path):
    source = r'''
import sys, threading
from datetime import datetime
from types import SimpleNamespace
from mt5gw.config import GatewayConfig
from mt5gw.connection import MT5Connection
from mt5live.executor import LiveExecutor, ExecutionAdapter
from mt5quant.strategies.base import ExitRules
from tests.fakes_live import FakeBroker, mk_decision
class Fake:
    def initialize(self, *a, **k): return True  # T201：initialize 带 MT5 timeout 关键字
    def terminal_info(self): return SimpleNamespace(connected=True)
    def account_info(self): return SimpleNamespace(trade_mode=0, login=123)
    def last_error(self): return (1, 'Success')
    def order_send(self, request): threading.Event().wait()
conn = MT5Connection(GatewayConfig(query_timeout_s=0.2, order_timeout_s=0.15), Fake())
class Broker(FakeBroker):
    def market_order(self, *args, **kwargs):
        conn.trade_send({}, lambda mode: None)
broker = Broker()
exe = LiveExecutor(ExecutionAdapter(broker, broker, broker, symbol='XAUUSD', magic=777),
                   symbol='XAUUSD', magic=777, live_dir=sys.argv[1], rules=ExitRules())
exe.poll(mk_decision(signal=1), datetime(2026, 10, 28, 10, 0, 7))
'''
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
               MT5_LIVE_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-u", "-c", source, str(tmp_path)],
                          cwd=ROOT, env=env, capture_output=True, timeout=10)
    assert proc.returncode == 75, proc.stderr.decode(errors="replace")
    journal = list(tmp_path.glob("execution-*.jsonl"))
    assert len(journal) == 1
    rows = [json.loads(s) for s in journal[0].read_text().splitlines()]
    assert [r["status"] for r in rows] == ["pending", "unknown"]
    assert "deadline" in rows[-1]["detail"]
    assert json.loads((tmp_path / "expected-777.json").read_text())["status"] == "pending"


@pytest.mark.parametrize("trigger", ["lock_query", "other_connection"])
def test_query_timeout_records_another_threads_inflight_order(tmp_path, trigger):
    source = r'''
import sys, threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from mt5gw.config import GatewayConfig
from mt5gw.connection import MT5Connection, timeout_evidence
from mt5live.executor import LiveExecutor, ExecutionAdapter
from mt5quant.strategies.base import ExitRules
from tests.fakes_live import FakeBroker, mk_decision
entered = threading.Event()
class Fake:
    def initialize(self, *a, **k): return True  # T201：initialize 带 MT5 timeout 关键字
    def terminal_info(self): return SimpleNamespace(connected=True)
    def account_info(self): return SimpleNamespace(trade_mode=0, login=123)
    def last_error(self): return (1, 'Success')
    def query(self): threading.Event().wait()
    def order_send(self, request):
        entered.set()
        threading.Event().wait()
fake = Fake()
conn = MT5Connection(GatewayConfig(query_timeout_s=0.15, order_timeout_s=4.0), fake)
class Broker(FakeBroker):
    def market_order(self, *args, **kwargs):
        conn.trade_send({}, lambda mode: None)
broker = Broker()
exe = LiveExecutor(ExecutionAdapter(broker, broker, broker, symbol='XAUUSD', magic=777),
                   symbol='XAUUSD', magic=777, live_dir=sys.argv[1], rules=ExitRules())
# A finished context must not survive into an unrelated terminal failure.
with timeout_evidence(lambda record: Path(sys.argv[1], 'finished-callback').touch()):
    pass
order = threading.Thread(target=lambda: exe.poll(mk_decision(signal=1), datetime(2026,10,28,10)))
order.start()
assert entered.wait(3), 'order_send not entered'
if sys.argv[2] == 'other_connection':
    conn = MT5Connection(GatewayConfig(query_timeout_s=0.15, order_timeout_s=4.0), Fake())
# This thread has no journal ContextVar. Its shorter query deadline fires first.
conn.call(fake.query)
raise AssertionError('continued after query deadline')
'''
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
               MT5_LIVE_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-u", "-c", source, str(tmp_path), trigger],
                          cwd=ROOT, env=env, capture_output=True, timeout=12)
    assert proc.returncode == 75, proc.stderr.decode(errors="replace")
    marker = json.loads((tmp_path / "gateway-stall.json").read_text())
    assert marker["call"] == ("lock:query" if trigger == "lock_query" else "query")
    rows = [json.loads(s) for s in (tmp_path / "execution-journal-777.jsonl").read_text().splitlines()]
    assert [r["status"] for r in rows] == ["pending", "unknown"]
    assert "deadline" in rows[-1]["detail"]
    assert json.loads((tmp_path / "expected-777.json").read_text())["status"] == "pending"
    assert not (tmp_path / "finished-callback").exists()
