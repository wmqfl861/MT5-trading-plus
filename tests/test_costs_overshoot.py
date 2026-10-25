"""StopOvershootModel（T36/H2）手算测试：tick 上"首笔穿价"越幅的相对分位。

口径（见 mt5quant.costs.overshoot 模块 docstring）：
- 假想价位 = 前一笔 tick 的触发侧价格（多头 Bid / 空头 Ask），本 tick 即
  首笔穿价 tick，越幅 = |本 tick − 前一笔|，相对价 = 越幅/价位；
- bid 下穿与 ask 上穿合并统计；键 = 品种 × 纽约本地小时（2026-06 EDT：
  UTC 小时 h → 纽约 h−4）；
- 每小时蓄水池上限 max_samples_per_hour：未超上限 → 精确统计（手算可对）；
  超限 → 确定性蓄水池抽样，流式（分块喂入）与一次性结果逐位一致。
"""
import json

import numpy as np
import pandas as pd
import pytest

from mt5quant.costs.overshoot import (
    OvershootStat, StopOvershootBuilder, StopOvershootModel,
)


def make_ticks(rows):
    """rows: [(naive-UTC 时间, bid, ask)]（恒定 spread 0.1 的用例直接写两列）。"""
    return pd.DataFrame({
        "time_msc": np.asarray(
            [pd.Timestamp(t).value // 10**6 for t, _, _ in rows], dtype="int64"),
        "bid": np.asarray([b for _, b, _ in rows], dtype="float64"),
        "ask": np.asarray([a for _, _, a in rows], dtype="float64"),
    })


def test_overshoot_hand_computed():
    """恒定 spread 0.1；bid 序列 100 → 99.8 → 100.1 → 99.6 → 99.5（UTC 14 → 纽约 10）。

    事件（首行只作种子）：
    - t2 bid 99.8 < 100.0：下穿，价位 100.0，rel = 0.2/100
    - t3 ask 100.2 > 99.9：上穿，价位 99.9，rel = 0.3/99.9
    - t4 bid 99.6 < 100.1：下穿，价位 100.1，rel = 0.5/100.1
    - t5 bid 99.5 < 99.6：下穿，价位 99.6，rel = 0.1/99.6
    （t3 的 ask 上穿与 bid 回升不同侧不触发；t4 的 ask 99.7 < 100.2 不触发）
    """
    df = make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),
        ("2026-06-01 14:00:01.000", 99.8, 99.9),
        ("2026-06-01 14:00:02.000", 100.1, 100.2),
        ("2026-06-01 14:00:03.000", 99.6, 99.7),
        ("2026-06-01 14:00:04.000", 99.5, 99.6),
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    rels = sorted([0.2 / 100.0, 0.3 / 99.9, 0.5 / 100.1, 0.1 / 99.6])
    st = m.stat(10)
    assert isinstance(st, OvershootStat)
    assert st.n == 4 and st.n_days == 1
    assert st.p50 == pytest.approx((rels[1] + rels[2]) / 2)
    assert st.p90 == pytest.approx(rels[2] + 0.7 * (rels[3] - rels[2]))
    assert m.missing_hours("XAUUSD") == tuple(h for h in range(24) if h != 10)
    # 价格单位查询：rel 分位 × price（p50/p90 两档）
    assert m.overshoot("XAUUSD", 10, price=2000.0) == pytest.approx(st.p50 * 2000.0)
    assert m.overshoot("XAUUSD", 10, price=2000.0, level="p90") == pytest.approx(
        st.p90 * 2000.0)


def test_overshoot_requires_valid_price_and_level():
    df = make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),
        ("2026-06-01 14:00:01.000", 99.8, 99.9),
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    with pytest.raises(ValueError, match="price"):
        m.overshoot("XAUUSD", 10)
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            m.overshoot("XAUUSD", 10, price=bad)
    with pytest.raises(ValueError, match="level"):
        m.overshoot("XAUUSD", 10, price=100.0, level="p99")


def test_overshoot_missing_hour_falls_back_to_daily_max():
    """缺统计小时 → 全天（有统计小时）最大 p50/p90（保守，语义同 forbid 出场侧）。"""
    df = make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),   # 纽约 10：下穿 rel 0.002
        ("2026-06-01 14:00:01.000", 99.8, 99.9),
        # 中转 tick：两侧都不穿价（bid 升、ask 持平）→ 无事件
        ("2026-06-01 15:00:00.000", 99.85, 99.9),
        # 纽约 11：下穿 0.4（价位 99.85）→ rel 0.4/99.85，为全天最大
        ("2026-06-01 15:00:01.000", 99.45, 99.5),
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    assert m.stat(10).n == 1 and m.stat(10).p50 == pytest.approx(0.002)
    assert m.stat(11).n == 1 and m.stat(11).p50 == pytest.approx(0.4 / 99.85)
    # 缺统计小时（纽约 9）→ 全天最大 p50；p90 档同理取最大 p90
    assert m.overshoot("XAUUSD", 9, price=1000.0) == pytest.approx(
        m.stat(11).p50 * 1000.0)
    assert m.overshoot("XAUUSD", 9, price=1000.0, level="p90") == pytest.approx(
        m.stat(11).p90 * 1000.0)
    with pytest.raises(ValueError):
        m.stat(9)                                    # 原始查询照抛


def _walk_ticks(rng, n, hour_utc=14):
    """确定性随机游走 tick：足够多事件以越过蓄水池上限。"""
    times = pd.date_range(f"2026-06-01 {hour_utc:02d}:00:00",
                          periods=n, freq="70ms").as_unit("ns")
    steps = rng.normal(0.0, 0.05, n).cumsum()
    bid = 100.0 + steps
    return pd.DataFrame({
        "time_msc": (times.asi8 // 10**6).astype("int64"),
        "bid": bid,
        "ask": bid + 0.1,
    })


def test_reservoir_streaming_equals_one_shot():
    """失败测试清单第 4 条（越幅侧）：小上限蓄水池下，分块喂入 == 一次性喂入。"""
    rng = np.random.default_rng(7)
    df = _walk_ticks(rng, 900)          # ~900 个事件（几乎每步都穿价）> 上限 60
    one = StopOvershootModel.from_ticks("XAUUSD", df, max_samples_per_hour=60)
    b = StopOvershootBuilder("XAUUSD", max_samples_per_hour=60)
    for lo, hi in ((0, 137), (137, 138), (138, 512), (512, 900)):   # 病态切块
        b.add_ticks(df.iloc[lo:hi])
    streamed = b.build()
    assert streamed.stat(10).n == one.stat(10).n          # 事件总数不受抽样影响
    assert streamed.stat(10).n_days == one.stat(10).n_days
    assert streamed.stat(10).p50 == pytest.approx(one.stat(10).p50, rel=1e-12)
    assert streamed.stat(10).p90 == pytest.approx(one.stat(10).p90, rel=1e-12)


def test_reservoir_cap_limits_memory_but_keeps_quantiles_sane():
    rng = np.random.default_rng(11)
    df = _walk_ticks(rng, 4000)
    full = StopOvershootModel.from_ticks("XAUUSD", df, max_samples_per_hour=10**9)
    capped = StopOvershootModel.from_ticks("XAUUSD", df, max_samples_per_hour=300)
    st_full, st_capped = full.stat(10), capped.stat(10)
    assert st_capped.n == st_full.n                       # n = 全部事件数
    # 抽样分位与全量接近（n=300 的蓄水库分位标准误约 6%，容差 15% ≈ 2.5 SE）
    assert st_capped.p50 == pytest.approx(st_full.p50, rel=0.15)
    assert st_capped.p90 == pytest.approx(st_full.p90, rel=0.15)


def test_builder_rejects_bad_input():
    df = make_ticks([("2026-06-01 14:00:00.000", 100.0, 100.1)])
    with pytest.raises(ValueError):
        StopOvershootBuilder("XAUUSD").add_ticks(df.drop(columns=["ask"]))
    with pytest.raises(ValueError):
        StopOvershootBuilder("XAUUSD", max_samples_per_hour=0)
    with pytest.raises(ValueError):
        StopOvershootBuilder("XAUUSD").build()            # 无事件 → 拒绝空模型
    zero = make_ticks([("2026-06-01 14:00:00.000", 0.0, 0.0),
                       ("2026-06-01 14:00:01.000", 0.0, 0.1)])
    with pytest.raises(ValueError, match="价位"):
        StopOvershootBuilder("XAUUSD").add_ticks(zero)    # 非正价位拒绝


def test_merge_multi_symbol_and_persistence(tmp_path):
    a = StopOvershootModel.from_ticks("XAUUSD", make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),
        ("2026-06-01 14:00:01.000", 99.8, 99.9)]))
    b = StopOvershootModel.from_ticks("USTEC", make_ticks([
        ("2026-06-01 15:00:00.000", 20000.0, 20000.1),
        ("2026-06-01 15:00:01.000", 19999.0, 19999.1)]))
    m = a.merge(b)
    assert m.symbols == ("USTEC", "XAUUSD")
    assert m.overshoot("USTEC", 11, price=20000.0) == pytest.approx(1.0 / 20000.0
                                                                  * 20000.0)
    with pytest.raises(ValueError):
        a.merge(a)
    p = str(tmp_path / "overshoot.json")
    m.save(p)
    m2 = StopOvershootModel.load(p)
    assert m2.stat(10, symbol="XAUUSD") == m.stat(10, symbol="XAUUSD")
    assert m2.stat(11, symbol="USTEC") == m.stat(11, symbol="USTEC")
    # 坏版本号拒绝加载
    d = m.to_dict()
    d["version"] = 99
    bad = str(tmp_path / "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        json.dump(d, f)
    with pytest.raises(ValueError, match="version"):
        StopOvershootModel.load(bad)
