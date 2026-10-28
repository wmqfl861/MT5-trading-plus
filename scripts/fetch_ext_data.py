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

批准状态（T81 落地 D36/第十二轮审核；T87 落地第十三轮 §5，详见
mt5data.ext.approvals）：
- **fred/cdd/histdata 批准**入研究库（export_research_store
  --include-ext 白名单放行；manifest 自带使用限定 usage_restriction；
  histdata 为第十三轮 §5 有条件批准：只作个人研究、不再分发、低频
  下载、只启用逐年口径验证通过的年份——EURUSD 另加定盘锚点）；
- **yahoo 不批准**，已从可选来源移除；Stooq 不用（拒服务）。

行为：
- 下载 → 原始文件存 data/ext/_raw/<来源>/（保留取证）→ 适配器转成
  仓库 K 线（naive UTC）→ 与已有年份合并 → manifest 校验后原子落盘；
- HistData 的 M1 同时重采样出 H1/D1（mt5data.resample，与主库同算法）；
- 每个来源各留 sha256 进 manifest；重复年份跳过下载（--force 重下）。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

import pandas as pd

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


HISTDATA_SUPPORTED_RULES = {"eu_calendar": 3, "us_wall": 4}


def _histdata_year_gate(raw: bytes, *, pair: str,
                        downloaded_at_utc: str,
                        source_name: str) -> tuple[object | None, dict]:
    """单年入库闸门（T87/R13 §5 逐年启用条件），返回 (ExtSeries|None, 记录)。

    1. **逐年口径检测**（consistency.yearly_label_calendar，原始标签）：
       判为 eu_calendar（converter v3）或 us_wall（converter v4）→ 按
       该年自己的规则转换；inconsistent/undetermined/fixed_est →
       **登记后不用**（不入库）；
    2. **EURUSD 另加定盘锚点**（consistency.fix_window_vol_spike，
       月末口径——C002 的事件总体）：status 必须为 consistent（尖峰锚在
       伦敦 16:00 定盘窗），否则该年同样登记后不用。
    """
    from mt5data.ext import consistency as cons

    stamps = adapters.histdata_raw_stamps(raw)
    rep = cons.yearly_label_calendar(stamps)
    years = sorted(rep)
    if len(years) != 1:
        return None, {"used": False,
                      "reason": f"zip 内年份 {years} 非单一年份"}
    year = years[0]
    calendar = rep[year]["calendar"]
    rec: dict = {"used": False, "calendar": calendar,
                 "label_deviations": rep[year].get("deviations"),
                 "label_tolerance": rep[year].get("tolerance"),
                 "converter_version": HISTDATA_SUPPORTED_RULES.get(calendar)}
    if calendar not in HISTDATA_SUPPORTED_RULES:
        rec["reason"] = (f"逐年口径判为 {calendar}（三种规则偏差均超容差"
                         "或无区分度）——登记后不用（R13 §5）")
        return None, rec
    s = adapters.histdata_m1_from_bytes(
        raw, symbol=pair, downloaded_at_utc=downloaded_at_utc,
        source_name=source_name, label_rule=calendar)
    if pair.upper() == "EURUSD":
        anchor = cons.fix_window_vol_spike(s.df, month_end_only=True)
        rec["fix_anchor_month_end"] = anchor.get(year)
        if anchor.get(year, {}).get("status") != "consistent":
            rec["reason"] = (f"定盘锚点月末口径判为 "
                             f"{anchor.get(year, {}).get('status')}——"
                             "尖峰未锚在伦敦 16:00 定盘窗，登记后不用")
            return None, rec
    rec["used"] = True
    return s, rec


def cmd_histdata(args) -> int:
    from mt5data.ext import ExtSeries

    failures = 0
    print("ℹ HistData 有条件批准入研究库（R13 §5：只作个人研究、不再分发；"
          "低频下载不并发不绕限；只入库逐年口径验证通过的年份，EURUSD "
          "另加定盘锚点月末口径）")
    for pair in args.pairs.split(","):
        pair = pair.strip().upper()
        for year in [int(y) for y in args.years.split(",")]:
            name = f"DAT_ASCII_{pair}_M1_{year}.zip"
            raw_path = os.path.join(args.ext_root, "_raw", "histdata", name)
            if os.path.exists(raw_path) and not args.force:
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
        # 全量重建：只合并通过闸门的年份（R13 §5 逐年启用条件）
        raw_dir = os.path.join(args.ext_root, "_raw", "histdata")
        usable: list[ExtSeries] = []
        verification: dict[str, dict] = {}
        for zp in sorted(glob.glob(os.path.join(
                raw_dir, f"DAT_ASCII_{pair}_M1_*.zip"))):
            year = int(Path(zp).stem.split("_")[-1])
            with open(zp, "rb") as f:
                raw = f.read()
            try:
                s, rec = _histdata_year_gate(
                    raw, pair=pair, downloaded_at_utc=utc_now_iso(),
                    source_name=os.path.basename(zp))
            except ExtDataError as e:
                s, rec = None, {"used": False, "reason": f"转换失败：{e}"}
            verification[str(year)] = rec
            mark = "✓" if rec.get("used") else "✗"
            extra = rec.get("reason", "")
            cal = rec.get("calendar", "?")
            anchor = (rec.get("fix_anchor_month_end") or {}).get("status")
            print(f"  {mark} {pair} {year}：口径={cal}"
                  + (f"，定盘锚点={anchor}" if anchor else "")
                  + (f"——{extra}" if extra else ""))
            if s is not None:
                usable.append(s)
        if not usable:
            print(f"✗ {pair}：无可用年份（全部被逐年闸门拒绝），不落盘")
            failures += 1
            continue
        df = pd.concat([s.df for s in usable], ignore_index=True) \
            .sort_values("time").reset_index(drop=True)
        m = dict(usable[-1].manifest)  # 以最新年份转换为模板
        files = {}
        for s in usable:
            for f in s.manifest["source_files"]:
                files[f["sha256"]] = f
        m["source_files"] = sorted(files.values(), key=lambda f: f["name"])
        m["converter_version"] = max(
            int(s.manifest["converter_version"]) for s in usable)
        m["label_rule"] = "per_year（逐年规则与版本见 yearly_verification）"
        m["rows"] = int(len(df))
        m["first_time"] = pd.Timestamp(df["time"].iloc[0]).isoformat()
        m["last_time"] = pd.Timestamp(df["time"].iloc[-1]).isoformat()
        m["yearly_verification"] = verification
        rebuilt = ExtSeries(df=df, manifest=m)
        # 全量重建：直接替换（不与旧序列合并——旧序列可能含按错误规则
        # 转换的年份），H1/D1 由重建后的 M1 重采样
        save_ext_series(args.ext_root, rebuilt)
        print(f"  histdata/{pair}/M1: {len(df):,} 根 "
              f"[{df['time'].iloc[0]} → {df['time'].iloc[-1]}]")
        for tf2 in ("H1", "D1"):
            agg = resample_bars(rebuilt.df, tf2)
            save_ext_series(args.ext_root,
                            rebuilt.with_timeframe(tf2, agg))
            print(f"  histdata/{pair}/{tf2}: {len(agg):,} 根 "
                  f"[{agg['time'].iloc[0]} → {agg['time'].iloc[-1]}]")
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
