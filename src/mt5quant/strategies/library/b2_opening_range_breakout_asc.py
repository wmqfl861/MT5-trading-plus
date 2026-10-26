"""B2 变体：NY 开盘区间突破 + reentry=after_signal_change（H003-v2，M15）。

背景（假设卡 H003-v2，谱系 H003）：v1（NyOpeningRangeBreakout，M5，
reentry=immediate）在成本口径 v3 下 4,108 笔 / PF 1.03 / 毛利成本倍数 1.41
（台账 run#375-380，docs/research/gates-calibration-v3-2026-10-26.md）——
毛利方向为正但换手吃掉大头，换手主要由"1.0×ATR 止损 → immediate 同向
重入"循环贡献。本变体只改引擎层出场规则：reentry="after_signal_change"
（保护性出场后信号离开原方向前禁止同向再入场，R7/A-1.3），当日每个方向
至多一段持仓。

信号层与 NyOpeningRangeBreakout **完全一致**（同一 generate_signals，
零复制）；差异只有两处类属性：

- exit_rules.reentry = "after_signal_change"（v1 为缺省 "immediate"）；
- required_data.timeframe = "M15"（H003-v2 绑定周期；信号逻辑本身与
  bar 周期无关——开盘区间按 NY 本地时刻构筑，M15 为 09:30/09:45 两根
  bar，窗口不变性论证见 b2_opening_range_breakout：状态以 NY 日为界，
  当日信号只依赖当日 bar + ATR 的 14 根回看。LOOKBACK_BARS=320 对 M15
  一日 96 根 + ATR14 + 缓冲成立，对 M5 一日 288 根 + 缓冲同样成立）。

机制（假设卡 H003）：隔夜信息在开盘竞价与首半小时集中定价，机构执行
算法与迟到市价单在区间边界触发止损/追价连锁，订单流不平衡延续到午盘；
v2 附加假设——v1 的正毛利来自区间突破的第一段延续，而非止损后反复
重入的段落（after_signal_change 后 PF 不升 = 该假设被证伪）。
"""
from __future__ import annotations

from mt5quant.strategies.base import ExitRules, RequiredData
from mt5quant.strategies.library.b2_opening_range_breakout import (
    LOOKBACK_BARS,
    NyOpeningRangeBreakout,
)


class NyOpeningRangeBreakoutAsc(NyOpeningRangeBreakout):
    """同 NyOpeningRangeBreakout，但保护性出场后不立即同向重入（H003-v2）。

    参数网格（继承 v1，不变）：exit_hour_ny (14,15) × min_range_atr
    (0.0,0.5,1.0) = 6 组；引擎层 1.0×ATR 止损（继承）。
    """

    required_data = RequiredData(symbols=("USTEC",), timeframe="M15",
                                 lookback_bars=LOOKBACK_BARS)
    exit_rules = ExitRules(stop_loss_atr=1.0, reentry="after_signal_change")
