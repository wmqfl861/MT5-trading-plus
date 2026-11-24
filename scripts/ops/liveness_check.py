"""T214 小时级只读存活检查（第二十三轮施工计划 §3.2，D113）。

由独立的计划任务每小时运行一次，不依赖任何 agent 会话。不 import MetaTrader5、
不连接交易终端；只读登记、心跳、supervisor 状态、控制文件和磁盘余量，并写自己的
状态文件。输出一行 JSON 和退出码：0 没有生产级问题；3 有生产级问题；2 检查自身出错。
判级和排通知的纯逻辑在 liveness_logic.py。

生产级问题：任一角色消失或心跳停了超过 10 分钟没有被拉起；supervisor 超过 10 分钟没有
检查；halt、pause、killSwitch 或风险否决持续超过 1 小时；磁盘低于 10 GB 或者查不了余量
（disk_unknown）；已到期的日报缺失；登记表不成形状或者是空的（registry_invalid）；时间戳
来自未来。日报任务要到 21:57 UTC 才生成前一天的日报，所以 22:30 UTC 之前要求前天的日报
在，22:30 起要求昨天的；某一天的日报只在它生成的时候（它的 UTC 日次日 21:57 UTC）部署
已经在跑才要求，以登记表里最早的 started_utc 为准。登记里的 started_utc 被写早了，检查器
只能信它：会多报，不会漏报。不带时区的时间戳按 UTC 读；比现在晚 60 秒以上的不算新鲜。
心跳文件读不出、损坏、不是对象的记成陈旧（年龄 null）；supervisor 状态文件读不出、损坏、不是
对象的都按文件修改时间算年龄。检查都不因此出错。
halt.json、pause-<magic>.json 存在但读不出的按存在处理（生产对它们是 fail-closed）。

看护任务和终端（定版前跟进 D）：--expect-keeper 表示这个部署装了终端看护任务（runbook §6.5），
每小时任务的命令行里带上它。带了才看看护脚本的状态文件（缺省
<数据目录>/ops-evidence/terminal_keeper/state.json，--keeper-state 可改）：文件不在是 keeper_absent；
10 分钟没有更新是 keeper_stale（状态文件自己的年龄满了 10 分钟，第一次看到就是生产级）；最近一次是
refused 或 start_failed 是 keeper_refused（持续满 10 分钟升生产级）。不带给这三类都不评，输出的
keeper 是 not_checked。状态文件读不出、不是对象、没有 ts 的按文件修改时间算年龄；ts 来自未来记
clock_ahead:keeper。outcome、reason 只有长得像标识符的（字母开头，字母、数字、下划线）才进输出。
terminal_absent:<运行器> 不受 --expect-keeper 管：运行器的进程在、心跳新鲜，心跳的 last_error 说它
没有终端——启动等待（waiting_for_terminal），或者连上之后终端进程没了（网关的
TerminalNotConnectedError 带着类名进了 last_error）。启动等待的时长取"检查器第一次看到至今"和
"现在减去登记里的 started_utc"里大的那个：已经等了 10 分钟以上的，当次就是生产级；丢了终端没有
可信的起点，只用前者。两类都是持续满 10 分钟升生产级。对账、风控两个角色的心跳不带这类标记，
这一类只覆盖运行器。

通知：同一个生产级问题第一次发现时发一条；还在的话每 24 小时最多再发一条；消失时发一条
"已恢复"。消息里只有问题代码和时长，不带账号、路径、服务器名。halt、pause 的时长取"文件
修改时间距今"和"检查器第一次看到至今"里大的那个。
配置读本机不入库的 *.local.json，形状是 {"type": "webhook", "target": "<URL>"}：type 选出
发送器，其余字段归这个渠道自己。目前只有 webhook 一种，其余渠道等用户选定（U10）。
webhook 只发 https，http 只发回环地址，不跟随重定向。

输出里的 notified：none（没有要发的）、sent、failed（配 notify_error）、no_channel、
unsupported_channel、disabled（--no-notify）。没有渠道时不管有没有问题都写 no_channel：
"有问题但没人收到"要看得出来。通知发不出、状态文件写不了都不改变检查结论：退出码照旧，
另记 notify_error、state_error（只有异常类名）。状态文件读不出或形状不对的当空状态重新
起算，另记 state_reset（形状不对是 true，读不出是异常类名）。
halt 的 owner 只有是纯小写字母的标识（1 到 16 个字母）时才照常输出，其余写 other。登记项的
kind 也只有这样才照常使用（它就是非运行器角色的键，会进问题代码和消息）；其余的登记项整项
记 registry_invalid，那个 kind 不进输出和通知。
退出码 2 的那一行带 step（observe 或 evaluate）和异常类名，不带路径和异常原文。
那一行写不出去（标准输出没人读、已关闭）时：有生产级问题返回 3，否则返回 2。
完全没有标准输出时（例如用 pythonw 启动，sys.stdout 是 None），那一行被直接丢掉，不报错，
退出码仍是 0 或 3。
命令行参数写错时 argparse 也用退出码 2，输出是用法说明。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import ipaddress
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from liveness_logic import (  # noqa: E402
    assess, blank_state, evaluate as evaluate, format_message, load_state, mark_sent,
    notify, observe, parse_utc)
# evaluate 本脚本不调用（判定走 assess）；tests/test_t214_liveness_check.py 经本模块用它，
# 所以显式保留这个名字。

STEPS = ("observe", "evaluate", "notify", "state")     # 检查的四步；退出码 2 只带前两步


def process_started_utc(pid: int) -> str | None:
    """进程创建时刻（UTC，ISO）。打不开句柄或进程已退出 → None。只用 ctypes，不起子进程。"""
    import ctypes
    from ctypes import wintypes
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
            return None
        if exited.dwHighDateTime or exited.dwLowDateTime:
            return None                                   # exited, handle still held by someone
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return datetime.fromtimestamp(ticks / 1e7 - 11644473600, timezone.utc).isoformat()
    finally:
        kernel.CloseHandle(handle)


def process_alive(pid: int, started_utc: str) -> bool:
    """登记的启动器是否还是登记时的那个进程：PID 在，且创建时间相差不超过 1 秒。"""
    actual, registered = parse_utc(process_started_utc(pid)), parse_utc(started_utc)
    return actual is not None and registered is not None and abs((actual - registered).total_seconds()) <= 1.0


class RefusedTarget(Exception):
    """webhook 的目标不合规（不是 https，或者不是回环地址的 http）：不发请求。"""


def _loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host.lower() == "localhost"
    mapped = getattr(address, "ipv4_mapped", None)        # 只有 IPv6Address 有：IPv4 映射成的 IPv6 地址
    return address.is_loopback or mapped is not None and mapped.is_loopback


def _check_target(target) -> str:
    """只允许 https；http 只允许回环地址。"""
    try:
        parts = urllib.parse.urlsplit(target if isinstance(target, str) else "")
        host = parts.hostname or ""
    except ValueError:                                    # 例如 IPv6 的方括号不闭合
        raise RefusedTarget("malformed target") from None
    if not host or not (parts.scheme == "https" or parts.scheme == "http" and _loopback(host)):
        raise RefusedTarget("target refused")
    return target


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                                       # 3xx 不跟随：按发送失败处理


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def webhook_sender(config: dict, message: str) -> None:
    """通用 webhook：POST {"text": <message>} 到 config["target"]。载荷格式等用户选定渠道后（U10）再定。
    发送器的接口是 (渠道配置字典, 消息正文)：字典里的 type 选出发送器，其余字段归这个渠道自己。"""
    request = urllib.request.Request(
        _check_target(config.get("target")), data=json.dumps({"text": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _open(request, timeout=15) as response:
        response.read()


SENDERS = {"webhook": webhook_sender}       # type → 发送器。U10 没有答复：别的渠道不实现，只留接口


def _attempt(step: str, action):
    """跑一步。→ (结果, None) 或 (None, {"step": 步骤, "error": 异常类名})，不带路径和异常原文。"""
    try:
        return action(), None
    except Exception as exc:
        return None, {"step": step, "error": type(exc).__name__}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, path)


def _say(line: str) -> bool:
    """打出那一行。写不出去（标准输出没人读、已关闭）时把标准输出换成内存里的流并返回 False：
    解释器退出时还会冲刷一次标准输出，不换的话进程会带着 120 退出，盖掉已经定下的退出码。"""
    try:
        print(line, flush=True)
        return True
    except Exception:
        sink, sys.stdout = sys.stdout, io.StringIO()
        with contextlib.suppress(Exception):
            sink.close()
        return False


def main(argv=None, *, now=None, alive=process_alive, send=None) -> int:
    ap = argparse.ArgumentParser(description="T214 小时级只读存活检查：一行 JSON 加退出码")
    ap.add_argument("--live-dir", default="data/live")
    ap.add_argument("--reports-dir", default="data/reports")
    ap.add_argument("--disk-path", default=".")
    ap.add_argument("--state-file", default=None, help="默认 <live 的上一级>/ops-evidence/liveness/state.json")
    ap.add_argument("--notify-config", default="notify.local.json")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--expect-keeper", action="store_true",
                    help="这个部署装了终端看护任务：看它的状态文件（缺省 <数据目录>/ops-evidence/terminal_keeper/state.json）")
    ap.add_argument("--keeper-state", default=None, help="看护状态文件的路径（缺省见 --expect-keeper）")
    args = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    state_path = Path(args.state_file) if args.state_file else (
        Path(args.live_dir).resolve().parent / "ops-evidence" / "liveness" / "state.json")
    observed, failure = _attempt("observe", lambda: observe(
        args.live_dir, args.reports_dir, args.disk_path, now, alive, args.expect_keeper, args.keeper_state))
    if failure is None:
        # 状态文件读不出、形状不对：当空状态重新起算，不让检查因此退出码 2
        loaded, lost = _attempt("state", lambda: load_state(state_path))
        state, reset = loaded if lost is None else (blank_state(), lost["error"])
        verdict, failure = _attempt("evaluate", lambda: assess(observed, now, state))
    if failure:
        _say(json.dumps({"ts": now.isoformat(), "ok": None, **failure}))
        return 2
    findings, (announce, recovered, new_state) = verdict
    # 先有结论：通知发不发得出去、状态文件写不写得成，都不改它。先通知、后写状态文件。
    message = format_message(announce, recovered) if announce or recovered else None
    if args.no_notify:
        notified, notify_error = "disabled", None
    else:
        delivery, failure = _attempt("notify", lambda: notify(message, args.notify_config, SENDERS, send))
        notified, notify_error = delivery if failure is None else ("failed", failure["error"])
        if notified == "sent":
            mark_sent(new_state, announce, now)
    _, failure = _attempt("state", lambda: write_state(state_path, new_state))
    production = any(item["production"] for item in findings)
    said = _say(json.dumps({"ts": now.isoformat(), "ok": not findings, "production": production,
                            "notified": notified, "notify_error": notify_error,
                            "state_error": failure["error"] if failure else None, "state_reset": reset,
                            "keeper": "checked" if args.expect_keeper else "not_checked",
                            "findings": findings}, ensure_ascii=True))
    if not said and not production:
        return 2            # 那一行没写出去：没有生产级问题也不能报正常
    return 3 if production else 0


if __name__ == "__main__":
    sys.exit(main())
