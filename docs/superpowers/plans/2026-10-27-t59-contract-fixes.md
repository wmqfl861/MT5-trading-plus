# T59 修复计划（R10 成果审核阻断项，2026-10-27）

> R10 成果审核（独立子代理）判定不通过：P1/P2 阻断（契约 C-2/C-3 生产端-消费端
> 断链，端到端实证），P3 次要，P4 观察。本计划按审核建议修**消费端**
> （demo_check.py），生产端（已按计划契约实现）不动；并补两条**跨组契约联测**
> （用真实生产端产物喂消费端——这正是两侧测试互相掩蔽的根因）。
> 基线：040ac43（全量 1726 绿）。

## 修复项（全部在 scripts/demo_check.py + tests/test_demo_check.py）

- **P1（阻断）C-3 断链**：`load_drill_rounds` 在 drill-archive 子目录同时匹配
  `*.json` 与 `flatten.ack-*`（生产端复制保留原名 `flatten.ack-{magic}` 无
  扩展名，commands.py:48/191-194 不动）；解析按内容字段分类指令/ack 的既有
  逻辑不变。
- **P2（阻断）C-2 断链**：消费端关联键改为 **bar 收盘时刻（解析为 datetime 归一）**——
  从完整 decision_id（`SYMBOL|TF|bar`）解析后缀、与 decision_bar 一同经
  `_parse_any_ts` 归一为 **naive UTC datetime** 再作键（runner 的 decision_id
  后缀是 naive isoformat 无时区、executor 的 decision_bar 带 +00:00——**字符串
  相等会以同样方式再断链**，必须按时刻归一比较）。滑点（:1355）与逐日归因
  （:408-412）两处 join 改用归一键（paper 旧夹具全 id comment 解析后缀天然
  兼容）。生产端 executor 不动。
- **P3（次要）**：manifest `schema_version` **缺失**即拒绝（与生产端 fail-closed
  对齐：存在且 ==1）。
- **P4（观察）**：`activity_days` 的**全部四个消费点**按运行窗过滤——连续覆盖
  （accept_day_coverage :1004）、逐日存活（:1046-1080）、点差样本覆盖
  （accept_spread_samples :1277-1281）、--accept 逐日严格复核 sweep（:1569-1593）。
  实现方式：**build_ctx 内统一过滤**（运行窗可得时把 activity_days 及相关
  日报/日志集合裁剪到窗内；窗不可用（--day/--summary 宽松档）保持原样）——
  避免只改两处后其余判据仍被窗外旧证据"撑长"覆盖。

## 跨组契约联测（两条，失败先行——用真实生产端产物）

1. `test_drill_archive_contract_real_producer`：真实调用 `commands.write_flatten`
   + `write_ack` + `archive_if_all_acked`（临时目录）产出两轮归档 →
   `load_drill_rounds` 必须读出两轮 command 与全 magic ack（复现 P1 断链：
   修复前 acks=[]）。
2. `test_deals_decision_bar_contract_real_format`：deals 夹具的 decision_bar 用
   **生产端真实格式**（裸 bar ISO，如 "2026-10-20T18:00:00+00:00"）+ decisions
   日志用真实 decision_id（`XAUUSD|M15|2026-10-20T18:00:00+00:00`）→
   `--accept` 滑点与逐日归因判据必须命中（复现 P2 断链：修复前无滑点样本/
   归因失败）。

## 验收标准

- 两条联测修复前红（复现断链）、修复后绿；P3/P4 各一条失败先行测试
  （无 schema_version 拒绝；窗外旧证据不计覆盖——覆盖四消费点任一处泄漏
  都算不通过）。
- **P3 连带**：既有 manifest 夹具缺 schema_version 的同步补字段（修正而非
  削弱，测试注释注明）；可选加一行 C-1 形状断言（夹具必须含生产端恒写
  全部字段 schema_version/mode/magics/runs/account_hint）。
- 全量 pytest 绿（1726+新增）；既有 39 例 demo_check 测试语义不削弱
  （decision_bar 夹具改真实格式属修正）。
- 只改上述两文件；不推送（主会话统一）。

## 修订记录

- v2（计划审核增量确认意见）：①P2 关联键明确为 `_parse_any_ts` 归一 naive
  UTC datetime（字符串后缀相等会在 naive/带时区差异上再断链）；②P4 从两个
  判据扩为 activity_days 四个消费点、在 build_ctx 统一过滤（宽松档保持原样）；
  ③P3 连带补夹具字段与可选 C-1 形状断言。审核结论：补上即可开工。

## 流程

精简四步：本计划 → 原计划审核代理确认（增量）→ 一个修复子代理（非原 B 组）
→ 原成果审核代理复核 P1-P4 闭环。
