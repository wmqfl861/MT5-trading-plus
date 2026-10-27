"""T74 动作库 v1：A1 突破跟随 / A2 回踩跟随 / A3 价位反转 / A4 假突破反手。

以阻力位为例（支撑对称），全部收盘判断、下一根开盘按市价成交（蓝图
P 线 §2.2 / 决策 D39）：
- A1 突破跟随：收盘价高于价位 b 个 ATR → 做多；
- A2 回踩跟随：A1 触发后 m 根内，收盘价回到价位 ±0.25 ATR 且仍在价位
  之上 → 做多（限价单的市价近似，D48）；
- A3 价位反转：局面触及价位的那根（anchor）收盘未站上价位 → 做空；
- A4 假突破反手：anchor 起 m 根内收盘站上价位，之后 m 根内收盘跌回
  价位之下 → 做空。
支撑位方向全部镜像。马丁/网格/摊平/按连亏加仓不进动作库（铁律 20）。
"""
import pandas as pd
import pytest

from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
from mt5quant.backtest.engine_a import atr_series
from mt5quant.strategies.library.prob.actions import ActionParams, action_triggers
from mt5quant.strategies.library.prob.situations import (
    Approach, LevelEvent, find_level_events, s1_level_rows,
)

APPROACH = Approach(lookback_bars=6, min_distance_atr=5.0, touch_atr=0.3)
PARAMS = ActionParams(breakout_atr=2.0, retest_window_bars=8)
PLAT_BELOW = (1989.5, 1990.0, 1989.0, 1989.5)


def make_bars(rows, *, start="2026-03-02", tf="H1"):
    secs = TIMEFRAME_SECONDS[tf]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "time", pd.date_range(start, periods=len(rows), freq=f"{secs}s"))
    df["tick_volume"], df["spread"], df["real_volume"] = 10, 5, 0
    return df[_COLUMNS]


def path_2000_resistance():
    """26 根平台后 anchor（high 触带、收盘未站上）→ A3；随后突破/回踩/跌回。"""
    rows = [PLAT_BELOW] * 26
    rows.append((1992.0, 1999.9, 1991.0, 1997.0))    # 26: anchor（A3）
    rows.append((1997.0, 1998.0, 1996.0, 1997.5))    # 27
    rows.append((1997.5, 1998.0, 1996.5, 1998.0))    # 28
    rows.append((1998.0, 1998.5, 1997.0, 1998.0))    # 29
    rows.append((1998.5, 2005.5, 1998.0, 2005.0))    # 30: 收盘远超 2000+2·ATR → A1
    rows.append((2005.0, 2006.0, 2004.0, 2004.0))    # 31
    rows.append((2004.0, 2004.5, 1999.5, 2000.2))    # 32: 回踩 2000+0.25·ATR 带内 → A2
    rows.append((2000.2, 2001.0, 1997.5, 1998.0))    # 33: 收盘跌回价位下 → A4
    rows += [PLAT_BELOW] * 3
    return make_bars(rows)


def triggers_of(bars):
    atr = atr_series(bars)
    events = find_level_events(
        bars, s1_level_rows(bars, grid_step=50.0, approach=APPROACH, atr=atr),
        situation="S1", approach=APPROACH, atr=atr)
    return action_triggers(bars, events, params=PARAMS, atr=atr), events


def test_action_params_validate():
    with pytest.raises(ValueError):
        ActionParams(breakout_atr=0.0, retest_window_bars=8)
    with pytest.raises(ValueError):
        ActionParams(breakout_atr=2.0, retest_window_bars=0)
    with pytest.raises(ValueError):
        ActionParams(breakout_atr=2.0, retest_window_bars=8, retest_band_atr=0.0)


def test_actions_hand_computed_resistance():
    """A3@anchor(−1)、A1@30(+1)、A2@32(+1)、A4@33(−1) 与手算一致。"""
    bars = path_2000_resistance()
    trig, events = triggers_of(bars)
    assert [(e.kind, e.level, e.bar) for e in events] == [("resistance", 2000.0, 26)]
    got = sorted(zip(trig["action"], trig["side"], trig["bar"]))
    assert got == [("A1", 1, 30), ("A2", 1, 32), ("A3", -1, 26), ("A4", -1, 33)]
    row = trig[trig["action"] == "A2"].iloc[0]
    assert row["level"] == 2000.0 and row["kind"] == "resistance"
    assert row["situation"] == "S1" and row["anchor"] == 26
    assert pd.Timestamp(row["time"]) == pd.Timestamp(bars["time"].iloc[32])


def test_actions_mirror_symmetry():
    """价格围绕 2000 反射（高低互换）后：kind 支撑、全部方向取反、bar 不变。"""
    bars = path_2000_resistance()
    refl = bars.copy()
    for col in ("open", "high", "low", "close"):
        refl[col] = 2 * 2000.0 - bars[col]
    refl["high"], refl["low"] = refl["low"], refl["high"]
    t1, _ = triggers_of(bars)
    t2, _ = triggers_of(refl)
    cols = ["action", "side", "bar", "level"]
    a = sorted(t1[cols].itertuples(index=False, name=None))
    b = sorted((act, -side, bar, 2 * 2000.0 - lvl)
               for act, side, bar, lvl in t2[cols].itertuples(index=False, name=None))
    assert a == b
    assert set(t2["kind"]) == {"support"}


def test_no_breakout_no_a1_a2_a4():
    """窗口内收盘从未站上价位：只有 A3，无 A1/A2/A4。"""
    rows = [PLAT_BELOW] * 26
    rows.append((1992.0, 1999.9, 1991.0, 1997.0))    # anchor（A3）
    rows += [(1997.0, 1998.0, 1996.0, 1997.5)] * 12  # 一直在价位下方
    bars = make_bars(rows)
    trig, events = triggers_of(bars)
    assert [(e.bar) for e in events] == [26]
    assert sorted(zip(trig["action"], trig["bar"])) == [("A3", 26)]


def test_a2_requires_pullback_into_band():
    """A1 后 m 根内回落但未进 ±0.25·ATR 带（仍在上方）→ 无 A2。"""
    rows = [PLAT_BELOW] * 26
    rows.append((1992.0, 1999.9, 1991.0, 1997.0))    # anchor
    rows.append((1998.0, 2005.5, 1998.0, 2005.0))    # A1 当根
    rows += [(2003.0, 2004.0, 2002.0, 2003.5)] * 8   # 回落但停在 2003.5 > 带
    bars = make_bars(rows)
    trig, _ = triggers_of(bars)
    acts = sorted(zip(trig["action"], trig["bar"]))
    assert ("A2", 27 + 4) not in acts and not (trig["action"] == "A2").any()
    assert ("A1", 27) in acts


def test_a4_window_expires():
    """站上价位后 m 根内没有跌回 → 无 A4。"""
    rows = [PLAT_BELOW] * 26
    rows.append((1992.0, 1999.9, 1991.0, 1997.0))    # anchor
    rows.append((1998.0, 2005.5, 1998.0, 2005.0))    # 站上（c=27，A1 同根）
    rows += [(2004.0, 2005.0, 2003.0, 2004.5)] * 12  # 8 根窗口内不跌回
    rows.append((2004.0, 2004.5, 1995.0, 1996.0))    # 第 40 根才跌回（>27+8）
    bars = make_bars(rows)
    trig, _ = triggers_of(bars)
    assert not (trig["action"] == "A4").any()


def test_support_actions_mirror_hand_case():
    """支撑情形直接手算：自上而下接近 2000，anchor 收盘仍 ≥ 价位 → A3(+1)。"""
    plat_above = (2010.5, 2011.0, 2010.0, 2010.5)
    rows = [plat_above] * 26
    rows.append((2008.0, 2009.0, 2000.1, 2003.0))    # anchor：low 触带，收盘 2003 ≥ 2000
    rows.append((2003.0, 2003.5, 2002.0, 2002.5))    # 未跌破 2000−2·ATR
    rows.append((2002.5, 2003.0, 1994.0, 1995.0))    # 收盘 1995 < 2000−2·ATR → A1(−1)
    rows += [(1995.0, 1996.0, 1994.0, 1995.5)] * 2
    bars = make_bars(rows)
    trig, events = triggers_of(bars)
    assert [(e.kind, e.level, e.bar) for e in events] == [("support", 2000.0, 26)]
    got = sorted(zip(trig["action"], trig["side"], trig["bar"]))
    assert got == [("A1", -1, 28), ("A3", 1, 26)]
