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

# store.save_bars 必写的字段；缺任一即视为坏 meta
_REQUIRED = ("symbol", "timeframe", "rows", "first_time", "last_closed_time")


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
