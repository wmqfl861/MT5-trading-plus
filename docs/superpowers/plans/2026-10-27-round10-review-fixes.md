# R10 修复施工计划 v2（2026-10-27）：第九轮复审 6I+1M 闭环

> **流程**：计划 → 计划审核 → 开发 → 成果审核。
> v1 计划审核结论：批准但需修订 6 条（3 阻断）；v2 已全部落实（见文末修订记录）。
> 输入：docs/reviews/2026-10-27-review-round9-codex.md + probes/2026-10-27-round9-audit.*。
> 基线：fe26103（CI 绿）。

## 0. 跨组契约（先于一切任务，A/B/E 按此实现，不得各自发挥）

**C-1 run-manifest.json**（A 写，B 读）：`<DataDir>/live/run-manifest.json`
```json
{"schema_version": 1, "mode": "Demo|PaperSoak",
 "magics": [902601],
 "runs": [{"started_utc": "2026-11-02T00:05:00Z", "log_dir": "...", "kind": "start|restart"}],
 "account_hint": "demo"}
```
- 每次启动（含恢复重启）**追加**一条 runs（绝不覆盖整个文件；文件不存在则建）；
- **当前运行窗** = runs 中**当前 mode** 的最早 started_utc → now（重启不清窗）；
- PaperSoak 同样写（mode 区分；`--accept` 要求 mode=="Demo"）；
- ps1 启动前读失败/JSON 损坏 → fail-closed 拒绝启动。

**C-2 deals-\<magic\>.jsonl**（E 写，B 读）：执行器在①主动下单成交、②保护性/
外部出场解释（_sync_external_exits）时**追加**一行 JSON：
```json
{"ts": "...", "magic": 902601, "ticket": 123, "event": "fill_open|fill_close|external_exit",
 "reason": "SL|TP|SO|null", "price": 4012.35, "volume": 0.01, "side": "buy|sell",
 "decision_bar": "2026-11-02T00:00:00Z"}
```
- 与 orders-\<magic\>.jsonl 并存（后者仍是请求日志）；写入失败不阻断交易但
  记录告警（fail-open 限于此证据流，交易路径不变）。

**C-3 演练证据保全**（E 写 src/mt5live/commands.py，B 读）：自动归档删除
  flatten 指令/ack 前，把 command 与全部 ack **复制**到
  `<DataDir>/live/drill-archive/<command_id>/`（只增不删）。
- B 的双全平验收只从 drill-archive 或 commands 目录读取，且要求指令与 ack
  三点关联（见 T54）。

**C-4 重连标记**（E 写 scripts/live_runner.py）：网关调用失败后的下一次成功
  调用记一行日志 `gateway_reconnected prev_failure=<kind>`；幂等不设标记——
  B 由 deals/orders 与决策日志**推导**（同一 bar 同 magic 无重复开仓请求）。

## 1. 任务分组（文件域互斥）

### Agent A：scripts/demo_run.ps1、scripts/config_check.py（新）、tests/test_demo_run_ps1.py、docs/ops/demo-runbook.md

- **T51（R9-I1）**：`$demoFusePy` 改写临时 .py 文件（`$env:TEMP` 随机名，
  UTF8）后 `& $PythonExeAbs <file> @symbols` 调用；**try/finally 清理**（失败
  路径同样删除）；grep ` -c ` 其余原生传参一并改。
  测试（失败先行）：①复刻探针——here-string 经 PS5.1 `-File`→`ast.parse`
  老路径 SyntaxError（证明反例）；②新路径源码逐字符等原文；③
  **`-SkipGatewayCheck` 不带 `-PrecheckOnly` 单独使用必须拒绝启动**（防绕过
  DEMO 保险丝的后门）；④不连终端的 Demo 预检成功路径（-PrecheckOnly +
  -SkipGatewayCheck，断言到达"等待网关"边界且 Python 源码可解析）。
- **T52（R9-I2）**：弃用 `-m mt5live.config_check`（src 布局下 ModuleNotFoundError，
  已实测）。新建 `scripts/config_check.py`（自插 sys.path，与全仓惯例一致；
  调 LiveConfig 解析器，输出 JSON：{ok, volume_lots, has_risk_fraction, error}）。
  ps1 判定改为解析该 JSON：volume_lots==0.01 且无 risk_fraction 且解析 ok；
  Select-String 仅留提示。
  测试（失败先行，走**真实 ps1→python 链路**）：①审核反例（顶层 0.02+多行
  字符串诱饵行）拒绝；②volume_lots=0.01 通过；③TOML 语法错拒绝；④表作用域
  内 volume_lots 不影响顶层。
- **T55（R9-I5）**：①实例登记改 `<DataDir>/live/instances.json`（字段
  pid/kind/magic/mode/started_utc/log/account_hint），pids.txt 仅日志；②检测
  维度 magic+kind（跨 LogDir）；同 magic 不同 Mode → 拒绝并报冲突；③临界区
  （检测→Start-Process→登记）经 `<DataDir>/live/instances.lock` 排他：
  `[IO.File]::Open(..., OpenOrCreate, ReadWrite, None)` 保持句柄于
  `$script:` 变量防 GC 释放；**边界注明**：仅互斥 ps1 并发启动，与
  src/mt5live/fileio 的字节锁不互通（该文件只有 ps1 读写）；④存活判定 =
  PID 存活 + 进程创建时间与 started_utc 容差 5s + 命令行特征
  （live_runner/risk_process/live_reconcile）；⑤新增 **`-Stop` 开关**：只读
  instances.json 当前项、逐项核验存活后停止（不再遍历历史 PID）；ps1 头部
  用法注释与 runbook 同步；⑥恢复仅复用 Mode 匹配的存活实例。
  测试（失败先行）：换 LogDir 同 magic 漏检反例必须报冲突；跨模式拒绝；伪造
  PID/特征不匹配不视为存活；锁被独占时第二个调用立即失败。
- **run-manifest 写入（C-1）**：启动/重启追加 runs 条目（显式任务，勿漏）。
- **runbook（承接 B 组措辞修正，避免文件冲突）**：改"PaperSoak 必然无法通过
  --accept"为如实表述（PaperSoak 证据不被 --accept 采信）；补停止命令改用
  `-Stop`；补"每日日志≠整日连续"残余边界声明（--accept 的存活判据见 T53）。

### Agent B：scripts/demo_check.py、tests/test_demo_check.py

- **T53（R9-I3）**：①--accept 必须读到 C-1 manifest 且 mode=="Demo"、magics
  与证据一致，运行窗=当前 mode 最早 started_utc..now；存在 paper 状态文件/
  纯 paper 证据 = "证据来源可疑"拒绝；②成交/持仓/SL/TP 判据读 C-2
  deals-*.jsonl（external_exit 的 reason∈{SL,TP} 计样本）；③点差样本结构化
  解析（须含 bid/ask 与小时的可解析行），滑点判据=deals price vs 决策价差值
  分布（有样本+分位数入报告）；④§7 六项演练逐项判据（运行窗内）：
  killSwitch=alerts kill_switch 停机+恢复对、心跳超时=HEARTBEAT_TIMEOUT 告警、
  分策略暂停=RECONCILE 暂停事件+解除、重连=C-4 `gateway_reconnected` 行、
  幂等=推导判据（同 bar 同 magic 无重复开仓请求）、换日禁入=禁入窗拒单记录；
  每项缺失=未验收；⑤存活判据加强：风控心跳日志覆盖当日每小时（而非一条
  日志）；⑥六项之外的"整日连续"残余边界由 runbook 声明（A 组）。
  测试（失败先行）：审核三反例（纯 paper 证据/标题日报/仅 paper 保护性出场
  日志）--accept 必须失败；正例=C-1/C-2/C-3 契约格式构造完整 Demo 证据通过。
- **T54（R9-I4）**：验收读 C-3 drill-archive（或 commands 目录）+时间窗：
  两轮指令均在运行窗内；每轮指令须有**全部参与 magic**（manifest 集合）的
  ack（command_id 匹配+magic+时间在指令后合理窗内+closed>0 或附空仓证明）；
  孤儿 ack 不计；一轮缺任一 magic 的 ack=该轮未完成；D′ 旧指令（窗外）不计。
  测试（失败先行）：孤儿 ack（closed=0 无 magic 无指令）失败；单 magic 缺
  ack 失败；窗外指令不计；正例（两轮×全 magic+closed>0+时间序）通过。

### Agent C：scripts/merge_gate_power.py、tests/test_gate_calibration_script.py

- **T56（R9-I6）**：指纹校验：必须非空 dict 且必需字段类型/格式合法——
  `git_commit`：**空串合法**（生成器文档化的"git 不可用"语义，产物如实降级
  展示），非空则必须 40-hex；`git_dirty` bool；`gates_sha256`/`script_sha256`
  64-hex；`cost_versions` **非空** dict[str,str]。null/{}/缺字段/类型错 →
  拒绝且**不属** --allow-legacy 豁免（legacy 仅限键整体不存在）。分片间相等
  比较维持。
  测试（失败先行）：null、{}、缺字段、类型错四反例拒绝且 legacy 不豁免；
  键缺失+--allow-legacy 正常降级；**连带更新既有 `{}` cost_versions 夹具**
  （test_merge_shards_recomputes_rates_and_flags）并注明原因。

### Agent D：纯文档（T57/R9-M1）

- 98.13% → 98.11% 四处：docs/reports/reconciliation-v3-2026-10-25.md 第
  11/173/401/422 行；docs/blueprint.md P3 行；docs/superpowers/plans/
  2026-10-26-round8-finalization.md T43 节；docs/reviews/probes/
  2026-10-27-r9-probe-rerun.json:41。**不动** recalc 脚本（经审核证实无
  硬编码，摘要现算本为 98.11）；不动 158→213 与 97.45%。

### Agent E：src/mt5live/executor.py、src/mt5live/commands.py、scripts/live_runner.py 及各自测试

- **T58（新，解阻断）**：①executor 写 C-2 deals-\<magic\>.jsonl（主动成交
  与 external_exit 解释两处）；②commands.py 自动归档前复制 C-3 drill-archive；
  ③live_runner 补 C-4 gateway_reconnected 日志行（失败后首次成功时）。
  测试（失败先行）：外部出场解释后 deals 文件含 reason/price 行；归档后
  drill-archive 保指令+ack 副本；网关失败恢复后重连行出现（用 Fake 网关
  构造失败-成功序列）。交易路径行为不变（回归维持）。

## 2. 主会话收尾

1. 独立子代理审成果（对照本计划逐项验收 + 复跑 2026-10-27-round9-audit.py
   反例确认反转 + 全量 pytest）。
2. 蓝图 P8/开放清单按成果审核结论更新。
3. 统一提交推送，CI 绿。
4. 阶段 F 启动 / v2 卡 screen：维持暂缓，待本轮成果审核通过后再评估；G5
   维持禁跑（等 D16）。

## 3. 禁止事项（不变）

不跑 G5；不改 gates.toml；不碰仓库可见性；只在模拟盘下单（校准仓为用户已
确认存续仓位）；报告入库前脱敏；读锁箱段仅限 --ops-ack 并入账。

## 修订记录

- v2（2026-10-27，按计划审核 9 条意见）：①新增 T58 生产端任务（deals 持久
  化/drill-archive 保全/重连标记，Agent E）——解阻断 1；②C-1 契约钉死
  （追加式 runs、当前 mode 最早 started_utc 为窗、PaperSoak 同写、版本字段）
  且"写 manifest"列为 A 显式任务，runbook 全部改动移入 A——解阻断 2/冲突；
  ③T54 收口为"归档前复制"（commands.py 归 E），删 ledger 选项——解阻断 3；
  ④T52 改 scripts/config_check.py 入口（-m 实测不可行），测试走真实链路；
  ⑤T56 钉死 git_commit 空串合法、连带改 {} 夹具；⑥T57 改四处+probe json、
  删 recalc 项；⑦T51 补 try/finally 与 -SkipGatewayCheck 防滥用负例；
  ⑧T53 六项演练证据标记逐项定义（C-4）、存活判据加强为逐小时心跳；⑨T55
  锁边界注明、-Stop 载体明确、登记项补 account_hint。
