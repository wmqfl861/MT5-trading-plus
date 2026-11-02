"""execution journal 永久 unknown 项的人工仲裁出口（H2-01 阶段 1D，R7）。

order_send 回报丢失且重启核对始终无法裁决（券商核对查询长期不可用、
journal 撕裂行的合成未决项等）时，未决项会持续暂停开新仓（fail-closed，
平仓不受影响）。人工核对券商后台后用本工具落终态：

- ``--status confirmed``：人工确认该决策的订单已成交（或已人工了结）；
- ``--status rejected``：人工确认该决策的订单确未成交/未到达服务器；
- 必须先停止 runner；工具取得账户身份锁与旧目录锁，拒绝并发仲裁；
- 先恢复意图文件，再追加终态行（detail 前缀 ``arbitrated:``）；
  意图写失败时不提交新终态，保留开仓保护；
- 完成后仍需整体对账；不自动清除 halt/pause/killSwitch；
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
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import MT5Gateway  # noqa: E402
from mt5live.executor import JOURNAL_FILE_FMT  # noqa: E402
from mt5live.fileio import append_line, file_lock  # noqa: E402
from mt5live.monitor import append_alert, iso_utc, naive_utc  # noqa: E402
from mt5live.reconcile import (  # noqa: E402
    INTENT_FILE_FMT,
    STATUS_CONFIRMED,
    ExpectedStateError,
    Intent,
    load_intents,
    write_intent,
)

from mt5live.runner import instance_lock_key, runner_lock_path  # noqa: E402

ALERT_ACK_EXECUTION = "ACK_EXECUTION"


def _open_gateway(gw_config_path: str) -> MT5Gateway:
    """Require an explicit configuration; never connect a default terminal."""
    if not Path(gw_config_path).is_file():
        raise FileNotFoundError(f"网关配置不存在，拒绝回退默认账户：{gw_config_path}")
    gw = MT5Gateway.from_config_file(gw_config_path)
    try:
        gw.connect()
    except Exception:
        try:
            gw.shutdown()
        except Exception:
            pass
        raise
    return gw


def _journal_state(live_dir: str, magic: int) -> dict:
    """读 journal 的终态/未决两张表（与执行器 _journal_refresh 同规则；
    撕裂行同样按合成未决项列出，供 --list 与人工仲裁取 id）。"""
    path = os.path.join(live_dir, JOURNAL_FILE_FMT.format(magic=magic))
    finals: dict[str, dict] = {}
    undecided: dict[str, str] = {}
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raw = b""
    # Same universal-newline semantics and zero-based torn IDs as the runner.
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                raise ValueError("非对象日志行")
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
    return {"finals": finals, "undecided": undecided, "path": path, "raw": raw}


def _demo_identity(gw) -> tuple[str, int]:
    """Read-only account check; do not arbitrate against an unknown/live account."""
    account = gw.account.state()
    mode = getattr(account, "trade_mode", None)
    server = getattr(account, "server", None)
    login = getattr(account, "login", None)
    if type(mode) is not int or mode != 0:
        raise ValueError("仲裁只允许已确认的 DEMO 账户（trade_mode=0）")
    if not isinstance(server, str) or not server.strip():
        raise ValueError("账户 server 缺失，无法取得身份锁")
    if type(login) is not int or login <= 0:
        raise ValueError("账户 login 无效，无法取得身份锁")
    return server, login


def _no_pending_orders(gw, magic: int) -> None:
    pending = gw.account.pending_orders()
    if not isinstance(pending, (list, tuple)):
        raise ValueError("挂单查询没有返回完整列表")
    if any(order.magic == magic for order in pending):
        raise ValueError("本 magic 仍有挂单，先核实/撤销后再仲裁；不解除保护")


def _append_final(state: dict, record: dict) -> None:
    """Preserve a torn tail and append the final as a separate durable line."""
    current = Path(state["path"]).read_bytes()
    if current != state["raw"]:
        raise ValueError("journal 在仲裁期间变化，拒绝提交过期结论")
    prefix = "\n" if current and not current.endswith(b"\n") else ""
    append_line(state["path"], prefix + json.dumps(
        record, ensure_ascii=False, allow_nan=False) + "\n")


def ack(live_dir: str, gw: MT5Gateway, magic: int, decision_id: str,
        status: str, reason: str, *, symbol: str | None = None,
        now: datetime | None = None) -> int:
    """Stop the runner, repair intent, then commit the arbitration final last.

    Identity and legacy-directory locks exclude runners and competing operators.
    Failure before the final leaves a NEW arbitration unresolved. Legacy finals
    may only be repaired with the exact same status and reason; never rewritten.
    """
    if status not in ("confirmed", "rejected"):
        print(f"× 非法仲裁状态：{status!r}")
        return 2
    if type(magic) is not int or magic < 0:
        print("× magic 必须为非负整数")
        return 2
    for name, value in (("decision_id", decision_id), ("reason", reason)):
        if (not isinstance(value, str) or not value.strip()
                or any(c in value for c in "\r\n\t")):
            print(f"× {name} 必须为非空单行文本")
            return 2
    live_dir = str(Path(live_dir).resolve())
    try:
        identity = _demo_identity(gw)
        identity_lock = runner_lock_path(instance_lock_key(*identity, magic))
        legacy_lock = os.path.join(live_dir, f"runner-{magic}.lock")
        # The same kernel locks as the runner, in the same acquisition order.
        # killSwitch alone is insufficient: it does not stop a journal writer.
        with file_lock(identity_lock, attempts=1, retry_s=0):
            with file_lock(legacy_lock, attempts=1, retry_s=0):
                if _demo_identity(gw) != identity:
                    raise ValueError("账户在获取锁期间切换，拒绝仲裁")
                return _ack_locked(live_dir, gw, magic, decision_id, status,
                                   reason, symbol=symbol, now=now,
                                   identity=identity)
    except Exception as exc:
        print(f"× 仲裁未完成，保持停机并核对后重试：{type(exc).__name__}: {exc}")
        return 2


def _ack_locked(live_dir, gw, magic, decision_id, status, reason, *,
                symbol, now, identity) -> int:
    n = naive_utc(now if now is not None else datetime.now(timezone.utc))
    state = _journal_state(live_dir, magic)
    existing = state["finals"].get(decision_id)
    if existing is not None:
        if (existing.get("status") != status
                or existing.get("detail") != f"arbitrated: {reason}"):
            raise ValueError("已有终态，拒绝重复/反向仲裁或改写核对原因")
    elif decision_id not in state["undecided"]:
        raise ValueError(f"决策不在未决表中：{decision_id}")

    _no_pending_orders(gw, magic)
    try:
        actual = gw.account.positions()
    except Exception as exc:
        print(f"× 持仓查询失败（未写入 journal 终态，可重试）：{exc}")
        return 2
    if not isinstance(actual, (list, tuple)):
        raise ValueError("持仓查询没有返回完整列表")
    mine = [p for p in actual if p.magic == magic]
    symbols = {p.symbol for p in mine}
    if len(symbols) > 1:
        raise ValueError("同 magic 多品种持仓，不能写成单一意图")
    if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
        raise ValueError("symbol 必须为非空字符串")
    if symbol is not None and symbols and symbol not in symbols:
        raise ValueError("指定 symbol 与券商实际持仓不一致")
    sym = next(iter(symbols), None) or symbol
    if sym is None:
        for intent in load_intents(live_dir):
            if intent.magic == magic:
                sym = intent.symbol
                break
    if not isinstance(sym, str) or not sym.strip():
        raise ValueError("无法确定品种；核对后用 --symbol 指定")
    amounts = []
    for position in mine:
        if position.side not in ("buy", "sell"):
            raise ValueError("持仓方向无效，不允许猜测为卖出")
        if isinstance(position.volume, bool):
            raise ValueError("持仓量不能是布尔值")
        volume = float(position.volume)
        if not math.isfinite(volume) or volume <= 0:
            raise ValueError("持仓量必须为有限正数")
        amounts.append(volume if position.side == "buy" else -volume)
    net = math.fsum(amounts)
    if not math.isfinite(net):
        raise ValueError("净持仓不是有限数")
    intent = Intent(symbol=sym, magic=magic,
                    side="buy" if net >= 0 else "sell", volume=abs(net),
                    status=STATUS_CONFIRMED,
                    cause=f"ack_execution:{decision_id}", ts=n)
    if _demo_identity(gw) != identity:
        raise ValueError("账户已切换，拒绝提交仲裁")
    _no_pending_orders(gw, magic)
    if Path(state["path"]).read_bytes() != state["raw"]:
        raise ValueError("journal 已变化，拒绝覆盖其他处理结果")
    # Write the recoverable intent FIRST. Never release the journal gate when
    # this write fails. A crash here leaves pending/unknown intact on restart.
    write_intent(os.path.join(live_dir, INTENT_FILE_FMT.format(magic=magic)),
                 intent)
    if existing is None:
        _append_final(state, {
            "ts": iso_utc(n), "decision_id": decision_id, "status": status,
            "detail": f"arbitrated: {reason}"})
    append_alert(live_dir, ALERT_ACK_EXECUTION,
                 f"magic {magic} 决策 {decision_id} → {status}（{reason}）；"
                 f"意图已恢复 {net:+.2f} 手 {sym}。仍须完成整体对账后恢复运行。",
                 n)
    print(f"· 仲裁完成：{decision_id} → {status}；意图恢复 {net:+.2f} 手 {sym}。"
          "完成整体对账后再恢复运行；本工具不清除 halt/pause/killSwitch。")
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
    try:
        state = _journal_state(live_dir, args.magic)
    except (OSError, ValueError) as exc:
        print(f"× journal 读取失败：{exc}")
        return 2
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
