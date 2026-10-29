"""服务器时钟校验（R5 / 计划 T6）：实测偏移对照配置声明，超限拒绝运行。

背景（docs/blueprint.md §4 第 1 条）：库内一切时间都是"贴 UTC 标签的
naive datetime"，实为经纪商服务器时间。Exness 服务器为 GMT+0，目前恰好
等于 UTC；换成 GMT+2/+3 的券商，时段、周末缺口、周五强平、按小时取点差
会全部静默错位——因此配置必须显式声明 server_utc_offset_hours
（mt5gw.config.GatewayConfig），刷新与回测启动前用实测时钟校验。

本模块只做"比较"与"读样本"，不连 MT5：
- check_server_offset：实测偏移（秒）对照期望小时数，偏差超容差抛
  ClockMismatchError（fail-fast，绝不静默放行）；
- latest_valid_sample：读 data/server_clock.json（由
  scripts/measure_server_clock.py 追加写）的最新有效样本，供回测启动时
  校验；没有有效样本 → None，调用方警告但不阻断。
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone

DEFAULT_TOL_S = 120.0  # 容差（秒）：tick 时间粒度为秒，实测含网络/取样抖动
MAX_SAMPLE_AGE = timedelta(days=7)  # 样本超龄线：夏令时每年两次切换，
# 跨切换点的陈年样本证明不了"当前"时差；一周足够覆盖切换周前后的取证
FUTURE_GRACE = timedelta(seconds=DEFAULT_TOL_S)  # H-13：wall_utc 允许的取样
# 抖动超前量（秒级）；显著晚于"现在"的时间戳 = 时钟回拨/文件不可信


class ClockMismatchError(RuntimeError):
    """实测服务器时钟偏移与配置 server_utc_offset_hours 不符（R5）。"""


def check_server_offset(measured_offset_s: float, expected_hours: float,
                        *, tol_s: float = DEFAULT_TOL_S) -> None:
    """实测偏移与期望偏移之差 ≤ tol_s 才放行；超限抛 ClockMismatchError。

    单位：measured_offset_s 与 tol_s 为秒，expected_hours 为小时。
    measured_offset_s 的符号约定 = 服务器时间 − 墙钟 UTC，与
    scripts/measure_server_clock.py 落盘的 offset_s 一致
    （例：GMT+2 券商实测约 +7200）。
    H-13：参数须有限数——NaN 会让 |deviation| > tol 恒为 False 而静默放行。
    """
    for name, v in (("measured_offset_s", measured_offset_s),
                    ("expected_hours", expected_hours), ("tol_s", tol_s)):
        if not math.isfinite(v):
            raise ValueError(f"{name} 须为有限数，收到 {v!r}")
    deviation_s = measured_offset_s - expected_hours * 3600.0
    if abs(deviation_s) > tol_s:
        raise ClockMismatchError(
            f"服务器时钟偏移实测 {measured_offset_s:+.1f}s，与配置 "
            f"server_utc_offset_hours={expected_hours}"
            f"（{expected_hours * 3600.0:+.0f}s）偏差 {deviation_s:+.1f}s，"
            f"超容差 {tol_s:.0f}s——库内时间按服务器时间贴 UTC 标签，偏移不符时"
            "时段/周末缺口/按小时成本会全部错位。请核对配置的时差声明"
            "（夏令时切换前后会差 1 小时），或在开市时段重测 "
            "scripts/measure_server_clock.py 更新 data/server_clock.json。"
        )


def latest_valid_sample(path: str, *, now: datetime | None = None,
                        max_age: timedelta = MAX_SAMPLE_AGE) -> dict | None:
    """读 server_clock.json 的最新有效样本；没有 → None（调用方警告但不阻断）。

    有效样本 = 未标 "stale": true（休市静默期取到的是末笔旧 tick，差值度量
    的是静默时长而非时钟差）且 wall_utc 距 now 不超过 max_age。文件按时间
    顺序追加，取最后一条有效样本；wall_utc 为 naive 时按 UTC 解释（库内
    约定）。文件不存在 → None；文件存在但坏了（非 JSON 数组、记录缺
    wall_utc/offset_s、时间不可解析、offset_s 非有限数（H-13）、wall_utc
    显著晚于 now（H-13，超 FUTURE_GRACE 的"未来时间"= 时钟回拨/文件不可信）
    → 如实抛错，绝不把读不了的数据当"没有样本"糊弄过去。
    """
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(
            f"{path} 应为 JSON 数组（measure_server_clock.py 追加写），"
            f"实际为 {type(data).__name__}——先人工核查该文件"
        )
    now = datetime.now(timezone.utc) if now is None else now
    latest = None
    for i, rec in enumerate(data):
        if not isinstance(rec, dict) or "wall_utc" not in rec:
            raise ValueError(
                f"{path} 第 {i} 条记录不是带 wall_utc 的对象：{rec!r}"
            )
        offset = rec.get("offset_s")
        if not isinstance(offset, (int, float)) or isinstance(offset, bool):
            raise ValueError(
                f"{path} 第 {i} 条记录缺数值型 offset_s：{rec!r}"
            )
        if not math.isfinite(offset):
            raise ValueError(
                f"{path} 第 {i} 条 offset_s 非有限数：{offset!r}——"
                "NaN 偏移过不了任何容差比较，样本不可用，先人工核查该文件"
            )
        try:
            wall = datetime.fromisoformat(str(rec["wall_utc"]))
        except ValueError as e:
            raise ValueError(
                f"{path} 第 {i} 条 wall_utc 不可解析：{rec['wall_utc']!r}"
            ) from e
        if wall.tzinfo is None:
            wall = wall.replace(tzinfo=timezone.utc)  # naive 按 UTC 解释
        if wall - now > FUTURE_GRACE:
            raise ValueError(
                f"{path} 第 {i} 条 wall_utc 晚于当前时间 {wall.isoformat()} "
                f"（now={now.isoformat()}，超前超过 {FUTURE_GRACE} 容差）——"
                "未来时间样本不可信（时钟回拨/文件损坏），先人工核查该文件"
            )
        if rec.get("stale"):
            continue
        if now - wall > max_age:
            continue
        latest = rec
    return latest
