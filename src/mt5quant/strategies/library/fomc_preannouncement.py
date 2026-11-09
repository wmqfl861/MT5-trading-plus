"""FOMC 预排公告日前漂移（C005/H102/R3）的事件窗信号策略（T151/D66 重写；
T187/D74 事件时间口径修订）。

第十六轮 D66（所测即所交易，H16-2）：事件模式卡的策略类必须生成
**事件窗信号**——本类不再继承 BuyHold 脚手架，bar 级 generate_signals
即事件窗本身（与 b7/C002 的先例分工一致：研究评估走 validation.events
的事件收益路径，bar 级信号负责"将来要交易的代码"的形状）。

事件窗（H102/C005 登记，与 ``validation.events.fomc_calendar_events``
同一定义）：冻结日历工件（fomc_meetings.json 默认事件集，SHA256 绑定）
的每个公告日 D——前一主段交易日（锚日）收盘进场、公告日收盘离场，
``static_long``（方向恒定、不依赖任何当期信号）。事件窗
``price_timepoint="day_close"``（D74/T187/H17-1）：入场/出场是**价格
时点**（锚日收/公告日收）。bar 级信号语义（只用已收盘 bar 与**事前
公布的官方日历**，不读公告内容/惊喜方向）：

- 信号 bar = 入场锚日（D 的前一主段交易日）**本身**：s[i]=+1 → 引擎
  下一根（公告日 bar）开盘入场、再下一根开盘平仓，持仓恰为 1 根 D1
  （公告日 bar）。在 T187 统一后的合并序列口径（close-only 外部段
  改写为"开=前一观测收盘"，与 MT5 D1 同口径）下，公告日 bar 开盘 =
  锚日收盘、次一 bar 开盘 = 公告日收盘——引擎的开盘成交与研究口径
  ``event_returns`` 的 day_close 收盘成交是**同一价格时点**，逐事件
  对账（tests/test_t151_event_identity.py）按"引擎 = 研究口径 =
  登记窗"三方一致核对（T187 前旧口径：信号 bar = 锚日前一交易日、
  持仓落在公告日前一天——H17-1 所指错位）；
- **日历在构造时注入**（``event_windows`` = 事件窗序列（研究装配路径
  由 ``bt._h_event_setup`` 从冻结工件构造后注入），``event_calendar`` =
  显式 v1 交易日历），不读任何全局状态；
- 无注入事件窗时回落**同序列日历近似**（b7 ``_diagnostic_signals``
  同一先例语义：纯日历算术——每 6 周一个周三近似 FOMC 常规会议节奏，
  信号 bar = 其前一交易日（伪锚日），与注入路径同一"持公告日 bar"
  形状），只供诊断/形状核查，**不是研究事件集**；
  事件模式研究运行的注入核对在 bt 侧 ``_h_preflight``（fail-closed），
  不得静默以近似事件集跑研究。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from mt5quant.strategies.base import BaseStrategy, ExitRules, RequiredData

# mt5quant.validation.events 的符号（SignalUnavailable/trading_calendar）在
# 方法内**延迟导入**：本模块被 strategies.library 包 __init__ 收录，而
# mt5quant.strategies.__init__ 又经 b1 的库路径导入本包——顶层导入
# validation.events 会在 strategies 包 __init__ 未完成时触发
# validation.robustness 的 `from mt5quant.strategies import ...`（循环导入；
# b7 不在包 __init__ 收录内故可顶层导入，先例差异在此）。

# 同序列日历近似的伪 FOMC 节奏参数（仅诊断）：周锚 1970-01-05（周一），
# 每 _DIAG_WEEKS 周取一个周三当伪公告日（信号 bar = 其前一交易日的
# 伪锚日）→ 约 8.7 次/年（FOMC 常规会议 8 次/年量级）。纯日期算术，
# 与数据起点无关（截断/窗口不变性安全）。
_DIAG_EPOCH = pd.Timestamp("1970-01-05")
_DIAG_WEEKS = 6


class PreFomcAnnouncementDrift(BaseStrategy):
    """Pre-FOMC 公告前漂移（USTEC D1，static_long）：事件窗信号策略。

    事件日（公告日）前一交易日（锚日）收盘进场、事件日收盘离场（1 根
    D1 持仓 = 公告日 bar）；无参数（single_spec）。事件窗与显式交易日历
    经构造参数注入；研究路径由 ``bt._h_event_setup`` 注入冻结工件构造出
    的事件窗。
    """

    event_kind = "fomc_calendar"
    side_rule = "static_long"
    # T151-5/D66：事件窗信号标记——事件模式卡的策略类不生成事件窗信号
    # 时，bt 侧（_h_preflight）直接拒绝运行。
    generates_event_window_signals = True
    required_data = RequiredData(symbols=("USTEC",), timeframe="D1",
                                 lookback_bars=1)
    exit_rules = ExitRules()   # 固定 1 夜事件窗、信号出场；登记 stop=无

    def __init__(self, *, event_windows=None, event_calendar=None):
        self.event_windows = (list(event_windows)
                              if event_windows is not None else None)
        self.event_calendar = event_calendar

    def _injected_signal_days(self) -> pd.DatetimeIndex | None:
        """注入事件窗 → 信号日集合（D74/T187：day_close 窗的信号 bar =
        锚日本身——引擎下一根开盘成交 = 锚日收盘；bar_open 窗沿用旧
        语义 = 锚日在显式交易日历上的前一交易日）。

        只用 bar 自身日期即可判定的纯函数（截断/窗口不变性安全）；
        锚日不在显式日历（主段首日/窗外/数据边缘）→ 该事件无信号 bar。
        """
        if self.event_windows is None:
            return None
        if self.event_calendar is None:
            from mt5quant.validation.events import SignalUnavailable
            raise SignalUnavailable(
                "PreFomcAnnouncementDrift：注入了事件窗但没有显式交易日历"
                "（event_calendar）——锚日无从判定，fail-closed"
                "（不静默回落同序列近似）")
        from mt5quant.validation.events import trading_calendar
        cal = trading_calendar(self.event_calendar)
        days = pd.DatetimeIndex(cal.day)
        values = days.to_numpy()
        out: set[pd.Timestamp] = set()
        for e in self.event_windows:
            anchor = pd.Timestamp(e.entry_time).normalize()
            pos = int(np.searchsorted(values, anchor.to_datetime64()))
            if pos == 0 or pos >= len(values) or days[pos] != anchor:
                continue     # 锚不在显式日历上 → 无信号 bar（事件窗不可交易）
            if getattr(e, "price_timepoint", "bar_open") == "day_close":
                # D74/T187：信号 = 锚日收盘时点 → 锚日 bar 收盘发信号、
                # 下一根（公告日 bar）开盘入场 = 锚日收盘（统一口径下）。
                out.add(pd.Timestamp(values[pos]))
            else:
                out.add(pd.Timestamp(values[pos - 1]))
        return (pd.DatetimeIndex(sorted(out)) if out
                else pd.DatetimeIndex([]))

    def _signal_mask(self, bars: pd.DataFrame) -> pd.Series:
        """信号位掩码：注入事件窗 → 信号日集合匹配；无注入 → 同序列
        日历近似（仅诊断，非研究事件集）。"""
        dates = pd.to_datetime(bars["time"]).dt.normalize()
        injected = self._injected_signal_days()
        if injected is not None:
            return dates.isin(injected)
        # 诊断近似：伪公告日 = 每 _DIAG_WEEKS 周一个周三；信号 bar =
        # 其前一交易日 = 伪锚日（bdate 数据上即周二，与注入路径同一
        # "信号=锚日、持公告日 bar"形状）。纯 bar 自身日期算术。
        pseudo_day = dates + pd.Timedelta(days=1)      # 周二 + 1 = 周三
        week = (pseudo_day - _DIAG_EPOCH).dt.days // 7
        return (dates.dt.weekday == 1) & ((week % _DIAG_WEEKS) == 0)

    def signal_probe_positions(self, bars):
        """All scheduled decision bars, including flat signals, are G0 probes."""
        if len(bars) == 0:
            return []
        return [int(i) for i in np.flatnonzero(
            self._signal_mask(bars).to_numpy(dtype=bool))]

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        self.validate_params(dict(params))
        out = np.zeros(len(bars), dtype="int64")
        if len(bars):
            out[self._signal_mask(bars).to_numpy(dtype=bool)] = 1
        return pd.Series(out, index=bars.index, dtype="int64")
