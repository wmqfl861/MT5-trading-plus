"""mt5live.gate 的 T33 用例：停机文件 fail-closed（M2）与下单日志分文件（M8）。

- pause-<magic>.json / pause.json 存在但读不出（损坏/权限/缺字段）→
  按暂停处理（pause_unreadable）+ 告警——停机类信号读不出必须按停机处理；
- 下单日志每策略一个 orders-<magic>.jsonl；闸门读全部 orders-*.jsonl
  （兼容旧 orders.jsonl）；坏行保守按"此刻一笔下单"计入频率并告警，
  不再让一行坏日志阻断所有策略的开仓裁决。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from mt5quant.strategies.base import ExitRules

from mt5live.executor import ExecutionAdapter, LiveExecutor
from mt5live.gate import RiskGate

from tests.fakes_live import FakeBroker, mk_decision

MAGIC = 777
OTHER = 888                      # 另一策略（下单日志坏行的主人）
NOW = datetime(2026, 10, 28, 10, 0, 7)   # 周三 EDT 06:00 纽约，避开禁入窗

SESSIONS_TOML = """
version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 5
post_minutes = 60
exempt_symbols = ["BTCUSD"]
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
"""

RISK_TOML = """
version = 1
symbols = ["XAUUSD", "BTCUSD", "EURUSD"]
max_lots_per_order = 0.50
max_positions_per_symbol = 3
max_usd_exposure = 100000.0
max_orders_per_minute = 6
daily_loss_cap_usd = 500.0
"""


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def make_gate(tmp_path):
    """最小闸门装配（FakeBroker 无持仓；无点差模型 → 点差闸门不参与）。"""
    from mt5data.sessions import load_session_policy
    from mt5live.riskproc import RiskProcess, load_risk_config

    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    specs = _write(tmp_path / "specs" / "latest.json",
                   json.dumps({"XAUUSD": {"contract_size": 100.0}}))
    policy = load_session_policy(_write(tmp_path / "s.toml", SESSIONS_TOML))
    process = RiskProcess(load_risk_config(_write(tmp_path / "r.toml", RISK_TOML)),
                          live_dir=str(live), session_policy=policy)
    broker = FakeBroker()
    gate = RiskGate(process, account=broker, market=broker, specs_path=specs,
                    live_dir=str(live), session_policy=policy)
    return gate, live


def alerts_of(live: Path) -> str:
    p = live / "alerts.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---- M2：pause 读不出 → 暂停（pause_unreadable）+ 告警 ----

def test_corrupt_magic_pause_rejects_open_fail_closed(tmp_path):
    gate, live = make_gate(tmp_path)
    (live / f"pause-{MAGIC}.json").write_text("{ 这不是 JSON", encoding="utf-8")
    d = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    assert not d.allowed
    assert "pause_unreadable" in d.reason
    assert "PAUSE_UNREADABLE" in alerts_of(live)


def test_corrupt_global_pause_rejects_open_fail_closed(tmp_path):
    gate, live = make_gate(tmp_path)
    (live / "pause.json").write_text('{"paused": ', encoding="utf-8")  # 截断
    d = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    assert not d.allowed
    assert "pause_unreadable" in d.reason
    assert "PAUSE_UNREADABLE" in alerts_of(live)


def test_pause_missing_required_fields_is_unreadable(tmp_path):
    """合法 JSON 但缺 paused 字段 → 同样按读不出处理（不能默认未暂停）。"""
    gate, live = make_gate(tmp_path)
    (live / f"pause-{MAGIC}.json").write_text('{"reason": "一半"}',
                                              encoding="utf-8")
    d = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    assert not d.allowed
    assert "pause_unreadable" in d.reason


def test_missing_or_valid_pause_keeps_old_semantics(tmp_path):
    """回归：文件缺失 = 未暂停（可开仓）；合法 paused=true = 暂停。"""
    gate, live = make_gate(tmp_path)
    assert gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW).allowed
    (live / f"pause-{MAGIC}.json").write_text(
        json.dumps({"paused": True, "reason": "reconcile_mismatch"}),
        encoding="utf-8")
    d = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    assert not d.allowed and "reconcile_paused" in d.reason


# ---- M8：下单日志分文件 + 坏行保守计数 ----

def _order_line(ts: datetime, magic: int) -> str:
    return json.dumps({
        "ts": ts.isoformat() + "+00:00", "magic": magic, "symbol": "XAUUSD",
        "kind": "open", "side": "buy", "lots": 0.01, "decision_id": "d"})


def _write_log(path: Path, lines) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_bad_line_in_other_strategy_log_counts_one_now(tmp_path):
    """另一策略的日志有坏行：本策略闸门照常裁决（不抛错、不阻断），
    且坏行保守按"此刻一笔下单"计入频率（多计 1）+ 告警。"""
    gate, live = make_gate(tmp_path)
    log = live / f"orders-{OTHER}.jsonl"
    lines = [_order_line(NOW - timedelta(seconds=30), OTHER) for _ in range(5)]
    lines.append("<<<并发追加交错出的坏行>>>")
    _write_log(log, lines)

    d = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    # 5 笔真实在窗 + 1 坏行按此刻计 = 6，加本笔 7 > 上限 6 → 频率拒绝
    assert not d.allowed
    assert "max_orders_per_minute" in d.reason
    assert "ORDER_LOG_BAD_LINE" in alerts_of(live)

    # 对照：同一日志去掉坏行 → 5+1=6 恰在上限内 → 放行（证明多计的正是那 1）
    _write_log(log, lines[:-1])
    assert gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW).allowed


def test_gate_reads_all_magic_logs_and_legacy_file(tmp_path):
    """频率滑窗跨全部策略合并：orders-A.jsonl + orders-B.jsonl + 旧 orders.jsonl。"""
    gate, live = make_gate(tmp_path)
    _write_log(live / f"orders-{MAGIC}.jsonl",
               [_order_line(NOW - timedelta(seconds=30), MAGIC)] * 3)
    _write_log(live / f"orders-{OTHER}.jsonl",
               [_order_line(NOW - timedelta(seconds=40), OTHER)] * 2)
    _write_log(live / "orders.jsonl",
               [_order_line(NOW - timedelta(seconds=50), 999)] * 1)
    d = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    # 3+2+1=6 在窗内，加本笔 7 > 6 → 拒
    assert not d.allowed
    assert "max_orders_per_minute" in d.reason
    # 旧窗口外（>60 秒）的行不计入
    _write_log(live / "orders.jsonl",
               [_order_line(NOW - timedelta(seconds=120), 999)] * 1)
    d2 = gate.check_open("XAUUSD", "buy", 0.01, MAGIC, now=NOW)
    # 3+2=5 在窗内，加本笔 6 ≤ 6 → 放行
    assert d2.allowed


def test_executor_writes_own_magic_order_log(tmp_path):
    """执行器只写自己的 orders-<magic>.jsonl；不再写共用 orders.jsonl。"""
    b = FakeBroker()
    adapter = ExecutionAdapter(b, b, b, symbol=b.symbol, magic=MAGIC)
    ex = LiveExecutor(
        adapter, symbol=b.symbol, magic=MAGIC, live_dir=str(tmp_path),
        rules=ExitRules(stop_loss_atr=1.5))
    ex.poll(mk_decision(signal=1, volume_lots=0.02), NOW)

    log = tmp_path / f"orders-{MAGIC}.jsonl"
    assert log.exists()
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["magic"] == MAGIC and rows[0]["kind"] == "open"
    assert not (tmp_path / "orders.jsonl").exists()


def test_two_magics_write_two_separate_logs(tmp_path):
    b = FakeBroker()
    for magic in (MAGIC, OTHER):
        adapter = ExecutionAdapter(b, b, b, symbol=b.symbol, magic=magic)
        ex = LiveExecutor(
            adapter, symbol=b.symbol, magic=magic, live_dir=str(tmp_path),
            rules=ExitRules(stop_loss_atr=1.5))
        ex.poll(mk_decision(signal=1, volume_lots=0.02, decision_id=f"d{magic}"),
                NOW)
    assert (tmp_path / f"orders-{MAGIC}.jsonl").exists()
    assert (tmp_path / f"orders-{OTHER}.jsonl").exists()
