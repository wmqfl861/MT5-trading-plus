"""mt5quant.strategies：统一策略接口与策略库。"""
from mt5quant.strategies.base import (
    BaseStrategy,
    ExitRules,
    ParamSpec,
    RequiredData,
)
from mt5quant.strategies.library.buy_hold import BuyHold
from mt5quant.strategies.library.b1_donchian_breakout import DonchianBreakout
from mt5quant.strategies.library.b2_opening_range_breakout import (
    NyOpeningRangeBreakout,
)
from mt5quant.strategies.library.b3_london_breakout import LondonAsianBreakout
from mt5quant.strategies.library.b4_mean_reversion import (
    TrendFilteredMeanReversion,
)
from mt5quant.strategies.library.b5_intraday_momentum import IntradayMomentumNy
from mt5quant.strategies.library.b6_vol_target_long import VolTargetLong

__all__ = [
    "BaseStrategy",
    "BuyHold",
    "DonchianBreakout",
    "ExitRules",
    "IntradayMomentumNy",
    "LondonAsianBreakout",
    "NyOpeningRangeBreakout",
    "ParamSpec",
    "RequiredData",
    "TrendFilteredMeanReversion",
    "VolTargetLong",
]
