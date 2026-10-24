"""可交易性审计：来回成本 / 小时平均波幅热力图与纳入决策。

来回成本价（价格单位）= 2×half_spread + 2×slippage + commission_per_lot/contract_size；
格值 = 来回成本价 ÷ 该小时 mean_range（平均振幅）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ScopeDecision:
    """单个 (品种, 周期, 小时) 的纳入决策。"""

    symbol: str
    timeframe: str
    hour: int
    ratio: float
    include: bool


def cost_heatmap(spread, slip, commission, contract_sizes: dict[str, float],
                 vol: dict[tuple[str, str], pd.DataFrame],
                 symbols, timeframes) -> pd.DataFrame:
    """行=symbol×timeframe（按给定顺序），列=hour_utc（vol 出现过的小时并集）。

    数据缺失（该小时无点差统计 / 无波幅 / 波幅 ≤ 0）→ 格为 NaN，由
    decide_scope 判排除；contract_sizes 缺品种或非正、vol 缺 (symbol, timeframe)
    或缺 mean_range 列属调用方错误 → 直接抛。
    """
    symbols = list(symbols)
    timeframes = [str(tf) for tf in timeframes]
    for s in symbols:
        cs = contract_sizes.get(s)
        if cs is None:
            raise KeyError(f"contract_sizes 缺 {s}")
        if cs <= 0:
            raise ValueError(f"{s} contract_size 须为正，收到 {cs}")
    for s in symbols:
        for tf in timeframes:
            if (s, tf) not in vol:
                raise KeyError(f"vol 缺 ({s}, {tf}) 的波幅统计")
            if "mean_range" not in vol[(s, tf)].columns:
                raise ValueError(f"vol[({s}, {tf})] 缺 mean_range 列")
    hours = sorted({int(h) for s in symbols for tf in timeframes
                    for h in vol[(s, tf)].index})
    idx = pd.MultiIndex.from_tuples([(s, tf) for s in symbols for tf in timeframes],
                                    names=["symbol", "timeframe"])
    arr = np.full((len(idx), len(hours)), np.nan)
    for i, (s, tf) in enumerate(idx):
        stats = vol[(s, tf)]
        for j, h in enumerate(hours):
            mr = float(stats["mean_range"].get(h, np.nan))
            if not mr > 0:  # NaN 或非正 → 无有效波幅
                continue
            try:
                hs = spread.half_spread(s, h)
                sl = slip.slippage(s, h)
            except ValueError:  # 该小时/该品种无点差统计（如缺 tick）→ 留 NaN
                continue
            comm_price = commission.commission_per_lot(s) / contract_sizes[s]
            arr[i, j] = (2.0 * hs + 2.0 * sl + comm_price) / mr
    hm = pd.DataFrame(arr, index=idx, columns=hours)
    hm.columns.name = "hour_utc"
    return hm


def decide_scope(hm: pd.DataFrame, threshold: float = 0.12) -> list[ScopeDecision]:
    """ratio ≤ threshold 纳入（含边界）；NaN（无数据）→ 排除。"""
    out: list[ScopeDecision] = []
    for symbol, tf in hm.index:
        for hour in hm.columns:
            v = float(hm.loc[(symbol, tf), hour])
            include = (not math.isnan(v)) and v <= threshold
            out.append(ScopeDecision(symbol, tf, int(hour), v, include))
    return out
