# H 线基石建设计划 v2（2026-11-01，三阶段，R1–R10 已修订）

> 基线：main@6d4a160。独立分支 hline/foundation-safety。
> 审核依据：docs/reviews/2026-10-30-global-code-audit-2.md（ChatGPT gpt-4o 审计）。
> PR #2/#3 仅作参考——全部从 main 重写，不依赖旧 PR CI。
> v2 修订：R1 补文件域 / R2 :766 改 pending / R3 补注入 / R4 区间窗+部分减量 / R5 fail-closed / R6 server 级锁 / R7 仲裁 / R8 纠偏 / R9 测试 / R10 串行。

## 第一阶段 P0 交易安全（H2-01 核心）→ 第二阶段串行

**目标**：券商已成交但本地程序崩溃后重启，同一决策不重复开仓。

### 1A execution_unknown 持久化 + 订单幂等
- 发送前写 `execution-journal-<magic>.jsonl`（decision_id / request_uuid / 腿 / 状态 pending→sent→known_result）
- **R2**：executor.py:766 现有"未知异常也写 confirmed"改为 **pending + journal 记 unknown**
- 重启时：读 journal 最后一行 pending/sent → 核对券商持仓+挂单+成交历史 → confirmed / rejected / unknown
- 同一 decision_id 的重发被 journal 幂等拒绝
- **R5** journal 尾部撕裂行按 unknown 处理（不 crash）
- 故障注入测试（**R3 补齐 6+ 个场景**）：
  1. 发送前崩溃
  2. 发送后回报前崩溃
  3. 成交后 journal 写前崩溃
  4. 成交后 state 写前崩溃
  5. **R3** 重启时持仓/成交历史延迟可见（不得误判 rejected=重复开仓风险）
  6. **R3** 重启时仓位已被 SL/人工平+重放同 decision_id 不得再开
  + 判 rejected 需历史窗口覆盖发送时刻，否则保持 unknown

### 1B ACK 丢失恢复 + 重启对账
- expected 文件损坏/缺失：从 journal + 券商实际持仓重建，写差异告警
- 重启对账三步：① journal 待决项核对券商 ② expected vs 实际 ③ 差异写 pause
- 防止重复开仓：journal 同 decision_id confirmed 记录直接跳过

### 1C 跨数据目录进程锁
- **R6** 锁按 `server+login+magic` 哈希（login 跨券商可撞号）
- **R6** 锁文件放 OS 状态目录（Windows `%LOCALAPPDATA%\mt5-trading-plus\locks\`）
- **R6** 锁获取在网关连接后（需读 login）；paper 模式锁行为定义

### 1D 人工仲裁工具（R7）
- scripts/live_ack_execution.py：永久 unknown 项的仲裁出口
- Issue #4 risk watchdog：PR #3 已实现的线程在本批复用

## 第二阶段 P0 平仓安全（H2-02 核心）——与第一阶段**串行**（R10）

### 2A consumed deal 水位 + 部分减量（R4 关键补齐）
- 新增 `consumed-deals-<magic>.jsonl`：每笔已用于解释持仓变化的 deal（ticket / volume / time / reason）
- **R4** 部分减量：同 ticket 持仓量减少（非整仓消失）也要解释+消费（现 _sync_external_exits 只看 ticket 消失，旧部分 SL deal 从未被消费→审计探针根因）
- **R5** consumed 文件丢失/损坏 → fail-closed（防线视为仍在+告警，等同 state_corrupt）
- 解释成功时把覆盖 deal 记入 consumed；新消失候选 deal 排除 consumed 票号

### 2B 延迟成交历史
- **R4** _explain_exit 用事件**区间** `(最后已知时刻, 检测时刻]`+裕量（非固定 ±60s）
- 旧 deal 不允许解释新消失（区间不匹配保持 pending）
- 故障注入：旧部分 SL + 迟到人工平仓 → pending 不解除（审计探针原场景）
- **R9** 全矩阵：多个部分 SL / 重复旧 deal / 乱序历史 / 重启后补齐历史

### 2C 券商查询 fail-closed
- ~~对冲残仓检查~~（已在 main reconcile.py:253 实现，复用）
- 券商查询失败：不开新仓 + 不清空 pending exit + 写告警

## 第三阶段 P1 基石完善——与 1/2 阶段**并行**（R10）

### 3A 品种快照 currency_base（H2-03）
- ~~swap.py mode=2 拒绝~~（已在 main b2cb70b 实现，复用）
- **新增**：SymbolSpec/models/snapshot/symbols 加 `currency_base` 字段
- **新增**：快照→build_costs 集成测试
- **R8** 维护窗口刷新授权快照步骤

### 3B Demo/Live 隔离
- ~~demo_run.ps1 预检 trade_mode~~（已在 main :500 实现，复用）
- **新增** runner 启动时二次确认 trade_mode=0（**R1** 落 scripts/live_runner.py）

### 3C Demo 运行手册更新

## 文件域

- src/mt5live/executor.py（journal + consumed deals + 对账 + :766 修复）
- **R1** scripts/live_runner.py（锁获取+3B runner 启动二次确认）
- src/mt5live/runner.py（MagicInstanceLock 类改身份键）
- src/mt5gw/models.py / snapshot.py / symbols.py（currency_base）
- **R7** scripts/live_ack_execution.py + 测试
- scripts/demo_run.ps1（复用已有预检）
- docs/ops/demo-runbook.md
- tests/test_mt5live_executor.py（故障注入×10+）
- **R9** tests/test_mt5live_runner.py（跨目录锁）
- **R9** tests/test_symbols_market.py + tests/test_snapshot.py（currency_base）
- **R9** tests/test_costs_swap.py（快照→build_costs 集成）

## 流程

计划 → 独立子代理审计划 ✓（R1–R10 已修订）→ **R10 阶段 1→2 串行** + **阶段 3 并行** → 故障注入测试 → 独立子代理审成果 → 推送分支 → 创建 PR。
