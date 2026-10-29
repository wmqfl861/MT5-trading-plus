# T99 计划：P-10 跨线接入（原子试验预算 + 准入/政策继承验证）

来源：`docs/reviews/2026-10-29-global-code-audit.md` §P P-10（必改）。
前置解除：H 线 3733321（H-02/03/06/07/08）与 c9afbb8（H-01）已合入 main。
硬约束不变：不改引擎成交语义/实盘/gates.toml；ledger.py 与 bt.py 只加
P 功能、**H 调用路径行为逐位不变**；三套受保护探针不许改。

## 审核要求映射

- "在 P 调用层接入原子预算" → 模块 1+2：试验预算（trial_budget/
  round_n_dedup，成功行口径）的检查并入 record_run 的
  BEGIN IMMEDIATE 事务——镜像 H-03 reserve_lockbox_use 的做法。
- "完整准入" → P 路径已全部走共享 `_round_context`（含 H-08
  `_candidate_frozen_check` 冻结核对）；模块 3 补继承验证测试。
- "逐品种政策" → H-06 已把 policies_by_symbol 接进共享关卡复算；
  P1（run_p1_card）波次 2 已传 policy_by_symbol；模块 3 补 P1/P2
  同一禁入口径的一致性测试。
- "T73B 完成前保留拒绝" → `_reject_fittable_walkforward` 不动
  （已有测试 test_prob_bt_fit.py:205），模块 3 补一条 e2e 级。

## 模块 1：ledger.py 原子试验预算（可选 kwarg，缺省无检查）

- 新异常 `RoundTrialBudgetExceededError(RuntimeError)`（docstring 注明
  P-10/H-03 同族：检查与写入同一事务）。
- `record_run(..., round_trial_budget: int | None = None)`：给出且
  round_id 非空时，在现有事务内（验链 → 预算检查 → 写入）加
  `round_n_dedup(round_id) >= round_trial_budget → raise`（不写任何行、
  链不动）。缺省 None / round_id=None → 无检查，**H 行为逐位不变**。
  `round_trial_budget <= 0 → ValueError`（与 reserve_lockbox_use 的
  max_uses 校验对称；零预算=全拒应由调用方不发起运行表达）。
- 口径：round_n_dedup 只计成功行（失败/迁移行不计），因此
  - 预算检查只挂在**成功行**写入路径（调用方只对成功行传 kwarg）；
  - error 行不检查不消耗（崩溃重跑留痕不占预算，与现有口径一致；
    同七元组重复运行在预算满时一并拒绝——与
    `_round_budget_exhausted` 的保守口径一致）。
- 测试（镜像 test_h03_race_last_budget_slot_single_winner）：
  1. 两连接（ThreadPoolExecutor）竞争最后一个名额（budget=1、两个
     不同七元组）→ 恰一个成功，另一个 RoundTrialBudgetExceededError，
     链可验证；
  2. error 行（带 kwarg 也不检查）不消耗预算；
  3. 不传 kwarg → 预算外仍可写（H 路径不变）；
  4. round_id=None + kwarg → 不检查。

## 模块 2：bt.py P 调用点接线（三处）

1. **cmd_prob_test**（scripts/bt.py:2561 成功行）：`_prob_preconditions`
   的预检（2375）保留为快速失败；成功行 record_run 传
   `round_trial_budget=round_obj.trial_budget`；逐假设入账循环中途抛
   RoundTrialBudgetExceededError 时，对**所有尚未入账的假设**（含
   抛出的那条，k..N——run_p1_card 已把全部假设算完）补记
   "结果作废：运行期间预算被并发占满" error 行（均**不**带 kwarg，
   遵守 P-05"每条假设恰一行终态"）→ 整体退 1。轮次报告按"恰一条
   成功行"口径自然拒绝（fail-closed）。
2. **cmd_gate 基准运行**（scripts/bt.py:3467 成功行，`common` 之后的
   metrics 调用）：仅当 `card.prob` 非空（P 卡）且 round_ctx 有
   trial_budget 时传 kwarg；error 行（3472）不传；在通用 except 之前
   加专用 except：印预算信息、记 error 行、退 1。H 卡走原路径逐位不变。
   （standard 模式无 [prob] 的 P### 卡 prob=None → 同样无 kwarg——
   P-10 范围为概率/P2 决策卡，有意为之。）
3. **cmd_screen 组合循环**（scripts/bt.py:3105 成功行）：同 2——P 卡
   成功行传 kwarg；捕 RoundTrialBudgetExceededError → 该组合记 error
   行并置标志位跳出**双层**循环（外层 for sym、内层 for params；
   只 break 内层会把剩余组合留给 3068-3076 的既有预检逐条拒绝——
   结果同样 fail-closed 但白算，用标志位直接退出）。
- `cmd_fit` 的预算预检保持快速失败语义；docstring 把"预算非原子为
  P-10 残留"改为"P-10：预算执行的原子性在运行入账时（record_run
  可选上限）；fit 不产生 runs，其预算预检为快速失败"。

## 模块 3：跨线集成回归（新文件 tests/test_p10_cross_line.py）

1. **H/P 竞争共享额度**（审核验证点 1）：
   - 一轮 trial_budget=2；先以 H 风格成功行（record_run 不带 kwarg，
     模拟 H 线）占 1 → P 卡两假设经 `_prob_preconditions` 预检拒绝
     （1+2>2），零行产生；
   - budget=2、零占用 → P 两假设并发（线程池两连接、各带 kwarg）→
     恰 2 行成功；第三插入 RoundTrialBudgetExceededError。
2. **P 基准/关卡政策一致**（审核验证点 2）：合成 session 禁入窗 +
   LevelDecisionS1Demo —— P1（run_p1_card 直调）与 P2（validation 卡
   经 screen）入场时刻都不落在禁入时段（同一 load_session_policy
   源）。
3. **fittable G3/V3 保留拒绝**（审核验证点 3）：P2 卡 + fittable 策略
   + gate 请求 G3 → 拒绝（e2e 级，退出码 1、零 runs；单元级已有）。
4. **H-08 准入继承**：P prob test 在候选登记文件预筛通过后被改动
   （sha 不匹配）→ 拒绝（走共享 `_round_context` 的
   `_candidate_frozen_check`；预筛 pass 行须带 candidate_sha256
   绑定——旧式无绑定行不强制，测试要用新式行否则假绿）。

## 明确不做

- T73B 逐窗拟合/OOS 拼接/Brier 否决（仍等 D34 冻结；fittable
  G3/V3 拒绝保留）。
- H 运行路径的预算原子性（H 线所有权；本计划只保证 P 插入原子、
  P 的检查在同一事务内可见 H 的已提交行）。
- 引擎/实盘/gates.toml；受保护探针；H 线测试文件不改。

## 验收

- 全量回归 + 三探针（10/11，R5 为 P-04 预期 FAIL）；
- H 线相关测试文件（test_lockbox_budget、test_research_rounds、
  test_validation_* 等）不改且全绿——H 行为不变的证明；
- CI 绿（提交 + run_number 记录）。

## 执行记录

- 计划审核（子代理）：需修订两处后执行——①prob test 循环中途耗尽须
  对全部未入账假设补 error 行（P-05"每条假设恰一行终态"）；②screen
  双层循环用标志位退出。均已在实现中落实；可选建议（非正上限
  ValueError、H-08 测试须带 candidate_sha256 绑定）一并采纳。
- 成果审核（子代理）：通过，无 A 级；B1（政策一致性只覆盖 P2 侧）已补
  P1 侧测试 test_p10_p1_policy_same_source；B2（判空风格不一）已统一
  `is None`。定向测试 6 文件全绿（102 通过）。
- P1 侧政策断言实现说明：prob test 无条件经 entry_policy_for 构造
  policy_by_symbol（--costs 无分支差异），测试捕获构造入参与
  run_p1_card 实收值，断言与 load_session_policy(SESSIONS_PATH) 同源
  ——与 P2 侧（zero 成本分支直接传 sess_policy）互补成对。
