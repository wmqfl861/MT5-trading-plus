# D57 正式窗口机械预检实施裁定（2026-11-05）

本记录由主会话转录独立规格审核者直接返回的结论。审核者未参与调查方案或实现，固定基点为 `c0df5a1d133eadbe8a8c089fba0758ed840d830b`。

**approved，仅批准收窄后的离线实施。** 范围为 checker、合成测试与原 runbook 用法更新：绑定正式窗口证据并关闭自动正式验收通过出口。调用者的描述文件不是真实部署证明；真实读取、正式起算、A5、维护和部署均未由本裁定放行。

## 原因与出口

launcher 的 run 不含 SHA，instances 整体重写且启动时刻可能使用登记时刻回退，supervisor journal 缺完整路径身份。公开部署日志的短目录名、`13:2x` 等叙述也不能补成完整 SHA、准确完成时间及同期身份。依据为 `scripts/demo_run.ps1:220/593/689/710`、`scripts/live_supervisor.py:377`、`docs/ops/phase-f-log.md:201`。

正式入口固定为 `--accept --formal-window <描述文件>`，同时显式指定离线证据路径。裸 `--accept`、不合法描述必须在读取默认 data/config 之前拒绝。无 `--start/--end/--now` 或批准开关。旧 `--day/--summary` 保持语义。

| 退出码 | 正式分支含义 |
| --- | --- |
| 2 | 参数、原始证据、身份绑定或日历无效／不完整，无法判定 |
| 1 | 材料可解析，但天数、样本不足或存在异常 |
| 3 | 全部机械检查满足，历史真实性和A5仍未审定 |
| 0 | 正式分支禁止返回 |

输出固定 `historical_identity_verified=false`、`a5_status=not_evaluated`。完整合成正例可输出 `mechanical_checks_passed=true`，不再输出“阶段 F 验收通过”。

## 固定证据索引

```text
Ref = {path, sha256}
RunBinding = {
  run_index,
  manifest_snapshot: Ref,
  launcher_snapshot: Ref,
  instances_snapshot: Ref
}
FormalWindowV1 = {
  schema_version: 1,
  mode: "Demo",
  deployment_sha,
  magics,
  anchor: RunBinding + {
    deployment_claim: {
      source: Ref,
      source_lines: [first, last],
      excerpt_sha256,
      completed_utc,
      processes: [{kind, magic, pid}]
    }
  },
  run_bindings: [RunBinding],
  restart_bindings: [{line_number, before_instances: Ref, after_instances: Ref}],
  calendar: Ref
}
```

- Git SHA为完整小写40hex，文件与选段为SHA256。run_index零基，文本行号一基闭区间，restart行号为journal物理行号。整数拒bool；magics为非空、无重复正整数。
- launcher_snapshot引用既有逐次启动清单，instances引用同期留存副本；不得新造自称历史的身份文件来补齐事实。
- deployment_claim是人工转录声明。完整SHA、明确UTC时间、PID必须出现在被引用原文并与机器记录对应；短SHA、近似时间、缺原件拒绝。字面匹配只证明机械一致，不证明原件同期生成或所填时间确为部署完成；A5另行核实。
- 描述、日历、索引严格拒重复键、未知键和错误类型。原生产文件按其实际合法字段检查。每个输入读取一次，同批bytes计算哈希并解析，不修改原材料。

## 身份链

锚绑定首次完整部署的全部runner magic、risk和reconcile角色PID，部署声明、逐次启动清单与同期instances须相符。anchor.run_index为锚manifest快照末项。当前manifest保留锚快照整个runs数组前缀，后缀每项都有binding；比较数组内容，不要求JSON字节前缀。

核对SHA、Demo模式、部署目录、数据目录、角色/magic、关键argv。路径只作离线规范化比较，不访问其指向的部署或配置。缺身份的历史run不能继承描述文件全局SHA。

从锚至观测时刻的每条supervisor重启均须有前后instances关联，核对角色、旧/新PID与SHA。journal时间取得于launch之前，不要求与进程启动时间相等。坏行、不可读、缺留存身份拒绝；不能用当前instances回填过去。

异SHA、mode或路径出现后即使切回也拒绝。正常同SHA重启不清窗，kind=start也不能替代部署锚。后移锚与原部署PID、初始材料或更早留存身份冲突则拒绝；所有原件一起被替换的情况无法凭机械比较识别，仍不得产生正式通过。

## 日历与时间窗口

```text
{
  schema_version: 1,
  timezone: "UTC",
  market: "XAUUSD",
  source: Ref,
  days: [{date, trading, reason: "weekday"|"weekend"|"holiday",
          source_lines: [first, last]}]
}
```

日历逐自然日覆盖声明部署完成日期至观测日期，含今天、周末和休市日，不得缺日或重复。工作日休市必须有具体来源依据，不能从无活动推断假日。

C为声明部署完成时刻，T为程序一次取得的UTC当前时刻。S取最早交易日的日界，要求该日00:00≥C。日中部署不计当天剩余部分；恰00:00完成可计该日。该保守规则不授予正式起算批准。

检查S起至T全部已经结束的交易日，至少十日，不能挑选十个有活动的日期。今天未结束不计入十日、不要求尚未到期的整日日报，但已发生异常和已到期存活证据仍检查，十日后的异常不能截掉。连续性覆盖[S,T]全部自然日，含周末与休市日；要求集合来自日历和时钟，不能取活动首尾。

## 事件视图与当前保护

正式分支对deals/orders/alerts/日志/SL、TP/重连/恢复/pause解除/command、ack先校验自身事件时刻，再过滤到[S,T]，再派生日/小时/magic集合。mtime、下一条日志时间、decision bar均不能替代事件时间。关键坏行和不可归属异常不得静默忽略，窗外记录不能提供正面证据。

- pause解除必须对应同一magic；killSwitch恢复和pause解除均须有T上界。
- flatten满足 `S≤command_ts≤ack_ts≤min(T,command_ts+1小时)`，匹配command_id与全部magic，closed为非负int非bool。
- 开仓归因和滑点参考行也须按窗口和对应magic关联，不能仅过滤成交侧。
- 当前pause、pending出场、意图/持仓差分、killSwitch、halt、halt-risk-veto独立检查，不按创建时间过滤，保留合法在途宽限；坏状态或不可读拒绝，不依赖今天是否为activity latest_day。

## 最低实际验收

真实CLI子进程消费临时合成证据，所有输入字节保持不变：

1. 裸accept或坏descriptor在默认data/config读取前退出2；day/summary兼容。
2. 旧版十日加新版九日、旧演练齐全仍失败。
3. descriptor有SHA但无历史原件、仅短SHA或近似完成时间，退出2。
4. launcher或supervisor异SHA后切回仍拒绝；同SHA正常重启不清窗。
5. 后移锚避开故障与初始材料冲突时拒绝。
6. 中间已有十活动日而所需首/末日缺证据，报缺日；日历缺日或假日无依据拒绝。
7. 未来恢复/ack、其他magic解除、窗外SL/TP不计成功；窗前建立且未解除的保护仍阻断。
8. 坏journal、manifest旧前缀被删改退出2。
9. 全部合成条件满足，机械成功且退出3，固定历史未核实/A5未评估，绝无正式通过文案。

审查只读核对8次定点源码检索/读取，未运行套件或探针、未访问真实材料、未改文件/HEAD/index、未派子代理。上述条件纳入[实施计划](../superpowers/plans/2026-11-05-demo-formal-window.md)，实现后另经独立代码审核。A5仍须核实真实原件、完整历史和全部演练。
