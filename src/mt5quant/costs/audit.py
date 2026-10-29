"""可交易性审计：来回成本 / 小时平均波幅热力图与纳入决策。

absolute 口径（旧版行为，逐位一致）：
来回成本价（价格单位）= spread_p90 + 2×slippage + commission_per_lot/contract_size
（点差每个来回恰一次：bar 按 bid、ask 腿收全幅；滑点开平各一次）；
格值 = 来回成本价 ÷ 该小时 mean_range（平均振幅）。

relative 口径（R1 / 计划 T3，cost_heatmap 按 spread.mode 自动选择）：
格值 = 相对成本 ÷ 相对振幅——
( rel_p90 + 2×rel_slip + 佣金/(合约大小×小时均价) ) ÷ mean((high−low)/close)，
各分量都折成"占价格的比率"，比率不随整体价格水平变化（近期 tick 的点差结构
可以安全外推到价格水平不同的历史）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .volatility import ny_hour


@dataclass(frozen=True)
class ScopeDecision:
    """单个 (品种, 周期, 小时) 的纳入决策。hour 为纽约本地小时（H-10）。"""

    symbol: str
    timeframe: str
    hour: int
    ratio: float
    include: bool


def relative_hourly_stats(bars: pd.DataFrame) -> pd.DataFrame:
    """按纽约本地小时聚合的相对量：mean_rel_range=mean((high−low)/close)、
    mean_close=mean(close)。

    index 名 hour_ny（H-10：与点差/滑点模型的纽约小时分桶同时区），只含有
    K 线的小时；close ≤ 0/NaN 的行产出 NaN
    （由 cost_heatmap 判无数据）。相对口径热力图用它把成本与振幅都折成比率。
    """
    missing = [c for c in ("time", "high", "low", "close") if c not in bars.columns]
    if missing:
        raise ValueError(f"bars 帧缺列 {missing}")
    df = bars.sort_values("time")
    hour = ny_hour(df["time"])
    high = df["high"].astype("float64")
    low = df["low"].astype("float64")
    close = df["close"].astype("float64")
    rel_range = (high - low) / close
    out = pd.DataFrame({
        "mean_rel_range": rel_range.groupby(hour).mean(),
        "mean_close": close.groupby(hour).mean(),
    }).sort_index()
    out.index.name = "hour_ny"
    return out


def cost_heatmap(spread, slip, commission, contract_sizes: dict[str, float],
                 vol: dict[tuple[str, str], pd.DataFrame],
                 symbols, timeframes) -> pd.DataFrame:
    """行=symbol×timeframe（按给定顺序），列=hour_ny（vol 出现过的小时并集）。

    口径由 spread.mode 决定（无 mode 属性按 absolute）：计算式见模块 docstring。
    relative 口径下 vol 帧须含 mean_rel_range 与 mean_close 列
    （relative_hourly_stats 产出），且不支持固定滑点（绝对价格单位）。

    小时口径 = 纽约本地小时（H-10）：vol 帧由 hourly_stats /
    relative_hourly_stats 按纽约小时聚合，点差/滑点模型也按纽约小时查询——
    同一整数 h 同一含义（旧版波幅按 UTC 小时、点差按纽约小时，混用）。

    数据缺失（该小时无点差统计 / 无波幅 / 波幅 ≤ 0）→ 格为 NaN，由
    decide_scope 判排除；contract_sizes 缺品种或非正、vol 缺 (symbol, timeframe)
    或缺所需列属调用方错误 → 直接抛。
    """
    mode = str(getattr(spread, "mode", "absolute"))
    if mode not in ("relative", "absolute"):
        raise ValueError(f"未知点差口径 {mode!r}（合法：relative/absolute）")
    if mode == "relative":
        fixed = float(getattr(slip, "fixed_price", 0.0))
        if fixed != 0.0:
            raise ValueError(
                "relative 口径不支持固定滑点（绝对价格单位），请置 fixed_price=0")
    symbols = list(symbols)
    timeframes = [str(tf) for tf in timeframes]
    need = ["mean_range"] + (["mean_rel_range", "mean_close"]
                             if mode == "relative" else [])
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
            missing_cols = [c for c in need if c not in vol[(s, tf)].columns]
            if missing_cols:
                raise ValueError(
                    f"vol[({s}, {tf})] 缺 {missing_cols} 列（{mode} 口径需要）")
    hours = sorted({int(h) for s in symbols for tf in timeframes
                    for h in vol[(s, tf)].index})
    idx = pd.MultiIndex.from_tuples([(s, tf) for s in symbols for tf in timeframes],
                                    names=["symbol", "timeframe"])
    arr = np.full((len(idx), len(hours)), np.nan)
    for i, (s, tf) in enumerate(idx):
        stats = vol[(s, tf)]
        for j, h in enumerate(hours):
            if mode == "relative":
                mr = float(stats["mean_rel_range"].get(h, np.nan))
                mc = float(stats["mean_close"].get(h, np.nan))
                if not (mr > 0) or not (mc > 0) or not math.isfinite(mc):
                    continue  # NaN 或非正 → 无有效相对振幅/均价
                try:
                    sp = spread.spread_p90(s, h, price=1.0)   # rel_p90×1 = 比率
                    sl = slip.slippage(s, h, price=1.0)
                except ValueError:  # 该小时/该品种无点差统计（如缺 tick）→ 留 NaN
                    continue
                comm_rel = commission.commission_per_lot(s) / (contract_sizes[s] * mc)
                arr[i, j] = (sp + 2.0 * sl + comm_rel) / mr
            else:
                mr = float(stats["mean_range"].get(h, np.nan))
                if not mr > 0:  # NaN 或非正 → 无有效波幅
                    continue
                try:
                    sp = spread.spread_p90(s, h)
                    sl = slip.slippage(s, h)
                except ValueError:  # 该小时/该品种无点差统计（如缺 tick）→ 留 NaN
                    continue
                comm_price = commission.commission_per_lot(s) / contract_sizes[s]
                arr[i, j] = (sp + 2.0 * sl + comm_price) / mr
    hm = pd.DataFrame(arr, index=idx, columns=hours)
    hm.columns.name = "hour_ny"
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
