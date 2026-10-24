"""reconcile_mt5_tester：MT5 策略测试器成交 CSV ↔ B 层回测逐笔对账（T10）。

参考 EA（mql5/Experts/BuyHoldRef.mq5、MACrossRef.mq5）在 OnTester 里把
自己的成交写成 CSV（表头 = 本模块的 CSV_HEADER，写到 FILE_COMMON 公共目录
<终端数据目录>/Common/Files/）。本脚本读该 CSV（--csv 路径参数化），用
**同参数**跑 B 层（engine_b，逐 tick），逐笔比对时间/方向/价格/手数，
输出差异表与容差判定。

锁箱豁免声明（与 research_bars 的差异）：
- 研究路径必须经 mt5data.lockbox.research_bars 只看 cutoff 之前的 K 线；
  本脚本直接 store.load_bars 读全量。
- 豁免理由：本脚本是运维对账工具，不产生研究结论——只校验 B 层引擎与
  MT5 策略测试器（真实 tick 执行）的口径一致性；两侧喂同一份 bars，
  锁箱段数据不会单侧泄漏成"可研究的 alpha"。

窗口与预热注意（影响能否对上，务必读）：
- bars 窗口默认按 CSV 首末成交推导：起点 = 首笔入场 − 1 根 bar（B 层
  bar0 恰在首笔入场时刻收盘，信号才会在同一时刻成交）；终点 = 末笔出场
  + 1 根 bar（让边界信号可执行）。带预热的策略（如均线交叉）请显式传
  --start = 测试器起始时间，保证 SMA/ATR 预热与 EA 一致；
- 测试器末根的强制平仓与 B 层 end_of_data 的收盘 bar 边界天然差得到
  1 根 bar：EA 持仓到测试结束（BuyHold）请传 --end = 测试器结束时间；
  EA 提前平仓（MACross 末笔为 signal）可用 --drop-b-trailing-end-of-data
  丢弃 B 层尾部强制平仓的镜像交易。
- 默认容差（真机校准后由主会话写入 docs/reports/reconciliation-<日期>.md）：
  时间/价格/手数都要求**完全一致**（两侧同源真实 tick：我们的 tick 库与
  测试器"基于真实 tick"模式同出自经纪商服务器）。
- 退出码：0 = 全部在容差内；2 = 超容差；1 = 装配/引擎报错。

用法（Git Bash）：
    .venv/Scripts/python.exe scripts/reconcile_mt5_tester.py \
        --csv "C:/Users/<you>/AppData/Roaming/MetaQuotes/Terminal/Common/Files/BuyHoldRef_trades.csv" \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold \
        --symbol XAUUSD --timeframe H1 --params '{}' \
        --start 2026-08-01 --end 2026-09-01 --data-root data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 复用脚本装配

from mt5data.store import TIMEFRAME_SECONDS, load_bars  # noqa: E402  锁箱豁免：直读
from mt5data.ticks import load_ticks  # noqa: E402
from mt5quant.backtest.engine_b import ENGINE_VERSION_B, run_backtest_ticks  # noqa: E402
from mt5data.store import atomic_replace  # noqa: E402
from run_backtest import (  # noqa: E402
    ZeroCosts, build_costs, check_clock, load_strategy,
    resolve_contract_size,
)
from reconcile_ab import align_trades  # noqa: E402

# EA（BuyHoldRef/MACrossRef）OnTester 写出的成交表（与 .mq5 内 CSV_HEADER 逐字一致）
CSV_COLUMNS = ["seq", "side", "volume_lots", "entry_msc", "entry_time",
               "entry_price", "exit_msc", "exit_time", "exit_price", "gross",
               "exit_reason"]
CSV_HEADER = ",".join(CSV_COLUMNS)

# ------------------------------------------------ 默认容差（真机校准前先按"同源 tick 完全一致"）
TOL_TIME_MS = 0     # 入场/出场时间必须完全一致（ms）
TOL_PRICE = 0.0     # 成交价必须完全一致（价格单位）
TOL_LOTS = 0.0      # 手数必须完全一致

MAX_PRINT_ROWS = 50


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def _series_ms(times: pd.Series) -> pd.Series:
    return (times.astype("datetime64[ns]").astype("int64") // 1_000_000)


# ------------------------------------------------ CSV 解析

def parse_tester_csv(path) -> pd.DataFrame:
    """读参考 EA 写的成交 CSV 并校验；entry/exit 时间以 *_msc 为准重建。

    - 缺列/坏数值/未知方向 → ValueError（带列名与行号，绝不静默丢行）；
    - entry_time/exit_time 字符串列被 msc 换算的 Timestamp 覆盖（EA 侧
      字符串仅为人读，毫秒精度只保证在 *_msc）。
    """
    df = pd.read_csv(path)
    missing = [c for c in CSV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"EA 成交 CSV 缺列：{missing}（期望表头 {CSV_HEADER}）")
    for col in ("volume_lots", "entry_price", "exit_price", "gross"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
        bad = df[df[col].isna()]
        if len(bad):
            raise ValueError(f"CSV 列 {col} 第 {list(bad.index + 1)} 行不是数值")
    for col in ("entry_msc", "exit_msc"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("int64")
        bad = df[df[col].isna()]
        if len(bad):
            raise ValueError(f"CSV 列 {col} 第 {list(bad.index + 1)} 行不是整数毫秒")
    bad_side = df[~df["side"].isin(["long", "short"])]
    if len(bad_side):
        raise ValueError(f"CSV side 第 {list(bad_side.index + 1)} 行不是 "
                         "long/short（EA 只写这两个值）")
    df["entry_time"] = pd.to_datetime(df["entry_msc"], unit="ms")
    df["exit_time"] = pd.to_datetime(df["exit_msc"], unit="ms")
    return df.reset_index(drop=True)


# ------------------------------------------------ 与 B 层逐笔比对

@dataclass
class Mt5CompareReport:
    ok: bool
    rows: pd.DataFrame
    summary: dict


def compare_with_b(csv_df: pd.DataFrame, b_trades: pd.DataFrame, *,
                   tf_seconds: int,
                   tol_time_ms: int = TOL_TIME_MS,
                   tol_price: float = TOL_PRICE,
                   tol_lots: float = TOL_LOTS) -> Mt5CompareReport:
    """MT5 测试器成交 vs B 层交易逐笔比对（时间/方向/价格/手数 + 容差判定）。

    gross 只作交叉核对（contract_size 口径的毛利），不参与判定——EA 侧
    不含佣金/隔夜费口径的净利。
    """
    pairs = align_trades(csv_df, b_trades, max_entry_skew_ms=tf_seconds * 1000)
    rows = []
    for i, j in pairs:
        if i is None:
            rows.append({"match": "only_b",
                         "side_mt5": None, "side_b": str(b_trades["side"].iloc[j]),
                         "side_match": False, "entry_dt_ms": None,
                         "exit_dt_ms": None, "entry_price_diff": None,
                         "exit_price_diff": None, "lots_diff": None,
                         "gross_mt5": None, "gross_b": None, "gross_diff": None,
                         "reason_mt5": None,
                         "reason_b": str(b_trades["exit_reason"].iloc[j]),
                         "reason_ok": False,
                         "entry_time_ok": False, "exit_time_ok": False,
                         "price_ok": False, "lots_ok": False, "row_ok": False})
            continue
        if j is None:
            rows.append({"match": "only_mt5",
                         "side_mt5": str(csv_df["side"].iloc[i]), "side_b": None,
                         "side_match": False, "entry_dt_ms": None,
                         "exit_dt_ms": None, "entry_price_diff": None,
                         "exit_price_diff": None, "lots_diff": None,
                         "gross_mt5": None, "gross_b": None, "gross_diff": None,
                         "reason_mt5": str(csv_df["exit_reason"].iloc[i]),
                         "reason_b": None,
                         "reason_ok": False,
                         "entry_time_ok": False, "exit_time_ok": False,
                         "price_ok": False, "lots_ok": False, "row_ok": False})
            continue
        m, b = csv_df.iloc[i], b_trades.iloc[j]
        entry_dt = _ms(b["entry_time"]) - int(m["entry_msc"])
        exit_dt = _ms(b["exit_time"]) - int(m["exit_msc"])
        entry_diff = float(b["entry_price"]) - float(m["entry_price"])
        exit_diff = float(b["exit_price"]) - float(m["exit_price"])
        lots_diff = float(b["volume_lots"]) - float(m["volume_lots"])
        time_ok = abs(entry_dt) <= tol_time_ms and abs(exit_dt) <= tol_time_ms
        price_ok = (abs(entry_diff) <= tol_price and abs(exit_diff) <= tol_price)
        lots_ok = abs(lots_diff) <= tol_lots
        side_ok = str(m["side"]) == str(b["side"])
        reason_ok = str(m["exit_reason"]) == str(b["exit_reason"])
        rows.append({
            "match": "paired", "side_mt5": str(m["side"]), "side_b": str(b["side"]),
            "side_match": side_ok, "entry_dt_ms": entry_dt, "exit_dt_ms": exit_dt,
            "entry_price_diff": entry_diff, "exit_price_diff": exit_diff,
            "lots_diff": lots_diff,
            "gross_mt5": float(m["gross"]), "gross_b": float(b["gross"]),
            "gross_diff": float(b["gross"]) - float(m["gross"]),
            "reason_mt5": str(m["exit_reason"]), "reason_b": str(b["exit_reason"]),
            "reason_ok": reason_ok,
            "entry_time_ok": abs(entry_dt) <= tol_time_ms,
            "exit_time_ok": abs(exit_dt) <= tol_time_ms,
            "price_ok": price_ok, "lots_ok": lots_ok,
            "row_ok": bool(time_ok and price_ok and lots_ok and side_ok
                           and reason_ok)})
    frame = pd.DataFrame(rows)
    n_only_mt5 = int((frame["match"] == "only_mt5").sum()) if len(frame) else 0
    n_only_b = int((frame["match"] == "only_b").sum()) if len(frame) else 0
    summary = {
        "n_mt5": int((frame["match"] != "only_b").sum()) if len(frame) else 0,
        "n_b": int((frame["match"] != "only_mt5").sum()) if len(frame) else 0,
        "n_paired": int((frame["match"] == "paired").sum()) if len(frame) else 0,
        "n_only_mt5": n_only_mt5, "n_only_b": n_only_b,
        "violations": _violations(frame, n_only_mt5 + n_only_b),
        "engine_b_version": ENGINE_VERSION_B,
        "tolerances": {"time_ms": int(tol_time_ms),
                       "price": float(tol_price), "lots": float(tol_lots)},
    }
    summary["ok"] = summary["violations"]["total"] == 0
    return Mt5CompareReport(ok=summary["ok"], rows=frame, summary=summary)


def _violations(frame: pd.DataFrame, unmatched: int) -> dict:
    # 违规只统计已配对行——unmatched 行的 *_ok 恒 False，单列一档避免重复计数
    sub = frame[frame["match"] == "paired"] if len(frame) else frame

    def bad(col: str) -> int:
        if len(sub) == 0 or col not in sub.columns:
            return 0
        return int((~sub[col].astype(bool)).sum())
    out = {
        "time": bad("entry_time_ok") + bad("exit_time_ok"),
        "side": bad("side_match"),
        "price": bad("price_ok"),
        "lots": bad("lots_ok"),
        "reason": bad("reason_ok"),
        "unmatched": unmatched,
    }
    out["total"] = int(sum(out.values()))
    return out


# ------------------------------------------------ 尾部 end_of_data 处理

def drop_b_trailing_end_of_data(b_trades: pd.DataFrame, csv_df: pd.DataFrame,
                                *, tf_seconds: int) -> pd.DataFrame:
    """丢弃 B 层尾部因数据末尾强制平仓的镜像交易（EA 早已 signal 平仓时）。

    只丢"end_of_data 且出场晚于 EA 末笔出场 + 1 根 bar"的交易；EA 也持仓
    到最后（其末笔即 end_of_data）或 B 的 end_of_data 在 EA 末笔之前 → 不丢。
    """
    if not len(b_trades) or not len(csv_df):
        return b_trades
    cutoff = int(csv_df["exit_msc"].max()) + tf_seconds * 1000
    exit_ms = b_trades["exit_time"].map(_ms)
    keep = ~((b_trades["exit_reason"] == "end_of_data") & (exit_ms > cutoff))
    return b_trades[keep].reset_index(drop=True)


# ------------------------------------------------ bars 窗口（按 CSV 推导）

def bars_window_for_csv(bars: pd.DataFrame, csv_df: pd.DataFrame, *,
                        tf_seconds: int, start=None, end=None) -> pd.DataFrame:
    """把 bars 裁到能复现 CSV 成交的窗口（语义见模块 docstring"窗口与预热"）。

    默认起点 = CSV 首笔入场 − 1 根 bar（B 层首根 bar 恰在该时刻收盘，
    其收盘信号在首笔入场时刻成交）；默认终点 = 末笔出场 + 1 根 bar。
    显式 --start/--end 优先（预热/测试器结束时间对齐）。
    """
    from_ms = (_ms(start) if start is not None
               else int(csv_df["entry_msc"].min()) - tf_seconds * 1000)
    to_ms = (_ms(end) if end is not None
             else int(csv_df["exit_msc"].max()) + tf_seconds * 1000)
    close_ms = _series_ms(bars["time"]) + tf_seconds * 1000
    keep = (close_ms > from_ms) & (close_ms <= to_ms)
    out = bars[keep].reset_index(drop=True)
    if len(out) < 2:
        raise ValueError(
            f"bars 窗口内不足 2 根（{len(out)}）：CSV 首末成交 "
            f"[{pd.Timestamp(int(csv_df['entry_msc'].min()), unit='ms')} … "
            f"{pd.Timestamp(int(csv_df['exit_msc'].max()), unit='ms')}]；"
            "请检查 --start/--end 或 K 线覆盖")
    return out


# ------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="MT5 策略测试器成交 CSV ↔ B 层逐笔对账（运维工具）")
    ap.add_argument("--csv", required=True, help="参考 EA OnTester 写出的成交 CSV 路径")
    ap.add_argument("--strategy", required=True, help="module:Class（与 EA 对应的 Python 策略）")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", required=True, help="与测试器一致，如 H1")
    ap.add_argument("--params", default="{}", help="JSON 字符串")
    ap.add_argument("--start", default=None,
                    help="bars 窗口起点（naive UTC）；带预热的策略应传测试器起始时间")
    ap.add_argument("--end", default=None,
                    help="bars 窗口终点（naive UTC）；EA 持仓到测试结束时应传测试器结束时间")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--volume-lots", type=float, default=1.0)
    ap.add_argument("--initial-capital", type=float, default=10_000.0)
    ap.add_argument("--latency-ms", type=int, default=0)
    ap.add_argument("--slippage-price", type=float, default=0.0)
    ap.add_argument("--costs", choices=("auto", "zero"), default="auto")
    ap.add_argument("--spread-fraction", type=float, default=0.1)
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative")
    ap.add_argument("--commission", type=float, default=0.0)
    ap.add_argument("--allow-zero-swap", action="store_true")
    ap.add_argument("--contract-size", type=float, default=None)
    ap.add_argument("--tol-time-ms", type=int, default=TOL_TIME_MS)
    ap.add_argument("--tol-price", type=float, default=TOL_PRICE)
    ap.add_argument("--tol-lots", type=float, default=TOL_LOTS)
    ap.add_argument("--drop-b-trailing-end-of-data", action="store_true",
                    help="EA 末笔为 signal 平仓时，丢弃 B 层尾部强制平仓的镜像交易")
    ap.add_argument("--out-csv", default=None, help="差异表输出路径（CSV）")
    args = ap.parse_args(argv)

    tf = args.timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        print(f"✗ 未知周期：{args.timeframe}")
        return 1
    try:
        params = json.loads(args.params)
    except ValueError as e:
        print(f"✗ --params 不是合法 JSON：{e}")
        return 1
    try:
        csv_df = parse_tester_csv(args.csv)
        strategy_cls = load_strategy(args.strategy)
        contract_size = resolve_contract_size(args.data_root, args.symbol,
                                              args.contract_size)
        check_clock(args.data_root)
        # 锁箱豁免：直读全量 bars（理由见模块 docstring）
        bars_all = load_bars(args.data_root, args.symbol, tf)
        if bars_all is None or len(bars_all) == 0:
            raise ValueError(
                f"无 K 线数据：{args.symbol} {tf}（先跑 scripts/download_bars.py）")
        for label, v in (("--start", args.start), ("--end", args.end)):
            if v is not None and pd.Timestamp(v).tzinfo is not None:
                raise ValueError(f"{label} 只接受不带时区的 UTC 时间")
        bars = bars_window_for_csv(
            bars_all, csv_df, tf_seconds=TIMEFRAME_SECONDS[tf],
            start=pd.Timestamp(args.start) if args.start else None,
            end=pd.Timestamp(args.end) if args.end else None)
        if args.costs == "auto":
            costs, cost_version = build_costs(
                args.data_root, args.symbol, args.spread_fraction,
                args.commission, allow_zero_swap=args.allow_zero_swap,
                spread_mode=args.spread_mode)
        else:
            costs, cost_version = ZeroCosts(), "zero-v0"
        ticks_loader = lambda start_msc, end_msc: load_ticks(  # noqa: E731
            args.data_root, args.symbol, start_msc=start_msc, end_msc=end_msc)
        res_b = run_backtest_ticks(
            strategy_cls(), bars, ticks_loader, costs, symbol=args.symbol,
            params=params, contract_size=contract_size,
            volume_lots=args.volume_lots,
            initial_capital=args.initial_capital, timeframe=tf,
            latency_ms=args.latency_ms, slippage_price=args.slippage_price)
        b_trades = res_b.trades
        if args.drop_b_trailing_end_of_data:
            b_trades = drop_b_trailing_end_of_data(
                b_trades, csv_df, tf_seconds=TIMEFRAME_SECONDS[tf])
        report = compare_with_b(csv_df, b_trades, tf_seconds=TIMEFRAME_SECONDS[tf],
                                tol_time_ms=args.tol_time_ms,
                                tol_price=args.tol_price,
                                tol_lots=args.tol_lots)
    except Exception as e:  # fail-fast：任何错误如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1

    print(f"对账 MT5 测试器 CSV {args.csv}")
    print(f"B 层 bars={len(bars)} 窗口 [{bars['time'].iloc[0]} … "
          f"{bars['time'].iloc[-1]}] 成本={cost_version}")
    s = report.summary
    print(f"B={s['engine_b_version']}  配对 {s['n_paired']} / "
          f"仅 MT5 {s['n_only_mt5']} / 仅 B {s['n_only_b']}  容差 {s['tolerances']}")
    print(f"违规：{s['violations']}")
    show = report.rows.head(MAX_PRINT_ROWS)
    if len(report.rows) > MAX_PRINT_ROWS:
        print(f"（仅显示前 {MAX_PRINT_ROWS} 行）")
    cols = ["match", "side_mt5", "side_b", "entry_dt_ms", "exit_dt_ms",
            "entry_price_diff", "exit_price_diff", "lots_diff", "gross_diff",
            "reason_mt5", "reason_b", "row_ok"]
    cols = [c for c in cols if c in show.columns]
    with pd.option_context("display.width", 200):
        print(show[cols].to_string(index=False))
    if args.out_csv:
        tmp = args.out_csv + ".tmp"
        report.rows.to_csv(tmp, index=False)
        atomic_replace(tmp, args.out_csv)
        print(f"差异表：{args.out_csv}")
    print("✓ 与 MT5 策略测试器完全对上" if report.ok else
          "✗ 存在超容差差异（实测值请汇总进 "
          "docs/reports/reconciliation-<日期>.md 并校准容差常量）")
    return 0 if report.ok else 2


if __name__ == "__main__":
    sys.exit(main())
