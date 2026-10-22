"""纯 pandas 指标计算（不落库、服务端现算——参数变了随时重算）。

口径与 MT5 内置对齐（I15/M8 修复后）：
- RSI/ATR 用 Wilder 平滑，种子 = 前 period 个值的简单平均（此前 period 根为 NaN）；
  全平（涨跌均为 0）时 RSI 取 50（MT5 口径）。
- Bollinger 用总体标准差（ddof=0，MT5 iBands 口径）。
- MACD 信号线用 EMA（Appel 标准；注意 MT5 内置 MACD 用 SMA，口径差异在文档注明）。
"""
from __future__ import annotations

import pandas as pd


def sma(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(period).mean()


def ema(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def _wilder(values: pd.Series, period: int) -> pd.Series:
    """Wilder 平滑：种子=前 period 个有效值的均值，其后按 (n-1)/n 递推。

    实现要点：把种子写入第 period 个有效位置、抹掉其前的原始值，
    再用 ewm(alpha=1/period, adjust=False, ignore_na=True) 从种子起递推。
    """
    vals = values.astype("float64").copy()
    valid = vals.dropna()
    if len(valid) < period:
        return vals * float("nan")  # 数据不足，全部 NaN
    seed_idx = valid.index[period - 1]
    seed = valid.iloc[:period].mean()
    for i in range(period):
        vals[valid.index[i]] = float("nan")
    vals[seed_idx] = float(seed)
    return vals.ewm(alpha=1 / period, adjust=False, ignore_na=True).mean()


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_up = _wilder(up, period)
    avg_down = _wilder(down, period)
    out = 100 - 100 / (1 + avg_up / avg_down)
    out[(avg_up == 0) & (avg_down == 0)] = 50.0   # 全平：MT5 取 50
    out[(avg_down == 0) & (avg_up > 0)] = 100.0   # 无下跌
    return out


def macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(closes, fast) - ema(closes, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def bbands(closes: pd.Series, period: int = 20, std_mult: float = 2.0):
    mid = closes.rolling(period).mean()
    sd = closes.rolling(period).std(ddof=0)  # 总体标准差（MT5 iBands 口径）
    return mid, mid + std_mult * sd, mid - std_mult * sd


def atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> pd.Series:
    prev_close = closes.shift(1)
    tr = pd.concat(
        [highs - lows, (highs - prev_close).abs(), (lows - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return _wilder(tr, period)
