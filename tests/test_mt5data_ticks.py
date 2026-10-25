"""Task 2（P1 tick 采集/存储）+ T4（tick 按月分区 R3）测试。

假件贴真包基线 + 回补/刷新/存储语义；覆盖计划用例 a–g、假件契约、
None 上抛、静默窗口、空表守卫，以及按月分区的跨月写入、刷新只动末月、
区间读取只开必要月份、旧布局迁移提示与守恒。
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import mt5data.ticks as ticks_mod
from mt5gw.connection import MT5Connection
from mt5gw.errors import DataUnavailableError
from mt5data import store
from mt5data.ticks import (
    TICK_COLUMNS,
    DataIntegrityError,
    TicksLayoutError,
    backfill_ticks,
    load_ticks,
    merge_ticks,
    migrate_old_layout,
    read_ticks_watermark,
    refresh_ticks,
    save_ticks,
    ticks_dir,
)
from tests.fakes_ticks import FakeTicksMT5


def _sec(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _ms(dt):
    return _sec(dt) * 1000


def make_conn(fake):
    conn = MT5Connection(mt5_module=fake)
    conn.connect()
    return conn


def rows_from(start, n, step_s=3600, bid0=2000.0):
    """n 条从 start 起、每 step_s 秒一条的 (time_msc,bid,ask,last,volume,flags)。"""
    return [
        (_ms(start + timedelta(seconds=step_s * i)),
         bid0 + i, bid0 + i + 0.5, 0.0, 0.0, 2)
        for i in range(n)
    ]


def add_page(fake, start, end, rows):
    fake.pages.append({"from_s": _sec(start), "to_s": _sec(end), "rows": rows})


def ticks_df(rows):
    return pd.DataFrame(rows, columns=TICK_COLUMNS)


# ---- 假件契约：贴真包（Review Focus #3 的地基） ----


def test_fake_matches_real_package_contract():
    fake = FakeTicksMT5()
    assert fake.COPY_TICKS_ALL == -1
    add_page(fake, datetime(2026, 8, 1), datetime(2026, 8, 2),
             rows_from(datetime(2026, 8, 1), 3))
    arr = fake.copy_ticks_range(
        "XAUUSD", _sec(datetime(2026, 8, 1)), _sec(datetime(2026, 8, 2)),
        fake.COPY_TICKS_ALL)
    assert isinstance(arr, np.ndarray)
    assert list(arr.dtype.names) == TICK_COLUMNS  # 字段名恰为 TICK_COLUMNS
    assert isinstance(arr[0], np.void)  # 单行 numpy.void
    # 无数据窗口：真包返回 ()
    empty = fake.copy_ticks_range(
        "XAUUSD", _sec(datetime(2026, 9, 1)), _sec(datetime(2026, 9, 2)),
        fake.COPY_TICKS_ALL)
    assert empty == ()


# ---- 纯函数 ----


def test_ticks_dir_preserves_symbol_case(tmp_path):
    d = ticks_dir(str(tmp_path), "XAUUSDm")
    assert d == os.path.join(str(tmp_path), "ticks", "XAUUSDm")


def test_merge_ticks_dedups_identical_rows_and_sorts():
    """整行去重：完全相同的行（重叠重抓）只留一份；升序、列序化。

    同 time_msc 但价格不同的行是**两条真实 tick**（真包同一毫秒可有多条），
    必须都保留（稳定排序，旧帧行在前、新帧行在后）——绝不按 time_msc 去重。
    """
    old = ticks_df([(300, 1.0, 1.1, 0, 0, 2), (100, 1.0, 1.1, 0, 0, 2)])
    new = ticks_df([(300, 9.9, 9.99, 0, 0, 4), (200, 1.0, 1.1, 0, 0, 2),
                    (300, 1.0, 1.1, 0, 0, 2)])  # 末行=与 old 完全相同的重叠重抓
    merged = merge_ticks(old, new)
    assert list(merged["time_msc"]) == [100, 200, 300, 300]  # 升序，同毫秒两条都在
    at300 = merged[merged["time_msc"] == 300]
    assert len(at300) == 2
    assert sorted(at300["bid"]) == [1.0, 9.9]  # 两条都在；完全相同行不重复
    assert list(merged.columns) == TICK_COLUMNS


def test_backfill_same_millisecond_ticks_are_kept_not_rejected(tmp_path):
    """真机回归：真实 MT5 tick 同一毫秒可有多条（bid/ask 更新）——

    相邻 time_msc 相等必须照常收录；只有真正倒退（后一条更早）才判损坏。
    """
    fake = FakeTicksMT5()
    start, end = datetime(2026, 8, 1), datetime(2026, 8, 2)
    base = rows_from(start, 4, step_s=3600)
    # 在第 2 条的同一毫秒插入一条不同价的 tick（模拟同 ms 多条）
    dup_msc = base[1][0]
    same_ms = (dup_msc, 2.5, 2.6, 0, 0, 8)
    rows = base[:2] + [same_ms] + base[2:]
    add_page(fake, start, end, rows)
    added = backfill_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                           start=start, end=end)
    assert added == 5  # 一条不少
    df = load_ticks(str(tmp_path), "XAUUSD")
    t = df["time_msc"].to_numpy()
    assert (np.diff(t) >= 0).all()  # 不倒退
    assert (t == dup_msc).sum() == 2  # 同毫秒两条都在


# ---- backfill（用例 a/b/e + 扩展） ----


def test_backfill_chunks_merge_sorted_unique(tmp_path):
    """a) 分块拼接：升序、无重复、边界 tick 恰在两窗都被抓回也不重。"""
    fake = FakeTicksMT5()
    start, end = datetime(2026, 8, 1), datetime(2026, 8, 16)
    rows = rows_from(start, 60, step_s=6 * 3600)  # 8/1 00:00 → 8/15 18:00
    add_page(fake, start, end, rows)
    progress = []
    added = backfill_ticks(
        make_conn(fake), fake, str(tmp_path), "XAUUSD",
        start=start, end=end, chunk_days=7,
        on_progress=lambda a, b, n: progress.append((a, b, n)))
    assert added == 60
    # 从 end 往回：[8/9,8/16] [8/2,8/9] [8/1,8/2] 共 3 窗
    assert len(fake.copy_ticks_range_calls) == 3
    assert all(c[3] == -1 for c in fake.copy_ticks_range_calls)  # COPY_TICKS_ALL
    assert len(progress) == 3
    df = load_ticks(str(tmp_path), "XAUUSD")
    t = df["time_msc"].to_numpy()
    assert len(t) == 60 and len(set(t)) == 60  # 8/9 00:00 边界 tick 双窗抓回→去重
    assert (np.diff(t) > 0).all()
    wm = read_ticks_watermark(str(tmp_path), "XAUUSD")
    assert wm["rows"] == 60
    assert wm["first_msc"] == _ms(start)
    assert wm["last_msc"] == _ms(datetime(2026, 8, 15, 18))


def test_backfill_three_empty_windows_finish_normally(tmp_path):
    """b) 连续空窗 3 块 → 视为历史尽头正常收尾（不报错、不再空转）。"""
    fake = FakeTicksMT5()
    start, end = datetime(2026, 1, 1), datetime(2026, 3, 1)
    rows = rows_from(datetime(2026, 2, 22, 12), 10)  # 数据只在 2/22 12:00 起
    add_page(fake, datetime(2026, 2, 1), end, rows)
    added = backfill_ticks(make_conn(fake), fake, str(tmp_path), "EURUSD",
                           start=start, end=end, chunk_days=7)
    assert added == 10
    # [2/22,3/1] 有数据后：[2/15,2/22][2/8,2/15][2/1,2/8] 三连空即收尾（4 次调用）
    assert len(fake.copy_ticks_range_calls) == 4
    df = load_ticks(str(tmp_path), "EURUSD")
    assert len(df) == 10 and df["time_msc"].is_monotonic_increasing


def test_backfill_rerun_union_no_duplicates(tmp_path):
    fake = FakeTicksMT5()
    start, end = datetime(2026, 8, 1), datetime(2026, 8, 3)
    add_page(fake, start, end, rows_from(start, 20, step_s=3600))
    conn = make_conn(fake)
    assert backfill_ticks(conn, fake, str(tmp_path), "XAUUSD",
                          start=start, end=end) == 20
    assert backfill_ticks(conn, fake, str(tmp_path), "XAUUSD",
                          start=start, end=end) == 0  # 重跑并集：0 新增
    assert len(load_ticks(str(tmp_path), "XAUUSD")) == 20


def test_backfill_none_page_raises_with_last_error(tmp_path):
    fake = FakeTicksMT5()
    start, end = datetime(2026, 8, 1), datetime(2026, 8, 2)
    fake.pages.append({"from_s": _sec(start), "to_s": _sec(end),
                       "error": (4, "No history data")})
    with pytest.raises(DataUnavailableError, match="No history data"):
        backfill_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                       start=start, end=end)


def test_backfill_disordered_page_raises_and_keeps_old_file(tmp_path):
    """e) 块内 time_msc 非严格升序 → 抛，且残缺历史绝不落盘。"""
    fake = FakeTicksMT5()
    start, end = datetime(2026, 8, 1), datetime(2026, 8, 3)
    add_page(fake, start, end, rows_from(start, 5))
    backfill_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                   start=start, end=end)
    d = tmp_path / "ticks" / "XAUUSD"
    month_file = d / "2026" / "08.parquet"
    before = month_file.read_bytes()

    rows = rows_from(start, 5)
    bad = FakeTicksMT5()
    add_page(bad, start, end, [rows[2], rows[0], rows[1], rows[3], rows[4]])
    with pytest.raises(DataIntegrityError, match="倒退"):
        backfill_ticks(make_conn(bad), bad, str(tmp_path), "XAUUSD",
                       start=start, end=end)
    assert month_file.read_bytes() == before  # 旧文件一字节不动


def test_backfill_no_data_at_all_raises(tmp_path):
    fake = FakeTicksMT5()  # 无任何页 → 全空
    with pytest.raises(DataUnavailableError, match="未取到任何数据"):
        backfill_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                       start=datetime(2026, 8, 1), end=datetime(2026, 8, 8))


def test_backfill_requires_start_and_ordered_window(tmp_path):
    fake = FakeTicksMT5()
    conn = make_conn(fake)
    with pytest.raises(ValueError, match="start"):
        backfill_ticks(conn, fake, str(tmp_path), "XAUUSD")
    with pytest.raises(ValueError, match="晚于"):
        backfill_ticks(conn, fake, str(tmp_path), "XAUUSD",
                       start=datetime(2026, 8, 2), end=datetime(2026, 8, 1))


def test_backfill_records_source_in_meta(tmp_path):
    fake = FakeTicksMT5()
    add_page(fake, datetime(2026, 8, 1), datetime(2026, 8, 2),
             rows_from(datetime(2026, 8, 1), 4))
    backfill_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                   start=datetime(2026, 8, 1), end=datetime(2026, 8, 2),
                   meta_info={"server": "Exness-Trial", "login": 7})
    wm = read_ticks_watermark(str(tmp_path), "XAUUSD")
    assert wm["server"] == "Exness-Trial" and wm["login"] == 7


# ---- refresh（用例 c/d + 静默语义） ----


def test_refresh_with_boundary_tick_merges(tmp_path):
    """c) 抓回页含边界 tick → 合并且旧尾保留、边界不重复。"""
    fake = FakeTicksMT5()
    old_rows = rows_from(datetime(2026, 8, 1), 5)  # 00:00..04:00
    save_ticks(str(tmp_path), "XAUUSD", ticks_df(old_rows),
               server="Exness-Trial", login=42)
    page_rows = ([old_rows[-1]]
                 + rows_from(datetime(2026, 8, 1, 4, 30), 3, step_s=1800))
    add_page(fake, datetime(2026, 8, 1), datetime(2026, 8, 1, 23), page_rows)
    added = refresh_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                          now_server=datetime(2026, 8, 1, 6))
    assert added == 3
    df = load_ticks(str(tmp_path), "XAUUSD")
    assert len(df) == 8  # 旧 5 + 新 3；边界 tick 只算一条
    t = df["time_msc"].to_numpy()
    assert (np.diff(t) > 0).all()
    assert _ms(datetime(2026, 8, 1, 4)) in set(t)  # 旧尾保留


def test_refresh_without_boundary_tick_raises_and_keeps_file(tmp_path):
    """d) 抓回页缺边界 tick → 抛"无法证明接续"，文件不动。"""
    fake = FakeTicksMT5()
    old_rows = rows_from(datetime(2026, 8, 1), 5)
    save_ticks(str(tmp_path), "XAUUSD", ticks_df(old_rows))
    add_page(fake, datetime(2026, 8, 1), datetime(2026, 8, 1, 23),
             rows_from(datetime(2026, 8, 1, 4, 30), 3))  # 缺 04:00 边界
    d = tmp_path / "ticks" / "XAUUSD"
    month_file = d / "2026" / "08.parquet"
    before = month_file.read_bytes()
    with pytest.raises(DataIntegrityError, match="无法证明接续"):
        refresh_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                      now_server=datetime(2026, 8, 1, 6))
    assert month_file.read_bytes() == before


def test_refresh_empty_page_within_week_returns_zero(tmp_path):
    """空抓回且静默 < 7 天 → 0（周末/无行情），文件不动。"""
    fake = FakeTicksMT5()  # 无页 → 全空
    save_ticks(str(tmp_path), "XAUUSD",
               ticks_df(rows_from(datetime(2026, 8, 1), 5)))
    d = tmp_path / "ticks" / "XAUUSD"
    month_file = d / "2026" / "08.parquet"
    before = month_file.read_bytes()
    added = refresh_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                          now_server=datetime(2026, 8, 3))  # 静默 2 天
    assert added == 0
    assert month_file.read_bytes() == before


def test_refresh_empty_page_beyond_week_raises(tmp_path):
    fake = FakeTicksMT5()
    save_ticks(str(tmp_path), "XAUUSD",
               ticks_df(rows_from(datetime(2026, 8, 1), 5)))
    with pytest.raises(DataIntegrityError, match="无法证明接续"):
        refresh_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                      now_server=datetime(2026, 8, 12))  # 静默超一周


def test_refresh_without_history_raises(tmp_path):
    fake = FakeTicksMT5()
    with pytest.raises(DataUnavailableError, match="backfill"):
        refresh_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                      now_server=datetime(2026, 8, 1))


# ---- 存储（用例 f/g） ----


def test_save_load_roundtrip_preserves_dtypes_and_column_order(tmp_path):
    """f) 往返：dtype 与列序不变，乱序列入参被规范化，无 .tmp 残留。"""
    rows = rows_from(datetime(2026, 8, 1), 3)
    save_ticks(str(tmp_path), "XAUUSDm", ticks_df(rows))
    df = load_ticks(str(tmp_path), "XAUUSDm")
    assert list(df.columns) == TICK_COLUMNS
    assert str(df["time_msc"].dtype) == "int64"
    for c in ("bid", "ask", "last", "volume"):
        assert str(df[c].dtype) == "float64"
    assert str(df["flags"].dtype) == "int64"
    assert list(df["time_msc"]) == [r[0] for r in rows]
    wm = read_ticks_watermark(str(tmp_path), "XAUUSDm")
    assert wm["rows"] == 3
    assert wm["first_msc"] == rows[0][0] and wm["last_msc"] == rows[-1][0]
    assert wm["months"] == ["2026-08"]
    assert "updated_at_utc" in wm
    # 原子写不留临时文件（按月分区后 tmp 在年份子目录里，递归查）
    leftovers = [str(p) for p in (tmp_path / "ticks" / "XAUUSDm").rglob("*.tmp")]
    assert leftovers == []
    # 列序打乱入参 → 落盘仍按 TICK_COLUMNS
    scrambled = ticks_df(rows)[list(reversed(TICK_COLUMNS))]
    save_ticks(str(tmp_path), "EURUSD", scrambled)
    assert list(load_ticks(str(tmp_path), "EURUSD").columns) == TICK_COLUMNS


def test_meta_server_login_inherited_when_omitted(tmp_path):
    """g) meta server/login 缺省继承旧值，别用 null 覆盖数据来源。"""
    root = str(tmp_path)
    rows = rows_from(datetime(2026, 8, 1), 2)
    save_ticks(root, "XAUUSD", ticks_df(rows), server="Exness-Trial",
               login=123456789)
    wm = read_ticks_watermark(root, "XAUUSD")
    assert wm["server"] == "Exness-Trial" and wm["login"] == 123456789
    # 不带来源再写：继承
    save_ticks(root, "XAUUSD", ticks_df(rows_from(datetime(2026, 8, 1), 3)))
    wm = read_ticks_watermark(root, "XAUUSD")
    assert wm["server"] == "Exness-Trial" and wm["login"] == 123456789
    # 只给 server：login 仍继承
    save_ticks(root, "XAUUSD", ticks_df(rows), server="Other")
    wm = read_ticks_watermark(root, "XAUUSD")
    assert wm["server"] == "Other" and wm["login"] == 123456789


def test_save_ticks_rejects_empty_frame(tmp_path):
    with pytest.raises(ValueError, match="空"):
        save_ticks(str(tmp_path), "XAUUSD", ticks_df([]))


# ---- store 公共别名（T2 追加） ----


def test_store_exposes_public_aliases():
    assert store.series_lock is store._SeriesLock
    assert store.atomic_replace is store._atomic_replace
    assert store.fsync_file is store._fsync_file


# ---- T4：tick 按月分区（R3） ----


def _midx_of(msc) -> int:
    return ticks_mod._month_index_from_msc(int(msc))


def _spy_read_month(monkeypatch, reads: list):
    """记录 _read_month 打开的月份（只读月份数据文件的探针）。"""
    orig = ticks_mod._read_month

    def spy(d, m, columns=None):
        reads.append(m)
        return orig(d, m, columns=columns)

    monkeypatch.setattr(ticks_mod, "_read_month", spy)


def test_backfill_spans_months_into_separate_files(tmp_path):
    """跨月回补：数据按服务器时间月份写进各自分区文件，meta.months 齐全。"""
    fake = FakeTicksMT5()
    start, end = datetime(2026, 8, 30, 12), datetime(2026, 9, 2, 12)
    rows = rows_from(start, 49, step_s=3600)  # 8/30 12:00 → 9/1 12:00（每小时）
    add_page(fake, start, end, rows)
    added = backfill_ticks(make_conn(fake), fake, str(tmp_path), "XAUUSD",
                           start=start, end=end)
    assert added == 49
    d = tmp_path / "ticks" / "XAUUSD"
    aug = pd.read_parquet(d / "2026" / "08.parquet")
    sep = pd.read_parquet(d / "2026" / "09.parquet")
    assert len(aug) + len(sep) == 49  # 行数守恒，一条不少
    m_aug = _midx_of(_ms(datetime(2026, 8, 15)))
    m_sep = _midx_of(_ms(datetime(2026, 9, 15)))
    assert {_midx_of(x) for x in aug["time_msc"]} == {m_aug}
    assert {_midx_of(x) for x in sep["time_msc"]} == {m_sep}
    wm = read_ticks_watermark(str(tmp_path), "XAUUSD")
    assert wm["months"] == ["2026-08", "2026-09"]
    assert wm["rows"] == 49
    assert wm["first_msc"] == _ms(start)
    assert wm["last_msc"] == _ms(datetime(2026, 9, 1, 12))
    df = load_ticks(str(tmp_path), "XAUUSD")
    assert len(df) == 49 and df["time_msc"].is_monotonic_increasing


def test_refresh_touches_only_tail_month_file(tmp_path, monkeypatch):
    """刷新只改写末月分区：更早月份文件 mtime 与内容一字节不动，也绝不被读。"""
    root = str(tmp_path)
    seed = rows_from(datetime(2026, 7, 1), 62 * 24, step_s=3600)  # 7/1 → 8/31 23:00
    save_ticks(root, "XAUUSD", ticks_df(seed), server="Exness-Trial", login=1)
    d = tmp_path / "ticks" / "XAUUSD"
    jul = d / "2026" / "07.parquet"
    old_stamp = time.time() - 86400
    os.utime(jul, (old_stamp, old_stamp))
    jul_bytes = jul.read_bytes()

    reads: list = []
    _spy_read_month(monkeypatch, reads)

    fake = FakeTicksMT5()
    last = seed[-1]
    page_rows = [last,
                 (_ms(datetime(2026, 8, 31, 23, 20)), 2100.0, 2100.5, 0, 0, 2),
                 (_ms(datetime(2026, 8, 31, 23, 40)), 2101.0, 2101.5, 0, 0, 2),
                 (_ms(datetime(2026, 9, 1, 0, 10)), 2102.0, 2102.5, 0, 0, 2)]
    add_page(fake, datetime(2026, 8, 31, 23), datetime(2026, 9, 1, 1), page_rows)
    added = refresh_ticks(make_conn(fake), fake, root, "XAUUSD",
                          now_server=datetime(2026, 9, 1, 0, 30))
    assert added == 3
    # 7 月分区一字节不动、mtime 不变
    assert jul.read_bytes() == jul_bytes
    assert os.path.getmtime(jul) == pytest.approx(old_stamp, abs=1e-3)
    # 只读了末月（2026-08）及新月分区，绝不读更早月份
    m_aug = _midx_of(_ms(datetime(2026, 8, 15)))
    assert reads and all(m >= m_aug for m in reads)
    # 跨入 9 月的新 tick 落在新月分区；总量守恒
    assert len(pd.read_parquet(d / "2026" / "09.parquet")) == 1
    assert len(load_ticks(root, "XAUUSD")) == len(seed) + 3
    wm = read_ticks_watermark(root, "XAUUSD")
    assert wm["months"] == ["2026-07", "2026-08", "2026-09"]
    assert wm["last_msc"] == _ms(datetime(2026, 9, 1, 0, 10))


def test_load_ticks_range_opens_only_needed_months(tmp_path, monkeypatch):
    """区间读取只打开涉及的月份文件，行级过滤 [start_msc, end_msc] 含两端。"""
    root = str(tmp_path)
    save_ticks(root, "XAUUSD", ticks_df(rows_from(datetime(2026, 7, 1), 62 * 24,
                                                  step_s=3600)))
    reads: list = []
    _spy_read_month(monkeypatch, reads)
    lo = _ms(datetime(2026, 8, 10, 6))
    hi = _ms(datetime(2026, 8, 20, 18))
    df = load_ticks(root, "XAUUSD", start_msc=lo, end_msc=hi)
    assert reads == [_midx_of(lo)]  # 只开了 2026-08 一个文件
    t = df["time_msc"].to_numpy()
    assert len(t) == 253  # 8/10 06:00 → 8/20 18:00 逐小时含两端
    assert t.min() == lo and t.max() == hi
    assert (t >= lo).all() and (t <= hi).all()
    assert list(df.columns) == TICK_COLUMNS


def test_load_ticks_range_edges(tmp_path):
    """库不存在 → None；区间不涉及任何月份 → 空 DataFrame（列齐）；start>end → 抛。"""
    root = str(tmp_path)
    assert load_ticks(root, "NOEXIST") is None
    save_ticks(root, "XAUUSD", ticks_df(rows_from(datetime(2026, 8, 1), 48,
                                                  step_s=3600)))
    empty = load_ticks(root, "XAUUSD", start_msc=_ms(datetime(2027, 1, 1)),
                       end_msc=_ms(datetime(2027, 2, 1)))
    assert empty is not None and len(empty) == 0
    assert list(empty.columns) == TICK_COLUMNS
    with pytest.raises(ValueError, match="不得晚于"):
        load_ticks(root, "XAUUSD", start_msc=200, end_msc=100)


def _seed_old_layout(tmp_path, rows, symbol="XAUUSD"):
    """手造旧单文件布局：{SYMBOL}/data.parquet + 旧格式 meta.json。"""
    d = tmp_path / "ticks" / symbol
    d.mkdir(parents=True)
    df = ticks_df(rows)
    df.to_parquet(d / "data.parquet", index=False)
    meta = {"symbol": symbol, "rows": len(df),
            "first_msc": int(df["time_msc"].iloc[0]),
            "last_msc": int(df["time_msc"].iloc[-1]),
            "updated_at_utc": "2026-10-01T00:00:00+00:00",
            "server": "Exness-Trial", "login": 42}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def test_old_layout_raises_with_migration_hint(tmp_path):
    """旧单文件布局：读取/写入/回补/刷新都拒绝并提示先跑迁移脚本。"""
    _seed_old_layout(tmp_path, rows_from(datetime(2026, 8, 1), 5))
    root, fake = str(tmp_path), FakeTicksMT5()
    with pytest.raises(TicksLayoutError, match="migrate_ticks_layout"):
        load_ticks(root, "XAUUSD")
    with pytest.raises(TicksLayoutError, match="migrate_ticks_layout"):
        save_ticks(root, "XAUUSD", ticks_df(rows_from(datetime(2026, 8, 1), 2)))
    with pytest.raises(TicksLayoutError, match="migrate_ticks_layout"):
        backfill_ticks(make_conn(fake), fake, root, "XAUUSD",
                       start=datetime(2026, 8, 1), end=datetime(2026, 8, 2))
    with pytest.raises(TicksLayoutError, match="migrate_ticks_layout"):
        refresh_ticks(make_conn(fake), fake, root, "XAUUSD",
                      now_server=datetime(2026, 8, 2))


def test_migrate_old_layout_conserves_rows_and_bounds(tmp_path):
    """迁移：总行数与首末 time_msc 守恒；旧文件改名保留；meta 换月列表。"""
    rows = rows_from(datetime(2026, 7, 20, 12), 20 * 24, step_s=3600)  # 7/20→8/8
    d = _seed_old_layout(tmp_path, rows)
    root = str(tmp_path)
    summary = migrate_old_layout(root, "XAUUSD")
    assert summary["rows"] == len(rows)
    assert summary["first_msc"] == rows[0][0] and summary["last_msc"] == rows[-1][0]
    assert summary["months"] == ["2026-07", "2026-08"]
    assert summary["server"] == "Exness-Trial" and summary["login"] == 42
    # 旧 data.parquet 已改名保留（不删数据）
    assert not (d / "data.parquet").exists()
    backups = [p.name for p in d.iterdir()
               if p.name.startswith("data.parquet.migrated-")]
    assert len(backups) == 1 and backups[0] == summary["backup"]
    # 分区内容 = 旧文件逐行一致
    df = load_ticks(root, "XAUUSD")
    assert list(df["time_msc"]) == [r[0] for r in rows]
    wm = read_ticks_watermark(root, "XAUUSD")
    assert wm["rows"] == len(rows) and wm["months"] == ["2026-07", "2026-08"]
    assert wm["server"] == "Exness-Trial" and wm["login"] == 42
    # 再跑一次：无旧布局可迁 → None（幂等跳过）
    assert migrate_old_layout(root, "XAUUSD") is None


# ---- T28（M8）：columns 只读指定列；tick_watermark 只碰元数据 ----


def test_load_ticks_columns_reads_only_requested_columns(tmp_path, monkeypatch):
    """columns=["time_msc","bid","ask"]：parquet 只开三列（构建点差模型不必载
    last/volume/flags），返回列序按 TICK_COLUMNS 规范序、dtype 与全列读取一致、
    值与全列读取的子集逐位一致。"""
    root = str(tmp_path)
    save_ticks(root, "XAUUSD", ticks_df(rows_from(datetime(2026, 7, 1), 72,
                                                  step_s=3600)))
    seen: list = []
    real_read = pd.read_parquet

    def spy(path, columns=None, **kw):
        seen.append(columns)
        return real_read(path, columns=columns, **kw)

    monkeypatch.setattr(ticks_mod.pd, "read_parquet", spy)
    df3 = load_ticks(root, "XAUUSD", columns=["time_msc", "bid", "ask"])
    monkeypatch.setattr(ticks_mod.pd, "read_parquet", real_read)
    # 打开的每个月分区都只读了三列（不是全列）
    assert seen and all(c is not None and set(c) == {"time_msc", "bid", "ask"}
                        for c in seen)
    assert list(df3.columns) == ["time_msc", "bid", "ask"]
    full = load_ticks(root, "XAUUSD")
    for c in ("time_msc", "bid", "ask"):
        assert str(df3[c].dtype) == str(full[c].dtype)
        assert list(df3[c]) == list(full[c])


def test_load_ticks_columns_normalizes_order_and_validates(tmp_path):
    """乱序传入按规范序返回；columns 不含 time_msc 时行级过滤仍准确；
    非法列名/重复列 → ValueError。"""
    root = str(tmp_path)
    rows = rows_from(datetime(2026, 8, 1), 48, step_s=3600)
    save_ticks(root, "XAUUSD", ticks_df(rows))
    lo, hi = _ms(datetime(2026, 8, 1, 5)), _ms(datetime(2026, 8, 1, 9))

    rev = load_ticks(root, "XAUUSD", columns=["ask", "time_msc"])
    assert list(rev.columns) == ["time_msc", "ask"]   # TICK_COLUMNS 规范序

    bid_only = load_ticks(root, "XAUUSD", start_msc=lo, end_msc=hi,
                          columns=["bid"])
    assert list(bid_only.columns) == ["bid"]
    assert list(bid_only["bid"]) == [r[1] for r in rows[5:10]]  # 过滤仍按 time_msc

    with pytest.raises(ValueError, match="非法列"):
        load_ticks(root, "XAUUSD", columns=["time_msc", "mid"])
    with pytest.raises(ValueError, match="重复"):
        load_ticks(root, "XAUUSD", columns=["bid", "bid"])
    with pytest.raises(ValueError, match="不能为空"):
        load_ticks(root, "XAUUSD", columns=[])


def test_load_ticks_columns_empty_result_keeps_schema(tmp_path):
    """区间不涉及任何月份时返回空表：列与 dtype 与请求一致（不再 object 占位）。"""
    root = str(tmp_path)
    save_ticks(root, "XAUUSD", ticks_df(rows_from(datetime(2026, 8, 1), 5)))
    empty = load_ticks(root, "XAUUSD", start_msc=_ms(datetime(2027, 1, 1)),
                       end_msc=_ms(datetime(2027, 2, 1)),
                       columns=["ask", "bid", "time_msc"])
    assert empty is not None and len(empty) == 0
    assert list(empty.columns) == ["time_msc", "bid", "ask"]
    assert str(empty["time_msc"].dtype) == "int64"
    assert str(empty["bid"].dtype) == "float64"
    assert str(empty["ask"].dtype) == "float64"


def test_tick_watermark_reads_metadata_only_and_ignores_stale_meta(tmp_path):
    """tick_watermark：{rows, first_msc, last_msc} 与磁盘月分区一致；无库 → None；
    meta.json 被改陈旧也不信（以分区元数据重算，供成本缓存做水位键）。"""
    from mt5data.ticks import tick_watermark

    root = str(tmp_path)
    assert tick_watermark(root, "NOEXIST") is None
    rows = rows_from(datetime(2026, 7, 1), 10, step_s=3600)
    save_ticks(root, "XAUUSD", ticks_df(rows))
    wm = tick_watermark(root, "XAUUSD")
    assert wm == {"rows": 10, "first_msc": rows[0][0], "last_msc": rows[-1][0]}
    # 手改 meta.json 模拟异常中断留下的陈旧水位 → 仍按磁盘分区重算
    d = ticks_dir(root, "XAUUSD")
    stale = dict(read_ticks_watermark(root, "XAUUSD"))
    stale["rows"] = 999999
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(stale, f)
    assert tick_watermark(root, "XAUUSD")["rows"] == 10


def test_fetch_window_direct_structured_array_conversion():
    """_fetch_window 对结构化数组直转 DataFrame（不再 tolist），多余字段裁掉。"""
    fields = [(c, ticks_mod._TICK_DTYPES[c]) for c in TICK_COLUMNS] \
        + [("volume_real", "f8")]  # 贴真包防御：dtype 可能多出 volume_real
    arr = np.array([(_ms(datetime(2026, 8, 1)), 1.0, 1.1, 0.0, 0.0, 2, 0.0)],
                   dtype=np.dtype(fields))

    class _Mod:  # 贴真包最小面：结构化数组直出
        COPY_TICKS_ALL = -1

        @staticmethod
        def copy_ticks_range(symbol, from_s, to_s, flags):
            return arr

    class _Conn:
        @staticmethod
        def call(fn, *a, **k):
            return fn(*a, **k)

        @staticmethod
        def last_error():
            return (1, "Success")

    df = ticks_mod._fetch_window(_Conn, _Mod, "XAUUSD",
                                 _sec(datetime(2026, 8, 1)),
                                 _sec(datetime(2026, 8, 2)))
    assert list(df.columns) == TICK_COLUMNS  # volume_real 被列序化裁掉
    assert str(df["time_msc"].dtype) == "int64"
    assert str(df["flags"].dtype) == "int64"
    assert list(df["time_msc"]) == [_ms(datetime(2026, 8, 1))]
