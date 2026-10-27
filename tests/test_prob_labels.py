"""T72 首达标签：与引擎 A 逐笔同口径（合成数据对账 + 语义边界 + purge）。

对账口径：label_triggers 对每个触发截取有界切片（LOOKBACK_BARS 回看 +
K+2 根）单独跑引擎 A；测试用同一规则切片、直接调 run_backtest 做独立参照，
要求出场原因映射与 net_r（= 引擎 r_multiple）逐笔一致。
"""
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from mt5data.sessions import SessionPolicy
from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
from mt5quant.backtest.engine_a import run_backtest
from mt5quant.prob.labels import LOOKBACK_BARS, Bracket, label_triggers
from mt5quant.sizing import RiskSizing
from mt5quant.strategies import BaseStrategy, ExitRules

HALF, SLIP, COMM, ONIGHT = 0.5, 0.1, 1.0, -2.0


class FlatCosts:
    """手算假成本（绝对口径，price 被忽略）：与引擎测试同款。"""

    def __init__(self, half=HALF, slip=SLIP, commission=COMM, overnight=ONIGHT):
        self.half, self.slip = half, slip
        self.commission, self.overnight = commission, overnight

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return self.half

    def slippage(self, symbol, hour_ny, *, price=None):
        return self.slip

    def commission_per_lot(self, symbol):
        return self.commission

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        f, t = pd.Timestamp(from_ts), pd.Timestamp(to_ts)
        if t <= f:
            return 0.0
        return self.overnight * (t.normalize() - f.normalize()).days


class ScriptedStrategy(BaseStrategy):
    """按位置取脚本信号（缺省 0）。"""

    def __init__(self, script, rules):
        self.script = list(script)
        self.exit_rules = rules

    def generate_signals(self, bars, params):
        vals = [self.script[i] if i < len(self.script) else 0
                for i in range(len(bars))]
        return pd.Series(vals, index=bars.index, dtype="int64")


def synth_bars(n, *, seed, tf="H1", start="2024-01-01", s0=100.0):
    """几何随机游走 H1 bar（可复现）。"""
    rng = np.random.default_rng(seed)
    secs = TIMEFRAME_SECONDS[tf]
    times = pd.date_range(start, periods=n, freq=f"{secs}s")
    steps = rng.normal(0.0, 0.35, size=n)
    close = s0 + np.cumsum(steps)
    openc = close + rng.normal(0.0, 0.05, size=n)
    high = np.maximum(openc, close) + np.abs(rng.normal(0.0, 0.2, size=n))
    low = np.minimum(openc, close) - np.abs(rng.normal(0.0, 0.2, size=n))
    return pd.DataFrame({
        "time": times, "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


def flat_bars(n, *, tf="H1", openc=100.0, high=100.5, low=99.5, close=100.0):
    secs = TIMEFRAME_SECONDS[tf]
    times = pd.date_range("2024-01-01", periods=n, freq=f"{secs}s")
    return pd.DataFrame({
        "time": times, "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


SIZING = RiskSizing(risk_fraction=0.005)
VOL = (0.01, 0.01, 10.0)
CS = 100.0
# 轻成本：随机对账用的随机游走 ATR≈0.9，重成本（0.6）会把多头胜负距离
# 拉成 ~0.3 vs ~1.9 的不对称（引擎行为正确，但对账样本清一色 loss）；
# 胜负/到期三类覆盖由轻成本保证，成本语义另有专项手算测试。
LIGHT = FlatCosts(half=0.05, slip=0.01, commission=0.1, overnight=-0.01)


def _engine_ref(bars, t, side, bracket, costs, *, session_policy=None):
    """独立参照：同一规则切片 + 直接 run_backtest 一笔。"""
    n = len(bars)
    start = max(0, t - LOOKBACK_BARS)
    end = min(n, t + bracket.horizon_bars + 2)
    sl = bars.iloc[start:end].reset_index(drop=True)
    i0 = t - start
    script = [0] * len(sl)
    for j in range(i0, min(i0 + bracket.horizon_bars, len(sl))):
        script[j] = side
    strat = ScriptedStrategy(script, ExitRules(
        stop_loss_atr=bracket.sl_atr, take_profit_atr=bracket.tp_atr,
        reentry="after_signal_change"))
    res = run_backtest(strat, sl, costs, symbol="XAUUSD", params={},
                       contract_size=CS, timeframe="H1",
                       session_policy=session_policy, sizing=SIZING,
                       vol_min=VOL[0], vol_step=VOL[1], vol_max=VOL[2])
    if len(res.trades) == 0:
        return None, res
    assert len(res.trades) == 1
    return res.trades.iloc[0], res


def _trig(times, *, side=1, situation="S1", action="A3", level=100.0):
    return pd.DataFrame({"time": list(times), "situation": situation,
                         "action": action, "side": side, "level": level})


# ------------------------------------------------------------ 对账

def test_labels_match_engine_200_random_times():
    """随机抽 200 个随机时点：标签与引擎 A 单笔逐笔一致（先红后绿的验收线）。"""
    bars = synth_bars(700, seed=7)
    rng = np.random.default_rng(11)
    idx = rng.integers(LOOKBACK_BARS + 20, len(bars) - 10, size=200)
    sides = rng.choice([1, -1], size=200)
    bracket = Bracket(sl_atr=1.5, tp_atr=1.5, horizon_bars=12)
    out = label_triggers(bars, _trig([bars["time"].iloc[i] for i in idx],
                                     side=sides.tolist()),
                         LIGHT, symbol="XAUUSD", timeframe="H1",
                         bracket=bracket, contract_size=CS,
                         sizing=SIZING, vol_bounds=VOL)
    assert len(out) == 200
    outcomes = set(out["outcome"])
    assert outcomes <= {"win", "loss", "time", "skipped"}
    assert {"win", "loss", "time"} <= outcomes   # 样本足够三类都应出现
    for row, t, s in zip(out.itertuples(index=False), idx, sides):
        ref, _ = _engine_ref(bars, int(t), int(s), bracket, LIGHT)
        if ref is None:
            assert row.outcome == "skipped", (t, s, row)
            continue
        expected = {"take_profit": "win", "take_profit_gap": "win",
                    "stop": "loss", "stop_gap": "loss", "signal": "time"}[
                        ref.exit_reason]
        assert row.outcome == expected, (t, s, row.outcome, ref.exit_reason)
        assert row.exit_reason == ref.exit_reason
        assert abs(row.net_r - ref.r_multiple) < 1e-12
        assert pd.Timestamp(row.entry_time) == pd.Timestamp(ref.entry_time)
        assert pd.Timestamp(row.exit_time) == pd.Timestamp(ref.exit_time)
        assert np.isfinite(row.net_r)


# ------------------------------------------------------------ 语义边界

def test_same_bar_sl_tp_stop_priority_is_loss():
    """同一根 bar 同时触及止损与止盈 → 止损优先（loss / stop）。"""
    bars = flat_bars(360)
    t = LOOKBACK_BARS + 20            # ATR=1（TR 恒为 1）
    bars.loc[t + 1, ["open", "high", "low", "close"]] = [101.0, 104.0, 100.0, 103.0]
    bracket = Bracket(sl_atr=1.0, tp_atr=2.0, horizon_bars=6)
    out = label_triggers(bars, _trig([bars["time"].iloc[t]]), FlatCosts(),
                         symbol="XAUUSD", timeframe="H1", bracket=bracket,
                         contract_size=CS, sizing=SIZING, vol_bounds=VOL)
    row = out.iloc[0]
    assert row.outcome == "loss" and row.exit_reason == "stop"
    ref, _ = _engine_ref(bars, t, 1, bracket, FlatCosts())
    assert ref.exit_reason == "stop"
    assert abs(row.net_r - ref.r_multiple) < 1e-12


def test_gap_through_stop_is_loss():
    """开盘跳空穿过止损 → stop_gap，按开盘价成交（loss）。"""
    bars = flat_bars(360)
    t = LOOKBACK_BARS + 20
    # 入场 bar（t+1）平静：low 高于 SL（entry−1.0≈99.6）、high 低于 TP
    bars.loc[t + 1, ["open", "high", "low", "close"]] = [101.0, 101.2, 100.8, 101.0]
    # t+2 开盘 99.0，低于 SL=99.6 → 跳空穿止损
    bars.loc[t + 2, ["open", "high", "low", "close"]] = [99.0, 99.5, 98.5, 99.2]
    bracket = Bracket(sl_atr=1.0, tp_atr=2.0, horizon_bars=6)
    out = label_triggers(bars, _trig([bars["time"].iloc[t]]), FlatCosts(),
                         symbol="XAUUSD", timeframe="H1", bracket=bracket,
                         contract_size=CS, sizing=SIZING, vol_bounds=VOL)
    row = out.iloc[0]
    assert row.outcome == "loss" and row.exit_reason == "stop_gap"
    ref, _ = _engine_ref(bars, t, 1, bracket, FlatCosts())
    assert abs(row.net_r - ref.r_multiple) < 1e-12


def test_horizon_expiry_is_time_exit():
    """K 根内未触止损止盈 → 信号归零平仓（time / signal），持有恰 K 根。"""
    bars = flat_bars(360, high=100.2, low=99.8)   # TR=0.4 → ATR=0.4
    t = LOOKBACK_BARS + 20
    bracket = Bracket(sl_atr=3.0, tp_atr=4.0, horizon_bars=5)
    out = label_triggers(bars, _trig([bars["time"].iloc[t]]), FlatCosts(),
                         symbol="XAUUSD", timeframe="H1", bracket=bracket,
                         contract_size=CS, sizing=SIZING, vol_bounds=VOL)
    row = out.iloc[0]
    assert row.outcome == "time" and row.exit_reason == "signal"
    ref, _ = _engine_ref(bars, t, 1, bracket, FlatCosts())
    assert int(ref.bars_held) == 5
    assert abs(row.net_r - ref.r_multiple) < 1e-12


def test_blocked_entry_delayed_matches_engine():
    """禁入窗推迟入场：标签与引擎同口径（同一 SessionPolicy 下逐笔一致）。"""
    bars = synth_bars(500, seed=3)
    t = LOOKBACK_BARS + 20
    entry_time = bars["time"].iloc[t + 1]
    ny_hour = entry_time.tz_localize("UTC").tz_convert("America/New_York").hour
    policy = SessionPolicy(forbidden_hours_ny={"XAUUSD": frozenset({ny_hour})})
    bracket = Bracket(sl_atr=1.0, tp_atr=2.0, horizon_bars=6)
    out = label_triggers(bars, _trig([bars["time"].iloc[t]]), FlatCosts(),
                         symbol="XAUUSD", timeframe="H1", bracket=bracket,
                         contract_size=CS, sizing=SIZING, vol_bounds=VOL,
                         session_policy=policy)
    ref, _ = _engine_ref(bars, t, 1, bracket, FlatCosts(), session_policy=policy)
    row = out.iloc[0]
    if ref is None:
        assert row.outcome == "skipped"
    else:
        assert row.outcome in {"win", "loss", "time"}
        assert abs(row.net_r - ref.r_multiple) < 1e-12
        assert row.exit_reason == ref.exit_reason
        assert pd.Timestamp(row.entry_time) == pd.Timestamp(ref.entry_time)


def test_zero_lots_is_skipped():
    """风险额不足最小手数 → 手数 0 → 未成交（skipped）。"""
    bars = synth_bars(400, seed=5)
    t = LOOKBACK_BARS + 20
    tiny = RiskSizing(risk_fraction=1e-9)
    bracket = Bracket(sl_atr=1.0, tp_atr=2.0, horizon_bars=6)
    out = label_triggers(bars, _trig([bars["time"].iloc[t]]), FlatCosts(),
                         symbol="XAUUSD", timeframe="H1", bracket=bracket,
                         contract_size=CS, sizing=tiny, vol_bounds=VOL)
    row = out.iloc[0]
    assert row.outcome == "skipped" and row.exit_reason == "zero_lots"
    assert pd.isna(row.net_r)


def test_data_short_of_horizon_is_censored_skipped():
    """数据不足以让标签窗走完且未提前出场 → 截尾（skipped / end_of_data）。"""
    bars = flat_bars(360, high=100.2, low=99.8)
    t = LOOKBACK_BARS + 20
    bracket = Bracket(sl_atr=3.0, tp_atr=4.0, horizon_bars=5)
    cut = bars.iloc[:t + 4].reset_index(drop=True)   # 只到 t+3，信号出场需 t+6
    out = label_triggers(cut, _trig([cut["time"].iloc[t]]), FlatCosts(),
                         symbol="XAUUSD", timeframe="H1", bracket=bracket,
                         contract_size=CS, sizing=SIZING, vol_bounds=VOL)
    row = out.iloc[0]
    assert row.outcome == "skipped" and row.exit_reason == "end_of_data"


# ------------------------------------------------------------ purge

def test_purge_drops_triggers_crossing_train_end():
    """train_end 给定时，标签窗越过 train_end 的触发被剔除。"""
    bars = synth_bars(500, seed=13)
    tf_s = TIMEFRAME_SECONDS["H1"]
    t = LOOKBACK_BARS + 20
    bracket = Bracket(sl_atr=1.0, tp_atr=2.0, horizon_bars=6)
    trig = _trig([bars["time"].iloc[t], bars["time"].iloc[t + 30]])
    # 窗口宽 (K+2)=8 根：t+30 的触发越界；train_end 放在 t+20 根处
    train_end = bars["time"].iloc[t] + pd.Timedelta(seconds=tf_s * 20)
    out = label_triggers(bars, trig, FlatCosts(), symbol="XAUUSD",
                         timeframe="H1", bracket=bracket, contract_size=CS,
                         sizing=SIZING, vol_bounds=VOL, train_end=train_end)
    assert len(out) == 1
    assert pd.Timestamp(out.iloc[0]["time"]) == pd.Timestamp(bars["time"].iloc[t])
    # train_end 覆盖两个窗口时都不剔除
    out2 = label_triggers(bars, trig, FlatCosts(), symbol="XAUUSD",
                          timeframe="H1", bracket=bracket, contract_size=CS,
                          sizing=SIZING, vol_bounds=VOL,
                          train_end=bars["time"].iloc[t + 40])
    assert len(out2) == 2


# ------------------------------------------------------------ 入参校验

def test_label_triggers_rejects_bad_inputs():
    bars = synth_bars(400, seed=17)
    t = LOOKBACK_BARS + 20
    bracket = Bracket(sl_atr=1.0, tp_atr=2.0, horizon_bars=6)
    kw = dict(symbol="XAUUSD", timeframe="H1", bracket=bracket,
              contract_size=CS, sizing=SIZING, vol_bounds=VOL)
    with pytest.raises(ValueError):   # time 不在 bars 里
        label_triggers(bars, _trig([pd.Timestamp("2030-01-01")]), FlatCosts(), **kw)
    with pytest.raises(ValueError):   # side 非法
        label_triggers(bars, _trig([bars["time"].iloc[t]], side=0), FlatCosts(), **kw)
    with pytest.raises(ValueError):   # horizon 非法
        label_triggers(bars, _trig([bars["time"].iloc[t]]), FlatCosts(),
                       **{**kw, "bracket": Bracket(1.0, 2.0, 0)}, )
