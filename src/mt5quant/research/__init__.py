"""mt5quant.research：试验台账与假设卡（P4）——预测先登记，测量后进台账。

- cards：假设卡（TOML，docs/blueprint.md 附录 A 模板）装载与"先卡后测"校验；
- ledger：SQLite 试验台账（cards/runs/gates/lockbox_uses）。
"""
from mt5quant.research import cards, ledger

__all__ = ["cards", "ledger"]
