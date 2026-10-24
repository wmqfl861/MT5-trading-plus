"""成本模型汇总导出 + Costs 聚合体（四方法名与回测引擎侧 Protocol 逐字一致）。

成本接口 v2（R1 / 计划 T3）：spread_p90/slippage 均带 keyword-only price
（该笔成交的 bid 基准价）——相对点差模型必须拿到 price（缺失抛 ValueError），
绝对模型忽略。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .audit import ScopeDecision, cost_heatmap, decide_scope, relative_hourly_stats
from .commission import CommissionModel
from .slippage import SlippageModel
from .spread import MODES, SpreadModel, SpreadStat
from .swap import (
    SWAP_MODE_DISABLED, SWAP_MODE_POINTS, SwapModel, SwapRule,
    charge_nights, mt5_to_py_weekday, per_lot_per_night,
)
from .volatility import atr_series, hourly_stats

__all__ = [
    "SpreadStat", "SpreadModel", "SlippageModel", "CommissionModel",
    "SwapRule", "SwapModel", "atr_series", "hourly_stats", "MODES",
    "ScopeDecision", "cost_heatmap", "decide_scope", "relative_hourly_stats",
    "Costs",
    "SWAP_MODE_DISABLED", "SWAP_MODE_POINTS",
    "mt5_to_py_weekday", "per_lot_per_night", "charge_nights",
]


@dataclass(frozen=True)
class Costs:
    """把四类成本模型聚成一个对象，方法名与 T6 引擎的 Costs Protocol 一致。"""

    spread: SpreadModel
    slippage_model: SlippageModel
    commission: CommissionModel
    swap: SwapModel

    def spread_p90(self, symbol: str, hour_utc: int,
                   *, price: float | None = None) -> float:
        """价格单位；price 语义见 SpreadModel.spread_p90（相对模型必需）。"""
        return self.spread.spread_p90(symbol, hour_utc, price=price)

    def slippage(self, symbol: str, hour_utc: int,
                 *, price: float | None = None) -> float:
        """价格单位；price 语义见 SlippageModel.slippage。"""
        return self.slippage_model.slippage(symbol, hour_utc, price=price)

    def commission_per_lot(self, symbol: str) -> float:
        return self.commission.commission_per_lot(symbol)

    def overnight_fee(self, symbol: str, side: str,
                      from_ts: datetime, to_ts: datetime) -> float:
        return self.swap.overnight_fee(symbol, side, from_ts, to_ts)

