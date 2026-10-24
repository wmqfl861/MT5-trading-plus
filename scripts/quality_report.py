"""K 线质检报告 CLI。

逐序列写 JSON（{out}/{SYMBOL}_{TF}.json）+ 汇总 summary.md
（每序列一行：覆盖率、unexpected 缺口数、spike 数）；缺数据文件的
序列跳过并在汇总中注明。只读数据文件，不写回任何 K 线数据。

用法：
    .venv/Scripts/python.exe scripts/quality_report.py --symbols XAUUSD,BTCUSD \
        --timeframes M1,H1 --data-root data --out data/reports/quality
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.quality import quality_report  # noqa: E402
from mt5data.store import load_bars  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="K 线质检：逐序列 JSON + 汇总 summary.md")
    ap.add_argument("--symbols", required=True, help="逗号分隔，如 XAUUSD,BTCUSD")
    ap.add_argument("--timeframes", required=True, help="逗号分隔，如 M1,H1")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--out", default="data/reports/quality")
    args = ap.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]  # 保留大小写
    tfs = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
    if not symbols or not tfs:
        ap.error("--symbols 与 --timeframes 均不能为空")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = ["# K 线质检汇总", "",
             "| 品种 | 周期 | 覆盖率% | unexpected 缺口 | spike | 点差异常 | 说明 |",
             "|---|---|---|---|---|---|---|"]
    for sym in symbols:
        for tf in tfs:
            df = load_bars(args.data_root, sym, tf)
            if df is None:
                lines.append(f"| {sym} | {tf} | - | - | - | - | 缺数据文件，跳过 |")
                print(f"· {sym:8s} {tf:4s} 缺数据文件，跳过", flush=True)
                continue
            rep = quality_report(df, tf, symbol=sym)
            path = out_dir / f"{sym}_{tf}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rep.to_dict(), f, ensure_ascii=False, indent=2)
            n_unexpected = sum(1 for g in rep.gaps if g.kind == "unexpected")
            lines.append(
                f"| {sym} | {tf} | {rep.coverage_pct:.2f} | {n_unexpected} "
                f"| {len(rep.spikes)} | {len(rep.spread_anomalies)} | |")
            print(f"✓ {sym:8s} {tf:4s} rows={rep.rows:,} "
                  f"覆盖率={rep.coverage_pct:.2f}% unexpected={n_unexpected} "
                  f"spikes={len(rep.spikes)} 点差异常={len(rep.spread_anomalies)}",
                  flush=True)

    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"汇总：{out_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
