"""B5 变体：纳指日内动量，尾盘 15:45 NY 提前出场（H006-v2）。

背景（假设卡 H006-v2，谱系 H006）：v1（IntradayMomentumNy）尾盘信号窗为
[15:30, 16:00) NY——持有的最后一根 bar 收在 16:00 边界附近，冬季（EST）
恰落 USTEC 每日维护/换日小时（21:00 UTC 无 tick，成本模型按缺统计小时
禁入 missNY[17]；v1 关卡基准运行因"USTEC 小时 21 无点差统计"失败入台账
——docs/research/gates-calibration-2026-10-25.md："设计缺陷而非证伪，
v2 提前 15 分钟出场再测"）。本变体把尾盘信号窗上界从 16:00 提前到 15:45
NY（收盘本地时刻 ∈ [15:30,15:45) 的 bar 上取方向，即收盘 15:30/15:35/
15:40 的三根 M5 bar）：引擎入场在 15:30 整点开盘价，出场在 15:45 整点
开盘价（窗口结束后的首根 bar）——持仓恰为收盘前 15 分钟；冬季出场成交
落在 20:45 UTC，距 21:00 维护窗还有 15 分钟，不再触碰缺统计小时。

信号层与 IntradayMomentumNy 一致，唯尾盘窗上界 LAST_END_MIN（960→945）
不同——v1 的 generate_signals 直接引用其模块级常量，无法经子类属性
覆盖，故本模块重写 generate_signals（逐行同构，仅 LAST_END_MIN 取本
模块常量），并复用 b5 的 _local_parts/_atr_simple（同一时间换算与 ATR
口径）。方向判定、参数网格、引擎层 2.0×ATR 止损全部继承不变——v2 只
动尾盘出场时点这一个变量（口径与 v1 相同的左闭右开窗，窗口整体提前
15 分钟）。

机制（假设卡 H006）：Gao, Han, Li & Zhou (2018) *Market Intraday
Momentum*——开盘头半小时收益对尾盘收益有正预测；v2 附加假设：尾盘延续
的可交易部分在 15:30-15:45 已兑现，最后 15 分钟（贴近维护窗/收盘结算）
执行质量差、不值得持有（提前出场后仍复现 v3 的 PF≈0.7 = 现象在该品种/
成本下不成立，放弃谱系）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.library.b5_intraday_momentum import (
    IntradayMomentumNy,
    _atr_simple,
    _local_parts,
)

NY_TZ = "America/New_York"
FIRST_START_MIN = 9 * 60 + 30    # NY 09:30（与 v1 相同）
FIRST_END_MIN = 10 * 60          # NY 10:00（与 v1 相同）
LAST_START_MIN = 15 * 60 + 30    # NY 15:30 入场（与 v1 相同）
LAST_END_MIN = 15 * 60 + 45      # NY 15:45 出场（v1 为 16:00——避开维护窗）
LOOKBACK_BARS = 320              # 同 v1：≥ M5 一日 288 根 + 缓冲（窗口不变性）


class IntradayMomentumNyEarlyExit(IntradayMomentumNy):
    """开盘头半小时方向 → 尾盘 [15:30,15:45) 同向持有（提前出场变体）。

    参数网格（继承 v1，不变）：min_ret_atr (0.0,0.5,1.0,1.5) = 4 组；
    引擎层 2.0×ATR 止损兜底（继承）。required_data 继承（USTEC M5，
    lookback 320）。
    """

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        """与 IntradayMomentumNy.generate_signals 逐行同构，仅尾盘窗
        [15:30,15:45)（v1 为 [15:30,16:00)）。"""
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
                sig[i] = direction              # 尾盘 15 分钟：同向持有
        return pd.Series(sig, index=bars.index, dtype="int64")
