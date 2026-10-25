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


# ---- 窗口 = lookback_bars、同一份策略代码 ----

def test_window_is_exactly_lookback_bars():
    strategy = LastBarDirection()
    r = mk_runner(strategy, BARS_UP, lookback_bars=30)
    d = r.decide(NOW_DUE)
    assert strategy.calls[0]["n"] == 30
    # 40 根取尾部 30 → 首根 = 08:00 + 10×15m = 10:30
    assert strategy.calls[0]["first_time"] == pd.Timestamp("2026-10-20 10:30")
    assert d.bars_used == 30


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
        window = bars.iloc[-30:]
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
    assert default_heartbeat_path(mk_config()).endswith(
        "live\\heartbeat.json") or default_heartbeat_path(mk_config()).endswith(
        "live/heartbeat.json")
    cfg = mk_config(heartbeat_path="x/hb.json")
    assert default_heartbeat_path(cfg) == "x/hb.json"


class _ConstSignal(BaseStrategy):
    """恒定信号（含非法值 2 的注入用）。"""

    def __init__(self, signal):
        self.signal = signal

    def generate_signals(self, bars, params):
        return pd.Series(self.signal, index=bars.index, dtype="int64")
