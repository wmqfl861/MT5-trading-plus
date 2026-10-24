"""reconcile_ab：A/B 双引擎逐笔对账（运维工具，T10）。

同一策略、同一份 bars、同一 tick 覆盖期，分别跑 A 层（engine_a，逐 bar +
点差模型）与 B 层（engine_b，逐 tick 真实报价），逐笔对齐输出差异表
（入场/出场时间、价格、净利），并汇总每笔净利差的分布。

锁箱豁免声明（与 research_bars 的差异）：
- 研究路径（scripts/run_backtest.py / bt.py）必须经 mt5data.lockbox.research_bars，
  只能看到锁箱 cutoff 之前的 K 线；本脚本**直接 store.load_bars 读全量**。
- 豁免理由：本脚本是运维对账工具，不产生任何研究结论——它只校验两个执行
  引擎在同一数据上的口径一致性，输出的是差异表与容差判定，不会进入试验
  台账或假设卡。A/B 两层喂的是同一份 bars，锁箱段数据不会单侧泄漏成
  "可研究的 alpha"。

用法（Git Bash）：
    .venv/Scripts/python.exe scripts/reconcile_ab.py \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold \
        --symbol XAUUSD --timeframe H1 --params '{}' \
        --start 2026-08-01 --end 2026-09-01 --data-root data

约定：
- 对账期 = tick 覆盖期 ∩ [--start, --end]：bars 只保留收盘时刻落在该窗口内
  的（两端含边界），保证 B 层所需 tick 在边界处可用；窗口内 bar 区间缺 tick
  时由 B 层 TickCoverageError fail-fast，绝不静默跳过。
- 成本装配复用 run_backtest.py（build_costs 等，import 不改）。
- 默认容差（真机实测后由主会话把最终值写进 docs/reports/reconciliation-<日期>.md
  并更新这里的常量）：
  * 信号类成交时间差 ≤ 2s（TOL_ENTRY_TIME_MS = TOL_EXIT_TIME_SIGNAL_MS = 2000；
    覆盖 tick 到达抖动，真机实测 1ms——超出 2s 即真分歧）
    ——A 层的 bar 开盘标签 vs B 层的"收盘后第一笔 tick"，tick 正常时同为
    下一根 bar 的开盘 tick；
  * 收盘价类出场（time_stop/week_close/end_of_data）与保护性出场相差
    不足 1 根 bar（结构性差异，见 engine_b docstring）；
  * 每笔净利差 ≤ 1 个点差（NET_TOL_SPREADS = 1.0，容差按该笔
    spread_cost（= 点差 × 手数 × contract_size）计）。
- 退出码：0 = 全部在容差内；2 = 存在超容差差异（差异表已写出）；1 = 装配
  或引擎报错。
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
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 复用 run_backtest 的装配

from mt5data.store import TIMEFRAME_SECONDS, load_bars  # noqa: E402  锁箱豁免：直读全量
from mt5data.ticks import load_ticks, read_ticks_watermark  # noqa: E402
from mt5quant.backtest.engine_a import (  # noqa: E402
    ENGINE_VERSION, run_backtest,
)
from mt5quant.backtest.engine_b import (  # noqa: E402
    ENGINE_VERSION_B, run_backtest_ticks,
)
from mt5quant.backtest.report import make_run_dir  # noqa: E402
from mt5data.store import atomic_replace  # noqa: E402
from run_backtest import (  # noqa: E402
    ZeroCosts, build_costs, check_clock, load_strategy,
    resolve_contract_size,
)

# ------------------------------------------------ 默认容差（T10 约定）
TOL_ENTRY_TIME_MS = 2_000    # 信号类入场时间容差 2s：B 在收盘后第一笔 tick 成交，
                              # 与 A 的次根开盘标签存在毫秒级到达抖动（真机实测
                              # 2026-02→09 XAUUSD H1 为 1ms；见 docs/reports/）
TOL_EXIT_TIME_SIGNAL_MS = 2_000  # 同上
NET_TOL_SPREADS = 1.0        # 每笔净利差 ≤ 1 个点差
EXIT_TIME_OTHER_BARS = 1.0   # 非信号出场（收盘价类/保护性）容差：1 根 bar

# A 层跳空变体与 B 层逐 tick 出场视为同因（B 层无 stop_gap 之分）
_REASON_EQUIV = {"stop_gap": "stop", "take_profit_gap": "take_profit"}

MAX_PRINT_ROWS = 50


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def _series_ms(times: pd.Series) -> pd.Series:
    """时间列 → 毫秒 int64（显式经 ns 单位转换；pandas 3 的 datetime64[us]
    直接 astype('int64') 会得到微秒，绝不能裸转）。"""
    return (times.astype("datetime64[ns]").astype("int64") // 1_000_000)


# ------------------------------------------------ 逐笔对齐

def align_trades(trades_a: pd.DataFrame, trades_b: pd.DataFrame, *,
                 max_entry_skew_ms: int) -> list:
    """贪心对齐两层交易列表（均按时间升序），返回 [(i|None, j|None), ...]。

    规则：同方向且入场时间差 ≤ max_entry_skew_ms 才可配对；否则入场更早的
    一侧记为 unmatched 并前进——配对是容差判定的前提，超容差的时间差必须
    以"已配对但违规"的形式浮出，而不是被吞成两笔 unmatched。
    """
    pairs: list = []
    i = j = 0
    na, nb = len(trades_a), len(trades_b)
    while i < na or j < nb:
        if i >= na:
            pairs.append((None, j)); j += 1
            continue
        if j >= nb:
            pairs.append((i, None)); i += 1
            continue
        ta = pd.Timestamp(trades_a["entry_time"].iloc[i])
        tb = pd.Timestamp(trades_b["entry_time"].iloc[j])
        skew = abs(_ms(tb) - _ms(ta))
        same_side = (str(trades_a["side"].iloc[i])
                     == str(trades_b["side"].iloc[j]))
        if same_side and skew <= max_entry_skew_ms:
            pairs.append((i, j)); i += 1; j += 1
        elif ta <= tb:
            pairs.append((i, None)); i += 1
        else:
            pairs.append((None, j)); j += 1
    return pairs


def build_diff_rows(trades_a, trades_b, pairs, *, tf_seconds: int,
                    tol_entry_time_ms: int = TOL_ENTRY_TIME_MS,
                    tol_exit_time_signal_ms: int = TOL_EXIT_TIME_SIGNAL_MS,
                    net_tol_spreads: float = NET_TOL_SPREADS) -> pd.DataFrame:
    """按配对结果构建逐笔差异表；unmatched 行（only_a/only_b）直接违规。"""
    other_tol_ms = int(EXIT_TIME_OTHER_BARS * tf_seconds * 1000)
    rows = []
    for i, j in pairs:
        if i is None:
            rows.append(_row_one_side(trades_b.iloc[j], "only_b"))
            continue
        if j is None:
            rows.append(_row_one_side(trades_a.iloc[i], "only_a"))
            continue
        a, b = trades_a.iloc[i], trades_b.iloc[j]
        entry_dt = _ms(b["entry_time"]) - _ms(a["entry_time"])
        exit_dt = _ms(b["exit_time"]) - _ms(a["exit_time"])
        reason_a = str(a["exit_reason"])
        reason_b = str(b["exit_reason"])
        reason_ok = (_REASON_EQUIV.get(reason_a, reason_a)
                     == _REASON_EQUIV.get(reason_b, reason_b))
        if reason_a == "signal" and reason_b == "signal":
            exit_tol = tol_exit_time_signal_ms
        else:
            exit_tol = other_tol_ms
        # 净利容差 = net_tol_spreads × 该笔点差成本（账户货币，取两侧较宽者）
        spread_costs = [a["spread_cost"], b["spread_cost"]]
        spread_costs = [float(s) for s in spread_costs
                        if s == s and s is not None]   # NaN 剔除
        net_tol = net_tol_spreads * max(spread_costs) if spread_costs else 0.0
        net_diff = float(b["net"]) - float(a["net"])
        rows.append({
            "match": "paired", "side": str(a["side"]),
            "entry_time_a": a["entry_time"], "entry_time_b": b["entry_time"],
            "entry_dt_ms": entry_dt,
            "exit_time_a": a["exit_time"], "exit_time_b": b["exit_time"],
            "exit_dt_ms": exit_dt,
            "entry_price_a": float(a["entry_price"]),
            "entry_price_b": float(b["entry_price"]),
            "entry_price_diff": float(b["entry_price"]) - float(a["entry_price"]),
            "exit_price_a": float(a["exit_price"]),
            "exit_price_b": float(b["exit_price"]),
            "exit_price_diff": float(b["exit_price"]) - float(a["exit_price"]),
            "net_a": float(a["net"]), "net_b": float(b["net"]),
            "net_diff": net_diff, "net_tol": net_tol,
            "exit_reason_a": reason_a, "exit_reason_b": reason_b,
            "entry_time_ok": abs(entry_dt) <= tol_entry_time_ms,
            "exit_time_ok": abs(exit_dt) <= exit_tol,
            "reason_ok": reason_ok,
            "net_ok": abs(net_diff) <= net_tol,
            "row_ok": bool(abs(entry_dt) <= tol_entry_time_ms
                           and abs(exit_dt) <= exit_tol and reason_ok
                           and abs(net_diff) <= net_tol),
        })
    cols = list(rows[0].keys()) if rows else _EMPTY_ROW_COLS
    return pd.DataFrame(rows, columns=cols)


_EMPTY_ROW_COLS = [
    "match", "side", "entry_time_a", "entry_time_b", "entry_dt_ms",
    "exit_time_a", "exit_time_b", "exit_dt_ms", "entry_price_a",
    "entry_price_b", "entry_price_diff", "exit_price_a", "exit_price_b",
    "exit_price_diff", "net_a", "net_b", "net_diff", "net_tol",
    "exit_reason_a", "exit_reason_b", "entry_time_ok", "exit_time_ok",
    "reason_ok", "net_ok", "row_ok"]


def _row_one_side(t, match: str) -> dict:
    """unmatched 行：该侧字段如实填，另一侧留空，row_ok=False。"""
    return {
        "match": match, "side": str(t["side"]),
        "entry_time_a": t["entry_time"] if match == "only_a" else pd.NaT,
        "entry_time_b": t["entry_time"] if match == "only_b" else pd.NaT,
        "entry_dt_ms": None, "exit_time_a": t["exit_time"] if match == "only_a" else pd.NaT,
        "exit_time_b": t["exit_time"] if match == "only_b" else pd.NaT,
        "exit_dt_ms": None,
        "entry_price_a": float(t["entry_price"]) if match == "only_a" else None,
        "entry_price_b": float(t["entry_price"]) if match == "only_b" else None,
        "entry_price_diff": None,
        "exit_price_a": float(t["exit_price"]) if match == "only_a" else None,
        "exit_price_b": float(t["exit_price"]) if match == "only_b" else None,
        "exit_price_diff": None,
        "net_a": float(t["net"]) if match == "only_a" else None,
        "net_b": float(t["net"]) if match == "only_b" else None,
        "net_diff": None, "net_tol": None,
        "exit_reason_a": str(t["exit_reason"]) if match == "only_a" else None,
        "exit_reason_b": str(t["exit_reason"]) if match == "only_b" else None,
        "entry_time_ok": False, "exit_time_ok": False, "reason_ok": False,
        "net_ok": False, "row_ok": False,
    }


# ------------------------------------------------ 汇总

def summarize_net_diffs(rows: pd.DataFrame) -> dict:
    """差异表的汇总：净利差分布 + 各类违规计数 + 总判定。"""
    paired = rows[rows["match"] == "paired"] if len(rows) else rows
    diffs = pd.to_numeric(paired["net_diff"], errors="coerce").dropna()
    if len(diffs):
        net_stats = {
            "count": int(len(diffs)),
            "mean": float(diffs.mean()),
            "median": float(diffs.median()),
            "min": float(diffs.min()),
            "max": float(diffs.max()),
            "mean_abs": float(diffs.abs().mean()),
            "p95_abs": float(diffs.abs().quantile(0.95)),
            "max_abs": float(diffs.abs().max()),
            "within_tol": int((paired["net_ok"].astype(bool)).sum()),
        }
    else:
        net_stats = {"count": 0, "mean": None, "median": None, "min": None,
                     "max": None, "mean_abs": None, "p95_abs": None,
                     "max_abs": None, "within_tol": 0}
    n_only_a = int((rows["match"] == "only_a").sum()) if len(rows) else 0
    n_only_b = int((rows["match"] == "only_b").sum()) if len(rows) else 0
    # 违规只统计已配对行——unmatched 行的 *_ok 恒 False，单列一档避免重复计数
    sub = paired

    def _bad(col: str) -> int:
        if len(sub) == 0 or col not in sub.columns:
            return 0
        return int((~sub[col].astype(bool)).sum())
    viol = {
        "entry_time": _bad("entry_time_ok"),
        "exit_time": _bad("exit_time_ok"),
        "reason": _bad("reason_ok"),
        "net": _bad("net_ok"),
        "unmatched": n_only_a + n_only_b,
    }
    viol["total"] = int(viol["entry_time"] + viol["exit_time"] + viol["reason"]
                        + viol["net"] + viol["unmatched"])
    return {
        "n_trades_a": int(rows["match"].isin(["paired", "only_a"]).sum()) if len(rows) else 0,
        "n_trades_b": int(rows["match"].isin(["paired", "only_b"]).sum()) if len(rows) else 0,
        "n_paired": int((rows["match"] == "paired").sum()) if len(rows) else 0,
        "n_only_a": n_only_a, "n_only_b": n_only_b,
        "net_diff": net_stats, "violations": viol,
        "ok": viol["total"] == 0,
    }


# ------------------------------------------------ 主对账入口

@dataclass
class ReconcileReport:
    ok: bool
    rows: pd.DataFrame
    summary: dict
    trades_a: pd.DataFrame
    trades_b: pd.DataFrame


def reconcile(strategy, bars: pd.DataFrame, ticks_loader, costs, *,
              symbol: str, params: dict, timeframe: str,
              contract_size: float, volume_lots: float = 1.0,
              initial_capital: float = 10_000.0,
              latency_ms: int = 0, slippage_price: float = 0.0,
              tol_entry_time_ms: int = TOL_ENTRY_TIME_MS,
              tol_exit_time_signal_ms: int = TOL_EXIT_TIME_SIGNAL_MS,
              net_tol_spreads: float = NET_TOL_SPREADS) -> ReconcileReport:
    """同一策略同一数据跑 A/B 两层并对账；语义见模块 docstring。"""
    tf_seconds = TIMEFRAME_SECONDS[timeframe.upper()]
    common = dict(symbol=symbol, params=dict(params),
                  contract_size=contract_size, volume_lots=volume_lots,
                  initial_capital=initial_capital, timeframe=timeframe.upper())
    res_a = run_backtest(strategy, bars, costs, **common)
    res_b = run_backtest_ticks(strategy, bars, ticks_loader, costs,
                               latency_ms=latency_ms,
                               slippage_price=slippage_price, **common)
    pairs = align_trades(res_a.trades, res_b.trades,
                         max_entry_skew_ms=tf_seconds * 1000)
    rows = build_diff_rows(
        res_a.trades, res_b.trades, pairs, tf_seconds=tf_seconds,
        tol_entry_time_ms=tol_entry_time_ms,
        tol_exit_time_signal_ms=tol_exit_time_signal_ms,
        net_tol_spreads=net_tol_spreads)
    summary = summarize_net_diffs(rows)
    summary["engine_a_version"] = ENGINE_VERSION
    summary["engine_b_version"] = ENGINE_VERSION_B
    summary["tolerances"] = {
        "entry_time_ms": int(tol_entry_time_ms),
        "exit_time_signal_ms": int(tol_exit_time_signal_ms),
        "exit_time_other_bars": EXIT_TIME_OTHER_BARS,
        "net_tol_spreads": float(net_tol_spreads),
    }
    summary["ok"] = bool(summary["violations"]["total"] == 0)
    return ReconcileReport(ok=summary["ok"], rows=rows, summary=summary,
                           trades_a=res_a.trades, trades_b=res_b.trades)


# ------------------------------------------------ tick 覆盖期窗口

def bars_in_tick_window(bars: pd.DataFrame, *, first_msc: int, last_msc: int,
                        tf_seconds: int, start=None, end=None) -> pd.DataFrame:
    """把 bars 裁到对账窗口 = tick 覆盖期 ∩ [start, end]（naive UTC）。

    保留收盘时刻落在窗口内的 bar（close_ms ∈ [from_ms, to_ms+1ms]，+1ms 容纳
    "收盘前最后一笔 tick"在 close−1ms 的边界）；首根 bar 的盘中段可以部分
    早于窗口起点——引擎只从它的收盘时刻起消费 tick，且两层喂的是同一份
    bars，不影响对账。不足 2 根 → ValueError（跑不出任何往返）。
    """
    from_ms = int(first_msc) if start is None else max(int(first_msc), _ms(start))
    to_ms = int(last_msc) if end is None else min(int(last_msc), _ms(end))
    if from_ms > to_ms:
        raise ValueError(
            f"对账窗口为空：tick 覆盖 [{pd.Timestamp(first_msc, unit='ms')} … "
            f"{pd.Timestamp(last_msc, unit='ms')}] 与 --start/--end 无交集")
    close_ms = _series_ms(bars["time"]) + tf_seconds * 1000
    keep = (close_ms >= from_ms) & (close_ms <= to_ms + 1)
    out = bars[keep].reset_index(drop=True)
    if len(out) < 2:
        raise ValueError(
            f"对账窗口内 bars 不足（{len(out)} 根）：tick 覆盖 "
            f"{pd.Timestamp(from_ms, unit='ms')} … {pd.Timestamp(to_ms, unit='ms')}，"
            "请放宽 --start/--end 或回补 tick（scripts/download_ticks.py）")
    return out


# ------------------------------------------------ 输出

def _atomic_write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    atomic_replace(tmp, path)


def write_outputs(out_dir: str, report: ReconcileReport, *,
                  meta: dict | None = None) -> dict:
    """写 diff.csv / trades_a.csv / trades_b.csv / summary.json（原子写）。"""
    os.makedirs(out_dir, exist_ok=True)
    summary = dict(report.summary)
    if meta is not None:
        summary["meta"] = meta
    paths = {
        "diff": os.path.join(out_dir, "diff.csv"),
        "trades_a": os.path.join(out_dir, "trades_a.csv"),
        "trades_b": os.path.join(out_dir, "trades_b.csv"),
        "summary": os.path.join(out_dir, "summary.json"),
    }
    report.rows.to_csv(paths["diff"], index=False)
    report.trades_a.to_csv(paths["trades_a"], index=False)
    report.trades_b.to_csv(paths["trades_b"], index=False)
    _atomic_write_text(paths["summary"], json.dumps(summary, ensure_ascii=False,
                                                    indent=2, default=str))
    return paths


def _print_report(report: ReconcileReport, paths: dict | None) -> None:
    s = report.summary
    nd = s["net_diff"]
    print(f"A={s['engine_a_version']}  B={s['engine_b_version']}  "
          f"配对 {s['n_paired']} / A 独有 {s['n_only_a']} / B 独有 {s['n_only_b']}")
    if nd["count"]:
        print(f"每笔净利差（B−A）：mean={nd['mean']:+.4f} median={nd['median']:+.4f} "
              f"min={nd['min']:+.4f} max={nd['max']:+.4f} "
              f"|diff| p95={nd['p95_abs']:.4f} 容差内 {nd['within_tol']}/{nd['count']}")
    print(f"违规：{s['violations']}")
    show = report.rows.head(MAX_PRINT_ROWS)
    if len(report.rows) > MAX_PRINT_ROWS:
        print(f"（仅显示前 {MAX_PRINT_ROWS} 行，全量见 diff.csv）")
    cols = ["match", "side", "entry_dt_ms", "exit_dt_ms", "entry_price_diff",
            "exit_price_diff", "net_diff", "net_tol", "exit_reason_a",
            "exit_reason_b", "row_ok"]
    with pd.option_context("display.width", 200):
        print(show[cols].to_string(index=False))
    if paths:
        print(f"差异表：{paths['diff']}")
        print(f"汇总：{paths['summary']}")


# ------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A/B 双引擎逐笔对账（运维工具）")
    ap.add_argument("--strategy", required=True,
                    help="module:Class，如 mt5quant.strategies.library.buy_hold:BuyHold")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", required=True, help="M1/H1/D1 等")
    ap.add_argument("--params", default="{}", help="JSON 字符串")
    ap.add_argument("--start", default=None, help="对账窗口起点（naive UTC）")
    ap.add_argument("--end", default=None, help="对账窗口终点（naive UTC）")
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--out", default="data/reports/reconciliation")
    ap.add_argument("--volume-lots", type=float, default=1.0)
    ap.add_argument("--initial-capital", type=float, default=10_000.0)
    ap.add_argument("--latency-ms", type=int, default=0)
    ap.add_argument("--slippage-price", type=float, default=0.0,
                    help="B 层每腿额外不利滑点（价格单位）；A 层用成本模型的滑点")
    ap.add_argument("--costs", choices=("auto", "zero"), default="auto")
    ap.add_argument("--spread-fraction", type=float, default=0.1)
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative")
    ap.add_argument("--commission", type=float, default=0.0)
    ap.add_argument("--allow-zero-swap", action="store_true")
    ap.add_argument("--contract-size", type=float, default=None)
    ap.add_argument("--tol-entry-ms", type=int, default=TOL_ENTRY_TIME_MS)
    ap.add_argument("--tol-exit-signal-ms", type=int,
                    default=TOL_EXIT_TIME_SIGNAL_MS)
    ap.add_argument("--net-tol-spreads", type=float, default=NET_TOL_SPREADS)
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
        strategy_cls = load_strategy(args.strategy)
        contract_size = resolve_contract_size(args.data_root, args.symbol,
                                              args.contract_size)
        check_clock(args.data_root)
        # 锁箱豁免：直读全量 bars（理由见模块 docstring）
        bars_all = load_bars(args.data_root, args.symbol, tf)
        if bars_all is None or len(bars_all) == 0:
            raise ValueError(
                f"无 K 线数据：{args.symbol} {tf}（先跑 scripts/download_bars.py）")
        meta = read_ticks_watermark(args.data_root, args.symbol)
        first_msc = (meta or {}).get("first_msc")
        last_msc = (meta or {}).get("last_msc")
        if first_msc is None or last_msc is None:
            raise ValueError(
                f"{args.symbol} 无 tick 水位（data/ticks/{args.symbol}/meta.json），"
                "B 层无法回测；先跑 scripts/download_ticks.py")
        start = pd.Timestamp(args.start) if args.start else None
        end = pd.Timestamp(args.end) if args.end else None
        for label, ts in (("--start", start), ("--end", end)):
            if ts is not None and ts.tzinfo is not None:
                raise ValueError(f"{label} 只接受不带时区的 UTC 时间")
        bars = bars_in_tick_window(bars_all, first_msc=first_msc,
                                   last_msc=last_msc,
                                   tf_seconds=TIMEFRAME_SECONDS[tf],
                                   start=start, end=end)
        if args.costs == "auto":
            costs, cost_version = build_costs(
                args.data_root, args.symbol, args.spread_fraction,
                args.commission, allow_zero_swap=args.allow_zero_swap,
                spread_mode=args.spread_mode)
        else:
            costs, cost_version = ZeroCosts(), "zero-v0"
        ticks_loader = lambda start_msc, end_msc: load_ticks(  # noqa: E731
            args.data_root, args.symbol, start_msc=start_msc, end_msc=end_msc)
        report = reconcile(
            strategy_cls(), bars, ticks_loader, costs, symbol=args.symbol,
            params=params, timeframe=tf, contract_size=contract_size,
            volume_lots=args.volume_lots, initial_capital=args.initial_capital,
            latency_ms=args.latency_ms, slippage_price=args.slippage_price,
            tol_entry_time_ms=args.tol_entry_ms,
            tol_exit_time_signal_ms=args.tol_exit_signal_ms,
            net_tol_spreads=args.net_tol_spreads)
        run_dir = make_run_dir(args.out, args.symbol, tf)
        paths = write_outputs(run_dir, report, meta={
            "symbol": args.symbol, "timeframe": tf,
            "strategy": args.strategy, "params": params,
            "bars_first": str(bars["time"].iloc[0]),
            "bars_last": str(bars["time"].iloc[-1]),
            "start": args.start, "end": args.end,
            "latency_ms": args.latency_ms,
            "slippage_price": args.slippage_price,
            "cost_version": cost_version,
            "tick_first_msc": first_msc, "tick_last_msc": last_msc,
        })
    except Exception as e:  # fail-fast：任何错误如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1

    print(f"对账 {args.symbol} {tf} bars={len(bars)} "
          f"窗口 [{bars['time'].iloc[0]} … {bars['time'].iloc[-1]}]")
    _print_report(report, paths)
    print("✓ 全部差异在容差内" if report.ok else
          "✗ 存在超容差差异（见上表；实测值请汇总进 "
          "docs/reports/reconciliation-<日期>.md）")
    return 0 if report.ok else 2


if __name__ == "__main__":
    sys.exit(main())
