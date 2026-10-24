"""成本与可交易性审计 CLI（只读本地 parquet，不连 MT5）。

    .venv/Scripts/python.exe scripts/cost_audit.py --symbols XAUUSD,BTCUSD
输出 {out}/heatmap.csv（行=品种×周期，列=UTC 小时，格=来回成本/平均振幅）
与 {out}/scope.md（纳入/排除清单及理由、禁止时段=rollover 小时、
无 tick/无 K 线备注）。ticks 缺失的品种注明"无tick，跳过点差实测"，
对应格为空=不可判，全部按排除处理。

点差口径（R1 / 计划 T3）：默认 --spread-mode relative——格值 = 相对成本 ÷
相对振幅（(high−low)/close 的小时均值），各分量折成"占价格的比率"，
比率不随整体价格水平变化（近期 tick 的点差结构可外推到价格水平不同的
历史）；absolute 为旧口径（价格单位 ÷ 平均振幅）。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from mt5data.store import load_bars  # noqa: E402
from mt5quant.costs import (  # noqa: E402
    CommissionModel,
    SlippageModel,
    SpreadModel,
    cost_heatmap,
    decide_scope,
    relative_hourly_stats,
)
from mt5quant.costs.volatility import hourly_stats  # noqa: E402


def _parse_kv(pairs: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in filter(None, (p.strip() for p in pairs.split(","))):
        if "=" not in item:
            raise SystemExit(f"格式须为 SYM=数值，收到 {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = float(v)
    return out


def _load_ticks(data_root: str, symbol: str) -> pd.DataFrame | None:
    """读该品种全部 tick（按月分区布局，经 mt5data.ticks.load_ticks）。

    返回 None 表示尚无 tick 库；旧单文件布局会抛 TicksLayoutError 并提示
    先跑 scripts/migrate_ticks_layout.py。
    """
    from mt5data.ticks import load_ticks
    df = load_ticks(data_root, symbol)
    return df if df is not None and len(df) else None


def _contract_sizes(data_root: str, symbols, overrides: dict[str, float],
                    require_for: set[str]) -> dict[str, float]:
    """合约大小：CLI 覆盖 > {data_root}/specs/latest.json；都缺 → 报错（R12）。

    只对 require_for（实际进入热力图、需要把佣金折成价格单位的品种）强制；
    无 tick 的品种整列跳过，不因缺合约大小挡住整个审计。
    静默按 1.0 会让热力图差几个数量级——禁止。
    """
    sizes: dict[str, float] = {}
    snap_path = os.path.join(data_root, "specs", "latest.json")
    snap: dict = {}
    if os.path.exists(snap_path):
        try:
            with open(snap_path, encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError) as e:
            print(f"⚠ 读 {snap_path} 失败（{e}），无 CLI 覆盖的品种将报错")
    missing = []
    for s in symbols:
        if s in overrides:
            sizes[s] = overrides[s]
        elif isinstance(snap.get(s), dict) and snap[s].get("contract_size"):
            sizes[s] = float(snap[s]["contract_size"])
        elif s in require_for:
            missing.append(s)
    if missing:
        raise SystemExit(
            f"✗ 合约大小未知：{', '.join(missing)}（无 specs 快照且无 CLI 覆盖）。"
            f"先跑 scripts/snapshot_specs.py，或传 --contract-size SYM=数值"
        )
    return sizes


def _render_scope_md(decisions, rollover, notes, *, threshold, assumptions,
                     relative: bool) -> str:
    cost_line = (
        "- 来回成本（相对价）= rel_p90 + 2×rel_slip + 佣金/(合约大小×该小时均价)；"
        "格值 = 相对成本 ÷ 该小时平均相对振幅 ((high−low)/close)"
        if relative else
        "- 来回成本价 = spread_p90(点差全幅,每来回一次) + 2×slippage + 佣金/合约大小"
        "（价格单位）"
    )
    lines = [
        "# 成本与可交易性审计",
        "",
        f"- 生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        cost_line,
        f"- 判定：成本 ÷ 该小时{'平均相对振幅' if relative else '平均振幅'}"
        f" ≤ {threshold} → 纳入；无数据 → 排除",
    ]
    lines += [f"- {a}" for a in assumptions]
    lines += ["", "## 品种备注"]
    lines += [f"- {n}" for n in notes] if notes else ["- （无）"]
    lines += ["", "## 禁止时段（点差 p99 > 3×全体中位，UTC 整点）"]
    for sym, hours, why in rollover:
        hs = "、".join(f"{h:02d}:00" for h in hours) if hours else "无"
        lines.append(f"- {sym}：{hs}{why}")
    lines += ["", "## 纳入 / 排除清单"]
    grouped: dict[tuple[str, str], list] = {}
    for d in decisions:
        grouped.setdefault((d.symbol, d.timeframe), []).append(d)
    for (sym, tf), ds in grouped.items():
        inc = [d.hour for d in ds if d.include]
        lines.append(f"### {sym} {tf}")
        lines.append(f"- 纳入小时（{len(inc)}）：{'、'.join(str(h) for h in inc) or '无'}")
        for d in ds:
            if d.include:
                continue
            if math.isnan(d.ratio):
                reason = "无数据（点差或波幅缺失）"
            else:
                reason = f"成本占比 {d.ratio:.3f} > 阈值 {threshold:.2f}"
            lines.append(f"- 排除 {d.hour:02d}:00 —— {reason}")
    n_in = sum(1 for d in decisions if d.include)
    lines += ["", f"合计：纳入 {n_in} 格 / 排除 {len(decisions) - n_in} 格", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="成本与可交易性审计（点差/滑点/佣金 vs 波幅）")
    ap.add_argument("--symbols", default="XAUUSD,BTCUSD,EURUSD,USTEC")
    ap.add_argument("--timeframes", default="H1")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--out", default="data/reports/costs")
    ap.add_argument("--threshold", type=float, default=0.12,
                    help="来回成本/小时平均振幅 的纳入阈值（≤ 纳入）")
    ap.add_argument("--commission", default="",
                    help="SYM=每手来回佣金（计价货币），逗号分隔，如 XAUUSD=6.0")
    ap.add_argument("--contract-size", default="",
                    help="SYM=合约大小覆盖，逗号分隔；缺省读 {data-root}/specs/latest.json，缺则报错")
    ap.add_argument("--spread-fraction", type=float, default=0.1,
                    help="滑点=该比例×p90 点差")
    ap.add_argument("--fixed-slippage", type=float, default=0.0)
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative",
                    help="点差口径（R1）：relative=相对价 p90，格值=相对成本÷相对振幅，"
                         "不随价格水平漂移（默认）；absolute=旧口径（价格单位）")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
    notes: list[str] = []

    # 1) 点差实测（有 tick 的品种）
    spread = SpreadModel()
    have_any_tick = False
    have_tick: set[str] = set()
    for s in symbols:
        ticks = _load_ticks(args.data_root, s)
        if ticks is None or len(ticks) == 0:
            notes.append(f"{s}：无tick，跳过点差实测")
            continue
        try:
            spread = spread.merge(SpreadModel.from_ticks(s, ticks,
                                                         mode=args.spread_mode))
            have_any_tick = True
            have_tick.add(s)
            print(f"✓ {s}：tick {len(ticks):,} 条已测点差"
                  f"（{args.spread_mode}）")
        except ValueError as e:
            notes.append(f"{s}：点差实测失败（{e}），跳过")
    if not have_any_tick:
        print(f"未找到任何 tick 数据（{args.data_root}/ticks/）：无法测点差。"
              f"请先运行 scripts/download_ticks.py 回补后再重跑本脚本。")
        return 1

    # 2) 波幅统计（有 K 线的品种×周期；relative 另需相对振幅/均价列）
    vol: dict[tuple[str, str], pd.DataFrame] = {}
    for s in symbols:
        for tf in timeframes:
            bars = load_bars(args.data_root, s, tf)
            if bars is None or len(bars) == 0:
                notes.append(f"{s} {tf}：无K线，跳过")
                continue
            stats = hourly_stats(bars, tf)
            if args.spread_mode == "relative":
                stats = stats.join(relative_hourly_stats(bars))
            vol[(s, tf)] = stats
    if not vol:
        print(f"{args.data_root}/bars/ 下没有可用 K 线（{symbols} × {timeframes}）：无可审计序列。")
        return 1

    # 3) 收敛成矩形网格（cost_heatmap 要求 symbols×timeframes 全有 vol）
    grid_syms = [s for s in symbols if all((s, tf) in vol for tf in timeframes)]
    grid_tfs = [tf for tf in timeframes if all((s, tf) in vol for s in grid_syms)]

    sizes = _contract_sizes(args.data_root, symbols,
                            _parse_kv(args.contract_size), have_tick)
    commissions = _parse_kv(args.commission)
    commission = CommissionModel(default_per_lot=0.0,
                                 per_symbol=commissions or None)
    slip = SlippageModel(spread, spread_fraction=args.spread_fraction,
                         fixed_price=args.fixed_slippage)

    hm = cost_heatmap(spread, slip, commission, sizes, vol, grid_syms, grid_tfs)
    decisions = decide_scope(hm, threshold=args.threshold)

    rollover = []
    for s in grid_syms:
        try:
            rollover.append((s, spread.rollover_hours(s), ""))
        except ValueError:
            rollover.append((s, (), "（无tick，无法判定）"))

    os.makedirs(args.out, exist_ok=True)
    hm.to_csv(os.path.join(args.out, "heatmap.csv"))
    assumptions = [
        f"点差口径：{args.spread_mode}"
        f"（{'p90 of (ask−bid)/mid 按小时，随价格水平缩放' if args.spread_mode == 'relative' else '价格单位，旧口径'}）",
        f"佣金（计价货币/手，来回）：默认 0；按品种 {commissions if commissions else '无'}"
        + ("；relative 口径按该小时均价折成相对价" if args.spread_mode == "relative" else ""),
        f"合约大小来源：CLI 覆盖 > {args.data_root}/specs/latest.json > 报错",
        f"滑点模型：{args.spread_fraction}×p90 点差 + 固定 {args.fixed_slippage}（事件小时未配置）",
    ]
    md = _render_scope_md(decisions, rollover, notes,
                          threshold=args.threshold, assumptions=assumptions,
                          relative=args.spread_mode == "relative")
    with open(os.path.join(args.out, "scope.md"), "w", encoding="utf-8") as f:
        f.write(md)

    n_in = sum(1 for d in decisions if d.include)
    print(f"热力图 {hm.shape[0]} 行 × {hm.shape[1]} 小时 → {args.out}/heatmap.csv")
    print(f"纳入 {n_in} / 排除 {len(decisions) - n_in} 格（阈值 {args.threshold}）"
          f" → {args.out}/scope.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
