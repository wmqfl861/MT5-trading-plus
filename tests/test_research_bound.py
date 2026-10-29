"""H-01：统一有效研究上界契约（mt5data.lockbox.effective_research_bound）。

契约（docs/reviews/2026-10-29-global-code-audit.md H-01）：
- 声明上界 declared：主库 lockbox cutoff（v2 全局 / v1 序列）、研究导出库
  bars meta 的 source_cutoff、ext manifest 的 research_cutoff——多个声明
  并存且不一致 → LockboxError（fail-closed：分不清哪条是真边界）；
- 显式 end：合法（≤ declared）则生效为更紧上界（含端点）；越界 →
  LockboxError（显式请求不做静默截断，与 research_bars 语义一致）；
- 已收盘水位 last_closed_time（含端点）参与取 min，统一换算为**严格**上界
  strict：合法研究行一律 time < strict；
- fail-closed：坏 meta（非法 JSON / 缺 store 必需字段）、ext 有数据无
  manifest、ext 段 manifest 缺 research_cutoff 且无锁箱声明兜底——一律
  LockboxError，绝不带着不明边界放行；
- 无任何声明边界的主库（未设 lockbox、无 source_cutoff、无 ext）→
  declared/strict 视情况可得（水位仍参与）；research_bars 对手写 parquet
  （无 meta 无锁箱）保持回归兼容直读。
"""
import json
from pathlib import Path

import pandas as pd
import pytest

from mt5data.ext.core import ExtSeries, save_ext_series
from mt5data.lockbox import (
    LockboxError,
    effective_research_bound,
    research_bars,
    write_global_cutoff,
)
from mt5data.store import save_bars

SYM, TF = "XAUUSD", "H1"
CUTOFF = pd.Timestamp("2026-06-01T00:00:00")
H1 = pd.Timedelta(hours=1)


def bars(n_pre=10, n_post=6):
    """cutoff 前后各若干根 H1（前段 05-31 10:00..19:00，后段 06-01 起）。"""
    times = list(pd.date_range("2026-05-31 10:00", periods=n_pre, freq="h")) \
        + list(pd.date_range("2026-06-01 00:00", periods=n_post, freq="h"))
    return pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": 100.0, "tick_volume": 10, "spread": 5, "real_volume": 0})


def stamp_source_cutoff(root, symbol=SYM, tf=TF, source_cutoff=CUTOFF):
    """把 bars meta 变成研究导出库形态：追加 source_cutoff（不写 lockbox.json）。"""
    p = Path(root) / "bars" / symbol / tf / "meta.json"
    meta = json.loads(p.read_text(encoding="utf-8"))
    meta["source_cutoff"] = pd.Timestamp(source_cutoff).isoformat()
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def write_ext(root, df, *, research_cutoff=CUTOFF, symbol=SYM, tf=TF,
              source="histdata"):
    """写一份带 manifest 的 ext 序列；research_cutoff=None → 不带该字段。"""
    manifest = {
        "source": source,
        "source_url": "https://example.invalid/histdata",
        "symbol": symbol,
        "timeframe": tf,
        "price_side": "bid（合成测试）",
        "original_timezone": "合成 naive UTC",
        "dst_rule": "无（合成）",
        "terms_note": "合成测试序列，非真实来源",
        "downloaded_at_utc": "2026-10-29T00:00:00+00:00",
        "source_files": [{"name": "synthetic.zip", "sha256": "0" * 64}],
        "converter_version": 1,
        "rows": int(len(df)),
        "first_time": pd.Timestamp(df["time"].iloc[0]).isoformat(),
        "last_time": pd.Timestamp(df["time"].iloc[-1]).isoformat(),
    }
    if research_cutoff is not None:
        manifest["research_cutoff"] = pd.Timestamp(research_cutoff).isoformat()
    save_ext_series(str(Path(root) / "ext"), ExtSeries(df=df, manifest=manifest))


def ext_bars(n=5):
    """cutoff 之前的 ext H1 段（2026-05-20 起）。"""
    return pd.DataFrame({
        "time": pd.date_range("2026-05-20 10:00", periods=n, freq="h"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0,
        "tick_volume": 10, "spread": 5, "real_volume": 0})


# ================================================================ 各库型上界

class TestDeclaredBound:
    def test_main_library_bound_is_lockbox_cutoff(self, tmp_path):
        """主库：声明上界 = 全局 lockbox cutoff；水位来自 meta。"""
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        write_global_cutoff(str(root), CUTOFF, note="t")
        b = effective_research_bound(str(root), SYM, TF)
        assert b.declared == CUTOFF
        assert "lockbox" in b.sources
        assert b.last_closed == pd.Timestamp("2026-06-01T05:00:00")
        assert b.strict == CUTOFF  # cutoff 早于 last_closed+1h

    def test_exported_store_bound_is_source_cutoff(self, tmp_path):
        """合法导出研究库（无 lockbox.json）：声明上界 = meta.source_cutoff。"""
        root = tmp_path / "data_research"
        save_bars(str(root), SYM, TF, bars())
        stamp_source_cutoff(root)
        b = effective_research_bound(str(root), SYM, TF)
        assert b.declared == CUTOFF
        assert "bars:source_cutoff" in b.sources

    def test_pure_ext_store_bound_is_manifest_research_cutoff(self, tmp_path):
        """纯 ext 库（无 bars 段）：声明上界 = ext manifest.research_cutoff。"""
        root = tmp_path / "data_research"
        write_ext(root, ext_bars(), research_cutoff=CUTOFF)
        b = effective_research_bound(str(root), SYM, TF)
        assert b.declared == CUTOFF
        assert b.sources == ("ext:histdata",)
        assert b.last_closed is None
        assert b.strict == CUTOFF

    def test_mixed_store_bounds_agree(self, tmp_path):
        """混合库（bars + ext）：两处声明一致 → 同一上界，不冲突。"""
        root = tmp_path / "data_research"
        save_bars(str(root), SYM, TF, bars())
        stamp_source_cutoff(root)
        write_ext(root, ext_bars(), research_cutoff=CUTOFF)
        b = effective_research_bound(str(root), SYM, TF)
        assert b.declared == CUTOFF
        assert set(b.sources) == {"bars:source_cutoff", "ext:histdata"}

    def test_conflicting_lockbox_vs_source_cutoff_fails(self, tmp_path):
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        stamp_source_cutoff(root, source_cutoff=CUTOFF)
        write_global_cutoff(str(root), CUTOFF - pd.Timedelta(days=1), note="t")
        with pytest.raises(LockboxError, match="冲突"):
            effective_research_bound(str(root), SYM, TF)
        with pytest.raises(LockboxError):
            research_bars(str(root), SYM, TF)

    def test_conflicting_bars_vs_ext_fails(self, tmp_path):
        root = tmp_path / "data_research"
        save_bars(str(root), SYM, TF, bars())
        stamp_source_cutoff(root, source_cutoff=CUTOFF)
        write_ext(root, ext_bars(), research_cutoff=CUTOFF + pd.Timedelta(days=1))
        with pytest.raises(LockboxError, match="冲突"):
            effective_research_bound(str(root), SYM, TF)


# ================================================================ 显式 end

class TestEndParam:
    def test_end_equal_to_bound_is_legal(self, tmp_path):
        """end == cutoff 合法：结果仍严格 < cutoff（cutoff 时刻属锁箱）。"""
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        write_global_cutoff(str(root), CUTOFF, note="t")
        b = effective_research_bound(str(root), SYM, TF, end=CUTOFF)
        assert b.strict == CUTOFF
        assert research_bars(str(root), SYM, TF, end=CUTOFF)["time"].max() \
            == pd.Timestamp("2026-05-31T19:00:00")

    def test_end_beyond_declared_raises(self, tmp_path):
        """end 越界 → LockboxError（主库 cutoff 与导出库 source_cutoff 同规）。"""
        main_root = tmp_path / "data"
        save_bars(str(main_root), SYM, TF, bars())
        write_global_cutoff(str(main_root), CUTOFF, note="t")
        with pytest.raises(LockboxError):
            effective_research_bound(str(main_root), SYM, TF,
                                     end=CUTOFF + H1)
        with pytest.raises(LockboxError):
            research_bars(str(main_root), SYM, TF, end=CUTOFF + H1)

        exp_root = tmp_path / "data_research"
        save_bars(str(exp_root), SYM, TF, bars())
        stamp_source_cutoff(exp_root)
        with pytest.raises(LockboxError):
            research_bars(str(exp_root), SYM, TF, end=CUTOFF + H1)

    def test_end_below_declared_takes_effect(self, tmp_path):
        """合法 end 生效为更紧上界（含端点：end 时刻的 bar 保留）。"""
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        write_global_cutoff(str(root), CUTOFF, note="t")
        end = pd.Timestamp("2026-05-31T15:00:00")
        df = research_bars(str(root), SYM, TF, end=end)
        assert df["time"].max() == end
        assert len(df) == 6  # 10:00..15:00


# ================================================================ 收盘水位

class TestWatermark:
    def test_watermark_caps_effective_bound(self, tmp_path):
        """有效上界 = min(声明上界, 最后已收盘 bar)——水位提前时以水位为准，
        且水位含端点（last_closed 那根保留，下一根剔除）。"""
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        write_global_cutoff(str(root), CUTOFF, note="t")
        p = Path(root) / "bars" / SYM / TF / "meta.json"
        meta = json.loads(p.read_text(encoding="utf-8"))
        meta["last_closed_time"] = "2026-05-31T15:00:00"
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        b = effective_research_bound(str(root), SYM, TF)
        assert b.last_closed == pd.Timestamp("2026-05-31T15:00:00")
        assert b.strict == pd.Timestamp("2026-05-31T16:00:00")  # 15:00 + 1 根 H1
        df = research_bars(str(root), SYM, TF)
        assert df["time"].max() == pd.Timestamp("2026-05-31T15:00:00")


# ================================================================ fail-closed

class TestFailClosed:
    def test_corrupt_meta_json_raises(self, tmp_path):
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        write_global_cutoff(str(root), CUTOFF, note="t")
        (Path(root) / "bars" / SYM / TF / "meta.json").write_text(
            "{ 不是 JSON", encoding="utf-8")
        with pytest.raises(LockboxError, match="fail-closed"):
            effective_research_bound(str(root), SYM, TF)
        with pytest.raises(LockboxError):
            research_bars(str(root), SYM, TF)

    def test_meta_missing_required_fields_raises(self, tmp_path):
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        write_global_cutoff(str(root), CUTOFF, note="t")
        p = Path(root) / "bars" / SYM / TF / "meta.json"
        meta = json.loads(p.read_text(encoding="utf-8"))
        del meta["last_closed_time"]
        p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(LockboxError, match="fail-closed"):
            effective_research_bound(str(root), SYM, TF)

    def test_ext_data_without_manifest_raises(self, tmp_path):
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        write_global_cutoff(str(root), CUTOFF, note="t")
        d = Path(root) / "ext" / "histdata" / SYM / TF
        d.mkdir(parents=True)
        ext_bars().to_parquet(d / "data.parquet")
        with pytest.raises(LockboxError, match="manifest"):
            effective_research_bound(str(root), SYM, TF)

    def test_unstamped_ext_in_exported_store_raises(self, tmp_path):
        """混合研究库的 ext 段缺 research_cutoff 戳：导出库的边界声明只盖了
        bars 段，ext 段来源不明 → 保守拒绝。"""
        root = tmp_path / "data_research"
        save_bars(str(root), SYM, TF, bars())
        stamp_source_cutoff(root)
        write_ext(root, ext_bars(), research_cutoff=None)
        with pytest.raises(LockboxError, match="research_cutoff"):
            effective_research_bound(str(root), SYM, TF)

    def test_unstamped_ext_with_lockbox_ok(self, tmp_path):
        """主库 ext 源 manifest 本就不带 research_cutoff：lockbox 全局 cutoff
        兜底，合法不拒。"""
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        write_global_cutoff(str(root), CUTOFF, note="t")
        write_ext(root, ext_bars(), research_cutoff=None)
        b = effective_research_bound(str(root), SYM, TF)
        assert b.declared == CUTOFF
        assert b.sources == ("lockbox",)


# ================================================================ 回归兼容

class TestCompat:
    def test_bare_store_without_lockbox_reads_all(self, tmp_path):
        """无任何声明边界的主库：research_bars 与直读一致（回归兼容）。"""
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars())
        b = effective_research_bound(str(root), SYM, TF)
        assert b.declared is None
        assert len(research_bars(str(root), SYM, TF)) == 16

    def test_handwritten_parquet_without_meta_reads_all(self, tmp_path):
        """bt 脚本测试形态：手写 parquet（无 meta 无锁箱）→ 直读全部。"""
        root = tmp_path / "data"
        d = root / "bars" / SYM / TF
        d.mkdir(parents=True)
        bars().to_parquet(d / "data.parquet")
        b = effective_research_bound(str(root), SYM, TF)
        assert b.declared is None and b.strict is None
        assert len(research_bars(str(root), SYM, TF)) == 16


# ================================================================ 研究读取零越界

class TestResearchReadsStayBelowCutoff:
    @pytest.mark.parametrize("kind", ["main", "exported"])
    def test_zero_rows_at_or_beyond_cutoff(self, tmp_path, kind):
        """所有研究读取（research_bars 走同一契约）不得返回 time >= cutoff。"""
        root = tmp_path / ("data" if kind == "main" else "data_research")
        save_bars(str(root), SYM, TF, bars())
        if kind == "main":
            write_global_cutoff(str(root), CUTOFF, note="t")
        else:
            stamp_source_cutoff(root)
        df = research_bars(str(root), SYM, TF)
        assert len(df) == 10
        assert (df["time"] >= CUTOFF).sum() == 0
        df_end = research_bars(str(root), SYM, TF, end=CUTOFF)
        assert (df_end["time"] >= CUTOFF).sum() == 0

    def test_old_fully_locked_series_returns_empty(self, tmp_path):
        """只剩锁箱数据的旧目标序列：读取返回零行，而不是报错或泄出。"""
        root = tmp_path / "data"
        save_bars(str(root), SYM, TF, bars(n_pre=0, n_post=5))
        write_global_cutoff(str(root), CUTOFF, note="t")
        df = research_bars(str(root), SYM, TF)
        assert df is not None and len(df) == 0
