"""K 线下载 CLI。

首次全量回填：
    .venv/Scripts/python.exe scripts/download_bars.py --symbols XAUUSD,BTCUSD
增量刷新（定时任务用）：
    .venv/Scripts/python.exe scripts/download_bars.py --symbols XAUUSD --refresh
可选：--data-dir data --timeframes M1,H1,D1 --chunk 50000
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import MT5Gateway  # noqa: E402
from mt5data.downloader import ALL_TIMEFRAMES, backfill_series, refresh_series  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="下载/刷新 MT5 K线到 Parquet")
    ap.add_argument("--symbols", required=True, help="逗号分隔，如 XAUUSD,XAUUSDm")
    ap.add_argument("--config", default=None,
                    help="可选 config.json（读取终端路径等网关配置）")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--timeframes", default=",".join(ALL_TIMEFRAMES))
    ap.add_argument("--refresh", action="store_true", help="增量刷新已有序列")
    ap.add_argument("--chunk", type=int, default=500_000)
    ap.add_argument("--force", action="store_true",
                    help="新数据比现存短时仍取并集写入（不丢行）")
    ap.add_argument("--replace", action="store_true",
                    help="彻底用新数据替换旧文件（自动备份到 data.parquet.bak-时间戳）")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]  # 保留大小写
    tfs = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
    failed = []

    gw = (MT5Gateway.from_config_file(args.config) if args.config else MT5Gateway())
    with gw:
        try:
            st = gw.account.state()
            meta_info = {"server": st.server, "login": st.login}
        except Exception:
            meta_info = None
        for sym in symbols:
            for tf in tfs:
                t0 = time.time()
                try:
                    if args.refresh:
                        stats = refresh_series(args.data_dir, gw.market, sym, tf,
                                               meta_info=meta_info)
                        extra = f" 新增={stats.get('new_bars', 0):>6,}"
                    else:
                        stats = backfill_series(args.data_dir, gw.market, sym, tf,
                                                chunk=args.chunk, force=args.force,
                                                replace=args.replace,
                                                meta_info=meta_info)
                        extra = f" 块数={stats['chunks_fetched']}"
                except Exception as e:  # 单序列失败不中断其余下载
                    print(f"✗ {sym:8s} {tf:4s} {type(e).__name__}: {e}", flush=True)
                    failed.append((sym, tf))
                    continue
                dt = time.time() - t0
                if stats["rows"]:
                    print(f"✓ {sym:8s} {tf:4s} rows={stats['rows']:>9,}"
                          f" {stats['first_time']} → {stats['last_time']}"
                          f"{extra} ({dt:.1f}s)", flush=True)
                else:
                    print(f"· {sym:8s} {tf:4s} 无数据 ({dt:.1f}s)", flush=True)

    if failed:
        print(f"\n失败序列：{failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
