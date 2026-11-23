"""T218 链路基线（第二十三轮施工计划 §3.3，D116）。

只读：解析交易终端日志里的断线、重连和 ping，以及 Windows 系统日志里的 DNS
解析超时事件（DNS-Client 1014），按 UTC 日出统计。不 import MetaTrader5、
不连接交易终端——读日志文件不算连接终端。

输出只有计数和秒数：不含账号、服务器名、接入点、IP。

用法（仓库根；参数是终端数据目录下的 logs 目录）：
    python scripts/ops/link_baseline.py --terminal-log-dir <终端数据目录>/logs --date 2026-11-24

终端日志每个本地日期一个文件（YYYYMMDD.log，UTF-16），行里的时刻是本机
本地时间；--log-utc-offset-hours 给出它相对 UTC 的小时数（默认 8，本机无夏令时）。
周末窗（周五 17:00 纽约 → 周日 17:00 纽约）取自 mt5data.sessions，与回测同一
个定义源；落在周末窗里的断线不计入交易时段。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mt5data.sessions import weekend_windows_between  # noqa: E402

_LOST = re.compile(r": connection to \S+ lost")
_AUTHORIZED = re.compile(r"\(ping: ([0-9.]+) ms")
_AP_SWITCH = re.compile(r": auto connecting to a better access point")
_SCAN = re.compile(r": scanning network for access points$")
_DISCONNECTED = re.compile(r": disconnected from ")

DNS_QUERY = ("*[System[Provider[@Name='Microsoft-Windows-DNS-Client'] and EventID=1014 "
             "and TimeCreated[@SystemTime>='{start}' and @SystemTime<'{end}']]]")

Event = tuple[datetime, str, float | None]


def parse_events(text: str) -> list[tuple[float, str, float | None]]:
    """终端日志文本 → [(当日秒数, 类型, ping 毫秒)]，类型取 lost / authorized /
    ap_switch / scan / disconnected；其余行忽略。"""
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 5 or parts[3] != "Network":
            continue
        clock = parts[2].split(".")
        try:
            hour, minute, second = (int(x) for x in clock[0].split(":"))
        except ValueError:
            continue
        fraction = int(clock[1][:3]) / 1000 if len(clock) > 1 and clock[1][:3].isdigit() else 0.0
        at = hour * 3600 + minute * 60 + second + fraction
        message = "\t".join(parts[4:])
        found = _AUTHORIZED.search(message)
        if found:
            out.append((at, "authorized", float(found.group(1))))
        elif _LOST.search(message):
            out.append((at, "lost", None))
        elif _AP_SWITCH.search(message):
            out.append((at, "ap_switch", None))
        elif _SCAN.search(message):
            out.append((at, "scan", None))
        elif _DISCONNECTED.search(message):
            out.append((at, "disconnected", None))
    return out


def read_events(log_dir, start: datetime, end: datetime, utc_offset_hours: float) -> list[Event]:
    """UTC 时间 [start, end) 内的网络事件，按 UTC 时刻排序。

    按终端本地日期逐个读日志（文件名是本地日期，行里是本地时间）；缺文件就跳过。
    """
    out: list[Event] = []
    first = (start + timedelta(hours=utc_offset_hours)).date() - timedelta(days=1)
    last = (end + timedelta(hours=utc_offset_hours)).date() + timedelta(days=1)
    file_day = first
    while file_day <= last:
        path = Path(log_dir) / f"{file_day.strftime('%Y%m%d')}.log"
        local_midnight = datetime(file_day.year, file_day.month, file_day.day)
        file_day += timedelta(days=1)
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-16")              # 带 BOM 或不带 BOM 的 UTF-16
        except UnicodeError:
            text = raw.decode("utf-8-sig", errors="replace")
        base = local_midnight - timedelta(hours=utc_offset_hours)   # 本地零点对应的 UTC 时刻
        for at, kind, value in parse_events(text):
            moment = base + timedelta(seconds=at)
            if start <= moment < end:
                out.append((moment, kind, value))
    return sorted(out, key=lambda item: item[0])


def segments(events) -> list[tuple[datetime, datetime | None]]:
    """一次 lost 到下一次 authorized 为一段；中间终端退出（disconnected）的，终点
    记 None；到事件列表末尾仍未恢复的也记 None。"""
    out, opened = [], None
    for at, kind, _ in events:
        if kind == "lost":
            if opened is not None:
                out.append((opened, at))      # 还没恢复就又丢失：以再次丢失为界
            opened = at
        elif kind in ("authorized", "disconnected") and opened is not None:
            out.append((opened, at if kind == "authorized" else None))
            opened = None
    if opened is not None:
        out.append((opened, None))
    return out


def trading_overlap_s(start: datetime, end: datetime) -> float:
    """[start, end) 里不在周末窗内的秒数（naive UTC）。"""
    total = (end - start).total_seconds()
    for weekend_start, weekend_end in weekend_windows_between(start, end):
        overlap = (min(end, weekend_end) - max(start, weekend_start)).total_seconds()
        total -= max(overlap, 0.0)
    return max(total, 0.0)


def dns_timeouts(day: date, runner=subprocess.run, system_root: str | None = None) -> int | None:
    """UTC 日内 DNS-Client 1014（名称解析超时）的条数；查不到返回 None，不冒充 0。"""
    tool = os.path.join(system_root, "System32", "wevtutil.exe") if system_root else "wevtutil"
    query = DNS_QUERY.format(start=f"{day.isoformat()}T00:00:00.000Z",
                             end=f"{(day + timedelta(days=1)).isoformat()}T00:00:00.000Z")
    try:
        done = runner([tool, "qe", "System", "/q:" + query, "/c:10001", "/rd:true", "/f:xml"],
                      capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace").count("<Event ")


def summarize(day: date, events: list[Event], dns: int | None) -> dict:
    """一个 UTC 日的链路统计。events 须覆盖该日前后各一段时间，跨日的断线段才量得出时长。"""
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    spans = [(s, e) for s, e in segments(events) if start <= s < end]
    resolved = [(s, e) for s, e in spans if e is not None]
    pings = [v for at, kind, v in events if kind == "authorized" and start <= at < end and v is not None]
    return {
        "date_utc": day.isoformat(),
        "trading_day": day.weekday() < 5,
        "disconnect_segments": len(spans),
        "unresolved": sum(1 for _, e in spans if e is None),
        "longest_s": round(max(((e - s).total_seconds() for s, e in resolved), default=0.0), 1),
        "longest_trading_s": round(max((trading_overlap_s(s, e) for s, e in resolved), default=0.0), 1),
        "total_s": round(sum(((e - s).total_seconds() for s, e in resolved), 0.0), 1),
        "authorizations": len(pings),
        "ping_min_ms": min(pings) if pings else None,
        "ping_max_ms": max(pings) if pings else None,
        "ap_switches": sum(1 for at, kind, _ in events if kind == "ap_switch" and start <= at < end),
        "scans": sum(1 for at, kind, _ in events if kind == "scan" and start <= at < end),
        "dns_timeouts": dns,
    }


def markdown_row(summary: dict) -> str:
    ping = ("—" if summary["ping_min_ms"] is None
            else f"{summary['ping_min_ms']:.0f}–{summary['ping_max_ms']:.0f}")
    dns = "未查" if summary["dns_timeouts"] is None else str(summary["dns_timeouts"])
    return (f"| {summary['date_utc']} | {'是' if summary['trading_day'] else '否'} | "
            f"{summary['disconnect_segments']} | {summary['longest_trading_s']:.0f} | "
            f"{summary['total_s']:.0f} | {ping} | {summary['ap_switches']} | "
            f"{summary['scans']} | {dns} |")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T218 链路基线：终端日志与系统日志的只读逐日统计")
    ap.add_argument("--terminal-log-dir", required=True, help="终端数据目录下的 logs")
    ap.add_argument("--date", default=None, help="UTC 日 YYYY-MM-DD（默认昨天）")
    ap.add_argument("--log-utc-offset-hours", type=float, default=8.0,
                    help="终端日志时钟相对 UTC 的小时数（默认 8）")
    ap.add_argument("--no-dns", action="store_true", help="不查系统日志里的 DNS 超时")
    ap.add_argument("--format", choices=("json", "table"), default="json")
    args = ap.parse_args(argv)
    try:
        day = (date.fromisoformat(args.date) if args.date
               else datetime.now(timezone.utc).date() - timedelta(days=1))
    except ValueError:
        ap.error("--date 须为 YYYY-MM-DD")
    start = datetime(day.year, day.month, day.day)
    # 前后各留一天：跨日的断线段要读到恢复那一刻才量得出时长。
    events = read_events(args.terminal_log_dir, start - timedelta(days=1),
                         start + timedelta(days=2), args.log_utc_offset_hours)
    if not any(start <= at < start + timedelta(days=1) for at, _, _ in events):
        print(f"× {day}（UTC）的终端日志里没有任何网络事件：日志目录里没有这一天的文件，"
              "或终端整天没有重连。", file=sys.stderr)
        return 2
    dns = None if args.no_dns else dns_timeouts(day, system_root=os.environ.get("SystemRoot"))
    summary = summarize(day, events, dns)
    if args.format == "table":
        print("| UTC 日 | 交易日 | 断线段 | 最长交易时段秒 | 合计秒 | ping 毫秒 | 接入点切换 | 扫描 | DNS 超时 |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        print(markdown_row(summary))
    else:
        print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
