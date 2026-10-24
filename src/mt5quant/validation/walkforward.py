"""G3 walk-forward：滚动训练/测试窗，参数只在卡的网格内重选（P4/T12）。

流程（滚动，非锚定）：
- 数据按 train_bars + k×test_bars 切段；对每段：
  训练窗 [t0, t1) 在网格内按**净利**挑最优参数（选择只用训练数据；
  切片带回看预热条，预热条也在样本内，选择不接触测试数据）；
  测试窗 [t1, t1+test_bars) 用挑出的参数出样本外结果——回看预热条取自
  训练窗末尾（合法：属于样本内数据），引擎从切片起点空仓起步；
- 样本外 Sharpe 由各段测试区权益收益率（每段 test_bars 根）拼接而成
  （年化口径与 report.summarize 一致：实测每年 bar 数，不足退回日历）；
- 通过条件（蓝图附录 B）：样本外夏普 > 0，且 ≥ 样本内夏普 × min_oos_ratio。
"""
from __future__ import annotations

import math

import pandas as pd

from mt5quant.backtest.report import bars_per_year, observed_bars_per_year
from mt5quant.validation.robustness import BacktestSpec, run_engine


def run_g3(spec: BacktestSpec, bars, costs, *, grid, in_sample_sharpe, cfg) -> dict:
    """grid：参数组合列表（卡网格的笛卡尔积，如 [{"k": 2}, {"k": 3}] 或 [{}]）。"""
    strategy = spec.strategy
    lookback = max(int(getattr(strategy.required_data, "lookback_bars", 0) or 0), 0)
    n = len(bars)
    train_bars = int(cfg.walkforward_train_bars)
    test_bars = int(cfg.walkforward_test_bars)
    bpy = observed_bars_per_year(bars["time"]) or bars_per_year(spec.timeframe)

    segments = []
    oos_returns: list[pd.Series] = []
    t1 = train_bars
    while t1 + test_bars <= n:
        train = bars.iloc[max(0, t1 - train_bars - lookback): t1]
        best_params, best_net = None, -math.inf
        for combo in grid:
            trades = run_engine(spec, train, costs, combo).trades
            net = float(trades["net"].sum()) if len(trades) else 0.0
            if net > best_net:
                best_net, best_params = net, dict(combo)
        test = bars.iloc[max(0, t1 - lookback): t1 + test_bars]
        oos = run_engine(spec, test, costs, best_params)
        oos_trades = oos.trades
        tail = oos.equity.astype("float64").iloc[-(test_bars + 1):]
        oos_returns.append(tail.pct_change().dropna())
        segments.append({
            "train_range": [int(max(0, t1 - train_bars)), int(t1)],
            "test_range": [int(t1), int(t1 + test_bars)],
            "test_bars": test_bars,
            "params": best_params,
            "train_net": float(best_net),
            "oos_net": (float(oos_trades["net"].sum())
                        if len(oos_trades) else 0.0),
            "oos_trades": int(len(oos_trades)),
        })
        t1 += test_bars

    if not segments:
        return {"passed": False, "segments": [], "oos_sharpe": None,
                "in_sample_sharpe": in_sample_sharpe,
                "reason": (f"数据不足：{n} 根 < 训练 {train_bars} + 测试 "
                           f"{test_bars}（回看 {lookback}）")}

    all_rets = pd.concat(oos_returns, ignore_index=True)
    oos_sharpe = None
    if len(all_rets) >= 2:
        std = float(all_rets.std(ddof=1))
        if std > 0:
            oos_sharpe = float(all_rets.mean()) / std * math.sqrt(bpy)

    is_sharpe = float(in_sample_sharpe) if in_sample_sharpe is not None else None
    min_oos = None if is_sharpe is None else is_sharpe * float(cfg.walkforward_min_oos_ratio)
    passed = bool(oos_sharpe is not None and oos_sharpe > 0
                  and is_sharpe is not None
                  and (is_sharpe <= 0 or oos_sharpe >= min_oos))
    return {
        "passed": passed,
        "segments": segments,
        "oos_sharpe": oos_sharpe,
        "in_sample_sharpe": is_sharpe,
        "min_oos_ratio": float(cfg.walkforward_min_oos_ratio),
        "min_required_oos_sharpe": min_oos,
        "n_test_bars_total": int(sum(len(r) for r in oos_returns)),
    }
