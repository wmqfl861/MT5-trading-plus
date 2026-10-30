# T102 总计划：第二次全局审核（2026-10-30）修复——分四批

来源：`docs/reviews/2026-10-30-global-code-audit-2.md`（codex gpt-6-astra/max，
594k tokens，14/14 行为探针复现；探针 `docs/reviews/probes/2026-10-30-audit2-probes.py`）。
背景：H 线开发已结束（用户 2026-10-30），审核修复由本会话统一执行。
铁律不变：不改引擎成交语义、**不改实盘链路（mt5live/mt5gw/live_runner——
H2-01/H2-02 与 H2-03 快照部分登记移交 H 维护批，本会话不做）**、不动
config/gates.toml（D34 冻结指纹 d0a8b2f3… 保持）、三套受保护探针不改。

## 审核结论要点（修复的靶子）

- 上轮 26 项：6 消化 / 20 部分 / 0 未消化——"部分"的关键残留即本轮 X/P2 条目；
- 两处任务前提纠正：**D34 已冻结**（R14+冻结提交 12a5b05d 已在 HEAD 祖先链，
  加载验证 dsr_v0/n_min=2/rounds_lineage）——T73B 不再等冻结、只差实现；
  **EURUSD 已进候选计数**（25–1333 日含两品种）——缺的是标定池并池；
- 共享约束未闭合（X-01~X-06）+ P 线入口缺陷（P2-01~P2-09）+ 2 建议（P2-10/11）；
- §4 阻塞方案：T73B 按 5 步实施；T91 建议正式归档"现有数据/标准下不可检验"；
  成簇方法下一批主候选=簇单位置换（先修证据生成、预写冻结设计、再独立标定）。

## 批次划分（按审核 §4.4 推荐落地顺序）

### 批 1（T103）：共享契约——预算/身份/边界/快照/旁路/G5 参照
- **X-01 计算前共享占位**：ledger 增 reserve_trial（阶段事件或独立表，
  started→completed/failed 不可变推进；到达主检验后失败/崩溃不回收 N；
  同身份幂等重试只占一次；screen→gate 不因已占自己的额度被堵）；
  **reservation 成为新轮次的 N 计数权威（到达主检验的 started 即计），
  record_run 的 round_trial_budget 成功检查降级为与 reservation 对账的
  一致性后盾（防双计）或移除；旧轮次保留 round_n_dedup 口径并显式
  版本标记**；迁移前备份并只读核对旧台账、列出可识别已启动假设；
  H/P screen/gate/prob test 主计算前占位。
- **X-02 G5 参照绑定**：资格解析返回获准 G3/V3 的 run_id 与完整 trial
  identity；参照重放只消费该 G3 的 segments/原上下文，hash/run_id 写入
  锁箱 reservation；与 X-05 身份版本一并设计。
- **X-03 边界契约统一**：H 的 lockbox 边界契约为唯一契约，merge 直接消费；
  统一 end 等号/越界/冲突/收盘水位/合法导出/纯 ext；裸库自动直读只留
  显式测试/迁移通道。
- **X-04 一致性读快照**：store 发布协议（bars+meta+generation/content hash，
  原子指针或可恢复发布）；lockbox/merge/研究读全部消费；测试注入两步
  发布间失败/中间行变化。
- **X-05 TrialIdentity**：版本化试验身份含真实导入闭包（prob 核心三个
  绝对依赖）、模型 hash（从 metrics_prob 移入 params_prob 即改去重身份，
  runs 表无结构变化）、方法版本、**身份版本字段（为批 2 P2-01 白名单
  扩身份预留，避免二次迁移）**；进准入/预算/去重/模型装载/G5 绑定；
  旧 ledger 显式迁移映射（新旧 strategy_sha 并存需版本标记）。
- **X-06 空台账旁路**：research 入口一律要求已开启轮次；空库≠免预注册；
  calibration/ops 分类不变。
- **H2-03 研究侧**：mode=2 基础币种未知/非账户币种在
  `src/mt5quant/costs/swap.py`（per_lot_per_night）fail-closed 拒绝
  （落点选 swap.py 而非 build_costs 装配层——探针 currency 才翻转；
  消费者无实盘链路引用，已核实）；快照字段部分移交 H。
- 测试修正（错误契约断言）：tests/test_p10_cross_line.py:110（H 越限
  预期）、test_prob_p1_cli.py:978（计算后作废预期）、
  test_research_bound.py:272/280（裸库直读预期）、
  test_research_cards.py:635（库外绝对依赖不进指纹预期）。

### 批 2（T104）：P 线入口——白名单/验证起点/资格/预筛/证据状态/标定证据
- **P2-01**：冻结卡/轮次构造获准（局面×动作）集合，fit/模型/信号三处
  同一集合；checker 核对 hypotheses⊆白名单、实例真实 ExitRules 与
  bracket 一致；白名单进模型身份（用批 1 预留的身份版本字段）。
- **P2-02**：决策起点契约——预热只算特征不开持仓状态机，split 后空
  状态处理新触发；summary 以 OOS 起点切取（预热零收益不进统计）。
- **P2-03**：共同 EligibilityContext（特征+局面回看、anchor→action、
  K+1 未来窗、缺口、era、政策）；计数/P1/标定/拟合同一规则；
  count_triggers 接 feature_warmup/hold_bars。
- **P2-04**：预筛消费 merge 实测 source_tags/逐行 era；缺来源拒绝
  成本判定（不退 1×）；与 P1 同一情景成本装配。
- **P2-05**：预筛成本边界全量有限值/正分母校验；NaN 判拒绝或
  waiting_data，不默认 pass。
- **P2-06（标定证据）**：成簇平移内部洞不再 nearest 映回（合法 bar
  严格命中或预写有限容差；两组断言不含任何原始触发时刻；重证 NY
  小时/星期/ATR 匹配）；修后 2026-10-29-p02-cluster-postfix.py 探针
  必须继续通过且不改。
- **P2-07（标定证据）**：池指纹改内容指纹（精确有序 pool 内容 hash
  入 run manifest）；元数据缺失不再退空串；cluster 相位与 pool 同一
  冻结快照。
- **P2-08**：样本量证据状态三分 calibrated_sufficient /
  lower_bound_only / uncalibrated——下界+1 不再授权 pass（现行数值
  保留为保守阻塞，不下调锚点）。
- **P2-09**：方法批准状态门——正式入口消费 method approval；
  未批准/负对照失败方法只作诊断（不产生推进结论）。
- **P2-11（建议）**：n_total/n_usable/n_skipped 等诊断随身份进
  metrics 与轮次报告。
- 测试修正：test_prob_precheck.py:459（下界+1 即过）等错误契约。

### 批 3（T105）：文档纠偏 + T91 终局 + T73B 实现
- 文档：P 线蓝图/计划登记 R14 冻结证据（提交/指纹/加载结果），
  T73B 改"前置已满足、实现待完成"；T91 按审核 §4.2 措辞正式归档
  "不可检验"（沿用候选报告 §7.2，不开空 R2，登记重启条件）。
- T73B 五步：逐窗 fit（purge_bars 真正生效——level_decision.py:195
  现只接收未使用）、每窗模型/输入/配置 hash 与拟合范围、OOS 拼接
  不重叠不含训练收益、接冻结 dsr_v0 主检验、Brier skill≤0 否决；
  max_net_profit 占位字段处理；fittable G3/V3 拒绝在完整验收后放开。

### 批 4（T106）：簇单位置换方法批（按审核 §4.3）
- 前置：批 1/2 的证据生成修复（P2-03/06/07、X-03/04/05）；
- 预写冻结设计（method_id、簇构造、合法变换、匹配容差、最少交换单位、
  seed/job manifest、验收标准含置信上界、方法探索与验证分离种子）；
- 实现整簇随机化（保簇内时距、整簇移动、成对交换、重聚合统计量）；
- 全相位重跑标定（neg/neg-holm/cluster/pos/brier+输入几何检查），
  审核会话按预写规则验收；不合格保留失败不换选项。
- P2-10（建议）独立小样本覆盖报告随本批或 T92 前提交。

## 移交清单（不做，登记）

- H2-01 决策幂等空窗（executor journal/重启核对/所有权锁）——H 维护批；
- H2-02 旧保护性出场解释迟到人工出场（deal 水位持久化）——H 维护批；
- H2-03 快照基础币种字段（mt5gw symbol/spec/snapshot）——H 维护批；
- Demo 阶段 F 运维记录补齐/killSwitch 演练——运维。

## 每批流程与探针约定

计划（本文件+批级细化）→ 子代理审计划 → 失败测试先行开发（并行子代理、
**按文件所有权分模块——bt.py/ledger.py 每批内单子代理串行持有**，批间
重叠靠顺序执行化解）→ 子代理审成果 → 全量回归+三探针 → 推送 → CI 绿。

**audit2 探针约定（不改该探针）**：`2026-10-30-audit2-probes.py` 是审核
证据，两状态 REPRODUCED/PROBE_ERROR。修复后对应条目应从 REPRODUCED 翻转
为 PROBE_ERROR(AssertionError)、**探针退出码预期由 0 变非 0**——这是
修复生效的信号；live 与 currency（H2-03 快照部分落地前）保持 REPRODUCED
属正常；正向行为另写回归测试；CI 不以该探针 exit 0 为门槛。逐批预期翻转：
- 批 1：budgets、torn_store、boundary_contract、empty_ledger_round、
  dependencies、currency（研究侧落点 swap.py 生效后）、g5_reference；
- 批 2：whitelist、boundary_state、gaps、precheck、cluster_shift、
  calibration_identity；
- 始终 REPRODUCED（移交/未做）：live。

## 验收

- 全部可做条目落地或明确移交；审核探针中对应条目行为翻转验证；
- 文档与代码事实一致（D34/T91/T73B 状态）；CI 绿。
