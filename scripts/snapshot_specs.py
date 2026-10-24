"""品种参数快照 CLI（P0）。

抓取各品种合约参数写入 data/specs/（latest + history），并与上一份
latest 比对打印中文变更报告——券商悄悄改合约大小/库存费时第一时间可见。

用法：
    .venv/Scripts/python.exe scripts/snapshot_specs.py
    .venv/Scripts/python.exe scripts/snapshot_specs.py --symbols XAUUSD,BTCUSD,EURUSD,USTEC --config config.json
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway, load  # noqa: E402
from mt5gw.snapshot import (  # noqa: E402
    SpecChange, diff_snapshots, load_snapshot, save_snapshot, take_snapshot,
)


def render_changes(changes) -> str:
    """中文变更报告；无变更时固定输出"无变化"。"""
    if not changes:
        return "无变化"
    return "\n".join(
        f"- {c.symbol}.{c.field}: {c.old!r} → {c.new!r}" for c in changes
    )


def _load_config(path: str) -> GatewayConfig:
    if os.path.exists(path):
        return load(path)
    print(f"· 未找到 {path}，使用默认网关配置（本脚本只读行情不下单）")
    return GatewayConfig()


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取品种合约参数快照并与上一份比对")
    ap.add_argument("--symbols", default="XAUUSD,BTCUSD,EURUSD,USTEC",
                    help="逗号分隔品种列表（保留大小写）")
    ap.add_argument("--config", default="config.json",
                    help="网关配置 JSON（不存在则用默认配置）")
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cfg = _load_config(args.config)
    with MT5Gateway(cfg) as gw:
        snap = take_snapshot(gw.symbols, symbols)  # 取不到的品种如实抛错

    latest = os.path.join(args.data_dir, "specs", "latest.json")
    old = load_snapshot(latest) if os.path.exists(latest) else None
    save_snapshot(args.data_dir, snap)

    if old is None:
        print(f"首份快照：已记录 {len(snap)} 个品种 → {latest}")
        return 0
    print(f"比对 {len(old)} → {len(snap)} 个品种：")
    print(render_changes(diff_snapshots(old, snap)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
