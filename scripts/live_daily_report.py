"""实盘日报 CLI（T24）：调 mt5live.monitor.render_daily_report 落盘。

从 MT5 网关（只读）拉当日账户/持仓/成交流水，从 data/live/ 读全部策略
心跳与 alerts.log，渲染 Markdown 写 {reports_dir}/live/daily-YYYY-MM-DD.md。

用法（计划任务每日一次，见 scripts/setup_scheduled_tasks.ps1）：
    .venv/Scripts/python.exe scripts/live_daily_report.py --date yesterday
    .venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-10-28

报告内容与 monitor.render_daily_report 一致：账户概览、心跳状态（全部
登记策略）、分策略盈亏（按 magic 聚合当日 deals）、点差比对（本脚本不采
tick 样本，如实标注"当日无点差样本"）、当日告警计数。账号仅显示 login
（README 约定脱敏：不写余额以外可识别个人的信息；如需更强脱敏由报告
落库流程处理）。
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5live.monitor import (  # noqa: E402
    HeartbeatStatus,
    aggregate_by_magic,
    heartbeat_files,
    heartbeat_status,
    read_alerts,
    read_heartbeat_path,
    render_daily_report,
    write_daily_report,
)


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


def main(argv=None, *, gw=None) -> int:
    ap = argparse.ArgumentParser(description="实盘日报（render_daily_report）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--reports-dir", default="data/reports")
    ap.add_argument("--gw-config", default="config.json")
    ap.add_argument("--date", default="yesterday",
                    help="YYYY-MM-DD / yesterday / today（默认 yesterday）")
    ap.add_argument("--live-subdir", default="live",
                    help="live 目录名（默认 data/live）")
    args = ap.parse_args(argv)

    day = parse_day(args.date)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    live_dir = os.path.join(args.data_dir, args.live_subdir)

    if gw is None:
        from mt5gw import GatewayConfig, MT5Gateway

        cfg_path = args.gw_config if os.path.exists(args.gw_config) else None
        gw = (MT5Gateway.from_config_file(cfg_path) if cfg_path
              else MT5Gateway(GatewayConfig()))
        gw.connect()
        owned = True
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
            spread_rows=(),                  # 本脚本不采 tick 样本（如实标注）
            alert_counts=day_alert_counts(live_dir, day))
    finally:
        if owned:
            gw.shutdown()
    path = write_daily_report(args.reports_dir, day, text)
    print(f"· 日报已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
