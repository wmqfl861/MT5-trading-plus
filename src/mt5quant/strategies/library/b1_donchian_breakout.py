"""B1 唐奇安通道突破（H1，P5 基线）。

语义（信号层，全部只用已收盘 bar）：
- 通道：entry_n 根（= entry_days × 24 根 H1；周末缺 bar 按 bar 数近似，即
  "N 日"是 N×24 根的名义窗口）内最高高点 / 最低低点，**整体右移一根**
  （shift(1)）：突破比较的是 close[i] 与 bars[i-entry_n .. i-1] 的通道，
  本根自身不进通道（用收盘价对已收盘前窗的突破，无前视）。
- 入场：close 突破上沿 → 目标仓位 +1；跌破下沿 → -1；粘性持有。
- 出场：close 跌破 exit_n 根低点通道 → 平多；升破 exit_n 根高点通道 → 平空。
  exit_days < entry_days 由网格保证、运行时再校验（短通道在内、长通道在外，
  反向突破必然先触发同向出场——出场与反手的次序确定）。
- 时间止损在**信号层**实现：入场后 MAX_HOLD_BARS 根仍未被通道出场 → 信号
  归零（引擎下一根开盘平仓）。不用引擎 time_stop_bars 的原因：保护性出场
  后 reentry=immediate 会在信号仍指向时立刻重入，与"时间止损离场"语义相悖。

窗口不变性（铁律 5，lookback_bars = 960 + 720 + 24 的依据）：
任一时刻的非零信号都源于至多 MAX_HOLD_BARS 根前的入场事件，而入场/出场
事件本身只依赖事件 bar 往前 entry_n 根。因此窗口 ≥ entry_n_max + MAX_HOLD
时，截窗重算的状态机必然与全史一致（更早的持仓在窗口内过期，两台状态机
在窗口起点后 MAX_HOLD + entry_n 根内收敛）。参数网格上界放大时必须同步
放大 lookback_bars（见 MAX_ENTRY_DAYS）。

机制（假设卡 H002）：时间序列动量——Moskowitz, Ooi & Pedersen (2012)
*Time Series Momentum*：各类资产过去 1–12 个月收益的方向对后续收益有正预测，
突破 N 日高/低点正是该现象的可交易化；"送钱方"是趋势后期才入场/离场的
慢资金与止损盘。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.base import BaseStrategy, ExitRules, ParamSpec, RequiredData

BARS_PER_DAY = 24          # H1 × 24（名义日；周末休市缺 bar，按 bar 数近似）
MAX_ENTRY_DAYS = 40        # 网格上界：lookback 声明据此计算，勿单独放大网格
MAX_HOLD_BARS = 24 * 30    # 信号层时间止损（30 天）


class DonchianBreakout(BaseStrategy):
    """N 日唐奇安通道突破 + 短通道出场 + ATR 止损（引擎层）。

    参数网格：entry_days (10,20,40) × exit_days (2,4,8) = 9 组；
    引擎出场：stop_loss_atr=3.0（宽止损，先让通道出场主导）。
    """

    params_schema = (
        ParamSpec(name="entry_days", type="int", grid=(10, 20, 40)),
        ParamSpec(name="exit_days", type="int", grid=(2, 4, 8)),
    )
    required_data = RequiredData(
        symbols=("XAUUSD", "BTCUSD", "EURUSD", "USTEC"),
        timeframe="H1",
        lookback_bars=MAX_ENTRY_DAYS * BARS_PER_DAY + MAX_HOLD_BARS + 24,
    )
    exit_rules = ExitRules(stop_loss_atr=3.0)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        entry_n = int(p["entry_days"]) * BARS_PER_DAY
        exit_n = int(p["exit_days"]) * BARS_PER_DAY
        if exit_n >= entry_n:
            raise ValueError(
                f"exit_days({p['exit_days']}) 必须小于 entry_days({p['entry_days']})"
                "——短通道在外会让出场晚于反向突破，次序不再确定")

        high = bars["high"].astype("float64")
        low = bars["low"].astype("float64")
        close = bars["close"].astype("float64")
        # 通道右移一根：close[i] 对比 bars[i-n .. i-1] 的极值（只用已收盘 bar）
        ent_hi = high.rolling(entry_n).max().shift(1).to_numpy()
        ent_lo = low.rolling(entry_n).min().shift(1).to_numpy()
        exit_hi = high.rolling(exit_n).max().shift(1).to_numpy()
        exit_lo = low.rolling(exit_n).min().shift(1).to_numpy()
        c = close.to_numpy()
        up = c > ent_hi        # NaN 比较为 False：回看不足不产生事件
        dn = c < ent_lo
        x_long = c < exit_lo   # 平多
        x_short = c > exit_hi  # 平空

        n = len(bars)
        sig = np.zeros(n, dtype="int64")
        state = 0
        entry_i = -1
        for i in range(n):
            if state == 1 and (x_long[i] or i - entry_i >= MAX_HOLD_BARS):
                state = 0
            elif state == -1 and (x_short[i] or i - entry_i >= MAX_HOLD_BARS):
                state = 0
            if state == 0 and up[i]:
                state, entry_i = 1, i
            elif state == 0 and dn[i]:
                state, entry_i = -1, i
            sig[i] = state
        return pd.Series(sig, index=bars.index, dtype="int64")
