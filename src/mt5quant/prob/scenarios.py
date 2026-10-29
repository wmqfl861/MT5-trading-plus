"""T97：点差情景成本包装（标签路径，计划 v2 先决 2）。

ext 段（早年外部数据）触发的标签必须用情景倍数包装的 Costs 跑引擎
（`mt5quant.prob.labels.label_triggers` → engine_a）。**只乘点差**：
批准条款是"早年点差用情景倍数"——ext 段（HistData bid-only）的点差
无从实测，保守按倍数放大（倍数出处 = H 线 robustness.
EARLY_YEARS_SPREAD_FACTOR = 1.5，同源先例；包装写法也沿用其
SpreadOnlyScaledCosts 的"只缩放点差"口径）；滑点/佣金/隔夜费/止损越幅
是执行与经纪侧费用，与数据来源无关，不随点差情景缩放。
倍数 1.0 时为恒等包装（缺省场景 [1.0, 1.0] → 旧卡逐位不变）。
"""
from __future__ import annotations

__all__ = ["ScenarioCosts"]


class ScenarioCosts:
    """包装一个 Costs：spread_p90 × mult，其余分量与属性原样透传。"""

    def __init__(self, base, mult: float):
        if isinstance(mult, bool) or not isinstance(mult, (int, float)) \
                or not mult > 0:
            raise ValueError(f"mult 必须为有限正数，收到 {mult!r}")
        self._base = base
        self.mult = float(mult)

    def spread_p90(self, symbol: str, hour_ny: int,
                   *, price: float | None = None) -> float:
        return self._base.spread_p90(symbol, hour_ny, price=price) * self.mult

    def __getattr__(self, name):
        return getattr(self._base, name)
