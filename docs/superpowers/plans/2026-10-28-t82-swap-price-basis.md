# T82 修复计划：引擎 A 隔夜费按相对价格基准计费（2026-10-28）

> 来源：T65 审核观察 3（蓝图开放清单登记待办）。现象：点数模式隔夜费换算为
> **绝对 USD/手/夜**（swap.py per_lot_per_night：points × point × contract），
> 该费率校准于快照时代价格（USTEC ≈25,097 → −2.5bp/夜）；套到 1986 年
> 价格 131 时同一绝对额 = −4.8%/夜，实测 FRED 段 6 笔后权益爆掉（每笔
> swap −1.9万~−4.1万）。T66 事件路径已修同类问题（按参考价折相对比率），
> 本任务把同一口径接到引擎 A 与成本接口。**无需批准**（引擎域，非
> gates/研究运行）；是外部数据延长后对照标定（gates-v3 延长前后）的前置。

## 设计（镜像 T66 §1.2 口径）

1. **相对化**：点数模式费率换算为**相对费率**（每夜占价格的比率）：
   `rel = |per_lot_per_night_usd| / (ref_price × contract_size)`，
   ref_price = **成本模型缓存的参考价**（spread 模型同源——T66 审核确认
   "参考价同取最后收盘"的仓库约定；快照 specs 无价格字段）。计费：
   `fee = rel × 当根 bar 价格 × contract_size × lots`。
   货币模式（2/3/4）费率本身就是绝对金额语义，**不相对化**（如实保留，
   报告注明）；禁用模式不变。
2. **接口**：`overnight_fee(..., price=...)` keyword-only 参数（沿用成本
   接口 v2 的 price 惯例）；engine_a.py:448 调用点传入出场侧 bar 价格。
   **缺省行为**：不带 price 且模型为点数模式 → fail-fast 报错（不静默
   绝对额——防旧口径复活），B 层/其他调用点同步接线。
3. **身份**：成本版本串 swap 段追加基准标记 `relref[ref_price 8 位哈希]`
   （换算假设变了，台账去重必须区分新旧）；引擎 docstring 版本历史追加
   A-1.7 条目。
4. **快照时代自洽**：ref_price=近期收盘时，新旧行为在近期价格上数值近似
   （差异 = |price/ref−1| × rel），2014–2025 段结果会小幅变化——**这是
   修正而非回归**，受影响快照测试如实更新并注明。

## 验收（失败测试先行）

- [ ] 1986 年价格反例：合成 131 价位 USTEC 持仓 5 夜，新口径每夜 ≈−2.5bp
      （旧口径 −4.8%/夜）——金额差 190 倍量级实证；
- [ ] 近期价位回归：25,000 价位新旧口径每夜金额比值 ≈1（容差 5%）；
- [ ] 货币模式不受影响；禁用模式 0；
- [ ] 缺 price 调用点数模式 → fail-fast；
- [ ] 成本版本串新旧不同（去重身份区分）；
- [ ] engine_a 端到端：早年段回测权益不再爆炸（FRED 段 6 笔场景复算）；
- [ ] 全量 pytest 绿（受影响快照测试更新附注明）。

## 文件域

src/mt5quant/costs/swap.py、src/mt5quant/backtest/engine_a.py、
src/mt5quant/backtest/engine_b.py（同接口调用点）、scripts/run_backtest.py
（版本串）、tests/test_costs_swap.py、tests/test_engine_a*.py 受影响者、
蓝图开放清单勾销该待办。不碰 gates/mt5live/mt5gw/研究库。
