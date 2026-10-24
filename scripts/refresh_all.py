"""每日全序列增量刷新（计划任务 MT5Quant\\refresh_bars，00:10）。

按序列清单（schedule.series_inventory）逐序列调 downloader.refresh_series，
不写死品种列表——新回填的序列自动进入次日的刷新计划。
日志双写：控制台 + {data-dir}/logs/refresh_YYYY-MM-DD.log（UTC 日期，追加）。
任一序列失败 exit 1（计划任务的"上次结果"非 0 即报警）；摘要行报成功/失败数。

用法：
    .venv/Scripts/python.exe scripts/refresh_all.py
    .venv/Scripts/python.exe scripts/refresh_all.py --symbols XAUUSD,BTCUSD --config config.json
"""
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import MT5Gateway  # noqa: E402
from mt5data.downloader import refresh_series  # noqa: E402
from mt5data.schedule import series_inventory  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="按序列清单增量刷新全部 K 线")
    ap.add_argument("--symbols", default=None,
                    help="可选过滤：逗号分隔品种子集（缺省=清单全部序列）")
    ap.add_argument("--config", default=None,
                    help="可选 config.json（读取终端路径等网关配置）")
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()

    inventory, warnings = series_inventory(args.data_dir)
    for w in warnings:
        print(f"⚠ 坏 meta，跳过：{w}", flush=True)
    if args.symbols:
        keep = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        inventory = [r for r in inventory if r.symbol.upper() in keep]
    if not inventory:
        print("无匹配序列，未刷新任何数据")
        return 0

    log_dir = os.path.join(args.data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir, f"refresh_{datetime.now(timezone.utc):%Y-%m-%d}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(log_path, encoding="utf-8")],
    )
    log = logging.getLogger("refresh_all")
    log.info("开始刷新：%d 个序列%s", len(inventory),
             f"（过滤：{args.symbols.upper()}）" if args.symbols else "")

    ok = fail = 0
    gw = (MT5Gateway.from_config_file(args.config) if args.config else MT5Gateway())
    with gw:
        try:
            st = gw.account.state()
            meta_info = {"server": st.server, "login": st.login}
        except Exception:
            meta_info = None
        for ref in inventory:  # (symbol, timeframe) 升序，计划顺序稳定
            t0 = time.time()
            try:
                stats = refresh_series(args.data_dir, gw.market, ref.symbol,
                                       ref.timeframe, meta_info=meta_info)
                log.info("✓ %-8s %-4s rows=%9s 新增=%6s %s → %s (%.1fs)",
                         ref.symbol, ref.timeframe, f"{stats['rows']:,}",
                         f"{stats.get('new_bars', 0):,}",
                         stats["first_time"], stats["last_time"],
                         time.time() - t0)
                ok += 1
            except Exception as e:  # 单序列失败不中断其余序列
                log.error("✗ %-8s %-4s %s: %s", ref.symbol, ref.timeframe,
                          type(e).__name__, e)
                fail += 1
    log.info("摘要：成功 %d / 失败 %d", ok, fail)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
