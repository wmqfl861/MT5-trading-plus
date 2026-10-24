"""导出锁箱段：把各序列 time >= cutoff 的数据物理复制到独立目录。

研究侧靠 lockbox.research_bars 的加载器约束（读不到锁箱段）；本脚本把
锁箱段另存一份物理副本（parquet+meta，布局与主库一致）供 G5/实盘侧读取
——实盘需要最新数据，不应也不必绕开研究侧约束去读主库。
ACL 级隔离（研究账号根本无权打开锁箱目录）属 P6，本轮不做。

用法：
    .venv/Scripts/python.exe scripts/export_lockbox.py
    .venv/Scripts/python.exe scripts/export_lockbox.py --dest C:\\mt5-lockbox
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from mt5data.lockbox import cutoff_for, load_config  # noqa: E402
from mt5data.schedule import series_inventory  # noqa: E402
from mt5data.store import load_bars, save_bars  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="导出各序列锁箱段（time >= cutoff）")
    ap.add_argument("--dest", default=r"C:\mt5-lockbox",
                    help="导出根目录（默认 C:\\mt5-lockbox）")
    ap.add_argument("--data-root", default="data")
    args = ap.parse_args()

    lockbox = load_config(args.data_root)
    if not lockbox:
        print("✗ 未找到锁箱配置（data/lockbox.json）：先按 compute_cutoff 生成再导出")
        return 1

    inventory, warnings = series_inventory(args.data_root)
    for w in warnings:
        print(f"⚠ 坏 meta，跳过：{w}", flush=True)

    exported = skipped = failed = 0
    for ref in inventory:
        cutoff = cutoff_for(lockbox, ref.symbol, ref.timeframe)
        if cutoff is None:
            print(f"· {ref.symbol:8s} {ref.timeframe:4s} 无 cutoff，跳过")
            skipped += 1
            continue
        try:
            df = load_bars(args.data_root, ref.symbol, ref.timeframe)
            if df is None or len(df) == 0:
                print(f"· {ref.symbol:8s} {ref.timeframe:4s} 无数据文件，跳过")
                skipped += 1
                continue
            seg = df[df["time"] >= pd.Timestamp(cutoff)].reset_index(drop=True)
            save_bars(args.dest, ref.symbol, ref.timeframe, seg,
                      server=ref.watermark.get("server"),
                      login=ref.watermark.get("login"))
            print(f"✓ {ref.symbol:8s} {ref.timeframe:4s} 锁箱段 {len(seg):,} 根"
                  f"（{cutoff.isoformat()} 起）→ {args.dest}", flush=True)
            exported += 1
        except Exception as e:  # 单序列失败不中断其余序列
            print(f"✗ {ref.symbol:8s} {ref.timeframe:4s} "
                  f"{type(e).__name__}: {e}", flush=True)
            failed += 1

    print(f"摘要：导出 {exported} / 跳过 {skipped} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
