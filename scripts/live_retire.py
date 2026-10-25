"""策略退役（T32/D21）：确认该 magic 无持仓后，归档它的登记文件。

全平指令的归档仲裁按"登记 magic"（heartbeat-/expected- 文件名，
mt5live.riskproc.registered_magics）进行；某策略永久下线时，这些登记
文件会让它永远算作登记策略——风控进程会一直等它的 flatten ack，且它
心跳停更会触发全局停机。本脚本在退役前：

1. 连终端确认该 magic 名下**无持仓**（有持仓 → 拒绝，先走全平协议或
   人工平仓）；
2. 把 heartbeat-<magic>.json / expected-<magic>.json / state-<magic>.json
   移入 live/retired/（归档，不删除；同名旧归档加时间戳后缀）；
3. 写告警日志（RETIRED，含 --reason 说明）。

先停该策略的运行器（live_runner）再退役，否则运行器会把登记文件写回来。

用法：
    .venv/Scripts/python.exe scripts/live_retire.py --data-dir data \
        --magic 20261101 --reason "策略 H003 下线"
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway  # noqa: E402
from mt5gw.errors import DataUnavailableError  # noqa: E402
from mt5live.monitor import append_alert, iso_utc, naive_utc  # noqa: E402
from mt5live.reconcile import INTENT_FILE_FMT  # noqa: E402

ALERT_RETIRED = "RETIRED"
RETIRED_SUBDIR = "retired"


def _open_gateway(gw_config_path: str) -> MT5Gateway:
    """连接 MT5 终端（只读持仓）；配置缺失用默认网关配置。"""
    if os.path.exists(gw_config_path):
        gw = MT5Gateway.from_config_file(gw_config_path)
    else:
        gw = MT5Gateway(GatewayConfig())
    gw.connect()
    return gw


def _archive_name(retired_dir: str, name: str) -> str:
    """归档目标路径：同名旧归档存在 → 加时间戳后缀（不覆盖历史）。"""
    dst = os.path.join(retired_dir, name)
    if not os.path.exists(dst):
        return dst
    stamp = iso_utc(datetime.now(timezone.utc))
    stamp = stamp.replace(":", "").replace("+", "_")
    return f"{dst}.{stamp}"


def retire(live_dir: str, gw: MT5Gateway, magic: int, reason: str,
           *, now: datetime | None = None) -> int:
    """确认无持仓 → 归档 heartbeat/expected/state，使其不再算作登记策略。"""
    n = naive_utc(now if now is not None
                  else datetime.now(timezone.utc).replace(tzinfo=None))
    try:
        positions = gw.account.positions()
    except DataUnavailableError as e:
        print(f"× 持仓查询失败：{e}")
        return 2
    mine = [p for p in positions if p.magic == magic]
    if mine:
        detail = ", ".join(f"{p.symbol} {p.side} {p.volume}" for p in mine)
        print(f"× magic {magic} 仍有 {len(mine)} 笔持仓（{detail}）——"
              "先平仓（全平协议或人工）再退役")
        return 2

    retired_dir = os.path.join(live_dir, RETIRED_SUBDIR)
    os.makedirs(retired_dir, exist_ok=True)
    # expected 路径按 reconcile 的意图文件名拼（INTENT_FILE_FMT 同源）
    names = (
        os.path.basename(default_heartbeat_path_for(live_dir, magic)),
        os.path.basename(INTENT_FILE_FMT.format(magic=magic)),
        f"state-{magic}.json",
    )
    moved = []
    for name in names:
        src = os.path.join(live_dir, name)
        if not os.path.exists(src):
            continue
        os.replace(src, _archive_name(retired_dir, name))
        moved.append(name)
    append_alert(live_dir, ALERT_RETIRED,
                 f"magic {magic} 退役（{reason}）：归档 {moved or '无登记文件'}"
                 f" → {RETIRED_SUBDIR}/，不再计入全平仲裁与心跳检查", n)
    print(f"· magic {magic} 已退役：{moved or '无登记文件'} → "
          f"{RETIRED_SUBDIR}/（风控进程不再等它的 ack / 心跳）")
    return 0


def default_heartbeat_path_for(live_dir: str, magic: int) -> str:
    """heartbeat-<magic>.json 的绝对路径（与 runner 的命名同源）。"""
    return os.path.join(live_dir, f"heartbeat-{magic}.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="策略退役：确认无持仓后归档 heartbeat/expected/state")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--gw-config", default="config.json", help="网关配置 JSON")
    ap.add_argument("--magic", type=int, required=True, help="策略 magic")
    ap.add_argument("--reason", required=True, help="退役说明（写进告警日志）")
    args = ap.parse_args(argv)

    live_dir = os.path.join(args.data_dir, "live")
    try:
        gw = _open_gateway(args.gw_config)
    except Exception as e:
        print(f"× 网关连接失败：{e}")
        return 2
    try:
        return retire(live_dir, gw, args.magic, args.reason)
    finally:
        gw.shutdown()


if __name__ == "__main__":
    sys.exit(main())
