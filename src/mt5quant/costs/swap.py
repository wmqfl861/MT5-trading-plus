"""隔夜费模型：计费夜计数 + 三倍日，恒 ≤ 0（成本方向）。

口径（与经纪商对账时逐项核对）：
- 每日 charge_hour_utc（默认 21:00 UTC）为计费时刻；持仓区间 (from_ts, to_ts]
  跨过的每个计费时刻记 1 夜（from 恰在计费时刻不记、to 恰在计费时刻要记）；
- 计费时刻所在日期的星期 ∈ triple_weekdays（默认周三——FX 把周六日两夜并入
  周三夜）→ 该夜 ×3；
- **恒返回 ≤ 0**：费率本身为正（罕见）也按成本方向取负——回测宁可高估成本。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd


@dataclass(frozen=True)
class SwapRule:
    """单品种隔夜规则（weekday 约定 Monday=0 … Sunday=6）。"""

    long_per_day: float
    short_per_day: float
    triple_weekdays: frozenset = frozenset({2})  # 周三
    charge_hour_utc: int = 21

    def __post_init__(self):
        if not 0 <= self.charge_hour_utc <= 23:
            raise ValueError(f"charge_hour_utc 须 0..23，收到 {self.charge_hour_utc}")
        object.__setattr__(self, "triple_weekdays", frozenset(self.triple_weekdays))
        for wd in self.triple_weekdays:
            if not 0 <= wd <= 6:
                raise ValueError(f"triple_weekdays 须 0..6（Monday=0），收到 {wd}")


def _as_naive_utc(ts) -> pd.Timestamp:
    """aware → 转 UTC 后贴 naive 标签（库内既定约定）。"""
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t


class SwapModel:
    """品种 → SwapRule 映射；未配置品种直接抛（fail-fast，不静默按 0 算）。"""

    def __init__(self, per_symbol: dict[str, SwapRule] | None = None):
        self._per_symbol: dict[str, SwapRule] = dict(per_symbol or {})

    def rule(self, symbol: str) -> SwapRule:
        try:
            return self._per_symbol[symbol]
        except KeyError:
            raise KeyError(
                f"{symbol} 未配置 SwapRule（已配置：{sorted(self._per_symbol)}）") from None

    def overnight_fee(self, symbol: str, side: str,
                      from_ts: datetime, to_ts: datetime) -> float:
        """结构化接口：计价货币/手，恒 ≤ 0（正费率也按成本方向取负）。"""
        rule_ = self.rule(symbol)
        if side not in ("long", "short"):
            raise ValueError(f"side 须为 'long'/'short'，收到 {side!r}")
        f = _as_naive_utc(from_ts)
        t = _as_naive_utc(to_ts)
        if t < f:
            raise ValueError(f"to_ts({to_ts}) 早于 from_ts({from_ts})")
        per_day = rule_.long_per_day if side == "long" else rule_.short_per_day
        nights = self._charge_nights(rule_, f, t)
        return -abs(nights * per_day)

    @staticmethod
    def _charge_nights(rule_: SwapRule, f: pd.Timestamp, t: pd.Timestamp) -> int:
        """(f, t] 内跨过的计费夜数（三倍日记 3）。"""
        total = 0
        day = f.date()
        end = t.date()
        while day <= end:
            m = pd.Timestamp(day.year, day.month, day.day, rule_.charge_hour_utc)
            if f < m <= t:
                total += 3 if day.weekday() in rule_.triple_weekdays else 1
            day += timedelta(days=1)
        return total
