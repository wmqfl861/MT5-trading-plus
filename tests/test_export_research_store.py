"""T27/H3：研究库导出（scripts/export_research_store.py）。

契约：
- 只导出 time < 全局 cutoff 的 K 线到 data_research/bars/**（同布局），
  meta 追加来源信息（source_root / source_cutoff / data_sha256 /
  exported_at_utc）；
- 成本模型缓存 data/costs/spread_model.json 复制到 data_research/costs/
  （tick **不**导出：tick 库对研究账户拒读，研究侧成本经缓存文件提供，
  缓存新鲜度在导出时对照主库 tick 水位核验）；specs/latest.json 必须有
  （合约大小与手数上下限），calibrated.json / server_clock.json 有则复制；
- lockbox.json **不**复制：研究库没有锁箱段，research_bars 在研究库上
  行为与直读一致（锁箱语义自然满足）；
- 导出后自检：研究库里任何序列 last_time 都必须早于 cutoff，越界即
  失败退出（--verify-only 可单独跑自检）。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "export_research_store.py"

CUTOFF = pd.Timestamp("2026-06-01 00:00")


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def bars_around(n_pre=10, n_post=6):
    """cutoff 前后各若干根 H1。"""
    times = list(pd.date_range("2026-05-31 10:00", periods=n_pre, freq="h")) \
        + list(pd.date_range("2026-06-01 00:00", periods=n_post, freq="h"))
    return pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": 100.0, "tick_volume": 10, "spread": 5, "real_volume": 0})


def full_hour_ticks(sym="XAUUSD"):
    """覆盖纽约全 24 小时的 tick（每小时 2 条）——可建点差缓存。"""
    import numpy as np
    return pd.DataFrame({
        "time_msc": np.asarray(
            [pd.Timestamp(f"2026-10-{19 + h // 24} {h % 24:02d}:00:00")
             .value // 10**6
             for h in range(24) for _ in range(2)], dtype="int64"),
        "bid": [2400.0] * 48, "ask": [2400.3] * 48,
        "last": [2400.15] * 48, "volume": [0.0] * 48, "flags": [2] * 48,
    })


@pytest.fixture
def main_store(tmp_path):
    """可用主库：bars（跨 cutoff）+ lockbox.json + tick + 点差缓存 + specs。"""
    from mt5data.lockbox import write_global_cutoff
    from mt5data.store import save_bars
    from mt5data.ticks import save_ticks
    from mt5quant.costs import SpreadModel

    rb = load_script("run_backtest_script_x", REPO_ROOT / "scripts"
                     / "run_backtest.py")
    root = tmp_path / "data"
    save_bars(str(root), "XAUUSD", "H1", bars_around())
    write_global_cutoff(str(root), CUTOFF, note="测试锁箱")
    save_ticks(str(root), "XAUUSD", full_hour_ticks())
    ticks = rb.load_ticks(str(root), "XAUUSD",
                          columns=["time_msc", "bid", "ask"])
    model = SpreadModel.from_ticks("XAUUSD", ticks, mode="relative")
    from mt5data.ticks import tick_watermark
    rb.save_spread_model_cache(
        rb.spread_model_cache_path(str(root)), model,
        {"XAUUSD": tick_watermark(str(root), "XAUUSD")})
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(json.dumps(
        {"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                    "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
    (root / "server_clock.json").write_text("{}", encoding="utf-8")
    return root


# ---------------------------------------------------------------- 导出内容


def test_export_writes_open_segment_with_provenance_meta(tmp_path, main_store):
    """只导出 time < cutoff 的行；meta 带来源信息（data_sha256 与导出段
    逐位一致——研究侧 run 的 data_sha256 与主库研究窗相同，谱系连续）。"""
    from mt5data.store import load_bars, read_watermark
    from mt5quant.backtest.report import data_sha256

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0

    seg = load_bars(str(dest), "XAUUSD", "H1")
    assert seg is not None and len(seg) == 10
    assert seg["time"].max() < CUTOFF
    meta = read_watermark(str(dest), "XAUUSD", "H1")
    assert meta["rows"] == 10
    assert meta["source_cutoff"] == CUTOFF.isoformat()
    assert meta["exported_at_utc"]
    assert meta["data_sha256"] == data_sha256(seg)
    assert "data" in meta["source_root"].replace("\\", "/")


def test_export_copies_costs_cache_and_specs_not_ticks_or_lockbox(
        tmp_path, main_store):
    """costs/spread_model.json 与 specs 复制；tick 目录与 lockbox.json
    不出现在研究库（tick 对研究拒读；研究库无锁箱段）。"""
    from mt5data.lockbox import research_bars

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0
    assert (dest / "costs" / "spread_model.json").is_file()
    src_cache = json.loads((main_store / "costs" / "spread_model.json")
                           .read_text(encoding="utf-8"))
    dst_cache = json.loads((dest / "costs" / "spread_model.json")
                           .read_text(encoding="utf-8"))
    assert dst_cache == src_cache
    assert (dest / "specs" / "latest.json").is_file()
    assert not (dest / "ticks").exists()
    assert not (dest / "lockbox.json").exists()
    # 研究库上 research_bars = 直读（无锁箱段：end 越过原 cutoff 也不报错）
    df = research_bars(str(dest), "XAUUSD", "H1",
                       end=pd.Timestamp("2030-01-01"))
    assert len(df) == 10


# ---------------------------------------------------------------- 导出失败


def test_export_fails_without_lockbox_config(tmp_path, capsys):
    from mt5data.store import save_bars

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    root = tmp_path / "data"
    save_bars(str(root), "XAUUSD", "H1", bars_around())
    assert mod.main(["--data-root", str(root), "--dest",
                     str(tmp_path / "r")]) == 1
    assert "锁箱" in capsys.readouterr().out


def test_export_fails_on_stale_spread_model(tmp_path, main_store, capsys):
    """主库 tick 水位已变化而缓存未重建 → 拒绝导出（研究侧无法自验，
    新鲜度必须在导出时把关），提示重建。"""
    from mt5data.ticks import save_ticks

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    save_ticks(str(main_store), "XAUUSD",
               full_hour_ticks().assign(
                   time_msc=full_hour_ticks()["time_msc"] + 86_400_000))
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(tmp_path / "r")]) == 1
    out = capsys.readouterr().out
    assert "水位" in out and "build_cost_model" in out


def test_export_requires_spread_cache(tmp_path, capsys):
    """缺点差缓存 → 拒绝导出（研究侧成本无来源、又不许读 tick 自建）。"""
    from mt5data.lockbox import write_global_cutoff
    from mt5data.store import save_bars

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    root = tmp_path / "data"
    save_bars(str(root), "XAUUSD", "H1", bars_around())
    write_global_cutoff(str(root), CUTOFF, note="t")
    assert mod.main(["--data-root", str(root), "--dest",
                     str(tmp_path / "r")]) == 1
    assert "spread_model" in capsys.readouterr().out


def test_export_fails_when_series_missing_cutoff(tmp_path, capsys):
    """v1 序列键配置下有未锁序列 → 该序列拒绝导出（研究库不得含未锁数据）。"""
    from mt5data.lockbox import write_config
    from mt5data.store import save_bars

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    root = tmp_path / "data"
    save_bars(str(root), "XAUUSD", "H1", bars_around())
    save_bars(str(root), "BTCUSD", "H1", bars_around())
    write_config(str(root), {"XAUUSD/H1": CUTOFF.isoformat()})  # BTCUSD 无
    assert mod.main(["--data-root", str(root), "--dest",
                     str(tmp_path / "r")]) == 1
    assert "BTCUSD" in capsys.readouterr().out


# ---------------------------------------------------------------- 导出自检


def test_verify_flags_out_of_bound_series(tmp_path, main_store, capsys):
    """计划失败测试（T27）：研究库出现一条越界序列（last_time ≥ cutoff）
    → 自检失败退出；--verify-only 单独复检同样失败。"""
    from mt5data.store import save_bars

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0
    # 伪造越界：往研究库写一根 cutoff 之后的 bar
    bad = bars_around(n_pre=2, n_post=1)
    save_bars(str(dest), "XAUUSD", "H1", bad)
    violations = mod.verify_research_store(str(dest), CUTOFF)
    assert violations and "XAUUSD" in violations[0]
    assert mod.main(["--data-root", str(main_store), "--dest", str(dest),
                     "--verify-only"]) == 2


def test_verify_clean_store_passes(tmp_path, main_store):
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0
    assert mod.verify_research_store(str(dest), CUTOFF) == []
    assert mod.main(["--data-root", str(main_store), "--dest", str(dest),
                     "--verify-only"]) == 0


def test_export_skips_series_with_empty_open_segment(tmp_path, main_store):
    """cutoff 之前一根都没有的序列（整段都在锁箱内）：跳过、不写空
    parquet，导出整体仍成功（该序列的研究库本就该没有）。"""
    from mt5data.lockbox import write_global_cutoff
    from mt5data.store import read_watermark, save_bars

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    save_bars(str(main_store), "USTEC", "H1",
              bars_around(n_pre=0, n_post=5))   # 全部 ≥ cutoff
    dest = tmp_path / "data_research"
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0
    assert read_watermark(str(dest), "USTEC", "H1") is None
    assert mod.verify_research_store(str(dest), CUTOFF) == []


# ---------------------------------------------------------------- 研究侧可用


def test_build_costs_works_on_research_store_without_ticks(tmp_path,
                                                            main_store):
    """研究侧成本装配：研究库无 tick 目录，build_costs(research_store=True)
    直接命中缓存文件（水位核验由导出方完成）。"""
    rb = load_script("run_backtest_script_x", REPO_ROOT / "scripts"
                     / "run_backtest.py")
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0
    costs, ver = rb.build_costs(str(dest), "XAUUSD", 0.1,
                                allow_zero_swap=True, research_store=True)
    assert "缓存" in ver or ver.startswith("tick-spread")   # 可装配即可
    assert costs.spread.stat(8).n > 0
