"""T27/H3：研究库导出（scripts/export_research_store.py）。

契约：
- 只导出 time < 全局 cutoff 的 K 线到 data_research/bars/**（同布局），
  meta 追加来源信息（source_root / source_cutoff / data_sha256 /
  exported_at_utc）；
- 成本模型缓存 data/costs/spread_model.json 复制到 data_research/costs/
  （tick **不**导出：tick 库对研究账户拒读，研究侧成本经缓存文件提供，
  缓存新鲜度在导出时对照主库 tick 水位核验）；specs/latest.json 必须有
  （合约大小与手数上下限），calibrated.json / server_clock.json 有则复制；
- lockbox.json **不**复制：研究库没有锁箱段；H-01 起研究库的有效上界 =
  meta.source_cutoff（research_bars 在研究库上同样拒绝越界 end）；
- 导出后自检（H-01 强化）：枚举**真实 parquet 文件**（不只 meta 清单），
  任何序列 last_time 都必须早于 cutoff；orphan parquet（有数据无 meta）、
  坏 meta（非法 JSON / 缺字段 / 声明越界）一律失败（--verify-only 单独复检）；
- H-01 暂存发布：导出先写 {dest}.staging，自检通过后原子上位；任何失败
  清理暂存——目标目录永远只有完整快照（旧快照或新快照）。
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
    # H-01：研究库的有效上界 = meta.source_cutoff（导出时记录）——end 越过
    # 它同样 LockboxError（可信导出库不再是"无锁箱直读"）
    from mt5data.lockbox import LockboxError
    with pytest.raises(LockboxError):
        research_bars(str(dest), "XAUUSD", "H1", end=pd.Timestamp("2030-01-01"))
    df = research_bars(str(dest), "XAUUSD", "H1", end=CUTOFF)
    assert len(df) == 10
    assert (df["time"] >= CUTOFF).sum() == 0


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


# ================================================================ H-01 自检强化


def _export_ok(mod, main_store, dest):
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0


def test_verify_flags_orphan_parquet(tmp_path, main_store):
    """【H-01 失败测试】orphan parquet（有数据无 meta，含越界数据）：
    旧实现只遍历 meta 清单 → 零违规漏检（审核已合成复现）；必须失败。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    _export_ok(mod, main_store, dest)
    sdir = dest / "bars" / "EURUSD" / "H1"
    sdir.mkdir(parents=True)
    bars_around(n_pre=2, n_post=2).to_parquet(sdir / "data.parquet")
    violations = mod.verify_research_store(str(dest), CUTOFF)
    assert any("EURUSD" in v and "meta" in v for v in violations), violations
    assert mod.main(["--data-root", str(main_store), "--dest", str(dest),
                     "--verify-only"]) == 2


def test_verify_flags_bad_meta_missing_fields(tmp_path, main_store):
    """【H-01 失败测试】meta 缺 store 必需字段（坏 meta）→ 失败（旧实现
    经 series_inventory 记成 warning 后被丢弃）。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    _export_ok(mod, main_store, dest)
    p = dest / "bars" / "XAUUSD" / "H1" / "meta.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    del meta["last_closed_time"]
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    violations = mod.verify_research_store(str(dest), CUTOFF)
    assert any("XAUUSD" in v and "坏 meta" in v for v in violations), violations
    assert mod.main(["--data-root", str(main_store), "--dest", str(dest),
                     "--verify-only"]) == 2


def test_verify_flags_corrupt_meta_json(tmp_path, main_store):
    """【H-01 失败测试】meta.json 非法 JSON → 失败（warnings 不再被忽略）。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    _export_ok(mod, main_store, dest)
    (dest / "bars" / "XAUUSD" / "H1" / "meta.json").write_text(
        "{ 不是 JSON", encoding="utf-8")
    violations = mod.verify_research_store(str(dest), CUTOFF)
    assert any("坏 meta" in v for v in violations), violations
    assert mod.main(["--data-root", str(main_store), "--dest", str(dest),
                     "--verify-only"]) == 2


def test_verify_flags_meta_without_data(tmp_path, main_store):
    """【H-01 失败测试】meta 在、data.parquet 缺（数据/元数据不一致）→ 失败。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    _export_ok(mod, main_store, dest)
    (dest / "bars" / "XAUUSD" / "H1" / "data.parquet").unlink()
    violations = mod.verify_research_store(str(dest), CUTOFF)
    assert any("data.parquet" in v or "无数据" in v
               for v in violations), violations


def test_verify_flags_source_cutoff_beyond_cutoff(tmp_path, main_store):
    """【H-01 失败测试】meta 声明越界（source_cutoff 晚于核验 cutoff）→ 失败。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    _export_ok(mod, main_store, dest)
    p = dest / "bars" / "XAUUSD" / "H1" / "meta.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["source_cutoff"] = (CUTOFF + pd.Timedelta(days=1)).isoformat()
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    violations = mod.verify_research_store(str(dest), CUTOFF)
    assert any("source_cutoff" in v for v in violations), violations


def test_verify_flags_data_beyond_own_source_cutoff(tmp_path, main_store):
    """【H-01 失败测试】数据越过自身 meta.source_cutoff（坏 meta 越界）→ 失败，
    即便 last_time 仍早于核验 cutoff。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    _export_ok(mod, main_store, dest)
    p = dest / "bars" / "XAUUSD" / "H1" / "meta.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["source_cutoff"] = pd.Timestamp("2026-05-31T15:00:00").isoformat()
    p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    violations = mod.verify_research_store(str(dest), CUTOFF)
    assert any("source_cutoff" in v for v in violations), violations


def test_verify_ext_flags_orphan_ext_parquet(tmp_path, main_store):
    """【H-01 失败测试】ext 段 orphan parquet（无 manifest）→ ext 自检失败。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    _export_ok(mod, main_store, dest)
    d = dest / "ext" / "histdata" / "XAUUSD" / "H1"
    d.mkdir(parents=True)
    bars_around(n_pre=2, n_post=2).to_parquet(d / "data.parquet")
    violations = mod.verify_ext_research_store(str(dest), CUTOFF)
    assert any("manifest" in v for v in violations), violations


# ================================================================ H-01 暂存发布


def test_failed_export_leaves_no_partial_store(tmp_path, main_store):
    """【H-01 失败测试】导出中途失败（缓存过期在 bars 已写之后）：
    旧实现直接写目标目录 → 留下可读的不完整快照；必须清理暂存、目标不存在。"""
    from mt5data.ticks import save_ticks

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "r"
    save_ticks(str(main_store), "XAUUSD",
               full_hour_ticks().assign(
                   time_msc=full_hour_ticks()["time_msc"] + 86_400_000))
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 1
    assert not dest.exists()          # 旧实现：dest/bars/** 已写入 → RED
    assert not (tmp_path / "r.staging").exists()


def test_verify_failure_cleans_staging(tmp_path, main_store, monkeypatch):
    """【H-01 失败测试】自检不通过 → 导出作废：清理暂存，目标不发布。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    monkeypatch.setattr(mod, "verify_research_store",
                        lambda d, c: ["伪造越界：TEST/H1"])
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(tmp_path / "r")]) == 2
    assert not (tmp_path / "r").exists()
    assert not (tmp_path / "r.staging").exists()


def test_staged_publish_replaces_stale_locked_series(tmp_path, main_store):
    """【H-01 失败测试】收紧锁箱后旧目标序列只剩锁箱数据：
    暂存发布 → 新快照只含仍开放的序列，旧的越界序列不得残留
    （旧实现把新旧混写进目标 → 自检失败 rc=2 且留下可读坏快照）。"""
    from mt5data.lockbox import write_global_cutoff
    from mt5data.store import load_bars, read_watermark, save_bars
    from mt5data.ticks import save_ticks, tick_watermark

    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0
    # 收紧全局锁箱（只许更早）：XAUUSD 开放段（05-31 10:00 起）整段落入锁箱
    tighter = pd.Timestamp("2026-05-31T00:00:00")
    write_global_cutoff(str(main_store), tighter, note="H-01 收紧")
    # 新增一条收紧后仍开放的序列（含 tick 缓存水位，供成本缓存核验）
    eur = pd.DataFrame({
        "time": pd.date_range("2026-05-20 10:00", periods=6, freq="h"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 10, "spread": 5, "real_volume": 0})
    save_bars(str(main_store), "EURUSD", "H1", eur)
    save_ticks(str(main_store), "EURUSD", full_hour_ticks())
    cache_p = main_store / "costs" / "spread_model.json"
    doc = json.loads(cache_p.read_text(encoding="utf-8"))
    doc["watermarks"]["EURUSD"] = tick_watermark(str(main_store), "EURUSD")
    cache_p.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    rc = mod.main(["--data-root", str(main_store), "--dest", str(dest)])
    assert rc == 0                                   # 旧实现 rc=2 → RED
    assert read_watermark(str(dest), "XAUUSD", "H1") is None   # 旧序列不残留
    seg = load_bars(str(dest), "EURUSD", "H1")
    assert seg is not None and seg["time"].max() < tighter
    assert not Path(str(dest) + ".staging").exists()
    assert mod.main(["--data-root", str(main_store), "--dest", str(dest),
                     "--verify-only"]) == 0


def test_staged_publish_success_replaces_and_cleans(tmp_path, main_store):
    """【H-01 失败测试】成功导出：目标发布为完整快照，暂存目录消失。"""
    mod = load_script("export_research_store_x", SCRIPT_PATH)
    dest = tmp_path / "data_research"
    assert mod.main(["--data-root", str(main_store), "--dest",
                     str(dest)]) == 0
    assert (dest / "bars" / "XAUUSD" / "H1" / "data.parquet").is_file()
    assert (dest / "costs" / "spread_model.json").is_file()
    assert not Path(str(dest) + ".staging").exists()

