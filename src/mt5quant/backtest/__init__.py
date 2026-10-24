"""mt5quant.backtest：A 层向量化回测引擎、标准报告与截断测试工具。"""
from mt5quant.backtest.engine_a import (
    BacktestResult,
    Costs,
    Trade,
    atr_series,
    run_backtest,
)
from mt5quant.backtest.report import (
    Manifest,
    bars_per_year,
    current_git_commit,
    data_sha256,
    render_markdown,
    summarize,
    write_report,
)
from mt5quant.backtest.truncation import check_no_lookahead

__all__ = [
    "BacktestResult", "Costs", "Trade", "atr_series", "run_backtest",
    "Manifest", "bars_per_year", "current_git_commit", "data_sha256",
    "render_markdown", "summarize", "write_report", "check_no_lookahead",
]
