"""G0 前视关卡：截断测试（复用 backtest.truncation）+ 窗口不变性 + 静态扫描。

三道互补防线：
1. 截断测试（backtest.truncation.check_no_lookahead）：任意前缀 bars[:c] 的
   信号与全量信号在重叠段一致——抓"用到下一根"（负向 shift、未来函数）。
2. 窗口不变性（新，check_window_invariance）：任取 t，用 bars[t−L+1..t]
   （L = 策略声明的 lookback_bars）算出的**末位**信号必须等于全量信号第
   t 位——抓"全史统计量"（EMA 从第一根递推、全样本标准化、声明回看不足：
   实盘信号运行器只备 L 根，截窗与全量不一致 = 声明与现实不符）。
3. 静态扫描（scan_forbidden_patterns）：策略源码文本里的高危写法——
   负向 `shift(`、`center=True`、以及不在滚动窗口内的 `mean()/std()`
   （全样本标准化）。启发式首道滤网，漏报/误报都可能，真正的判定靠 1、2。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import numpy as np
import pandas as pd

from mt5quant.backtest.truncation import check_no_lookahead

# 窗口内合法的滚动/递推算子：同一行出现即视为 mean/std 有界
_WINDOWED = ("rolling(", "ewm(", "expanding(")


def check_window_invariance(strategy, bars: pd.DataFrame, params: dict, *,
                            lookback: int | None = None,
                            probes: int = 8) -> list[int]:
    """窗口不变性检查：返回违规的 t 索引列表（空 = 通过）。

    做法：在 [max(L−1,0), n−1] 上均匀取 probes 个 t，对每个 t 用
    bars.iloc[t−L+1 : t+1] 重算信号，末位值必须与全量信号第 t 位一致
    （NaN 与 NaN 相等；数值比较带容差）。L 缺省取
    strategy.required_data.lookback_bars（0 视为 1：至少要有一根 bar）；
    L ≥ n 时无有效探针，返回空列表（调用方按 probes_run=0 如实记录）。
    """
    strategy.validate_params(dict(params))
    if lookback is None:
        lookback = int(getattr(strategy.required_data, "lookback_bars", 0) or 0)
    lookback = max(lookback, 1)
    n = len(bars)
    lo = lookback - 1
    if n <= lo or probes < 1:
        return []
    full = pd.Series(strategy.generate_signals(bars, params))
    full_vals = full.astype("float64").to_numpy()
    cuts = sorted({int(round(t)) for t in np.linspace(lo, n - 1, probes)})
    violations: list[int] = []
    for t in cuts:
        t = max(lo, min(t, n - 1))
        window = bars.iloc[t - lookback + 1: t + 1]
        tail = pd.Series(strategy.generate_signals(window, params))
        if len(tail) == 0:
            violations.append(t)
            continue
        a = float(tail.iloc[-1])
        b = float(full_vals[t])
        same = (np.isnan(a) and np.isnan(b)) or np.isclose(a, b, rtol=1e-8,
                                                           atol=1e-8)
        if not same:
            violations.append(t)
    return sorted(set(violations))


def scan_forbidden_patterns(source: str) -> list[dict]:
    """静态扫描源码文本（启发式），返回 [{line, pattern, text}]。

    模式：negative_shift（shift(负)）、center_true（滚动居中=用未来）、
    full_sample_stat（不在 rolling/ewm/expanding 链上的 mean()/std()，
    即全样本统计量）。注释行（# 之后）忽略。
    """
    findings: list[dict] = []
    for lineno, raw in enumerate(source.splitlines(), start=1):
        line = raw.split("#", 1)[0]
        if re.search(r"\.shift\s*\(\s*-", line):
            findings.append({"line": lineno, "pattern": "negative_shift",
                             "text": raw.strip()})
        if re.search(r"center\s*=\s*True", line):
            findings.append({"line": lineno, "pattern": "center_true",
                             "text": raw.strip()})
        has_stat = re.search(r"\.(mean|std)\s*\(", line)
        if has_stat and not any(op in line for op in _WINDOWED):
            findings.append({"line": lineno, "pattern": "full_sample_stat",
                             "text": raw.strip()})
    return findings


def static_scan(strategy, *, source: str | None = None) -> list[dict]:
    """扫描策略源码（缺省 = 策略类所在模块文件全文）；source 可注入。"""
    if source is None:
        cls = type(strategy)
        path = inspect.getsourcefile(cls)
        if path is None:                    # 动态定义的类：退化为类体源码
            source = inspect.getsource(cls)
        else:
            source = Path(path).read_text(encoding="utf-8")
    return scan_forbidden_patterns(source)


def run_g0(strategy, bars: pd.DataFrame, params: dict, *, probes: int = 5,
           source: str | None = None) -> dict:
    """G0 组合：截断 + 窗口不变性 + 静态扫描，全部干净才通过。"""
    truncation = check_no_lookahead(strategy, bars, params=params,
                                    probe_points=probes)
    window = check_window_invariance(strategy, bars, params, probes=probes)
    static = static_scan(strategy, source=source)
    passed = not (truncation or window or static)
    return {
        "passed": passed,
        "truncation_violations": truncation,
        "window_violations": window,
        "static_findings": static,
        "lookback_bars": int(getattr(strategy.required_data,
                                     "lookback_bars", 0) or 0),
        "probes": int(probes),
    }
