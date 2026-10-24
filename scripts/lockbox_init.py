"""初始化/收紧全局锁箱：写 {data-root}/lockbox.json v2（全局单一 cutoff）。

用法：
    .venv/Scripts/python.exe scripts/lockbox_init.py --cutoff 2025-10-24
    .venv/Scripts/python.exe scripts/lockbox_init.py --auto

- --cutoff：直接指定日期（YYYY-MM-DD，取当日 00:00，naive-UTC）；
- --auto：对库里每个序列按 lockbox.compute_cutoff 计算（历史 >5 年锁
  12 个月、否则 6 个月），取最早者（最保守）作为全局 cutoff；
- 写入走 lockbox.write_global_cutoff——已存在 v2（或 v1）时只允许更早，
  更晚直接报错退出；与磁盘现值一致则跳过写入；
- 写完打印每个序列的开放/锁定行数。

tick 库豁免说明：tick 只用于成本统计（点差/滑点按小时分布），不受锁箱
约束（理由见 mt5data.lockbox 模块 docstring）；本脚本只处理 K 线序列。
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.lockbox import (  # noqa: E402
    LockboxError,
    compute_global_cutoff,
    load_config,
    lockbox_status,
    write_global_cutoff,
)
from mt5data.schedule import series_inventory  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="初始化/收紧全局锁箱（lockbox.json v2，只许更早不许更晚）")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--cutoff", metavar="YYYY-MM-DD",
                       help="直接指定全局 cutoff（当日 00:00，naive-UTC）")
    group.add_argument("--auto", action="store_true",
                       help="按各序列 compute_cutoff 取最早者（最保守）")
    ap.add_argument("--data-root", default="data", help="数据根目录（默认 data）")
    args = ap.parse_args(argv)

    inventory, warnings = series_inventory(args.data_root)
    for w in warnings:
        print(f"⚠ 坏 meta，跳过：{w}", flush=True)

    if args.auto:
        if not inventory:
            print("✗ 库里没有可用序列，无法 --auto 计算全局 cutoff", flush=True)
            return 1
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = compute_global_cutoff(inventory, now)
        note = "lockbox_init.py --auto"
    else:
        try:
            cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d")
        except ValueError:
            print(f"✗ --cutoff 须为 YYYY-MM-DD 格式，收到 {args.cutoff!r}", flush=True)
            return 1
        note = f"lockbox_init.py --cutoff {args.cutoff}"

    existing = load_config(args.data_root)
    if existing is not None and existing.global_cutoff == cutoff:
        print(f"· 全局 cutoff 已是 {cutoff.isoformat()}，跳过写入", flush=True)
    else:
        try:
            path = write_global_cutoff(args.data_root, cutoff, note=note)
            print(f"✓ 全局 cutoff {cutoff.isoformat()} → {path}", flush=True)
        except LockboxError as e:
            print(f"✗ {e}", flush=True)
            return 1

    for ref in inventory:
        st = lockbox_status(args.data_root, ref.symbol, ref.timeframe)
        cut = st["cutoff"].isoformat() if st["cutoff"] is not None else "无"
        print(f"· {st['symbol']:8s} {st['timeframe']:4s} cutoff={cut} "
              f"开放 {st['open_rows']:,} 行 / 锁定 {st['locked_rows']:,} 行",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
