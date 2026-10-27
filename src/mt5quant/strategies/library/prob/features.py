"""特征 v1（T74，蓝图 P 线 §2.4）：概率表的最多 2 个分组特征。

- trend_sma200：收盘价相对 SMA(200) 的上/下（等于 SMA 归 above，确定性）；
- atr_pct_2000：ATR14 在最近 2,000 根中的分位，以 0.5 分高/低两档。

两者都只用有界窗口的已收盘 bar；窗口未满 → None（该触发不进细粒度格，
由概率表回退到父级）。本模块属策略库（D45：研究角色可写）。
"""
from __future__ import annotations

import pandas as pd

SMA_WINDOW = 200
ATR_RANK_WINDOW = 2000
DEFAULT_SPLIT = 0.5


def trend_sma200(bars: pd.DataFrame, *, window: int = SMA_WINDOW) -> pd.Series:
    """"above" / "below" / None（SMA 未满窗口）。"""
    close = bars["close"].astype("float64")
    sma = close.rolling(window).mean()
    off = close - sma
    return pd.Series(
        [None if pd.isna(v) else ("above" if v >= 0 else "below")
         for v in off], index=bars.index, dtype="object")


def atr_pct_2000(atr: pd.Series, *, window: int = ATR_RANK_WINDOW,
                 split: float = DEFAULT_SPLIT) -> pd.Series:
    """"high" / "low" / None（窗口未满）。分位 = 当前 ATR 在窗内的滚动排名比例。"""
    if not (0.0 < split < 1.0):
        raise ValueError(f"split 须在 (0,1) 开区间：{split!r}")
    pct = atr.rolling(window).rank(pct=True)
    return pd.Series(
        [None if pd.isna(v) else ("high" if v >= split else "low")
         for v in pct], index=atr.index, dtype="object")
