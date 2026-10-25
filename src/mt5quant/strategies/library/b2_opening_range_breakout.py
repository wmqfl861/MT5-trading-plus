"""B2 NY 开盘区间突破（USTEC M5/M15，P5 基线）。

语义（信号层，全部只用已收盘 bar；时间口径：库内 naive-UTC 服务器时间，
NY 本地时间经 zoneinfo 换算，DST 自动处理）：
- 开盘区间（OR）：NY 09:30–10:00 内开盘的 bar 的最高高点/最低低点
  （M5 为 6 根、M15 为 2 根）；区间未完成前不交易。
- 入场：10:00 后首根收盘 > OR 高点 → 目标仓位 +1；< OR 低点 → -1；
  收盘回到区间内则粘性维持原方向（跌到对侧收盘则反手）。
- 出场：bar 收盘本地时刻达到 exit_hour（NY）→ 信号归零，引擎恰在
  exit_hour 整点那根 bar 的开盘价平仓；引擎层另有 1.0×ATR 止损。
  exit_hour 网格取 (14, 15)：NY 16:00 收盘在冬季（EST）恰是 21:00 UTC
  换日/结算窗（scope-2026-10-25 禁开仓时段），提前一小时离场避开。
- 过滤：OR 宽度 < min_range_atr × ATR14（简单 TR 均值，非引擎 Wilder
  ATR，仅作过滤尺度）的"窄区间日"不交易；资格在当日首个盘后 bar 冻结。

窗口不变性（铁律 5）：全部状态以 NY 日为界（日切重置），当天信号只依赖
当天 bar + ATR 的 14 根回看；lookback_bars = 320 ≥ M5 一天 288 根 + 缓冲，
截窗必含完整当日序列，重算与全史一致。bar 周期由时间索引最小正差推导
（数据孔只会让差变大）。

机制（假设卡 H003）：隔夜信息集中释放在开盘竞价与开盘半小时，做市商与
散户市价单在区间边界堆积止损/追价盘，区间边界成为短期供需不平衡的
参照点——突破后路径依赖的资金流延续方向（开盘区间突破的经典微观结构
解释，见卡内文献）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.base import BaseStrategy, ExitRules, ParamSpec, RequiredData

NY_TZ = "America/New_York"
OR_START_MIN = 9 * 60 + 30     # NY 09:30 开盘
OR_END_MIN = 10 * 60           # NY 10:00：开盘区间完成
ATR_N = 14                     # 信号层波动尺度（简单 TR 均值）
LOOKBACK_BARS = 320            # ≥ M5 一日 288 根 + 缓冲（窗口不变性）


def _local_parts(bars: pd.DataFrame, tz_name: str):
    """(日键, 开盘本地分钟, 收盘本地分钟) 数组；bar<2 或无正差 → None。

    收盘本地分钟 = 开盘本地分钟 + bar 周期（分钟）；bar 周期取时间索引的
    最小正差。bar 数不足时调用方返回全 0 信号。
    """
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


class NyOpeningRangeBreakout(BaseStrategy):
    """NY 09:30–10:00 开盘区间突破，exit_hour 整点收盘离场 + 1.0×ATR 止损。

    参数网格：exit_hour_ny (14,15) × min_range_atr (0.0,0.5,1.0) = 6 组。
    """

    params_schema = (
        ParamSpec(name="exit_hour_ny", type="int", grid=(14, 15)),
        ParamSpec(name="min_range_atr", type="float", grid=(0.0, 0.5, 1.0)),
    )
    required_data = RequiredData(symbols=("USTEC",), timeframe="M5",
                                 lookback_bars=LOOKBACK_BARS)
    exit_rules = ExitRules(stop_loss_atr=1.0)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        exit_min = int(p["exit_hour_ny"]) * 60
        k = float(p["min_range_atr"])

        n = len(bars)
        sig = np.zeros(n, dtype="int64")
        parts = _local_parts(bars, NY_TZ)
        if parts is None:
            return pd.Series(sig, index=bars.index)
        day, open_min, close_min = parts
        high = bars["high"].astype("float64").to_numpy()
        low = bars["low"].astype("float64").to_numpy()
        close = bars["close"].astype("float64").to_numpy()
        atr = _atr_simple(bars)
        in_or = (open_min >= OR_START_MIN) & (open_min < OR_END_MIN)
        after = open_min >= OR_END_MIN

        state = 0
        cur_day = int(day[0]) - 1
        or_hi = or_lo = float("nan")
        or_frozen = False     # 当日资格（宽/窄区间判定）是否已冻结
        day_ok = False
        for i in range(n):
            if int(day[i]) != cur_day:          # 日切：NY 本地日重置状态
                cur_day = int(day[i])
                state = 0
                or_hi = or_lo = float("nan")
                or_frozen = False
                day_ok = False
            if in_or[i]:
                h, l = float(high[i]), float(low[i])
                or_hi = h if np.isnan(or_hi) else max(or_hi, h)
                or_lo = l if np.isnan(or_lo) else min(or_lo, l)
            elif after[i] and not np.isnan(or_hi):
                if close_min[i] >= exit_min:
                    state = 0                   # 收盘信号归零 → 引擎整点开盘平仓
                else:
                    if not or_frozen:           # 首个盘后 bar 冻结当日资格
                        or_frozen = True
                        day_ok = (k <= 0.0) or (
                            not np.isnan(atr[i])
                            and (or_hi - or_lo) >= k * float(atr[i]))
                    if day_ok:
                        c = float(close[i])
                        if c > or_hi:
                            state = 1
                        elif c < or_lo:
                            state = -1
                        # 收盘回到区间内：粘性维持原方向
                    else:
                        state = 0
            else:
                state = 0                       # 盘前 / 区间构筑中 / 缺 OR：空仓
            sig[i] = state
        return pd.Series(sig, index=bars.index, dtype="int64")
