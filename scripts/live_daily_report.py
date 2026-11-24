"""实盘/演练日报 CLI（T24；G2/T46 演练运维完善）：调 mt5live.monitor.

render_daily_report 落盘。账户/持仓/成交流水两个来源：
- ``--demo --gw-config <path>``：Demo 网关（只读，trade_mode=0）；
- ``--paper-state <dir|file>``：影子券商状态文件（PaperStateGateway 聚合
  data/live/paper/state-<magic>.json，与 risk_process / live_reconcile 的
  同名参数同口径）——PaperSoak 只读文件，不连终端。
未显式选择模式时拒绝运行；不因影子目录缺失而回退到终端。

另从 data/live/ 读全部策略心跳与 alerts.log，渲染 Markdown 写
{reports_dir}/live/daily-YYYY-MM-DD.md。

点差比对（蓝图 §7"按小时比对，超出的小时写进报告"，demo_check 依据日报
验收）：``--spread-model`` 给定时，从运行器决策日志行
``[ts] 决策 <id>：... bid= ask=``（live_runner 每轮打印）采当日样本，经
monitor.compare_spreads 按 (品种, 小时) 比对 SpreadModel p90。小时 = 决策
时刻的**纽约本地小时**（SpreadModel 分桶口径 BUCKET_TZ；SpreadSample
字段名 hour_utc 沿用 monitor，实填模型口径）。模型文件兼容
build_cost_model.py 的 v1/v2 缓存包装与旧裸模型（与 live_runner 同口径）。
不给 --spread-model 或当日无样本 → 如实标注"当日无点差样本"。

用法（计划任务每日一次，见 scripts/setup_scheduled_tasks.ps1）：
    .venv/Scripts/python.exe scripts/live_daily_report.py --demo --gw-config gateway.json --date yesterday
    .venv/Scripts/python.exe scripts/live_daily_report.py --demo --gw-config gateway.json --date 2026-10-28
    # 演练（paper）：日报读影子状态 + 决策日志点差比对
    .venv/Scripts/python.exe scripts/live_daily_report.py --date yesterday \
        --paper-state data/live/paper \
        --spread-model data/costs/spread_model.json --log-dir data/demo

T213（第二十三轮 §2.4；评审修正 2026-11-23）：
- 每次运行重新生成 --date 当天的日报，并补生成它之前几天的日报（--backfill-days，
  默认含当天共 3 天）：已有的不覆盖，补生成的写实际的生成时刻。
- 连终端的模式（--demo）每一次尝试开始之前都判换日窗（--sessions-config，不给就用
  SessionPolicy 的缺省窗）：不在窗内就不开始，那一天的日报记为未生成（D82）。
  --paper-state 只读影子状态、不连终端，不受此限。
- 连接或取数失败时重试，至多 --attempts 次（默认 3），间隔 --retry-interval-s（默认
  300 秒）；重试开始的时刻落在窗外就不再试。
- 一次运行结束时有日报没生成：发一条 DAILY_REPORT_FAILED（记录类），退出码 3。
  配置或用法错误（退出码 2）、未预期异常和写盘失败（退出码 1，带回溯）、网关期限
  （退出码 75）同样各发一条，消息里写明是哪一类。

报告内容与 monitor.render_daily_report 一致：账户概览、心跳状态（全部
登记策略）、分策略盈亏（按 magic 聚合当日 deals）、点差比对、当日告警
计数。账号仅显示 login（README 约定脱敏：不写余额以外可识别个人的信息；
如需更强脱敏由报告落库流程处理）。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5live.monitor import (  # noqa: E402
    DAILY_REPORT_SUBDIR,
    HeartbeatStatus,
    SpreadSample,
    aggregate_by_magic,
    append_alert,
    compare_spreads,
    heartbeat_files,
    heartbeat_status,
    read_alerts,
    read_heartbeat_path,
    render_daily_report,
    write_daily_report,
)
from mt5gw.errors import DataUnavailableError, NotConnectedError  # noqa: E402
from mt5data.sessions import (  # noqa: E402
    SessionConfigError,
    SessionPolicy,
    load_session_policy,
)
from mt5quant.costs.spread import (  # noqa: E402
    BUCKET_TZ,
    SpreadModel,
    SpreadStat,
)

# SpreadModel 小时分桶时区（纽约本地小时；T20 换日 17:00 所在时区）
_NY_TZ = ZoneInfo(BUCKET_TZ)

# 运行器决策日志行（live_runner 主循环）：
# [decided_at.isoformat()] 决策 {symbol}|{TF}|{bar_close}：... bid= ask= ...
DECISION_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}T[0-9:.]+)\] 决策 (\S+?)："
    r".*?\bbid=([0-9.eE+-]+) ask=([0-9.eE+-]+)")

_SPREAD_REBUILD_HINT = "重跑 scripts/build_cost_model.py 重建点差模型缓存"


def parse_day(text: str) -> date:
    if text == "yesterday":
        return datetime.now(timezone.utc).date() - timedelta(days=1)
    if text == "today":
        return datetime.now(timezone.utc).date()
    try:
        return date.fromisoformat(text)
    except ValueError as e:
        raise SystemExit(f"× --date 须为 YYYY-MM-DD / yesterday / today：{e}")


def heartbeat_overview(live_dir: str, now: datetime,
                       timeout_s: float = 120.0) -> HeartbeatStatus | None:
    """全部登记策略（heartbeat-<magic>.json）的聚合心跳：任一异常即异常。"""
    statuses = []
    for path in heartbeat_files(live_dir):
        st = heartbeat_status(read_heartbeat_path(path), now,
                              timeout_s=timeout_s)
        statuses.append((os.path.basename(path), st))
    if not statuses:
        return None
    bad = next(((name, st) for name, st in statuses if not st.ok), None)
    if bad is None:
        age = min(st.age_s for _, st in statuses if st.age_s is not None)
        return HeartbeatStatus(True, "ok", age)
    name, st = bad
    return HeartbeatStatus(False, f"{st.reason}（{name}）", st.age_s)


def day_alert_counts(live_dir: str, day: date) -> tuple[tuple[str, int], ...]:
    """alerts.log 中落在该 UTC 日的告警按类别计数。"""
    try:
        alerts = read_alerts(live_dir)
    except (OSError, ValueError):
        return ()
    counts = Counter(a.kind for a in alerts if a.time.date() == day)
    return tuple(sorted(counts.items()))


# ---- 点差比对（§7：按小时比对，超出的小时写进报告） ----

def _canonical_json_sha256(payload: dict) -> str:
    """模型负载规范 sha256（与 run_backtest / live_runner 同一算法）。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def load_spread_model_any(path: str) -> SpreadModel:
    """加载点差模型：兼容 build_cost_model.py 缓存包装（v1/v2）与旧裸模型。

    与 live_runner.load_spread_model_for_live 同口径（校验 version、
    model_sha256、bucket_tz），但不限定单品种——日报按品种逐行比对。
    校验不过 → ValueError（绝不静默用坏模型出报告）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"点差文件 {path} 不可读（{e}）——{_SPREAD_REBUILD_HINT}") from e
    if not isinstance(doc, dict):
        raise ValueError(f"点差文件 {path} 顶层须为对象——{_SPREAD_REBUILD_HINT}")
    if "model" in doc:
        version = doc.get("version")
        if version not in (1, 2):
            raise ValueError(f"点差缓存 {path} version={version!r} 不受支持"
                             f"（当前 1/2）——{_SPREAD_REBUILD_HINT}")
        try:
            payload = doc["model"]
            sha = doc["model_sha256"]
        except KeyError as e:
            raise ValueError(f"点差缓存 {path} 缺键 {e}——{_SPREAD_REBUILD_HINT}") from e
        if _canonical_json_sha256(payload) != sha:
            raise ValueError(f"点差缓存 {path} 的 model_sha256 与模型内容不一致"
                             f"（文件被改动或写坏）——{_SPREAD_REBUILD_HINT}")
        if payload.get("bucket_tz") != BUCKET_TZ:
            raise ValueError(
                f"点差缓存 bucket_tz={payload.get('bucket_tz')!r}"
                f"（缺标注=UTC 分桶的旧文件），与纽约本地小时口径不兼容"
                f"——{_SPREAD_REBUILD_HINT}")
        try:
            per = {s: {int(h): SpreadStat(**st) for h, st in hours.items()}
                   for s, hours in payload["per_symbol"].items()}
            overall = {s: float(v)
                       for s, v in payload["overall_median"].items()}
            return SpreadModel(per, overall,
                               mode=payload.get("mode", "absolute"),
                               sampling=payload.get("sampling", "tick"))
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"点差缓存 {path} 模型负载非法（{e}）"
                             f"——{_SPREAD_REBUILD_HINT}") from e
    if "per_symbol" in doc:
        return SpreadModel.load(path)   # 旧裸模型文件（SpreadModel.save 直写）
    raise ValueError(f"点差文件 {path} 既不是缓存包装（缺 model 键）也不是"
                     f"裸模型（缺 per_symbol 键）——{_SPREAD_REBUILD_HINT}")


def spread_samples_from_logs(log_dirs: list[str],
                             day: date) -> tuple[SpreadSample, ...]:
    """从运行器决策日志采当日点差样本（[ts] 决策 行的 bid= ask=）。

    - 只取 decided_at 落在报告 UTC 日的行（与其余日报口径一致）；
    - bid/ask 缺失（"bid=None"）或非正跳过；symbol 取 decision_id 前缀
      （"{symbol}|{TF}|{bar_close}"，runner 契约）；
    - 小时 = 决策时刻的纽约本地小时（SpreadModel 分桶口径；字段名
      hour_utc 沿用 monitor，实填模型口径小时）；
    - spread_price = ask−bid，mid = (ask+bid)/2（relative 口径换算用）。
    """
    samples: list[SpreadSample] = []
    seen: set[str] = set()
    for d in log_dirs:
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.log"))):
            real = os.path.realpath(path)
            if real in seen:            # log-dir 与 live 目录重叠时不双计
                continue
            seen.add(real)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            for ln in lines:
                m = DECISION_LINE_RE.match(ln)
                if not m:
                    continue
                try:
                    ts = datetime.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if ts.date() != day:
                    continue
                bid, ask = float(m.group(3)), float(m.group(4))
                if not (ask > bid > 0):
                    continue            # 坏报价不入样本
                symbol = m.group(2).split("|", 1)[0]
                hour_ny = ts.replace(tzinfo=timezone.utc).astimezone(_NY_TZ).hour
                samples.append(SpreadSample(
                    symbol=symbol, hour_utc=hour_ny,
                    spread_price=ask - bid, mid=(ask + bid) / 2.0))
    return tuple(samples)


# ---- 取数失败的分类（T213）----
# 网关取不到数据、连接断开、调用超时：都是暂时性的，可以重试。build_gw 里 trade_mode
# 检查那次账户读取和后面的取数阶段用同一张表（评审 R2，2026-11-23）。
RETRYABLE_DATA_ERRORS = (NotConnectedError, DataUnavailableError, TimeoutError)


class RetryableFailure(Exception):
    """连接或取数失败：重试只在换日窗内进行。"""


def build_gw(paper_state: str | None, gw_config: str | None, live_dir: str,
             *, demo: bool = False, connect: bool = True):
    """显式选择影子状态或 Demo；缺模式/配置时不连接终端。

    用法和配置问题一律抛 DataUnavailableError：没选模式、两个模式同给、缺网关配置、
    配置装载不了、实盘解锁配置、账户不是 Demo。connect=False 只做这些检查，返回还
    没有连接的 Demo 网关——调用方在连接之前先判换日窗。连接之后的账户读取取不到
    数据不是配置问题，见 _connect_demo_gateway。
    """
    if paper_state:
        if demo:
            raise DataUnavailableError("--demo 与 --paper-state 互斥")
        from mt5live.paper import PaperStateGateway

        return PaperStateGateway(paper_state, alert_dir=live_dir), True
    if not demo:
        raise DataUnavailableError("日报须显式选择 --demo 或 --paper-state")
    from mt5gw import MT5Gateway, load

    if not gw_config or not os.path.isfile(gw_config):
        raise DataUnavailableError("Demo 日报须提供存在的 --gw-config")
    try:
        config = load(gw_config)
    except (OSError, ValueError) as exc:
        raise DataUnavailableError("Demo 网关配置无法装载") from exc
    if config.allow_live_trading:
        raise DataUnavailableError("Demo 日报拒绝 allow_live_trading=true")
    gw = MT5Gateway(config)
    if connect:
        _connect_demo_gateway(gw)
    return gw, True


def _connect_demo_gateway(gw) -> None:
    """连接终端并核对 trade_mode=0。任何一步失败，都是先关闭网关再抛出。"""
    try:
        gw.connect()
    except Exception:
        gw.shutdown()
        raise
    try:
        state = gw.account.state()
    except RETRYABLE_DATA_ERRORS as exc:
        # initialize 之后的第一笔取数取不到，和后面取数阶段的同类失败一样是取数失败
        gw.shutdown()
        raise RetryableFailure(f"取数失败：{type(exc).__name__}: {exc}") from exc
    if state.trade_mode != 0:
        gw.shutdown()
        raise DataUnavailableError("Demo 日报要求 trade_mode=0，停止读取")


# ---- T213：重试、补生成、失败告警 ----
ALERT_DAILY_REPORT_FAILED = "DAILY_REPORT_FAILED"
DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_INTERVAL_S = 300
DEFAULT_BACKFILL_DAYS = 3        # 含 --date 当天
EXIT_INCOMPLETE = 3              # 有日报没有生成


class FatalReportError(Exception):
    """用法或配置问题：不重试，退出码 2。"""


class OutsideRolloverWindow(Exception):
    """这次尝试开始的时刻不在换日窗内：没有连终端（D82），不计入尝试次数。"""


class ReportWriteError(Exception):
    """日报写盘失败：不重试，退出码 1。"""


class _Missing:
    """一次运行结束时还没有日报的一天，和它实际开始的尝试次数。
    （不用 dataclass：这个脚本常被 importlib 按路径直接加载，不登记进 sys.modules。）"""

    def __init__(self, day: date, attempts: int, note: str = ""):
        self.day, self.attempts, self.note = day, attempts, note


_NOT_IN_WINDOW = "换日窗外"                         # 这一天一次都没有开始
_RETRY_OUTSIDE_WINDOW = "重试落在换日窗外，没有再试"   # 开始过，下一次尝试已在窗外
_RUN_ABORTED = "运行中止"                           # 前面的日期出错，这次运行没有走到这里


def _one_line(text: str) -> str:
    """告警消息必须是一行：alerts.log 每行三段、制表符分隔（评审 R11）。"""
    return re.sub(r"[\t\r\n]+", " ", text).strip()


class _FailureAlert:
    """一次运行最多一条 DAILY_REPORT_FAILED；live 目录不存在就不写（不为写告警建目录）。"""

    def __init__(self, live_dir: str, tick):
        self._live_dir, self._tick, self._sent = live_dir, tick, False

    def send(self, missing, reason: str, exit_code: int) -> None:
        if self._sent or not os.path.isdir(self._live_dir):
            return
        self._sent = True
        parts = [f"{m.day} 已尝试 {m.attempts} 次" + (f"（{m.note}）" if m.note else "")
                 if m.attempts else f"{m.day} 未尝试（{m.note}）" for m in missing]
        message = f"日报未生成：{'；'.join(parts)}。{reason}（退出码 {exit_code}）"
        append_alert(self._live_dir, ALERT_DAILY_REPORT_FAILED, _one_line(message),
                     self._tick())


def report_path(reports_dir: str, day: date) -> str:
    return os.path.join(reports_dir, DAILY_REPORT_SUBDIR, f"daily-{day.isoformat()}.md")


def in_rollover_window(policy: SessionPolicy, t_utc: datetime) -> bool:
    """t_utc（naive UTC）是否落在换日窗内。日报只许在窗内连终端（D82）；
    窗的定义源与运行器相同（sessions.toml）。"""
    local = t_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(policy.rollover_tz))
    start, end = policy.rollover_window(local.date())
    return start <= t_utc < end


def generate_day(args, day: date, now: datetime, live_dir: str, gw=None,
                 policy: SessionPolicy | None = None) -> str:
    """生成一天的日报，返回路径。生成时刻如实取 now（补生成的日报也一样）。

    连终端的模式每一次尝试开始之前都判换日窗（now）：不在窗内抛 OutsideRolloverWindow，
    不连接（D82）。用法和配置检查在判窗之前，所以什么时候运行都照常报告配置问题。
    """
    spread_rows = ()
    if args.spread_model:
        try:
            model = load_spread_model_any(args.spread_model)
        except ValueError as e:
            raise FatalReportError(str(e)) from e
        samples = spread_samples_from_logs([args.log_dir, live_dir], day)
        if samples:
            spread_rows = compare_spreads(samples, model)

    owned = False
    if gw is None:                          # 测试注入的替身不归本函数关闭
        # build_gw 抛出的 DataUnavailableError 是用法或配置问题，原样上抛
        gw, owned = build_gw(args.paper_state, args.gw_config, live_dir,
                             demo=args.demo, connect=False)
    if args.paper_state is None:            # 连终端的模式：先判窗，再连接
        if not in_rollover_window(policy, now):
            raise OutsideRolloverWindow(f"{now:%Y-%m-%d %H:%M:%S} UTC 不在换日窗内")
        if owned:
            try:
                _connect_demo_gateway(gw)
            except NotConnectedError as e:
                raise RetryableFailure(f"终端未连接：{e}") from e
    try:
        account = gw.account.state()
        day_start = datetime(day.year, day.month, day.day)
        day_end = day_start + timedelta(days=1)
        deals = tuple(gw.account.deals(day_start, day_end))
        text = render_daily_report(
            day, generated_at=now, account=account,
            heartbeat=heartbeat_overview(live_dir, now),
            magic_pnl=aggregate_by_magic(deals),
            spread_rows=spread_rows,
            alert_counts=day_alert_counts(live_dir, day))
    except RETRYABLE_DATA_ERRORS as e:
        raise RetryableFailure(f"取数失败：{type(e).__name__}: {e}") from e
    finally:
        if owned:
            gw.shutdown()
    try:
        return write_daily_report(args.reports_dir, day, text)
    except OSError as e:
        raise ReportWriteError(f"日报 {day} 写不进 {args.reports_dir}：{e}") from e


def main(argv=None, *, gw=None, clock=None, sleep=None) -> int:
    ap = argparse.ArgumentParser(description="实盘日报（render_daily_report）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--reports-dir", default="data/reports")
    ap.add_argument("--gw-config", default=None)
    ap.add_argument("--demo", action="store_true",
                    help="仅允许 Demo 终端；须提供 --gw-config，拒绝实盘解锁配置")
    ap.add_argument("--date", default="yesterday",
                    help="YYYY-MM-DD / yesterday / today（默认 yesterday）")
    ap.add_argument("--live-subdir", default="live",
                    help="live 目录名（默认 data/live）")
    ap.add_argument("--paper-state", default=None, metavar="PATH",
                    help="影子状态文件/目录（演练 --paper 模式；给了就不连"
                         "真实终端，账户/成交读 paper/state-<magic>.json 聚合）")
    ap.add_argument("--spread-model", default=None, metavar="PATH",
                    help="点差模型（裸模型或 build_cost_model 缓存包装）；"
                         "给了就从决策日志采当日样本按小时比对 p90")
    ap.add_argument("--log-dir", default="data/demo",
                    help="运行器日志目录（点差样本来源；另自动并入 "
                         "<data-dir>/live/*.log）")
    ap.add_argument("--sessions-config", default=None, metavar="PATH",
                    help="sessions.toml（换日窗的定义源）；不给就用 SessionPolicy 的缺省窗。"
                         "连终端的每一次尝试开始之前都判窗，不在窗内就不连接")
    ap.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS,
                    help="连接或取数失败时最多的尝试次数（默认 3）")
    ap.add_argument("--retry-interval-s", type=int, default=DEFAULT_RETRY_INTERVAL_S,
                    help="重试间隔秒数（默认 300）")
    ap.add_argument("--backfill-days", type=int, default=DEFAULT_BACKFILL_DAYS,
                    help="顺带检查最近几天的日报、缺哪天补哪天（默认 3，含 --date 当天）")
    args = ap.parse_args(argv)
    if args.attempts < 1 or args.retry_interval_s < 0 or args.backfill_days < 1:
        ap.error("--attempts、--backfill-days 须不小于 1；--retry-interval-s 须不小于 0")

    day = parse_day(args.date)
    tick = clock or (lambda: datetime.now(timezone.utc))
    pause = sleep or time.sleep
    live_dir = os.path.join(args.data_dir, args.live_subdir)
    alert = _FailureAlert(live_dir, tick)
    # --date 当天总是重新生成；更早的几天只在日报缺失时补。
    earlier = (day - timedelta(days=back) for back in range(1, args.backfill_days))
    todo = [day] + [d for d in earlier
                    if not os.path.exists(report_path(args.reports_dir, d))]
    try:
        policy = (load_session_policy(args.sessions_config) if args.sessions_config
                  else SessionPolicy())
    except (SessionConfigError, OSError) as e:     # 读不了（比如路径是目录）也是配置错误
        print(f"× 会话配置错误：{e}")
        alert.send([_Missing(d, 0, "配置或用法错误") for d in todo],
                   f"配置或用法错误：会话配置错误：{e}", 2)
        return 2

    connects = args.paper_state is None     # --paper-state 不连终端，不受换日窗约束
    interval = timedelta(seconds=args.retry_interval_s)
    missing: list[_Missing] = []            # 还没有日报的那些天
    last_failure = None                     # 最后一次失败的（日期, 第几次, 原因）
    progress = {"day": day, "attempts": 0, "rest": ()}

    def not_reached():
        """正在处理的那一天，和这次运行还没有走到的日子。"""
        return ([_Missing(progress["day"], progress["attempts"])]
                + [_Missing(d, 0, _RUN_ABORTED) for d in progress["rest"]])

    def on_deadline(record):
        """网关期限：网关自己 os._exit(75)，没有异常回到这里，只能在它退出之前写告警。"""
        alert.send(missing + not_reached(),
                   f"网关期限：{record['call']} 超过 {record['timeout_s']} 秒", 75)

    from mt5gw.connection import timeout_evidence

    try:
        with timeout_evidence(on_deadline):
            for index, target in enumerate(todo):
                attempts = 0
                while True:
                    now = tick().replace(tzinfo=None)
                    attempts += 1
                    progress.update(day=target, attempts=attempts, rest=todo[index + 1:])
                    try:
                        path = generate_day(args, target, now, live_dir, gw, policy)
                    except OutsideRolloverWindow as e:
                        attempts -= 1                  # 没有开始，不算一次尝试
                        progress["attempts"] = attempts
                        print(f"× 日报 {target} 未生成：{e}，没有连终端")
                        missing.append(_Missing(
                            target, attempts,
                            _NOT_IN_WINDOW if not attempts else _RETRY_OUTSIDE_WINDOW))
                        break
                    except RetryableFailure as e:
                        note = ""
                        if attempts < args.attempts:
                            # 判的是重试真正开始的时刻（现在加间隔），不是上一次尝试开始的时刻
                            wake_at = tick().replace(tzinfo=None) + interval
                            if connects and not in_rollover_window(policy, wake_at):
                                note = _RETRY_OUTSIDE_WINDOW    # 窗外：不睡、不再试
                            else:
                                print(f"· {target} 第 {attempts} 次尝试失败，"
                                      f"{args.retry_interval_s} 秒后重试：{e}")
                                pause(args.retry_interval_s)
                                continue
                        print(f"× 日报 {target} 未生成（已尝试 {attempts} 次）：{e}")
                        missing.append(_Missing(target, attempts, note))
                        last_failure = (target, attempts, str(e))
                        break
                    except (FatalReportError, DataUnavailableError) as e:
                        print(f"× {e}")
                        alert.send(missing + [_Missing(target, attempts, "配置或用法错误")]
                                   + [_Missing(d, 0, _RUN_ABORTED) for d in todo[index + 1:]],
                                   f"配置或用法错误：{e}", 2)
                        return 2
                    print(f"· 日报已写入 {path}")
                    break
    except ReportWriteError as e:
        print(f"× {e}")
        alert.send(missing + not_reached(), f"写盘失败：{e}", 1)
        raise
    except Exception as e:
        print(f"× 未预期异常 {type(e).__name__}：{e}")
        alert.send(missing + not_reached(), f"未预期异常 {type(e).__name__}：{e}", 1)
        raise
    if missing:
        if last_failure is None:
            reason = "没有一次尝试开始：都不在换日窗内"
        else:
            failed_day, count, error = last_failure
            reason = f"最近一次失败（{failed_day} 第 {count} 次）：{error}"
        alert.send(missing, reason, EXIT_INCOMPLETE)
    return EXIT_INCOMPLETE if missing else 0


if __name__ == "__main__":
    sys.exit(main())
