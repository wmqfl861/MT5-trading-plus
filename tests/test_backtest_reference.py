"""T26 多笔 A/B 对账参考策略（backtest.reference.MaCrossATR）测试。

覆盖（失败测试先行，2026-10-26）：
1. 信号语义手造断言：金叉/死叉首次变号的精确 bar 位、SMA 预热为 0、
   fast==slow 恒 0（np.sign(0)=0，与 MACrossRef.mq5 的 f[0]==s[0] 分支一致）、
   只用收盘价（扰动 high/low 信号不变）；
2. G0 干净：截断（check_no_lookahead）+ 窗口不变性（随机探针 + 连续 sweep）
   + 静态扫描；
3. 出场规则与变体：默认 SL/TP、after_signal_change 子类、参数与 ExitRules
   不一致必须报错（引擎只读 ExitRules，参数副本只作记录，禁止静默分叉）；
4. A/B 双引擎多笔冒烟（tick 复现 bar OHLC）：多头止损+immediate 重入、
   空头止盈、反向交叉 signal 出场+反手——每笔入场时间差 0、净利差 0，
   止损/收盘价类出场时间差 < 1 根 bar（结构内差异）；
5. 隔离：参考策略不进 mt5quant.strategies（library）。
"""
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mt5quant.backtest.engine_a import atr_series
from mt5quant.backtest.reference import (
    LOOKBACK_BARS, MaCrossATR, MaCrossATRAfterSignalChange,
)
from mt5quant.backtest.truncation import check_no_lookahead
from mt5quant.validation.lookahead import (
    check_window_invariance, check_window_invariance_sweep, run_g0,
    static_scan,
)

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_ab.py"

P_DEFAULT = {"fast": 10, "slow": 30, "stop_loss_atr": 2.0, "take_profit_atr": 3.0}
P_FAST = {"fast": 5, "slow": 15, "stop_loss_atr": 2.0, "take_profit_atr": 3.0}


def _load_script():
    spec = importlib.util.spec_from_file_location("reconcile_ab_script", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_ab_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def make_bars(closes, highs=None, lows=None, opens=None, start="2026-01-05",
              freq="h"):
    """手造 H1 bars：默认 open=前收盘、high/low=收盘±0.5。"""
    closes = pd.Series(closes, dtype="float64")
    highs = closes + 0.5 if highs is None else pd.Series(highs, dtype="float64")
    lows = closes - 0.5 if lows is None else pd.Series(lows, dtype="float64")
    if opens is None:
        opens = closes.shift(1).fillna(closes.iloc[0])
    else:
        opens = pd.Series(opens, dtype="float64")
    return pd.DataFrame({
        "time": pd.date_range(start, periods=len(closes), freq=freq),
        "open": opens, "high": highs, "low": lows, "close": closes,
    })


def flat_then_step(base, n_flat, step, n_after, start="2026-01-05"):
    """n_flat 根横盘 base，随后每根收盘 ±step 递变（交叉位精确可算）。"""
    closes = [base] * n_flat + [base + step * (i + 1) for i in range(n_after)]
    return make_bars(closes, start=start)


def signals(bars, params=P_DEFAULT, strategy=None):
    return (strategy or MaCrossATR()).generate_signals(bars, params).tolist()


# ---------------------------------------------------------------- 信号语义（手造）

def test_golden_cross_first_positive_at_exact_bar_10_30():
    """30 根横盘后首根上涨收盘：SMA30 在第 29 根恰好可用且 fast==slow → 0；
    第 30 根（首个上涨收盘）fast−slow = d×(1/10−1/30) > 0 → 首次 +1，粘性。"""
    sig = signals(flat_then_step(100.0, 30, 3.0, 12))
    assert sig[:29] == [0] * 29            # rolling(30) NaN：预热期信号 0
    assert sig[29] == 0                    # fast==slow==100：sign(0)=0
    assert sig[30] == 1                    # 金叉当根（收盘判定）
    assert all(s == 1 for s in sig[31:])   # 无反向事件：目标仓位粘性


def test_dead_cross_first_negative_at_exact_bar_10_30():
    sig = signals(flat_then_step(100.0, 30, -3.0, 12))
    assert sig[:29] == [0] * 29
    assert sig[29] == 0
    assert sig[30] == -1                   # 死叉当根 → 空头（多空都做）
    assert all(s == -1 for s in sig[31:])


def test_cross_position_follows_slow_period_5_15():
    """高换手变体 5/15：交叉位随 slow 平移到第 15 根。"""
    sig = signals(flat_then_step(100.0, 15, 2.0, 8), params=P_FAST)
    assert sig[:14] == [0] * 14
    assert sig[14] == 0
    assert sig[15] == 1
    assert sig[16] == 1


def test_flat_series_signal_is_zero_not_side():
    """恒平收盘：fast==slow 恒成立 → 信号恒 0（不是默认多头）。"""
    bars = make_bars([100.0] * 40)
    assert set(signals(bars)) == {0}
    assert set(signals(bars, params=P_FAST)) == {0}


def test_signal_uses_close_only_high_low_irrelevant():
    base = flat_then_step(100.0, 30, 3.0, 8)
    perturbed = base.copy()
    perturbed["high"] = perturbed["close"] + 7.0
    perturbed["low"] = perturbed["close"] - 7.0
    assert signals(base) == signals(perturbed)


def test_fast_not_below_slow_rejected():
    bars = make_bars([100.0] * 20)
    bad = {"fast": 5, "slow": 4, "stop_loss_atr": 2.0, "take_profit_atr": 3.0}
    with pytest.raises(ValueError, match="fast"):
        MaCrossATR().generate_signals(bars, bad)


# ---------------------------------------------------------------- 出场规则与变体

def test_default_exit_rules_sl_tp_immediate():
    r = MaCrossATR().exit_rules
    assert r.stop_loss_atr == 2.0
    assert r.take_profit_atr == 3.0
    assert r.reentry == "immediate"


def test_after_signal_change_variant():
    r = MaCrossATRAfterSignalChange().exit_rules
    assert r.reentry == "after_signal_change"
    assert r.stop_loss_atr == 2.0 and r.take_profit_atr == 3.0


def test_constructor_overrides_engine_rules():
    s = MaCrossATR(stop_loss_atr=1.0, take_profit_atr=2.0)
    assert s.exit_rules.stop_loss_atr == 1.0
    assert s.exit_rules.take_profit_atr == 2.0
    assert MaCrossATRAfterSignalChange(stop_loss_atr=3.0).exit_rules \
        .stop_loss_atr == 3.0


def test_params_sl_tp_must_match_exit_rules():
    """引擎只读 ExitRules；--params 里的 SL/TP 只是记录副本，不一致即报错。"""
    bars = make_bars([100.0] * 40)
    mismatch = dict(P_DEFAULT, stop_loss_atr=3.0)
    with pytest.raises(ValueError, match="stop_loss_atr"):
        MaCrossATR().generate_signals(bars, mismatch)
    mismatch_tp = dict(P_DEFAULT, take_profit_atr=6.0)
    with pytest.raises(ValueError, match="take_profit_atr"):
        MaCrossATR().generate_signals(bars, mismatch_tp)
    # 与实例规则一致（构造器改规则 + 参数同步）→ 正常出信号
    s = MaCrossATR(stop_loss_atr=3.0, take_profit_atr=6.0)
    assert set(s.generate_signals(bars, dict(P_DEFAULT, stop_loss_atr=3.0,
                                             take_profit_atr=6.0))) <= {0}


def test_required_lookback_covers_max_slow():
    assert LOOKBACK_BARS == 30
    assert MaCrossATR.required_data.lookback_bars == 30
    assert MaCrossATR.required_data.symbols == ("XAUUSD", "USTEC")


def test_reference_not_in_research_strategy_library():
    """ops 参考策略隔离：不进策略库（不可开研究卡/进谱系）。"""
    import mt5quant.strategies as strategies
    import mt5quant.strategies.library as library
    for ns in (strategies, library):
        assert not hasattr(ns, "MaCrossATR"), ns
        assert not hasattr(ns, "MaCrossATRAfterSignalChange"), ns
    assert MaCrossATR.__module__ == "mt5quant.backtest.reference"


# ---------------------------------------------------------------- G0 干净

def _random_walk_bars(n=340, seed=20261026):
    rng = np.random.default_rng(seed)
    closes = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    return make_bars(closes.tolist())


def test_g0_truncation_clean():
    bars = _random_walk_bars()
    assert check_no_lookahead(MaCrossATR(), bars, params=P_FAST,
                              probe_points=25) == []


def test_g0_window_invariance_clean():
    bars = _random_walk_bars()
    assert check_window_invariance(MaCrossATR(), bars, P_FAST, probes=40) == []
    assert check_window_invariance_sweep(MaCrossATR(), bars, P_FAST,
                                         span=120) == []


def test_g0_static_scan_clean_and_run_g0_passes():
    assert static_scan(MaCrossATR()) == []
    bars = _random_walk_bars()
    res = run_g0(MaCrossATR(), bars, P_FAST, probes=30, sweep_span=100)
    assert res["passed"] is True, res
    assert res["lookback_bars"] == 30


# ---------------------------------------------------------------- A/B 多笔冒烟

class ZeroNetCosts:
    """A/B 通吃：绝对点差 0.5、零滑点/佣金/隔夜——差异应为精确 0。"""

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.5

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


SPREAD = 0.5


def ohlc_ticks(bars):
    """每根 bar 四笔 tick 恰好复现 OHLC（bid=价、ask=bid+0.5）。"""
    out = []
    for _, row in bars.iterrows():
        t = pd.Timestamp(row["time"])
        for at, price in ((t, row["open"]),
                          (t + pd.Timedelta(minutes=20), row["high"]),
                          (t + pd.Timedelta(minutes=40), row["low"]),
                          (t + pd.Timedelta(hours=1, milliseconds=-1),
                           row["close"])):
            msc = int(at.value // 1_000_000)
            out.append((msc, float(price), float(price) + SPREAD))
    return out


def make_loader(ticks):
    def load(start_msc=None, end_msc=None):
        rows = [t for t in ticks
                if (start_msc is None or t[0] >= start_msc)
                and (end_msc is None or t[0] <= end_msc)]
        return pd.DataFrame(rows, columns=["time_msc", "bid", "ask"])
    return load


def run_reconcile(strategy, bars):
    mod = _load_script()
    params = {"fast": 2, "slow": 4, "stop_loss_atr": strategy.exit_rules.stop_loss_atr,
              "take_profit_atr": strategy.exit_rules.take_profit_atr}
    return mod.reconcile(strategy, bars, make_loader(ohlc_ticks(bars)),
                         ZeroNetCosts(), symbol="XAUUSD", params=params,
                         timeframe="H1", contract_size=1.0, volume_lots=1.0)


def _bars_long_stop_reentry():
    """多头止损 + immediate 重入 + 期末强平（H1，ATR 手算见断言注释）。

    bar 0–14 横盘 100（TR=1，ATR(15)=1+0.5/14）；bar15 收盘 101 金叉（2/4 线）；
    bar16 开盘 101 入场（+点差 0.5 → 101.5），low 恰为 SL=入场−1×ATR(15)；
    bar17 起缓升（信号仍 +1 → immediate 重入；缓升避免 SMA2 收敛于 SMA4
    提前 signal 出场），到 bar20 末根强平。"""
    n = 21
    opens = [100.0] * 15 + [100.0, 101.0] + [101.2, 101.3, 101.4, 101.5]
    closes = [100.0] * 15 + [101.0, 101.2] + [101.3, 101.4, 101.5, 101.6]
    highs = [100.5] * 15 + [101.5, 101.6] + [101.8, 101.9, 102.0, 102.1]
    lows = [99.5] * 15 + [100.4] + [None] + [101.0, 101.1, 101.2, 101.3]
    bars = make_bars(closes, highs=highs, lows=lows, opens=opens)
    atr15 = float(atr_series(bars.iloc[:16]).iloc[15])
    assert atr15 == pytest.approx(1 + 0.5 / 14)          # Wilder 手算锚点
    entry = opens[16] + SPREAD                           # 多头入场=开盘+点差
    bars.loc[16, "low"] = entry - 1.0 * atr15            # low 恰触 SL（无跳越）
    return bars


def test_ab_long_stop_immediate_reentry_end_of_data():
    bars = _bars_long_stop_reentry()
    rep = run_reconcile(MaCrossATR(stop_loss_atr=1.0, take_profit_atr=2.0), bars)
    assert rep.ok, rep.rows.to_string()
    assert rep.summary["n_paired"] == 2
    assert rep.summary["n_only_a"] == rep.summary["n_only_b"] == 0
    r_stop, r_eod = rep.rows.iloc[0], rep.rows.iloc[1]
    assert r_stop["exit_reason_a"] == r_stop["exit_reason_b"] == "stop"
    assert r_eod["exit_reason_a"] == r_eod["exit_reason_b"] == "end_of_data"
    assert (rep.rows["entry_dt_ms"] == 0).all()          # 信号类入场时间一致
    assert r_stop["exit_dt_ms"] == 40 * 60 * 1000        # 止损在 low tick（+40m）
    assert r_eod["exit_dt_ms"] == 3_600_000 - 1          # 收盘前最后一笔 tick
    for col in ("entry_price_diff", "exit_price_diff", "net_diff"):
        assert (rep.rows[col].abs() < 1e-9).all(), rep.rows[col]
    assert rep.rows["row_ok"].all()


def test_ab_after_signal_change_blocks_reentry_after_stop():
    bars = _bars_long_stop_reentry()
    rep = run_reconcile(MaCrossATRAfterSignalChange(stop_loss_atr=1.0,
                                                    take_profit_atr=2.0), bars)
    assert rep.ok, rep.rows.to_string()
    # 保护性出场后信号仍 +1 → 禁同向重入：只剩 1 笔（stop），期末空仓
    assert rep.summary["n_paired"] == 1
    row = rep.rows.iloc[0]
    assert row["exit_reason_a"] == row["exit_reason_b"] == "stop"
    assert rep.trades_a["exit_reason"].tolist() == ["stop"]
    assert rep.trades_b["exit_reason"].tolist() == ["stop"]
    assert row["row_ok"]


def _bars_short_take_profit():
    """空头止盈 + immediate 重入 + 期末强平：bar15 收盘 99 死叉；bar16 开盘
    99 入场空头（Bid−滑点 0），low 使 Ask 恰触 TP=入场−2×ATR(15)；bar17 起
    缓跌（仍 −1 → 重入空头；缓跌避免 SMA 收敛提前 signal 出场）持有到末根。"""
    n = 21
    opens = [100.0] * 15 + [100.0, 99.0] + [97.0, 96.9, 96.8, 96.7]
    closes = [100.0] * 15 + [99.0, 97.0] + [96.9, 96.8, 96.7, 96.6]
    highs = [100.5] * 15 + [100.4, 99.4] + [97.4, 97.3, 97.2, 97.1]
    lows = [99.5] * 15 + [98.9] + [None] + [96.6, 96.5, 96.4, 96.3]
    bars = make_bars(closes, highs=highs, lows=lows, opens=opens)
    atr15 = float(atr_series(bars.iloc[:16]).iloc[15])
    entry = opens[16]                                    # 空头入场=Bid（开盘）
    bars.loc[16, "low"] = (entry - 2.0 * atr15) - SPREAD  # low+点差=Ask 恰触 TP
    return bars


def test_ab_short_take_profit_and_end_of_data():
    bars = _bars_short_take_profit()
    rep = run_reconcile(MaCrossATR(stop_loss_atr=1.0, take_profit_atr=2.0), bars)
    assert rep.ok, rep.rows.to_string()
    assert rep.summary["n_paired"] == 2
    r_tp, r_eod = rep.rows.iloc[0], rep.rows.iloc[1]
    assert r_tp["exit_reason_a"] == r_tp["exit_reason_b"] == "take_profit"
    assert r_eod["exit_reason_a"] == r_eod["exit_reason_b"] == "end_of_data"
    assert rep.trades_a["side"].tolist() == ["short", "short"]
    assert (rep.rows["entry_dt_ms"] == 0).all()
    for col in ("entry_price_diff", "exit_price_diff", "net_diff"):
        assert (rep.rows[col].abs() < 1e-9).all(), rep.rows[col]
    assert rep.rows["row_ok"].all()


def _bars_signal_flip_reverse():
    """反向交叉 signal 出场 + 反手：宽 SL/TP（3/6 ATR）不干预，只测信号路径。

    bar15 金叉→bar16 入场多；bar19 收盘大跌死叉→bar20 开盘 signal 平多并
    反手开空，缓跌持有到末根强平。"""
    opens = [100.0] * 15 + [100.0, 101.0, 102.0, 103.0, 103.0] \
        + [99.0, 98.9, 98.8, 98.7]
    closes = [100.0] * 15 + [101.0, 102.0, 103.0, 103.0, 99.0] \
        + [98.9, 98.8, 98.7, 98.6]
    highs = [100.5] * 15 + [101.4, 102.1, 103.1, 103.1, 103.1] \
        + [99.4, 99.3, 99.2, 99.1]
    lows = [99.5] * 15 + [99.9, 100.9, 101.9, 102.9, 99.0] \
        + [98.6, 98.5, 98.4, 98.3]
    return make_bars(closes, highs=highs, lows=lows, opens=opens)


def test_ab_signal_exit_and_reverse_entry():
    bars = _bars_signal_flip_reverse()
    rep = run_reconcile(MaCrossATR(stop_loss_atr=3.0, take_profit_atr=6.0), bars)
    assert rep.ok, rep.rows.to_string()
    assert rep.summary["n_paired"] == 2
    r_sig, r_eod = rep.rows.iloc[0], rep.rows.iloc[1]
    assert r_sig["exit_reason_a"] == r_sig["exit_reason_b"] == "signal"
    assert r_eod["exit_reason_a"] == r_eod["exit_reason_b"] == "end_of_data"
    assert rep.trades_a["side"].tolist() == ["long", "short"]
    assert rep.trades_b["side"].tolist() == ["long", "short"]
    # signal 出场与入场都发生在 bar 边界 tick：时间差精确 0
    assert (rep.rows["entry_dt_ms"] == 0).all()
    assert r_sig["exit_dt_ms"] == 0
    for col in ("entry_price_diff", "exit_price_diff", "net_diff"):
        assert (rep.rows[col].abs() < 1e-9).all(), rep.rows[col]
    assert rep.rows["row_ok"].all()
