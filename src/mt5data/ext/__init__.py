"""外部数据接入（T67/T81/D36/铁律 19）：适配器、manifest、批准状态、一致性核对。

公共 API 从 core/adapters/approvals/consistency 汇出；使用约束与批准结论见
docs/research/ext-data-2026-10-27.md、mt5data.ext.approvals 与蓝图 §6.5。
"""
from mt5data.ext.adapters import (  # noqa: F401
    ExtSourceUnreachable,
    cdd_daily_from_bytes,
    fetch_cdd_csv,
    fetch_fred_csv,
    fetch_histdata_year,
    fetch_stooq_csv,
    fred_daily_from_bytes,
    histdata_m1_from_bytes,
    histdata_raw_stamps,
    stooq_daily_from_bytes,
)
from mt5data.ext.approvals import (  # noqa: F401
    APPROVED_AT,
    REVIEW_REF,
    SOURCE_APPROVALS,
    approved_sources,
    check_exportable,
)
from mt5data.ext.core import (  # noqa: F401
    REQUIRED_MANIFEST_FIELDS,
    ExtDataError,
    ExtSeries,
    assert_within_cutoff,
    ext_inventory,
    fixed_offset_to_utc, histdata_stamps_to_utc,
    load_ext_series,
    save_ext_series,
    slice_before_cutoff,
    utc_now_iso,
    validate_manifest,
)

__all__ = [
    "APPROVED_AT", "REVIEW_REF", "REQUIRED_MANIFEST_FIELDS",
    "SOURCE_APPROVALS", "ExtDataError", "ExtSeries",
    "ExtSourceUnreachable",
    "approved_sources", "assert_within_cutoff", "cdd_daily_from_bytes",
    "check_exportable", "ext_inventory",
    "fetch_cdd_csv", "fetch_fred_csv", "fetch_histdata_year",
    "fetch_stooq_csv", "fixed_offset_to_utc", "fred_daily_from_bytes",
    "histdata_m1_from_bytes", "histdata_raw_stamps", "load_ext_series",
    "save_ext_series", "slice_before_cutoff", "stooq_daily_from_bytes",
    "utc_now_iso", "validate_manifest",
]
