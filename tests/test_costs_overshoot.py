"""StopOvershootModel（T36/H2，第六轮 I3 后双口径）手算测试。

口径（见 mt5quant.costs.overshoot 模块 docstring）：
- **stop_level（v2 缺省）**：预定价位回放——bar = tick 时间戳聚合的
  1 分钟桶；多头止损代理 = 前一根（时间紧邻）分钟 bar 的最低 Bid，
  空头 = 前一根分钟 bar 的最高 Ask；当根 tick 里首笔 Bid<=stop / Ask>=stop
  即触发，越幅 = |触发侧价 − stop| / stop，恰触价位的零越幅**计入**
  （零是真实结果）；分钟桶不连续（周末/停盘缺口）不回放；
- **adjacent_jump（secondary，旧口径仅对比用）**：假想价位 = 前一笔
  tick 的触发侧价格，越幅 = |本 tick − 前一笔|——审核 I3 指出它与真实
  止损（预先固定价位）条件分布不同（反例见
  test_stop_level_review_counterexample）；
- bid 下穿与 ask 上穿两类事件合并统计；键 = 品种 × 纽约本地小时
  （2026-06 EDT：UTC 小时 h → 纽约 h−4）；
- 每小时每口径蓄水池上限 max_samples_per_hour：未超上限 → 精确统计
  （手算可对）；超限 → 确定性蓄水池抽样（两口径独立随机流），流式
  （分块喂入）与一次性结果逐位一致；
- 持久化：version 2 带 basis 键；version 1 旧表按 adjacent_jump 载入
  （缺省口径查询指路重建）。
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
    """旧口径（adjacent_jump）手算：恒定 spread 0.1；bid 序列
    100 → 99.8 → 100.1 → 99.6 → 99.5（UTC 14 → 纽约 10，同一分钟内）。

    事件（首行只作种子）：
    - t2 bid 99.8 < 100.0：下穿，价位 100.0，rel = 0.2/100
    - t3 ask 100.2 > 99.9：上穿，价位 99.9，rel = 0.3/99.9
    - t4 bid 99.6 < 100.1：下穿，价位 100.1，rel = 0.5/100.1
    - t5 bid 99.5 < 99.6：下穿，价位 99.6，rel = 0.1/99.6
    （t3 的 ask 上穿与 bid 回升不同侧不触发；t4 的 ask 99.7 < 100.2 不触发）
    （五笔 tick 同一分钟 → stop_level 无前根 bar 可回放，该口径无事件）
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
    st = m.stat(10, basis="adjacent_jump")
    assert isinstance(st, OvershootStat)
    assert st.n == 4 and st.n_days == 1
    assert st.p50 == pytest.approx((rels[1] + rels[2]) / 2)
    assert st.p90 == pytest.approx(rels[2] + 0.7 * (rels[3] - rels[2]))
    assert m.missing_hours("XAUUSD", basis="adjacent_jump") == tuple(
        h for h in range(24) if h != 10)
    # 价格单位查询：rel 分位 × price（p50/p90 两档）
    assert m.overshoot("XAUUSD", 10, price=2000.0,
                       basis="adjacent_jump") == pytest.approx(st.p50 * 2000.0)
    assert m.overshoot("XAUUSD", 10, price=2000.0, level="p90",
                       basis="adjacent_jump") == pytest.approx(st.p90 * 2000.0)
    # 同一分钟的数据 stop_level 无事件（无前根 bar）：缺省口径如实无统计
    with pytest.raises(ValueError, match="stop_level"):
        m.stat(10)


def test_stop_level_review_counterexample():
    """I3 失败测试（第六轮审核 Important 3 反例）：预定价位回放 vs 相邻跳幅。

    Bid 100 → 99.9 → 99.9，止损价预定在前根分钟 bar 的 low = 99.9：
    - stop_level：当根首笔 Bid=99.9 恰触价位 → 首次触发越幅 = 0（真实
      结果，计入分位；审核指出旧口径排除了恰触事件）；
    - adjacent_jump：把前一笔报价 100.0 当假想止损价，记 0.1/100——
      与真实止损（预先固定价位）条件分布不同，仅对比用。
    """
    df = make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),   # 分钟 bar A 开始
        ("2026-06-01 14:00:01.000", 99.9, 100.0),    # bar A：minBid=99.9
        ("2026-06-01 14:01:00.000", 99.9, 100.0),    # bar B：Bid=99.9 恰触
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    st = m.stat(10)                                  # 缺省 basis=stop_level
    assert st.n == 1 and st.n_days == 1
    assert st.p50 == pytest.approx(0.0)              # 零越幅计入分位
    assert st.p90 == pytest.approx(0.0)
    assert m.overshoot("XAUUSD", 10, price=2000.0) == pytest.approx(0.0)
    old = m.stat(10, basis="adjacent_jump")
    assert old.n == 1                                # 两口径共存、互不污染
    assert old.p50 == pytest.approx(0.1 / 100.0)     # 旧口径 ≈0.1（失真）


def test_stop_level_long_and_short_replay_hand_computed():
    """前根分钟 bar 极值回放手算：多头用前根 min(Bid)、空头用前根 max(Ask)，
    每根 bar 每侧只记首笔触发，越幅 = |触发侧价 − stop| / stop。

    - bar A（14:00）：minBid 99.8 / maxAsk 100.1
    - bar B（14:01）首笔 (99.5, 99.6)：多头触发 |99.5−99.8|/99.8；
      第二笔 (99.7, 99.8) 同 bar 不再触发（首笔语义）
    - bar C（14:02）首笔 (99.9, 100.0)：bar B 的 maxAsk=99.8 → 空头触发
      |100.0−99.8|/99.8
    """
    df = make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),
        ("2026-06-01 14:00:30.000", 99.8, 99.9),
        ("2026-06-01 14:01:00.000", 99.5, 99.6),
        ("2026-06-01 14:01:30.000", 99.7, 99.8),
        ("2026-06-01 14:02:00.000", 99.9, 100.0),
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    st = m.stat(10)
    rels = sorted([0.3 / 99.8, 0.2 / 99.8])
    assert st.n == 2 and st.n_days == 1
    assert st.p50 == pytest.approx(rels[0] + 0.5 * (rels[1] - rels[0]))
    assert st.p90 == pytest.approx(rels[0] + 0.9 * (rels[1] - rels[0]))
    # 同一数据的旧口径（adjacent_jump）4 个事件、数值不同 → 共存不污染
    old = m.stat(10, basis="adjacent_jump")
    old_rels = sorted([0.2 / 100.0, 0.3 / 99.8, 0.2 / 99.6, 0.2 / 99.8])
    assert old.n == 4
    assert old.p50 == pytest.approx((old_rels[1] + old_rels[2]) / 2)


def test_stop_level_requires_contiguous_previous_bar():
    """分钟桶不连续（周末/停盘缺口）不回放：止损代理要求前一根 bar 时间紧邻
    ——缺口后首根 bar 的极端穿越不构成"预定价位首次触发"事件。"""
    df = make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),
        ("2026-06-01 14:00:30.000", 99.0, 99.1),    # bar A：minBid 99.0
        ("2026-06-01 14:05:00.000", 98.0, 98.1),    # 缺口 4 分钟 → 不回放
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    with pytest.raises(ValueError, match="stop_level"):
        m.stat(10)                                   # 无 stop_level 事件
    assert m.stat(10, basis="adjacent_jump").n == 2  # 旧口径照常


def test_overshoot_requires_valid_price_and_level():
    df = make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),
        ("2026-06-01 14:00:01.000", 99.8, 99.9),
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    with pytest.raises(ValueError, match="price"):
        m.overshoot("XAUUSD", 10, basis="adjacent_jump")
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            m.overshoot("XAUUSD", 10, price=bad, basis="adjacent_jump")
    with pytest.raises(ValueError, match="level"):
        m.overshoot("XAUUSD", 10, price=100.0, level="p99",
                    basis="adjacent_jump")
    with pytest.raises(ValueError, match="basis"):
        m.overshoot("XAUUSD", 10, price=100.0, basis="tick_jump")


def test_overshoot_missing_hour_falls_back_to_daily_max():
    """缺统计小时 → 全天（有统计小时）最大 p50/p90（保守，语义同 forbid
    出场侧）；两口径各自回退到**自己**口径的全天最大。"""
    df = make_ticks([
        ("2026-06-01 14:58:00.000", 100.0, 100.1),   # NY 10：分钟 bar m1
        ("2026-06-01 14:59:00.000", 99.8, 99.9),     # NY 10：bar m1：minBid 99.8
        # NY 11：14:59 → 15:00 分钟紧邻 → 回放：|99.4−99.8|/99.8 为 stop_level
        # 全天最大；adjacent_jump 在 NY 10/11 各一个下穿事件
        ("2026-06-01 15:00:00.000", 99.4, 99.5),
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    # adjacent_jump：NY 10 记 0.2/100、NY 11 记 0.4/99.8
    assert m.stat(10, basis="adjacent_jump").n == 1
    assert m.stat(10, basis="adjacent_jump").p50 == pytest.approx(0.002)
    assert m.stat(11, basis="adjacent_jump").n == 1
    assert m.stat(11, basis="adjacent_jump").p50 == pytest.approx(0.4 / 99.8)
    # stop_level：14:59 bar 的前根是 14:58（紧邻）→ NY 10 也触发（价位
    # 100.0）；15:00 bar 用 14:59 的 minBid=99.8 → NY 11 触发 0.4/99.8
    assert m.stat(10).n == 1
    assert m.stat(10).p50 == pytest.approx(0.002)
    assert m.stat(11).n == 1
    assert m.stat(11).p50 == pytest.approx(0.4 / 99.8)
    # 缺统计小时 → 各口径自己全天最大 p50；p90 档同理
    assert m.overshoot("XAUUSD", 9, price=1000.0,
                       basis="adjacent_jump") == pytest.approx(
        m.stat(11, basis="adjacent_jump").p50 * 1000.0)
    assert m.overshoot("XAUUSD", 9, price=1000.0) == pytest.approx(
        m.stat(11).p50 * 1000.0)
    assert m.overshoot("XAUUSD", 9, price=1000.0, level="p90",
                       basis="adjacent_jump") == pytest.approx(
        m.stat(11, basis="adjacent_jump").p90 * 1000.0)
    with pytest.raises(ValueError):
        m.stat(9)                                    # 原始查询照抛
    with pytest.raises(ValueError):
        m.stat(9, basis="adjacent_jump")


def _walk_ticks(rng, n, hour_utc=14):
    """确定性随机游走 tick：足够多事件以越过蓄水池上限（70ms × n → 跨多根
    分钟 bar，stop_level 与 adjacent_jump 两口径都有充足事件）。"""
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
    """失败测试清单第 4 条（越幅侧）：小上限蓄水池下，分块喂入 == 一次性喂入
    （adjacent_jump 口径——900 步游走几乎每步都穿价；stop_level 的同等
    逐位检查见 test_two_bases_streaming_equals_one_shot）。"""
    rng = np.random.default_rng(7)
    df = _walk_ticks(rng, 900)          # ~900 个事件（几乎每步都穿价）> 上限 60
    one = StopOvershootModel.from_ticks("XAUUSD", df, max_samples_per_hour=60)
    b = StopOvershootBuilder("XAUUSD", max_samples_per_hour=60)
    for lo, hi in ((0, 137), (137, 138), (138, 512), (512, 900)):   # 病态切块
        b.add_ticks(df.iloc[lo:hi])
    streamed = b.build()
    st1 = one.stat(10, basis="adjacent_jump")
    st2 = streamed.stat(10, basis="adjacent_jump")
    assert st2.n == st1.n                    # 事件总数不受抽样影响
    assert st2.n_days == st1.n_days
    assert st2.p50 == pytest.approx(st1.p50, rel=1e-12)
    assert st2.p90 == pytest.approx(st1.p90, rel=1e-12)


def test_two_bases_streaming_equals_one_shot():
    """两口径独立随机流：分块边界穿过分钟 bar 与纽约小时边界时，stop_level
    与 adjacent_jump 的蓄水池抽样仍与一次性喂入逐位一致（builder 同时算
    两口径、互不挤占对方的随机流）。"""
    rng = np.random.default_rng(7)
    df = _walk_ticks(rng, 52_000)       # ≈61 分钟 → 跨纽约小时边界（10→11）
    one = StopOvershootModel.from_ticks("XAUUSD", df, max_samples_per_hour=5)
    b = StopOvershootBuilder("XAUUSD", max_samples_per_hour=5)
    for lo, hi in ((0, 137), (137, 138), (138, 512), (512, 9_000),
                   (9_000, 30_000), (30_000, 51_999), (51_999, 52_000)):
        b.add_ticks(df.iloc[lo:hi])     # 病态切块 + 跨分钟/跨小时边界
    streamed = b.build()
    for basis in ("stop_level", "adjacent_jump"):
        assert one.missing_hours("XAUUSD", basis=basis) == streamed.missing_hours(
            "XAUUSD", basis=basis), f"覆盖小时不一致 basis={basis}"
        for h in range(24):
            if h in one.missing_hours("XAUUSD", basis=basis):
                continue
            assert one.stat(h, basis=basis) == streamed.stat(
                h, basis=basis), f"basis={basis} hour={h}"


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
    # stop_level 回放的前根止损代理价非正 → 同样拒绝（相邻层不先报错：
    # t2 下穿价位 1.0、t3 上穿价位 0.1 均为正，只有回放的 stop=0 非法）
    zero_stop = make_ticks([("2026-06-01 14:00:00.000", 1.0, 1.1),
                            ("2026-06-01 14:00:30.000", 0.0, 0.1),
                            ("2026-06-01 14:01:00.000", 0.0, 0.3)])
    with pytest.raises(ValueError, match="价位"):
        StopOvershootBuilder("XAUUSD").add_ticks(zero_stop)


def test_overshoot_dict_marks_basis_and_legacy_v1_loads_as_adjacent():
    """产物 dict 带 basis 键（version 2）；version 1 旧表（仓库现有缓存格式）
    按 adjacent_jump 载入并标记——缺省口径查询指路重建，不冒充 stop_level。"""
    df = make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),
        ("2026-06-01 14:00:01.000", 99.9, 100.0),
        ("2026-06-01 14:01:00.000", 99.9, 100.0),
    ])
    m = StopOvershootModel.from_ticks("XAUUSD", df)
    d = m.to_dict()
    assert d["version"] == 2
    assert d["basis"] == "stop_level"
    assert d["secondary"]["basis"] == "adjacent_jump"
    m2 = StopOvershootModel.from_dict(d)
    assert m2.stat(10) == m.stat(10)
    assert m2.stat(10, basis="adjacent_jump") == m.stat(10, basis="adjacent_jump")
    # version 1 旧表：只有一张 per_symbol（旧口径数值）→ 载入为 adjacent_jump
    legacy = {
        "version": 1,
        "bucket_tz": d["bucket_tz"],
        "per_symbol": {"XAUUSD": d["secondary"]["per_symbol"]["XAUUSD"]},
    }
    lm = StopOvershootModel.from_dict(legacy)
    assert lm.stat(10, basis="adjacent_jump").p50 == pytest.approx(0.1 / 100.0)
    with pytest.raises(ValueError, match="重建"):
        lm.stat(10)                          # 缺省 stop_level：旧表没有 → 指路重建
    assert lm.to_dict()["basis"] == "adjacent_jump"    # 标记随再序列化保留
    # version 99 拒绝
    bad = dict(d)
    bad["version"] = 99
    with pytest.raises(ValueError, match="version"):
        StopOvershootModel.from_dict(bad)


def test_merge_multi_symbol_and_persistence(tmp_path):
    a = StopOvershootModel.from_ticks("XAUUSD", make_ticks([
        ("2026-06-01 14:00:00.000", 100.0, 100.1),
        ("2026-06-01 14:01:00.000", 99.8, 99.9)]))
    b = StopOvershootModel.from_ticks("USTEC", make_ticks([
        ("2026-06-01 15:00:00.000", 20000.0, 20000.1),
        ("2026-06-01 15:01:00.000", 19999.0, 19999.1)]))
    m = a.merge(b)
    assert m.symbols == ("USTEC", "XAUUSD")
    # 缺省口径（stop_level）：XAUUSD 用前根 bar low=100.0 回放 → 0.2/100
    assert m.overshoot("XAUUSD", 10, price=100.0) == pytest.approx(0.2 / 100.0
                                                                  * 100.0)
    # 旧口径（adjacent_jump）：USTEC 记 1.0/20000 的相邻跳幅
    assert m.overshoot("USTEC", 11, price=20000.0,
                       basis="adjacent_jump") == pytest.approx(
        1.0 / 20000.0 * 20000.0)
    with pytest.raises(ValueError):
        a.merge(a)
    p = str(tmp_path / "overshoot.json")
    m.save(p)
    m2 = StopOvershootModel.load(p)
    assert m2.stat(10, symbol="XAUUSD") == m.stat(10, symbol="XAUUSD")
    assert m2.stat(10, symbol="XAUUSD",
                   basis="adjacent_jump") == m.stat(10, symbol="XAUUSD",
                                                    basis="adjacent_jump")
    assert m2.stat(11, symbol="USTEC") == m.stat(11, symbol="USTEC")
    # 坏版本号拒绝加载
    d = m.to_dict()
    d["version"] = 99
    bad = str(tmp_path / "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        json.dump(d, f)
    with pytest.raises(ValueError, match="version"):
        StopOvershootModel.load(bad)
