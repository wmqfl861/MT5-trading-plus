"""SignalRunner：收盘触发网格、只读已收盘 bar、窗口=lookback、SL/TP 与心跳。"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from mt5live.config import LiveConfig
from mt5live.runner import (Decision, SignalRunner, default_heartbeat_path,
                            write_heartbeat)
from mt5quant.backtest.engine_a import atr_series
from mt5quant.strategies.base import BaseStrategy
from mt5quant.strategies.library.buy_hold import BuyHold

from tests.fakes_live import (LastBarDirection, const_range_bars, mk_bars)

TF = "M15"          # 900 秒
DELAY = 5.0
# 40 根 M15，08:00 起 → 末根 17:45，其收盘时刻 18:00
NOW_DUE = datetime(2026, 10, 20, 18, 0, 7)      # 收盘后 7s ≥ delay
NOW_EARLY = datetime(2026, 10, 20, 18, 0, 3)    # 收盘后 3s < delay


def mk_config(**kw):
    base = dict(symbol="XAUUSD", timeframe=TF, strategy="x:X", params={},
                magic=777, lookback_bars=30, volume_lots=0.1,
                trigger_delay_s=DELAY)
    base.update(kw)
    return LiveConfig(**base)


def mk_runner(strategy, bars, *, quote=(100.0, 100.2), **cfg_kw):
    return SignalRunner(
        mk_config(**cfg_kw), strategy,
        bars_loader=lambda root, symbol, tf: bars,
        quote_fn=(None if quote is None else (lambda s: quote)))


BARS_UP = const_range_bars(40, 900, last_up=True)     # 末根阳线 → +1
BARS_DOWN = const_range_bars(40, 900, last_up=False)  # 末根阴线 → -1


# ---- 触发时机 ----

def test_no_decision_before_delay_elapses():
    r = mk_runner(LastBarDirection(), BARS_UP)
    assert r.decide(NOW_EARLY) is None


def test_decision_after_delay():
    r = mk_runner(LastBarDirection(), BARS_UP)
    d = r.decide(NOW_DUE)
    assert isinstance(d, Decision)
    assert d.bar_time == datetime(2026, 10, 20, 17, 45)
    assert d.bar_close_time == datetime(2026, 10, 20, 18, 0)


def test_midbar_forming_bar_is_never_used():
    # 库里混进了 18:00 起的未收盘 bar（收盘 18:15 > 墙钟）→ 必须被剔除，
    # 信号仍取 17:45 那根已收盘 bar
    strategy = LastBarDirection()
    forming = mk_bars([("2026-10-20 18:00", 100.0, 101.5, 98.5, 99.0)])
    bars = pd.concat([BARS_UP, forming], ignore_index=True)
    r = mk_runner(strategy, bars)
    d = r.decide(NOW_DUE)
    assert d.bar_time == datetime(2026, 10, 20, 17, 45)
    assert strategy.calls[0]["last_time"] == pd.Timestamp("2026-10-20 17:45")


def test_same_bar_second_call_returns_none():
    r = mk_runner(LastBarDirection(), BARS_UP)
    assert r.decide(NOW_DUE) is not None
    later = NOW_DUE + timedelta(minutes=14)   # 仍在同一网格内
    assert r.decide(later) is None


def test_store_lag_behind_grid_raises():
    stale = BARS_UP.iloc[:-1]                 # 末根 17:45 缺失 → 滞后
    r = mk_runner(LastBarDirection(), stale)
    with pytest.raises(ValueError, match="滞后"):
        r.decide(NOW_DUE)


def test_no_bars_raises():
    r = mk_runner(LastBarDirection(), None)
    with pytest.raises(ValueError, match="无可用"):
        r.decide(NOW_DUE)


def test_short_window_raises():
    r = mk_runner(LastBarDirection(), BARS_UP.iloc[:20], lookback_bars=30)
    # 墙钟对齐被截断的序列：末根 12:45（收盘 13:00），窗口 20 < 30 → 不足
    with pytest.raises(ValueError, match="不足"):
        r.decide(datetime(2026, 10, 20, 13, 0, 7))


# ---- 计算窗口 = max(3×lookback, 1000)，可配置；lookback 只作下限（T25/M3）----

def test_window_default_is_compute_window_not_lookback():
    """缺省计算窗口 = max(3×lookback_bars, 1000)（T25/M3：窗口不变性问题上
    实盘侧的兜底——窗口远大于声明 lookback）；不足时喂全部已收盘 bar。"""
    strategy = LastBarDirection()
    r = mk_runner(strategy, BARS_UP, lookback_bars=30)
    assert r.compute_window_bars == max(3 * 30, 1000) == 1000
    d = r.decide(NOW_DUE)
    # 40 根已收盘 < 1000 → 喂全部 40 根（≥ lookback 下限即可）
    assert strategy.calls[0]["n"] == 40
    assert strategy.calls[0]["first_time"] == pd.Timestamp("2026-10-20 08:00")
    assert d.bars_used == 40


def test_compute_window_clips_to_available_bars():
    bars = const_range_bars(1300, 900, last_up=True)
    strategy = LastBarDirection()
    r = mk_runner(strategy, bars, lookback_bars=400)
    # 3×400 = 1200 > 1000 → 1200；1300 根可用 → 喂尾 1200 根
    assert r.compute_window_bars == 1200
    # 直接对齐末根收盘：末根开盘 = 08:00 + 1299×15m，收盘再 +15m
    last_open = pd.Timestamp("2026-10-20 08:00") + pd.Timedelta(minutes=15 * 1299)
    close = last_open + pd.Timedelta(minutes=15)
    d = r.decide(close.to_pydatetime() + timedelta(seconds=7))
    assert strategy.calls[0]["n"] == 1200
    assert strategy.calls[0]["first_time"] == last_open - pd.Timedelta(minutes=15 * 1199)
    assert d.bars_used == 1200


def test_compute_window_bars_configurable():
    strategy = LastBarDirection()
    bars = const_range_bars(60, 900, last_up=True)
    r = SignalRunner(mk_config(lookback_bars=30), strategy,
                     bars_loader=lambda root, s, tf: bars,
                     quote_fn=lambda s: (100.0, 100.2),
                     compute_window_bars=50)
    assert r.compute_window_bars == 50
    last_open = pd.Timestamp("2026-10-20 08:00") + pd.Timedelta(minutes=15 * 59)
    close = last_open + pd.Timedelta(minutes=15)
    d = r.decide(close.to_pydatetime() + timedelta(seconds=7))
    assert strategy.calls[0]["n"] == 50
    assert d.bars_used == 50


def test_compute_window_bars_below_lookback_rejected():
    with pytest.raises(ValueError, match="compute_window_bars"):
        SignalRunner(mk_config(lookback_bars=30), LastBarDirection(),
                     bars_loader=lambda root, s, tf: BARS_UP,
                     compute_window_bars=20)


def test_required_lookback_still_lower_bound_for_config():
    """required_data.lookback_bars 只作下限：config.lookback_bars ≥ 声明值即可，
    计算窗口另按 max(3×lookback, 1000) 取（不再等于 lookback）。"""
    strategy = LastBarDirection()
    strategy.required_data = type(strategy.required_data)(
        symbols=(), timeframe="", lookback_bars=30)
    r = mk_runner(strategy, BARS_UP, lookback_bars=30)   # 恰等于声明值：允许
    assert r.compute_window_bars == 1000                 # 计算窗口远大于下限


def test_signal_comes_from_last_bar_of_window():
    assert mk_runner(LastBarDirection(), BARS_UP).decide(NOW_DUE).signal == 1
    assert mk_runner(LastBarDirection(), BARS_DOWN).decide(NOW_DUE).signal == -1


def test_same_strategy_code_as_backtest_buy_hold():
    # 与回测共用同一份策略实现（blueprint §4 第 5 条）
    d = mk_runner(BuyHold(), BARS_UP).decide(NOW_DUE)
    assert d.signal == 1
    assert d.target_position == 1


# ---- 决策 ID ----

def test_decision_id_deterministic_and_unique_per_bar():
    d1 = mk_runner(LastBarDirection(), BARS_UP).decide(NOW_DUE)
    d2 = mk_runner(LastBarDirection(), BARS_UP).decide(NOW_DUE)
    assert d1.decision_id == d2.decision_id
    assert d1.decision_id == "XAUUSD|M15|2026-10-20T18:00:00"
    nxt = NOW_DUE + timedelta(minutes=15)   # 18:15 收盘，再下一格
    bars2 = pd.concat([BARS_UP, mk_bars([("2026-10-20 18:00", 100.0, 101.5, 98.5, 99.0)])],
                      ignore_index=True)
    d3 = mk_runner(LastBarDirection(), bars2).decide(nxt + timedelta(seconds=1))
    assert d3.decision_id == "XAUUSD|M15|2026-10-20T18:15:00"


# ---- SL/TP 口径：现价侧 ± k × ATR（信号 bar，engine_a.atr_series 同源）----

def test_sl_tp_prices_for_long_and_short():
    for bars, ref in ((BARS_UP, 100.2), (BARS_DOWN, 100.0)):  # 多用 Ask、空用 Bid
        d = mk_runner(LastBarDirection(), bars, quote=(100.0, 100.2)).decide(NOW_DUE)
        window = bars.iloc[-min(1000, len(bars)):]   # T25：计算窗口（非 lookback）
        atr = float(atr_series(window, 14).iloc[-1])
        assert d.atr == pytest.approx(atr)
        if d.signal == 1:
            assert d.sl == pytest.approx(ref - 1.5 * atr)
            assert d.tp == pytest.approx(ref + 2.5 * atr)
        else:
            assert d.sl == pytest.approx(ref + 1.5 * atr)
            assert d.tp == pytest.approx(ref - 2.5 * atr)


def test_atr_nan_means_no_sl_tp():
    # lookback=10 < ATR14 所需 → NaN → 不设 SL/TP（与引擎口径一致）
    # 10 根从 08:00 起 → 末根 10:15（收盘 10:30），墙钟对齐该收盘
    bars = const_range_bars(10, 900, last_up=True)
    d = mk_runner(LastBarDirection(), bars, lookback_bars=10).decide(
        datetime(2026, 10, 20, 10, 30, 7))
    assert d.atr is None and d.sl is None and d.tp is None


def test_flat_signal_has_no_sl_tp():
    d = mk_runner(LastBarDirection(), BARS_UP).decide(NOW_DUE)
    assert d.signal != 0
    flat = SignalRunner(  # 恒 0 信号策略
        mk_config(), _ConstSignal(0),
        bars_loader=lambda root, s, tf: BARS_UP,
        quote_fn=lambda s: (100.0, 100.2)).decide(NOW_DUE)
    assert flat.signal == 0 and flat.sl is None and flat.tp is None


# ---- 现价 ----

def test_quote_missing_leaves_prices_none():
    r = mk_runner(LastBarDirection(), BARS_UP, quote=None)
    d = r.decide(NOW_DUE)
    assert d.quote_bid is None and d.quote_ask is None
    assert d.sl is None and d.tp is None


# ---- 输入校验 ----

def test_invalid_signal_value_rejected():
    r = mk_runner(_ConstSignal(2), BARS_UP)
    with pytest.raises(ValueError, match="-1,0,1"):
        r.decide(NOW_DUE)


def test_config_lookback_below_strategy_requirement_rejected():
    strategy = LastBarDirection()
    strategy.required_data = type(strategy.required_data)(
        symbols=(), timeframe="", lookback_bars=50)
    with pytest.raises(ValueError, match="lookback_bars"):
        mk_runner(strategy, BARS_UP, lookback_bars=30)


def test_timeframe_mismatch_with_strategy_rejected():
    strategy = LastBarDirection()
    strategy.required_data = type(strategy.required_data)(
        symbols=(), timeframe="H1", lookback_bars=2)
    with pytest.raises(ValueError, match="timeframe"):
        mk_runner(strategy, BARS_UP)


# ---- 心跳文件（供风控进程判活，接口约定见 write_heartbeat docstring）----

def test_heartbeat_written_and_replaced(tmp_path):
    cfg = mk_config()
    hb = tmp_path / "hb.json"
    d = mk_runner(LastBarDirection(), BARS_UP).decide(NOW_DUE)
    write_heartbeat(str(hb), config=cfg, decision=d, dry_run=True, pid=123)
    payload = json.loads(hb.read_text(encoding="utf-8"))
    assert payload["pid"] == 123
    assert payload["symbol"] == "XAUUSD"
    assert payload["timeframe"] == TF
    assert payload["magic"] == 777
    assert payload["risk_check_enabled"] is True
    assert payload["dry_run"] is True
    assert payload["last_error"] is None
    assert payload["last_decision"]["decision_id"] == d.decision_id
    assert payload["last_decision"]["signal"] == 1
    assert payload["last_decision"]["bar_close_time"] == "2026-10-20T18:00:00"

    write_heartbeat(str(hb), config=cfg, decision=None,
                    error="boom", dry_run=False, pid=123)
    payload2 = json.loads(hb.read_text(encoding="utf-8"))
    assert payload2["last_decision"] is None
    assert payload2["last_error"] == "boom"
    assert payload2["dry_run"] is False


def test_heartbeat_readable_by_t14b_monitor(tmp_path):
    """心跳兼容 monitor.read_heartbeat 的 runner_alive/ts 契约（超集字段）。"""
    from mt5live.monitor import read_heartbeat  # T14b 的读取端

    live_dir = tmp_path / "live"
    write_heartbeat(str(live_dir / "heartbeat.json"), config=mk_config(),
                    dry_run=True, pid=123)
    parsed = read_heartbeat(str(live_dir))
    assert parsed.runner_alive is True
    assert parsed.ts.tzinfo is None            # 读取端归一化为 naive UTC


def test_default_heartbeat_path_under_data_root():
    # T24/M9：每策略一个心跳文件 heartbeat-<magic>.json
    assert default_heartbeat_path(mk_config()).endswith(
        "live\\heartbeat-777.json") or default_heartbeat_path(
        mk_config()).endswith("live/heartbeat-777.json")
    cfg = mk_config(heartbeat_path="x/hb.json")
    assert default_heartbeat_path(cfg) == "x/hb.json"


# ---- T33/M1：心跳写入失败不退出（PermissionError 不再杀进程）----

def test_write_heartbeat_survives_transient_permission_error(tmp_path,
                                                             monkeypatch):
    """M1 根因：风控进程正读着心跳文件 → os.replace 抛 PermissionError。
    原子写的重试预算内自愈，心跳写入成功（monkeypatch 前两次抛错）。"""
    import os as _os

    real = _os.replace
    state = {"n": 0}

    def flaky(src, dst):
        if state["n"] < 2:
            state["n"] += 1
            raise PermissionError(32, "另一个进程正在使用此文件")
        return real(src, dst)

    monkeypatch.setattr(_os, "replace", flaky)
    hb = tmp_path / "heartbeat-777.json"
    write_heartbeat(str(hb), config=mk_config(), dry_run=True, pid=1)
    payload = json.loads(hb.read_text(encoding="utf-8"))
    assert payload["runner_alive"] is True and payload["pid"] == 1


def test_heartbeat_writer_failure_does_not_kill_and_alerts_after_3(tmp_path,
                                                                  monkeypatch):
    """写心跳持续失败：不抛（运行器不退出）、记日志；连续 3 次 → 告警一次；
    成功一次即复位（新的连续失败段才再告警）。"""
    from mt5live import runner as runner_mod
    from mt5live.runner import HeartbeatWriter

    live = tmp_path / "live"
    live.mkdir()
    writer = HeartbeatWriter(str(live / "hb.json"), config=mk_config(),
                             alert_dir=str(live))
    alerts = live / "alerts.log"

    def _boom(*a, **kw):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(runner_mod, "write_heartbeat", _boom)
    for _ in range(2):                            # 前 2 次：只记日志，不告警
        assert writer.write(dry_run=True) is False
    assert not alerts.exists()

    assert writer.write(dry_run=True) is False    # 第 3 次：告警一次
    text = alerts.read_text(encoding="utf-8")
    assert "HEARTBEAT_WRITE_ERROR" in text and "3" in text
    n_alerts = text.count("HEARTBEAT_WRITE_ERROR")
    writer.write(dry_run=True)                    # 持续失败不重复刷告警
    assert alerts.read_text(encoding="utf-8").count(
        "HEARTBEAT_WRITE_ERROR") == n_alerts

    monkeypatch.setattr(runner_mod, "write_heartbeat", lambda *a, **kw: None)
    assert writer.write(dry_run=True) is True     # 恢复：计数复位
    assert writer.consecutive_failures == 0

    monkeypatch.setattr(runner_mod, "write_heartbeat", _boom)
    for _ in range(2):
        writer.write(dry_run=True)
    assert alerts.read_text(encoding="utf-8").count(
        "HEARTBEAT_WRITE_ERROR") == n_alerts      # 新段前 2 次不告警
    writer.write(dry_run=True)                    # 新段第 3 次 → 再告警
    assert alerts.read_text(encoding="utf-8").count(
        "HEARTBEAT_WRITE_ERROR") == n_alerts + 1


def test_heartbeat_writer_alert_channel_failure_is_swallowed(tmp_path,
                                                             monkeypatch):
    """告警通道（alerts.log）同样写不进去时也不许把运行器打崩（日志已记）。"""
    from mt5live import runner as runner_mod
    from mt5live.runner import HeartbeatWriter

    live = tmp_path / "live"
    live.mkdir()
    writer = HeartbeatWriter(str(live / "hb.json"), config=mk_config(),
                             alert_dir=str(live))

    def _boom(*a, **kw):
        raise PermissionError(5, "拒绝访问")

    monkeypatch.setattr(runner_mod, "write_heartbeat", _boom)
    monkeypatch.setattr(runner_mod, "append_alert", _boom)
    for _ in range(3):
        assert writer.write(dry_run=True) is False    # 不抛 = 不退出


class _ConstSignal(BaseStrategy):
    """恒定信号（含非法值 2 的注入用）。"""

    def __init__(self, signal):
        self.signal = signal

    def generate_signals(self, bars, params):
        return pd.Series(self.signal, index=bars.index, dtype="int64")
