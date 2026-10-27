"""外部数据下载与入库（T67/T81/D36）：data/ext/<来源>/<品种>/<周期>/。

用法（Git Bash；--help 看全量参数）：
    # HistData 样本段（M1 bid，实测规则=美东偏移+欧盟 DST 日历；每年一个 zip）
    .venv/Scripts/python.exe scripts/fetch_ext_data.py histdata \
        --pairs EURUSD,XAUUSD --years 2020,2021
    # BTC 现货日线（CryptoDataDownload/Bitstamp，全量一个 csv）【批准】
    .venv/Scripts/python.exe scripts/fetch_ext_data.py cdd
    # 纳指 100 指数收盘价（FRED NASDAQ100，全量一个 csv）【批准】
    .venv/Scripts/python.exe scripts/fetch_ext_data.py fred
    # Stooq（实测被拒也会如实记录并退出非零）
    .venv/Scripts/python.exe scripts/fetch_ext_data.py stooq

批准状态（T81 落地 D36/第十二轮审核，详见 mt5data.ext.approvals）：
- **fred/cdd 批准**入研究库（export_research_store --include-ext 白名单
  只放行这两个来源；manifest 自带使用限定 usage_restriction）；
- **histdata 暂不批准**（条款不明，查找记录存 data/ext/terms/）——下载
  只为主库侧一致性核对，不进研究库；
- **yahoo 不批准**，已从可选来源移除；Stooq 不用（拒服务）。

行为：
- 下载 → 原始文件存 data/ext/_raw/<来源>/（保留取证）→ 适配器转成
  仓库 K 线（naive UTC）→ 与已有年份合并 → manifest 校验后原子落盘；
- HistData 的 M1 同时重采样出 H1/D1（mt5data.resample，与主库同算法）；
- 每个来源各留 sha256 进 manifest；重复年份跳过下载（--force 重下）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.ext import (  # noqa: E402
    ExtDataError, ExtSeries, adapters, approved_sources, ext_inventory,
    load_ext_series, save_ext_series, utc_now_iso)
from mt5data.resample import resample_bars  # noqa: E402


def _raw_dir(ext_root: str, source: str) -> str:
    d = os.path.join(ext_root, "_raw", source)
    os.makedirs(d, exist_ok=True)
    return d


def _save_raw(ext_root: str, source: str, name: str, raw: bytes) -> str:
    p = os.path.join(_raw_dir(ext_root, source), name)
    with open(p, "wb") as f:
        f.write(raw)
    return p


def _store(ext_root: str, series: ExtSeries, *, resample_to=()) -> None:
    """落盘一条序列（M1 原始 + 指定重采样周期），与已有数据合并。"""
    src, sym, tf = (series.manifest["source"], series.manifest["symbol"],
                    series.manifest["timeframe"])
    existing = load_ext_series(ext_root, src, sym, tf)
    merged = series.merged_with(ExtSeries(df=existing[0], manifest=existing[1])
                                if existing else None)
    save_ext_series(ext_root, merged)
    print(f"  {src}/{sym}/{tf}: {len(merged.df):,} 根 "
          f"[{merged.df['time'].iloc[0]} → {merged.df['time'].iloc[-1]}]")
    for tf2 in resample_to:
        agg = resample_bars(merged.df, tf2)
        m2 = merged.with_timeframe(tf2, agg)
        save_ext_series(ext_root, m2)
        print(f"  {src}/{sym}/{tf2}: {len(agg):,} 根 "
              f"[{agg['time'].iloc[0]} → {agg['time'].iloc[-1]}]")


def cmd_histdata(args) -> int:
    failures = 0
    print("⚠ HistData 暂不批准入研究库（D36：条款不明的不用，查找记录见 "
          "data/ext/terms/）——下载只作主库侧一致性核对，"
          "--include-ext 白名单不放行该来源")
    for pair in args.pairs.split(","):
        pair = pair.strip().upper()
        for year in [int(y) for y in args.years.split(",")]:
            name = f"DAT_ASCII_{pair}_M1_{year}.zip"
            raw_path = os.path.join(args.ext_root, "_raw", "histdata", name)
            if os.path.exists(raw_path) and not args.force:
                print(f"· {pair} {year}：原始文件已存在，直接转换（--force 重下）")
                with open(raw_path, "rb") as f:
                    raw = f.read()
            else:
                print(f"↓ HistData {pair} {year} …", flush=True)
                try:
                    raw, fname = adapters.fetch_histdata_year(pair, year)
                except adapters.ExtSourceUnreachable as e:
                    print(f"✗ {pair} {year} 下载失败：{e}")
                    failures += 1
                    continue
                _save_raw(args.ext_root, "histdata", fname, raw)
            s = adapters.histdata_m1_from_bytes(
                raw, symbol=pair, downloaded_at_utc=utc_now_iso(),
                source_name=name)
            _store(args.ext_root, s, resample_to=("H1", "D1"))
    return 1 if failures else 0


def cmd_cdd(args) -> int:
    name = args.dataset
    raw_path = os.path.join(args.ext_root, "_raw", "cdd", name)
    if os.path.exists(raw_path) and not args.force:
        with open(raw_path, "rb") as f:
            raw = f.read()
        print(f"· {name}：原始文件已存在，直接转换")
    else:
        print(f"↓ CryptoDataDownload {name} …", flush=True)
        try:
            raw = adapters.fetch_cdd_csv(name)
        except adapters.ExtSourceUnreachable as e:
            print(f"✗ 下载失败：{e}")
            return 1
        _save_raw(args.ext_root, "cdd", name, raw)
    s = adapters.cdd_daily_from_bytes(raw, symbol="BTCUSD",
                                      downloaded_at_utc=utc_now_iso(),
                                      source_name=name)
    _store(args.ext_root, s)
    return 0


def cmd_fred(args) -> int:
    name = f"{args.series}.csv"
    raw_path = os.path.join(args.ext_root, "_raw", "fred", name)
    if os.path.exists(raw_path) and not args.force:
        with open(raw_path, "rb") as f:
            raw = f.read()
        print(f"· {name}：原始文件已存在，直接转换")
    else:
        print(f"↓ FRED {args.series} …", flush=True)
        try:
            raw = adapters.fetch_fred_csv(args.series)
        except adapters.ExtSourceUnreachable as e:
            print(f"✗ 下载失败：{e}")
            return 1
        _save_raw(args.ext_root, "fred", name, raw)
    s = adapters.fred_daily_from_bytes(raw, symbol="USTEC",
                                       downloaded_at_utc=utc_now_iso(),
                                       series_id=args.series, source_name=name)
    _store(args.ext_root, s)
    return 0


def cmd_stooq(args) -> int:
    name = f"{args.symbol}_d.csv"
    try:
        raw = adapters.fetch_stooq_csv(args.symbol)
    except adapters.ExtSourceUnreachable as e:
        print(f"✗ Stooq 不可用（如实记录，改用 FRED 或用户网络手工下载）：{e}")
        return 1
    _save_raw(args.ext_root, "stooq", name, raw)
    s = adapters.stooq_daily_from_bytes(raw, symbol="USTEC",
                                        downloaded_at_utc=utc_now_iso(),
                                        source_name=name)
    _store(args.ext_root, s)
    return 0


def cmd_inventory(args) -> int:
    inv = ext_inventory(args.ext_root)
    if not inv:
        print(f"{args.ext_root} 无外部序列")
        return 0
    print(f"{'来源':<10}{'品种':<8}{'周期':<5}{'根数':>12}  起点 → 终点")
    for m in inv:
        print(f"{m['source']:<10}{m['symbol']:<8}{m['timeframe']:<5}"
              f"{m['rows']:>12,}  {m['first_time']} → {m['last_time']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="外部数据下载入库（T67）")
    ap.add_argument("--ext-root", default="data/ext", help="外部数据根目录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("histdata", help="HistData M1（按年 zip）")
    p.add_argument("--pairs", default="EURUSD,XAUUSD")
    p.add_argument("--years", default="2020,2021",
                   help="样本段年份；全量历史 2003 起按年追加")
    p.add_argument("--force", action="store_true", help="重下已存在的年份")
    p.set_defaults(fn=cmd_histdata)

    p = sub.add_parser("cdd", help="CryptoDataDownload 日线（默认 Bitstamp BTC）")
    p.add_argument("--dataset", default="Bitstamp_BTCUSD_d.csv")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_cdd)

    p = sub.add_parser("fred", help="FRED 日线（默认 NASDAQ100）")
    p.add_argument("--series", default="NASDAQ100")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_fred)

    p = sub.add_parser("stooq", help="Stooq 日线（实测被拒则如实失败）")
    p.add_argument("--symbol", default="^ndx")
    p.set_defaults(fn=cmd_stooq)

    p = sub.add_parser("inventory", help="列出已入库的外部序列")
    p.set_defaults(fn=cmd_inventory)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ExtDataError as e:
        print(f"✗ 入库被拒：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
