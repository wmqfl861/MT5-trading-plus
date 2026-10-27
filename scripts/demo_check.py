"""scripts/demo_check.py（T45/T49）：阶段 F 演练检查（蓝图 §7 逐条转化）。

两档口径（R8-I5：日常"没有发现异常"≠阶段"取得验收证据"，绝不共用布尔）：

- 日常档 ``--day`` / ``--summary``（宽松显示）：逐日 7 项检查，返回 0
  只要无异常**证据**（ok=False 项）；"无样本"（ok=None）如实标注、不判
  不通过，但汇总不再宣称"全部通过"——只说"未发现异常证据（日常口径）"。
- 验收档 ``--accept``（阶段 F 最终验收，严格）：证据源钉死为 **Demo 运行
  契约**（R10/T53+T54，R9-I3/I4：C-1 run-manifest.json / C-2
  deals-<magic>.jsonl / C-3 drill-archive）——在逐日严格复核（任何
  ok=None 无样本/无法判定项 = 不通过）之上，另强制九条全期证据判据，
  任一"无样本/未覆盖"= 未验收，退出码非 0：
  0. C-1 manifest 证据源：``<data>/live/run-manifest.json`` 存在且
     mode=="Demo"、magics 与证据一致；运行窗=当前 mode 最早 started_utc
     ..now（重启不清窗）；manifest 缺失/mode 不符/schema_version 缺失或
     ≠1/存在 paper 状态文件/日志含 ``[paper`` 行 = "证据来源可疑"拒绝；
  1. 持仓状态可核（T53 持仓判据源的选择）：state-<magic>.json 的
     **known_positions**（executor 每轮 ``_finish`` 已持久化——不连 MT5、
     不读 paper）逐 magic 存在，且与 confirmed 意图净手数一致；
  2. 连续交易日覆盖 ≥10（工作日无缺日；假日历不可得，按周一至五近似，
     跨假日段须人工在验收报告注明例外）；
  3. 各进程存活证据（覆盖跨度内逐日：风控/对账进程有当日日志行；每个
     magic 的运行器有当日日志行，或当日风控日志有"心跳=ok"行佐证——
     风控每轮核对全部登记心跳，ok 即全部运行器心跳新鲜），且**风控心跳
     日志覆盖当日每小时**（T53 加强：一条日志不再能证明整日存活）；
  4. SL 与 TP 样本均存在（唯一采信源 = C-2 deals 的 external_exit
     reason∈{SL,TP}；paper 影子盘证据不被 --accept 采信——R9-I3）；
  5. §7 六项演练逐项判据（运行窗内）：killSwitch=kill_switch 停机告警+
     恢复对（其后有 halt=否 风控行，恢复由风控日志推导）、心跳超时=
     HEARTBEAT_TIMEOUT 告警、分策略暂停=RECONCILE 暂停告警+pause 解除、
     重连=C-4 ``gateway_reconnected`` 行、幂等=推导（同 bar 同 magic 无
     重复开仓请求）、换日禁入=forbidden_session 拒单记录；每项缺失=
     未验收；
  6. 双全平演练真实完成（T54/C-3）：两轮指令均在运行窗内；每轮须有
     **全部参与 magic**（manifest 集合）的关联 ack（command_id 匹配+
     magic+时间在指令后 1 小时内+closed>0 或附空仓证明——编号协议 ack
     本身即执行器重查持仓/挂单清零后写出的空仓证明，closed=0 的**关联**
     ack 计入并标注）；孤儿 ack 不计；一轮缺任一 magic 的 ack=该轮未完成；
     窗外旧指令（D′）不计；
  7. 实际点差样本（结构化解析）：覆盖工作日日报存在、含可解析的结构化
     点差行（品种|小时|样本，逐小时比对由日报承担）、无"当日无点差
     样本"、无"**超标**"行；
  8. 滑点样本：C-2 fill_open 成交价 vs 决策行参考价（buy=ask / sell=bid）
     的差值分布——有样本且 min/p50/p95/max 分位数入报告（PaperSoak 影子
     盘不产生经纪商滑点，无样本=未验收）。

只读检查（不连 MT5、不下单、不改任何状态文件）。数据源：
- ``<data>/live/run-manifest.json``（C-1，T53）：运行清单——mode/magics/
  追加式 runs（--accept 的证据源与运行窗定义）；
- ``<data>/live/deals-<magic>.jsonl``（C-2，T53）：执行器成交流水
  （fill_open/fill_close/external_exit；external_exit 的 decision_bar
  语义=检测当轮 bar 或 null，按此解析、不作归因键）；
- ``<data>/live/drill-archive/<command_id>/``（C-3，T54）：演练保全的全平
  指令与 ack 副本（--accept 双全平判据的主源；commands 目录兜底）；
- ``<data>/live/paper/state-<magic>.json``：影子券商账本（PaperSoak 日常
  诊断用；positions/deals，含 reason——SL=4/TP=5/SO=6，见 mt5gw.models。
  --accept 不采信 paper 证据——存在即"证据来源可疑"拒绝）；
- ``<data>/live/expected-<magic>.json``（意图，T30/D19）、
  ``pause-<magic>.json``（对账按策略暂停，T30/D20）、
  ``state-<magic>.json``（执行器状态：known_positions 持仓判据源 +
  pending_external_exits 未解释出场）、
  ``orders-<magic>.jsonl``（下单台账，幂等推导数据源）、``alerts.log``、
  ``heartbeat-<magic>.json``；
- 运行器/风控/对账进程日志：``<log-dir>/*.log``（demo_run.ps1 逐次启动写
  data/demo/）与 ``<data>/live/*.log``；
- 日报：``<reports>/live/daily-YYYY-MM-DD.md``（live_daily_report.py 产物）。

蓝图 §7 → 日常检查映射（一行一项）：
- 无法解释的持仓差异 = 0 ………… check_unexplained_diffs：最新日的意图 vs
  实际净持仓差分（mt5live.reconcile.diff_positions）+ 当日
  RECONCILE_MISMATCH / EXTERNAL_EXIT 告警是否至今未解除（pause 仍 paused、
  执行器 pending_external_exits 非空）；历史日只按当日告警追溯（现场状态
  无法复原，告警是当时落下的证据）。
- 每笔成交可在台账或日志中找到对应决策 ………… check_deal_attribution：
  开仓成交的 comment（= decision_id）须出现在 orders-*.jsonl 的 open 记录
  或运行器日志"决策 <id>："行；平仓成交按 reason∈{SL,TP,SO} 解释为保护性
  出场，comment="close" 的须有 orders 台账 close 记录支撑（数量口径）。
  成交记录两源并存：paper 影子账本（PaperSoak 日常诊断）与 C-2
  deals-*.jsonl（fill_open→in/decision_bar、fill_close→out/"close"、
  external_exit→out/reason 数值化——Demo 运行的成交证据源）。
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
  --summary 汇总展示；--accept 走上面的九条全期判据）。
- 实际点差落在成本模型区间内 ………… check_daily_report：日报里
  compare_spreads 的"**超标**"行数 = 0（按小时比对由日报承担）。
- 每日日报自动生成 ………… check_daily_report：daily-<day>.md 存在且非空。

用法：
    .venv/Scripts/python.exe scripts/demo_check.py --day 2026-11-10   # 日常
    .venv/Scripts/python.exe scripts/demo_check.py --summary          # 全周汇总
    .venv/Scripts/python.exe scripts/demo_check.py --accept           # 阶段 F 验收
    # 可选：--data-dir data --log-dir data/demo --reports-dir data/reports
    #       --sessions-config config/sessions.toml

退出码：0 = 通过（日常档=无异常证据；--accept=验收通过）；1 = 存在不通过
项（--accept 下即"未验收"）；2 = 用法/数据错误。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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

# ---- R10 契约（T53/T54）：Demo 验收的证据源 ----
RUN_MANIFEST_FILE = "run-manifest.json"        # C-1：运行清单（A 组写入）
DRILL_ARCHIVE_SUBDIR = "drill-archive"         # C-3：演练证据保全（E 组写入）
FLATTEN_COMMAND = "flatten_all"
# C-2 event → 统一成交形状（entry/comment 供 §7 日常检查复用）
DEAL_EVENT_ENTRY = {"fill_open": "in", "fill_close": "out",
                    "external_exit": "out"}
REASON_CODE_BY_NAME = {"SL": REASON_SL, "TP": REASON_TP, "SO": REASON_SO}
PAPER_LOG_MARK = "[paper"                      # 影子盘日志行标记（含 [paper offline]）
GATEWAY_RECONNECTED_MARK = "gateway_reconnected"   # C-4 重连标记（E 组写入）
KILL_SWITCH_MARK = "kill_switch"               # RISK_HALT 告警的 killSwitch 语义
ACK_LINK_WINDOW_S = 3600.0                     # T54：ack 与指令的时间关联窗（秒）

# 心跳连续性：进程日志里的标记行
RUNNER_START_MARK = "mt5live 启动"
EXIT_MARKS = ("收到中断，退出", "手动停止", "Traceback", "执行异常")
TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}T[0-9:.]+)\]")
# 风控/对账进程行格式："· 2026-10-26 11:00:40 UTC [live] 心跳=ok …"
DOT_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC")
HEARTBEAT_OK_MARK = "心跳=ok"          # 风控该轮全部登记心跳新鲜
# 运行器决策行的参考报价（滑点判据：决策价 vs C-2 成交价）
DECISION_QUOTE_RE = re.compile(
    r"决策 (\S+?)：.*?\bbid=([0-9.eE+-]+) ask=([0-9.eE+-]+)")
# 日报点差表的结构化行（monitor.render_daily_report 表格式）
SPREAD_ROW_RE = re.compile(
    r"^\|\s*([A-Za-z0-9_.-]+)\s*\|\s*(\d{1,2})\s*\|\s*(\d+)\s*\|", re.M)


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


def _parse_known_position(v) -> dict | None:
    """known_positions 单项（executor._parse_known 同款宽容解析）：
    {"dir": ±1, "volume": 手数}；纯方向整数的旧格式 volume=0。坏值 → None。"""
    try:
        if isinstance(v, dict):
            return {"dir": int(v.get("dir", 0)),
                    "volume": float(v.get("volume") or 0.0)}
        return {"dir": int(v), "volume": 0.0}
    except (TypeError, ValueError):
        return None


def actual_positions(states: dict[int, dict],
                     executor_states: dict[int, dict] | None = None,
                     intent_symbols: dict[int, str] | None = None) -> list:
    """意图 vs 实际差分用的 actual 持仓（最小鸭子类型：symbol/magic/side/volume）。

    两源合成（T53 持仓判据源的选择）：
    - paper 影子账本 positions（PaperSoak 日常诊断）；
    - **state-<magic>.json 的 known_positions**（executor 每轮 ``_finish``
      已持久化——Demo 运行唯一的现场持仓记录；不连 MT5）。仅补 paper 未
      覆盖的 magic（两源并存即配置错误，--accept 由证据源判据另行拒绝）。
    known_positions 无 symbol 字段——取同 magic 意图文件的 symbol。"""

    class _P:
        __slots__ = ("symbol", "magic", "side", "volume")

        def __init__(self, d):
            self.symbol = d["symbol"]
            self.magic = int(d.get("magic", 0))
            self.side = d["side"]
            self.volume = float(d["volume"])

    out = [_P(p) for doc in states.values() for p in doc.get("positions", [])]
    symbols = intent_symbols or {}
    for magic, st in (executor_states or {}).items():
        if magic in states:
            continue
        known = st.get("known_positions")
        if not isinstance(known, dict):
            continue
        for v in known.values():
            kv = _parse_known_position(v)
            if kv is None or kv["volume"] <= 0:
                continue
            out.append(_P({"symbol": symbols.get(magic, ""),
                           "magic": magic,
                           "side": "buy" if kv["dir"] > 0 else "sell",
                           "volume": kv["volume"]}))
    return out


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


# ------------------------------------------------ R10 契约装载（T53/T54）

def _parse_any_ts(value) -> datetime | None:
    """宽容解析 ISO 时间（接受尾缀 Z；坏值返回 None 而非抛错）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _bar_close_key(value) -> datetime | None:
    """滑点/归因关联键（T59/P2，C-2 契约）：decision_id（``SYMBOL|TF|bar``）
    的后缀或裸 bar ISO → 归一 naive UTC bar 收盘时刻；坏值 None。

    生产端两侧形态不同：runner 的 decision_id 后缀是 naive isoformat
    （无时区，runner.py:267-268）、executor 的 deals decision_bar 是
    iso_utc（带 +00:00，executor.py:471）——**字符串相等比较会以同样
    方式断链**，关联一律经 ``_parse_any_ts`` 归一为 naive UTC datetime
    再作 dict 键比较。"""
    if not isinstance(value, str) or not value:
        return None
    return _parse_any_ts(value.rsplit("|", 1)[-1])


def load_run_manifest(live_dir: str) -> tuple[dict | None, str | None]:
    """读 C-1 run-manifest.json。返回 (文档, 错误说明)——文件缺失 →
    (None, None)；存在但读不出/不是对象 → (None, 原因)。"""
    path = os.path.join(live_dir, RUN_MANIFEST_FILE)
    if not os.path.exists(path):
        return None, None
    try:
        doc = _read_json(path)
    except (OSError, ValueError) as e:
        return None, f"读不出（{e}）"
    if not isinstance(doc, dict):
        return None, f"顶层须为对象，收到 {type(doc).__name__}"
    return doc, None


def manifest_run_start(doc: dict | None) -> datetime | None:
    """运行窗起点 = runs 中**当前 mode** 的最早 started_utc（C-1：追加式
    runs、重启不清窗；run 条目自带 mode 字段才按其过滤，否则全体属于
    manifest 声明的当前 mode）。无 runs/无合法时间 → None。"""
    if not isinstance(doc, dict):
        return None
    mode = doc.get("mode")
    starts = []
    for r in doc.get("runs") or []:
        if not isinstance(r, dict):
            continue
        if r.get("mode", mode) != mode:   # 条目声明了别的 mode（跨模式清单）
            continue
        ts = _parse_any_ts(r.get("started_utc"))
        if ts is not None:
            starts.append(ts)
    return min(starts) if starts else None


def load_deals_log(live_dir: str) -> tuple[list[dict], int]:
    """读 C-2 deals-<magic>.jsonl 全部行 → （统一成交列表, 坏行数）。

    统一成交形状与 paper 影子账本兼容（§7 日常检查复用同一形状）：
    - ``event``/``decision_bar``/``price``/``_t`` 原样保留；
    - ``entry``：fill_open→"in"、fill_close/external_exit→"out"；
    - ``comment``：fill_open→decision_bar（开仓归因键=决策 bar 收盘时刻，
      消费侧经 ``_bar_close_key`` 归一 naive UTC 比较——T59/P2，两侧
      ISO 形态不同不得字符串相等）、
      fill_close→"close"（主动平仓，须 orders 台账 close 记录支撑）、
      external_exit→""（保护性出场已由 reason 解释）；
    - ``reason``：字符串名称按 mt5gw 语义数值化（SL→4/TP→5/SO→6，null→None）；
    - ``position_id``=ticket（C-2 不含持仓号，加仓腿检测以票号为键——
      重复开仓检测由幂等演练判据承担）。

    external_exit 的 ``decision_bar`` 语义=检测当轮 bar 或 null（按此解析、
    不作归因键——保护性出场与决策无关）。"""
    out: list[dict] = []
    bad = 0
    for path in sorted(glob.glob(os.path.join(live_dir, "deals-*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = json.loads(ln)
                        ts = _parse_any_ts(rec.get("ts"))
                        if ts is None:
                            raise ValueError("ts")
                        event = str(rec.get("event", ""))
                        if event not in DEAL_EVENT_ENTRY:
                            raise ValueError(f"event={event!r}")
                        reason = rec.get("reason")
                        doc = {
                            "ticket": rec.get("ticket"),
                            "position_id": rec.get("ticket"),
                            "magic": int(rec.get("magic", 0)),
                            "event": event,
                            "entry": DEAL_EVENT_ENTRY[event],
                            "comment": (str(rec["decision_bar"])
                                        if event == "fill_open"
                                        and rec.get("decision_bar")
                                        else "close" if event == "fill_close"
                                        else ""),
                            "reason": REASON_CODE_BY_NAME.get(reason)
                            if isinstance(reason, str) else None,
                            "price": rec.get("price"),
                            "volume": rec.get("volume"),
                            "side": rec.get("side"),
                            "decision_bar": rec.get("decision_bar"),
                            "_t": ts,
                        }
                        out.append(doc)
                    except (ValueError, KeyError, TypeError):
                        bad += 1
        except OSError:
            continue
    return out, bad


def load_drill_rounds(live_dir: str) -> dict[str, dict]:
    """读 C-3 drill-archive/<command_id>/（主源）+ commands 目录（兜底）。

    返回 {command_id: {"command": 指令文档|None, "command_ts": dt|None,
    "acks": [{"magic","id","closed","ts"}]}}。**文件名不作关联依据**（C-3
    只钉死目录名=编号）——命令/ack 按内容字段分类：
    - 命令：``command=="flatten_all"`` 且有 id（drill-archive 目录内任意
      *.json、commands/flatten.json 当前指令、commands/archive/flatten-*
      人工归档指令）；
    - ack：含 magic/id/closed/ts 四字段（drill-archive 目录内任意 *.json
      与 ``flatten.ack-*``——**T59/P1：生产端归档副本保留原名
      flatten.ack-<magic>，无 .json 扩展名（commands.py:48/191-194），
      两种模式都匹配**；commands/flatten.ack-<magic> 未删 ack）。
    三点关联（编号+magic+时间窗）由 accept_double_flatten 判定，这里只收集。
    """
    rounds: dict[str, dict] = {}

    def _round(cid: str) -> dict:
        return rounds.setdefault(
            cid, {"command": None, "command_ts": None, "acks": []})

    def _feed(doc) -> None:
        try:
            if doc.get("command") == FLATTEN_COMMAND and doc.get("id"):
                r = _round(str(doc["id"]))
                r["command"] = doc
                r["command_ts"] = (r["command_ts"]
                                   or _parse_any_ts(doc.get("ts")))
            elif all(k in doc for k in ("magic", "id", "closed")):
                ts = _parse_any_ts(doc.get("ts"))
                if ts is not None:
                    _round(str(doc["id"]))["acks"].append({
                        "magic": int(doc["magic"]), "id": str(doc["id"]),
                        "closed": int(doc["closed"]), "ts": ts})
        except (TypeError, ValueError):
            return

    root = os.path.join(live_dir, DRILL_ARCHIVE_SUBDIR)
    if os.path.isdir(root):
        for cid in sorted(os.listdir(root)):
            d = os.path.join(root, cid)
            if not os.path.isdir(d):
                continue
            # T59/P1：生产端（commands._copy_drill_evidence）把 ack 按原名
            # flatten.ack-<magic> 复制进来（无扩展名）——只 glob *.json 会
            # 漏掉全部 ack（消费端-生产端断链）
            for pattern in ("*.json", "flatten.ack-*"):
                for path in sorted(glob.glob(os.path.join(d, pattern))):
                    try:
                        _feed(_read_json(path))
                    except (OSError, ValueError):
                        continue
    cmds = os.path.join(live_dir, "commands")
    for path in [os.path.join(cmds, "flatten.json")]:
        if os.path.exists(path):
            try:
                _feed(_read_json(path))
            except (OSError, ValueError):
                pass
    for pattern in ("flatten.ack-*", os.path.join("archive", "flatten-*.json")):
        for path in sorted(glob.glob(os.path.join(cmds, pattern))):
            try:
                _feed(_read_json(path))
            except (OSError, ValueError):
                continue
    return rounds


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


def _dot_line_ts(ln: str) -> datetime | None:
    """风控/对账进程行（"· YYYY-MM-DD HH:MM:SS UTC …"）→ naive UTC；无时
    间戳行返回 None。"""
    m = DOT_TS_RE.search(ln)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}",
                                 "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def scan_log_days(logs: list[LogFile]) -> dict:
    """逐进程"哪些天有日志行"证据（--accept 进程存活判据用）。

    返回 {runner_days: {magic: set(date)}, risk_days, reconcile_days,
    heartbeat_ok_days, risk_hb_hours}——运行器行带 [ISO] 括号时间戳，风控/
    对账行是"· YYYY-MM-DD HH:MM:SS UTC"格式，两种都认；风控行含"心跳=ok"
    另计入 heartbeat_ok_days（该轮全部登记策略心跳新鲜的证据）；T53 加强：
    风控行含"心跳="的（日期, 小时）计入 risk_hb_hours——逐小时存活证据。"""
    runner_days: dict[int | None, set] = {}
    risk_days: set = set()
    reconcile_days: set = set()
    heartbeat_ok_days: set = set()
    risk_hb_hours: dict[date, set] = {}

    def _days_in_line(ln: str) -> set:
        out = set()
        for m in TS_RE.findall(ln):
            try:
                out.add(parse_iso(m).date())
            except ValueError:
                continue
        for m in DOT_TS_RE.findall(ln):
            try:
                out.add(date.fromisoformat(m[0]))
            except ValueError:
                continue
        return out

    for lf in logs:
        if lf.kind == "runner":
            for ln in lf.lines:
                runner_days.setdefault(lf.magic, set()).update(_days_in_line(ln))
        elif lf.kind == "risk":
            for ln in lf.lines:
                ds = _days_in_line(ln)
                risk_days.update(ds)
                if HEARTBEAT_OK_MARK in ln:
                    heartbeat_ok_days.update(ds)
                if "心跳=" in ln:
                    ts = _dot_line_ts(ln)
                    if ts is not None:
                        risk_hb_hours.setdefault(ts.date(), set()).add(ts.hour)
        elif lf.kind == "reconcile":
            for ln in lf.lines:
                reconcile_days.update(_days_in_line(ln))
    return {"runner_days": runner_days, "risk_days": risk_days,
            "reconcile_days": reconcile_days,
            "heartbeat_ok_days": heartbeat_ok_days,
            "risk_hb_hours": risk_hb_hours}


def _span_days(days: list[date]) -> list[date]:
    """覆盖跨度内的全部日历日（min→max，含周末——风控/对账 24/7 不间断）。"""
    if not days:
        return []
    out, cur = [], days[0]
    while cur <= days[-1]:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _missing_weekdays(days: list[date]) -> list[date]:
    """连续覆盖日之间缺失的工作日（周六日不算缺；假日历不可得，按周一至
    五近似——跨假日段须人工在验收报告注明例外）。"""
    missing: list[date] = []
    for a, b in zip(days, days[1:]):
        cur = a + timedelta(days=1)
        while cur < b:
            if cur.isoweekday() <= 5:
                missing.append(cur)
            cur += timedelta(days=1)
    return missing


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
                intents, actual_positions(ctx["states"], ctx["executor_states"],
                                          ctx["intent_symbols"]),
                now=ctx["now"],
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
    """§7-2 后半：每笔成交可在台账或日志中找到对应决策。

    开仓成交的归因键 = **解析后的 bar 收盘时刻**（T59/P2）：comment/
    decision_id（含 paper 旧夹具的全 id comment）取 ``|`` 后缀经
    ``_bar_close_key`` 归一为 naive UTC datetime 再比较——decision_id
    后缀（naive）与 C-2 decision_bar（iso_utc 带 +00:00）字符串形态
    不同，禁止字符串相等比较（会断链）。"""
    name = "每笔成交可归因（台账/日志有对应决策）"
    decision_ids = {r["decision_id"] for r in ctx["orders"]
                    if r.get("kind") == "open" and r.get("decision_id", "-") != "-"}
    for lf in ctx["logs"]:
        for ln in lf.lines:
            m = re.search(r"决策 (\S+?)：", ln)
            if m:
                decision_ids.add(m.group(1))
    decision_keys = {k for k in map(_bar_close_key, decision_ids)
                     if k is not None}
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
            key = _bar_close_key(d.get("comment", ""))
            if key is None or key not in decision_keys:
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


# ---------------------------------------------------------------- --accept 判据
# 阶段 F 最终验收（R8-I5 + R10/T53/T54）：任何"无样本/未覆盖"= 不通过
# （未验收）。证据源钉死 Demo 运行契约（C-1/C-2/C-3）。

def _in_window(ctx, ts: datetime) -> bool:
    """ts 是否落在 C-1 运行窗（当前 mode 最早 started_utc..now）内。"""
    start, end = ctx["run_window"]
    return start is not None and start <= ts <= end


def accept_evidence_source(ctx) -> Check:
    """判据 0（T53/C-1）：证据来源与运行窗——run-manifest.json 存在且
    mode=="Demo"、magics 与证据一致；存在 paper 状态文件/[paper] 日志行 =
    "证据来源可疑"拒绝（PaperSoak 证据不被 --accept 采信）。"""
    name = "证据来源与运行窗（C-1 run-manifest，mode=Demo）"
    doc, err = ctx["manifest"], ctx["manifest_error"]
    if doc is None:
        reason = f"：{err}" if err else "（文件不存在）"
        return Check(name, False,
                     "证据来源可疑——未读到可用的 run-manifest.json" + reason
                     + "；纯 paper/无运行清单证据不被 --accept 采信（R9-I3）")
    problems = []
    if str(doc.get("mode")) != "Demo":
        problems.append(f"manifest mode={doc.get('mode')!r} ≠ Demo")
    # T59/P3：schema_version 缺失同样拒绝（fail-closed，与生产端写入器对齐
    # ——demo_run.ps1 恒写该字段；缺字段=契约不符，不得静默按 1 处理）
    version = doc.get("schema_version")
    if version != 1:
        problems.append(f"schema_version={version!r} 不受支持（当前 1；"
                        "缺失同样拒绝——fail-closed）")
    magics = set(ctx["manifest_magics"])
    if not magics:
        problems.append("manifest magics 为空")
    else:
        evidence = ctx["evidence_magics"]
        if evidence != magics:
            extra = sorted(evidence - magics)
            lacking = sorted(magics - evidence)
            problems.append(
                f"magics 与证据不一致（manifest {sorted(magics)} vs 证据 "
                f"{sorted(evidence)}；多出 {extra}、缺证据 {lacking}）")
    if ctx["paper_state_files"]:
        problems.append(
            f"证据来源可疑：存在 paper 状态文件 {len(ctx['paper_state_files'])}"
            f" 个（{os.path.basename(ctx['paper_state_files'][0])} 等）")
    if ctx["paper_log_count"]:
        problems.append(
            f"证据来源可疑：日志含 [paper 影子盘行 {ctx['paper_log_count']} 行")
    if ctx["run_window"][0] is None:
        problems.append("manifest 无当前 mode 的合法 runs（运行窗不可定）")
    if problems:
        return Check(name, False, "；".join(problems))
    start = ctx["run_window"][0]
    return Check(name, True,
                 f"mode=Demo magics={sorted(magics)}，运行窗 "
                 f"{start:%Y-%m-%d %H:%M} UTC → now（runs "
                 f"{len(doc.get('runs') or [])} 条，重启不清窗）")


def accept_positions_state(ctx) -> Check:
    """判据 1（T53 持仓判据源的选择）：state-<magic>.json 的 known_positions
    （executor 每轮 ``_finish`` 已持久化）——不连 MT5、不读 paper。逐 magic
    要求：状态文件存在且含 known_positions 字段；其净手数（Σ dir×volume）
    与 confirmed 意图一致（pending 意图在途不判）。"""
    name = "持仓状态可核（state-<magic>.json known_positions）"
    magics = set(ctx["manifest_magics"]) or ctx["runner_magics"]
    problems = []
    for m in sorted(magics):
        st = ctx["executor_states"].get(m)
        if st is None:
            problems.append(f"magic {m} 无 state 文件（执行器未持久化"
                            " known_positions）")
        elif not isinstance(st.get("known_positions"), dict):
            problems.append(f"magic {m} state 无/坏 known_positions 字段")
    nets: dict[int, float] = {}
    for m, st in ctx["executor_states"].items():
        net = 0.0
        for v in (st.get("known_positions") or {}).values():
            kv = _parse_known_position(v)
            if kv is not None:
                net += kv["dir"] * kv["volume"]
        nets[m] = net
    try:
        intents = load_intents(ctx["live_dir"])
    except ExpectedStateError as e:
        problems.append(f"意图不可读（{e}）")
        intents = []
    for it in intents:
        if getattr(it, "status", None) != "confirmed":
            continue                    # pending 在途：对账进程另有宽限口径
        net = nets.get(it.magic)
        if net is None:
            problems.append(
                f"magic {it.magic} 有 confirmed 意图但无 known_positions 状态"
                "可核")
        elif abs(net - it.net_volume) > 1e-6:
            problems.append(
                f"magic {it.magic} known_positions 净 {net:+.2f} 手与 confirmed"
                f" 意图 {it.net_volume:+.2f} 手不一致")
    if problems:
        return Check(name, False, "；".join(problems[:6])
                     + ("…" if len(problems) > 6 else ""))
    known_n = sum(len(st.get("known_positions") or {})
                  for st in ctx["executor_states"].values())
    return Check(name, True,
                 f"magics {sorted(magics)} 状态齐全（known_positions 共 "
                 f"{known_n} 项），净额与 confirmed 意图一致")


def accept_day_coverage(ctx) -> Check:
    """判据 2：连续交易日覆盖 ≥10 个、全齐（工作日无缺日）。"""
    name = "连续交易日覆盖（≥10 个交易日全齐）"
    days = sorted(ctx["activity_days"])
    if not days:
        return Check(name, False,
                     "无活动日证据（成交/告警/运行器日志/日报任一日皆无）"
                     "——未覆盖")
    n_trading = sum(1 for d in days if d.isoweekday() <= 5)
    problems = []
    if n_trading < 10:
        problems.append(f"仅覆盖 {n_trading} 个交易日（<10）")
    missing = _missing_weekdays(days)
    if missing:
        shown = "、".join(d.isoformat() for d in missing[:5])
        problems.append("覆盖不连续，缺失交易日 " + shown
                        + (f" 等 {len(missing)} 天" if len(missing) > 5 else ""))
    if problems:
        return Check(name, False,
                     "；".join(problems) + f"（{days[0]} → {days[-1]}）")
    return Check(name, True,
                 f"{days[0]} → {days[-1]} 共 {n_trading} 个交易日全齐，"
                 "其间无缺失工作日（周六日不计；假日按工作日近似，跨假日段"
                 "须人工注明例外）")


def _required_hours(day: date, win_start: datetime, now: datetime) -> range:
    """逐小时存活要求的 hour 集合：窗首日从窗起点小时起；now 所在日到当前
    小时止（小时刚开头 10 分钟内还等不到本小时首条日志，宽限到上一小时）；
    其余日整日 0..23。"""
    lo, hi = 0, 23
    if day == win_start.date():
        lo = win_start.hour
    if day == now.date():
        hi = now.hour if now.minute >= 10 else max(now.hour - 1, 0)
        hi = max(hi, lo)
    return range(lo, hi + 1)


def accept_process_liveness(ctx) -> Check:
    """判据 3：各进程存活证据——覆盖跨度内逐日（含周末，进程 24/7 不间断）：
    风控/对账有当日日志行；每个 magic 的运行器有当日日志行，或当日风控
    日志有"心跳=ok"行佐证（风控每轮核对全部登记心跳）；且**风控心跳日志
    覆盖当日每小时**（T53 加强：一条日志不再能证明整日存活）。"""
    name = "各进程存活证据（逐日风控/对账/运行器+逐小时心跳）"
    days = sorted(ctx["activity_days"])
    magics = ctx["runner_magics"]
    if not days:
        return Check(name, False, "无活动日，无从判定进程存活")
    no_log = sorted(m for m in magics if not ctx["runner_magic_days"].get(m))
    if not magics or no_log:
        return Check(name, False,
                     f"magic {no_log or magics or '?'} 没有任何运行器日志"
                     "证据——无法证明运行器存活过")
    problems = []
    win_start = ctx["run_window"][0] or ctx["now"]
    for d in _span_days(days):
        if d not in ctx["risk_days"]:
            problems.append(f"{d.isoformat()} 无风控进程日志行")
        if d not in ctx["reconcile_days"]:
            problems.append(f"{d.isoformat()} 无对账进程日志行")
        missing_h = sorted(set(_required_hours(d, win_start, ctx["now"]))
                           - ctx["risk_hb_hours"].get(d, set()))
        if missing_h:
            problems.append(
                f"{d.isoformat()} 风控心跳日志缺 {len(missing_h)} 个小时"
                f"（如 {missing_h[:6]}）——逐小时存活证据不全")
        if d in ctx["heartbeat_ok_days"]:
            continue                     # 该轮全部登记心跳新鲜＝全员佐证
        for m in magics:
            if d not in ctx["runner_magic_days"].get(m, set()):
                problems.append(
                    f"{d.isoformat()} magic {m} 无运行器日志行"
                    "（且当日无风控 心跳=ok 佐证）")
    if problems:
        shown = "；".join(problems[:6])
        if len(problems) > 6:
            shown += f" 等 {len(problems)} 项"
        return Check(name, False, shown)
    span = _span_days(days)
    n_hours = sum(len(ctx["risk_hb_hours"].get(d, set())) for d in span)
    return Check(name, True,
                 f"{span[0]} → {span[-1]} 逐日（含周末 {len(span)} 天）风控/"
                 f"对账/运行器（{sorted(magics)}）均有存活证据，风控心跳逐小时"
                 f"覆盖 {n_hours} 小时样本")


def accept_sl_tp_samples(ctx) -> Check:
    """判据 4（T53/C-2）：SL 与 TP 样本均存在——唯一采信源=C-2
    deals-*.jsonl 的 external_exit（reason∈{SL,TP}）。paper 影子账本与
    "[paper] 保护性出场"日志不再作为验收证据（R9-I3：该日志仅 paper 分支
    产生，Demo 真实成交路径不落此格式）。"""
    name = "SL 与 TP 样本均存在（C-2 deals external_exit）"
    sl = [d for d in ctx["deals_log"]
          if d.get("event") == "external_exit" and d.get("reason") == REASON_SL]
    tp = [d for d in ctx["deals_log"]
          if d.get("event") == "external_exit" and d.get("reason") == REASON_TP]
    if not sl or not tp:
        return Check(name, False,
                     f"C-2 成交流水 SL 样本 {len(sl)} 笔、TP 样本 {len(tp)} 笔"
                     "——无样本=未验收（§7 要求 SL/TP 均有实测出场证据；"
                     "paper 保护性出场日志不构成 Demo 证据）")
    return Check(name, True,
                 f"SL {len(sl)} 笔、TP {len(tp)} 笔（C-2 deals external_exit）"
                 "均已有实测样本")


def accept_drills(ctx) -> Check:
    """判据 5（T53）：§7 六项演练逐项判据（运行窗内），每项缺失=未验收。

    - killSwitch：RISK_HALT 告警消息含 kill_switch（停机）+ 其后有 halt=否
      的风控行（恢复对——恢复不单设标记，由风控日志 halt=否 推导）；
    - 心跳超时：HEARTBEAT_TIMEOUT 告警（恢复记录由逐日复核
      check_heartbeat_continuity 把关）；
    - 分策略暂停：RECONCILE_MISMATCH 告警 + pause-<magic>.json 解除
      （paused=false 且 ts 晚于告警——对账进程一致后写回）；
    - 重连：运行器日志 C-4 ``gateway_reconnected`` 行（E 组网关失败后
      首次成功时写）；
    - 幂等（推导，不设标记）：orders-*.jsonl 运行窗内开仓请求按
      （magic, decision_id=bar）分组无重复，且至少有 1 笔样本；
    - 换日禁入：RISK_REJECT 告警含 forbidden_session（禁入窗拒单记录）。"""
    name = "§7 六项演练证据（运行窗内逐项）"
    if ctx["run_window"][0] is None:
        return Check(name, False, "运行窗不可定（C-1 manifest 缺失/无 runs）"
                     "——演练证据无从核")
    missing, notes = [], []
    # 1) killSwitch 停机 + 恢复对
    halts = [a for a in ctx["alerts"]
             if a.kind == "RISK_HALT" and KILL_SWITCH_MARK in a.message
             and _in_window(ctx, a.time)]
    recovered = any(
        lf.kind == "risk" and "halt=否" in ln
        and (ts := _dot_line_ts(ln)) is not None
        and any(ts > a.time for a in halts)
        for lf in ctx["logs"] for ln in lf.lines)
    if halts and recovered:
        notes.append(f"killSwitch×{len(halts)}（停机告警+恢复 halt=否）")
    else:
        why = "无 kill_switch 停机告警" if not halts else "停机后无 halt=否 恢复行"
        missing.append(f"killSwitch 停机+恢复对（{why}）")
    # 2) 心跳超时
    hb = [a for a in ctx["alerts"]
          if a.kind == "HEARTBEAT_TIMEOUT" and _in_window(ctx, a.time)]
    if hb:
        notes.append(f"心跳超时×{len(hb)}")
    else:
        missing.append("心跳超时（HEARTBEAT_TIMEOUT 告警）")
    # 3) 分策略暂停 + 解除
    mism = [a for a in ctx["alerts"]
            if a.kind == "RECONCILE_MISMATCH" and _in_window(ctx, a.time)]
    released = any(
        not doc.get("paused")
        and (ts := _parse_any_ts(doc.get("ts"))) is not None
        and any(ts > a.time for a in mism)
        for doc in ctx["pauses"].values())
    if mism and released:
        notes.append(f"分策略暂停×{len(mism)}+解除")
    else:
        why = "无 RECONCILE 暂停告警" if not mism else "暂停未解除（pause 仍 paused）"
        missing.append(f"分策略暂停+解除（{why}）")
    # 4) 重连（C-4）
    reconnected = any(
        lf.kind == "runner" and GATEWAY_RECONNECTED_MARK in ln
        and (m := TS_RE.search(ln)) is not None
        and _in_window(ctx, parse_iso(m.group(1)))
        for lf in ctx["logs"] for ln in lf.lines)
    if reconnected:
        notes.append("重连 gateway_reconnected")
    else:
        missing.append("重连（运行器日志 gateway_reconnected 行）")
    # 5) 幂等（推导）
    opens = [r for r in ctx["orders"]
             if r.get("kind") == "open" and r.get("decision_id", "-") != "-"
             and _in_window(ctx, r["_t"])]
    groups: dict[tuple[int, str], int] = {}
    for r in opens:
        key = (int(r.get("magic", 0)), str(r.get("decision_id")))
        groups[key] = groups.get(key, 0) + 1
    dup = {k: v for k, v in groups.items() if v > 1}
    if opens and not dup:
        notes.append(f"幂等（{len(opens)} 笔开仓请求同 bar 同 magic 无重复）")
    elif dup:
        shown = "、".join(f"magic {m} {did}×{n}"
                          for (m, did), n in sorted(dup.items())[:3])
        missing.append(f"幂等（同 bar 同 magic 重复开仓请求：{shown}）")
    else:
        missing.append("幂等（运行窗内无开仓请求样本可推导）")
    # 6) 换日禁入拒单
    rejects = [a for a in ctx["alerts"]
               if a.kind == "RISK_REJECT" and "forbidden_session" in a.message
               and _in_window(ctx, a.time)]
    if rejects:
        notes.append(f"换日禁入拒单×{len(rejects)}")
    else:
        missing.append("换日禁入（forbidden_session 拒单记录）")
    if missing:
        return Check(name, False, "演练证据缺失 " + "；".join(missing)
                     + "——每项缺失=未验收")
    return Check(name, True, "六项齐全：" + "，".join(notes))


def accept_double_flatten(ctx) -> Check:
    """判据 6（T54/C-3）：双全平演练真实完成——两点收口：

    - 证据源=C-3 drill-archive/<command_id>/（归档删除**前**复制的指令与
      全部 ack 副本）或 commands 目录兜底；孤儿 ack（无指令文档/无 magic/
      编号不匹配）不计；
    - 两轮指令（不同 uuid）均在**运行窗内**（窗外旧指令——D′ 10 月演练
      的指令——不计）；每轮须有全部参与 magic（manifest 集合）的**关联**
      ack：command_id 匹配 + magic + ack 时间在指令后 ACK_LINK_WINDOW_S 内
      + closed>0 或附空仓证明——编号协议的完成 ack 本身即空仓证明
      （executor 仅在重查确认本 magic 持仓与挂单均清零后才写 ack，
      mt5live.commands.write_ack 契约），closed=0 的关联 ack 计入并标注；
    - 一轮缺任一 magic 的 ack=该轮未完成；不足两轮完整=未验收。"""
    name = "双全平演练真实完成（两轮指令×全部 magic 关联 ack）"
    start, end = ctx["run_window"]
    if start is None:
        return Check(name, False, "运行窗不可定（C-1 manifest 缺失/无 runs）")
    magics = set(ctx["manifest_magics"]) or ctx["runner_magics"]
    rounds = load_drill_rounds(ctx["live_dir"])
    complete: list[tuple[str, datetime, list[str]]] = []
    incomplete: list[tuple[str, list[int]]] = []
    n_stale = 0
    for cid, r in sorted(rounds.items()):
        ts = r["command_ts"]
        if r["command"] is None or ts is None:
            continue                     # 无指令文档：孤儿 ack 不计
        if not (start <= ts <= end):
            n_stale += 1                 # 窗外旧指令（D′）不计
            continue
        missing: list[int] = []
        detail: list[str] = []
        for m in sorted(magics):
            acks = [a for a in r["acks"]
                    if a["id"] == cid and a["magic"] == m
                    and ts <= a["ts"]
                    <= ts + timedelta(seconds=ACK_LINK_WINDOW_S)]
            if not acks:
                missing.append(m)
                continue
            best = max(acks, key=lambda a: a["ts"])
            proof = (f"{m}:closed={best['closed']}" +
                     ("（空仓证明）" if best["closed"] <= 0 else ""))
            detail.append(proof)
        if missing:
            incomplete.append((cid, missing))
        else:
            complete.append((cid, ts, detail))
    if len(complete) < 2:
        parts = [f"完整轮 {len(complete)}（<2）"]
        if incomplete:
            parts.append("未完成轮：" + "；".join(
                f"{cid[:8]} 缺 magic {[m for m in mg]}" for cid, mg in incomplete))
        if n_stale:
            parts.append(f"窗外旧指令 {n_stale} 个（D′，不计）")
        orphans = sum(1 for r in rounds.values() if r["command"] is None)
        if orphans:
            parts.append(f"孤儿 ack（无指令文档）{orphans} 组不计")
        return Check(name, False,
                     "；".join(parts) + "——§7 要求连续两次、每轮全部参与 "
                     f"magic（{sorted(magics)}）均有三点关联的完成 ack"
                     "（RISK_FLATTEN 告警条数/孤立 ack 文件不能代替）")
    detail = "；".join(f"{cid[:8]}@{ts:%m-%d %H:%M}（{'，'.join(d)}）"
                       for cid, ts, d in complete[:2])
    return Check(name, True,
                 f"{len(complete)} 轮完整（两轮以上、不同 uuid、窗内、全员关联"
                 f" ack）：{detail}" + (f"；另有完整轮 {len(complete) - 2} 轮"
                                       if len(complete) > 2 else ""))


def accept_spread_samples(ctx) -> Check:
    """判据 7（T53 结构化解析）：实际点差样本存在且无超标小时——覆盖工作日
    逐日：日报存在、非"当日无点差样本"、无"**超标**"行，且含**可解析的结构
    化点差行**（品种|小时|样本，monitor.render_daily_report 表格式；只查
    非空文本/否定词会被标题日报蒙混——R9-I3 反例）。"""
    name = "实际点差样本存在且无超标小时（结构化行）"
    days = sorted(ctx["activity_days"])
    if not days:
        return Check(name, False, "无活动日——无从要求点差样本")
    problems = []
    weekdays = [d for d in _span_days(days) if d.isoweekday() <= 5]
    for d in weekdays:
        path = os.path.join(ctx["reports_dir"], "live",
                            f"daily-{d.isoformat()}.md")
        if not os.path.exists(path):
            problems.append(f"{d.isoformat()} 日报缺失")
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            problems.append(f"{d.isoformat()} 日报不可读：{e}")
            continue
        if not text.strip():
            problems.append(f"{d.isoformat()} 日报为空")
            continue
        if "当日无点差样本" in text:
            problems.append(f"{d.isoformat()} 当日无点差样本（无样本=未验收）")
            continue
        n_breach = text.count("**超标**")
        if n_breach:
            problems.append(f"{d.isoformat()} 点差超标 {n_breach} 行"
                            "（超限小时已由日报列出，超出模型区间）")
            continue
        rows = [(sym, int(h), int(n)) for sym, h, n in SPREAD_ROW_RE.findall(text)
                if 0 <= int(h) <= 23 and int(n) > 0]
        if not rows:
            problems.append(
                f"{d.isoformat()} 日报无结构化点差行（须含 品种|小时|样本 "
                "可解析行——标题/纯文本不算样本）")
    if problems:
        shown = "；".join(problems[:6])
        if len(problems) > 6:
            shown += f" 等 {len(problems)} 项"
        return Check(name, False, shown)
    return Check(name, True,
                 f"覆盖 {len(weekdays)} 个交易日日报均有结构化点差样本行且无"
                 "**超标** 小时（PaperSoak 不产生经纪商点差/滑点证据）")


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    """线性插值分位数（无 numpy 依赖；空表 None）。"""
    if not sorted_vals:
        return None
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def accept_slippage(ctx) -> Check:
    """判据 8（T53）：滑点样本——C-2 fill_open 成交价 vs 决策参考价
    （buy=ask / sell=bid，取自运行器决策行的 bid=/ask=）的差值分布：
    运行窗内有样本 + min/p50/p95/max 分位数入报告（无阈值判定，如实呈现
    分布；PaperSoak 影子盘按决策价成交、不产生经纪商滑点——无样本=未验收）。

    关联键 = **解析后的 bar 收盘时刻**（T59/P2）：决策行 decision_id 后缀
    （naive isoformat）与 C-2 decision_bar（iso_utc 带 +00:00）经
    ``_bar_close_key`` 归一为 naive UTC datetime——字符串相等比较在两种
    真实形态之间恒 miss（C-2 断链根因）。"""
    name = "滑点样本（deals 成交价 vs 决策参考价）"
    if ctx["run_window"][0] is None:
        return Check(name, False, "运行窗不可定（C-1 manifest 缺失/无 runs）")
    refs: dict[datetime, tuple[float, float]] = {}
    for lf in ctx["logs"]:
        if lf.kind != "runner":
            continue
        for ln in lf.lines:
            m = DECISION_QUOTE_RE.search(ln)
            if m:
                key = _bar_close_key(m.group(1))
                if key is None:
                    continue
                try:
                    refs[key] = (float(m.group(2)), float(m.group(3)))
                except ValueError:
                    continue
    samples: list[float] = []
    for d in ctx["deals_log"]:
        if d.get("event") != "fill_open" or not _in_window(ctx, d["_t"]):
            continue
        bar_key = _bar_close_key(d.get("decision_bar"))
        ref = refs.get(bar_key) if bar_key is not None else None
        if ref is None or not isinstance(d.get("price"), (int, float)):
            continue
        bid, ask = ref
        samples.append(float(d["price"]) - ask if d.get("side") == "buy"
                       else bid - float(d["price"]))
    if not samples:
        return Check(name, False,
                     "无滑点样本（C-2 fill_open 无运行窗内样本，或决策行缺 "
                     "bid/ask 参考价）——无样本=未验收（滑点证据须 Demo 真实"
                     "成交，PaperSoak 影子盘不产生经纪商滑点）")
    s = sorted(samples)

    def _f(v: float | None) -> str:
        return "—" if v is None else f"{v:+.2f}"

    return Check(name, True,
                 f"{len(s)} 样本：min {_f(s[0])} / p50 {_f(_quantile(s, .5))} / "
                 f"p95 {_f(_quantile(s, .95))} / max {_f(s[-1])}（价格单位，"
                 "正=劣于决策参考价）")


ACCEPT_CHECKS = (
    accept_evidence_source,
    accept_positions_state,
    accept_day_coverage,
    accept_process_liveness,
    accept_sl_tp_samples,
    accept_drills,
    accept_double_flatten,
    accept_spread_samples,
    accept_slippage,
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
    day_evidence = scan_log_days(logs)
    runner_magic_days = day_evidence["runner_days"]
    # 运行器 magic 全集：日志（含启动行 magic）∪ 影子状态 ∪ 意图文件——
    # 有状态/意图却无日志 = 无法证明运行器存活过（--accept 不通过）
    intent_symbols: dict[int, str] = {}
    expected_magics = set()
    for path in glob.glob(os.path.join(live_dir, "expected-*.json")):
        m = re.search(r"expected-(\d+)\.json$", os.path.basename(path))
        if not m:
            continue
        magic = int(m.group(1))
        expected_magics.add(magic)
        try:
            doc = _read_json(path)
            sym = doc.get("symbol")
            if isinstance(sym, str) and sym:
                intent_symbols[magic] = sym
        except (OSError, ValueError):
            pass
    runner_magics = ({m for m in runner_magic_days if m is not None}
                     | set(states.keys()) | expected_magics)
    activity_days = (all_days
                     | {d for ds in runner_magic_days.values() for d in ds})
    # ---- R10 契约证据（T53/T54）----
    executor_states = load_executor_states(live_dir)
    manifest, manifest_error = load_run_manifest(live_dir)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    win_start = manifest_run_start(manifest)
    # T59/P4：activity_days 的四个消费点（连续覆盖 accept_day_coverage、
    # 逐日存活 accept_process_liveness、点差样本覆盖 accept_spread_samples、
    # --accept 逐日严格复核 sweep）在此统一按 C-1 运行窗裁剪——窗外旧证据
    # （D′ 演练期遗留的告警/日志/日报）既不得撑长覆盖跨度，也不得反过来
    # 被要求补齐窗内才需要的证据。窗不可得（无 manifest/无合法 runs）保持
    # 原样；--day/--summary 宽松档消费 all_days，本就不受影响。
    if win_start is not None:
        lo_day, hi_day = win_start.date(), now.date()
        activity_days = {d for d in activity_days if lo_day <= d <= hi_day}
    deals_log, deals_log_bad = load_deals_log(live_dir)
    paper_state_files = sorted(glob.glob(
        os.path.join(live_dir, "paper", "state-*.json")))
    paper_log_count = sum(1 for lf in logs for ln in lf.lines
                          if PAPER_LOG_MARK in ln)
    manifest_magics: list[int] = []
    if isinstance(manifest, dict):
        try:
            manifest_magics = [int(m) for m in (manifest.get("magics") or [])]
        except (TypeError, ValueError):
            manifest_magics = []
    evidence_magics = ({d["magic"] for d in deals_log}
                       | {m for m in runner_magic_days if m is not None}
                       | expected_magics | set(executor_states))
    return {
        "live_dir": live_dir, "reports_dir": reports_dir,
        "states": states, "deals": deals + deals_log, "alerts": alerts,
        "orders": load_order_log(live_dir),
        "pauses": load_pause_files(live_dir),
        "executor_states": executor_states,
        "intent_symbols": intent_symbols,
        "logs": logs, "start_events": start_events,
        "log_tail_text": log_tail_text,
        "session_policy": session_policy,
        "now": now,
        "all_days": all_days, "latest_day": max(all_days) if all_days else None,
        "activity_days": activity_days,
        "runner_magic_days": runner_magic_days,
        "runner_magics": runner_magics,
        "risk_days": day_evidence["risk_days"],
        "reconcile_days": day_evidence["reconcile_days"],
        "heartbeat_ok_days": day_evidence["heartbeat_ok_days"],
        "risk_hb_hours": day_evidence["risk_hb_hours"],
        # T53/T54 契约证据
        "manifest": manifest, "manifest_error": manifest_error,
        "manifest_magics": manifest_magics,
        "run_window": (win_start, now),
        "deals_log": deals_log, "deals_log_bad": deals_log_bad,
        "paper_state_files": paper_state_files,
        "paper_log_count": paper_log_count,
        "evidence_magics": evidence_magics,
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
    print("== 模拟盘演练检查汇总（日常口径，蓝图 §7） ==")
    for rep in reps:
        n_ok = sum(1 for c in rep.checks if c.ok is True)
        n_bad = len(rep.failed)
        n_na = sum(1 for c in rep.checks if c.ok is None)
        status = "未见异常" if (n_bad == 0 and n_na == 0) else (
            f"{n_bad} 项不通过" if n_bad else "无未决样本")
        failed_names = "、".join(c.name for c in rep.failed) or "—"
        print(f"{rep.day.isoformat()}  通过 {n_ok} / 不通过 {n_bad} / "
              f"无样本 {n_na}（{status}）不通过项：{failed_names}")
    n_fail = sum(len(r.failed) for r in reps)
    print()
    print("演练证据（§7 演练项，全期累计展示——最终验收以 --accept 为准）：")
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
        print("未发现异常证据（日常口径）——阶段 F 最终验收须另跑 --accept："
              "Demo 运行清单（C-1）/持仓状态/连续覆盖/逐小时进程存活/SL 与 TP "
              "样本（C-2）/六项演练/双全平（两轮指令×全部 magic 关联 ack，"
              "C-3）/结构化点差/滑点样本任一无证据 = 未验收。")


def run_accept(ctx) -> tuple[list[Check], list[tuple[date, list[str], list[str]]]]:
    """--accept：九条全期判据 + 逐日严格复核（ok=None 无样本=不通过）。

    逐日复核只跑覆盖跨度内的工作日（周末无交易/无日报，进程存活由判据 2
    的逐日风控/对账/心跳证据覆盖）。"""
    checks = [fn(ctx) for fn in ACCEPT_CHECKS]
    problems: list[tuple[date, list[str], list[str]]] = []
    days = sorted(ctx["activity_days"])
    if days:
        for d in _span_days(days):
            if d.isoweekday() > 5:
                continue
            rep = run_day(ctx, d)
            failed = [c.name for c in rep.checks if c.ok is False]
            nas = [c.name for c in rep.checks if c.ok is None]
            if failed or nas:
                problems.append((d, failed, nas))
    return checks, problems


def print_accept(ctx, checks: list[Check],
                 problems: list[tuple[date, list[str], list[str]]]) -> None:
    print("== 阶段 F 最终验收（--accept 严格口径：无样本/未覆盖=未验收） ==")
    days = sorted(ctx["activity_days"])
    if days:
        print(f"数据范围：{days[0].isoformat()} → {days[-1].isoformat()}")
    for c in checks:
        mark = "通过" if c.ok is True else "不通过"
        print(f"[{mark}] {c.name}：{c.detail}")
    print()
    print("逐日严格复核（覆盖跨度内交易日；无样本/无法判定项按不通过）：")
    weekdays = [d for d in _span_days(days) if d.isoweekday() <= 5]
    if not weekdays:
        print("  （无覆盖日——跳过）")
    else:
        bad = {d: (f, n) for d, f, n in problems}
        for d in weekdays:
            if d not in bad:
                print(f"  {d.isoformat()}：通过")
            else:
                failed, nas = bad[d]
                parts = []
                if failed:
                    parts.append("不通过：" + "、".join(failed))
                if nas:
                    parts.append("无样本（未验收）：" + "、".join(nas))
                print(f"  {d.isoformat()}：" + "；".join(parts))
    n_bad = sum(1 for c in checks if c.ok is not True)
    print()
    if n_bad == 0 and not problems:
        print("结论：验收通过——阶段 F 证据齐全（蓝图 §7，严格口径）。")
    else:
        print(f"结论：未通过（未验收）——判据不满足 {n_bad} 项、逐日复核异常 "
              f"{len(problems)} 天；日常检查用 --day/--summary（宽松口径）。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="阶段 F 演练检查（蓝图 §7，只读不连 MT5；日常档宽松 / "
                    "--accept 验收档严格）")
    ap.add_argument("--data-dir", default="data", help="数据根（live=data/live）")
    ap.add_argument("--log-dir", default="data/demo",
                    help="进程日志目录（另自动并入 <data-dir>/live/*.log）")
    ap.add_argument("--reports-dir", default="data/reports",
                    help="日报根（daily-*.md 在 <reports-dir>/live/）")
    ap.add_argument("--sessions-config", default="config/sessions.toml",
                    help="会话配置（换日禁入窗唯一定义源）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", metavar="YYYY-MM-DD",
                   help="单日日常检查（宽松：无异常证据即 0，无样本如实标注）")
    g.add_argument("--summary", action="store_true",
                   help="全期逐日汇总（日常口径，同 --day 宽松）")
    g.add_argument("--accept", action="store_true",
                   help="阶段 F 最终验收（严格）：证据源钉死 Demo 运行契约——"
                        "C-1 run-manifest（mode=Demo）/持仓状态（"
                        "known_positions）/连续覆盖/逐小时进程存活/SL 与 TP "
                        "样本（C-2 deals）/§7 六项演练/双全平（C-3 两轮指令×"
                        "全部 magic 关联 ack）/结构化点差/滑点样本——任何"
                        "无样本/未覆盖/纯 paper 证据 = 未验收，退出码非 0")
    args = ap.parse_args(argv)

    ctx = build_ctx(args)
    if not ctx["all_days"]:
        print("× 未发现任何演练数据（成交/告警/日报均无）——先启动 demo_run.ps1")
        return 2

    if args.accept:
        checks, problems = run_accept(ctx)
        print_accept(ctx, checks, problems)
        return 0 if (all(c.ok is True for c in checks) and not problems) else 1

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
