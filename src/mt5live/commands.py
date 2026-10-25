"""mt5live.commands（T32/D21）：全平指令编号协议——风控进程、闸门、执行器共用。

修复 C3/P10（旧 ack 吞掉新指令）：指令带编号，确认文件引用编号，归档时
逐 magic 核对编号并清理确认。文件契约（data/live/commands/）：

- ``flatten.json``——``{"command": "flatten_all", "id": <uuid4().hex>,
  "reason": str, "ts": iso}``。已有未归档指令时 :func:`write_flatten`
  幂等返回它（不覆盖，首次原因优先）；
- ``flatten.ack-<magic>``——``{"magic": int, "id": <指令编号>, "closed": int,
  "ts": iso}``。执行器消费指令后写，引用指令编号；
- ``archive/flatten-<id 前 8 位>-<时间戳>.json``——所有登记 magic 的
  ``ack.id == 当前指令 id`` → 归档指令并**删除这些 ack**；旧编号的 ack
  不算数（防 C3），ack 缺失/读不出按"未确认"处理（fail-closed）。

指令文件存在但读不出（损坏/缺字段，含 T24 旧格式无 id）→
:exc:`CommandUnreadableError`——调用方按"有指令"处理（执行器照样全平、
风控进程不归档）并写告警（ALERT_COMMAND_UNREADABLE）。

时间约定与仓库一致：内存 naive UTC，文件 ISO 带 +00:00。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime

from mt5live.fileio import atomic_replace, write_json_atomic
from mt5live.monitor import iso_utc, naive_utc, parse_iso

COMMANDS_SUBDIR = "commands"
FLATTEN_FILE = "flatten.json"
FLATTEN_ACK_FMT = "flatten.ack-{magic}"     # 执行器确认文件（引用指令编号）
FLATTEN_ARCHIVE_SUBDIR = "archive"          # 全员确认后指令归档处
FLATTEN_COMMAND = "flatten_all"

# 告警类别（T32；monitor 未定义的新类别，常量放本层——同 executor 先例）
ALERT_COMMAND_UNREADABLE = "COMMAND_UNREADABLE"   # 指令文件读不出：按有指令处理


class CommandUnreadableError(RuntimeError):
    """指令文件存在但读不出（损坏/缺字段/旧格式无编号）——按"有指令"处理。"""


@dataclass(frozen=True)
class FlattenCommand:
    """一条全平指令（编号是 ack 与归档核对的键）。"""

    id: str                   # uuid4().hex
    reason: str
    ts: datetime              # naive UTC


@dataclass(frozen=True)
class FlattenAck:
    """一个 magic 对某条指令的确认（closed = 该轮平掉的持仓笔数）。"""

    magic: int
    id: str                   # 所确认的指令编号
    closed: int
    ts: datetime              # naive UTC


def _commands_dir(live_dir: str) -> str:
    return os.path.join(live_dir, COMMANDS_SUBDIR)


def flatten_path(live_dir: str) -> str:
    """当前（未归档）全平指令文件路径。"""
    return os.path.join(_commands_dir(live_dir), FLATTEN_FILE)


def ack_path(live_dir: str, magic: int) -> str:
    """magic 的确认文件路径。"""
    return os.path.join(_commands_dir(live_dir),
                        FLATTEN_ACK_FMT.format(magic=magic))


def write_flatten(live_dir: str, *, reason: str, now: datetime) -> FlattenCommand:
    """写全平指令（带新编号）；已有未归档指令 → 返回它（不覆盖，首次原因
    优先）。已有指令读不出 → CommandUnreadableError（保留原文件，由调用方
    告警——执行器会按"有指令"处理，重写反而可能吞掉它）。"""
    path = flatten_path(live_dir)
    existing = read_flatten(live_dir)       # 缺文件 → None；读不出 → 抛
    if existing is not None:
        return existing
    cmd = FlattenCommand(id=uuid.uuid4().hex, reason=str(reason),
                         ts=naive_utc(now))
    write_json_atomic(path, {
        "command": FLATTEN_COMMAND, "id": cmd.id, "reason": cmd.reason,
        "ts": iso_utc(cmd.ts)})
    return cmd


def read_flatten(live_dir: str) -> FlattenCommand | None:
    """读当前指令：无文件 → None；存在但读不出（含旧格式无 id）→
    CommandUnreadableError（调用方按"有指令"处理并告警）。"""
    path = flatten_path(live_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"顶层须为对象，收到 {type(data).__name__}")
        cmd = FlattenCommand(id=data["id"], reason=data["reason"],
                             ts=parse_iso(data["ts"]))
    except KeyError as e:
        raise CommandUnreadableError(
            f"{path} 缺字段 {e}（T24 旧格式无编号同样按读不出处理）——"
            "按有指令处理并告警") from e
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        raise CommandUnreadableError(
            f"{path} 读不出（{e}）——按有指令处理并告警") from e
    if not cmd.id or not cmd.reason:
        raise CommandUnreadableError(
            f"{path} id/reason 为空——按有指令处理并告警")
    return cmd


def write_ack(live_dir: str, magic: int, command_id: str, *, closed: int,
              now: datetime) -> None:
    """写本 magic 的确认（引用指令编号；重复写 = 覆盖为最新一轮）。"""
    write_json_atomic(ack_path(live_dir, magic), {
        "magic": int(magic), "id": str(command_id), "closed": int(closed),
        "ts": iso_utc(naive_utc(now))})


def read_ack(live_dir: str, magic: int) -> FlattenAck | None:
    """读本 magic 的确认：缺失或读不出 → None（一律按"未确认"处理，
    fail-closed——绝不凭残缺 ack 认为已确认）。"""
    path = ack_path(live_dir, magic)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return FlattenAck(magic=int(data["magic"]), id=str(data["id"]),
                          closed=int(data["closed"]),
                          ts=parse_iso(data["ts"]))
    except (OSError, KeyError, TypeError, ValueError):
        return None


def archive_if_all_acked(live_dir: str, magics: set[int],
                         now: datetime) -> bool:
    """所有登记 magic 的 ack.id == 当前指令 id → 归档指令并删除这些 ack。

    任一 magic 无 ack、ack 编号不匹配（旧 id）或 ack 读不出 → False（指令
    原地保留，等执行器消费）。无登记 magic（空集）→ False（无从确认）。
    指令文件读不出 → CommandUnreadableError（调用方告警，不归档）。"""
    cmd = read_flatten(live_dir)            # 先读：损坏在任何情况下都要暴露
    if cmd is None or not magics:
        return False
    for magic in sorted(magics):
        ack = read_ack(live_dir, magic)
        if ack is None or ack.id != cmd.id:
            return False
    archive_dir = os.path.join(_commands_dir(live_dir), FLATTEN_ARCHIVE_SUBDIR)
    os.makedirs(archive_dir, exist_ok=True)
    stamp = iso_utc(now).replace(":", "").replace("+", "_")
    # T33：归档挪档同样可能撞上执行器正读着指令文件（Windows 句柄）——
    # 经 fileio.atomic_replace 带重试
    atomic_replace(flatten_path(live_dir),
                   os.path.join(archive_dir, f"flatten-{cmd.id[:8]}-{stamp}.json"))
    for magic in sorted(magics):
        try:
            os.remove(ack_path(live_dir, magic))
        except OSError:
            pass                            # ack 已不在：归档已完成使命
    return True
