"""G3 walk-forward：滚动训练/测试窗，只在卡的网格内重选参数，记录样本外。"""
import dataclasses

import pandas as pd
import pytest

from mt5quant.backtest.engine_a import run_backtest
from mt5quant.backtest.report import summarize
from mt5quant.strategies import BaseStrategy, BuyHold, ParamSpec
from mt5quant.validation import walkforward as wf
from mt5quant.validation.gates import load_gates
from mt5quant.validation.robustness import BacktestSpec


@pytest.fixture(scope="module")
def cfg():
    base = load_gates()[0]
    return dataclasses.replace(base, walkforward_train_bars=200,
                               walkforward_test_bars=50)


class Zero:
    def spread_p90(self, symbol, hour_utc, *, price=None):
        return 0.0

    def slippage(self, symbol, hour_utc, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


def make_bars(closes, start="2024-01-01"):
    closes = list(map(float, closes))
    times = pd.date_range(start, periods=len(closes), freq="1h")
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "time": times, "open": opens,
        "high": [max(o, c) + 0.2 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.2 for o, c in zip(opens, closes)],
        "close": closes, "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


class SidePicker(BaseStrategy):
    """side=1 全程做多、side=-1 全程做空：网格里只有一个正确答案。"""

    params_schema = (ParamSpec("side", "int", (1, -1)),)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        return pd.Series(params["side"], index=bars.index, dtype="int64")


def spec_of(strategy):
    return BacktestSpec(strategy=strategy, symbol="TST", timeframe="H1",
                        contract_size=1.0)


def test_walkforward_trending_buy_hold_passes(cfg):
    bars = make_bars([100 + 0.3 * i for i in range(350)])
    base = run_backtest(BuyHold(), bars, Zero(), symbol="TST", params={},
                        contract_size=1.0, timeframe="H1")
    out = wf.run_g3(spec_of(BuyHold()), bars, Zero(), grid=[{}],
                    in_sample_sharpe=summarize(base)["sharpe"], cfg=cfg)
    assert out["passed"] is True
    assert len(out["segments"]) == 3            # 200 训练 + 3×50 测试
    assert out["oos_sharpe"] > 0
    for seg in out["segments"]:
        assert seg["test_bars"] == 50
        assert seg["oos_net"] > 0


def test_walkforward_declining_fails(cfg):
    bars = make_bars([100 - 0.3 * i for i in range(350)])
    base = run_backtest(BuyHold(), bars, Zero(), symbol="TST", params={},
                        contract_size=1.0, timeframe="H1")
    out = wf.run_g3(spec_of(BuyHold()), bars, Zero(), grid=[{}],
                    in_sample_sharpe=summarize(base)["sharpe"], cfg=cfg)
    assert out["passed"] is False
    assert out["oos_sharpe"] < 0


def test_walkforward_reselects_within_grid(cfg):
    """只在卡网格内重选：上升趋势里每个训练窗都必须挑 side=1。"""
    bars = make_bars([100 + 0.3 * i for i in range(350)])
    base = run_backtest(SidePicker(), bars, Zero(), symbol="TST",
                        params={"side": 1}, contract_size=1.0, timeframe="H1")
    out = wf.run_g3(spec_of(SidePicker()), bars, Zero(),
                    grid=[{"side": 1}, {"side": -1}],
                    in_sample_sharpe=summarize(base)["sharpe"], cfg=cfg)
    assert out["passed"] is True
    assert all(seg["params"] == {"side": 1} for seg in out["segments"])


def test_walkforward_insufficient_data_fails(cfg):
    bars = make_bars([100.0] * 100)
    out = wf.run_g3(spec_of(BuyHold()), bars, Zero(), grid=[{}],
                    in_sample_sharpe=None, cfg=cfg)
    assert out["passed"] is False
    assert out["segments"] == []
    assert "不足" in out["reason"]
