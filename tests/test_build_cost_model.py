"""scripts/build_cost_model.py（T28/M8）：点差模型缓存构建脚本测试。

缓存文件 {data-root}/costs/spread_model.json：各品种 tick 水位（只读元数据）
+ 模型 sha256 + 模型负载；读取方（run_backtest.build_costs）据此免载原始
tick 并做水位/sha/口径四重校验。
"""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_cost_model.py"
RUN_BT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_backtest.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_ticks(root, symbol, hours):
    """每小时 1 条 tick 的最小库（覆盖给定纽约小时集合）。"""
    import numpy as np
    import pandas as pd
    from mt5data.ticks import save_ticks

    save_ticks(str(root), symbol, pd.DataFrame({
        "time_msc": np.asarray(
            [pd.Timestamp(f"2026-10-19 {h:02d}:00:00").value // 10**6
             for h in hours], dtype="int64"),
        "bid": [2400.0] * len(hours),
        "ask": [2400.3] * len(hours),
        "last": [2400.15] * len(hours),
        "volume": [0.0] * len(hours),
        "flags": [2] * len(hours),
    }))


def test_build_writes_watermark_sha_and_model(tmp_path):
    """两品种建缓存：水位与 tick_watermark 逐项一致；sha 自洽；
    run_backtest.load_spread_model_cache 能直接命中且统计与直建一致。"""
    from mt5data.ticks import tick_watermark
    from mt5quant.costs import SpreadModel

    mod = _load("build_cost_model_t28", SCRIPT_PATH)
    rb = _load("run_backtest_t28", RUN_BT_PATH)
    root = tmp_path / "data"
    _seed_ticks(root, "XAUUSD", range(24))
    _seed_ticks(root, "EURUSD", range(12))

    doc = mod.build_cost_models(str(root), ["XAUUSD", "EURUSD"])
    p = root / "costs" / "spread_model.json"
    assert p.exists()
    assert doc["watermarks"]["XAUUSD"] == tick_watermark(str(root), "XAUUSD")
    assert doc["watermarks"]["EURUSD"] == tick_watermark(str(root), "EURUSD")
    # sha 自洽（规范 JSON 哈希）
    assert (rb.canonical_json_sha256(doc["model"])
            == doc["model_sha256"])
    # 读取方能命中，统计与从 tick 直建逐位一致
    for sym in ("XAUUSD", "EURUSD"):
        model = rb.load_spread_model_cache(
            str(p), sym, spread_mode="relative",
            watermark=tick_watermark(str(root), sym))
        direct = SpreadModel.from_ticks(
            sym, rb.load_ticks(str(root), sym,
                               columns=["time_msc", "bid", "ask"]))
        assert set(model.symbols) == {"XAUUSD", "EURUSD"}  # 多品种合并缓存
        for h in direct._per_symbol[sym]:      # noqa: SLF001 — 对照内部表
            assert model.stat(h, symbol=sym) == direct.stat(h, symbol=sym)


def test_build_reads_only_three_tick_columns(tmp_path, monkeypatch):
    """构建过程只读 time_msc/bid/ask 三列（M8：不载 last/volume/flags）。"""
    import pandas as pd

    mod = _load("build_cost_model_t28b", SCRIPT_PATH)
    root = tmp_path / "data"
    _seed_ticks(root, "XAUUSD", range(24))
    seen = []
    real_read = pd.read_parquet

    def spy(path, columns=None, **kw):
        seen.append(columns)
        return real_read(path, columns=columns, **kw)

    monkeypatch.setattr(pd, "read_parquet", spy)
    mod.build_cost_models(str(root), ["XAUUSD"])
    assert seen and all(c is not None and set(c) == {"time_msc", "bid", "ask"}
                        for c in seen)


def test_build_defaults_to_all_symbols_and_rejects_missing(tmp_path):
    """symbols=None → tick 库下全部品种；无 tick 的品种报错指路 download_ticks。"""
    mod = _load("build_cost_model_t28c", SCRIPT_PATH)
    root = tmp_path / "data"
    _seed_ticks(root, "XAUUSD", range(24))
    _seed_ticks(root, "BTCUSD", range(24))
    doc = mod.build_cost_models(str(root), None)
    assert set(doc["watermarks"]) == {"XAUUSD", "BTCUSD"}

    with pytest.raises(ValueError, match="download_ticks"):
        mod.build_cost_models(str(root), ["NOEXIST"])


def test_build_rebuild_overwrites_stale_watermark(tmp_path):
    """幂等重建：tick 增量后重跑 → 新水位落盘，读取方不再报水位不一致。"""
    import numpy as np
    import pandas as pd
    from mt5data.ticks import save_ticks, tick_watermark

    mod = _load("build_cost_model_t28d", SCRIPT_PATH)
    rb = _load("run_backtest_t28d", RUN_BT_PATH)
    root = tmp_path / "data"
    _seed_ticks(root, "XAUUSD", range(24))
    mod.build_cost_models(str(root), ["XAUUSD"])
    save_ticks(str(root), "XAUUSD", pd.DataFrame({
        "time_msc": np.asarray(
            [pd.Timestamp("2026-10-21 05:00:00").value // 10**6], dtype="int64"),
        "bid": [2400.0], "ask": [2400.4], "last": [2400.2],
        "volume": [0.0], "flags": [2],
    }))
    # 水位变了：旧缓存被 build_costs 拒绝；重建后恢复可用
    with pytest.raises(ValueError, match="水位"):
        rb.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True)
    mod.build_cost_models(str(root), ["XAUUSD"])
    doc = json.loads((root / "costs" / "spread_model.json")
                     .read_text(encoding="utf-8"))
    assert doc["watermarks"]["XAUUSD"] == tick_watermark(str(root), "XAUUSD")
    rb.build_costs(str(root), "XAUUSD", 0.1, allow_zero_swap=True)  # 不再抛


def test_cli_main_writes_cache(tmp_path, capsys, monkeypatch):
    mod = _load("build_cost_model_t28e", SCRIPT_PATH)
    root = tmp_path / "data"
    _seed_ticks(root, "XAUUSD", range(24))
    monkeypatch.setattr("sys.argv", [
        "build_cost_model.py", "--data-root", str(root),
        "--symbols", "XAUUSD"])
    assert mod.main() == 0
    assert (root / "costs" / "spread_model.json").exists()
    assert "spread_model.json" in capsys.readouterr().out
