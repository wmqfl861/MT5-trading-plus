"""mt5quant.strategies：统一策略接口与策略库。"""
from mt5quant.strategies.base import (
    BaseStrategy,
    ExitRules,
    ParamSpec,
    RequiredData,
)
from mt5quant.strategies.library.buy_hold import BuyHold

__all__ = ["BaseStrategy", "BuyHold", "ExitRules", "ParamSpec", "RequiredData"]
