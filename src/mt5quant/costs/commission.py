"""佣金模型：计价货币/手，**来回**（开+平合计，一次性给出）。"""


class CommissionModel:
    """commission_per_lot 返回该品种每手来回佣金；调用方不得再 ×2。"""

    def __init__(self, default_per_lot: float = 0.0,
                 per_symbol: dict[str, float] | None = None):
        if default_per_lot < 0:
            raise ValueError("default_per_lot 不能为负（成本幅值）")
        for k, v in (per_symbol or {}).items():
            if v < 0:
                raise ValueError(f"{k} 佣金不能为负：{v}")
        self.default_per_lot = float(default_per_lot)
        self._per_symbol = {k: float(v) for k, v in (per_symbol or {}).items()}

    def commission_per_lot(self, symbol: str) -> float:
        """结构化接口：计价货币/手，来回合计（未配置品种走默认）。"""
        return self._per_symbol.get(symbol, self.default_per_lot)
