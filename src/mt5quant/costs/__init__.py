"""成本模型汇总导出 + Costs 聚合体（引擎侧 Protocol 的方法逐字一致）。

成本接口 v2（R1 / 计划 T3）：spread_p90/slippage 均带 keyword-only price
（该笔成交的 bid 基准价）——相对点差模型必须拿到 price（缺失抛 ValueError），
绝对模型忽略。

T36（H2）：新增可选第五方法 stop_overshoot（止损越幅，A-1.6 的 stop 出场
成交口径）。Costs 聚合体带 overshoot_model=None 时返回 0（越幅表缺失＝
不加，保持 A-1.5 行为；构建侧 scripts/build_cost_model.py --overshoot）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .audit import ScopeDecision, cost_heatmap, decide_scope, relative_hourly_stats
from .commission import CommissionModel
from .overshoot import (
    OvershootStat, StopOvershootBuilder, StopOvershootModel,
)
from .slippage import SlippageModel
from .spread import MODES, SAMPLINGS, SpreadModel, SpreadModelBuilder, SpreadStat
from .swap import (
    SWAP_MODE_DISABLED, SWAP_MODE_POINTS, SwapModel, SwapRule,
    charge_nights, mt5_to_py_weekday, per_lot_per_night,
)
from .volatility import atr_series, hourly_stats

__all__ = [
    "SpreadStat", "SpreadModel", "SpreadModelBuilder", "SlippageModel",
    "CommissionModel", "OvershootStat", "StopOvershootModel",
    "StopOvershootBuilder", "SwapRule", "SwapModel", "atr_series",
    "hourly_stats", "MODES", "SAMPLINGS",
    "ScopeDecision", "cost_heatmap", "decide_scope", "relative_hourly_stats",
    "Costs",
    "SWAP_MODE_DISABLED", "SWAP_MODE_POINTS",
    "mt5_to_py_weekday", "per_lot_per_night", "charge_nights",
]


@dataclass(frozen=True)
class Costs:
    """把各类成本模型聚成一个对象，方法名与引擎的 Costs Protocol 一致。"""

    spread: SpreadModel
    slippage_model: SlippageModel
    commission: CommissionModel
    swap: SwapModel
    overshoot_model: StopOvershootModel | None = None   # T36：缺省无越幅表

    def spread_p90(self, symbol: str, hour_ny: int,
                   *, price: float | None = None) -> float:
        """价格单位（hour_ny=纽约本地小时，T20）；price 语义见 SpreadModel.spread_p90。"""
        return self.spread.spread_p90(symbol, hour_ny, price=price)

    def slippage(self, symbol: str, hour_ny: int,
                 *, price: float | None = None) -> float:
        """价格单位（hour_ny=纽约本地小时，T20）；price 语义见 SlippageModel.slippage。"""
        return self.slippage_model.slippage(symbol, hour_ny, price=price)

    def commission_per_lot(self, symbol: str) -> float:
        return self.commission.commission_per_lot(symbol)

    def overnight_fee(self, symbol: str, side: str,
                      from_ts: datetime, to_ts: datetime) -> float:
        return self.swap.overnight_fee(symbol, side, from_ts, to_ts)

    def stop_overshoot(self, symbol: str, hour_ny: int, *,
                       price: float | None = None,
                       level: str = "p50") -> float:
        """T36/A-1.6：该纽约小时止损越幅（价格单位，不利方向）。

        level="p50" 为 A 层 stop 出场成交口径；"p90" 供 G2 敏感性备查。
        未注入越幅模型 → 0.0（越幅表缺失＝不加，与 A-1.5 行为一致）。
        """
        if self.overshoot_model is None:
            return 0.0
        return self.overshoot_model.overshoot(symbol, hour_ny, price=price,
                                              level=level)

