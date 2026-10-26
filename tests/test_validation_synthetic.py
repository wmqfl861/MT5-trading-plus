"""T22/H7 合成对照（validation.synthetic）：负对照块置换/槽位置换、正对照
植入优势、PlantedEdgeStrategy / RandomEntries。

计划失败测试落点（第四轮 T22，docs/superpowers/plans/ 下相应计划）：
- 植入器 d=0 时数据逐位还原（test_plant_edge_zero_drift_bitwise_identity）；
- PlantedEdgeStrategy 通过 G0（截断 + 窗口不变性 + 静态扫描）
  （test_planted_edge_strategy_passes_g0）；
- 同一种子结果可复现（test_block_permute_seed_reproducible /
  test_plant_edge_deterministic）；
- 块置换保留日收益的窗口统计量（日内方差）而破坏跨日自相关
  （test_block_permute_preserves_daily_return_multiset /
  test_block_permute_preserves_intraday_variance /
  test_block_permute_destroys_cross_day_autocorrelation）。

第五轮 T39（M4，2026-10-26-round5-hardening）日内零假设 slot_permute_bars：
- 槽位收益多重集不变（test_slot_permute_preserves_slot_return_multiset）；
- 同种子逐位复现（test_slot_permute_seed_reproducible）；
- 植入"日内第 k 根 bar 漂移"的数据置换后漂移消失
  （test_slot_permute_destroys_planted_intraday_drift）。

第六轮 C7（2026-10-26-review-round6-codex）：slot 置换改为**联合置换**
(收益 r, bar 内相对路径 o/c、h/c、l/c) 四元组——旧实现只置换 close/close
收益而逐位保留 bar 内比值，恒有 new_close/new_open == old_close/old_open，
"开盘入场、收盘出场"的单 bar 优势原样存活（审核在仓库漂移样例复现：
置换前后开→收漂移均 ≈ 0.0303554846）。新语义下同一槽位的整根标准化 bar
一起换到新日期：
- 每个槽位的收益多重集不变（原测试照常成立）；
- 每个槽位的相对形状多重集（o/c、h/c、l/c）不变、但与日期解绑
  （test_slot_permute_preserves_slot_shape_multiset）；
- 单 bar 开→收漂移样例置换后条件漂移塌向槽位无条件均值
  （test_slot_permute_destroys_planted_open_close_drift）；
- bar 0 是锚点：不参与置换、四价逐位保留。
"""
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from mt5quant.backtest.engine_a import run_backtest
from mt5quant.strategies.base import MAX_PARAMS
from mt5quant.validation import lookahead as vlook
from mt5quant.validation.synthetic import (
    PlantedEdgeStrategy,
    RandomEntries,
    block_permute_bars,
    event_bars,
    plant_edge,
    slot_permute_bars,
)

NY_TZ = ZoneInfo("America/New_York")


def daily_bars(day_closes, bars_per_day=4, start="2025-01-01", bar_seconds=3600,
               intraday_vol=0.001, seed=7):
    """按日构造 OHLC：每天 bars_per_day 根，日内形状由 rng 给定，收盘贴
    day_closes（跨日由调用方控制——自相关检验需要）。

    日内路径：从 day_open 出发几何随机走到 day_close（首日 open=100）。
    """
    rng = np.random.default_rng(seed)
    rows = []
    t0 = pd.Timestamp(start)
    prev_close = None
    for d, close_target in enumerate(day_closes):
        base = prev_close if prev_close is not None else 100.0
        # 日内形状：随机增量归一后缩放，终点恰为 close_target
        steps = rng.normal(0.0, intraday_vol, bars_per_day)
        steps[0] = 0.0
        path = np.cumprod(1.0 + steps)
        path = base * (path / path[0])
        scale = close_target / path[-1]
        path = path * np.linspace(1.0, scale, bars_per_day)
        for k in range(bars_per_day):
            o = path[k]
            c = path[k]
            hi = max(o, c) * (1.0 + abs(rng.normal(0.0, intraday_vol / 4)))
            lo = min(o, c) * (1.0 - abs(rng.normal(0.0, intraday_vol / 4)))
            rows.append({
                "time": t0 + pd.Timedelta(
                    days=d, seconds=bar_seconds * k),
                "open": o, "high": hi, "low": lo, "close": c,
                "tick_volume": 1, "spread": 1, "real_volume": 0,
            })
        prev_close = close_target
    return pd.DataFrame(rows)


def ar1_day_returns(n_days, rho=0.85, seed=3, mu=0.0005):
    rng = np.random.default_rng(seed)
    rets = np.empty(n_days)
    r = 0.0
    for i in range(n_days):
        r = mu + rho * r + rng.normal(0.0, 0.004)
        rets[i] = r
    return rets


def lag1_autocorr(x):
    x = np.asarray(x, dtype="float64")
    x = x - x.mean()
    denom = float((x * x).sum())
    if denom <= 0:
        return 0.0
    return float((x[:-1] * x[1:]).sum() / denom)


# ---------------------------------------------------------------- 植入优势

def test_plant_edge_zero_drift_bitwise_identity():
    bars = daily_bars(100.0 * np.cumprod(1.0 + ar1_day_returns(20)), seed=5)
    out = plant_edge(bars, period_days=1, interval_hours=24, hold_bars=4,
                     drift=0.0)
    pd.testing.assert_frame_equal(out, bars, check_exact=True)


def test_plant_edge_adds_drift_exactly_k_bars_after_event():
    # 5 天 × 4 根/天、period=1、interval=24h → 每天首根 bar 是事件 bar
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    bars = daily_bars(closes, bars_per_day=4, seed=11)
    d, k = 0.75, 2
    out = plant_edge(bars, period_days=1, interval_hours=24, hold_bars=k,
                     drift=d)
    events = [0, 4, 8, 12, 16]
    expected = bars.copy()
    for i in events:
        for j in range(i + 1, min(i + 1 + k, len(bars))):
            for col in ("open", "high", "low", "close"):
                expected.loc[j, col] = bars.loc[j, col] + d
    pd.testing.assert_frame_equal(out, expected, check_exact=True)
    # 事件后第 k+1 根起未受影响（16 号事件的漂移到 18 为止，19 保持原值）
    assert out.loc[19, "close"] == bars.loc[19, "close"]


def test_plant_edge_deterministic_same_seed_same_output():
    bars = daily_bars(100.0 * np.cumprod(1.0 + ar1_day_returns(15)), seed=9)
    a = plant_edge(bars, period_days=1, interval_hours=24, hold_bars=4,
                   drift=1.5)
    b = plant_edge(bars, period_days=1, interval_hours=24, hold_bars=4,
                   drift=1.5)
    pd.testing.assert_frame_equal(a, b, check_exact=True)


def test_event_bars_daily_first_bar_with_period_one():
    bars = daily_bars([100.0, 101.0, 102.0, 103.0, 104.0], bars_per_day=4)
    ev = event_bars(bars, period_days=1, interval_hours=24)
    assert ev.tolist() == [True, False, False, False] * 5
    # period=2：纪元日本身 %2 的日子才合格——事件不再每天出现
    ev2 = event_bars(bars, period_days=2, interval_hours=24)
    assert ev2.sum() < ev.sum()
    # 因果性：任意前缀的事件掩码 = 全量掩码的前缀
    for cut in (7, 13, 19):
        pre = event_bars(bars.iloc[:cut], period_days=1, interval_hours=24)
        assert pre.tolist() == ev.tolist()[:cut]


def test_plant_edge_alignment_strategy_captures_drift():
    """植入器与策略是配套的：PlantedEdgeStrategy 在植入数据上每笔交易的
    毛利恰为 d（零成本、固定手数、恒定价格）——证明优势可被策略捕获。"""
    n_days, bpd = 12, 8
    t0 = pd.Timestamp("2025-01-01")
    times = [t0 + pd.Timedelta(days=d, hours=k)
             for d in range(n_days) for k in range(bpd)]
    bars = pd.DataFrame({
        "time": times,
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })
    d, k = 2.0, 4
    planted = plant_edge(bars, period_days=1, interval_hours=24, hold_bars=k,
                         drift=d)
    strat = PlantedEdgeStrategy(period_days=1, interval_hours=24)
    res = run_backtest(strat, planted, _ZeroCosts(), symbol="TST",
                       params={"hold_bars": k}, contract_size=1.0,
                       volume_lots=1.0, initial_capital=10_000.0,
                       timeframe="H1")
    assert len(res.trades) == n_days          # 每天一个事件 → 每天一笔空头
    assert set(res.trades["exit_reason"]) == {"signal"}
    assert res.trades["gross"].apply(lambda g: abs(g - d) < 1e-9).all()


class _ZeroCosts:
    def spread_p90(self, symbol, hour_ny, *, price=None):
        return 0.0

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


# ---------------------------------------------------------------- 策略

def test_planted_edge_strategy_params_within_limit_and_schema():
    assert len(PlantedEdgeStrategy.params_schema) <= MAX_PARAMS
    s = PlantedEdgeStrategy(period_days=1, interval_hours=24)
    assert s.validate_params({"hold_bars": 4}) == {"hold_bars": 4}
    with pytest.raises(ValueError):
        s.validate_params({"hold_bars": 5})   # 越网格


def test_planted_edge_strategy_signal_shape():
    bars = daily_bars([100.0, 101.0, 102.0, 103.0, 104.0], bars_per_day=8)
    s = PlantedEdgeStrategy(period_days=1, interval_hours=24)
    sig = s.generate_signals(bars, {"hold_bars": 4})
    assert set(pd.unique(sig)) <= {-1, 0}
    expected = np.zeros(len(bars), dtype="int64")
    for start in (0, 8, 16, 24, 32):     # 每天首根是事件 bar，信号持续 4 根
        expected[start:start + 4] = -1
    assert sig.to_numpy().tolist() == expected.tolist()


def test_planted_edge_strategy_passes_g0():
    closes = 100.0 * np.cumprod(1.0 + ar1_day_returns(40, rho=0.0))
    bars = daily_bars(closes, bars_per_day=6, seed=13)
    planted = plant_edge(bars, period_days=1, interval_hours=24, hold_bars=4,
                         drift=0.8)
    strat = PlantedEdgeStrategy(period_days=1, interval_hours=24)
    result = vlook.run_g0(strat, planted, {"hold_bars": 4}, probes=12)
    assert result["passed"], result


def test_random_entries_is_causal_and_window_invariant():
    closes = 100.0 * np.cumprod(1.0 + ar1_day_returns(30, rho=0.0))
    bars = daily_bars(closes, bars_per_day=6, seed=17)
    strat = RandomEntries(seed=99, hold_bars=4, event_every_bars=6)
    sig = strat.generate_signals(bars, {})
    assert set(pd.unique(sig)) <= {-1, 0, 1}
    result = vlook.run_g0(strat, bars, {}, probes=10)
    assert result["passed"], result


def test_random_entries_deterministic_by_seed():
    closes = 100.0 * np.cumprod(1.0 + ar1_day_returns(20, rho=0.0))
    bars = daily_bars(closes, bars_per_day=6, seed=23)
    a = RandomEntries(seed=5, hold_bars=4, event_every_bars=5)
    b = RandomEntries(seed=5, hold_bars=4, event_every_bars=5)
    pd.testing.assert_series_equal(a.generate_signals(bars, {}),
                                   b.generate_signals(bars, {}),
                                   check_exact=True)
    c = RandomEntries(seed=6, hold_bars=4, event_every_bars=5)
    assert not a.generate_signals(bars, {}).equals(
        c.generate_signals(bars, {}))


# ---------------------------------------------------------------- 块置换

def test_block_permute_seed_reproducible():
    closes = 100.0 * np.cumprod(1.0 + ar1_day_returns(25))
    bars = daily_bars(closes, seed=31)
    a = block_permute_bars(bars, seed=42)
    b = block_permute_bars(bars, seed=42)
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    c = block_permute_bars(bars, seed=43)
    assert not a["close"].equals(c["close"])


def test_block_permute_keeps_timestamps_and_length():
    closes = 100.0 * np.cumprod(1.0 + ar1_day_returns(15))
    bars = daily_bars(closes, seed=3)
    out = block_permute_bars(bars, seed=1)
    assert len(out) == len(bars)
    assert out["time"].tolist() == bars["time"].tolist()
    assert (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-12).all()
    assert (out["low"] <= out[["open", "close"]].min(axis=1) + 1e-12).all()


def _daily_returns(bars):
    closes = pd.Series(bars["close"].to_numpy(),
                       index=pd.DatetimeIndex(bars["time"]).normalize())
    last = closes.groupby(level=0).last()
    return last.pct_change().dropna().to_numpy()


def test_block_permute_preserves_daily_return_multiset():
    closes = 100.0 * np.cumprod(1.0 + ar1_day_returns(30))
    bars = daily_bars(closes, seed=8)
    out = block_permute_bars(bars, seed=4)
    orig = np.sort(_daily_returns(bars))
    shuf = np.sort(_daily_returns(out))
    np.testing.assert_allclose(orig, shuf, rtol=1e-10)


def test_block_permute_preserves_intraday_variance():
    """日内形状留在原日历槽：逐日日内收益方差（不含隔夜腿）逐位保留。"""
    closes = 100.0 * np.cumprod(1.0 + ar1_day_returns(30))
    bars = daily_bars(closes, bars_per_day=5, intraday_vol=0.006, seed=19)

    def per_day_var(df):
        day = pd.DatetimeIndex(df["time"]).normalize()
        rets = df["close"].astype("float64").pct_change()
        # 剔除每日首根的隔夜腿（它承载被置换的日收益，属于刻意改动）
        first = ~day.duplicated()
        rets = rets.where(~first)
        return rets.groupby(day).var().dropna().to_numpy()

    v_orig = per_day_var(bars)
    v_shuf = per_day_var(block_permute_bars(bars, seed=6))
    np.testing.assert_allclose(v_orig, v_shuf, rtol=1e-9)
    # 方差排序（窗口统计量）不变
    assert np.argsort(v_orig).tolist() == np.argsort(v_shuf).tolist()


def test_block_permute_destroys_cross_day_autocorrelation():
    """强 AR(1) 日收益（rho≈0.85）：打乱后一阶自相关塌向 0（可预测性破坏）。"""
    rets = ar1_day_returns(160, rho=0.85, seed=3)
    closes = 100.0 * np.cumprod(1.0 + rets)
    bars = daily_bars(closes, bars_per_day=4, seed=21)
    assert lag1_autocorr(_daily_returns(bars)) > 0.55
    out = block_permute_bars(bars, seed=11)
    assert abs(lag1_autocorr(_daily_returns(out))) < 0.30


def test_block_permute_rejects_bad_input():
    bars = daily_bars([100.0, 101.0], bars_per_day=2)
    with pytest.raises(ValueError):
        block_permute_bars(bars.iloc[:0], seed=1)
    # 单日数据（不足两块）也要如实拒绝
    with pytest.raises(ValueError):
        block_permute_bars(daily_bars([100.0], bars_per_day=4), seed=1)


def test_block_permute_handles_weekend_day_gaps():
    """真实数据有周末缺口（日历日不连续）：日槽位索引不得越界，
    且日收益多重集照样保留（2026-10-27 实跑回归：IndexError）。"""
    rng = np.random.default_rng(77)
    closes = [100.0]
    rows = []
    t = pd.Timestamp("2025-01-06")          # 周一
    day = 0
    price = 100.0
    while day < 30:
        if t.weekday() >= 5:                # 周末无 bar
            t += pd.Timedelta(days=1)
            continue
        ret = rng.normal(0.0, 0.004)
        o = price
        c = price * (1.0 + ret)
        for k in range(3):
            p = o + (c - o) * k / 2.0
            rows.append({"time": t + pd.Timedelta(hours=k), "open": p,
                         "high": p * 1.001, "low": p * 0.999, "close": p,
                         "tick_volume": 1, "spread": 1, "real_volume": 0})
        closes.append(c)
        price = c
        t += pd.Timedelta(days=1)
        day += 1
    bars = pd.DataFrame(rows)
    out = block_permute_bars(bars, seed=5)
    assert len(out) == len(bars)
    np.testing.assert_allclose(np.sort(_daily_returns(bars)),
                               np.sort(_daily_returns(out)), rtol=1e-10)


# ------------------------------------------------- 槽位置换（T39/M4 日内零假设）

def _slot_of_day(times) -> np.ndarray:
    """bar 时间（naive UTC）→ 纽约本地时刻的分钟数（槽位键）。"""
    idx = pd.DatetimeIndex(pd.Series(times))
    local = idx.tz_localize("UTC").tz_convert(NY_TZ)
    return np.asarray(local.hour * 60 + local.minute, dtype="int64")


def _slot_returns(bars) -> dict[int, np.ndarray]:
    """每个 NY 槽位的 bar 收益（close[i]/close[i-1]，i≥1），升序排序。"""
    close = bars["close"].to_numpy(dtype="float64")
    rets = close[1:] / close[:-1]
    slots = _slot_of_day(bars["time"])[1:]
    return {int(s): np.sort(rets[slots == s]) for s in np.unique(slots)}


def intraday_drift_bars(n_days=80, bars_per_day=4, *, drift=0.03, noise=0.002,
                        drift_slot=1, drift_every=2, seed=101,
                        start="2024-12-01", bar_seconds=3600):
    """日内第 k 根 bar（= 第 k 个 NY 槽位）只在隔日的日期上漂移 +d。

    每天 bars_per_day 根 bar、UTC 0..N 点 → 纽约本地时刻逐根不同且逐日
    对齐；起点 12 月、80 天 → 全程冬令时（EST 固定 UTC−5，不跨 3 月
    夏令时切换，槽位不漂移）。漂移日的第 drift_slot 根 bar 收益 =
    噪声+d，其余（含其他槽位、非漂移日）只有噪声。
    """
    rng = np.random.default_rng(seed)
    rows = []
    t0 = pd.Timestamp(start)
    price = 100.0
    for day in range(n_days):
        for k in range(bars_per_day):
            r = rng.normal(0.0, noise)
            if k == drift_slot and day % drift_every == 0:
                r += drift
            o = price
            c = price * (1.0 + r)
            rows.append({"time": t0 + pd.Timedelta(
                days=day, seconds=bar_seconds * k),
                "open": o, "high": max(o, c) * 1.001,
                "low": min(o, c) * 0.999, "close": c,
                "tick_volume": 1, "spread": 1, "real_volume": 0})
            price = c
    return pd.DataFrame(rows)


def test_slot_permute_seed_reproducible():
    bars = daily_bars(100.0 * np.cumprod(1.0 + ar1_day_returns(25)),
                      start="2025-01-06", seed=31)
    a = slot_permute_bars(bars, seed=42)
    b = slot_permute_bars(bars, seed=42)
    pd.testing.assert_frame_equal(a, b, check_exact=True)
    c = slot_permute_bars(bars, seed=43)
    assert not a["close"].equals(c["close"])


def test_slot_permute_keeps_timestamps_length_and_ohlc_relations():
    bars = daily_bars(100.0 * np.cumprod(1.0 + ar1_day_returns(15)),
                      start="2025-01-06", seed=3)
    out = slot_permute_bars(bars, seed=1)
    assert len(out) == len(bars)
    assert out["time"].tolist() == bars["time"].tolist()
    # 每根 bar 的 OHLC 关系仍成立（donor 形状满足的关系随重锚等比传递）
    assert (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-12).all()
    assert (out["low"] <= out[["open", "close"]].min(axis=1) + 1e-12).all()
    # C7 后 bar 内形状**不再**逐位保留（形状随槽位置换换到新日期）——
    # 至少有一根 bar 的 high/close 比值发生变化（置换非恒等）
    assert not np.allclose(
        out["high"].to_numpy() / out["close"].to_numpy(),
        bars["high"].to_numpy() / bars["close"].to_numpy(), rtol=1e-9)
    # bar 0 是锚点：不参与置换，四价逐位保留
    for col in ("open", "high", "low", "close"):
        assert out.loc[0, col] == bars.loc[0, col]


def test_slot_permute_preserves_slot_return_multiset():
    """T39 失败测试 1：每个 NY 槽位的收益多重集不变（时段波动分布保留）。"""
    bars = daily_bars(100.0 * np.cumprod(1.0 + ar1_day_returns(30)),
                      bars_per_day=4, start="2025-01-06", seed=8)
    out = slot_permute_bars(bars, seed=4)
    orig = _slot_returns(bars)
    shuf = _slot_returns(out)
    assert set(orig) == set(shuf) and len(orig) >= 4   # 每根日内 bar 一个槽位
    for slot in orig:
        np.testing.assert_allclose(orig[slot], shuf[slot], rtol=1e-10)


def test_slot_permute_preserves_multiset_across_dst_switch():
    """同一 UTC 时刻在夏令时切换两侧落到不同 NY 槽位：按本地时刻分组仍成立，
    多重集逐槽位保留（2025-03-09 美国切换夏令时）。"""
    t0 = pd.Timestamp("2025-03-05 13:00")      # 切换前一周，跨 03-09
    rng = np.random.default_rng(9)
    rows = []
    price = 100.0
    for i in range(24 * 8):
        r = rng.normal(0.0, 0.003)
        o, c = price, price * (1.0 + r)
        rows.append({"time": t0 + pd.Timedelta(hours=i), "open": o,
                     "high": max(o, c) * 1.001, "low": min(o, c) * 0.999,
                     "close": c, "tick_volume": 1, "spread": 1,
                     "real_volume": 0})
        price = c
    bars = pd.DataFrame(rows)
    out = slot_permute_bars(bars, seed=6)
    orig, shuf = _slot_returns(bars), _slot_returns(out)
    assert set(orig) == set(shuf)
    for slot in orig:
        np.testing.assert_allclose(orig[slot], shuf[slot], rtol=1e-10)


def test_slot_permute_destroys_planted_intraday_drift():
    """T39 失败测试 3：植入"日内第 k 根 bar 漂移"（只在隔日漂移）后置换，
    漂移日第 k 槽位的条件均值收益塌回槽位无条件均值（≈d/2）——
    日期与槽位收益的对齐被打乱，日内条件优势不可再捕获。"""
    d = 0.03
    bars = intraday_drift_bars(drift=d, drift_slot=1, drift_every=2)
    slots = _slot_of_day(bars["time"])
    k_slot = int(np.unique(slots)[1])          # 第 2 个槽位 = 日内第 2 根 bar
    n_days = len(bars) // 4

    def cond_mean(df):
        close = df["close"].to_numpy(dtype="float64")
        rets = close[1:] / close[:-1] - 1.0
        sl = _slot_of_day(df["time"])
        # bar (day, k) 的槽位；漂移日 = UTC 日序号偶数（每日 4 根，i//4）
        idx = np.arange(1, len(df))
        sel = (sl[1:] == k_slot) & ((idx // 4) % 2 == 0)
        return float(np.mean(rets[sel])), int(sel.sum())

    before, n_sel = cond_mean(bars)
    assert n_sel == n_days // 2
    assert before > 0.9 * d                    # 植入生效：条件均值 ≈ d
    after, n_sel2 = cond_mean(slot_permute_bars(bars, seed=13))
    assert n_sel2 == n_sel
    # 置换后：漂移日的第 k 槽位收益是槽位多重集的随机抽取（≈一半含漂移）
    # → 条件均值塌向 d/2；对齐信息消失（阈值留出 3σ 余量）
    assert after < 0.62 * d
    # 无条件（时段）分布原样保留：第 k 槽位收益总均值不变
    total_before = float(np.mean(
        (bars["close"].to_numpy(dtype="float64")[1:]
         / bars["close"].to_numpy(dtype="float64")[:-1] - 1.0)[
            _slot_of_day(bars["time"])[1:] == k_slot]))
    out = slot_permute_bars(bars, seed=13)
    total_after = float(np.mean(
        (out["close"].to_numpy(dtype="float64")[1:]
         / out["close"].to_numpy(dtype="float64")[:-1] - 1.0)[
            _slot_of_day(out["time"])[1:] == k_slot]))
    assert abs(total_after - total_before) < 1e-12


def test_slot_permute_preserves_slot_shape_multiset():
    """C7 新不变量：每个 NY 槽位的相对形状多重集（o/c、h/c、l/c）原样留在
    原槽位（各时段的 bar 形状分布保留），只是与日期解绑。"""
    bars = daily_bars(100.0 * np.cumprod(1.0 + ar1_day_returns(30)),
                      bars_per_day=4, start="2025-01-06", seed=8)
    out = slot_permute_bars(bars, seed=4)
    slots = _slot_of_day(bars["time"])
    for num, den in (("open", "close"), ("high", "close"), ("low", "close")):
        a = bars[num].to_numpy(dtype="float64") / bars[den].to_numpy(
            dtype="float64")
        b = out[num].to_numpy(dtype="float64") / out[den].to_numpy(
            dtype="float64")
        for s in np.unique(slots):
            np.testing.assert_allclose(np.sort(a[slots == s]),
                                       np.sort(b[slots == s]), rtol=1e-10)


def test_slot_permute_destroys_planted_open_close_drift():
    """C7 失败测试（第六轮审核反例）：开盘入场 → 收盘出场的**单 bar** 漂移
    置换后必须塌向槽位无条件均值（≈d/2）。

    旧实现只置换 close/close 收益、bar 内比值逐位保留 → 恒有
    new_close/new_open == old_close/old_open，本样例（open=前收）的条件
    开→收漂移 ≈ 0.0304 原样存活（审核复现值 0.0303554846）。新语义联合
    置换 (r, o/c, h/c, l/c)：开→收收益 c/o = c_d/o_d 改从槽位多重集随机
    抽取，日期对齐信息消失。"""
    d = 0.03
    bars = intraday_drift_bars(drift=d, drift_slot=1, drift_every=2)
    slots = _slot_of_day(bars["time"])
    k_slot = int(np.unique(slots)[1])          # 第 2 个槽位 = 日内第 2 根 bar
    n_days = len(bars) // 4

    def cond_open_close(df):
        o = df["open"].to_numpy(dtype="float64")
        c = df["close"].to_numpy(dtype="float64")
        oc = c / o - 1.0
        sl = _slot_of_day(df["time"])
        idx = np.arange(1, len(df))
        sel = (sl[1:] == k_slot) & ((idx // 4) % 2 == 0)
        return float(np.mean(oc[1:][sel])), int(sel.sum())

    before, n_sel = cond_open_close(bars)
    assert n_sel == n_days // 2
    assert before > 0.9 * d                    # 植入生效：条件开→收漂移 ≈ d
    after, n_sel2 = cond_open_close(slot_permute_bars(bars, seed=13))
    assert n_sel2 == n_sel
    # 置换后：漂移日第 k 槽位的开→收收益是槽位多重集的随机抽取（≈一半含
    # 漂移）→ 条件均值塌向 d/2（阈值留 3σ 余量；旧实现此处仍是 ≈d）
    assert after < 0.62 * d
    # 无条件（时段）分布原样保留：第 k 槽位开→收总均值不变（多重集未动）
    total_before = float(np.mean(
        (bars["close"].to_numpy(dtype="float64")
         / bars["open"].to_numpy(dtype="float64") - 1.0)[
            _slot_of_day(bars["time"]) == k_slot]))
    out = slot_permute_bars(bars, seed=13)
    total_after = float(np.mean(
        (out["close"].to_numpy(dtype="float64")
         / out["open"].to_numpy(dtype="float64") - 1.0)[
            _slot_of_day(out["time"]) == k_slot]))
    assert abs(total_after - total_before) < 1e-9


def test_slot_permute_rejects_bad_input():
    bars = daily_bars([100.0, 101.0], bars_per_day=2, start="2025-01-06")
    with pytest.raises(ValueError):
        slot_permute_bars(bars.iloc[:0], seed=1)
    # 单个纽约本地日（不足两个日期可置换）如实拒绝
    with pytest.raises(ValueError):
        slot_permute_bars(daily_bars([100.0], bars_per_day=4,
                                     start="2025-01-06"), seed=1)
    # 非正价格（乘法重锚的前提）
    bad = daily_bars([100.0, 101.0, 102.0], bars_per_day=2,
                     start="2025-01-06")
    bad.loc[2, "close"] = -1.0
    with pytest.raises(ValueError):
        slot_permute_bars(bad, seed=1)
