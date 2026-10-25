"""B6 波动率目标多头（XAUUSD H1，P5 基线）。

语义（信号层，全部只用已收盘 bar）：
- 已实现波动率：vol[i] = close.pct_change().rolling(vol_n).std()（窗口化
  统计，无全史量）。
- 风险开关：vol ≤ vol_target → 目标仓位 +1（持有多头）；否则 0（空仓）。
  信号接口只支持 {-1,0,1}，经典波动率目标组合的连续仓位缩放
  （target_vol / realized_vol）在此退化为**二元开关**——低波动吃趋势、
  高波动躲回撤的近似；如实证结果与 BuyHold 差异可观，后续可在引擎层
  引入手数缩放再开新卡。

窗口不变性（铁律 5）：每根信号只依赖最近 vol_n 根收盘（网格上界 120），
lookback_bars = 128 时截窗重算与全史一致。

机制（假设卡 H007）：Moreira & Muir (2017) *Volatility-Managed Portfolios*
——风险敞口与已实现波动负相关的管理能提升夏普：波动聚集下，高波动期
的下一期风险溢价并不按波动同比例补偿（杠杆约束与投资者风险承受的
凸性），降低高波动期敞口改善组合形状。本策略只保留其二元近似。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.base import BaseStrategy, ExitRules, ParamSpec, RequiredData

MAX_VOL_N = 120         # 网格上界（lookback 声明依据）


class VolTargetLong(BaseStrategy):
    """已实现波动 ≤ 目标时做多、超过时空仓（BuyHold 的波动率管理变体）。

    参数网格：vol_n (20,60,120) × vol_target (0.001,0.0015,0.002,0.003)
    = 12 组；无保护性出场（基准性质，同 BuyHold）。
    """

    params_schema = (
        ParamSpec(name="vol_n", type="int", grid=(20, 60, 120)),
        ParamSpec(name="vol_target", type="float",
                  grid=(0.001, 0.0015, 0.002, 0.003)),
    )
    required_data = RequiredData(symbols=("XAUUSD",), timeframe="H1",
                                 lookback_bars=MAX_VOL_N + 8)
    exit_rules = ExitRules()

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        vol_n = int(p["vol_n"])
        vol_target = float(p["vol_target"])

        close = bars["close"].astype("float64")
        ret = close.pct_change()
        vol = ret.rolling(vol_n).std()            # 窗口化统计（无全史量）
        invested = (vol <= vol_target).to_numpy()  # NaN 比较 → False：预热期空仓
        return pd.Series(invested.astype("int64"), index=bars.index)
