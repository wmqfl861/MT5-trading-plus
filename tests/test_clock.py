"""T6 服务器时钟校验测试（R5）：偏移超限拒绝 + server_clock.json 样本筛选。

语义钉死（docs/blueprint.md §4 第 1 条）：
- 库内时间是"贴 UTC 标签的 naive datetime"，实为经纪商服务器时间；
- 实测偏移（服务器 − 墙钟 UTC，秒）与配置 server_utc_offset_hours（小时）
  之差超容差 → ClockMismatchError，绝不静默放行（换 GMT+2/+3 券商时
  时段/周末缺口/按小时点差会全部错位）；
- latest_valid_sample 只认未标 stale 且未超龄的样本；没有有效样本 → None
  （调用方警告但不阻断）；文件坏了如实抛错，绝不装作"没有样本"。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from mt5data.clock import (
    ClockMismatchError,
    check_server_offset,
    latest_valid_sample,
)

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def make_sample(wall: datetime, offset_s: float, *, stale: bool = False) -> dict:
    """按 scripts/measure_server_clock.py 的落盘格式造一条样本。"""
    rec = {
        "wall_utc": wall.isoformat(),
        "server_time": (wall + timedelta(seconds=offset_s)).isoformat(),
        "offset_s": offset_s,
    }
    if stale:
        rec["stale"] = True
    return rec


def write_samples(tmp_path, records, name="server_clock.json") -> str:
    p = tmp_path / name
    p.write_text(json.dumps(records, indent=1), encoding="utf-8")
    return str(p)


# ---- check_server_offset：实测 vs 配置声明 ----

def test_offset_zero_passes_when_expected_zero():
    check_server_offset(0.0, 0.0)  # 必须不抛
    check_server_offset(0, 0.0)  # int 输入同样接受（数值比较）


def test_small_jitter_within_tolerance_passes():
    # tick 时间粒度为秒、实测含取样抖动：容差内一律放行
    check_server_offset(30.0, 0.0)
    check_server_offset(-119.0, 0.0)


def test_boundary_deviation_equal_to_tol_passes():
    # 恰好等于容差不算超限（> tol 才拒）
    check_server_offset(120.0, 0.0)
    check_server_offset(-120.0, 0.0)


def test_plus_7200s_rejected_when_expected_zero():
    # GMT+2 券商、配置仍写 0 → 拒绝（R5 的核心场景）
    with pytest.raises(ClockMismatchError, match="server_utc_offset_hours"):
        check_server_offset(7200.0, 0.0)


def test_matching_offset_passes_for_nonzero_broker():
    check_server_offset(7200.0, 2.0)  # 实测与声明一致
    check_server_offset(10_800.0, 3.0)
    with pytest.raises(ClockMismatchError):
        check_server_offset(7200.0, 3.0)  # 差 1 小时（夏令时切换没跟上）


def test_custom_tolerance_is_honored():
    with pytest.raises(ClockMismatchError):
        check_server_offset(300.0, 0.0, tol_s=120.0)
    check_server_offset(300.0, 0.0, tol_s=400.0)  # 放宽容差后放行


# ---- latest_valid_sample：server_clock.json 样本筛选 ----

def test_missing_file_returns_none(tmp_path):
    assert latest_valid_sample(str(tmp_path / "nope.json")) is None


def test_returns_latest_valid_sample(tmp_path):
    p = write_samples(tmp_path, [
        make_sample(NOW - timedelta(hours=2), 5.0),
        make_sample(NOW - timedelta(hours=1), 7.0),
    ])
    rec = latest_valid_sample(p, now=NOW)
    assert rec is not None and rec["offset_s"] == 7.0  # 取最后一条有效样本


def test_stale_samples_are_skipped(tmp_path):
    # 实测文件里的真实形态：周末休市，tick 冻结 → 整条标 stale
    p = write_samples(tmp_path, [
        make_sample(NOW - timedelta(hours=3), -61_728.9, stale=True),
        make_sample(NOW - timedelta(hours=1), 1.5),
    ])
    rec = latest_valid_sample(p, now=NOW)
    assert rec is not None and rec["offset_s"] == 1.5


def test_all_stale_returns_none(tmp_path):
    p = write_samples(tmp_path, [
        make_sample(NOW - timedelta(hours=2), -61_728.9, stale=True),
        make_sample(NOW - timedelta(hours=1), -61_733.9, stale=True),
    ])
    assert latest_valid_sample(p, now=NOW) is None


def test_over_age_samples_are_skipped(tmp_path):
    # 超龄线 7 天：夏令时每年两次切换，跨切换点的陈年样本不能证明当前时差
    p = write_samples(tmp_path, [
        make_sample(NOW - timedelta(days=8), 0.0),   # 超龄 → 跳过
        make_sample(NOW - timedelta(days=6), 12.0),  # 龄内 → 有效
    ])
    rec = latest_valid_sample(p, now=NOW)
    assert rec is not None and rec["offset_s"] == 12.0


def test_age_exactly_max_age_is_valid(tmp_path):
    p = write_samples(tmp_path, [make_sample(NOW - timedelta(days=7), 3.0)])
    rec = latest_valid_sample(p, now=NOW)
    assert rec is not None and rec["offset_s"] == 3.0


def test_everything_skipped_returns_none(tmp_path):
    p = write_samples(tmp_path, [
        make_sample(NOW - timedelta(days=30), 0.0),
        make_sample(NOW - timedelta(days=20), 0.0, stale=True),
    ])
    assert latest_valid_sample(p, now=NOW) is None


def test_naive_wall_utc_treated_as_utc(tmp_path):
    # wall_utc 少见地写成 naive：按库内约定视为 UTC（server_time 本就是 naive）
    rec = make_sample(NOW.replace(tzinfo=None) - timedelta(minutes=5), 9.0)
    p = write_samples(tmp_path, [rec])
    got = latest_valid_sample(p, now=NOW)
    assert got is not None and got["offset_s"] == 9.0


def test_corrupt_file_raises(tmp_path):
    p = tmp_path / "server_clock.json"
    p.write_text('{"not": "a list"}', encoding="utf-8")  # 非数组
    with pytest.raises(ValueError, match="数组"):
        latest_valid_sample(str(p))


def test_malformed_record_raises(tmp_path):
    p = write_samples(tmp_path, [{"wall_utc": "x", "offset_s": "not-a-number"}])
    with pytest.raises(ValueError):
        latest_valid_sample(p, now=NOW)


def test_record_missing_keys_raises(tmp_path):
    p = write_samples(tmp_path, [{"wall_utc": NOW.isoformat()}])  # 缺 offset_s
    with pytest.raises(ValueError, match="offset_s"):
        latest_valid_sample(p, now=NOW)


# ---- H-13：非有限数 / 未来时间检查（NaN 偏移会让 |deviation|>tol 恒 False 而放行） ----

def _sample_with_offset(wall: datetime, offset_s: float) -> dict:
    """绕开 make_sample 的 server_time 时间加法（inf/NaN 偏移做不了算术）。"""
    rec = make_sample(wall, 0.0)
    rec["offset_s"] = offset_s
    return rec


def test_h13_nan_offset_sample_rejected(tmp_path):
    p = write_samples(tmp_path, [_sample_with_offset(
        NOW - timedelta(minutes=5), float("nan"))])
    with pytest.raises(ValueError, match="有限"):
        latest_valid_sample(p, now=NOW)


def test_h13_inf_offset_sample_rejected(tmp_path):
    p = write_samples(tmp_path, [_sample_with_offset(
        NOW - timedelta(minutes=5), float("inf"))])
    with pytest.raises(ValueError, match="有限"):
        latest_valid_sample(p, now=NOW)


def test_h13_future_sample_rejected(tmp_path):
    # wall_utc 晚于 now（超出取样抖动容差）= 文件时间戳不可信 → 如实报错
    p = write_samples(tmp_path, [make_sample(NOW + timedelta(days=1), 5.0)])
    with pytest.raises(ValueError, match="未来"):
        latest_valid_sample(p, now=NOW)
    p2 = write_samples(tmp_path, [
        make_sample(NOW - timedelta(hours=1), 5.0),
        make_sample(NOW + timedelta(hours=2), 6.0),
    ])
    with pytest.raises(ValueError, match="未来"):
        latest_valid_sample(p2, now=NOW)


def test_h13_small_future_jitter_within_grace_accepted(tmp_path):
    # 秒级取样抖动内（< 容差 120s）的轻微超前不算坏样本
    p = write_samples(tmp_path, [make_sample(NOW + timedelta(seconds=30), 5.0)])
    rec = latest_valid_sample(p, now=NOW)
    assert rec is not None and rec["offset_s"] == 5.0


def test_h13_check_server_offset_nonfinite_args_rejected():
    with pytest.raises(ValueError, match="有限"):
        check_server_offset(float("nan"), 0.0)
    with pytest.raises(ValueError, match="有限"):
        check_server_offset(float("inf"), 0.0)
    with pytest.raises(ValueError, match="有限"):
        check_server_offset(0.0, float("nan"))
    # 回归：有限参数的原有行为不变
    check_server_offset(30.0, 0.0)
