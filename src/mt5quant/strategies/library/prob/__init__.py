"""P 线策略库（T74，蓝图 P 线 §1–§2）：局面库、动作库与特征。

研究角色可写（D45），计入代码指纹；评估代码（标签/概率表/决策/模型产物）
在 `mt5quant.prob`（研究角色只读）。v1 约定：同一张卡的全部动作共用止损
`sl_atr`×ATR、止盈 `tp_atr`×ATR（ATR 取信号 bar 值，与引擎同口径）；持有
上限 K 根在信号层实现；`reentry = "after_signal_change"`（D44）。
"""
from mt5quant.strategies.library.prob.actions import ActionParams, action_triggers
from mt5quant.strategies.library.prob.features import atr_pct_2000, trend_sma200
from mt5quant.strategies.library.prob.situations import (
    GRID_DEFAULTS, Approach, LevelEvent, find_level_events, grid_levels_between,
    levels_from_frame, s1_level_rows, s2_level_rows, s3_level_rows,
)

__all__ = [
    "GRID_DEFAULTS", "ActionParams", "Approach", "LevelEvent",
    "action_triggers", "atr_pct_2000", "find_level_events",
    "grid_levels_between", "levels_from_frame", "s1_level_rows",
    "s2_level_rows", "s3_level_rows", "trend_sma200",
]
