"""S4c：export_research_store.py 域限定导出模式（--only-ext-source）。

背景（05-minimal-change-design）：全量导出被两处阻断——成本缓存新鲜度门
（主库 tick 水位是移动靶）与 _publish_staging 整库替换（会清掉外部进程
写入研究库的 USDJPY bars 与 calendars/）。域限定模式 = 唯一安全路径：
只把指定批准来源的 ext 序列重导进研究库 ext 段。

契约：
- 只导指定来源的 ext 段：**不**导 bars、**不**复制 costs/specs、**不**触发
  成本缓存新鲜度门、**不**做整库暂存发布——dest 既有 bars/costs/specs/
  calendars 逐字节不变（外部写入得以保全）；
- 自检只跑 verify_ext_research_store（范围 = 写入范围）；违例退出 2；
- 域外来源（未列出的，含未批准来源）不导出、不拒绝计数、不 raise——
  only_sources 过滤在 check_exportable 之前（否则未批准的域外来源会把
  exported=0 + rejected>0 变成整体失败）；
- 指定来源内白名单/使用限定校验照常生效（零新语义）；
- 无全局 cutoff → 拒绝（ext 同样必须受锁箱约束）；指定来源无可导出
  序列（不存在或开放段全空）→ 退出非零；
- 与 --include-ext / --verify-only 互斥（明确拒绝，退出 1）；
- flag 可重复（action='append'，集合语义）；
- 默认路径（不带新 flag）零变化——由既有 export/store 测试全绿证明。
"""
import hashlib
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


# ---------------------------------------------------------------- 测试工具


def make_fred_series(ext, *, df=None, drop_usage=False, **manifest_over):
    """批准来源（fred/USTEC D1）的合法 ExtSeries，天然跨 cutoff。

    日期 05-28→06-02：前 4 天 < CUTOFF(06-01)，后 2 天 ≥。
    """
    raw = ("observation_date,NASDAQ100\n"
           "2026-05-28,100.0\n2026-05-29,101.0\n2026-05-30,102.0\n"
           "2026-05-31,103.0\n2026-06-01,104.0\n2026-06-02,105.0\n")
    s = ext.fred_daily_from_bytes(raw.encode(), symbol="USTEC",
                                  downloaded_at_utc="2026-10-27T00:00:00+00:00",
                                  source_name="NASDAQ100.csv")
    if df is not None:
        s = s.replace(df=df)
    if drop_usage:
        s.manifest.pop("usage_restriction", None)
    for k, v in manifest_over.items():
        s.manifest[k] = v
    return s


def make_cdd_series(ext, **manifest_over):
    """批准来源（cdd/BTCUSD D1）的合法 ExtSeries，跨 cutoff。"""
    raw = ("https://www.CryptoDataDownload.com\n"
           "unix,date,symbol,open,high,low,close,Volume BTC,Volume USD\n"
           "1780416000,2026-06-02 00:00:00,BTC/USD,105.0,106.0,104.0,105.5,"
           "1.0,105.5\n"
           "1780329600,2026-06-01 00:00:00,BTC/USD,104.0,105.0,103.0,104.5,"
           "1.0,104.5\n"
           "1780243200,2026-05-31 00:00:00,BTC/USD,103.0,104.0,102.0,103.5,"
           "1.0,103.5\n"
           "1780156800,2026-05-30 00:00:00,BTC/USD,102.0,103.0,101.0,102.5,"
           "1.0,102.5\n")
    s = ext.cdd_daily_from_bytes(raw.encode(), symbol="BTCUSD",
                                 downloaded_at_utc="2026-10-27T00:00:00+00:00")
    for k, v in manifest_over.items():
        s.manifest[k] = v
    return s


def make_stooq_series(ext, **manifest_over):
    """未批准来源（stooq/USTEC D1）的合法 ExtSeries。"""
    raw = ("Date,Open,High,Low,Close,Volume\n"
           "2026-05-28,100.0,101.0,99.5,100.5,1000\n"
           "2026-05-29,100.5,101.5,100.0,101.0,1100\n"
           "2026-06-01,101.0,102.0,100.5,101.5,1200\n")
    s = ext.stooq_daily_from_bytes(raw.encode(), symbol="USTEC",
                                   downloaded_at_utc="2026-10-27T00:00:00+00:00")
    for k, v in manifest_over.items():
        s.manifest[k] = v
    return s


@pytest.fixture
def main_store(tmp_path):
    """可用主库：bars（跨 cutoff）+ lockbox.json + tick + 点差缓存 + specs。

    域限定模式本身只需要 lockbox（cutoff 边界），bars/tick/缓存/specs 供
    "先全量导出再域限定重导"场景与互斥/拒绝类测试共用。
    """
    from mt5data.lockbox import write_global_cutoff
    from mt5data.store import save_bars
    from mt5data.ticks import save_ticks, tick_watermark
    from mt5quant.costs import SpreadModel

    rb = load_script("run_backtest_script_scoped", REPO_ROOT / "scripts"
                     / "run_backtest.py")
    root = tmp_path / "data"
    times = list(pd.date_range("2026-05-31 10:00", periods=10, freq="h")) \
        + list(pd.date_range("2026-06-01 00:00", periods=6, freq="h"))
    save_bars(str(root), "XAUUSD", "H1", pd.DataFrame({
        "time": times, "open": 100.0, "high": 100.5, "low": 99.5,
        "close": 100.0, "tick_volume": 10, "spread": 5, "real_volume": 0}))
    write_global_cutoff(str(root), CUTOFF, note="测试锁箱")
    ticks = pd.DataFrame({
        "time_msc": [pd.Timestamp(f"2026-10-19 {h:02d}:00:00").value // 10**6
                     for h in range(24) for _ in range(2)],
        "bid": [2400.0] * 48, "ask": [2400.3] * 48,
        "last": [2400.15] * 48, "volume": [0.0] * 48, "flags": [2] * 48})
    save_ticks(str(root), "XAUUSD", ticks)
    tk = rb.load_ticks(str(root), "XAUUSD", columns=["time_msc", "bid", "ask"])
    rb.save_spread_model_cache(
        rb.spread_model_cache_path(str(root)),
        SpreadModel.from_ticks("XAUUSD", tk, mode="relative"),
        {"XAUUSD": tick_watermark(str(root), "XAUUSD")})
    specs = root / "specs"
    specs.mkdir(parents=True)
    (specs / "latest.json").write_text(json.dumps(
        {"XAUUSD": {"contract_size": 100.0, "volume_min": 0.01,
                    "volume_step": 0.01, "volume_max": 200.0}}),
        encoding="utf-8")
    return root


def _tree_sha(root: Path) -> dict[str, str]:
    """全目录快照：{相对路径: sha256}（零触碰断言的事实来源）。"""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return out


def _scoped_run(mod, main_store, dest, ext_root, *sources):
    return mod.main(["--data-root", str(main_store), "--dest", str(dest),
                     "--ext-root", str(ext_root)]
                    + [f"--only-ext-source={s}" for s in sources])


# ================================================================ 域限定导出


class TestScopedExtExport:
    def test_scoped_exports_only_listed_source_clipped_to_cutoff(
            self, tmp_path, main_store, capsys):
        """--only-ext-source fred：只 fred 进研究库 ext 段，裁到 cutoff；
        同库的 stooq（未批准、域外）不导出、不拒绝、零触碰。"""
        from mt5data import ext
        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root), make_fred_series(ext))
        ext.save_ext_series(str(ext_root), make_stooq_series(ext))
        dest = tmp_path / "r"
        rc = _scoped_run(mod, main_store, dest, ext_root, "fred")
        assert rc == 0
        df, manifest = ext.load_ext_series(str(dest / "ext"), "fred",
                                           "USTEC", "D1")
        assert len(df) == 4                       # 05-28..05-31 < CUTOFF
        assert df["time"].max() < CUTOFF
        assert manifest["research_cutoff"] == CUTOFF.isoformat()
        assert manifest["exported_at_utc"]
        assert manifest["usage_restriction"] \
            == ext.SOURCE_APPROVALS["fred"]["usage_restriction"]
        assert not (dest / "ext" / "stooq").exists()
        out = capsys.readouterr().out
        # 域外来源静默跳过：不出现白名单拒绝报告（过滤在 check_exportable 前）
        assert "stooq" not in out
        assert "白名单" not in out
        assert "fred" in out


    def test_scoped_repeatable_flag_exports_source_set(
            self, tmp_path, main_store):
        """flag 可重复（action='append'）：fred + cdd 两个批准来源都导出。"""
        from mt5data import ext
        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root), make_fred_series(ext))
        ext.save_ext_series(str(ext_root), make_cdd_series(ext))
        dest = tmp_path / "r"
        rc = mod.main(["--data-root", str(main_store), "--dest", str(dest),
                       "--ext-root", str(ext_root),
                       "--only-ext-source", "fred",
                       "--only-ext-source", "cdd"])
        assert rc == 0
        assert ext.load_ext_series(str(dest / "ext"), "fred", "USTEC", "D1")
        assert ext.load_ext_series(str(dest / "ext"), "cdd", "BTCUSD", "D1")


    def test_scoped_parquet_bytes_stable_across_reruns(self, tmp_path,
                                                       main_store):
        """域限定重导确定性：同源同 cutoff 两次导出的 data.parquet 逐位相同
        （生产 S4c 依赖：研究库 fred parquet sha 保持 c6645256… 不变）。"""
        from mt5data import ext
        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root), make_fred_series(ext))
        dest = tmp_path / "r"
        assert _scoped_run(mod, main_store, dest, ext_root, "fred") == 0
        pq1 = (dest / "ext" / "fred" / "USTEC" / "D1" / "data.parquet") \
            .read_bytes()
        assert _scoped_run(mod, main_store, dest, ext_root, "fred") == 0
        pq2 = (dest / "ext" / "fred" / "USTEC" / "D1" / "data.parquet") \
            .read_bytes()
        assert pq1 == pq2


    def test_scoped_skips_cost_gate_and_touches_nothing_but_ext_fred(
            self, tmp_path, main_store):
        """核心安全断言：先全量导出成 dest，再（1）把主库 tick 水位推前让
        成本缓存过期（全量路径必拒），（2）外部进程往 dest 写 USDJPY bars
        （越过 cutoff）与 calendars/——域限定重导必须全部零触碰：无成本门、
        bars/costs/specs/calendars/server_clock 逐字节不变、无整库暂存发布。"""
        from mt5data import ext
        from mt5data.store import save_bars
        from mt5data.ticks import save_ticks

        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root), make_fred_series(ext))
        dest = tmp_path / "data_research"
        assert mod.main(["--data-root", str(main_store), "--dest",
                         str(dest), "--include-ext", "--ext-root",
                         str(ext_root)]) == 0
        # 缓存过期（移动靶：主库 tick 前进、缓存未重建）——全量路径 rc=1
        save_ticks(str(main_store), "XAUUSD", pd.DataFrame({
            "time_msc": [pd.Timestamp("2026-10-20 00:00:00").value // 10**6],
            "bid": [2400.0], "ask": [2400.3], "last": [2400.15],
            "volume": [0.0], "flags": [2]}))
        assert mod.main(["--data-root", str(main_store), "--dest",
                         str(dest)]) == 1
        # 外部进程写入：USDJPY bars（越过 cutoff，无 source_cutoff 边界）
        # + calendars/（全量暂存发布会整体清掉它们）
        usdjpy = pd.DataFrame({
            "time": pd.date_range("2026-06-05", periods=3, freq="D"),
            "open": 150.0, "high": 151.0, "low": 149.0, "close": 150.5,
            "tick_volume": 10, "spread": 5, "real_volume": 0})
        save_bars(str(dest), "USDJPY", "D1", usdjpy,
                  server="Exness-MT5Trial5", login=278008680)
        cal = dest / "calendars"
        cal.mkdir()
        (cal / "USTEC.json").write_text('{"外部进程写入": true}',
                                        encoding="utf-8")
        pre = _tree_sha(dest)
        assert "bars/USDJPY/D1/data.parquet" in pre
        assert "calendars/USTEC.json" in pre
        fred_pq_sha = pre["ext/fred/USTEC/D1/data.parquet"]

        rc = _scoped_run(mod, main_store, dest, ext_root, "fred")
        assert rc == 0                            # 过期缓存不再阻断
        post = _tree_sha(dest)
        # 零触碰：bars/costs/specs/calendars/server_clock 全部逐字节不变
        untouched = {p: s for p, s in post.items()
                     if not p.startswith("ext/fred/")}
        assert untouched == {p: s for p, s in pre.items()
                             if not p.startswith("ext/fred/")}
        # fred parquet 逐位不变（确定性重导）；manifest 因 exported_at_utc 更新
        assert post["ext/fred/USTEC/D1/data.parquet"] == fred_pq_sha
        assert post["ext/fred/USTEC/D1/manifest.json"] \
            != pre["ext/fred/USTEC/D1/manifest.json"]
        # 无整库暂存发布的痕迹
        assert not Path(str(dest) + ".staging").exists()
        assert not Path(str(dest) + ".superseded").exists()


    def test_scoped_unlisted_source_only_means_nothing_exportable(
            self, tmp_path, main_store, capsys):
        """ext 库只有域外来源（stooq）而请求 fred：域外静默跳过（不拒绝、
        不 raise——过滤在 check_exportable 之前），exported=0 → 退出非零。"""
        from mt5data import ext
        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root), make_stooq_series(ext))
        dest = tmp_path / "r"
        rc = _scoped_run(mod, main_store, dest, ext_root, "fred")
        assert rc == 1
        out = capsys.readouterr().out
        assert "无可导出" in out
        assert "stooq" not in out            # 域外不报拒绝
        assert not dest.exists()             # 零写入


    def test_scoped_missing_source_name_fails(self, tmp_path, main_store,
                                              capsys):
        """请求不存在的来源 → exported=0 → 退出非零（点名错误来源）。"""
        from mt5data import ext
        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root), make_fred_series(ext))
        rc = _scoped_run(mod, main_store, tmp_path / "r", ext_root, "cdd")
        assert rc == 1
        assert "无可导出" in capsys.readouterr().out


    def test_scoped_still_enforces_usage_restriction_within_source(
            self, tmp_path, main_store, capsys):
        """指定来源内白名单/使用限定校验照常生效：fred manifest 缺使用限定
        → 拒绝导出（域限定不是绕过批准语义的后门）。"""
        from mt5data import ext
        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root),
                            make_fred_series(ext, drop_usage=True))
        rc = _scoped_run(mod, main_store, tmp_path / "r", ext_root, "fred")
        assert rc == 1
        assert "使用限定" in capsys.readouterr().out


    def test_scoped_postcheck_violation_fails_exit_2(self, tmp_path,
                                                     main_store, capsys):
        """导出后 ext 段自检（verify_ext_research_store）：预置 orphan ext
        parquet → 违例 → 退出 2（自检范围 = ext 段，与写入范围一致）。"""
        from mt5data import ext
        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root), make_fred_series(ext))
        dest = tmp_path / "r"
        orphan = dest / "ext" / "bad" / "XAUUSD" / "D1"
        orphan.mkdir(parents=True)
        pd.DataFrame({"time": [pd.Timestamp("2026-05-01")], "open": 1.0,
                      "high": 1.0, "low": 1.0, "close": 1.0,
                      "tick_volume": 1, "spread": 0,
                      "real_volume": 0}).to_parquet(orphan / "data.parquet")
        rc = _scoped_run(mod, main_store, dest, ext_root, "fred")
        assert rc == 2
        out = capsys.readouterr().out
        assert "越界" in out and "bad" in out
        # 自检违例作废本次导出，但 fred 序列按 save_ext_series 原子语义
        # 保持完整（新旧之一）；bars 段自检（USDJPY 类外部违例）不在范围
        assert ext.load_ext_series(str(dest / "ext"), "fred", "USTEC", "D1")


# ================================================================ 互斥与边界


class TestScopedExtGuardrails:
    def test_scoped_rejects_include_ext_and_verify_only(self, tmp_path,
                                                        main_store, capsys):
        """--only-ext-source 与 --include-ext / --verify-only 互斥：明确
        拒绝退出 1（域限定模式的语义与二者不叠加）。"""
        from mt5data import ext
        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        ext_root = tmp_path / "extroot"
        ext.save_ext_series(str(ext_root), make_fred_series(ext))
        base = ["--data-root", str(main_store), "--dest", str(tmp_path / "r"),
                "--ext-root", str(ext_root)]
        rc = mod.main(base + ["--only-ext-source", "fred", "--include-ext"])
        assert rc == 1
        assert "--only-ext-source" in capsys.readouterr().out
        rc = mod.main(base + ["--only-ext-source", "fred", "--verify-only"])
        assert rc == 1
        assert "--only-ext-source" in capsys.readouterr().out


    def test_scoped_requires_global_cutoff(self, tmp_path, capsys):
        """v1 逐序列锁箱（无全局 cutoff）→ 拒绝：域限定导出同样必须受
        锁箱约束，没有 cutoff 就没有入研究库的边界。"""
        from mt5data.lockbox import write_config
        from mt5data.store import save_bars

        mod = load_script("export_research_store_scoped", SCRIPT_PATH)
        root = tmp_path / "data"
        save_bars(str(root), "XAUUSD", "H1", pd.DataFrame({
            "time": [pd.Timestamp("2026-05-31 10:00")], "open": 100.0,
            "high": 100.5, "low": 99.5, "close": 100.0, "tick_volume": 1,
            "spread": 5, "real_volume": 0}))
        write_config(str(root), {"XAUUSD/H1": CUTOFF.isoformat()})
        rc = mod.main(["--data-root", str(root), "--dest", str(tmp_path / "r"),
                       "--ext-root", str(tmp_path / "extroot"),
                       "--only-ext-source", "fred"])
        assert rc == 1
        assert "全局 cutoff" in capsys.readouterr().out
