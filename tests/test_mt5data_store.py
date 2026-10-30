from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from mt5data.store import (
    TIMEFRAME_SECONDS,
    drop_unclosed,
    load_bars,
    merge_bars,
    read_watermark,
    save_bars,
)


def bars_df(times, closes):
    return pd.DataFrame({
        "time": pd.to_datetime([datetime(2023, 10, 22) + timedelta(minutes=t) for t in times]),
        "open": 1.0, "high": 1.0, "low": 1.0,
        "close": closes, "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


def test_timeframe_seconds_cover_all_21():
    assert len(TIMEFRAME_SECONDS) == 21
    assert TIMEFRAME_SECONDS["M1"] == 60
    assert TIMEFRAME_SECONDS["H1"] == 3600
    assert TIMEFRAME_SECONDS["D1"] == 86400
    assert TIMEFRAME_SECONDS["W1"] == 7 * 86400
    assert TIMEFRAME_SECONDS["MN1"] == 31 * 86400


def test_merge_bars_dedups_by_time_keeping_newer_and_sorts():
    old = bars_df([0, 1, 2], [1.0, 1.0, 1.0])
    new = bars_df([1, 2, 3], [2.0, 2.0, 2.0])
    merged = merge_bars(old, new)
    assert list(merged["close"]) == [1.0, 2.0, 2.0, 2.0]
    assert merged["time"].is_monotonic_increasing


def test_merge_bars_empty_old():
    merged = merge_bars(None, bars_df([0, 1], [1.0, 1.0]))
    assert len(merged) == 2


def test_drop_unclosed_removes_forming_last_bar():
    t0 = datetime(2023, 10, 22, 12, 0)
    df = bars_df([0, 1], [1.0, 2.0])
    df["time"] = pd.to_datetime([t0, t0 + timedelta(minutes=1)])
    now = t0 + timedelta(minutes=1, seconds=30)  # 第二根还在形成
    kept = drop_unclosed(df, "M1", now)
    assert len(kept) == 1 and kept.iloc[-1]["close"] == 1.0


def test_drop_unclosed_keeps_bar_closed_exactly_at_now():
    t0 = datetime(2023, 10, 22, 12, 0)
    df = bars_df([0, 1], [1.0, 2.0])
    df["time"] = pd.to_datetime([t0, t0 + timedelta(minutes=1)])
    now = t0 + timedelta(minutes=2)  # 恰在收盘时刻 → 已收盘
    kept = drop_unclosed(df, "M1", now)
    assert len(kept) == 2


def test_drop_unclosed_no_slack_at_all():  # I7：宽容秒数已废除
    t0 = datetime(2023, 10, 22, 12, 0)
    df = bars_df([0, 1], [1.0, 2.0])
    df["time"] = pd.to_datetime([t0, t0 + timedelta(minutes=1)])
    now = t0 + timedelta(minutes=2) - timedelta(seconds=1)  # 差 1 秒未收盘
    kept = drop_unclosed(df, "M1", now)
    assert len(kept) == 1  # 最后一根仍算未收盘，绝不宽容


def test_save_load_roundtrip_watermark_and_atomic(tmp_path):
    df = bars_df([0, 1, 2], [1.0, 2.0, 3.0])
    save_bars(tmp_path, "EURUSD", "M1", df, server="Exness-Trial", login=123456789)
    loaded = load_bars(tmp_path, "EURUSD", "M1")
    assert list(loaded["close"]) == [1.0, 2.0, 3.0]
    assert str(loaded["time"].dtype).startswith("datetime64")

    wm = read_watermark(tmp_path, "EURUSD", "M1")
    assert wm["symbol"] == "EURUSD" and wm["timeframe"] == "M1"
    assert wm["rows"] == 3
    assert wm["first_time"] == "2023-10-22T00:00:00"
    assert wm["last_closed_time"] == "2023-10-22T00:02:00"
    assert "updated_at_utc" in wm  # M2：明确标注为 UTC 墙钟
    assert wm["server"] == "Exness-Trial" and wm["login"] == 123456789  # M11：数据来源可追溯
    # 原子写不留临时文件（I8）
    leftovers = [p.name for p in (tmp_path / "bars" / "EURUSD" / "M1").iterdir()
                 if p.name.endswith(".tmp")]
    assert leftovers == []


def test_load_missing_returns_none(tmp_path):
    assert load_bars(tmp_path, "XXX", "M1") is None
    assert read_watermark(tmp_path, "XXX", "M1") is None


# ------------------------------------------------ H-11：写锁覆盖读改写 + 审计元数据保留

def test_save_bars_preserves_registered_known_gaps(tmp_path):
    """H-11 失败测试：save_bars 重建 meta 时保留 known_gaps（审计登记不是水位）。

    场景：先落库 → quality.write_known_gaps 登记缺口 → 再 save_bars（刷新
    落库的等价路径，不带任何 known_gaps 参数）。修复前 meta 只继承
    server/login，登记被静默清掉。
    """
    from mt5data.quality import write_known_gaps

    save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1], [1.0, 2.0]),
              server="Exness-Trial", login=123456789)
    write_known_gaps(str(tmp_path), "EURUSD", "M1", [
        {"start": "2021-05-17T14:00:00", "end": "2021-06-14T11:00:00",
         "reason": "终端无该段历史"}])

    save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1, 2], [1.0, 2.0, 3.0]))

    wm = read_watermark(tmp_path, "EURUSD", "M1")
    assert wm["rows"] == 3  # 水位确实更新为最新数据
    assert wm["server"] == "Exness-Trial" and wm["login"] == 123456789
    gaps = wm.get("known_gaps")
    assert gaps, "刷新后 known_gaps 丢失（H-11：审计登记必须保留）"
    assert gaps[0]["start"] == "2021-05-17T14:00:00"
    assert gaps[0]["end"] == "2021-06-14T11:00:00"
    assert gaps[0]["reason"] == "终端无该段历史"
    assert "registered_utc" in gaps[0]  # 登记时间等审计字段完整保留


def test_save_bars_tmp_paths_unique_per_write(tmp_path, monkeypatch):
    """H-11 失败测试：两次保存的临时文件各用各的名（固定 .tmp 会被第二写者截断）。"""
    from mt5data import store

    recorded = []
    real_replace = store._atomic_replace

    def rec_replace(tmp, final):
        recorded.append(tmp)
        return real_replace(tmp, final)

    monkeypatch.setattr(store, "_atomic_replace", rec_replace)

    save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1], [1.0, 2.0]))
    save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1, 2], [1.0, 2.0, 3.0]))

    # 两次写 × (parquet tmp + meta tmp) = 4 个临时文件名，两两互不相同
    assert len(recorded) == 4
    assert len(set(recorded)) == 4, f"临时文件名复用：{recorded}"
    assert all(p.endswith(".tmp") for p in recorded)


def test_load_bars_checked_rejects_torn_publish(tmp_path, monkeypatch):
    """H-11 失败测试：parquet/meta 发布之间失败 → 读者要么一致版本要么明确拒绝。

    data.parquet 与 meta.json 分两次原子发布；在两者之间注入失败后，
    库里是"新 parquet + 旧水位"的撕裂快照——load_bars_checked 必须明确
    拒绝（StoreConsistencyError），不得把自相矛盾的版本当正常数据返回。
    正常再保存一次即可自愈。
    """
    from mt5data import store
    from mt5data.store import StoreConsistencyError, load_bars_checked

    save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1, 2], [1.0, 2.0, 3.0]))
    # 一致状态：校验读取通过，与 load_bars 同值
    assert len(load_bars_checked(tmp_path, "EURUSD", "M1")) == 3

    real_replace = store._atomic_replace
    calls = {"n": 0}

    def boom(tmp, final):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 2 次 _atomic_replace = meta.json 发布
            raise OSError("注入：parquet 已发布、meta 尚未发布时崩溃")
        return real_replace(tmp, final)

    monkeypatch.setattr(store, "_atomic_replace", boom)
    with pytest.raises(OSError, match="meta 尚未发布"):
        save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1, 2, 3], [1.0, 2.0, 3.0, 4.0]))
    monkeypatch.undo()

    # 撕裂快照：数据 4 根、水位 3 根 → 明确拒绝
    assert len(load_bars(tmp_path, "EURUSD", "M1")) == 4
    with pytest.raises(StoreConsistencyError, match="不是同一份快照"):
        load_bars_checked(tmp_path, "EURUSD", "M1")
    # 正常保存一次即自愈
    save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1, 2, 3], [1.0, 2.0, 3.0, 4.0]))
    assert len(load_bars_checked(tmp_path, "EURUSD", "M1")) == 4


# ------------------------------------------------ X-04：同代发布与内容指纹


def test_save_bars_publishes_generation_and_content_fingerprint(tmp_path):
    """【X-04 失败测试】meta 记录发布代数与内容指纹：generation 每次发布 +1；
    content_sha256 是 data.parquet 的字节指纹，与真实文件一致、两代不同。"""
    import hashlib

    from mt5data.store import read_watermark as _rw

    save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1], [1.0, 2.0]))
    wm1 = _rw(tmp_path, "EURUSD", "M1")
    assert wm1["generation"] == 1
    fp1 = wm1["content_sha256"]
    assert len(fp1) == 64
    raw = (Path(tmp_path) / "bars" / "EURUSD" / "M1"
           / "data.parquet").read_bytes()
    assert fp1 == hashlib.sha256(raw).hexdigest()

    save_bars(tmp_path, "EURUSD", "M1", bars_df([0, 1, 2], [1.0, 2.0, 3.0]))
    wm2 = _rw(tmp_path, "EURUSD", "M1")
    assert wm2["generation"] == 2
    assert wm2["content_sha256"] != fp1


def test_checked_reader_rejects_middle_only_price_change(tmp_path):
    """【X-04 失败测试】行数/首尾完全不变、仅中间 OHLC 被改写：旧的
    条数/首尾核对分辨不出，内容指纹必须拒绝。"""
    from mt5data.store import StoreConsistencyError, load_bars_checked

    df = bars_df([0, 1, 2], [1.0, 2.0, 3.0])
    save_bars(tmp_path, "EURUSD", "M1", df)
    tampered = df.copy()
    tampered.loc[1, ["open", "high", "low", "close"]] = [9.0, 9.5, 8.5, 9.0]
    tampered.to_parquet(
        Path(tmp_path) / "bars" / "EURUSD" / "M1" / "data.parquet",
        index=False)
    with pytest.raises(StoreConsistencyError, match="内容指纹"):
        load_bars_checked(tmp_path, "EURUSD", "M1")


def test_checked_reader_rejects_torn_publish_with_same_shape(tmp_path,
                                                             monkeypatch):
    """【X-04 失败测试】两步发布间失败且新旧快照行数/首尾相同（只改首行
    OHLC）：撕裂配读（新 parquet + 旧 meta）必须被内容指纹拒绝，
    不得把半新半旧的混版当正常数据返回；重存一次自愈。"""
    from mt5data import store
    from mt5data.store import StoreConsistencyError, load_bars_checked

    old = bars_df([0, 1, 2], [100.0, 100.0, 100.0])
    save_bars(tmp_path, "EURUSD", "M1", old)
    new = old.copy()
    new.loc[0, ["open", "high", "low", "close"]] = [102.0, 102.5, 101.5, 102.0]

    real_replace = store._atomic_replace

    def boom(tmp, final):
        if Path(final).name == "meta.json":
            raise OSError("注入：parquet 已发布、meta 尚未发布时崩溃")
        return real_replace(tmp, final)

    monkeypatch.setattr(store, "_atomic_replace", boom)
    with pytest.raises(OSError):
        save_bars(tmp_path, "EURUSD", "M1", new)
    monkeypatch.undo()

    with pytest.raises(StoreConsistencyError):
        load_bars_checked(tmp_path, "EURUSD", "M1")
    save_bars(tmp_path, "EURUSD", "M1", new)
    assert load_bars_checked(tmp_path, "EURUSD", "M1").iloc[0]["close"] == 102.0


def test_concurrent_publish_and_read_never_tears(tmp_path):
    """【X-04 失败测试】并发发布/读取：发布协议是两步（先 parquet 后
    meta），读者必须拿到同代一致数据——行数与内容来自同一代发布，
    绝不因踩进发布窗口而被撕裂拒绝或配到混版。"""
    import threading

    from mt5data.store import load_bars_checked, save_bars

    base = bars_df([0, 1, 2, 3], [1.0, 1.0, 1.0, 1.0])
    save_bars(tmp_path, "EURUSD", "M1", base)
    pairs = {(4, 1.0)}          # (行数, close)：每代发布行数与内容同变
    for i in range(10):
        n = 4 + (i % 3)
        pairs.add((n, float(i + 2)))

    errors: list[Exception] = []
    seen: list[tuple[int, float]] = []
    stop = threading.Event()

    def writer():
        try:
            for i in range(10):
                n = 4 + (i % 3)
                save_bars(tmp_path, "EURUSD", "M1",
                          bars_df(list(range(n)), [float(i + 2)] * n))
        except Exception as e:              # pragma: no cover - 失败即失败
            errors.append(e)
        finally:
            stop.set()

    def reader():
        while not stop.is_set():
            try:
                df = load_bars_checked(tmp_path, "EURUSD", "M1")
            except Exception as e:
                errors.append(e)
                stop.set()
                return
            closes = set(float(c) for c in df["close"])
            if len(closes) != 1:            # 同代内容必须自洽
                errors.append(AssertionError(f"混版读取：close={closes}"))
                stop.set()
                return
            seen.append((len(df), closes.pop()))

    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    wt.start()
    rt.start()
    wt.join(timeout=60)
    rt.join(timeout=60)
    assert errors == []
    assert seen                         # 确实读到过
    assert set(seen) <= pairs           # 每次读取都是一次真实发布的同代数据
