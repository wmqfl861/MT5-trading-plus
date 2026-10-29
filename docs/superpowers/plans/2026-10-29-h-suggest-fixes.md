# H 线建议项修复计划（H-11/H-12/H-13，2026-10-29）

> 来源：全局审核建议项——不阻塞但提高数据完整性与数值安全。
> 不碰 src/mt5live/、src/mt5gw/（Demo 运行中）。

## H-11 数据写锁覆盖读改写（mt5data 域）
- src/mt5data/downloader.py:145、store.py:86/:131、quality.py:371、ext/core.py:222
- bars 刷新锁外读旧表→合并→锁内写：两刷新可能互相覆盖；known_gaps 重建时丢失
- 修法：读最新表+合并+校验+发布在同一序列锁内；meta 保留审计字段（known_gaps）；唯一临时文件
- 失败测试：两写者分别新增不相交段→合并后两段都在；刷新后 known_gaps 不丢

## H-12 试验指纹逐品种上下文（research/backtest 域）
- report.py:311、cards.py:688、ledger.py:1296、bt.py screen/gate 成本装配
- data_sha256 丢顺序且不含 volume；依赖指纹固定 library 根漏库外模块；成本版本跨品种复用
- 修法：版本化有序数据指纹（含 volume 等实际输入列）；逐品种成本/政策上下文独立
- 失败测试：只改第二品种→身份变；改 volume 列→身份变；品种排列不变集合身份

## H-13 有限数与币种校验（costs/backtest 域）
- commission.py:7、slippage.py:20、swap.py:98/:204、engine_a.py:266、clock.py:31/:95
- NaN/Inf 可进入成本模型；bar 校验缺完整有限值/OHLC 关系；swap mode=2 币种代理
- 修法：统一有限数/范围验证（math.isfinite）；swap mode=2 非账户币种明确拒绝（不代算）；bar OHLC 关系校验
- 失败测试：NaN/Inf 成本参数→拒绝；非法 OHLC bar→拒绝；EURUSD mode=2→明确报错

## 文件域
H-11: src/mt5data/{downloader,store,quality}.py + ext/core.py + 测试
H-12: src/mt5quant/backtest/report.py + research/{cards,ledger}.py + scripts/bt.py + 测试
H-13: src/mt5quant/costs/{commission,slippage,swap}.py + backtest/engine_a.py + mt5data/clock.py + 测试

三组文件域互斥，可并行。
