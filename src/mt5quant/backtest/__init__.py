"""mt5quant.backtest：A 层逐 bar / B 层逐 tick 回测引擎、标准报告与截断测试工具。"""
from mt5quant.backtest.engine_a import (
    ENGINE_VERSION,
    BacktestResult,
    Costs,
    Trade,
    atr_series,
    run_backtest,
)
from mt5quant.backtest.engine_b import (
    ENGINE_VERSION_B,
    TickCoverageError,
    run_backtest_ticks,
)
from mt5quant.backtest.report import (
    Manifest,
    annualization_factor,
    bars_per_year,
    build_manifest,
    current_git_commit,
    data_sha256,
    make_run_dir,
    observed_bars_per_year,
    render_markdown,
    summarize,
    write_report,
)
from mt5quant.backtest.truncation import check_no_lookahead

__all__ = [
    "ENGINE_VERSION", "BacktestResult", "Costs", "Trade", "atr_series",
    "run_backtest", "ENGINE_VERSION_B", "TickCoverageError", "run_backtest_ticks",
    "Manifest", "annualization_factor", "bars_per_year",
    "build_manifest", "current_git_commit", "data_sha256", "make_run_dir",
    "observed_bars_per_year", "render_markdown", "summarize", "write_report",
    "check_no_lookahead",
]
