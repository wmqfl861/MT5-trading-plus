"""点差模型：tick 实测 ask−bid，按 UTC 小时聚合全周。

口径：
- 只按列名 time_msc（epoch 毫秒）/bid/ask 读帧，不依赖 mt5data.ticks 模块；
- epoch 毫秒直读为 UTC，先落到 weekday×hour，再按小时聚合全周
  （最终统计键=hour_utc 0..23）；
- 分位用线性插值（numpy 默认），与 pandas quantile 一致；
- half_spread 取该小时 p90（保守估计）：引擎在开、平两侧各扣一次（共 2×）。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SpreadStat:
    """单小时点差统计（价格单位）。"""

    median: float
    p90: float
    p99: float
    n: int


class SpreadModel:
    """多品种点差统计：{symbol: {hour_utc: SpreadStat}}。

    单品种模型可直接 stat(hour)/rollover_hours()；多品种（merge 后）
    须显式传 symbol。
    """

    def __init__(self, per_symbol: dict[str, dict[int, SpreadStat]] | None = None,
                 overall_median: dict[str, float] | None = None):
        self._per_symbol: dict[str, dict[int, SpreadStat]] = {
            s: dict(h) for s, h in (per_symbol or {}).items()
        }
        self._overall_median: dict[str, float] = dict(overall_median or {})

    # ---- 构建 ----

    @classmethod
    def from_ticks(cls, symbol: str, ticks_df: pd.DataFrame) -> "SpreadModel":
        """从 tick 帧实测点差统计（spread=ask−bid，价格单位）。"""
        missing = [c for c in ("time_msc", "bid", "ask") if c not in ticks_df.columns]
        if missing:
            raise ValueError(f"tick 帧缺列 {missing}（只需 time_msc/bid/ask）")
        if len(ticks_df) == 0:
            raise ValueError(f"{symbol} tick 数据为空")
        spread = (ticks_df["ask"].to_numpy(dtype="float64")
                  - ticks_df["bid"].to_numpy(dtype="float64"))
        hour = pd.to_datetime(ticks_df["time_msc"], unit="ms").dt.hour.to_numpy()
        stats: dict[int, SpreadStat] = {}
        for h in np.unique(hour):
            v = spread[hour == h]
            stats[int(h)] = SpreadStat(
                median=float(np.quantile(v, 0.50)),
                p90=float(np.quantile(v, 0.90)),
                p99=float(np.quantile(v, 0.99)),
                n=int(v.size),
            )
        overall = float(np.quantile(spread, 0.50))
        return cls({symbol: stats}, {symbol: overall})

    def merge(self, *others: "SpreadModel") -> "SpreadModel":
        """合并多品种模型（品种重复即抛）。"""
        per = {s: dict(h) for s, h in self._per_symbol.items()}
        om = dict(self._overall_median)
        for o in others:
            for s, h in o._per_symbol.items():
                if s in per:
                    raise ValueError(f"品种 {s} 重复，不能合并")
                per[s] = dict(h)
                om[s] = o._overall_median[s]
        return SpreadModel(per, om)

    # ---- 查询 ----

    def _resolve(self, symbol: str | None) -> str:
        if symbol is not None:
            if symbol not in self._per_symbol:
                raise ValueError(
                    f"{symbol} 不在点差模型中（品种：{sorted(self._per_symbol)}）")
            return symbol
        if len(self._per_symbol) == 1:
            return next(iter(self._per_symbol))
        raise ValueError(
            f"点差模型含 {len(self._per_symbol)} 个品种，须显式指定 symbol"
            f"（{sorted(self._per_symbol)}）")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._per_symbol))

    def stat(self, hour: int, *, symbol: str | None = None) -> SpreadStat:
        """该小时统计；多品种模型须显式 symbol。"""
        sym = self._resolve(symbol)
        h = int(hour)
        table = self._per_symbol[sym]
        if h not in table:
            raise ValueError(f"{sym} 小时 {h} 无点差统计（tick 覆盖缺失？）")
        return table[h]

    def half_spread(self, symbol: str, hour_utc: int) -> float:
        """结构化接口：价格单位，取该小时 p90（保守；引擎每侧各扣一次）。"""
        return float(self.stat(hour_utc, symbol=symbol).p90)

    def rollover_hours(self, symbol: str | None = None) -> tuple[int, ...]:
        """结算/换日风险时段：p99 > 3×全体中位的整点（UTC）。"""
        sym = self._resolve(symbol)
        base = 3.0 * self._overall_median[sym]
        return tuple(sorted(h for h, st in self._per_symbol[sym].items() if st.p99 > base))

    def overall_median(self, symbol: str | None = None) -> float:
        """该品种全部 tick 点差的中位数（rollover 判定基准）。"""
        return float(self._overall_median[self._resolve(symbol)])

    # ---- 持久化 ----

    def to_dict(self) -> dict:
        return {
            "per_symbol": {
                s: {str(h): asdict(st) for h, st in hours.items()}
                for s, hours in self._per_symbol.items()
            },
            "overall_median": dict(self._overall_median),
        }

    def save(self, path: str) -> None:
        """原子写 JSON（临时文件 + os.replace，读者不会看到半截文件）。"""
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "SpreadModel":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        per = {
            s: {int(h): SpreadStat(**st) for h, st in hours.items()}
            for s, hours in d["per_symbol"].items()
        }
        return cls(per, {s: float(v) for s, v in d["overall_median"].items()})
