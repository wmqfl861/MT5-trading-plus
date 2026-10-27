"""概率决策线（P 线）评估代码（T72，蓝图 `docs/blueprint-p-line.md`）。

本包是标签、概率表、决策规则与模型产物——**研究角色只读**（D45）；局面与
动作在策略库 `mt5quant.strategies.library.prob`。使用约束：T75 标定报告与
T77 轮次草案获批之前，不得在真实局面触发上计算任何标签（蓝图 v4 §2）。
"""
from mt5quant.prob.artifact import ModelIntegrityError, load_model, save_model
from mt5quant.prob.decision import ACTION_ORDER, choose, ev_lower
from mt5quant.prob.labels import LOOKBACK_BARS, Bracket, label_triggers
from mt5quant.prob.tables import CellEstimate, ProbTable

__all__ = [
    "ACTION_ORDER", "Bracket", "CellEstimate", "LOOKBACK_BARS",
    "ModelIntegrityError", "ProbTable", "choose", "ev_lower", "label_triggers",
    "load_model", "save_model",
]
