"""Single-file time diagnostics. Declarations do not authorize real input access."""
import argparse
from contextlib import contextmanager
from datetime import date, timedelta
import json
import os
import re
import stat
import sys

import pyarrow as pa
import pyarrow.parquet as pq

_BATCH_SIZE = 65536
_DAY_NS = 86400000000000
_HOUR_NS = 3600000000000
_EPOCH = date(1970, 1, 1)
_BINDINGS = {
    "histdata": ("EURUSD", "H1", "utc_bar_open", "2007-01-01"),
    "fred": ("USTEC", "D1", "fred_observation_date", "2006-11-22"),
    "mt5": ("USTEC", "D1", "utc_bar_open", "2006-11-22"),
}
_UTC = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?(?:Z|\+00:00)")


class AuditError(Exception):
    """A sanitized local refusal; never carries the input or original exception."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _day(text):
    if type(text) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise AuditError("bounds_refused")
    try:
        return (date.fromisoformat(text) - _EPOCH).days
    except ValueError:
        raise AuditError("bounds_refused") from None


def _boundary(text, is_date):
    if is_date:
        return _day(text)
    match = _UTC.fullmatch(text) if type(text) is str else None
    if not match:
        raise AuditError("bounds_refused")
    day, hour, minute, second, fraction = match.groups()
    h, m, s = int(hour), int(minute), int(second)
    if h > 23 or m > 59 or s > 59:
        raise AuditError("bounds_refused")
    return _day(day) * _DAY_NS + (h * 3600 + m * 60 + s) * 1000000000 + int((fraction or "0").ljust(9, "0"))


def _declarations(source, symbol, timeframe, semantics, start, end):
    if any(type(v) is not str for v in (source, symbol, timeframe, semantics)):
        raise AuditError("declaration_refused")
    binding = _BINDINGS.get(source)
    if binding is None or (symbol, timeframe, semantics) != binding[:3]:
        raise AuditError("declaration_refused")
    is_date = source == "fred"
    lo, hi = _boundary(start, is_date), _boundary(end, is_date)
    scale = 1 if is_date else _DAY_NS
    if not _day(binding[3]) * scale <= lo < hi <= _day("2021-05-17") * scale:
        raise AuditError("bounds_refused")
    return lo, hi, is_date


def _local_path(path):
    # Validate the spelling before any OS lookup; never resolve or fold components.
    if type(path) is not str or not re.match(r"^[A-Za-z]:[\\/]", path):
        raise AuditError("path_refused")
    spelling = path.replace("/", "\\")
    parts = spelling[3:].split("\\")
    devices = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    devices.update(prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³")
    for component in parts:
        if (not component or component in (".", "..") or component[-1] in ". "
                or any(ord(c) < 32 or c in ':*?[]<>|"' for c in component)
                or component.split(".", 1)[0].rstrip(" ").upper() in devices):
            raise AuditError("path_refused")
    return spelling


@contextmanager
def _open_local(path):
    """Windows fixed local tree only; hold no-delete directory handles until close.

    Each name is opened relative to the previous directory handle, without
    backup intent. Read access is deliberate: an attributes-only directory
    handle did not block rename in the local Windows probe. No token or DACL
    changes are made; this is not an adversarial OS/owner boundary.
    """
    if os.name != "nt":
        raise AuditError("path_refused")
    import ctypes
    from ctypes import wintypes as w
    import msvcrt

    handles = []
    content = None
    fd = None
    try:
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            n = ctypes.WinDLL("ntdll", use_last_error=True)
            k.CloseHandle.argtypes = [w.HANDLE]
            k.CloseHandle.restype = w.BOOL
            k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
            k.GetFileInformationByHandleEx.restype = w.BOOL
            k.GetFileType.argtypes = [w.HANDLE]
            k.GetFileType.restype = w.DWORD
            k.GetDriveTypeW.argtypes = [w.LPCWSTR]
            k.GetDriveTypeW.restype = w.UINT
            k.QueryDosDeviceW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
            k.QueryDosDeviceW.restype = w.DWORD

            class Attributes(ctypes.Structure):
                _fields_ = [("attributes", w.DWORD), ("tag", w.DWORD)]

            class Unicode(ctypes.Structure):
                _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPWSTR)]

            class Object(ctypes.Structure):
                _fields_ = [("Length", w.ULONG), ("RootDirectory", w.HANDLE),
                            ("ObjectName", ctypes.POINTER(Unicode)), ("Attributes", w.ULONG),
                            ("SecurityDescriptor", w.LPVOID), ("SecurityQualityOfService", w.LPVOID)]

            class IoStatus(ctypes.Structure):
                # NTSTATUS/PVOID union occupies pointer size, including x64 padding.
                _fields_ = [("Status", w.LPVOID), ("Information", ctypes.c_size_t)]

            n.NtCreateFile.argtypes = [ctypes.POINTER(w.HANDLE), w.ULONG, ctypes.POINTER(Object),
                                      ctypes.POINTER(IoStatus), w.LPVOID, w.ULONG, w.ULONG,
                                      w.ULONG, w.ULONG, w.LPVOID, w.ULONG]
            n.NtCreateFile.restype = w.LONG

            # A SUBST drive could hide ancestors behind the drive letter. Accept
            # only a direct native volume mapping, before even querying its type.
            mapping = ctypes.create_unicode_buffer(32768)
            if (not k.QueryDosDeviceW(path[:2], mapping, len(mapping))
                    or not re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", mapping.value)):
                raise AuditError("path_refused")
            if k.GetDriveTypeW(path[:3]) != 3:  # DRIVE_FIXED, no mapped network/removable drive
                raise AuditError("path_refused")
            names = [mapping.value + "\\", *path[3:].split("\\")]
            for index, name in enumerate(names):
                directory = index < len(names) - 1
                name_buffer = ctypes.create_unicode_buffer(name)
                size = len(name.encode("utf-16-le"))
                if size + 2 > 65535:
                    raise AuditError("path_refused")
                unicode_name = Unicode(size, size + 2, ctypes.cast(name_buffer, w.LPWSTR))
                attributes = Object(ctypes.sizeof(Object), handles[-1] if handles else None,
                                    ctypes.pointer(unicode_name), 0x40, None, None)  # case insensitive
                io_status, handle = IoStatus(), w.HANDLE()
                # FILE_OPEN only; relative no-follow opens; no BACKUP_INTENT.
                # Read/list + attributes + EA + READ_CONTROL + SYNCHRONIZE.
                # Directories share READ|WRITE, content shares only READ, neither DELETE.
                status = n.NtCreateFile(ctypes.byref(handle), 0x120089, ctypes.byref(attributes),
                                        ctypes.byref(io_status), None, 0, 3 if directory else 1, 1,
                                        0x200020 | (1 if directory else 0x40), None, 0)
                if status < 0 or not handle.value:
                    raise AuditError("path_refused")
                handles.append(handle.value)
                attrs = Attributes()
                if (not k.GetFileInformationByHandleEx(handle.value, 9, ctypes.byref(attrs), ctypes.sizeof(attrs))
                        or k.GetFileType(handle.value) != 1
                        or attrs.attributes & (0x400 | 0x40 | 0x1000)
                        or bool(attrs.attributes & 0x10) != directory):
                    raise AuditError("path_refused")
            # Transfer ownership, without reopening by pathname or duplicating.
            fd = msvcrt.open_osfhandle(handles[-1], os.O_RDONLY | os.O_BINARY)
            handles.pop()
            content = os.fdopen(fd, "rb", buffering=0)
            fd = None
        except AuditError:
            raise
        except Exception:
            raise AuditError("path_refused") from None
        yield content
    finally:
        if content is not None:
            content.close()
        if fd is not None:
            os.close(fd)
        for handle in reversed(handles):
            k.CloseHandle(handle)


def _type_allowed(dtype, source):
    if source == "fred":
        return pa.types.is_date(dtype) or (pa.types.is_timestamp(dtype) and dtype.unit == "ns" and dtype.tz is None)
    return pa.types.is_timestamp(dtype) and dtype.unit in ("s", "ms", "us", "ns") and dtype.tz in (None, "UTC")


def _time_schema(parquet, source):
    arrow = parquet.schema_arrow
    if arrow.names.count("time") != 1:
        raise AuditError("schema_refused")
    dtype = arrow.field("time").type
    if not _type_allowed(dtype, source):
        raise AuditError("schema_refused")
    # Parquet prefix matching must not admit time.extra, repeated/nested time,
    # or any second physical leaf bearing the reserved name.
    leaves = []
    for index in range(len(parquet.schema)):
        column = parquet.schema.column(index)
        if "time" in column.path.split("."):
            leaves.append((index, column))
    if len(leaves) != 1 or leaves[0][1].path != "time":
        raise AuditError("schema_refused")
    index, column = leaves[0]
    expected = ("INT32", "DATE") if pa.types.is_date(dtype) else ("INT64", "TIMESTAMP")
    if (column.physical_type, column.logical_type.type) != expected or column.max_repetition_level != 0:
        raise AuditError("schema_refused")
    for group in range(parquet.metadata.num_row_groups):
        if parquet.metadata.row_group(group).column(index).file_path not in (None, ""):
            raise AuditError("schema_refused")
    return dtype


def _time_values(array, source):
    """Decode integers, never Python datetime (which would truncate ns)."""
    dtype = array.type
    if not _type_allowed(dtype, source):
        raise AuditError("schema_refused")
    if array.null_count:
        raise AuditError("time_refused")
    raw = array.cast(pa.int32() if pa.types.is_date32(dtype) else pa.int64(), safe=True).to_pylist()
    values = []
    for value in raw:
        if pa.types.is_date32(dtype):
            day = value
        elif pa.types.is_date64(dtype):
            if value % 86400000:
                raise AuditError("time_refused")
            day = value // 86400000
        else:
            value *= {"s": 1000000000, "ms": 1000000, "us": 1000, "ns": 1}[dtype.unit]
            if not -(2**63) <= value < 2**63:
                raise AuditError("time_refused")
            if source != "fred":
                values.append(value)
                continue
            if value % _DAY_NS:
                raise AuditError("time_refused")
            day = value // _DAY_NS
        if not (date.min - _EPOCH).days <= day <= (date.max - _EPOCH).days:
            raise AuditError("time_refused")
        values.append(day)
    return values


def _format(value, is_date):
    if is_date:
        return (_EPOCH + timedelta(days=value)).isoformat()
    days, remaining = divmod(value, _DAY_NS)
    seconds, nano = divmod(remaining, 1000000000)
    hour, seconds = divmod(seconds, 3600)
    minute, second = divmod(seconds, 60)
    return f"{(_EPOCH + timedelta(days=days)).isoformat()}T{hour:02d}:{minute:02d}:{second:02d}.{nano:09d}Z"


def _statistics(values, lo, hi, is_date, timeframe):
    # This function is called only after validation of every time value and the
    # second fstat. start/end restrict statistics, never input reads.
    selected = [value for value in values if lo <= value < hi]
    details = []
    positive = descending = 0
    maximum = None
    duration = "duration_days" if is_date else "duration_ns"
    for previous, current in zip(selected, selected[1:]):
        if current < previous:
            descending += 1
        elif current > previous:
            positive += 1
            item = {"start": _format(previous, is_date), "end": _format(current, is_date), duration: current - previous}
            if len(details) < 20:
                details.append(item)
            if maximum is None or item[duration] > maximum[duration]:
                maximum = item
    unique = len(set(selected))
    return {
        "status": "observations" if selected else "no_observations",
        "scanned_count": len(values), "selected_count": len(selected),
        "unique_count": unique, "duplicate_count": len(selected) - unique,
        "descending_count": descending,
        "h1_off_grid_count": sum(v % _HOUR_NS != 0 for v in selected) if timeframe == "H1" else None,
        "min_time": _format(min(selected), is_date) if selected else None,
        "max_time": _format(max(selected), is_date) if selected else None,
        "positive_interval_count": positive, "interval_details": details,
        "details_truncated": positive > 20, "max_positive_interval": maximum,
    }


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def audit_time_file(path, *, source, symbol, timeframe, timestamp_semantics, start, end):
    """Validate all time labels, then diagnose selected rows in physical order."""
    lo, hi, is_date = _declarations(source, symbol, timeframe, timestamp_semantics, start, end)
    path = _local_path(path)
    try:
        with _open_local(path) as content:
            before = os.fstat(content.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AuditError("path_refused")
            parquet = pq.ParquetFile(content, memory_map=False, buffer_size=0, pre_buffer=False)
            try:
                dtype = _time_schema(parquet, source)
                values = []
                for group in range(parquet.metadata.num_row_groups):
                    for batch in parquet.iter_batches(batch_size=_BATCH_SIZE, row_groups=[group],
                                                      columns=["time"], use_threads=False, use_pandas_metadata=False):
                        if batch.num_columns != 1 or batch.schema.names != ["time"] or batch.column(0).type != dtype:
                            raise AuditError("schema_refused")
                        values.extend(_time_values(batch.column(0), source))
                if _stamp(before) != _stamp(os.fstat(content.fileno())):
                    raise AuditError("input_changed")
            finally:
                parquet.close()
    except AuditError:
        raise
    except Exception:
        raise AuditError("parquet_refused") from None
    result = {
        "kind": "time_projection_diagnostic", "research_status": "not_evaluated",
        "execution_status": "not_released", "snapshot_identity": "unverified",
        "source": source, "symbol": symbol, "timeframe": timeframe, "series": f"{symbol}_{timeframe}",
        "timestamp_semantics": timestamp_semantics, "time_domain": "observation_date" if is_date else "utc",
        "input_time_type": str(dtype), "start": _format(lo, is_date), "end": _format(hi, is_date),
        "statistics_scope": "selected_rows_in_physical_order",
        "read_scope": {
            "decoded_columns": ["time"], "time_rows": "entire_file_before_selection",
            "footer": "contains_other_column_metadata",
            "tail_preread": "may_include_price_pages_or_entire_small_file",
            "zero_price_byte_io_guaranteed": False, "caller_declarations": "unverified",
            "metadata_stability": "no_detectable_change_on_same_handle_not_snapshot_identity",
        },
    }
    result.update(_statistics(values, lo, hi, is_date, timeframe))
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise AuditError("arguments_refused")


class _Once(argparse.Action):
    def __call__(self, parser, namespace, value, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            raise AuditError("arguments_refused")
        setattr(namespace, self.dest, value)


def main(argv=None):
    try:
        parser = _Parser(add_help=False, allow_abbrev=False)
        for name in ("path", "source", "symbol", "timeframe", "timestamp-semantics", "start", "end"):
            parser.add_argument("--" + name, required=True, action=_Once)
        result = audit_time_file(**vars(parser.parse_args(argv)))
    except AuditError as error:
        print(json.dumps({"error": {"code": error.code}}, separators=(",", ":")))
        return 2
    except Exception:
        print('{"error":{"code":"diagnostic_refused"}}')
        return 2
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
