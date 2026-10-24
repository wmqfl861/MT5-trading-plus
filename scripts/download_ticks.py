"""Tick 历史下载 CLI（退出码语义与 download_bars.py 一致：0 全成，1 有失败）。

小窗口首跑回补（先试浅水）：
    .venv/Scripts/python.exe scripts/download_ticks.py --symbols XAUUSD --mode backfill --start 2026-08-01
深回补（后台长跑）：
    .venv/Scripts/python.exe scripts/download_ticks.py --symbols XAUUSD,BTCUSD --mode backfill --start 2020-01-01
增量刷新（定时任务用）：
    .venv/Scripts/python.exe scripts/download_ticks.py --symbols XAUUSD --mode refresh
可选：--end --chunk-days 7 --data-dir data --config gateway.json
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import MT5Gateway  # noqa: E402
from mt5data.downloader import server_now  # noqa: E402
from mt5data.ticks import backfill_ticks, read_ticks_watermark, refresh_ticks  # noqa: E402


def _parse_naive(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        raise SystemExit(f"--start/--end 只接受贴 UTC 标签的 naive 时刻，收到 {text}")
    return dt


def main() -> int:
    ap = argparse.ArgumentParser(description="下载/刷新 MT5 tick 到 Parquet")
    ap.add_argument("--symbols", required=True, help="逗号分隔，如 XAUUSD,BTCUSD（保留大小写）")
    ap.add_argument("--config", default=None,
                    help="可选 config.json（读取终端路径等网关配置）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--mode", choices=["backfill", "refresh"], default="backfill")
    ap.add_argument("--start", default=None,
                    help="backfill 起点：YYYY-MM-DD 或 ISO 时刻（naive UTC）")
    ap.add_argument("--end", default=None, help="backfill 终点（缺省=现在）")
    ap.add_argument("--chunk-days", type=float, default=7.0)
    args = ap.parse_args()

    if args.mode == "backfill" and not args.start:
        ap.error("--mode backfill 需要 --start（如 --start 2026-08-01）")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    failed = []

    gw = (MT5Gateway.from_config_file(args.config) if args.config else MT5Gateway())
    with gw:
        try:
            st = gw.account.state()
            meta_info = {"server": st.server, "login": st.login}
        except Exception:
            meta_info = None
        for sym in symbols:
            t0 = time.time()
            try:
                if args.mode == "refresh":
                    # ticks 接口收 (conn, mt5_module)：复用网关的同一连接与包模块
                    added = refresh_ticks(gw._conn, gw._mt5, args.data_dir, sym,
                                          now_server=server_now(gw.market, sym),
                                          meta_info=meta_info)
                else:
                    added = backfill_ticks(gw._conn, gw._mt5, args.data_dir, sym,
                                           start=_parse_naive(args.start),
                                           end=_parse_naive(args.end) if args.end else None,
                                           chunk_days=args.chunk_days,
                                           meta_info=meta_info)
            except Exception as e:  # 单品种失败不中断其余下载
                print(f"✗ {sym:8s} [{args.mode}] {type(e).__name__}: {e}", flush=True)
                failed.append(sym)
                continue
            wm = read_ticks_watermark(args.data_dir, sym)
            dt = time.time() - t0
            if wm and wm["rows"]:
                print(f"✓ {sym:8s} [{args.mode}] 新增={added:>10,} 总行数={wm['rows']:>12,}"
                      f" msc {wm['first_msc']} → {wm['last_msc']} ({dt:.1f}s)", flush=True)
            else:
                print(f"· {sym:8s} [{args.mode}] 无数据 ({dt:.1f}s)", flush=True)

    if failed:
        print(f"\n失败品种：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
