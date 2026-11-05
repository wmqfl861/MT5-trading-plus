"""Synthetic-only acceptance tests; no business readers or real snapshots."""
from contextlib import contextmanager
from datetime import date
import ctypes
from ctypes import wintypes as wt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/h_time_audit.py"
BASE = 1167609600000000000  # 2007-01-01 UTC, independent integer fixture
HOUR = 3600000000000
DAY = 86400000000000
PRICE = 987654321.125
HIST = dict(source="histdata", symbol="EURUSD", timeframe="H1",
            timestamp_semantics="utc_bar_open", start="2007-01-01T00:00:00Z",
            end="2007-01-01T06:00:00+00:00")
FRED = dict(source="fred", symbol="USTEC", timeframe="D1",
            timestamp_semantics="fred_observation_date", start="2006-11-22",
            end="2021-05-17")
MT5 = dict(source="mt5", symbol="USTEC", timeframe="D1",
           timestamp_semantics="utc_bar_open", start="2006-11-22T00:00:00Z",
           end="2021-05-17T00:00:00Z")


def load():
    assert SCRIPT.is_file(), "H time projector is not implemented"
    spec = importlib.util.spec_from_file_location("h_time_audit_test_subject", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_file(tmp_path, values=None, dtype=None, *, name="artificial.parquet", **kwargs):
    if values is None:
        values = [BASE - HOUR, BASE, BASE + 2 * HOUR + 1, BASE + HOUR + 2,
                  BASE + 2 * HOUR + 1, BASE + 4 * HOUR, BASE + 6 * HOUR, BASE + 3 * HOUR]
    arr = pa.array(values, type=dtype or pa.timestamp("ns"))
    table = pa.table({"price": [PRICE] * len(arr), "time": arr}).replace_schema_metadata(
        {b"pandas": b"invalid-json-PRICE-METADATA-must-not-be-consumed"})
    path = tmp_path / name
    pq.write_table(table, path, row_group_size=3, **kwargs)
    return path


def cli_args(path, binding=HIST):
    args = ["--path", str(path)]
    for key, value in binding.items():
        args += ["--" + key.replace("_", "-"), value]
    return args


def cold(args, code=None):
    cmd = [sys.executable, "-I", "-B"]
    cmd += ["-c", code, str(SCRIPT)] if code else [str(SCRIPT)]
    return subprocess.run(cmd + args, cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"},
                          capture_output=True, text=True, timeout=45)


def successful(proc):
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert proc.stderr == ""
    assert len(proc.stdout.splitlines()) == 1
    return json.loads(proc.stdout)


def refusal(module, path, code, binding=HIST):
    with pytest.raises(module.AuditError) as caught:
        module.audit_time_file(str(path), **binding)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert str(path) not in str(caught.value)


def assert_physical_result(result):
    assert result["selected_count"] == 6
    assert result["scanned_count"] == 8
    assert result["unique_count"] == 5
    assert result["duplicate_count"] == 1
    assert result["descending_count"] == 2
    assert result["h1_off_grid_count"] == 3
    assert result["min_time"] == "2007-01-01T00:00:00.000000000Z"
    assert result["max_time"] == "2007-01-01T04:00:00.000000000Z"
    assert result["positive_interval_count"] == 3
    assert result["details_truncated"] is False
    assert result["interval_details"] == [
        {"start": "2007-01-01T00:00:00.000000000Z", "end": "2007-01-01T02:00:00.000000001Z", "duration_ns": 7200000000001},
        {"start": "2007-01-01T01:00:00.000000002Z", "end": "2007-01-01T02:00:00.000000001Z", "duration_ns": 3599999999999},
        {"start": "2007-01-01T02:00:00.000000001Z", "end": "2007-01-01T04:00:00.000000000Z", "duration_ns": 7199999999999},
    ]
    assert result["max_positive_interval"] == result["interval_details"][0]
    assert result["start"] == "2007-01-01T00:00:00.000000000Z"
    assert result["end"] == "2007-01-01T06:00:00.000000000Z"
    assert result["series"] == "EURUSD_H1"
    assert result["kind"] == "time_projection_diagnostic"
    assert result["research_status"] == "not_evaluated"
    assert result["execution_status"] == "not_released"
    assert result["snapshot_identity"] == "unverified"
    assert result["statistics_scope"] == "selected_rows_in_physical_order"
    assert result["time_domain"] == "utc"
    assert result["input_time_type"] == "timestamp[ns]"
    assert result["read_scope"]["decoded_columns"] == ["time"]
    assert result["read_scope"]["time_rows"] == "entire_file_before_selection"
    assert result["read_scope"]["zero_price_byte_io_guaranteed"] is False
    assert result["read_scope"]["caller_declarations"] == "unverified"
    text = json.dumps(result)
    for forbidden in ("approved_by", "PASS", "missing_intervals", "coverage_year", str(PRICE)):
        assert forbidden not in text


def test_cold_cli_real_parquet_physical_order_and_nanoseconds(tmp_path):
    assert pa.__version__ == "26.0.0"
    path = write_file(tmp_path)
    before = path.read_bytes()
    assert_physical_result(successful(cold(cli_args(path))))
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_small_batches_price_independence_and_no_input_writes(tmp_path):
    module = load()
    module._BATCH_SIZE = 2
    path = write_file(tmp_path)
    other = tmp_path / "price-changed.parquet"
    # Test fixture construction may read all columns; the projector may not.
    table = pq.read_table(path, use_pandas_metadata=False)
    table = table.set_column(0, "price", pa.array([-123456789.75] * table.num_rows))
    pq.write_table(table, other, row_group_size=2)
    before = {p.name: p.read_bytes() for p in (path, other)}
    result = module.audit_time_file(str(path), **HIST)
    assert_physical_result(result)
    assert module.audit_time_file(str(other), **HIST) == result
    assert {p.name: p.read_bytes() for p in (path, other)} == before


@pytest.mark.parametrize("values,want", [([], "no_observations"), ([BASE - HOUR, BASE + 6 * HOUR], "no_observations")])
def test_empty_selection(tmp_path, values, want):
    module = load()
    result = module.audit_time_file(str(write_file(tmp_path, values)), **HIST)
    assert result["status"] == want
    for key in ("selected_count", "unique_count", "duplicate_count", "descending_count", "positive_interval_count", "h1_off_grid_count"):
        assert result[key] == 0
    for key in ("min_time", "max_time", "max_positive_interval"):
        assert result[key] is None
    assert result["interval_details"] == []
    assert result["details_truncated"] is False


def test_long_interval_details_cap_and_late_maximum(tmp_path):
    values = [BASE + n for n in range(26)] + [BASE + HOUR]
    result = load().audit_time_file(str(write_file(tmp_path, values)), **HIST)
    assert result["positive_interval_count"] == 26
    assert len(result["interval_details"]) == 20
    assert result["details_truncated"] is True
    assert result["max_positive_interval"]["duration_ns"] == HOUR - 25


def test_descending_equal_only_has_no_positive_interval(tmp_path):
    result = load().audit_time_file(str(write_file(tmp_path, [BASE + 2, BASE + 1, BASE + 1])), **HIST)
    assert result["descending_count"] == 1
    assert result["duplicate_count"] == 1
    assert result["positive_interval_count"] == 0
    assert result["max_positive_interval"] is None


@pytest.mark.parametrize("dtype,values,read_type", [
    (pa.timestamp("ns"), [BASE, BASE + DAY], "timestamp[ns]"),
    (pa.date32(), [date(2007, 1, 1), date(2007, 1, 2)], "date32[day]"),
    (pa.date64(), [date(2007, 1, 1), date(2007, 1, 2)], "date32[day]"),
])
def test_fred_real_readback_including_production_naive_ns(tmp_path, dtype, values, read_type):
    path = write_file(tmp_path, values, dtype)
    result = successful(cold(cli_args(path, FRED)))
    assert result["input_time_type"] == read_type
    assert result["time_domain"] == "observation_date"
    assert result["series"] == "USTEC_D1"
    assert result["min_time"] == "2007-01-01"
    assert result["max_time"] == "2007-01-02"
    assert result["h1_off_grid_count"] is None
    assert result["interval_details"] == [{"start": "2007-01-01", "end": "2007-01-02", "duration_days": 1}]
    assert result["start"] == "2006-11-22"


def test_date64_raw_milliseconds_cannot_hide_partial_days():
    # Parquet DATE is INT32 days: its writer discards date64 subday bits.
    # Exercise the decoded-array validation boundary for date64 itself as well.
    module = load()
    valid = pa.array([1167609600000], type=pa.int64()).cast(pa.date64())
    assert module._time_values(valid, "fred") == [13514]
    invalid = pa.array([1167609600001], type=pa.int64()).cast(pa.date64())
    with pytest.raises(module.AuditError, match="time_refused"):
        module._time_values(invalid, "fred")


@pytest.mark.parametrize("unit,scale", [("s", 1000000000), ("ms", 1000000), ("us", 1000), ("ns", 1)])
@pytest.mark.parametrize("tz", [None, "UTC"])
def test_mt5_non_midnight_and_native_timestamp_units(tmp_path, unit, scale, tz):
    path = write_file(tmp_path, [(BASE + HOUR) // scale], pa.timestamp(unit, tz=tz))
    result = load().audit_time_file(str(path), **MT5)
    assert result["min_time"] == "2007-01-01T01:00:00.000000000Z"
    assert result["selected_count"] == 1
    assert result["h1_off_grid_count"] is None


@pytest.mark.parametrize("dtype,values,binding,code", [
    (pa.timestamp("ns"), [BASE, None], HIST, "time_refused"),
    (pa.timestamp("ns"), [BASE, BASE - DAY + 1], FRED, "time_refused"),
    (pa.timestamp("ns", tz="UTC"), [BASE], FRED, "schema_refused"),
    (pa.timestamp("us"), [BASE // 1000], FRED, "schema_refused"),
    (pa.timestamp("ns", tz="Europe/London"), [BASE], HIST, "schema_refused"),
    (pa.timestamp("ns", tz="+00:00"), [BASE], HIST, "schema_refused"),
    (pa.string(), ["2007-01-01"], FRED, "schema_refused"),
    (pa.int64(), [BASE], HIST, "schema_refused"),
    (pa.date32(), [date(2007, 1, 1)], HIST, "schema_refused"),
    (pa.timestamp("us"), [BASE // 1000, 9223372036854775807], HIST, "time_refused"),
    (pa.date32(), [13514, 2147483647], FRED, "time_refused"),
])
def test_bad_schema_and_labels_even_outside_selection(tmp_path, dtype, values, binding, code):
    module = load()
    narrow = dict(binding)
    if binding == FRED:
        narrow.update(start="2007-01-01", end="2007-01-02")
    path = write_file(tmp_path, values, dtype)
    refusal(module, path, code, narrow)


@pytest.mark.parametrize("shape", ["missing", "duplicate", "struct", "list", "nested_elsewhere", "prefix_collision", "int96", "external"])
def test_real_schema_projection_refusal_before_decode(tmp_path, monkeypatch, shape):
    module = load()
    a = pa.array([BASE], type=pa.timestamp("ns"))
    if shape == "missing":
        table = pa.table({"price": [PRICE]})
    elif shape == "duplicate":
        table = pa.Table.from_arrays([a, a], names=["time", "time"])
    elif shape == "struct":
        table = pa.table({"time": pa.StructArray.from_arrays([a], names=["value"])})
    elif shape == "list":
        table = pa.table({"time": pa.array([[BASE]], type=pa.list_(pa.int64()))})
    elif shape == "nested_elsewhere":
        table = pa.table({"time": a, "other": pa.StructArray.from_arrays([a], names=["time"])})
    elif shape == "prefix_collision":
        table = pa.Table.from_arrays([a, a], names=["time", "time.extra"])
    else:
        table = pa.table({"time": a, "price": [PRICE]})
    path = tmp_path / "schema.parquet"
    if shape == "external":
        metadata = []
        pq.write_table(table, path, metadata_collector=metadata)
        metadata[0].set_file_path("private-price-other-file.parquet")
        metadata[0].write_metadata_file(path)
    else:
        pq.write_table(table, path, use_deprecated_int96_timestamps=shape == "int96")
    def forbidden(*args, **kwargs):
        pytest.fail("schema rejection must precede decoding")
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", forbidden)
    refusal(module, path, "schema_refused")


@pytest.mark.parametrize("change,code", [
    ({"source": "other"}, "declaration_refused"), ({"symbol": "USTEC"}, "declaration_refused"),
    ({"timeframe": "D1"}, "declaration_refused"), ({"timestamp_semantics": "close_only"}, "declaration_refused"),
    ({"source": None}, "declaration_refused"), ({"symbol": 1}, "declaration_refused"),
    ({"start": "2007-01-01"}, "bounds_refused"), ({"start": "2007-01-01T00:00:00"}, "bounds_refused"),
    ({"start": "2007-01-01T00:00:00+01:00"}, "bounds_refused"),
    ({"start": "2007-01-01T00:00:00.0000000001Z"}, "bounds_refused"),
    ({"start": "2007-02-30T00:00:00Z"}, "bounds_refused"),
    ({"start": "2006-12-31T23:59:59Z"}, "bounds_refused"),
    ({"end": "2021-05-17T00:00:00.000000001Z"}, "bounds_refused"),
    ({"start": "2007-01-01T06:00:00Z"}, "bounds_refused"),
    ({"end": "2007-01-01T00:00:00Z"}, "bounds_refused"),
    ({"start": 1167609600}, "bounds_refused"),
])
def test_declarations_and_boundaries_refused_before_any_path_access(tmp_path, monkeypatch, change, code):
    module = load()
    def forbidden(*args, **kwargs):
        pytest.fail("input path accessed before declaration validation")
    monkeypatch.setattr(module, "_open_local", forbidden)
    refusal(module, tmp_path / "private.parquet", code, {**HIST, **change})


@pytest.mark.parametrize("start,end", [("2006-11-21", "2007-01-01"), ("2007-01-01", "2021-05-18"),
                                       ("2007-01-01T00:00:00Z", "2007-01-02"), ("2007-01-01", "2007-01-01")])
def test_fred_date_bounds_before_open(tmp_path, monkeypatch, start, end):
    module = load()
    monkeypatch.setattr(module, "_open_local", lambda *a: pytest.fail("unexpected input open"))
    refusal(module, tmp_path / "private.parquet", "bounds_refused", {**FRED, "start": start, "end": end})


def test_nanosecond_half_open_bounds(tmp_path):
    path = write_file(tmp_path, [BASE, BASE + 1, BASE + 2, BASE + 3])
    result = load().audit_time_file(str(path), **{**HIST, "start": "2007-01-01T00:00:00.000000001+00:00", "end": "2007-01-01T00:00:00.000000003Z"})
    assert result["selected_count"] == 2
    assert result["min_time"] == "2007-01-01T00:00:00.000000001Z"
    assert result["max_time"] == "2007-01-01T00:00:00.000000002Z"


@pytest.mark.parametrize("path", [
    "relative.parquet", "C:relative.parquet", "/root/file.parquet", "https://private/file.parquet",
    "file:///C:/private.parquet", "\\\\server\\share\\file.parquet", "//server/share/file.parquet",
    "\\\\?\\C:\\private.parquet", "\\\\.\\PhysicalDrive0", "C:/private.parquet:stream", "C:/a/*/file.parquet",
    "C:/a/?/file.parquet", "C:/a/[xy]/file.parquet", "C:/a/../file.parquet", "C:/a/./file.parquet",
    "C:/a//file.parquet", "C:/a./file.parquet", "C:/a /file.parquet", "C:/CON.parquet", "C:/NUL",
    "C:/COM1.txt", "C:/lpt9/file.parquet", "C:/COM¹.txt", "C:/CONIN$", "C:/CONOUT$", "C:/a\x00b.parquet",
    "C:/a\nb.parquet", "C:/file.parquet/", "C:/", "C:/a|b.parquet", "C:/file.parquet ",
])
def test_lexical_paths_never_reach_os_open(monkeypatch, path):
    module = load()
    monkeypatch.setattr(module, "_open_local", lambda *a: pytest.fail("lexical path reached OS open"))
    refusal(module, path, "path_refused")


def test_api_path_must_be_string(tmp_path, monkeypatch):
    module = load()
    monkeypatch.setattr(module, "_open_local", lambda *a: pytest.fail("bad type reached OS open"))
    with pytest.raises(module.AuditError, match="path_refused"):
        module.audit_time_file(tmp_path / "x.parquet", **HIST)


def test_one_content_handle_fixed_projection_no_statistics(tmp_path, monkeypatch):
    module = load()
    module._BATCH_SIZE = 2
    path = write_file(tmp_path)
    actual_open = module._open_local
    actual_pf = pq.ParquetFile
    handles, calls, reads, fstats = [], [], [], []
    actual_fstat = os.fstat

    class FileSpy:
        def __init__(self, raw): self.raw = raw
        def __getattr__(self, name): return getattr(self.raw, name)
        def read(self, size=-1):
            assert size >= 0, "no unbounded whole-file read"
            reads.append(size)
            return self.raw.read(size)

    @contextmanager
    def opened(p):
        with actual_open(p) as raw:
            spy = FileSpy(raw)
            handles.append(spy)
            yield spy

    class Column:
        def __init__(self, real): self.real = real
        @property
        def file_path(self): return self.real.file_path
        def __getattr__(self, name): pytest.fail("column metadata consumption: " + name)

    class Group:
        def __init__(self, real): self.real = real
        def column(self, index):
            assert index == 1, "price metadata must not be consumed"
            return Column(self.real.column(index))

    class Metadata:
        def __init__(self, real): self.real = real
        @property
        def num_row_groups(self): return self.real.num_row_groups
        def row_group(self, i): return Group(self.real.row_group(i))
        def __getattr__(self, name): pytest.fail("full metadata consumption: " + name)

    class Projector:
        def __init__(self, source, **kw):
            assert source is handles[0]
            assert kw == dict(memory_map=False, buffer_size=0, pre_buffer=False)
            self.real = actual_pf(source, **kw)
            self.schema = self.real.schema
            self.schema_arrow = self.real.schema_arrow
            self.metadata = Metadata(self.real.metadata)
        def iter_batches(self, **kw):
            assert kw == dict(batch_size=2, row_groups=[len(calls)], columns=["time"],
                              use_threads=False, use_pandas_metadata=False)
            calls.append(kw)
            return self.real.iter_batches(**kw)
        def close(self): self.real.close()
        def __getattr__(self, name): pytest.fail("unapproved Parquet read API: " + name)

    def fstat(fd):
        assert fd == handles[0].fileno()
        fstats.append(fd)
        return actual_fstat(fd)

    monkeypatch.setattr(module, "_open_local", opened)
    monkeypatch.setattr(module.os, "fstat", fstat)
    monkeypatch.setattr(pq, "ParquetFile", Projector)
    monkeypatch.setattr(pq, "read_table", lambda *a, **k: pytest.fail("whole table read"))
    result = module.audit_time_file(str(path), **HIST)
    assert_physical_result(result)
    assert len(handles) == 1 and handles[0].closed
    assert len(calls) == 3 and len(fstats) == 2 and reads


def test_validation_finishes_before_statistics(tmp_path, monkeypatch):
    module = load()
    path = write_file(tmp_path, [BASE, BASE + 1, None])
    monkeypatch.setattr(module, "_statistics", lambda *a, **k: pytest.fail("selected before full validation"))
    refusal(module, path, "time_refused")


def test_detectable_same_handle_change_discards_output(tmp_path, monkeypatch):
    module = load()
    path = write_file(tmp_path)
    actual = module.os.fstat
    count = 0
    class Changed:
        def __init__(self, original): self.original = original
        def __getattr__(self, name): return getattr(self.original, name)
        @property
        def st_mtime_ns(self): return self.original.st_mtime_ns + 1
    def observed(fd):
        nonlocal count
        count += 1
        s = actual(fd)
        return s if count == 1 else Changed(s)
    monkeypatch.setattr(module.os, "fstat", observed)
    refusal(module, path, "input_changed")
    assert count == 2


def test_real_corrupt_file_sanitized_cli(tmp_path):
    path = tmp_path / "PRIVATE-PRICE-987654321.125.parquet"
    path.write_bytes(b"PRIVATE_ORIGINAL_EXCEPTION_987654321.125")
    proc = cold(cli_args(path))
    assert proc.returncode != 0
    assert proc.stderr == ""
    assert json.loads(proc.stdout) == {"error": {"code": "parquet_refused"}}
    assert "PRIVATE" not in proc.stdout


@pytest.mark.parametrize("variant", ["unknown", "missing", "bad_value", "abbreviation", "duplicate", "output", "allow", "help"])
def test_cli_argument_errors_are_single_sanitized_json(tmp_path, variant):
    path = tmp_path / "PRIVATE-987654321.125.parquet"
    args = cli_args(path)
    if variant == "unknown": args += ["--PRIVATE-987654321.125", str(path)]
    if variant == "missing": args = ["--path", str(path)]
    if variant == "bad_value": args[-1] = str(path)
    if variant == "abbreviation": args[0] = "--pa"
    if variant == "duplicate": args += ["--path", str(path)]
    if variant == "output": args += ["--output", str(path)]
    if variant == "allow": args += ["--allow-real", str(path)]
    if variant == "help": args = ["--help", str(path)]
    proc = cold(args)
    assert proc.returncode != 0 and proc.stderr == ""
    assert len(proc.stdout.splitlines()) == 1
    assert json.loads(proc.stdout) == {"error": {"code": "bounds_refused" if variant == "bad_value" else "arguments_refused"}}
    assert "PRIVATE" not in proc.stdout and "987654321" not in proc.stdout


def kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
    k.CreateFileW.restype = wt.HANDLE
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.CloseHandle.restype = wt.BOOL
    return k


def test_windows_fixed_ancestors_and_file_stay_locked_until_return(tmp_path, monkeypatch):
    assert os.name == "nt", "this acceptance run requires Windows local handles"
    module = load()
    path = write_file(tmp_path)
    real_pf = pq.ParquetFile
    def observed(*a, **kw):
        with pytest.raises(OSError): tmp_path.rename(tmp_path.with_name(tmp_path.name + "-moved"))
        with pytest.raises(OSError): path.rename(path.with_name("moved.parquet"))
        with pytest.raises(OSError): path.write_bytes(b"replacement")
        return real_pf(*a, **kw)
    monkeypatch.setattr(pq, "ParquetFile", observed)
    assert module.audit_time_file(str(path), **HIST)["selected_count"] == 6
    path.rename(path.with_name("after-close.parquet"))


@pytest.mark.parametrize("kind", ["directory", "missing", "file_symlink", "ancestor_symlink", "junction"])
def test_windows_nonregular_and_reparse_refused_before_parquet(tmp_path, monkeypatch, kind):
    module = load()
    path = write_file(tmp_path)
    if kind == "directory": path = tmp_path
    if kind == "missing": path = tmp_path / "does-not-exist.parquet"
    if kind == "file_symlink":
        link = tmp_path / "file-link.parquet"
        link.symlink_to(path)
        path = link
    if kind == "ancestor_symlink":
        link = tmp_path / "directory-link"
        link.symlink_to(tmp_path, target_is_directory=True)
        path = link / path.name
    if kind == "junction":
        link = tmp_path / "junction"
        # Only the artificial test directory is a junction target.
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path)],
                              capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr
        path = link / path.name
    monkeypatch.setattr(pq, "ParquetFile", lambda *a, **k: pytest.fail("reparse/type refusal must precede content read"))
    try:
        refusal(module, path, "path_refused")
    finally:
        if kind == "junction": os.rmdir(link)
        elif kind in ("file_symlink", "ancestor_symlink"): link.unlink()


def test_windows_sharing_denial_is_path_refused(tmp_path):
    module = load()
    path = write_file(tmp_path)
    k = kernel()
    h = k.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
    assert h != ctypes.c_void_p(-1).value
    try:
        refusal(module, path, "path_refused")
    finally:
        assert k.CloseHandle(h)


@pytest.mark.parametrize("subject", ["file", "ancestor"])
def test_windows_actual_read_permission_denial(tmp_path, monkeypatch, subject):
    module = load()
    path = write_file(tmp_path)
    denied = path if subject == "file" else tmp_path
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.GetFileSecurityW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    adv.GetFileSecurityW.restype = wt.BOOL
    adv.SetFileSecurityW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.LPVOID]
    adv.SetFileSecurityW.restype = wt.BOOL
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(wt.LPVOID), ctypes.POINTER(wt.DWORD)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
    needed = wt.DWORD()
    adv.GetFileSecurityW(str(denied), 4, None, 0, ctypes.byref(needed))
    original = ctypes.create_string_buffer(needed.value)
    assert adv.GetFileSecurityW(str(denied), 4, original, needed, ctypes.byref(needed))
    sd = wt.LPVOID()
    assert adv.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(D;;FR;;;WD)(A;;FA;;;BA)", 1, ctypes.byref(sd), None)
    k = kernel()
    k.LocalFree.argtypes = [wt.LPVOID]
    k.LocalFree.restype = wt.LPVOID
    try:
        assert adv.SetFileSecurityW(str(denied), 4, sd)
        monkeypatch.setattr(pq, "ParquetFile", lambda *a, **kw: pytest.fail("permission denied before content decode"))
        refusal(module, path, "path_refused")
    finally:
        assert adv.SetFileSecurityW(str(denied), 4, original)
        k.LocalFree(sd)


GUARDED_COLD = r'''
import sys, os, runpy
script = sys.argv[1]
sys.argv = sys.argv[1:]
allowed_roots = (os.path.normcase(sys.base_prefix) + os.sep, os.path.normcase(sys.prefix) + os.sep)
# PyArrow imports socket; actual socket/network calls remain forbidden below.
forbidden = ("mt5data", "MetaTrader5", "sqlite3", "duckdb", "requests", "http.client", "urllib.request", "pandas")
def guard(event, args):
    if event == "import" and any(args[0] == n or args[0].startswith(n + ".") for n in forbidden):
        raise RuntimeError("forbidden business/network/database import")
    if event.startswith(("socket.", "sqlite3.", "subprocess.", "os.spawn", "os.exec")) or event in ("os.system", "os.remove", "os.rename", "os.mkdir", "os.rmdir"):
        raise RuntimeError("forbidden side effect")
    if event == "open":
        p, mode, flags = args
        if isinstance(p, int):
            if mode not in ("r", "rb"): raise RuntimeError("unexpected fd mode")
            return
        name = os.path.normcase(os.path.abspath(p))
        if mode and any(c in mode for c in "wax+"): raise RuntimeError("result write forbidden")
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC): raise RuntimeError("result write forbidden")
        if name != os.path.normcase(script) and not name.startswith(allowed_roots):
            raise RuntimeError("extra data read forbidden")
sys.addaudithook(guard)
runpy.run_path(script, run_name="__main__")
'''


class NativeFunction:
    """Pass through the real Win32 call while observing its arguments/results."""
    def __init__(self, real, observer):
        self.real = real
        self.observer = observer
    def __call__(self, *args):
        return self.observer(self.real, args)
    @property
    def argtypes(self): return self.real.argtypes
    @argtypes.setter
    def argtypes(self, value): self.real.argtypes = value
    @property
    def restype(self): return self.real.restype
    @restype.setter
    def restype(self, value): self.real.restype = value


def observe_kernel(monkeypatch, overrides):
    original = ctypes.WinDLL
    def wrapped(name, *args, **kwargs):
        real = original(name, *args, **kwargs)
        assert name in ("kernel32", "ntdll"), "unexpected native library"
        class Library:
            def __getattr__(self, method):
                function = getattr(real, method)
                return NativeFunction(function, overrides[method]) if method in overrides else function
        return Library()
    monkeypatch.setattr(ctypes, "WinDLL", wrapped)


@pytest.mark.parametrize("mapping", [r"\??\C:\hidden-tree", r"\Device\Mup\private-server", "unknown", None])
def test_ambiguous_drive_mapping_refused_without_native_file_open(tmp_path, monkeypatch, mapping):
    module = load()
    path = write_file(tmp_path)
    def mapped(real, args):
        assert args[0] == path.drive
        if mapping is None:
            return 0
        args[1].value = mapping
        return len(mapping) + 2
    def unopened(real, args):
        pytest.fail("ambiguous drive reached native file open")
    observe_kernel(monkeypatch, {"QueryDosDeviceW": mapped, "CreateFileW": unopened, "NtCreateFile": unopened})
    refusal(module, path, "path_refused")


def test_native_opens_are_only_ancestors_and_one_read_only_content_handle(tmp_path, monkeypatch):
    module = load()
    path = write_file(tmp_path)
    k = kernel()
    k.QueryDosDeviceW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
    k.QueryDosDeviceW.restype = wt.DWORD
    mapping = ctypes.create_unicode_buffer(32768)
    assert k.QueryDosDeviceW(path.drive, mapping, len(mapping))
    expected = [mapping.value + "\\", *path.parts[1:]]
    opened, closed, live = [], [], set()
    real_pf = pq.ParquetFile
    def opening(real, args):
        output, access, object_ptr, io, allocation, attributes, share, disposition, options, ea, ea_length = args
        obj = object_ptr._obj
        name = obj.ObjectName.contents
        assert name.Buffer == expected[len(opened)]
        assert name.Length == len(expected[len(opened)].encode("utf-16-le"))
        assert obj.RootDirectory == (opened[-1] if opened else None)
        assert obj.Attributes == 0x40 and obj.SecurityDescriptor is None and obj.SecurityQualityOfService is None
        directory = len(opened) < len(expected) - 1
        assert (access, allocation, attributes, share, disposition, options, ea, ea_length) == (
            0x120089, None, 0, 3 if directory else 1, 1,
            0x200020 | (1 if directory else 0x40), None, 0)
        status = real(*args)
        assert status == 0
        handle = output._obj.value
        opened.append(handle)
        live.add(handle)
        return status
    def closing(real, args):
        live.remove(args[0])
        closed.append(args[0])
        return real(*args)
    def parquet(*args, **kwargs):
        assert len(opened) == len(expected) and set(opened) == live
        return real_pf(*args, **kwargs)
    def no_path_reopen(real, args):
        pytest.fail("unexpected pathname reopen")
    observe_kernel(monkeypatch, {"NtCreateFile": opening, "CloseHandle": closing, "CreateFileW": no_path_reopen})
    monkeypatch.setattr(pq, "ParquetFile", parquet)
    assert module.audit_time_file(str(path), **HIST)["selected_count"] == 6
    assert closed == list(reversed(opened[:-1]))  # final HANDLE transferred to CRT


@pytest.mark.parametrize("operation", ["unknown_type", "unknown_attributes", "missing_api"])
def test_native_capability_failure_is_closed_before_decode(tmp_path, monkeypatch, operation):
    module = load()
    path = write_file(tmp_path)
    def failure(real, args):
        if operation == "missing_api":
            raise AttributeError("PRIVATE_SYSTEM_API")
        return 0
    method = "GetFileType" if operation == "unknown_type" else "GetFileInformationByHandleEx"
    observe_kernel(monkeypatch, {method: failure})
    monkeypatch.setattr(pq, "ParquetFile", lambda *a, **kw: pytest.fail("unknown native capability reached decoder"))
    refusal(module, path, "path_refused")


def test_cold_import_and_real_call_have_no_business_or_result_side_effects(tmp_path):
    path = write_file(tmp_path)
    before = path.read_bytes()
    assert_physical_result(successful(cold(cli_args(path), GUARDED_COLD)))
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
