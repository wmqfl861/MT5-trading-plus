"""G0 前视关卡：截断测试（复用 backtest.truncation）+ 窗口不变性 + 静态扫描。

三道互补防线：
1. 截断测试（backtest.truncation.check_no_lookahead）：任意前缀 bars[:c] 的
   信号与全量信号在重叠段一致——抓"用到下一根"（负向 shift、未来函数）。
2. 窗口不变性（check_window_invariance）：任取 t，用 bars[t−L+1..t]
   （L = 策略声明的 lookback_bars）算出的**末位**信号必须等于全量信号第
   t 位——抓"全史统计量"（EMA 从第一根递推、全样本标准化、声明回看不足：
   实盘信号运行器只备 L 根，截窗与全量不一致 = 声明与现实不符）。
   T25（M3）：默认 100 个**随机**探针（5 个均匀探针对路径依赖状态机——
   B1/B4 的持有计数、B2/B3 的粘性——证明力不足）；种子由卡 id 派生
   （zlib.crc32，g0_seed），同卡同种子可复现，probes 可覆盖
   （bt CLI 的 --probes 透传）。另加 check_window_invariance_sweep：对最近
   一段连续 500 根逐根比对窗口信号与全史信号——连续段上任何路径依赖
   漏检都被逐根暴露，run_g0 默认一并执行。
3. 静态扫描（scan_forbidden_patterns）：策略源码文本里的高危写法——
   负向 `shift(`、`center=True`、以及不在滚动窗口内的 `mean()/std()`
   （全样本标准化）。启发式首道滤网，漏报/误报都可能，真正的判定靠 1、2。
"""
from __future__ import annotations

import inspect
import re
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

from mt5quant.backtest.truncation import check_no_lookahead

# 窗口内合法的滚动/递推算子：同一行出现即视为 mean/std 有界
_WINDOWED = ("rolling(", "ewm(", "expanding(")

# T25（M3）：窗口不变性的默认探针密度与连续比对段长
DEFAULT_WINDOW_PROBES = 100   # 随机探针数（原 5/8 个均匀切点证明力不足）
DEFAULT_SWEEP_SPAN = 500      # 连续逐根比对的段长（根）
DEFAULT_G0_SEED = 0           # 未给 card_id 时的可复现缺省种子


def g0_seed(card_id: str | None) -> int:
    """G0 探针种子：由卡 id 派生（zlib.crc32），同卡同种子 → 探针位置可复现。

    card_id 为 None（无卡上下文，如单测）时用固定缺省种子 DEFAULT_G0_SEED。
    """
    if card_id is None:
        return DEFAULT_G0_SEED
    return zlib.crc32(card_id.encode("utf-8"))


def draw_probe_positions(lo: int, hi: int, probes: int, seed: int) -> list[int]:
    """在闭区间 [lo, hi] 内按种子可复现地随机抽 probes 个位置（去重升序）。

    空区间或 probes < 1 返回空列表。numpy default_rng 种子确定 → 结果确定。
    """
    if probes < 1 or hi < lo:
        return []
    rng = np.random.default_rng(seed)
    drawn = rng.integers(lo, hi + 1, size=int(probes))
    return sorted({int(t) for t in drawn})


def check_window_invariance(strategy, bars: pd.DataFrame, params: dict, *,
                            lookback: int | None = None,
                            probes: int = DEFAULT_WINDOW_PROBES,
                            seed: int = DEFAULT_G0_SEED) -> list[int]:
    """窗口不变性检查：返回违规的 t 索引列表（空 = 通过）。

    做法（T25/M3）：在 [max(L−1,0), n−1] 上按种子随机取 probes 个 t，对每个
    t 用 bars.iloc[t−L+1 : t+1] 重算信号，末位值必须与全量信号第 t 位一致
    （NaN 与 NaN 相等；数值比较带容差）。L 缺省取
    strategy.required_data.lookback_bars（0 视为 1：至少要有一根 bar）；
    L ≥ n 时无有效探针，返回空列表（调用方按 probes_run=0 如实记录）。
    种子缺省 DEFAULT_G0_SEED；run_g0 侧用 g0_seed(card_id) 派生。
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
    cuts = draw_probe_positions(lo, n - 1, probes, seed)
    violations: list[int] = []
    for t in cuts:
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


def check_window_invariance_sweep(strategy, bars: pd.DataFrame, params: dict, *,
                                  lookback: int | None = None,
                                  span: int = DEFAULT_SWEEP_SPAN) -> list[int]:
    """连续逐根比对（T25/M3）：对最近一段连续 span 根 bar 的**每个** t，
    用 bars[t−L+1..t] 重算信号末位并与全量信号第 t 位比对，返回违规列表。

    与随机探针互补：路径依赖状态机（持有计数、粘性状态）的违规可能恰好
    落在随机探针之间，连续段逐根比对无死角。区间为
    [max(L−1, n−span), n−1]（n < span 时用可得区间）；L ≥ n 时返回空列表。
    """
    strategy.validate_params(dict(params))
    if lookback is None:
        lookback = int(getattr(strategy.required_data, "lookback_bars", 0) or 0)
    lookback = max(lookback, 1)
    n = len(bars)
    lo = lookback - 1
    if n <= lo or span < 1:
        return []
    full = pd.Series(strategy.generate_signals(bars, params))
    full_vals = full.astype("float64").to_numpy()
    violations: list[int] = []
    for t in range(max(lo, n - span), n):
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
    return violations


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


def run_g0(strategy, bars: pd.DataFrame, params: dict, *,
           probes: int = DEFAULT_WINDOW_PROBES,
           source: str | None = None, card_id: str | None = None,
           sweep_span: int = DEFAULT_SWEEP_SPAN) -> dict:
    """G0 组合：截断 + 窗口不变性（100 随机探针，种子 = g0_seed(card_id)）
    + 连续 sweep_span 根逐根比对 + 静态扫描，全部干净才通过。

    card_id（研究卡 id）：探针种子来源，同卡可复现（T25/M3）；缺省
    DEFAULT_G0_SEED。probes 覆盖默认探针数（bt 的 --probes 透传）。
    签名向后兼容：run_g0(strategy, bars, params) 照常工作。
    """
    seed = g0_seed(card_id)
    truncation = check_no_lookahead(strategy, bars, params=params,
                                    probe_points=probes)
    window = check_window_invariance(strategy, bars, params, probes=probes,
                                     seed=seed)
    sweep = check_window_invariance_sweep(strategy, bars, params,
                                          span=sweep_span)
    static = static_scan(strategy, source=source)
    passed = not (truncation or window or sweep or static)
    return {
        "passed": passed,
        "truncation_violations": truncation,
        "window_violations": window,
        "window_sweep_violations": sweep,
        "static_findings": static,
        "lookback_bars": int(getattr(strategy.required_data,
                                     "lookback_bars", 0) or 0),
        "probes": int(probes),
        "seed": int(seed),
        "sweep_span": int(sweep_span),
    }
