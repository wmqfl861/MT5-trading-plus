"""tick 库旧单文件布局 → 按月分区迁移（T4/R3，一次性；本机实跑由主会话执行）。

把 {data-dir}/ticks/{SYMBOL}/data.parquet 按服务器时间月份拆成
{data-dir}/ticks/{SYMBOL}/{YYYY}/{MM}.parquet 并重算 meta.json（含 months）；
守恒校验（总行数、首末 time_msc 与旧文件逐项相等）通过后才把旧文件改名
data.parquet.migrated-<UTC戳>——原数据保留不删，校验失败则旧文件不动、
可直接重跑（半成品月分区会被覆盖重写）。

用法：
    .venv/Scripts/python.exe scripts/migrate_ticks_layout.py --symbols XAUUSD,BTCUSD
可选：--data-dir data（缺省）
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.ticks import migrate_old_layout, ticks_dir  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="tick 库旧单文件布局 → 按月分区迁移")
    ap.add_argument("--symbols", required=True,
                    help="逗号分隔，如 XAUUSD,BTCUSD（保留大小写）")
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()

    failed = []
    for sym in (s.strip() for s in args.symbols.split(",") if s.strip()):
        try:
            summary = migrate_old_layout(args.data_dir, sym)
        except Exception as e:  # 单品种失败不中断其余迁移
            print(f"✗ {sym:8s} 迁移失败 {type(e).__name__}: {e}", flush=True)
            failed.append(sym)
            continue
        if summary is None:
            print(f"· {sym:8s} 无旧单文件布局"
                  f"（{ticks_dir(args.data_dir, sym)} 下无 data.parquet），跳过",
                  flush=True)
            continue
        print(f"✓ {sym:8s} 迁移完成：{summary['rows']:,} 行、"
              f"{len(summary['months'])} 个月份分区（{', '.join(summary['months'])}），"
              f"旧文件改名 {summary['backup']}", flush=True)

    if failed:
        print(f"\n失败品种：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
