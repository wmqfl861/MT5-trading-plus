"""B7 月末对冲调仓（C002/H101：USTEC 当月涨跌 → EURUSD，伦敦定盘前一小时）。

事件模式策略（蓝图 v4 §6.6；候选登记 C002）。研究评估走
validation.events 的事件收益路径（bt gate 关卡 V3 事件分支——
london_fix_events：月末最后交易日伦敦 15:00 入场、16:00 定盘出场）；
本类的 bar 级 generate_signals 只承担卡模板要求的策略绑定、台账基线
运行与 G0 一致性检查，**不是研究结论来源**。

机制（Melvin & Prins 2015 的对冲通道，方向按 C002 登记）：
外国股票组合管理人按月末市值调汇率对冲敞口——当月美股上涨 → 对冲
市值上升 → 月末最后一个定盘前须卖出 USD 买回本币（或反向），集中在
伦敦定盘前一小时成交。信号 = 当月（USTEC）涨幅；交易 = 月末最后
交易日伦敦定盘前 1 小时的 EURUSD 方向仓：涨幅 > +threshold_pct →
USD 贬值方向 → **多 EURUSD（+1）**；涨幅 < -threshold_pct → 空
EURUSD（-1）；|涨幅| ≤ 阈值 → 不交易（幅度不够没有调仓流）。

bar 级信号语义（只用已收盘 bar；库内 naive-UTC，伦敦本地经 zoneinfo
换算，DST 自动处理）：
- 事件 bar = 伦敦本地 14:00 开盘的 H1 bar（收盘恰为 15:00——定盘前
  一小时窗起点）：s[i]=±1 → 引擎下一根（15:00 开盘）入场、再下一根
  （16:00 定盘时刻开盘）平仓，持仓恰为事件窗 [15:00, 16:00]，不过夜
  （事件窗为日内 1 小时，overnight_nights=0）；
- 事件日 = 该伦敦日历日是当月**最后一个工作日**（纯日历判定：本身为
  工作日且当月其后无工作日——不看未来 bar，保证截断一致）；交易所
  假日恰落在最后工作日时该月 bar 级无信号（bar 级的如实代价；事件
  路径按真实交易日历构造事件，不受此影响）；
- 信号幅度 = 传入序列的当月月内涨幅：上一伦敦日历月最后一根收盘 →
  事件日前一伦敦日最后一根收盘（决策时点全部已收盘）。机制上信号
  品种是 USTEC、成交品种 EURUSD（C002 登记"信号品种只出信号不下单"）；
  bar 级单品种接口下信号与成交同为传入序列（同序列近似，文档如实，
  评估以事件路径为准）。

lookback_bars = 720（≥ 事件 bar 回到上一日历月最后一根的跨度：月末
事件距上月末最多约 3 周 ≈ 550 根 H1，+缓冲）：计划文本 lookback_bars=2
按**月度观测数**写（上月收盘 + 当月实时两个锚点），在 H1 bar 级上
声明回看窗必须真放得下这两个锚点——G0 窗口不变性按声明回看窗逐探针
复算，2 根 H1 放不下月度锚点（会 fail-closed）。
"""
from __future__ import annotations

import calendar

import numpy as np
import pandas as pd

from mt5quant.strategies.base import (
    BaseStrategy, ExitRules, ParamSpec, RequiredData,
)

LDN_TZ = "Europe/London"
ENTRY_LDN_MIN = 14 * 60          # 事件 bar 伦敦本地开盘分钟（收盘 15:00 =
                                 # 定盘前一小时窗起点）
LOOKBACK_BARS = 720              # 覆盖上一日历月最后一根（≈3 周 H1）+ 缓冲


def _is_last_business_day(year: int, month: int, day: int) -> bool:
    """伦敦日历日是否当月最后一个工作日（纯日历算术，不用未来 bar）。"""
    if day < 1 or month < 1 or month > 12:
        return False
    last = calendar.monthrange(year, month)[1]
    if day >= 1 and day <= last - 1:
        for d in range(day + 1, last + 1):
            if calendar.weekday(year, month, d) < 5:   # 周一..周五
                return False                           # 当月此后还有工作日
    # day == last 时循环为空：本月最后一天，是否工作日由自身 weekday 决定
    return calendar.weekday(year, month, day) < 5


class MonthEndHedgeRebalance(BaseStrategy):
    """月末最后交易日伦敦定盘前一小时，按当月涨幅定向的 EURUSD 事件仓。

    参数网格：threshold_pct (0.0, 0.02)。0.0 = 登记主规格（任何非零当月
    涨跌都触发，方向 = 涨跌符号——折算期望 3.1bp/事件按 E|r_月| 无条件
    口径）；0.02 仅保留为诊断变体（未登记、R1 单规格不跑）。
    """

    params_schema = (
        ParamSpec(name="threshold_pct", type="float", grid=(0.0, 0.02)),
    )
    required_data = RequiredData(symbols=("EURUSD",), timeframe="H1",
                                 lookback_bars=LOOKBACK_BARS)
    exit_rules = ExitRules(stop_loss_atr=1.0)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        thr = float(p["threshold_pct"])

        n = len(bars)
        sig = np.zeros(n, dtype="int64")
        if n == 0:
            return pd.Series(sig, index=bars.index)
        lt = pd.to_datetime(bars["time"]).dt.tz_localize("UTC") \
            .dt.tz_convert(LDN_TZ)
        day = (lt.dt.year * 10000 + lt.dt.month * 100 + lt.dt.day)
        day = day.to_numpy(dtype="int64")
        month = day // 100                      # 伦敦日历月键 YYYYMM
        open_min = (lt.dt.hour * 60 + lt.dt.minute).to_numpy(dtype="int64")
        close = bars["close"].astype("float64").to_numpy()

        for i in range(n):
            if open_min[i] != ENTRY_LDN_MIN:
                continue
            ymd = int(day[i])
            if not _is_last_business_day(ymd // 10000, (ymd // 100) % 100,
                                         ymd % 100):
                continue
            prev_day_close = None     # 事件日前一伦敦日最后一根收盘
            prev_month_close = None   # 上一伦敦日历月最后一根收盘
            cur_month = int(month[i])
            for j in range(i - 1, -1, -1):
                if prev_day_close is None and day[j] < ymd:
                    prev_day_close = float(close[j])
                if month[j] < cur_month:
                    prev_month_close = float(close[j])
                    break
            if (prev_day_close is None or prev_month_close is None
                    or not (prev_day_close > 0.0 and prev_month_close > 0.0)):
                continue            # 样本首月/首日无月度锚点（或坏价）→ 不交易
            mtd = prev_day_close / prev_month_close - 1.0
            if mtd > thr:
                sig[i] = 1          # 当月上涨 → USD 贬值方向 → 多 EURUSD
            elif mtd < -thr:
                sig[i] = -1         # 当月下跌 → 反向
        return pd.Series(sig, index=bars.index, dtype="int64")
