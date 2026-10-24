"""成本模型汇总导出 + Costs 聚合体（四方法名与回测引擎侧 Protocol 逐字一致）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .audit import ScopeDecision, cost_heatmap, decide_scope
from .commission import CommissionModel
from .slippage import SlippageModel
from .spread import SpreadModel, SpreadStat
from .swap import SwapModel, SwapRule
from .volatility import atr_series, hourly_stats

__all__ = [
    "SpreadStat", "SpreadModel", "SlippageModel", "CommissionModel",
    "SwapRule", "SwapModel", "atr_series", "hourly_stats",
    "ScopeDecision", "cost_heatmap", "decide_scope", "Costs",
]


@dataclass(frozen=True)
class Costs:
    """把四类成本模型聚成一个对象，方法名与 T6 引擎的 Costs Protocol 一致。"""

    spread: SpreadModel
    slippage_model: SlippageModel
    commission: CommissionModel
    swap: SwapModel

    def spread_p90(self, symbol: str, hour_utc: int) -> float:
        return self.spread.spread_p90(symbol, hour_utc)

    def slippage(self, symbol: str, hour_utc: int) -> float:
        return self.slippage_model.slippage(symbol, hour_utc)

    def commission_per_lot(self, symbol: str) -> float:
        return self.commission.commission_per_lot(symbol)

    def overnight_fee(self, symbol: str, side: str,
                      from_ts: datetime, to_ts: datetime) -> float:
        return self.swap.overnight_fee(symbol, side, from_ts, to_ts)
