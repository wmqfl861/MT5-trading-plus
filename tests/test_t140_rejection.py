"""T140（第十六轮 O16-2/O16-7；计划 docs/handoff/2026-11-08-round16.md §2）：
确定态拒单不退出。

- mt5gw.errors.RETCode 补充确定态码（10013/10020/10021/10024 等，中文描述）
  并更正 10031：MQL5 中是 TRADE_RETCODE_CONNECTION（与交易服务器无连接），
  不是"客户端禁止此类交易"（O16-7；断线的处理逻辑属 T141，不在本测试）。
- 执行器对 TradeRejectedError：journal 记 rejected + 写 confirmed 意图
  （保持现状）→ 返回 RoundReport(action="rejected") 不再重抛 → 告警
  ORDER_REJECTED（含 retcode）→ 本 bar 内不再重试。
- 同一 magic 连续 3 个 bar 被拒 → 写 pause-<magic>.json
  （reason=order_rejected_streak），只暂停该策略，人工确认后解除。
- OrderResultUnknownError（状态未知）保持退出（交给重启核对）。
- live_runner 主循环：executor.poll 的 TradeRejectedError 路径不再整进程
  退出（心跳继续，下一 bar 正常决策）。

11-05 14:22 事故（'10021 No prices' 致运行器退出数小时）的回归钉。
"""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mt5gw.errors import (
    RETCode,
    OrderResultUnknownError,
    TradeRejectedError,
    describe_retcode,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MAGIC = 777
OTHER_MAGIC = 778
NOW = datetime(2026, 10, 20, 18, 0)


# ---------------------------------------------------------------- 码表（O16-7）

def test_t140_retcode_definite_reject_codes_present():
    """10013/10020/10021/10024 等确定态码补入 RETCode，且中文描述可读
    （describe 不落入"未知返回码"）。"""
    assert RETCode.INVALID_REQUEST == 10013
    assert RETCode.PRICE_CHANGED == 10020
    assert RETCode.PRICE_OFF == 10021
    assert RETCode.TOO_MANY_REQUESTS == 10024
    for code in (10013, 10020, 10021, 10024):
        text = describe_retcode(code)
        assert text and "未知" not in text
    # 10021 事故文案：11-05 的 'No prices' 必须能按可读中文落日志/告警
    assert "报价" in describe_retcode(10021)


def test_t140_retcode_10031_is_trade_retcode_connection():
    """O16-7 更正：10031 = TRADE_RETCODE_CONNECTION（与交易服务器无连接），
    按断线语义归类；10027 才是终端禁自动交易。误导性的 CLIENT_DISABLED
    常量名必须移除（断线处理逻辑属 T141）。"""
    assert RETCode.NO_CONNECTION == 10031
    assert "无连接" in describe_retcode(10031)
    assert "客户端禁止" not in describe_retcode(10031)
    assert RETCode.AUTO_TRADING_DISABLED == 10027     # 10027 语义不变
    assert not hasattr(RETCode, "CLIENT_DISABLED")    # 旧误标常量已移除
    # 按断线归类（T141 铺垫：本次只改描述与分类，不改处理逻辑）
    from mt5gw.errors import DISCONNECTION_RETCODES
    assert 10031 in DISCONNECTION_RETCODES


# ------------------------------------------------------- 执行器（LiveExecutor）

def _mk_executor(broker, tmp_path, *, magic: int = MAGIC):
    from mt5quant.strategies.base import ExitRules
    from mt5live.executor import ExecutionAdapter, LiveExecutor

    return LiveExecutor(
        ExecutionAdapter(broker, broker, broker,
                         symbol=broker.symbol, magic=magic),
        symbol=broker.symbol, magic=magic, live_dir=str(tmp_path),
        rules=ExitRules(stop_loss_atr=1.5))


def _mk_decision(bar: str, **kw):
    from tests.fakes_live import mk_decision

    return mk_decision(decision_id=f"XAUUSD|M15|{bar}", **kw)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _alerts_text(live_dir) -> str:
    path = os.path.join(str(live_dir), "alerts.log")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _journal_lines(live_dir):
    path = os.path.join(str(live_dir), f"execution-journal-{MAGIC}.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f.read().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_t140_rejection_returns_round_report_not_raise(tmp_path):
    """假网关返回 10021：poll 不再重抛——返回 action="rejected" 回报
    （含 retcode）；journal 记 rejected（现状保持）；意图 confirmed(实际
    净持仓)（现状保持）；写告警 ORDER_REJECTED（含 retcode）。"""
    from tests.fakes_live import FakeBroker

    b = FakeBroker()
    b.error_on_market_order = TradeRejectedError(10021, "No prices")
    ex = _mk_executor(b, tmp_path)
    d = _mk_decision("2026-10-20T18:00:00", signal=1, volume_lots=0.02)
    rr = ex.poll(d, NOW)                     # 不再 pytest.raises
    assert rr.action == "rejected"
    assert rr.decision_id == d.decision_id
    assert "10021" in (rr.error or "")
    # journal：确定态拒绝落终态 rejected（含 retcode）
    lines = _journal_lines(tmp_path)
    rejected = [r for r in lines if r.get("status") == "rejected"]
    assert rejected and rejected[-1]["retcode"] == 10021
    assert rejected[-1]["decision_id"] == d.decision_id
    # 意图：confirmed（实际净持仓 = 空仓 0）
    intent = _read_json(tmp_path / f"expected-{MAGIC}.json")
    assert intent["status"] == "confirmed"
    assert intent["volume"] == 0.0
    assert intent["cause"] == d.decision_id
    # 告警 ORDER_REJECTED 含 retcode
    alerts = _alerts_text(tmp_path)
    assert "ORDER_REJECTED" in alerts and "10021" in alerts


def test_t140_rejected_no_retry_within_bar(tmp_path):
    """被拒的 decision 本 bar 内不再重试：同 decision 再投 → 零新增请求
    （journal 终态幂等拦住，回报 duplicate）。"""
    from tests.fakes_live import FakeBroker

    b = FakeBroker()
    b.error_on_market_order = TradeRejectedError(10021, "No prices")
    ex = _mk_executor(b, tmp_path)
    d = _mk_decision("2026-10-20T18:00:00", signal=1, volume_lots=0.02)
    assert ex.poll(d, NOW).action == "rejected"
    assert len(b.market_orders) == 1         # 只发出过一次请求
    rr2 = ex.poll(d, NOW + timedelta(seconds=30))   # 同 bar 重投（重放）
    assert rr2.action == "duplicate"
    assert len(b.market_orders) == 1         # 零新增请求


def test_t140_three_consecutive_rejected_bars_pause_this_magic(tmp_path):
    """连续 3 个 bar 被拒 → 写 pause-<magic>.json（paused=true、
    reason=order_rejected_streak），只暂停该 magic（闸门读得到、别的
    magic 不受影响），人工确认后解除。"""
    from tests.fakes_live import FakeBroker
    from mt5live.gate import read_pause

    b = FakeBroker()
    b.error_on_market_order = TradeRejectedError(10021, "No prices")
    ex = _mk_executor(b, tmp_path)
    for i, bar in enumerate(("18:00:00", "18:15:00", "18:30:00")):
        rr = ex.poll(_mk_decision(f"2026-10-20T{bar}",
                                  signal=1, volume_lots=0.02),
                     NOW + timedelta(minutes=15 * i))
        assert rr.action == "rejected"
    pause_path = tmp_path / f"pause-{MAGIC}.json"
    assert pause_path.exists()
    doc = _read_json(pause_path)
    assert doc["paused"] is True
    assert doc["reason"] == "order_rejected_streak"
    # 只暂停该策略：闸门读本 magic 暂停、其他 magic 不受影响
    assert read_pause(str(tmp_path), MAGIC).paused is True
    assert read_pause(str(tmp_path), OTHER_MAGIC).paused is False
    # 告警留痕（第 3 次连拒写暂停）
    alerts = _alerts_text(tmp_path)
    assert "order_rejected_streak" in alerts
    # 人工解除 = 删除文件（read_pause 回到未暂停）
    os.remove(pause_path)
    assert read_pause(str(tmp_path), MAGIC).paused is False


def test_t140_rejected_streak_reset_by_successful_bar(tmp_path):
    """连拒计数按"连续"计：中间隔一个成功下单的 bar → 重新数起，
    2+2 次被拒不写 pause。"""
    from tests.fakes_live import FakeBroker

    b = FakeBroker()
    ex = _mk_executor(b, tmp_path)
    # 两个 bar 被拒（开仓腿 10021）
    for i, bar in enumerate(("18:00:00", "18:15:00")):
        b.error_on_market_order = TradeRejectedError(10021, "No prices")
        rr = ex.poll(_mk_decision(f"2026-10-20T{bar}",
                                  signal=1, volume_lots=0.02),
                     NOW + timedelta(minutes=15 * i))
        assert rr.action == "rejected"
    # 一个成功的 bar：开多仓成交（连拒计数中断归零）
    b.error_on_market_order = None
    rr = ex.poll(_mk_decision("2026-10-20T18:30:00",
                              signal=1, volume_lots=0.02),
                 NOW + timedelta(minutes=30))
    assert rr.action == "open"
    # 再两个 bar 被拒（反向开仓腿连续被拒数只有 2，不写 pause）
    b.error_on_market_order = TradeRejectedError(10021, "No prices")
    rr = ex.poll(_mk_decision("2026-10-20T18:45:00",
                              signal=-1, volume_lots=0.02),
                 NOW + timedelta(minutes=45))
    assert rr.action == "rejected"            # 平仓腿成交、反向开仓腿被拒
    rr = ex.poll(_mk_decision("2026-10-20T19:00:00",
                              signal=-1, volume_lots=0.02),
                 NOW + timedelta(minutes=60))
    assert rr.action == "rejected"
    assert not (tmp_path / f"pause-{MAGIC}.json").exists()


def test_t140_unknown_result_still_raises(tmp_path):
    """状态未知（OrderResultUnknownError）保持上抛退出（交给重启核对）
    ——测试钉死，不许被"不退出"改造波及。"""
    from tests.fakes_live import FakeBroker

    b = FakeBroker()
    b.error_on_market_order = OrderResultUnknownError("IPC 中断")
    ex = _mk_executor(b, tmp_path)
    d = _mk_decision("2026-10-20T18:00:00", signal=1, volume_lots=0.02)
    with pytest.raises(OrderResultUnknownError):
        ex.poll(d, NOW)
    # journal 记 unknown（重启核对裁决）
    lines = _journal_lines(tmp_path)
    assert any(r.get("status") == "unknown" for r in lines)


# --------------------------------------------------- live_runner 主循环（TDD）

RISK_TOML = """
version = 1
symbols = ["XAUUSD"]
max_lots_per_order = 0.50
max_positions_per_symbol = 3
max_usd_exposure = 100000.0
max_orders_per_minute = 6
daily_loss_cap_usd = 500.0
"""

SESSIONS_TOML = """
version = 1
[rollover]
local_time = "17:00"
tz = "America/New_York"
[forbidden]
pre_minutes = 0
post_minutes = 0
exempt_symbols = ["BTCUSD"]
[costs]
spread_gate_multiplier = 3.0
missing_hour_policy = "forbid"
"""


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _real_mode_env(tmp_path) -> dict:
    """实跑模式（非 dry-run/非 paper）的环境：specs/风控/会话/live.toml。"""
    _write(tmp_path / "data" / "specs" / "latest.json", json.dumps({
        "XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                   "volume_step": 0.01, "volume_max": 100.0}}))
    return {
        "risk": _write(tmp_path / "risk.toml", RISK_TOML),
        "sessions": _write(tmp_path / "sessions.toml", SESSIONS_TOML),
        "config": _write(tmp_path / "live.toml", f"""
symbol = "XAUUSD"
timeframe = "M15"
strategy = "mt5quant.strategies.library.buy_hold:BuyHold"
magic = {MAGIC}
lookback_bars = 30
volume_lots = 0.02
data_root = "{(tmp_path / 'data').as_posix()}"
trigger_delay_s = 5.0
"""),
        "live_dir": str(tmp_path / "data" / "live"),
    }


def _load_live_runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "live_runner_script_t140", REPO_ROOT / "scripts" / "live_runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wire_gateway(monkeypatch, mod, broker, now):
    class _FakeGW:
        market = broker
        account = broker
        trading = broker

        def connect(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr("mt5gw.MT5Gateway.from_config_file",
                        staticmethod(lambda p: _FakeGW()))
    monkeypatch.setattr(mod, "now_utc", lambda: now)


def _exit_doc(live_dir) -> dict:
    path = os.path.join(live_dir, "exit-runner-777.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_t140_runner_survives_10021_and_decides_next_bar(tmp_path, monkeypatch,
                                                         capsys):
    """假网关返回 10021 → 进程不退出（rc=0，经 KeyboardInterrupt 正常收尾），
    心跳继续（两轮 loop_completed、心跳文件最终无错），下一 bar 正常决策
    并成交。"""
    from mt5live.monitor import write_role_heartbeat
    from tests.fakes_live import FakeBroker, bars_from_df, boundary_open, \
        grid_bars

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lockshome"))
    mod = _load_live_runner()
    t1 = datetime(2026, 10, 28, 10, 0, 7)
    t2 = t1 + timedelta(minutes=15)
    clock = {"now": t1}
    broker = FakeBroker("XAUUSD")
    broker.set_bars("XAUUSD", "M15",
                    bars_from_df(grid_bars(boundary_open(t1), last="up")))
    broker.quote_time = t1
    broker.error_on_market_order = TradeRejectedError(10021, "No prices")
    _wire_gateway(monkeypatch, mod, broker, clock["now"])

    def mutable_now():
        return clock["now"]

    monkeypatch.setattr(mod, "now_utc", mutable_now)
    env = _real_mode_env(tmp_path)
    write_role_heartbeat(env["live_dir"], "risk", interval=10.0,
                         now=clock["now"])

    real_sleep = time.sleep

    def advance_or_exit(s):
        clock["now"] = t2                      # 下一根 bar 的决策墙钟
        broker.error_on_market_order = None    # 下一 bar 恢复正常成交
        broker.set_bars("XAUUSD", "M15",
                        bars_from_df(grid_bars(boundary_open(t2), last="up")))
        broker.quote_time = t2
        # 风控心跳按新墙钟重写（闸门按注入时钟判活，冻结时钟下会显得陈旧）
        write_role_heartbeat(env["live_dir"], "risk", interval=10.0,
                             now=clock["now"])
        monkeypatch.setattr(mod, "now_utc", mutable_now)  # 保持可变时钟
        # 再睡一轮后经中断退出主循环（第 2 轮已完整执行）
        def exit_next(s2):
            raise KeyboardInterrupt
        monkeypatch.setattr(mod.time, "sleep", exit_next)
        real_sleep(0)

    monkeypatch.setattr(mod.time, "sleep", advance_or_exit)
    gw_cfg = _write(tmp_path / "gw.json", "{}")
    rc = mod.main(["--config", env["config"], "--loop", "--no-spread-gate",
                   "--risk-config", env["risk"],
                   "--sessions-config", env["sessions"],
                   "--gateway-config", gw_cfg,
                   "--poll-interval", "0.1"])
    assert rc == 0                              # 不再因拒单退出
    out = capsys.readouterr().out
    assert out.count("loop_completed") >= 2     # 两轮完整收尾（心跳继续）
    assert "rejected" in out                    # 拒单轮如实留痕
    # 下一 bar 正常决策并成交：第一次被拒的请求 + 第二次成功
    assert len(broker.market_orders) == 2
    assert len(broker.positions()) == 1
    hb = _read_json(os.path.join(env["live_dir"], f"heartbeat-{MAGIC}.json"))
    assert hb["last_decision"] is not None
    assert hb["last_error"] is None             # 成功轮覆盖，无残留错误
    # 经正常收尾（completed=循环内中断返回 0 / interrupted=guarded 捕获），
    # 而不是 guarded fatal（fatal 时 reason 会是 "TradeRejectedError: ..."）
    assert _exit_doc(env["live_dir"]).get("reason") in ("completed",
                                                        "interrupted")
    assert not os.path.exists(os.path.join(env["live_dir"], "halt.json"))


def test_t140_runner_exits_on_unknown_result(tmp_path, monkeypatch, capsys):
    """状态未知 → 仍然退出（guarded fatal，rc=2）——钉死。"""
    from mt5live.monitor import write_role_heartbeat
    from tests.fakes_live import FakeBroker, bars_from_df, boundary_open, \
        grid_bars

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lockshome"))
    mod = _load_live_runner()
    now = datetime(2026, 10, 28, 10, 0, 7)
    broker = FakeBroker("XAUUSD")
    broker.set_bars("XAUUSD", "M15",
                    bars_from_df(grid_bars(boundary_open(now), last="up")))
    broker.quote_time = now
    broker.error_on_market_order = OrderResultUnknownError("IPC 中断")
    _wire_gateway(monkeypatch, mod, broker, now)
    env = _real_mode_env(tmp_path)
    write_role_heartbeat(env["live_dir"], "risk", interval=10.0, now=now)
    gw_cfg = _write(tmp_path / "gw.json", "{}")
    rc = mod.main(["--config", env["config"], "--once", "--no-spread-gate",
                   "--risk-config", env["risk"],
                   "--sessions-config", env["sessions"],
                   "--gateway-config", gw_cfg])
    assert rc == 2                              # 退出（交给重启核对）
    assert "OrderResultUnknownError" in _exit_doc(env["live_dir"]).get(
        "reason", "")
