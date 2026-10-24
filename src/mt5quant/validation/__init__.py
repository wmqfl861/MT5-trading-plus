"""mt5quant.validation：验证关卡 G0–G5（P4/T12）。

- lookahead：G0 前视（截断 + 窗口不变性 + 静态扫描）；
- robustness：G2 稳健性（成本放大/参数邻域/按年/跨品种/随机入场/买入持有/
  去最好 5%/延迟一根 bar）；
- walkforward：G3 滚动训练/测试窗；
- stats：G4 Deflated Sharpe Ratio 与蒙特卡洛回撤分布（种子 = card_id 哈希）；
- gates：门槛配置（config/gates.toml，sha256 冻结核对）与 G1/G4/G5。
"""
from mt5quant.validation import gates, lookahead, robustness, stats, walkforward

__all__ = ["gates", "lookahead", "robustness", "stats", "walkforward"]
