"""T214 小时级只读存活检查（第二十三轮施工计划 §3.2，D113）。

由独立的计划任务每小时运行一次，不依赖任何 agent 会话。不 import MetaTrader5、
不连接交易终端；只读登记、心跳、supervisor 状态、控制文件和磁盘余量，并写自己的
状态文件。输出一行 JSON 和退出码：0 正常；3 有生产级问题；2 检查自身出错。

生产级问题（当小时通知）：任一角色消失或心跳停了超过 10 分钟没有被拉起；supervisor
超过 10 分钟没有检查；halt、pause、killSwitch 或风险否决持续超过 1 小时；磁盘低于
10 GB；昨天的日报缺失。持续时间靠状态文件跨运行累计。

通知器读本机不入库的配置（*.local.json：{"channel": "webhook", "target": "<URL>"}），
按用户选定的渠道发一条短消息。消息里只有问题代码、角色和时长，不带账号、路径、服务器名。
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
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _age(now: datetime, text) -> float | None:
    try:
        return max((now - datetime.fromisoformat(text).astimezone(timezone.utc)).total_seconds(), 0.0)
    except (TypeError, ValueError):
        return None


def observe(live_dir, reports_dir, disk_path, now: datetime, alive=process_alive) -> dict:
    """只读一次现场。任何一项读不到都如实记 None，不猜。"""
    live = Path(live_dir)
    obs = {"roles": {}, "registry_readable": True, "supervisor_age_s": None, "halt": None,
           "pauses": [], "kill_switch": False, "risk_veto": False, "disk_free_gb": None,
           "report_yesterday": None}
    registry = _read_json(live / "instances.json")
    if not isinstance(registry, dict) or not isinstance(registry.get("instances"), list):
        obs["registry_readable"] = False
    else:
        for entry in registry["instances"]:
            key = str(entry.get("magic")) if entry.get("kind") == "runner" else entry.get("kind")
            try:
                running = bool(alive(entry["pid"], entry["started_utc"]))
            except (KeyError, TypeError, ValueError):
                running = False
            heartbeat = _read_json(live / f"heartbeat-{key}.json") or {}
            obs["roles"][key] = {"launcher_alive": running, "heartbeat_age_s": _age(now, heartbeat.get("ts"))}
    supervisor = _read_json(live / "supervisor-state.json") or {}
    obs["supervisor_age_s"] = _age(now, supervisor.get("last_checked"))
    halt = _read_json(live / "halt.json")
    if isinstance(halt, dict):
        obs["halt"] = {"reason": str(halt.get("reason"))[:80], "owner": str(halt.get("owner")),
                       "age_s": _age(now, halt.get("ts"))}
    for path in sorted(live.glob("pause-*.json")):
        pause = _read_json(path)
        if isinstance(pause, dict) and pause.get("paused"):
            obs["pauses"].append({"magic": str(pause.get("magic")), "reason": str(pause.get("reason"))[:80],
                                  "age_s": _age(now, pause.get("ts"))})
    obs["kill_switch"] = (live / "killSwitch").exists()
    obs["risk_veto"] = (live / "halt-risk-veto.json").exists()
    try:
        obs["disk_free_gb"] = round(shutil.disk_usage(disk_path).free / GB, 2)
    except OSError:
        pass
    yesterday = (now.astimezone(timezone.utc) - timedelta(days=1)).date()
    report = Path(reports_dir) / "live" / f"daily-{yesterday.isoformat()}.md"
    obs["report_yesterday"] = report.is_file() if Path(reports_dir).is_dir() else None
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
    if obs["report_yesterday"] is False:
        present["report_missing"] = {}
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


def notify(findings: list[dict], config_path, send=None) -> str:
    """有生产级问题才发。返回 none / no_channel / unsupported_channel / sent。
    配置文件只在本机（*.local.json 已被 .gitignore 忽略），不入库。"""
    production = [item for item in findings if item["production"]]
    if not production:
        return "none"
    config = _read_json(Path(config_path))
    channel = config.get("channel") if isinstance(config, dict) else None
    target = config.get("target") if isinstance(config, dict) else None
    if not channel or not target:
        return "no_channel"
    sender = send or SENDERS.get(channel)
    if sender is None:
        return "unsupported_channel"
    sender(target, format_message(production))
    return "sent"


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
    try:
        findings, state = evaluate(observe(args.live_dir, args.reports_dir, args.disk_path, now, alive),
                                   now, _read_json(state_path) or {})
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, state_path)
        notified = "disabled" if args.no_notify else notify(findings, args.notify_config, send)
    except Exception as exc:                   # the output must not leak paths: type name only
        print(json.dumps({"ts": now.isoformat(), "ok": None, "error": type(exc).__name__}))
        return 2
    production = any(item["production"] for item in findings)
    print(json.dumps({"ts": now.isoformat(), "ok": not findings, "production": production,
                      "notified": notified, "findings": findings}, ensure_ascii=True))
    return 3 if production else 0


if __name__ == "__main__":
    sys.exit(main())
