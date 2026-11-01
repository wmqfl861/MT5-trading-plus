# H 线基石建设计划（2026-11-01，三阶段）

> 基线：main@6d4a160。独立分支 hline/foundation-safety。
> 审核依据：docs/reviews/2026-10-30-global-code-audit-2.md（ChatGPT gpt-4o 审计）。
> PR #2/#3 仅作参考——全部从 main 重写，不依赖旧 PR CI。

## 第一阶段 P0 交易安全（H2-01 核心）

**目标**：券商已成交但本地程序崩溃后重启，同一决策不重复开仓。

### 1A execution_unknown 持久化 + 订单幂等
- 发送前写 `execution-journal-<magic>.jsonl`（decision_id / request_uuid / 腿 / 状态 pending→sent→known_result）
- 重启时：读 journal 最后一行 pending/sent → 核对券商持仓+挂单+成交历史 → 决定 confirmed(已成交)/rejected(确定拒)/unknown(仍未知)
- 同一 decision_id 的重发被 journal 幂等拒绝（查 journal 已有 confirmed/rejected 记录）
- 状态文件写失败时：保留 journal 不删——下次重启可恢复
- 故障注入测试：发送前崩溃、发送后回报前崩溃、成交后 journal 写前崩溃、成交后 state 写前崩溃

### 1B ACK 丢失恢复 + 重启对账
- expected 文件损坏/缺失时：不静默重建——从 journal + 券商实际持仓重建，写差异告警
- 重启对账三步：① journal 待决项核对券商 ② expected vs 实际持仓 ③ 差异写 pause
- 防止重复开仓：journal 中同 decision_id 的 confirmed 记录直接跳过

### 1C 跨数据目录进程锁
- 进程锁按 `login+magic` 而非 live_dir——同账户同 magic 两个数据目录也互斥
- 锁文件放在用户目录（`~/.mt5quant/locks/`）而非数据目录

## 第二阶段 P0 平仓安全（H2-02 核心）

**目标**：旧保护性出场不能解释新的人工出场。

### 2A 已消费 deal 水位持久化
- 新增 `consumed-deals-<magic>.jsonl`：每笔已用于解释持仓变化的 deal（ticket / volume / time / reason）
- 出场解释只允许使用**不在 consumed 列表**的 deal
- 重启后 consumed 列表保留

### 2B 延迟成交历史
- _explain_exit 优先用本次事件时间窗内的 deal（事件时间 ± 60s）
- 旧 deal 不允许解释新消失（时间窗不匹配时保持 pending）
- 故障注入测试：旧部分 SL + 迟到人工平仓 → pending 不解除

### 2C 券商持仓一致性 + 查询失败 fail-closed
- 对账增加对冲残仓检查（净零但总仓>0 → 差异告警）
- 券商查询失败时：不开新仓（fail-closed）+ 不清空 pending exit + 写告警
- 故障注入测试：网关超时 → 开仓拒 + pending 保留

## 第三阶段 P1 基石完善

### 3A 品种快照 currency_base（H2-03）
- SymbolSpec/models/snapshot 增加 `currency_base` 字段（从 MT5 SYMBOL_CURRENCY_BASE 读取）
- swap mode=2：基础币种≠账户币种 → 拒绝（不再用 currency_profit 代理）
- 旧快照无该字段 → mode=2 拒绝（fail-closed）

### 3B Demo/Live 隔离强化
- demo_run.ps1 Demo 模式预检追加：确认 gateway.json 的 login 账户 trade_mode=0（模拟盘）
- runner 启动时二次确认（防 demo_run 预检后 gateway.json 被换）

### 3C Demo 运行手册更新
- 更新 demo-runbook.md：execution journal / consumed deals / 跨目录锁 / 故障恢复流程

## 文件域

- src/mt5live/executor.py（journal + consumed deals + 对账）
- src/mt5live/runner.py（跨目录锁）
- src/mt5gw/models.py / snapshot.py / symbols.py（currency_base）
- src/mt5quant/costs/swap.py（mode=2 拒绝）
- scripts/demo_run.ps1（预检强化）
- docs/ops/demo-runbook.md（手册更新）
- tests/test_mt5live_executor.py（故障注入×8+）
- tests/test_snapshot.py（currency_base）

## 流程

计划 → 独立子代理审计划 → 并行开发（worktree）→ 独立子代理审成果 → 推送分支 → 创建 PR。
