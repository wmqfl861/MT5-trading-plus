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
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone

from mt5data.store import atomic_replace, fsync_file
from mt5live.fileio import append_line, write_json_atomic
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
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path} 不是合法 JSON：{e}") from e
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
        if name.startswith("heartbeat-") and name.endswith(".json"))
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
    if not hb.runner_alive:
        return HeartbeatStatus(False, "runner_stopped", age)
    if age > timeout_s:
        return HeartbeatStatus(False, "timeout", age)
    return HeartbeatStatus(True, "ok", age)


def check_heartbeat(live_dir: str, now: datetime,
                    *, timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
                    ) -> HeartbeatStatus:
    return heartbeat_status(read_heartbeat(live_dir), now, timeout_s=timeout_s)


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
