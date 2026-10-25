"""SpreadModel 手算测试：奇偶样本分位、小时分桶、rollover、save/load、多品种；
相对口径（R1/T3）：rel=(ask−bid)/mid、spread_p90(price) 随价格缩放、旧文件兼容。

T20（H2）：分桶改**纽约本地小时**（DST 拆开 21/22 点 UTC 不再混桶）——
2026-06 为夏令时（EDT，UTC−4），UTC 小时 h → 纽约小时 h−4；
缺统计小时不再抛错（missing_hour_policy=forbid：查价取全天最大 p90、
missing_hours 供接线禁入）。

T36（H2）：采样口径 sampling——缺省 bar_open_m1（每个 M1 边界后的第一笔
tick，近似时间加权、内存只随分钟数增长），"tick" 保留全 tick 口径对比；
每纽约小时记 n 与 n_days（覆盖的不同纽约日期数），n_days < min_days_per_hour
视为缺统计（forbid 语义）。
"""
import json
import os

import numpy as np
import pandas as pd
import pytest

from mt5quant.costs.spread import BUCKET_TZ, SpreadModel, SpreadStat


def make_ticks(rows, base=1.0):
    """rows: [(naive-UTC 时间字符串, spread)]；bid=base、ask=base+spread。"""
    return pd.DataFrame(
        {
            "time_msc": np.asarray(
                [pd.Timestamp(t).value // 10**6 for t, _ in rows], dtype="int64"
            ),
            "bid": np.asarray([base for _ in rows], dtype="float64"),
            "ask": np.asarray([base + s for _, s in rows], dtype="float64"),
        }
    )


def test_bucket_tz_is_new_york():
    assert BUCKET_TZ == "America/New_York"


def test_stat_hand_computed_odd_and_even_samples():
    ticks = make_ticks(
        [
            (f"2026-06-01 10:{i:02d}:00", s)
            for i, s in zip((0, 10, 20, 30, 40), (0.0001, 0.0002, 0.0003, 0.0004, 0.0005))
        ]
        + [
            (f"2026-06-01 14:{i:02d}:00", s)
            for i, s in zip((0, 10, 20, 30), (0.0001, 0.0002, 0.0003, 0.0004))
        ]
    )
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    # 2026-06-01 为 EDT：UTC 10 点 → 纽约 6 点；UTC 14 点 → 纽约 10 点
    # 奇样本 n=5，线性分位：p90=4+0.6×(5−4)=4.6、p99=4+0.96×1=4.96
    st6 = m.stat(6)
    assert isinstance(st6, SpreadStat)
    assert st6.n == 5
    assert st6.median == pytest.approx(0.0003)
    assert st6.p90 == pytest.approx(0.00046)
    assert st6.p99 == pytest.approx(0.000496)
    # 偶样本 n=4：median=(2+3)/2、p90=3+0.7×1=3.7、p99=3+0.97×1=3.97
    st10 = m.stat(10)
    assert st10.n == 4
    assert st10.median == pytest.approx(0.00025)
    assert st10.p90 == pytest.approx(0.00037)
    assert st10.p99 == pytest.approx(0.000397)


def test_hour_bucket_is_new_york_local():
    # UTC 23:59 与次日 00:01（EDT）= 纽约 19:59 / 20:01 → 分属 19/20 两个桶；
    # 同一纽约小时（冬 22:30 / 夏 21:30 UTC 两个日期的 17 点纽约侧）必须同桶。
    ticks = make_ticks([("2026-06-01 23:59:00", 0.0002), ("2026-06-02 00:01:00", 0.0004)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    assert m.stat(19).n == 1 and m.stat(19).median == pytest.approx(0.0002)
    assert m.stat(20).n == 1 and m.stat(20).median == pytest.approx(0.0004)


def test_dst_split_utc_21_and_22_not_merged():
    # H2 核心：夏令时 21:00 UTC 与冬令时 22:00 UTC 都是纽约 17 点 → 同一桶
    # （换日尖峰不再被 DST 摊薄到两个 UTC 小时）；夏 22:00 UTC = 纽约 18 点另一桶
    ticks = make_ticks([                   # 时间升序（T36 采样口径的前提）
        ("2026-07-15 21:00:00", 0.0100),   # EDT：纽约 17 点
        ("2026-07-15 21:30:00", 0.0100),
        ("2026-07-15 22:00:00", 0.0002),   # EDT：纽约 18 点
        ("2026-12-15 22:00:00", 0.0200),   # EST：纽约 17 点（同一桶）
        ("2026-12-15 22:30:00", 0.0200),
    ])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    assert m.stat(17).n == 4
    assert m.stat(17).median == pytest.approx(0.015)   # 冬夏换日样本合并统计
    assert m.stat(18).n == 1 and m.stat(18).median == pytest.approx(0.0002)


def test_spread_p90_returns_p90_and_validates():
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.0001 * (i + 1)) for i in range(5)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    # 口径：返回该小时 p90（保守），不是 p90 的一半（UTC 10 → 纽约 6）
    assert m.spread_p90("EURUSD", 6) == pytest.approx(0.00046)
    with pytest.raises(ValueError):  # 品种不在模型
        m.spread_p90("XAUUSD", 6)


def test_missing_hour_falls_back_to_daily_max_p90_no_raise():
    # T20：缺统计小时不再抛错——出场成本取全天最大 p90（保守）
    ticks = make_ticks([
        ("2026-06-01 10:00:00", 0.0001), ("2026-06-01 10:10:00", 0.0001),
        ("2026-06-01 14:00:00", 0.0010), ("2026-06-01 14:10:00", 0.0010),
    ])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    assert m.missing_hours("EURUSD") == tuple(
        h for h in range(24) if h not in (6, 10))
    # 缺统计小时（纽约 7）→ 全天最大 p90 = 0.001
    assert m.spread_p90("EURUSD", 7) == pytest.approx(0.001)
    assert m.spread_p90("EURUSD", 7, price=123.0) == pytest.approx(0.001)  # absolute 忽略 price
    # 有统计的小时照常
    assert m.spread_p90("EURUSD", 6) == pytest.approx(0.0001)


def test_missing_hour_relative_scales_daily_max_with_price():
    ticks = make_ticks([
        ("2026-06-01 10:00:00", 0.2), ("2026-06-01 10:10:00", 0.2),
        ("2026-06-01 14:00:00", 1.0), ("2026-06-01 14:10:00", 1.0),
    ], base=100.0)
    m = SpreadModel.from_ticks("XAUUSD", ticks)   # relative
    big = 1.0 / 100.5                              # 纽约 10 点桶的 rel p90（更大）
    assert m.spread_p90("XAUUSD", 7, price=2000.0) == pytest.approx(big * 2000.0)
    assert m.spread_p90("XAUUSD", 6, price=2000.0) == pytest.approx(0.2 / 100.1 * 2000.0)


def test_missing_hours_multi_symbol():
    a = SpreadModel.from_ticks("EURUSD", make_ticks([("2026-06-01 10:00:00", 0.0002)]),
                               mode="absolute")
    b = SpreadModel.from_ticks("XAUUSD", make_ticks([("2026-06-01 11:00:00", 0.30)]),
                               mode="absolute")
    m = a.merge(b)
    assert m.missing_hours("EURUSD") == tuple(h for h in range(24) if h != 6)
    assert m.missing_hours("XAUUSD") == tuple(h for h in range(24) if h != 7)


def test_rollover_hours_flag_p99_spike():
    rows = [(f"2026-06-01 10:{i // 60:02d}:{i % 60:02d}", 0.0001) for i in range(100)]
    rows += [
        (f"2026-06-01 21:{i // 60:02d}:{i % 60:02d}", 0.02 if i == 19 else 0.0001)
        for i in range(20)
    ]
    # T36：本夹具同一分钟内有多笔 tick（分位依赖全部 tick）——钉死 tick 口径
    m = SpreadModel.from_ticks("EURUSD", make_ticks(rows), mode="absolute",
                               sampling="tick")
    # UTC 10 → 纽约 6；UTC 21 → 纽约 17（尖峰桶）
    # 17 点 p99 = 0.0001+0.81×(0.02−0.0001) = 0.016219 > 3×全体中位 0.0003
    assert m.stat(17).p99 == pytest.approx(0.016219)
    assert m.rollover_hours() == (17,)  # 常规小时（p99=0.0001）不误报
    assert m.overall_median() == pytest.approx(0.0001)


def test_save_load_roundtrip(tmp_path):
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.0001 * (i + 1)) for i in range(5)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    p = str(tmp_path / "spread.json")
    m.save(p)
    assert not os.path.exists(p + ".tmp")  # 原子写不留残片
    m2 = SpreadModel.load(p)
    assert m2.stat(6) == m.stat(6)
    assert m2.spread_p90("EURUSD", 6) == pytest.approx(0.00046)
    assert m2.rollover_hours() == m.rollover_hours()
    assert m2.missing_hours("EURUSD") == m.missing_hours("EURUSD")


def test_from_ticks_reads_only_required_columns():
    # 只含 time_msc/bid/ask 三列即可（不依赖 mt5data.ticks 的帧定义）
    df = make_ticks([("2026-06-01 10:00:00", 0.0002)])
    m = SpreadModel.from_ticks("EURUSD", df, mode="absolute")
    assert m.stat(6).n == 1


def test_from_ticks_rejects_missing_columns_and_empty():
    with pytest.raises(ValueError):
        SpreadModel.from_ticks("EURUSD", pd.DataFrame({"time_msc": [], "bid": []}),
                               mode="absolute")
    df = make_ticks([("2026-06-01 10:00:00", 0.0002)]).drop(columns=["ask"])
    with pytest.raises(ValueError):
        SpreadModel.from_ticks("EURUSD", df, mode="absolute")


def test_merge_and_multi_symbol_lookup():
    a = SpreadModel.from_ticks("EURUSD", make_ticks([("2026-06-01 10:00:00", 0.0002)]),
                               mode="absolute")
    b = SpreadModel.from_ticks("XAUUSD", make_ticks([("2026-06-01 10:00:00", 0.30)]),
                               mode="absolute")
    m = a.merge(b)
    assert m.symbols == ("EURUSD", "XAUUSD")
    assert m.spread_p90("EURUSD", 6) == pytest.approx(0.0002)
    assert m.spread_p90("XAUUSD", 6) == pytest.approx(0.30)
    with pytest.raises(ValueError):  # 多品种时无 symbol 的查询必须显式
        m.stat(6)
    with pytest.raises(ValueError):
        m.rollover_hours()
    with pytest.raises(ValueError):
        m.missing_hours()
    assert m.rollover_hours("EURUSD") == ()
    assert m.stat(6, symbol="EURUSD").p90 == pytest.approx(0.0002)
    with pytest.raises(ValueError):  # 品种重复不能合并
        a.merge(a)


# ------------------------------------------------- 相对口径（R1 / 计划 T3）

def test_default_mode_is_relative_and_stats_are_ratios():
    # 默认 mode="relative"：统计 (ask−bid)/((ask+bid)/2) 的分位（无量纲）
    ticks = make_ticks(
        [(f"2026-06-01 10:{i:02d}:00", 0.2 if i < 4 else 0.4) for i in range(5)],
        base=100.0)
    m = SpreadModel.from_ticks("XAUUSD", ticks)
    assert m.mode == "relative"
    assert SpreadModel().mode == "relative"          # 构造器默认同样相对
    rel_small, rel_big = 0.2 / 100.1, 0.4 / 100.2    # mid=(ask+bid)/2
    st = m.stat(6)                                   # UTC 10 → 纽约 6
    assert st.n == 5
    assert st.median == pytest.approx(rel_small)     # 4 小 1 大 → 中位=小
    assert st.p90 == pytest.approx(rel_small + 0.6 * (rel_big - rel_small))
    assert m.overall_median() == pytest.approx(rel_small)


def test_relative_spread_scales_with_price_and_requires_it():
    # 手算：bid 99.9 / ask 100.1 → spread 0.2、mid 100.0 → rel=0.002 恒定
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.2) for i in range(5)], base=99.9)
    m = SpreadModel.from_ticks("XAUUSD", ticks)
    assert m.stat(6).p90 == pytest.approx(0.002)
    assert m.spread_p90("XAUUSD", 6, price=1000.0) == pytest.approx(2.0)
    # 价格翻倍 → 点差（价格单位）恰翻倍（相对模型的核心性质，R1）
    assert m.spread_p90("XAUUSD", 6, price=2000.0) == pytest.approx(
        2 * m.spread_p90("XAUUSD", 6, price=1000.0))
    with pytest.raises(ValueError, match="price"):   # 相对口径缺 price 即拒绝（有统计的小时）
        m.spread_p90("XAUUSD", 6)
    for bad in (0.0, -100.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            m.spread_p90("XAUUSD", 6, price=bad)


def test_absolute_mode_ignores_price_and_matches_legacy_values():
    # 旧口径逐位一致：mode="absolute" 时 price 被完全忽略
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.0001 * (i + 1)) for i in range(5)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    assert m.mode == "absolute"
    legacy = 0.00046                                  # 与旧版实现完全相同的手算值
    assert m.spread_p90("EURUSD", 6) == pytest.approx(legacy)
    for p in (None, 1.0, 1234.5):
        assert m.spread_p90("EURUSD", 6, price=p) == pytest.approx(legacy)


def test_relative_rejects_nonpositive_mid():
    df = make_ticks([("2026-06-01 10:00:00", 0.0)], base=0.0)   # bid=ask=0
    with pytest.raises(ValueError, match="相对点差"):
        SpreadModel.from_ticks("EURUSD", df)


def test_invalid_mode_rejected():
    df = make_ticks([("2026-06-01 10:00:00", 0.2)])
    with pytest.raises(ValueError, match="mode"):
        SpreadModel.from_ticks("EURUSD", df, mode="foo")
    with pytest.raises(ValueError, match="mode"):
        SpreadModel(mode="foo")


def test_save_load_roundtrip_keeps_mode_and_legacy_file_is_absolute(tmp_path):
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.2) for i in range(5)], base=100.0)
    m = SpreadModel.from_ticks("EURUSD", ticks)                 # relative：rel=0.2/100.1
    p = str(tmp_path / "rel.json")
    m.save(p)
    m2 = SpreadModel.load(p)
    assert m2.mode == "relative"
    assert m2.spread_p90("EURUSD", 6, price=50.0) == pytest.approx(0.2 / 100.1 * 50.0)
    with pytest.raises(ValueError):
        m2.spread_p90("EURUSD", 6)

    # 旧文件没有 mode 键 → 按 absolute 兼容（无 price 也能查）
    a = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    d = a.to_dict()
    d.pop("mode")                                               # 模拟旧版本产物
    legacy_path = str(tmp_path / "legacy.json")
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    m3 = SpreadModel.load(legacy_path)
    assert m3.mode == "absolute"
    assert m3.spread_p90("EURUSD", 6) == pytest.approx(0.2)    # 无 price、价格单位

    # 文件里 mode 非法 → 报错而非猜测
    d["mode"] = "foo"
    bad_path = str(tmp_path / "bad.json")
    with open(bad_path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    with pytest.raises(ValueError, match="mode"):
        SpreadModel.load(bad_path)


def test_load_rejects_utc_bucketed_legacy_file(tmp_path):
    # T20 前（UTC 小时分桶）的落盘文件没有 bucket_tz 标注——与纽约本地小时
    # 口径不兼容，拒绝加载而非静默错读
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.2) for i in range(5)], base=100.0)
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    d = m.to_dict()
    d.pop("bucket_tz")
    p = str(tmp_path / "utc-buckets.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)
    with pytest.raises(ValueError, match="纽约本地小时"):
        SpreadModel.load(p)
    d["bucket_tz"] = "Europe/London"
    p2 = str(tmp_path / "wrong-tz.json")
    with open(p2, "w", encoding="utf-8") as f:
        json.dump(d, f)
    with pytest.raises(ValueError, match="bucket_tz"):
        SpreadModel.load(p2)


def test_merge_mode_rules_empty_adopts_mismatch_rejected():
    rel = SpreadModel.from_ticks("A", make_ticks([("2026-06-01 10:00:00", 0.2)], base=100.0))
    ab = SpreadModel.from_ticks("B", make_ticks([("2026-06-01 10:00:00", 0.2)]),
                                mode="absolute")
    assert SpreadModel().merge(ab).mode == "absolute"    # 空模型不约束口径
    assert ab.merge(SpreadModel()).mode == "absolute"
    with pytest.raises(ValueError, match="口径"):
        rel.merge(ab)
    with pytest.raises(ValueError, match="口径"):
        ab.merge(rel)


# ------------------------------------------------- T36（H2）：采样口径 + n_days

def test_default_sampling_is_bar_open_m1():
    # 缺省口径 = bar_open_m1（计划 T36：回测在 bar 开盘成交，tick 加权会把
    # 繁忙时段的窄点差放大权重）；tick 口径显式保留对比
    ticks = make_ticks([("2026-06-01 10:00:00", 0.0002)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    assert m.sampling == "bar_open_m1"
    assert SpreadModel().sampling == "bar_open_m1"
    assert SpreadModel.from_ticks("EURUSD", ticks, mode="absolute",
                                  sampling="tick").sampling == "tick"
    with pytest.raises(ValueError, match="sampling"):
        SpreadModel.from_ticks("EURUSD", ticks, mode="absolute", sampling="foo")


def test_bar_open_m1_sampling_matches_hand_computation():
    """失败测试清单第 1 条：每个 M1 边界后的第一笔 tick 才进统计（手算）。

    2026-06-01 为 EDT（UTC−4）：UTC 14/15 点 → 纽约 10/11 点。
    纽约 10 点：分钟 14:00 首笔 0.0002（:20/:40 的 0.0006/0.0004 不采），
    分钟 14:01 首笔 0.0008（恰在边界 14:01:00.000 的 tick 属本分钟）；
    纽约 11 点：15:00 首笔 0.0003、15:01 首笔 0.0009。
    """
    rows = [
        ("2026-06-01 14:00:00.000", 0.0002),   # ← 采样（分钟首笔）
        ("2026-06-01 14:00:20.000", 0.0006),
        ("2026-06-01 14:00:40.000", 0.0004),
        ("2026-06-01 14:01:00.000", 0.0008),   # ← 采样（恰在边界上）
        ("2026-06-01 14:01:30.000", 0.0010),
        ("2026-06-01 15:00:05.000", 0.0003),   # ← 采样
        ("2026-06-01 15:01:00.000", 0.0009),   # ← 采样
        ("2026-06-01 15:01:10.000", 0.0005),
    ]
    df = pd.DataFrame({
        "time_msc": [pd.Timestamp(t).value // 10**6 for t, _ in rows],
        "bid": [1.0] * len(rows),
        "ask": [1.0 + s for _, s in rows],
    })
    m = SpreadModel.from_ticks("EURUSD", df, mode="absolute")
    st10, st11 = m.stat(10), m.stat(11)
    assert st10.n == 2 and st11.n == 2          # 每分钟恰一笔
    assert st10.median == pytest.approx((0.0002 + 0.0008) / 2)
    assert st10.p90 == pytest.approx(0.0002 + 0.9 * (0.0008 - 0.0002))
    assert st10.p99 == pytest.approx(0.0002 + 0.99 * (0.0008 - 0.0002))
    assert st11.median == pytest.approx((0.0003 + 0.0009) / 2)
    # 全体中位同样只按采样样本：4 个样本排序 [2,3,8,9]e-4 → 中位 (3+8)/2
    assert m.overall_median() == pytest.approx((0.0003 + 0.0008) / 2)
    # tick 口径对照：全部 8 笔都进统计（旧口径行为保留）
    mt = SpreadModel.from_ticks("EURUSD", df, mode="absolute", sampling="tick")
    assert mt.stat(10).n == 5 and mt.stat(11).n == 3
    assert mt.stat(10).median == pytest.approx(0.0006)   # [2,4,6,8,10]e-4


def test_bar_open_m1_rejects_unsorted_ticks():
    # 采样口径要求 time_msc 升序（分钟首笔=游程首笔的前提；库内写盘已保证）
    rows = [("2026-06-01 14:01:00.000", 0.0002), ("2026-06-01 14:00:00.000", 0.0004)]
    df = pd.DataFrame({
        "time_msc": [pd.Timestamp(t).value // 10**6 for t, _ in rows],
        "bid": [1.0, 1.0], "ask": [1.0 + s for _, s in rows],
    })
    with pytest.raises(ValueError, match="升序"):
        SpreadModel.from_ticks("EURUSD", df, mode="absolute")


def test_n_days_counts_distinct_ny_dates_per_hour():
    # 三个纽约日期在 10 点各有一笔采样 → n_days=3；11 点只有一天 → 1
    rows = [(f"2026-06-{d:02d} 14:00:00", 0.0002) for d in (1, 2, 3)]
    rows += [("2026-06-03 15:00:00", 0.0004)]          # 时间升序（采样口径前提）
    m = SpreadModel.from_ticks("EURUSD", make_ticks(rows), mode="absolute")
    assert m.stat(10).n == 3 and m.stat(10).n_days == 3
    assert m.stat(11).n == 1 and m.stat(11).n_days == 1
    # tick 口径同样记 n_days
    mt = SpreadModel.from_ticks("EURUSD", make_ticks(rows), mode="absolute",
                                sampling="tick")
    assert mt.stat(10).n_days == 3


def test_insufficient_n_days_is_missing_stats():
    """失败测试清单第 2 条：n_days < min_days_per_hour → 缺统计（forbid 语义）。

    用手工统计表钉死查询语义（构建侧的 n_days 计数另有测试）：
    小时 6 有 3 天覆盖（< 20）、小时 10 有 25 天（足额）。
    """
    thin = SpreadStat(median=0.0002, p90=0.0003, p99=0.0004, n=3, n_days=3)
    rich = SpreadStat(median=0.0010, p90=0.0020, p99=0.0030, n=250, n_days=25)
    m = SpreadModel({"EURUSD": {6: thin, 10: rich}}, {"EURUSD": 0.0005},
                    mode="absolute")
    # 阈值 0（缺省）＝ 旧语义：只有结构上无统计的小时才算缺
    assert m.missing_hours("EURUSD") == tuple(h for h in range(24) if h not in (6, 10))
    assert m.spread_p90("EURUSD", 6) == pytest.approx(0.0003)
    # 阈值 20：小时 6 覆盖不足 → 缺统计；查价回落到足额小时的最大 p90
    assert m.missing_hours("EURUSD", min_days_per_hour=20) == tuple(
        h for h in range(24) if h != 10)
    assert m.spread_p90("EURUSD", 6, min_days_per_hour=20) == pytest.approx(0.0020)
    assert m.spread_p90("EURUSD", 10, min_days_per_hour=20) == pytest.approx(0.0020)
    # 回退的"全天最大"也只用足额小时（覆盖不足的小时 p90 不得抬高保守值）
    thin_spike = SpreadStat(median=0.02, p90=0.09, p99=0.10, n=3, n_days=3)
    m2 = SpreadModel({"EURUSD": {6: thin_spike, 10: rich}}, {"EURUSD": 0.0005},
                     mode="absolute")
    assert m2.spread_p90("EURUSD", 7, min_days_per_hour=20) == pytest.approx(0.0020)
    # relative 口径下同样按 price 缩放（缺统计回退）
    rel = SpreadModel(
        {"XAUUSD": {6: SpreadStat(1e-4, 2e-4, 3e-4, 3, 3),
                    10: SpreadStat(1e-3, 2e-3, 3e-3, 250, 25)}},
        {"XAUUSD": 5e-4})
    assert rel.spread_p90("XAUUSD", 6, price=2000.0,
                          min_days_per_hour=20) == pytest.approx(2e-3 * 2000.0)


def test_save_load_roundtrip_preserves_sampling_and_n_days(tmp_path):
    rows = [(f"2026-06-{d:02d} 14:00:00", 0.0002) for d in (1, 2)]
    m = SpreadModel.from_ticks("EURUSD", make_ticks(rows), mode="absolute")
    assert m.stat(10).n_days == 2
    p = str(tmp_path / "spread.json")
    m.save(p)
    m2 = SpreadModel.load(p)
    assert m2.sampling == "bar_open_m1"
    assert m2.stat(10) == m.stat(10)                 # 含 n_days 逐位一致
    # 旧文件（T36 之前）没有 sampling 键 → 按 tick 口径解释（统计是全 tick 的）
    d = m.to_dict()
    d.pop("sampling")
    legacy = str(tmp_path / "legacy-t36.json")
    with open(legacy, "w", encoding="utf-8") as f:
        json.dump(d, f)
    assert SpreadModel.load(legacy).sampling == "tick"
    d["sampling"] = "foo"
    bad = str(tmp_path / "bad-sampling.json")
    with open(bad, "w", encoding="utf-8") as f:
        json.dump(d, f)
    with pytest.raises(ValueError, match="sampling"):
        SpreadModel.load(bad)


def test_merge_sampling_rules_mirror_mode():
    rows = [("2026-06-01 10:00:00", 0.0002)]
    bar = SpreadModel.from_ticks("A", make_ticks(rows), mode="absolute")
    tick = SpreadModel.from_ticks("B", make_ticks(rows), mode="absolute",
                                  sampling="tick")
    assert SpreadModel().merge(tick).sampling == "tick"     # 空模型继承
    assert tick.merge(SpreadModel()).sampling == "tick"
    with pytest.raises(ValueError, match="采样"):
        bar.merge(tick)


def test_builder_streaming_equals_one_shot():
    """失败测试清单第 4 条（点差侧）：按块喂入与一次性喂入结果逐位一致。

    跨块去重：上一块末尾未走完的分钟，下一块的同分钟 tick 不得再采样。
    """
    from mt5quant.costs.spread import SpreadModelBuilder

    rows = []                       # 三个"月"，每分钟 3 笔、跨小时
    for day in (1, 2, 3):
        for minute in (0, 1):
            for k, sec in enumerate((0.0, 20.0, 40.0)):
                rows.append((f"2026-06-{day:02d} 14:{minute:02d}:{int(sec):02d}"
                             f".{int(sec % 1 * 1000):03d}", 0.0001 * (k + 1) * day * (minute + 1)))
    df = pd.DataFrame({
        "time_msc": [pd.Timestamp(t).value // 10**6 for t, _ in rows],
        "bid": [1.0] * len(rows),
        "ask": [1.0 + s for _, s in rows],
    })
    one_shot = SpreadModel.from_ticks("EURUSD", df, mode="absolute")
    streamed = SpreadModelBuilder("EURUSD", mode="absolute")
    # 任意切块（含"分钟被块边界劈开"的病态切法）都必须得到同一结果
    for lo, hi in ((0, 7), (7, 7), (7, 13), (13, len(rows))):
        streamed.add_ticks(df.iloc[lo:hi])
    streamed_model = streamed.build()
    assert streamed_model.sampling == one_shot.sampling
    table = one_shot._per_symbol["EURUSD"]        # noqa: SLF001 — 对照内部表
    for h, st in table.items():
        assert streamed_model.stat(h) == st
    assert streamed_model.overall_median() == pytest.approx(one_shot.overall_median())
