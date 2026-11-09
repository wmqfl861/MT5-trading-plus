# 远端分支审计——audit-fixes-2026-10-30 / audit-live-safety-2026-10-30（T189）

2026-11-09。范围：两个远端分支相对 origin/main（23edcff）独有的全部
51 个提交（audit-fixes 2 个 + audit-live-safety 49 个，均无 merge 提交）。
分叉点同为 `d67476a`（2026-10-30）。逐提交核对 main 上是否已有等价实现；
删除由主会话另行执行，本文只出结论。

## 0. 结论（先行）

- **51/51 提交的语义在 main 上均有等价或更强的实现；无"无等价且有价值"
  的提交**——两个分支作为整体可以删除（删除动作由主会话执行，本文为依据）。
- main 是**从分叉点重写**而非合并这两个分支：`git patch-id --stable` 扫描
  51×225 个补丁，**零共同 patch-id**（无 cherry-pick）。重写依据在案：
  `dc0af05 plan(H线基石)`（"基于 ChatGPT 审计 H2-01/02/03；**从 main 重写
  不依赖旧 PR**"；计划全文 `docs/superpowers/plans/2026-11-01-hline-foundation.md`）。
  分支自身定位即"PR #3 参考件"（其 `2026-10-30-audit2-remediation-status.md`）。
- 两处**设计差异**（非缺失，§3 详述）：① 实例锁粒度——分支按账户级
  （跨 magic 互斥），main 按 `server|login|magic` 身份键（R6 有意裁定，
  允许同账户多 magic 并行）；② 卡死看门狗——分支为进程内线程，main 为
  T110 外部 supervisor（进程树级，能覆盖整进程死亡，强于进程内线程）。

## 1. 方法

1. 清单：`git log --oneline origin/main..<branch>`（2 + 49 = 51）。
2. 逐提交看 diff 主题（`git show --stat` + 新增行），与 main 历史对照：
   `git log origin/main -- <涉及路径>`、按主题 grep（H2-01/H2-02/H2-03/
   基石/T110 系列），再对关键标识符（函数名/marker 名/测试名）做
   `git grep origin/main` 内容比对。
3. patch-id 全量扫描（零命中）排除 cherry-pick 直入。
4. 内容级抽查 8 个提交（不只标题，§4）：e179aee、23c1a0d、2eba121、
   23638c4、d04c0a7、fa5a2c0、1c79285、5989e07。

main 侧等价实现的主要载体（下表引用）：

| 载体 | 内容 |
| --- | --- |
| `814fe7e` feat(H线基石P0) | execution journal+幂等重启恢复+跨目录身份锁+consumed deal 水位+部分减量+区间窗+fail-closed+仲裁工具（tests/test_mt5live_executor.py 73 个测试，含 h201_1..8 / h202_1..5 故障注入矩阵） |
| `f4dd503` feat(H线基石P1) | currency_base（H2-03）快照字段+mode=2 集成测试+demo-runbook 故障恢复节 |
| H2-01 系列（`c931a6a`…`716843e`，2026-11-01） | 恢复证据规则（request-linked/PLACED≠成交/正证据才落 rejected）、仲裁重试安全、锁升级兼容 |
| `6d85ef1` [H] T110 | 外部 supervisor（live_supervisor.py）+ 心跳健康检查 + 卡死写 fail-closed halt（test_t110_health/supervisor 900+ 行测试） |
| `890838b` feat(T30) / `82f9917` fix(R6) | 对账意图语义（Intent pending/confirmed、外部出场 deal.reason 自动解释、live_ack_external、C2 未解释禁开仓） |
| `docs/ops/demo-runbook.md`（§1/§5.3/§5.4/§6） | 锁/仲裁/supervisor 运维手册 |

## 2. 逐提交结论（51 行）

### audit-fixes-2026-10-30（2）

| 提交 | 结论 |
| --- | --- |
| `e179aee` fix(H2-03): carry base currency through symbol specs and snapshots | 等价 main `f4dd503`（models/snapshot/symbols 加 `currency_base`，缺字段=None 由成本层 fail-closed、不用 currency_profit 代理——语义逐条一致，内容级抽查 §4.1） |
| `23c1a0d` test(H2-03): extend full symbol fixtures with base currency | 等价 main `f4dd503`（test_snapshot/test_symbols_market 的 currency_base 断言 + test_costs_swap.py mode=2 集成/旧快照拒绝测试——覆盖广于分支的 2 行夹具改动） |

### audit-live-safety-2026-10-30（49）

| 提交 | 结论 |
| --- | --- |
| `1c79285` fix(live): fail closed on ambiguous order lifecycle across restart | 等价 main `814fe7e` 1A/1B（journal pending→sent→known_result；重启 `_run_journal_recovery` 按请求关联证据裁决，其余保持 unknown 挡开仓；executor 四道开仓挡板） |
| `3c275b1` test(live): cover ambiguous order recovery and duplicate-open prevention | 等价 main `814fe7e` tests（`test_h201_1/4/5/6/8`、`test_h201_unjournaled_pending_order_blocks_new_open_but_not_close`） |
| `2eba121` fix(live): track consumed partial-exit deals to prevent stale SL attribution | 等价 main `814fe7e` 2A（`consumed-deals-<magic>.jsonl` 持久水位，ticket/volume/time/reason；内容级抽查 §4.3） |
| `6aa510a` fix(live): preserve consumed exit-deal watermark across poll finalization | 等价 main `814fe7e` 2A（水位为独立持久文件，轮次结束/重启不丢；`test_h202_5_restart_keeps_consumed_watermark`） |
| `9f3ed91` fix(live): persist manual exit reason while deal history catches up | 等价 main `814fe7e` 2B（延迟成交历史：事件区间窗 `(最后已知时刻,检测时刻]`+裕量，旧 deal 不解释新消失；`test_h202_1`） |
| `41af7fd` test(live): cover stale protective deal and delayed manual exit | 等价 main `test_h202_1_old_partial_sl_plus_delayed_manual_close_pending_holds` |
| `1dbd930` test(live): verify exit-consumption watermark survives executor restart | 等价 main `test_h202_5_restart_keeps_consumed_watermark` |
| `f9bc680` test(live): verify consumed partial-exit deal watermark survives restart | 等价 main `test_h202_5`（同上场景族） |
| `b35a56f` test(live): verify delayed partial-exit pending state survives restart | 等价 main `890838b`/`814fe7e` tests（`test_r6_pending_exit_survives_restart_and_deal_query_failure`） |
| `d10f61a` fix(live): treat observed partial manual reductions as consumed, not full exits | 等价 main `814fe7e` 2A R4（部分减量：同 ticket 量减也要解释+消费；`test_h202_2_multiple_partial_sl_each_explained_and_consumed`） |
| `c18fd6b` fix(live): preserve partial-reduction semantics and restore confirmed external-exit intent | 等价 main `814fe7e` 2A R4 + `890838b` T30（Intent pending/confirmed 状态机与外部出场意图恢复） |
| `49c4b8f` fix(live): avoid double counting pending partial-exit volume | 等价 main `814fe7e` 2A（水位按 ticket/volume 记账，pending 视图不重复计量；`test_h202_3_repeat_old_deal_not_reconsumed`） |
| `64ec5e6` fix(live): journal ambiguous risk-reducing close outcomes too | 等价 main `814fe7e` 1A（journal 覆盖所有发单方向的未知回报，不限开仓腿） |
| `c09ed67` fix(live): persist consumed-deal and pending-exit state across restart | 等价 main `814fe7e` 2A（consumed 文件 + state-<magic>.json 重启装载） |
| `05b7e32` fix(live): clear prepared marker for confirmed no-order outcomes | 等价（按设计消除）main `814fe7e`：hold/duplicate 在写 journal 前返回，无"prepared 但零订单"状态可滞留（`test_hold_when_already_at_target`） |
| `60dd9d0` test(live): ensure ordinary hold clears prepared execution marker | 同上，等价 main `814fe7e` tests（hold 路径零 journal 写） |
| `dbebfc5` fix(live): write acknowledged intent through canonical reconcile schema | 等价 main `814fe7e`/`890838b`（`write_intent` 规范 Intent：STATUS_CONFIRMED、cause=`ack_execution:<id>`） |
| `cf4665d` fix(live): distinguish definite broker rejection from unknown order outcome | 等价 main `814fe7e` 1A + `f1860bd`（仅明确 TradeRejectedError 或人工仲裁落 rejected） |
| `05a826b` test(live): cover successful UNKNOWN reconciliation acknowledgement | 等价 main `test_h201_arbitration_tool_resolves_permanent_unknown` |
| `5989e07` feat(live): add guarded manual reconciliation for UNKNOWN orders | 等价 main `814fe7e` 1D（scripts/live_ack_execution.py：DEMO 身份核验+身份锁/目录锁同序抢占+挂单清空+journal 未决校验+意图先写终态后写；内容级抽查 §4.8） |
| `4a108b1` test(live): verify guarded manual UNKNOWN acknowledgement | 等价 main `test_h201_arbitration_tool_resolves_permanent_unknown` + tests/test_live_ack_execution.py |
| `40212e6` fix(live): preserve corrupt-state halt precedence over order uncertainty | 等价 main `814fe7e` R5（executor.py:878 `blocked_state_corrupt` 优先于 `blocked_execution_unknown`，平仓不受限） |
| `d02bced` fix(live): retain execution reconciliation evidence in durable state | 等价 main `814fe7e`（journal/state 持久留证：撕裂行保留、终态 append-only、`consumed_written` 标记） |
| `7f5cdac` fix(live): treat malformed execution markers as unresolved orders | 等价 main `814fe7e` R5（journal 撕裂/畸形行按 undecided 处理不 crash；`test_h201_8_journal_torn_tail_no_crash_blocks_opening`） |
| `24d5d30` fix(live): fail closed on malformed persisted execution marker types | 等价 main `814fe7e` R5（state 畸形→`state_corrupt` 挡开仓；journal 畸形→unknown） |
| `dd60434` test(live): fail closed when execution marker JSON type is malformed | 等价 main `test_h201_8` + state_corrupt 测试组 |
| `d04c0a7` fix(live): derive runner lock from stable MT5 account identity | 等价 main `814fe7e` 1C/R6（`instance_lock_key(server,login,magic)` 哈希→`%LOCALAPPDATA%` 锁目录，跨数据目录互斥；内容级抽查 §4.5） |
| `d3b2a58` fix(live): enforce account-wide runner lock before first live decision | 部分等价 main `814fe7e` 1C：连接后首决策前升级身份锁，读不出身份即拒启（rc=3）——粒度为 server\|login\|magic，**非跨 magic 账户级**（设计差异 §3.1） |
| `fa5a2c0` fix(live): require account-wide runner lock for UNKNOWN acknowledgement | 等价 main `814fe7e` 1D（仲裁工具与 runner 抢同一身份键内核锁、同序获取；持有者在线则仲裁拒绝）——同为 magic 粒度（§3.1） |
| `b3d1fb1` test(live): verify account-wide lock across data roots and magic ids | 等价 main `test_h201_cross_data_dir_same_identity_second_refused` + `test_h201_instance_lock_key_is_identity_based`（跨数据目录同身份拒二；跨 magic 不互斥为 main 有意设计） |
| `56db28a` test(live): use stable MT5 account identity for acknowledgement lock | 等价 main（live_ack_execution `_demo_identity` 读 server/login 定身份键 + 期间换账拒绝） |
| `b17603f` fix(live): fail closed if account lock cannot be acquired | 等价 main `814fe7e`（live_runner：锁获取失败→rc=3 拒启） |
| `1e54d6c` fix(live): handle account-lock filesystem failures before live loop | 等价 main `814fe7e`（账户状态读失败→释放临时锁+关网关+rc=3；"无身份不交易"） |
| `80a532f` fix(live): correct account lock class declaration formatting | 无等价必要（main 侧 `MagicInstanceLock` 为重写实现，类声明形态不同；纯格式修正） |
| `72495bf` fix(live): refuse UNKNOWN acknowledgement without trustworthy history start time | 等价（异构）main `814fe7e` R3：重启裁决要求成交历史窗覆盖（`test_h201_5_unknown_no_fill_blocked_until_history_window_covers`）；仲裁工具不查历史而查当前持仓+挂单并在查询失败时拒绝提交（fail-closed 同效） |
| `b341373` test(live): keep UNKNOWN when execution timestamp cannot bound broker history | 等价 main `test_h201_5`（历史窗未覆盖→保持 unknown 挡开仓） |
| `e4bf9a3` fix(live): write confirmed intent before clearing durable UNKNOWN marker | 等价 main `814fe7e`/`716843e`（journal 形态同序约束："Write the recoverable intent FIRST … A crash here leaves pending/unknown intact"；终态最后提交、撕裂保留） |
| `f83cb75` test(live): preserve UNKNOWN if acknowledgement state commit fails | 等价 main `716843e`（仲裁终态最后落盘+过程失败可重试语义；`test_h201_arbitration_tool`） |
| `93255d8` fix(live): validate state and intent JSON before UNKNOWN acknowledgement | 等价 main live_ack_execution（journal 根类型/未决表/decision_id 非空/状态枚举校验，畸形即拒绝且不写终态） |
| `b9b59c0` fix(live): validate UNKNOWN marker and strategy symbol before manual resolution | 等价 main live_ack_execution（magic/journal 归属、decision 必须在未决表、symbol 与券商实际持仓一致性、单品种约束） |
| `061a755` fix(live): scope manual exit acknowledgement to its bound event | 等价 main `890838b`/R7-N-C1（live_ack_external `confirmed_events` 绑定被确认 ticket 集合+时刻；`test_r7_nc1_old_ack_cannot_release_new_external_exit`、`test_r7_ack_without_event_binding_does_not_release`） |
| `7cf5309` test(live): accept no-op hold when uncertain order already matches broker state | 等价 main（未决存在时开仓腿挡、hold/平仓照常：`test_h201_pending_broker_order_keeps_execution_unknown` + hold 路径测试） |
| `23638c4` fix(risk): add independent watchdog for stalled risk polling loop | 等价（更强）main `6d85ef1` T110：外部 supervisor 按心跳检测卡死→`write_halt_once` fail-closed halt+告警，且覆盖整进程死亡（进程内线程不能）；内容级抽查 §4.4 |
| `107dd46` test(risk): verify stalled-loop watchdog fail-closed halt | 等价 main `test_t110_health.py`/`test_t110_supervisor.py`（心跳超时/未来时间戳/权限错误有界重试/halt 优先于账户装配） |
| `72f9a8d` fix(audit): include H2-03 base-currency snapshot propagation | 等价 main `f4dd503`（同 e179aee 载体；分支此提交即 H2-03 的落点汇总） |
| `e0008a7` docs(audit): track remediation evidence and operational acceptance boundary | **无逐字等价文件**（main 无 audit2-remediation-status.md）；其内容已被取代与覆盖：整改=H线基石计划（`docs/superpowers/plans/2026-11-01-hline-foundation.md`）+H2-01 系列提交、验收边界=2026-11-05 demo-readiness 裁定系列+runbook §6（supervisor/killSwitch/证据窗）。该文档本身是 PR#3 的进度追踪表，对象已不存在——无保留价值 |
| `8492ee3` docs(ops): document UNKNOWN execution acknowledgement and risk watchdog | 等价 main runbook（§5.4 仲裁工具与"禁止删 journal 清待决"+§6 supervisor 检测/停止；f4dd503/6d85ef1 落笔） |
| `623b7bb` docs(ops): clarify cross-data-root account lock maintenance procedure | 等价 main runbook §5.3（跨数据目录进程锁：身份键哈希、OS 状态目录、"不得手工删锁文件绕过内核锁"、退出自动释放） |
| `638ebcf` docs(live): describe account-scoped and paper runner lock behavior | 等价 main runbook §1/§5.3 + scripts/live_runner.py 模块头（实跑身份锁/paper 目录锁/dry-run 不持锁三态） |

## 3. 设计差异（两处，均非缺失）

1. **实例锁粒度**：分支=账户级（server/login 下跨 magic、跨数据目录互斥）；
   main=身份键 `sha256(server|login|magic)`（R6 有意裁定："login 跨券商可撞号"
   故带 server；同账户多 magic 并行是 main 的既定用法，demo-runbook §1 第 5
   条与 §5.3 明文）。实际竞争面（同 magic 双启动、仲裁 vs 运行器、跨数据
   目录）两者等价；跨 magic 互斥是分支更严但与 main 多 magic 设计冲突，
   不构成缺陷。相关提交：d3b2a58/fa5a2c0/b3d1fb1。
2. **看门狗架构**：分支=风控进程内独立线程（轮询循环卡死→写 halt）；
   main=T110 外部 supervisor（每分钟巡检心跳，卡死**或整进程死亡**→
   fail-closed halt + 需人工；`live_supervisor.py` + test_t110_*）。main
   方案覆盖面严格更宽（进程内线程无法覆盖进程死亡/被杀）。相关提交：
   23638c4/107dd46。

## 4. 内容级抽查记录（8 个，非标题比对）

1. **e179aee vs f4dd503**：逐行对照 mt5gw 三文件——`currency_base: str|None =
   None` 字段、`getattr(row,"currency_base",None)` 旧 row 回退、注释同义
   （"旧终端/旧快照缺失时保持 None，不得用 currency_profit 冒充" vs
   "None=未知…绝不用 currency_profit 代理"）。main 侧额外有
   test_costs_swap.py mode=2 集成与旧快照拒绝测试。
2. **23c1a0d vs f4dd503**：分支改 2 处夹具（currency_base="XAU"/None 断言）；
   main 测试含同型断言（test_snapshot/test_symbols_market）+ 更强的
   `test_h13_symbol_mode_uses_currency_base_when_present`（EUR 基础币 vs
   USD 利润币不得代理）。
3. **2eba121 vs 814fe7e**：分支加 `consumed_deal_tickets`/
   `unmatched_exit_volume`（state 内嵌）；main 为独立
   `consumed-deals-<magic>.jsonl`（ticket/volume/time/reason/ts_consumed）
   + 轮次/重启装载——同一水位语义，main 持久化更强（独立文件、
   `consumed_written` 丢失检测）。
4. **23638c4 vs 6d85ef1**：分支 `_risk_watchdog` 线程+`_write_stall_halt`；
   main `live_supervisor.py` 心跳超时→`write_halt_once("risk_lost"/...)+`
   需操作员，且 supervisor "never authorizes a kill or a duplicate worker"
   （比分支的线程方案多覆盖进程死亡与重复拉起风险）。
5. **d04c0a7 vs main runner.py**：分支 `MagicInstanceLock.for_account/
   account_lock_path`；main `instance_lock_key(server,login,magic)`→
   `runner_lock_path`→`%LOCALAPPDATA%\mt5-trading-plus\locks\runner-<hash>.lock`，
   live_runner.py 连接后升级身份锁（临时 magic 锁→身份锁），语义同构。
6. **1c79285 vs 814fe7e**：分支 `pending_intent_found_on_restart`/
   `process_restarted_before_confirmation` 状态机；main journal
   pending→sent→known_result + `_run_journal_recovery` + 四道开仓挡板
   （state_corrupt→pending_external_exits→journal 未决→broker 挂单），
   "平仓不受限"同款。
7. **fa5a2c0 vs main live_ack_execution.ack**：分支要求账户级锁空闲才能
   解除 UNKNOWN；main `ack()` 以 `attempts=1` 抢同一身份键 file_lock +
   旧目录锁（与 runner 同序），抢不到即"保持停机并核对后重试"——
   同效（magic 粒度，§3.1）。
8. **5989e07 vs main live_ack_execution.py**：分支守卫（身份/挂单/decision
   匹配/证据）；main 守卫清单逐项对上：`_demo_identity`（trade_mode=0+
   server/login）、`_no_pending_orders`、未决表归属、旧终态禁改写、
   持仓查询失败不写终态、意图先写终态后写、期间账变/journal 变拒绝。

## 5. 红旗清单（无等价且有价值 → 不删）

**无。** 51 个提交全部有等价或更强实现（§2）；两处设计差异（§3）为
main 侧有意裁定且在 runbook/计划留痕。删除 `audit-fixes-2026-10-30`、
`audit-live-safety-2026-10-30` 两分支不丢失任何独有价值（删除动作由
主会话执行）。

---
*审计基线：origin/main=23edcff；分支基 d67476a；patch-id 扫描 51/225
零重叠；本文全部结论可用上述 hash 在任一检出复验。*
