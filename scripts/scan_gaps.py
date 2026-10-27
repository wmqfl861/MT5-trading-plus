"""全库缺口普查（T79 / 第十二轮 G1）。

对主库与研究库全部 品种×周期 跑 mt5data.quality.quality_report，汇总
缺口分类计数，逐条列出 long_gap / unexpected（起止、时长、缺根数、
是否已登记 known_gaps）。只读 K 线、只写报告文件——普查属运维测量，
不产生任何研究结论；输出仅缺口清单。

登记模式（--register-known）：终端补不了的缺口写进序列 meta.json 的
known_gaps（mt5data.quality.write_known_gaps），研究工具读取后会在报告
里注明相关窗口样本不完整。离线运维操作，不与下载/导出并发运行。

用法（Git Bash，仓库根）：
    .venv/Scripts/python.exe scripts/scan_gaps.py --roots data,data_research
    .venv/Scripts/python.exe scripts/scan_gaps.py --register-known --root data \
        --symbol EURUSD --timeframes H1,M1 \
        --start 2021-05-17T14:00 --end 2021-06-14T11:00 \
        --reason "终端无该段历史"
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.quality import (  # noqa: E402
    quality_report, read_known_gaps, write_known_gaps,
)
from mt5data.store import load_bars  # noqa: E402

_KINDS = ("weekend", "long_gap", "daily_break", "holiday", "micro_gap",
          "unexpected")
_NOTABLE = ("long_gap", "unexpected", "holiday")   # 逐条列出的类别


def _series_inventory(root: str) -> list[tuple[str, str]]:
    """枚举 {root}/bars/{品种}/{周期}（品种保留目录名大小写）。"""
    bars = Path(root) / "bars"
    if not bars.is_dir():
        return []
    out = []
    for sym_dir in sorted(bars.iterdir()):
        if not sym_dir.is_dir():
            continue
        for tf_dir in sorted(sym_dir.iterdir()):
            if tf_dir.is_dir() and (tf_dir / "data.parquet").exists():
                out.append((sym_dir.name, tf_dir.name.upper()))
    return out


def _fmt_hours(start: datetime, end: datetime) -> str:
    return f"{(end - start).total_seconds() / 3600:.1f}h"


def scan_root(root: str, out_dir: Path) -> list[dict]:
    """普查一个数据根：返回逐序列结果（写 JSON 与汇总 markdown）。"""
    name = Path(root).name or "root"
    rows: list[dict] = []
    known_total = 0
    for sym, tf in _series_inventory(root):
        df = load_bars(root, sym, tf)
        if df is None:
            continue
        rep = quality_report(df, tf, symbol=sym)
        known = read_known_gaps(root, sym, tf)
        known_total += len(known)
        counts = {k: 0 for k in _KINDS}
        notable: list[dict] = []
        for g in rep.gaps:
            counts[g.kind] = counts.get(g.kind, 0) + 1
            if g.kind in _NOTABLE:
                registered = any(k0 < g.end and k1 > g.start for k0, k1 in known)
                notable.append({
                    "kind": g.kind, "start": g.start.isoformat(),
                    "end": g.end.isoformat(),
                    "hours": round((g.end - g.start).total_seconds() / 3600, 2),
                    "bars_missing": g.bars_missing,
                    "known_gap_registered": registered,
                })
        rows.append({
            "symbol": sym, "timeframe": tf, "rows": rep.rows,
            "first_time": rep.first_time.isoformat()
            if rep.first_time else None,
            "last_time": rep.last_time.isoformat() if rep.last_time else None,
            "coverage_pct": round(rep.coverage_pct, 2),
            "gap_counts": counts, "notable_gaps": notable,
            "known_gaps_registered": len(known),
            "duplicates": rep.duplicates, "disorder": rep.disorder,
        })
        flag = ("  ⚠ " + ",".join(
            f"{k}={counts[k]}" for k in _NOTABLE if counts[k]) if any(
            counts[k] for k in _NOTABLE) else "")
        print(f"· {sym:8s} {tf:4s} rows={rep.rows:>9,} "
              f"覆盖率={rep.coverage_pct:6.2f}% "
              f"weekend={counts['weekend']:>4} long_gap={counts['long_gap']} "
              f"holiday={counts['holiday']:>3} unexpected={counts['unexpected']}"
              f"{flag}", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"gaps-{name}.json", "w", encoding="utf-8") as f:
        json.dump({"root": root, "series": rows,
                   "scanned_at_utc": datetime.now(timezone.utc).isoformat()},
                  f, ensure_ascii=False, indent=2)

    lines = [f"# 缺口普查：{root}", "",
             "| 品种 | 周期 | 行数 | 覆盖率% | weekend | long_gap | "
             "daily_break | holiday | micro_gap | unexpected | 已登记known |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        c = r["gap_counts"]
        lines.append(
            f"| {r['symbol']} | {r['timeframe']} | {r['rows']:,} "
            f"| {r['coverage_pct']:.2f} | {c['weekend']} | {c['long_gap']} "
            f"| {c['daily_break']} | {c['holiday']} | {c['micro_gap']} "
            f"| {c['unexpected']} | {r['known_gaps_registered']} |")
    (out_dir / f"summary-{name}.md").write_text("\n".join(lines) + "\n",
                                                encoding="utf-8")

    tot = {k: sum(r["gap_counts"].get(k, 0) for r in rows) for k in _KINDS}
    print(f"— {root}：{len(rows)} 个序列；分类合计 " +
          "、".join(f"{k}={tot[k]}" for k in _KINDS) +
          f"；known_gaps 已登记 {known_total} 条", flush=True)
    return rows


def register_known(args) -> int:
    tfs = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
    if not tfs:
        print("--timeframes 不能为空", file=sys.stderr)
        return 2
    entry = {"start": args.start, "end": args.end, "reason": args.reason}
    for tf in tfs:
        write_known_gaps(args.root, args.symbol, tf, [entry])
        gaps = read_known_gaps(args.root, args.symbol, tf)
        print(f"✓ {args.symbol} {tf} known_gaps={len(gaps)} "
              f"({args.start} → {args.end})", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全库缺口普查 / known_gaps 登记")
    ap.add_argument("--roots", default="data,data_research",
                    help="逗号分隔的数据根（普查模式）")
    ap.add_argument("--out", default="data/reports/gaps",
                    help="普查输出目录（JSON + 汇总 markdown）")
    ap.add_argument("--register-known", action="store_true",
                    help="登记模式：把一条已知缺口写进 meta.json")
    ap.add_argument("--root", help="登记模式：数据根")
    ap.add_argument("--symbol", help="登记模式：品种")
    ap.add_argument("--timeframes", help="登记模式：逗号分隔周期")
    ap.add_argument("--start", help="登记模式：缺口起点（首根缺失 bar，ISO）")
    ap.add_argument("--end", help="登记模式：缺口终点（缺口后首根现存 bar，ISO）")
    ap.add_argument("--reason", default="", help="登记模式：缺口原因")
    args = ap.parse_args(argv)

    if args.register_known:
        missing = [k for k, v in (("--root", args.root), ("--symbol", args.symbol),
                                  ("--timeframes", args.timeframes),
                                  ("--start", args.start), ("--end", args.end))
                   if not v]
        if missing:
            ap.error(f"登记模式缺参数：{missing}")
        return register_known(args)

    roots = [r.strip() for r in args.roots.split(",") if r.strip()]
    if not roots:
        ap.error("--roots 不能为空")
    all_rows = 0
    for root in roots:
        rows = scan_root(root, Path(args.out))
        all_rows += len(rows)
    print(f"普查完成：{len(roots)} 个库、{all_rows} 个序列；输出目录 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
