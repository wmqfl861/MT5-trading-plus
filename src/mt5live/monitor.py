"""mt5live.monitor（T14/P7）：监控——心跳、即时告警、日报。

文件契约（与信号运行器/执行器/T14a 的集成约定，全部落在 data/live/）：
- ``heartbeat.json``：``{"runner_alive": bool, "ts": <iso8601-UTC>}``——
  信号运行器每轮原子写；风控进程发现超时会改写 runner_alive=false。
- ``alerts.log``：追加式文本，每行 ``{iso}\t{KIND}\t{消息}``。
- 日报：``{reports_dir}/live/daily-YYYY-MM-DD.md``。

时间约定与仓库一致：内存中一律"贴 UTC 标签的 naive datetime"；
文件里的 ISO 时间带 +00:00 后缀（无歧义），读取时归一化回 naive。
"""
from __future__ import annotations

import json
import logging
import math
import os
import faulthandler
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from mt5data.store import atomic_replace, fsync_file
from mt5live.fileio import append_line, file_lock, write_json_atomic
from mt5quant.costs.spread import SpreadModel

HEARTBEAT_FILE = "heartbeat.json"
ALERTS_FILE = "alerts.log"
DAILY_REPORT_SUBDIR = "live"  # data/reports/live/

# 即时告警类别（任务约定的三类 + 账户轮询失败 + 开仓被拒 + 心跳写失败）
ALERT_HEARTBEAT_TIMEOUT = "HEARTBEAT_TIMEOUT"    # 断线：心跳超时/缺失
ALERT_RECONCILE_MISMATCH = "RECONCILE_MISMATCH"  # 持仓对账不一致
ALERT_RISK_HALT = "RISK_HALT"                    # 风控触发停机
ALERT_RISK_FLATTEN = "RISK_FLATTEN"              # 风控触发全平
ALERT_ACCOUNT_POLL_ERROR = "ACCOUNT_POLL_ERROR"  # 账户轮询失败
ALERT_RISK_REJECT = "RISK_REJECT"                # 开仓被风控/闸门拒绝（T24）
ALERT_HEARTBEAT_WRITE_ERROR = "HEARTBEAT_WRITE_ERROR"  # 心跳写入连续失败（T33/M1）

DEFAULT_HEARTBEAT_TIMEOUT_S = 120.0

# T141（第十六轮计划 §2，O16-3）：断线容忍与专用退出。
# - 风控/对账进程对断线降级继续（文件级检查照常、角色心跳照写），连续
#   DISCONNECT_LIMIT_S 连不上才以专用退出码 DISCONNECT_EXIT_RC 退出，
#   交给 supervisor（T143），并留 disconnect-exit-<component>.json 标记；
# - 专用码取值避开既有退出码：0=正常/中断、1=单轮业务失败、2=fatal
#   （guarded_main）、3=单实例拒绝、75=T110 网关期限硬停。
DISCONNECT_EXIT_RC = 78
DISCONNECT_LIMIT_S = 15 * 60.0        # 连续断线容忍上限：15 分钟
ALERT_DISCONNECTED = "DISCONNECTED"   # 断线段首/恢复/专用退出告警
DISCONNECT_EXIT_FILE_FMT = "disconnect-exit-{component}.json"

# T142（第十六轮计划，O16-3）：主循环异常分级与专用退出。
#
# 风控/对账主循环异常分两类（scripts/risk_process.py、scripts/live_reconcile.py）：
# - 已知可恢复（文件被占用 PermissionError、文件锁等待 TimeoutError；T142
#   期还包括数据暂缺 DataUnavailableError——T184 起后者改走独立时间窗，
#   见下）：记录后继续；
# - 断线只走 T141 的 15 分钟时间窗（78），**不进 5 轮计数**——统一计数会让
#   5 轮 × interval 早于 900s 触发，破坏 T141 验收场景"断线 3 分钟恢复
#   三进程不退出"；两套计时互不读写（钉死在 tests/test_t142_loop_grading.py）；
# - 其余可恢复异常连续 LOOP_FAILURE_LIMIT 轮失败，或任何未知异常：写
#   loop-exit-<component>.json（reason=unrecoverable_loop/<具体>）+ halt
#   （fail-closed：安全监视者离开必须停交易）后以 LOOP_EXIT_RC 退出。
#
# 退出码表（supervisor/取证统一汇总；三进程共用 guarded_main 边界）：
#   0  = 正常完成 / Ctrl-C 中断（exit-<component>.json：completed/interrupted）
#   1  = 单轮业务失败（--once 轮询失败 / 对账不一致——动作不是故障）
#   2  = fatal：启动失败或主循环外的未知异常（guarded_main，写 halt；
#        例外：reconcile --once 的业务性 rc=2=意图缺失/持仓查询失败，
#        不写 halt、不经 guarded_main——见 live_reconcile 文档）
#   3  = 单实例拒绝（instances 内核锁，首道拒绝不连网关）
#   75 = T110 网关期限硬停：单次 MT5 调用超 30s（os._exit，防原生/GIL 挂死；
#        落 gateway-stall.json + exit-<component>.json，不写 halt）
#   78 = T141 断线专用：连续 15 分钟连不上（disconnect-exit-<component>.json，
#        不写 halt——重启后果由 supervisor 决定）
#   79 = T142 主循环不可恢复：文件访问类可恢复异常连续 5 轮失败或未知异常
#        （loop-exit-<component>.json + halt——fail-closed）；T184 起账户取数
#        连续不可用达 ACCOUNT_UNAVAILABLE_LIMIT_S 也走本码（cause=
#        account_unavailable_window，halt 语义不变——O17-4 只改触发条件）
LOOP_EXIT_RC = 79
LOOP_FAILURE_LIMIT = 5              # 连续失败轮上限（只数可恢复失败轮）
ALERT_RECOVERABLE_FAILURE = "RECOVERABLE_FAILURE"  # 失败段首告警（不刷屏）
ALERT_LOOP_EXIT = "LOOP_EXIT"       # 专用退出告警
LOOP_EXIT_FILE_FMT = "loop-exit-{component}.json"

# T184（第十七轮计划 §T184，O17-4/P17-4）：账户取数失败改按独立时间窗计。
# - T142 把 DataUnavailableError 计入 5 轮上限：风控 --interval 10 下约 50
#   秒取不到账户就以 79 退出并写 owner=<component> 的 halt，而 supervisor
#   只清自己写的 halt——终端一抖就把交易停到人工解除；
# - 与 T141 DisconnectWatch 同构（时间契约优先）：单调钟累计连续失败时长，
#   段首一条告警、恢复一条、段内不刷屏；连续 ACCOUNT_UNAVAILABLE_LIMIT_S
#   （缺省 300 秒，CLI --account-unavailable-limit-s）才退出；
# - 退出仍复用 T142 的 79 语义（halt 分层不变）：loop-exit-<component>.json
#   （cause=account_unavailable_window）+ halt（fail-closed）+ LOOP_EXIT 告警；
# - 三套计时互不读写（钉死在 tests/test_t184_account_window.py）：断线只走
#   T141 的 900s 窗（78）；文件占用/锁超时（PermissionError/TimeoutError）
#   仍走 T142 的 5 轮计数；断线轮对其他两套计时中性（不 note_ok 不
#   note_failure），只有完整成功的轮清零。
ACCOUNT_UNAVAILABLE_LIMIT_S = 300.0    # 连续账户取数失败容忍上限：5 分钟
ALERT_ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"  # 段首/恢复告警（不刷屏）
ACCOUNT_UNAVAILABLE_CAUSE = "account_unavailable_window"  # 退出标记 cause

# 成交流水的交易类 entry（mt5gw.models.Deal.entry）；出入金等流水绝不计入盈亏
TRADE_ENTRY_KINDS = frozenset({"in", "out", "inout", "out_by"})


# ---- 时间工具（供 riskproc/reconcile 复用） ----

def naive_utc(dt: datetime) -> datetime:
    """归一化为贴 UTC 标签的 naive datetime；naive 输入原样通过。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso_utc(dt: datetime) -> str:
    """写文件用的无歧义 ISO 时间（总是带 +00:00）。"""
    return naive_utc(dt).replace(tzinfo=timezone.utc).isoformat()


def parse_iso(text: str) -> datetime:
    """读文件用：ISO → naive UTC；带时区则换算，naive 视为 UTC 标签。"""
    ts = datetime.fromisoformat(text)
    return naive_utc(ts)


# ---- 心跳 ----

@dataclass(frozen=True)
class Heartbeat:
    runner_alive: bool
    ts: datetime  # naive UTC


def write_heartbeat(live_dir: str, runner_alive: bool, *, ts: datetime) -> None:
    """原子写心跳文件（运行器每轮调用；风控超时改写 runner_alive=false）。"""
    write_json_atomic(
        os.path.join(live_dir, HEARTBEAT_FILE),
        {"runner_alive": bool(runner_alive), "ts": iso_utc(ts)},
    )


def read_heartbeat_path(path: str) -> Heartbeat | None:
    """读指定路径的心跳文件（T24：每策略一个 heartbeat-<magic>.json）。

    文件不存在 → None，内容不合法 → ValueError（如实抛错）。
    """
    data = read_json_retry(path)
    if data is None:
        return None
    if (not isinstance(data, dict) or "runner_alive" not in data
            or "ts" not in data):
        raise ValueError(f"{path} 缺 runner_alive/ts 字段：{data!r}")
    if not isinstance(data["runner_alive"], bool):
        raise ValueError(
            f"{path} runner_alive 须为 bool，收到 {data['runner_alive']!r}")
    try:
        ts = parse_iso(data["ts"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"{path} ts 不是合法 ISO 时间：{data['ts']!r}") from e
    return Heartbeat(bool(data["runner_alive"]), ts)


def heartbeat_files(live_dir: str) -> list[str]:
    """live 目录下全部心跳文件路径：每策略 heartbeat-<magic>.json（T24/M9）
    + 兼容的旧版单文件 heartbeat.json（存在才列入）。"""
    per_magic = sorted(
        os.path.join(live_dir, name)
        for name in os.listdir(live_dir)
        if name.startswith("heartbeat-") and name.endswith(".json")
        and name[len("heartbeat-"):-len(".json")].isdigit())
    legacy = os.path.join(live_dir, HEARTBEAT_FILE)
    if os.path.exists(legacy):
        per_magic.append(legacy)
    return per_magic


def mark_heartbeat_stale(path: str, hb: Heartbeat) -> None:
    """把超时的心跳文件改写为 runner_alive=false（保留其余字段）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["runner_alive"] = False
    data["ts"] = iso_utc(hb.ts)
    write_json_atomic(path, data)


def read_heartbeat(live_dir: str) -> Heartbeat | None:
    """读默认心跳（旧版单文件路径；每策略文件用 read_heartbeat_path）。"""
    return read_heartbeat_path(os.path.join(live_dir, HEARTBEAT_FILE))


@dataclass(frozen=True)
class HeartbeatStatus:
    ok: bool            # 运行器存活且心跳新鲜
    reason: str         # "ok" / "timeout" / "runner_stopped" / "missing"
    age_s: float | None  # 心跳距今秒数（missing 时 None）


def heartbeat_status(hb: Heartbeat | None, now: datetime,
                     *, timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
                     ) -> HeartbeatStatus:
    """心跳裁决：缺失/超时/运行器自报停止都判 not ok（fail-closed）。"""
    if hb is None:
        return HeartbeatStatus(False, "missing", None)
    age = (naive_utc(now) - hb.ts).total_seconds()
    if age < 0:
        return HeartbeatStatus(False, "future", age)
    if not hb.runner_alive:
        return HeartbeatStatus(False, "runner_stopped", age)
    if age > timeout_s:
        return HeartbeatStatus(False, "timeout", age)
    return HeartbeatStatus(True, "ok", age)


def check_heartbeat(live_dir: str, now: datetime,
                    *, timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
                    ) -> HeartbeatStatus:
    return heartbeat_status(read_heartbeat(live_dir), now, timeout_s=timeout_s)


# ---- T110 completed-loop health and process evidence ----

def read_json_retry(path: str):
    """Bounded Windows sharing retry; persistent access failure stays visible."""
    for attempt in range(3):
        try:
            with open(path, encoding="utf-8-sig") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05)


def health_timeout(interval: float) -> float:
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or interval <= 0):
        raise ValueError("interval must be finite and positive")
    return max(120.0, 3.0 * interval)


def write_role_heartbeat(live_dir: str, component: str, *, interval: float,
                         now: datetime | None = None, **details) -> None:
    health_timeout(interval)
    write_json_atomic(os.path.join(live_dir, f"heartbeat-{component}.json"), {
        **details, "component": component, "runner_alive": True,
        "ts": iso_utc(now or datetime.now(timezone.utc)),
        "interval_s": interval, "pid": os.getpid(),
        "deployment_sha": os.environ.get("MT5_DEPLOYMENT_SHA", "")})


def risk_health(live_dir: str, *, clock=None, interval: float = 10.0) -> HeartbeatStatus:
    # Observe time AFTER the read: a writer may complete during sharing retries.
    # Configured interval, never an untrusted heartbeat's self-declared TTL.
    observe = clock or (lambda: datetime.now(timezone.utc))
    try:
        hb = read_heartbeat_path(os.path.join(live_dir, "heartbeat-risk.json"))
        return heartbeat_status(hb, observe(), timeout_s=health_timeout(interval))
    except (OSError, ValueError):
        return HeartbeatStatus(False, "unreadable", None)


def write_halt_once(live_dir: str, reason: str, now: datetime, *,
                    owner: str = "risk", token: str | None = None) -> bool:
    """Shared lock + exclusive create preserves even a concurrent manual halt."""
    os.makedirs(live_dir, exist_ok=True)
    with file_lock(os.path.join(live_dir, "halt.lock")):
        try:
            with open(os.path.join(live_dir, "halt.json"), "x", encoding="utf-8") as f:
                json.dump({"reason": reason, "ts": iso_utc(now), "owner": owner,
                           "token": token or uuid.uuid4().hex}, f)
                f.flush()
                os.fsync(f.fileno())
        except FileExistsError:
            existing = read_json_retry(os.path.join(live_dir, "halt.json"))
            if owner != "supervisor" and isinstance(existing, dict) and existing.get("owner") == "supervisor":
                # Preserve the first owner/reason, but do not let its later
                # auto-clear erase a concurrent independent risk/manual stop.
                write_json_atomic(os.path.join(live_dir, "halt-risk-veto.json"), {
                    "reason": reason, "owner": owner, "ts": iso_utc(now)})
            return False
    append_alert(live_dir, ALERT_RISK_HALT, f"{owner} halt: {reason}", now)
    return True


def reset_watchdog(interval: float) -> None:
    # Stack evidence only: the independent supervisor handles hard process stalls.
    faulthandler.dump_traceback_later(health_timeout(interval), repeat=True, file=2)


class DisconnectWatch:
    """T141：进程级连续断线计时（风控/对账主循环用）。

    - note_failure(exc)：每轮网关调用抛 NotConnectedError 后调用；断线段
      首写一次告警（不刷屏），单调钟累计连续断线时长；
    - note_ok()：每轮成功后调用；若此前处于断线段，写恢复告警并清零；
    - expired()：连续断线 ≥ limit_s（默认 15 分钟）；
    - exit_code()：写 disconnect-exit-<component>.json 退出标记（T143
      supervisor 演练与取证读它：reason/rc/首末时刻/时长/最后错误/pid）
      + 退出告警，返回专用退出码 78——调用方直接 return，guarded_main
      的 finally 会再落 exit-<component>.json（rc=78）。断线退出**不写
      halt**：进程死亡/重启的后果由 supervisor 决定，不自作主张。
    """

    def __init__(self, live_dir: str, component: str, *,
                 limit_s: float = DISCONNECT_LIMIT_S,
                 clock=time.monotonic):
        self.live_dir = str(live_dir)
        self.component = str(component)
        self.limit_s = float(limit_s)
        self._clock = clock
        self._first_mono: float | None = None
        self._first_wall: datetime | None = None
        self._last_error = ""

    @property
    def disconnected_s(self) -> float:
        if self._first_mono is None:
            return 0.0
        return self._clock() - self._first_mono

    def note_failure(self, exc: BaseException | None = None) -> None:
        if exc is not None:
            self._last_error = f"{type(exc).__name__}: {exc}"
        if self._first_mono is not None:
            return                       # 断线段已在计时（告警只写段首）
        self._first_mono = self._clock()
        self._first_wall = datetime.now(timezone.utc)
        append_alert(
            self.live_dir, ALERT_DISCONNECTED,
            f"{self.component} 进程与交易服务器断线（{self._last_error}）——"
            f"降级继续（文件级检查照常），网关按 5/10/30/60 秒退避重连；"
            f"连续 {self.limit_s / 60:.0f} 分钟连不上将以退出码 "
            f"{DISCONNECT_EXIT_RC} 交给 supervisor（T141）",
            self._first_wall)

    def note_ok(self) -> None:
        if self._first_mono is None:
            return
        duration = self.disconnected_s
        self._first_mono = self._first_wall = None
        append_alert(
            self.live_dir, ALERT_DISCONNECTED,
            f"{self.component} 进程与交易服务器恢复连接（断线 {duration:.0f} "
            "秒）——恢复正常轮询（T141）")

    def expired(self) -> bool:
        return (self._first_mono is not None
                and self.disconnected_s >= self.limit_s)

    def exit_code(self, *, last_error: str | None = None) -> int:
        """落退出标记 + 告警，返回专用退出码（调用方直接 return 它）。"""
        detail = last_error or self._last_error
        duration = self.disconnected_s
        first = iso_utc(self._first_wall) if self._first_wall else None
        marker = {
            "component": self.component,
            "rc": DISCONNECT_EXIT_RC,
            "reason": "disconnect_timeout",
            "first_failure_utc": first,
            "duration_s": round(duration, 3),
            "limit_s": self.limit_s,
            "last_error": detail,
            "ts": iso_utc(datetime.now(timezone.utc)),
            "pid": os.getpid(),
        }
        write_json_atomic(os.path.join(
            self.live_dir,
            DISCONNECT_EXIT_FILE_FMT.format(component=self.component)),
            marker)
        append_alert(
            self.live_dir, ALERT_DISCONNECTED,
            f"{self.component} 进程连续断线 {duration / 60:.0f} 分钟"
            f"（≥ {self.limit_s / 60:.0f} 分钟上限，最后错误：{detail}）——"
            f"以专用退出码 {DISCONNECT_EXIT_RC} 退出交给 supervisor；退出"
            f"标记 {DISCONNECT_EXIT_FILE_FMT.format(component=self.component)}"
            "（T141，供 T143 演练与取证）")
        return DISCONNECT_EXIT_RC


class LoopFailureWatch:
    """T142：主循环"已知可恢复异常"的连续失败轮计数（风控/对账用）。

    与 T141 DisconnectWatch 的边界（时间契约优先，测试钉死）：
    - 断线轮**既不 note_failure 也不 note_ok**——只归 DisconnectWatch 的
      900s 时间窗管；本类根本不该看到 NotConnectedError；
    - T184 起账户取数失败（DataUnavailableError）同样不进本计数——只归
      AccountUnavailableWatch 的 300s 时间窗管；本类只数文件访问类
      （PermissionError/TimeoutError）失败轮；
    - 只有完整成功的轮清零（risk=run_one_cycle 正常返回；reconcile=
      run_once rc∈{0,1}——1 是"按规则写 pause"的动作不是故障）；
    - 恢复（note_ok）静默清零，不写告警（瞬时失败是常态，避免刷屏；
      失败段首有 RECOVERABLE_FAILURE 告警）。

    exit_code()：写 loop-exit-<component>.json（reason=unrecoverable_loop/
    <具体>、cause=consecutive_failures|unknown_exception、轮数/上限/首末
    信息）+ halt（fail-closed：监视者离开必须停交易）+ LOOP_EXIT 告警，
    返回专用退出码 79——调用方直接 return，guarded_main 的 finally 会再落
    exit-<component>.json（rc=79）。标记/halt/告警全部尽力而为：文件系统
    正是被占用的故障源时，退到 stderr 打印（.log.err 可见），退出不因
    留痕失败而取消。
    """

    def __init__(self, live_dir: str, component: str, *,
                 limit: int = LOOP_FAILURE_LIMIT):
        self.live_dir = str(live_dir)
        self.component = str(component)
        self.limit = int(limit)
        self._count = 0
        self._first_wall: datetime | None = None
        self._last_detail = ""

    @property
    def consecutive_failures(self) -> int:
        return self._count

    @staticmethod
    def _detail_of(exc_or_detail) -> str:
        if isinstance(exc_or_detail, BaseException):
            return f"{type(exc_or_detail).__name__}: {exc_or_detail}"
        return str(exc_or_detail)

    def note_failure(self, exc_or_detail) -> None:
        """记一轮可恢复失败；失败段首写一次告警（不刷屏）。"""
        detail = self._detail_of(exc_or_detail)
        self._count += 1
        self._last_detail = detail
        if self._first_wall is None:
            self._first_wall = datetime.now(timezone.utc)
        if self._count == 1:                    # 段首告警一次
            try:
                append_alert(
                    self.live_dir, ALERT_RECOVERABLE_FAILURE,
                    f"{self.component} 主循环已知可恢复异常（{detail}）——"
                    f"记录后继续；连续 {self.limit} 轮失败将以专用退出码 "
                    f"{LOOP_EXIT_RC} 退出（T142）", self._first_wall)
            except Exception as e:              # 告警写失败不推翻计数
                print(f"× 失败段首告警写入失败（继续计数）：{e}",
                      file=sys.stderr, flush=True)

    def note_ok(self) -> None:
        """完整成功的轮：静默清零。"""
        self._count = 0
        self._first_wall = None

    def expired(self) -> bool:
        return self._count >= self.limit

    def exit_code(self, *, exc: BaseException | None = None,
                  detail: str | None = None,
                  cause: str = "consecutive_failures") -> int:
        """落退出标记 + halt + 告警（全部尽力而为），返回专用退出码 79。"""
        last = detail or (self._detail_of(exc) if exc is not None
                          else self._last_detail)
        reason = f"unrecoverable_loop/{last}"
        now = datetime.now(timezone.utc)
        marker = {
            "component": self.component,
            "rc": LOOP_EXIT_RC,
            "reason": reason,
            "cause": cause,
            "consecutive_failures": self._count,
            "limit_rounds": self.limit,
            "first_failure_utc": (iso_utc(self._first_wall)
                                  if self._first_wall else None),
            "last_error": last,
            "ts": iso_utc(now),
            "pid": os.getpid(),
        }
        logging.critical(
            "%s unrecoverable loop exit rc=%s cause=%s failures=%d/%d: %s",
            self.component, LOOP_EXIT_RC, cause, self._count, self.limit,
            last, exc_info=exc)
        try:
            write_json_atomic(os.path.join(
                self.live_dir,
                LOOP_EXIT_FILE_FMT.format(component=self.component)), marker)
        except Exception as e:
            print(f"× 退出标记写入失败（{reason}）：{e}",
                  file=sys.stderr, flush=True)
        try:   # fail-closed：安全监视者离开必须停交易（78 断线除外，见码表）
            write_halt_once(
                self.live_dir,
                f"{self.component}_unrecoverable_loop: {last}", now,
                owner=self.component)
        except Exception as e:
            print(f"× 退出 halt 写入失败（{reason}）：{e}",
                  file=sys.stderr, flush=True)
        try:
            append_alert(
                self.live_dir, ALERT_LOOP_EXIT,
                f"{self.component} 主循环{'连续 ' + str(self._count) + ' 轮失败'
                   if cause == 'consecutive_failures' else '未知异常'}"
                f"（{last}）——写退出标记 "
                f"{LOOP_EXIT_FILE_FMT.format(component=self.component)} 并以"
                f"专用退出码 {LOOP_EXIT_RC} 退出；已写 halt（fail-closed）"
                "（T142）", now)
        except Exception as e:
            print(f"× 退出告警写入失败（{reason}）：{e}",
                  file=sys.stderr, flush=True)
        return LOOP_EXIT_RC


class AccountUnavailableWatch:
    """T184：账户/持仓取数失败（DataUnavailableError）的连续时间窗。

    与 DisconnectWatch 同构（时间契约优先）：单调钟累计连续失败时长；
    note_failure 失败段首写一次告警（不刷屏）；note_ok 恢复写一条并清零
    （只有完整成功的轮调用）；expired() = 连续失败 ≥ limit_s（缺省
    ACCOUNT_UNAVAILABLE_LIMIT_S=300 秒，CLI --account-unavailable-limit-s）。

    与另两套计时的边界（钉死在 tests/test_t184_account_window.py）：
    - 断线轮（NotConnectedError）不进本计时——只归 DisconnectWatch 的
      900s 窗（78）；断线段内的单调流逝如实累计（账户确实一直取不到），
      重连后首个失败轮按真实累计时长裁决；
    - 本计时的失败轮不进 LoopFailureWatch 的 5 轮计数；PermissionError/
      TimeoutError 也不进本计时；
    - 混合并发时先到期的窗先裁决（300s 账户窗早于 900s 断线窗）。

    exit_code()：与 T142 的 79 语义完全一致（O17-4 修复只改触发条件，
    不改 halt 分层）——loop-exit-<component>.json（cause=
    account_unavailable_window、reason=unrecoverable_loop/<具体>、首末
    时刻/时长/上限）+ halt（fail-closed：监视者离开必须停交易）+
    LOOP_EXIT 告警，返回 LOOP_EXIT_RC=79——调用方直接 return，guarded_main
    的 finally 会再落 exit-<component>.json（rc=79）。留痕全部尽力而为：
    文件系统正被占用时退到 stderr，退出不因留痕失败而取消。
    """

    def __init__(self, live_dir: str, component: str, *,
                 limit_s: float = ACCOUNT_UNAVAILABLE_LIMIT_S,
                 clock=time.monotonic):
        self.live_dir = str(live_dir)
        self.component = str(component)
        self.limit_s = float(limit_s)
        self._clock = clock
        self._first_mono: float | None = None
        self._first_wall: datetime | None = None
        self._last_error = ""

    @property
    def unavailable_s(self) -> float:
        if self._first_mono is None:
            return 0.0
        return self._clock() - self._first_mono

    def note_failure(self, exc: BaseException | None = None) -> None:
        """记一轮账户取数失败；失败段首写一次告警（不刷屏）。"""
        if exc is not None:
            self._last_error = f"{type(exc).__name__}: {exc}"
        if self._first_mono is not None:
            return                       # 失败段已在计时（告警只写段首）
        self._first_mono = self._clock()
        self._first_wall = datetime.now(timezone.utc)
        try:
            append_alert(
                self.live_dir, ALERT_ACCOUNT_UNAVAILABLE,
                f"{self.component} 账户取数失败（{self._last_error}）——按"
                f"时间窗计（T184，不进 5 轮上限），降级继续（文件级检查"
                f"照常、角色心跳照写）；连续 {self.limit_s / 60:.0f} 分钟"
                f"取不到将以专用退出码 {LOOP_EXIT_RC} 退出（O17-4）",
                self._first_wall)
        except Exception as e:          # 告警写失败不推翻计时
            print(f"× 失败段首告警写入失败（继续计时）：{e}",
                  file=sys.stderr, flush=True)

    def note_ok(self) -> None:
        """完整成功的轮：写恢复告警（段内才写）并清零。"""
        if self._first_mono is None:
            return
        duration = self.unavailable_s
        self._first_mono = self._first_wall = None
        try:
            append_alert(
                self.live_dir, ALERT_ACCOUNT_UNAVAILABLE,
                f"{self.component} 账户取数恢复（此前连续失败 {duration:.0f} "
                "秒，未达时间窗上限）——恢复正常轮询（T184）")
        except Exception as e:
            print(f"× 恢复告警写入失败（照常清零）：{e}",
                  file=sys.stderr, flush=True)

    def expired(self) -> bool:
        return (self._first_mono is not None
                and self.unavailable_s >= self.limit_s)

    def exit_code(self, *, last_error: str | None = None) -> int:
        """落退出标记 + halt + 告警（全部尽力而为），返回专用退出码 79。"""
        detail = last_error or self._last_error
        duration = self.unavailable_s
        first = iso_utc(self._first_wall) if self._first_wall else None
        reason = f"unrecoverable_loop/{detail}"
        now = datetime.now(timezone.utc)
        marker = {
            "component": self.component,
            "rc": LOOP_EXIT_RC,
            "reason": reason,
            "cause": ACCOUNT_UNAVAILABLE_CAUSE,
            "first_failure_utc": first,
            "duration_s": round(duration, 3),
            "limit_s": self.limit_s,
            "last_error": detail,
            "ts": iso_utc(now),
            "pid": os.getpid(),
        }
        logging.critical(
            "%s account-unavailable window exit rc=%s duration=%.0fs/"
            "limit=%.0fs: %s",
            self.component, LOOP_EXIT_RC, duration, self.limit_s, detail)
        try:
            write_json_atomic(os.path.join(
                self.live_dir,
                LOOP_EXIT_FILE_FMT.format(component=self.component)), marker)
        except Exception as e:
            print(f"× 退出标记写入失败（{reason}）：{e}",
                  file=sys.stderr, flush=True)
        try:   # fail-closed：监视者离开必须停交易（与 T142 同分层）
            write_halt_once(
                self.live_dir,
                f"{self.component}_unrecoverable_loop: {detail}", now,
                owner=self.component)
        except Exception as e:
            print(f"× 退出 halt 写入失败（{reason}）：{e}",
                  file=sys.stderr, flush=True)
        try:
            append_alert(
                self.live_dir, ALERT_LOOP_EXIT,
                f"{self.component} 账户取数连续 {duration:.0f} 秒不可用"
                f"（≥ {self.limit_s:.0f} 秒上限，最后错误：{detail}）——写"
                f"退出标记 {LOOP_EXIT_FILE_FMT.format(component=self.component)}"
                f" 并以专用退出码 {LOOP_EXIT_RC} 退出；已写 halt（fail-closed）"
                "（T184，O17-4）", now)
        except Exception as e:
            print(f"× 退出告警写入失败（{reason}）：{e}",
                  file=sys.stderr, flush=True)
        return LOOP_EXIT_RC


def guarded_main(component: str, live_dir: str, interval: float, function) -> int:
    """All three CLI roles use this boundary, including startup and shutdown."""
    old = {k: os.environ.get(k) for k in ("MT5_LIVE_DIR", "MT5_COMPONENT")}
    os.environ["MT5_LIVE_DIR"] = os.path.abspath(live_dir)
    os.environ["MT5_COMPONENT"] = component
    rc, reason = 2, "startup_failed"
    try:
        faulthandler.enable(file=2)
        reset_watchdog(interval)
        rc = function()
        reason = "completed" if rc == 0 else f"exit_code_{rc}"
        if rc:
            logging.critical("%s failed rc=%s", component, rc)
        return rc
    except KeyboardInterrupt:
        rc, reason = 0, "interrupted"
        return rc
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logging.critical("%s fatal: %s", component, reason, exc_info=True)
        write_halt_once(live_dir, f"{component}_fatal: {reason}", datetime.now(timezone.utc))
        return rc
    finally:
        faulthandler.cancel_dump_traceback_later()
        try:
            write_json_atomic(os.path.join(live_dir, f"exit-{component}.json"), {
                "component": component, "rc": rc, "reason": reason,
                "ts": iso_utc(datetime.now(timezone.utc)), "pid": os.getpid()})
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


# ---- 即时告警 ----

@dataclass(frozen=True)
class Alert:
    time: datetime
    kind: str
    message: str


def append_alert(live_dir: str, kind: str, message: str,
                 now: datetime | None = None) -> None:
    """追加一行告警（O_APPEND 单次 write + 共享冲突重试，T33 经 fileio）。"""
    ts = now if now is not None else datetime.now(timezone.utc)
    append_line(os.path.join(live_dir, ALERTS_FILE),
                f"{iso_utc(ts)}\t{kind}\t{message}\n")


def read_alerts(live_dir: str) -> tuple[Alert, ...]:
    """读全部告警（无文件 → 空元组）；行格式异常如实抛错。"""
    path = os.path.join(live_dir, ALERTS_FILE)
    if not os.path.exists(path):
        return ()
    out: list[Alert] = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln:
                continue
            parts = ln.split("\t", 2)
            if len(parts) != 3:
                raise ValueError(f"alerts.log 行格式异常（须 ts\\tKIND\\t消息）：{ln!r}")
            out.append(Alert(parse_iso(parts[0]), parts[1], parts[2]))
    return tuple(out)


# ---- 分策略盈亏（按 magic 聚合当日 deals） ----

@dataclass(frozen=True)
class MagicPnl:
    magic: int
    strategy: str   # 无映射时为空串
    n_deals: int
    volume: float   # 成交量（手）
    net_pnl: float  # profit+commission+swap+fee 合计（账户货币 USD）


def aggregate_by_magic(deals, *,
                       strategy_names: dict[int, str] | None = None,
                       ) -> tuple[MagicPnl, ...]:
    """按 magic 聚合成交净盈亏；出入金流水（entry=None）绝不计入。"""
    names = strategy_names or {}
    agg: dict[int, list] = {}
    for d in deals:
        if d.entry not in TRADE_ENTRY_KINDS:
            continue
        a = agg.setdefault(d.magic, [0, 0.0, 0.0])
        a[0] += 1
        a[1] += d.volume
        a[2] += d.profit + d.commission + d.swap + d.fee
    return tuple(
        MagicPnl(m, names.get(m, ""), n, float(v), float(p))
        for m, (n, v, p) in sorted(agg.items())
    )


# ---- 实际点差 vs SpreadModel p90 ----

@dataclass(frozen=True)
class SpreadSample:
    symbol: str
    hour_utc: int
    spread_price: float  # ask−bid（价格单位）
    mid: float           # (ask+bid)/2；relative 口径换算用


@dataclass(frozen=True)
class SpreadRow:
    symbol: str
    hour_utc: int
    n: int
    observed_max: float     # 与模型同口径（relative: 比率；absolute: 价格单位）
    model_p90: float | None  # 品种/小时不在模型中 → None（日报如实标注）
    ratio: float | None      # observed_max / model_p90
    breach: bool             # ratio > threshold_multiple


def compare_spreads(samples, model: SpreadModel, *,
                    threshold_multiple: float = 1.5) -> tuple[SpreadRow, ...]:
    """实测点差与模型 p90 比对（口径随模型：relative 按比率、absolute 按价差）。

    breach 判定：实测最大 / 模型 p90 > threshold_multiple。品种或小时不在
    模型中 → model_p90=None、breach=False（无法判定，不冒充超标）。
    """
    groups: dict[tuple[str, int], list[SpreadSample]] = {}
    for s in samples:
        groups.setdefault((s.symbol, int(s.hour_utc)), []).append(s)
    rows: list[SpreadRow] = []
    for (sym, hour), grp in sorted(groups.items()):
        if model.mode == "relative":
            if any(s.mid <= 0 for s in grp):
                bad = next(s for s in grp if s.mid <= 0)
                raise ValueError(
                    f"{sym} 小时 {hour} 样本 mid ≤ 0，无法换算相对点差：{bad!r}")
            observed = [s.spread_price / s.mid for s in grp]
        else:
            observed = [s.spread_price for s in grp]
        obs_max = max(observed)
        try:
            p90 = model.stat(hour, symbol=sym).p90
        except ValueError:
            p90 = None
        ratio = (obs_max / p90) if (p90 is not None and p90 > 0) else None
        rows.append(SpreadRow(
            symbol=sym, hour_utc=hour, n=len(grp), observed_max=obs_max,
            model_p90=p90, ratio=ratio,
            breach=bool(ratio is not None and ratio > threshold_multiple),
        ))
    return tuple(rows)


# ---- 日报 ----

def _fmt_pnl(v: float) -> str:
    return f"{v:+,.2f}"


def _fmt6(v: float | None) -> str:
    return "—" if v is None else f"{v:.6g}"


def render_daily_report(day: date, *, generated_at: datetime,
                        account=None, heartbeat: HeartbeatStatus | None = None,
                        magic_pnl: tuple[MagicPnl, ...] = (),
                        spread_rows: tuple[SpreadRow, ...] = (),
                        alert_counts: tuple[tuple[str, int], ...] = (),
                        ) -> str:
    """渲染日报 Markdown（关键行：标题/分策略盈亏表/点差比对表/信号一致性占位）。"""
    lines: list[str] = []
    lines.append(f"# 实盘日报 {day.isoformat()}")
    lines.append("")
    lines.append(f"- 生成时间：{iso_utc(generated_at)}（UTC）")
    if account is not None:
        lines.append(
            f"- 账户：{account.server} login {account.login}，"
            f"balance {account.balance:,.2f} {account.currency}，"
            f"equity {account.equity:,.2f} {account.currency}")
    if heartbeat is not None:
        if heartbeat.ok:
            lines.append(f"- 心跳：正常（{heartbeat.age_s:.0f} 秒前）")
        else:
            age = (f"{heartbeat.age_s:.0f} 秒" if heartbeat.age_s is not None
                   else "无记录")
            lines.append(f"- 心跳：**异常（{heartbeat.reason}，{age}）**")
    if alert_counts:
        lines.append("- 当日告警：" + "、".join(
            f"{kind} {n}" for kind, n in alert_counts))
    lines.append("")

    lines.append("## 分策略盈亏（按 magic 聚合当日 deals）")
    lines.append("")
    if magic_pnl:
        lines.append("| magic | 策略 | 成交笔数 | 成交量（手） | 净盈亏（USD） |")
        lines.append("| --- | --- | ---: | ---: | ---: |")
        for r in magic_pnl:
            name = r.strategy or "—"
            lines.append(f"| {r.magic} | {name} | {r.n_deals} "
                         f"| {r.volume:.2f} | {_fmt_pnl(r.net_pnl)} |")
        total_n = sum(r.n_deals for r in magic_pnl)
        total_pnl = sum(r.net_pnl for r in magic_pnl)
        lines.append("")
        lines.append(f"合计：{total_n} 笔，净盈亏 {_fmt_pnl(total_pnl)} USD")
    else:
        lines.append("当日无成交。")
    lines.append("")

    lines.append("## 实际点差 vs SpreadModel p90")
    lines.append("")
    if spread_rows:
        lines.append("| 品种 | 小时(UTC) | 样本 | 实测最大 | 模型 p90 "
                     "| 实测/p90 | 判定 |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
        for r in spread_rows:
            verdict = ("无模型" if r.model_p90 is None
                       else ("**超标**" if r.breach else "正常"))
            lines.append(
                f"| {r.symbol} | {r.hour_utc} | {r.n} "
                f"| {_fmt6(r.observed_max)} | {_fmt6(r.model_p90)} "
                f"| {_fmt6(r.ratio)} | {verdict} |")
    else:
        lines.append("当日无点差样本。")
    lines.append("")

    lines.append("## 信号一致性（占位）")
    lines.append("")
    lines.append("待接入（T14 后续）：每日收盘后用回测引擎重跑当日已收盘 bar，")
    lines.append("比对策略信号与实际成交的一致性。")
    lines.append("")
    return "\n".join(lines)


def write_daily_report(reports_dir: str, day: date, text: str) -> str:
    """日报落盘 {reports_dir}/live/daily-YYYY-MM-DD.md（原子写）；返回路径。"""
    d = os.path.join(reports_dir, DAILY_REPORT_SUBDIR)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"daily-{day.isoformat()}.md")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    fsync_file(tmp)
    atomic_replace(tmp, path)
    return path
