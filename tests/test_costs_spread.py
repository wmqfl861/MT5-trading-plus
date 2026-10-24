"""SpreadModel 手算测试：奇偶样本分位、UTC 小时分桶、rollover、save/load、多品种。"""
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
    m = SpreadModel.from_ticks("EURUSD", ticks)
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
    m = SpreadModel.from_ticks("EURUSD", ticks)
    assert m.stat(23).n == 1 and m.stat(23).median == pytest.approx(0.0002)
    assert m.stat(0).n == 1 and m.stat(0).median == pytest.approx(0.0004)


def test_half_spread_returns_p90_and_validates():
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.0001 * (i + 1)) for i in range(5)])
    m = SpreadModel.from_ticks("EURUSD", ticks)
    # 口径：返回该小时 p90（保守），不是 p90 的一半
    assert m.half_spread("EURUSD", 10) == pytest.approx(0.00046)
    with pytest.raises(ValueError):  # 品种不在模型
        m.half_spread("XAUUSD", 10)
    with pytest.raises(ValueError):  # 小时无统计
        m.half_spread("EURUSD", 3)


def test_rollover_hours_flag_p99_spike():
    rows = [(f"2026-06-01 10:{i // 60:02d}:{i % 60:02d}", 0.0001) for i in range(100)]
    rows += [
        (f"2026-06-01 21:{i // 60:02d}:{i % 60:02d}", 0.02 if i == 19 else 0.0001)
        for i in range(20)
    ]
    m = SpreadModel.from_ticks("EURUSD", make_ticks(rows))
    # 21 点 p99 = 0.0001+0.81×(0.02−0.0001) = 0.016219 > 3×全体中位 0.0003
    assert m.stat(21).p99 == pytest.approx(0.016219)
    assert m.rollover_hours() == (21,)  # 常规小时（p99=0.0001）不误报
    assert m.overall_median() == pytest.approx(0.0001)


def test_save_load_roundtrip(tmp_path):
    ticks = make_ticks([(f"2026-06-01 10:{i:02d}:00", 0.0001 * (i + 1)) for i in range(5)])
    m = SpreadModel.from_ticks("EURUSD", ticks)
    p = str(tmp_path / "spread.json")
    m.save(p)
    assert not os.path.exists(p + ".tmp")  # 原子写不留残片
    m2 = SpreadModel.load(p)
    assert m2.stat(10) == m.stat(10)
    assert m2.half_spread("EURUSD", 10) == pytest.approx(0.00046)
    assert m2.rollover_hours() == m.rollover_hours()


def test_from_ticks_reads_only_required_columns():
    # 只含 time_msc/bid/ask 三列即可（不依赖 mt5data.ticks 的帧定义）
    df = make_ticks([("2026-06-01 10:00:00", 0.0002)])
    m = SpreadModel.from_ticks("EURUSD", df)
    assert m.stat(10).n == 1


def test_from_ticks_rejects_missing_columns_and_empty():
    with pytest.raises(ValueError):
        SpreadModel.from_ticks("EURUSD", pd.DataFrame({"time_msc": [], "bid": []}))
    df = make_ticks([("2026-06-01 10:00:00", 0.0002)]).drop(columns=["ask"])
    with pytest.raises(ValueError):
        SpreadModel.from_ticks("EURUSD", df)


def test_merge_and_multi_symbol_lookup():
    a = SpreadModel.from_ticks("EURUSD", make_ticks([("2026-06-01 10:00:00", 0.0002)]))
    b = SpreadModel.from_ticks("XAUUSD", make_ticks([("2026-06-01 10:00:00", 0.30)]))
    m = a.merge(b)
    assert m.symbols == ("EURUSD", "XAUUSD")
    assert m.half_spread("EURUSD", 10) == pytest.approx(0.0002)
    assert m.half_spread("XAUUSD", 10) == pytest.approx(0.30)
    with pytest.raises(ValueError):  # 多品种时无 symbol 的查询必须显式
        m.stat(10)
    with pytest.raises(ValueError):
        m.rollover_hours()
    assert m.rollover_hours("EURUSD") == ()
    assert m.stat(10, symbol="EURUSD").p90 == pytest.approx(0.0002)
    with pytest.raises(ValueError):  # 品种重复不能合并
        a.merge(a)
