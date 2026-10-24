"""CommissionModel 测试：默认值/按品种覆盖/负值拒绝；来回口径一次性给出。"""
import pytest

from mt5quant.costs.commission import CommissionModel


def test_default_applied():
    m = CommissionModel(default_per_lot=6.0)
    assert m.commission_per_lot("EURUSD") == 6.0
    assert m.commission_per_lot("XAUUSD") == 6.0


def test_per_symbol_override():
    m = CommissionModel(default_per_lot=6.0, per_symbol={"BTCUSD": 8.5})
    assert m.commission_per_lot("BTCUSD") == 8.5
    assert m.commission_per_lot("EURUSD") == 6.0  # 未覆盖的走默认


def test_round_trip_charged_once_semantics():
    # 口径：返回值即开+平合计（计价货币/手，来回），调用方不得再 ×2
    # （热力图测试 test_heatmap_hand_computed_both_symbols 钉死只加一次）
    m = CommissionModel(default_per_lot=0.0, per_symbol={"XAUUSD": 6.0})
    assert m.commission_per_lot("XAUUSD") == 6.0  # 3.0 开 + 3.0 平，一次给出
    assert m.commission_per_lot("EURUSD") == 0.0


def test_negative_rejected():
    with pytest.raises(ValueError):
        CommissionModel(default_per_lot=-1.0)
    with pytest.raises(ValueError):
        CommissionModel(per_symbol={"XAUUSD": -0.1})
