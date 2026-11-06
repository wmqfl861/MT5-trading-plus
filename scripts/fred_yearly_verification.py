"""FRED 逐年核验程序（方案 A / 通道 S1；只读核验，零既有文件改动）。

设计文档（先读，本程序是其 §6 A.1/A.2 的 S1 实现）：
.superpowers/sdd/2026-11-07-r3-main-test-fix1/fred-yeargate-channel-design.md

对一对绑定的原始工件（fredgraph CSV + manifest）与目标 parquet 逐年产出
五类证据，生成 manifest.yearly_verification 条目（FRED 版 schema，键=
四位年字符串，merge.py T94/R14 年份闸门直接消费 used 布尔）：

- V1 工件完整性：raw CSV sha256 必须命中 manifest.source_files；parquet
  rows/first/last 与 manifest 声明一致（镜像 load_ext_series 的强校验）。
  任一不符 → 整体 halt（exit 2，不产出任何 used 声明）。
- V2 转换复算逐位：调既有 adapters.fred_daily_from_bytes（converter v1，
  import 使用不改）重算全序列，按自然年切片与目标 parquet 逐位比对
  （time+退化 OHLC+成交量列）→ roundtrip_identical。不一致 → 该年
  used=false。
- V3 日历形状不变量（逐年，在 parquet 年切片上测）：零周末行（周六/周日）、
  零重复日期、严格递增、年内内部洞 ≤30 天（R14-L3 口径）。节假日洞
  （首末观测日之间缺失的工作日）逐年在案，只作证据不作门。
- V4 修订对账（设计 R1 预写 fail-closed 规则）：fresh CSV 以字节参数化
  注入（--fresh-csv，**不联网**——真下载属 S2），按年逐观测比对值与日期
  集：任一 value diff → 该年 used=false；date diff 同理。未注入 → V4
  未执行 → 该年 used=false（无证据绝不 used=true）。
- V5 跨源对账：明确不实现，顶层如实记录
  cross_source = "unavailable (declared, design R5)"。

fail-closed 总原则：任何检查未执行/异常 → 该年 used=false 且 reason
说明；任何年份无完整证据链（V2 一致 + V3 不变量全过 + V4 零差）→
used=false。绝不手填 used=true（fix1 告诫 / 设计 R6）。

输出与退出码：
- stdout 恒为完整证据 JSON（status=ok|halted + yearly_verification +
  summary + checked_at_utc）；--emit-manifest-patch 额外产出 S3 消费的
  manifest 补丁 JSON（写文件或 "-" 打到 stdout，跟在证据后，以
  "=== manifest-patch ===" 分隔）；
- --dry-run：只打印不写任何文件（真实 manifest 重写属 S3，本程序不碰）；
- exit 0 = 全部在场年 used=true；1 = 完成但存在 used=false；2 = halt
  （V1 违例/输入不可读/参数不可解析——此时无任何逐年声明）。

本程序只读入参指向的工件；不写 manifest、不联网、不改动任何既有文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.ext.adapters import fred_daily_from_bytes  # noqa: E402
from mt5data.store import _COLUMNS  # noqa: E402

# ---------------------------------------------------------------- 常量

VERIFICATION_ID = "fred_d1_v1"
# 设计 A.2：顶层同步加的 label_rule（与 HistData 的 label_rule 键位对齐）
LABEL_RULE = "calendar dates（日线日期标签，无 DST 规则；逐年证据见 yearly_verification）"
# 设计 R5：无第二纳斯达克指数来源，如实登记，不假装有
CROSS_SOURCE = "unavailable (declared, design R5)"
# 主检验消费域（设计 §4：Lucca-Moench era [1994-09-27, 2021-05-17)）
DEFAULT_ERA_START = "1994-09-27"
DEFAULT_ERA_END = "2021-05-17"
# R14-L3：>30 天内部洞为年份闸门否决线；节假日洞只记录
MAX_INTERNAL_GAP_DAYS = 30

PATCH_SEPARATOR = "=== manifest-patch ==="


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- V2 工具

def _frames_bitwise_equal(a: pd.DataFrame | None,
                          b: pd.DataFrame | None) -> bool:
    """逐位（值+dtype+顺序）比对两帧；datetime64 单位差异按 ns 归一。"""
    if a is None or b is None:
        return False
    if len(a) != len(b):
        return False
    for col in _COLUMNS:
        if col not in a.columns or col not in b.columns:
            return False
        va, vb = a[col].to_numpy(), b[col].to_numpy()
        if va.dtype != vb.dtype:
            if (pd.api.types.is_datetime64_any_dtype(va)
                    and pd.api.types.is_datetime64_any_dtype(vb)):
                va = va.astype("datetime64[ns]")
                vb = vb.astype("datetime64[ns]")
            else:
                return False
        if not np.array_equal(va, vb):
            return False
    return True


def _year_slice(df: pd.DataFrame | None, year: int) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df.iloc[0:0] if df is not None else pd.DataFrame()
    return df[df["time"].dt.year == year]


# ---------------------------------------------------------------- V3 工具

def _calendar_shape(times: pd.Series) -> dict:
    """年切片的日历形状不变量（节假日洞=首末观测日间缺失的工作日数）。"""
    t = pd.Series(pd.to_datetime(times)).reset_index(drop=True)
    weekend_rows = int(((t.dt.dayofweek == 5) | (t.dt.dayofweek == 6)).sum())
    duplicate_dates = int(t.duplicated().sum())
    if len(t) >= 2:
        diffs = t.diff().dropna()
        monotonic = bool((diffs > pd.Timedelta(0)).all())
        max_internal_gap_days = int(diffs.max().days)
        observed = set(t.dt.normalize())
        bd = pd.bdate_range(t.iloc[0].normalize(), t.iloc[-1].normalize())
        holiday_gap_count = int(sum(1 for d in bd if d not in observed))
    else:
        monotonic = True
        max_internal_gap_days = 0
        holiday_gap_count = 0
    return {"weekend_rows": weekend_rows,
            "duplicate_dates": duplicate_dates,
            "monotonic": monotonic,
            "max_internal_gap_days": max_internal_gap_days,
            "holiday_gap_count": holiday_gap_count}


# ---------------------------------------------------------------- V4 工具

def _revision_diffs(bound: pd.DataFrame, fresh: pd.DataFrame) -> dict:
    """同年逐观测对账：值差（公有日期 close 不等）与日期集差（对称差）。

    fail-closed（设计 R1 预写规则）：任一 value diff → used=false；
    日期集变化同理。记录计数与首例。
    """
    b = {pd.Timestamp(x).normalize(): float(v)
         for x, v in zip(bound["time"], bound["close"])}
    f = {pd.Timestamp(x).normalize(): float(v)
         for x, v in zip(fresh["time"], fresh["close"])}
    value_diff_dates = sorted(d for d in set(b) & set(f) if b[d] != f[d])
    only_bound = sorted(set(b) - set(f))
    only_fresh = sorted(set(f) - set(b))
    first_value = ({"date": value_diff_dates[0].date().isoformat(),
                    "bound_value": b[value_diff_dates[0]],
                    "fresh_value": f[value_diff_dates[0]]}
                   if value_diff_dates else None)
    first_date = None
    if only_bound or only_fresh:
        if only_bound and (not only_fresh or only_bound[0] <= only_fresh[0]):
            first_date = {"date": only_bound[0].date().isoformat(),
                          "side": "bound_only"}
        else:
            first_date = {"date": only_fresh[0].date().isoformat(),
                          "side": "fresh_only"}
    return {"value_diffs": len(value_diff_dates),
            "date_diffs": len(only_bound) + len(only_fresh),
            "first_value_diff": first_value,
            "first_date_diff": first_date}


# ---------------------------------------------------------------- 条目生成

def _empty_calendar_shape() -> dict:
    return {"weekend_rows": 0, "duplicate_dates": 0, "monotonic": True,
            "max_internal_gap_days": 0, "holiday_gap_count": 0}


def _not_performed_revision() -> dict:
    return {"performed": False, "checked_at_utc": None,
            "fresh_csv_sha256": None, "value_diffs": None,
            "date_diffs": None, "first_value_diff": None,
            "first_date_diff": None}


def _minimal_failed_entry(reason: str) -> dict:
    """安全网条目：某年检查链异常时仍保证条目在场且 used=false。"""
    return {"used": False, "verification": VERIFICATION_ID,
            "rows": 0, "raw_rows": 0, "roundtrip_identical": False,
            "calendar_shape": _empty_calendar_shape(),
            "revision_check": _not_performed_revision(),
            "reason": reason}


def _verify_year(year: int, parquet_df: pd.DataFrame,
                 recomputed: pd.DataFrame | None, v2_error: str | None,
                 fresh_df: pd.DataFrame | None, fresh_sha: str | None,
                 v4_error: str | None, checked_at: str) -> dict:
    """单年五类证据 → 条目。fail-closed：reasons 非空即 used=false。"""
    reasons: list[str] = []
    pq = _year_slice(parquet_df, year)
    rc = _year_slice(recomputed, year)
    rows = int(len(pq))
    raw_rows = int(len(rc))

    # —— 在场性（条目年 = parquet ∪ 原始复算；单边在场的年份 fail-closed）
    if rows == 0:
        reasons.append(f"该年在目标 parquet 无行（原始复算 {raw_rows} 行）"
                       "——数据不在场")

    # —— V2 转换复算逐位
    if v2_error is not None:
        roundtrip_identical = False
        reasons.append(f"V2 转换复算未执行（异常）：{v2_error}")
    else:
        roundtrip_identical = _frames_bitwise_equal(pq, rc)
        if not roundtrip_identical:
            reasons.append("V2 转换复算逐位不一致"
                           f"（parquet {rows} 行 vs 重算 {raw_rows} 行）")

    # —— V3 日历形状不变量（在 parquet 年切片上）
    try:
        shape = _calendar_shape(pq["time"]) if rows else _empty_calendar_shape()
        if shape["weekend_rows"]:
            reasons.append(f"V3 周末行 {shape['weekend_rows']} 根"
                           "（D1 美股序列不得有周六/周日观测）")
        if shape["duplicate_dates"]:
            reasons.append(f"V3 重复日期 {shape['duplicate_dates']} 行")
        if not shape["monotonic"]:
            reasons.append("V3 日期非严格递增")
        if shape["max_internal_gap_days"] > MAX_INTERNAL_GAP_DAYS:
            reasons.append(f"V3 年内最大内部洞 "
                           f"{shape['max_internal_gap_days']} 天 > "
                           f"{MAX_INTERNAL_GAP_DAYS} 天（R14-L3）")
    except Exception as e:  # noqa: BLE001 — fail-closed 安全网
        shape = _empty_calendar_shape()
        reasons.append(f"V3 日历形状检查异常：{type(e).__name__}: {e}")

    # —— V4 修订对账（fresh vs 绑定原始的重算）
    revision = _not_performed_revision()
    if v4_error is not None:
        reasons.append(f"V4 修订对账异常（fail-closed）：{v4_error}")
    elif fresh_df is None:
        reasons.append("V4 修订对账未执行（未注入 --fresh-csv）——无证据不启用")
    elif recomputed is None:
        reasons.append("V4 修订对账未执行（绑定原始复算不可用）")
    else:
        try:
            diffs = _revision_diffs(rc, _year_slice(fresh_df, year))
            revision = {"performed": True, "checked_at_utc": checked_at,
                        "fresh_csv_sha256": fresh_sha,
                        "value_diffs": diffs["value_diffs"],
                        "date_diffs": diffs["date_diffs"],
                        "first_value_diff": diffs["first_value_diff"],
                        "first_date_diff": diffs["first_date_diff"]}
            if diffs["value_diffs"]:
                reasons.append(
                    f"V4 修订对账 value_diffs={diffs['value_diffs']}"
                    f"（任一值差→该年 used=false，设计 R1 预写规则）；首例 "
                    f"{json.dumps(diffs['first_value_diff'], ensure_ascii=False)}")
            if diffs["date_diffs"]:
                reasons.append(
                    f"V4 修订对账 date_diffs={diffs['date_diffs']}"
                    "（日期集变化同理）；首例 "
                    f"{json.dumps(diffs['first_date_diff'], ensure_ascii=False)}")
        except Exception as e:  # noqa: BLE001 — fail-closed 安全网
            reasons.append(f"V4 修订对账异常（fail-closed）："
                           f"{type(e).__name__}: {e}")

    used = not reasons
    return {"used": used,
            "verification": VERIFICATION_ID,
            "rows": rows,
            "raw_rows": raw_rows,
            "roundtrip_identical": bool(roundtrip_identical),
            "calendar_shape": shape,
            "revision_check": revision,
            "reason": None if used else "；".join(reasons)}


# ---------------------------------------------------------------- 顶层

def _era_coverage(entries: dict, era_start: pd.Timestamp,
                  era_end: pd.Timestamp) -> dict:
    """era 覆盖段提示（设计 §4：主检验消费域只需 era 内年份 used=true）。

    touched = 与 era 窗口相交且**有条目在场**的自然年（本程序逐年证据的
    覆盖范围）；era 窗口内其余日历年单独列出（信息性——真实 S2 运行
    1986-2025 全在场，该列表为空）。
    """
    def _intersects(y: int) -> bool:
        return (era_start <= pd.Timestamp(y, 12, 31)
                and pd.Timestamp(y, 1, 1) < era_end)

    touched = [y for y in sorted(entries, key=int) if _intersects(int(y))]
    used_true = [y for y in touched if entries[y].get("used") is True]
    not_used = [y for y in touched if entries[y].get("used") is not True]
    y0, y1 = int(era_start.year), int(era_end.year)
    without_entry = [str(y) for y in range(y0, y1 + 1)
                     if _intersects(y) and str(y) not in entries]
    fully = bool(touched) and not not_used
    return {"start": era_start.date().isoformat(),
            "end": era_end.date().isoformat(),
            "touched_years": touched,
            "used_true_years": used_true,
            "not_used_years": not_used,
            "era_window_years_without_entry": without_entry,
            "fully_used": fully,
            "hint": (f"era [{era_start.date()},{era_end.date()}) 在场触及 "
                     f"{len(touched)} 个自然年：used=true {len(used_true)}、"
                     f"used=false {len(not_used)}"
                     + (f"；era 窗口另有 {len(without_entry)} 个日历年无在场"
                        "条目（信息性）" if without_entry else "")
                     + ("；era 覆盖完整" if fully else "；era 覆盖不完整"))}


def _halt_evidence(checked_at: str, reason: str, argv_info: dict) -> dict:
    """V1/输入级失败：整体 halt，不产出任何 used 声明。"""
    return {"program": "fred_yearly_verification",
            "verification": VERIFICATION_ID,
            "checked_at_utc": checked_at,
            "status": "halted",
            "halt_reason": reason,
            "inputs": argv_info,
            "v1_artifact_integrity": {"match": False},
            "cross_source": CROSS_SOURCE,
            "yearly_verification": {},
            "summary": {"years_present": 0, "used_true": 0, "used_false": 0,
                        "era_coverage": None, "fresh_only_years": []}}


def verify(raw_bytes: bytes, manifest: dict, parquet_df: pd.DataFrame,
           fresh_bytes: bytes | None, *, series_id: str, symbol: str,
           era_start: pd.Timestamp, era_end: pd.Timestamp,
           checked_at: str) -> dict:
    """主核验流程（纯函数式：入参即全部输入，返回完整证据 dict）。"""
    # —— V1 工件完整性（不过 → halt 由调用方处理，这里抛 ValueError）
    raw_sha = _sha256(raw_bytes)
    files = manifest.get("source_files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files:
        raise ValueError("V1 工件完整性失败：manifest.source_files 缺失或为空")
    manifest_shas = [f.get("sha256") for f in files if isinstance(f, dict)]
    matched = next((f.get("name") for f in files if isinstance(f, dict)
                    and f.get("sha256") == raw_sha), None)
    coverage_ok, coverage_note = True, ""
    try:
        m_rows = int(manifest["rows"])
        m_first = pd.Timestamp(manifest["first_time"])
        m_last = pd.Timestamp(manifest["last_time"])
    except Exception as e:  # noqa: BLE001 — fail-closed
        raise ValueError(f"V1 工件完整性失败：manifest 覆盖字段不可解析：{e}") from e
    if len(parquet_df) != m_rows:
        coverage_ok = False
        coverage_note = (f"manifest.rows={m_rows} 与 parquet 行数 "
                         f"{len(parquet_df)} 不一致——数据与元数据不是同一份")
    elif (pd.Timestamp(parquet_df["time"].min()) != m_first
            or pd.Timestamp(parquet_df["time"].max()) != m_last):
        coverage_ok = False
        coverage_note = ("manifest 覆盖 [{} → {}] 与 parquet [{} → {}] 不一致"
                         .format(m_first, m_last,
                                 parquet_df["time"].min(),
                                 parquet_df["time"].max()))
    if matched is None:
        raise ValueError("V1 工件完整性失败：raw csv sha256=" + raw_sha
                         + " 不在 manifest.source_files（在册：" 
                         + json.dumps(manifest_shas) + "）")
    if not coverage_ok:
        raise ValueError("V1 工件完整性失败：" + coverage_note)

    # —— V2 重算（converter v1，既有适配器 import 使用不改）
    recomputed, v2_error = None, None
    try:
        recomputed = fred_daily_from_bytes(
            raw_bytes, symbol=symbol,
            downloaded_at_utc=str(manifest.get("downloaded_at_utc")
                                  or checked_at),
            series_id=series_id).df
    except Exception as e:  # noqa: BLE001 — fail-closed
        v2_error = f"{type(e).__name__}: {e}"

    # —— V4 fresh 解析（字节注入，不联网）
    fresh_df, fresh_sha, v4_error = None, None, None
    if fresh_bytes is not None:
        fresh_sha = _sha256(fresh_bytes)
        try:
            fresh_df = fred_daily_from_bytes(
                fresh_bytes, symbol=symbol,
                downloaded_at_utc=checked_at,
                series_id=series_id).df
        except Exception as e:  # noqa: BLE001 — fail-closed
            v4_error = f"{type(e).__name__}: {e}"

    # —— 条目年 = parquet ∪ 原始复算（单边在场 → fail-closed 条目）
    years: set[int] = set(parquet_df["time"].dt.year.unique())
    if recomputed is not None:
        years |= set(recomputed["time"].dt.year.unique())

    entries: dict[str, dict] = {}
    for year in sorted(years):
        try:
            entries[str(year)] = _verify_year(
                int(year), parquet_df, recomputed, v2_error, fresh_df,
                fresh_sha, v4_error, checked_at)
        except Exception as e:  # noqa: BLE001 — fail-closed 安全网
            entries[str(year)] = _minimal_failed_entry(
                f"检查异常（fail-closed）：{type(e).__name__}: {e}")

    used_true = [y for y, e in entries.items() if e["used"] is True]
    used_false = [y for y, e in entries.items() if e["used"] is not True]
    entry_years = set(entries)
    fresh_only = []
    if fresh_df is not None:
        for y in sorted(set(fresh_df["time"].dt.year.unique())):
            if str(y) not in entry_years:
                fresh_only.append(str(y))
    performed = fresh_df is not None and recomputed is not None
    summary = {
        "years_present": len(entries),
        "used_true": len(used_true),
        "used_false": len(used_false),
        "used_false_years": used_false,
        "roundtrip_identical_all": bool(
            entries and all(e["roundtrip_identical"] for e in entries.values())),
        "v4": {"performed": performed,
               "fresh_csv_sha256": fresh_sha,
               "value_diffs_total": (sum(e["revision_check"]["value_diffs"]
                                         for e in entries.values())
                                     if performed else None),
               "date_diffs_total": (sum(e["revision_check"]["date_diffs"]
                                        for e in entries.values())
                                    if performed else None),
               "error": v4_error,
               "note": ("fresh CSV 为参数化注入字节（不联网）；真实同源重下载"
                        " 属 S2 运行" if fresh_bytes is not None else
                        "未注入 fresh CSV")},
        "fresh_only_years": fresh_only,
        "era_coverage": _era_coverage(entries, era_start, era_end),
    }
    return {"program": "fred_yearly_verification",
            "verification": VERIFICATION_ID,
            "checked_at_utc": checked_at,
            "status": "ok",
            "inputs": {"series_id": series_id, "symbol": symbol.upper()},
            "v1_artifact_integrity": {
                "raw_csv_sha256": raw_sha,
                "manifest_source_files": manifest_shas,
                "matched_name": matched,
                "match": True,
                "parquet_rows": int(len(parquet_df)),
                "manifest_rows": m_rows,
                "coverage_match": True},
            "cross_source": CROSS_SOURCE,
            "yearly_verification": entries,
            "summary": summary}


def build_manifest_patch(evidence: dict, manifest: dict) -> dict:
    """S3 消费的 manifest 补丁（本模块不执行写路径）。

    只追加 yearly_verification + label_rule；usage_restriction（导出侧
    逐字校验）、converter_version=1、rows/first/last（数据零变更）等
    既有字段必须原样保留——S3 落盘时逐项核对。
    """
    return {"patch_program": "fred_yearly_verification",
            "verification": VERIFICATION_ID,
            "checked_at_utc": evidence["checked_at_utc"],
            "raw_csv_sha256": evidence["v1_artifact_integrity"]["raw_csv_sha256"],
            "target": {"source": str(manifest.get("source", "fred")),
                       "symbol": str(manifest.get("symbol", "")).upper(),
                       "timeframe": str(manifest.get("timeframe", "D1")).upper()},
            "fields_to_add": {
                "yearly_verification": evidence["yearly_verification"],
                "label_rule": LABEL_RULE},
            "fields_must_remain_unchanged": [
                "usage_restriction", "converter_version", "rows",
                "first_time", "last_time", "close_only", "source_files",
                "terms_note", "price_side", "original_timezone", "dst_rule"],
            "note": ("S3（save_ext_series 原子重写）消费；本程序不写 manifest"
                     "（通道分工见设计 §6 A.4）")}


# ---------------------------------------------------------------- CLI

def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="fred_yearly_verification.py",
        description=("FRED 逐年核验（方案 A/S1，设计 fred-yeargate-channel-"
                     "design.md §6 A.1/A.2）：V1 工件完整性 → V2 转换复算逐位"
                     " → V3 日历形状不变量 → V4 修订对账（fresh 字节注入，"
                     "不联网）；V5 跨源不实现（如实登记）。fail-closed：任何"
                     "检查未执行/异常 → 该年 used=false；无证据绝不 used=true。"
                     " stdout 恒为完整证据 JSON。"),
        epilog=("退出码：0=全部在场年 used=true；1=完成但存在 used=false；"
                "2=halt（V1 违例/输入不可读，无任何逐年声明）。\n"
                "示例：\n"
                "  python scripts/fred_yearly_verification.py "
                "--raw-csv NASDAQ100.csv --manifest manifest.json "
                "--parquet data.parquet --fresh-csv fresh.csv "
                "--emit-manifest-patch patch.json\n"
                "  python scripts/fred_yearly_verification.py ... --dry-run"
                "  # 只打印不写\n"
                "--emit-manifest-patch 不带值（或给 '-'）时补丁 JSON 打到"
                " stdout（以 " + PATCH_SEPARATOR + " 分隔）。"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raw-csv", required=True, metavar="PATH",
                   help="绑定的原始 fredgraph.csv（V1 比对 + V2 重算输入）")
    p.add_argument("--manifest", required=True, metavar="PATH",
                   help="现行 manifest.json（V1 的 sha/覆盖声明来源）")
    p.add_argument("--parquet", required=True, metavar="PATH",
                   help="目标 data.parquet（研究库或主库侧，逐年比对象）")
    p.add_argument("--fresh-csv", metavar="PATH",
                   help="V4 修订对账的 fresh CSV 字节（参数化注入，不联网；"
                        "缺省则 V4 未执行 → 各年 used=false）")
    p.add_argument("--series-id", default="NASDAQ100",
                   help="FRED 序列列名（默认 NASDAQ100）")
    p.add_argument("--symbol", default="USTEC",
                   help="仓库品种名（默认 USTEC）")
    p.add_argument("--era-start", default=DEFAULT_ERA_START,
                   help="era 覆盖段提示起点（默认 %s，主检验消费域）"
                        % DEFAULT_ERA_START)
    p.add_argument("--era-end", default=DEFAULT_ERA_END,
                   help="era 覆盖段提示终点（默认 %s，排他）" % DEFAULT_ERA_END)
    p.add_argument("--output", metavar="PATH",
                   help="证据 JSON 落盘路径（--dry-run 时不写）")
    p.add_argument("--emit-manifest-patch", nargs="?", const="-",
                   default=None, metavar="PATH|-",
                   help="产出 S3 消费的 manifest 补丁 JSON：给路径写文件，"
                        "给 '-'（或缺省值）打到 stdout")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印不写任何文件")
    return p.parse_args(argv)


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def main(argv=None) -> int:
    args = _parse_args(argv)
    checked_at = _utc_now_iso()
    inputs = {"raw_csv": str(args.raw_csv), "manifest": str(args.manifest),
              "parquet": str(args.parquet),
              "fresh_csv": str(args.fresh_csv) if args.fresh_csv else None}

    def halt(reason: str) -> int:
        print(_dump(_halt_evidence(checked_at, reason, inputs)))
        return 2

    # —— 输入工件（任何不可读 → halt，exit 2）
    try:
        raw_bytes = Path(args.raw_csv).read_bytes()
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        parquet_df = pd.read_parquet(args.parquet)
        if not isinstance(manifest, dict):
            raise ValueError(f"manifest 应为 JSON 对象，实际 "
                             f"{type(manifest).__name__}")
    except Exception as e:  # noqa: BLE001 — halt 路径
        return halt(f"输入工件不可读：{type(e).__name__}: {e}")
    fresh_bytes = None
    if args.fresh_csv:
        try:
            fresh_bytes = Path(args.fresh_csv).read_bytes()
        except Exception as e:  # noqa: BLE001 — halt 路径
            return halt(f"V4 fresh csv 不可读：{type(e).__name__}: {e}")
    try:
        era_start = pd.Timestamp(args.era_start)
        era_end = pd.Timestamp(args.era_end)
    except Exception as e:  # noqa: BLE001 — halt 路径
        return halt(f"era 参数不可解析：{type(e).__name__}: {e}")

    # —— V1→V4 主流程（V1 违例抛 ValueError；其余意外异常同样 halt，绝不 crash）
    try:
        evidence = verify(raw_bytes, manifest, parquet_df, fresh_bytes,
                          series_id=args.series_id, symbol=args.symbol,
                          era_start=era_start, era_end=era_end,
                          checked_at=checked_at)
    except Exception as e:  # noqa: BLE001 — halt 路径（V1 违例/工件形态异常）
        return halt(f"{type(e).__name__}: {e}" if not str(e).startswith("V1")
                    else str(e))

    print(_dump(evidence))
    exit_code = 0 if evidence["summary"]["used_false"] == 0 else 1

    patch_path = args.emit_manifest_patch
    patch_text = None
    if patch_path is not None:
        patch_text = _dump(build_manifest_patch(evidence, manifest))

    if not args.dry_run:
        if args.output:
            Path(args.output).write_text(_dump(evidence) + "\n",
                                         encoding="utf-8")
        if patch_path and patch_path != "-":
            Path(patch_path).write_text(patch_text + "\n", encoding="utf-8")
    if patch_path == "-":
        print(PATCH_SEPARATOR)
        print(patch_text)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
