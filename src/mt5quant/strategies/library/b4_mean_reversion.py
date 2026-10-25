"""B4 带趋势过滤的均值回归（M15/H1，P5 基线）。

语义（信号层，全部只用已收盘 bar；窗口化统计，无全史量）：
- z-score：z[i] = (close[i] − rolling(z_n).mean) / rolling(z_n).std。
- 入场（只顺大趋势逆小波动）：
  z < −z_entry 且 close > SMA(trend_n) → 目标仓位 +1（上升趋势中买深度回落）；
  z > +z_entry 且 close < SMA(trend_n) → −1（下降趋势中卖冲高）。
- 出场（信号归零，引擎下一根开盘平仓）：
  z 回归中值（多头 z ≥ 0 / 空头 z ≤ 0）；或趋势过滤失效
  （多头 close < SMA / 空头 close > SMA）；或持有满 MAX_HOLD_BARS 根
  （信号层时间止损，避免 z 长期不回归的死仓）。
  次序保证：反向入场条件蕴含同向出场条件（z>z_entry ⇒ z≥0；close 穉
  SMA 即过滤失效），出场先于反向入场，状态机无歧义。
- 引擎层另有 2.0×ATR 止损兜底（深度回落演变为破位趋势时离场）。

窗口不变性（铁律 5）：任一非零信号源于至多 MAX_HOLD_BARS 根前的入场，
而入场/出场事件只依赖事件 bar 往前 max(z_n, trend_n) 根（网格上界 200）。
lookback_bars = 200 + 96 + 24 = 320 时截窗重算与全史一致（论证同 B1）。

机制（假设卡 H005）：流动性提供者在短期超调处挂限价单赚取回摆，但裸
逆势接刀在动量冲击下亏损；SMA 趋势过滤把对手限定为"趋势中的获利回吐
与止损潮"——送钱方是追涨杀跌的市价单与执行受限的强制平仓盘（见卡内
文献）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.base import BaseStrategy, ExitRules, ParamSpec, RequiredData

MAX_Z_N = 80          # 网格上界（lookback 声明依据）
MAX_TREND_N = 200     # 网格上界（lookback 声明依据）
MAX_HOLD_BARS = 96    # 信号层时间止损（M15 一日 / H1 四日）


class TrendFilteredMeanReversion(BaseStrategy):
    """z-score 极值反转 + SMA 趋势过滤 + 2.0×ATR 止损。

    参数网格：z_n (20,40,80) × z_entry (2.0,2.5,3.0) × trend_n (50,100,200)
    = 27 组。
    """

    params_schema = (
        ParamSpec(name="z_n", type="int", grid=(20, 40, 80)),
        ParamSpec(name="z_entry", type="float", grid=(2.0, 2.5, 3.0)),
        ParamSpec(name="trend_n", type="int", grid=(50, 100, 200)),
    )
    required_data = RequiredData(
        symbols=("XAUUSD", "BTCUSD", "EURUSD", "USTEC"),
        timeframe="M15",
        lookback_bars=MAX_TREND_N + MAX_HOLD_BARS + 24,
    )
    exit_rules = ExitRules(stop_loss_atr=2.0)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        z_n = int(p["z_n"])
        z_entry = float(p["z_entry"])
        trend_n = int(p["trend_n"])

        close = bars["close"].astype("float64")
        mean = close.rolling(z_n).mean()
        sd = close.rolling(z_n).std()
        z = (close - mean) / sd                  # 窗口化 z-score（无全史量）
        sma = close.rolling(trend_n).mean()

        long_entry = ((z < -z_entry) & (close > sma)).to_numpy()
        short_entry = ((z > z_entry) & (close < sma)).to_numpy()
        long_exit = ((z >= 0) | (close < sma)).to_numpy()
        short_exit = ((z <= 0) | (close > sma)).to_numpy()
        # NaN 比较 → False：回看不足不产生事件

        n = len(bars)
        sig = np.zeros(n, dtype="int64")
        state = 0
        entry_i = -1
        for i in range(n):
            if state == 1 and (long_exit[i] or i - entry_i >= MAX_HOLD_BARS):
                state = 0
            elif state == -1 and (short_exit[i] or i - entry_i >= MAX_HOLD_BARS):
                state = 0
            if state == 0 and long_entry[i]:
                state, entry_i = 1, i
            elif state == 0 and short_entry[i]:
                state, entry_i = -1, i
            sig[i] = state
        return pd.Series(sig, index=bars.index, dtype="int64")
