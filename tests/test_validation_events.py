"""T66/D35 低频事件模式：月末/伦敦定盘事件窗、净收益成本口径、年块自助
单侧检验、事件时间随机化零假设与植入正对照（蓝图 v4 §6.4）。

失败测试先行（计划五条）：
1. 月末事件跳过周末与节假日（合成日历含周末+假日）；
2. 伦敦 16:00 在 3 月/10 月切换周换算正确（合成 DST 数据，zoneinfo）；
3. 事件收益扣成本与隔夜费（手算闭式对照，与 T62 预筛同口径）；
4. 同种子结果逐位一致；
5. 随机化后植入效应消失、未植入时误报率接近名义水平（合成数据 1000 次）。

支撑覆盖：多品种同日聚合（相关性调整简化实现）、检验力/样本量闭式、
事件窗波动 σ、植入工具 plant_event_effect、随机化分层结构保持。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from mt5quant.costs.swap import SwapModel, SwapRule
from mt5quant.validation import events as ev


# ---------------------------------------------------------------- 测试替身

class ConstCosts:
    """常数成本替身（价格单位点差/滑点；每手佣金），可选带 SwapModel。"""

    def __init__(self, spread=0.50, slippage=0.20, commission=2.0,
                 swap: SwapModel | None = None):
        self._spread, self._slip = spread, slippage
        self._commission = commission
        self.swap = swap

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return self._spread

    def slippage(self, symbol, hour_ny, *, price=None):
        return self._slip

    def commission_per_lot(self, symbol):
        return self._commission


def _ny_hour(ts: pd.Timestamp) -> int:
    return int(ts.tz_localize("UTC").tz_convert("America/New_York").hour)


def synthetic_trading_days(start: str, periods: int, *,
                           drop: set[date] | None = None,
                           skip_weekends: bool = True) -> pd.DatetimeIndex:
    """合成交易日历：逐日生成，可剔除指定日期（假日）、可含周末。"""
    drop = drop or set()
    days, d = [], pd.Timestamp(start).date()
    while len(days) < periods:
        if (not skip_weekends or d.weekday() < 5) and d not in drop:
            days.append(pd.Timestamp(d))
        d += pd.Timedelta(days=1)
    return pd.DatetimeIndex(days)


def daily_bars(days: pd.DatetimeIndex, closes=None, *, s0=100.0,
               seed=7, vol=0.0) -> pd.DataFrame:
    """D1 K 线（open=close=给定/随机游走收盘；close-only 源同构）。"""
    n = len(days)
    if closes is None:
        rng = np.random.default_rng(seed)
        closes = s0 * np.cumprod(1.0 + rng.normal(0.0, vol or 1e-8, n))
    arr = np.asarray(closes, dtype="float64")
    return pd.DataFrame({
        "time": days,
        "open": arr, "high": arr, "low": arr, "close": arr,
        "tick_volume": 1, "spread": 0, "real_volume": 0,
    })


def h1_bars(days: pd.DatetimeIndex, *, s0=1.10, seed=11,
            vol=0.001) -> pd.DataFrame:
    """H1 K 线：每 24 根/日（00:00–23:00 UTC），随机游走收盘。"""
    rng = np.random.default_rng(seed)
    n = len(days) * 24
    closes = s0 * np.cumprod(1.0 + rng.normal(0.0, vol, n))
    opens = np.concatenate(([s0], closes[:-1]))
    times = pd.DatetimeIndex(
        [d + pd.Timedelta(hours=h) for d in days for h in range(24)])
    return pd.DataFrame({
        "time": times, "open": opens,
        "high": np.maximum(opens, closes) * 1.0002,
        "low": np.minimum(opens, closes) * 0.9998, "close": closes,
        "tick_volume": 1, "spread": 0, "real_volume": 0,
    })


def always_long(event) -> str:
    return "long"


# ================================================================ 1. 月末事件

def test_month_end_events_skip_weekends_and_holidays():
    """月末事件落在真实交易日上：跳过周末与节假日，且为该月最后一根
    交易日；出场为次月第 first_n 根交易日（last_n/first_n 推广）。"""
    # 2026-01：1 日假日；1-31 是周六 → 一月最后交易日 = 1-30（周五）
    days = synthetic_trading_days("2026-01-01", 90,
                                  drop={date(2026, 1, 1), date(2026, 2, 16)})
    evs = ev.month_end_events(days, last_n=1, first_n=3)
    by_label = {e.label: e for e in evs}
    assert "month_end:2026-01" in by_label
    e = by_label["month_end:2026-01"]
    assert e.entry_time == pd.Timestamp("2026-01-30")   # 31 日是周六 → 30 日
    assert e.entry_time in set(days)
    assert e.kind == "month_end"
    # 2 月：16 日假日剔除后，月末窗的入场仍在 2-27（周五），不受月中假日影响
    e2 = by_label["month_end:2026-02"]
    assert e2.entry_time == pd.Timestamp("2026-02-27")
    # 出场 = 次月第 3 根交易日（3-2 周一、3-3、3-4 → 第 3 根 = 3-4）
    feb_days = [d for d in days if d.month == 3]
    assert e2.exit_time == feb_days[2]
    # 每个事件入场都是所在月最后一根交易日（无周末/假日混入）
    for e in evs:
        in_month = [d for d in days
                    if (d.year, d.month) == (e.entry_time.year,
                                             e.entry_time.month)]
        assert e.entry_time == in_month[-1]
    # 末月没有"次月前三根"可指 → 不产事件
    months_in = {(d.year, d.month) for d in days}
    assert len(evs) == len(months_in) - 1


def test_month_end_events_last_n_generalizes():
    """last_n>1：入场提前到该月倒数第 last_n 根交易日（月末窗起点）。"""
    days = synthetic_trading_days("2026-01-01", 120)
    evs = ev.month_end_events(days, last_n=2, first_n=1)
    e = {x.label: x for x in evs}["month_end:2026-01"]
    jan = [d for d in days if d.month == 1]
    assert e.entry_time == jan[-2]
    feb = [d for d in days if d.month == 2]
    assert e.exit_time == feb[0]


# ================================================================ 2. 伦敦定盘

def test_london_fix_events_dst_march_and_october():
    """伦敦 16:00 定盘：3 月/10 月切换周 UTC 换算正确（zoneinfo 处理 DST）。

    欧洲 2026：3-29（周日）01:00 GMT 拨快 → 3-27（周五）16:00 GMT=16:00 UTC，
    3-30（周一）16:00 BST=15:00 UTC；10-25（周日）02:00 BST 拨回 →
    10-23（周五）= 15:00 UTC、10-26（周一）= 16:00 UTC。lead 60 分钟。
    """
    days = synthetic_trading_days("2026-03-23", 7)
    evs = {e.label: e for e in
           ev.london_fix_events(days, tz="Europe/London", hhmm="16:00",
                                lead_minutes=60)}
    assert evs["london_fix:2026-03-27"].exit_time == pd.Timestamp(
        "2026-03-27 16:00")           # GMT 周五：16:00 UTC
    assert evs["london_fix:2026-03-30"].exit_time == pd.Timestamp(
        "2026-03-30 15:00")           # BST 周一：15:00 UTC
    assert evs["london_fix:2026-03-27"].entry_time == pd.Timestamp(
        "2026-03-27 15:00")           # lead 60 分钟
    days = synthetic_trading_days("2026-10-19", 7)
    evs = {e.label: e for e in
           ev.london_fix_events(days, tz="Europe/London", hhmm="16:00",
                                lead_minutes=60)}
    assert evs["london_fix:2026-10-23"].exit_time == pd.Timestamp(
        "2026-10-23 15:00")           # BST 周五
    assert evs["london_fix:2026-10-26"].exit_time == pd.Timestamp(
        "2026-10-26 16:00")           # GMT 周一
    # 每个事件 entry = exit − lead
    for e in evs.values():
        assert e.exit_time - e.entry_time == pd.Timedelta(minutes=60)
        # 独立换算核对（datetime + zoneinfo 逐事件对照）
        local = datetime(e.exit_time.date().year, e.exit_time.date().month,
                         e.exit_time.date().day, 16, 0,
                         tzinfo=ZoneInfo("Europe/London"))
        assert e.exit_time == pd.Timestamp(
            local.astimezone(timezone.utc).replace(tzinfo=None))


def test_london_fix_events_custom_tz_and_hhmm():
    """非缺省时区/时刻（美东 17:00）同样换算正确。"""
    days = pd.DatetimeIndex([pd.Timestamp("2026-01-05"),
                             pd.Timestamp("2026-07-06")])
    evs = {e.label: e for e in ev.london_fix_events(
        days, tz="America/New_York", hhmm="17:00", lead_minutes=30)}
    assert evs["london_fix:2026-01-05"].exit_time == pd.Timestamp(
        "2026-01-05 22:00")           # EST：UTC+0 → 17+5
    assert evs["london_fix:2026-07-06"].exit_time == pd.Timestamp(
        "2026-07-06 21:00")           # EDT：UTC−4 → 17+4


# ================================================================ 3. 成本口径

def test_event_returns_costs_and_overnight_closed_form():
    """事件净收益扣成本与隔夜费——手算闭式对照（与 T62 预筛同口径：
    点差来回全幅一次（入/出场小时较不利侧）/滑点双边/佣金每手来回/
    隔夜取更不利侧费率 × 实际计费夜数）。"""
    days = synthetic_trading_days("2024-01-02", 45)   # 2024-01-02 起，无假日
    closes = np.linspace(100.0, 145.0, len(days))
    bars = daily_bars(days, closes)
    events = ev.month_end_events(days, last_n=1, first_n=3)
    # 无三倍日的恒定费率规则（周三三倍默认改为无三倍，闭式可手算）
    swap = SwapModel({"DEMO": SwapRule(long_per_day=-1.0, short_per_day=-2.0,
                                      triple_weekdays=frozenset())})
    costs = ConstCosts(spread=0.50, slippage=0.20, commission=2.0, swap=swap)
    out = ev.event_returns(bars, events, side_fn=always_long, costs=costs,
                           symbol="DEMO", contract_size=10.0)
    assert len(out) == len(events)
    row = out.iloc[0]
    # 入场 = 1 月最后交易日（2024-01-31）bar 开盘；出场 = 2 月第 3 根开盘
    i_e = days.get_loc(pd.Timestamp("2024-01-31"))
    i_x = days.get_loc([d for d in days if d.month == 2][2])
    assert row.entry_bar_time == days[i_e]
    assert row.exit_bar_time == days[i_x]
    assert row.side == "long"
    entry, exit_ = closes[i_e], closes[i_x]
    assert row.entry_price == pytest.approx(entry)
    assert row.exit_price == pytest.approx(exit_)
    gross = exit_ / entry - 1.0
    assert row.gross == pytest.approx(gross)
    # 点差一次（较不利侧=常数）、滑点双边、佣金每手来回、隔夜更不利侧
    # （费率按参考价 = 序列最后收盘折成相对比率——每手金额跨价格水平外推）
    ref = closes[-1]                             # linspace 终点 145.0
    notional = 10.0 * ref
    spread = 0.50 / entry
    slip = 2.0 * 0.20 / entry
    comm = 2.0 / notional
    rule = swap.rule("DEMO")
    from mt5quant.costs.swap import charge_nights
    nights = charge_nights(days[i_e], days[i_x], rule)
    worst = max(abs(-1.0), abs(-2.0))          # short 每夜 2 → 更不利
    overnight = nights * worst / notional
    assert row.spread == pytest.approx(spread)
    assert row.slippage == pytest.approx(slip)
    assert row.commission == pytest.approx(comm)
    assert row.nights == nights
    assert row.overnight == pytest.approx(overnight)
    assert row.cost == pytest.approx(spread + slip + comm + overnight)
    assert row.net == pytest.approx(gross - (spread + slip + comm + overnight),
                                    rel=1e-12)
    # 成本口径与 T62 预筛一致：净值 = 毛 −（点差+滑点+佣金+隔夜）
    assert row.net < row.gross


def test_event_returns_short_side_and_intraday_window():
    """空头符号与日内（伦敦定盘）窗的 bar 解析：entry/exit 取各自时点后
    首根 bar 的开盘价；空头净收益 = −毛 − 成本。"""
    days = synthetic_trading_days("2026-03-23", 10)
    bars = h1_bars(days)
    events = ev.london_fix_events(days, lead_minutes=60)
    costs = ConstCosts(spread=0.0, slippage=0.0, commission=0.0)
    out = ev.event_returns(bars, events, side_fn=lambda e: "short",
                           costs=costs, symbol="DEMO")
    assert len(out) == len(events)
    row = out.iloc[0]
    e_time = events[0].entry_time
    x_time = events[0].exit_time
    times = bars["time"]
    i_e = int(np.searchsorted(times.to_numpy(), np.datetime64(e_time)))
    i_x = int(np.searchsorted(times.to_numpy(), np.datetime64(x_time)))
    assert row.entry_bar_time == times.iloc[i_e]
    assert row.exit_bar_time == times.iloc[i_x]
    gross = -(bars["open"].iloc[i_x] / bars["open"].iloc[i_e] - 1.0)
    assert row.gross == pytest.approx(gross)
    assert row.net == pytest.approx(gross)      # 零成本替身
    assert (out["net"] < out["gross"] + 1e-15).all()


def test_event_returns_drops_unresolvable_windows():
    """出场越过数据末尾的事件被剔除（不静默外推），attrs 记录剔除数。"""
    days = synthetic_trading_days("2026-01-01", 46)   # 1 月/2 月全 + 3 月 5 根
    bars = daily_bars(days)
    events = ev.month_end_events(days, last_n=1, first_n=3)
    assert len(events) >= 2
    short_bars = bars.iloc[:-4]                  # 砍掉 3 月尾部 → 末事件出不了场
    out = ev.event_returns(short_bars, events, side_fn=always_long,
                           costs=ConstCosts(spread=0.0, slippage=0.0,
                                           commission=0.0), symbol="DEMO")
    assert len(out) == len(events) - 1
    assert out.attrs["dropped"] == 1


# ================================================================ 4. 逐位一致

def test_same_seed_bitwise_identical():
    """同种子逐位一致：randomize_event_times / event_test /
    randomization_test 同种子两次调用输出完全相等（含 p 值浮点位）。"""
    days = synthetic_trading_days("2020-01-01", 800)
    bars = daily_bars(days, s0=100.0, seed=3, vol=0.01)
    events = ev.month_end_events(days)
    costs = ConstCosts(spread=0.0, slippage=0.0, commission=0.0)
    r1 = ev.randomize_event_times(events, days, seed=42)
    r2 = ev.randomize_event_times(events, days, seed=42)
    assert r1 == r2                              # frozen dataclass 列表相等
    assert ev.randomize_event_times(events, days, seed=43) != r1
    rets1 = ev.event_returns(bars, events, side_fn=always_long, costs=costs,
                             symbol="DEMO")
    rets2 = ev.event_returns(bars, events, side_fn=always_long, costs=costs,
                             symbol="DEMO")
    pd.testing.assert_frame_equal(rets1, rets2)
    t1 = ev.event_test(rets1, sims=500, seed=9)
    t2 = ev.event_test(rets1, sims=500, seed=9)
    assert t1 == t2
    assert math.isnan(t1["p_value"]) is False
    rt1 = ev.randomization_test(bars, events, side_fn=always_long,
                                costs=costs, symbol="DEMO", sims=99, seed=5)
    rt2 = ev.randomization_test(bars, events, side_fn=always_long,
                                costs=costs, symbol="DEMO", sims=99, seed=5)
    assert rt1 == rt2
    assert rt1["p_value"] != ev.randomization_test(
        bars, events, side_fn=always_long, costs=costs, symbol="DEMO",
        sims=99, seed=6)["p_value"]


# ============================================== 5. 随机化零假设（合成 1000 次）

def test_randomized_null_false_positive_and_plant_disappears():
    """合成随机游走（无漂移）上 1000 次：
    - 未植入：随机化事件时间的检验误报率接近名义 5%（每次迭代**重新生成
      数据**——固定单一实现只随机化日程时，p 值簇拥在该实现的条件均值
      附近，名义水平只对"数据×日程"联合零假设成立）。样本取 40 个年块：
      年块自助百分位检验的经验水平随块数收敛（合成 iid 实测 6 块约
      9.5%、12 块约 7.8%、20 块约 6.1%、40 块约 5.05%——有效临界分布
      ≈ t_{B−1}），40 块才谈得上"接近名义"；
    - 植入 δ=1σ窗：真实事件时间检出率 ≥ 90%，随机化后检出率回落到
      名义水平（植入效应对随机化零假设不可见——matched 抽样池不含原窗）。
    """
    rng_days = synthetic_trading_days("1986-01-01", 252 * 40)   # ~40 个年块
    events = ev.month_end_events(rng_days)
    costs = ConstCosts(spread=0.0, slippage=0.0, commission=0.0)
    sims_test, alpha = 200, 0.05

    # 未植入：1000 个种子，数据与日程都随种子更新 → 无条件误报率
    rejections = 0
    for k in range(1000):
        bars_k = daily_bars(rng_days, s0=100.0, seed=500_000 + k, vol=0.01)
        rand = ev.randomize_event_times(events, rng_days, seed=1_000_000 + k)
        rets = ev.event_returns(bars_k, rand, side_fn=always_long, costs=costs,
                                symbol="DEMO")
        res = ev.event_test(rets, sims=sims_test, seed=k)
        rejections += res["p_value"] < alpha
    fp = rejections / 1000.0
    assert 0.02 <= fp <= 0.09, f"未植入误报率 {fp:.3f} 偏离名义 5%"

    # 植入 δ = 1 × 事件窗波动 σ（逐种子重新生成数据并植入真实事件窗）
    bars0 = daily_bars(rng_days, s0=100.0, seed=101, vol=0.01)
    sigma_w = ev.window_volatility(bars0, hold_bars=int(
        (events[0].exit_time - events[0].entry_time).days))
    det_true = 0
    det_rand = 0
    n_power = 200
    for k in range(n_power):
        bars_k = daily_bars(rng_days, s0=100.0, seed=700_000 + k, vol=0.01)
        planted = ev.plant_event_effect(bars_k, events, 1.0 * sigma_w)
        rets = ev.event_returns(planted, events, side_fn=always_long,
                                costs=costs, symbol="DEMO")
        det_true += ev.event_test(rets, sims=sims_test, seed=10_000 + k
                                  )["p_value"] < alpha
        rand = ev.randomize_event_times(events, rng_days, seed=2_000_000 + k)
        rets = ev.event_returns(planted, rand, side_fn=always_long,
                                costs=costs, symbol="DEMO")
        det_rand += ev.event_test(rets, sims=sims_test,
                                  seed=20_000 + k)["p_value"] < alpha
    power_true, power_rand = det_true / n_power, det_rand / n_power
    assert power_true >= 0.90, f"真实事件时间检出率 {power_true:.2f} < 90%"
    assert power_rand <= 0.15, (
        f"随机化后检出率 {power_rand:.2f} 未回到名义水平——植入效应"
        "应被随机化零假设抹掉")


def test_randomize_preserves_weekday_and_month_position():
    """随机化保持月内位置（分位）与星期分布（事件层的边缘分布）。"""
    days = synthetic_trading_days("2000-01-03", 252 * 8)
    events = ev.month_end_events(days)
    rand = ev.randomize_event_times(events, days, seed=77)
    orig_days = [e.entry_time for e in events]
    rand_days = [e.entry_time for e in rand]
    # 星期分布逐位保持
    assert sorted(d.weekday() for d in orig_days) == \
        sorted(d.weekday() for d in rand_days)
    # 月内位置：入场的"月内交易日序/当月天数"三分位桶分布保持
    info = {d: _month_position(d, days) for d in set(orig_days) | set(rand_days)}
    bucket = lambda d: min(2, info[d][0] * 3 // max(1, info[d][1]))  # noqa: E731
    assert sorted(bucket(d) for d in orig_days) == \
        sorted(bucket(d) for d in rand_days)
    # 窗长（交易日根数）保持
    idx = {d: i for i, d in enumerate(days)}
    for e0, e1 in zip(events, rand):
        h0 = idx[e0.exit_time.normalize()] - idx[e0.entry_time.normalize()]
        h1 = idx[e1.exit_time.normalize()] - idx[e1.entry_time.normalize()]
        assert h0 == h1
    # 且随机化事件不再与原事件窗重叠（matched 抽样池）
    orig_spans = [(idx[e.entry_time.normalize()],
                   idx[e.exit_time.normalize()]) for e in events]
    for e in rand:
        a, b = idx[e.entry_time.normalize()], idx[e.exit_time.normalize()]
        for x, y in orig_spans:
            assert b < x or a > y


def _month_position(day: pd.Timestamp, days: pd.DatetimeIndex) -> tuple[int, int]:
    in_month = [d for d in days
                if (d.year, d.month) == (day.year, day.month)]
    return in_month.index(day), len(in_month)


# ================================================================ 支撑覆盖

def test_event_test_year_block_bootstrap():
    """年块自助单侧检验：正值显著 → p 小；零均值 → p 不小；块数=年数。"""
    rng = np.random.default_rng(35)         # 噪声实现 t≈−0.19（不走运偏移）
    frame = pd.DataFrame({
        "entry_time": pd.date_range("2018-01-01", periods=60, freq="MS"),
        "net": 0.008 + rng.normal(0.0, 0.02, 60),   # +0.8%/事件，σ=2%
    })
    res = ev.event_test(frame, sims=1000, seed=1)
    assert res["block"] == "year"
    assert res["n_blocks"] == 5
    assert 0.0 <= res["p_value"] <= 1.0
    assert res["p_value"] < 0.05            # 60 事件 ×0.8%/2% → t≈3.1 检出
    zero = pd.DataFrame({
        "entry_time": pd.date_range("2018-01-01", periods=60, freq="MS"),
        "net": rng.normal(0.0, 0.02, 60),
    })
    assert abs(zero["net"].mean() / (zero["net"].std(ddof=1)
                                     / np.sqrt(60))) < 1.5   # 零均值未走运
    assert ev.event_test(zero, sims=1000, seed=2)["p_value"] > 0.05
    with pytest.raises(ValueError, match="block"):
        ev.event_test(frame, block="month", sims=100, seed=1)


def test_event_test_accepts_series_with_datetime_index():
    """聚合后的日序列（DatetimeIndex）同样可检验。"""
    rng = np.random.default_rng(6)
    s = pd.Series(rng.normal(0.001, 0.01, 100),
                  index=pd.date_range("2015-01-01", periods=100, freq="MS"))
    res = ev.event_test(s, sims=500, seed=3)
    assert res["n_events"] == 100
    assert res["n_blocks"] == 9


def test_aggregate_daily_merges_symbols():
    """多品种合并：同一天的事件按日聚合（等权均值）后再检验——
    同日跨品种相关性被折叠成单一观测。"""
    days = synthetic_trading_days("2022-01-03", 400)
    events = ev.month_end_events(days)
    frame = pd.DataFrame({
        "entry_time": [e.entry_time for e in events],
        "net": np.arange(len(events), dtype="float64"),
    })
    frame_a = frame.assign(net=frame["net"])
    frame_b = frame.assign(net=frame["net"] * 10.0)
    daily = ev.aggregate_daily({"AAA": frame_a, "BBB": frame_b})
    assert isinstance(daily.index, pd.DatetimeIndex)
    assert len(daily) == len(events)         # 同一天两品种 → 一行
    assert daily.iloc[0] == pytest.approx((0.0 + 0.0) / 2.0)
    assert daily.iloc[1] == pytest.approx((1.0 + 10.0) / 2.0)
    res = ev.event_test(daily, sims=200, seed=4)
    assert res["n_events"] == len(events)


def test_power_and_required_events_consistent():
    """检验力闭式（Lo 口径、power.py 同一 z 约定）与样本量规则自洽：
    required_events 得到的 n 代回 event_power ≥ 目标；n−1 不足。"""
    sigma, effect = 0.02, 0.005              # δ/σ = 0.25
    n = ev.required_events(effect, sigma, power=0.5)
    assert n >= 1
    assert ev.event_power(effect, sigma, n) >= 0.5
    assert ev.event_power(effect, sigma, n - 1) < 0.5
    # 大效应 → 少事件；小效应 → 多事件（单调）
    assert ev.required_events(0.01, 0.02, power=0.5) < n
    assert ev.required_events(0.001, 0.02, power=0.5) > n
    # 检验力随事件数与效应单调上升
    assert ev.event_power(effect, sigma, 50) < ev.event_power(effect, sigma, 200)
    assert ev.event_power(0.002, 0.02, 100) < ev.event_power(0.006, 0.02, 100)


def test_window_volatility_matches_hand_computed():
    """事件窗波动 σ：同窗长全样本滚动窗口收益的样本标准差（随机时点口径，
    与 T80/G2 的 σ 同思想、作用在窗口长度上）。"""
    days = synthetic_trading_days("2020-01-01", 60)
    closes = np.arange(1.0, 61.0)            # 每日 +1/价格 ≈ 线性
    bars = daily_bars(days, closes)
    sigma = ev.window_volatility(bars, hold_bars=3)
    rets = closes[3:] / closes[:-3] - 1.0
    assert sigma == pytest.approx(float(np.std(rets, ddof=1)))
    assert ev.window_volatility(bars, hold_bars=1) == pytest.approx(
        float(np.std(closes[1:] / closes[:-1] - 1.0, ddof=1)))


def test_required_events_bonferroni_n_tests():
    """T84（D35 修改 4）失败测试：样本量规则按 α/N——z 走 z_{α/N}
    （_z_alpha 同一 Bonferroni 约定）；与第十三轮探针 B 节对照：
    每事件 SR=0.2、检验力 50% 时 N=1/2/6/24 → 70/98/147/210 个事件。"""
    sigma, effect = 0.01, 0.002              # SR = 0.2
    assert ev.required_events(effect, sigma, power=0.5) == 70
    assert ev.required_events(effect, sigma, power=0.5, n_tests=1) == 70
    assert ev.required_events(effect, sigma, power=0.5, n_tests=2) == 98
    assert ev.required_events(effect, sigma, power=0.5, n_tests=6) == 147
    assert ev.required_events(effect, sigma, power=0.5, n_tests=24) == 210
    # N 越大所需事件越多（单调）
    assert ev.required_events(effect, sigma, power=0.5, n_tests=3) \
        > ev.required_events(effect, sigma, power=0.5, n_tests=2)
    # 非法 N（0、布尔、非整数）→ ValueError（fail-closed）
    for bad in (0, -1, True, 2.0):
        with pytest.raises(ValueError):
            ev.required_events(effect, sigma, n_tests=bad)


def test_window_volatility_4day_iid_about_double_single_day():
    """T84/K4 失败测试 1：σ 按事件窗长度取——独立同分布合成数据上
    4 日窗的 σ ≈ 单日 σ 的 2 倍（σ_h = σ_1·√h；单根 bar 口径少算 √h、
    隐含夏普多算 √h——K4 的 C001 反例）。"""
    rng = np.random.default_rng(20261028)
    n = 4000
    closes = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, n))
    days = pd.date_range("2015-01-01", periods=n, freq="D")
    bars = daily_bars(days, closes)
    s1 = ev.window_volatility(bars, hold_bars=1)
    s4 = ev.window_volatility(bars, hold_bars=4)
    assert s4 == pytest.approx(2.0 * s1, rel=0.1), \
        f"4 日窗 σ {s4:.4f} 应 ≈ 2×单日 σ（{2.0 * s1:.4f}）"
    assert ev.window_volatility(bars, hold_bars=9) \
        == pytest.approx(3.0 * s1, rel=0.1)


def test_window_volatility_entry_hour_fixes_time_of_day():
    """T84/K4 失败测试 2：日内按 entry_hour_ny 取同一时段起点——
    窗口起点 = 锚 bar 的**收盘时刻**（入场成交时点），只取纽约本地小时
    等于 entry_hour_ny 的锚；合成的时段异方差数据上（纽约 10→11 点窗
    波动 3 倍于其余小时），定盘前一时段的 σ 高于全天平均。

    冬令时段（EST=UTC-5，2021-01 起六周内无 DST 切换）：纽约 10:00
    = 15:00 UTC → 锚 bar = 14:00 UTC（收盘 15:00）、窗长 1 根 H1 的
    窗口收益 = 15:00→16:00 UTC。
    """
    rng = np.random.default_rng(20261028)
    days = synthetic_trading_days("2021-01-04", 40)
    n = len(days) * 24
    vols = np.full(n, 0.0004)
    times = pd.DatetimeIndex(
        [d + pd.Timedelta(hours=h) for d in days for h in range(24)])
    # 15:00→16:00 UTC（纽约 10→11 点）的 bar 收益波动放大 3 倍：
    # 单 bar 收益 r_i = closes[i]/closes[i-1]-1 覆盖 (t_[i-1]+1h, t_i+1h]，
    # 15:00 UTC 收盘的锚配上窗长 1 恰好取到 r_{i(15点)}
    vols[np.asarray([t.hour for t in times]) == 15] = 0.0012
    rets = rng.normal(0.0, 1.0, n) * vols
    closes = 1.10 * np.cumprod(1.0 + rets)
    opens = np.concatenate(([1.10], closes[:-1]))
    bars = pd.DataFrame({
        "time": times, "open": opens,
        "high": np.maximum(opens, closes) * 1.0002,
        "low": np.minimum(opens, closes) * 0.9998, "close": closes,
        "tick_volume": 1, "spread": 0, "real_volume": 0})
    all_day = ev.window_volatility(bars, hold_bars=1)
    fix_hour = ev.window_volatility(bars, hold_bars=1, entry_hour_ny=10)
    assert fix_hour == pytest.approx(0.0012, rel=0.25), \
        f"定盘前一小时（纽约 10 点窗）σ 应 ≈ 0.0012，得到 {fix_hour:.5f}"
    assert fix_hour > 1.5 * all_day, \
        f"定盘时段 σ {fix_hour:.5f} 应显著高于全天平均 {all_day:.5f}"
    # 全天平均不传时段 → 与逐 bar 全体一致（缺省行为不变）
    rets_all = closes[1:] / closes[:-1] - 1.0
    assert all_day == pytest.approx(float(np.std(rets_all, ddof=1)))
    # 非法时段/窗口数不足 → ValueError（fail-closed，σ 无定义）
    with pytest.raises(ValueError):
        ev.window_volatility(bars, hold_bars=1, entry_hour_ny=24)
    with pytest.raises(ValueError):
        ev.window_volatility(bars, hold_bars=1, entry_hour_ny=True)
    few = bars.iloc[:24]                     # 一天只有一个 10 点窗
    with pytest.raises(ValueError):
        ev.window_volatility(few, hold_bars=1, entry_hour_ny=10)


def test_plant_event_effect_shifts_window_return():
    """植入：出场端 ×(1+δ) → 事件毛收益近似 +δ、非事件窗不受影响。"""
    days = synthetic_trading_days("2020-01-01", 90)
    closes = np.full(len(days), 100.0)
    bars = daily_bars(days, closes)
    events = ev.month_end_events(days)
    planted = ev.plant_event_effect(bars, events, 0.01)
    costs = ConstCosts(spread=0.0, slippage=0.0, commission=0.0)
    out = ev.event_returns(planted, events, side_fn=always_long, costs=costs,
                           symbol="DEMO")
    assert (out["gross"] > 0.0099).all() and (out["gross"] < 0.0101).all()
    # 原数据不变（拷贝植入）
    pd.testing.assert_frame_equal(bars, daily_bars(days, closes))
    # 非事件日收益仍为 0
    mid = planted["close"].iloc[10] / planted["close"].iloc[9] - 1.0
    assert mid == pytest.approx(0.0)


# ================================================================ 标定脚本臂

def load_gate_calibration():
    """装载 scripts/gate_calibration.py（事件模式对照臂 T66/D35）。"""
    import importlib.util
    path = (Path(__file__).resolve().parents[1] / "scripts"
            / "gate_calibration.py")
    spec = importlib.util.spec_from_file_location("event_gate_calibration",
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["event_gate_calibration"] = mod
    spec.loader.exec_module(mod)
    return mod


class TinySwapCosts:
    """带 SwapModel 的小额常数成本（隔夜费口径进事件收益）。"""

    def __init__(self):
        self._c = ConstCosts(spread=0.01, slippage=0.005, commission=0.0)
        self.swap = SwapModel({"TST": SwapRule(
            long_per_day=-0.01, short_per_day=-0.02,
            triple_weekdays=frozenset())})

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return self._c.spread_p90(symbol, hour_ny, price=price)

    def slippage(self, symbol, hour_ny, *, price=None):
        return self._c.slippage(symbol, hour_ny, price=price)

    def commission_per_lot(self, symbol):
        return self._c.commission_per_lot(symbol)


def event_daily_bars(years: int = 8, seed: int = 21, vol: float = 0.01):
    """跨多年的随机游走 D1（月末事件 ≥ 2 个年块才可检验）。"""
    return daily_bars(synthetic_trading_days("2015-01-01", 252 * years),
                      s0=100.0, seed=seed, vol=vol)


def test_scaled_costs_multiplies_all_components():
    """成本 ×1.5 否决臂：四个成本入口（点差/滑点/佣金/隔夜费率）全部
    乘同一因子——事件模式走 spread_p90/slippage/commission/swap.rule。"""
    gc = load_gate_calibration()
    base = TinySwapCosts()
    scaled = gc._ScaledCosts(base, 1.5)
    assert scaled.spread_p90("TST", 10, price=100.0) == pytest.approx(0.015)
    assert scaled.slippage("TST", 10, price=100.0) == pytest.approx(0.0075)
    assert scaled.commission_per_lot("TST") == pytest.approx(0.0)
    r0, r1 = base.swap.rule("TST"), scaled.swap.rule("TST")
    assert r1.long_per_day == pytest.approx(r0.long_per_day * 1.5)
    assert r1.short_per_day == pytest.approx(r0.short_per_day * 1.5)
    # 三倍日/计费时刻等结构不动（只放大费率）
    assert r1.triple_weekdays == r0.triple_weekdays
    assert r1.charge_tz == r0.charge_tz


def test_event_mode_calibration_structure_and_reproducible():
    """标定脚本事件臂：结构（负对照误报率/正对照检出率/检验力曲线/
    样本量规则/观测诊断）齐全，同参数两次运行逐位一致，档位单调。"""
    gc = load_gate_calibration()
    bars = event_daily_bars()
    kwargs = dict(symbol="TST", timeframe="D1", costs=TinySwapCosts(),
                  contract_size=1.0, kind="month_end", neg_seeds=2,
                  levels=(0.5, 1.0), pos_seeds=2, test_sims=100,
                  neg_rand_sims=19, pos_rand_sims=19, power_n_grid=(24,))
    a = gc.event_mode_calibration(bars, **kwargs)
    b = gc.event_mode_calibration(bars, **kwargs)
    assert a == b                       # 同参数逐位一致
    for key in ("n_events", "sigma_window", "cost_mean", "hold_bars",
                "false_positive_rates", "detection_rates", "power_runs",
                "sample_size_rule", "negative_runs", "positive_runs",
                "observed_diagnostic"):
        assert key in a
    fp = a["false_positive_rates"]
    for key in ("reject_boot", "reject_rand", "reject_joint",
                "reject_joint_rand"):
        assert 0.0 <= fp[key] <= 1.0
    assert len(a["negative_runs"]) == 2
    assert len(a["positive_runs"]) == 4          # 2 档 × 2 种子
    assert a["detection_rates"]["1.0"]["reject_boot"] >= \
        a["detection_rates"]["0.5"]["reject_boot"]
    n50 = {r["level"]: r["required_events_power50"]
           for r in a["sample_size_rule"]["rows"]}
    assert n50[1.0] <= n50[0.5]                 # 大效应需要更少事件
    assert any(row["n_events"] == 24 for row in a["power_runs"])
    # 植入精度：1.0σ 档（随机化基线，零真实效应）的净收益均值显著高于
    # 未植入的随机化观测（负对照 boot_mean）
    raw_mean = min(r["boot_mean"] for r in a["negative_runs"])
    pos_mean = np.mean([r["boot_mean"] for r in a["positive_runs"]
                        if r["level"] == 1.0])
    assert pos_mean - raw_mean > 0.5 * a["sigma_window"]


def test_event_mode_calibration_rejects_short_samples():
    gc = load_gate_calibration()
    bars = event_daily_bars(years=1)
    with pytest.raises(ValueError, match="事件数"):
        gc.event_mode_calibration(bars, symbol="TST", timeframe="D1",
                                  costs=TinySwapCosts(), contract_size=1.0,
                                  neg_seeds=1, levels=(1.0,), pos_seeds=1,
                                  test_sims=50, neg_rand_sims=9,
                                  pos_rand_sims=9)


def test_cli_event_phase_end_to_end(tmp_path, monkeypatch):
    """--phase event 端到端：注入小样本 K 线与成本 → event-power.json/md
    落盘、退出码 0（不碰真实数据根；不写 gate-power.*）。"""
    gc = load_gate_calibration()
    import run_backtest

    monkeypatch.setattr(
        gc, "load_event_bars",
        lambda dr, sym, tf, *, ext_source=None, max_bars=None:
        (event_daily_bars(6), "test"))
    monkeypatch.setattr(run_backtest, "build_costs",
                        lambda *a, **k: (TinySwapCosts(), "test[v0]"))
    monkeypatch.setattr(run_backtest, "resolve_contract_size",
                        lambda *a, **k: 1.0)
    monkeypatch.setattr(run_backtest, "classify_store", lambda root: "research")
    monkeypatch.setattr(run_backtest, "sessions_sha256", lambda p: "0" * 64)
    monkeypatch.setattr(run_backtest, "SESSIONS_PATH", "config/sessions.toml")
    out_dir = tmp_path / "event"
    rc = gc.main(["--phase", "event", "--quick", "--event-symbols", "TST D1",
                  "--event-seeds", "2", "--event-levels", "1.0",
                  "--event-rand-sims", "19", "--out", str(out_dir)])
    assert rc == 0
    payload = json.loads((out_dir / "event-power.json").read_text(
        encoding="utf-8"))
    assert payload["meta"]["phase"] == "event"
    assert payload["meta"]["kind"] == "month_end"
    assert payload["arms"][0]["symbol"] == "TST"
    assert payload["arms"][0]["n_events"] > 0
    md = (out_dir / "event-power.md").read_text(encoding="utf-8")
    assert "事件模式对照实验" in md
    assert "负对照误报率" in md and "样本量规则" in md
    # event 臂不写 gate-power.json（与既有产物互不覆盖）
    assert not (out_dir / "gate-power.json").exists()


# ================================================ T86：关卡 v3 事件分支诊断助手

def test_shift_events_by_bars_moves_window_exactly_one_bar():
    """T86 失败测试：shift_events_by_bars（[v3.event] 诊断
    window_shift_1_bar 的构造器）——事件窗整体前后移 n 根 bar（bar 时长
    取序列最小正间隔），kind/label 原样保留。"""
    days = synthetic_trading_days("2024-01-01", 120)
    bars = daily_bars(days)
    evs = ev.month_end_events(days, last_n=1, first_n=2)
    assert len(evs) >= 3
    shifted = ev.shift_events_by_bars(evs, bars, n_bars=1)
    assert len(shifted) == len(evs)
    one_bar = pd.Timedelta(days=1)             # D1 序列：一根 bar = 1 天
    for s, e in zip(shifted, evs):
        assert s.entry_time == e.entry_time + one_bar
        assert s.exit_time == e.exit_time + one_bar
        assert s.kind == e.kind and s.label == e.label


def test_segmented_means_splits_by_fixed_year_blocks():
    """T86 失败测试：segmented_means（[v3.event] 诊断 segmented_means）——
    按 years 年一块分段算均值，块键 "YYYY-YYYY"，空段如实缺席。"""
    idx = pd.date_range("2000-01-01", periods=25, freq="YS")
    values = [float(i) for i in range(25)]
    seg = ev.segmented_means(pd.Series(values, index=idx), years=10)
    assert set(seg) == {"2000-2009", "2010-2019", "2020-2029"}
    assert seg["2000-2009"] == pytest.approx(sum(values[0:10]) / 10.0)
    assert seg["2010-2019"] == pytest.approx(sum(values[10:20]) / 10.0)
    assert seg["2020-2029"] == pytest.approx(sum(values[20:25]) / 5.0)


def test_v3_event_diagnostics_reports_four_items():
    """T86 失败测试：v3_event_diagnostics——四项诊断齐全（D35 修改 3：
    year_block_vs_zero / window_shift_1_bar / segmented_means / known_gaps），
    全部只报告（带 note，不抛异常、不含否决语义）；known_gaps 与事件窗
    相交时如实列出。"""
    days = synthetic_trading_days("2020-01-01", 365 * 4)
    bars = daily_bars(days, vol=0.002)
    evs = ev.month_end_events(days)
    assert len(evs) >= 40
    gap = (pd.Timestamp("2021-03-01"), pd.Timestamp("2021-03-20"))
    diags = ev.v3_event_diagnostics(
        bars, evs, side_fn=always_long, costs=ConstCosts(), symbol="TST",
        contract_size=100.0, known_gaps=[gap], sims=50, seed=3)
    assert set(diags) == {"year_block_vs_zero", "window_shift_1_bar",
                          "segmented_means", "known_gaps"}
    # 年块自助对 0：p 值与块数
    assert "p_value" in diags["year_block_vs_zero"]
    assert diags["year_block_vs_zero"]["n_blocks"] >= 3
    # 窗口前后移一根：净均值与事件数（末事件平移出数据尾被剔除——
    # dropped 如实计数，两侧相加守恒）
    assert "net_mean" in diags["window_shift_1_bar"]
    assert (diags["window_shift_1_bar"]["n_events"]
            + diags["window_shift_1_bar"]["dropped"]) == len(evs)
    # 分段均值：4 年数据一块（10 年分段）
    assert diags["segmented_means"]["segments"]
    # 已知缺口注记：与 2021-03 事件窗相交 → 命中 ≥1
    assert len(diags["known_gaps"]["hits"]) >= 1
    assert diags["known_gaps"]["passed"] is True   # 注记只报告，恒不否决


# ------------------------------------- H-07：ScaledCosts 的 swap 分量随 factor 缩放

def test_h07_robustness_scaled_costs_swap_scales_overnight():
    """H-07 失败测试：robustness.ScaledCosts 补 swap 分量——event_returns
    直接读 costs.swap.rule，修复前 ScaledCosts 无 swap → ×1.5 后隔夜费
    变 0（审核复现：原 0.002 → 缩放后 0）。修复后 0.002 → 0.003，
    factor=1 逐位一致。"""
    from mt5quant.validation import robustness as rob

    rule = SwapRule(long_per_day=-0.002, short_per_day=-0.002,
                    triple_weekdays=frozenset())
    base = ConstCosts(spread=0.0, slippage=0.0, commission=0.0,
                      swap=SwapModel({"TST": rule}))
    # D1 bars，收盘恒 1.0：ref_price=1.0、contract_size=1.0 → notional=1，
    # overnight（相对价格）= 夜数 × |费率|。窗口 (周一0点, 周二0点] 恰含
    # 周一 17:00 NY 计费时刻 → 1 夜。
    bars = daily_bars(pd.date_range("2024-01-01", periods=3, freq="D"),
                      closes=[1.0, 1.0, 1.0])
    events = [ev.EventWindow(kind="test", label="t1",
                             entry_time=pd.Timestamp("2024-01-01"),
                             exit_time=pd.Timestamp("2024-01-02"))]
    kw = dict(side_fn=always_long, symbol="TST", contract_size=1.0)

    frame = ev.event_returns(bars, events, costs=base, **kw)
    assert frame["overnight"].iloc[0] == pytest.approx(0.002)
    assert frame["nights"].iloc[0] == 1

    scaled = ev.event_returns(bars, events, costs=rob.ScaledCosts(base, 1.5),
                              **kw)
    assert scaled["overnight"].iloc[0] == pytest.approx(0.003)   # 非 0
    assert scaled["net"].iloc[0] == pytest.approx(
        frame["net"].iloc[0] - 0.001)

    same = ev.event_returns(bars, events, costs=rob.ScaledCosts(base, 1.0),
                            **kw)
    assert same["overnight"].iloc[0] == pytest.approx(0.002)      # factor=1
    assert same["net"].iloc[0] == pytest.approx(frame["net"].iloc[0])
