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
- 研究事件日来自显式完整交易日历的月末交易日，不从价格末行推断。
- 研究信号来自显式 USTEC 辅助序列，使用共享 penultimate-close-v1：
  上月倒数第二交易日收盘 → 本月倒数第二交易日收盘；两个收盘必须严格
  早于事件入场。FRED D1/MT5 D1 的收盘可见时刻由明确来源日历提供。
- LOOKBACK_BARS 是成交序列窗口；辅助序列另外保留窗口起点前 40 天的
  有界月度预热，G0 同步裁剪辅助价格上界，并强制覆盖所有计划事件点。
  已知未来交易日只作为日历计划，不能带入未来价格。
- 旧同序列/工作日日历近似仅由 diagnostic_same_series=True 显式启用；
  不作研究默认，不进入 V3 方向判定。旧诊断实现保留用于历史回归。
"""
from __future__ import annotations

import calendar

import numpy as np
import pandas as pd

from mt5quant.validation.events import (
    EventWindow, SignalUnavailable, declared_event_side, signal_close_times,
    trading_calendar,
)
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

    auxiliary_signal_required = True
    signal_symbol = "USTEC"
    side_rule = "mtd_sign"
    event_kind = "london_fix"

    def __init__(self, *, signal_bars=None, signal_timeframe="H1",
                 signal_calendar=None, event_calendar=None, diagnostic_same_series=False):
        self.signal_bars = signal_bars
        self.signal_timeframe = signal_timeframe
        self.signal_calendar = signal_calendar
        self.event_calendar = event_calendar
        self.diagnostic_same_series = bool(diagnostic_same_series)

    def signal_probe_positions(self, bars):
        """All scheduled decision bars, including flat signals, are G0 probes."""
        if self.diagnostic_same_series or self.event_calendar is None:
            return []
        cal = trading_calendar(self.event_calendar)
        last_days = {g.day.iloc[-1] for _, g in cal.groupby(cal.day.dt.to_period("M"))
                     if g.day.iloc[0].to_period("M").end_time.normalize() <= cal.attrs["complete_through"]}
        local = pd.to_datetime(bars.time).dt.tz_localize("UTC").dt.tz_convert(LDN_TZ)
        return [i for i, t in enumerate(local)
                if t.hour * 60 + t.minute == ENTRY_LDN_MIN
                and pd.Timestamp(t.date()) in last_days]

    def for_signal_window(self, bars):
        """G0 must crop the instance's auxiliary data along with each trade probe."""
        if self.signal_bars is None or bars.empty:
            return self
        close_times = signal_close_times(self.signal_bars, self.signal_timeframe,
                                         self.signal_calendar)
        visible = pd.Timestamp(bars.time.iloc[-1]) + pd.Timedelta(hours=1)
        # Auxiliary prehistory is an explicit 40-day warmup before the first
        # trade bar: that bar may itself be month-end and need last month's
        # penultimate close. Calendar schedules remain intact; prices are cropped.
        warmup_start = pd.Timestamp(bars.time.iloc[0]) - pd.Timedelta(days=40)
        mask = (close_times < visible) & (close_times >= warmup_start)
        import copy
        view = copy.copy(self)
        view.signal_bars = self.signal_bars.loc[mask].copy()
        return view

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        p = self.validate_params(dict(params))
        if self.diagnostic_same_series:
            return self._diagnostic_signals(bars, p)
        if self.signal_bars is None:
            raise SignalUnavailable("B7 signal bars required; same-series approximation is diagnostic only")
        cal = trading_calendar(self.event_calendar)
        complete = cal.attrs["complete_through"]
        last_days = {g.day.iloc[-1] for _, g in cal.groupby(cal.day.dt.to_period("M"))
                     if g.day.iloc[0].to_period("M").end_time.normalize() <= complete}
        side = None  # construct only at an event; an empty G0 warmup has no decision
        times = pd.to_datetime(bars.time)
        local = times.dt.tz_localize("UTC").dt.tz_convert(LDN_TZ)
        out = np.zeros(len(bars), dtype="int64")
        for i, t in enumerate(local):
            if t.hour * 60 + t.minute == ENTRY_LDN_MIN and pd.Timestamp(t.date()) in last_days:
                entry = pd.Timestamp(times.iloc[i]) + pd.Timedelta(hours=1)
                # A slice's warmup does not supply an earlier month's anchor. Its
                # events are unavailable, never interpreted as zero by V3.
                previous = entry.to_period("M") - 1
                if self.signal_bars.empty or self.signal_bars.time.min().to_period("M") > previous:
                    continue
                if side is None:
                    side = declared_event_side("mtd_sign", signal_bars=self.signal_bars,
                                               signal_timeframe=self.signal_timeframe,
                                               calendar=self.signal_calendar,
                                               threshold=float(p["threshold_pct"]))
                out[i] = side(EventWindow("london_fix", str(entry), entry,
                                           entry + pd.Timedelta(hours=1)))
        return pd.Series(out, index=bars.index, dtype="int64")

    def _diagnostic_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
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
