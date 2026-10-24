"""重采样（M1 → 高周期）与经纪商对账。

- label="left"、closed="left"：桶标签 = 桶首分钟，边界 bar 归右侧新桶
  （与 MT5 "bar 时间 = 开盘时间" 一致）；
- spread 聚合取 min（MQL5 语义：bar 的 spread 字段为 bar 内最小点差）；
- 对账：价格（OHLC）超差即失败；成交量差异只记录不判失败
  （tick 计数口径两边可不同）。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS, _COLUMNS

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last",
        "tick_volume": "sum", "spread": "min", "real_volume": "sum"}


def resample_bars(df_m1: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """M1 → 更高周期；输出列序与 store._COLUMNS 一致、dtype 对齐存储规范。

    桶边界对齐到数据首日 UTC 0 点（origin="start_day"），与 MT5 的
    整点对齐周期一致；空桶自动丢弃。timeframe 必须高于 M1，否则抛错。
    """
    tf = timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知周期 {timeframe!r}")
    seconds = TIMEFRAME_SECONDS[tf]
    if seconds <= TIMEFRAME_SECONDS["M1"]:
        raise ValueError(f"目标周期必须高于 M1，收到 {tf}")

    if df_m1 is None or len(df_m1) == 0:
        out = pd.DataFrame({c: pd.Series(dtype="int64" if c not in
                                          ("time", "open", "high", "low", "close")
                                          else "float64") for c in _COLUMNS})
        out["time"] = pd.Series(dtype="datetime64[ns]")
        return out[_COLUMNS]

    agg = (
        df_m1.set_index("time")
        .resample(pd.Timedelta(seconds=seconds),
                  label="left", closed="left", origin="start_day")
        .agg(_AGG)
        .dropna(subset=["open"])  # 空桶不留行
        .reset_index()
    )
    out = agg[_COLUMNS].copy()
    out["time"] = pd.to_datetime(out["time"]).astype("datetime64[ns]")
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].astype("float64")
    for c in ("tick_volume", "spread", "real_volume"):
        out[c] = out[c].astype("int64")
    return out


@dataclass(frozen=True)
class ResampleCompare:
    """重采样 vs 经纪商原生周期的对账结果。

    - common / only_res / only_broker：按 time 对齐后的三方计数；
    - price_mismatches：[(time, 字段, res 值, broker 值)]，非空即 ok=False；
    - max_volume_diff：公共 bar 上 tick_volume/real_volume 的最大绝对差
      （只记录，不判失败——两边成交量口径可能不同）。
    """

    common: int
    only_res: int
    only_broker: int
    price_mismatches: list
    max_volume_diff: int
    ok: bool


def compare_with_broker(df_res: pd.DataFrame, df_broker: pd.DataFrame,
                        *, atol: float = 1e-9) -> ResampleCompare:
    """逐根对账（按 time 对齐）。spread 语义两边可能不同（min vs 快照），不比较。"""
    res = df_res.set_index("time")
    brk = df_broker.set_index("time")
    res_times, brk_times = set(res.index), set(brk.index)
    common_times = sorted(res_times & brk_times)

    price_mismatches: list = []
    max_volume_diff = 0
    for t in common_times:
        a, b = res.loc[t], brk.loc[t]
        for c in ("open", "high", "low", "close"):
            va, vb = float(a[c]), float(b[c])
            if abs(va - vb) > atol:
                price_mismatches.append((t, c, va, vb))
        for c in ("tick_volume", "real_volume"):
            max_volume_diff = max(max_volume_diff, abs(int(a[c]) - int(b[c])))

    return ResampleCompare(
        common=len(common_times),
        only_res=len(res_times - brk_times),
        only_broker=len(brk_times - res_times),
        price_mismatches=price_mismatches,
        max_volume_diff=int(max_volume_diff),
        ok=not price_mismatches,
    )
