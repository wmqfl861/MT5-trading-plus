"""策略接口测试：参数个数/网格组合上限、validate_params、BuyHold。"""
import pandas as pd
import pytest
from dataclasses import FrozenInstanceError

from mt5quant.strategies import (
    BaseStrategy,
    BuyHold,
    ExitRules,
    ParamSpec,
    RequiredData,
)


class FlatStrategy(BaseStrategy):
    """测试用：恒 fill 信号。"""

    def __init__(self, fill=1):
        self.fill = fill

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        return pd.Series(self.fill, index=bars.index, dtype="int64")


class GridStrategy(BaseStrategy):
    """带参数模式的测试策略。"""

    params_schema = (
        ParamSpec(name="k", type="int", grid=(1, 2, 3)),
        ParamSpec(name="thr", type="float", grid=(0.5, 1.0)),
    )

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        return pd.Series(1, index=bars.index, dtype="int64")


def bars(n=3):
    return pd.DataFrame({
        "time": pd.date_range("2026-01-05", periods=n, freq="h"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


def test_param_specs_are_frozen():
    spec = ParamSpec(name="k", type="int", grid=(1, 2))
    with pytest.raises(FrozenInstanceError):
        spec.name = "x"  # type: ignore[misc]
    rules = ExitRules()
    with pytest.raises(FrozenInstanceError):
        rules.stop_loss_atr = 1.0  # type: ignore[misc]
    req = RequiredData(symbols=("XAUUSD",), timeframe="H1", lookback_bars=10)
    with pytest.raises(FrozenInstanceError):
        req.timeframe = "M1"  # type: ignore[misc]


def test_rejects_more_than_four_params():
    with pytest.raises(ValueError, match="参数"):
        class FiveParams(BaseStrategy):
            params_schema = tuple(
                ParamSpec(name=f"p{i}", type="int", grid=(1,)) for i in range(5)
            )

            def generate_signals(self, bars, params):
                return pd.Series(0, index=bars.index, dtype="int64")


def test_rejects_grid_product_over_100():
    with pytest.raises(ValueError, match="网格"):
        class TooBig(BaseStrategy):
            params_schema = (
                ParamSpec(name="a", type="int", grid=tuple(range(11))),
                ParamSpec(name="b", type="int", grid=tuple(range(10))),
            )

            def generate_signals(self, bars, params):
                return pd.Series(0, index=bars.index, dtype="int64")


def test_grid_product_exactly_100_is_allowed():
    class AtLimit(BaseStrategy):
        params_schema = (
            ParamSpec(name="a", type="int", grid=tuple(range(10))),
            ParamSpec(name="b", type="int", grid=tuple(range(10))),
        )

        def generate_signals(self, bars, params):
            return pd.Series(0, index=bars.index, dtype="int64")

    assert AtLimit().validate_params({"a": 3, "b": 7}) == {"a": 3, "b": 7}


def test_rejects_empty_grid_at_definition():
    with pytest.raises(ValueError, match="网格"):
        class EmptyGrid(BaseStrategy):
            params_schema = (ParamSpec(name="k", type="int", grid=()),)

            def generate_signals(self, bars, params):
                return pd.Series(0, index=bars.index, dtype="int64")


def test_validate_params_rejects_unknown_name():
    with pytest.raises(ValueError, match="未知参数"):
        FlatStrategy().validate_params({"k": 1})


def test_validate_params_rejects_missing_param():
    with pytest.raises(ValueError, match="缺少参数"):
        GridStrategy().validate_params({"k": 1})


def test_validate_params_rejects_off_grid_value():
    with pytest.raises(ValueError, match="越网格"):
        GridStrategy().validate_params({"k": 9, "thr": 0.5})


def test_validate_params_rejects_type_mismatch():
    # int 参数给了 float（1.0 == 1 会骗过 in 判断，必须先做类型检查）
    with pytest.raises(ValueError, match="类型"):
        GridStrategy().validate_params({"k": 1.0, "thr": 0.5})
    # float 参数接受 int 值（1 == 1.0 且数值语义一致）
    assert GridStrategy().validate_params({"k": 1, "thr": 1}) == {"k": 1, "thr": 1}


def test_validate_params_accepts_exact_grid():
    out = GridStrategy().validate_params({"k": 2, "thr": 0.5})
    assert out == {"k": 2, "thr": 0.5}


def test_buy_hold_signals_all_one():
    sig = BuyHold().generate_signals(bars(5), {})
    assert len(sig) == 5
    assert set(sig.unique()) == {1}
    assert list(sig.index) == list(bars(5).index)
    BuyHold().validate_params({})  # 无参数策略接受空参
