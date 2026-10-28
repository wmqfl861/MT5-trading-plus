"""T73A 拟合协议（D43/D44）：fittable 缺省 False，fit/with_model 的默认拒绝。"""
import pandas as pd
import pytest

from mt5quant.strategies import BaseStrategy, BuyHold, ExitRules


class PlainStrategy(BaseStrategy):
    def generate_signals(self, bars, params):
        return pd.Series(0, index=bars.index, dtype="int64")


class FittableStrategy(BaseStrategy):
    fittable = True

    def fit(self, bars_by_symbol, costs_by_symbol, params, *, train_end,
            purge_bars):
        return {"schema_version": 1, "fitted": True}

    def with_model(self, model):
        new = FittableStrategy()
        new.model = model
        return new

    def generate_signals(self, bars, params):
        return pd.Series(0, index=bars.index, dtype="int64")


def test_default_not_fittable_and_protocol_rejects():
    assert BaseStrategy.fittable is False
    assert PlainStrategy.fittable is False
    assert BuyHold.fittable is False
    s = PlainStrategy()
    bars = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=3),
                         "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    with pytest.raises(RuntimeError, match="fittable"):
        s.fit({"X": bars}, {}, {}, train_end=None, purge_bars=0)
    with pytest.raises(RuntimeError, match="fittable"):
        s.with_model({})


def test_fittable_protocol_roundtrip():
    s = FittableStrategy()
    assert s.fittable is True
    bars = pd.DataFrame({"time": pd.date_range("2024-01-01", periods=3),
                         "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0})
    model = s.fit({"X": bars}, {}, {}, train_end=pd.Timestamp("2024-01-02"),
                  purge_bars=2)
    assert model == {"schema_version": 1, "fitted": True}
    s2 = s.with_model(model)
    assert s2 is not s and s2.model == model and getattr(s, "model", None) is None
