"""截断测试工具：前缀信号 vs 全量信号的重叠段必须全等（查前视偏差）。"""
import pandas as pd
import pytest

from mt5quant.strategies import BaseStrategy, BuyHold
from mt5quant.backtest.truncation import check_no_lookahead


class LookaheadShift(BaseStrategy):
    """故意埋前视：用下一根 bar 的收盘决定本 bar 信号（shift(-1)）。"""

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        close = bars["close"]
        return (close.shift(-1) > close).astype("int64").set_axis(bars.index)


class CausalWindow(BaseStrategy):
    """因果对照：只用已收盘数据（过去 3 根均值）。"""

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        close = bars["close"]
        return (close > close.rolling(3).mean()).astype("int64").set_axis(bars.index)


def bars(n=60):
    # 锯齿行情，保证 shift(-1) 策略在多处产生非零信号
    closes = [100.0 + (i % 7) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(
            [pd.Timestamp("2026-01-05 00:00") + pd.Timedelta(hours=i) for i in range(n)]
        ),
        "open": closes, "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes], "close": closes,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


def test_buy_hold_passes():
    assert check_no_lookahead(BuyHold(), bars(), params={}) == []


def test_causal_window_strategy_passes():
    assert check_no_lookahead(CausalWindow(), bars(), params={}) == []


def test_shift_minus_one_lookahead_is_caught():
    n = 60
    df = bars(n)
    bad = check_no_lookahead(LookaheadShift(), df, params={})
    assert bad != [], "埋了 shift(-1) 的策略必须被查出"
    assert all(isinstance(i, int) for i in bad)
    assert bad == sorted(set(bad))  # 去重且升序
    assert all(0 <= i < n for i in bad)
    # 前视只影响"前缀末尾用到不存在的下一根"的位置：任何违规点 i 都必须满足
    # 全量信号在 i 处确实依赖 close[i+1]（close[i+1] > close[i] 才有差异）
    closes = list(df["close"])
    frontier = {c - 1 for c in range(1, n) if closes[c] > closes[c - 1]}
    assert set(bad) <= frontier


def test_probe_points_configurable():
    bad = check_no_lookahead(LookaheadShift(), bars(), params={}, probe_points=2)
    assert bad != []


def test_short_series_returns_empty():
    assert check_no_lookahead(BuyHold(), bars(2), params={}) == []


def test_invalid_params_fail_fast():
    with pytest.raises(ValueError, match="未知参数"):
        check_no_lookahead(BuyHold(), bars(), params={"k": 1})
