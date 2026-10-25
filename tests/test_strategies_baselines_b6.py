"""B6 波动率目标多头（XAUUSD H1）信号语义测试。

语义：BuyHold 的波动率管理变体——已实现波动率（vol_n 根收盘收益率的
滚动标准差）≤ 目标时持有多头，超过时空仓。信号接口只支持 {-1,0,1}，
连续仓位缩放退化为二元风险开关（卡内如实说明）。
"""
import inspect

import numpy as np
import pandas as pd
import pytest

from mt5quant.strategies.library.b6_vol_target_long import VolTargetLong
from mt5quant.strategies.base import ExitRules
from mt5quant.validation.lookahead import (
    check_window_invariance, scan_forbidden_patterns,
)
from mt5quant.backtest.truncation import check_no_lookahead

P = {"vol_n": 20, "vol_target": 0.001}


def make_bars(closes):
    closes = pd.Series(closes, dtype="float64")
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=len(closes), freq="h"),
        "open": closes.shift(1).fillna(closes.iloc[0]),
        "high": closes * 1.001, "low": closes * 0.999, "close": closes,
    })


def calm(n, base=2000.0, amp=0.0004):
    """低波动段：逐根收益 ±amp（收益率 std≈0.0004 < 0.001）。"""
    out, v = [], base
    for i in range(n):
        v = v * (1.0 + (amp if i % 2 == 0 else -amp))
        out.append(v)
    return out


def wild(n, base=2000.0, amp=0.01):
    """高波动段：逐根收益 ±1%（收益率 std≈0.01 > 0.003）。"""
    out, v = [], base
    for i in range(n):
        v = v * (1.0 + (amp if i % 2 == 0 else -amp))
        out.append(v)
    return out


def signals(bars, params=P):
    return VolTargetLong().generate_signals(bars, params).tolist()


# ---------------------------------------------------------------- 风险开关语义

def test_flat_prices_are_always_invested_after_warmup():
    bars = make_bars([2000.0] * 60)
    sig = signals(bars)
    assert sig[19] == 0                # rolling(20) 尚不足（首收益 NaN）：0
    assert sig[20] == 1                # vol=0 ≤ 目标：持有多头
    assert all(s == 1 for s in sig[20:])


def test_risk_off_when_realized_vol_exceeds_target():
    closes = calm(40) + wild(30, base=calm(1)[-1]) + calm(40, base=wild(1)[-1])
    bars = make_bars(closes)
    sig = signals(bars)                # vol_n=20：高波动进入窗口后 ≈20 根内关掉
    assert all(s == 1 for s in sig[20:40])
    off = [i for i, s in enumerate(sig) if s == 0 and i >= 40]
    assert off, "高波动段必须出现空仓"
    assert 40 <= off[0] <= 60          # 窗口洗净需要 ≤ vol_n 根
    assert sig[-1] == 1                # 回到低波动：重新持有多头
    assert set(sig) <= {0, 1}          # 只做多


def test_threshold_is_the_dividing_line():
    """收益率 std≈0.0025：目标 0.002 → 空仓；目标 0.003 → 持有。"""
    r = 0.0025
    closes, v = [2000.0], 2000.0
    for i in range(59):
        v = v * (1.0 + (r if i % 2 == 0 else -r))
        closes.append(v)
    bars = make_bars(closes)
    sig_strict = signals(bars, {"vol_n": 20, "vol_target": 0.002})
    sig_loose = signals(bars, {"vol_n": 20, "vol_target": 0.003})
    assert sig_strict[-1] == 0
    assert sig_loose[-1] == 1


def test_long_only_never_short():
    rng = np.random.default_rng(7)
    closes = list(2000.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))))
    sig = signals(make_bars(closes))
    assert set(sig) <= {0, 1}


# ---------------------------------------------------------------- 只用已收盘 bar

def test_truncation_and_window_invariance_and_static_scan():
    rng = np.random.default_rng(11)
    closes = list(2000.0 * np.exp(np.cumsum(rng.normal(0, 0.008, 500))))
    bars = make_bars(closes)
    s = VolTargetLong()
    for params in (P, {"vol_n": 120, "vol_target": 0.003}):
        assert check_no_lookahead(s, bars, params=params, probe_points=5) == []
        assert check_window_invariance(s, bars, params, probes=5) == []
    from mt5quant.strategies.library import b6_vol_target_long as mod
    assert scan_forbidden_patterns(inspect.getsource(mod)) == []


# ---------------------------------------------------------------- 声明与约束

def test_required_data_and_exit_rules_declared():
    rd = VolTargetLong.required_data
    assert rd.timeframe == "H1"
    assert rd.symbols == ("XAUUSD",)
    assert rd.lookback_bars >= 120 + 8         # 网格最大 vol_n + 缓冲
    assert VolTargetLong.exit_rules.stop_loss_atr is None   # 基准变体：无保护性出场
    assert VolTargetLong.exit_rules.reentry == "immediate"


def test_grid_size_within_limits():
    s = VolTargetLong()
    combos = [(n, t) for n in (20, 60, 120)
              for t in (0.001, 0.0015, 0.002, 0.003)]
    assert len(combos) == 12 <= 100
    for n, t in combos:
        s.validate_params({"vol_n": n, "vol_target": t})
    with pytest.raises(ValueError):
        s.validate_params({"vol_n": 20, "vol_target": 0.005})


def test_signal_series_valid_for_engine():
    bars = make_bars(calm(60))
    sig = VolTargetLong().generate_signals(bars, P)
    assert isinstance(sig, pd.Series)
    assert sig.index.equals(bars.index)
    assert set(sig.unique()) <= {-1, 0, 1}


def test_exit_rules_default_object():
    """ExitRules() 缺省值全空——与 BuyHold 同性质（基准，不设止损/时间止损）。"""
    assert VolTargetLong.exit_rules == ExitRules()
