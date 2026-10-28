"""P 线审核一、二的复现探针（2026-10-28）。

用法（仓库根目录）：
    ./.venv/Scripts/python.exe docs/reviews/probes/2026-10-28-pline-probes.py

四个探针，全部只用合成数据或测试夹具，不读研究库、不看任何真实局面结局：
- P1 概率表平均净收益收缩（审核一 F1）：与基准同分布的格，r_win 应为 1.950、EV(z=0) 应为 +0.2100R
- P2 purge 跨周末（审核一 F2）：周五 18:00 触发、train_end 在周六，应被剔除
- P3 接近价位定义（审核一 F3）：3 万根随机游走 H1、网格 5.0，阻力事件应为 117 个、事件 K 线中位冲高 0.89 ATR
- P4 预筛成本逐品种（审核二 Q1）：两品种成本 0.005R 与 0.05R、ΔEV 0.06R，成本条件应判不通过

任一探针不符合预期时退出码为 1。
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from mt5quant.backtest.engine_a import atr_series  # noqa: E402
from mt5quant.prob.decision import ev_lower  # noqa: E402
from mt5quant.prob.labels import LOOKBACK_BARS, Bracket, label_triggers  # noqa: E402
from mt5quant.prob.tables import ProbTable  # noqa: E402
from mt5quant.research import candidates as cand  # noqa: E402
from mt5quant.strategies.library.prob.situations import (  # noqa: E402
    Approach, find_level_events, s1_level_rows,
)

import test_prob_labels as TL  # noqa: E402
import test_prob_precheck as TP  # noqa: E402

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))


# ---------------------------------------------------------------- P1
rows = [("win", 1.95)] * 400 + [("loss", -1.05)] * 550 + [("time", 0.15)] * 50
labels = pd.DataFrame({"situation": "S1", "action": "A3",
                       "outcome": [o for o, _ in rows], "net_r": [r for _, r in rows]})
tbl = ProbTable.fit(labels, group_by=[], prior_strength=100, min_cell=100)
cell = tbl.lookup("S1", "A3", {})
ev = ev_lower(cell, z=0.0)
true_ev = 0.40 * 1.95 + 0.55 * (-1.05) + 0.05 * 0.15
record("P1 收缩公式", abs(cell.r_win - 1.95) < 1e-9 and abs(ev - true_ev) < 1e-9,
       f"r_win={cell.r_win:.3f}（应 1.950），EV={ev:+.4f}R（应 {true_ev:+.4f}R）")

# ---------------------------------------------------------------- P2
rng = np.random.default_rng(7)
idx = pd.date_range("2024-01-01", "2024-02-10", freq="1h")
idx = idx[idx.dayofweek < 5]
n = len(idx)
close = 100 + np.cumsum(rng.normal(0, 0.35, n))
op = close + rng.normal(0, 0.05, n)
bars = pd.DataFrame({"time": idx, "open": op,
                     "high": np.maximum(op, close) + np.abs(rng.normal(0, 0.2, n)),
                     "low": np.minimum(op, close) - np.abs(rng.normal(0, 0.2, n)),
                     "close": close, "tick_volume": 10, "spread": 5, "real_volume": 0})
t = next(i for i, ts in enumerate(idx)
         if ts.dayofweek == 4 and ts.hour == 18 and i > LOOKBACK_BARS)
K = 24
trig = pd.DataFrame({"time": [idx[t]], "situation": "S1", "action": "A3",
                     "side": [1], "level": [0.0]})
train_end = idx[t] + pd.Timedelta(hours=K + 3)          # 落在周六
out = label_triggers(bars, trig, TL.LIGHT, symbol="XAUUSD", timeframe="H1",
                     bracket=Bracket(sl_atr=50.0, tp_atr=50.0, horizon_bars=K),
                     contract_size=TL.CS, sizing=TL.SIZING, vol_bounds=TL.VOL,
                     train_end=train_end)
record("P2 purge 跨周末", len(out) == 0,
       f"触发 {idx[t]}，train_end {train_end}，保留 {len(out)} 个（应 0）")

# ---------------------------------------------------------------- P3
bars3 = TL.synth_bars(30_000, seed=11)
atr3 = atr_series(bars3)
ap = Approach(lookback_bars=24, min_distance_atr=1.0, touch_atr=0.25)
levels = s1_level_rows(bars3, grid_step=5.0, approach=ap, atr=atr3)
events = [e for e in find_level_events(bars3, levels, situation="S1",
                                       approach=ap, atr=atr3)
          if e.kind == "resistance"]
hi, a = bars3["high"].to_numpy(), atr3.to_numpy()
jump = float(np.median([(hi[e.bar] - hi[e.bar - 1]) / a[e.bar] for e in events]))
record("P3 接近价位定义", len(events) == 117 and math.isclose(jump, 0.89, abs_tol=0.005),
       f"阻力事件 {len(events)} 个（应 117），中位冲高 {jump:.2f} ATR（应 0.89）")


# ---------------------------------------------------------------- P4
class TwoCosts:
    spreads = {"CHEAP": 0.05, "DEAR": 0.50}

    def spread_p90(self, symbol, hour_ny, *, price=None):
        return self.spreads[symbol]

    def slippage(self, symbol, hour_ny, *, price=None):
        return 0.0

    def commission_per_lot(self, symbol):
        return 0.0


root = Path(tempfile.mkdtemp())
(root / "specs").mkdir()
(root / "specs" / "latest.json").write_text(json.dumps(
    {"CHEAP": {"contract_size": 100.0}, "DEAR": {"contract_size": 100.0}}),
    encoding="utf-8")
data = TP.prob_data(symbols=["CHEAP", "DEAR"])
data["prob"] = {**data["prob"], "level_grid": {"CHEAP": 50.0, "DEAR": 50.0},
                "expected_delta_p": 0.02}
candidate = cand.validate_candidate(data, source="probe")
counted = TP.counted_df([(s, "S1", "A3", 10, 10.0, 2000.0, "2018-01-02")
                         for s in ("CHEAP", "DEAR") for _ in range(50)])
res = cand.precheck_prob(candidate, data_root=root, cost_model=TwoCosts(),
                         counted=counted, split="2020-01-01")
passed = res.checks["ex_ante_cost_prob"]["passed"]
record("P4 成本逐品种", passed is False,
       f"成本条件 passed={passed}（应 False：DEAR 0.05R 需 ΔEV ≥ 0.15R，实际 0.06R）")

# ---------------------------------------------------------------- 汇总
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}：{detail}")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
