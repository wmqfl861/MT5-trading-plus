"""scripts/demo_check.py（T45）：阶段 F 模拟盘演练验收检查（蓝图 §7 逐条转化）。

只读检查（不连 MT5、不下单、不改任何状态文件）。数据源：
- ``<data>/live/paper/state-<magic>.json``：影子券商账本（positions/deals，
  含 reason——SL=4/TP=5/SO=6/EXPERT=3，见 mt5gw.models）；
- ``<data>/live/expected-<magic>.json``（意图，T30/D19）、
  ``pause-<magic>.json``（对账按策略暂停，T30/D20）、
  ``state-<magic>.json``（执行器状态：pending_external_exits 未解释出场）、
  ``orders-<magic>.jsonl``（下单台账）、``alerts.log``、``heartbeat-<magic>.json``；
- 运行器/风控/对账进程日志：``<log-dir>/*.log``（demo_run.ps1 逐次启动写
  data/demo/）与 ``<data>/live/*.log``；
- 日报：``<reports>/live/daily-YYYY-MM-DD.md``（live_daily_report.py 产物）。

蓝图 §7 → 检查映射（一行一项）：
- 无法解释的持仓差异 = 0 ………… check_unexplained_diffs：最新日的意图 vs
  实际净持仓差分（mt5live.reconcile.diff_positions）+ 当日
  RECONCILE_MISMATCH / EXTERNAL_EXIT 告警是否至今未解除（pause 仍 paused、
  执行器 pending_external_exits 非空）；历史日只按当日告警追溯（现场状态
  无法复原，告警是当时落下的证据）。
- 每笔成交可在台账或日志中找到对应决策 ………… check_deal_attribution：
  开仓成交的 comment（= decision_id）须出现在 orders-*.jsonl 的 open 记录
  或运行器日志"决策 <id>："行；平仓成交按 reason∈{SL,TP,SO} 解释为保护性
  出场，comment="close" 的须有 orders 台账 close 记录支撑（数量口径）。
- 持仓期间零调仓（市价单数 = 入场数 + 出场数）………… check_zero_rebalance：
  当日无 UNEXPECTED_RESIZE 告警；成交 entry 只为 in/out；同一 position_id
  至多一笔开仓成交（无加仓腿）。
- 服务器端 SL/TP 出场后不触发暂停 ………… check_protective_exit：当日有
  保护性出场（reason∈{SL,TP,SO}）的 magic，现在 pause 不得仍 paused、
  执行器 pending_external_exits 必须已清空（自动解除 = 同策略可按重入规则
  继续）。
- 进程不间断（重启有日志说明）………… check_heartbeat_continuity：当日
  HEARTBEAT_TIMEOUT 告警之后须有运行器重启记录（新日志文件的启动行）；
  每次重启（同 magic 第二个日志文件 / 同文件第二个"mt5live 启动"行）须在
  上一份日志尾部留有说明（"收到中断，退出" / Traceback / 执行异常 /
  "手动停止"）或当日有 HEARTBEAT_TIMEOUT / RISK_HALT 告警佐证。
- 换日禁止时段（含冬令时 22:00）………… check_forbidden_session：当日
  开仓成交时刻须全部在 config/sessions.toml 禁入窗之外（SessionPolicy.
  entry_allowed，DST 感知——冬令时窗自动落在 22:00 GMT）；拒单记录数取
  alerts.log 中含 forbidden_session 的 RISK_REJECT 条目（§7 演练项证据，
  --summary 汇总要求两周内 ≥ 1 条）。
- 实际点差落在成本模型区间内 ………… check_daily_report：日报里
  compare_spreads 的"**超标**"行数 = 0（按小时比对由日报承担）。
- 每日日报自动生成 ………… check_daily_report：daily-<day>.md 存在且非空。

用法：
    .venv/Scripts/python.exe scripts/demo_check.py --day 2026-11-10
    .venv/Scripts/python.exe scripts/demo_check.py --summary
    # 可选：--data-dir data --log-dir data/demo --reports-dir data/reports
    #       --sessions-config config/sessions.toml

退出码：0 = 全部通过；1 = 存在不通过项；2 = 用法/数据错误。
--summary 额外汇总演练证据（RISK_HALT / RISK_FLATTEN 次数——§7 要求日亏
全平连续两次，即 RISK_FLATTEN ≥ 2；禁入拒单条数；覆盖天数）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5live.monitor import parse_iso, read_alerts  # noqa: E402
from mt5live.reconcile import (  # noqa: E402
    DEFAULT_VOLUME_TOLERANCE,
    ExpectedStateError,
    diff_positions,
    load_intents,
)

# mt5gw.models.DEAL_REASON_*（这里不 import mt5gw，保持脚本只读轻依赖）
REASON_SL, REASON_TP, REASON_SO = 4, 5, 6
PROTECTIVE_REASONS = frozenset({REASON_SL, REASON_TP, REASON_SO})
TRADE_ENTRIES = frozenset({"in", "out", "inout", "out_by"})

# 心跳连续性：进程日志里的标记行
RUNNER_START_MARK = "mt5live 启动"
EXIT_MARKS = ("收到中断，退出", "手动停止", "Traceback", "执行异常")
TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T[0-9:.]+)\]")


@dataclass
class Check:
    """单项验收结果：ok=None 表示当日无样本/无法判定（不算不通过）。"""

    name: str
    ok: bool | None
    detail: str


@dataclass
class DayReport:
    day: date
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]


# ---------------------------------------------------------------- 数据装载

def _read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_paper_states(live_dir: str) -> dict[int, dict]:
    """{magic: state 文档}（paper/state-<magic>.json；缺失文件跳过）。"""
    out: dict[int, dict] = {}
    for path in sorted(glob.glob(os.path.join(live_dir, "paper", "state-*.json"))):
        try:
            doc = _read_json(path)
        except (OSError, ValueError) as e:
            print(f"× 影子状态不可读（该策略按无数据处理，人工核对）：{path}：{e}",
                  file=sys.stderr)
            continue
        out[int(doc.get("magic", 0))] = doc
    return out


def all_deals(states: dict[int, dict]) -> list[dict]:
    """全部成交（含 time 解析后的 naive UTC）。"""
    out = []
    for magic, doc in states.items():
        for d in doc.get("deals", []):
            rec = dict(d)
            rec["magic"] = int(d.get("magic", magic))
            try:
                rec["_t"] = parse_iso(str(d["time"]))
            except (KeyError, ValueError, TypeError):
                continue
            out.append(rec)
    return out


def actual_positions(states: dict[int, dict]) -> list:
    """意图 vs 实际差分用的 actual 持仓（最小鸭子类型：symbol/magic/side/volume）。"""

    class _P:
        __slots__ = ("symbol", "magic", "side", "volume")

        def __init__(self, d):
            self.symbol = d["symbol"]
            self.magic = int(d.get("magic", 0))
            self.side = d["side"]
            self.volume = float(d["volume"])

    return [_P(p) for doc in states.values() for p in doc.get("positions", [])]


def load_pause_files(live_dir: str) -> dict[int, dict]:
    """{magic: pause 文档}（对账进程写的 pause-<magic>.json）。"""
    out: dict[int, dict] = {}
    for path in sorted(glob.glob(os.path.join(live_dir, "pause-*.json"))):
        try:
            doc = _read_json(path)
        except (OSError, ValueError):
            continue
        m = re.search(r"pause-(\d+)\.json$", os.path.basename(path))
        if m:
            out[int(m.group(1))] = doc
    return out


def load_executor_states(live_dir: str) -> dict[int, dict]:
    """{magic: 执行器 state 文档}（live/state-<magic>.json，pending_external_exits）。"""
    out: dict[int, dict] = {}
    for path in sorted(glob.glob(os.path.join(live_dir, "state-*.json"))):
        m = re.search(r"state-(\d+)\.json$", os.path.basename(path))
        if not m:
            continue
        try:
            out[int(m.group(1))] = _read_json(path)
        except (OSError, ValueError):
            continue
    return out


def load_order_log(live_dir: str) -> list[dict]:
    """orders-<magic>.jsonl 全部行（坏行跳过并计数由调用方汇总）。"""
    out = []
    for path in sorted(glob.glob(os.path.join(live_dir, "orders-*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                        rec["_t"] = parse_iso(str(rec["ts"]))
                    except (ValueError, KeyError, TypeError):
                        continue
                    out.append(rec)
        except OSError:
            continue
    return out


@dataclass
class LogFile:
    path: str
    lines: list[str]
    magic: int | None       # 运行器日志：启动行里的 magic；其余 None
    kind: str               # runner / risk / reconcile / other
    first_ts: datetime | None
    last_ts: datetime | None
    mtime: datetime


def scan_logs(log_dirs: list[str]) -> list[LogFile]:
    """读全部 *.log（demo_run.ps1 的 <kind>-<name>-<stamp>.log 与既有命名都认）。"""
    out: list[LogFile] = []
    seen: set[str] = set()
    for d in log_dirs:
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.log"))):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            if not lines:
                continue
            text = "\n".join(lines)
            magic = None
            m = re.search(r"mt5live 启动：.*?magic=(\d+)", text)
            if m:
                magic = int(m.group(1))
                kind = "runner"
            elif "心跳=" in text:
                kind = "risk"
            elif ("对账一致" in text) or ("持仓不一致" in text):
                kind = "reconcile"
            else:
                kind = "other"
            tss = [parse_iso(m) for m in TS_RE.findall(text)]
            # mtime 归一化为 naive UTC（与 parse_iso 同口径，可比较）
            mtime = datetime.fromtimestamp(
                os.path.getmtime(path), timezone.utc).replace(tzinfo=None)
            out.append(LogFile(
                path=path, lines=lines, magic=magic, kind=kind,
                first_ts=min(tss) if tss else None,
                last_ts=max(tss) if tss else None,
                mtime=mtime))
    return out


def log_start_events(logs: list[LogFile]) -> list[dict]:
    """运行器启动事件（每个"mt5live 启动"行一个；ts 取其后第一条带时间戳行，
    无则用文件 mtime——最近刚启动的进程日志可能还没打出时间戳行）。"""
    events = []
    for lf in logs:
        if lf.kind != "runner":
            continue
        for i, ln in enumerate(lf.lines):
            if RUNNER_START_MARK not in ln:
                continue
            ts = None
            for later in lf.lines[i + 1:]:
                m = TS_RE.search(later)
                if m:
                    ts = parse_iso(m.group(1))
                    break
            events.append({"magic": lf.magic, "ts": ts or lf.mtime,
                           "log": os.path.basename(lf.path)})
    return sorted(events, key=lambda e: e["ts"])


# ---------------------------------------------------------------- §7 检查项

def check_unexplained_diffs(ctx, day: date) -> Check:
    """§7-2：无法解释的持仓差异 = 0（意图 vs 实际 + 未解除告警）。"""
    name = "无法解释持仓差异=0（意图 vs 实际）"
    unresolved: list[str] = []
    if day == ctx["latest_day"]:
        # 最新日：现场差分（pending 宽限 60 秒与对账进程同口径）
        try:
            intents = load_intents(ctx["live_dir"])
            res = diff_positions(
                intents, actual_positions(ctx["states"]), now=ctx["now"],
                grace_s=60.0, volume_tolerance=DEFAULT_VOLUME_TOLERANCE)
            for d in res.diffs:
                unresolved.append(f"当前差分 {d.detail}")
        except ExpectedStateError as e:
            return Check(name, False, f"意图文件不可用：{e}")
    # 对账暂停写了却至今未解除（pause 仍 paused；ts ≤ 当日 → 与该日相关）
    for mg, doc in sorted(ctx["pauses"].items()):
        if not doc.get("paused") or doc.get("reason") != "reconcile_mismatch":
            continue
        try:
            ts = parse_iso(str(doc.get("ts", "")))
        except (ValueError, TypeError):
            ts = None
        if ts is None or ts.date() <= day:
            unresolved.append(f"magic {mg} 对账不一致暂停未解除")
    # 未解释外部出场至今未决（pending 条目 ts ≤ 当日；条目时间不可判按未决）
    for mg, st in sorted(ctx["executor_states"].items()):
        for ticket, item in (st.get("pending_external_exits") or {}).items():
            try:
                its = parse_iso(str(item.get("ts", "")))
            except (ValueError, TypeError):
                its = None
            if its is None or its.date() <= day:
                unresolved.append(
                    f"magic {mg} 未解释外部出场（ticket {ticket}）仍未决")
                break
    n_day = sum(1 for a in ctx["alerts"]
                if a.time.date() == day and a.kind in
                ("RECONCILE_MISMATCH", "EXTERNAL_EXIT"))
    if not unresolved:
        extra = f"（当日相关告警 {n_day} 条，均已解除）" if n_day else ""
        return Check(name, True, "意图与实际一致，无未解除的对账暂停/未决出场" + extra)
    return Check(name, False, "；".join(unresolved))


def check_deal_attribution(ctx, day: date) -> Check:
    """§7-2 后半：每笔成交可在台账或日志中找到对应决策。"""
    name = "每笔成交可归因（台账/日志有对应决策）"
    decisions = {r["decision_id"] for r in ctx["orders"]
                 if r.get("kind") == "open" and r.get("decision_id", "-") != "-"}
    for lf in ctx["logs"]:
        for ln in lf.lines:
            m = re.search(r"决策 (\S+?)：", ln)
            if m:
                decisions.add(m.group(1))
    closes_available: dict[int, int] = {}
    for r in ctx["orders"]:
        if r.get("kind") == "close":
            closes_available[r.get("magic", 0)] = (
                closes_available.get(r.get("magic", 0), 0) + 1)
    bad_in, closes_needed = [], {}
    for d in ctx["deals"]:
        if d["_t"].date() != day or d.get("entry") not in TRADE_ENTRIES:
            continue
        if d["entry"] == "in":
            if str(d.get("comment", "")) not in decisions:
                bad_in.append(f"#{d.get('ticket')} comment={d.get('comment')!r}")
        else:
            if d.get("reason") in PROTECTIVE_REASONS:
                continue                      # SL/TP/SO：服务器端出场，已解释
            if str(d.get("comment", "")) == "close":
                closes_needed[d["magic"]] = closes_needed.get(d["magic"], 0) + 1
            else:
                bad_in.append(f"#{d.get('ticket')} 出场无解释（comment="
                              f"{d.get('comment')!r} reason={d.get('reason')}）")
    short_close = {m: n for m, n in closes_needed.items()
                   if closes_available.get(m, 0) < n}
    problems = []
    if bad_in:
        problems.append(f"{len(bad_in)} 笔无对应决策：" + "；".join(bad_in[:5]))
    if short_close:
        problems.append(f"平仓成交多于台账 close 记录：{short_close}")
    if problems:
        return Check(name, False, "；".join(problems))
    n_day = sum(1 for d in ctx["deals"] if d["_t"].date() == day)
    return Check(name, True, f"当日 {n_day} 笔成交全部可归因")


def check_zero_rebalance(ctx, day: date) -> Check:
    """§7-3：持仓期间零调仓（市价单数 = 入场数 + 出场数）。"""
    name = "持仓期间零调仓（市价单数=入场+出场）"
    resize = [a for a in ctx["alerts"]
              if a.time.date() == day and a.kind == "UNEXPECTED_RESIZE"]
    bad_kind, double_in = [], []
    seen_in: set[tuple[int, int]] = set()
    for d in ctx["deals"]:
        if d["_t"].date() != day:
            continue
        if d.get("entry") not in ("in", "out"):
            bad_kind.append(f"#{d.get('ticket')} entry={d.get('entry')!r}")
            continue
        if d["entry"] == "in":
            key = (d["magic"], int(d.get("position_id", 0)))
            if key in seen_in:
                double_in.append(f"position {key[1]} 第二笔开仓（加仓腿）")
            seen_in.add(key)
    problems = []
    if resize:
        problems.append(f"UNEXPECTED_RESIZE 告警 {len(resize)} 条")
    if bad_kind:
        problems.append("非 in/out 成交：" + "；".join(bad_kind[:5]))
    if double_in:
        problems.append("；".join(sorted(set(double_in))[:5]))
    if problems:
        return Check(name, False, "；".join(problems))
    n_in = sum(1 for k in seen_in)
    n_out = sum(1 for d in ctx["deals"]
                if d["_t"].date() == day and d.get("entry") == "out")
    return Check(name, True, f"开仓 {n_in} + 出场 {n_out}，无加/减仓腿、无调仓告警")


def check_protective_exit(ctx, day: date) -> Check:
    """§7-4：服务器端 SL/TP 出场后不触发暂停（同策略按重入规则继续）。"""
    name = "SL/TP 出场后无未解除暂停"
    magics = sorted({d["magic"] for d in ctx["deals"]
                     if d["_t"].date() == day and d.get("entry") != "in"
                     and d.get("reason") in PROTECTIVE_REASONS})
    if not magics:
        return Check(name, True, "当日无保护性出场（无样本）")
    problems = []
    for mg in magics:
        pause = ctx["pauses"].get(mg)
        if pause is not None and pause.get("paused"):
            problems.append(f"magic {mg} 仍处对账暂停")
        pend = ctx["executor_states"].get(mg, {}).get("pending_external_exits")
        if pend:
            problems.append(f"magic {mg} 未决外部出场 {len(pend)} 项")
    if problems:
        return Check(name, False, "；".join(problems))
    return Check(name, True,
                 f"保护性出场 {len(magics)} 个 magic（{magics}），暂停均已自动解除")


def check_heartbeat_continuity(ctx, day: date) -> Check:
    """§7-1：进程不间断、重启有日志说明（心跳超时后有恢复记录）。"""
    name = "心跳连续性（中断有日志说明）"
    timeouts = [a for a in ctx["alerts"]
                if a.time.date() == day and a.kind == "HEARTBEAT_TIMEOUT"]
    starts = ctx["start_events"]
    problems = []
    # 1) 心跳超时之后须有运行器重启（新启动事件 ts > 告警 ts）
    for a in timeouts:
        if not any(s["ts"] > a.time for s in starts):
            problems.append(f"{a.time:%H:%M} 心跳超时后无重启恢复记录")
    # 2) 每次重启须在上一段日志留有说明（退出标记或当期停机类告警）；
    #    只判"重启发生在当日"的事件（历史日的重启归历史日）
    by_magic: dict[int | None, list[dict]] = {}
    for s in starts:
        by_magic.setdefault(s["magic"], []).append(s)
    for mg, evs in by_magic.items():
        for prev, cur in zip(evs, evs[1:]):
            if cur["ts"].date() != day:
                continue
            tail = ctx["log_tail_text"].get((mg, prev["log"]), "")
            explained = any(mark in tail for mark in EXIT_MARKS)
            if not explained:
                covered = [a for a in ctx["alerts"]
                           if a.kind in ("HEARTBEAT_TIMEOUT", "RISK_HALT")
                           and prev["ts"] <= a.time <= cur["ts"]]
                if not covered:
                    problems.append(
                        f"magic {mg} 重启（{prev['log']} → {cur['log']}）"
                        "无退出说明（taskkill 强杀不留日志——停止请用 killSwitch "
                        "或 Ctrl-C，并在日志留一行说明）")
    if problems:
        return Check(name, False, "；".join(problems))
    if timeouts:
        return Check(name, True,
                     f"心跳超时 {len(timeouts)} 次，均有重启恢复；重启均有说明")
    return Check(name, True, "无心跳超时告警；无未说明的重启")


def check_forbidden_session(ctx, day: date) -> Check:
    """§7-5 演练项：换日禁止时段（含冬令时 22:00）内不得开仓。"""
    name = "换日禁入时段（含冬令时 22:00）"
    if ctx["session_policy"] is None:
        return Check(name, None, "会话配置不可用，无法判定禁入窗")
    violations = []
    for d in ctx["deals"]:
        if d["_t"].date() != day or d.get("entry") != "in":
            continue
        if not ctx["session_policy"].entry_allowed(d.get("symbol", ""), d["_t"]):
            violations.append(f"#{d.get('ticket')} {d.get('symbol')} "
                              f"{d['_t']:%H:%M} 开仓落在禁入窗")
    rejects = [a for a in ctx["alerts"]
               if a.time.date() == day and a.kind == "RISK_REJECT"
               and "forbidden_session" in a.message]
    if violations:
        return Check(name, False, "禁入窗内开仓 " + "；".join(violations[:5]))
    note = f"禁入窗内零开仓；拒单记录 {len(rejects)} 条"
    if not rejects:
        note += "（当日无禁入窗开仓尝试——§7 演练项要求覆盖换日窗，见 --summary）"
    return Check(name, True, note)


def check_daily_report(ctx, day: date) -> Check:
    """§7-6/§7-7：每日日报存在；点差按小时比对无超标（由日报承担）。"""
    name = "日报存在且点差无超标"
    path = os.path.join(ctx["reports_dir"], "live", f"daily-{day.isoformat()}.md")
    if not os.path.exists(path):
        return Check(name, False, f"日报缺失：{path}")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return Check(name, False, f"日报不可读：{e}")
    if not text.strip():
        return Check(name, False, f"日报为空：{path}")
    n_breach = text.count("**超标**")
    if n_breach:
        return Check(name, False, f"日报含 {n_breach} 行点差超标（按小时比对）")
    if "当日无点差样本" in text:
        return Check(name, True, "日报存在；当日无点差样本（无法比对，如实标注）")
    return Check(name, True, "日报存在，点差比对无超标行")


CHECKS = (
    check_unexplained_diffs,
    check_deal_attribution,
    check_zero_rebalance,
    check_protective_exit,
    check_heartbeat_continuity,
    check_forbidden_session,
    check_daily_report,
)


# ---------------------------------------------------------------- 汇总渲染

def build_ctx(args) -> dict:
    live_dir = os.path.join(args.data_dir, "live")
    reports_dir = args.reports_dir
    log_dirs = [args.log_dir, os.path.join(args.data_dir, "live")]
    logs = scan_logs(log_dirs)
    states = load_paper_states(live_dir)
    deals = all_deals(states)
    try:
        alerts = list(read_alerts(live_dir))
    except (OSError, ValueError) as e:
        print(f"× alerts.log 不可读：{e}", file=sys.stderr)
        sys.exit(2)
    report_days = _report_days(reports_dir)
    all_days = ({a.time.date() for a in alerts}
                | {d["_t"].date() for d in deals}
                | report_days)
    session_policy = None
    try:
        from mt5data.sessions import load_session_policy
        session_policy = load_session_policy(args.sessions_config)
    except Exception as e:  # SessionConfigError 等：如实标注为无法判定
        print(f"× 会话配置加载失败：{e}", file=sys.stderr)
    start_events = log_start_events(logs)
    log_tail_text: dict[tuple[int | None, str], str] = {}
    for lf in logs:
        if lf.kind == "runner":
            log_tail_text[(lf.magic, os.path.basename(lf.path))] = \
                "\n".join(lf.lines[-80:])
    return {
        "live_dir": live_dir, "reports_dir": reports_dir,
        "states": states, "deals": deals, "alerts": alerts,
        "orders": load_order_log(live_dir),
        "pauses": load_pause_files(live_dir),
        "executor_states": load_executor_states(live_dir),
        "logs": logs, "start_events": start_events,
        "log_tail_text": log_tail_text,
        "session_policy": session_policy,
        "now": datetime.now(timezone.utc).replace(tzinfo=None),
        "all_days": all_days, "latest_day": max(all_days) if all_days else None,
    }


def _report_days(reports_dir: str) -> set[date]:
    out = set()
    for path in glob.glob(os.path.join(reports_dir, "live", "daily-*.md")):
        m = re.search(r"daily-(\d{4}-\d{2}-\d{2})\.md$", os.path.basename(path))
        if m:
            try:
                out.add(date.fromisoformat(m.group(1)))
            except ValueError:
                continue
    return out


def run_day(ctx, day: date) -> DayReport:
    rep = DayReport(day)
    for fn in CHECKS:
        rep.checks.append(fn(ctx, day))
    return rep


def print_day(rep: DayReport) -> None:
    print(f"== 模拟盘演练验收检查 {rep.day.isoformat()}（UTC 日） ==")
    for c in rep.checks:
        if c.ok is True:
            mark = "通过"
        elif c.ok is False:
            mark = "不通过"
        else:
            mark = "无样本"
        print(f"[{mark}] {c.name}：{c.detail}")
    print()


def print_summary(ctx, reps: list[DayReport]) -> None:
    print("== 模拟盘演练验收汇总（蓝图 §7） ==")
    for rep in reps:
        n_ok = sum(1 for c in rep.checks if c.ok is True)
        n_bad = len(rep.failed)
        n_na = sum(1 for c in rep.checks if c.ok is None)
        status = "全部通过" if (n_bad == 0 and n_na == 0) else (
            f"{n_bad} 项不通过" if n_bad else "无未决样本")
        failed_names = "、".join(c.name for c in rep.failed) or "—"
        print(f"{rep.day.isoformat()}  通过 {n_ok} / 不通过 {n_bad} / "
              f"无样本 {n_na}（{status}）不通过项：{failed_names}")
    n_fail = sum(len(r.failed) for r in reps)
    print()
    print("演练证据（§7 演练项，全期累计）：")
    kinds = {}
    for a in ctx["alerts"]:
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
    flatten = kinds.get("RISK_FLATTEN", 0)
    print(f"  一键停机/风控停机（RISK_HALT）：{kinds.get('RISK_HALT', 0)} 次")
    print(f"  日亏全平（RISK_FLATTEN）：{flatten} 次"
          + ("" if flatten >= 2 else "——§7 要求连续触发两次（≥2），未达标"))
    print(f"  心跳超时停机（HEARTBEAT_TIMEOUT）："
          f"{kinds.get('HEARTBEAT_TIMEOUT', 0)} 次（每次须有重启恢复记录）")
    rejects = sum(1 for a in ctx["alerts"]
                  if a.kind == "RISK_REJECT" and "forbidden_session" in a.message)
    print(f"  换日禁入拒单（forbidden_session）：{rejects} 条"
          + ("" if rejects >= 1 else "——§7 要求覆盖换日窗（含冬令时 22:00），未达标"))
    print(f"  覆盖天数：{len(reps)}（§7 要求连续 10 个交易日）")
    print()
    if n_fail:
        print(f"合计 {n_fail} 项不通过。")
    else:
        print("全部通过。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="阶段 F 模拟盘演练验收检查（蓝图 §7，只读不连 MT5）")
    ap.add_argument("--data-dir", default="data", help="数据根（live=data/live）")
    ap.add_argument("--log-dir", default="data/demo",
                    help="进程日志目录（另自动并入 <data-dir>/live/*.log）")
    ap.add_argument("--reports-dir", default="data/reports",
                    help="日报根（daily-*.md 在 <reports-dir>/live/）")
    ap.add_argument("--sessions-config", default="config/sessions.toml",
                    help="会话配置（换日禁入窗唯一定义源）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", metavar="YYYY-MM-DD", help="单日验收报告")
    g.add_argument("--summary", action="store_true", help="全期逐日汇总")
    args = ap.parse_args(argv)

    ctx = build_ctx(args)
    if not ctx["all_days"]:
        print("× 未发现任何演练数据（成交/告警/日报均无）——先启动 demo_run.ps1")
        return 2

    if args.day:
        try:
            day = date.fromisoformat(args.day)
        except ValueError:
            print("× --day 须为 YYYY-MM-DD")
            return 2
        rep = run_day(ctx, day)
        print_day(rep)
        return 1 if rep.failed else 0

    reps = [run_day(ctx, d) for d in sorted(ctx["all_days"])]
    for rep in reps:
        print_day(rep)
    print_summary(ctx, reps)
    return 1 if any(r.failed for r in reps) else 0


if __name__ == "__main__":
    sys.exit(main())
