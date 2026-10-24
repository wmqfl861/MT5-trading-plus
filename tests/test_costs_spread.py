"""SpreadModel 手算测试：奇偶样本分位、UTC 小时分桶、rollover、save/load、多品种；
相对口径（R1/T3）：rel=(ask−bid)/mid、spread_p90(price) 随价格缩放、旧文件兼容。"""
import json
import os

import numpy as np
import pandas as pd
import pytest

from mt5quant.costs.spread import SpreadModel, SpreadStat


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
    # 奇样本 n=5，线性分位：p90=4+0.6×(5−4)=4.6、p99=4+0.96×1=4.96
    st10 = m.stat(10)
    assert isinstance(st10, SpreadStat)
    assert st10.n == 5
    assert st10.median == pytest.approx(0.0003)
    assert st10.p90 == pytest.approx(0.00046)
    assert st10.p99 == pytest.approx(0.000496)
    # 偶样本 n=4：median=(2+3)/2、p90=3+0.7×1=3.7、p99=3+0.97×1=3.97
    st14 = m.stat(14)
    assert st14.n == 4
    assert st14.median == pytest.approx(0.00025)
    assert st14.p90 == pytest.approx(0.00037)
    assert st14.p99 == pytest.approx(0.000397)


def test_hour_bucket_is_utc_from_epoch_msc():
    # epoch 毫秒直读 UTC：23:59 与次日 00:01 分属 23/0 两个桶
    ticks = make_ticks([("2026-06-01 23:59:00", 0.0002), ("2026-06-02 00:01:00", 0.0004)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    assert m.stat(23).n == 1 and m.stat(23).median == pytest.approx(0.0002)
    assert m.stat(0).n == 1 and m.stat(0).median == pytest.approx(0.0004)


def test_spread_p90_returns_p90_and_validates():
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.0001 * (i + 1)) for i in range(5)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    # 口径：返回该小时 p90（保守），不是 p90 的一半
    assert m.spread_p90("EURUSD", 10) == pytest.approx(0.00046)
    with pytest.raises(ValueError):  # 品种不在模型
        m.spread_p90("XAUUSD", 10)
    with pytest.raises(ValueError):  # 小时无统计
        m.spread_p90("EURUSD", 3)


def test_rollover_hours_flag_p99_spike():
    rows = [(f"2026-06-01 10:{i // 60:02d}:{i % 60:02d}", 0.0001) for i in range(100)]
    rows += [
        (f"2026-06-01 21:{i // 60:02d}:{i % 60:02d}", 0.02 if i == 19 else 0.0001)
        for i in range(20)
    ]
    m = SpreadModel.from_ticks("EURUSD", make_ticks(rows), mode="absolute")
    # 21 点 p99 = 0.0001+0.81×(0.02−0.0001) = 0.016219 > 3×全体中位 0.0003
    assert m.stat(21).p99 == pytest.approx(0.016219)
    assert m.rollover_hours() == (21,)  # 常规小时（p99=0.0001）不误报
    assert m.overall_median() == pytest.approx(0.0001)


def test_save_load_roundtrip(tmp_path):
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.0001 * (i + 1)) for i in range(5)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    p = str(tmp_path / "spread.json")
    m.save(p)
    assert not os.path.exists(p + ".tmp")  # 原子写不留残片
    m2 = SpreadModel.load(p)
    assert m2.stat(10) == m.stat(10)
    assert m2.spread_p90("EURUSD", 10) == pytest.approx(0.00046)
    assert m2.rollover_hours() == m.rollover_hours()


def test_from_ticks_reads_only_required_columns():
    # 只含 time_msc/bid/ask 三列即可（不依赖 mt5data.ticks 的帧定义）
    df = make_ticks([("2026-06-01 10:00:00", 0.0002)])
    m = SpreadModel.from_ticks("EURUSD", df, mode="absolute")
    assert m.stat(10).n == 1


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
    assert m.spread_p90("EURUSD", 10) == pytest.approx(0.0002)
    assert m.spread_p90("XAUUSD", 10) == pytest.approx(0.30)
    with pytest.raises(ValueError):  # 多品种时无 symbol 的查询必须显式
        m.stat(10)
    with pytest.raises(ValueError):
        m.rollover_hours()
    assert m.rollover_hours("EURUSD") == ()
    assert m.stat(10, symbol="EURUSD").p90 == pytest.approx(0.0002)
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
    st = m.stat(10)
    assert st.n == 5
    assert st.median == pytest.approx(rel_small)     # 4 小 1 大 → 中位=小
    assert st.p90 == pytest.approx(rel_small + 0.6 * (rel_big - rel_small))
    assert m.overall_median() == pytest.approx(rel_small)


def test_relative_spread_scales_with_price_and_requires_it():
    # 手算：bid 99.9 / ask 100.1 → spread 0.2、mid 100.0 → rel=0.002 恒定
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.2) for i in range(5)], base=99.9)
    m = SpreadModel.from_ticks("XAUUSD", ticks)
    assert m.stat(10).p90 == pytest.approx(0.002)
    assert m.spread_p90("XAUUSD", 10, price=1000.0) == pytest.approx(2.0)
    # 价格翻倍 → 点差（价格单位）恰翻倍（相对模型的核心性质，R1）
    assert m.spread_p90("XAUUSD", 10, price=2000.0) == pytest.approx(
        2 * m.spread_p90("XAUUSD", 10, price=1000.0))
    with pytest.raises(ValueError, match="price"):   # 相对口径缺 price 即拒绝
        m.spread_p90("XAUUSD", 10)
    for bad in (0.0, -100.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            m.spread_p90("XAUUSD", 10, price=bad)


def test_absolute_mode_ignores_price_and_matches_legacy_values():
    # 旧口径逐位一致：mode="absolute" 时 price 被完全忽略
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.0001 * (i + 1)) for i in range(5)])
    m = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    assert m.mode == "absolute"
    legacy = 0.00046                                  # 与旧版实现完全相同的手算值
    assert m.spread_p90("EURUSD", 10) == pytest.approx(legacy)
    for p in (None, 1.0, 1234.5):
        assert m.spread_p90("EURUSD", 10, price=p) == pytest.approx(legacy)


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
    assert m2.spread_p90("EURUSD", 10, price=50.0) == pytest.approx(0.2 / 100.1 * 50.0)
    with pytest.raises(ValueError):
        m2.spread_p90("EURUSD", 10)

    # 旧文件没有 mode 键 → 按 absolute 兼容（无 price 也能查）
    a = SpreadModel.from_ticks("EURUSD", ticks, mode="absolute")
    d = a.to_dict()
    d.pop("mode")                                               # 模拟旧版本产物
    legacy_path = str(tmp_path / "legacy.json")
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    m3 = SpreadModel.load(legacy_path)
    assert m3.mode == "absolute"
    assert m3.spread_p90("EURUSD", 10) == pytest.approx(0.2)    # 无 price、价格单位

    # 文件里 mode 非法 → 报错而非猜测
    d["mode"] = "foo"
    bad_path = str(tmp_path / "bad.json")
    with open(bad_path, "w", encoding="utf-8") as f:
        json.dump(d, f)
    with pytest.raises(ValueError, match="mode"):
        SpreadModel.load(bad_path)


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
