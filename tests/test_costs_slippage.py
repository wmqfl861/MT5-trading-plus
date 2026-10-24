"""SlippageModel 手算测试：fraction×p90+fixed、事件小时乘数、恒正、fail-fast。"""
import pandas as pd
import pytest

from mt5quant.costs.slippage import SlippageModel
from mt5quant.costs.spread import SpreadModel


def _spread_model():
    rows = [(f"2026-06-01 10:00:{i:02d}", 0.001) for i in range(5)]      # p90=0.001
    rows += [(f"2026-06-01 11:00:{i:02d}", 0.003) for i in range(5)]     # p90=0.003
    df = pd.DataFrame(
        {
            "time_msc": [pd.Timestamp(t).value // 10**6 for t, _ in rows],
            "bid": [1.0] * len(rows),
            "ask": [1.0 + s for _, s in rows],
        }
    )
    return SpreadModel.from_ticks("EURUSD", df)


def test_fraction_of_p90():
    m = SlippageModel(_spread_model(), spread_fraction=0.1)
    assert m.slippage("EURUSD", 10) == pytest.approx(0.0001)  # 0.1×0.001
    assert m.slippage("EURUSD", 11) == pytest.approx(0.0003)  # 0.1×0.003


def test_fixed_component_added():
    m = SlippageModel(_spread_model(), spread_fraction=0.1, fixed_price=0.00002)
    assert m.slippage("EURUSD", 10) == pytest.approx(0.00012)  # 0.1×0.001+0.00002


def test_event_hour_multiplies():
    base = SlippageModel(_spread_model(), spread_fraction=0.1)
    ev2 = SlippageModel(_spread_model(), spread_fraction=0.1, event_hours=frozenset({10}))
    ev3 = SlippageModel(
        _spread_model(), spread_fraction=0.1,
        event_hours=frozenset({10, 11}), event_multiplier=3.0,
    )
    # 事件小时 ×2；非事件小时不受影响
    assert ev2.slippage("EURUSD", 10) == pytest.approx(2 * base.slippage("EURUSD", 10))
    assert ev2.slippage("EURUSD", 11) == pytest.approx(base.slippage("EURUSD", 11))
    assert ev3.slippage("EURUSD", 10) == pytest.approx(0.0003)  # (0.1×0.001)×3
    assert ev3.slippage("EURUSD", 11) == pytest.approx(0.0009)  # (0.1×0.003)×3


def test_slippage_strictly_positive():
    m = SlippageModel(_spread_model(), spread_fraction=0.1, fixed_price=0.00001)
    for h in (10, 11):
        v = m.slippage("EURUSD", h)
        assert v > 0


def test_fixed_only_model_positive():
    m = SlippageModel(_spread_model(), spread_fraction=0.0, fixed_price=0.00005)
    assert m.slippage("EURUSD", 10) == pytest.approx(0.00005)


def test_zero_or_negative_config_rejected():
    with pytest.raises(ValueError):
        SlippageModel(_spread_model(), spread_fraction=0.0, fixed_price=0.0)
    with pytest.raises(ValueError):
        SlippageModel(_spread_model(), spread_fraction=-0.1)
    with pytest.raises(ValueError):
        SlippageModel(_spread_model(), event_hours=frozenset({24}))


def test_missing_spread_model_raises():
    m = SlippageModel(spread_fraction=0.1)
    with pytest.raises(ValueError):
        m.slippage("EURUSD", 10)


def test_missing_hour_propagates_from_spread_model():
    m = SlippageModel(_spread_model(), spread_fraction=0.1)
    with pytest.raises(ValueError):
        m.slippage("EURUSD", 3)  # 点差模型缺 3 点统计
