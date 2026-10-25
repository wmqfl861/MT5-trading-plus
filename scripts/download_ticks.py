"""Tick 历史下载 CLI（退出码语义与 download_bars.py 一致：0 全成，1 有失败）。

存储为按月分区（T4/R3）：{data-dir}/ticks/{SYMBOL}/{YYYY}/{MM}.parquet +
meta.json（rows/first_msc/last_msc/months）。回补/刷新只读写涉及月份的
分区文件。库还是旧单文件布局（data.parquet）时先跑
scripts/migrate_ticks_layout.py --symbols <品种>，否则下载会拒绝并提示。

小窗口首跑回补（先试浅水）：
    .venv/Scripts/python.exe scripts/download_ticks.py --symbols XAUUSD --mode backfill --start 2026-08-01
深回补（后台长跑）：
    .venv/Scripts/python.exe scripts/download_ticks.py --symbols XAUUSD,BTCUSD --mode backfill --start 2020-01-01
增量刷新（定时任务用）：
    .venv/Scripts/python.exe scripts/download_ticks.py --symbols XAUUSD --mode refresh
可选：--end --chunk-days 7 --data-dir data --config config.json

T36（H2/锁箱铁律）——--ops-ack：tick 库整段落在锁箱期内、只用于运维成本
测量（换日时刻、点差/越幅、执行口径），读锁箱期 tick 必须显式确认：
- backfill 窗口 [start, end] 触及锁箱 cutoff（{data-dir}/lockbox.json 的
  global_cutoff）之后的数据而未带 --ops-ack → 拒绝（退出码 1，不联网）；
- 带 --ops-ack 时启动即打印 actor=ops（本次读取仅运维测量，不产生研究
  结论）。台账不涉及（脚本级确认）；refresh 为既有增量维护路径，不设此门槛
  （收紧与否由主会话统一决定，避免静默破坏已装计划任务）。
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5gw import MT5Gateway  # noqa: E402
from mt5data.downloader import server_now  # noqa: E402
from mt5data.lockbox import load_config as load_lockbox  # noqa: E402
from mt5data.ticks import backfill_ticks, read_ticks_watermark, refresh_ticks  # noqa: E402


def _parse_naive(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        raise SystemExit(f"--start/--end 只接受贴 UTC 标签的 naive 时刻，收到 {text}")
    return dt


def _precheck_clock(gw):
    """R5：开跑前用最新 tick 实测服务器时钟偏移并与配置比对；行情冻结则跳过并打印。"""
    from datetime import datetime, timezone

    from mt5data.clock import check_server_offset

    try:
        q = gw.market.quote("XAUUSD")
    except Exception as e:  # 行情取不到不阻塞刷新本身，但如实记录
        print(f"⚠ 时钟预检取行情失败（{e}），跳过")
        return
    lag = (datetime.now(timezone.utc).replace(tzinfo=None) - q.time).total_seconds()
    if lag > 120:
        print(f"⚠ 行情静默（末笔 tick 距今 {lag / 3600:.1f} 小时，休市？），时钟预检跳过")
        return
    check_server_offset(-lag, gw.config.server_utc_offset_hours)
    print(f"✓ 时钟预检：实测偏移 {-lag:+.0f}s 与配置 "
          f"{gw.config.server_utc_offset_hours:g}h 一致")


def _ops_ack_gate(data_dir: str, start: datetime, end: datetime,
                  acked: bool) -> str | None:
    """T36：backfill 窗口触及锁箱 cutoff 之后的数据而未 --ops-ack → 拒绝。

    返回拒绝信息（None=放行）。锁箱段 = time >= global_cutoff；窗口与之
    相交 ⟺ end >= cutoff（窗口连续、end 为上界）。
    """
    try:
        lb = load_lockbox(data_dir)
    except Exception as e:  # 锁箱配置读不出来：如实报错，不静默放行
        return f"读取锁箱配置失败（{type(e).__name__}: {e}），拒绝本次回补"
    cutoff = getattr(lb, "global_cutoff", None) if lb is not None else None
    if cutoff is None or end < cutoff:
        return None
    if acked:
        return None
    return (f"backfill 窗口 [{start} → {end}] 触及锁箱 cutoff {cutoff} 之后的"
            "数据：读锁箱期 tick 仅限运维成本测量（换日时刻、点差/越幅、执行"
            "口径），必须显式加 --ops-ack（本次读取记 actor=ops，不产生研究"
            "结论）后重跑")


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
    ap.add_argument("--ops-ack", action="store_true",
                    help="T36：确认本次读取锁箱期 tick 仅用于运维成本测量"
                         "（actor=ops，不产生研究结论）；backfill 窗口触及锁箱"
                         " cutoff 之后的数据时必须显式给出")
    args = ap.parse_args()

    if args.mode == "backfill" and not args.start:
        ap.error("--mode backfill 需要 --start（如 --start 2026-08-01）")

    if args.ops_ack:
        print("✓ --ops-ack：本次读取锁箱期 tick 仅用于运维成本测量"
              "（actor=ops），不产生研究结论", flush=True)

    if args.mode == "backfill":
        start = _parse_naive(args.start)
        end = (_parse_naive(args.end) if args.end
               else datetime.now(timezone.utc).replace(tzinfo=None))
        refuse = _ops_ack_gate(args.data_dir, start, end, args.ops_ack)
        if refuse is not None:
            print(f"✗ {refuse}", flush=True)
            return 1

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    failed = []

    gw = (MT5Gateway.from_config_file(args.config) if args.config else MT5Gateway())
    with gw:
        _precheck_clock(gw)
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
