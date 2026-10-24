"""截断测试：用前缀数据重算信号，与全量信号在重叠段比对，查前视偏差。

原理：因果策略（只用已收盘数据）对任意前缀 bars[:c] 产生的信号，
必然与全量信号在位置 0..c-1 上完全一致；任何不一致都意味着该位置的
信号依赖了未来数据（负向 shift、未来函数、全局统计量泄露等）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def check_no_lookahead(strategy, bars: pd.DataFrame, *, params: dict,
                       probe_points: int = 5) -> list[int]:
    """返回前视违规的位置索引列表（空列表 = 通过）。

    做法：在 [1, n-1] 上均匀取 probe_points 个切点 c，分别用 bars.iloc[:c]
    重算信号，与全量信号在重叠段逐位比对（NaN 与 NaN 视为相等，
    数值比较带 1e-8 相对/绝对容差）。切点 c 处前缀缺下一根 bar，
    恰好是 shift(-1) 这类"用到下一根"的前视最容易暴露的位置。
    """
    strategy.validate_params(dict(params))
    n = len(bars)
    if n < 2 or probe_points < 1:
        return []
    full = pd.Series(strategy.generate_signals(bars, params))
    full_vals = full.astype("float64").to_numpy()
    cuts = sorted({
        int(round(c))
        for c in np.linspace(1, n - 1, probe_points)
    })
    violations: list[int] = []
    for c in cuts:
        c = max(1, min(c, n - 1))
        prefix_vals = pd.Series(
            strategy.generate_signals(bars.iloc[:c], params)
        ).astype("float64").to_numpy()
        if len(prefix_vals) != c:  # 长度都对不上：整个前缀按违规处理
            violations.extend(range(c))
            continue
        ok = np.isclose(prefix_vals, full_vals[:c], rtol=1e-8, atol=1e-8,
                        equal_nan=True)
        violations.extend(int(i) for i in np.where(~ok)[0])
    return sorted(set(violations))
