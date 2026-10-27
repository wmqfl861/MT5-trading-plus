"""动作库 v1（T74，蓝图 P 线 §2.2 / 决策 D39）。

动作模板取材于有订单流机制支撑的成熟形态，不引入网上现成 EA、指标策略
或付费信号；同一张卡的全部动作共用止损止盈（D44），持有上限在信号层实现。
以阻力位为例（支撑对称），全部收盘判断、下一根开盘按市价成交：

| 编号 | 动作 | 触发条件 | 方向（阻力/支撑） |
| --- | --- | --- | --- |
| A0 | 不做 | 缺省（决策层，见 mt5quant.prob.decision） | — |
| A1 | 突破跟随 | 局面事件后 m 根内收盘价突破价位 b 个 ATR | +1 / −1 |
| A2 | 回踩跟随 | A1 触发后 m 根内收盘回到价位 ±0.25 ATR 且仍在突破侧 | +1 / −1 |
| A3 | 价位反转 | 事件 anchor 根收盘未站上（未跌破）价位 | −1 / +1 |
| A4 | 假突破反手 | m 根内收盘站上价位，其后 m 根内收盘跌回价位之下 | −1 / +1 |

A2 的回踩用"收盘回到价位 + 下一根开盘市价"近似限价单（D48）。
马丁、网格、摊平、按连亏加仓不进动作库（铁律 20）。本模块属策略库
（D45：研究角色可写，计入代码指纹）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from mt5quant.strategies.library.prob.situations import LevelEvent

DEFAULT_RETEST_BAND_ATR = 0.25   # 蓝图 §2.2 写死的 ±0.25 ATR


@dataclass(frozen=True)
class ActionParams:
    """动作参数（卡的 breakout_atr 与 retest_window_bars）。"""

    breakout_atr: float            # b：A1 的突破幅度（ATR 倍数）
    retest_window_bars: int        # m：局面活动窗与 A2/A4 的回看窗
    retest_band_atr: float = DEFAULT_RETEST_BAND_ATR

    def __post_init__(self):
        for name in ("breakout_atr", "retest_band_atr"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not math.isfinite(v) or v <= 0:
                raise ValueError(f"{name} 须为有限正数：{v!r}")
        if isinstance(self.retest_window_bars, bool) \
                or not isinstance(self.retest_window_bars, int) \
                or self.retest_window_bars < 1:
            raise ValueError(
                f"retest_window_bars 须为 ≥1 整数：{self.retest_window_bars!r}")


def action_triggers(bars: pd.DataFrame, events: list[LevelEvent], *,
                    params: ActionParams, atr: pd.Series) -> pd.DataFrame:
    """把局面事件展开为动作触发（每行一个动作触发，含方向与价位）。

    返回列：time、situation、action、side、level、kind、anchor、bar。
    sgn = 价位的突破方向（阻力 +1、支撑 −1）：A1/A2 顺突破方向，
    A3/A4 逆突破方向。ATR 不可得的 bar 跳过（不产生窗口内触发）。
    """
    closes = bars["close"].to_numpy(dtype="float64")
    atrs = atr.to_numpy(dtype="float64")
    times = pd.to_datetime(bars["time"])
    n = len(bars)
    m = params.retest_window_bars
    rows: list[dict] = []

    def _add(bar: int, action: str, side: int, ev: LevelEvent) -> None:
        rows.append({"time": times.iloc[bar], "situation": ev.situation,
                     "action": action, "side": side, "level": ev.level,
                     "kind": ev.kind, "anchor": ev.bar, "bar": bar})

    for ev in events:
        i, level = ev.bar, ev.level
        sgn = 1 if ev.kind == "resistance" else -1

        # A3：anchor 收盘未站上（阻力）/未跌破（支撑）价位
        if math.isfinite(atrs[i]) and sgn * (closes[i] - level) <= 0:
            _add(i, "A3", -sgn, ev)

        # A1：[i, i+m] 内首个收盘突破价位 b 个 ATR
        a1 = None
        for j in range(i, min(i + m + 1, n)):
            if not math.isfinite(atrs[j]):
                continue
            if sgn * (closes[j] - level) > params.breakout_atr * atrs[j]:
                _add(j, "A1", sgn, ev)
                a1 = j
                break

        # A2：A1 后 m 根内收盘回到价位 ±band·ATR 且仍在突破侧
        if a1 is not None:
            for j in range(a1 + 1, min(a1 + m + 1, n)):
                if not math.isfinite(atrs[j]):
                    continue
                off = sgn * (closes[j] - level)
                if 0 < off <= params.retest_band_atr * atrs[j]:
                    _add(j, "A2", sgn, ev)
                    break

        # A4：[i, i+m] 内首个收盘站上价位，其后 m 根内收盘跌回
        stood = None
        for j in range(i, min(i + m + 1, n)):
            if sgn * (closes[j] - level) > 0:
                stood = j
                break
        if stood is not None:
            for j in range(stood + 1, min(stood + m + 1, n)):
                if sgn * (closes[j] - level) < 0:
                    _add(j, "A4", -sgn, ev)
                    break

    out = pd.DataFrame(
        rows, columns=["time", "situation", "action", "side", "level",
                       "kind", "anchor", "bar"])
    return out.sort_values(["bar", "action"], kind="stable").reset_index(drop=True)
