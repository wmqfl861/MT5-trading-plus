"""触发计数（T76，蓝图 P 线 §6 PA / 计划 T76）：只数触发，不看结果。

输出每个动作触发的时刻、纽约小时、品种、局面、动作、方向、价位与当时的
ATR/收盘价——供 P 线预筛的检验力（发现段内可用触发数）与事前成本（触发
小时分布）使用。**绝不 import mt5quant.prob.labels、不跑引擎**：计数不是
研究结论，也不得泄漏结局信息（蓝图 v4 §2 的允许项；tests/test_prob_
counting.py 有静态检查）。评估代码（D45：研究角色只读）。
"""
from __future__ import annotations

import pandas as pd

from mt5quant.backtest.engine_a import atr_series
from mt5quant.prob.gaps import eligibility_mask
from mt5quant.strategies.library.prob.actions import (
    ActionParams, action_triggers,
)
from mt5quant.strategies.library.prob.situations import (
    Approach, find_level_events, levels_from_frame, s1_level_rows,
    s2_level_rows, s3_level_rows,
)

NY_TZ = "America/New_York"

__all__ = ["count_triggers", "level_rows_for_situation", "triggers_before"]


def level_rows_for_situation(bars: pd.DataFrame, situation: str, *,
                             grid_step: float | None = None,
                             approach: Approach | None = None,
                             atr: pd.Series | None = None) -> pd.Series:
    """局面 → 每 bar 候选价位列表（S1 需 grid_step/approach/atr）。"""
    if situation == "S1":
        if grid_step is None or approach is None or atr is None:
            raise ValueError("S1 需要 grid_step、approach 与 atr")
        return s1_level_rows(bars, grid_step=grid_step, approach=approach,
                             atr=atr)
    if situation == "S2":
        return levels_from_frame(s2_level_rows(bars),
                                 columns=["prev_high", "prev_low"])
    if situation == "S3":
        return levels_from_frame(s3_level_rows(bars),
                                 columns=["chan_high", "chan_low"])
    raise ValueError(f"未知局面编号：{situation!r}")


def count_triggers(bars: pd.DataFrame, *, symbol: str, situation: str,
                   approach: Approach, action_params: ActionParams,
                   grid_step: float | None = None,
                   atr: pd.Series | None = None, gap_spans=None,
                   feature_warmup: int | None = None, hold_bars: int = 0,
                   max_gap_days: int = 30) -> pd.DataFrame:
    """数一个品种一个局面上的全部动作触发（不看结果）。

    返回列：time、situation、action、side、level、kind、anchor、bar、
    ny_hour、atr、close、symbol——没有任何结局列（win/loss/net_r 等）。

    gap_spans（T97 波次 2，可选）：合并研究序列的洞集合
    （mt5data.ext.merge.merged_research_series 的产出，缺省 None 行为
    不变）——回看窗跨洞的触发按先决 4 抑制（mt5quant.prob.gaps）。

    P-09 共同可用集合（T98，可选三元组；都不给 → 行为与旧 gap_spans
    抑制一致，仅 A2/A4 的局面窗改从 anchor 根起算——audit P-09）：
    - feature_warmup：触发根须 ≥ 该值（缺省 None=不检查；2000 =
      atr_pct_2000 的排名窗）；
    - hold_bars：持有窗 [触发根, +hold_bars] 不跨洞（缺省 0=不检查）；
    - max_gap_days：持有窗的洞宽阈值（缺省 30，与 merge 的 gap_spans
      口径一致；回看窗按先决 4 用 gap_spans 原样）。
    """
    if atr is None:
        atr = atr_series(bars)
    levels = level_rows_for_situation(bars, situation, grid_step=grid_step,
                                      approach=approach, atr=atr)
    events = find_level_events(bars, levels, situation=situation,
                               approach=approach, atr=atr)
    trig = action_triggers(bars, events, params=action_params, atr=atr)
    if len(trig) and (gap_spans or feature_warmup is not None or hold_bars):
        keep = eligibility_mask(
            trig, bars, gap_spans or [], situation, approach.lookback_bars,
            feature_warmup=(0 if feature_warmup is None
                            else int(feature_warmup)),
            hold_bars=int(hold_bars), max_gap_days=max_gap_days)
        trig = trig.iloc[[k for k, ok in enumerate(keep) if ok]]
    if len(trig) == 0:
        return trig.assign(symbol=symbol, ny_hour=pd.Series(dtype="int64"),
                           atr=pd.Series(dtype="float64"),
                           close=pd.Series(dtype="float64"))
    ny_hour = (pd.to_datetime(trig["time"]).dt.tz_localize("UTC")
               .dt.tz_convert(NY_TZ).dt.hour.astype("int64"))
    closes = bars["close"].to_numpy(dtype="float64")
    atrs = atr.to_numpy(dtype="float64")
    return trig.assign(
        symbol=symbol, ny_hour=ny_hour.to_numpy(),
        atr=[float(atrs[int(b)]) for b in trig["bar"]],
        close=[float(closes[int(b)]) for b in trig["bar"]])


def triggers_before(counted: pd.DataFrame, split) -> pd.DataFrame:
    """发现段子集：触发时刻早于分界日期（D50：P1 只用发现段计数）。"""
    if len(counted) == 0:
        return counted
    return counted[pd.to_datetime(counted["time"]) < pd.Timestamp(split)]
