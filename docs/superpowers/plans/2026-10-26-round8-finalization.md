# R8 收尾施工计划（2026-10-26）：正式验收三连 + 演练准备

> **For agentic workers:** 多子代理并行执行。每个任务：先写失败测试（凡有代码改动）→ 实现 →
> 全量回归 → 提交（<type>(T##): 摘要）→ 推送 → CI 绿 → 勾选复选框并附证据。
> 完成后由 codex（gpt-6-astra，思考等级 max）复审一轮，蓝图按结果更新。

**Goal:** 按蓝图 v3.1 路线图完成三个放行级验收（C 正式标定、E 四组对账、D′ 影子盘），
并准备阶段 F（两周模拟盘）。完成后项目进入"等日历时间"状态（模拟盘跑满两周 + 用户操作）。

**输入文档：** docs/blueprint.md（v3.1 + R7-5 更新）、docs/reviews/2026-10-26-review-round7-codex.md。

**基线：** d95d572（1632 tests，CI 绿）。

## T42：T39 正式重跑（修复后实现 + 运行指纹）

上一轮五分片系修复前启动、无运行指纹（R7-N-I1）。本轮重跑：

- [ ] 每分片产物 meta 带运行指纹：gate_calibration.py 已记录 seeds_from/sampling/成本版本
      （含缓存 sha+越幅 sha+swap sha）；合并前 merge_gate_power 校验五分片指纹一致
      （当前 _meta_identity_problems 已有；确认包含 git commit——若无则补一个
      `git_commit` 进 meta 并校验）。失败测试：分片指纹不一致 → 合并拒绝。
- [ ] 重建研究库缓存（R7-3 已重建主库 stop_level 口径；确认 data_research 同步）。
- [ ] 跑 5 分片（seeds 0-3/4-7/8-11/12-15/16-19）并行；完成后 merge 出正式 gate-power。
- [ ] 正式报告：docs/research/gate-power-v2-<日期>.md——含 run 指纹、完整条件清单、
      校准收敛与分布、passed_all 事实、G5 参照限制；如决策规则触发则列门槛建议（只建议）。
- [ ] 蓝图 §2 P4/P5 与路线图 C 行更新。

**注意：** 分片是后台计算（无 LLM 消耗），用 Bash 后台跑；种子与分片参数不变时
成本版本会因缓存重建而变化（ov[eca09e24]），新运行自动与新缓存配对。

## T43：T40 四组对账（越幅缓存 v2 + I6 修复后）

reconciliation-v3 报告的 §5–§7 还是占位。本轮重跑并回填：

- [ ] 四组对账：XAUUSD H1 + USTEC M5 各 immediate / after_signal_change，
      MaCrossATR（10/30 或 5/15 参数），窗口取 tick 覆盖期（--ops-ack）。
- [ ] 确认成本缓存为 stop_level 口径（ov[eca09e24]）， reconcile 的越幅分量用新表。
- [ ] 回填 v3 报告：四组逐项结果表、按出场类型的分布、容差符合率、"其余"违规清单与归因。
- [ ] 若结构差异超容差：分析原因（预期：换日窗点差建模差 vs 静默 tick），如实报告。
- [ ] 蓝图 §2 P3 与路线图 E 行更新。

## T44：T34 影子盘一日双策略（真终端，阶段 D′ 验收）

R7 修复（账户级风控聚合视图、一致快照、确认绑定事件、净零残仓、内核字节锁）已落地。

- [ ] 准备两份 paper 配置（M15，确保止损/止盈出场自然发生）：
      XAUUSD immediate magic=902431 / EURUSD after_signal_change magic=902432。
- [ ] 启动：2 个 --paper 运行器 + 1 个风控 --paper-state + 1 个对账 --paper-state --loop。
- [ ] 白天演练：killSwitch 置放→确认→移除；用测试风控配置（日亏上限调低）触发**连续两次**全平。
- [ ] 收盘后收集：决策数、订单数（= 入场 + 出场）、保护性出场数、暂停数（无法解释的应为 0）、
      调仓数（持仓期间应为 0）、心跳与告警摘要。写 docs/reports/live-paper-2026-10-26.md。
- [ ] 蓝图 §2 P7 / 路线图 D′ 行更新：通过则影子盘记"真机"证据，阶段 F 放行。

## T45：阶段 F 准备 + 蓝图 + codex 复审

- [ ] 模拟盘准备（不开跑）：0.01 手两周演练的运行器/风控/对账配置与启动脚本
      （scripts/demo_run.ps1），验收清单从蓝图 §7 逐条转化为日志检查脚本（scripts/demo_check.py）。
- [ ] 蓝图 §2 / §5 全表更新（T42/T43/T44 证据入表）。
- [ ] codex 复审（gpt-6-astra max）：覆盖 8e8bb76..HEAD 的 R8 改动 + 三个验收报告。
- [ ] 按复审结果修复或标注，最终推送。

## 禁止事项（不变）

- 不跑 G5（锁箱预算等用户 D16 决策）；不开新研究卡（基线重跑除外）；
- 不改 gates.toml；不碰仓库可见性；只在模拟盘下单；报告入库前去账号/IP/令牌。
