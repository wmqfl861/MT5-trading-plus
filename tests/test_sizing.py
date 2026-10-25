"""T23/M2 共享定手数：RiskSizing + lots_for_risk + 快照 vol 上下限读取。

计划条目（失败测试先行）：
- XAUUSD（合约 100）、资金 10,000、风险 0.5%、止损距离 5.0 → 0.10 手；
- 向下取整到步长；低于 vol_min 返回 0.0（跳过）；上限封顶 vol_max；
- 严格校验（负值/零步长等拒绝）；
- basis="equity" 随权益变化（引擎级，见本文件底部集成用例）；
- 引擎 sizing 与 volume_lots 互斥（在 test_backtest_engine_a/b.py）。
"""
import json
from datetime import timedelta

import pandas as pd
import pytest

from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
from mt5quant.strategies import BaseStrategy, ExitRules
from mt5quant.sizing import (
    RiskSizing,
    load_volume_limits,
    lots_for_risk,
)

# XAUUSD 快照口径（data/specs/latest.json）：合约 100，0.01 步长
XAU = dict(contract_size=100.0, vol_min=0.01, vol_step=0.01, vol_max=200.0)


# ---------------------------------------------------------------- lots_for_risk

def test_plan_case_xauusd_10k_half_percent_stop5():
    """计划逐条：10,000 × 0.5% = 50 风险；每手风险 5.0×100=500 → 0.10 手。"""
    assert lots_for_risk(10_000.0, 0.005, 5.0, **XAU) == pytest.approx(0.10)


def test_floors_to_volume_step():
    """49.995 风险额 / 500 每手 = 0.09999 → 向下取整到 0.09（宁可少不可多）。"""
    assert lots_for_risk(9_999.0, 0.005, 5.0, **XAU) == pytest.approx(0.09)


def test_below_volume_min_returns_zero():
    """风险额不够开最小手数 → 0.0（跳过该入场）；恰好一步不丢。"""
    # 90×0.005=0.45 风险额 / 500 每手 = 0.0009 手 → 低于 0.01 → 0.0
    assert lots_for_risk(90.0, 0.005, 5.0, **XAU) == pytest.approx(0.0)
    # 1000×0.005=5.0 风险额 → 恰好 0.01 手（边界不丢）
    assert lots_for_risk(1_000.0, 0.005, 5.0, **XAU) == pytest.approx(0.01)


def test_caps_at_volume_max():
    """手数上限封顶 vol_max（资金再大也不超品种上限）。"""
    assert lots_for_risk(10_000_000.0, 0.01, 5.0, **XAU) == pytest.approx(200.0)


def test_lots_for_risk_rejects_bad_inputs():
    bad = [
        (dict(capital=-1.0, risk_fraction=0.005, stop_distance=5.0)),   # 负资金
        (dict(capital=0.0, risk_fraction=0.005, stop_distance=5.0)),    # 零资金
        (dict(capital=1e4, risk_fraction=0.0, stop_distance=5.0)),      # 零风险
        (dict(capital=1e4, risk_fraction=-0.005, stop_distance=5.0)),   # 负风险
        (dict(capital=1e4, risk_fraction=0.005, stop_distance=0.0)),    # 零止损距离
        (dict(capital=1e4, risk_fraction=0.005, stop_distance=-5.0)),   # 负止损距离
        (dict(capital=1e4, risk_fraction=0.005, stop_distance=float("nan"))),
        (dict(capital=float("inf"), risk_fraction=0.005, stop_distance=5.0)),
    ]
    for kw in bad:
        with pytest.raises(ValueError):
            lots_for_risk(kw["capital"], kw["risk_fraction"], kw["stop_distance"],
                          **XAU)
    # vol 三元组非法：零步长 / 零最小 / 上限低于最小 / 负合约大小
    for vol in (dict(contract_size=100.0, vol_min=0.01, vol_step=0.0, vol_max=1.0),
                dict(contract_size=100.0, vol_min=0.0, vol_step=0.01, vol_max=1.0),
                dict(contract_size=100.0, vol_min=0.5, vol_step=0.01, vol_max=0.1),
                dict(contract_size=0.0, vol_min=0.01, vol_step=0.01, vol_max=1.0)):
        with pytest.raises(ValueError):
            lots_for_risk(10_000.0, 0.005, 5.0, **vol)


# ---------------------------------------------------------------- RiskSizing

def test_risk_sizing_defaults_and_validation():
    s = RiskSizing(risk_fraction=0.005)
    assert s.basis == "initial"
    assert s.fallback_stop_atr == 1.0
    with pytest.raises(ValueError, match="basis"):
        RiskSizing(risk_fraction=0.005, basis="compounding")
    with pytest.raises(ValueError, match="risk_fraction"):
        RiskSizing(risk_fraction=0.0)
    with pytest.raises(ValueError, match="risk_fraction"):
        RiskSizing(risk_fraction=-0.1)
    with pytest.raises(ValueError, match="fallback_stop_atr"):
        RiskSizing(risk_fraction=0.005, fallback_stop_atr=0.0)
    with pytest.raises(ValueError, match="risk_fraction"):
        RiskSizing(risk_fraction=float("nan"))


# ------------------------------------------------------- 快照 vol 上下限读取

def test_load_volume_limits_reads_specs_snapshot(tmp_path):
    (tmp_path / "specs").mkdir(parents=True)
    (tmp_path / "specs" / "latest.json").write_text(json.dumps({
        "XAUUSD": {"volume_min": 0.01, "volume_step": 0.01, "volume_max": 200.0},
    }), encoding="utf-8")
    assert load_volume_limits(str(tmp_path), "XAUUSD") == (0.01, 0.01, 200.0)


def test_load_volume_limits_missing_data_is_an_error(tmp_path):
    # 缺文件
    with pytest.raises(ValueError, match="snapshot_specs"):
        load_volume_limits(str(tmp_path), "XAUUSD")
    # 缺品种
    (tmp_path / "specs").mkdir(parents=True)
    (tmp_path / "specs" / "latest.json").write_text(
        json.dumps({"BTCUSD": {"volume_min": 0.01}}), encoding="utf-8")
    with pytest.raises(ValueError, match="XAUUSD"):
        load_volume_limits(str(tmp_path), "XAUUSD")
    # 品种在但缺字段（旧快照）
    (tmp_path / "specs" / "latest.json").write_text(
        json.dumps({"XAUUSD": {"contract_size": 100.0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="volume_"):
        load_volume_limits(str(tmp_path), "XAUUSD")


# ---------------------------------------------------------------- 引擎集成

def _flat_bars(n=40, *, high=100.5, low=99.5, close=100.0, openc=100.0):
    """恒定波幅 H1：TR 恒 1.0 → ATR14 从 bar13 起恒 1.0。"""
    times = [pd.Timestamp("2026-01-05 10:00") + timedelta(
        seconds=TIMEFRAME_SECONDS["H1"] * i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


class ScriptStrategy(BaseStrategy):
    """按位置取脚本信号（与引擎测试同款；本文件独立定义避免跨测试模块 import）。"""

    def __init__(self, script, fill=0, exit_rules=None):
        self._script = list(script)
        self._fill = fill
        self.exit_rules = exit_rules or ExitRules()

    def generate_signals(self, bars, params):
        vals = [self._script[i] if i < len(self._script) else self._fill
                for i in range(len(bars))]
        return pd.Series(vals, index=bars.index, dtype="int64")


class FlatCosts:
    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.5

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.1

    def commission_per_lot(self, symbol):
        return 1.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


def _two_episode_script(n=40, first_at=14, second_at=26):
    """两段单 bar 信号（bar14、bar26）→ bar15、bar27 各入场一次，
    次根 bar 由信号归零离场（出场与手数计算无关，只看两笔的手数）。"""
    sig = [0] * n
    sig[first_at] = 1
    sig[second_at] = 1
    return sig


def test_engine_a_equity_basis_sizes_down_after_loss():
    """basis="equity"：第一笔亏损后权益下降 → 第二次入场手数变小；
    basis="initial"（默认）两次相同（不复利，便于横向比较）。"""
    from mt5quant.backtest.engine_a import run_backtest

    bars = _flat_bars(40)
    strat = ScriptStrategy(_two_episode_script(),
                           exit_rules=ExitRules(time_stop_bars=1))
    common = dict(symbol="XAUUSD", params={}, contract_size=100.0,
                  timeframe="H1", initial_capital=10_000.0,
                  vol_min=0.01, vol_step=0.01, vol_max=200.0)
    res_init = run_backtest(strat, bars, FlatCosts(), volume_lots=None,
                            sizing=RiskSizing(risk_fraction=0.005,
                                              basis="initial"), **common)
    res_eq = run_backtest(strat, bars, FlatCosts(), volume_lots=None,
                          sizing=RiskSizing(risk_fraction=0.005,
                                            basis="equity"), **common)
    lots_init = res_init.trades["volume_lots"].tolist()
    lots_eq = res_eq.trades["volume_lots"].tolist()
    assert len(lots_init) == len(lots_eq) == 2
    # initial：两次都是 floor(50/(1.0×ATR=1.0×100)/0.01)×0.01 = 0.50
    assert lots_init[0] == pytest.approx(0.50)
    assert lots_init[1] == pytest.approx(0.50)
    # equity：第一笔吃点差+滑点+佣金净亏 → 权益 < 10000 → 第二笔 ≤ 0.49
    assert lots_eq[0] == pytest.approx(0.50)
    assert lots_eq[1] < lots_init[1] - 0.005   # 明确变小，不是浮点噪声
    # 手数随权益如实变化：第二笔风险额 = 权益×0.005 < 50
    eq_after = 10_000.0 + float(res_eq.trades.iloc[0]["net"])
    assert lots_eq[1] == pytest.approx(
        lots_for_risk(eq_after, 0.005, 1.0, **XAU))


def test_engine_a_zero_lots_skips_entry_and_summary_counts():
    """手数低于 vol_min → 不开仓；每个入场尝试计入 summary.skipped_zero_lots。"""
    from mt5quant.backtest.engine_a import run_backtest
    from mt5quant.backtest.report import summarize

    bars = _flat_bars(30)
    sig = [0] * 14 + [1] * 16          # bar14 起信号为 1 直到末尾
    strat = ScriptStrategy(sig)
    res = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1",
                       volume_lots=None, initial_capital=150.0,   # 风险 0.75 → 0 手
                       sizing=RiskSizing(risk_fraction=0.005),
                       vol_min=0.01, vol_step=0.01, vol_max=200.0)
    assert len(res.trades) == 0
    assert res.skipped_zero_lots == 15  # bar15..29 每 bar 一次入场尝试
    s = summarize(res, cost_version="v0")
    assert s["skipped_zero_lots"] == 15
    # 固定手数模式恒 0（字段存在且如实）
    res_fixed = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD",
                             params={}, contract_size=100.0, timeframe="H1",
                             volume_lots=1.0, initial_capital=10_000.0)
    assert res_fixed.skipped_zero_lots == 0
    assert summarize(res_fixed)["skipped_zero_lots"] == 0


def test_robustness_run_engine_passes_sizing_through():
    """G2/G3 复算沿用同一 sizing 口径：BacktestSpec 带 sizing 时 run_engine
    按风险定手数（与直接调引擎结果逐位一致）。"""
    from mt5quant.validation.robustness import BacktestSpec, run_engine

    bars = _flat_bars(30)
    sig = [0] * 14 + [1] * 16
    strat = ScriptStrategy(sig)
    spec = BacktestSpec(strategy=strat, symbol="XAUUSD", timeframe="H1",
                        contract_size=100.0, volume_lots=None,
                        initial_capital=10_000.0,
                        sizing=RiskSizing(risk_fraction=0.005),
                        vol_min=0.01, vol_step=0.01, vol_max=200.0)
    out = run_engine(spec, bars, FlatCosts(), {})
    assert len(out.trades) == 1
    assert out.trades.iloc[0]["volume_lots"] == pytest.approx(0.50)
    # spec 层互斥：volume_lots 与 sizing 同给 → 拒绝
    with pytest.raises(ValueError, match="二选一"):
        BacktestSpec(strategy=strat, symbol="XAUUSD", timeframe="H1",
                     contract_size=100.0, volume_lots=1.0,
                     sizing=RiskSizing(risk_fraction=0.005),
                     vol_min=0.01, vol_step=0.01, vol_max=200.0)
    # sizing 模式缺 vol 上下限 → 拒绝
    with pytest.raises(ValueError, match="vol_"):
        BacktestSpec(strategy=strat, symbol="XAUUSD", timeframe="H1",
                     contract_size=100.0, volume_lots=None,
                     sizing=RiskSizing(risk_fraction=0.005))


def test_cross_symbol_check_uses_per_symbol_volume_bounds():
    """G2 跨品种子检查（T23 透传链）：sizing 模式下各品种用各自 vol 三元组
    定手数（XAUUSD 0.01 步长 → 0.50 手；步长 0.1 的品种 → 0.5 手）；缺
    品种的三元组 → ValueError（绝不借用别的品种的上下限）。"""
    from mt5quant.validation import robustness as vrob
    from mt5quant.validation import gates as vgate

    cfg, _ = vgate.load_gates()
    bars = _flat_bars(30)
    sig = [0] * 14 + [1] * 15
    strat = ScriptStrategy(sig)
    vols = {"XAUUSD": (0.01, 0.01, 200.0), "TST2": (0.01, 0.1, 100.0)}
    bars_by = {"XAUUSD": bars, "TST2": bars}
    costs_by = {"XAUUSD": FlatCosts(), "TST2": FlatCosts()}
    out = vrob.cross_symbol_check(
        strat, bars_by, costs_by, {"XAUUSD": 100.0, "TST2": 100.0},
        {}, "H1", None, 10_000.0, cfg,
        sizing=RiskSizing(risk_fraction=0.005), vols_by_symbol=vols)
    assert out["applicable"] is True
    # 两品种都按各自步长定到 0.5 手 → 同一净利（同一 bars/成本占位）
    assert out["net_by_symbol"]["XAUUSD"] == pytest.approx(
        out["net_by_symbol"]["TST2"])
    vols.pop("TST2")
    with pytest.raises(ValueError, match="TST2"):
        vrob.cross_symbol_check(
            strat, bars_by, costs_by, {"XAUUSD": 100.0, "TST2": 100.0},
            {}, "H1", None, 10_000.0, cfg,
            sizing=RiskSizing(risk_fraction=0.005), vols_by_symbol=vols)
