"""风控主循环 CLI（T14/P7，独立于策略进程运行——蓝图 §4 第 10 条）。

每轮：
1. 读全部登记策略的心跳（data/live/heartbeat-<magic>.json，T24/M9；兼容
   旧版单文件 heartbeat.json）——任一超时/缺失 → 置该文件 runner_alive=false
   并写 data/live/halt.json（fail-closed：运行器未启动也拒绝交易。恢复 =
   人工删除 halt.json；建议启动顺序：先运行器、后风控进程）；
2. 检查 data/live/killSwitch——存在 → 写 halt.json（一键停机，不自动平仓）；
3. 轮询账户——当日（UTC 日）已实现+浮动 ≤ -daily_loss_cap_usd → 写
   data/live/commands/flatten.json（执行器全平后写 flatten.ack-<magic> 确认，
   T24）；所有登记 magic 都确认后把指令文件归档到 commands/archive/。

退出码：0 = 本轮完成（含"按规则写了 halt/flatten"——那是动作不是故障）；
1 = 账户轮询失败（--once 模式）；2 = 配置/连接等不可恢复错误。

用法：
    .venv/Scripts/python.exe scripts/risk_process.py --config config/risk.toml --once
    .venv/Scripts/python.exe scripts/risk_process.py --config config/risk.toml --loop --interval 10
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway  # noqa: E402
from mt5gw.errors import DataUnavailableError  # noqa: E402
from mt5live.monitor import ALERT_ACCOUNT_POLL_ERROR, append_alert  # noqa: E402
from mt5live.riskproc import (  # noqa: E402
    AccountSnapshot,
    CycleResult,
    RiskConfigError,
    RiskProcess,
    load_risk_config,
)


def _open_gateway(gw_config_path: str) -> MT5Gateway:
    """连接 MT5 终端（本进程只读账户，绝不下单）；配置缺失用默认网关配置。"""
    if os.path.exists(gw_config_path):
        gw = MT5Gateway.from_config_file(gw_config_path)
    else:
        gw = MT5Gateway(GatewayConfig())
    gw.connect()
    return gw


def build_snapshot(gw: MT5Gateway, now: datetime) -> AccountSnapshot:
    """轮询账户：状态 + 全部持仓 + 当日（UTC 00:00 起）全部成交流水。"""
    state = gw.account.state()
    if state.currency != "USD":
        raise ValueError(
            f"账户货币须为 USD（美元暴露/日亏口径），收到 {state.currency!r}")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    positions = gw.account.positions()
    deals = gw.account.deals(day_start, now)
    return AccountSnapshot(currency=state.currency, positions=tuple(positions),
                           deals=tuple(deals))


def run_one_cycle(risk: RiskProcess, gw: MT5Gateway) -> CycleResult:
    """单轮（账户轮询失败如实抛 DataUnavailableError，由调用方决定重试）。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    snap = build_snapshot(gw, now)
    res = risk.run_cycle(snap, now)
    print(f"· {now:%Y-%m-%d %H:%M:%S} UTC 心跳={res.heartbeat_reason} "
          f"halt={'是' if res.halt_active else '否'} "
          f"flatten={'写入' if res.flatten_written else '—'} "
          f"日亏触发={'是' if res.daily_loss_breached else '否'}")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="独立风控主循环（只读账户，不下单）")
    ap.add_argument("--config", default="config/risk.toml", help="风控配置 TOML")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--gw-config", default="config.json", help="网关配置 JSON")
    ap.add_argument("--once", action="store_true", help="单轮后退出（默认行为）")
    ap.add_argument("--loop", action="store_true", help="常驻轮询")
    ap.add_argument("--interval", type=float, default=10.0, help="轮询间隔（秒）")
    ap.add_argument("--heartbeat-timeout", type=float, default=120.0,
                    help="心跳超时判定（秒）")
    args = ap.parse_args(argv)

    try:
        config = load_risk_config(args.config)
    except RiskConfigError as e:
        print(f"× {e}")
        return 2
    risk = RiskProcess(
        config, live_dir=os.path.join(args.data_dir, "live"),
        heartbeat_timeout_s=args.heartbeat_timeout)

    try:
        gw = _open_gateway(args.gw_config)
    except Exception as e:
        print(f"× 网关连接失败：{e}")
        return 2

    def _poll_failed(e: DataUnavailableError) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        append_alert(risk.live_dir, ALERT_ACCOUNT_POLL_ERROR, str(e), now)
        print(f"× {now:%H:%M:%S} 账户轮询失败：{e}")

    try:
        if args.loop:
            while True:
                try:
                    run_one_cycle(risk, gw)
                except DataUnavailableError as e:
                    _poll_failed(e)  # 瞬时失败：告警后下一轮重试
                time.sleep(args.interval)
        try:
            run_one_cycle(risk, gw)
        except DataUnavailableError as e:
            _poll_failed(e)
            return 1
        return 0
    except KeyboardInterrupt:
        print("· 手动停止")
        return 0
    except ValueError as e:  # 账户货币等配置性错误：停止并如实报告
        print(f"× {e}")
        return 2
    finally:
        gw.shutdown()


if __name__ == "__main__":
    sys.exit(main())
