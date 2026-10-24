"""G0 前视关卡：截断（复用）+ 窗口不变性（新）+ 静态扫描禁用写法。

计划要求的两类坏例（必须被窗口不变性抓出）：
- EMA 全史计算（ewm 从窗口起点递推，截窗重算的末位信号不同）；
- lookback 声明不足（实际用 rolling(30) 却声明 10）。
"""
import math

import pandas as pd

from mt5quant.strategies import BaseStrategy, BuyHold, RequiredData
from mt5quant.validation.lookahead import (
    check_window_invariance,
    run_g0,
    scan_forbidden_patterns,
    static_scan,
)


def wiggle_bars(n=600):
    """确定性行情：前 560 根围绕 100 摆动，末 40 根冲高 160 后回落到 130。

    末位 close=130 落在"全史 EMA（≈123，被高位拉起但记忆长）"之上、
    "20 根窗口 EMA（≈145，只看见冲高段）"之下——两种口径的信号必然相反。
    """
    closes = [100.0 + 2.0 * math.sin(i / 7.0) for i in range(min(n, 560))]
    for k in range(max(0, n - 560)):
        closes.append(100.0 + 3.0 * k if k < 20 else 160.0 - 1.5 * (k - 19))
    if n > 560:
        closes[-1] = 130.0
    times = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({
        "time": times,
        "open": [closes[0]] + closes[:-1],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "tick_volume": 1, "spread": 1, "real_volume": 0,
    })


class EmaFullHistory(BaseStrategy):
    """坏例 1：EMA 全史计算——ewm 从第一根开始递推，窗口截断后末位值不同。"""

    required_data = RequiredData(symbols=(), timeframe="", lookback_bars=20)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        close = bars["close"]
        ema = close.ewm(span=60, adjust=False).mean()
        return (close > ema).astype("int64").set_axis(bars.index)


class UnderDeclaredLookback(BaseStrategy):
    """坏例 2：声明 lookback=10，实际用 rolling(30)——截到 10 根时 SMA 是 NaN。"""

    required_data = RequiredData(symbols=(), timeframe="", lookback_bars=10)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        close = bars["close"]
        return (close > close.rolling(30).mean()).astype("int64").set_axis(bars.index)


class CausalSma(BaseStrategy):
    """因果对照：rolling(10) 且声明 lookback=10——窗口恰好够。"""

    required_data = RequiredData(symbols=(), timeframe="", lookback_bars=10)

    def generate_signals(self, bars: pd.DataFrame, params: dict) -> pd.Series:
        close = bars["close"]
        return (close > close.rolling(10).mean()).astype("int64").set_axis(bars.index)


# ---------------------------------------------------------------- 窗口不变性

def test_causal_sma_passes_window_invariance():
    assert check_window_invariance(CausalSma(), wiggle_bars(), {}) == []


def test_buy_hold_passes_window_invariance():
    assert check_window_invariance(BuyHold(), wiggle_bars(), {}) == []


def test_ema_full_history_is_caught():
    """EMA 全史计算：截窗重算的末位信号 ≠ 全量信号（计划失败测试 1a）。"""
    bad = check_window_invariance(EmaFullHistory(), wiggle_bars(), {})
    assert bad, "EMA 全史计算必须被窗口不变性抓出"
    assert all(0 <= t < 600 for t in bad)


def test_under_declared_lookback_is_caught():
    """声明 lookback=10 但实际用 rolling(30)：探针落在 t≥30 处必露馅（失败测试 1b）。"""
    bad = check_window_invariance(UnderDeclaredLookback(), wiggle_bars(), {})
    assert bad


def test_explicit_lookback_overrides_declared():
    """给足 lookback=30 时，rolling(30) 策略通过（坏例只坏在声明不足）。"""
    assert (check_window_invariance(UnderDeclaredLookback(), wiggle_bars(), {},
                                     lookback=30, probes=6) == [])


def test_window_invariance_rejects_negative_shift_too():
    class PeekNext(BaseStrategy):
        def generate_signals(self, bars, params):
            close = bars["close"]
            return (close.shift(-1) > close).astype("int64").set_axis(bars.index)

    assert check_window_invariance(PeekNext(), wiggle_bars(), {})


# ---------------------------------------------------------------- 静态扫描

def test_scan_flags_negative_shift():
    src = "sig = (close.shift(-1) > close).astype('int64')\n"
    findings = scan_forbidden_patterns(src)
    assert [f["pattern"] for f in findings] == ["negative_shift"]


def test_scan_flags_center_true():
    src = "m = close.rolling(10, center=True).mean()\n"
    assert [f["pattern"] for f in scan_forbidden_patterns(src)] == ["center_true"]


def test_scan_flags_full_sample_normalization():
    src = "z = (close - close.mean()) / close.std()\n"
    assert [f["pattern"] for f in scan_forbidden_patterns(src)] == ["full_sample_stat"]


def test_scan_allows_windowed_stats():
    src = ("m = close.rolling(10).mean()\n"
           "e = close.ewm(span=5).mean()\n"
           "x = close.expanding(5).std()\n")
    assert scan_forbidden_patterns(src) == []


def test_scan_reports_line_numbers():
    src = "a = 1\nb = 2\nc = close.mean()\n"
    findings = scan_forbidden_patterns(src)
    assert len(findings) == 1 and findings[0]["line"] == 3


def test_static_scan_default_reads_strategy_module():
    """缺省扫描策略类所在模块源码：BuyHold 模块干净。"""
    assert static_scan(BuyHold()) == []


def test_static_scan_accepts_injected_source():
    dirty = "s = close - close.mean()\n"
    findings = static_scan(BuyHold(), source=dirty)
    assert [f["pattern"] for f in findings] == ["full_sample_stat"]


# ---------------------------------------------------------------- run_g0 组合

CLEAN_SRC = "def generate_signals(self, bars, params):\n    return bars['close']\n"


def test_run_g0_passes_causal_strategy():
    out = run_g0(CausalSma(), wiggle_bars(), {}, source=CLEAN_SRC)
    assert out["passed"] is True
    assert out["truncation_violations"] == []
    assert out["window_violations"] == []
    assert out["static_findings"] == []


def test_run_g0_fails_on_ema_full_history():
    out = run_g0(EmaFullHistory(), wiggle_bars(), {}, source=CLEAN_SRC)
    assert out["passed"] is False
    assert out["window_violations"]


def test_run_g0_fails_on_static_finding():
    dirty = "z = (close - close.mean()) / close.std()\n"
    out = run_g0(BuyHold(), wiggle_bars(), {}, source=dirty)
    assert out["passed"] is False
    assert out["static_findings"][0]["pattern"] == "full_sample_stat"


def test_run_g0_fails_on_truncation_violation():
    class PeekNext(BaseStrategy):
        def generate_signals(self, bars, params):
            close = bars["close"]
            return (close.shift(-1) > close).astype("int64").set_axis(bars.index)

    out = run_g0(PeekNext(), wiggle_bars(), {}, source=CLEAN_SRC)
    assert out["passed"] is False
    assert out["truncation_violations"]
