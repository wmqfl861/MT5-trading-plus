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

    @classmethod
    def from_calibrated(cls, data: dict, *,
                        default_per_lot: float = 0.0) -> "CommissionModel":
        """从 scripts/calibrate_costs.py 产出的 calibrated.json（已解析 dict）构造。

        逐品种取 symbols[sym]["commission"]["round_trip_per_lot"]（实测每手来回，
        账户货币）；窗口内无成交的品种不配置（走 default_per_lot）。
        空文件（没有任何品种）按缺数据报错——绝不静默全 0。
        """
        symbols = data.get("symbols")
        if not symbols:
            raise ValueError(
                "calibrated 数据里无任何品种的佣金记录（symbols 为空）——"
                "先在模拟盘开平几笔后重跑 scripts/calibrate_costs.py")
        per_symbol: dict[str, float] = {}
        for sym, info in symbols.items():
            comm = (info or {}).get("commission") or {}
            rt = comm.get("round_trip_per_lot")
            if isinstance(rt, (int, float)) and not isinstance(rt, bool):
                if rt < 0:
                    raise ValueError(f"{sym} 校准佣金不能为负：{rt}")
                per_symbol[sym] = float(rt)
        return cls(default_per_lot=default_per_lot, per_symbol=per_symbol)

    def commission_per_lot(self, symbol: str) -> float:
        """结构化接口：计价货币/手，来回合计（未配置品种走默认）。"""
        return self._per_symbol.get(symbol, self.default_per_lot)
