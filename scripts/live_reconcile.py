"""对账 CLI（T14/P7）：data/live/expected.json vs 账户实际持仓（按 magic 过滤）。

- 启动即全量对账一次；--loop 时每 --interval 秒轮询；
- 不一致：逐条打印差分，写 data/live/pause.json（paused=true）+
  alerts.log 告警（loop 模式只在"一致 → 不一致"跳变时告警，不刷屏），
  exit 1；
- 一致：pause.json 写回 paused=false，exit 0；
- expected.json 缺失/不合法、持仓查询失败：exit 2（--loop 模式等待重试，
  运行器启动写出 expected.json 后自愈）。

用法：
    .venv/Scripts/python.exe scripts/live_reconcile.py --data-dir data --once
    .venv/Scripts/python.exe scripts/live_reconcile.py --data-dir data --loop --interval 30
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
from mt5live.monitor import ALERT_RECONCILE_MISMATCH, append_alert  # noqa: E402
from mt5live.reconcile import (  # noqa: E402
    EXPECTED_FILE,
    ExpectedStateError,
    ReconcileResult,
    diff_positions,
    load_expected,
    write_pause,
)


def _open_gateway(gw_config_path: str) -> MT5Gateway:
    """连接 MT5 终端（只读持仓）；配置缺失用默认网关配置。"""
    if os.path.exists(gw_config_path):
        gw = MT5Gateway.from_config_file(gw_config_path)
    else:
        gw = MT5Gateway(GatewayConfig())
    gw.connect()
    return gw


def run_once(live_dir: str, gw: MT5Gateway, *,
             volume_tolerance: float, alert: bool = True
             ) -> tuple[int, ReconcileResult | None]:
    """单轮对账；返回 (exit_code, 对账结果|None)。

    不一致即写暂停指令（pause.json paused=true）；一致写回 paused=false。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        exp = load_expected(os.path.join(live_dir, EXPECTED_FILE))
    except ExpectedStateError as e:
        print(f"× {e}")
        return 2, None
    try:
        actual = gw.account.positions()
    except DataUnavailableError as e:
        print(f"× 持仓查询失败：{e}")
        return 2, None

    res = diff_positions(exp.positions, actual,
                         volume_tolerance=volume_tolerance)
    if res.ok:
        write_pause(live_dir, paused=False, reason=None, now=now)
        print(f"· {now:%Y-%m-%d %H:%M:%S} UTC 对账一致（{len(actual)} 个持仓）")
        return 0, res
    for d in res.diffs:
        print(f"× {d.detail}")
    write_pause(live_dir, paused=True, reason="reconcile_mismatch",
                diffs=res.diffs, now=now)
    if alert:
        append_alert(live_dir, ALERT_RECONCILE_MISMATCH,
                     f"{len(res.diffs)} 处持仓不一致，策略已暂停（详见 pause.json）",
                     now)
    return 1, res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="持仓对账（expected vs actual）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--gw-config", default="config.json", help="网关配置 JSON")
    ap.add_argument("--once", action="store_true", help="单轮后退出（默认行为）")
    ap.add_argument("--loop", action="store_true", help="常驻轮询")
    ap.add_argument("--interval", type=float, default=30.0, help="轮询间隔（秒）")
    ap.add_argument("--volume-tolerance", type=float, default=1e-6,
                    help="净手数比较容差（手）")
    args = ap.parse_args(argv)

    live_dir = os.path.join(args.data_dir, "live")
    try:
        gw = _open_gateway(args.gw_config)
    except Exception as e:
        print(f"× 网关连接失败：{e}")
        return 2

    try:
        if args.loop:
            was_ok: bool | None = None  # 首轮与跳变时告警；持续不一致不重复告警
            while True:
                _, res = run_once(live_dir, gw,
                                  volume_tolerance=args.volume_tolerance,
                                  alert=was_ok is not False)
                if res is not None:
                    was_ok = res.ok
                time.sleep(args.interval)
        rc, _ = run_once(live_dir, gw, volume_tolerance=args.volume_tolerance)
        return rc
    except KeyboardInterrupt:
        print("· 手动停止")
        return 0
    finally:
        gw.shutdown()


if __name__ == "__main__":
    sys.exit(main())
