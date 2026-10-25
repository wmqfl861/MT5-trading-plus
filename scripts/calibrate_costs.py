"""成本校准 CLI（T5 / R4）：模拟盘真实成交 vs 快照推算。

只读账户历史（gw.account.deals），绝不下单、不初始化交易。产出：
- {data-root}/costs/calibrated.json——机器读：CommissionModel.from_calibrated、
  SwapModel 规则核对（实测每手每夜隔夜费多/空）；
- {data-root}/reports/costs/calibration.md——人读：实测与快照推算并列对照。

用法（主会话在模拟盘开平几笔、含过夜持仓后执行）：
    .venv/Scripts/python.exe scripts/calibrate_costs.py
    .venv/Scripts/python.exe scripts/calibrate_costs.py --since 2026-10-19 --to 2026-10-23

口径：
- 佣金按成交方向聚合（entry=in 为开、out/out_by/inout 为平），每手金额=Σ佣/Σ量，
  来回=开+平（与 CommissionModel 一次性给出口径一致）；
- 隔夜费（T20/H1）：MT5 把累计 swap 记在平仓成交上，按 position_id 配对出入场
  得到持仓区间 (entry, exit]，计费夜数与 mt5quant.costs.swap.charge_nights
  同源——规则 = SwapRule 默认（计费时刻 17:00 America/New_York，DST 感知；
  周末不计），三倍日取快照 swap_rollover3days（经 mt5_to_py_weekday）。
  实测费率即"每夜"口径，回测按同一规则（含 ×3）复算；
- 免隔夜费判定（T20）：存在**跨过计费时刻**的持仓且全部 swap=0 → 是；
  没有任何跨计费时刻的持仓 → 未知（None）。跨日历日但未跨计费时刻
  （如冬令时 22:00 入场、次日 20:00 出场）不算过夜。
"""
import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw.snapshot import write_json_atomic  # noqa: E402
from mt5quant.costs.swap import (  # noqa: E402
    SwapRule, charge_nights, mt5_to_py_weekday, per_lot_per_night,
)

_TRADING_SIDES = ("buy", "sell")
_ENTRY_IN = "in"
_ENTRY_OUT = ("out", "out_by", "inout")  # inout=反手：按平仓侧计佣金


@dataclass(frozen=True)
class ClosedPosition:
    """按 position_id 配对的已平持仓（swap 为平仓成交上的累计值，账户货币）。"""

    position_id: int
    symbol: str
    side: str  # "long"/"short"（按入场成交方向）
    volume: float
    entry_time: datetime
    exit_time: datetime
    swap: float


def _is_trading(deal) -> bool:
    return deal.side in _TRADING_SIDES and deal.entry in (_ENTRY_IN,) + _ENTRY_OUT


def nights_between(entry_time: datetime, exit_time: datetime,
                   rule: SwapRule) -> int:
    """(entry, exit] 内的计费夜数（与 mt5quant.costs.swap.charge_nights 同源）。

    T20：计费时刻/星期由 rule（17:00 America/New_York、周末不计）决定，
    三倍日 = rule.triple_weekdays（快照 swap_rollover3days 换算）。
    """
    return charge_nights(entry_time, exit_time, rule)


def match_closed_positions(deals) -> tuple[list[ClosedPosition], int]:
    """交易类成交 → 已平持仓列表 + 未平仓数（有入场无平仓）。"""
    by_pos: dict[int, dict] = {}
    for d in deals:
        g = by_pos.setdefault(d.position_id, {"in": [], "out": []})
        g[_ENTRY_IN if d.entry == _ENTRY_IN else "out"].append(d)
    closed: list[ClosedPosition] = []
    open_count = 0
    for pid, g in by_pos.items():
        if not g["in"]:
            continue  # 只有平仓成交（窗口截掉了入场）：无区间，不进统计
        if not g["out"]:
            open_count += 1
            continue
        first_in = min(g["in"], key=lambda d: d.time)
        closed.append(ClosedPosition(
            position_id=pid,
            symbol=first_in.symbol,
            side="long" if first_in.side == "buy" else "short",
            volume=sum(d.volume for d in g["in"]),
            entry_time=first_in.time,
            exit_time=max(d.time for d in g["out"]),
            swap=sum(d.swap for d in g["out"]),
        ))
    return closed, open_count


def commission_summary(deals) -> dict:
    """每手佣金（账户货币）：开/平分侧聚合，来回=开+平。"""
    comm_in = vol_in = comm_out = vol_out = 0.0
    n_in = n_out = 0
    for d in deals:
        if d.entry == _ENTRY_IN:
            comm_in += d.commission
            vol_in += d.volume
            n_in += 1
        else:
            comm_out += d.commission
            vol_out += d.volume
            n_out += 1
    per_in = comm_in / vol_in if vol_in > 0 else None
    per_out = comm_out / vol_out if vol_out > 0 else None
    return {
        "deals_in": n_in, "volume_in": round(vol_in, 8),
        "per_lot_in": per_in,
        "deals_out": n_out, "volume_out": round(vol_out, 8),
        "per_lot_out": per_out,
        "round_trip_per_lot": (per_in + per_out
                               if per_in is not None and per_out is not None else None),
    }


def _side_stats(positions, side, charged, rule, feasible, free):
    of_side = [p for p in positions if p.side == side]
    charged_side = [p for p in charged if p.side == side]
    if not feasible:
        rate = None            # 实测与固定规则矛盾：费率置空，warnings 已说明
    elif charged_side:
        rates = [p.swap / p.volume /
                 nights_between(p.entry_time, p.exit_time, rule)
                 for p in charged_side]
        rate = statistics.median(rates)
    else:
        rate = 0.0 if free is True else None   # 免隔夜费=实测 0；无从判断=None
    return {
        "positions": len(of_side),
        "positions_with_swap": len(charged_side),
        "total_swap": round(sum(p.swap for p in of_side), 8),
        "per_lot_per_night": rate,
    }


def _crossed_charge_hours(positions, rule) -> list[int]:
    """全部持仓区间跨过的计费时刻所在的 UTC 小时（信息性，T20 固定规则）。

    实际数夜一律走 charge_nights；本函数只为报告展示计费时刻随 DST 的落点
    （夏令时 21、冬令时 22 UTC）。
    """
    hours: set[int] = set()
    for p in positions:
        day = p.entry_time.date() - timedelta(days=1)
        end = p.exit_time.date() + timedelta(days=1)
        while day <= end:
            moment = _charge_moment(day)
            if moment is not None and p.entry_time < moment <= p.exit_time:
                hours.add(moment.hour)
            day += timedelta(days=1)
    return sorted(hours)


def _charge_moment(day):
    """day（本地日）17:00 America/New_York → naive UTC datetime。"""
    from mt5data.sessions import local_to_utc_naive
    try:
        return local_to_utc_naive(day, "America/New_York", "17:00")
    except ValueError:
        return None


def calibrate_swap(symbol: str, positions: list[ClosedPosition],
                   snap: dict, warnings: list) -> dict:
    """单品种隔夜费校准：实测费率（按 SwapRule v2 复算）+ 快照推算并列。

    T20：计费时刻固定 17:00 America/New_York（DST 感知、周末不计），三倍日取
    快照 swap_rollover3days。实测费率 = swap / 手数 / 计费夜数（含 ×3 加权），
    回测 build_costs 按同一规则复算即与实测对上。
    """
    entry = snap.get(symbol)
    triple_raw = entry.get("swap_rollover3days") if entry else None
    triple_py = None
    if triple_raw is None:
        warnings.append(f"{symbol} 快照缺 swap_rollover3days，按无三倍日估算"
                        "（费率口径与回测可能不一致，重跑 snapshot_specs.py）")
        triple = frozenset()
    else:
        triple_py = mt5_to_py_weekday(triple_raw)
        triple = frozenset({triple_py})
    rule = SwapRule(long_per_day=0.0, short_per_day=0.0, triple_weekdays=triple)

    charged = [p for p in positions if p.swap != 0 and p.volume > 0]
    # T20：过夜 = 跨过计费时刻（不再是跨日历日）
    overnight = [p for p in positions
                 if nights_between(p.entry_time, p.exit_time, rule) > 0]
    if charged:
        free = False
    elif overnight:
        free = True
    else:
        free = None  # 无跨计费时刻的持仓：无从判断，不猜

    # 可行性：swap≠0 的持仓在固定规则下必须有 ≥1 夜，否则实测与规则矛盾
    feasible = True
    infeasible = [p for p in charged
                  if nights_between(p.entry_time, p.exit_time, rule) == 0]
    if infeasible:
        feasible = False
        warnings.append(
            f"{symbol}：{len(infeasible)} 笔 swap≠0 的持仓未跨过 17:00 纽约"
            "计费时刻——实测与固定规则矛盾，费率置空，须人工核对（Exness "
            "冬令时跟随欧洲还是美国见 docs/research/rollover-dst-2026-10-25.md）")

    snap_block = {"available": False}
    if entry is None:
        reason = "不在快照里（跑 snapshot_specs.py）"
        warnings.append(f"{symbol} {reason}，无法并列推算")
        snap_block["reason"] = reason
    else:
        try:
            lng, sht = per_lot_per_night(entry)
            snap_block = {
                "available": True, "swap_mode": entry.get("swap_mode"),
                "long_per_lot_per_night": lng,
                "short_per_lot_per_night": sht,
                "triple_weekday_py": triple_py,
            }
        except (KeyError, NotImplementedError, ValueError) as e:
            warnings.append(f"{symbol} 快照隔夜费不可推算：{e}")
            snap_block["reason"] = str(e)

    result = {
        "free_of_swap": free,
        # T20：计费时刻固定 17:00 纽约——此字段改为信息性输出（跨过的计费时刻
        # 所在 UTC 小时，随 DST 在 21/22 间移动），回测不再消费
        "charge_hour_candidates_utc": _crossed_charge_hours(charged, rule),
        "charge_hour_ambiguous": False,
        "long": _side_stats(positions, "long", charged, rule, feasible, free),
        "short": _side_stats(positions, "short", charged, rule, feasible, free),
        "snapshot": snap_block,
    }
    if snap_block["available"]:
        for side, snap_key in (("long", "long_per_lot_per_night"),
                               ("short", "short_per_lot_per_night")):
            measured = result[side]["per_lot_per_night"]
            expected = snap_block[snap_key]
            if measured is not None and expected is not None:
                if abs(measured - expected) > max(0.2 * abs(expected), 0.1):
                    warnings.append(
                        f"{symbol} {side} 实测 {measured:.4g}/手/夜 与快照推算 "
                        f"{expected:.4g} 偏差超 20%，用前先人工核对")
    return result


def build_result(gw, snap: dict, since: datetime, to: datetime,
                 *, now_utc: datetime | None = None) -> dict:
    """主逻辑（gateway 可注入）：读成交历史 → 校准结果 dict（写盘前的唯一真相）。"""
    state = gw.account.state()
    deals = gw.account.deals(since, to)
    trading = [d for d in deals if _is_trading(d)]

    by_symbol: dict[str, list] = {}
    for d in trading:
        by_symbol.setdefault(d.symbol, []).append(d)

    symbols_out: dict[str, dict] = {}
    warnings: list[str] = []
    closed_total = open_total = 0
    for symbol in sorted(by_symbol):
        sym_deals = by_symbol[symbol]
        positions, open_count = match_closed_positions(sym_deals)
        closed_total += len(positions)
        open_total += open_count
        symbols_out[symbol] = {
            "commission": commission_summary(sym_deals),
            "swap": calibrate_swap(symbol, positions, snap, warnings),
        }

    free_flags = [symbols_out[s]["swap"]["free_of_swap"] for s in symbols_out]
    if False in free_flags:
        account_free = False
    elif True in free_flags:
        account_free = True
    else:
        account_free = None

    return {
        "version": 1,
        "generated_utc": (now_utc or datetime.now(timezone.utc)).isoformat(),
        "account": {"login": state.login, "server": state.server,
                    "currency": state.currency},
        "window": {"since": since.isoformat(sep=" "),
                   "to": to.isoformat(sep=" ")},
        "summary": {"deals_total": len(deals), "deals_trading": len(trading),
                    "closed_positions": closed_total, "open_positions": open_total},
        "account_free_of_swap": account_free,
        "symbols": symbols_out,
        "warnings": warnings,
    }


def _fmt(x) -> str:
    return "—" if x is None else f"{x:.4g}"


def _fmt_hours(hours: list[int]) -> str:
    return "未知" if not hours else "/".join(str(h) for h in hours)


def render_markdown(res: dict) -> str:
    lines = [
        "# 成本校准报告（模拟盘实测 vs 快照推算）",
        "",
        f"- 账户：{res['account']['login']}@{res['account']['server']}"
        f"（货币 {res['account']['currency']}）",
        f"- 窗口：{res['window']['since']} → {res['window']['to']}"
        f"（成交 {res['summary']['deals_total']} 笔，交易类 "
        f"{res['summary']['deals_trading']} 笔；已平仓 "
        f"{res['summary']['closed_positions']}，未平仓 {res['summary']['open_positions']}）",
        f"- 免隔夜费：{_fmt(res['account_free_of_swap'])}；"
        f"生成：{res['generated_utc']}",
        "",
    ]
    for sym, info in res["symbols"].items():
        c = info["commission"]
        sw = info["swap"]
        lines.append(f"## {sym}")
        lines.append("")
        lines.append(
            f"- 佣金/手：开 {_fmt(c['per_lot_in'])} + 平 {_fmt(c['per_lot_out'])}"
            f" = 来回 {_fmt(c['round_trip_per_lot'])}"
            f"（开 {c['deals_in']} 笔 {_fmt(c['volume_in'])} 手 / "
            f"平 {c['deals_out']} 笔 {_fmt(c['volume_out'])} 手）")
        free_txt = {True: "是", False: "否", None: "未知"}[sw["free_of_swap"]]
        snap = sw["snapshot"]
        lines.append(
            f"- 隔夜费（免隔夜费={free_txt}；计费时刻=17:00 纽约，实测跨过"
            f" UTC {_fmt_hours(sw['charge_hour_candidates_utc'])}）")
        for side in ("long", "short"):
            s = sw[side]
            label = "多" if side == "long" else "空"
            snap_rate = (snap.get(f"{side}_per_lot_per_night")
                         if snap.get("available") else None)
            lines.append(
                f"  - {label}：{s['positions_with_swap']} 笔带费共 "
                f"{_fmt(s['total_swap'])} → 实测 {_fmt(s['per_lot_per_night'])}"
                f"/手/夜（快照推算 {_fmt(snap_rate)}）")
        if not snap.get("available"):
            lines.append(f"  - 快照推算不可用：{snap.get('reason', '缺品种或口径未实现')}")
        lines.append("")
    if res["warnings"]:
        lines.append("## 告警")
        lines.append("")
        lines.extend(f"- {w}" for w in res["warnings"])
        lines.append("")
    return "\n".join(lines)


def _write_text_atomic(path: str, text: str) -> None:
    # 模式对齐 mt5gw.snapshot：tmp + os.replace 带重试（Windows 句柄占用）
    import time
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)


def write_outputs(res: dict, data_root: str) -> tuple[str, str]:
    """落盘 calibrated.json + calibration.md，返回两路径。"""
    json_path = os.path.join(data_root, "costs", "calibrated.json")
    md_path = os.path.join(data_root, "reports", "costs", "calibration.md")
    write_json_atomic(json_path, res)
    _write_text_atomic(md_path, render_markdown(res))
    return json_path, md_path


def main() -> int:
    ap = argparse.ArgumentParser(description="用模拟盘成交校准佣金与隔夜费（只读）")
    ap.add_argument("--since", default=None, help="窗口起（naive UTC，如 2026-10-19）")
    ap.add_argument("--to", default=None, help="窗口止（naive UTC；缺省=现在）")
    ap.add_argument("--days", type=int, default=30,
                    help="不给 --since/--to 时回看的天数")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    snap_path = os.path.join(args.data_root, "specs", "latest.json")
    if not os.path.exists(snap_path):
        print(f"✗ 缺快照 {snap_path}，先跑 scripts/snapshot_specs.py")
        return 1
    with open(snap_path, encoding="utf-8") as f:
        snap = json.load(f)

    to = (datetime.fromisoformat(args.to) if args.to
          else datetime.now(timezone.utc).replace(tzinfo=None))
    since = (datetime.fromisoformat(args.since) if args.since
             else to - timedelta(days=args.days))

    from mt5gw import GatewayConfig, MT5Gateway, load
    cfg = load(args.config) if os.path.exists(args.config) else GatewayConfig()
    with MT5Gateway(cfg) as gw:
        res = build_result(gw, snap, since, to)

    json_path, md_path = write_outputs(res, args.data_root)
    print(f"✓ 窗口内 {res['summary']['deals_trading']} 笔交易类成交、"
          f"{res['summary']['closed_positions']} 个已平持仓"
          f"（{len(res['symbols'])} 个品种）")
    for w in res["warnings"]:
        print(f"⚠ {w}")
    print(f"✓ {json_path}\n✓ {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
