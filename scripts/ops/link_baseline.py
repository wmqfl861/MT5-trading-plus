"""T218 链路基线（第二十三轮施工计划 §3.3，D116）。

只读：解析交易终端日志里的断线、重连和 ping，以及 Windows 系统日志里的 DNS
解析超时事件（DNS-Client 1014），按 UTC 日出统计。不 import MetaTrader5、
不连接交易终端——读日志文件不算连接终端。不依赖 src。

输出只有计数、秒数和时刻：不含账号、服务器名、接入点、IP、本机路径。

用法（仓库根；参数是交易终端数据目录下的 logs）：
    python scripts/ops/link_baseline.py --terminal-log-dir <终端数据目录>/logs --date 2026-11-24
    python scripts/ops/link_baseline.py --terminal-log-dir … --from 2026-11-24 --to 2026-11-28 --format table

不给 --date 时取 UTC 的前一天（不是本地日历的前一天）。每个 UTC 日输出一行
JSON；表格格式每个日一行。

终端日志每个本地日期一个文件（YYYYMMDD.log，UTF-16），行里的时刻是本机本地
时间；--log-utc-offset-hours 给出它相对 UTC 的小时数（默认 8）。

读法：
- 一段中断从 `connection to … lost` 到下一次 `authorized`。终端自己断开
  （`disconnected from …`）时，这一段在那一刻结束，同时开始一段"终端不在"，
  到下一次 `authorized` 为止。首尾相接的段算同一段连续中断（中间没有恢复
  过）；按起头的方式分开计数，"终端不在"的秒数另计。
- 从当天开始的段按整段计时，跨日的也算全。从前一天延续进来的段只计它落在
  当天的部分，计入"延续"。
- 向前回看 --lookback-days 天，向后最多再读 30 天（或到目录里最后一个日志
  文件），跨日、跨天的中断才量得出时长。
- 读到数据末尾（最后一个有日志行的时刻）仍未恢复的段，时长是下界
  （open_segments 里的 at_least_s；表格里最长一栏前带 ≥）。

窗口判据（与正式检查 scripts/demo_check.py 一致，不放宽）：进程连续断线
900 秒以 78 退出；退出的时刻落在 UTC 日历的交易日就算窗口失败。
- 一段中断开始后第 900 秒还没恢复、那一刻是交易日，就在那一刻失败（第一条
  规则；离 900 秒不到一分钟就恢复的记 near_breach_segments）。
- 中断跨进后面的交易日时，退出的时刻取决于进程何时被拉起。这里取偏严的口径：
  落在那个交易日里满 900 秒就按失败计，时刻记为在交易日里累计满 900 秒之时
  （would_fail_window、first_breach_utc）。
- 零点前开始、进了交易日不足 900 秒就恢复的尾巴不报失败，只计数
  （tails_into_trading_day）；这时窗口是否失败以正式检查里 rc=78 的日期为准。
日历用 --calendar（正式检查的日历 JSON，或日历源文件）；不给时周一至周五算
交易日，节假日不排除，输出里 calendar 记为 weekday。

终端在不在：终端在线时大约每 6 小时写一条扫描行。当天任意相邻两条日志行相隔
达到 --absent-gap-s（默认 7 小时），或者"终端不在"的段落落在当天的部分达到
这个数，就记 terminal_absent。

baseline_day 为真要同时满足：是交易日；日志覆盖到当天结束；终端全程在；
没有让窗口失败的中断；没有到数据末尾仍未恢复的中断。五项里有一项不满足的
日子不计入基线。

退出码：0 出了统计；3 有日子出不了统计（该日 stdout 一行 JSON，error 说明是
哪一种：no_terminal_log、log_unreadable、log_dir_unreadable）；2 参数错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from math import floor
from pathlib import Path

_LOST = re.compile(r": connection to \S+ lost")
_AUTHORIZED = re.compile(r": authorized on .*?\(ping: ([0-9][0-9 .,]*) ms")
_AP_SWITCH = re.compile(r": auto connecting to a better access point")
_SCAN = re.compile(r": scanning network for access points$")
_DISCONNECTED = re.compile(r": disconnected from ")
_CALENDAR_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(weekday|weekend|holiday)\b")
_LOG_NAME = re.compile(r"^(\d{4})(\d{2})(\d{2})\.log$")
_ACCOUNT = re.compile(r"^'([^']+?)'\s*:")
_SYSTEM_TIME = re.compile(r"SystemTime='([0-9T:.\-]+Z)'")

WINDOW_FAIL_S = 900.0              # 连续断线 15 分钟：进程以 78 退出，窗口判失败（D105）
NEAR_S = 60.0                      # 离判据不到一分钟：终端日志和进程的计时起点有差
LOOKBACK_DAYS = 30                 # 量跨日、跨天的中断时向前回看的天数
FORWARD_DAYS = 30                  # 向后最多多读的天数
ABSENT_GAP_S = 7 * 3600.0          # 终端在线时约每 6 小时一条扫描行
GAP_MARK_S = 300.0                 # 相隔这么久以上的两条日志行才记下来
DNS_CAP = 10001                    # 查询上限；到了上限就不报数

DNS_QUERY = ("*[System[Provider[@Name='Microsoft-Windows-DNS-Client'] and EventID=1014 "
             "and TimeCreated[@SystemTime>='{start}' and @SystemTime<'{end}']]]")
OLDEST_QUERY = "*[System[TimeCreated[@SystemTime<'{bound}']]]"
NEWEST_QUERY = "*[System[TimeCreated[@SystemTime>='{bound}']]]"

Event = tuple[datetime, str, "float | None"]
Span = tuple[datetime, "datetime | None", str]      # start, end (None = 未恢复), kind


def _clock(parts: list[str]) -> "float | None":
    clock = parts[2].split(".")
    try:
        hour, minute, second = (int(x) for x in clock[0].split(":"))
    except ValueError:
        return None
    fraction = int(clock[1][:3]) / 1000 if len(clock) > 1 and clock[1][:3].isdigit() else 0.0
    return hour * 3600 + minute * 60 + second + fraction


def _ping(text: str) -> "float | None":
    """ping 的数字：小数点或小数逗号；千位分隔是空格、点或逗号。"""
    digits = text.replace(" ", "")
    if "," in digits and "." in digits:
        if digits.rfind(",") < digits.rfind("."):
            digits = digits.replace(",", "")
        else:
            digits = digits.replace(".", "").replace(",", ".")
    elif "," in digits:
        digits = digits.replace(",", ".")
    try:
        return float(digits)
    except ValueError:
        return None


def parse_events(text: str) -> list[tuple[float, str, "float | None"]]:
    """终端日志文本 → [(当日秒数, 类型, ping 毫秒)]，类型取 lost / authorized /
    ap_switch / scan / disconnected；其余行忽略。"""
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 5 or parts[3] != "Network":
            continue
        at = _clock(parts)
        if at is None:
            continue
        message = "\t".join(parts[4:])
        found = _AUTHORIZED.search(message)
        if found:
            out.append((at, "authorized", _ping(found.group(1))))
        elif _LOST.search(message):
            out.append((at, "lost", None))
        elif _AP_SWITCH.search(message):
            out.append((at, "ap_switch", None))
        elif _SCAN.search(message):
            out.append((at, "scan", None))
        elif _DISCONNECTED.search(message):
            out.append((at, "disconnected", None))
    return out


def parse_line_times(text: str) -> list[float]:
    """每一行带时刻的那一列（不只网络行）：判断终端在不在用。"""
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        at = _clock(parts)
        if at is not None:
            out.append(at)
    return out


def parse_accounts(text: str) -> set[str]:
    """网络行里出现过的账号（第 5 列引号里的部分）。只用来数个数，不输出。"""
    out = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5 and parts[3] == "Network":
            found = _ACCOUNT.match(parts[4])
            if found:
                out.add(found.group(1))
    return out


def _decode(raw: bytes) -> str:
    """按 BOM 选编码；没有 BOM 的按 UTF-16 LE。正在写的文件末尾可能是半个
    字符，截掉；最后一行没写完的，写到哪儿读到哪儿。认不出来的编码抛 UnicodeError。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        body = raw[2:] if len(raw) % 2 == 0 else raw[2:len(raw) - 1]
        return body.decode("utf-16-le" if raw[:2] == b"\xff\xfe" else "utf-16-be")
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8")
    return (raw if len(raw) % 2 == 0 else raw[:len(raw) - 1]).decode("utf-16-le")


class Reading:
    """读到的内容。events 是按 UTC 时刻排序的事件；marks 是各文件首末行和相隔
    久的两行端点（判断终端在不在）；unreadable 是读不了的文件的本地日期。"""

    def __init__(self, events=(), marks=(), unreadable=(), last_line=None, accounts=(), found=()):
        self.events = list(events)
        self.marks = list(marks)
        self.unreadable = list(unreadable)
        self.last_line = last_line
        self.accounts = set(accounts)
        self.found = set(found)                    # 有文件的本地日期


def log_dates(log_dir) -> list[date]:
    """目录里有的日志文件的本地日期。"""
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    out = []
    for name in names:
        found = _LOG_NAME.match(name)
        if not found:
            continue
        try:
            out.append(date(int(found.group(1)), int(found.group(2)), int(found.group(3))))
        except ValueError:
            continue
    return sorted(out)


def log_files(log_dir, start: datetime, end: datetime, utc_offset_hours: float):
    """能装下 UTC 时间 [start, end) 的那些日志文件：(本地日期, 路径, 本地零点)。"""
    first = (start + timedelta(hours=utc_offset_hours)).date()
    last = (end - timedelta(microseconds=1) + timedelta(hours=utc_offset_hours)).date()
    file_day = first
    while file_day <= last:
        yield (file_day, Path(log_dir) / f"{file_day.strftime('%Y%m%d')}.log",
               datetime(file_day.year, file_day.month, file_day.day))
        file_day += timedelta(days=1)


def read_logs(log_dir, start: datetime, end: datetime, utc_offset_hours: float) -> Reading:
    """读 UTC 时间 [start, end) 涉及的日志文件。没有的文件跳过；读不了的记下来。"""
    reading = Reading()
    previous = None                                   # 上一条日志行；跨文件接着比间隔
    for file_day, path, local_midnight in log_files(log_dir, start, end, utc_offset_hours):
        if not path.is_file():
            continue
        reading.found.add(file_day.isoformat())
        try:
            text = _decode(path.read_bytes())
        except (OSError, UnicodeError):
            reading.unreadable.append(file_day.isoformat())
            previous = None                          # 这个文件的行不知道，不做跨文件的间隔判断
            continue
        base = local_midnight - timedelta(hours=utc_offset_hours)   # 本地零点对应的 UTC 时刻
        reading.accounts.update(parse_accounts(text))
        moments = sorted(base + timedelta(seconds=at) for at in parse_line_times(text))
        if moments:
            reading.marks.append(moments[0])
            if moments[-1] != moments[0]:
                reading.marks.append(moments[-1])
            chain = ([previous] if previous is not None else []) + moments
            for before, after in zip(chain, chain[1:]):
                if (after - before).total_seconds() >= GAP_MARK_S:
                    reading.marks.extend((before, after))
            previous = reading.last_line = moments[-1]
        for at, kind, value in parse_events(text):
            moment = base + timedelta(seconds=at)
            if start <= moment < end:
                reading.events.append((moment, kind, value))
    reading.events.sort(key=lambda item: item[0])     # 稳定排序：同一毫秒保持文件里的先后
    reading.marks = sorted(set(reading.marks))
    return reading


def segments(events) -> list[Span]:
    """lost 到下一次 authorized 为一段（lost）；终端断开（disconnected）结束当前段，
    并从那一刻起开始一段 terminal_down，直到下一次 authorized。
    到数据末尾仍未恢复的段 end 为 None。"""
    out: list[Span] = []
    opened: datetime | None = None
    kind = "lost"
    for at, event, _ in events:
        if event == "lost":
            if opened is not None:
                out.append((opened, at, kind))        # 还没恢复就又丢失：以再次丢失为界
            opened, kind = at, "lost"
        elif event == "disconnected":
            if opened is None:
                opened, kind = at, "terminal_down"
            elif kind != "terminal_down":
                out.append((opened, at, kind))        # 链路一直断到终端断开
                opened, kind = at, "terminal_down"
        elif event == "authorized" and opened is not None:
            out.append((opened, at, kind))
            opened = None
    if opened is not None:
        out.append((opened, None, kind))
    return out


def outages(spans) -> list[list[Span]]:
    """首尾相接的中断段合成一段连续中断：中间没有恢复过就不算断开。
    lost 接 terminal_down（链路断到终端自己退出）、没恢复又 lost，都是同一段。"""
    merged: list[list[Span]] = []
    for span in spans:
        if merged and merged[-1][-1][1] == span[0]:
            merged[-1].append(span)
        else:
            merged.append([span])
    return merged


def _trading(calendar: "dict[str, bool] | None"):
    if calendar:
        return lambda moment: calendar.get(moment.date().isoformat(), moment.weekday() < 5)
    return lambda moment: moment.weekday() < 5


def _wevtutil(system_root, *args) -> "str | None":
    tool = os.path.join(system_root, "System32", "wevtutil.exe") if system_root else "wevtutil"
    try:
        done = subprocess.run([tool, "qe", "System", *args], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.decode("utf-8", errors="replace") if done.returncode == 0 else None


def _bound(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _system_time(text: "str | None") -> "datetime | None":
    found = _SYSTEM_TIME.search(text or "")
    if not found:
        return None
    try:
        return datetime.fromisoformat(found.group(1).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def dns_timeouts(day: date, system_root=None, run=None) -> "int | None":
    """UTC 日内 DNS-Client 1014（名称解析超时）的条数。

    系统日志不覆盖那一天（最早的一条不早于当天结束，或者当天开始之后一条
    都没有）、查不到、或者到了查询上限时返回 None，不冒充 0。
    """
    run = run or _wevtutil
    day_start = datetime(day.year, day.month, day.day)
    day_end = day_start + timedelta(days=1)
    oldest = _system_time(run(system_root, "/q:" + OLDEST_QUERY.format(bound=_bound(day_end)), "/c:1", "/rd:false", "/f:xml"))
    newest = _system_time(run(system_root, "/q:" + NEWEST_QUERY.format(bound=_bound(day_start)), "/c:1", "/rd:true", "/f:xml"))
    if oldest is None or newest is None or oldest >= day_end or newest < day_start:
        return None                               # 系统日志没有覆盖这一天
    page = run(system_root, "/q:" + DNS_QUERY.format(start=_bound(day_start), end=_bound(day_end)),
               f"/c:{DNS_CAP}", "/rd:true", "/f:xml")
    if page is None:
        return None
    count = page.count("<Event ")
    return None if count >= DNS_CAP else count


def load_calendar(path) -> dict[str, bool]:
    """正式检查的日历 → {日期: 是否交易日}。两种格式：日历 JSON（days: [{date, trading}]），
    或日历源文件（每行 `YYYY-MM-DD weekday|weekend|holiday [理由]`）。"""
    raw = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(raw)
    except ValueError:
        out = {}
        for line in raw.splitlines():
            found = _CALENDAR_LINE.match(line.strip())
            if found:
                out[found.group(1)] = found.group(2) == "weekday"
        if not out:
            raise ValueError("日历源里没有日期行") from None
        return out
    return {row["date"]: bool(row["trading"]) for row in data["days"]}


def longest_silent_gap(start: datetime, end: datetime, marks) -> "float | None":
    """[start, end) 内相邻两条日志行（含两端）最长的间隔；没有达到 GAP_MARK_S 的
    间隔就是 None。一行都没有算整天。"""
    inside = [mark for mark in marks if start < mark < end]
    points = [start, *inside, end]
    longest = max((b - a).total_seconds() for a, b in zip(points, points[1:]))
    return longest if longest >= GAP_MARK_S else None


def _floor(seconds: float) -> float:
    """截到 0.1 秒（不是四舍五入）：报 900.0 就是确实到了 900 秒。"""
    return floor(seconds * 10 + 1e-6) / 10


def summarize(day: date, reading: Reading, dns: "int | None" = None,
              calendar: "dict[str, bool] | None" = None, *, absent_gap_s: float = ABSENT_GAP_S,
              missing_log_days=()) -> dict:
    """一个 UTC 日的链路统计。reading 须覆盖该日之前（回看期）到之后；
    missing_log_days 是这一天的日志里缺了的本地日期。"""
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    limit = (reading.last_line if reading.last_line is not None    # 数据到这里为止
             else end + timedelta(days=1))
    is_trading = _trading(calendar)
    mark = timedelta(seconds=WINDOW_FAIL_S)

    def span_measured(span_end) -> datetime:
        """能量到的终点：恢复时刻；没恢复的就是数据末尾。"""
        return limit if span_end is None else min(span_end, limit)

    def counted_span(from_: datetime, to_: "datetime | None") -> float:
        """记到当天的秒数：当天开始的算整段，延续进来的只算落在当天的部分。"""
        upper = span_measured(to_)
        return (upper - from_).total_seconds() if from_ >= start else (min(upper, end) - start).total_seconds()

    def inside_span(from_: datetime, to_: "datetime | None") -> float:
        """落在当天之内的秒数。"""
        return (min(span_measured(to_), end) - max(from_, start)).total_seconds()

    def trading_runs(from_: datetime, to_: datetime) -> list[tuple[datetime, float]]:
        """[from_, to_) 在每个交易日里连续覆盖的部分：[(起点, 秒数)]。"""
        out = []
        first, last = from_.date(), to_.date()
        while first <= last:
            midnight = datetime(first.year, first.month, first.day)
            if is_trading(midnight):
                low, high = max(from_, midnight), min(to_, midnight + timedelta(days=1))
                if high > low:
                    out.append((low, (high - low).total_seconds()))
            first += timedelta(days=1)
        return out

    def counted(row) -> float:
        return counted_span(row["start"], row["end"])

    rows, breach, near, tails, trading_longest, down_seconds = [], None, 0, 0, 0.0, 0.0
    for parts in outages(segments(reading.events)):
        span_start, span_end, kind = parts[0][0], parts[-1][1], parts[0][2]
        if span_end is not None and span_end <= start:
            continue                              # 当天开始前已结束
        if span_start >= end:
            break                                 # 中断按起点排序
        row = {"start": span_start, "end": span_end, "kind": kind, "parts": parts}
        rows.append(row)
        down_seconds += sum(counted_span(part[0], part[1]) for part in parts if part[2] == "terminal_down")
        stop = span_measured(span_end)
        runs = trading_runs(span_start, stop)      # [(交易日起点, 连续覆盖的秒数)]
        lengths = [seconds for _, seconds in runs]
        trading_longest = max(trading_longest, max(lengths, default=0.0))
        exits = []
        if stop - span_start >= mark and is_trading(span_start + mark):
            exits.append(span_start + mark)        # 第一个进程累计满 900 秒退出
        exits += [low + mark for low, seconds in runs if low > span_start and seconds >= WINDOW_FAIL_S]
        if exits:                                 # 跨进交易日的：落在交易日里满 900 秒
            moment = min(exits)
            if start <= moment < end:             # 失败记在退出时刻所在的那一天
                breach = moment if breach is None else min(breach, moment)
        elif (mark - timedelta(seconds=NEAR_S) <= stop - span_start < mark and is_trading(span_start + mark)
              or any(low > span_start and WINDOW_FAIL_S - NEAR_S <= seconds < WINDOW_FAIL_S
                     for low, seconds in runs)):
            near += 1                             # 离 900 秒不到一分钟就恢复了
        if any(start <= low < end and low > span_start and seconds < WINDOW_FAIL_S for low, seconds in runs):
            tails += 1                            # 零点前开始，进了交易日不足 900 秒就恢复
    longest = max(rows, key=counted) if rows else None
    open_at_end = [row for row in rows if span_measured(row["end"]) >= end]
    unresolved = [row for row in rows if row["end"] is None]
    # 只在有数据的部分里找空档：数据到头了不算终端不在
    silent = longest_silent_gap(start, min(end, limit), reading.marks) if limit > start else None
    missing = sorted(missing_log_days)
    down_inside = [inside_span(part[0], part[1]) for row in rows for part in row["parts"]
                   if part[2] == "terminal_down"]
    absent = None if missing else ((silent or 0.0) >= absent_gap_s
                                   or max(down_inside, default=0.0) >= absent_gap_s)
    complete = not missing and limit >= end
    down = sum(1 for row in rows if row["kind"] == "terminal_down")
    on_day = [value for at, kind, value in reading.events if kind == "authorized" and start <= at < end]
    return {
        "date_utc": day.isoformat(),
        "trading_day": is_trading(start),
        "calendar": "file" if calendar else "weekday",
        "disconnect_segments": len(rows),
        "lost_segments": len(rows) - down,
        "terminal_down_segments": down,
        "continuing_from_previous_day": sum(1 for row in rows if row["start"] < start),
        "unresolved_at_day_end": len(open_at_end),
        "whole_day_in_outage": any(row["start"] < start for row in open_at_end),
        "longest_s": _floor(counted(longest)) if longest else 0.0,
        "longest_is_lower_bound": bool(longest and longest["end"] is None),
        "longest_trading_s": _floor(trading_longest),
        "total_s": _floor(sum(counted(row) for row in rows)),
        "terminal_down_s": _floor(down_seconds),
        "open_segments": [{"start_utc": row["start"].isoformat(), "kind": row["kind"],
                           "at_least_s": _floor(counted(row))} for row in unresolved],
        "would_fail_window": breach is not None,
        "first_breach_utc": breach.isoformat() if breach else None,
        "near_breach_segments": near,
        "tails_into_trading_day": tails,
        "max_silent_gap_s": _floor(silent) if silent is not None else None,
        "terminal_absent": absent,                   # None：这一天的日志不全，判断不了
        "data_complete": complete,
        "missing_log_days": missing,
        "observed_until": limit.isoformat(),
        "authorizations": len(on_day),
        "ping_min_ms": min((v for v in on_day if v is not None), default=None),
        "ping_max_ms": max((v for v in on_day if v is not None), default=None),
        "ap_switches": sum(1 for at, kind, _ in reading.events if kind == "ap_switch" and start <= at < end),
        "scans": sum(1 for at, kind, _ in reading.events if kind == "scan" and start <= at < end),
        "dns_timeouts": dns,
        "accounts_seen": len(reading.accounts),
        "unreadable_log_days": sorted(reading.unreadable),
        "baseline_day": bool(is_trading(start) and complete and absent is False
                             and breach is None and not unresolved),
    }


TABLE_HEAD = ("| UTC 日 | 交易日 | 断线段（终端不在） | 日末未恢复 | 最长秒 | 窗口判据 | 终端 | "
              "ping 毫秒 | 接入点切换 | 扫描 | DNS 超时 | 基线 |")


def markdown_row(summary: dict) -> str:
    ping = "—" if summary["ping_min_ms"] is None else f"{summary['ping_min_ms']:.0f}–{summary['ping_max_ms']:.0f}"
    longest = f"{'≥' if summary['longest_is_lower_bound'] else ''}{int(summary['longest_s'])}"
    if summary["terminal_absent"] is None:
        presence = f"判断不了（缺 {'、'.join(summary['missing_log_days'])} 的日志）"
    elif summary["terminal_absent"]:
        presence = "**不在**"
    elif summary["max_silent_gap_s"] is None:
        presence = "在（日志行相隔不到 5 分钟）"
    else:
        presence = f"在（最长空档 {summary['max_silent_gap_s'] / 3600:.1f} 小时）"
    verdict = "**失败**" if summary["would_fail_window"] else ("临界" if summary["near_breach_segments"] else "不失败")
    dns = "未查" if summary["dns_timeouts"] is None else str(summary["dns_timeouts"])
    return (f"| {summary['date_utc']} | {'是' if summary['trading_day'] else '否'} | "
            f"{summary['disconnect_segments']}（{summary['terminal_down_segments']}） | "
            f"{summary['unresolved_at_day_end']} | {longest} | {verdict} | {presence} | {ping} | "
            f"{summary['ap_switches']} | {summary['scans']} | {dns} | "
            f"{'计入' if summary['baseline_day'] else '**不计入**'} |")


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("日期须为 YYYY-MM-DD") from None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T218 链路基线：终端日志与系统日志的只读逐日统计")
    ap.add_argument("--terminal-log-dir", required=True, help="交易终端数据目录下的 logs")
    ap.add_argument("--date", type=_parse_day, default=None, help="UTC 日 YYYY-MM-DD（默认 UTC 的前一天）")
    ap.add_argument("--from", dest="from_date", type=_parse_day, default=None, help="起始 UTC 日（与 --to 一起给）")
    ap.add_argument("--to", type=_parse_day, default=None, help="结束 UTC 日（含）")
    ap.add_argument("--log-utc-offset-hours", type=float, default=8.0,
                    help="终端日志时钟相对 UTC 的小时数（默认 8）")
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS,
                    help="量跨日、跨天的中断时向前回看的天数（默认 30）")
    ap.add_argument("--calendar", default=None, help="正式检查的日历 JSON 或日历源文件（不给：周一至周五算交易日）")
    ap.add_argument("--absent-gap-s", type=float, default=ABSENT_GAP_S,
                    help="日志相隔多少秒算终端不在（默认 25200，即 7 小时）")
    ap.add_argument("--no-dns", action="store_true", help="不查系统日志里的 DNS 超时")
    ap.add_argument("--format", choices=("json", "table"), default="json")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    if (args.from_date is None) != (args.to is None):
        ap.error("--from 和 --to 要一起给")
    if args.date is not None and args.to is not None:
        ap.error("--date 和 --from/--to 只能给一种")
    if args.from_date is not None:
        first, last = args.from_date, args.to
        if last < first or (last - first).days > 62:
            ap.error("--to 不能早于 --from，一次最多 63 天")
    else:
        first = last = args.date or (now.date() - timedelta(days=1))
    offset = args.log_utc_offset_hours
    if not (offset == offset and abs(offset) <= 24) or not 0 <= args.lookback_days <= 120 \
            or not (args.absent_gap_s == args.absent_gap_s and 0 < args.absent_gap_s <= 86400):
        ap.error("--log-utc-offset-hours 须在 -24 到 24 之间，--lookback-days 须在 0 到 120 之间，"
                 "--absent-gap-s 须大于 0、不超过 86400")
    log_dir = Path(args.terminal_log_dir)
    days = [first + timedelta(days=step) for step in range((last - first).days + 1)]
    try:
        calendar = load_calendar(args.calendar) if args.calendar else None
    except (OSError, ValueError, KeyError, TypeError):
        ap.error("--calendar 读不了或格式不对")

    def emit_error(day: date, error: str) -> int:
        if args.format == "table":
            print(f"| {day.isoformat()} | — | {error} | — | — | — | — | — | — | — | — | **不计入** |")
        else:
            print(json.dumps({"date_utc": day.isoformat(), "error": error}), flush=True)
        return 3

    if not log_dir.is_dir():
        return emit_error(first, "log_dir_unreadable")
    begin = datetime(first.year, first.month, first.day) - timedelta(days=args.lookback_days)
    finish = datetime(last.year, last.month, last.day) + timedelta(days=1)
    newest = log_dates(log_dir)
    if newest:
        through = (datetime(newest[-1].year, newest[-1].month, newest[-1].day) + timedelta(days=1)
                   - timedelta(hours=offset))
        finish = min(max(finish, through), finish + timedelta(days=FORWARD_DAYS))
    reading = read_logs(log_dir, begin, finish, offset)
    system_root = os.environ.get("SystemRoot")
    status, header = 0, args.format == "table"
    for day in days:
        begin_day = datetime(day.year, day.month, day.day)
        own = [file_day for file_day, path, _ in log_files(log_dir, begin_day, begin_day + timedelta(days=1), offset)
               if path.is_file()]
        if not own:
            status = emit_error(day, "no_terminal_log")
            continue
        if any(file_day.isoformat() in reading.unreadable for file_day in own):
            status = emit_error(day, "log_unreadable")
            continue
        dns = None if args.no_dns else dns_timeouts(day, system_root)
        missing = [file_day.isoformat() for file_day, _, _ in
                   log_files(log_dir, begin_day, begin_day + timedelta(days=1), offset)
                   if file_day.isoformat() not in reading.found]
        summary = summarize(day, reading, dns, calendar, absent_gap_s=args.absent_gap_s,
                            missing_log_days=missing)
        if header:
            print(TABLE_HEAD)
            print("| --- " * 12 + "|")
            header = False
        print(markdown_row(summary) if args.format == "table" else json.dumps(summary, ensure_ascii=True), flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
