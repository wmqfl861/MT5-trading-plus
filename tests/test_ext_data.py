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
- **--include-ext 白名单硬校验**：只有批准来源可导出；未批准来源被拒；
- **使用限定入 manifest**：批准来源的 manifest 必须带
  usage_restriction 且与批准文本一致，缺/错 → 拒绝导出；
- **逐年时间口径检测**（G5）：合成数据上能区分"固定美东（EST 不调
  表）/美东墙钟/欧盟日历"三种标签规则，并识别口径不一致的年份。

T87 追加（第十三轮 D36 补充结论：HistData 有条件批准）：
- histdata 进入 --include-ext 白名单（此前被拒的反转）：manifest 带
  R13 §5 的使用限定（只作个人研究、不再分发）即可导出；
- **定盘锚点检测**（EURUSD 逐年启用条件）：伦敦 16:00 定盘窗的分钟级
  波动尖峰位置核对——合成数据里把尖峰错位一小时必须判为不一致。
"""
import importlib.util
import io
import json
import sys
import zipfile
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
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


def make_stooq_series(mt, *, df=None, **manifest_over):
    """未批准来源（stooq/USTEC D1，实测拒服务不用）的合法 ExtSeries。

    T87 起 histdata 已批准，"白名单拒绝未批准来源"的行为改由 stooq
    验证（批准表见 mt5data.ext.approvals.SOURCE_APPROVALS）。
    """
    raw = ("Date,Open,High,Low,Close,Volume\n"
           "2026-05-28,100.0,101.0,99.5,100.5,1000\n"
           "2026-05-29,100.5,101.5,100.0,101.0,1100\n"
           "2026-06-01,101.0,102.0,100.5,101.5,1200\n")
    s = mt.stooq_daily_from_bytes(raw.encode(), symbol="USTEC",
                                  downloaded_at_utc="2026-10-27T00:00:00+00:00")
    if df is not None:
        s = s.replace(df=df)
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

    def test_save_ext_series_interleaved_writers_do_not_share_tmp(
            self, tmp_path, monkeypatch):
        """H-11 失败测试：两个保存者不共用同一 .tmp（ext 写入无锁，全靠唯一名）。

        写者 A 落完 parquet 临时文件、尚未发布时，写者 B 对同一序列完整
        保存一轮。固定名 data.parquet.tmp 下 B 会把 A 的半成品截断/替换
        掉，A 随后 fsync 或 replace 直接 FileNotFoundError。唯一临时名后
        两个写者互不相干，最终落盘版本自洽可读。
        """
        from mt5data import ext
        from mt5data.ext import core as ext_core

        s_a = make_ext_series(ext)
        s_b = make_ext_series(ext).replace(  # 同序列不同行数：B 少 5 根
            df=make_ext_series(ext).df.iloc[:-5])

        calls = {"n": 0}
        real_fsync = ext_core._fsync_file

        def seam(path):
            if calls["n"] == 0:  # A 的 parquet tmp 刚写完、还没发布
                calls["n"] += 1
                ext.save_ext_series(str(tmp_path / "ext"), s_b)  # B 全程完成
            real_fsync(path)

        monkeypatch.setattr(ext_core, "_fsync_file", seam)

        ext.save_ext_series(str(tmp_path / "ext"), s_a)  # A 不再因共用 tmp 失败

        df, manifest = ext.load_ext_series(str(tmp_path / "ext"), "histdata",
                                           "EURUSD", "H1")
        assert len(df) == len(s_a.df)  # A 后发布，最终版本 = A
        assert manifest["rows"] == len(s_a.df)
        d = tmp_path / "ext" / "histdata" / "EURUSD" / "H1"
        assert not any(p.name.endswith(".tmp") for p in d.iterdir())


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


# ================================================================ T81/T87 白名单

class TestIncludeExtWhitelist:
    """失败测试先行 ⑤（T81/D36）+ T87 反转（第十三轮 D36 补充结论）。

    批准表演变：第十二轮批准 fred/cdd、histdata 暂不批准；**第十三轮
    §5 histdata 有条件批准**（只作个人研究、不再分发，依据 FAQ 原文 +
    抓取日期 + 本地条款核查路径）——T87 起进入 --include-ext 白名单，
    此前"histdata 被拒"的测试反转为"histdata 可导出"；未批准来源的
    拒绝行为改由 stooq（实测拒服务不用）验证。
    """

    def histdata_store(self, path, ext_mod):
        s = make_ext_series(ext_mod)
        ext_mod.save_ext_series(str(path), s)
        return path

    def stooq_store(self, path, ext_mod):
        ext_mod.save_ext_series(str(path), make_stooq_series(ext_mod))
        return path

    def test_unapproved_only_present_refuses_export(self, tmp_path,
                                                    main_store, capsys):
        """ext 库里只有未批准来源（stooq）→ --include-ext 下该序列被拒：
        不进研究库；因没有任何批准序列可导出，整体退出非零。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        self.stooq_store(tmp_path / "extroot", ext)
        rc = mod.main(["--data-root", str(main_store), "--dest",
                       str(tmp_path / "r"), "--include-ext", "--ext-root",
                       str(tmp_path / "extroot")])
        assert rc == 1
        out = capsys.readouterr().out
        assert "白名单" in out and "stooq" in out
        # 研究库里绝不能出现未批准来源的数据
        assert not (tmp_path / "r" / "ext" / "stooq").exists()

    def test_unapproved_skipped_approved_exported(self, tmp_path, main_store,
                                                  capsys):
        """stooq（未批准）与 fred（批准）同库：stooq 被拒并逐条报告，
        fred 正常导出——研究库只含批准来源。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        root = tmp_path / "extroot"
        self.stooq_store(root, ext)
        ext.save_ext_series(str(root), make_fred_series(ext))
        rc = mod.main(["--data-root", str(main_store), "--dest",
                       str(tmp_path / "r"), "--include-ext", "--ext-root",
                       str(root)])
        assert rc == 0
        df, _ = ext.load_ext_series(str(tmp_path / "r" / "ext"), "fred",
                                    "USTEC", "D1")
        assert len(df) == 4
        assert not (tmp_path / "r" / "ext" / "stooq").exists()
        out = capsys.readouterr().out
        assert "白名单" in out and "stooq" in out

    def test_histdata_now_approved_and_exported(self, tmp_path, main_store):
        """失败测试先行（T87）：histdata 有条件批准后的反转——带 R13 §5
        使用限定的 histdata 序列在 --include-ext 下**可以**导出（此前
        该场景整体退出非零）；研究库 ext 段出现 histdata 数据，且
        manifest 逐字携带批准时的使用限定。"""
        from mt5data import ext
        from mt5data.ext.approvals import SOURCE_APPROVALS
        assert SOURCE_APPROVALS["histdata"]["approved"] is True  # R13 §5
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        root = tmp_path / "extroot"
        self.histdata_store(root, ext)
        rc = mod.main(["--data-root", str(main_store), "--dest",
                       str(tmp_path / "r"), "--include-ext", "--ext-root",
                       str(root)])
        assert rc == 0
        df, manifest = ext.load_ext_series(str(tmp_path / "r" / "ext"),
                                           "histdata", "EURUSD", "H1")
        assert len(df) > 0
        assert manifest["usage_restriction"] \
            == SOURCE_APPROVALS["histdata"]["usage_restriction"]
        assert manifest["research_cutoff"] == CUTOFF.isoformat()

    def test_histdata_missing_usage_restriction_refuses(self, tmp_path,
                                                        main_store, capsys):
        """histdata 已批准但 manifest 缺使用限定（如 T87 之前的旧
        manifest）→ 拒绝导出：使用限定必须随批准数据入库（重新下载/
        重转换补齐 manifest），fail-fast 整体失败。"""
        from mt5data import ext
        mod = load_script("export_research_store_ext",
                          REPO_ROOT / "scripts" / "export_research_store.py")
        s = make_ext_series(ext)
        s.manifest.pop("usage_restriction", None)
        ext.save_ext_series(str(tmp_path / "extroot"), s)
        rc = mod.main(["--data-root", str(main_store), "--dest",
                       str(tmp_path / "r"), "--include-ext", "--ext-root",
                       str(tmp_path / "extroot")])
        assert rc == 1
        assert "使用限定" in capsys.readouterr().out

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

    def test_histdata_manifest_carries_r13_restriction(self):
        """失败测试先行（T87）：histdata 适配器产出的 manifest 必须带
        R13 §5 有条件批准的使用限定（只作个人研究、不再分发），且与
        批准表逐字一致——缺限定即无法通过 --include-ext 导出。"""
        from mt5data import ext
        from mt5data.ext.approvals import SOURCE_APPROVALS
        s = make_ext_series(ext)
        assert s.manifest["usage_restriction"] \
            == SOURCE_APPROVALS["histdata"]["usage_restriction"]
        assert "个人研究" in s.manifest["usage_restriction"]
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


# ================================================================ T94/R14-L9 按月

class TestMonthlyLabelCalendar:
    """失败测试先行（T94/R14-L9）：年判 inconsistent（两代规则过渡年）
    时按月分段判定——能判定的月份给出规则或 either_supported（该月美欧
    DST 状态一致，两支持规则转换等价），回收部分月份。"""

    def _mixed_labels_2021(self):
        """2021：上半年 us_wall、下半年 eu_calendar——错位月分出各自
        规则；区分度不足的月判 undetermined（不回收）。"""
        utc = _synth_weeks_utc(2021)
        first = utc[utc < pd.Timestamp("2021-07-01")]
        second = utc[utc >= pd.Timestamp("2021-07-01")]
        return pd.concat([_labels_for_rule(first, "us_wall"),
                          _labels_for_rule(second, "eu_calendar")],
                         ignore_index=True)

    def test_mixed_year_months_classified(self):
        from mt5data.ext import consistency as cons
        months = cons.monthly_label_calendar(self._mixed_labels_2021())[2021]
        # 3 月（美已切夏、欧未切——错位窗标签跟 us_wall，偏差分离清晰）
        assert months["2021-03"]["calendar"] == "us_wall", months["2021-03"]
        # 11 月（欧已回冬、美未回：错位窗标签跟 eu_calendar）
        assert months["2021-11"]["calendar"] == "eu_calendar", months["2021-11"]
        # 10 月：唯一错位日 10-31 的单个观测跟 eu_calendar，但两支持规则
        # 偏差都在容差内、区分度不足 → undetermined（不回收，保守）
        assert months["2021-10"]["calendar"] == "undetermined", \
            months["2021-10"]
        # 1 月（全冬）/7 月（全夏）：美欧状态一致，两支持规则不可分但
        # 都自洽——either_supported（转换等价，可回收）
        assert months["2021-01"]["calendar"] == "either_supported"
        assert months["2021-07"]["calendar"] == "either_supported"

    def test_inconsistent_year_monthly_salvage(self):
        """两代规则过渡场景（春季错位窗跟 eu、秋冬错位窗跟 us）——月份
        分歧正是一致性破坏的来源，按月判定各自给出规则：3 月 eu_calendar、
        11 月 us_wall、1 月 either_supported。（年判 inconsistent 的真实
        场景 = 2019/2024 文件，见 ext-data 报告 T94 节实测。）"""
        from mt5data.ext import consistency as cons
        utc = _synth_weeks_utc(2021)
        us_labels = pd.to_datetime(_labels_for_rule(utc, "us_wall"))
        eu_labels = pd.to_datetime(_labels_for_rule(utc, "eu_calendar"))
        months_idx = utc.month.to_numpy()
        mixed = pd.Series(us_labels)
        mixed[months_idx == 3] = eu_labels[months_idx == 3]
        months = cons.monthly_label_calendar(mixed)[2021]
        assert months["2021-03"]["calendar"] == "eu_calendar", \
            months["2021-03"]
        assert months["2021-11"]["calendar"] == "us_wall", months["2021-11"]
        assert months["2021-01"]["calendar"] == "either_supported"

    def test_pure_rule_year_months_agree_or_either(self):
        """整年单一规则：错位月判出该规则，非错位月 either_supported。"""
        from mt5data.ext import consistency as cons
        labels = _labels_for_rule(_synth_weeks_utc(2021), "us_wall")
        months = cons.monthly_label_calendar(labels)[2021]
        assert months["2021-03"]["calendar"] == "us_wall", months["2021-03"]
        assert months["2021-06"]["calendar"] == "either_supported"

    def test_fixed_est_month_not_salvageable(self):
        """错位月标签跟固定 EST（不支持规则）→ 该月不可回收（偏差对
        两个支持规则都超容差 → inconsistent）。"""
        from mt5data.ext import consistency as cons
        labels = _labels_for_rule(_synth_weeks_utc(2021), "fixed_est")
        months = cons.monthly_label_calendar(labels)[2021]
        assert months["2021-03"]["calendar"] == "inconsistent", \
            months["2021-03"]
        assert months["2021-07"]["calendar"] == "inconsistent", \
            months["2021-07"]


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


# ================================================================ T87 定盘锚点

def _synth_fix_spike_minutes(year: int, *, freq_min: int = 1) -> pd.DataFrame:
    """合成一年分钟 bar（naive UTC，全天连续）：定盘窗（伦敦本地
    15:55–16:05）分钟收益幅度 ×20，其余分钟为基线幅度——尖峰位置即
    "伦敦 16:00 定盘窗分钟级波动尖峰"的合成替身。确定性构造（无种子，
    逐位可复算）：交替正负的三角波幅度序列。
    """
    t = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01",
                      freq=f"{freq_min}min", inclusive="left")
    t = t[~(t.dayofweek == 5)]  # 剔除周六留周结构感（对小时画像无影响）
    local = t.tz_localize("UTC").tz_convert("Europe/London")
    mod = local.hour * 60 + local.minute
    base = 1e-5
    n = len(t)
    # 幅度：基线小幅起伏 + 定盘窗放大
    wave = base * (1 + ((np.arange(n) % 7) / 7.0))
    amp = np.where((mod >= 15 * 60 + 55) & (mod < 16 * 60 + 5),
                   wave * 20.0, wave)
    ret = amp * np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    close = 1.10 * np.cumprod(1.0 + ret)
    prev = np.concatenate([[1.10], close[:-1]])
    return pd.DataFrame({
        "time": t, "open": prev, "high": np.maximum(prev, close),
        "low": np.minimum(prev, close), "close": close,
        "tick_volume": 1, "spread": 0, "real_volume": 0})


class TestFixWindowAnchor:
    """失败测试先行（T87/R13 §5）：定盘锚点逐年核对——伦敦 16:00 定盘窗
    分钟级波动尖峰的位置（三分判定：定盘窗 vs 平移 ±1h 对照窗）；合成
    数据里把尖峰错位一小时，必须判为不一致（该年份不得启用）。"""

    def test_spike_located_at_fix_window(self):
        from mt5data.ext import consistency as cons
        df = _synth_fix_spike_minutes(2015)
        rep = cons.fix_window_vol_spike(df)
        assert 2015 in rep, rep
        r = rep[2015]
        assert r["status"] == "consistent", r
        # 定盘窗均值压过两个平移对照窗（合成里 ×20）
        assert r["location"] == "fix"
        assert r["v_fix"] > 5 * r["v_minus_1h"], r
        assert r["v_fix"] > 5 * r["v_plus_1h"], r

    def test_spike_located_on_month_end_population(self):
        """月末口径（C002 的事件总体）：判定与全日口径一致。"""
        from mt5data.ext import consistency as cons
        df = _synth_fix_spike_minutes(2015)
        rep = cons.fix_window_vol_spike(df, month_end_only=True)
        assert rep[2015]["status"] == "consistent", rep[2015]

    def test_spike_shifted_one_hour_flagged_inconsistent(self):
        """错位一小时攻击：整段时间 +1h（等效标签口径错一小时）→ 尖峰
        落到 16:55–17:05 → 位置=plus_1h → inconsistent，该年不得启用。"""
        from mt5data.ext import consistency as cons
        df = _synth_fix_spike_minutes(2015)
        shifted = df.copy()
        shifted["time"] = shifted["time"] + pd.Timedelta(hours=1)
        rep = cons.fix_window_vol_spike(shifted)
        assert rep[2015]["status"] == "inconsistent", rep[2015]
        assert rep[2015]["location"] == "plus_1h", rep[2015]

    def test_shifted_minus_one_hour_also_flagged(self):
        from mt5data.ext import consistency as cons
        df = _synth_fix_spike_minutes(2016)
        shifted = df.copy()
        shifted["time"] = shifted["time"] - pd.Timedelta(hours=1)
        rep = cons.fix_window_vol_spike(shifted)
        assert rep[2016]["status"] == "inconsistent", rep[2016]
        assert rep[2016]["location"] == "minus_1h", rep[2016]

    def test_no_spike_undetermined(self):
        """无定盘尖峰（平价波动）→ 分不出位置 → undetermined。T94/R14-L4
        起 undetermined **不排除**年份（锚点强度与效应强弱相关，按强度取舍
        等于按效应挑样本）——闸门侧以标签规则检测为准，manifest 注明。"""
        from mt5data.ext import consistency as cons
        df = _synth_fix_spike_minutes(2015)
        n = len(df)
        flat = 1e-5 * np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
        close = 1.10 * np.cumprod(1.0 + flat)
        prev = np.concatenate([[1.10], close[:-1]])
        flat_df = pd.DataFrame({
            "time": df["time"], "open": prev, "high": np.maximum(prev, close),
            "low": np.minimum(prev, close), "close": close,
            "tick_volume": 1, "spread": 0, "real_volume": 0})
        rep = cons.fix_window_vol_spike(flat_df)
        assert rep[2015]["status"] == "undetermined", rep[2015]

    def test_years_reported_separately(self):
        """多年序列逐年给出锚点结论（与 yearly_label_calendar 同粒度）。"""
        from mt5data.ext import consistency as cons
        df = pd.concat([_synth_fix_spike_minutes(2014),
                        _synth_fix_spike_minutes(2015)], ignore_index=True)
        rep = cons.fix_window_vol_spike(df)
        assert set(rep) == {2014, 2015}
        assert all(r["status"] == "consistent" for r in rep.values())


# ================================================================ T87 v4/us_wall 与逐年闸门

class TestHistDataUsWallRule:
    """失败测试先行（T87）：2013–2018 文件实测为美东墙钟标签（v4），
    与 2020+ 的欧盟日历管道（v3）不同——adapter 按 label_rule 分派转换，
    切换周边界由 zoneinfo 钉死；混用 v3 会把美欧错位窗错一小时。"""

    def _zip_from_labels(self, labels: pd.Series, year: int) -> bytes:
        rows = []
        px = 1.1
        for t in labels:
            px += 0.0001
            rows.append(f"{t.strftime('%Y%m%d %H%M%S')};{px:.5f};"
                        f"{px:.5f};{px:.5f};{px:.5f};0")
        return histdata_zip("\n".join(rows).replace(
            "DAT_ASCII_EURUSD_M1_2020.csv",
            f"DAT_ASCII_EURUSD_M1_{year}.csv"))

    def test_us_wall_labels_convert_with_v4(self):
        """us_wall 标签（美东墙钟）经 v4 转换后：周日 18:00 标签的美东
        冬/夏两季分别落在 23:00 / 22:00 UTC（跟美国 DST，不跟欧盟）；
        2021-03-21（美国 03-14 已切夏、欧盟 03-28 未切）标签偏移已是
        夏令 −4——v3 在同一时刻仍是 −5（美欧错位窗两规则分得开）。"""
        from mt5data import ext
        stamps = pd.Series([
            pd.Timestamp("2021-01-10 18:00"),   # 美/欧均冬
            pd.Timestamp("2021-07-11 18:00"),   # 美/欧均夏
            pd.Timestamp("2021-03-21 18:00"),   # 美已切夏、欧未切
        ])
        s = ext.histdata_m1_from_bytes(
            self._zip_from_labels(stamps, 2021), symbol="EURUSD",
            downloaded_at_utc="2026-10-28T00:00:00+00:00",
            label_rule="us_wall")
        assert s.manifest["converter_version"] == 4
        assert s.manifest["label_rule"] == "us_wall"
        got = {pd.Timestamp(t).strftime("%Y-%m-%d %H:%M")
               for t in s.df["time"]}
        assert got == {"2021-01-10 23:00",   # EST −5
                       "2021-07-11 22:00",   # EDT −4
                       "2021-03-21 22:00"}   # 美墙钟已夏令 −4
        # 同一标签按 v3（欧盟日历）：错位窗内 03-21 仍 −5 → 23:00
        s3 = ext.histdata_m1_from_bytes(
            self._zip_from_labels(stamps, 2021), symbol="EURUSD",
            downloaded_at_utc="2026-10-28T00:00:00+00:00",
            label_rule="eu_calendar")
        got3 = {pd.Timestamp(t).strftime("%Y-%m-%d %H:%M")
                for t in s3.df["time"]}
        assert got3 == {"2021-01-10 23:00", "2021-07-11 22:00",
                        "2021-03-21 23:00"}

    def test_unsupported_rule_rejected(self):
        from mt5data import ext
        with pytest.raises(ext.ExtDataError, match="标签规则"):
            ext.histdata_m1_from_bytes(
                histdata_zip(week_csv("2020-01-06 00:00", "2020-01-07 00:00")),
                symbol="EURUSD",
                downloaded_at_utc="2026-10-28T00:00:00+00:00",
                label_rule="fixed_est")


class TestHistDataYearGate:
    """T87/R13 §5 逐年闸门（scripts/fetch_ext_data.py::_histdata_year_gate）：
    口径判为支持规则才入库；inconsistent 年份登记后不用；EURUSD 另加
    月末定盘锚点——T94/R14-L4 起**只有锚点 inconsistent（尖峰在错的
    小时）才排除**，undetermined 不排除（以标签规则检测为准，manifest
    注明）。用合成年度 zip 直接驱动闸门。"""

    def _year_zip(self, rule: str, year: int, *, spike_london_minute=None
                  ) -> bytes:
        """合成年度 zip；spike_london_minute=(lo, hi) 时在该伦敦本地
        分钟窗放波动尖峰（×20 幅度）——错位窗=C−1h 用于构造锚点
        inconsistent 的年份。"""
        utc = _synth_weeks_utc(year)
        labels = _labels_for_rule(utc, rule)
        local = pd.DatetimeIndex(utc).tz_localize("UTC") \
            .tz_convert("Europe/London")
        mod_min = local.hour * 60 + local.minute
        n = len(labels)
        amp = np.where((mod_min >= spike_london_minute[0])
                       & (mod_min < spike_london_minute[1]),
                       20.0, 1.0) if spike_london_minute else np.ones(n)
        ret = 1e-4 * amp * np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
        px = 1.10 * np.cumprod(1.0 + ret)
        prev = np.concatenate([[1.10], px[:-1]])
        rows = []
        for t, o, c in zip(labels, prev, px):
            rows.append(f"{t.strftime('%Y%m%d %H%M%S')};{o:.5f};"
                        f"{max(o, c):.5f};{min(o, c):.5f};{c:.5f};0")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"DAT_ASCII_EURUSD_M1_{year}.csv", "\n".join(rows))
        return buf.getvalue()

    def test_supported_rules_gated_and_converted(self):
        mod = load_script("fetch_ext_data_gate",
                          REPO_ROOT / "scripts" / "fetch_ext_data.py")
        for rule, cv in (("eu_calendar", 3), ("us_wall", 4)):
            s, rec = mod._histdata_year_gate(
                self._year_zip(rule, 2021), pair="EURUSD",
                downloaded_at_utc="2026-10-28T00:00:00+00:00",
                source_name=f"DAT_ASCII_EURUSD_M1_2021.zip")
            # 合成数据无定盘尖峰 → EURUSD 锚点 undetermined——T94/R14-L4
            # 起**不排除**：口径判对即入库（undetermined 以标签规则检测
            # 为准，manifest 注明），2014 年按此重判回收
            assert s is not None and rec["used"] is True, (rule, rec)
            assert rec["calendar"] == rule, rec
            assert rec["converter_version"] == cv
            assert rec["fix_anchor_month_end"]["status"] == "undetermined"
            assert "undetermined" in rec.get("anchor_note", ""), rec

    def test_inconsistent_anchor_year_excluded(self):
        """T94/R14-L4 失败测试：锚点 inconsistent（月末定盘窗尖峰出现在
        错的小时，C−1h）才是排除条件——合成尖峰放在伦敦 14:57–15:03
        （= 定盘窗平移 −1h）→ inconsistent → 登记后不用。"""
        mod = load_script("fetch_ext_data_gate_anchor",
                          REPO_ROOT / "scripts" / "fetch_ext_data.py")
        s, rec = mod._histdata_year_gate(
            self._year_zip("us_wall", 2021,
                           spike_london_minute=(14 * 60 + 57, 15 * 60 + 3)),
            pair="EURUSD",
            downloaded_at_utc="2026-10-28T00:00:00+00:00",
            source_name="DAT_ASCII_EURUSD_M1_2021.zip")
        assert s is None and rec["used"] is False, rec
        assert rec["calendar"] == "us_wall"        # 口径本身通过
        assert rec["fix_anchor_month_end"]["status"] == "inconsistent"
        assert rec["fix_anchor_month_end"]["location"] == "minus_1h"
        assert "inconsistent" in rec["reason"]

    def test_consistent_anchor_year_used(self):
        """尖峰锚在定盘窗（伦敦 15:57–16:03）→ consistent → 入库（锚点
        两态与 L4 规则的组合完整性）。"""
        mod = load_script("fetch_ext_data_gate_anchor2",
                          REPO_ROOT / "scripts" / "fetch_ext_data.py")
        s, rec = mod._histdata_year_gate(
            self._year_zip("us_wall", 2021,
                           spike_london_minute=(15 * 60 + 57, 16 * 60 + 3)),
            pair="EURUSD",
            downloaded_at_utc="2026-10-28T00:00:00+00:00",
            source_name="DAT_ASCII_EURUSD_M1_2021.zip")
        assert s is not None and rec["used"] is True, rec
        assert rec["fix_anchor_month_end"]["status"] == "consistent"

    def test_inconsistent_year_registered_not_used(self):
        """同年混两种规则 → 年级判 inconsistent。T94/R14-L9 起不再整年
        报废：上半年（eu_calendar/either 月）按月回收，下半年（fixed_est
        月：盛夏两支持规则恒差 1h、秋冬换日锚恒差 1h）不可回收——
        rec 保留年级判定与不可回收月份的原因。"""
        mod = load_script("fetch_ext_data_gate2",
                          REPO_ROOT / "scripts" / "fetch_ext_data.py")
        utc = _synth_weeks_utc(2021)
        first = utc[utc < pd.Timestamp("2021-07-01")]
        second = utc[utc >= pd.Timestamp("2021-07-01")]
        labels = pd.concat([_labels_for_rule(first, "eu_calendar"),
                            _labels_for_rule(second, "fixed_est")],
                           ignore_index=True)
        rows = []
        px = 1.1
        for t in labels:
            px += 0.0001
            rows.append(f"{t.strftime('%Y%m%d %H%M%S')};{px:.5f};{px:.5f};"
                        f"{px:.5f};{px:.5f};0")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("DAT_ASCII_EURUSD_M1_2021.csv", "\n".join(rows))
        s, rec = mod._histdata_year_gate(
            buf.getvalue(), pair="XAUUSD",
            downloaded_at_utc="2026-10-28T00:00:00+00:00",
            source_name="DAT_ASCII_XAUUSD_M1_2021.zip")
        assert rec["calendar"] == "inconsistent"      # 年级判定如实保留
        assert rec["used"] is True and rec["salvage"] is True
        salvaged = rec["salvaged_months"]
        # 1–6 月（eu/either）+ 11/12 月（fixed_est 在美欧皆冬时与支持
        # 规则逐分钟等价；11 月标签恰跟 eu_calendar）回收；7–10 月
        # （fixed_est 与支持规则恒差 1h 或无区分度）不回收
        assert set(salvaged) == {"2021-01", "2021-02", "2021-03", "2021-04",
                                 "2021-05", "2021-06", "2021-11", "2021-12"}
        assert salvaged["2021-03"] == "eu_calendar"
        assert salvaged["2021-11"] == "eu_calendar"
        t = pd.to_datetime(s.df["time"])
        assert not t.dt.strftime("%Y-%m").isin(["2021-07", "2021-08",
                                                "2021-09", "2021-10"]).any()

    def test_inconsistent_year_monthly_salvage_stores_months(self):
        """T94/R14-L9 失败测试：年判 inconsistent 的过渡年按月分段回收——
        盛夏三个月标签跟 fixed_est（该季 fixed_est 与支持规则恒差 1h，
        月判 inconsistent 不回收），其余月份 us_wall/either 可回收：入库
        序列只含回收月份，manifest 注明逐月判定。"""
        mod = load_script("fetch_ext_data_gate_l9",
                          REPO_ROOT / "scripts" / "fetch_ext_data.py")
        utc = _synth_weeks_utc(2021)
        us_labels = pd.to_datetime(_labels_for_rule(utc, "us_wall"))
        est_labels = pd.to_datetime(_labels_for_rule(utc, "fixed_est"))
        months = utc.month.to_numpy()
        mixed = pd.Series(us_labels)
        bad = np.isin(months, (7, 8, 9))
        mixed[bad] = est_labels[bad]
        rows = []
        px = 1.1
        for t in mixed:
            px += 0.0001
            rows.append(f"{t.strftime('%Y%m%d %H%M%S')};{px:.5f};{px:.5f};"
                        f"{px:.5f};{px:.5f};0")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("DAT_ASCII_EURUSD_M1_2021.csv", "\n".join(rows))
        s, rec = mod._histdata_year_gate(
            buf.getvalue(), pair="XAUUSD",      # 锚点闸只加给 EURUSD，隔离变量
            downloaded_at_utc="2026-10-29T00:00:00+00:00",
            source_name="DAT_ASCII_XAUUSD_M1_2021.zip")
        assert rec["used"] is True, rec
        assert rec["calendar"] == "inconsistent"      # 年级判定保持原样
        assert rec["salvage"] is True
        salvaged = rec["salvaged_months"]
        assert "2021-07" not in salvaged and "2021-08" not in salvaged
        assert salvaged.get("2021-01") in ("us_wall", "eu_calendar")
        assert salvaged.get("2021-03") == "us_wall"
        # 10 月只含单个错位日观测，两支持规则都在容差内、区分度不足 →
        # 月判 undetermined，不回收（与 TestMonthlyLabelCalendar 一致）
        assert "2021-10" not in salvaged
        assert salvaged.get("2021-11") == "us_wall"
        t = pd.to_datetime(s.df["time"])
        assert not t.dt.strftime("%Y-%m").isin(["2021-07", "2021-08",
                                                "2021-09"]).any()
        assert (t < pd.Timestamp("2021-07-01")).any()   # 回收了盛夏之外的月份

    def test_xauusd_skips_fix_anchor(self):
        """定盘锚点只加给 EURUSD（R13 §5）；XAUUSD 只过口径闸。"""
        mod = load_script("fetch_ext_data_gate3",
                          REPO_ROOT / "scripts" / "fetch_ext_data.py")
        s, rec = mod._histdata_year_gate(
            self._year_zip("eu_calendar", 2021), pair="XAUUSD",
            downloaded_at_utc="2026-10-28T00:00:00+00:00",
            source_name="DAT_ASCII_XAUUSD_M1_2021.zip")
        assert s is not None and rec["used"] is True, rec
        assert "fix_anchor_month_end" not in rec
        assert s.manifest["label_rule"] == "eu_calendar"
