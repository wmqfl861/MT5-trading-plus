"""Resolve a durable UNKNOWN execution only after an operator reconciles MT5.

This command never submits or modifies orders. It refuses to run while the
strategy runner owns its per-magic lock, refuses while pending orders exist,
and requires an explicit human confirmation that broker positions/orders/deals
have been reconciled against the recorded decision.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway  # noqa: E402
from mt5live.fileio import write_json_atomic  # noqa: E402
from mt5live.monitor import append_alert, iso_utc, parse_iso  # noqa: E402
from mt5live.reconcile import (  # noqa: E402
    INTENT_FILE_FMT, STATUS_CONFIRMED, Intent, write_intent,
)
from mt5live.runner import MagicInstanceLock  # noqa: E402


def _open_gateway(path: str) -> MT5Gateway:
    gw = MT5Gateway.from_config_file(path) if os.path.exists(path) else MT5Gateway(GatewayConfig())
    gw.connect()
    return gw


def ack_execution(live_dir: str, gw: MT5Gateway, magic: int, decision_id: str,
                  reason: str, *, now: datetime | None = None) -> int:
    """Acknowledge an UNKNOWN order only after explicit broker reconciliation."""
    try:
        account_state = gw.account.state()
        lock = MagicInstanceLock.for_account(account_state, magic)
    except Exception as exc:
        print(f"× 无法确定 MT5 账户身份，拒绝解除 UNKNOWN：{exc}")
        return 2
    if not lock.acquire():
        print("× 同一 MT5 账户/magic 的 Runner 仍持有账户级锁；"
              "先停止所有 data_root 下该账户的 Runner")
        return 3
    try:
        state_path = os.path.join(live_dir, f"state-{magic}.json")
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"× state 文件缺失/损坏，拒绝自动解除：{exc}")
            return 2
        if state.get("magic") != magic:
            print("× state 文件 magic 与请求不一致，拒绝解除")
            return 2
        marker = state.get("execution_unknown")
        if not isinstance(marker, dict):
            print("× 没有待确认的 execution_unknown")
            return 2
        if marker.get("status") not in ("prepared", "unknown"):
            print(f"× execution_unknown 状态不允许人工解除：{marker.get('status')!r}")
            return 2
        if str(marker.get("decision_id") or "") != decision_id:
            print("× decision_id 与待确认记录不一致，拒绝解除")
            return 2

        try:
            positions = [p for p in gw.account.positions() if p.magic == magic]
            pending = [p for p in gw.account.pending_orders() if p.magic == magic]
        except Exception as exc:
            print(f"× 查询券商持仓/挂单失败，保持 UNKNOWN：{exc}")
            return 2
        if pending:
            print(f"× magic={magic} 仍有挂单 {[p.ticket for p in pending]}，不得解除")
            return 2
        symbols = {p.symbol for p in positions}
        if len(symbols) > 1:
            print(f"× 同一 magic 出现多个品种持仓 {sorted(symbols)}，需人工处理")
            return 2

        intent_path = os.path.join(live_dir, INTENT_FILE_FMT.format(magic=magic))
        try:
            with open(intent_path, encoding="utf-8") as f:
                intent = json.load(f)
        except (OSError, ValueError):
            intent = {}
        expected_symbol = intent.get("symbol") or marker.get("symbol")
        if symbols and expected_symbol and symbols != {expected_symbol}:
            print(f"× 当前持仓品种 {sorted(symbols)} 与策略登记品种 "
                  f"{expected_symbol!r} 不一致，拒绝解除")
            return 2
        symbol = next(iter(symbols), None) or expected_symbol
        if not symbol:
            print("× 无法确定策略品种，拒绝解除")
            return 2

        n = (now or datetime.now(timezone.utc).replace(tzinfo=None))
        if n.tzinfo is not None:
            n = n.astimezone(timezone.utc).replace(tzinfo=None)
        try:
            since = parse_iso(str(marker.get("ts") or intent.get("ts")))
        except (TypeError, ValueError):
            since = n - timedelta(days=2)
        try:
            deals = [d for d in gw.account.deals(since, n) if d.magic == magic]
        except Exception as exc:
            print(f"× 查询成交历史失败，保持 UNKNOWN：{exc}")
            return 2

        foreign_deals = sorted({d.symbol for d in deals if d.symbol != symbol})
        if foreign_deals:
            print(f"× 该 magic 的成交历史包含其他品种 {foreign_deals}，拒绝解除")
            return 2
        net = sum(float(p.volume) if p.side == "buy" else -float(p.volume)
                  for p in positions)
        snapshot = [{"ticket": int(p.ticket), "symbol": p.symbol,
                     "side": p.side, "volume": float(p.volume)}
                    for p in positions]
        deal_snapshot = [{"ticket": int(d.ticket), "order": int(d.order),
                          "symbol": d.symbol, "entry": d.entry,
                          "side": d.side, "volume": float(d.volume),
                          "reason": d.reason, "time": iso_utc(d.time)}
                         for d in deals]
        stamp = iso_utc(n)
        state["execution_resolution"] = {
            "decision_id": decision_id, "confirmed_at": stamp,
            "reason": reason, "net_volume": net,
            "positions": snapshot, "deals_since_intent": deal_snapshot,
            "pending_orders": [],
        }
        state["execution_unknown"] = None
        write_json_atomic(state_path, state)
        write_intent(intent_path, Intent(
            symbol=symbol, magic=magic,
            side="buy" if net >= 0 else "sell", volume=abs(net),
            status=STATUS_CONFIRMED, cause="ack_execution", ts=n))
        append_alert(
            live_dir, "ACK_EXECUTION_UNKNOWN",
            f"magic {magic} 人工核对并解除 UNKNOWN（decision_id={decision_id}；"
            f"reason={reason}；net={net:+.2f}；positions={snapshot}；"
            f"deals_since_intent={len(deal_snapshot)}）。该操作仅记录人工确认，"
            "不提交订单；解除前必须已在券商终端核对成交/挂单/持仓", n)
        print(f"✓ UNKNOWN 已按人工核对解除：magic={magic} decision_id={decision_id} "
              f"net={net:+.2f}；未发送任何订单")
        return 0
    finally:
        lock.release()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="人工核对并解除 UNKNOWN 订单状态（只读券商，不发送订单）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--gw-config", default="config.json")
    ap.add_argument("--magic", type=int, required=True)
    ap.add_argument("--decision-id", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--confirm-broker-reconciled", action="store_true",
                    help="确认已人工核对 MT5 持仓、挂单、成交历史与订单状态")
    args = ap.parse_args(argv)
    if not args.confirm_broker_reconciled:
        print("× 必须提供 --confirm-broker-reconciled；不会自动解除未知订单")
        return 2
    try:
        gw = _open_gateway(args.gw_config)
    except Exception as exc:
        print(f"× 网关连接失败：{exc}")
        return 2
    try:
        return ack_execution(
            os.path.join(args.data_dir, "live"), gw, args.magic,
            args.decision_id, args.reason)
    finally:
        gw.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
