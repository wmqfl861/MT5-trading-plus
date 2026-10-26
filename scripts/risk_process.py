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
    # 影子盘（T34）：账户快照读 paper 状态（目录=聚合全部 state-<magic>.json，
    # 每轮重读——多运行器并行、常驻风控看到最新成交与浮盈；R6/C3），
    # 不连真实终端；也可传单个状态文件（单策略兼容）
    .venv/Scripts/python.exe scripts/risk_process.py --config config/risk.toml --once \
        --paper-state data/live/paper
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
from mt5live.paper import PaperStateGateway  # noqa: E402（T34 --paper-state）
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


def build_snapshot(gw, now: datetime) -> AccountSnapshot:
    """轮询账户：状态 + 全部持仓 + 当日（UTC 00:00 起）全部成交流水。

    gw 是"带 account 的网关对象"：真终端 MT5Gateway，或 T34 影子盘的
    PaperStateGateway（--paper-state：paper 目录聚合全部 state-<magic>.json
    或单状态文件，每查询重读——R6/C3、不碰真实账户）。

    R7/N-C2：account 提供 snapshot()（PaperAccountView）→ **单次**调用取
    同一版本的持仓+当日成交+状态——两次独立查询之间落盘的平仓不会被同时
    计为浮亏与已实现；状态文件存在但读不出 → 经聚合视图抛
    DataUnavailableError（调用方按轮询失败告警/重试，绝不静默当日盈亏
    变 0）。真终端退回逐查询。
    """
    snap_fn = getattr(gw.account, "snapshot", None)
    snap = snap_fn() if callable(snap_fn) else None
    state = snap.state if snap is not None else gw.account.state()
    if state.currency != "USD":
        raise ValueError(
            f"账户货币须为 USD（美元暴露/日亏口径），收到 {state.currency!r}")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if snap is not None:
        positions: tuple = snap.positions
        deals = tuple(d for d in snap.deals if day_start <= d.time <= now)
    else:
        positions = tuple(gw.account.positions())
        deals = tuple(gw.account.deals(day_start, now))
    return AccountSnapshot(currency=state.currency, positions=positions,
                           deals=deals)


def run_one_cycle(risk: RiskProcess, gw) -> CycleResult:
    """单轮（账户轮询失败如实抛 DataUnavailableError，由调用方决定重试）。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    snap = build_snapshot(gw, now)
    res = risk.run_cycle(snap, now)
    mode = ("paper" if isinstance(gw, PaperStateGateway) else "live")
    print(f"· {now:%Y-%m-%d %H:%M:%S} UTC [{mode}] 心跳={res.heartbeat_reason} "
          f"halt={'是' if res.halt_active else '否'} "
          f"flatten={'写入' if res.flatten_written else '—'} "
          f"日亏触发={'是' if res.daily_loss_breached else '否'}")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="独立风控主循环（只读账户，不下单）")
    ap.add_argument("--config", default="config/risk.toml", help="风控配置 TOML")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--gw-config", default="config.json", help="网关配置 JSON")
    ap.add_argument("--paper-state", default=None,
                    help="影子券商状态（T34/R6）：paper 目录（每轮聚合读全部"
                         " state-<magic>.json——多运行器并行）或单个状态文件"
                         "（单策略兼容）；账户快照读它而非真实终端，"
                         "给了就不连网关")
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
        if args.paper_state:
            try:
                # alert_dir：影子状态读失败（fail-closed 数据不可用）时
                # 同步落 alerts.log
                gw = PaperStateGateway(args.paper_state,
                                       alert_dir=risk.live_dir)
            except DataUnavailableError as e:
                print(f"× 影子状态不可用：{e}")
                return 2
        else:
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
