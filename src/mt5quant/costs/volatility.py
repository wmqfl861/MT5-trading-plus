"""波动率统计：Wilder ATR（与 mt5mcp.indicators 同法）与按小时聚合。

小时聚合口径 = 纽约本地小时（H-10：与 SpreadModel/SlippageModel 的
BUCKET_TZ 分桶同时区；此前按 UTC hour 聚合、点差按纽约 hour 查询，
同一整数 h 两种含义）。
"""
from __future__ import annotations

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS

from .spread import BUCKET_TZ


def atr_series(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR：种子=前 period 个 TR 均值，ewm(alpha=1/n, adjust=False, ignore_na=True)。

    与 mt5mcp.indicators.atr 完全同法（本包不依赖 mt5mcp，算法逐行对齐，
    测试钉死两边输出全等）；不足 period 根时全 NaN。bars 帧须含
    high/low/close 列（store._COLUMNS 结构），index 须唯一。
    """
    highs = bars["high"].astype("float64")
    lows = bars["low"].astype("float64")
    closes = bars["close"].astype("float64")
    prev_close = closes.shift(1)
    tr = pd.concat(
        [highs - lows, (highs - prev_close).abs(), (lows - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    vals = tr.copy()
    valid = vals.dropna()
    if len(valid) < period:
        return vals * float("nan")
    seed_idx = valid.index[period - 1]
    seed = valid.iloc[:period].mean()
    for i in range(period):
        vals[valid.index[i]] = float("nan")
    vals[seed_idx] = float(seed)
    return vals.ewm(alpha=1 / period, adjust=False, ignore_na=True).mean()


def ny_hour(times: pd.Series) -> pd.Series:
    """K 线时间逐条转纽约本地小时（H-10：与点差模型 BUCKET_TZ 同口径）。

    naive 时间按库内约定视为 UTC；带时区的直接转换。逐条转换保证
    DST 切换日前后落在正确的桶（不能按固定偏移加减小时）。
    """
    t = pd.to_datetime(times)
    if t.dt.tz is None:
        t = t.dt.tz_localize("UTC")
    return t.dt.tz_convert(BUCKET_TZ).dt.hour


def hourly_stats(bars: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """按纽约本地小时聚合：mean_range=平均振幅（high−low），atr14_median=ATR14 中位。

    index 名 hour_ny（H-10：与点差/滑点模型的纽约小时分桶同时区；旧版
    按 UTC hour 聚合、点差按纽约 hour 查询，同一整数 h 两种含义），只含有
    K 线的小时；ATR 归属 bar 自身（开盘时刻）所在小时，不足 14 根 → NaN。
    timeframe 须是 store.TIMEFRAME_SECONDS 认可的周期。
    bars 须为已收盘 K 线（只算已收盘是全局约定，由调用方保证）。
    """
    tf = str(timeframe).upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知周期 {timeframe!r}（合法：{sorted(TIMEFRAME_SECONDS)}）")
    missing = [c for c in ("time", "high", "low", "close") if c not in bars.columns]
    if missing:
        raise ValueError(f"bars 帧缺列 {missing}")
    df = bars.sort_values("time")
    hour = ny_hour(df["time"])
    mean_range = (df["high"].astype("float64")
                  - df["low"].astype("float64")).groupby(hour).mean()
    atr_med = atr_series(df, 14).groupby(hour).median()
    out = pd.DataFrame({"mean_range": mean_range, "atr14_median": atr_med}).sort_index()
    out.index.name = "hour_ny"
    return out
