"""A 层回测 CLI。

示例：
    .venv/Scripts/python.exe scripts/run_backtest.py \
        --strategy mt5quant.strategies.library.buy_hold:BuyHold \
        --symbol XAUUSD --timeframe H1 --params '{}' \
        --data-root data --end 2026-09-01 --out data/reports/backtests

约定：
- --end 经 mt5data.lockbox.research_bars（锁箱语义：显式越过 cutoff 的请求
  直接报错，绝不静默截断）；无 lockbox.json 时返回全量，行为与直读一致。
- 成本：--costs auto（默认）用 {data-root}/ticks 的实测点差+滑点
  （mt5quant.costs.Costs，佣金/隔夜费未配置时如实为 0 并打印说明）；
  --costs zero 用零成本占位。
"""
import argparse
import glob
import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mt5data.lockbox import research_bars  # noqa: E402
from mt5data.store import TIMEFRAME_SECONDS  # noqa: E402
from mt5quant.backtest.engine_a import run_backtest  # noqa: E402
from mt5quant.backtest.report import summarize, write_report  # noqa: E402


class ZeroCosts:
    """零成本占位：实现 engine_a.Costs Protocol。"""

    def spread_p90(self, symbol, hour_utc):
        return 0.0

    def slippage(self, symbol, hour_utc):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0

    def overnight_fee(self, symbol, side, from_ts, to_ts):
        return 0.0


def build_costs(data_root: str, symbol: str, spread_fraction: float):
    """从本地 tick 实测构建成本（佣金/隔夜未配置 → 0，如实打印说明）。"""
    import pandas as pd

    from mt5quant.costs import (
        CommissionModel, Costs, SlippageModel, SpreadModel, SwapModel,
        SwapRule,
    )

    d = os.path.join(data_root, "ticks", symbol)
    files = sorted(glob.glob(os.path.join(d, "*.parquet"))) if os.path.isdir(d) else []
    if not files:
        print(f"⚠ {symbol} 无 tick 数据（{d}），退回零成本；"
              f"先跑 scripts/download_ticks.py 可启用实测点差/滑点")
        return ZeroCosts(), "zero-v0(no-ticks)"
    ticks = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    spread = SpreadModel.from_ticks(symbol, ticks)
    slip = SlippageModel(spread, spread_fraction=spread_fraction)
    costs = Costs(
        spread=spread, slippage_model=slip,
        commission=CommissionModel(default_per_lot=0.0),
        # 未取到账户 swap 规则前如实按 0 计（零费率规则，仍走真实模型路径）
        swap=SwapModel(per_symbol={
            symbol: SwapRule(long_per_day=0.0, short_per_day=0.0)}),
    )
    print(f"✓ 成本：tick {len(ticks):,} 条实测点差（p90 口径）；"
          f"佣金/隔夜费未配置按 0 计（账户佣金与 swap 规则待 P5 配置）")
    return costs, f"tick-spread-p90+slip{spread_fraction}-comm0-swap0"


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
        bars = load_series(args.data_root, args.symbol, args.timeframe.upper(),
                           args.end)
        if args.costs == "auto":
            costs, cost_version = build_costs(args.data_root, args.symbol,
                                              args.spread_fraction)
        else:
            costs, cost_version = ZeroCosts(), "zero-v0"
        if args.cost_version:
            cost_version = args.cost_version
        result = run_backtest(
            strategy_cls(), bars, costs, symbol=args.symbol,
            params=params, volume_lots=args.volume_lots,
            initial_capital=args.initial_capital,
            timeframe=args.timeframe.upper(),
        )
        summary = summarize(result, cost_version=cost_version)
        report_path = write_report(args.out, result, cost_version=cost_version)
    except Exception as e:  # fail-fast：任何错误如实报出
        print(f"✗ {type(e).__name__}: {e}")
        return 1

    print(f"✓ {args.symbol} {args.timeframe.upper()} "
          f"bars={len(result.bars)} trades={summary['trade_count']} "
          f"净利={summary['net_profit']:.2f} "
          f"回撤={summary['max_drawdown']:.2%}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
