# 第二基线 PaperSoak 启动计划（2026-11-13，用户指令执行留痕）

来源：用户 2026-11-13 指令本会话"重新看下项目新的代码，然后开始第二基线的
模拟交易"。主会话同日已按平行指令启动**第一基线**（MaCross，magic 902610，
PaperSoak，a2291da 留痕）。本会话按 D80 默认执行+用户明示授权行事，启动后
在 phase-f-log 留痕并推 main（执行记录渠道与主会话一致）。

## 代码复审结论（执行前完成）

- 候选 `cd6fc4a`（= main + T201 网关重连修复 + T202 降级段截断 + T205
  断线演练 + D60 纸面）：CI 5/5 绿、全量回归 4393 passed、独立审核×4
  approved（第二十轮 D92–D95 通过）。T201 核心 diff（三分支重连+partial
  真传+探活重置）本会话亲读：重连不再调 initialize 撞期限，O19-1 已闭合。
- 第一基线现状：三进程（runner/risk/reconcile）自 demo-deploy-cd6fc4a
  树运行，--paper，data-dir=shadow-data-r19，config=SHADOW-MA-XAUUSD-M15
  （magic 902610），paper state 存在。

## 第二基线定义（依据）

**BuyHold XAUUSD H1，magic 902501，PaperSoak 模式**——仓库既有的第二份
PaperSoak 基线配置（demo-deploy-cd6fc4a/demo-configs/BUY-HOLD-XAUUSD-H1.toml）：
- 与第一基线互补的验收覆盖：信号恒 1 → 启动后首格开多并持有，验证
  "持仓期间零调仓""入场 1 笔/期间 0 笔出场""心跳/对账/日报链路最纯粹
  样本"（配置注释原文）；唯一出场应来自停止/演练。
- 固定 0.01 手；风险敞口远低于 risk.toml 各上限。

## 隔离与安全边界

1. **独立 data-dir** `workspace/shadow-data-b2`（骨架克隆自 shadow-data-r19：
   demo-configs/costs/specs/结构）——实例注册 instances.json 按 DataDir
   隔离（T110 单实例生命周期为每目录一份），与运行中的第一基线零冲突；
   paper state 按 magic 分文件（state-902501.json）。
2. **--paper 模式**：PaperBroker 影子券商——**绝不下真实订单**（连 demo
   下单通道都不碰）；经网关只读行情（影子盘属 STATUS 禁止事项的三例外
   之一）。本会话不直接触碰终端；b2 运器在影子例外下只读行情（risk/reconcile paper 模式不连终端）。
3. 不触碰第一基线：启动前后核验其三进程存活与 state-902610 心跳新鲜。
4. 磁盘 ≥5GB（发射器预检强制）；不在窗口期做任何整机/任务计划操作
   （D85/D82 不适用——本操作不涉计划任务与重启）。
5. 生命周期：开放浸泡（至用户/计划叫停）；不进入正式窗口证据链
   （PaperSoak 证据不被正式预检采信，runbook §1 表）。

## 执行步骤

1. 骨架准备：新建 shadow-data-b2/{demo-configs,costs,specs}；配置 =
   BUY-HOLD-XAUUSD-H1.toml（改名 SHADOW-BUYHOLD-XAUUSD-H1.toml 以明示）；
   costs/spread_model.json 与 specs 从 shadow-data-r19 复制（同一终端
   同一品种，只读缓存）。
2. 预检：demo-deploy-cd6fc4a 树下 `demo_run.ps1 -Mode PaperSoak
   -PrecheckOnly -DataDir … -ConfigDir … -LogDir …`——七项全过后才继续。
3. 启动：同参数去掉 -PrecheckOnly；确认三进程起、instances.json 登记、
   state-902501.json 出现、心跳写入。
4. 双基线核验：第一基线三进程仍存活、其 state 心跳新鲜、日志无异常。
5. 留痕：docs/ops/phase-f-log.md 增补条目（第二基线启动：指令来源/
   配置/magic/隔离/预检与启动证据/双基线核验结果）；提交推 main
   （正常 push，无强推）。
6. 独立子代理复核启动证据（进程/状态/隔离/第一基线无扰）后向用户汇报。

## 明确不做

- 不动 Demo 三进程/计划任务/终端（周一部署序列完全归主会话）；
- 不下单到 MT5（paper 模式结构性不可能）；不改任何代码（部署树 detached
  +clean 保持）；不动第一基线数据目录。
