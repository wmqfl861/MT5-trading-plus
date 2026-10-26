"""人工确认外部出场（T30/D19 语义 6）：把当前实际持仓登记为意图。

人工平仓（deal.reason=CLIENT，或任何执行器解释不了的出场）之后，对账
进程会看到"意图 ≠ 实际"并写 ``pause-<magic>.json``——这是设计行为（不
把未经确认的漂移洗白）。人工核对无误后运行本脚本：

- 读该 magic 的当前实际持仓（净额），写成 ``expected-<magic>.json`` 的
  confirmed 意图（``cause="ack_external"``）；
- **N-C1/R7 确认绑定事件**：意图文件附 ``confirmed_events``——被确认的
  ticket 集合（此刻 state-<magic>.json 的未决外部出场）+ 确认时刻 +
  确认时实际净持仓快照（明细+净额）。执行器只清空绑定覆盖、且确认后
  世界未变的未决事件；确认之后新出现的出场需要**再次**人工确认——
  旧的裸确认不再永久放行（N-C1 缺陷）；
- 写告警日志（ACK_EXTERNAL，含 --reason 与绑定明细）；
- 下一轮对账一致 → ``pause-<magic>.json`` 自动解除 → 策略恢复开仓。

空仓时（人工已全部平掉）品种从现有意图/旧格式文件读取（策略登记过）；
既无持仓又无登记文件时须用 --symbol 指定。

用法：
    .venv/Scripts/python.exe scripts/live_ack_external.py --data-dir data \
        --magic 20261101 --reason "人工平仓已核对"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway  # noqa: E402
from mt5gw.errors import DataUnavailableError  # noqa: E402
from mt5live.fileio import write_json_atomic  # noqa: E402
from mt5live.monitor import append_alert, iso_utc, naive_utc  # noqa: E402
from mt5live.reconcile import (  # noqa: E402
    INTENT_FILE_FMT,
    STATUS_CONFIRMED,
    ExpectedStateError,
    Intent,
    load_intents,
)

ALERT_ACK_EXTERNAL = "ACK_EXTERNAL"


def _open_gateway(gw_config_path: str) -> MT5Gateway:
    """连接 MT5 终端（只读持仓）；配置缺失用默认网关配置。"""
    if os.path.exists(gw_config_path):
        gw = MT5Gateway.from_config_file(gw_config_path)
    else:
        gw = MT5Gateway(GatewayConfig())
    gw.connect()
    return gw


def _pending_exit_tickets(live_dir: str, magic: int) -> list[int]:
    """读 state-<magic>.json 的未决外部出场 ticket（N-C1/R7 确认绑定的
    事件集合）。文件缺失/损坏 → 空集——执行器对绑定不含的票一律不放行
    （fail-closed），人工需在执行器记录事件后再次确认。"""
    path = os.path.join(live_dir, f"state-{magic}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    pending = data.get("pending_external_exits") if isinstance(data, dict) \
        else None
    if not isinstance(pending, dict):
        return []
    out = []
    for k in pending:
        try:
            out.append(int(k))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def ack(live_dir: str, gw: MT5Gateway, magic: int, reason: str, *,
        symbol: str | None = None,
        now: datetime | None = None) -> int:
    """把 magic 当前实际净持仓登记为 confirmed 意图（cause=ack_external），
    并附确认绑定（N-C1/R7：被确认 ticket 集合+确认时刻+净持仓快照）。"""
    n = naive_utc(now if now is not None
                  else datetime.now(timezone.utc).replace(tzinfo=None))
    try:
        actual = gw.account.positions()
    except DataUnavailableError as e:
        print(f"× 持仓查询失败：{e}")
        return 2
    mine = [p for p in actual if p.magic == magic]
    symbols = {p.symbol for p in mine}
    if len(symbols) > 1:
        print(f"× magic {magic} 在多个品种上有持仓（{sorted(symbols)}），"
              "无法登记为单一意图——先人工处理")
        return 2
    sym = (symbols.pop() if symbols else None) or symbol
    if sym is None and not mine:
        # 空仓且未显式指定品种：从已登记的意图文件（含旧格式）取
        try:
            for it in load_intents(live_dir):
                if it.magic == magic:
                    sym = it.symbol
                    break
        except ExpectedStateError:
            pass
    if sym is None:
        print(f"× magic {magic} 无持仓且无已登记意图文件，无法确定品种"
              "——用 --symbol 指定")
        return 2
    net = sum(p.volume if p.side == "buy" else -p.volume for p in mine)
    intent = Intent(symbol=sym, magic=magic,
                    side="buy" if net >= 0 else "sell", volume=abs(net),
                    status=STATUS_CONFIRMED, cause="ack_external", ts=n)
    # N-C1/R7 确认绑定：被确认的未决出场集合 + 确认时刻 + 确认时实际
    # 持仓快照（明细+净额）。执行器只消费绑定覆盖的事件；之后新出场
    # 需要再次人工确认
    tickets = _pending_exit_tickets(live_dir, magic)
    snapshot = [{"ticket": int(p.ticket), "side": str(p.side),
                 "volume": float(p.volume)} for p in mine]
    path = os.path.join(live_dir, INTENT_FILE_FMT.format(magic=magic))
    write_json_atomic(path, {
        "symbol": intent.symbol,
        "magic": intent.magic,
        "side": intent.side,
        "volume": float(intent.volume),
        "status": intent.status,
        "cause": intent.cause,
        "ts": iso_utc(intent.ts),
        "confirmed_events": {
            "tickets": tickets,
            "confirmed_at": iso_utc(n),
            "net_volume": float(net),
            "positions": snapshot,
        },
    })
    bound = (f"，绑定未决出场 ticket {tickets}"
             if tickets else "，无未决出场事件绑定")
    append_alert(live_dir, ALERT_ACK_EXTERNAL,
                 f"magic {magic} 人工确认外部出场（{reason}）：意图登记为 "
                 f"{intent.side} {intent.volume:.2f} 手 {sym}（确认时净持仓"
                 f"快照 {net:+.2f} 手{bound}）——仅解除绑定事件的出场阻断，"
                 f"确认之后新出现的出场需再次确认；下一轮对账一致后自动"
                 f"解除暂停", n)
    print(f"· magic {magic} 意图已登记：{intent.side} {intent.volume:.2f} 手 "
          f"{sym}（cause=ack_external{bound}，净持仓快照 {net:+.2f} 手）"
          "——下一轮对账一致后自动解除暂停")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="人工确认外部出场：把当前实际持仓登记为意图")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--gw-config", default="config.json", help="网关配置 JSON")
    ap.add_argument("--magic", type=int, required=True, help="策略 magic")
    ap.add_argument("--reason", required=True, help="人工确认说明（写进告警日志）")
    ap.add_argument("--symbol", default=None,
                    help="空仓且无登记文件时指定品种")
    args = ap.parse_args(argv)

    live_dir = os.path.join(args.data_dir, "live")
    try:
        gw = _open_gateway(args.gw_config)
    except Exception as e:
        print(f"× 网关连接失败：{e}")
        return 2
    try:
        return ack(live_dir, gw, args.magic, args.reason,
                   symbol=args.symbol)
    finally:
        gw.shutdown()


if __name__ == "__main__":
    sys.exit(main())
