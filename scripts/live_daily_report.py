"""实盘/演练日报 CLI（T24；G2/T46 演练运维完善）：调 mt5live.monitor.

render_daily_report 落盘。账户/持仓/成交流水两个来源：
- 缺省：MT5 网关（只读）；
- ``--paper-state <dir|file>``：影子券商状态文件（PaperStateGateway 聚合
  data/live/paper/state-<magic>.json，与 risk_process / live_reconcile 的
  同名参数同口径）——阶段 F 模拟盘演练用它，绝不连真实终端。

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
    .venv/Scripts/python.exe scripts/live_daily_report.py --date yesterday
    .venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-10-28
    # 演练（paper）：日报读影子状态 + 决策日志点差比对
    .venv/Scripts/python.exe scripts/live_daily_report.py --date yesterday \
        --paper-state data/live/paper \
        --spread-model data/costs/spread_model.json --log-dir data/demo

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
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5live.monitor import (  # noqa: E402
    HeartbeatStatus,
    SpreadSample,
    aggregate_by_magic,
    compare_spreads,
    heartbeat_files,
    heartbeat_status,
    read_alerts,
    read_heartbeat_path,
    render_daily_report,
    write_daily_report,
)
from mt5gw.errors import DataUnavailableError  # noqa: E402
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
    ok = all(st.ok for _, st in statuses)
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


def build_gw(paper_state: str | None, gw_config: str | None, live_dir: str):
    """装配账户网关：--paper-state → PaperStateGateway（只读影子状态，
    不连终端）；缺省 → MT5Gateway。返回 (gw, owned)。
    """
    if paper_state:
        from mt5live.paper import PaperStateGateway

        return PaperStateGateway(paper_state, alert_dir=live_dir), True
    from mt5gw import GatewayConfig, MT5Gateway

    cfg_path = gw_config if os.path.exists(gw_config or "") else None
    gw = (MT5Gateway.from_config_file(cfg_path) if cfg_path
          else MT5Gateway(GatewayConfig()))
    gw.connect()
    return gw, True


def main(argv=None, *, gw=None) -> int:
    ap = argparse.ArgumentParser(description="实盘日报（render_daily_report）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--reports-dir", default="data/reports")
    ap.add_argument("--gw-config", default="config.json")
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
    args = ap.parse_args(argv)

    day = parse_day(args.date)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    live_dir = os.path.join(args.data_dir, args.live_subdir)

    spread_rows = ()
    if args.spread_model:
        try:
            model = load_spread_model_any(args.spread_model)
        except ValueError as e:
            print(f"× {e}")
            return 2
        samples = spread_samples_from_logs(
            [args.log_dir, live_dir], day)
        if samples:
            spread_rows = compare_spreads(samples, model)

    if gw is None:
        try:
            gw, owned = build_gw(args.paper_state, args.gw_config, live_dir)
        except DataUnavailableError as e:   # 影子状态不可用：fail-closed
            print(f"× {e}")
            return 2
    else:
        owned = False                      # 测试注入的替身，不归本函数关闭

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
    finally:
        if owned:
            gw.shutdown()
    path = write_daily_report(args.reports_dir, day, text)
    print(f"· 日报已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
