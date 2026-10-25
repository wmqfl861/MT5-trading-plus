"""隔夜费计费时刻观测（T35 / H5 / 决策 D28）：只读轮询校准持仓的 swap 字段。

背景：10-26 至 10-30 这周（欧洲已切冬令时、美国 11-01 才切）是唯一能直接测出
Exness 隔夜费计费锚点跟谁的窗口——
- 变化发生在约 21:00 UTC → 计费锚点为纽约 17:00（与 SwapRule 缺省一致）；
- 变化发生在约 22:00 UTC → 锚点随欧洲冬令时前移，须改 SwapRule/sessions.toml
  并在蓝图追加决策（见 docs/research/rollover-dst-2026-10-25.md 第 4 节）。

用法（只读，绝不下单）：
    ./.venv/Scripts/python.exe scripts/watch_swap.py --once             # 单次轮询
    ./.venv/Scripts/python.exe scripts/watch_swap.py --loop-minutes 180 # 本轮询 3 小时

计划任务（scripts/setup_scheduled_tasks.ps1 的 MT5Quant\\watch_swap）用 --once
每 60 秒起一个进程：状态落 swap-watch-<UTC日期>.state.json，跨进程可比对；
swap 变化即向 data/reports/costs/swap-watch-<UTC日期>.csv 追加一行
（时刻、品种、票号、swap 前后值）。首次见到持仓只建基线不记行。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import GatewayConfig, MT5Gateway  # noqa: E402

MAGIC_DEFAULT = 20261025          # 校准持仓专用 magic（scripts/calibration_positions.py）
DEFAULT_OUT_DIR = "data/reports/costs"
CSV_HEADER = ["ts_utc", "symbol", "ticket", "magic", "swap_before", "swap_after"]
_ONE_MINUTE = timedelta(minutes=1)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(t: datetime) -> datetime:
    """aware → naive UTC；naive 视作已是 UTC。"""
    if t.tzinfo is None:
        return t
    return t.astimezone(timezone.utc).replace(tzinfo=None)


class SwapWatcher:
    """只读轮询：account.positions() → 与上一快照差分 → 变化记 CSV。

    account 只需提供 positions() -> list[mt5gw.models.Position]（真实网关的
    AccountService 与测试 FakeBroker 都满足）。
    """

    def __init__(self, account, magic: int = MAGIC_DEFAULT,
                 out_dir: str = DEFAULT_OUT_DIR):
        self.account = account
        self.magic = int(magic)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # -- 路径 --

    def _paths(self, now: datetime):
        day = _naive_utc(now).strftime("%Y-%m-%d")
        return (self.out_dir / f"swap-watch-{day}.csv",
                self.out_dir / f"swap-watch-{day}.state.json")

    # -- 快照 --

    def _load_state(self, state_path: Path) -> dict:
        if not state_path.exists():
            return {}
        try:
            with open(state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}   # 状态损坏 → 重建基线（宁可少记一行，不误报）

    def _save_state(self, state_path: Path, state: dict) -> None:
        tmp = state_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, state_path)

    # -- 轮询 --

    def poll(self, now: datetime | None = None) -> list[dict]:
        """读一次持仓；swap 与上一快照不同 → 记一行并返回该行。

        首次见到的持仓只建基线（不记行）；消失的持仓从快照移除（平仓由
        calibration_positions --close 负责，不在此记录）。
        """
        t = _naive_utc(now) if now is not None else _now_utc()
        csv_path, state_path = self._paths(t)
        positions = [p for p in self.account.positions()
                     if p.magic == self.magic]
        state = self._load_state(state_path)
        rows: list[dict] = []
        for p in positions:
            key = p.symbol
            prev = state.get(key, {}).get(str(p.ticket))
            if prev is not None and float(prev) != float(p.swap):
                rows.append({
                    "ts_utc": t.isoformat(),
                    "symbol": p.symbol,
                    "ticket": str(p.ticket),
                    "magic": str(self.magic),
                    "swap_before": repr(float(prev)),
                    "swap_after": repr(float(p.swap)),
                })
        if rows:
            new_file = not csv_path.exists()
            with open(csv_path, "a", encoding="utf-8", newline="") as f:
                wtr = csv.DictWriter(f, fieldnames=CSV_HEADER)
                if new_file:
                    wtr.writeheader()
                wtr.writerows(rows)
        state = {p.symbol: {str(p.ticket): float(p.swap)} for p in positions}
        self._save_state(state_path, state)
        return rows


def run_loop(watcher: SwapWatcher, minutes: float, interval: float = 60.0) -> int:
    """循环轮询 minutes 分钟（默认每 60 秒一次）；返回记录的行数。"""
    deadline = time.monotonic() + minutes * 60.0
    n = 0
    while True:
        rows = watcher.poll()
        for r in rows:
            print(f"· {r['ts_utc']} {r['symbol']} swap "
                  f"{r['swap_before']} → {r['swap_after']}")
        n += len(rows)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return n
        time.sleep(min(interval, remaining))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="只读轮询校准持仓 swap 字段，变化记 CSV（T35 计费时刻测量）")
    ap.add_argument("--magic", type=int, default=MAGIC_DEFAULT)
    ap.add_argument("--once", action="store_true",
                    help="单次轮询后退出（计划任务的每分钟模式）")
    ap.add_argument("--loop-minutes", type=float, default=None,
                    help="连续轮询 N 分钟（缺省 60 秒间隔）")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="循环模式的轮询间隔秒数（默认 60）")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    if args.once and args.loop_minutes is not None:
        ap.error("--once 与 --loop-minutes 互斥")

    cfg = GatewayConfig()   # 默认 allow_live=False；本脚本只读不下单
    with MT5Gateway(cfg) as gw:
        watcher = SwapWatcher(gw.account, magic=args.magic, out_dir=args.out_dir)
        if args.loop_minutes is not None:
            n = run_loop(watcher, args.loop_minutes, interval=args.interval)
        else:
            rows = watcher.poll()
            for r in rows:
                print(f"· {r['ts_utc']} {r['symbol']} swap "
                      f"{r['swap_before']} → {r['swap_after']}")
            n = len(rows)
    print(f"共记录 {n} 行 swap 变化 → {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
