"""第十二轮审核探针 G1：质检把多周缺口归为"周末"（只读，不碰数据目录）。

运行（仓库根目录）：
    ./.venv/Scripts/python.exe docs/reviews/probes/2026-10-27-round12-gap-probe.py

构造一段工作日 H1 序列，挖掉 2021-06-03 至 07-01 约 27 天，跑
mt5data.quality.quality_report。修复前：只报若干 weekend 缺口，其中一处横跨
27 天；期望（T79 修复后）：该缺口报为 unexpected（或新增的长缺口类别）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from mt5data.quality import quality_report  # noqa: E402

idx = pd.date_range("2021-05-03", "2021-07-30 20:00", freq="h")
idx = idx[idx.dayofweek < 5]
hole = (idx >= "2021-06-03 10:00") & (idx < "2021-07-01 08:00")
df = pd.DataFrame({"time": idx[~hole], "open": 1.2, "high": 1.21, "low": 1.19,
                   "close": 1.2, "tick_volume": 1, "spread": 1, "real_volume": 0})

rep = quality_report(df, "H1")
for g in rep.gaps:
    days = (g.end - g.start).days
    if days >= 3:
        print(f"缺口 {g.start} → {g.end}（{days} 天）分类 = {g.kind}")
counts: dict[str, int] = {}
for g in rep.gaps:
    counts[g.kind] = counts.get(g.kind, 0) + 1
print("缺口分类计数：", counts)
print("期望：横跨 27 天的缺口不得归为 weekend")
