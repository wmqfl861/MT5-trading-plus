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


# ---- T5（R4）：从 calibrated.json 构造 ----

CALIBRATED_SAMPLE = {
    "version": 1,
    "symbols": {
        "EURUSD": {"commission": {"per_lot_in": 30.0, "per_lot_out": 20.0,
                                  "round_trip_per_lot": 50.0}},
        "XAUUSD": {"commission": {"per_lot_in": 3.0, "per_lot_out": 3.0,
                                  "round_trip_per_lot": 6.0}},
        # 无成交（commission 为空）的品种不配置，走默认
        "USTEC": {"commission": {}},
    },
}


def test_from_calibrated_maps_round_trip_per_lot():
    m = CommissionModel.from_calibrated(CALIBRATED_SAMPLE)
    assert m.commission_per_lot("EURUSD") == 50.0  # 实测来回（开 30 + 平 20）
    assert m.commission_per_lot("XAUUSD") == 6.0
    assert m.commission_per_lot("USTEC") == 0.0    # 缺实测 → 默认 0
    assert m.commission_per_lot("BTCUSD") == 0.0   # 不在文件里 → 默认 0


def test_from_calibrated_default_overridable():
    m = CommissionModel.from_calibrated(CALIBRATED_SAMPLE, default_per_lot=7.0)
    assert m.commission_per_lot("USTEC") == 7.0
    assert m.commission_per_lot("EURUSD") == 50.0


def test_from_calibrated_empty_rejects():
    # 空 calibrated.json（没有任何品种）按缺数据报错，不静默全 0
    with pytest.raises(ValueError, match="品种"):
        CommissionModel.from_calibrated({"version": 1, "symbols": {}})
    with pytest.raises(ValueError, match="calibrated"):
        CommissionModel.from_calibrated({})
