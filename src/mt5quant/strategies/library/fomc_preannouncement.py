"""FOMC 预排公告日前漂移（C005/H102/R3）的事件模式策略身份类。

与 b7（C002/H101，T112 先例）同一角色分工：研究评估走
validation.events 的事件收益路径（bt gate 关卡 V3 事件分支，
``--event-kind fomc_calendar``：冻结日历工件 fomc_meetings.json 默认
事件集 → 公告日 D 的前一主段交易日收盘入场 → 公告日收盘平仓，
``static_long``——与 C005 [declared].side_rule 同一方向声明，方向恒定、
不依赖任何当期信号）。本类只承担：

- 事件机制身份：``event_kind = "fomc_calendar"`` 供 ``_h_preflight`` 与
  ``--event-kind`` 做一致性核对（声明冲突即拒绝）；
- bar 级台账基线脚手架：generate_signals 继承 BuyHold 恒多——卡模板
  策略绑定、台账基线运行与 G0 一致性检查用，**不是研究结论来源**
  （与 b7 的 bar 级 generate_signals 同一先例语义）。

信号只用已收盘 bar 与事前公布的官方日历（federalreserve.gov 工件，
SHA256 绑定），不读公告内容/惊喜方向。
"""
from __future__ import annotations

from mt5quant.strategies.base import RequiredData
from mt5quant.strategies.library.buy_hold import BuyHold


class PreFomcAnnouncementDrift(BuyHold):
    """Pre-FOMC 公告前漂移（USTEC D1，static_long）：事件机制身份 +
    buy&hold bar 级基线脚手架（无参数，single_spec）。"""

    event_kind = "fomc_calendar"
    side_rule = "static_long"
    required_data = RequiredData(symbols=("USTEC",), timeframe="D1",
                                 lookback_bars=1)
