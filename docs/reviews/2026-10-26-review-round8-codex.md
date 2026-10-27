# 第八轮独立终审报告（Codex，R8 收尾施工）

审查范围：`8e8bb76..29109af`，审查时 HEAD 为 `29109afd532fee4072e4963bf2af88e87d9eb692`。文件名沿用任务指定的 2026-10-26，实际复核日期为 2026-10-27（Asia/Taipei）。

范围校正：`git rev-list --count 8e8bb76..29109af` 实际为 **12 个提交**，不是 11 个，包含 `e8e9881`、`d95d572` 两次并发测试调整。R7 主要代码修复位于区间之前的 `fe93834`；本轮根据任务要求一并检查其在当前 HEAD 的终态，不能把它们都算作这 12 个提交新增的实现。

基线与方法：初始工作区干净；执行 `.venv/Scripts/python.exe -m pytest tests/ -q`，结果 **1642 passed in 229.87s**。读取五个正式分片、合并 JSON/Markdown、四组对账 CSV/summary、影子盘账本/运行日志/归档指令，并运行 [P14–P19 只读探针](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/reviews/probes/2026-10-26-round8-probes.py:1)，结果保存在 [脱敏结果 JSON](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/reviews/probes/2026-10-26-round8-probes.json:1)。台账以 SQLite `mode=ro` 连接核验，绕开会执行迁移的构造函数。没有运行生产 G5、screen、新回测、模拟盘启动或下单，没有读取原始锁箱期行情，也没有新增 `--ops-ack` 豁免；历史四次豁免仅作审计核对。

### 结论：可合并？修后可

**修后可；当前不建议以“R8 收尾完成、三验收通过、阶段 F 已就绪”的状态合并或放行。** 本轮没有新增 Critical 级发现，但有 **7 项 Important、2 项 Minor**。阻塞集中于 CI 白名单仍可绕过、合并器仍可接受整臂缺样、对账残差错误归因，以及阶段 F 启动/验收工具与蓝图目标不符。

R7 四项主要 Critical 代码缺陷——净零残仓误 ack、旧人工确认放行新事件、影子账户风控缺少聚合一致视图、台账写事务不原子——**针对原复现场景已修**，有对应回归且全量测试通过。不能因本轮发现其他问题，抹掉这些已完成的修复。

三份报告应分别判定：

| 产物 | 本轮结论 |
|---|---|
| **T42 / T39 gate-power-v2** | 本批五分片来源、成本加载、合并算术及三节补充通过核实；只能保留为有运行证据的诊断实验。正式统计验收的解释仍有错误，合并器也未达到“完整条件与身份均强校验”的声明。 |
| **T43 / T40 reconciliation-v3** | 8,364 配对、158 条已列残差、2,119 条纯 stop 子集等数量真实；但错误的越幅分量掩盖了另外至少 55 条超容差残差，“机制一致性通过”的总体结论不成立。 |
| **T44 / T34 live-paper** | 83 决策、21 成交、5 次 SL、零调仓、两次真正执行全平等核心影子盘事实成立；可保留为 **D′ 已观测场景通过**，须更正事件时间线。它不能证明当前 T45 工具已经准备好完成阶段 F。 |

**阶段 F 暂缓按现有脚本启动；screen 继续待终审；G5 维持禁跑。** 阶段 F 的主要阻塞是 T45 的模式、生命周期和验收判据，不能解释为还要重新证明全部已通过的 D′ 场景。screen 还需满足蓝图已有的隔离三层前置；U0 的 ACL/分支保护不能因本轮测试通过而视为落地。

### 修复复核表

“已修”只针对 R7 原问题及本轮实际核对的边界；“部分”表示仍有实现或验收证据缺口。R7 新编号与对应原编号合并列示；此前已修项说明是否维持，不冒称重新做过真机测量。

| R7 编号 | 判定 | 证据与边界 |
|---|---|---|
| **C1 全平部分成交 / 净零残仓** | **已修** | [executor.py:706](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/executor.py:706) 执行后重新读取 `positions()` 与 `pending_orders()`，两者都为空才 `write_ack`；净额不再是完成判据。[净零残仓回归:627](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_executor.py:627)、[部分成交重试:438](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_executor.py:438)、[撤挂单:483](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_executor.py:483) 均在本轮全量中通过。T44 第二次两策略各 `closed=1`，P18 核实。 |
| **C2 / N-C1 外部出场与人工确认** | **已修，针对旧确认重用** | [live_ack_external.py:122](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/live_ack_external.py:122) 写被确认 ticket 集、时刻、净仓及持仓明细；[executor.py:889](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/executor.py:889) 要求 ticket 命中、事件时刻不晚于确认、当前净仓匹配，旧格式不放行。[旧确认回归:669](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_executor.py:669)、[完整链路回归:1877](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_integration.py:1877) 通过。实际消费检查的是净仓快照，不应写成“逐持仓明细版本校验”。 |
| **C3 / N-C2 多运行器影子账户** | **已修，针对原三项复现** | [live_runner.py:423](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/live_runner.py:423) 给同步闸门注入目录聚合 `PaperAccountView`；[gate.py:306](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/gate.py:306) 只取一次 snapshot；[paper.py:592](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/paper.py:592) 读失败抛异常，[paper.py:617](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/paper.py:617) 每文件只读一次并从同一文档取持仓/成交。[跨 magic 上限:892](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_paper.py:892)、[读失败:940](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_paper.py:940)、[亏损不双计:993](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_paper.py:993) 通过。跨文件时刻不一致仅标记，不是全账户同一物理时刻的原子快照；重复启动同一 magic 的新路径另见 R8-I6。 |
| **C4 / N-C4 越幅加载与错误验收声明** | **已修核心加载及复跑证据；文档仍有陈旧段落** | [run_backtest.py:601](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/run_backtest.py:601) 装载新越幅表；P19 调用实际加载器，主库/研究库四品种全部 `loaded`、sha=`eca09e24`。台账 **run#364–430，67/67 含 `ov[eca09e24]`**，覆盖六卡；[v3 修订二:36](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/research/gates-calibration-v3-2026-10-26.md:36) 已撤回 #279–345 的错误解释。本次 T42 五序列也含 ov。旧报告前半段仍将更早无越幅运行描述为含越幅，需加历史口径标记，不能抹掉新修订的有效性。 |
| **C5 / N-C3 台账事务原子性** | **已修** | [ledger.py:356](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:356) `BEGIN IMMEDIATE`，异常统一回滚；[record_run:700](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:700) 验链与追加同事务；[record_gate:730](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:730) 更新/审计/快照同事务；[snapshot_refresh:398](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:398) 不再自提交。[并发写:957](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_research_ledger.py:957)、[中途失败整体回滚:1016](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_research_ledger.py:1016) 通过。生产库只读 `verify_chain()==[]`。 |
| **C6 运维豁免进入研究 / G5 根目录** | **维持已修** | [screen 拒绝 ops_ack:603](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_research_bt_script.py:603)、[gate 拒绝 ops_ack:635](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_research_bt_script.py:635) 本轮全量通过；生产 G5 未执行。T43 的历史 ops 记录不构成研究命令或 G5 放行。 |
| **C7 日内零假设** | **实现修复维持；统计解释部分闭环** | [slot 形状联合置换回归:511](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_validation_synthetic.py:511) 通过，新分片对应修复后提交。正式解释承认槽位无条件均值保留，[报告:38](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/research/gate-power-v2-2026-10-26.md:38)。因此能支持的是特定路径/次序依赖诊断，不能据此证明所有日内优势为零或完成真实 G5 的判别力标定，见 R8-I7。 |
| **C8 / N-I4 合并器条件、种子与实验身份** | **部分** | [merge_gate_power.py:394](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:394) 已能拒绝“某 setup×level 整组缺样”；指纹差异也会拒绝，相关测试与 P14 通过。但应存在的实验臂仍从产物有无区块推导，删除整个 G5 区块可被接受；`max_bars`、G5 cutoff 等不比较，见 R8-I2。 |
| **I1 / N-I2 多写者文件锁** | **已修** | [fileio.py:67](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/fileio.py:67) 使用 Windows `msvcrt.locking` / POSIX `flock`；[fileio.py:112](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/fileio.py:112) 取锁后才操作，释放不删路径，无超龄接管。[旧 mtime 不抢活锁:223](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_fileio.py:223)、[释放不删后继锁:252](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_mt5live_fileio.py:252) 及并发指令测试通过。 |
| **I2 swap 费率成本身份** | **已修** | [run_backtest.py:592](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/run_backtest.py:592) 将实际 long/short 每夜费率与三倍日规范化哈希后入版本串，覆盖 calibrated/snapshot/显式零路径。[实测费率:1021](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_run_backtest_script.py:1021)、[快照费率:1036](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_run_backtest_script.py:1036) 通过；五序列实际串含相应 `swap-snapshot[sha]`。 |
| **I3 越幅统计口径** | **维持已修** | [前定价位反例:80](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_costs_overshoot.py:80)、[多空手算:105](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_costs_overshoot.py:105) 通过；P19 证明本次缓存可装入新模型。新问题是对账拆解把非 stop 出场也当成越幅，见 R8-I3，不能归咎于缓存口径本身。 |
| **I4 CI 白名单信任边界** | **部分，仍可绕过** | `2490afa` 去掉非 `research/*` 分支直接成功路径，并维持 main 检查器。但 [workflow:57](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/.github/workflows/research-allowlist.yml:57) 直接插入不可信 head_ref，P15 复现任意合法分支名中的命令注入可成功跳过检查，见 R8-I1。workflow 自身来源/受保护执行入口也仍属未闭环信任边界。 |
| **I5 实盘点差 v2 加载** | **维持已修** | v1/v2 包装与内容校验实现未被本区间破坏；现有加载/入口测试本轮全量通过。T44 的两策略确有报价和拒单证据；这不等于真实经纪商滑点验证。 |
| **I6 同毫秒 tick 身份** | **已修原保护性成交反例** | [reconcile_ab.py:363](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/reconcile_ab.py:363) 消歧入场毫秒内序号，[reconcile_ab.py:420](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/reconcile_ab.py:420) 从入场 tick 后逐笔搜索并取出场毫秒首笔穿价。[同毫秒后续报价:389](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_reconcile_ab.py:389)、[首笔穿价成交价:611](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_reconcile_ab.py:611)、[入场同毫秒更早穿价:650](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_reconcile_ab.py:650) 通过。B 产物没有输出稳定 tick 序号，检查器根据价/序推导；不应扩大为所有成交类型都已记录显式 tick ID。 |
| **I7 EURUSD 禁入窗依据** | **维持已修，限既有观测** | 本区间没有提供新的全年观测证据，沿用 R7 对 [rollover/DST 报告:137](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/research/rollover-dst-2026-10-25.md:137) 的限定；本轮未重新读取 tick 测量。 |
| **I8 测试场景过于理想化** | **部分，持续项** | 净零残仓、旧确认、跨策略风控、台账回滚等回归已增加并通过。但 CI 测试仍主要检查工作流文本，未覆盖 P15；T45 的缺证据假通过、重复启动也未被现有全量捕获。`1642 passed` 不能代替这些业务不变量。 |
| **M1 P8 暂停文件探针** | **维持已修** | [既有探针:34](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/reviews/probes/2026-10-25-round5-probes.py:34) 同时检查全局与 magic 暂停；本轮未重跑旧探针，新的事件回归与 P18 分策略证据另列。 |
| **M2 合并脚本绝对路径** | **维持已修** | [merge_gate_power.py:52](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:52) 同目录定位/显式环境变量兜底，相关测试本轮通过，未见个人路径兜底回归。 |
| **M3 蓝图与证据索引** | **部分** | 新状态已补，但 P2 仍称缓存旧版与 swap 未入身份；P5 称六卡待复跑，与 67 条新记录冲突；P3“配对/时间核对全零”与 86 项时间违规冲突；P8/§3/决策记录仍陈旧。见蓝图更新建议及 R8-M2。 |
| **N-I1 正式运行指纹** | **本批来源已补；完整身份仍部分** | [gate_calibration.py:194](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/gate_calibration.py:194) 与 [指纹回归:1020](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_gate_calibration_script.py:1020) 已落地；五分片指纹一致，脚本与 git 提交经换行规范化后相符。但没有各输入 K 线内容/实际范围/规格哈希；全部缺 fingerprint 也能通过“相等”校验，不能宣称完整数据身份已封闭，见 R8-I2。 |
| **N-I3 审计事件内容与外部锚前缀** | **已修原复现** | [ledger.py:378](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:378) 哈希审计行完整内容，[ledger.py:641](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:641) 对比摘要；[ledger_anchor.py:251](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/ledger_anchor.py:251) 行数增长仍重放历史前缀，[写锚前:288](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/ledger_anchor.py:288) 同样先核验。[重封后追加:137](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_ledger_anchor.py:137)、[拒绝补锚:163](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_ledger_anchor.py:163)、合法更新回归均通过；P19 生产链及锚核验均空错误。 |
| **N-M1 校准 history/converged 入 JSON** | **已修** | [gate_calibration.py:1390](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/gate_calibration.py:1390) 保存完整校准记录，[产物字段断言:297](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_gate_calibration_script.py:297) 通过。P14 核对八组均有 history/target/tol/converged；USTEC 1.0 档 `converged=false`，history 7 条，实测 1.343020879。 |

### 新发现问题（Critical / Important / Minor）

**Critical：本轮未发现需要新增至该级别的问题。** 以下 Important 包括本轮新增工具缺陷，以及在新验收产物中复现的 R7 残留；不会把报告口误升级为资金安全事故。

**Important R8-I1：CI 的 head_ref 直接插入 Bash，分支名仍可绕过白名单。**

位置：[research-allowlist.yml:57](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/.github/workflows/research-allowlist.yml:57)。GitHub 表达式在 shell 执行前替换，`head="${{ github.head_ref }}"` 并不能安全引用分支名。

P15 使用合法 Git 分支名 `audit";exit${IFS}0;#`，只运行该赋值行和无害检查器标记：普通分支到达标记并返回预设 42；该分支 **返回 0，检查器标记完全未执行**。`git check-ref-format --branch` 返回成功。没有创建分支、触发 GitHub Actions 或访问外部系统。

影响：即使 PR 改到禁止路径，也可在 `test -f baseline/...` 和 Python 白名单校验之前绿色退出。现有 [workflow 回归:358](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_p6_docs.py:358) 只能证明旧前缀分支不存在，不能证明所有 head_ref 都真正执行检查。建议通过步骤 `env` 传值、shell 仅引用环境变量，并增加真实 shell 反例回归。另需可信工作流入口；单把同名 job 设为 required，不能阻止 PR 版工作流将该 job 改成成功返回。

**Important R8-I2：合并器仍接受整臂缺失，实验身份检查也不完整。**

位置：[merge_gate_power.py:335](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:335)、[G5 条件生成:355](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:355)、[G5 合并:146](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:146)、[身份键:186](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:186)。

P14 在正式五分片的内存/临时副本中删除 shard8-0 的整个 `g5` 区块：`_seed_problems()==[]`，实际合并入口 **返回 0**，输出 `meta.seeds=20`，但 G5 只剩 **384 条记录 / 16 个种子**。删除单个 setup×level 条件现在会拒绝，删除整个实验臂却让检查器不再要求该臂存在。

同时，改变某分片 `meta.max_bars`、`g5_cutoff`、`g5_window_source` 均无身份错误；全部分片删除 fingerprint 也无错误。运行指纹没有实际 K 线内容/范围或合约规格身份，[fingerprint:211](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/gate_calibration.py:211) 仅包含 git/dirty/gates/cost_versions/脚本。

建议从不可缺省的实验设计推导应有实验臂、条件和检查集合；校验窗口、截断参数、输入数据/规格及指纹必填项。兼容无指纹的历史诊断产物时，应显式降低产物等级。**本批原始五分片未发现实际缺样或指纹不一致**，无需因该防线缺陷直接宣布这 800 条原始记录失效。

**Important R8-I3：A 为 stop、B 为 signal/TP 时，价差被错误吸收到“止损越幅”，T43 净利符合率失真。**

位置：[reconcile_ab.py:477](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/reconcile_ab.py:477) 只检查 `reason_a == "stop"`；[实际越幅回推:501](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/reconcile_ab.py:501) 不要求 B 真正止损，也不拒绝负的“越幅”。

P17 的纯函数反例：A 多仓入场 100、止损 90、净利 −10；B 同价入场，后来按 signal 在 150 出场、净利 +50。函数输出 `net_diff=60, overshoot_comp=60, rest=0, tol=1, ok=True`。这 60 是不同出场路径的收益差，不是 B 穿过止损价位的成本。

实际产物中也存在：

| 记录 | 实际净利差 | 被算作越幅 | 剩余残差 / 容差 | 当前净利判定 |
|---|---:|---:|---:|---|
| USTEC immediate，2026-06-16 14:35 入场，A stop、B 15:46:22 TP | +271.8154 | +271.6196 | 0.1958 / 1.9584 | true |
| USTEC ASC，2026-06-29 13:30 入场，A stop、B 13:30:32 TP | +139.4825 | +139.2955 | 0.1871 / 1.9137 | true |

原始行：[immediate diff.csv:3420](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/data/reports/reconciliation-v3/USTEC_M5_20261026T105826Z_2fe72d/diff.csv:3420)、[ASC diff.csv:2223](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/data/reports/reconciliation-v3/USTEC_M5_20261026T105954Z_1835cc/diff.csv:2223)。

四组共有 **61 条 A-stop/B-nonstop**；仅取消这些行不适用的越幅分量、其余公式不动，就有 **55 条原先 net_ok=true 的记录变成超容差**。原 158 行仍真实存在，但不是所有应浮出的净利差异。其余 `row_ok`/原因检查仍会拒绝这些记录，因此这不是整行违规被完全抹掉；失真的是净利分量、符合率与“158 行已经解释全部残差”的验收陈述。

建议只对满足可比条件的 stop↔stop 行使用该越幅差分；非同因出场保留为路径/原因分叉，修复拆解后重算 CSV/summary 与报告，不能先把新增残差重新命名为成本。纯 stop 同 bar 的 2,119 行零违规可以作为独立子集保留。

**Important R8-I4：T45 提供的是延长影子盘，不能完成蓝图阶段 F 的模拟账户验收。**

位置：[demo_run.ps1:188](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_run.ps1:188) 无条件传 `--paper`，风控/对账也固定 `--paper-state`；[demo_check.py:528](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_check.py:528) 只读 PaperBroker 成交。[运维手册:3](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/ops/demo-runbook.md:3) 明确把“阶段 F”重新定义为 paper 集群。与此同时，[蓝图:176](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/blueprint.md:176) 定义 F 为 0.01 手两周模拟盘，[T44:164](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/reports/live-paper-2026-10-26.md:164) 将真实滑点证据留给 F。

现有脚本跑满两周仍不会产生经纪商成交滑点，也不能补经纪商服务器实际 SL/TP/拒单/重连证据；MA 示例 [配置:16](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/demo-configs/MA-XAUUSD-M15.toml:16) 使用 `risk_fraction=0.005`，也没有把手数限定为 0.01。应明确区分 paper soak 与 MT5 demo 阶段，提供符合蓝图的模拟账户配置及保险丝预检，再声明“阶段 F 就绪”。本审查没有启动任一模式。

**Important R8-I5：demo_check 会在没有交易、心跳、演练或点差证据时给出最终“全部通过”。**

位置：[无 SL/TP 样本直接通过:411](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_check.py:411)、[无超时告警即认为连续:463](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_check.py:463)、[无点差样本通过:505](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_check.py:505)、[汇总:614](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_check.py:614)、[退出码:666](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_check.py:666)。

P16 在临时目录仅放 **一个空仓 confirmed 意图**和**一天写着“当日无点差样本”的日报**；没有 paper 状态、成交、心跳、进程日志或演练告警。实际 CLI 输出 **7/7 通过，覆盖 1 天，RISK_FLATTEN=0、换日拒单=0（同时打印“未达标”），最后仍“全部通过”，退出码 0**。

根因：日常“没有发现异常”与阶段“取得验收证据”共用布尔结果；全期要求仅打印、不参与最终结果。另以 `RISK_FLATTEN` 告警条数代替两个独立指令及其真实清仓 ack，无法自动证明“第二次同样全平”。建议把无样本/未覆盖保留为未决，最终验收强制检查连续交易日、各进程存活覆盖、SL/TP、演练完成、成交归因及实际成本证据；允许日常检查宽松显示，但其返回值和最终验收必须区分。

**Important R8-I6：运维手册的恢复/单进程重启步骤会再启动整组运行器，破坏每 magic 单写者假设。**

位置：[手册恢复:51](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/ops/demo-runbook.md:51)、[单进程重启:59](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/ops/demo-runbook.md:59)、[启动循环:183](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_run.ps1:183)、[PID 覆盖写:217](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/demo_run.ps1:217)。

killSwitch 只阻止新开仓，不终止运行器。手册随后删除 killSwitch/halt 并重跑整个 `demo_run.ps1`；脚本只核对本批配置 magic 互异，没有检查现有 PID/运行器，也没有生命周期单实例锁。单独杀一个 PID 后重跑全组同样会复制其余进程；新的 `pids.txt` 还会覆盖旧 PID 列表。

这会让同 magic 的两份执行器/影子券商共享 `state-<magic>.json`、意图与下单日志。[PaperBroker.save:350](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/paper.py:350) 用各自内存状态整份写回；构造时的 [_load:370](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/paper.py:370) 不是多写者事务。原子替换只保证文件完整，不能防止覆盖另一进程的成交状态。该发现来自启动/恢复路径静态核对，未启动真实或影子订单进行复现。建议增加按运行目录/magic 的全生命周期排他锁或显式检测现有实例；恢复仅移除停机信号，重启应针对已退出进程，保留可核对的完整进程清单。

**Important R8-I7：T42 对结构性 flags 的描述与产物相反，统计验收措辞仍超过实验能力。**

证据：[报告:74](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/research/gate-power-v2-2026-10-26.md:74) 称 `G1.trade_count` 不参与标记，但 [正式合并 md:133](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/data/reports/gates/gate-power-v2/gate-power.md:133) 仍以“误报 100%”列它为调整建议；[STRUCTURAL_CHECKS:857](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/gate_calibration.py:857) 只排除了 G5.trade_count。13 flags 中这一条确实存在，不是渲染残留。

本版已正确承认完整组合正负对照均 0/160、USTEC 1.0 档未收敛、共享历史窗口、G5 使用替代参照；这些限定是进步。但 [验收陈述:85](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/research/gate-power-v2-2026-10-26.md:85) 随后称“关卡标定的正式统计验收就此补齐”，没有消除 R7 要求的独立强度校准、可比 G3 样本外参照或不确定性问题。它可以完成“T42 指定重跑及诊断展示”，不能同时证明“T39 判别力已达到正式放行标准”。

另 [报告:42](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/research/gate-power-v2-2026-10-26.md:42) 将 XAUUSD 更高 G5 通过率归因为“参照 p5 更低”：实数为 XAUUSD **−1.341～−0.930**，USTEC **−2.993～−1.688**，方向相反。单凭通过率与成本版本也不能把 v1→v2 的所有变化唯一归因为越幅，期间还有零假设实现/代码变化，未做控制单变量的归因实验。

建议移除结构性伪误报建议并重渲染；保留 13 条作为历史诊断时须逐条标注有效性。完整组合功效、校准有效强度与真实 G5 参照验证继续列为未验收，或明确降低该里程碑为诊断工具验收。**本区间 `config/gates.toml` 无差异，flags 确实没有被自动应用；“只建议”的执行边界成立。**

**Minor R8-M1：T44 黄金事件时间线和轮数与原始证据不符。**

报告 [第 78 行](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/reports/live-paper-2026-10-26.md:78)、[第 82 行](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/reports/live-paper-2026-10-26.md:82)、[第 142 行](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/reports/live-paper-2026-10-26.md:142) 把第三次 XAUUSD SL 写成 16:28、随后 16:30 重入空。实际是 **14:54:29 SL → 15:00:06 开空 → 16:30:06 信号翻多反手**，见 [原始日志:6988](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/data/live/paper-XAUUSD-M15.log:6988)、[15:00 决策:7156](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/data/live/paper-XAUUSD-M15.log:7156)、[16:30 决策:9800](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/data/live/paper-XAUUSD-M15.log:9800)；最后多仓平掉/转空是 **20:15**，不是逐仓表写的 20:00，见 [日志:16472](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/data/live/paper-XAUUSD-M15.log:16472)。

当前归档的对账一致轮数为 **1,208**、风控轮数 **3,622**，报告是 1,207/3,620；如统计截点不同应注明。10h06m 是该日的观测窗口，不宜写成完整覆盖所有 23 小时可交易时段。更正这些叙述不会改变 83/21、5 次 SL、零调仓和两次全平的有效证据。

**Minor R8-M2：蓝图及计划仍有明显内部冲突。**

[蓝图 P2:39](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/blueprint.md:39) 仍称缓存内嵌 v1、swap 未纳入版本；P5 第 42 行说六卡越幅逐卡复跑待做，但 run#364–430 及 v3 修订二已经存在；P8 第 45 行仍依赖“P7 在修/T34 重做”。页首仍引用旧审查基准，§3 包状态及 D19–D29 多处仍“待做”。[R8 计划 T43:41](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/superpowers/plans/2026-10-26-round8-finalization.md:41) 四项关键复选框仍未勾，不能称全部验收项附证据完成。另 P3“配对/时间核对全零”属于有实质影响的越级陈述，已并入 R8-I3，应改为具体计数及核验子集。

### 产物审查

**一、T42 / T39：gate-power-v2-2026-10-26.md 与五分片、合并产物。**

P14 从五分片重新调用合并函数，negative、positive、G5、flags 四个区块与正式 JSON **逐项完全一致**；补充 Markdown 也一致。另直接用原始 runs 计算计数、均值、中位数、极值与比例，未运行新的实验。

| 检查项 | 复核结果 |
|---|---|
| 五分片范围 | seeds 0–3 / 4–7 / 8–11 / 12–15 / 16–19；无重叠，本批各条件无缺样。 |
| 运行指纹 | 五份 `meta.fingerprint` 完全相同：git `2fa27a3f5f7577f0b3693ea296f67b5bfca35618`、dirty=true、gates `68f49237d1b5…`、脚本 `2f4813dcbade…`、五序列成本串。合并指纹相同。 |
| 脚本与 git 的对应 | 当前脚本原始字节 sha 与 fingerprint 相同。git blob 为 LF，工作树为 CRLF，直接字节 sha 不同；规范化换行后内容完全一致。因此这里不是未提交代码被伪装成相同提交。 |
| dirty 的归因 | [启动前状态记录:1](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/data/reports/gates/shard8-launch-state.txt:1) 时间为 12:37:33 UTC，HEAD 正确且唯一状态项为 `?? config/risk-demo.toml`；分片 meta 约 12:43。该配置后来在 2a0b386 提交，与实验加载路径无关。现有记录支持这一归因，但它是启动前约六分钟的快照，不是逐进程加载文件的证明。 |
| 越幅实际加载 | 五序列含 `ov[eca09e24]`；主库/研究库缓存经加载器校验四品种都 loaded，stop_level 模型可用。五个启动日志没有“缓存无止损越幅表”警告。此项结合加载器/缓存及指纹核实，不能只凭版本字符串“自证”。 |
| 运行条数 | negative 160 + positive 160 + G5 480 = 800；G5 误报臂去除重复 level 后 80 个不同条件，不等于 80 个统计独立样本。 |
| 完整组合 | 正对照 0/160，负对照 0/160（day 0/60，slot 0/100）；八个正对照 setup×level 均 0/20。算术正确。 |
| flags | 13 条 / 12 个不同检查；5 条低检出、8 条高通过率提示；随机入场双触发。未改冻结门槛，但 trade_count 建议与正文冲突。 |

八组逐种子 Sharpe 分布复算如下，均 n=20。表中“收敛”指 seed=0 的漂移校准是否在容差内，**不表示另外 19 个评估种子各自均达到标称 Sharpe**。

| 品种/周期 | 标称档位 | 均值 | 中位数 | 最小～最大 | 校准收敛 |
|---|---:|---:|---:|---:|---|
| XAUUSD H1 | 0.3 | 0.029 | 0.189 | −0.836～0.592 | 是 |
| XAUUSD H1 | 0.6 | 0.299 | 0.428 | −0.528～0.797 | 是 |
| XAUUSD H1 | 1.0 | 0.668 | 0.750 | −0.098～1.118 | 是 |
| XAUUSD H1 | 1.5 | 1.072 | 1.163 | 0.368～1.548 | 是 |
| USTEC M5 | 0.3 | 0.209 | 0.283 | −0.461～1.219 | 是 |
| USTEC M5 | 0.6 | 0.432 | 0.452 | −0.167～1.456 | 是 |
| USTEC M5 | 1.0 | 1.211 | 1.034 | 0.555～2.286 | **否** |
| USTEC M5 | 1.5 | 1.486 | 1.266 | 0.805～2.582 | 是 |

USTEC 1.0 档 seed=0 的校准 Sharpe 为 1.343020879，目标 1.0、tol 0.15、converged=false；JSON 和两份 md 都如实标注。不能把这一档逐种子都描述为“实际强于标称”，其最小值为 0.555。

分品种 G5 误报臂的 `passed` 与 `expectation_positive` 相同：XAUUSD slot **40/80=50%**、day **28/80=35%**；USTEC slot **4/80=5%**、day **0/80=0%**。每组 80 条由 20 个种子沿四 level 重复，去重分母应写 20；跨品种汇总 slot **27.5%**、day **17.5%**。合并表百分比算术正确。该表适用于本实验替代参照，不是经过前置关卡筛选后的真实 G5 误报率。

**二、T43 / T40：reconciliation-v3-2026-10-25.md。**

P17 用四份 diff.csv 重算；summary 计数一致，四份 `ok` 均为 false。未因“机制解释”而实际放宽配置容差，这一点可以保留。

| 组 | 配对 | only_a / only_b | 入场 / 出场时间违规 | 原因违规 | 原报告净利违规 |
|---|---:|---:|---:|---:|---:|
| XAUUSD H1 immediate | 382 | 1 / 1 | 0 / 0 | 0 | 18 |
| XAUUSD H1 ASC | 161 | 0 / 0 | 0 / 0 | 0 | 5 |
| USTEC M5 immediate | 4,170 | 50 / 1,277 | 21 / 27 | 34 | 73 |
| USTEC M5 ASC | 3,651 | 0 / 0 | 0 / 38 | 43 | 62 |
| **合计** | **8,364** | **51 / 1,278** | **21 / 65** | **77** | **158** |

`not_first_tick`、`not_last_tick`、`no_trigger_at_fill`、`fill_price_mismatch`、`earlier_cross`、`no_ticks` 六种标记为零，属实。实际还有 **36 个 before_ready、50 个 level_unknown**；后者恰是没有按该检查路径验证成功的记录。只能写“已执行且适用的这些子检查未报上述错误”，不能写“全部时间核对通过”或“无任一层行为错误”。

现有 158 条净利违规可按 CSV 重现四分类：**TP 穿价 121、TP gap 12、原因分叉 18、跨 bar 配对 7**；151/158 为 B 净利较有利。这支持报告对已列记录的分组，但分类名称并不替代逐笔数值归因，且 R8-I3 证明清单漏了被错误越幅抵消的记录。建议补每行分类、应解释金额及归因后剩余残差，保留原因/时间违规，修正整体符合率后再验收。

纯 stop↔stop、同 bar 入场子集 **134+62+1,046+877=2,119**，现有净利违规为零，算术成立。USTEC immediate 的 A/B 终止时点也能从 trades 净利累计核实：A 在 **2026-07-17 15:40** 首次权益降至 **−36.5632**；B 在 **2026-09-07 05:05:00.108** 降至 **−5.66**。因此大量 unmatched 来自一侧先停止交易的解释有数据支持；但两个执行模型在该配置下积累出足以改变存续期的差异，不能据此宣称行为一致性“不打折扣”。

四份 summary 均记录 `ops_ack=true`、`overshoot=loaded` 与 `ov[eca09e24]`。P19 在生产台账找到对应 **run#442–445、actor=ops**，策略/品种/周期/时刻对应四组运行；本次审核没有新增锁箱读取或运维豁免。

**三、T44 / T34：live-paper-2026-10-26.md。**

P18 核对最终账本、订单日志、运行器动作、风控和对账日志、归档指令及第二次 ack：

| 项目 | XAUUSD 902431 | EURUSD 902432 | 判断 |
|---|---:|---:|---|
| 决策 | 41 | 42 | 合计 83，成立 |
| 入场 / 出场成交 | 8 / 7 | 3 / 3 | 合计 21=11+10，成立 |
| SL / TP | 3 / 0 | 2 / 0 | SL 共5、TP零样本，如实 |
| add / reduce 动作 | 0 / 0 | 0 / 0 | 已关闭 position 的入/出量相等；黄金最后一仓仍持有 |
| 期末 equity | 9826.933 | 9897.14 | 总计 19724.073，相对初始20000为 −275.927，与报告舍入一致 |
| flatten 执行 | 2 | 2 | EURUSD 第一次本无仓，第二次有仓并平掉 |
| 第二次 ack.closed | 1 | 1 | 两份均引用新 uuid `bcc56ce9…`，成立 |

执行器订单日志为 **16 笔请求（11 open + 5 close）**，另有 PaperBroker 模拟保护性出场 **5 笔**，总成交21。报告把21称为“市价单数”时应注明按成交腿计数，不能将它当作21条主动市价请求；零调仓证据来自动作/逐仓量核对，不是等式本身。

两次全平 uuid 不同；第二次 ack 分别在 **13:15:08.935909 / 13:15:08.572334**，都绑定 **13:15:07.270619** 的新指令。归档中的两份命令与第二次两份 ack、账本关闭成交、日志 flatten→flatten_acked 形成正向证据。第一次 ack 未在现有 archive 中完整保留；“旧 ack 必定先被拒绝”的内部调用不能仅由最终文件反推出，编号匹配实现与测试补足该边界。两次都因旧 magic 登记而人工归档，**全自动 archive 完成不属于此次已证明的场景**；阶段 F 前应清理/退役旧登记并演练自动归档。

当日告警仅 RISK_REJECT=13（max_lots=11、kill_switch=2）、RISK_HALT=3、RISK_FLATTEN=2；没有 RECONCILE_MISMATCH、UNEXPECTED_RESIZE 或 PARTIAL_FILL。两策略最终 pause=false、pending_external_exits 为空；对账日志零不一致，风控零异常心跳，halt=是195轮，与约32.5分钟相符。EURUSD blocked_reentry 共8轮，支持两轮同向禁止重入。

限制呈现方面：TP=0、无经纪商滑点样本、无断线重连/负向不一致样本，报告已披露，应继续保留。报价点差本身可从影子行情日志比较，本区间的日报新增功能也已实现这种比较；不适用的是**经纪商实际成交滑点验证**，不能把点差样本一并称为不可测。夏令时窗口内零开仓是观测事实，因 max_lots 先拒绝，尚不是独立触发 forbidden_session 的演练证据。D′ 可接受已观测链路，F 仍需另取真实模拟账户证据及覆盖不足的场景。

### 蓝图是否需更新

**需要，而且应按“实现已修 / 本批证据 / 阶段放行”分别写准。** 本轮仅提出更新要求，不改蓝图或产品文件。

1. **P2 与 P5**：删掉缓存仍 v1、swap 未入身份的陈旧表述；明确 stop_level 缓存四品种可用，67 条六卡复跑已经存在。继续保留 swap 计费时刻/过夜实测的日历限制，不把缓存重建等同于其完成。
2. **P3 / E**：保留 I6 原反例已修与2,119条纯stop子集通过；撤回“配对/时间核对全零”“全部机制一致性通过”。列出21/65时间违规、77原因违规、1,329 unmatched，并把R8-I3拆解修复与报告重算设为未完成；MT5测试器证据仍待用户操作。
3. **P4 / C**：台账事务、审计哈希、历史锚原问题可以标已修；合并器条件完整性与完整运行身份改为部分。T42产物链可标核实，T39正式统计效力仍待补证；门槛不动，G5禁令保留。
4. **P6**：去掉“分支名逃逸口已关闭”的绝对表述，登记head_ref注入及可信执行入口缺口。ACL/分支保护仍为未完成前置；screen暂缓恰当，终审通过也不能自动替代U0。
5. **P7 / D′ / F**：四项原Critical修复可以保留；D′写“10:58–21:04观测窗口内双策略链路通过，TP/重连/自动归档等待补证”。撤回“demo_run/demo_check已准备好完成F”，修正模式、0.01手约束、单实例启动和最终验收，再决定F首日启动。
6. **禁止事项与开放清单**：不得将本报告解读为交易或G5授权。F不能按现有工具直接放行；screen继续等待本轮问题与原隔离前置闭环；G5继续等待台账/产物相关终审核实及D16决策。旧Critical划除需带本轮证据，新增Important必须进入开放清单。
7. **索引与计划**：统一页首、§2、§3、§5、P8和D19–D29状态；T43复选框只在修正报告并附结果后按真实情况勾选。三张v2卡已提交是先卡后测的准备证据，不是screen已经通过，也不是研究隔离已经启用。

### 建议处理顺序

1. **先补防线**：修R8-I1的CI注入与可信入口；修R8-I2的实验臂、身份必填和窗口/数据校验。各自加入本轮反例，保留现有正式分片原件。
2. **修对账，再重算报告**：限制越幅拆解适用范围，补非同因出场回归，从既有交易/行情证据重算T43；先判断是否需要重新读取锁箱数据，不为改报告无条件重跑整批。若确需原始锁箱行情，按已有运维规则使用并记录`--ops-ack`。
3. **修阶段F的交付边界与生命周期**：明确paper延长观察与MT5 demo分开；补模拟账户/0.01手配置、单实例保护和针对退出进程的重启；将缺样本/缺演练设为未验收。核对旧magic退役及自动归档后，再评估F启动。
4. **修T42解释和T44时间线**：移除G1.trade_count伪误报建议；更正p5方向、单变量归因和正式验收措辞；把D′实际SL/重入/反手时刻与原始日志对应。这里大部分可以使用现有产物完成，不需改冻结门槛。
5. **同步蓝图并定向复审**：对本轮反例及受影响测试完成回归，再执行必要全量；报告应同时保留本次1642 passed与新的反例结果。解除哪一项限制，必须对应其自身证据，不再用一个“R8全部完成”勾选替代分阶段放行。

探针复现命令（仅生成审查结果，临时副本不进入产品路径）：

```powershell
.\.venv\Scripts\python.exe docs/reviews/probes/2026-10-26-round8-probes.py --out docs/reviews/probes/2026-10-26-round8-probes.json
```

本轮交付仅为这份终审报告、只读探针脚本和脱敏结果JSON；没有修改产品代码、冻结门槛、交易配置或git历史。
