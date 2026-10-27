"""T74 局面库 v1：S1 整数关口 / S2 前一交易日高低点 / S3 20 日高低点。

手算对照 + 镜像对称（价格围绕常数反射后，阻力↔支撑互换）。
"""
import pandas as pd
import pytest

from mt5data.store import _COLUMNS, TIMEFRAME_SECONDS
from mt5quant.backtest.engine_a import atr_series
from mt5quant.strategies.library.prob.situations import (
    Approach, find_level_events, grid_levels_between, levels_from_frame,
    s1_level_rows, s2_level_rows, s3_level_rows,
)

APPROACH = Approach(lookback_bars=6, min_distance_atr=5.0, touch_atr=0.3)


def make_bars(rows, *, start="2026-03-02", tf="H1"):
    """rows: (open, high, low, close) 元组列表（全序列显式给定，无跳档）。"""
    secs = TIMEFRAME_SECONDS[tf]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "time", pd.date_range(start, periods=len(rows), freq=f"{secs}s"))
    df["tick_volume"], df["spread"], df["real_volume"] = 10, 5, 0
    return df[_COLUMNS]


# 平台 bar：TR 恒为 1（open=close=1989.5, high=1990, low=1989）
PLAT_BELOW = (1989.5, 1990.0, 1989.0, 1989.5)     # 距 2000 的 high 距离 = 10
PLAT_ABOVE = (2010.5, 2011.0, 2010.0, 2010.5)     # 距 2000 的 low 距离 = 10


def near_2000_from_below(n_warm=20, n_approach=6):
    rows = [PLAT_BELOW] * (n_warm + n_approach)
    rows.append((1992.0, 1999.9, 1991.0, 1997.0))          # anchor：high 触带
    rows += [PLAT_BELOW] * 3
    return make_bars(rows)


def near_2000_from_above(n_warm=20, n_approach=6):
    rows = [PLAT_ABOVE] * (n_warm + n_approach)
    rows.append((2008.0, 2009.0, 2000.1, 2003.0))          # anchor：low 触带
    rows += [PLAT_ABOVE] * 3
    return make_bars(rows)


def test_approach_validates():
    with pytest.raises(ValueError):
        Approach(lookback_bars=0, min_distance_atr=5.0, touch_atr=0.3)
    with pytest.raises(ValueError):
        Approach(lookback_bars=6, min_distance_atr=0.0, touch_atr=0.3)
    with pytest.raises(ValueError):
        Approach(lookback_bars=6, min_distance_atr=5.0, touch_atr=-0.1)


def test_grid_levels_between():
    assert grid_levels_between(50.0, 1990.0, 2010.0) == [2000.0]
    assert grid_levels_between(50.0, 1900.0, 2100.0) == [1900.0, 1950.0, 2000.0,
                                                         2050.0, 2100.0]
    assert grid_levels_between(0.0050, 1.0998, 1.1002) == [1.1000]


def test_s1_resistance_touch_hand_computed():
    bars = near_2000_from_below()
    atr = atr_series(bars)
    # anchor 自身 TR=10.4：ATR=(13×1+10.4)/14≈1.671（手算）
    assert atr.iloc[26] == pytest.approx(23.4 / 14, abs=1e-9)
    events = find_level_events(bars, s1_level_rows(bars, grid_step=50.0, approach=APPROACH, atr=atr_series(bars)),
                               situation="S1", approach=APPROACH, atr=atr)
    assert [(e.kind, e.level, e.bar) for e in events] == [("resistance", 2000.0, 26)]
    assert pd.Timestamp(events[0].time) == pd.Timestamp(bars["time"].iloc[26])


def test_s1_support_touch_and_mirror():
    bars_up = near_2000_from_above()
    events_up = find_level_events(
        bars_up, s1_level_rows(bars_up, grid_step=50.0, approach=APPROACH, atr=atr_series(bars_up)), situation="S1",
        approach=APPROACH, atr=atr_series(bars_up))
    assert [(e.kind, e.level, e.bar) for e in events_up] == [("support", 2000.0, 26)]

    # 镜像：围绕 2000 反射（p' = 4000 − p，高低互换）后阻力 ↔ 支撑
    bars_dn = near_2000_from_below()
    refl = bars_dn.copy()
    for col in ("open", "high", "low", "close"):
        refl[col] = 2 * 2000.0 - bars_dn[col]
    refl["high"], refl["low"] = refl["low"], refl["high"]
    e1 = find_level_events(bars_dn, s1_level_rows(bars_dn, grid_step=50.0, approach=APPROACH, atr=atr_series(bars_dn)),
                           situation="S1", approach=APPROACH,
                           atr=atr_series(bars_dn))
    e2 = find_level_events(refl, s1_level_rows(refl, grid_step=50.0, approach=APPROACH, atr=atr_series(refl)),
                           situation="S1", approach=APPROACH,
                           atr=atr_series(refl))
    assert [e.kind for e in e1] == ["resistance"]
    assert [(e.kind, e.bar) for e in e2] == [("support", 26)]


def test_s1_ramp_approach_accepted_new_definition():
    """审核修正 3：渐进接近——窗口内并非每根都 ≥ D·ATR（近处 2 根只有
    ~2 ATR），但曾在远端（10 ATR ≥ D=5）且全程带外同侧 → 触发。
    旧定义（每根都 ≥ D）会拒绝此路径。"""
    rows = [PLAT_BELOW] * 20                      # high=1990（距 2000 为 10）
    rows.append((1994.5, 1996.0, 1994.0, 1995.0))    # 20：抬进（距 4）
    rows.append((1996.0, 1997.0, 1995.5, 1996.5))    # 21（距 3，带外）
    rows.append((1996.5, 1997.4, 1996.0, 1997.0))    # 22（距 2.6）
    rows.append((1997.0, 1997.7, 1996.5, 1997.3))    # 23（距 2.3）
    rows.append((1997.3, 1997.9, 1996.8, 1997.6))    # 24（距 2.1，仍带外）
    rows.append((1998.0, 1999.9, 1997.5, 1998.5))    # 25：anchor 触带
    rows += [PLAT_BELOW] * 3
    bars = make_bars(rows)
    events = find_level_events(
        bars, s1_level_rows(bars, grid_step=50.0, approach=APPROACH,
                            atr=atr_series(bars)),
        situation="S1", approach=APPROACH, atr=atr_series(bars))
    # 旧定义下 bar 21-24 距价位 < D（5×ATR），事件不存在；新定义存在
    assert [(e.kind, e.level, e.bar) for e in events] == [("resistance", 2000.0, 25)]


def test_s1_approach_too_close_no_event():
    """回看窗内已有 bar 距价位不足 D×ATR（带内盘整后再碰）→ 无事件。"""
    rows = [(1996.5, 1998.0, 1995.0, 1996.5)] * 26        # high 距 2000 仅 2 < 5
    rows.append((1997.0, 1999.9, 1996.0, 1997.0))
    bars = make_bars(rows)
    events = find_level_events(bars, s1_level_rows(bars, grid_step=50.0, approach=APPROACH, atr=atr_series(bars)),
                               situation="S1", approach=APPROACH,
                               atr=atr_series(bars))
    assert events == []


def test_s1_atr_warmup_guarded():
    """ATR 不可得（回看不足）的 bar 不产生事件。"""
    bars = make_bars([PLAT_BELOW] * 3)
    atr = atr_series(bars)
    assert atr.isna().all()
    events = find_level_events(bars, s1_level_rows(bars, grid_step=50.0, approach=APPROACH, atr=atr_series(bars)),
                               situation="S1", approach=APPROACH, atr=atr)
    assert events == []


# ---------------------------------------------------------------- S2

def test_s2_prev_day_levels_and_dst():
    """前一交易日高低点：17:00 America/New_York 划界；DST 切换周边界随之时移。"""
    secs = TIMEFRAME_SECONDS["H1"]
    times = pd.date_range("2026-03-05", periods=24 * 5, freq=f"{secs}s")
    df = pd.DataFrame({
        "time": times, "open": 100.0, "high": 101.0, "low": 99.0,
        "close": 100.0, "tick_volume": 10, "spread": 5, "real_volume": 0,
    })[_COLUMNS]
    df.loc[10, "high"] = 111.0      # 03-05 会话最高
    df.loc[12, "low"] = 88.0        # 03-05 会话最低
    rows = s2_level_rows(df)
    # EST（03-08 切换前）：03-05 17:00 NY = 22:00 UTC → 22:00 起属 03-06 会话
    b22 = df.index[df["time"] == pd.Timestamp("2026-03-05 22:00")][0]
    assert rows.loc[b22 - 1, "sess"] != rows.loc[b22, "sess"]
    assert rows.loc[b22, "prev_high"] == 111.0
    assert rows.loc[b22, "prev_low"] == 88.0
    # 首会话内无前会话极值
    assert pd.isna(rows.loc[0, "prev_high"])
    # EDT（03-08 已切换）：03-09 17:00 NY = 21:00 UTC
    b21 = df.index[df["time"] == pd.Timestamp("2026-03-09 21:00")][0]
    assert rows.loc[b21 - 1, "sess"] != rows.loc[b21, "sess"]


def test_s2_event_via_generic_finder():
    """S2 价位经通用发现器产生事件：价格回升接近前一日高点 → 阻力。"""
    secs = TIMEFRAME_SECONDS["H1"]
    rows = []
    # 会话 1（03-02 22:00 UTC 起）… 简化：从 03-03 全天，高点 1101，TR≈1 平台
    plat = (1100.5, 1101.0, 1100.0, 1100.5)
    rows += [plat] * 24                        # 03-03 00:00–21:00 UTC（会话内）
    rows += [(1100.5, 1101.0, 1100.0, 1100.5)]  # 03-03 22:00 起：新会话（EDT）
    # 新会话价格低于 1101 平台 … 抬到 1094 平台（距 1101 为 7 ≥ 5×ATR；
    # 过渡 bar TR=7.5 使 ATR 残余 ~1.3，5×1.3≈6.5，距离 6 不够——手算取 7）
    plat2 = (1093.5, 1094.0, 1093.0, 1093.5)
    rows += [plat2] * 14
    rows.append((1097.0, 1100.9, 1096.0, 1098.0))    # high 触 1101 ±0.3 带
    rows += [plat2] * 3
    times = pd.date_range("2026-03-03", periods=len(rows), freq=f"{secs}s")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "time", times)
    df["tick_volume"], df["spread"], df["real_volume"] = 10, 5, 0
    bars = df[_COLUMNS]
    s2 = s2_level_rows(bars)
    anchor = len(rows) - 4
    assert s2.loc[anchor, "prev_high"] == 1101.0      # 手算：anchor 看到前会话高点
    events = find_level_events(
        bars, levels_from_frame(s2, columns=["prev_high", "prev_low"]),
        situation="S2", approach=APPROACH, atr=atr_series(bars))
    assert [(e.kind, e.level, e.bar) for e in events] == [("resistance", 1101.0, anchor)]


# ---------------------------------------------------------------- S3

def test_s3_channel_matches_b1_and_shift():
    """S3 = B1 同法（rolling 极值 shift 一根）：本根不进通道、无前视。"""
    rows = [(100.0, 101.0, 99.0, 100.0)] * 30
    bars = make_bars(rows)
    ch = s3_level_rows(bars, days=1, bars_per_day=24)   # 24 根窗便于手算
    hi = bars["high"].rolling(24).max().shift(1)
    lo = bars["low"].rolling(24).min().shift(1)
    assert (ch["chan_high"].dropna() == hi.dropna()).all()
    assert (ch["chan_low"].dropna() == lo.dropna()).all()
    assert ch["chan_high"].iloc[:24].isna().all()       # 窗满之前无通道
    assert ch["chan_high"].iloc[24] == 101.0
