# T78 分线风控独立审核与修复复核（2026-11-05）

本记录由主会话转录独立审核者直接返回的三轮结论，区分实施者测试与审核者探针。审核者未参与实现；技术通过不构成研究或部署放行。

## 固定版本与结论

- 基点：`def8744d90f75519ed9486e8d1ee7efa79ba2e6a`。
- 首轮：`29a898872ee4f238ddb68e7ce285c59136783377`。
- fix1：`5ebbae8f33e3cb3aaa708edcdaac823e9424a017`。
- fix2：`41236efbb12552cc39091db6acbc997edbfb872b`，提交树 `53c3dd370818f82a1323ad2c6c0738a0d138edb4`。
- 首轮：Spec FAIL、Quality NEEDS_FIXES，三个 Important/P1。
- fix1：原三项反例均 ADDRESSED，但修复引入一个暂停交接读取竞态，仍需修复。
- 同一审核者 fix2 定点复核：**暂停交接竞态 ADDRESSED；最终 Spec PASS / Quality PASS。原三项维持闭合，fix2 未发现新的 Important/Critical 问题。**

## 原问题与修复

1. **首次触限在 pause 锁超时后丢失。** 原代码先取兼容 pause 锁再持久化线状态；真实合作写者持锁时，已经观察到 H=-250 的监控周期不能留下锁存，价格恢复后新 H magic 实际开仓。fix1 改为在线锁内先持久化独立线状态，再处理兼容 pause。新增测试覆盖 monitor/gate、真实锁竞争、同日恢复、重建对象、新 magic 拒开和另一线继续。
2. **人工解除态掩盖新的对账 mismatch。** 保留人工 `paused=false` 文件时，原 write_pause 跳过写入，真实对账报告暂停但 Gate 仍实际开仓，caps 开关均受影响。fix1 新增独立 `reconcile-block-<magic>.json`，写入使用既有 pause 路径锁及原子文件；Gate 的 caps 开关两分支消费此阻断。一致恢复只清 reconcile 自身记录，人工文件原字节保留。
3. **paper 反序列化掩盖异常 magic。** 既有 `int(raw_magic)` 把 JSON true 等非法值转换为整数，绕过新的分线验证。fix1 在 positions/deals 两个解析入口严格校验原始 magic，拒绝 bool、字符串、浮点、缺失和越界值，合法0保留；测试经过真实 PaperAccountView、监控和执行链。成交算法未改。
4. **fix1 新增的暂停原因交接竞态。** Gate 无锁先读独立 block，再读兼容 pause。合作写者可在其间完成“人工 true → 对账 block → 人工 false”交接，全程存在阻断却被拼成允许，caps 开关均实际开0.01手。fix2 在同一 `pause-<magic>.json.lock` 的短临界区内一致读取两个文件；锁超时和读取失败拒开。判定和告警在锁外，不跨账户、报价或下单调用持锁。

最终复核依据包括 `gate.py:393` 的共同锁读取与 `test_t78_line_caps.py:656` 起的真实 Event/内核锁交错测试。测试验证写者等待、零新增订单/持仓/成交；账户=-500 在竞争 pause 锁前触发编号 flatten/halt 并由执行器全平，平仓路径仍允许。

## 验证证据与归属

| 阶段 | 实施者结果 |
| --- | --- |
| 首轮完整相关集 | 169 passed，退出0，无排除项 |
| fix1 有效反例 | 26 failed、1 passed，退出1 |
| fix1 定向与相关集 | 27 passed；235 passed，均退出0 |
| fix2 有效反例 | 4 failed、3 passed，退出1 |
| fix2 定向 | 7 passed，退出0 |
| fix2 最终相关与 paper 回归 | **242 passed，退出0，无排除项**；受测树与最终提交树一致 |

最终命令：

```text
python -m pytest -q tests/test_t78_line_caps.py tests/test_mt5live_riskproc.py tests/test_mt5live_gate.py tests/test_mt5live_reconcile.py tests/test_mt5live_paper.py tests/test_mt5live_integration.py::test_t32_second_flatten_command_executes_after_archive tests/test_mt5live_integration.py::test_t32_gate_triggered_flatten_uses_numbered_protocol tests/test_mt5live_integration.py::test_t34_shadow_full_chain_decision_to_flatten tests/test_mt5live_integration.py::test_t30_sl_exit_transient_pause_auto_releases --tb=short
```

审核者首轮用有界合成消费者探针确认三项问题，fix1 复审用真实读取与 Event 调度复现新竞态；没有伪造读取结果。fix2 复核读取修复 diff、源码和作者日志，没有重跑242项或追加探针。各轮保持审核 checkout、HEAD、索引不变。

## 保留条件

旧无 caps 配置保持兼容；启用 caps 后，H/P 线触限只停止各自开仓，不强平存量，账户总额规则仍通过编号全平协议处理。UTC 隔日只清可证明归属自身的线状态，不清人工或对账暂停；未知、损坏状态保持保守拒绝。协作工具须遵守共同文件锁，绕过锁的外部裸写不在此次一致性保证内。

本模块合成验证和技术审核不证明真实账户或部署健康。联合冻结模型交易验收、完整集成回归、对应 main CI 及研究/阶段 F/维护条件均须各自取得证据，当前整合状态见[准备验收](../ops/simulation-readiness-2026-11-05.md)。本次未连接 MT5、访问真实状态、操作业务进程或计划任务，也未批准部署。
