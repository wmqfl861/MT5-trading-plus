"""外部数据核心：ExtSeries、manifest 校验与 ext 库读写（T67/D36/铁律 19）。

- 统一仓库 K 线格式：naive UTC `time` + store._COLUMNS（OHLCV/spread/real_volume）；
- 落盘布局 {ext_root}/<来源>/<品种>/<周期>/data.parquet + manifest.json，
  manifest 必填字段缺一即 ExtDataError（fail-fast，绝不静默入库）；
- manifest 记录：来源与 URL、价格口径、原始时区与夏令时规则、使用条款
  摘要、下载时间、原始文件 sha256 清单、转换版本、覆盖（rows/first/last）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import pandas as pd

from mt5data.store import _COLUMNS, _atomic_replace, _fsync_file, merge_bars

# ---------------------------------------------------------------- 常量

# manifest 必填字段（缺一/为空 → ExtDataError）；rows/first_time/last_time
# 必须与落盘 DataFrame 逐项一致（防"元数据说的与数据不是同一份"）
REQUIRED_MANIFEST_FIELDS = (
    "source",            # 来源标识（histdata/cdd/fred/stooq/…）
    "source_url",        # 来源主页或数据页
    "symbol",            # 仓库标准品种名（EURUSD/XAUUSD/BTCUSD/USTEC…）
    "timeframe",         # M1/H1/D1…（规范大写）
    "price_side",        # 口径：bid / ask / mid / 成交价 / 指数收盘价…
    "original_timezone", # 原始时区描述（如 "EST (UTC-5) 固定"）
    "dst_rule",          # 夏令时规则（"无（全年 UTC-5）" 等）
    "terms_note",        # 使用条款摘要（如实，含"待用户确认"）
    "downloaded_at_utc", # ISO 时间（带时区）
    "source_files",      # [{"name": 原始文件名, "sha256": 64 位十六进制}]
    "converter_version", # 适配器版本（int，语义变更时 +1）
    "rows",              # 行数（与 DataFrame 一致）
    "first_time",        # naive UTC ISO（与数据一致）
    "last_time",
)

_HEX64 = set("0123456789abcdef")


class ExtDataError(RuntimeError):
    """manifest 缺字段/不一致、ext 库结构不对、或违反 cutoff 纪律。"""


# ---------------------------------------------------------------- 时间换算

def fixed_offset_to_utc(times: pd.Series, *, offset_hours: int) -> pd.Series:
    """固定偏移时区（不随夏令时变化）→ naive UTC。

    适用于确实**不调表**的源（UTC=标签 − offset_hours）。注意：HistData
    官方 FAQ 自称 EST 固定，但对 2020–2021 文件的实测（与 MT5/UTC 主库
    重叠期逐月按收盘价对齐）表明其标签随美东夏令时变化——HistData 适配
    器用 wall_clock_to_utc，本函数留给真正的固定偏移源。
    """
    return pd.to_datetime(times) - pd.to_timedelta(offset_hours, unit="h")


def wall_clock_to_utc(times: pd.Series, tz: str = "America/New_York"
                      ) -> pd.Series:
    """随夏令时调表的墙钟时间（naive）→ naive UTC（zoneinfo 处理切换）。

    歧义/不存在时刻的处理：秋季回拨的重复小时按夏令时一侧解释、春季
    拨快的不存在时刻前推一小时（外部行情源在这些小时通常无 bar 或每年
    至多影响一两根；一致性核对会把残余错位按月实测出来）。
    """
    s = pd.Series(pd.to_datetime(times)).dt.tz_localize(
        tz, ambiguous=True, nonexistent="shift_forward")
    return s.dt.tz_convert("UTC").dt.tz_localize(None)


def histdata_summer_window(year: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """HistData 标签空间的"夏令时窗"（欧盟 DST 日历，实测 2020–2021）。

    窗口 = [3 月最后一个周日 19:00 标签, 10 月最后一个周日 19:00 标签)。
    春季切换日 19:00–19:59 标签不存在（文件实测无此段）；秋季切换日
    19:00–19:59 标签出现两次（先夏令时侧、后冬令时侧）。
    """
    def last_sunday(y: int, month: int) -> int:
        import calendar
        last_day = calendar.monthrange(y, month)[1]
        wd = pd.Timestamp(y, month, last_day).dayofweek  # Mon=0 … Sun=6
        return last_day - ((wd + 1) % 7)
    return (pd.Timestamp(year, 3, last_sunday(year, 3), 19, 0),
            pd.Timestamp(year, 10, last_sunday(year, 10), 19, 0))


def histdata_stamps_to_utc(stamps: pd.Series) -> pd.Series:
    """HistData ASCII 时间标签 → naive UTC（实测规则，converter v3）。

    官方 FAQ 称标签为 EST（UTC-5）固定；对 2020–2021 文件与 MT5 主库
    （服务器=UTC）重叠期的逐月/逐日收盘价对齐实测结论：
    - 标签偏移为美东量级（冬 −5 / 夏 −4），但**夏令时窗按欧盟日历**
      （3 月最后一个周日 → 10 月最后一个周日）切换——等效"EET−7h"
      管道；美欧切换错位的 2–3 周里按美东墙钟会错一小时；
    - 秋季回拨日 19:00–19:59 标签出现两次：首次=夏令时侧（−4）、
      再次=冬令时侧（−5），按文件顺序区分。
    """
    labels = pd.Series(pd.to_datetime(stamps))
    summer = pd.Series(False, index=labels.index)
    for year in sorted(labels.dt.year.unique()):
        spring, fall = histdata_summer_window(int(year))
        in_year = labels.dt.year == year
        summer |= in_year & (labels >= spring) & (labels < fall)
    # 回拨窗（秋季边界后 1 小时）内重复标签：首次出现按夏令时侧
    occ = labels.groupby(labels).cumcount()
    fallback_extra = pd.Series(False, index=labels.index)
    for year in sorted(labels.dt.year.unique()):
        _, fall = histdata_summer_window(int(year))
        zone = (labels >= fall) & (labels < fall + pd.Timedelta(hours=1))
        fallback_extra |= zone & (occ == 0)
    summer = summer | fallback_extra
    delta = pd.Series(pd.Timedelta(hours=5), index=labels.index)
    delta[summer] = pd.Timedelta(hours=4)
    return labels + delta


# ---------------------------------------------------------------- ExtSeries

@dataclass(frozen=True)
class ExtSeries:
    """一条外部序列（K 线 + manifest）；df 为 naive UTC 的仓库格式。"""
    df: pd.DataFrame
    manifest: dict

    def with_timeframe(self, timeframe: str, df: pd.DataFrame) -> "ExtSeries":
        """重采样后的同一序列（M1 → H1/D1）：manifest 换周期并刷新覆盖。"""
        m = dict(self.manifest)
        m["timeframe"] = timeframe.upper()
        m["rows"] = int(len(df))
        m["first_time"] = pd.Timestamp(df["time"].iloc[0]).isoformat()
        m["last_time"] = pd.Timestamp(df["time"].iloc[-1]).isoformat()
        return ExtSeries(df=df, manifest=m)

    def replace_manifest(self, manifest: dict) -> "ExtSeries":
        return ExtSeries(df=self.df, manifest=dict(manifest))

    def replace(self, *, df: pd.DataFrame | None = None) -> "ExtSeries":
        """换数据后同步刷新覆盖字段（rows/first/last 必须与 df 一致）。"""
        new_df = df if df is not None else self.df
        m = dict(self.manifest)
        m["rows"] = int(len(new_df))
        if len(new_df):
            m["first_time"] = pd.Timestamp(new_df["time"].iloc[0]).isoformat()
            m["last_time"] = pd.Timestamp(new_df["time"].iloc[-1]).isoformat()
        return ExtSeries(df=new_df, manifest=m)

    def merged_with(self, older: "ExtSeries | None") -> "ExtSeries":
        """多年份/多次下载合并（按 time 去重，保留较新批次）。"""
        if older is None or len(older.df) == 0:
            return self
        df = merge_bars(older.df, self.df)
        m = dict(self.manifest)
        m["rows"] = int(len(df))
        m["first_time"] = pd.Timestamp(df["time"].iloc[0]).isoformat()
        m["last_time"] = pd.Timestamp(df["time"].iloc[-1]).isoformat()
        files = {f["sha256"]: f for f in older.manifest.get("source_files", [])}
        for f in m.get("source_files", []):
            files[f["sha256"]] = f
        m["source_files"] = sorted(files.values(), key=lambda f: f["name"])
        return ExtSeries(df=df, manifest=m)


# ---------------------------------------------------------------- manifest

def _sha256_hex_ok(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and set(value.lower()) <= _HEX64)


def validate_manifest(manifest: dict, *, df: pd.DataFrame | None = None) -> None:
    """必填字段齐全且非空；结构/一致性逐项校验，违例抛 ExtDataError。"""
    if not isinstance(manifest, dict):
        raise ExtDataError(f"manifest 应为 JSON 对象，实际 {type(manifest).__name__}")
    for key in REQUIRED_MANIFEST_FIELDS:
        value = manifest.get(key)
        if value is None or value == "" or value == []:
            raise ExtDataError(f"manifest 缺字段（或为空）：{key}")
    files = manifest["source_files"]
    if not isinstance(files, list):
        raise ExtDataError("manifest.source_files 必须是列表")
    for f in files:
        if (not isinstance(f, dict) or not f.get("name")
                or not _sha256_hex_ok(f.get("sha256"))):
            raise ExtDataError(
                f"manifest.source_files 元素需含非空 name 与 64 位十六进制 "
                f"sha256，实际 {f!r}")
    if not isinstance(manifest["converter_version"], int) \
            or manifest["converter_version"] < 1:
        raise ExtDataError("manifest.converter_version 须为 ≥1 的整数")
    try:
        rows = int(manifest["rows"])
        first = pd.Timestamp(manifest["first_time"])
        last = pd.Timestamp(manifest["last_time"])
    except (TypeError, ValueError) as e:
        raise ExtDataError(f"manifest 覆盖字段不可解析：{e}") from e
    if rows < 1:
        raise ExtDataError("manifest.rows 须 ≥1")
    if df is not None:
        if rows != len(df):
            raise ExtDataError(
                f"manifest.rows={rows} 与数据行数 {len(df)} 不一致")
        if len(df) == 0:
            raise ExtDataError("空序列拒绝入库（rows ≥1）")
        if first != pd.Timestamp(df["time"].min()) \
                or last != pd.Timestamp(df["time"].max()):
            raise ExtDataError(
                f"manifest 覆盖 [{first} → {last}] 与数据 "
                f"[{df['time'].min()} → {df['time'].max()}] 不一致")


# ---------------------------------------------------------------- ext 库读写

def _ext_dir(ext_root, source: str, symbol: str, timeframe: str) -> str:
    return os.path.join(str(ext_root), source, symbol, timeframe.upper())


def save_ext_series(ext_root, series: ExtSeries) -> str:
    """校验 manifest → 原子写 data.parquet + manifest.json；返回目录。

    目录布局 {ext_root}/<来源>/<品种>/<周期>/；data.parquet 列与
    mt5data.store 一致（time naive UTC）。
    """
    df = series.df[_COLUMNS].copy()
    df["time"] = pd.to_datetime(df["time"]).astype("datetime64[ns]")
    validate_manifest(series.manifest, df=df)
    d = _ext_dir(ext_root, series.manifest["source"],
                 series.manifest["symbol"], series.manifest["timeframe"])
    os.makedirs(d, exist_ok=True)
    parquet_tmp = os.path.join(d, "data.parquet.tmp")
    df.to_parquet(parquet_tmp, index=False)
    _fsync_file(parquet_tmp)
    _atomic_replace(parquet_tmp, os.path.join(d, "data.parquet"))
    mtmp = os.path.join(d, "manifest.json.tmp")
    with open(mtmp, "w", encoding="utf-8") as f:
        json.dump(series.manifest, f, ensure_ascii=False, indent=2)
    _atomic_replace(mtmp, os.path.join(d, "manifest.json"))
    return d


def load_ext_series(ext_root, source: str, symbol: str,
                    timeframe: str) -> tuple[pd.DataFrame, dict] | None:
    """读一条 ext 序列；manifest 缺字段同样拒绝（读取侧 fail-fast）。

    目录不存在 → None；有数据无 manifest/manifest 坏 → ExtDataError。
    """
    d = _ext_dir(ext_root, source, symbol, timeframe)
    p = os.path.join(d, "data.parquet")
    if not os.path.exists(p):
        return None
    mp = os.path.join(d, "manifest.json")
    if not os.path.exists(mp):
        raise ExtDataError(f"{d} 有 data.parquet 却没有 manifest.json——"
                           "来源与版本不明，拒绝读取（先重建或补 manifest）")
    with open(mp, encoding="utf-8") as f:
        manifest = json.load(f)
    validate_manifest(manifest)
    df = pd.read_parquet(p)
    if len(df) != int(manifest["rows"]):
        raise ExtDataError(
            f"{d}: manifest.rows={manifest['rows']} 与 parquet 行数 "
            f"{len(df)} 不一致——数据与元数据不是同一份")
    return df, manifest


def ext_inventory(ext_root) -> list[dict]:
    """扫 {ext_root}/<来源>/<品种>/<周期>/manifest.json → 序列清单。"""
    root = str(ext_root)
    out: list[dict] = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in sorted(os.walk(root)):
        if "manifest.json" not in filenames:
            continue
        with open(os.path.join(dirpath, "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
        validate_manifest(m)
        out.append(m)
    out.sort(key=lambda m: (m["source"], m["symbol"], m["timeframe"]))
    return out


# ---------------------------------------------------------------- cutoff 纪律

def slice_before_cutoff(df: pd.DataFrame, cutoff) -> pd.DataFrame:
    """cutoff 纪律（铁律 19）：外部数据只允许 cutoff 之前部分进研究路径。"""
    return df[df["time"] < pd.Timestamp(cutoff)].reset_index(drop=True)


def assert_within_cutoff(df: pd.DataFrame, cutoff, label: str) -> None:
    """硬校验：任何 time ≥ cutoff 即 ExtDataError（导出后自检用）。"""
    if len(df) and pd.Timestamp(df["time"].max()) >= pd.Timestamp(cutoff):
        raise ExtDataError(
            f"{label}: last_time={df['time'].max()} ≥ cutoff="
            f"{pd.Timestamp(cutoff)}——外部数据混入锁箱期，拒绝")


# ---------------------------------------------------------------- 下载工具

def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
