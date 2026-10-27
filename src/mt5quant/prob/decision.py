"""决策规则（T72，蓝图 P 线 §2.5 / 决策 D41）：期望值下界 + 门槛 + 确定性选择。

EV_下界按概率下界计算（赢率减 z·SE、输率加 z·SE、到期取余下比例）；门槛为
threshold_mult × 平均来回成本；多个动作同时满足取期望值最高，并列时按
A1 < A2 < A3 < A4 的顺序取第一个；都不满足就不做（返回 None）；不抽签。
样本少时 SE 大 → 下界自然压低 → 偏向"不做"（设计意图，算例见蓝图附录 C）。
评估代码（D45：研究角色只读）。
"""
from __future__ import annotations

import math

from mt5quant.prob.tables import CellEstimate

# 动作库 v1（蓝图 P 线 §2.2）：并列时按此序取第一个（确定性）
ACTION_ORDER = ("A1", "A2", "A3", "A4")


def ev_lower(est: CellEstimate, *, z: float) -> float:
    """期望净收益下界（单位 R，含成本）。

    EV = (p_win − z·se_win)·r_win + (p_loss + z·se_loss)·r_loss
         + 余下比例·r_time；余下比例 = 1 − 上两项（不截断：极端 SE 下
    概率可为负/超一，进一步压低下界，方向保守）。
    """
    if not math.isfinite(z) or z < 0:
        raise ValueError(f"z 必须是非负有限数，收到 {z!r}")
    p_win = est.p_win - z * est.se_win
    p_loss = est.p_loss + z * est.se_loss
    p_time = 1.0 - p_win - p_loss
    return p_win * est.r_win + p_loss * est.r_loss + p_time * est.r_time


def choose(candidates: list[tuple[str, CellEstimate, float]], *, z: float,
           threshold_mult: float) -> str | None:
    """在候选动作里选一个入场；None = 不做（A0）。

    candidates: (动作, 估计, 平均来回成本 R)。规则：EV_下界 ≥
    threshold_mult × 成本的动作中取 EV 最高；并列取 ACTION_ORDER 最前者。
    """
    if not math.isfinite(threshold_mult) or threshold_mult < 0:
        raise ValueError(
            f"threshold_mult 必须是非负有限数，收到 {threshold_mult!r}")
    known = {a: i for i, a in enumerate(ACTION_ORDER)}
    unknown = [a for a, _, _ in candidates if a not in known]
    if unknown:
        raise ValueError(f"未知动作编号 {unknown}；合法：{ACTION_ORDER}")
    best_action: str | None = None
    best_ev = -math.inf
    for action, est, cost in sorted(candidates, key=lambda c: known[c[0]]):
        if not math.isfinite(cost) or cost < 0:
            raise ValueError(f"动作 {action} 成本非法：{cost!r}")
        ev = ev_lower(est, z=z)
        if ev >= threshold_mult * cost and ev > best_ev:
            best_action, best_ev = action, ev
    return best_action
