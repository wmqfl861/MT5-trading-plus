"""A 层回测 CLI。

示例：
    .venv/Scripts/python.exe scripts/run_backtest.py \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold \
        --symbol XAUUSD --timeframe H1 --params '{}' \
        --data-root data --end 2026-09-01 --out data/reports/backtests

约定：
- --end 经 mt5data.lockbox.research_bars（锁箱语义：显式越过 cutoff 的请求
  直接报错，绝不静默截断）；无 lockbox.json 时返回全量，行为与直读一致。
- 金额口径：contract_size 取 --contract-size，否则读
  {data-root}/specs/latest.json（scripts/snapshot_specs.py 产出）；两者都没有
  → 报错退出（绝不静默按 1 折算——那会让盈亏差几百到十万倍）。
- 成本：--costs auto（默认）＝tick 实测点差+滑点，点差默认**相对口径**
  （--spread-mode relative，R1：p90 of (ask−bid)/mid 按小时，回测时按成交
  基准价缩放；--spread-mode absolute 为旧口径）；佣金与隔夜费优先
  {data-root}/costs/calibrated.json（scripts/calibrate_costs.py 实测），其次
  品种快照推算（SwapModel.from_snapshot）；两者都没有 → 报错，除非显式
  --allow-zero-swap（零隔夜费会写进 manifest）。--costs zero 用零成本占位。
- 时钟校验（R5）：读 {data-root}/server_clock.json 最新有效样本，与配置的
  server_utc_offset_hours 比对，超限拒绝运行；无有效样本仅警告。
- 每次运行写入独占目录 {out}/{SYMBOL}_{TF}_{UTC戳}_{随机}，不覆盖历史运行。
"""
import argparse
import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.clock import (  # noqa: E402
    ClockMismatchError, check_server_offset, latest_valid_sample,
)
from mt5data.lockbox import research_bars  # noqa: E402
from mt5data.store import TIMEFRAME_SECONDS  # noqa: E402
from mt5data.ticks import load_ticks  # noqa: E402
from mt5quant.backtest.engine_a import run_backtest  # noqa: E402
from mt5quant.backtest.engine_b import run_backtest_ticks  # noqa: E402
from mt5quant.backtest.report import make_run_dir, summarize, write_report  # noqa: E402


class ZeroCosts:
    """零成本占位：实现 engine_a.Costs Protocol。"""

    def spread_p90(self, symbol, hour_utc, *, price=None):
        return 0.0

    def slippage(self, symbol, hour_utc, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


def resolve_contract_size(data_root: str, symbol: str, override: float | None) -> float:
    """合约大小：CLI 覆盖 > {data_root}/specs/latest.json；都没有 → ValueError。"""
    if override is not None:
        if not override > 0:
            raise ValueError(f"--contract-size 必须为正，收到 {override}")
        return float(override)
    snap_path = os.path.join(data_root, "specs", "latest.json")
    if os.path.exists(snap_path):
        with open(snap_path, encoding="utf-8") as f:
            snap = json.load(f)
        spec = snap.get(symbol) if isinstance(snap, dict) else None
        cs = spec.get("contract_size") if isinstance(spec, dict) else None
        if isinstance(cs, (int, float)) and cs > 0:
            return float(cs)
    raise ValueError(
        f"{symbol} 合约大小未知：先跑 scripts/snapshot_specs.py 生成 "
        f"{snap_path}，或显式传 --contract-size（XAUUSD 常见 100、EURUSD 100000）"
    )


def check_clock(data_root: str, *, tol_s: float = 120.0) -> None:
    """R5：最新有效时钟样本 vs 配置的 server_utc_offset_hours；无样本仅警告。"""
    sample = latest_valid_sample(os.path.join(data_root, "server_clock.json"))
    expected = 0.0
    for cfg_name in ("config.json", "gateway.json"):
        p = os.path.join(os.getcwd(), cfg_name)
        if os.path.exists(p):
            from mt5gw import load
            expected = load(p).server_utc_offset_hours
            break
    if sample is None:
        print(f"⚠ {data_root}/server_clock.json 无有效时钟样本（休市或未测），"
              f"跳过时区校验；开市后跑 scripts/measure_server_clock.py")
        return
    check_server_offset(sample["offset_s"], expected, tol_s=tol_s)
    print(f"✓ 时钟校验：实测偏移 {sample['offset_s']:+.0f}s 与配置 "
          f"{expected:g}h 一致（样本 {sample['wall_utc']}）")


def _load_calibrated(data_root: str) -> dict | None:
    p = os.path.join(data_root, "costs", "calibrated.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build_costs(data_root: str, symbol: str, spread_fraction: float,
                commission_per_lot: float = 0.0, *,
                allow_zero_swap: bool = False,
                spread_mode: str = "relative") -> tuple:
    """tick 实测点差 + 佣金/隔夜费（calibrated.json > 品种快照 > 报错）。

    spread_mode（R1/T3）：relative（默认）＝(ask−bid)/mid 按小时分位，
    引擎按成交基准价缩放——近期点差结构可外推到价格水平不同的历史；
    absolute＝旧口径（价格单位，忽略 price）。cost_version 里写明 rel/abs。

    返回 (costs, cost_version)。佣金优先 calibrated 实测，其次 CLI 显式值；
    隔夜费优先 calibrated 实测费率（free_of_swap=true 属合法实测零费），其次
    快照推算；两者都没有且未 --allow-zero-swap → ValueError（禁止静默按 0）。
    """
    from mt5quant.costs import (
        CommissionModel, Costs, SlippageModel, SpreadModel, SwapModel,
        SwapRule,
    )

    ticks = load_ticks(data_root, symbol)
    if ticks is None or len(ticks) == 0:
        print(f"⚠ {symbol} 无 tick 数据，退回零成本；"
              f"先跑 scripts/download_ticks.py 可启用实测点差/滑点")
        return ZeroCosts(), "zero-v0(no-ticks)"
    spread = SpreadModel.from_ticks(symbol, ticks, mode=spread_mode)
    slip = SlippageModel(spread, spread_fraction=spread_fraction)
    mode_tag = "rel" if spread_mode == "relative" else "abs"

    calibrated = _load_calibrated(data_root)
    cal_sym = None
    if calibrated and symbol in calibrated.get("symbols", {}):
        cal_sym = calibrated["symbols"][symbol]

    # ---- 佣金：calibrated 实测 > CLI 显式值（默认 0，标准账户点差内含属合法） ----
    if cal_sym and cal_sym.get("commission", {}).get("round_trip_per_lot") is not None:
        rt = float(cal_sym["commission"]["round_trip_per_lot"])
        commission = CommissionModel(default_per_lot=rt)
        comm_tag = f"comm{rt:g}-calibrated"
    else:
        commission = CommissionModel(default_per_lot=commission_per_lot)
        comm_tag = f"comm{commission_per_lot:g}-cli"

    # ---- 隔夜费：calibrated 实测费率 > 快照推算 > 报错/显式零 ----
    swap = None
    swap_tag = "swap?"
    cal_sw = cal_sym.get("swap") if cal_sym else None
    cal_rate = None
    if cal_sw:
        lng_r = cal_sw.get("long", {}).get("per_lot_per_night")
        sht_r = cal_sw.get("short", {}).get("per_lot_per_night")
        if lng_r is not None or sht_r is not None:
            cal_rate = (lng_r if lng_r is not None else 0.0,
                        sht_r if sht_r is not None else 0.0)
    if cal_rate is not None:
        cands = cal_sw.get("charge_hour_candidates_utc") or [21]
        ch = int(cands[0])
        # 实测均值费率已含三倍日摊薄，按无三倍日规则使用（口径见 cost_version 标注）
        swap = SwapModel(per_symbol={symbol: SwapRule(
            long_per_day=cal_rate[0], short_per_day=cal_rate[1],
            triple_weekdays=frozenset(), charge_hour_utc=ch)})
        ambig = "（时刻有歧义取首个）" if cal_sw.get("charge_hour_ambiguous") else ""
        swap_tag = f"swap-cal(ch{ch}){ambig}"
    elif cal_sw and cal_sw.get("free_of_swap") is True:
        swap = SwapModel(per_symbol={symbol: SwapRule(
            long_per_day=0.0, short_per_day=0.0)})
        swap_tag = "swap0-calibrated(free)"
    else:
        snap_path = os.path.join(data_root, "specs", "latest.json")
        try:
            with open(snap_path, encoding="utf-8") as f:
                snap = json.load(f)
            swap = SwapModel.from_snapshot(snap, [symbol], charge_hour_utc=21)
            swap_tag = "swap-snapshot"
        except (OSError, KeyError, NotImplementedError, ValueError) as e:
            if allow_zero_swap:
                swap = SwapModel(per_symbol={symbol: SwapRule(
                    long_per_day=0.0, short_per_day=0.0)})
                swap_tag = "swap0-explicit-allow"
                print(f"⚠ 隔夜费无法推算（{e}），--allow-zero-swap 显式按 0 计"
                      "（已写进 manifest）")
            else:
                raise ValueError(
                    f"{symbol} 隔夜费未知（calibrated.json 无实测、快照推算失败：{e}）。"
                    "先跑 scripts/calibrate_costs.py / scripts/snapshot_specs.py，"
                    "或显式 --allow-zero-swap（会记录在 manifest）") from e

    costs = Costs(spread=spread, slippage_model=slip, commission=commission,
                  swap=swap)
    print(f"✓ 成本：tick {len(ticks):,} 条实测点差（{mode_tag} p90"
          f"{'×成交价' if mode_tag == 'rel' else ''}）；{comm_tag}；{swap_tag}")
    return costs, f"tick-spread-p90[{mode_tag}]+slip{spread_fraction}-{comm_tag}-{swap_tag}"


def load_strategy(path: str):
    """按 "module:Class" 装载策略类。"""
    try:
        module_name, class_name = path.split(":", 1)
        mod = importlib.import_module(module_name)
        return getattr(mod, class_name)
    except (ValueError, ImportError, AttributeError) as e:
        raise ValueError(f"无法装载策略 {path!r}：{e}") from e


def load_series(data_root, symbol, timeframe, end):
    """读序列：经锁箱加载器（end 越过 cutoff 直接报错，绝不静默截断）。

    end 语义与 research_bars 一致：保留 time <= end 的行（含端点）——
    注意 end 时刻"开盘"的那根 bar 会整根纳入（含其整段 OHLC）；
    做切分严谨的样本外研究时请传 bar 的收盘时刻。
    """
    import pandas as pd

    end_ts = pd.Timestamp(end) if end else None
    if end_ts is not None and end_ts.tzinfo is not None:
        raise ValueError("--end 只接受不带时区的 UTC 时间（如 2026-09-01）")
    df = research_bars(data_root, symbol, timeframe, end=end_ts)
    if df is None or len(df) == 0:
        suffix = (f"（--end {end} 截断后为空或序列不存在）" if end
                  else "（先跑 scripts/download_bars.py）")
        raise ValueError(f"无可用数据：{symbol} {timeframe}{suffix}")
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 A 层回测并出标准报告")
    ap.add_argument("--strategy", required=True,
                    help="module:Class，如 mt5quant.strategies.library.buy_hold:BuyHold")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", required=True,
                    help="M1/H1/D1 等（TIMEFRAME_SECONDS 之一）")
    ap.add_argument("--params", default="{}", help='JSON 字符串，如 \'{"k":1}\'')
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--end", default=None,
                    help="只用该 UTC 时刻及之前的数据（naive UTC；越过锁箱 cutoff 会报错）")
    ap.add_argument("--out", default="data/reports/backtests")
    ap.add_argument("--volume-lots", type=float, default=1.0)
    ap.add_argument("--initial-capital", type=float, default=10_000.0)
    ap.add_argument("--costs", choices=("auto", "zero"), default="auto",
                    help="auto=本地 tick 实测点差+滑点（佣金/隔夜未配置按 0 并说明）；zero=零成本")
    ap.add_argument("--spread-fraction", type=float, default=0.1,
                    help="--costs auto 时的滑点比例（×p90 点差）")
    ap.add_argument("--spread-mode", choices=("relative", "absolute"),
                    default="relative",
                    help="点差口径：relative=(ask−bid)/mid 按小时 p90，回测按成交基准价"
                         "缩放（R1 默认）；absolute=旧口径（价格单位，忽略 price）")
    ap.add_argument("--commission", type=float, default=0.0,
                    help="每手来回佣金（账户货币），--costs auto 时无 calibrated 实测则用此值")
    ap.add_argument("--engine", choices=("a", "b"), default="a",
                    help="a=向量化；b=tick 级事件驱动（需 tick 库覆盖回测区间）")
    ap.add_argument("--latency-ms", type=int, default=0,
                    help="--engine b：信号 bar 收盘后的执行延迟（毫秒）")
    ap.add_argument("--slippage-price", type=float, default=0.0,
                    help="--engine b：成交价叠加的额外不利滑点（价格单位）")
    ap.add_argument("--allow-zero-swap", action="store_true",
                    help="隔夜费推算不出时显式按 0 计（记入 manifest；默认报错）")
    ap.add_argument("--contract-size", type=float, default=None,
                    help="合约大小覆盖；缺省读 {data-root}/specs/latest.json")
    ap.add_argument("--cost-version", default=None,
                    help="成本模型版本（写进 manifest；缺省按 --costs 自动生成）")
    args = ap.parse_args()

    if args.timeframe.upper() not in TIMEFRAME_SECONDS:
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
        bars = load_series(args.data_root, args.symbol, args.timeframe.upper(),
                           args.end)
        if args.costs == "auto":
            costs, cost_version = build_costs(
                args.data_root, args.symbol, args.spread_fraction,
                args.commission, allow_zero_swap=args.allow_zero_swap,
                spread_mode=args.spread_mode)
        else:
            costs, cost_version = ZeroCosts(), "zero-v0"
        if args.cost_version:
            cost_version = args.cost_version
        if args.engine == "b":
            result = run_backtest_ticks(
                strategy_cls(), bars,
                lambda start_msc, end_msc: load_ticks(
                    args.data_root, args.symbol,
                    start_msc=start_msc, end_msc=end_msc),
                costs, symbol=args.symbol, params=params,
                contract_size=contract_size,
                volume_lots=args.volume_lots,
                initial_capital=args.initial_capital,
                timeframe=args.timeframe.upper(),
                latency_ms=args.latency_ms,
                slippage_price=args.slippage_price,
            )
        else:
            result = run_backtest(
                strategy_cls(), bars, costs, symbol=args.symbol,
                params=params, contract_size=contract_size,
                volume_lots=args.volume_lots,
                initial_capital=args.initial_capital,
                timeframe=args.timeframe.upper(),
            )
        summary = summarize(result, cost_version=cost_version)
        run_dir = make_run_dir(args.out, args.symbol, args.timeframe.upper())
        report_path = write_report(
            run_dir, result, cost_version=cost_version, strategy=args.strategy,
            extra={"end": args.end, "costs": args.costs,
                   "spread_fraction": args.spread_fraction,
                   "spread_mode": args.spread_mode,
                   "commission_per_lot": args.commission,
                   "allow_zero_swap": args.allow_zero_swap,
                   "engine": args.engine, "latency_ms": args.latency_ms,
                   "slippage_price": args.slippage_price})
    except Exception as e:  # fail-fast：任何错误如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1

    print(f"✓ {args.symbol} {args.timeframe.upper()} "
          f"bars={len(result.bars)} trades={summary['trade_count']} "
          f"合约大小={contract_size:g} 净利={summary['net_profit']:.2f} "
          f"回撤={summary['max_drawdown']:.2%}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
