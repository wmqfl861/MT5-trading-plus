"""T67/T81：外部数据接入（src/mt5data/ext + fetch/export 脚本）。

契约（蓝图 §6.5、铁律 19、D36、第十二轮审核）：
- 适配器统一输出仓库 K 线格式（naive UTC time + OHLCV），落
  data/ext/<来源>/<品种>/<周期>/data.parquet + manifest.json；
- manifest 必填字段（来源/下载时间/原始文件 sha256/转换版本/口径说明）
  缺一即拒绝入库；
- HistData 的 M1 时间戳按**实测规则**（美东偏移 + 欧盟 DST 日历，
  converter v3）转换——切换周的 bar 时刻必须正确；
- 外部数据只允许 cutoff（lockbox global_cutoff）之前部分进任何研究
  路径：导出工具 --include-ext（默认关）对 cutoff 硬校验；
- 一致性核对必须能检出"合成时区错位一小时"（部分月份差 1h 的
  夏令时处理错误）。

T81 追加（D36 批准落地、第十二轮 G5）：
- **--include-ext 白名单硬校验**：只有批准来源（fred/cdd，第十二轮
  审核批准表）可导出；未批准来源（histdata）被拒；
- **使用限定入 manifest**：批准来源的 manifest 必须带
  usage_restriction 且与批准文本一致，缺/错 → 拒绝导出；
- **逐年时间口径检测**（G5）：合成数据上能区分"固定美东（EST 不调
  表）/美东墙钟/欧盟日历"三种标签规则，并识别口径不一致的年份。
"""
import importlib.util
import io
import json
import sys
import zipfile
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

CUTOFF = pd.Timestamp("2026-06-01 00:00")


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 测试工具


def histdata_zip(csv_text: str) -> bytes:
    """把 ASCII 文本打包成 HistData 风格的 zip（单 csv）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("DAT_ASCII_EURUSD_M1_2020.csv", csv_text)
    return buf.getvalue()


def week_csv(start: str, end: str, freq_min: int = 60) -> str:
    """HistData 风格标签的分钟条（; 分隔：YYYYMMDD HHMMSS;O;H;L;C;V）。

    按外汇/黄金周结构留周末缺口：周六全天、周日至 18:00（标签，开市
    锚点）前无 bar。切换周的特殊标签（春季缺段/秋季重复）由专门测试
    手工构造，本生成器保持连续小时标签。
    """
    times = pd.date_range(start, end, freq=f"{freq_min}min")
    times = times[~((times.dayofweek == 5)
                    | ((times.dayofweek == 6) & (times.hour < 18)))]
    rows = []
    px = 1.1000
    for t in times:
        px += 0.0001
        stamp = t.strftime("%Y%m%d %H%M%S")
        rows.append(f"{stamp};{px:.5f};{px + 0.0002:.5f};"
                    f"{px - 0.0002:.5f};{px + 0.0001:.5f};0")
    return "\n".join(rows) + "\n"


def make_ext_series(mt, *, source="histdata", symbol="EURUSD", tf="H1",
                    df=None, **manifest_over):
    """用适配器产出一条合法 ExtSeries（manifest 可覆写）。"""
    from mt5data.resample import resample_bars
    csv = week_csv("2026-05-25 00:00", "2026-05-28 00:00")
    s = mt.histdata_m1_from_bytes(histdata_zip(csv), symbol=symbol,
                                  downloaded_at_utc="2026-10-27T00:00:00+00:00")
    if tf != "M1":
        s = s.with_timeframe(tf, resample_bars(s.df, tf))
    if df is not None:
        s = s.replace(df=df)
    for k, v in manifest_over.items():
        s.manifest[k] = v
    return s


def make_fred_series(mt, *, df=None, drop_usage=False, **manifest_over):
    """批准来源（fred/USTEC D1）的合法 ExtSeries，天然跨 cutoff。

    日期 05-28→06-02：前 4 天 < CUTOFF(06-01)，后 2 天 ≥。适配器写入
    批准的使用限定 usage_restriction；drop_usage=True 可模拟缺限定的
    坏 manifest。
    """
    raw = ("observation_date,NASDAQ100\n"
           "2026-05-28,100.0\n2026-05-29,101.0\n2026-05-30,102.0\n"
           "2026-05-31,103.0\n2026-06-01,104.0\n2026-06-02,105.0\n")
    s = mt.fred_daily_from_bytes(raw.encode(), symbol="USTEC",
                                 downloaded_at_utc="2026-10-27T00:00:00+00:00",
                                 source_name="NASDAQ100.csv")
    if df is not None:
        s = s.replace(df=df)
    if drop_usage:
        s.manifest.pop("usage_restriction", None)
    for k, v in manifest_over.items():
        s.manifest[k] = v
    return s


# ================================================================ 适配器：时区

class TestHistDataTz:
    """失败测试先行 ①：美东时间源的夏令时切换周边界。

    实测结论（推翻 HistData 官方"EST 固定"口径，见 manifest 与报告）：
    标签偏移为美东量级（冬 −5 / 夏 −4），但**夏令时窗按欧盟日历**
    （3 月最后一个周日 19:00 标签 → 10 月最后一个周日 19:00 标签）。
    周日开市锚点 18:00 标签：夏窗内 → 22:00 UTC，窗外 → 23:00 UTC。
    """

    def test_sunday_open_anchor_across_dst_switches(self):
        """美欧切换错位的 3 月（美国已夏/欧盟未夏 → 仍 23:00 UTC）、
        欧盟回拨日当天（10 月最后一个周日 18:00 标签仍在夏窗 → 22:00）
        与纯夏/纯冬周日的开市 bar 逐日落点必须全部正确。"""
        from mt5data import ext
        chunks = [
            week_csv("2020-02-28 12:00", "2020-03-02 06:00"),  # 冬（03-01）
            week_csv("2020-03-06 00:00", "2020-03-09 06:00"),  # 美切换周（03-08）
            week_csv("2020-03-13 00:00", "2020-03-16 06:00"),  # 美夏欧冬（03-15）
            week_csv("2020-07-03 00:00", "2020-07-06 06:00"),  # 纯夏（07-05）
            week_csv("2020-10-23 00:00", "2020-10-26 06:00"),  # 欧回拨日（10-25）
            week_csv("2020-10-30 00:00", "2020-11-02 06:00"),  # 美回拨周（11-01）
        ]
        s = ext.histdata_m1_from_bytes(
            histdata_zip("\n".join(chunks)), symbol="EURUSD",
            downloaded_at_utc="2026-10-27T00:00:00+00:00")
        df = s.df

        def sunday_first(d: str) -> pd.Timestamp:
            day = df[(df["time"] >= pd.Timestamp(d))
                     & (df["time"] < pd.Timestamp(d) + pd.Timedelta(days=1))]
            return day["time"].min()

        # 欧盟冬窗（含美欧错位的 3 月两周）：开市 18:00 标签 → 23:00 UTC
        for d in ("2020-03-01", "2020-03-08", "2020-03-15", "2020-11-01"):
            assert sunday_first(d) == pd.Timestamp(f"{d} 23:00"), d
        # 欧盟夏窗（含回拨日当天 18:00 仍属夏侧）→ 22:00 UTC
        for d in ("2020-07-05", "2020-10-25"):
            assert sunday_first(d) == pd.Timestamp(f"{d} 22:00"), d

    def test_fallback_duplicate_labels_resolve_by_order(self):
        """秋季回拨日 19:00–19:59 标签出现两次（先夏后冬）：转换后
        时刻严格递增、无重复（实测原始文件即如此）。"""
        from mt5data import ext
        from mt5data.ext.core import histdata_summer_window
        spring, fall = histdata_summer_window(2020)
        assert (spring.month, spring.day) == (3, 29)
        assert (fall.month, fall.day) == (10, 25)
        # 手工构造回拨窗重复标签：19:00 与 19:30 各出现两次
        rows = []
        px = 1.1
        for stamp in ("20201025 185900", "20201025 190000", "20201025 193000",
                      "20201025 193000", "20201025 190000", "20201025 200000"):
            px += 0.0001
            rows.append(f"{stamp};{px:.5f};{px:.5f};{px:.5f};{px:.5f};0")
        s = ext.histdata_m1_from_bytes(
            histdata_zip("\n".join(rows)), symbol="EURUSD",
            downloaded_at_utc="2026-10-27T00:00:00+00:00")
        t = list(s.df["time"])
        assert t == sorted(t) and len(set(t)) == len(t)
        # 首个 19:00（夏侧 −4）→ 23:00 UTC；第二个 19:00（冬侧 −5）→ 次日 00:00
        assert pd.Timestamp("2020-10-25 23:00") in t
        assert pd.Timestamp("2020-10-26 00:00") in t

    def test_no_gap_or_duplicate_hour_across_switch(self):
        """切换周内小时 cadence 连续（无缺小时、无重复小时）——美欧
        切换错位期间（如 2020-03-08 美国已夏、欧盟未夏）标签偏移不变
        （仍是 −5），转换后整周仍是整齐的 1 小时步进。"""
        from mt5data import ext
        csv = week_csv("2020-03-08 12:00", "2020-03-09 06:00")
        s = ext.histdata_m1_from_bytes(
            histdata_zip(csv), symbol="EURUSD",
            downloaded_at_utc="2026-10-27T00:00:00+00:00")
        assert s.df["time"].is_unique
        diffs = s.df["time"].diff().dropna().unique()
        assert len(diffs) == 1 and diffs[0] == pd.Timedelta(hours=1)

    def test_fixed_offset_helper_direction(self):
        """真正不调表的固定偏移源（如 UTC-5 固定）：UTC = 标签 − 偏移。"""
        from mt5data.ext import fixed_offset_to_utc
        t = pd.Series([pd.Timestamp("2020-03-08 17:00")])
        utc = fixed_offset_to_utc(t, offset_hours=-5)  # EST=-5 → +5h
        assert utc.iloc[0] == pd.Timestamp("2020-03-08 22:00")

    def test_wall_clock_conversion_matches_ny_dst(self):
        """墙钟换算（zoneinfo）：美东冬季 −5、夏季 −4。"""
        from mt5data.ext.core import wall_clock_to_utc
        wall = pd.Series([pd.Timestamp("2020-01-15 12:00"),
                          pd.Timestamp("2020-07-15 12:00")])
        utc = wall_clock_to_utc(wall, tz="America/New_York")
        assert utc.iloc[0] == pd.Timestamp("2020-01-15 17:00")  # EST
        assert utc.iloc[1] == pd.Timestamp("2020-07-15 16:00")  # EDT


class TestCddAndFredParsers:
    def test_cdd_daily_skips_banner_and_is_utc(self):
        from mt5data import ext
        raw = ("https://www.CryptoDataDownload.com\n"
               "unix,date,symbol,open,high,low,close,Volume BTC,Volume USD\n"
               "1793059200,2026-10-27 00:00:00,BTC/USD,80005.53,80078.01,"
               "79988.45,80043.17,4.73132841,378710.52\n"
               "1792972800,2026-10-26 00:00:00,BTC/USD,79000.0,80100.0,"
               "78900.0,80005.53,100.0,8000000.0\n")
        s = ext.cdd_daily_from_bytes(
            raw.encode(), symbol="BTCUSD",
            downloaded_at_utc="2026-10-27T00:00:00+00:00")
        assert list(s.df["time"]) == [pd.Timestamp("2026-10-26"),
                                      pd.Timestamp("2026-10-27")]
        assert s.df["close"].iloc[0] == pytest.approx(80005.53)
        assert s.manifest["price_side"] == "成交价（exchange last）"
        assert s.manifest["original_timezone"] == "UTC（日线日期）"

    def test_fred_daily_close_only(self):
        from mt5data import ext
        raw = ("observation_date,NASDAQ100\n"
               "2026-10-26,29273.400\n2026-10-23,28957.530\n")
        s = ext.fred_daily_from_bytes(
            raw.encode(), symbol="USTEC",
            downloaded_at_utc="2026-10-27T00:00:00+00:00")
        assert len(s.df) == 2
        row = s.df[s.df["time"] == pd.Timestamp("2026-10-26")].iloc[0]
        assert row["close"] == pytest.approx(29273.40)
        # close-only：OHLC 全部退化为收盘价，manifest 必须如实声明
        assert row["open"] == row["high"] == row["low"] == row["close"]
        assert s.manifest["close_only"] is True


# ================================================================ manifest

class TestManifest:
    def test_missing_field_rejected_on_save(self, tmp_path):
        """失败测试先行 ③：manifest 缺字段 → 拒绝入库（ExtDataError）。"""
        from mt5data import ext
        s = make_ext_series(ext)
        m = dict(s.manifest)
        m.pop("source_files")
        with pytest.raises(ext.ExtDataError, match="source_files"):
            ext.save_ext_series(str(tmp_path / "ext"), s.replace_manifest(m))

    def test_missing_terms_note_rejected(self, tmp_path):
        from mt5data import ext
        s = make_ext_series(ext)
        m = dict(s.manifest)
        m["terms_note"] = ""
        with pytest.raises(ext.ExtDataError, match="terms_note"):
            ext.save_ext_series(str(tmp_path / "ext"), s.replace_manifest(m))

    def test_load_rejects_manifest_missing_on_disk(self, tmp_path):
        """落盘后 manifest 被改坏 → 读取时同样拒绝（fail-fast）。"""
        from mt5data import ext
        s = make_ext_series(ext)
        ext.save_ext_series(str(tmp_path / "ext"), s)
        p = tmp_path / "ext" / "histdata" / "EURUSD" / "H1" / "manifest.json"
        m = json.loads(p.read_text(encoding="utf-8"))
        del m["converter_version"]
        p.write_text(json.dumps(m), encoding="utf-8")
        with pytest.raises(ext.ExtDataError, match="converter_version"):
            ext.load_ext_series(str(tmp_path / "ext"), "histdata", "EURUSD", "H1")

    def test_save_then_load_roundtrip_with_inventory(self, tmp_path):
        from mt5data import ext
        s = make_ext_series(ext)
        ext.save_ext_series(str(tmp_path / "ext"), s)
        df, manifest = ext.load_ext_series(str(tmp_path / "ext"), "histdata",
                                           "EURUSD", "H1")
        assert len(df) == len(s.df)
        assert manifest["source"] == "histdata"
        inv = ext.ext_inventory(str(tmp_path / "ext"))
        assert [(i["source"], i["symbol"], i["timeframe"]) for i in inv] == \
            [("histdata", "EURUSD", "H1")]

    def test_manifest_rows_must_match_df(self, tmp_path):
        from mt5data import ext
        s = make_ext_series(ext)
        m = dict(s.manifest)
        m["rows"] = m["rows"] + 1
        with pytest.raises(ext.ExtDataError, match="rows"):
            ext.save_ext_series(str(tmp_path / "ext"), s.replace_manifest(m))


# ================================================================ cutoff

@pytest.fixture
def main_store(tmp_path):
    """可用主库（复用 T27 测试的最小拼装：bars+lockbox+tick+缓存+specs）。"""
    from mt5data.lockbox import write_global_cutoff
    from mt5data.store import save_bars
    from mt5data.ticks import save_ticks
    from mt5quant.costs import SpreadModel
    from mt5data.ticks import tick_watermark

    rb = load_script("run_backtest_script_ext", REPO_ROOT / "scripts"
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


class TestCutoffDiscipline:
    """失败测试先行 ②：cutoff 之后的数据不进研究库。

    T81 起 --include-ext 有白名单，cutoff 行为用批准来源（fred）验证。
    """

    def ext_store(self, path, ext_mod):
        """外部库：一条 fred D1 序列跨 cutoff（前 4 天 <，后 2 天 ≥）。"""
        s = make_fred_series(ext_mod)
        ext_mod.save_ext_series(str(path), s)
        return path

    def test_ext_not_exported_by_default(self, tmp_path, main_store):
        """默认不启用：不带 --include-ext 时研究库不出现 ext/ 目录。"""
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        dest = tmp_path / "r"
        assert mod.main(["--data-root", str(main_store), "--dest",
                         str(dest)]) == 0
        assert not (dest / "ext").exists()

    def test_ext_clipped_to_cutoff_and_verified(self, tmp_path, main_store):
        """--include-ext：cutoff 之后的行被裁掉；自检断言全部 last_time < cutoff。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        self.ext_store(tmp_path / "extroot", ext)
        dest = tmp_path / "r"
        rc = mod.main(["--data-root", str(main_store), "--dest", str(dest),
                       "--include-ext", "--ext-root",
                       str(tmp_path / "extroot")])
        assert rc == 0
        df, manifest = ext.load_ext_series(str(dest / "ext"), "fred",
                                           "USTEC", "D1")
        assert df["time"].max() < CUTOFF
        assert len(df) == 4
        assert manifest["research_cutoff"] == CUTOFF.isoformat()
        assert mod.verify_ext_research_store(dest, CUTOFF) == []

    def test_ext_export_refuses_without_lockbox(self, tmp_path):
        """无锁箱配置 → --include-ext 直接拒绝（外部数据同样受 cutoff 约束，
        没有 cutoff 就没有入研究库的边界）。"""
        from mt5data import ext
        from mt5data.store import save_bars
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        root = tmp_path / "data"
        save_bars(str(root), "XAUUSD", "H1", pd.DataFrame({
            "time": [pd.Timestamp("2026-05-31 10:00")], "open": 100.0,
            "high": 100.5, "low": 99.5, "close": 100.0, "tick_volume": 1,
            "spread": 5, "real_volume": 0}))
        self.ext_store(tmp_path / "extroot", ext)
        assert mod.main(["--data-root", str(root), "--dest",
                         str(tmp_path / "r"), "--include-ext", "--ext-root",
                         str(tmp_path / "extroot")]) == 1

    def test_ext_export_fails_on_postcheck_violation(self, tmp_path,
                                                     main_store):
        """导出后自检：研究库 ext 段出现越界序列（伪造）→ 退出码 2。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        self.ext_store(tmp_path / "extroot", ext)
        dest = tmp_path / "r"
        assert mod.main(["--data-root", str(main_store), "--dest", str(dest),
                         "--include-ext", "--ext-root",
                         str(tmp_path / "extroot")]) == 0
        # 伪造越界：往研究库 ext 段写一根 cutoff 之后的 bar（带合法 manifest）
        bad = pd.DataFrame({
            "time": [pd.Timestamp("2026-06-05")], "open": 1.1,
            "high": 1.1, "low": 1.1, "close": 1.1, "tick_volume": 1,
            "spread": 0, "real_volume": 0})
        s = make_fred_series(ext, df=bad)
        ext.save_ext_series(str(dest / "ext"), s)
        v = mod.verify_ext_research_store(dest, CUTOFF)
        assert v and "fred" in v[0]


# ================================================================ T81 白名单

class TestIncludeExtWhitelist:
    """失败测试先行 ⑤（T81/D36）：--include-ext 白名单硬校验。

    第十二轮审核批准表：fred/cdd 批准（研究用途+使用限定入 manifest）；
    histdata 暂不批准（条款不明）——样本只留主库侧作一致性核对。
    """

    def histdata_store(self, path, ext_mod):
        s = make_ext_series(ext_mod)
        ext_mod.save_ext_series(str(path), s)
        return path

    def test_unapproved_only_present_refuses_export(self, tmp_path,
                                                    main_store, capsys):
        """ext 库里只有未批准来源（histdata）→ --include-ext 下该序列被拒：
        不进研究库；因没有任何批准序列可导出，整体退出非零。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        self.histdata_store(tmp_path / "extroot", ext)
        rc = mod.main(["--data-root", str(main_store), "--dest",
                       str(tmp_path / "r"), "--include-ext", "--ext-root",
                       str(tmp_path / "extroot")])
        assert rc == 1
        out = capsys.readouterr().out
        assert "白名单" in out and "histdata" in out
        # 研究库里绝不能出现未批准来源的数据
        assert not (tmp_path / "r" / "ext" / "histdata").exists()

    def test_unapproved_skipped_approved_exported(self, tmp_path, main_store,
                                                  capsys):
        """histdata（未批准）与 fred（批准）同库：histdata 被拒并逐条报告，
        fred 正常导出——研究库只含批准来源。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        root = tmp_path / "extroot"
        self.histdata_store(root, ext)
        ext.save_ext_series(str(root), make_fred_series(ext))
        rc = mod.main(["--data-root", str(main_store), "--dest",
                       str(tmp_path / "r"), "--include-ext", "--ext-root",
                       str(root)])
        assert rc == 0
        df, _ = ext.load_ext_series(str(tmp_path / "r" / "ext"), "fred",
                                    "USTEC", "D1")
        assert len(df) == 4
        assert not (tmp_path / "r" / "ext" / "histdata").exists()
        out = capsys.readouterr().out
        assert "白名单" in out and "histdata" in out

    def test_approved_missing_usage_restriction_refuses(self, tmp_path,
                                                        main_store, capsys):
        """批准来源但 manifest 缺使用限定（usage_restriction）→ 拒绝导出
        （研究数据不带使用限定入库违反 D36，fail-fast 整体失败）。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        ext.save_ext_series(str(tmp_path / "extroot"),
                            make_fred_series(ext, drop_usage=True))
        rc = mod.main(["--data-root", str(main_store), "--dest",
                       str(tmp_path / "r"), "--include-ext", "--ext-root",
                       str(tmp_path / "extroot")])
        assert rc == 1
        assert "使用限定" in capsys.readouterr().out

    def test_usage_restriction_text_mismatch_refuses(self, tmp_path,
                                                     main_store, capsys):
        """使用限定文本与批准表不一致（被改弱/改错）→ 拒绝导出。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        ext.save_ext_series(str(tmp_path / "extroot"),
                            make_fred_series(
                                ext, usage_restriction="随意用，可再分发"))
        rc = mod.main(["--data-root", str(main_store), "--dest",
                       str(tmp_path / "r"), "--include-ext", "--ext-root",
                       str(tmp_path / "extroot")])
        assert rc == 1
        assert "使用限定" in capsys.readouterr().out

    def test_manifest_carries_canonical_usage_restriction(self):
        """适配器产出的批准来源 manifest 自带与批准表一致的使用限定。"""
        from mt5data import ext
        from mt5data.ext.approvals import SOURCE_APPROVALS
        s = make_fred_series(ext)
        assert s.manifest["usage_restriction"] \
            == SOURCE_APPROVALS["fred"]["usage_restriction"]
        assert "只作研究用途" in s.manifest["usage_restriction"]
        assert "不再分发" in s.manifest["usage_restriction"]


# ================================================================ T81 逐年口径

def _synth_weeks_utc(year: int) -> pd.DatetimeIndex:
    """合成一年的分钟 bar（naive UTC）：周开盘=纽约周日 17:00，收周五，
    每个交易日纽约 17:00–18:00 换日空档（周一至周五；周日 17:00 是开市
    保留）。结构锚点即 G5 所述：周开盘与换日均锚在纽约 17:00 一侧。
    用分钟条（与真实 HistData M1 同 cadence），30 分钟下限的换日空档
    检测才有意义。
    """
    ny = "America/New_York"
    sundays = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="W-SUN")
    bars = []
    for sun in sundays:
        open_utc = pd.Timestamp(sun + pd.Timedelta(hours=17)) \
            .tz_localize(ny, nonexistent="shift_forward").tz_convert("UTC") \
            .tz_localize(None)
        close_utc = pd.Timestamp(sun + pd.Timedelta(days=5, hours=15)) \
            .tz_localize(ny, nonexistent="shift_forward").tz_convert("UTC") \
            .tz_localize(None)
        minutes = pd.date_range(open_utc, close_utc, freq="min")
        local = minutes.tz_localize("UTC").tz_convert(ny)
        # 换日空档：挖掉周一至周五的纽约 17:00 小时（周日 17:00 是开市）
        keep = ~((local.hour == 17) & (local.dayofweek != 6))
        bars.append(minutes[keep])
    return bars[0].append(bars[1:]) if len(bars) > 1 else bars[0]


def _labels_for_rule(utc_index: pd.DatetimeIndex, rule: str) -> pd.Series:
    """把真 UTC bar 起点映成各候选标签规则下的原始标签（naive）：
    - us_wall：美东墙钟（随美 DST 调表）；
    - fixed_est：固定 EST（UTC−5，不调表）；
    - eu_calendar：美东量级偏移、切换跟欧盟日历（按欧盟切换瞬间的
      理想化规则——用于检测器判别，周日 17/16 点档模式与实测一致）；
    - eu_calendar_histdata：converter v3 实测规则的逆（夏窗边界=标签
      空间"最后一个周日 19:00"≡UTC"次日周一 00:00"）——用于往返测试。
    """
    if rule == "us_wall":
        z = utc_index.tz_localize("UTC").tz_convert("America/New_York")
        return pd.Series(z.tz_localize(None))
    if rule == "fixed_est":
        return pd.Series(utc_index - pd.Timedelta(hours=5))
    if rule in ("eu_calendar", "eu_calendar_histdata"):
        if rule == "eu_calendar":
            berlin = ZoneInfo("Europe/Berlin")
            off = [4 if t.to_pydatetime().replace(
                        tzinfo=timezone.utc).astimezone(berlin).dst()
                   else 5 for t in utc_index]
        else:
            year = int(pd.DatetimeIndex(utc_index).year[0])

            def monday_after_last_sunday(month: int) -> pd.Timestamp:
                import calendar
                last = calendar.monthrange(year, month)[1]
                wd = pd.Timestamp(year, month, last).dayofweek  # Mon=0
                sunday = last - ((wd + 1) % 7)
                return pd.Timestamp(year, month, sunday) \
                    + pd.Timedelta(days=1)
            lo = monday_after_last_sunday(3)
            hi = monday_after_last_sunday(10)
            off = [4 if lo <= t < hi else 5 for t in utc_index]
        return pd.Series(utc_index) - pd.to_timedelta(off, unit="h")
    raise ValueError(rule)


class TestYearlyLabelCalendar:
    """失败测试先行 ⑥（T81/G5）：合成数据上逐年口径检测区分三种规则，
    并识别口径不一致的年份（不一致年份不得套用同一转换）。"""

    def test_distinguishes_three_rules_on_synthetic_year(self):
        from mt5data.ext import consistency as cons
        utc = _synth_weeks_utc(2021)
        for rule in ("us_wall", "fixed_est", "eu_calendar"):
            rep = cons.yearly_label_calendar(_labels_for_rule(utc, rule))
            assert rep[2021]["calendar"] == rule, \
                (rule, rep[2021])
            assert rep[2021]["anchor_hours"], rep[2021]

    def test_mixed_rules_within_one_year_flagged_inconsistent(self):
        """同一文件/年份内前半年欧盟日历、后半年固定 EST → 不一致
        （任何单一规则偏差都超容差），必须单独登记。"""
        from mt5data.ext import consistency as cons
        utc = _synth_weeks_utc(2021)
        first = utc[utc < pd.Timestamp("2021-07-01")]
        second = utc[utc >= pd.Timestamp("2021-07-01")]
        mixed = pd.concat([_labels_for_rule(first, "eu_calendar"),
                           _labels_for_rule(second, "fixed_est")],
                          ignore_index=True)
        rep = cons.yearly_label_calendar(mixed)
        assert rep[2021]["calendar"] == "inconsistent", rep[2021]
        # 证据：三种规则的偏差数都超出容差
        assert max(rep[2021]["deviations"].values()) \
            > rep[2021]["tolerance"]

    def test_years_classified_separately(self):
        """多年文件逐年判定：2020 欧盟日历、2021 固定 EST → 两个年份各自
        得出正确结论（不许整段套同一转换）。"""
        from mt5data.ext import consistency as cons
        labels = pd.concat([
            _labels_for_rule(_synth_weeks_utc(2020), "eu_calendar"),
            _labels_for_rule(_synth_weeks_utc(2021), "fixed_est")],
            ignore_index=True)
        rep = cons.yearly_label_calendar(labels)
        assert set(rep) == {2020, 2021}
        assert rep[2020]["calendar"] == "eu_calendar", rep[2020]
        assert rep[2021]["calendar"] == "fixed_est", rep[2021]

    def test_eu_labels_roundtrip_through_converter(self):
        """converter v3 实测规则的逆标签 ↔ histdata_stamps_to_utc 互逆，
        且检测器把它判为欧盟日历——检测器认可的规则与入库转换同源。"""
        from mt5data.ext import consistency as cons
        from mt5data.ext.core import histdata_stamps_to_utc
        utc = _synth_weeks_utc(2021)
        labels = _labels_for_rule(utc, "eu_calendar_histdata")
        back = histdata_stamps_to_utc(labels)
        assert list(pd.DatetimeIndex(back).sort_values()) == \
            list(pd.DatetimeIndex(utc).sort_values())
        rep = cons.yearly_label_calendar(labels)
        assert rep[2021]["calendar"] == "eu_calendar", rep[2021]


# ================================================================ 一致性核对

class TestConsistencyDetector:
    """失败测试先行 ④：一致性核对能检出合成时区错位一小时。"""

    def _pair(self, *, days=365):
        """合成全年工作日小时条：ext 为真 UTC，mt5 为 EET 风格服务器墙钟
        （冬 +2h / 夏 +3h，由 utc_to_server 按 Europe/Athens 生成）。"""
        from mt5data.ext import consistency as cons
        hours = pd.date_range("2020-01-02", periods=days * 24, freq="h")
        hours = hours[~((hours.dayofweek == 5) | (hours.dayofweek == 6))]
        closes = 1.10 + pd.Series(range(len(hours)), dtype="float64") * 0.0001
        ext_df = pd.DataFrame({
            "time": hours, "open": closes - 0.0001, "high": closes + 0.0002,
            "low": closes - 0.0002, "close": closes,
            "tick_volume": 5, "spread": 0, "real_volume": 0})
        mt5_df = ext_df.copy()
        mt5_df["time"] = cons.utc_to_server(pd.Series(hours),
                                            tz="Europe/Athens")
        return ext_df, mt5_df

    def test_detector_passes_on_aligned_pair(self):
        from mt5data.ext import consistency as cons
        ext_df, mt5_df = self._pair()
        rep = cons.detect_hour_misalignment(ext_df, mt5_df,
                                            server_tz="Europe/Athens")
        assert rep["misaligned"] is False
        assert rep["matched_fraction"] > 0.99
        # 原始标签差（服务器−UTC）全年模式 = 夏令时 +3（约 7 个月对 5 个月）
        assert rep["modal_offset_hours"] == 3
        assert rep["offset_by_month_diff"] == 0

    def test_detector_flags_one_hour_dst_error(self):
        """合成时区错位：把 ext 侧夏令时期间（5–10 月）整体 +1h——
        相当于把 EST 固定源误按墙钟处理。检测器必须报错位。"""
        from mt5data.ext import consistency as cons
        ext_df, mt5_df = self._pair()
        mask = (ext_df["time"] >= pd.Timestamp("2020-05-01")) & \
               (ext_df["time"] < pd.Timestamp("2020-11-01"))
        shifted = ext_df.copy()
        shifted.loc[mask, "time"] = shifted.loc[mask, "time"] \
            + pd.Timedelta(hours=1)
        rep = cons.detect_hour_misalignment(shifted, mt5_df,
                                            server_tz="Europe/Athens")
        assert rep["misaligned"] is True
        # 报告里能看到"部分月份差 1 小时"的实测证据
        assert rep["offset_by_month_diff"] == 1

    def test_dst_switch_week_bar_alignment(self):
        """3 月/10 月底切换周：server→UTC 换算在切换前后各差一小时，
        换算函数本身必须逐根正确（这是 bar 时刻对齐的基础）。"""
        from mt5data.ext import consistency as cons
        # 春季切换（欧洲 2020-03-29 03:00 → 04:00）：切换前 EET +2、后 EEST +3
        wall = pd.Series([pd.Timestamp("2020-03-28 12:00"),
                          pd.Timestamp("2020-03-29 12:00")])
        utc = cons.server_to_utc(wall, tz="Europe/Athens")
        assert utc.iloc[0] == pd.Timestamp("2020-03-28 10:00")  # EET +2
        assert utc.iloc[1] == pd.Timestamp("2020-03-29 09:00")  # EEST +3
        # 秋季切换（2020-10-25 04:00 → 03:00）：切换前 +3、后 +2
        wall2 = pd.Series([pd.Timestamp("2020-10-24 12:00"),
                           pd.Timestamp("2020-10-25 12:00")])
        utc2 = cons.server_to_utc(wall2, tz="Europe/Athens")
        assert utc2.iloc[0] == pd.Timestamp("2020-10-24 09:00")  # EEST +3
        assert utc2.iloc[1] == pd.Timestamp("2020-10-25 10:00")  # EET +2
