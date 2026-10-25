"""B5 纳指日内动量（USTEC M5，P5 基线）。

语义（信号层，全部只用已收盘 bar；NY 本地时间经 zoneinfo 换算）：
- 方向：开盘头半小时（NY 09:30–10:00 开盘的 bar）的首根开盘价 → 末根
  收盘价的收益 r；r > +k×ATR14 → 方向 +1，r < −k×ATR14 → −1，否则 0
  （k=min_ret_atr，0 表示只看符号）。ATR 为简单 TR 滚动均值（过滤尺度，
  非引擎 Wilder ATR），取头半小时末根 bar 的值；k>0 且 ATR 不足（NaN）
  → 当日不交易（fail-closed，不猜）。
- 持仓窗口：收盘本地时刻 ∈ [15:30, 16:00) 的 bar 上信号=方向，其余为 0。
  引擎在 15:30 整点开盘价入场、16:00 整点开盘价离场——恰覆盖尾盘半小时。
  方向在 10:00 即冻结，15:30 使用无前视。

窗口不变性（铁律 5）：全部状态以 NY 日为界，方向只依赖当日头半小时；
lookback_bars = 320 ≥ M5 一日 288 根 + 缓冲，截窗必含完整当日序列。
bar 周期由时间索引最小正差推导（数据孔只会让差变大）。

机制（假设卡 H006）：Gao, Han, Li & Zhou (2018) *Market Intraday Momentum*
——美股/ETF 开盘头半小时收益对尾盘半小时收益有显著正预测（首个与最后
半小时是日内收益最高、信息含量最大的时段；隔夜与日内信息在开盘集中定
价，收盘前的机构再平衡与日内交易者的隔夜风险卸载延续开盘方向）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.base import BaseStrategy, ExitRules, ParamSpec, RequiredData

NY_TZ = "America/New_York"
FIRST_START_MIN = 9 * 60 + 30    # NY 09:30
FIRST_END_MIN = 10 * 60          # NY 10:00：头半小时完成
LAST_START_MIN = 15 * 60 + 30    # NY 15:30：尾盘半小时开始
LAST_END_MIN = 16 * 60           # NY 16:00：尾盘半小时结束
ATR_N = 14                       # 信号层波动尺度（简单 TR 均值）
LOOKBACK_BARS = 320              # ≥ M5 一日 288 根 + 缓冲（窗口不变性）


def _local_parts(bars: pd.DataFrame, tz_name: str):
    """(日键, 开盘本地分钟, 收盘本地分钟) 数组；bar<2 或无正差 → None。"""
    t = pd.to_datetime(bars["time"])
    if len(t) < 2:
        return None
    deltas = t.diff().dropna()
    pos = deltas[deltas > pd.Timedelta(0)]
    if pos.empty:
        return None
    step_min = int(pos.min().total_seconds() // 60)
    lt = t.dt.tz_localize("UTC").dt.tz_convert(tz_name)
    day = (lt.dt.year * 10000 + lt.dt.month * 100 + lt.dt.day).to_numpy()
    open_min = (lt.dt.hour * 60 + lt.dt.minute).to_numpy()
    return day, open_min, open_min + step_min


def _atr_simple(bars: pd.DataFrame) -> np.ndarray:
    """简单 ATR：TR 的 ATR_N 根滚动均值（窗口化统计，区别于引擎 Wilder ATR）。"""
    high = bars["high"].astype("float64").to_numpy()
    low = bars["low"].astype("float64").to_numpy()
    close = bars["close"].astype("float64").to_numpy()
    prev_close = np.concatenate(([np.nan], close[:-1]))
    tr = np.maximum.reduce([
        high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    return pd.Series(tr).rolling(ATR_N).mean().to_numpy()


class IntradayMomentumNy(BaseStrategy):
    """开盘头半小时方向 → 尾盘半小时同向（Gao et al. 2018）。

    参数网格：min_ret_atr (0.0, 0.5, 1.0, 1.5) = 4 组；
    引擎层 2.0×ATR 止损兜底（尾盘闪跌离场）。
    """

    params_schema = (
        ParamSpec(name="min_ret_atr", type="float", grid=(0.0, 0.5, 1.0, 1.5)),
    )
    required_data = RequiredData(symbols=("USTEC",), timeframe="M5",
                                 lookback_bars=LOOKBACK_BARS)
    exit_rules = ExitRules(stop_loss_atr=2.0)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        k = float(p["min_ret_atr"])

        n = len(bars)
        sig = np.zeros(n, dtype="int64")
        parts = _local_parts(bars, NY_TZ)
        if parts is None:
            return pd.Series(sig, index=bars.index)
        day, open_min, close_min = parts
        open_ = bars["open"].astype("float64").to_numpy()
        close = bars["close"].astype("float64").to_numpy()
        atr = _atr_simple(bars)
        in_first = (open_min >= FIRST_START_MIN) & (open_min < FIRST_END_MIN)
        after_first = open_min >= FIRST_END_MIN

        cur_day = int(day[0]) - 1
        first_open = float("nan")
        last_close = float("nan")
        last_atr = float("nan")
        direction = 0            # 0 也表示"当日方向尚未冻结"
        frozen = False
        for i in range(n):
            if int(day[i]) != cur_day:          # 日切：NY 本地日重置
                cur_day = int(day[i])
                first_open = last_close = last_atr = float("nan")
                direction = 0
                frozen = False
            if in_first[i]:
                if np.isnan(first_open):
                    first_open = float(open_[i])
                last_close = float(close[i])    # 头半小时的最新收盘
                last_atr = float(atr[i])
            elif after_first[i] and not frozen:
                frozen = True                   # 头半小时结束：方向当日冻结
                r = last_close - first_open
                if np.isnan(r):
                    direction = 0               # 当日无头半小时数据：不交易
                elif k <= 0.0:
                    direction = 1 if r > 0.0 else (-1 if r < 0.0 else 0)
                elif not np.isnan(last_atr):
                    thr = k * last_atr
                    direction = 1 if r > thr else (-1 if r < -thr else 0)
                else:
                    direction = 0               # k>0 但 ATR 不足：fail-closed
            if (frozen and LAST_START_MIN <= close_min[i] < LAST_END_MIN):
                sig[i] = direction              # 尾盘半小时：同向持有
        return pd.Series(sig, index=bars.index, dtype="int64")
