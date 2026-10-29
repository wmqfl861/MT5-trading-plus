"""T4 序列清单测试：扫描全部 bars 序列 + 坏 meta 进 warnings + 顺序稳定。

refresh_all / export_lockbox 以 series_inventory 为唯一事实来源，
(symbol, timeframe) 升序保证每日调度计划可复现。
"""
import pandas as pd

from mt5data.schedule import SeriesRef, series_inventory
from mt5data.store import save_bars


def save_series(root, symbol, tf, n=3):
    df = pd.DataFrame({
        "time": pd.to_datetime([f"2026-01-0{1 + i // 24}T{i % 24:02d}:00:00"
                                for i in range(n)]),
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })
    save_bars(root, symbol, tf, df)


def test_inventory_scans_all_series_sorted(tmp_path):
    save_series(tmp_path, "EURUSD", "M1")
    save_series(tmp_path, "XAUUSD", "M1")
    save_series(tmp_path, "XAUUSD", "H1")
    save_series(tmp_path, "BTCUSD", "D1")
    inv, warnings = series_inventory(tmp_path)
    assert [(r.symbol, r.timeframe) for r in inv] == [
        ("BTCUSD", "D1"), ("EURUSD", "M1"), ("XAUUSD", "H1"), ("XAUUSD", "M1"),
    ]
    assert warnings == []
    r = inv[2]
    assert isinstance(r, SeriesRef)
    assert r.watermark["symbol"] == "XAUUSD" and r.watermark["rows"] == 3
    assert r.watermark["first_time"].startswith("2026-01-01T00:00")


def test_inventory_order_stable_across_calls(tmp_path):
    # refresh_all 的计划顺序必须可复现：与目录创建顺序、调用次数都无关
    save_series(tmp_path, "XAUUSD", "M1")
    save_series(tmp_path, "AAAUSD", "H1")
    first = [(r.symbol, r.timeframe) for r in series_inventory(tmp_path)[0]]
    second = [(r.symbol, r.timeframe) for r in series_inventory(tmp_path)[0]]
    assert first == second == [("AAAUSD", "H1"), ("XAUUSD", "M1")]


def test_inventory_bad_meta_goes_to_warnings(tmp_path):
    save_series(tmp_path, "EURUSD", "M1")
    bad_json = tmp_path / "bars" / "ZZBAD" / "H1"
    bad_json.mkdir(parents=True)
    (bad_json / "meta.json").write_text("{ 不是 JSON", encoding="utf-8")
    no_key = tmp_path / "bars" / "YYBAD" / "M5"
    no_key.mkdir(parents=True)
    (no_key / "meta.json").write_text('{"rows": 3}', encoding="utf-8")

    inv, warnings = series_inventory(tmp_path)
    assert [(r.symbol, r.timeframe) for r in inv] == [("EURUSD", "M1")]
    assert len(warnings) == 2
    assert any("ZZBAD" in w for w in warnings)
    assert any("YYBAD" in w for w in warnings)


def test_inventory_missing_bars_dir_returns_empty(tmp_path):
    inv, warnings = series_inventory(tmp_path / "nope")
    assert inv == [] and warnings == []


# ---------------------------------------------------------------- H-01 真实文件枚举


def test_bars_parquet_paths_enumerates_real_files(tmp_path):
    """【H-01 失败测试】枚举真实 data.parquet（不信 meta 清单）：
    orphan（有数据无 meta）必须可见——series_inventory 完全看不见它。"""
    from mt5data.schedule import bars_parquet_paths

    save_series(tmp_path, "XAUUSD", "H1")
    orphan = tmp_path / "bars" / "EURUSD" / "M1"
    orphan.mkdir(parents=True)
    pd.DataFrame({
        "time": pd.to_datetime(["2026-06-01T00:00:00", "2026-06-01T01:00:00"]),
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    }).to_parquet(orphan / "data.parquet")

    found = bars_parquet_paths(str(tmp_path))
    assert [(s, t) for s, t, _ in found] == [("EURUSD", "M1"), ("XAUUSD", "H1")]
    # orphan 在 meta 清单里不可见（对照：旧自检只遍历清单）
    inv, _ = series_inventory(tmp_path)
    assert ("EURUSD", "M1") not in [(r.symbol, r.timeframe) for r in inv]


def test_bars_parquet_paths_empty_root(tmp_path):
    from mt5data.schedule import bars_parquet_paths

    assert bars_parquet_paths(str(tmp_path / "nope")) == []
