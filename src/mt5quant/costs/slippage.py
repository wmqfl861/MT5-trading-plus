"""滑点模型：点差比例 + 固定值，事件小时按乘数放大。

口径：返回**不利方向幅值**（恒 > 0）——多单视为买贵、空单视为卖便宜；
引擎在开、平两侧各扣一次（来回共 2×）。
单位：spread_fraction 部分随注入的 SpreadModel 口径（relative 模型随 price
缩放、缺 price 抛 ValueError；absolute 模型为固定价格单位）；fixed_price 恒为
绝对价格单位。
"""
from __future__ import annotations

from .spread import SpreadModel


class SlippageModel:
    """slippage = (spread_fraction×p90(hour,price) + fixed_price) × (事件小时乘数)。

    p90(hour,price) 来自注入的 SpreadModel（与 spread_p90 同源，口径一致）。
    """

    def __init__(self, spread: SpreadModel | None = None, *,
                 spread_fraction: float = 0.1,
                 fixed_price: float = 0.0,
                 event_hours: frozenset[int] = frozenset(),
                 event_multiplier: float = 2.0):
        if spread_fraction < 0 or fixed_price < 0:
            raise ValueError("spread_fraction/fixed_price 不能为负")
        # 显式双零（零滑点）是合法配置：零点差品种/研究者有意忽略滑点时用；
        # 报告侧应如实呈现 cost_version，而非在此处强行拦截
        if event_multiplier <= 0:
            raise ValueError("event_multiplier 须为正")
        for h in event_hours:
            if not 0 <= int(h) <= 23:
                raise ValueError(f"event_hours 含非法小时 {h}（须 0..23）")
        self.spread = spread
        self.spread_fraction = float(spread_fraction)
        self.fixed_price = float(fixed_price)
        self.event_hours = frozenset(int(h) for h in event_hours)
        self.event_multiplier = float(event_multiplier)

    def slippage(self, symbol: str, hour_utc: int,
                 *, price: float | None = None) -> float:
        """结构化接口：价格单位，恒为正（不利方向幅值）。

        price（该笔成交的 bid 基准价）透传给 SpreadModel.spread_p90——
        relative 点差模型必须提供，缺失由其如实抛 ValueError。
        """
        if self.spread is None:
            raise ValueError("未注入 SpreadModel，无法按点差比例估计滑点")
        p90 = self.spread.spread_p90(symbol, hour_utc, price=price)
        v = self.spread_fraction * p90 + self.fixed_price
        if int(hour_utc) in self.event_hours:
            v *= self.event_multiplier
        if v < 0:
            raise ValueError(
                f"{symbol} 小时 {hour_utc} 滑点计算为负（p90={p90}），拒绝有利方向成本")
        # v==0 合法：零点差品种 + 零固定滑点的如实配置（恒正约束改为非负）
        return float(v)

