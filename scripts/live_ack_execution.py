"""execution journal 永久 unknown 项的人工仲裁出口（H2-01 阶段 1D，R7）。

order_send 回报丢失且重启核对始终无法裁决（券商核对查询长期不可用、
journal 撕裂行的合成未决项等）时，未决项会持续暂停开新仓（fail-closed，
平仓不受影响）。人工核对券商后台后用本工具落终态：

- ``--status confirmed``：人工确认该决策的订单已成交（或已人工了结）；
- ``--status rejected``：人工确认该决策的订单确未成交/未到达服务器；
- journal 追加终态行（detail 前缀 ``arbitrated:``）——执行器下一轮刷新
  journal 即出清未决、解除开仓暂停；
- 意图文件按当前实际净持仓重写为 confirmed（cause=``ack_execution:<id>``）
  ——崩溃留下的 pending 意图不再等对账宽限超时；
- 写告警（ACK_EXECUTION，含 --reason）。

撕裂行的合成未决项用其合成 id 仲裁（执行器告警里给出，形如 ``torn@3``）。

用法：
    .venv/Scripts/python.exe scripts/live_ack_execution.py \
        --live-dir data/live --magic 20261101 \
        --decision-id "XAUUSD|M15|2026-11-01T10:00:00" \
        --status rejected --reason "券商后台核对：该单未到达服务器"
（或 --data-dir data —— live 目录取 <data-dir>/live）
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
from mt5live.executor import JOURNAL_FILE_FMT  # noqa: E402
from mt5live.fileio import append_jsonl  # noqa: E402
from mt5live.monitor import append_alert, iso_utc, naive_utc  # noqa: E402
from mt5live.reconcile import (  # noqa: E402
    INTENT_FILE_FMT,
    STATUS_CONFIRMED,
    ExpectedStateError,
    Intent,
    load_intents,
    write_intent,
)

ALERT_ACK_EXECUTION = "ACK_EXECUTION"


def _open_gateway(gw_config_path: str) -> MT5Gateway:
    """连接 MT5 终端（只读持仓）；配置缺失用默认网关配置。"""
    if os.path.exists(gw_config_path):
        gw = MT5Gateway.from_config_file(gw_config_path)
    else:
        gw = MT5Gateway(GatewayConfig())
    gw.connect()
    return gw


def _journal_state(live_dir: str, magic: int) -> dict:
    """读 journal 的终态/未决两张表（与执行器 _journal_refresh 同规则；
    撕裂行同样按合成未决项列出，供 --list 与人工仲裁取 id）。"""
    path = os.path.join(live_dir, JOURNAL_FILE_FMT.format(magic=magic))
    finals: dict[str, dict] = {}
    undecided: dict[str, str] = {}
    if not os.path.exists(path):
        return {"finals": finals, "undecided": undecided, "path": path}
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            did = str(rec.get("decision_id") or "")
            status = str(rec.get("status") or "")
            if not isinstance(rec, dict) or not did or not status:
                raise ValueError("损坏行")
        except ValueError:
            undecided[f"torn@{i}"] = "torn"
            continue
        if status == "pending" and did not in finals:
            undecided[did] = "pending"
        elif status in ("sent", "unknown") and did in undecided:
            undecided[did] = status
        elif status in ("confirmed", "rejected"):
            finals[did] = {"status": status, "detail": rec.get("detail")}
            undecided.pop(did, None)
    return {"finals": finals, "undecided": undecided, "path": path}


def ack(live_dir: str, gw: MT5Gateway, magic: int, decision_id: str,
        status: str, reason: str, *, symbol: str | None = None,
        now: datetime | None = None) -> int:
    """落 journal 终态行 + 意图恢复（实际净持仓 confirmed）。"""
    n = naive_utc(now if now is not None
                  else datetime.now(timezone.utc).replace(tzinfo=None))
    if status not in ("confirmed", "rejected"):
        print(f"× 非法仲裁状态：{status!r}")
        return 2
    if not isinstance(reason, str) or not reason.strip():
        print("× 仲裁原因不能为空")
        return 2

    state = _journal_state(live_dir, magic)
    existing = state["finals"].get(decision_id)
    already_arbitrated = False
    if existing is not None:
        # 仲裁终态已落但意图写入失败时，允许同一结论幂等重试，
        # 只补修意图；普通执行终态或不同结论绝不允许改写。
        if (existing.get("status") != status
                or existing.get("detail") != f"arbitrated: {reason}"):
            print(f"× 决策 {decision_id} 已有终态 "
                  f"{existing.get('status')}——拒绝重复/反向仲裁")
            return 2
        already_arbitrated = True
    elif not state["undecided"].get(decision_id):
        print(f"× 决策 {decision_id} 不在未决表中（journal 未决项："
              f"{sorted(state['undecided']) or '无'}）——先核对 --decision-id")
        return 2

    # 先读券商并完成意图构造/校验，再落 journal 终态。查询失败时不
    # 消耗仲裁机会；若后续意图写入失败，同一 arbitrated 结论可幂等重试。
    try:
        actual = gw.account.positions()
    except Exception as e:
        print(f"× 持仓查询失败（未写入 journal 终态，可重试）："
              f"{type(e).__name__}: {e}")
        return 2
    mine = [p for p in actual if p.magic == magic]
    symbols = {p.symbol for p in mine}
    if len(symbols) > 1:
        print(f"× magic {magic} 在多个品种上有持仓（{sorted(symbols)}），"
              "无法登记为单一意图——未写入 journal 终态，意图请人工核对")
        return 2
    sym = (symbols.pop() if symbols else None) or symbol
    if sym is None and not mine:
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
                    status=STATUS_CONFIRMED,
                    cause=f"ack_execution:{decision_id}", ts=n)
    if not already_arbitrated:
        append_jsonl(state["path"], {
            "ts": iso_utc(n), "decision_id": decision_id, "status": status,
            "detail": f"arbitrated: {reason}"})
    try:
        write_intent(os.path.join(live_dir, INTENT_FILE_FMT.format(magic=magic)),
                     intent)
    except Exception as e:
        print(f"× 意图恢复写入失败（仲裁终态已落；可用相同 status/reason "
              f"重试修复）：{type(e).__name__}: {e}")
        return 2
    append_alert(live_dir, ALERT_ACK_EXECUTION,
                 f"magic {magic} 人工仲裁 execution journal 未决项：决策 "
                 f"{decision_id} → {status}（{reason}）——意图恢复为实际净"
                 f"持仓 {net:+.2f} 手 {sym}；执行器下一轮解除开仓暂停",
                 n)
    print(f"· magic {magic} 决策 {decision_id} 仲裁为 {status}；意图恢复为 "
          f"实际净持仓 {net:+.2f} 手 {sym}（cause={intent.cause}）"
          "——执行器下一轮解除开仓暂停")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="execution journal 永久 unknown 项的人工仲裁出口")
    ap.add_argument("--live-dir", default=None,
                    help="live 目录（与 --data-dir 二选一）")
    ap.add_argument("--data-dir", default=None,
                    help="数据目录（live 目录取 <data-dir>/live）")
    ap.add_argument("--gw-config", default="config.json",
                    help="网关配置 JSON")
    ap.add_argument("--magic", type=int, required=True, help="策略 magic")
    ap.add_argument("--decision-id", default=None,
                    help="被仲裁的 decision_id（撕裂行用合成 id，如 torn@3）")
    ap.add_argument("--status", default=None,
                    choices=("confirmed", "rejected"),
                    help="仲裁结论：已成交 / 确未成交")
    ap.add_argument("--reason", default=None, help="人工核对说明（入告警）")
    ap.add_argument("--symbol", default=None,
                    help="空仓且无登记文件时指定品种")
    ap.add_argument("--list", action="store_true",
                    help="只列出该 magic 的未决项后退出（不仲裁）")
    args = ap.parse_args(argv)

    if not args.list and not (args.decision_id and args.status and args.reason):
        ap.error("仲裁需要 --decision-id、--status 与 --reason"
                 "（或用 --list 只查看未决项）")

    if bool(args.live_dir) == bool(args.data_dir):
        print("× --live-dir 与 --data-dir 恰好提供一个")
        return 2
    live_dir = args.live_dir or os.path.join(args.data_dir, "live")
    state = _journal_state(live_dir, args.magic)
    if args.list:
        undecided = state["undecided"]
        print(f"journal：{state['path']}")
        print(f"未决 {len(undecided)} 项："
              + (", ".join(f"{k}({v})" for k, v in sorted(undecided.items()))
                 or "无"))
        return 0
    try:
        gw = _open_gateway(args.gw_config)
    except Exception as e:
        print(f"× 网关连接失败：{e}")
        return 2
    try:
        return ack(live_dir, gw, args.magic, args.decision_id, args.status,
                   args.reason, symbol=args.symbol)
    finally:
        gw.shutdown()


if __name__ == "__main__":
    sys.exit(main())
