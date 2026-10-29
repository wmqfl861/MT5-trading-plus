"""序列清单：扫出库里全部 品种×周期 序列，供每日调度枚举。

refresh_all / export_lockbox 等批处理脚本以本清单为唯一事实来源
（不写死品种列表，新回填的序列自动进入次日的刷新计划）；
顺序按 (symbol, timeframe) 升序，保证每日计划可复现。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# store.save_bars 必写的字段；缺任一即视为坏 meta（H-01 起公共化：
# mt5data.lockbox 的边界 fail-closed 校验与导出自检用同一口径）
REQUIRED_META_FIELDS = ("symbol", "timeframe", "rows", "first_time",
                        "last_closed_time")
_REQUIRED = REQUIRED_META_FIELDS


@dataclass(frozen=True)
class SeriesRef:
    symbol: str
    timeframe: str
    watermark: dict


def series_inventory(root) -> tuple[list[SeriesRef], list[str]]:
    """扫描 {root}/bars/*/*/meta.json → (inventory, warnings)。

    - inventory 按 (symbol, timeframe) 升序，顺序稳定；
    - 坏 meta（非法 JSON / 结构不对 / 缺必需字段）跳过并记入 warnings，
      单序列损坏不阻塞整个每日调度，但必须可见。
    """
    bars_dir = Path(root) / "bars"
    inventory: list[SeriesRef] = []
    warnings: list[str] = []
    if not bars_dir.is_dir():
        return inventory, warnings
    for meta_path in sorted(bars_dir.glob("*/*/meta.json")):
        rel = os.path.relpath(meta_path, root)
        try:
            with open(meta_path, encoding="utf-8") as f:
                wm = json.load(f)
            if not isinstance(wm, dict):
                raise TypeError(f"meta 应为 JSON 对象，实际 {type(wm).__name__}")
            for key in _REQUIRED:
                if key not in wm:
                    raise KeyError(f"缺少字段 {key}")
        except (OSError, ValueError, TypeError, KeyError) as e:
            warnings.append(f"{rel}: {type(e).__name__}: {e}")
            continue
        inventory.append(SeriesRef(symbol=str(wm["symbol"]),
                                   timeframe=str(wm["timeframe"]),
                                   watermark=wm))
    inventory.sort(key=lambda r: (r.symbol, r.timeframe))
    return inventory, warnings


def bars_parquet_paths(root) -> list[tuple[str, str, Path]]:
    """枚举 {root}/bars/*/*/data.parquet 的**真实**数据文件（H-01）。

    以文件系统为事实来源、不信 meta 清单：orphan parquet（有数据无
    meta.json）在 series_inventory 的 meta 扫描下完全不可见，而它同样
    可能携带 cutoff 之后的锁箱段——导出自检（export_research_store.
    verify_research_store）据此枚举。返回 [(symbol, timeframe, parquet
    路径)]，按 (symbol, timeframe) 升序（与 series_inventory 同序）。
    """
    bars_dir = Path(root) / "bars"
    if not bars_dir.is_dir():
        return []
    out = [(pq.parent.parent.name, pq.parent.name, pq)
           for pq in sorted(bars_dir.glob("*/*/data.parquet"))]
    out.sort(key=lambda t: (t[0], t[1]))
    return out
