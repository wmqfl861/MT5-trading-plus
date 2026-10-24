"""买入持有基准：无参数，从第一根可交易 bar 起恒做多。"""
from __future__ import annotations

import pandas as pd

from mt5quant.strategies.base import BaseStrategy


class BuyHold(BaseStrategy):
    """信号恒 1（bar 0 收盘信号 → bar 1 开盘入场，持有到数据末尾）。"""

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        return pd.Series(1, index=bars.index, dtype="int64")
