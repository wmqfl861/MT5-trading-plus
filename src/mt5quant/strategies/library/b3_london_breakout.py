"""B3 伦敦时段突破亚盘区间（EURUSD/XAUUSD M15，P5 基线）。

语义（信号层，全部只用已收盘 bar；库内 naive-UTC 服务器时间，伦敦本地
时间经 zoneinfo 换算，DST 自动处理——BST 与 GMT 的 08:00 对应不同 UTC 时刻）：
- 亚盘区间：伦敦本地日 [00:00, 08:00) 内开盘的 bar 的最高高点/最低低点
  （M15 为 32 根）；开盘于 07:45 的 bar 收于 08:00，仍属亚盘（信息在
  伦敦开盘前已知）。
- 入场：伦敦 08:00 后首根收盘 > 亚盘高点 → 目标仓位 +1；< 亚盘低点 → -1；
  收盘回到区间内粘性维持原方向（对侧收盘反手）。
- 出场：bar 收盘本地时刻达到 exit_hour_ldn → 信号归零（引擎恰在该整点
  开盘价平仓）；引擎层另有 1.0×ATR 止损。
- 过滤：亚盘宽度 < min_range_atr × ATR14（简单 TR 均值，仅作过滤尺度，
  非引擎 Wilder ATR）的"窄亚盘日"不交易，资格在当日首个盘后 bar 冻结。

窗口不变性（铁律 5）：状态以伦敦日为界；当天信号只依赖当天 bar + 14 根
ATR 回看；lookback_bars = 128 ≥ M15 一日 96 根 + 缓冲，截窗必含完整当日
序列。bar 周期由时间索引最小正差推导（数据孔只会让差变大）。

机制（假设卡 H004）：亚盘（东京时段）流动性薄、信息流少，价格在窄幅
均衡；伦敦开盘注入大额指令流（欧洲定盘/对冲盘），亚盘高低点成为亚盘
参与者的心理参照与止损聚集处，方向性指令流突破后延续——经典"伦敦突破"
现象（见卡内文献）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.base import BaseStrategy, ExitRules, ParamSpec, RequiredData

LDN_TZ = "Europe/London"
LDN_OPEN_MIN = 8 * 60         # 伦敦本地 08:00 开盘
ATR_N = 14                    # 信号层波动尺度（简单 TR 均值）
LOOKBACK_BARS = 128           # ≥ M15 一日 96 根 + 缓冲（窗口不变性）


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


class LondonAsianBreakout(BaseStrategy):
    """伦敦 08:00 破亚盘区间，exit_hour 整点收盘离场 + 1.0×ATR 止损。

    参数网格：exit_hour_ldn (11,12) × min_range_atr (0.0,0.5,1.0) = 6 组。
    """

    params_schema = (
        ParamSpec(name="exit_hour_ldn", type="int", grid=(11, 12)),
        ParamSpec(name="min_range_atr", type="float", grid=(0.0, 0.5, 1.0)),
    )
    required_data = RequiredData(symbols=("EURUSD", "XAUUSD"), timeframe="M15",
                                 lookback_bars=LOOKBACK_BARS)
    exit_rules = ExitRules(stop_loss_atr=1.0)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        exit_min = int(p["exit_hour_ldn"]) * 60
        k = float(p["min_range_atr"])

        n = len(bars)
        sig = np.zeros(n, dtype="int64")
        parts = _local_parts(bars, LDN_TZ)
        if parts is None:
            return pd.Series(sig, index=bars.index)
        day, open_min, close_min = parts
        high = bars["high"].astype("float64").to_numpy()
        low = bars["low"].astype("float64").to_numpy()
        close = bars["close"].astype("float64").to_numpy()
        atr = _atr_simple(bars)
        in_asian = open_min < LDN_OPEN_MIN
        after = open_min >= LDN_OPEN_MIN

        state = 0
        cur_day = int(day[0]) - 1
        a_hi = a_lo = float("nan")
        frozen = False        # 当日资格是否已在首个盘后 bar 冻结
        day_ok = False
        for i in range(n):
            if int(day[i]) != cur_day:            # 日切：伦敦本地日重置状态
                cur_day = int(day[i])
                state = 0
                a_hi = a_lo = float("nan")
                frozen = False
                day_ok = False
            if in_asian[i]:
                h, l = float(high[i]), float(low[i])
                a_hi = h if np.isnan(a_hi) else max(a_hi, h)
                a_lo = l if np.isnan(a_lo) else min(a_lo, l)
            elif after[i] and not np.isnan(a_hi):
                if close_min[i] >= exit_min:
                    state = 0                     # 收盘信号归零 → 引擎整点开盘平仓
                else:
                    if not frozen:
                        frozen = True
                        day_ok = (k <= 0.0) or (
                            not np.isnan(atr[i])
                            and (a_hi - a_lo) >= k * float(atr[i]))
                    if day_ok:
                        c = float(close[i])
                        if c > a_hi:
                            state = 1
                        elif c < a_lo:
                            state = -1
                        # 收盘回到区间内：粘性维持原方向
                    else:
                        state = 0
            else:
                state = 0                         # 无亚盘数据可破：空仓
            sig[i] = state
        return pd.Series(sig, index=bars.index, dtype="int64")
