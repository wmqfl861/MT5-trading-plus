"""纯 pandas 指标计算（不落库、服务端现算——参数变了随时重算）。"""
from __future__ import annotations

import pandas as pd


def sma(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(period).mean()


def ema(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder 平滑 RSI；首值 NaN，其后与手算递推一致。"""
    delta = closes.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_up = up.ewm(alpha=1 / period, adjust=False).mean()
    avg_down = down.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_up / avg_down
    out = 100 - 100 / (1 + rs)
    out[avg_down == 0] = 100.0  # 无下跌 → 满值
    return out


def macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(closes, fast) - ema(closes, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def bbands(closes: pd.Series, period: int = 20, std_mult: float = 2.0):
    mid = closes.rolling(period).mean()
    sd = closes.rolling(period).std(ddof=1)
    return mid, mid + std_mult * sd, mid - std_mult * sd


def atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> pd.Series:
    prev_close = closes.shift(1)
    tr = pd.concat(
        [highs - lows, (highs - prev_close).abs(), (lows - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()
