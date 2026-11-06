"""S1（方案 A）：FRED 逐年核验程序 fred_yearly_verification 的单测。

设计文档（先读）：
.superpowers/sdd/2026-11-07-r3-main-test-fix1/fred-yeargate-channel-design.md §6 A.1/A.2/R1/R5。

契约要点（fail-closed 总原则：任何检查未执行/异常 → 该年 used=false 且
reason 说明；无证据绝不 used=true）：
- V1 工件完整性：raw CSV sha256 必须命中 manifest.source_files；parquet
  rows/first/last 与 manifest 一致——任一不符 → 整体 halt（exit 2）；
- V2 转换复算逐位：fred_daily_from_bytes 重算，按自然年切片与目标
  parquet 逐位比对（roundtrip_identical）；不一致 → used=false；
- V3 日历形状不变量：零周末行 / 零重复 / 严格递增 / 年内 ≤30 天内部洞；
  节假日洞（缺失工作日）只记录不否决；
- V4 修订对账：注入 fresh CSV 字节（不联网），按年逐观测 value/date
  diff；任一 value diff → 该年 used=false（设计 R1 预写规则）；
  未注入 → 该年 used=false（无证据不启用）；
- V5 跨源：不实现，顶层记录 cross_source=unavailable (declared, design R5)。

全部用合成 fredgraph.csv / parquet 夹具；不读真实 _raw 工件、不联网、
不写任何真实 manifest（manifest 重写属 S3）。
"""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from mt5data.ext.adapters import fred_daily_from_bytes

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "fred_yearly_verification.py"

DL_AT = "2026-11-07T00:00:00+00:00"

ENTRY_KEYS = {"used", "verification", "rows", "raw_rows", "roundtrip_identical",
              "calendar_shape", "revision_check", "reason"}
CALENDAR_KEYS = {"weekend_rows", "duplicate_dates", "monotonic",
                 "max_internal_gap_days", "holiday_gap_count"}
REVISION_KEYS = {"performed", "checked_at_utc", "fresh_csv_sha256",
                 "value_diffs", "date_diffs", "first_value_diff",
                 "first_date_diff"}
LABEL_RULE = "calendar dates（日线日期标签，无 DST 规则；逐年证据见 yearly_verification）"


# ---------------------------------------------------------------- 测试工具

def load_script():
    assert SCRIPT_PATH.exists(), "scripts/fred_yearly_verification.py 未实现"
    spec = importlib.util.spec_from_file_location(
        "fred_yearly_verification", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def close_of(d: pd.Timestamp) -> float:
    """确定性收盘价（同值在 CSV 文本、断言两侧一致）。"""
    return float(f"{1000.0 + d.dayofyear + d.dayofweek / 10.0:.2f}")


def fred_csv_bytes(years, *, exclude_ranges=(), empty_value_dates=(),
                   override=None):
    """合成 fredgraph.csv（observation_date,NASDAQ100）。

    工作日网格（周一至周五）；exclude_ranges=[(a, b)] 闭区间剔除（造
    长洞）；empty_value_dates 置空值（pandas→NaN→适配器 dropna）；
    override={date: value} 改观测值。
    """
    override = override or {}
    ranges = [(pd.Timestamp(a), pd.Timestamp(b)) for a, b in exclude_ranges]
    lines = ["observation_date,NASDAQ100"]
    for y in years:
        for d in pd.bdate_range(f"{y}-01-01", f"{y}-12-31"):
            if any(a <= d <= b for a, b in ranges):
                continue
            if d in empty_value_dates:
                v = ""
            elif d in override:
                v = f"{override[d]:.2f}"
            else:
                v = f"{close_of(d):.2f}"
            lines.append(f"{d.date().isoformat()},{v}")
    return "\n".join(lines).encode("utf-8")


def build_store(tmp_path, raw: bytes, *, df: pd.DataFrame | None = None,
                manifest_patch: dict | None = None):
    """合成待核验工件三件套（raw csv + manifest.json + data.parquet）。

    manifest 取自真实适配器（source_files sha256 = raw 的 sha）；rows/
    first/last 与最终落盘 df 同步（模拟 load_ext_series 的一致性约束）；
    manifest_patch 显式覆盖键（造 V1 违例）。
    """
    conv = fred_daily_from_bytes(raw, downloaded_at_utc=DL_AT)
    final_df = df if df is not None else conv.df
    m = dict(conv.manifest)
    m["rows"] = int(len(final_df))
    m["first_time"] = pd.Timestamp(final_df["time"].iloc[0]).isoformat()
    m["last_time"] = pd.Timestamp(final_df["time"].iloc[-1]).isoformat()
    m.update(manifest_patch or {})
    raw_p = tmp_path / "NASDAQ100.csv"
    raw_p.write_bytes(raw)
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    pq = tmp_path / "data.parquet"
    final_df.to_parquet(pq, index=False)
    return {"raw": raw_p, "manifest": mp, "parquet": pq}


def run_verify(mod, store, capsys, *, fresh=None, extra=()):
    argv = ["--raw-csv", str(store["raw"]),
            "--manifest", str(store["manifest"]),
            "--parquet", str(store["parquet"])]
    if fresh is not None:
        argv += ["--fresh-csv", str(fresh)]
    argv += [str(a) for a in extra]
    code = mod.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def write_fresh(tmp_path, raw: bytes):
    p = tmp_path / "fresh.csv"
    p.write_bytes(raw)
    return p


# ---------------------------------------------------------------- 干净路径

def test_clean_years_all_used_true_fields_complete(tmp_path, capsys):
    """干净年 → used=true，条目字段完整、数值精确（S2 预期形态）。"""
    mod = load_script()
    raw = fred_csv_bytes([1993, 1994])
    store = build_store(tmp_path, raw)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw))
    assert code == 0
    assert ev["status"] == "ok"
    assert ev["verification"] == "fred_d1_v1"
    assert ev["cross_source"] == "unavailable (declared, design R5)"
    assert ev["checked_at_utc"].startswith("20")
    # V1：sha 命中 manifest.source_files
    v1 = ev["v1_artifact_integrity"]
    assert v1["match"] is True
    assert v1["raw_csv_sha256"] == hashlib.sha256(raw).hexdigest()
    yv = ev["yearly_verification"]
    assert sorted(yv) == ["1993", "1994"]
    for y in (1993, 1994):
        e = yv[str(y)]
        assert set(e) == ENTRY_KEYS
        assert e["used"] is True
        assert e["reason"] is None
        assert e["verification"] == "fred_d1_v1"
        expected_rows = len(pd.bdate_range(f"{y}-01-01", f"{y}-12-31"))
        assert e["rows"] == expected_rows
        assert e["raw_rows"] == expected_rows
        assert e["roundtrip_identical"] is True
        cs = e["calendar_shape"]
        assert set(cs) == CALENDAR_KEYS
        assert cs["weekend_rows"] == 0
        assert cs["duplicate_dates"] == 0
        assert cs["monotonic"] is True
        assert cs["max_internal_gap_days"] == 3  # 干净网格最长洞=周末 3 天
        assert cs["holiday_gap_count"] == 0
        rc = e["revision_check"]
        assert set(rc) == REVISION_KEYS
        assert rc["performed"] is True
        assert rc["checked_at_utc"].startswith("20")
        assert rc["fresh_csv_sha256"] == hashlib.sha256(raw).hexdigest()
        assert rc["value_diffs"] == 0 and rc["date_diffs"] == 0
        assert rc["first_value_diff"] is None
        assert rc["first_date_diff"] is None
    assert ev["summary"]["used_true"] == 2
    assert ev["summary"]["used_false"] == 0


def test_holiday_and_missing_value_counted_not_vetoed(tmp_path, capsys):
    """节假日洞与空值观测只记录在案（holiday_gap_count），不否决年份。"""
    mod = load_script()
    july_wed = [d for d in pd.bdate_range("1994-07-01", "1994-07-31")
                if d.dayofweek == 2][0]
    empty_day = pd.bdate_range("1994-03-01", "1994-03-31")[10]
    raw = fred_csv_bytes([1994], exclude_ranges=[(july_wed, july_wed)],
                         empty_value_dates={empty_day})
    store = build_store(tmp_path, raw)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw))
    assert code == 0
    e = ev["yearly_verification"]["1994"]
    assert e["used"] is True
    grid = pd.bdate_range("1994-01-01", "1994-12-31")
    assert e["rows"] == e["raw_rows"] == len(grid) - 2  # 剔除 1 + dropna 1
    assert e["calendar_shape"]["holiday_gap_count"] == 2
    assert e["calendar_shape"]["max_internal_gap_days"] == 3  # 周三洞只 2 天
    assert e["reason"] is None


# ---------------------------------------------------------------- V4 修订对账

def test_fresh_value_diff_vetoes_year(tmp_path, capsys):
    """fresh 单观测值差 → 该年 used=false（R1 预写规则），diff 计数+首例。"""
    mod = load_script()
    d = [x for x in pd.bdate_range("1994-06-01", "1994-06-30")
         if x.dayofweek == 1][2]
    raw = fred_csv_bytes([1993, 1994])
    fresh_raw = fred_csv_bytes([1993, 1994], override={d: 4242.42})
    store = build_store(tmp_path, raw)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, fresh_raw))
    assert code == 1
    assert ev["yearly_verification"]["1993"]["used"] is True
    e = ev["yearly_verification"]["1994"]
    assert e["used"] is False
    rc = e["revision_check"]
    assert rc["value_diffs"] == 1
    assert rc["date_diffs"] == 0
    assert rc["fresh_csv_sha256"] == hashlib.sha256(fresh_raw).hexdigest()
    assert rc["first_value_diff"] == {
        "date": d.date().isoformat(),
        "bound_value": close_of(d),
        "fresh_value": 4242.42}
    assert "V4" in e["reason"]


def test_fresh_date_diff_vetoes_year(tmp_path, capsys):
    """fresh 日期集变化（缺一个观测日）→ 该年 used=false（日期差同理）。"""
    mod = load_script()
    d = pd.bdate_range("1993-09-01", "1993-09-30")[7]
    raw = fred_csv_bytes([1993, 1994])
    fresh_raw = fred_csv_bytes([1993, 1994],
                               exclude_ranges=[(d, d)])
    store = build_store(tmp_path, raw)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, fresh_raw))
    assert code == 1
    e = ev["yearly_verification"]["1993"]
    assert e["used"] is False
    rc = e["revision_check"]
    assert rc["date_diffs"] == 1
    assert rc["value_diffs"] == 0
    assert rc["first_date_diff"] == {"date": d.date().isoformat(),
                                     "side": "bound_only"}
    assert ev["yearly_verification"]["1994"]["used"] is True


def test_missing_fresh_csv_marks_years_not_used(tmp_path, capsys):
    """未注入 fresh CSV → V4 未执行 → 全部 used=false（无证据不启用）。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    store = build_store(tmp_path, raw)
    code, ev = run_verify(mod, store, capsys)
    assert code == 1
    e = ev["yearly_verification"]["1993"]
    assert e["used"] is False
    rc = e["revision_check"]
    assert rc["performed"] is False
    assert rc["checked_at_utc"] is None
    assert rc["fresh_csv_sha256"] is None
    assert rc["value_diffs"] is None and rc["date_diffs"] is None
    assert "V4" in e["reason"]
    assert ev["summary"]["v4"]["performed"] is False


def test_fresh_csv_garbage_no_crash(tmp_path, capsys):
    """fresh CSV 解析异常 → 该年 used=false、reason 记异常，程序不 crash。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    store = build_store(tmp_path, raw)
    garbage = tmp_path / "fresh.csv"
    garbage.write_bytes(b"not,a\nfred,csv\n")
    code, ev = run_verify(mod, store, capsys, fresh=garbage)  # 不得抛异常
    assert code == 1
    assert ev["status"] == "ok"
    e = ev["yearly_verification"]["1993"]
    assert e["used"] is False
    assert e["revision_check"]["performed"] is False
    assert "V4" in e["reason"] and "异常" in e["reason"]


# ---------------------------------------------------------------- V3 日历形状

def test_weekend_row_vetoes_year(tmp_path, capsys):
    """parquet 混入周六行 → weekend_rows=1 → 该年 used=false。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    conv = fred_daily_from_bytes(raw, downloaded_at_utc=DL_AT)
    sat = pd.date_range("1993-07-01", "1993-07-31", freq="W-SAT")[0]
    extra = conv.df.iloc[100:101].copy()
    extra["time"] = sat
    df = (pd.concat([conv.df, extra], ignore_index=True)
          .sort_values("time").reset_index(drop=True))
    store = build_store(tmp_path, raw, df=df)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw))
    e = ev["yearly_verification"]["1993"]
    assert e["used"] is False
    assert e["calendar_shape"]["weekend_rows"] == 1
    assert e["calendar_shape"]["monotonic"] is True  # 周六行按序插入
    assert "周末" in e["reason"]


def test_duplicate_dates_veto(tmp_path, capsys):
    """重复日期行 → duplicate_dates=1、非严格递增 → used=false。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    conv = fred_daily_from_bytes(raw, downloaded_at_utc=DL_AT)
    k = 100
    df = pd.concat([conv.df.iloc[:k + 1], conv.df.iloc[k:k + 1],
                    conv.df.iloc[k + 1:]], ignore_index=True)
    store = build_store(tmp_path, raw, df=df)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw))
    e = ev["yearly_verification"]["1993"]
    assert e["used"] is False
    assert e["calendar_shape"]["duplicate_dates"] == 1
    assert e["calendar_shape"]["monotonic"] is False
    assert "重复" in e["reason"]


def test_non_monotonic_veto(tmp_path, capsys):
    """行序非递增（无重复、无周末行）→ monotonic=false → used=false。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    conv = fred_daily_from_bytes(raw, downloaded_at_utc=DL_AT)
    i, j = 100, 200
    df = pd.concat([conv.df.iloc[:i], conv.df.iloc[[j]],
                    conv.df.iloc[i:j], conv.df.iloc[j + 1:]],
                   ignore_index=True)
    store = build_store(tmp_path, raw, df=df)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw))
    e = ev["yearly_verification"]["1993"]
    assert e["used"] is False
    cs = e["calendar_shape"]
    assert cs["monotonic"] is False
    assert cs["duplicate_dates"] == 0
    assert cs["weekend_rows"] == 0
    assert "递增" in e["reason"]


def test_internal_gap_over_30_days_veto(tmp_path, capsys):
    """年内 >30 天内部洞 → 该年 used=false（V2 干净，纯 V3 否决）。"""
    mod = load_script()
    raw = fred_csv_bytes([1990, 1991, 1992],
                         exclude_ranges=[("1991-04-05", "1991-05-20")])
    store = build_store(tmp_path, raw)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw))
    assert ev["yearly_verification"]["1990"]["used"] is True
    assert ev["yearly_verification"]["1992"]["used"] is True
    e = ev["yearly_verification"]["1991"]
    assert e["used"] is False
    assert e["roundtrip_identical"] is True  # parquet 与重算一致——纯 V3 否决
    assert e["calendar_shape"]["max_internal_gap_days"] >= 31
    assert "30" in e["reason"]


# ---------------------------------------------------------------- V2 转换复算

def test_roundtrip_mismatch_vetoes_year(tmp_path, capsys):
    """parquet 值被篡改 → 逐位比对不一致 → used=false（V4 零差也不救）。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    conv = fred_daily_from_bytes(raw, downloaded_at_utc=DL_AT)
    df = conv.df.copy()
    df.loc[5, "close"] = df.loc[5, "close"] + 111.11
    store = build_store(tmp_path, raw, df=df)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw))
    e = ev["yearly_verification"]["1993"]
    assert e["used"] is False
    assert e["roundtrip_identical"] is False
    assert e["revision_check"]["value_diffs"] == 0  # V4 比对 raw vs fresh，零差
    assert "V2" in e["reason"]


def test_raw_year_absent_from_parquet_fails_closed(tmp_path, capsys):
    """原始 CSV 有、目标 parquet 无的年份（如 cutoff 裁剪形态之外的多出
    年段）→ 该年 rows=0 → used=false；在场年不受牵连。"""
    mod = load_script()
    raw3 = fred_csv_bytes([1990, 1991, 1992])
    conv2 = fred_daily_from_bytes(fred_csv_bytes([1990, 1991]),
                                  downloaded_at_utc=DL_AT)
    store = build_store(tmp_path, raw3, df=conv2.df)  # sha=raw3，parquet=2 年
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw3))
    assert code == 1
    assert ev["yearly_verification"]["1990"]["used"] is True
    assert ev["yearly_verification"]["1991"]["used"] is True
    e = ev["yearly_verification"]["1992"]
    assert e["used"] is False
    assert e["rows"] == 0
    assert e["raw_rows"] > 0
    assert e["roundtrip_identical"] is False
    assert "parquet" in e["reason"]


def test_parquet_year_absent_from_raw_fails_closed(tmp_path, capsys):
    """目标 parquet 有、原始复算无的年份 → roundtrip=false → used=false。"""
    mod = load_script()
    raw2 = fred_csv_bytes([1990, 1991])
    conv3 = fred_daily_from_bytes(fred_csv_bytes([1990, 1991, 1992]),
                                  downloaded_at_utc=DL_AT)
    store = build_store(tmp_path, raw2, df=conv3.df)  # sha=raw2，parquet=3 年
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw2))
    assert code == 1
    assert ev["yearly_verification"]["1990"]["used"] is True
    assert ev["yearly_verification"]["1991"]["used"] is True
    e = ev["yearly_verification"]["1992"]
    assert e["used"] is False
    assert e["rows"] > 0
    assert e["raw_rows"] == 0
    assert e["roundtrip_identical"] is False


# ---------------------------------------------------------------- V1 工件完整性

def test_v1_sha_mismatch_halts(tmp_path, capsys):
    """raw sha 不在 manifest.source_files → 整体 halt（exit 2，无逐年条目）。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    store = build_store(tmp_path, raw, manifest_patch={
        "source_files": [{"name": "NASDAQ100.csv", "sha256": "0" * 64}]})
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw))
    assert code == 2
    assert ev["status"] == "halted"
    assert "V1" in ev["halt_reason"]
    assert ev["v1_artifact_integrity"]["match"] is False
    assert ev["yearly_verification"] == {}


def test_v1_parquet_rows_mismatch_halts(tmp_path, capsys):
    """manifest.rows 与 parquet 行数不一致（数据与元数据非同一份）→ halt。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    conv = fred_daily_from_bytes(raw, downloaded_at_utc=DL_AT)
    store = build_store(tmp_path, raw,
                        manifest_patch={"rows": int(len(conv.df)) + 5})
    code, ev = run_verify(mod, store, capsys)
    assert code == 2
    assert ev["status"] == "halted"
    assert "rows" in ev["halt_reason"]


def test_missing_input_file_halts(tmp_path, capsys):
    """输入工件不可读 → halt（exit 2），stdout 仍是可解析 JSON。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    store = build_store(tmp_path, raw)
    store["raw"].unlink()
    code, ev = run_verify(mod, store, capsys)
    assert code == 2
    assert ev["status"] == "halted"


def test_malformed_parquet_halts_no_crash(tmp_path, capsys):
    """parquet 形态异常（无 time 列）→ 整体 halt（exit 2），不 crash。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    store = build_store(tmp_path, raw)
    pd.DataFrame({"foo": [1, 2]}).to_parquet(store["parquet"], index=False)
    code, ev = run_verify(mod, store, capsys)
    assert code == 2
    assert ev["status"] == "halted"
    assert ev["yearly_verification"] == {}


# ---------------------------------------------------------------- 输出/条目生成

def test_manifest_patch_shape(tmp_path, capsys):
    """--emit-manifest-patch：S3 消费的补丁形态（逐年条目+label_rule，
    不动 usage_restriction/converter_version 的守卫清单）。"""
    mod = load_script()
    raw = fred_csv_bytes([1993, 1994])
    store = build_store(tmp_path, raw)
    patch_p = tmp_path / "patch.json"
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw),
                          extra=["--emit-manifest-patch", str(patch_p)])
    assert code == 0
    patch = json.loads(patch_p.read_text(encoding="utf-8"))
    assert patch["verification"] == "fred_d1_v1"
    assert patch["target"] == {"source": "fred", "symbol": "USTEC",
                               "timeframe": "D1"}
    add = patch["fields_to_add"]
    assert add["label_rule"] == LABEL_RULE
    assert add["yearly_verification"] == ev["yearly_verification"]
    for key in ("usage_restriction", "converter_version", "rows",
                "first_time", "last_time"):
        assert key in patch["fields_must_remain_unchanged"]


def test_dry_run_writes_nothing(tmp_path, capsys):
    """--dry-run：只打印不写文件。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    store = build_store(tmp_path, raw)
    out_p = tmp_path / "ev.json"
    patch_p = tmp_path / "patch.json"
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw),
                          extra=["--output", str(out_p),
                                 "--emit-manifest-patch", str(patch_p),
                                 "--dry-run"])
    assert code == 0
    assert not out_p.exists()
    assert not patch_p.exists()
    assert ev["status"] == "ok"


def test_output_file_written_and_matches_stdout(tmp_path, capsys):
    """非 dry-run：--output 落盘且与 stdout 逐内容一致。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    store = build_store(tmp_path, raw)
    out_p = tmp_path / "ev.json"
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, raw),
                          extra=["--output", str(out_p)])
    assert code == 0
    assert out_p.exists()
    assert json.loads(out_p.read_text(encoding="utf-8")) == ev


def test_era_coverage_summary(tmp_path, capsys):
    """汇总含 era 覆盖段提示：touched 年 used 情况如实反映。"""
    mod = load_script()
    raw = fred_csv_bytes([1993, 1994, 1995])
    d = pd.bdate_range("1994-06-01", "1994-06-30")[10]
    fresh_raw = fred_csv_bytes([1993, 1994, 1995], override={d: 1.0})
    store = build_store(tmp_path, raw)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, fresh_raw))
    assert code == 1
    era = ev["summary"]["era_coverage"]
    # 默认 era [1994-09-27, 2021-05-17)：touched = 1994、1995
    assert era["start"] == "1994-09-27" and era["end"] == "2021-05-17"
    assert era["touched_years"] == ["1994", "1995"]
    assert era["not_used_years"] == ["1994"]
    assert era["fully_used"] is False
    assert era["used_true_years"] == ["1995"]
    # era 参数化：1993 全年
    code2, ev2 = run_verify(mod, store, capsys,
                            fresh=write_fresh(tmp_path, fresh_raw),
                            extra=["--era-start", "1993-01-01",
                                   "--era-end", "1993-12-31"])
    era2 = ev2["summary"]["era_coverage"]
    assert era2["touched_years"] == ["1993"]
    assert era2["fully_used"] is True


def test_fresh_only_years_recorded_in_summary(tmp_path, capsys):
    """fresh 比 raw 多出的年段（无对应条目）→ 汇总记录，供证据链复核。"""
    mod = load_script()
    raw = fred_csv_bytes([1993])
    fresh_raw = fred_csv_bytes([1993, 1994])
    store = build_store(tmp_path, raw)
    code, ev = run_verify(mod, store, capsys,
                          fresh=write_fresh(tmp_path, fresh_raw))
    assert code == 0  # fresh 多年段不动摇 1993（在场年零差）
    assert "1994" in ev["summary"]["fresh_only_years"]
    assert sorted(ev["yearly_verification"]) == ["1993"]
