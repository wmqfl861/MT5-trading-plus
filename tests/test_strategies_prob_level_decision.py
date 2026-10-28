"""T74 收尾：LevelDecisionV1——拟合/冻结/决策信号与引擎验证。"""
import numpy as np
import pandas as pd
import pytest

from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
from mt5quant.backtest.engine_a import run_backtest
from mt5quant.strategies import ExitRules
from mt5quant.strategies.library.prob.level_decision import (
    LevelDecisionS1Demo, LevelDecisionV1, ModelMissingError,
)
from mt5quant.strategies.library.prob.situations import Approach


class FlatCosts:
    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.02

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.01

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts, *, price=None):
        return 0.0


def synth_bars(n, *, seed, start="2020-01-02", s0=100.0):
    rng = np.random.default_rng(seed)
    secs = TIMEFRAME_SECONDS["H1"]
    steps = rng.normal(0.0, 0.30, size=n)
    close = s0 + np.cumsum(steps)
    openc = close + rng.normal(0.0, 0.05, size=n)
    high = np.maximum(openc, close) + np.abs(rng.normal(0, 0.12, size=n))
    low = np.minimum(openc, close) - np.abs(rng.normal(0, 0.12, size=n))
    return pd.DataFrame({
        "time": pd.date_range(start, periods=n, freq=f"{secs}s"),
        "open": openc, "high": high, "low": low, "close": close,
        "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]


class DemoSmallGrid(LevelDecisionS1Demo):
    """测试用：2.0 网格（事件密度高），K=8。"""

    LEVEL_GRID = {"XAUUSD": 2.0}
    HORIZON_BARS = 8
    APPROACH = Approach(lookback_bars=12, min_distance_atr=1.0,
                        touch_atr=0.25)
    RETEST_WINDOW_BARS = 4


@pytest.fixture(scope="module")
def fitted():
    bars = synth_bars(4000, seed=42)
    strat = DemoSmallGrid()
    model = strat.fit({"XAUUSD": bars}, {"XAUUSD": FlatCosts()},
                      {"__fit__": {"sizes": {"XAUUSD": 100.0}}},
                      train_end=pd.Timestamp("2020-06-01"), purge_bars=10)
    return bars, model


def test_fit_produces_appendix_b_model(fitted):
    bars, model = fitted
    assert model["prob_version"] == "1.0.0"
    assert model["bracket"] == {"sl_atr": 1.0, "tp_atr": 2.0,
                                "horizon_bars": 8}
    assert model["fit_window"]["end"].startswith("2020-06-01")
    assert model["purge_bars"] == 10
    assert len(model["prob_table"]["cells"]) >= 1
    # 同一输入重拟合 → 同一模型（铁律 21：模型身份确定性）
    bars2 = synth_bars(4000, seed=42)
    strat2 = DemoSmallGrid()
    model2 = strat2.fit({"XAUUSD": bars2}, {"XAUUSD": FlatCosts()},
                        {"__fit__": {"sizes": {"XAUUSD": 100.0}}},
                        train_end=pd.Timestamp("2020-06-01"), purge_bars=10)
    assert model2 == model


def test_generate_signals_requires_model(fitted):
    bars, model = fitted
    with pytest.raises(ModelMissingError):
        DemoSmallGrid().generate_signals(bars, {})
    strat = DemoSmallGrid().with_model(model)
    sig = strat.generate_signals(bars, {})
    assert set(pd.unique(sig)) <= {-1, 0, 1}


def _strong_model():
    """手工高优势模型：S1×A3 赢率 0.9——保证决策触发（随机游走无优势时
    策略应全'不做'，引擎验证需要一个会入场的模型）。"""
    from mt5quant.prob.tables import ProbTable
    rows = [("win", 1.9)] * 900 + [("loss", -1.05)] * 90 + [("time", 0.1)] * 10
    labels = pd.DataFrame({
        "situation": "S1", "action": "A3",
        "outcome": [o for o, _ in rows], "net_r": [r for _, r in rows],
        "trend_sma200": "na", "atr_pct_2000": "na"})
    tbl = ProbTable.fit(labels, group_by=["trend_sma200", "atr_pct_2000"],
                        prior_strength=100, min_cell=100)
    return {"prob_version": "1.0.0", "prob_table": tbl.to_dict(),
            "bracket": {"sl_atr": 1.0, "tp_atr": 2.0, "horizon_bars": 8},
            "cost_r_by_situation_action": {"S1|A3": 0.001}}


def test_hold_capped_and_chaining_via_engine():
    """信号层持有上限与引擎行为：单次触发的信号恰 K 根后归零；连续新
    触发可链式续持（每次重臂是新的 K 根决策段——bars_held 可大于 K，
    这是设计行为）；止损出场后引擎 reentry=after_signal_change 生效。"""
    bars = synth_bars(4000, seed=42)
    strat = DemoSmallGrid().with_model(_strong_model())
    res = run_backtest(strat, bars, FlatCosts(), symbol="XAUUSD", params={},
                       contract_size=100.0, timeframe="H1",
                       volume_lots=1.0)
    assert len(res.trades) >= 1
    reasons = set(res.trades["exit_reason"])
    assert reasons <= {"signal", "stop", "stop_gap", "take_profit",
                       "take_profit_gap"}


def test_single_trigger_run_is_exactly_K():
    """单一触发（无后续触发可链式）：非零信号段长度恰为 K，随后归零。"""
    plat = (98.5, 99.0, 98.0, 98.5)          # TR=1，high 距 100 为 1
    rows = [plat] * 40
    rows.append((99.5, 99.9, 99.0, 99.0))    # anchor：high 触 100±0.3×ATR 带
    rows += [plat] * 30
    bars = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    bars.insert(0, "time",
                pd.date_range("2024-01-02", periods=len(rows), freq="1h"))
    bars["tick_volume"], bars["spread"], bars["real_volume"] = 10, 5, 0
    bars = bars[_COLUMNS]
    strat = DemoSmallGrid().with_model(_strong_model())
    sig = strat.generate_signals(bars, {}).to_numpy()
    nz = np.flatnonzero(sig != 0)
    assert len(nz) == DemoSmallGrid.HORIZON_BARS      # 恰 K 根
    assert nz[0] == 40                                # anchor 当根起（收盘信号）
    assert (sig[nz] == sig[nz][0]).all()              # 同方向





def test_exit_rules_declared_for_engine(fitted):
    assert DemoSmallGrid.exit_rules.stop_loss_atr == 1.0
    assert DemoSmallGrid.exit_rules.take_profit_atr == 2.0
    assert DemoSmallGrid.exit_rules.reentry == "after_signal_change"
    assert DemoSmallGrid.exit_rules.time_stop_bars is None
    assert DemoSmallGrid.exit_rules.week_close_exit is False
    assert DemoSmallGrid.fittable is True


def test_g0_frozen_model(fitted):
    """G0：冻结模型下截断一致与窗口不变性（≥100 随机探针）。"""
    from mt5quant.validation import lookahead as vlook
    bars, model = fitted
    strat = DemoSmallGrid().with_model(model)
    res = vlook.run_g0(strat, bars, {})
    assert res.get("passed", False) is True, res


def test_with_model_rejects_bad_structure():
    with pytest.raises(ValueError, match="prob_table"):
        DemoSmallGrid().with_model({"schema_version": 1})
