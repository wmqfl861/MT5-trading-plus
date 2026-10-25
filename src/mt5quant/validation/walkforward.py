"""G3 walk-forward（T21b/H5）：**日历月**滚动训练/测试窗，参数只在卡的网格内重选。

窗口口径（config/gates.toml [g3]，随门槛冻结）：
- H1 及以上：训练 train_months_hourly（24）个月、测试 test_months_hourly（6）个月；
- 日内（< 1 小时）：12 / 3 个月——旧实现按 bar 数固定（2000/500 根），
  M5 的测试窗只有约 2.4 天，样本外夏普基本是噪声（审核 H5/探针 P5）；
- 滚动步长 = 测试月数；最后一段的测试窗可被数据末尾截短（按实际 bar 计）。

流程（滚动，非锚定），每段：
- 训练窗 [train_start, train_end) 在网格内按**净利**挑最优参数（选择只用
  训练数据；切片带回看预热条，预热条也在样本内，选择不接触测试数据）；
- 测试窗 [test_start, test_end) 用挑出的参数出样本外结果——回看预热条取自
  训练窗末尾（合法：属于样本内数据），引擎从切片起点空仓起步；
- oos_net / oos_trades **只计 entry_time ≥ 测试窗起点的交易**：预热段里
  （测试窗起点之前）发生的交易不计入样本外统计（H5）；
- 样本外 Sharpe 由各段测试窗权益收益率拼接（年化口径与 report.summarize
  一致：实测每年 bar 数，不足退回日历）。口径差（L4）：测试窗收益取自
  权益曲线，**含预热段开仓的持仓在测试窗内的浮动盈亏**（持仓跨越测试窗
  起点时），与 oos_trades（只计测试窗入场）不同——影响小，读报告时留意；
- 通过条件（蓝图附录 B）：样本外夏普 > 0，且 ≥ 样本内夏普 × min_oos_ratio。
"""
from __future__ import annotations

import math

import pandas as pd

from mt5data.store import TIMEFRAME_SECONDS
from mt5quant.backtest.report import bars_per_year, observed_bars_per_year
from mt5quant.validation.robustness import BacktestSpec, run_engine


def month_window_spec(timeframe: str, cfg) -> tuple[int, int]:
    """(train_months, test_months)：≥ H1 用小时档，< H1 用日内档（gates.toml）。"""
    tf = timeframe.upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"未知 timeframe：{timeframe!r}")
    if TIMEFRAME_SECONDS[tf] >= 3600:
        return int(cfg.train_months_hourly), int(cfg.test_months_hourly)
    return int(cfg.train_months_intraday), int(cfg.test_months_intraday)


def _month_index(ts) -> int:
    """绝对月序号：year*12 + (month-1)（日历月运算不用日期差）。"""
    return int(ts.year) * 12 + int(ts.month) - 1


def _month_start(m: int) -> pd.Timestamp:
    return pd.Timestamp(year=m // 12, month=m % 12 + 1, day=1)


def plan_windows(times, *, train_months: int, test_months: int) -> list[dict]:
    """日历月滚动窗计划（纯函数，供测试与诊断）。

    返回每段 {"train_start", "train_end", "test_start", "test_end"}（
    Timestamp 边界，左闭右开；train_end == test_start）。段起点从首根 bar
    所在月的月初开始，滚动步长 = test_months；收录条件：测试窗的最后一个
    月不晚于最后一根 bar 所在月（末段测试窗可被数据末尾截短）。
    """
    if train_months < 1 or test_months < 1:
        raise ValueError(
            f"train_months/test_months 必须 ≥1，收到 {train_months}/{test_months}")
    t = pd.Series(times)
    if not pd.api.types.is_datetime64_any_dtype(t):
        t = pd.to_datetime(t)
    if len(t) == 0:
        return []
    m_first = _month_index(t.iloc[0])
    m_last = _month_index(t.iloc[-1])
    out: list[dict] = []
    start = m_first
    while start + train_months + test_months <= m_last + 1:
        out.append({
            "train_start": _month_start(start),
            "train_end": _month_start(start + train_months),
            "test_start": _month_start(start + train_months),
            "test_end": _month_start(start + train_months + test_months),
        })
        start += test_months
    return out


def _span_months(times) -> int:
    return _month_index(times.iloc[-1]) - _month_index(times.iloc[0]) + 1


def run_g3(spec: BacktestSpec, bars, costs, *, grid, in_sample_sharpe, cfg) -> dict:
    """grid：参数组合列表（卡网格的笛卡尔积，如 [{"k": 2}, {"k": 3}] 或 [{}]）。"""
    strategy = spec.strategy
    lookback = max(int(getattr(strategy.required_data, "lookback_bars", 0) or 0), 0)
    n = len(bars)
    train_m, test_m = month_window_spec(spec.timeframe, cfg)
    windows = plan_windows(bars["time"], train_months=train_m, test_months=test_m)
    bpy = observed_bars_per_year(bars["time"]) or bars_per_year(spec.timeframe)
    times = bars["time"].reset_index(drop=True)

    segments = []
    oos_returns: list[pd.Series] = []
    for w in windows:
        t0 = int(times.searchsorted(w["train_start"], side="left"))
        t1 = int(times.searchsorted(w["test_start"], side="left"))
        t2 = int(times.searchsorted(w["test_end"], side="left"))
        if t1 <= t0 or t2 <= t1:
            continue    # 该窗整段落进数据缺口（整月无 bar）→ 无法评估，跳过
        train = bars.iloc[max(0, t0 - lookback): t1]
        best_params, best_net = None, -math.inf
        for combo in grid:
            trades = run_engine(spec, train, costs, combo).trades
            net = float(trades["net"].sum()) if len(trades) else 0.0
            if net > best_net:
                best_net, best_params = net, dict(combo)
        test = bars.iloc[max(0, t1 - lookback): t2]
        oos = run_engine(spec, test, costs, best_params)
        test_start = times.iloc[t1]
        in_window = (pd.to_datetime(oos.trades["entry_time"]) >= test_start)
        oos_trade_count = int(in_window.sum())
        oos_net = (float(oos.trades.loc[in_window, "net"].sum())
                   if oos_trade_count else 0.0)
        tail = oos.equity.astype("float64").iloc[-(t2 - t1 + 1):]
        oos_returns.append(tail.pct_change().dropna())
        segments.append({
            "train_range": [t0, t1],
            "test_range": [t1, t2],
            "train_start": w["train_start"].isoformat(),
            "test_start": w["test_start"].isoformat(),
            "test_end": w["test_end"].isoformat(),
            "test_bars": int(t2 - t1),
            "train_months": train_m,
            "test_months": test_m,
            "params": best_params,
            "train_net": float(best_net),
            "oos_net": oos_net,
            "oos_trades": oos_trade_count,
        })

    if not segments:
        return {"passed": False, "segments": [], "oos_sharpe": None,
                "in_sample_sharpe": in_sample_sharpe,
                "reason": (f"数据不足：{n} 根、跨 {_span_months(times)} 个日历月"
                           f" < 训练 {train_m} + 测试 {test_m} 个月"
                           f"（回看 {lookback}）")}

    all_rets = pd.concat(oos_returns, ignore_index=True)
    oos_sharpe = None
    if len(all_rets) >= 2:
        std = float(all_rets.std(ddof=1))
        if std > 0:
            oos_sharpe = float(all_rets.mean()) / std * math.sqrt(bpy)

    is_sharpe = float(in_sample_sharpe) if in_sample_sharpe is not None else None
    min_oos = None if is_sharpe is None else is_sharpe * float(cfg.min_oos_ratio)
    passed = bool(oos_sharpe is not None and oos_sharpe > 0
                  and is_sharpe is not None
                  and (is_sharpe <= 0 or oos_sharpe >= min_oos))
    return {
        "passed": passed,
        "segments": segments,
        "oos_sharpe": oos_sharpe,
        "in_sample_sharpe": is_sharpe,
        "min_oos_ratio": float(cfg.min_oos_ratio),
        "min_required_oos_sharpe": min_oos,
        "n_test_bars_total": int(sum(len(r) for r in oos_returns)),
    }
