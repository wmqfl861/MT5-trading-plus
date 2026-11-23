"""T214 小时级只读存活检查（第二十三轮施工计划 §3.2，D113）。

由独立的计划任务每小时运行一次，不依赖任何 agent 会话。不 import MetaTrader5、
不连接交易终端；只读登记、心跳、supervisor 状态、控制文件和磁盘余量，并写自己的
状态文件。输出一行 JSON 和退出码：0 没有生产级问题；3 有生产级问题；2 检查自身出错。

生产级问题（当小时通知）：任一角色消失或心跳停了超过 10 分钟没有被拉起；supervisor
超过 10 分钟没有检查；halt、pause、killSwitch 或风险否决持续超过 1 小时；磁盘低于
10 GB；已到期的日报缺失。日报任务要到 21:57 UTC 才生成前一天的日报，所以 22:30 UTC
之前要求前天的日报在，22:30 起要求昨天的。持续时间靠状态文件跨运行累计。
halt.json 和 pause-<magic>.json 存在但读不出、形状不对的，按存在处理（生产是 fail-closed）。

通知器读本机不入库的配置（*.local.json：{"channel": "webhook", "target": "<URL>"}），
按用户选定的渠道发一条短消息。消息里只有问题代码、角色和时长，不带账号、路径、
服务器名。halt、pause 的 reason 原文也不进输出，只给有没有、多少字符。
通知发不出、状态文件写不了都不改变检查结论：退出码照旧，另记 notify_error、
state_error（只有异常类名）。退出码 2 的那一行带 step（observe 或 evaluate）和异常
类名，不带路径和异常原文。命令行参数写错时 argparse 也用退出码 2，输出是用法说明。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEARTBEAT_MAX_AGE_S = 120.0        # 与 supervisor 的 health_timeout 下限相同
NOT_REVIVED_S = 600.0              # 角色消失 / 心跳停 / supervisor 不检查：超过即生产级
CONTROL_STUCK_S = 3600.0           # halt、pause、killSwitch、否决：超过即生产级
DISK_MIN_GB = 10.0
GB = 2 ** 30
REPORT_DUE_UTC = (22, 30)          # 日报任务 21:57 UTC 才生成昨天的日报；22:30 起才要求它在
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
    actual = process_started_utc(pid)
    if actual is None:
        return False
    try:
        gap = (datetime.fromisoformat(actual) - datetime.fromisoformat(started_utc)).total_seconds()
    except (TypeError, ValueError):
        return False
    return abs(gap) <= 1.0


def _read_json(path: Path):
    """→ (文件存在, 解析结果)。存在但读不出或解析不了时，第二个是 None。"""
    try:
        return True, json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError):
        return True, None


def _age(now: datetime, text) -> float | None:
    try:
        return max((now - datetime.fromisoformat(text).astimezone(timezone.utc)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


def _reason(value) -> dict:
    """reason 原文不输出（里面会有路径和账号）：只给有没有、多少字符。"""
    text = value if isinstance(value, str) else "" if value is None else str(value)
    return {"reason_present": bool(text), "reason_chars": len(text)}


def observe(live_dir, reports_dir, disk_path, now: datetime, alive=process_alive) -> dict:
    """只读一次现场。任何一项读不到都如实记 None，不猜。"""
    live = Path(live_dir)
    obs = {"roles": {}, "registry_readable": True, "supervisor_age_s": None, "halt": None,
           "pauses": [], "kill_switch": False, "risk_veto": False, "disk_free_gb": None,
           "due_report": None, "due_report_present": None}
    registry = _read_json(live / "instances.json")[1]
    if not isinstance(registry, dict) or not isinstance(registry.get("instances"), list):
        obs["registry_readable"] = False
    else:
        for entry in registry["instances"]:
            key = str(entry.get("magic")) if entry.get("kind") == "runner" else entry.get("kind")
            try:
                running = bool(alive(entry["pid"], entry["started_utc"]))
            except (KeyError, TypeError, ValueError):
                running = False
            heartbeat = _read_json(live / f"heartbeat-{key}.json")[1] or {}
            obs["roles"][key] = {"launcher_alive": running, "heartbeat_age_s": _age(now, heartbeat.get("ts"))}
    supervisor = _read_json(live / "supervisor-state.json")[1] or {}
    obs["supervisor_age_s"] = _age(now, supervisor.get("last_checked"))
    exists, halt = _read_json(live / "halt.json")
    if exists:
        # 存在即停机（生产是 fail-closed）：读不出的也算存在，只标明读不出
        obs["halt"] = ({"unreadable": True, "age_s": None} if not isinstance(halt, dict) else
                       {**_reason(halt.get("reason")), "owner": str(halt.get("owner")),
                        "age_s": _age(now, halt.get("ts"))})
    for path in sorted(live.glob("pause-*.json")):
        _, pause = _read_json(path)
        magic = path.name[len("pause-"):-len(".json")]
        if not isinstance(pause, dict) or type(pause.get("paused")) is not bool:
            obs["pauses"].append({"magic": magic, "unreadable": True, "age_s": None})  # 读不出按暂停
        elif pause["paused"]:
            obs["pauses"].append({"magic": magic, **_reason(pause.get("reason")),
                                  "age_s": _age(now, pause.get("ts"))})
    obs["kill_switch"] = (live / "killSwitch").exists()
    obs["risk_veto"] = (live / "halt-risk-veto.json").exists()
    try:
        obs["disk_free_gb"] = round(shutil.disk_usage(disk_path).free / GB, 2)
    except OSError:
        pass
    utc = now.astimezone(timezone.utc)
    due = (utc - timedelta(days=1 if (utc.hour, utc.minute) >= REPORT_DUE_UTC else 2)).date()
    obs["due_report"] = due.isoformat()
    obs["due_report_present"] = ((Path(reports_dir) / "live" / f"daily-{due.isoformat()}.md").is_file()
                                 if Path(reports_dir).is_dir() else None)
    return obs


def _production(code: str, detail: dict, lasted: float) -> bool:
    age = detail.get("age_s")
    lasted = max(lasted, age if isinstance(age, (int, float)) else lasted)
    if code.startswith(("role_missing:", "heartbeat_stale:")) or code in ("supervisor_stale", "registry_unreadable"):
        return lasted >= NOT_REVIVED_S
    if code.startswith("pause:") or code in ("halt", "kill_switch", "risk_veto"):
        return lasted >= CONTROL_STUCK_S
    return code in ("disk_low", "report_missing")      # these are production-level at once


def evaluate(obs: dict, now: datetime, state: dict) -> tuple[list[dict], dict]:
    """把现场变成问题清单。state 记每个问题第一次被看到的时刻（ISO），只保留仍在的问题。"""
    present: dict[str, dict] = {}
    stamp = now.astimezone(timezone.utc).isoformat()
    if not obs["registry_readable"]:
        present["registry_unreadable"] = {}
    for key, role in sorted(obs["roles"].items()):
        if not role["launcher_alive"]:
            # The heartbeat's own age says how long the role has been down: if nobody
            # revived it, that is already past the limit on the very first look.
            present[f"role_missing:{key}"] = {"role": key, "age_s": role["heartbeat_age_s"]}
        elif role["heartbeat_age_s"] is None or role["heartbeat_age_s"] > HEARTBEAT_MAX_AGE_S:
            present[f"heartbeat_stale:{key}"] = {"role": key, "age_s": role["heartbeat_age_s"]}
    if obs["supervisor_age_s"] is None or obs["supervisor_age_s"] > HEARTBEAT_MAX_AGE_S:
        present["supervisor_stale"] = {"age_s": obs["supervisor_age_s"]}
    if obs["halt"]:
        present["halt"] = dict(obs["halt"])
    for pause in obs["pauses"]:
        present[f"pause:{pause['magic']}"] = dict(pause)
    for code, flag in (("kill_switch", obs["kill_switch"]), ("risk_veto", obs["risk_veto"])):
        if flag:
            present[code] = {}
    if obs["disk_free_gb"] is not None and obs["disk_free_gb"] < DISK_MIN_GB:
        present["disk_low"] = {"free_gb": obs["disk_free_gb"]}
    if obs["due_report_present"] is False:
        present["report_missing"] = {"report": obs["due_report"]}
    state = {code: state.get(code, stamp) for code in present}
    findings = []
    for code, detail in present.items():
        lasted = (now.astimezone(timezone.utc) - datetime.fromisoformat(state[code])).total_seconds()
        findings.append({"code": code, "since": state[code], "lasted_s": round(lasted),
                         "production": _production(code, detail, lasted), **detail})
    return findings, state


def format_message(production: list[dict]) -> str:
    parts = [f"{item['code']}({item['lasted_s'] // 60}分)" for item in production]
    return "MT5 Demo 值守：" + " ".join(parts)


def webhook_sender(target: str, message: str) -> None:
    """通用 webhook：POST {"text": <message>}。渠道的具体格式等用户选定后（U10）再定。"""
    import urllib.request
    request = urllib.request.Request(
        target, data=json.dumps({"text": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


SENDERS = {"webhook": webhook_sender}


def notify(findings: list[dict], config_path, send=None) -> tuple[str, str | None]:
    """有生产级问题才发。→ (none / no_channel / unsupported_channel / sent / failed, 异常类名)。
    发送失败只记下来，由调用方照常下结论。配置文件只在本机（*.local.json 已被 .gitignore 忽略）。"""
    production = [item for item in findings if item["production"]]
    if not production:
        return "none", None
    config = _read_json(Path(config_path))[1]
    channel = config.get("channel") if isinstance(config, dict) else None
    target = config.get("target") if isinstance(config, dict) else None
    if not channel or not target:
        return "no_channel", None
    sender = send or SENDERS.get(channel)
    if sender is None:
        return "unsupported_channel", None
    try:
        sender(target, format_message(production))
    except Exception as exc:                   # sending is best effort: type name only, no paths
        return "failed", type(exc).__name__
    return "sent", None


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


def main(argv=None, *, now=None, alive=process_alive, send=None) -> int:
    ap = argparse.ArgumentParser(description="T214 小时级只读存活检查：一行 JSON 加退出码")
    ap.add_argument("--live-dir", default="data/live")
    ap.add_argument("--reports-dir", default="data/reports")
    ap.add_argument("--disk-path", default=".")
    ap.add_argument("--state-file", default=None, help="默认 <live 的上一级>/ops-evidence/liveness/state.json")
    ap.add_argument("--notify-config", default="notify.local.json")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args(argv)
    now = now or datetime.now(timezone.utc)
    state_path = Path(args.state_file) if args.state_file else (
        Path(args.live_dir).resolve().parent / "ops-evidence" / "liveness" / "state.json")
    observed, failure = _attempt("observe", lambda: observe(
        args.live_dir, args.reports_dir, args.disk_path, now, alive))
    if failure is None:
        verdict, failure = _attempt(
            "evaluate", lambda: evaluate(observed, now, _read_json(state_path)[1] or {}))
    if failure:
        print(json.dumps({"ts": now.isoformat(), "ok": None, **failure}))
        return 2
    findings, state = verdict
    # 先有结论：通知发不发得出去、状态文件写不写得成，都不改它
    delivery, failure = _attempt("notify", lambda: "disabled" if args.no_notify
                                 else notify(findings, args.notify_config, send))
    notified, notify_error = (("disabled", None) if args.no_notify
                              else delivery if failure is None else ("failed", failure["error"]))
    _, failure = _attempt("state", lambda: write_state(state_path, state))
    production = any(item["production"] for item in findings)
    print(json.dumps({"ts": now.isoformat(), "ok": not findings, "production": production,
                      "notified": notified, "notify_error": notify_error,
                      "state_error": failure["error"] if failure else None, "findings": findings},
                     ensure_ascii=True))
    return 3 if production else 0


if __name__ == "__main__":
    sys.exit(main())
