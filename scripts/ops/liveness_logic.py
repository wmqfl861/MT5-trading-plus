"""T214 存活检查的纯逻辑：读一次现场、判级、排通知、校验状态文件。

不 import MetaTrader5、不连交易终端、不起子进程、不联网、不写任何文件。进程探针由
调用方注入；状态文件的写入和通知的发送都在 liveness_check.py 里。

看护任务和终端（定版前跟进 D）：给了 --expect-keeper 才看护任务的状态文件，得 keeper_absent、
keeper_stale、keeper_refused 三类发现；terminal_absent:<运行器> 不受这个开关管，信号来自运行器
自己的心跳——等终端的时候心跳照写，role_missing、heartbeat_stale 都不会触发。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEARTBEAT_MAX_AGE_S = 120.0        # 与 supervisor 的 health_timeout 下限相同
NOT_REVIVED_S = 600.0              # 角色消失 / 心跳停 / supervisor 不检查 / 登记表读不出：超过即生产级
KEEPER_STALE_S = NOT_REVIVED_S     # 看护每分钟运行一次：状态文件 10 分钟没有更新，就是 10 次没有跑
WAITING_FOR_TERMINAL = "waiting_for_terminal"   # 运行器启动后等终端时心跳的 last_error（live_runner.py）
TERMINAL_GONE = "MT5 终端进程不在"                 # 网关在终端进程不在时的报错原文开头（mt5gw/connection.py）
STATE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")   # 看护状态文件里的 outcome、reason：只有长得像标识符才照常输出
                                                  # （reason 是拒绝的代码，或启动失败时 .NET 异常的类名）
CONTROL_STUCK_S = 3600.0           # halt、pause、killSwitch、否决：超过即生产级
DISK_MIN_GB = 10.0
GB = 2 ** 30
CLOCK_SKEW_S = 60.0                # 时间戳比现在晚这么多以上，就不算新鲜
REPORT_MADE_UTC = (21, 57)         # 日报任务在这个时刻（UTC）生成前一天的日报
REPORT_DUE_UTC = (22, 30)          # 22:30 起才要求最近生成的那份日报在（给它的重试留出宽限）
REMIND_AFTER_S = 86400.0           # 同一个生产级问题还在：两条通知至少隔这么久
IDENTIFIER = re.compile(r"[a-z]{1,16}")   # halt 的 owner、登记项的 kind：只有长这样才照常使用
UNREADABLE = object()              # _read_json 读不到内容时的结果：文件不存在、读不出、解析不了


def parse_utc(value) -> datetime | None:
    """ISO 字符串或 datetime → 带 UTC 时区的时刻；读不出 → None。
    不带时区的按 UTC（项目里一律写 UTC）。"""
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _age(now: datetime, value) -> float | None:
    """时间戳距今多少秒。读不出 → None；比 now 晚 60 秒以上 → None（调用方另记 clock_ahead）；
    60 秒以内的小偏差按 0 算。"""
    stamp = parse_utc(value)
    seconds = None if stamp is None else (now - stamp).total_seconds()
    return None if seconds is None or seconds < -CLOCK_SKEW_S else max(seconds, 0.0)


def _file_age(now: datetime, path: Path) -> float | None:
    """文件修改时间距今多少秒；读不到 → None。"""
    try:
        return _age(now, datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    except OSError:
        return None


def _read_json(path: Path):
    """→ (文件存在, 解析结果)。文件不存在、存在但读不出或解析不了时，第二个是 UNREADABLE。
    文件里写的是 null 时解析结果就是 None，和读不出分得开。"""
    try:
        return True, json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return False, UNREADABLE
    except (OSError, ValueError):
        return True, UNREADABLE


def _reason(value) -> dict:
    """reason 原文不输出（里面会有路径和账号）：只给有没有、多少字符。"""
    text = value if isinstance(value, str) else "" if value is None else str(value)
    return {"reason_present": bool(text), "reason_chars": len(text)}


def _owner(value) -> str:
    """halt 的 owner 只放行纯小写字母的标识（1 到 16 个字母）；其余（可能带路径、账号）一律写 other。"""
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else "other"


def _keeper(now: datetime, path: Path, ahead: list) -> dict:
    """看护任务的状态文件（terminal_keeper.ps1 每次做过判断的运行写一次）。不在 → present 是 False。
    在：ts 读得出就按 ts 算年龄（比 now 晚 60 秒以上记 clock_ahead:keeper）；读不出、不是对象、
    没有 ts 的按文件修改时间算——supervisor-state 同一个做法。outcome、reason 只有长得像标识符
    （字母开头，字母、数字、下划线）才照常记，其余记 None：文件里可能写着别的东西，不往输出和通知里带。"""
    exists, state = _read_json(path)
    if not exists:
        return {"present": False}
    if isinstance(state, dict) and parse_utc(state.get("ts")) is not None:
        age = _age(now, state.get("ts"))
        if age is None:
            ahead.append("keeper")
    else:
        age = _file_age(now, path)
    return {"present": True, "age_s": age,
            "outcome": _state_code(state.get("outcome") if isinstance(state, dict) else None),
            "reason": _state_code(state.get("reason") if isinstance(state, dict) else None)}


def _state_code(value):
    return value if isinstance(value, str) and STATE_CODE.fullmatch(value) else None


def _terminal_wait(error) -> str | None:
    """运行器的心跳在说它没有终端时的两种说法（心跳的 last_error）。启动等待是一个固定的标记；
    连上之后终端进程没了，是网关的 TerminalNotConnectedError 带着类名进了 last_error——运行器把它
    写成 "broker_unavailable: NotConnectedError: MT5 终端进程不在：未调用 initialize（…）"。IPC 不通
    而终端进程还在是另一句报错，不在这里。"""
    if error == WAITING_FOR_TERMINAL:
        return "startup"
    if isinstance(error, str) and TERMINAL_GONE in error and "NotConnectedError" in error:
        return "lost"
    return None


def _control(now: datetime, doc: dict, path: Path, source: str, ahead: list) -> dict:
    """halt / pause 文件：持续时间按文件修改时间算；文件里的 ts 只用来查时钟。"""
    if parse_utc(doc.get("ts")) is not None and _age(now, doc.get("ts")) is None:
        ahead.append(source)
    return {**_reason(doc.get("reason")), "age_s": _file_age(now, path)}


def _well_formed(entry) -> bool:
    """登记项的形状：对象，pid 是 32 位以内的正整数，started_utc 是时间，kind 是 1 到 16 个小写字母的
    标识（它就是非运行器角色的键，会进问题代码、输出和通知），运行器带正整数 magic。
    形状不对的项没法核对身份。"""
    return (isinstance(entry, dict) and type(entry.get("pid")) is int and 0 < entry["pid"] < 2 ** 32
            and parse_utc(entry.get("started_utc")) is not None
            and isinstance(entry.get("kind"), str) and IDENTIFIER.fullmatch(entry["kind"])
            and (entry["kind"] != "runner" or type(entry.get("magic")) is int and entry["magic"] > 0))


def observe(live_dir, reports_dir, disk_path, now: datetime, alive, expect_keeper=False, keeper_state=None) -> dict:
    """只读一次现场。任何一项读不到都如实记 None，不猜。alive(pid, started_utc) 是进程探针。
    expect_keeper 为假时不看护任务的状态文件，obs["keeper"] 是 None；为真时读 keeper_state，
    缺省是 <live 的上一级>/ops-evidence/terminal-keeper/state.json（看护脚本的 -LogPath 所在目录）。"""
    live, now = Path(live_dir), now.astimezone(timezone.utc)
    ahead: list[str] = []          # 比 now 晚 60 秒以上的时间戳，按来源
    obs = {"roles": {}, "registry_readable": True, "registry_valid": True, "clock_ahead": ahead,
           "earliest_start": None, "supervisor_age_s": None, "halt": None, "pauses": [], "kill_switch": False,
           "risk_veto": False, "disk_known": True, "disk_free_gb": None, "due_report": None,
           "due_report_present": None, "keeper": None}
    registry = _read_json(live / "instances.json")[1]
    if registry is UNREADABLE:                  # 不存在、读不出、解析不了
        obs["registry_readable"] = False
    elif not isinstance(registry, dict) or not isinstance(registry.get("instances"), list):
        obs["registry_valid"] = False           # 顶层不成形状（null 也是）：和条目不成形状同样处理
    else:
        starts = []
        for entry in registry["instances"]:
            if not _well_formed(entry):
                obs["registry_valid"] = False      # 身份对不上就不能证明在跑；不记成角色消失
                continue
            key = str(entry["magic"]) if entry["kind"] == "runner" else entry["kind"]
            heartbeat = _read_json(live / f"heartbeat-{key}.json")[1]
            # 读不出、损坏、不是对象的心跳都算陈旧（年龄记 None），不让检查因此出错
            stamp = heartbeat.get("ts") if isinstance(heartbeat, dict) else None
            age = _age(now, stamp)
            if parse_utc(stamp) is not None and age is None:
                ahead.append(f"heartbeat-{key}")
            started = parse_utc(entry["started_utc"])
            starts.append(started)
            waiting = _terminal_wait(heartbeat.get("last_error") if isinstance(heartbeat, dict) else None)
            # 启动等待的角色从启动起就在等：现在减去 started_utc 是它等了多久的下界。
            obs["roles"][key] = {"launcher_alive": bool(alive(entry["pid"], entry["started_utc"])),
                                 "heartbeat_age_s": age, "terminal": waiting,
                                 "waiting_s": max((now - started).total_seconds(), 0.0)
                                 if waiting == "startup" and started is not None else None}
        if not starts:                          # 登记表是空的：部署之后不该这样
            obs["registry_valid"] = False
        else:
            obs["earliest_start"] = min(starts).isoformat()
    state_path = live / "supervisor-state.json"
    exists, supervisor = _read_json(state_path)
    # 不是对象的状态文件和读不出、损坏的一样：没有 last_checked，按文件修改时间算
    checked = supervisor.get("last_checked") if isinstance(supervisor, dict) else None
    age = _age(now, checked)
    if parse_utc(checked) is None:
        age = _file_age(now, state_path) if exists else None     # 没有 last_checked：按文件修改时间算
    elif age is None:
        ahead.append("supervisor")
    obs["supervisor_age_s"] = age
    exists, halt = _read_json(live / "halt.json")
    if exists:
        # 存在即停机（生产是 fail-closed）：读不出、形状不对的也算存在，只标明读不出
        obs["halt"] = ({**_control(now, halt, live / "halt.json", "halt", ahead), "owner": _owner(halt.get("owner"))}
                       if isinstance(halt, dict)
                       else {"unreadable": True, "age_s": _file_age(now, live / "halt.json")})
    for path in sorted(live.glob("pause-*.json")):
        _, pause = _read_json(path)
        magic = path.name[len("pause-"):-len(".json")]
        if not isinstance(pause, dict) or type(pause.get("paused")) is not bool:
            # 读不出、缺 paused 字段：生产按暂停处理（fail-closed）
            obs["pauses"].append({"magic": magic, "unreadable": True, "age_s": _file_age(now, path)})
        elif pause["paused"]:
            obs["pauses"].append({"magic": magic, **_control(now, pause, path, f"pause-{magic}", ahead)})
    obs["kill_switch"] = (live / "killSwitch").exists()
    obs["risk_veto"] = (live / "halt-risk-veto.json").exists()
    try:
        obs["disk_free_gb"] = round(shutil.disk_usage(disk_path).free / GB, 2)
    except OSError:
        obs["disk_known"] = False                # 查不了余量：记 disk_unknown，不静默跳过
    due = (now - timedelta(days=1 if (now.hour, now.minute) >= REPORT_DUE_UTC else 2)).date()
    obs["due_report"] = due.isoformat()
    obs["due_report_present"] = ((Path(reports_dir) / "live" / f"daily-{due.isoformat()}.md").is_file()
                                 if Path(reports_dir).is_dir() else None)
    if expect_keeper:
        state_path = (Path(keeper_state) if keeper_state is not None
                      else live.resolve().parent / "ops-evidence" / "terminal-keeper" / "state.json")
        obs["keeper"] = _keeper(now, state_path, ahead)
    return obs


def _owed(report_day: str, earliest) -> bool:
    """那一天的日报只在它生成的时候部署已经在跑才要求：生成时刻是它的 UTC 日次日 21:57 UTC，
    登记表里最早的 started_utc 得早于那个时刻。登记里的 started_utc 被写早了，这里只能信它：
    会多报，不会漏报。没有登记项可依据时不要求（那时登记表自己另有发现）。"""
    started, midnight = parse_utc(earliest), parse_utc(report_day)
    made = None if midnight is None else midnight + timedelta(days=1, hours=REPORT_MADE_UTC[0],
                                                             minutes=REPORT_MADE_UTC[1])
    return started is not None and made is not None and started < made


def _production(code: str, detail: dict, lasted: float) -> bool:
    age = detail.get("age_s")
    lasted = max(lasted, age if isinstance(age, (int, float)) else lasted)
    if code.startswith(("role_missing:", "heartbeat_stale:", "terminal_absent:", "supervisor_state_stale",
                        "registry_unreadable", "keeper_absent", "keeper_stale", "keeper_refused")):
        # 年龄来自现场自己的（心跳年龄、看护状态文件的年龄、启动等待的下界）：现场已经持续了
        # 这么久的，第一次看到就是生产级
        return lasted >= NOT_REVIVED_S
    if code.startswith("pause:") or code in ("halt", "kill_switch", "risk_veto"):
        return lasted >= CONTROL_STUCK_S
    # disk_low、disk_unknown、report_missing、registry_invalid、clock_ahead:*：一出现就是生产级
    return (code in ("disk_low", "disk_unknown", "report_missing", "registry_invalid")
            or code.startswith("clock_ahead:"))


def evaluate(obs: dict, now: datetime, since: dict) -> tuple[list[dict], dict]:
    """把现场变成问题清单。since 记每个问题第一次被看到的时刻（ISO），只保留仍在的问题。"""
    now = now.astimezone(timezone.utc)
    present: dict[str, dict] = {}
    stamp = now.isoformat()
    if not obs["registry_readable"]:
        present["registry_unreadable"] = {}
    elif not obs["registry_valid"]:
        present["registry_invalid"] = {}
    for key, role in sorted(obs["roles"].items()):
        if not role["launcher_alive"]:
            # The heartbeat's own age says how long the role has been down: if nobody
            # revived it, that is already past the limit on the very first look.
            present[f"role_missing:{key}"] = {"role": key, "age_s": role["heartbeat_age_s"]}
        elif role["heartbeat_age_s"] is None or role["heartbeat_age_s"] > HEARTBEAT_MAX_AGE_S:
            present[f"heartbeat_stale:{key}"] = {"role": key, "age_s": role["heartbeat_age_s"]}
        elif role.get("terminal"):
            # 进程在、心跳新鲜，心跳自己说没有终端：启动等待带"从启动起等了多久"的下界，
            # 连上之后丢了终端没有可信的起点，只靠检查器自己的时钟。
            present[f"terminal_absent:{key}"] = {"role": key, "kind": role["terminal"], "age_s": role["waiting_s"]}
    keeper = obs.get("keeper")
    if keeper is not None:                     # None：没有 --expect-keeper，不看护任务
        if not keeper["present"]:
            present["keeper_absent"] = {}
        else:
            if keeper["age_s"] is None or keeper["age_s"] > KEEPER_STALE_S:
                present["keeper_stale"] = {"age_s": keeper["age_s"]}
            if keeper["outcome"] in ("refused", "start_failed"):
                present["keeper_refused"] = {"outcome": keeper["outcome"], "reason": keeper["reason"]}
    if obs["supervisor_age_s"] is None or obs["supervisor_age_s"] > HEARTBEAT_MAX_AGE_S:
        present["supervisor_state_stale"] = {"age_s": obs["supervisor_age_s"]}
    if obs["halt"]:
        present["halt"] = dict(obs["halt"])
    for pause in obs["pauses"]:
        present[f"pause:{pause['magic']}"] = dict(pause)
    for source in obs["clock_ahead"]:
        present[f"clock_ahead:{source}"] = {}
    for code, flag in (("kill_switch", obs["kill_switch"]), ("risk_veto", obs["risk_veto"])):
        if flag:
            present[code] = {}
    if not obs["disk_known"]:
        present["disk_unknown"] = {}
    elif obs["disk_free_gb"] is not None and obs["disk_free_gb"] < DISK_MIN_GB:
        present["disk_low"] = {"free_gb": obs["disk_free_gb"]}
    if obs["due_report_present"] is False and _owed(obs["due_report"], obs["earliest_start"]):
        present["report_missing"] = {"report": obs["due_report"]}
    since = {code: since.get(code, stamp) for code in present}
    findings = []
    for code, detail in present.items():
        lasted = max((now - parse_utc(since[code])).total_seconds(), 0.0)
        findings.append({"code": code, "since": since[code], "lasted_s": round(lasted),
                         "production": _production(code, detail, lasted), **detail})
    return findings, since


def _shown(lasted: float) -> str:
    """不足 1 分钟的写"刚发现"，不写 0 分钟。"""
    return "刚发现" if lasted < 60 else f"{int(lasted // 60)}分"


def _duration(item: dict) -> float:
    """消息里的时长。halt、pause 取"文件修改时间距今"（age_s）和"检查器第一次看到至今"（lasted_s）里大的
    那个，和判级用的是同一个数：对账进程每 30 秒重写自己的 pause 文件，只看文件时间会一直像刚出现。
    terminal_absent（启动等待的下界）、keeper_stale（状态文件的年龄）同样取大的那个。其余只有检查器
    自己的时钟。"""
    age = item.get("age_s")
    from_file = item["code"] == "halt" or item["code"].startswith(("pause:", "terminal_absent:", "keeper_"))
    return max(item["lasted_s"], age) if from_file and isinstance(age, (int, float)) else item["lasted_s"]


def format_message(items: list[dict], recovered=()) -> str:
    """通知消息：只有问题代码、时长，以及已恢复的问题代码。"""
    parts = [f"{item['code']}({_shown(_duration(item))})" for item in items]
    if recovered:
        parts.append("已恢复 " + " ".join(sorted(recovered)))
    return "MT5 Demo 值守：" + " ".join(parts)


def track(findings: list[dict], since: dict, state: dict, now: datetime) -> tuple[list[dict], list[str], dict]:
    """排通知。→ (要通知的生产级问题, 已恢复的代码, 要写回的状态)。
    同一个生产级问题第一次出现时通知一次；还在的话每 24 小时最多再提醒一次；消失时报"已恢复"。
    "是不是同一个问题、从什么时候算"全靠 state 里记的；state 空的就都是第一次发现。"""
    now = now.astimezone(timezone.utc)
    open_, recovered = dict(state["open"]), dict(state["recovered"])
    for code in since:                        # 又出现了：撤回还没发出去的"已恢复"
        recovered.pop(code, None)
    announce = []
    for item in findings:
        record = open_.setdefault(item["code"], {"since": since[item["code"]], "notified": None})
        record["production"] = item["production"]
        last = parse_utc(record["notified"])
        if item["production"] and (last is None or (now - last).total_seconds() >= REMIND_AFTER_S):
            announce.append(item)
    for code, record in open_.items():
        if code not in since and record["production"]:
            recovered.setdefault(code, now.isoformat())     # 上次还在、这次没了
    return announce, sorted(recovered), {"open": {code: open_[code] for code in since}, "recovered": recovered}


def mark_sent(state: dict, announce: list[dict], now: datetime) -> None:
    """通知确实发出去了才记：发失败了，下一次按"还没通知过"再发。"""
    for item in announce:
        state["open"][item["code"]]["notified"] = now.astimezone(timezone.utc).isoformat()
    state["recovered"].clear()


def assess(obs: dict, now: datetime, state: dict) -> tuple[list[dict], tuple[list[dict], list[str], dict]]:
    """一次检查的判定：问题清单，以及通知安排 (要通知的生产级问题, 已恢复的代码, 要写回的状态)。"""
    findings, since = evaluate(obs, now, {code: record["since"] for code, record in state["open"].items()})
    return findings, track(findings, since, state, now)


def blank_state() -> dict:
    return {"open": {}, "recovered": {}}


def _valid_record(record) -> bool:
    return (isinstance(record, dict) and parse_utc(record.get("since")) is not None
            and "notified" in record and (record["notified"] is None or parse_utc(record["notified"]) is not None)
            and type(record.get("production")) is bool)


def checked_state(raw) -> tuple[dict, bool | None]:
    """状态文件的形状：{"open": {代码: {"since", "notified", "production"}}, "recovered": {代码: 时刻}}。
    → (状态, state_reset 标记)。形状不对的当空状态（调用方重新起算），标记为 True。"""
    if raw == []:
        return blank_state(), None             # 空的：没有丢东西
    if (isinstance(raw, dict) and isinstance(raw.get("open"), dict) and isinstance(raw.get("recovered"), dict)
            and all(_valid_record(record) for record in raw["open"].values())
            and all(parse_utc(stamp) is not None for stamp in raw["recovered"].values())):
        return raw, None
    return blank_state(), True


def load_state(path) -> tuple[dict, bool | str | None]:
    """读状态文件。→ (状态, state_reset 标记)：没有文件 → (空, None)；读不出 → (空, 异常类名)；
    形状不对 → (空, True)。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return blank_state(), None
    except (OSError, ValueError) as exc:
        return blank_state(), type(exc).__name__
    return checked_state(raw)


def notify(message, config_path, senders: dict, send=None) -> tuple[str, str | None]:
    """→ (notified, 异常类名)。没有可用渠道时，不管有没有要发的消息，都如实报 no_channel /
    unsupported_channel：有问题但没人收到，要看得出来。发送失败只记异常类名，结论由调用方照常下。
    配置文件只在本机（*.local.json 已被 .gitignore 忽略），不入库。"""
    config = _read_json(Path(config_path))[1]
    kind = config.get("type") if isinstance(config, dict) else None
    if not kind:
        return "no_channel", None
    sender = senders.get(kind)
    if sender is None:
        return "unsupported_channel", None
    if message is None:
        return "none", None
    try:
        (send or sender)(config, message)
    except Exception as exc:                   # 发送是尽力而为：只留异常类名，不带路径
        return "failed", type(exc).__name__
    return "sent", None
