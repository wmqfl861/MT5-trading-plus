# R9 修复施工计划（2026-10-27）：第八轮终审 7I+2M 闭环

> **For agentic workers:** 多子代理并行执行。每个任务：先写失败测试（凡有代码改动）→
> 实现 → 相关回归 + 全量 pytest → 提交（不推送，主会话统一推送）。
> 输入：docs/reviews/2026-10-26-review-round8-codex.md（编号 R8-I*/M*）。

**基线：** 82be091（1642 tests，CI 绿）。

## T46（R8-I1）：CI head_ref 注入修复

- workflow 改为经步骤 `env:` 传 `head_ref`，shell 只引用环境变量（绝不内插
  `${{ }}` 进 shell 字符串）；保留全头分支强制与 baseline 装载。
- 失败测试先行：模拟注入分支名（`audit";exit${IFS}0;#` 类）经新引号路径
  不得提前成功——本地复现 workflow 脚本的变量展开语义（P15 探针思路），
  断言注入串只被当作数据。
- 受信入口缺口（workflow 自身随 PR 版本）继续留在 U0，不越权处理。

## T47（R8-I2 + I7 渲染部分）：合并器整臂缺样与身份必填 + 结构性 flags 标注

- `_expected_conditions`/`_seed_problems` 改为从实验设计推导**应有实验臂**
  （negative/positive/g5 由 meta.phase 与设计决定，不随"产物有无区块"缺省）；
  删除任一分片整个 g5/negative/positive 区块 → 拒绝合并（P14 反例入测试）。
- META_IDENTITY_KEYS 增补 `max_bars`、`g5_cutoff`、`g5_window_source`；
  `fingerprint` 缺失（全部分片都没有）→ 拒绝合并（历史无指纹诊断产物合并
  须显式 `--allow-legacy`，产物 meta 记 legacy 降级标记）。
- 结构性 flags 标注（R8-I7 渲染部分）：合并侧渲染把 `G1.trade_count` 等
  结构性检查的 flag 行标注"结构性检查——不构成门槛建议依据"（JSON 原始
  flags 不动；gate_calibration.py 字节不动——分片指纹效力保持）。
- 失败测试先行（P14 反例、身份键缺失、legacy 路径、标注渲染）。

## T48（R8-I3）：对账越幅拆解限定 stop↔stop + T43 重算

- reconcile_ab：越幅分量仅在 A 与 B **同因 stop** 且可比条件下使用；
  A-stop/B-nonstop 行保留为"原因分叉"类，不走越幅抵消（P17 纯函数反例
  入回归：+60 差异不得被计为 overshoot_comp）。
- 用既有 diff.csv/交易证据重算 T43 净利符合率与"其余"违规清单（61 条
  A-stop/B-nonstop 中预计 ≥55 条浮出超容差）；如确需重跑对账读取锁箱段，
  按运维规则 `--ops-ack`（台账 actor=ops）并如实记录。
- 回填 reconciliation-v3 报告：撤回"机制一致性通过"总述，改为分口径
  （纯 stop↔stop 2,119 行零违规子集维持；全量净利符合率按新拆解重报；
  时间违规 21/65、原因违规 77、unmatched 1,329 如实列出；36 before_ready/
  50 level_unknown 单列）。

## T49（R8-I4/I5/I6）：阶段 F 工具交付边界与生命周期

- demo_run.ps1 明确两种模式：`--paper-soak`（现影子盘延长观察，不改语义）
  与 `--demo`（MT5 模拟盘 0.01 手：固定手数 0.01、DEMO 保险丝预检、真实
  成交路径）；缺省拒绝裸跑并打印模式说明。
- demo_check.py 拆分两档：`--day`（日常宽松显示）与 `--accept`（阶段验收
  严格：连续交易日覆盖、各进程存活证据、SL/TP 样本存在、双全平演练真实
  ack（两个 uuid 指令 + 两轮 closed）、点差/滑点实际样本、缺样本=未验收
  非通过）；退出码区分（P16 反例入测试：空意图+无点差日报不得通过验收）。
- 单实例生命周期：demo_run.ps1 启动前检测同 magic 运行器/PID 存活（跳过
  或报错，绝不并行第二份）；pids.txt 追加不覆盖；runbook 恢复步骤改为
  "只移除停机信号 + 仅重启已退出进程"，不再整组重跑。
- demo-configs 补 0.01 手模拟盘配置（volume 固定，不用 risk_fraction）。

## T50（R8-I7/I3 措辞 + M1 + M2）：报告与蓝图同步

- gate-power-v2 报告更正：①"trade_count 不参与标记"改为如实说明
  G1.trade_count 仍被 compute_flags 计入（结构性口径不一致已登记）+
  T47 的标注落地后重引；②XAUUSD/USTEC 参照 p5 归因方向更正（USTEC 更低：
  −2.99～−1.69）；③v1→v2 差异归因降为"与越幅切换一致的单变量未控变更"；
  ④"正式统计验收就此补齐"降级为"T42 指定重跑与诊断展示完成；正式判别力
  标定（独立强度校准/可比 G3 参照/不确定性）仍开放"。
- T44 报告时间线更正（R8-M1）：黄金第三次 SL 14:54:29 → 15:00:06 开空 →
  16:30:06 反手；最后平仓/转空 20:15；对账轮 1,208 / 风控轮 3,622；
  "10h06m 观测窗口"措辞；21 笔=成交腿计数注明。
- 蓝图同步（R8-M2）：P2 删"缓存 v1/swap 未入身份"陈旧表述；P3 撤
  "配对/时间核对全零"（等 T48 重算后按新数字写）；P5 补 run#364–430
  67 条复跑已存在；P8/§3 包状态/D19–D29 与现状对齐；R8 计划 T43 四项
  复选框按 T48 实际结果补勾；开放问题清单加入 R8-I1..I7 残留项状态。
- 蓝图修订须等 T46–T49 结果（部分行引用其落地状态），由主会话最后统一
  提交（T50 其余文档先改）。

## 收尾（主会话）

- 复跑 P14–P19 探针（docs/reviews/probes/2026-10-26-round8-probes.py）
  确认反例全部反转；全量 pytest；统一推送 + CI 绿。
- 视修复规模决定是否请 codex 对 R9 修复做定向复审（gpt-6-astra max，
  范围 82be091..HEAD，聚焦 R8-I1..I7 反转）。
- 阶段 F 启动 / v2 卡 screen：仍等复审通过 + 蓝图禁止事项按证据逐项解除。

## 禁止事项（不变）

- 不跑 G5；不改 gates.toml；不碰仓库可见性；只在模拟盘下单（校准仓除外
  须用户已确认）；报告入库前去账号/IP/令牌；读锁箱段仅限 --ops-ack 并入账。
