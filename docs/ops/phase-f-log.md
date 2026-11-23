# 阶段 F 演练日志（逐日）

> **磨合期与正式窗口（D57）**：10-28、10-29、10-30 是磨合期第 1–3 个交易日；10-31、11-01 为周末。11-02 起至 T111b 部署前继续作为磨合期。PR #5 与 T110 在维护窗口部署到固定 SHA 的独立目录后，从第一个交易日起重新计算连续 10 个交易日；正式窗口起算日尚未确定。旧记录保留事件事实，不能折算为正式窗口已完成天数。
>
> **D63（第十六轮，2026-11-07 生效）**：正式窗口未起算；交易日计数以主会话的部署记录为准（runbook §7 执行方分工），其他会话的日历口径计数不折算为正式窗口天数。
>
> 蓝图 §7 允许“重启有日志说明”——本文件即该说明。公开内容仅含脱敏证据；原始账户与终端日志保留在本机。

## 2026-10-28（周三）· 磨合期交易日 1

| 时刻（UTC） | 事件 |
| --- | --- |
| 06:37 | Demo 三进程启动（runner 902601 / risk / reconcile），run-manifest 落盘 |
| 06:37:32 | 首条决策：XAUUSD M15 signal=-1 空头 0.01 手（SL/TP 随单） |
| 06:37~06:38 | **首单事故**：MT5 拒绝订单 comment 参数（`-2 Invalid "comment"`），mt5gw fail-closed 抛 OrderResultUnknownError → runner 退出 |
| 06:39:40 | 风控 128 秒检出心跳超时 → RISK_HALT 全局停机（**安全链路正确工作**） |
| ~07:00 | 持仓核对：magic 902601 **零持仓零挂单**（订单在校验层被拒未成交）；`-Stop` 清登记表；移除 halt |
| 07:05~08:00 | 停修：comment 白名单+31 截断+压缩 20 字符（a193f37）→ 独立复审通过 |
| 08:08 | 重启 Demo 三进程 |
| 08:08:44 | **首单真实成交**：buy 0.01 XAUUSD @4048.069（ticket 2758…），comment `XAUUSD_M15_1028_0800` 被 MT5 接受，deals 证据流记录 fill_open |
| 12:18 | 首次 SL 保护性出场被识别（external_exit），暂停自动解除——**零暂停** |
| 13:15 | 调度器例行 7/7 通过（日报补生成） |

**当日成交**：7 笔（开 4+出 3）；**保护性出场** 1 次 SL；**调仓** 0；**无法解释暂停** 0。

**当日演练**：无（演练表从 10-29 起）。

**事故复盘**：comment 被拒根因是 MT5 Python 库 30 字符以上注释返回 `-2`（第十四轮 L5 引公开实测报告确认），事故注释恰 30 字符。压缩到 20 后规避。上限待改 25+order_check 实测（第十四轮安排：下次计划内停机时执行，T89 周末项）。

## 2026-10-29（周四）· 磨合期交易日 2

原预定 killSwitch 演练与校准任务；当日实际事故、恢复与延期见下表。

| 时刻（UTC） | 事件 |
| --- | --- |
| 12:30:05 | Runner 决策 signal=-1 欲平多仓，**订单被 MT5 拒绝 [10004] requote** → runner 崩溃退出 |
| 12:32:08 | 风控 121 秒检出心跳超时 → RISK_HALT 全局停机（**安全链路第二次正确工作**） |
| ~13:20 | 调度器例行发现：runner 死/持仓差异(期望平仓但实际仍 +0.01)/心跳超时无恢复记录 |
| 13:20 | 清 halt → demo_run 重启（只补 runner 缺口进程，risk/reconcile 未动） |
| 13:22 | Runner 恢复正常（心跳 1 秒），持仓差异由新 runner 的对账进程自行消化 |

**当日成交**：10 笔（开 5+出 5）；SL 1 次；零调仓。日报已补生成。

**事故复盘**：requote(10004) 是 MT5 经纪商重新报价错误——mt5gw 正确抛 TradeRejectedError，runner 未捕获该异常而退出。修复属 H-04（实盘部分成交与清仓异常路径），已列周末休市计划。

**当日演练**：killSwitch 演练因 runner 恢复而推迟至下一 M15 bar 稳定后执行（本日志后续补记）。

## 2026-10-30（周五）· 磨合期交易日 3

| 时刻（UTC） | 事件 |
| --- | --- |
| 12:30 | 外部出场持续未决（ticket 2767796955）→ 每 15 分钟 EXTERNAL_EXIT 告警 |
| 13:17 | **人工 ack**（live_ack_external --reason client）：该出场为 10-29 requote 事故中 runner 自身决策的平仓腿（reason=3 EXPERT），runner 崩溃前未写 ack——H-05 保守恢复正确暂停开新仓直到确认 |
| 13:20 | ack 后 runner 恢复正常（7/7 通过） |
| ~13:25 | **心跳超时演练**：暂停 runner 6 分钟→恢复 |
| 13:26 | **演练未完成**：风控进程日志停在 13:13:20（13+分钟无输出/心跳超时未检测）——发现新问题 |
| 13:27:26–13:27:36 | -Stop 后新组三角色已有记录：runner 13:27:26.848、risk 13:27:34、reconcile 13:27:36（PID 18008/9248/16804）；原文约 13:33 的恢复时刻按日志纠正。 |
| 13:36 | 三进程恢复确认 |

**当日发现**：风控日志停更，心跳演练未完成。原文提出 stdout 缓冲或网关阻塞两种可能；取证不足以确认其中任一种，根因保留“无法确定”。

**T110 取证补记（2026-11-02，只读）**：窗口 12:35–14:05 UTC。旧 `risk-account-20261030-024650.log:6633` 最后正常轮为 13:13:20（心跳 ok、无 halt/flatten），旧 runner 对应日志第 32964 行在 13:15:06.316 仍有决策；旧 reconcile 最后记录在窗口前 12:31:52（第 2130 行）。旧组三份 `.log.err` 全部 0 字节。新组 `*-20261030-212725.log` 的首记录分别在 runner 第 2 行、risk/reconcile 第 1 行，证实恢复早于原文约 13:33。

匹配终端 `logs/20261030.log:60–80` 在窗口内仅见交易记录；无已读取的断线/崩溃/更新证据。Windows System `Kernel-General/ID 1` 在 12:57:59 记录校时约 +2.194 秒（RecordID 1579），随后变化仅毫秒级，不足解释此后停更。该窗口缺少栈和可靠退出标记，无法区分调用阻塞、输出缓冲或终止；没有 PermissionError 证据。`alerts.log:112` 在 13:17:54.732 的 `ACK_EXTERNAL` 与第 109–111 行历史告警匹配，属于上表已说明事件，不计新增未解释差异。

**swap 观测**：四窗全零（10-26..10-30 观测期结束，swap-free 确认）。

**明日（10-31 周六）**：换日窗 DST 实测报告。

## 2026-10-31（周六）· 磨合期周末（不计交易日）

| 时刻（UTC） | 事件 |
| --- | --- |
| 13:16 | 三进程存活确认；demo_check 7/7 通过（日报补生成后）；心跳正常 |
| 13:18 | **换日窗 DST 实测报告完成并推送**（2b94fca）——Exness 跟随美国；sessions.toml 无需修改 |

**当日成交**：0 笔（XAUUSD 周末休市）；持仓 0。

**换日窗实测**：见 docs/research/rollover-dst-2026-10-31.md。关键发现：
- XAUUSD/USTEC 在 H21 零 tick（换日关闭 60-63 分钟）
- EURUSD H21 点差 p50 为正常值 3.5-11.3 倍
- **Exness 换日跟随美国 DST**（NY 17:00 = 本周 21:00 UTC）
- sessions.toml 的 zoneinfo 定义已正确处理——11-02 美国切冬令时后自动切到 22:00 UTC

**演练**：原预定对账不一致分策略暂停；因 XAUUSD 周末休市、持仓为零而推迟。D57 下须在 T111b 部署后的正式窗口按顺序重新完成，旧延期计划不计验收证据。

**swap 观测**：已结束（10-26..10-30 四窗全零——swap-free 确认）。

## 2026-11-01（周日）· 磨合期周末（不计交易日）

| 时刻（UTC） | 事件 |
| --- | --- |
| ~05:03 | Demo 三进程（runner 18008/risk 9248/reconcile 16804）异常退出；11-02 取证在三份 stderr 中确认 `NotConnectedError(-6, authorization failed)`，详见补记。 |
| 13:17 | 调度器例行发现心跳超时 29594s → -Stop 清登记表 → 重启 Demo 三进程（新 runner 13:25 心跳 2s） |
| 13:26 | demo_check 5/7 通过（心跳重启无退出说明——taskkill 强杀不留日志；日报缺失→已补生成） |

**当日成交**：0 笔；持仓 0（runner 日志末尾 net -0.01 表示重启前有空仓——journal 恢复已解释）。

**演练**：断线重连（预定今日）——因进程挂起+重启推迟；待 runner 稳定后补做。

**T110 取证补记（2026-11-02，只读）**：窗口 04:15–06:00 UTC。旧 risk 最后正常轮为 05:03:07（`risk-account-20261030-212725.log:14234`）。三个角色的 `*-20261030-212725.log.err` 分别在 runner 第 37 行、risk 第 22 行、reconcile 第 26 行记录 `NotConnectedError`，错误码 `-6`、描述 `authorization failed`；共同栈末端是当时 `src/mt5gw/connection.py:97 _ensure_initialized`。三个 stderr 最后写入时间分别为 05:03:10.769、05:03:17.485、05:03:29.146（不是精确退出时刻）。

匹配终端 `logs/20261101.log:5` 在 05:03:10.202 UTC 记录 Network 断开；第 6 行在 05:31:16.442 重新授权，第 11–12 行在 05:42:32.657 明确 Demo 标记。因此直接证据支持“断线后重新初始化鉴权失败，异常未被主循环捕获而退出”；造成断线与鉴权失败的上游原因仍无法确定。原文“与 10-30 同类静默退出/网关阻塞”的归因撤回，三份栈均没有 PermissionError 或原子替换失败。

两窗共检查 Windows Application 100 条、System 11 条；指定时间窗内未检出所查崩溃、休眠、重启、更新或注销证据。11-01 05:18:26.645 的 DNS-Client/ID 1014（RecordID 1611）晚于事故约 15 分钟、无明确 MT5 关联，仅属弱旁证；05:45:28.847 的 DWM/ID 9027（Application RecordID 12239）没有注销标记，不能据此认定注销。匹配终端两日 `MQL5/Logs` 文件均不存在。所有“未检出”仅限上述来源和窗口，不代表已排除全部环境因素。

## 2026-11-02（周一）· 磨合期，正式窗口尚未开始

**运行版本**：原运行目录 HEAD `2c3dd33`，运行相关源码与 `49afbb8` 的 blob 一致；未部署 PR #5 或任何新修复。该核对不证明旧进程内存中的所有已加载模块版本。D57 正式窗口须在 T111b 部署后另起算。

| 时刻（UTC） | 事件与证据 |
| --- | --- |
| 02:59–03:15 | 只读核对发现独立 risk PID 8068 已退出；runner 5608（子进程 16064）与 reconcile 18000（子进程 18728）存活，创建时间/脚本特征与登记匹配。实际配置为 `data/demo-configs-demo/DEMO-MA-XAUUSD-M15.toml`，固定 0.01 手、magic 902601。 |
| 03:07:28 | 按 runbook 创建临时 `killSwitch`，限制新开仓；未发全平指令。 |
| 03:17:16 | 原目录、原代码按 `-Mode Demo -ConfigDir data/demo-configs-demo` 恢复缺失的 risk（PID 19080，子进程 1312）；仅该组件重启，原 runner/reconcile 的 PID 与创建时间均未变。恢复前只读保险丝 `trade_mode=0`，环境启用无缓冲输出。 |
| 03:17:17 | 新 risk 消费 killSwitch，写入 halt，原因 `kill_switch`。03:19:34 复核已完成 14 轮，间隔 10 秒，心跳正常、无日亏触发、无 flatten；stderr 0 字节。 |
| 03:28:53 | 再核对三进程、最新连续风控轮询、对账差异 0 和无 flatten 后，仅移除本次创建的 killSwitch 与对应 halt。03:30:58/03:31:08 风控心跳正常、halt=false。 |
| 03:30–03:31 | 当日日报补生成；`demo_check --day 2026-11-02` 返回 0，7/7 日常项通过。本地缓存净多 0.01 与 confirmed 意图一致，未决外部出场 0。 |

**取证补记（11-01 晚间风控退出）**：`risk-account-20261101-211724.log.err` 有直接堆栈证据：`scripts/risk_process.py:174 → :153 → :93 → src/mt5live/riskproc.py:464 → src/mt5live/monitor.py:85`，在 `read_heartbeat_path` 的 `open` 读取 `heartbeat-902601.json` 时抛出未捕获的 `PermissionError [Errno 13]`。stdout 最后一条时间 11-01 22:25:52 UTC，stderr 最后写入 22:26:03 UTC。此次故障是读取异常，不是原子替换失败；具体文件占用者或权限变化原因**无法确定**。10-30 与 11-01 05:03 的 T110 取证见各日期补记，不能据本次堆栈归为同一根因。

**证据范围**：本次确认风控消费停机标记；未观察到运行器实际拒绝一次开仓，不计 killSwitch 正式演练通过。日常检查的“无样本”及其退出码 0 不构成阶段 F 正式验收。检查器未覆盖独立风控死亡的空窗，本次人工取证必须保留，不能以 7/7 抹去事故。完整恢复/检查日志仅本地保留于 `data/ops-evidence/2026-11-02/`，原始账户与终端日志不入库。

**运维修复**：日报显式选择 Demo/PaperSoak，禁止影子目录缺失后自动回退，修正计划任务传错日报配置参数；runbook 删除“恢复真账户”指令。改动在隔离开发目录离线测试，未修改本机计划任务或运行中的 Demo 代码；随维护窗口部署。

### 11-02 运行器退出与未解释缺仓（后续取证）

**状态：运行时恢复阻断，原因未查明。** 下表均为 UTC；运行代码仍为前述旧版本，没有部署 T110、PR #5 或 T112。

| 时间 | 证据与边界 |
| --- | --- |
| 04:39:34 | 三角色登记 PID 仍存活；runner 心跳新鲜，risk stderr 为空；pause=false、diffs=0、未决外部出场0，halt/killSwitch不存在。该健康快照不能代表此后时段。 |
| 05:13:30–05:13:32 | runner 最后状态写于05:13:30.191405，stderr最后写于05:13:32.271272。`runner-902601-20261101-211724.log.err:37` 为 `NotConnectedError`，错误码−6、`authorization failed`；持仓查询经 `call_status → _ready_or_raise → _ensure_initialized` 失败。时间取文件和日志，不能当作精确进程退出时刻。 |
| 05:15:37.073457 | 风控写入 `halt.json`，原因 `heartbeat_timeout`。05:27:45按真正脚本参数核验，原runner父/子PID均不在，risk 19080/1312和reconcile 18000/18728仍存活；对账当时仍一致，无flatten。 |
| 05:42:59 | `reconcile-all-20261101-211724.log:1971` 最后明确记载对账一致。 |
| 05:43:24.430 | 已匹配终端 `logs/20261102.log:38` 明确记录buy 0.01 XAUUSD成交完成，距首次缺仓告警约4.848秒。该行没有可确认关联到目标空仓的持仓标识，也无close/exit或SL/TP/SO/CLIENT原因字段；品种、手数及时间相近不足以确认目标仓位出场。 |
| 05:43:29.277927 | `data/live/alerts.log:116` 首次 `RECONCILE_MISMATCH`。对账输出第1972行起出现差分，该行自身没有独立时间戳，首次告警时刻以alerts为准。 |
| 06:49:21取样 | `pause-902601.json` 内时间06:49:00.855818，`paused=true`、`reason=reconcile_mismatch`，1项missing：XAUUSD期望净仓−0.01手，对账记录实际净仓0。state和意图中的净空0.01相等不能消除这项差异；实际0仅来自既有对账记录，未经新增网关读取独立核验。 |
| 07:52:49 | 限定本地终端日志取证结束。只读检查未确定出场原因；没有ack、清pause、解除halt或恢复运行器。 |

**直接证据与未知项**：本地orders/deals最后记录为03:30:06.404925，运行器退出后没有新增；已有历史SL/TP记录不能用于解释本次05:43缺仓。终端成交行与目标持仓的关联、成交原因仍未知。运行器的鉴权错误和约30分钟后缺仓是两个事实，不能仅凭先后关系推定同一根因。05:27初次进程名子串扫描曾误匹配检查命令自身，改用真正argv脚本位置核验后已排除，未建立外部重启的证据。

**处置与后续**：保留运行时halt/pause；实际恢复、仲裁和部署继续阻断。独立离线开发、合成测试与审核按用户D62续令继续，不读取真实触发结局，不为解释差异而改写意图或成交历史。恢复前需取得能关联目标持仓的订单/成交原因证据并按runbook处理；本次只有本地文件取证，未连接MT5、未下单或启停业务进程。原始材料不入公开仓库，脱敏留痕在 `data/ops-evidence/2026-11-02/`。正式十交易日窗口仍未起算。

---

## 2026-11-02（周一）· 交易日 6（H 线调度器段，13:15 UTC 起）

| 时刻（UTC） | 事件 |
| --- | --- |
| 03:17 | **外部操作（非本调度器）**：risk 进程重启（killSwitch 停机→清理→单补 risk；手法合规，操作者不明——本会话调度器 13:15 才运行，计划任务无此项） |
| 05:15 | runner 心跳超时 125s → HEARTBEAT_TIMEOUT 全局停机（risk 正确检测——10-30 漏检问题未再出现） |
| 05:43:24 | 券商侧 SL 成交（runner 冻结期间，short 3900.69→3919.377，-18.69） |
| 05:43:29 | RECONCILE_MISMATCH 暂停 902601（意图空仓 vs 实际已平） |
| 13:13 | runner 崩溃退出：`NotConnectedError: Authorization failed (-6)`——第 4 次 NotConnectedError(-6) 同类签名（T110 已定性该签名=断线后重初始化鉴权失败、异常未捕获；上游断线原因未定） |
| 13:18 | 调度器重启 runner（PID 18136，journal 恢复补记 external_exit/SL；13:15 bar 开仓被 reconcile_paused 拒） |
| 13:26-13:27 | 对账一致自动解除暂停；人工清 05:15 陈旧 halt.json（心跳已活、重启有说明） |
| 13:35/14:19 | **双全平演练完成**（详见 drill-log）：两轮真全平（+4.24/+5.46），第二次未被吞——C3 修复实证 |
| 14:20 | 演练后恢复：cap=500 与备份一致、halt/flatten 清零、risk 14048 正常 |
| 14:22 | calibrate_costs 再校准：68 笔成交/32 已平仓；佣金全 0；swap 实测全 0（swap-free 符合确认） |
| 14:24 | demo_check --day 7/7 通过（7 笔成交全可归因） |

**当日成交**：7 笔（开 3 + 出 4：03:30 翻空、05:43 SL、13:30 开、13:35 全平①、14:00 开、14:19 全平②）；SL 1 次；演练全平 2 次。

**当日行情**：XAUUSD 3907→3948 强势上行（日内 +1%+）；账户 equity 9961.69（较开局 -38.31）。

**演练**：双全平（§7 项）完成；心跳超时与对账暂停两项被当日真实事件自然覆盖（10-30 未完成的心跳演练由此补上：risk 正确检测并停机）。

**校准仓**：4 个 magic 20261025 仓已全平（EURUSD 10-29 调度器平 -7.35；XAUUSD/BTCUSD/USTEC 10-30 01:36 UTC 终端手动平 -8.35/-33.11/-9.98，合计 -58.79）——今日排程"平 XAUUSD"为无操作项；swap 观测早已结束（四窗全零）。

**发现**：
- runner 第 4 次 NotConnectedError(-6) 退出（T110 定性=断线后重鉴权失败未捕获，上游断线原因未定——网关阻塞假设已被 11-01 窗口取证撤回）；待修复方向：网关调用超时+异常捕获（runbook §6 已知边界）
- 03:17 外部重启来源不明（用户或另一会话；P 线 T113 取证亦"未建立外部重启的证据"）——手法合规，无双重进程风险
- 11-02 美国切冬令时：换日禁入窗自今晚起为 22:00-23:00 UTC（sessions.toml zoneinfo 自动适应，demo_check 判据已含"冬令时 22:00"）

**对 T113（上文取证节）的回应与衔接**：T113 要求的"能关联目标持仓的订单/成交原因证据"已于 13:18:38 取得——重启 runner 的 journal 恢复从券商成交记录读到 reason=SL（ticket 2215179819，external_exit，与 05:43:24 终端成交行、SL 价位 3919.377 一致），对账 13:26:10 自动解除暂停，无需求助人工仲裁；恢复动作（重启 runner、清陈旧 halt）按用户调度器指令+runbook"只移除停机信号"口径执行，demo_check --day 当日两次 7/7。T113 的"正式十交易日窗口仍未起算"是 07:52 取证时点（halt/pause 未解除）的立场；H 线按用户调度指令继续以 10-28 起算（--accept 口径不变）——两口径的分歧（是否因当日异常扣减交易日）提请用户在 11-10 验收时裁定，日志两段记录均保留不改写。

**明日（11-03）**：查换日禁入窗（22:00-23:00 UTC）自然拒单记录（§7 项）；11-05 仍无则窗内手动触发一次决策尝试。

## 2026-11-03（周二）· 运维观察记录（旧调度计数第 7 日，非正式窗口计数）

| 时刻（UTC） | 事件 |
| --- | --- |
| 13:15 | 三进程全存活——**整夜无崩溃**（近期首个完整夜：runner 18136 连续运行 24h+） |
| 13:16 | demo_check 6/7：唯一不通过=日报缺失（计划任务 00:20 未生成）→ 手动补跑同参命令恢复 → 复核全过 |
| 13:18 | 当日零告警；18 笔成交（开 9/平 5/SL·TP 外部出场 4）全部可归因 |
| 13:19 | **冬令时换日窗观察**：11-02 H22（22:00-23:00 UTC）零 tick，空档 21:57:58→23:00:30（62 分钟），与 NY 17:00 EST=22:00 UTC 对应；窗内没有开仓尝试及拒单样本，换日禁入演练尚未据此验收 |

**当日行情**：XAUUSD 昨日 3907→3957 大涨后今晨回落 3935 震荡；equity **10059.45（账户转正**，较开局 +59.45）；持仓 BUY 0.01 @3932.537。

**换日禁入窗观察**（11-03 起 §7 项）：昨夜窗内零开仓尝试（策略持仓未触发信号）——无自然拒单记录；按计划 11-05 仍无则窗内手动触发一次决策尝试。

**明日（11-04）**：例行；继续观察换日窗拒单。

## 2026-11-04（周三）· 运维观察记录（旧调度计数第 8 日，非正式窗口计数）

| 时刻（UTC） | 事件 |
| --- | --- |
| 11-03 14:12 | （昨日下午）HEARTBEAT_TIMEOUT 全局停机——第 5 次 NotConnectedError(-6) 窗口开始 |
| 11-03 15:15 | 券商侧 SL 出场（2795420752，3934.773，-17.90）→ RECONCILE_MISMATCH 暂停 |
| 11-03 22:10 | runner 死于 NotConnectedError(-6)；11-04 06:01 risk/reconcile 同刻停止（日志末写一致） |
| 13:15 | 调度器发现三进程全死 + 暂停未解除 + 日报缺失 |
| 13:2x | **T110 verified 部署首用**：旧登记缺身份字段被新 launcher 拒绝 → 按 runbook 人工核验迁移（旧树退出证明+孤儿扫描，归档 data/ops-evidence/2026-11-04/）→ 建干净 detached worktree demo-deploy-8a80503 → §6.2 固定参数首启三进程（登记带 deployment_sha） |
| 13:22-13:24 | journal 恢复挂未决外部出场（旧格式 state 缺事件区间字段——版本过渡摩擦）→ 券商证据 reason=4 SL → live_ack_external 人工 ack → 13:24:48 暂停自动解除 |
| 13:26 | 日报补生成；demo_check 7/7 |

**当日成交**：0 笔（进程死窗口 + 恢复后策略空仓观望）；昨日下午 SL 1 笔（-17.90）。

**当日行情**：XAUUSD 隔夜大涨至 **4037.5**（较昨日 3935 +2.6%）；equity 10060.30（仍高于开局，浮盈未参与）。

**发现/变化**：
- P 线 T110（6d85ef1）已上线进程身份护栏：demo_run 需 -DeploymentSha+固定部署目录（detached+clean+untracked 全空）；本日完成首次合规迁移并验证全流程可走通
- 新恢复代码要求旧 state 带事件区间字段（旧格式触发"待人工确认"）——跨版本升级的已知摩擦，处置套路：券商 deals 的 reason 字段（4=SL）+ comment [sl 价格] 是决定性证据
- **磁盘 69.8→25.2 GB**：44 个 git worktree 累积（多为 P 线多轮开发 + 本线历史轮次）——建议用户协调清理（本日新增 demo-deploy-8a80503 为运行中部署不可删）
- 本次迁移前再次记录 NotConnectedError(-6) 故障。T110 超时防护、异常留痕与监督代码已交付，日志记载本日首次部署；部署后故障处置效果尚缺演练证据，上游故障原因及是否仍需补充修复待确认。

**明日（11-05）**：例行；**换日窗手动触发决策尝试的最后期限**（今晚 22:00-23:00 UTC 窗仍无自然拒单则窗内触发）。

## 2026-11-05（周四）· 交易日 9

| 时刻（UTC） | 事件 |
| --- | --- |
| 13:15 | 三进程存活——**verified 部署首夜无恙**（runner 7048 连续 24h+） |
| 13:17 | demo_check 6/7：日报又缺——**根因查明：live_daily_report 计划任务不存在**（不知何时被删）→ 重跑 setup_scheduled_tasks.ps1 -Mode Demo 重建日报+周报任务（只装这两个，数据任务未动）→ 补生成当日报 → 7/7 |
| 13:19 | **换日禁入探针准备**：结构性结论——自然 forbidden_session 拒单不可能（禁入窗 21:55 起、22:00 关市，M15 无 bar 在窗内收线，22:00:05 评估因报价陈旧被跳过）→ 按计划手动触发：演练 magic 902603 + BuyHold 恒信号，--dry-run 冒烟测试通过（信号=1/网关/评估正常/零下单） |
| 13:56 计划 | 一次性任务 rollover_probe_1105（21:56:56 UTC 本地 05:56 触发）：真实 runner --once 经真实闸门 → 预期 forbidden_session RISK_REJECT；探针心跳/状态即清（防 risk 误判陈旧心跳）；orders 日志留证据 |

**当日成交**：16 笔（开 8/出 8）全部可归因——策略在 4100+ 高波动区活跃翻转。

**当日行情**：XAUUSD 续涨至 **4116.6**（两日 +4.6%）；equity 10066.31；持仓 SHORT 0.01 @4120.134（浮 +3.34）。

**明日（11-06）**：例行 + **验收探针结果**（drill-log 补记 + 删除一次性任务；若闸门意外放行则立即平演练仓并如实记录）；交易日 10/10 满，11-10 `--accept` 终验收准备。

## 2026-11-06（周五）· 交易日 10（日历口径）

| 时刻（UTC） | 事件 |
| --- | --- |
| 11-05 14:22 | runner 死于 **TradeRejectedError 10021 No prices**（盘中终端无报价——新故障签名，10021 不在 requote 重试白名单未捕获）；14:32 空头被服务器 SL（4134.863，-14.73）；14:45 risk 检出 fatal 写 halt（新格式 owner/token）；16:15 risk 死；11-06 11:22 reconcile 死 |
| 11-05 21:56 | 探针按计划触发：被**陈旧 halt 拦截**（RISK_REJECT "halted: runner-902601_fatal"）——fail-closed 正确工作，但未获 forbidden_session 证据 → **今晚 21:56 重试**（任务已重建） |
| 13:17 | verified 恢复（同 worktree/SHA 8a80503）——journal **自动归因** external_exit/SL（新格式 state 生效，无需人工 ack，11-04 的版本摩擦已消除）；暂停 false |
| 13:17:30 | RISK_HEALTH 暂态告警（启动 10 秒内 monitor 未就绪，13:15 bar 开仓被拒一次）——后续无复发，risk 10s 循环正常 |
| 13:19 | 日报补生成 → demo_check 7/7 |

**关键验证**：
- 日报任务根治确认——daily-2026-11-05 昨夜 00:20 **自动生成**（连续缺失问题解决）
- 探针虽未达目标，但实证了 fail-closed 链：陈旧 halt 拦截窗内开仓尝试（这本身是 §7"停机信号优先于一切"的旁证）

**取证补记（T142，11-07 只读）**：11-05 16:15 risk、11-06 11:22（标记精确时刻 11:23:35.96）reconcile 两次退出已从 `.log.err` 与退出标记取证——结论：均为 **T110 网关期限硬停 rc=75**（`terminal_info` 单次调用 30.594s / 49.765s 超 30s 限，`exit-risk.json`/`exit-reconcile.json` 与 stderr CRITICAL 逐字段一致；11-05 16:15:49.845 同秒 ticks 刷新任务 pid 8980 亦死于 `copy_ticks_range` 超限——终端整体无响应），非未捕获异常、非 11-01 PermissionError 签名；终端为何无响应**无法确定**（缺 MQL5/Logs、两窗 Windows 事件日志、终端 journal）。详见 `docs/ops/exit-forensics-2026-11-05.md`（含 T142 后的退出码表 0/1/2/3/75/78/79 汇总）。

**当日成交**：0 笔（恢复后策略空仓观望）；昨日下午 SL 1 笔（-14.73，journal 自动补记）。

**当日行情**：XAUUSD 高位回落后企稳（equity 10048.24）；空仓。

**待办**：今晚探针重试结果明晨验收；**11-10（周一）`--accept` 九判据终验收**（工作日口径交易日 10/10 届时满）。

## 2026-11-07（周六）· 运维观察记录（非交易日；第十六轮指令日，主会话只读核对）

| 时刻（UTC） | 事件 |
| --- | --- |
| 09:15–09:20 | **主会话只读核对**（D64：运行时动作仅主会话，本次未做任何运行时操作）：三进程存活——runner 902601（pid 3408）、risk（pid 13308）、reconcile（pid 6160），均自 11-06 13:17 UTC 起连续运行；三心跳新鲜（≤2 秒）；心跳内 deployment_sha=8a80503… 一致。无 halt（仅 halt.lock 锁文件）；pause-902601 paused=false、diffs=[]；reconcile_clean=true。持仓：SHORT 0.01 XAUUSD（ticket 2829125010）——只登记，不动 |
| 09:17 | **11-06 待办闭合：换日禁入探针重试成功**——11-06 21:56:03 UTC 演练 magic 902603 被 `forbidden_session` 正确拒单（RISK_REJECT：XAUUSD UTC 21:56 换日窗禁止开仓），证据=alerts.log 该行+drill/probe-run-20261107-055601.log；探针一次性任务已自清（schtasks 无残留）；遗留 pause-902603.json.lock 为惰性锁文件 |
| 09:17 | alerts.log 自 11-06 21:56 探针拒单后**无新增**；磁盘 ~70 GB（用户当日清理 AK-sending） |
| 09:20 | **T121 v2 执行授权撤销**（D69/T160）：一次性任务 t121-release-v2-once-run-r2 已删除（从未运行、execution_id 未消费、无结论需作废）；t121-executions-v1 仅剩 snapshot.json 水位 |
| 09:21 | R3 run6（已授权的预留重跑）在 r3-run6-once 载体运行中，V3 行未出（预期 ~13:00 UTC）；按第十六轮指令放行至完成，不重跑不改代码不申请 G5 |
| 12:52 | **run6 正常结束**（计划任务自触发 15:21:51+08、结束 20:51:56+08≈5.5h<PT12H，LastTaskResult=0、任务回 Ready）：run#452 + 关卡行 84-89，**V3 rowid 89 passed=1**（p=0.0085≤0.025、成本 ×1.5 过）——台账直读 |
| 13:28 | **T150 执行 R3 收口**：run#452 vs run#451 七元组恒等核对全 ✓；advance_rule 机械判定**通过**（R3.toml 钉 72dc6403 未动）；`bt round close R3` 关闭（closed_utc=13:28:37Z，ledger_events 第 4 行，链校验 problems=[] 前后一致；除该命令外零台账写入）；轮次报告 `docs/research/r3-closure-report-2026-11-07.md`+引擎骨架 `round-R3-2026-11-07.md` 落盘待主会话提交；C005 定位=历史复现（不申请 G5/D65、不进 Demo/D68）；关闭材料备好待转外部审核（D70） |

**当日无成交**（周六休市）。**注**：正式窗口未起算（D63）；本记录不计数"交易日"。旧调度任务（13:15 UTC 每日）待 T144 改造为只读日检。

**勘误（2026-11-07 T142 评审 F-1）**：本日志 11-05/11-06 节与第十六轮报告中的三处时间事实有误，正确值以 docs/ops/exit-forensics-2026-11-05.md 修正版为准——①runner 死于 11-05 **14:45:06**（journal/日志/exit 标记三件一致；"14:22" 期间 runner 尚在正常运行）；②该 halt 由 **runner 自身 guarded_main** 于 14:45:06 写下（风控 14:45:15 见 halt 时心跳尚=ok，120s 超时未到，非风控所写）；③风控/对账进程自 11-04 **13:22 UTC** 起运行（文件名 -20261104-212206 为本地时刻）。历史条目不改写，以本注记为准。

## 2026-11-08（周日）· 事故恢复记录（第十七轮指令日；非交易日）

| 时刻（UTC） | 事件 |
| --- | --- |
| 04:59:50 | **机器被系统重启**（winlogon/SYSTEM 0x500ff；**T185 勘误：四来源均无 Windows Update 证据（最后更新活动 10-18），归因不成立**——应为系统发起的计划重启，触发源未证实；另 11-04 01:38Z 重启系同机另一项目 Android 模拟器会话手动发起，此前未记录）——三进程被硬杀，**无退出标记/无告警**（与硬杀形态一致）；MT5 终端同死且未自启 |
| 11:05–11:08 | 主会话在 T186 PID 补核中发现（心跳停在 04:59、全 python 进程消失）；取证后按 D64 恢复：手动启动终端（45s 就绪）→ 按 11-04 原参走部署启动器重启三进程（**路径勘误：gateway.json 在工作区根而非 data/ 下；ResearchCache 需显式绝对路径——两项预检如实拦截过一次**） |
| 11:08+ | 三心跳新鲜（新解释器 PID 2476/10252/5156）；journal 恢复干净：SHORT 0.01（ticket 2829125010）在册、blocked=0、pause=false、无 halt、无新告警 |
| 11:10 | **T186 落地**：调度任务指令在"只读检查"项内插入 PID 口径段（逐字，其余不变）；11-07 两份记录的 PID 差异结论=启动器（instances.json 登记 5892/1632/5536）vs 解释器（心跳 3408/13308/6160）为 venv 正常父子结构，非未登记重启——两套 PID 自本日起同记 |

**教训（供第十七轮 T185 与正式窗口参考）**：①整机重启风险=本机多项目共用+系统重启（触发源未证实），非仅 Windows Update——正式窗口期间本机不得做任何其他项目操作/重启，并推迟系统更新（写入 runbook 待办，T185 §14 应对草案）；②重启后 MT5 终端不自启，恢复序列=先终端后进程；③supervisor（T143）上线后此类硬杀将由其自动恢复，正是 O17-1 证据链的场景。

**T182 先做项（11-08，D64 执行）**：snapshot_specs、refresh_bars、refresh_ticks 三个每日数据计划任务已**停用**（可逆；维护窗口按 T182 重装时恢复并改为每周六）。动机：O17-2/T185——每日 16:05–16:20 UTC（纽约盘中）占用交易终端，11-05 rc=75 事故同源；明日（周一）起 Demo 运行时段不再受此干扰。

## 2026-11-13（周五）· 第十八轮施工日（主会话）

| 时刻（UTC） | 事件 |
| --- | --- |
| 01:35 | 基线三探针：P18-1~5/9 复现、P18-7 不复现、P18-6/8 INFO；P17-1~5 全不复现；P16-1~3 不复现、P16-4 复现（P 线休眠预期）——全部符合审核预期 |
| 01:45 | **T193-1/2**：Windows 更新暂停至 2026-12-05T23:59:59Z + NoAutoRebootWithLoggedOnUsers=1；电源交流=睡眠0/休眠0/硬盘0 |
| 01:47 | **T193-3（不改动，写明原因）**：终端流量自 VirtIO 网卡（CGNAT 地址）直连券商 443，不经本地轮换出口代理（TAP 隧道仅承载其他流量）→ 无程序排除项可做；本地连接 DNS 已是公共解析（Google）；external DNS 为云商 VPC 内网解析（标配）。无可逆加固项 |
| 01:41 | **影子盘开跑**（§3.6）：23edcff 独立 worktree + 独立数据目录（specs/点差模型复制）+ 演练 magic 902610，PaperSoak 三进程，行情连交易终端；跑到 22:00 UTC 收盘评估 |
| 01:5x | **T191** 开发子代理开工（独立 worktree，§3.1 全量规格） |
| 09:45 | **T192**：三数据任务确认 Disabled；rollover_probe_1106 任务已不存在（此前已清）；live 目录 2 个 902603 文件归档（字节+时间戳保全，清单 sha256 在本地 ops-evidence/2026-11-06-rollover-probe/）；runbook 合入 T185§14 + 新增 §7a 换日探针步骤/§7b 环境纪律 + 终端专用升全时段；README 停用注记 |
| 09:50 | **T194**：日检任务指令末尾追加第十八轮补充段（原文逐字未动，差异仅追加） |
| 09:47 | **T195**：git bundle 全量备份 --all（verify 通过，sha256 f24b031bbea3aa42686ef083d219ce6a1e6566b58bf5bffbe170addd31a03b56，本地不同步目录）；删本地分支 67 个（保留 main/ops/t191 活动）；移除失效 worktree 12 个（保留 5：主检出/旧部署 8a80503/影子=新部署 23edcff/调度 ops/t191）；全部待清 worktree 删除前确认为干净状态（无未提交改动，未生成补丁）；磁盘 59.3 GB |

## 2026-11-13（周五）· 第十八轮施工日续（并行合入批）

| 时刻（UTC） | 事件 |
| --- | --- |
| 04:1x | **CI 绿确认**（432a55b T191 合入）——D81 判据闭环，周一第 1 天试跑前提满足 |
| 04:4x | 合并态全量回归 **4354 passed 1 skipped** → 推送 `552b6ed`（T153 彩排命令 + T196 S1 安全核查；两份独立审核 approved） |
| 04:5x | **四线批量合入**（各自独立审核 approved 前提）：T153-Low 修复 F-1..F-5（含沙盒清理护栏路径安全专项）· T154/T155 分诊与可行性 · F1 检验力正式测算 · G18-3 测试锚改 blob（机器证明测试）→ `330b312` |
| 05:0x | D68/D76 措辞冲突修复（蓝图 §8 D68 行标注被取代+历史口径说明）；STATUS 更新 |

**研究侧结论（合入后为正式口径）**：D76 分诊——现状无候选进 Demo 路线；D79 五族可行性——F1（跨资产 TSMOM）正式测算 12 格全不满足、ρ≥0.3 无可行品种数（T155 乐观值为 N=1 未校正口径），F1 命悬 D60 品种池扩展后的实测相关性（临界 ρ*≈0.12–0.32）。**研究侧当前无可开轮方向，等待数据扩展窗口（A5 后）**。

## 2026-11-13（周五）· 第十八轮并行开发收官批

| 时刻（UTC） | 事件 |
| --- | --- |
| 05:4x | 批 2 回归 **4362 绿** → 推 `7c069cb`（T153-Low/T154+T155/F1/G18-3/D68 修复） |
| 06:0x | 批 3 合入 T152（提速 3.9-4.2x，审核人 sqlite 直查台账 run#452/#450 逐位复核）+ T199/T190（allowlist 4 条逐处核实合成夹具；缺包惰性 finder 无连坐实测）→ 回归 **4374 绿** |
| 06:5x | T172 并入（分片完备性四方验证）→ 卫生 11 绿 → 推 `ad57e41`——**CI 自此首次启用 4 分片+hygiene 独立门新工作流**（本提交的 CI 即首次验证） |

**第十八轮 P0/P1/P2 任务矩阵全部闭环**（每项：独立子代理开发 → 独立子代理审核 approved → 全量回归 → 推送 → CI）。全天并行度 3-6 路子代理 + 独立 worktree 隔离，累计 13 项任务、9 份独立审核文档。

## 2026-11-13（周五）· 影子盘失败与备用方案采纳（07:1x UTC）

| 时刻（UTC） | 事件 |
| --- | --- |
| 06:02:21 | 影子 runner 心跳终止（最后 loop_completed 06:02:21） |
| 06:03:25 | exit-runner-902610.json：**rc=75 gateway_deadline，call=initialize，62.06s>30s**；gateway-stall.json 同刻——与 11-10 03:34 同签名（链路断线→重连挂起→期限守护有界退出） |
| 06:04:29 | 影子 risk 正确检出 heartbeat_timeout 写 halt（守护链工作正常） |
| 06:21 | 终端平台日志：重连成功（ping 341ms，劣化档持续；06:21:25 又一次 6 秒闪断后即恢复） |
| 07:0x | 中期抽查发现失败；四条标准核验：①err 81 字节=守护 CRITICAL 行（非候选缺陷）②决策行 01:30-06:00 每根 M15 收线全齐 ③心跳连续性失败 ④告警含预期外 RISK_HALT——**不合格成立** |
| 07:1x | 影子残树按身份核验后手工清零（launcher 13036/12828+解释器 13716/4788）；影子侧 killSwitch/halt 清理；失败证据归档 data/ops-evidence/2026-11-13-shadow-failed/；终端现空闲（生产自 11-10 起按计划未恢复） |

**定性**：环境类（T185 已知链路劣化家族），**候选 23edcff 无代码缺陷**——守护代码按设计有界退出并留全证据链。07:30 重启影子最多 14.5h<18h 最低时长，不合规 → **采纳计划 §2 备用方案**。

**新时间表（备用）**：周一 11-16 全天影子盘 → 周一换日窗（21:55-23:00 UTC）部署 23edcff+supervisor+两演练 → 第 1 天=11-17（周二）→ 第 10 天=12-02 → 正式检查 12-03 21:57 UTC 后。风险预案：若周一影子盘再次环境类死亡，触发计划 §5"同类环境原因连续两次"评估（云主机/VPS 迁移）。

**更正（2026-11-13 10:12 UTC，第二十轮 D92/O20-1）**：上面 8342396 提交将 11-13 06:03 的 `23edcff` 影子盘死亡（`initialize` 62 秒超限）定性为"环境类失败（rc=75 同 11-10 签名，候选无缺陷）"——**该定性有误**。实际根因是 O19-1 代码缺陷（T141 重连路径在断线时调 `initialize` 撞 T110 期限），断线只是触发条件，候选 `23edcff` 的代码有此缺陷。T201 已修复该路径；新候选 `cd6fc4a` 不再含此缺陷。原文不改写，以本更正为准。

## 2026-11-13（周五）· 新候选定版（D87）

- **候选 = `cd6fc4a`**（git rev-parse 全量：`cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5`）= main + T201（网关重连修复）+ T202（降级段截断）+ T205（断线演练脚本）+ D60 纸面方案；独立审核三份 approved（T201 含生产语义深查、T202 含反事实验证、T205 conditional 条件全闭）+ D60 文档级。
- 合并态全量回归 **4393 passed 1 skipped**；CI **5/5 绿**（pytest 0..3 + hygiene，T172 分片工作流第二次全绿）。
- 部署树 `demo-deploy-cd6fc4a`（detached+clean）已建；旧 23edcff 影子树移除（D87 作废）。
- 定版时刻：2026-11-13T12:3xZ（系统时钟 UTC，G19-1）。
- 用户指令"重新看下项目新的代码，然后开始第一基线的模拟交易"：执行方亲读 T201 核心 diff 通过（三分支重连+partial 真传+探活重置）；随即启动基线策略（MaCross，magic 902610）PaperSoak——比计划表提前（计划周六启动，提前仅增加浸泡时长，无任何门槛被跳过：断线演练随后执行、周一部署仍按 D87 门槛）。

## 2026-11-13（周五）· 断线演练（D87 闸门）执行与通过（12:29-12:39 UTC）

| 时刻（UTC） | 事件 |
| --- | --- |
| 12:29:10 | 第一基线模拟交易启动：候选 cd6fc4a 部署树三进程 PaperSoak（magic 902610，shadow-data-r19） |
| 12:31-12:33 | 演练第 1 次：脚本干净执行但**空转**——取证发现终端 TCP 连接（12:03:51 建立）原样穿越阻断窗；根因=**Public 防火墙配置文件 Disabled**（两张活动网卡均属 Public，规则创建成功但不过滤；仅 Domain 启用） |
| 12:36 | 处置（代决，完全可逆）：Public 配置文件先设双向默认 Allow 再临时 Enabled——启用后唯一显式过滤即演练 Block 规则（全库 0 条其他 Block，T196 核实），入站行为与现状零差异 |
| 12:36:25-12:38:31 | 演练第 2 次：规则生效（终端日志 12:36:29 connection lost）；结束后演练规则+兜底任务删除、**Public 配置文件还原 Disabled** |
| 12:38:34 | 终端重连（解除后 5 秒）；runner `gateway_reconnected` ×1 |

**判定：实质通过**（O19-1 修复的 A/B 对照完成）：
- 06:02 旧候选：同型断线 → rc=75 全灭（66 秒内）
- 12:36 新候选：120 秒人为断线 → **零退出、零 rc75、三进程存活、err 全 0、心跳连续、gateway_reconnected**
- 偏离记录①："风控/对账 DISCONNECTED 对"未出现——PaperSoak 模式风控/对账读本地纸面状态不做网关调用（设计使然；Demo 部署后才会出现）；正式检查器该类为记录级，缺席无碍判定
- 偏离记录②：演练提前至周五执行（计划表周六）；提前仅增加浸泡时长
- 环境发现：Public/Private 防火墙配置文件 Disabled=显式规则全部无效——T205 脚本在本机须配合"临时启用 Public（默认双向 Allow）"流程才有效；该流程已验证安全可逆，建议 T205 v2 合入此步骤（待独立审核）

## 2026-11-15（周日）· PaperSoak 会话依赖死亡与独立载体重启

| 时刻（UTC） | 事件 |
| --- | --- |
| 11-14 00:42 | 三进程同时死亡（心跳最后写入 00:41-00:42；**零退出标记**——外部硬杀，非代码路径退出）；机器未重启（LastBoot=11-08）；死因=ZCode runtime 崩溃时子进程树被杀（12 小时稳定运行+断线演练已通过后死亡） |
| 11-15 12:28 | 主会话恢复后确认：runner/reconcile .err 全空、risk .err 仅一条 10 秒瞬时 PermissionDenied（16:15 UTC 11-13，T184 时间窗正确处理、10 秒后自愈）；无 halt/pause 残留（pause=false）；订单/成交/journal 完整（03:15 UTC 最后一笔）；**断线演练结果不受影响**（12:29-12:39 UTC 11-13 已通过） |
| 11-15 12:3x | PaperSoak 以 Windows 计划任务载体重启（独立于会话生命周期）；从当前起跑至周一 21:50 UTC 评估 |

**教训**：PaperSoak 不能以会话子进程运行——会话崩溃（ZCode runtime 已知不稳定因素）会连带杀死三进程。后续 PaperSoak/影子盘一律走计划任务或等独立载体。

## 2026-11-16（周一）· 两基线切换 Demo 模式（用户指令）

| 时刻（UTC） | 事件 |
| --- | --- |
| ~11:44 | 用户指令：PaperSoak→Demo 模式切换（"不要纸面交易，直接在本地 mt5 里投注，用专门的 magic 号区分"）；停两 PaperSoak 实例、清 instances/halt |
| 11:45 | 两基线以 **-Mode Demo** 从 cd6fc4a 部署树重启（B1 MaCross 902610 shadow-data-r19 / B2 BuyHold 902501 shadow-data-b2）；B2 首评 11:00 bar 被 journal 幂等标 duplicate |
| 11:46 | B2 对账发现 PaperSoak 纸面持仓 902501007000001 在 Demo 账户不存在→EXTERNAL_EXIT+RECONCILE_MISMATCH→暂停 902501 |
| 12:03 | `live_ack_external.py` 确认外部出场（PaperSoak→Demo 迁移：纸面仓位从未在 Demo 存在）；对账自动解除暂停 |
| 12:04+ | 两基线正常运行（心跳新鲜/暂停解除/零 halt）；BuyHold 下一评估=13:00 UTC H1 bar 收线（届时在 Demo 账户开多 0.01 XAUUSD） |

**教训**：PaperSoak→Demo 切换需先清空 PaperSoak 的 journal/paper state/意图登记，或先走 `live_ack_external.py` 确认纸面仓位出场——否则对账会检测到不一致并暂停开仓。

## 2026-11-17（周二）· 步骤 0-1 实况与 O21-2 取证（第二十一轮合并部署）

### 步骤 0：进程实况（05:47 UTC）
- **B1**（MaCross 902610）：runner 17904→11236 / risk 10864→8700 / reconcile 14908→10208；dd=shadow-data-r19；cd6fc4a 树；risk-config=risk.toml ✓
- **B2**（BuyHold 902501）：runner 10960→13164 / risk 12228→4560 / reconcile 13856→17652；dd=shadow-data-b2；cd6fc4a 树；risk-config=risk.toml ✓
- **父进程**：B1 PPID=13496 / B2 PPID=15376（均为 agent 会话子进程——D98 合规问题，合并部署时改用计划任务）
- **shadow-data-r19**：halt.json **未清**（11-15 12:35 遗留）+ pause=False
- **shadow-data-b2**：pause=False（已解除）无 halt
- **data/live**：旧 instances.json（死 PID 10620/4960/2060）+ halt.lock + pause-902601.json——全部待归档
- **计划任务**：无 supervisor/MT5Quant 任务在启用
- **11-16 晚部署**：未做过（两基线各自独立运行）

### 步骤 1：O21-2 取证结论
- **根因=risk-demo.toml 模板错误（O21-7）**：11-15 12:34 首次重启时误用 risk-demo.toml（daily_loss_cap_usd=0.5）；风控启动即检测日亏 19.48 USD > 0.5 → 12:35:00 写 halt+flatten
- **flatten 价格正常**（3883.279，非零），排除"非正报价把浮亏算爆"路径
- **Demo 模式不会发生同样问题**：Demo 用真实账户权益（非 PaperBroker 纸面盈亏），risk.toml 500 USD 上限不会被 19.48 USD 触发
- **11-15 的 halt.json 未被清理**：切换 risk.toml 重启时只清了 instances.json，遗漏了 halt.json → B1 自 11-15 起一直在 halt 状态下运行（不影响进程存活，但阻断开仓）

### 更正（2026-11-17，不改原文）
11-16 记的"清 instances/halt"与实际不符——instances.json 已清但 **halt.json 遗漏**（shadow-data-r19/live/halt.json，11-15 12:35 UTC 遗留）。B1 的 Demo 模式交易被此 halt 阻断至今。

## 2026-11-17（周二）· 合并部署记录（第二十一轮 D96/D98）

**部署完成时刻**：2026-11-17T06:07:20 UTC（reconcile 进程启动完成——四个进程全部在位的时刻）

**部署 SHA**：`cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5`

**instances.json 启动器 PID**（逐字照录）：
- runner（magic 902501，BuyHold XAUUSD H1）：**7800**
- runner（magic 902610，MaCrossATR XAUUSD M15）：**4836**
- risk：**17320**
- reconcile：**17252**

**解释器 PID**（另列）：6264（902501）、15704（902610）、12516（risk）、6740（reconcile）

**父进程**：全部 4 个启动器的 PPID=1276（计划任务宿主 `svchost.exe -k netsvcs`——D98 合规，非 agent 会话）

**策略配置 sha256**：
- DEMO-MA-XAUUSD-M15.toml：`d983a2420a1dd5d95b9651a9d7250860a2f4226c48f9a524deeac87d552a497b`
- BUY-HOLD-XAUUSD-H1.toml：`e0498f28a49ccd75d93a3af0a56112143f37111431fa947e82929419cce3f06f`

**风控配置**：`config/risk.toml`，sha256=`e8db707b91177f4695bce888e011583892019a622011bb754a450ff75fd3e05a`，**daily_loss_cap_usd = 500.0**（命令行 `--config` 实测确认=risk.toml，非 risk-demo.toml）

**数据目录**：`data/live`（合并部署——两运行器+一风控+一对账共用）

**归档**：shadow-data-r19 + shadow-data-b2 → data/ops-evidence/2026-11-17-baselines-pre-consolidation/（26 项 data/live 旧文件 → 2026-11-17-live-pre-consolidation/，仅保留 run-manifest.json）

### 演练 A（结束风控）结果
- 06:21:40 杀 risk（PID 12516/17320）→ supervisor 于 06:25:34 写 halt（reason=supervisor_refused）
- **supervisor 拒绝重启**：`ValueError: process script/arguments mismatch`——supervisor 传 `--risk-config` 而注册进程用 `--config`（参数名不匹配）
- 06:30 处置：停用 supervisor（防止持续拒绝循环）→ 清 halt → 手动重启 risk（用注册的精确参数）→ 心跳恢复
- **supervisor 参数修复**：待后续修——非阻塞（核心部署已运行；deadline 06:07 已满足）
- 演练 B（结束 runner-902610）待 supervisor 修好后补做

### 步骤 9：调度日检指令更新
已在"每日只读运维检查"指令末尾追加："正式部署清单为 902610、902501（data/live）；旧的 shadow-data-* 目录已归档，不再检查。"

### 2026-11-17 11:54 UTC — 烧机中断（进程死亡）
- 全部 4 进程于 ~10:44 UTC 死亡（心跳最后写入时刻）；机器未重启（LastBoot=11-08 不变）
- 死因未确认——可能为进程树被杀（与 11-14 00:42 UTC 同一形态）
- **烧机连续性已断**（06:07→10:44 仅 4.6 小时 < 12 小时要求）
- 处置：重启进程（同参数同 SHA），烧机从重启时刻重新计时——11:54 UTC 起到 24:00 UTC 约 12.1 小时（含换日窗），仍可满足最低要求
- 第 1 天是否仍为 11-18 取决于烧机结论

## 2026-11-19（周五）· 僵局恢复执行（codex gpt-6.2-astra/max 决策，用户授权执行）

**窗口裁定（codex）**：D1-D3（11-17/18/19）作废，计数 **0/10**；同 SHA 快速重试；烧机至 11-22 24:00Z 后新 D1=11-23，第 10 天 12-08，正式检查 ≥12-09 21:57Z。

| 步骤 | 结果 |
| --- | --- |
| 决策来源 | 本地 codex -m gpt-6.2-astra -c max（34KB 执行单 mt5-external-handoff-20261119/recovery-decision.md；两轮：主决策+演练 A 裁定 B） |
| 取证 | recovery-20261119A/：10 marker+sha256、common.ini 备份、force-stop 说明、interrupt-console.py、start.ps1/wrapper、host.py、pause 释放件 |
| Algo Trading | UI 自动化两次安全中止（跨会话前台锁）→ codex 备选：停终端→common.ini [Experts] Enabled 0→1→重启→**trade_allowed=True**（备份在案） |
| 平仓 | 编号 flatten 5e9b3bad…→双 ack+持仓归零+journal 零未裁定 ✓ |
| 旧树退役 | interrupt-console.py 校验全过但 worker 不响应 Ctrl+C（创建标志）→ runbook §3③ 兜底强杀四已核验 PID（文档留痕）；登记归档 sha256 100461c4…、零孤儿 |
| 发现① | MT5 SDK 的 initialize 会把重启的终端收养为 worker 子进程（PPID 竞态）——处置=外部重启使 PPID 悬空 |
| 四角色重启 | 计划任务宿主+start.ps1（NoNewWindow，RepoRoot/脚本路径钉定）→ **conhost=0 全部**；inspect `refused:false alive×4` |
| supervisor | 标准 §6.3 任务安装；**演练 A 首跑暴露真根因**：supervisor 重启用 CREATE_NO_WINDOW 产生 conhost 子进程→自证失败；codex 裁定 B=树外 host.py 包装器（creationflags→0+隐藏控制台继承）→ live_supervisor2 注册 |
| 演练 A | PASS：杀 risk→41s 外拉起→连续健康→**halt 自动解除** ✓ |
| 演练 B | PASS：杀 902610→41s 拉起、902501 无恙 ✓ |
| pause 902610 | 原件逐字节校验后释放归档；reconcile 置清除态；trade_allowed=True 下不再 10027 |

**遗留（交下轮审核）**：①supervisor 重启链 CREATE_NO_WINDOW 缺陷属部署树代码（冻结中）——树外 host.py 为运维级补丁，建议 T197 类正式修复；②11-22 休市窗需按执行单做干净部署+新 F-8 记录（四角色 PID/配置 hash/T180 三副本）；③烧机期 11-19→11-22 24:00Z。

## 2026-11-20（周五）· 第二十二轮 §2.6 记录补齐（更正，不改原文）

- **更正 1（星期）**：11-19 是**周四**——本 log 11-19 节标题"2026-11-19（周五）"记错星期
  （11-20 才是周五）。日检/汇报引用该节时按周四理解。
- **更正 2（演练 A 根因）**：11-17 演练 A 中 supervisor 拒绝重启 risk 的根因是 **O22-4
  （supervisor 重启子进程用 CREATE_NO_WINDOW，每个子进程附带 conhost.exe，verified-tree
  身份校验拒绝未知后代）**，不是当时记录的"参数名不匹配（supervisor 传 --risk-config
  而注册进程用 --config）"。codex 11-19 执行单复核：固定树的 supervisor `command` 与
  `demo_run.ps1` 给 risk 的本就都是 `--config`、给 runner 的都是 `--risk-config`，
  11-19 本机复核四条登记 argv 全部匹配；树外 host.py 包装器（裁定 B）绕开 conhost 后
  演练 A 即通过——证实真根因。11-17 节"supervisor 参数修复：待后续修"的说法作废，
  由 T197 类正式修复取代。

## 2026-11-20（周五）· 第二十二轮 §2.5 网络与终端排查【草稿——未提交，待审核后入库】

方法：只读排查为主。例外两处（均按计划、可逆）：common.ini 两个开关改动（已备份）；
启用任务计划器历史日志。背景：本机为 KVM 虚机（VirtIO 网卡 + TAP VPN 适配器），
时区 UTC+8；终端为专用 Demo 终端（build 6230）。敏感项（服务器名/账号/IP）本节脱敏。

### 1. 四个时刻的事件日志排查（§2.5-1）

**总结论：四个时刻系统层均无事件（无重启/更新/网卡复位记录）；11-15 两个时刻与
11-17 20:48Z 为出网路径抖动/中断所致的券商连接丢失，11-17 10:44Z 与网络无关（见
第 5 节）。任务计划器历史日志（TaskScheduler/Operational）一直未启用、历史不可查
——已于 11-20 启用（可逆），此后可追溯。**

| 时刻（UTC） | 系统/应用日志 | 终端日志（本地时间−8h） | 判定 |
| --- | --- | --- | --- |
| 11-15 05:03 | 无系统事件；04:45:50Z DNS 解析超时（System 日志 DNS-Client 1014，第三方域名） | 05:03:46Z connection lost → 05:29:09Z 重连（ping 400.31 ms），随后 40 分钟内接入点反复切换 | 出网抖动致券商断线，重连间隔 ≈25 分钟 |
| 11-15 07:11 | 无任何事件 | 07:11:21Z connection lost → 07:37:15Z 重连（ping 461.09 ms），07:43–07:59Z 又多次秒级闪断换点 | 同一抖动时段延续 |
| 11-17 10:44 | 无系统事件；10:42:29Z DNS 超时（zcode.z.ai） | 终端**未断线**（06:36Z 重连后至 12:36Z 才换接入点）；10:42:24Z disabled→enabled 快速翻转、10:43:27Z "automated trading is disabled" | 与网络无关；algo trading 被关+进程死亡见第 5 节 |
| 11-17 20:48 | 无系统事件；20:42:47Z DNS 超时 | 20:29Z/20:43Z 两次秒级闪断 → **20:48:28Z 断线、23:26:28Z 才重连（ping 399.32 ms）**，其间 23:05Z/23:26Z 仍见 DNS 超时 | 出网路径长时间中断 ≈9480 秒，与 11-19 日检勘误（20:48→23:26）一致 |

补充：DNS-Client 1014 自 11-13 起每天数条、11-17 晚间密集——本机出网 DNS 路径长期
不稳，是 §7b 断线类环境事件的底层背景。已排除：整机重启（LastBoot=11-08 不变）、
Windows 更新（无事件，且已暂停至 12-05）。

### 2. 网卡省电（§2.5-2）

两块在用网卡（Red Hat VirtIO + TAP）的"允许计算机关闭此设备以节约电源"均为
**Unsupported**——KVM 虚机网卡无该能力，无需也无法改动（前后状态一致）。睡眠/
休眠系统均不可用（虚机无 S1–S3/hibernate）。宿主机层事件对客户机不可见：11-15/
11-17 的出网抖动不能排除宿主层原因——若同类环境原因再次导致窗口失败，§7b 的
云主机/VPS 评估应把宿主层列入。

### 3. 终端接入点与 ping（§2.5-3）

- 当前接入：服务器 Exness-MT5<脱敏>（Demo），Access Point #5；最近一次授权
  11-19 18:45:47Z，ping **324.25 ms**。
- 近一周 ping 区间 ≈315–460 ms、链路质量 26–51%——整体偏高；终端每小时自动扫描，
  常在接入点间切换（切换瞬间秒级 lost→重连，属 §7b 预期内）。
- 11-19 18:45Z 至本次检查（11-20 03:3xZ）仅一次秒级断线自愈，烧机未受影响。
- 计划中"§3 部署前维护窗内重新扫描服务器并记录前后 ping"留给 11-22 窗口执行。

### 4. common.ini [Experts]（§2.5-4）

- 检查时（11-20 03:24Z）：`Enabled=1`（算法交易开，11-19 修复后保持）✓；`Api=0` ✓。
- 改动（按计划、已备份、可逆）：`Account=1→0`、`Profile=1→0`——关闭"账户变更时
  禁用算法交易""配置文件变更时禁用算法交易"两项。前/后 sha256
  `e39c9662…a8d1`/`9f79bc43…b365`；备份与说明在
  `data/ops-evidence/commonini-account-profile-20261120T032431Z/`（不入库）。
- ⚠ **11-22 窗口必办**：改动时终端在运行，MT5 退出时可能以内存设置回写 common.ini
  覆盖本改动——终端停止做干净重部署时须复核这两项，被回写就在停机态重改（同理
  复核 Enabled=1）。

### 5. 11-17 10:44Z 四进程死因（§2.5-5）

**结论（高置信）：ZCode 桌面端整体退出触发的运行时清理（10:44:25.24–26.07Z）连带
杀死业务进程树——与 11-14 00:42Z 三进程死亡同一关闭指纹。触发 ZCode 退出的直接
动作未查明。**

证据链（均为 UTC）：
- ZCode 运行时日志：10:44:25.240Z 两会话 state=stopping → 10:44:25.242Z 取消后台
  exec 任务（reason=session_closed）→ 10:44:25.253Z 起 shutdown.completed 多条
  （含 sessions.shutdown.failed、mcp_pool.shutdown.failed）→ 10:44:25.9Z unloaded。
- 四进程死亡窗口与之逐秒重合：runner-902501 最后日志 10:44:24.677Z、runner-902610
  最后日志 10:44:26.149Z、reconcile 最后 10:44:07Z（30 秒周期、死于下一拍前）；
  四份 .err 全部 0 字节（无 Python 异常路径=外部硬杀）。
- 11-14 00:42:04.5Z 事件为同一指纹（stopping→cleanup_cancel→shutdown.failed→
  unloaded），当时三进程死亡已归因"ZCode runtime 崩溃/退出时子进程树被杀"。
- 前置 67 秒：RDP 会话断开重连（10:43:13Z 断开 / 10:43:18Z 自另一公网源重连——
  用户侧换网）；10:43:27Z 终端记 "automated trading is disabled"，此后持续关闭
  ——即 12:30/12:45/13:00Z 三个 M15 10027 拒单三连、11-19 恢复时发现 Enabled=0
  的直接原因。algo 开关翻转与进程群死同源于该分钟内的人为/会话活动，但操作者与
  具体动作（关闭 ZCode？误触 Ctrl+E？）无日志可证。

已排除：整机重启；Windows 更新；券商断线（终端当时在线）；计划任务结束进程（历史
日志未启用无法实证，但当时 supervisor 任务自 06:30Z 起已停用、无针对业务进程的
任务）；Python 异常退出（.err 全空）；WMI 主动杀进程疑点（nettcpip 查询为每 5 分钟
周期性监控，非触发点）；资源耗尽（窗口内无 Resource-Exhaustion 事件）。
未查明：ZCode 退出的直接触发；algo trading 10:43:27Z 被关的操作来源。
待核（交下轮审核）：11-17 06:07 批次部署记录称四个启动器 PPID=1276（计划任务宿主）
——若属实，ZCode 关闭清理如何波及该树需要解释（或该 PPID 记录有误）；不影响上述
时间证据归因。

### 遗留给 11-22 窗口清单（本次新增）

1. 终端停止后复核 common.ini：Enabled=1、Account=0、Profile=0（被回写则在停机态重改）；
2. 维护窗内"重新扫描服务器"，记录前后 ping；
3. 任务计划器历史已启用——11-22 起的计划任务事件可查（含 supervisor 每分钟 tick）。

## 2026-11-23（周一）· 第二十三轮 §1：探针、观察运行补记、supervisor 任务、证据保全、11-21 取证（主会话）

写入时刻 2026-11-23 17:00 UTC 前后（系统时钟）。依据：第二十三轮施工计划 §1、D109、D110、O23-6、O23-13。
**11-23 10:51 UTC 起的运行是观察运行，不计烧机、不计窗口；窗口 0/10，11-24 不是第 1 天。**
时间一律 UTC。路径用占位符：`<工作区>`＝主仓工作区；`<部署树>`＝主仓同级的 `demo-deploy-cd6fc4a`；
`<用户目录>`＝用户主目录。证据目录 `data/ops-evidence/…` 不入库，本机留 sha256 清单。

### 1. 第二十三轮探针在本机 Windows 上的输出（第 0 步）

main 快进到 `64b8f56`（CI 45210397492 success；`4609be1`、`ded9407` 的 CI 也是 success）。
在仓库根执行 `python -I docs/reviews/probes/2026-11-23-round23-probes.py`，原样输出：

```text
[P23-1] REPRODUCED: 健康树校验通过（2 个进程）；启动器退出、PID 被两天后启动的无关进程占用 → 返回=None，报错='process script/arguments mismatch'（期望：视为 process_missing，返回 []，由 supervisor 重启）
[P23-2] REPRODUCED: 终端进程退出后的下一次网关调用里 initialize(path) 被调 1 次（调用成功）——真实 MetaTrader5 包这一调用会拉起 terminal64.exe 作为调用进程的子进程；verified_tree 遇到 worker 下的 terminal64.exe 报错='process script/arguments mismatch'
[P23-3a] NOT-REPRODUCED: 启动器下挂系统 conhost 叶子 → 返回 pid=[101, 100]，报错=None（NOT-REPRODUCED = O23-1 对这一形态有效；conhost 不进终止清单）
[P23-3b] REPRODUCED: 另有一个早一天启动的无关进程，其 parent_pid 等于 conhost 的 PID → 报错='process script/arguments mismatch'（期望：按创建时间排除陈旧父链接，conhost 仍算叶子）
[P23-3c] REPRODUCED: 启动器已退出、只剩它的 conhost → 返回=None，报错='missing launcher with surviving/duplicate worker'（期望：不把系统 conhost 当成存活 worker）
[P23-4] REPRODUCED: 四个角色全部健康，仅 verify_deployment 的 git 调用超时 → refused=True，reason="TimeoutExpired: Command '['git', '-C', '<deploy>', 'status']' timed out after 15 seconds"，halt.reason='supervisor_refused'，告警含 SUPERVISOR_REFUSED=True；正式检查把 SUPERVISOR_REFUSED 归异常类=True（窗口内一条即判失败）
[P23-5] REPRODUCED: 生产启动路径：supervisor 用 CREATE_NO_WINDOW=True、demo_run.ps1 用 -WindowStyle Hidden=True；supervisor 测试经过 launch()=False（测试自己用 subprocess.Popen 起进程 4 处）；tests/ 含 conhost 的文件=[]；70cd7eb..ded9407 改动文件=['scripts/live_supervisor.py']
[P23-6] REPRODUCED: 开仓成交后 SL/TP 修正被拒（10016）→ 回报 action='rejected'（异常=None），券商侧该 magic 持仓 1 笔，journal 状态序列=['pending', 'rejected']，告警含 ORDER_REJECTED=True，deals 证据流 0 行（期望：成交如实入 journal 与 deals，SL/TP 修正失败单独告警）
[P23-7] INFO: tests.yml 含静态检查步骤=False；pyproject.toml 含 ruff/mypy 配置=False

汇总：
  P23-1: REPRODUCED
  P23-2: REPRODUCED
  P23-3a: NOT-REPRODUCED
  P23-3b: REPRODUCED
  P23-3c: REPRODUCED
  P23-4: REPRODUCED
  P23-5: REPRODUCED
  P23-6: REPRODUCED
  P23-7: INFO
```

`python -I docs/reviews/probes/2026-11-20-round22-probes.py`，原样输出：

```text
[P22-1] NOT-REPRODUCED: 窗口第 2 天一条运行器断线告警 BROKER_UNAVAILABLE → 返回 3，判'窗口内异常'=False（基线证据本身返回 3）
[P22-2c] INFO: 对照：周六 rc=78、无 supervisor 重启 → 返回 1（T210 前后都应为 1）
[P22-2] NOT-REPRODUCED: 周六 rc=78 退出告警且 2 分钟后 supervisor 已重启 → 返回 3（判异常=False）；周六两段已恢复断线共 3077 秒 → 返回 3（判降级超限=False）
[P22-3] NOT-REPRODUCED: 仓库卫生扫描违规 0 处，涉及 []
[P22-4] NOT-REPRODUCED: launch() 使用 CREATE_NO_WINDOW=True；无 conhost 时校验通过（2 个进程），加一个 conhost.exe 子进程后报错=None（11-17 演练 A 报错原文相同）

汇总：
  P22-1: NOT-REPRODUCED
  P22-2c: INFO
  P22-2: NOT-REPRODUCED
  P22-3: NOT-REPRODUCED
  P22-4: NOT-REPRODUCED
```

读法：P22-4 在 Windows 上不复现，是因为 `ded9407` 已在 main 上，而探针里的 conhost 路径在 Windows 上
能解析到系统目录（审核在 Linux 上仍复现，属平台差异）。这只说明 O23-1 对"启动器下挂系统 conhost
叶子"这一种形态有效（P23-3a），另外两种 conhost 形态仍然复现（P23-3b、P23-3c）。结论与审核一致：
`4609be1`、`ded9407` 不作为窗口候选，由 T211 重做。当时 codex 对 `4609be1` 的否决记录见
`docs/reviews/2026-11-23-o23-1-codex-review.md`（`ded9407` 没有再送复审）。

### 2. 观察运行补记：11-23 10:51 UTC 的部署（观察运行，不计烧机、不计窗口）

主会话在 11-23 做了一次紧急重启，当时没有留记录。以下全部是事后只读取证（11-23 15:53–16:45 UTC），
**没有为了补记录去连接交易终端**。

- **代码**：`cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5`，恰好一个。`<部署树>` 是 detached（`git symbolic-ref`
  返回非零），`git status --porcelain --untracked-files=all` 零行（只被忽略的运行产物）。按 D110，它不作为窗口候选。
- **启动方式**：一次性任务 `\emergency-restart-1123`，11-23 10:51:00.395 UTC 注册（任务计划器事件 106/140）
  → `<工作区>` 根目录的 `deploy-consolidated.ps1` → `<部署树>\scripts\demo_run.ps1 -Mode Demo`，参数显式写全。
  部署发生在盘中（周一 10:51 UTC），不在休市维护窗内；此前从 11-21 12:51:47 UTC 起四个进程为零，约 46 小时。
- **配置**（三份 sha256 与 11-17 的部署记录逐字一致）：
  - `DEMO-MA-XAUUSD-M15.toml`（magic 902610，固定 0.01 手）：`d983a2420a1dd5d95b9651a9d7250860a2f4226c48f9a524deeac87d552a497b`
  - `BUY-HOLD-XAUUSD-H1.toml`（magic 902501，固定 0.01 手）：`e0498f28a49ccd75d93a3af0a56112143f37111431fa947e82929419cce3f06f`
  - `<部署树>\config\risk.toml`：`e8db707b91177f4695bce888e011583892019a622011bb754a450ff75fd3e05a`，
    `daily_loss_cap_usd = 500.0`；风控进程命令行里的 `--config` 实测就是这份 `risk.toml`（不是 `risk-demo.toml`）。
  - `sessions.toml`：`762782591ed839d26b1dd35c76bcdb1ae9726cf2b57a62901b8d04e56ad59fa4`。
- **进程**（11-23 16:01:20 UTC 的进程表；"启动器"是 venv 的 `python.exe`，"解释器"是它拉起的真正工作进程）：

  | 角色 | 启动器 PID / 创建时刻 | 解释器 PID | conhost 子进程 | 启动器的父进程 |
  | --- | --- | --- | --- | --- |
  | runner-902610 | 17336 / 10:51:13 | 11752 | 20632 | PID 5428（任务宿主的 shell，已退出） |
  | runner-902501 | 13408 / 10:51:13 | 19204 | 4712 | 同上 |
  | risk | 18212 / 10:51:21 | 10992 | 5736 | 同上 |
  | reconcile | 11568 / 13:18:20（supervisor 重启） | 16968 | 快照里没有以它为父的 conhost | PID 20508（supervisor 宿主，已退出） |

  四棵树都是"启动器 → 解释器 + 一个系统 conhost 叶子"；16:01 的快照里没有任何 worker 下面挂着 terminal64.exe。
  agent 会话不是这些进程的祖先（符合 D98）；但解释器、数据目录、包装脚本都在 agent 工作区内（不符合 D112，已知）。
- **登记与清单**：`run-manifest.json` 追加了一条 `start`（10:51:23 UTC）；`evidence/run-20/` 三个文件写于 10:51:23–24；
  `instances.json` 由启动器重写。**旧 `data/live` 没有归档，原地沿用**（同模式、同部署形态、同数据目录，按 runbook §6.2
  属标准重启）：各 magic 的 journal、orders、deals、expected 文件，`alerts.log` 全文，`ops-restarts.jsonl`，
  `evidence/instances/`、`evidence/run-17`…`run-20`，以及 11-17 留下的 `exit-*.json`、`disconnect-exit-*.json` 旧标记都在原处。
- **halt 和 pause 是怎么清掉的**：主会话在启动前（约 10:50 UTC）手工删除了 `halt.json` 和 pause 文件，
  **删除前没有留存、也没有记录内容**（风控日志 10:51:23 UTC 显示 `halt=否`）。这是主会话的疏漏。
  当时在场的控制文件只能从告警反推：`halt.json`（supervisor 所有）、`halt-risk-veto.json`（见 §3）、902610 的 pause
  （11-21 12:46:46 UTC 的 `HEARTBEAT_TIMEOUT`）。11-23 启动前的登记文件是否也被删除，现在已无法确认；
  它当时的内容与 `evidence/instances/20261121-124635-8-after.json` 相同（12:46:46 UTC 之后登记没有再变），已保全。
- **902501 那笔多单是怎么接的**：靠 journal 原地接续。运行器 10:51:15 UTC 的第一条决策就是
  `signal=1 target=1 volume=0.01` → `执行 hold：net 0.01 → 0.01，0 笔请求`；`expected-902501.json` 仍是 11-19 15:00:07 UTC 那份；
  账户上这笔多单（ticket 2916…313，0.01 手）自 11-21 12:51 到 11-23 10:51 UTC 约 46 小时没有任何进程管理，BuyHold 无止损。
  没有发平仓单、没有发新单。11-23 对账每 30 秒"对账一致"。
- **902610 在 11-23 的成交**（都是策略自己的单，journal 回码 10009）：11:15:05 UTC 开多 0.01 手；12:30:05 UTC 反手卖出 0.01 手；
  13:25:04 UTC 止损成交平仓（deal 2353…846，对账记为 `external_exit` 并已消费）。此后空仓。
- **终端**：生产网关的 `gateway.json` 是 `{}`，终端路径取默认值（`Program Files` 下的正式安装）。现在的生产终端 PID 15760，
  创建于 11-22 00:21:09 UTC，父进程是 explorer（从桌面手动打开），不是任何 worker 的子进程。
  `trade_allowed`、`tradeapi_disabled` 没有读（D82、D104）；11:15 和 12:30 UTC 两笔单子都成交，说明当时允许算法交易。
  机器上还跑着第二个 MT5 终端（盘根 `MT5_B1` 目录，PID 15348，11-21 13:20:33 UTC 启动，见 §5），生产网关不引用它。
- **supervisor**：`live_supervisor2`，树外 `host.py` 包装（生产文件 sha256 `9260842ecd348729933d55d29bac4bcf291676660f431ec87637ade324e55425`，
  `sup-wrapper.ps1` `b7445890548bf36a26f848e8e2c2ff4235256e46489182393c8e1f259e2fcb99`），见 §4。

### 3. 观察运行在 11-23 13:18–16:42 UTC 处于 halt（新发现，已处置）

- **事件**：13:18:18.0019 UTC supervisor 判 `unhealthy={'reconcile': 'heartbeat_or_log_stale'}`，写 `halt.json`
  （`component_lost`，owner=supervisor）；13:18:20 UTC 重启对账（15924 → 11568，`ops-restarts.jsonl` 第 9 行，
  `evidence/instances/20261123-131818-9-before/after.json` 和 `index.jsonl` 齐全，没有 `SUPERVISOR_EVIDENCE`）；
  13:19:17 UTC 起 `healthy_since` 连续。旧对账日志最后一行是 13:18:17 UTC"对账一致（2 个持仓）"，节拍 30 秒，`.err` 为空。
- **后果**：supervisor 重启成功后要连续健康 5 分钟才解除自己写的 halt，但 `_clear_owned()` 开头规定
  **只要 `halt-risk-veto.json` 存在就不解除**。这个否决文件是 11-21 12:46:46 UTC 风控写的（`heartbeat_timeout`），
  11-23 紧急重启时被漏清。于是 halt 持续 3 小时 24 分：902610 每根 M15 的决策都被风控以 `halted: component_lost`
  拒绝（13:30–16:30 UTC 共 13 条 `RISK_REJECT`）；902501 持仓不动，不受影响。11-23 13:16 UTC 的日检在这之前 2 分钟，
  没有看到。
- **对账为什么被判陈旧**：未查明。已排除：超时（上限是 120 秒，对账 30 秒写一次）；时钟回拨（系统日志 11-23 没有
  "系统时间已更改"事件）；崩溃（`.err` 为空，没有退出标记）；日志停写（13:18:17 UTC 刚写过）。
  时间上的巧合：supervisor 每分钟在 `:18` 秒检查，对账每 30 秒在 `:17`/`:47` 秒写心跳和日志，两者撞在同一秒。
  心跳文件读写相撞是可能的原因，但告警不记录是哪一条子条件失败，无法证实。
- **处置**（D109：先保全现场再处置）：16:01:20 UTC 进程表、16:14 UTC 的 `data/live` 全量快照已存
  `data/ops-evidence/observe-20261123/`；16:41:44 UTC 把 `halt-risk-veto.json`（sha256 `ad0bf2f8…`）
  **移到**证据目录（没有删除）；16:42:18 UTC supervisor 自动解除 halt（`halt.json` 已不存在，没有 killSwitch）。
  没有重启任何进程，没有改代码和配置。16:45:05 UTC 起 902610 恢复决策并开多 0.01 手（回码 10009）。

### 4. supervisor 任务：只留一个（已核对，无需停用）

`\MT5Quant\` 下 8 个任务（11-23 16:09 UTC，只读导出，存证据目录）：

| 任务 | 状态 | 最近一次运行 / 结果 | 说明 |
| --- | --- | --- | --- |
| `live_supervisor2` | **Running（唯一启用）** | 每分钟；`ExecTimeLimit` PT2M；`StartWhenAvailable` | 动作 `wscript //B //Nologo sup-launcher.vbs` → `sup-wrapper.ps1` → `host.py`，指向 `<部署树>` 和 SHA `cd6fc4a…`，与 §2 的部署一致。登录类型 Interactive，RunLevel Highest |
| `live_supervisor` | Disabled（11-19 起） | 11-19 15:16:16 UTC，rc=2 | 树内直连版，指向同一部署树和 SHA |
| `live_daily_report` | **Disabled（11-21 12:52:23 UTC 起）** | 11-20 21:57:57 UTC，rc=1 | 原因见 §5 第 8 项 |
| `demo_weekly_check` | Disabled（11-21 12:52:21 UTC 起） | 11-15 23:00 UTC，rc=1 | 同上 |
| `refresh_bars`、`refresh_ticks`、`snapshot_specs` | Disabled | 11-07 | 数据任务，停用到正式检查结束（D82） |
| `watch_swap` | Disabled | 10-27 | 旧任务 |

没有两个 supervisor 任务同时启用的情况。`live_supervisor2` 指向的就是当前部署，不停用。
11-23 的重登记记录：10:57:40 UTC 注册、11:00:29 UTC 禁用、11:07:08 UTC 再注册（主会话调试 `host.py` 期间）。
一次性的 `\emergency-restart-1123`、`\deploy-consolidated-20261117` 两个任务仍留在任务库里（后者已禁用），待新候选部署时一并撤除。
观察：拒绝轮次里任务结果码是 0x80070002（2147942402），而脚本设计是返回 2；来源未查明。

### 5. 证据保全（不入库）

`data/ops-evidence/collapse-20261121/`：138 个文件、177.2 MB，清单 `SHA256SUMS-20261123T164221Z.txt`。
`data/ops-evidence/observe-20261123/`：11-23 的进程表、任务清单、移走的否决文件，各带清单。内容：

- `live-snapshot-20261123T161427Z/live/`：`data/live` 全量（两个单实例锁文件被进程锁住，复制不了，属正常）；
- `demo-logs/`：`data/demo` 下 11-19 14:58 UTC 之后的全部日志和 `.err`；
- `supervisor-host/`：生产在用的 `host.py`、`sup-wrapper.ps1`、`sup-launcher.vbs`、`start.ps1`、`start-wrapper.ps1`、`common.ini.bak`；
- `agent-logs/`：agent 桌面端 11-20 至 11-23 的运行日志；
- `terminal-logs/`：两个终端 11-17 至 11-23 的日志和 `origin.txt`；
- `event-logs/`：任务计划器操作日志全量、System 与 Application（11-21 00:00–14:00 UTC、11-23 12:00–17:00 UTC）、
  远程桌面会话日志（11-20 20:00 至 11-22 02:00 UTC）、PowerShell 操作日志（11-21 12:00–13:30 UTC）、8 个任务的 XML、本项目任务的事件时间线。

**没有保住或从来不存在的**：

1. 任务计划器历史只保留到 11-21 05:28:01 UTC（通道上限 10 MB，循环覆盖）；01:24 前后那一段已丢。
2. 11-21 12:46–12:52 UTC 的进程快照不存在；进程审计（Security 4688/4689）没有开启。
3. 11-23 重启前 `data/live` 里的 halt、pause 原件和当时的登记（主会话删除时没有留存，见 §2）。
4. Herdr 远程 shell 在 11-21 的命令记录没有留存（只剩文件传输目录的时间戳）。
5. PowerShell 脚本块日志在 11-21 12:00–13:30 UTC 是 0 条。

### 6. 11-21 取证结论（D109，对照审核 O23-6 的候选表）

**第 1 项：01:24:34 第一次拒绝时四个登记的启动器是否都活着；此前有没有退出标记。**
都活着。两个运行器 01:00–01:59 UTC 每 2 秒一条日志（各约 1,700 条），风控、对账日志连续；
退出标记只有 11-17 21:03 UTC 的旧文件；11-21 在 01:24:34 之前没有任何告警。
12:46:35 UTC 的登记副本（`20261121-124635-5-before.json`）里四个 PID 仍是 11-19 那批
（902501=6984、902610=15348、risk=14208、reconcile=3136），11-19 21:51:35 到 11-21 12:46:35 UTC 之间 supervisor 没有重启过任何角色。

**第 2 项：被拒绝的是哪一棵树。** 未查明：告警只有一句 `process script/arguments mismatch`，没有角色、PID、映像，也没有进程快照。

**第 3 项：终端当时的 PID、父进程、创建时间；日志里 01:1x–01:2x 有没有启动记录。**
PID 和父进程没有记录。**有启动记录**（生产终端日志，原文件是本地时间，这里换算成 UTC）：
01:24:20.672 一个实例记 `terminal process already started` 后退出；01:24:20.943 `MetaTrader 5 x64 build 6274 started`；
01:24:26 授权；01:24:28 同步完成、trading has been enabled。
在此之前**没有旧实例的 `exit with code 0` / `disconnected`**——原来在跑的终端不是正常退出的（它上一条日志是 11-20 19:15 UTC）。
**触发源是直接证据**（agent 桌面端主进程日志）：01:24:17.250 `[app-quit] explicit quit requested (tray-quit)` →
01:24:17.4–18.4 各工作区的运行时宿主被逐个清理（含本项目工作区）→ 01:24:19.031 应用退出 →
01:24:26.9 新的应用进程启动（版本 3.18.2，自动更新检查"已是最新"）。即：**有人从系统托盘菜单显式退出了 ZCode 桌面端，8 秒后又打开**；
不是崩溃，也不是自动更新。同一账户的远程桌面会话在 01:09:08、01:20:48 UTC 两次重连。
**之后的四次终端重启也是同一形态**（UTC）：旧实例正常退出 → 2–5 秒后出现"另一个实例已在运行"的短命实例 → 几百毫秒内
真正启动。11:37:28.7 退出、11:37:33.9/34.4 启动；12:36:43.1 退出、12:36:44.7/45.2 启动；12:43:48.4 退出、12:43:50.8/50.9 启动；
12:44:37.5 退出、12:44:40.7 和 12:44:55.7 两次启动；12:46:40.3 再次启动（前一个实例没有正常退出记录）。
01:24 和 12:46 两次可以确定是 worker 拉起的：前者在 ZCode 应用进程退出（01:24:19.0）之后 1.6 秒，
后者在 supervisor 拉起第一批 worker（12:46:37）之后约 3 秒，人都来不及操作。其余几次形态相同，但不能排除人工双击。无论哪一种，**supervisor 的拒绝在每一次终端重启之后都照旧**：
如果新终端是人手动开的，它不在 worker 的进程树里，拒绝应当停止。这与"终端整天挂在 worker 的进程树下"一致。

**第 4 项：12:46 之前四个进程在什么时刻、以什么方式消失。**
最后一行日志：runner-902610 12:44:35、reconcile 12:45:30、risk 12:45:50、runner-902501 12:45:53（UTC）；
12:46:32.349 UTC supervisor 启动，判四个角色 `process_missing`。四份 `.err` 全是 0 字节，没有退出标记，
应用日志没有 python 崩溃记录，系统日志 12:20–13:00 UTC 没有事件，机器上次开机是 11-08 04:59:51 UTC
→ **是被外部结束的**，前后错开约 80 秒（不是一次批量结束）。
现场背景（都是 Administrator 账户下的操作）：Herdr 远程 shell（`hc.exe` → `cmd.exe`）10:57:10 UTC 打开；
Herdr 文件传输 10:58、11:50 UTC；盘根 `MT5_B1` 目录 11:50:11 UTC 创建（第二个 MT5 安装）；
生产终端被重启多次（UTC）：11:37:26–41、12:36:42–51、12:43:47–12:44:00、12:44:36–58（前一个正常退出，随后起了两个实例）、
12:46:40（前一个实例没有正常退出记录）。runner-902610 的最后一行（12:44:35）比终端那次正常退出（12:44:36.966）早 1.6 秒。

**第 5 项：agent 桌面端日志在 12:4x 有没有关闭指纹。** 没有。11-21 12:3x–12:5x UTC 桌面端一直在运行
（唯一的生命周期事件是 12:38:07 UTC 一个会话卸载）。当时活跃的是另一个项目工作区的会话（一个主会话带三个子代理，
12:40–12:55 UTC 约 460 条工具事件），命令内容不在这两类日志里。→ 12:46 和 12:51 的两次消失**不是** ZCode 退出造成的。

**第 6 项：12:46:37–45 UTC 重启的四个进程的退出码和最后日志；是不是启动时连接失败。**
不是。四个都正常起来并连上：运行器 12:46:46 UTC 取到报价（休市，跳过）；风控 12:46:46 UTC `心跳=timeout halt=是`，
之后 `心跳=ok`；对账 12:46:47 UTC 起"对账一致（1 个持仓）"。
之后四个进程在**同一秒**停止（12:51:46.6–12:51:47 UTC：runner-902610 12:51:46.686、runner-902501 12:51:46.756、
风控 12:51:46、对账 12:51:47），`.err` 为空，没有退出标记、没有退出码 → 同时被外部结束。

**第 7 项：12:51 之后 `SUPERVISOR_REFUSED` 为什么停了；supervisor 任务此后还在不在触发。**
因为任务被禁用。任务计划器事件 142（"任务已禁用"，用户 Administrator），UTC：
`\deploy-consolidated-20261117` 12:51:42.597 → 四个进程 12:51:46–47 被结束 →
`\MT5Quant\live_supervisor2` 12:52:19.534 → `\MT5Quant\demo_weekly_check` 12:52:21.393 → `\MT5Quant\live_daily_report` 12:52:23.214。
supervisor 的每分钟触发（事件 100）从保留的最早时刻 05:28:32 UTC 到 12:51:32 UTC 每分钟一次，之后到 11-23 10:57:45 UTC 一次都没有。

**第 8 项：日报任务 rc=1 的 stderr 原文。** `<部署树>\data\logs\task_live_daily_report.log` 里是四行
`The system cannot find the path specified.`（最后写入 11-20 21:57 UTC）。
原因：任务命令 `task_live_daily_report.cmd` 调用的是 `<部署树>\.venv\Scripts\python.exe`，而 `<部署树>` 是纯检出、没有 `.venv`
（虚拟环境在 `<工作区>`）。**Python 根本没有启动**，不是连接失败或取数失败。`task_demo_weekly_check.cmd` 同样的毛病。
现存日报到 `daily-2026-11-15.md`。11-21、11-22 日检报的"rc=1 第二天、第三天"读到的是 11-20 那次的旧结果——任务已被禁用，并没有运行。

**对照 O23-6 候选表**：

| 候选 | 结论 | 依据 |
| --- | --- | --- |
| 终端被收养（O23-3） | **01:24 一段：采纳（证据链完整，缺进程快照）** | 托盘显式退出 ZCode → 终端无正常退出记录即消失 → 270 毫秒内两次启动尝试 → 重启后 6 秒出现第一条拒绝 → 此后每次终端重启后拒绝照旧，每分钟一条，共 684 条，直到进程被结束、任务被禁用 |
| PID 复用（O23-2） | 这两段里没有证据支持 | 四个登记的 PID 直到 12:46:35 UTC 都对应活着的进程 |
| conhost | 排除 | 11-20 日检四棵树 conhost 全 0；01:24 之前没有重启；`host.py` 把启动标志改成了 0 |
| 其他子进程 | 没有发现 | — |
| 第三种机理（不在表里） | **12:44–12:52 一段：有顺序的人工停机** | 结束进程（错开 80 秒）→ supervisor 重启 → 再次同时结束 → 禁用四个任务；其间还有终端多次重启和第二个终端的安装 |

**未查明**：01:24 被重启的终端的父进程；被拒绝的是哪一棵树；从托盘退出 ZCode、以及 10:57–13:20 UTC 通过 Herdr 和交互会话
操作机器、结束进程的具体的人或程序；对账在 11-23 13:18:18 UTC 被判陈旧的子条件；supervisor 任务结果码 0x80070002 的来源；
11-21 05:28 UTC 之前的任务计划器历史。

**已排除**：整机重启（上次开机 11-08 04:59:51 UTC）；Windows 更新或系统事件（System 日志当天 21 条，12:20–13:00 UTC 为空）；
python 自身崩溃（应用日志无记录，`.err` 全空）；时钟回拨（11-21 只有 12:00:00.779 UTC 一次 −8 毫秒的校正，11-23 没有）；
ZCode 退出（12:4x 那一段）。

### 7. 磁盘（11-23 15:53–16:46 UTC）

- 余量：11-22 13:16 UTC 25.2 GB → 11-23 13:16 UTC 13.2 GB → 15:53 UTC 11.85 GB → 16:40:58 UTC 10.47 GB →
  **16:45:01 UTC 9.09 GB（已低于 10 GB）**。每 5 分钟采样一次，继续中。
- **本项目不是占用方**：工作区共 4.82 GB（研究数据 `ticks` 2.57 GB、`bars` 0.74 GB，证据目录不到 0.2 GB）；
  近 30 小时本项目新建文件不到 10 MB。本项目能清的只有两个旧 worktree（26 MB、9 MB，按 U8 要用户确认后才删）和缓存，合计不足 40 MB。
- 占用在项目之外：agent 平台自己的数据 35.2 GB（其中别的项目工作区 22.8 GB、本工作区 4.8 GB、预置工具 2 GB、
  agent 数据库 2.3 GB）；安卓模拟器 `ak_test` 8.5 GB（用户数据镜像 5.87 GB + 内存快照 2.5 GB，11-23 16:01 UTC 仍在写，正在运行）；
  两个 MT5 终端的数据目录 11.0 GB（生产终端 8.35 GB、第二个终端 2.64 GB）；系统页面文件 `pagefile.sys` 18.7 GB
  （系统自动管理，当前只用 1.5 GB，峰值 17.9 GB；物理内存 32 GB）；移动端与 Rust 工具链约 16 GB；Windows 14.7 GB。
- 近 30 小时的新建量（GB，按小时，UTC）：

  | 11-22 | 14 点 0.4 | 15 点 2.2 | 16 点 0.2 | 17 点 0.5 | 23 点 0.3 |
  | --- | --- | --- | --- | --- | --- |
  | 11-23 | 01 点 0.8 | 02 点 0.5 | 04 点 0.3 | 05 点 2.3 | 07 点 0.2 |
  | 11-23 | 09 点 1.0 | 12 点 0.7 | 13 点 0.5 | **14 点 4.5** | **15 点 1.7** |
  | 11-23 | 16 点 2.5（到 16:10） | | | | |

  14 点那 4.5 GB：agent 桌面端自我升级 0.9 GB、它自动装入的预置工具（pandoc、LibreOffice）2.0 GB、盘根 `dev` 下另一个项目的构建 1.4 GB。
  05 点、09 点的 2.2 GB、1.0 GB 是另一个项目工作区的 Rust/Flutter 构建。
- 净变化：27.4 小时减少约 15 GB（平均 0.55 GB/小时），但其间新建约 19 GB、别处同时删掉约 4 GB——
  这块盘被其他项目当作周转空间，余量由它们的清理节奏决定。

### 8. 环境方面的其他发现（只记录，未改动）

- 本机时间服务**没有在同步**：`w32tm` 状态为未同步、来源是本地 CMOS 时钟；11-10 以来每天一次幅度不超过 8 毫秒的校正。
  证据时间戳的可信度依赖这台机器的时钟。
- 生产终端每次启动都加载自定义指标 `SMC_Atelier (XAUUSD,Weekly)`：生产终端的图表也被用于手工看盘。
- 生产终端在 11-21 12:47:44 UTC 到 11-22 00:21:12 UTC 之间没有运行；11-22 起由用户从桌面手动启动。
- `docs/ops/ops-patches/2026-11-19/` 之前留档的是 11-19 的旧版 `host.py`，本次已更新为生产在用的版本并更新 sha256。

### 9. 对施工计划的影响（下一步要做的事）

1. **T211 的失效模式表要补**：(12) 陈旧的 `halt-risk-veto.json` 让 supervisor 的 halt 永不自动解除；
   (13) 对账在同一秒被判陈旧（机理未查明；先把是哪条子条件失败写进告警和 `supervisor-state.json`）。
   第 10 行（worker 下出现 terminal64.exe）已被生产证实。
2. **T213 的根因变了**：是任务模板指向不存在的 `<部署树>\.venv`。先修模板（或把 `-PythonExe` 传进去）并加测试钉住
   "任务命令里的 python 必须存在"，再做计划里的重试、补生成、失败告警。
3. **T215 多了一个数据点**：11-21 01:24 ZCode 被显式退出时，经计划任务载体启动、解释器和目录都在 agent 工作区内的四个生产进程
   **活了下来**，被连带结束的是交易终端；而 11-17 的记录是四个进程死了。对照实验要把"一次性任务由 agent 会话触发"和
   "由调度器自行触发"分开测。
4. **T209 的必要性有了生产证据**：终端消失后 worker 的 `initialize()` 确实把终端拉起（01:24:20 的两次启动尝试）。
5. **T214**：小时级检查要把"halt 存在超过 N 分钟"和磁盘余量作为生产级问题。
6. 这台机器上 D64 无法强制：11-21 的禁用任务、结束进程没有进任何日志，直到今天靠任务计划器历史才还原。
   D112（迁出）和 U11（专用主机）要解决的正是这个。

### 10. 补记（17:05 UTC）：跌破 10 GB 之后的增长来源

- 余量：16:50:01 UTC 10.35 GB → 16:55:01 7.52 GB → 17:00:01 6.76 GB → 17:04:02 6.04 GB。
- 顶层目录两次测量（16:00 与 17:01 UTC）相减：盘根 `Projects` 6.52 → 10.46 GB（+3.94 GB），盘根 `dev` 2.96 → 4.10 GB（+1.14 GB），
  用户目录 +0.21 GB，其余不变；合计与同期余量的减少量吻合。
- `Projects` 下的增长全部来自**另一个仓库** `mt5-trading` 的 `.work` 目录（8.74 GB，其中 `.work\intraday\agent_b2` 7.54 GB、
  `.work\intraday\data` 1.1 GB）：最近 40 分钟新建 8,957 个文件、共 4.26 GB，单个文件都不超过 40 MB
  （`raw` 下的 csv、`data_quality` 下的 json、`daily_p1` 下的 txt），约 6 GB/小时。
  命令行指向该仓库的进程有 powershell、cmd、python 和两个 bash（16:53–17:04 UTC 陆续启动），即有一个 agent 会话正在那里跑任务。
- 安卓模拟器进程 17:01 UTC 已不在；页面文件仍是 18.66 GB。
- 本项目四个进程 17:03 UTC 心跳新鲜，`healthy_since` 16:43:18 UTC（halt 解除时健康窗口按设计重置），没有新告警。
- 这是一个与本项目共用这台机器的**另一个 MT5 项目**。主会话无权、也没有去停止它；已向用户报告，由用户处置。

## 2026-11-23（周一）· 第二十三轮 §2：候选代码的评审、修正与合入（主会话；截至 22:23 UTC）

时刻一律系统时钟 UTC。每项的评审记录在 `docs/reviews/2026-11-23-t21x-review.md`，都是没有参与实现的新上下文子代理写的。分支上的 CI 是手动派发的（`workflow_dispatch`），main 上的 CI 随推送触发。T209 的评审还在进行，这一条随后补记。

### 1. 合入 main 的五项

| 项 | 分支上的提交（按顺序） | 独立评审 | 分支 CI | main 合并提交 | main CI |
| --- | --- | --- | --- | --- | --- |
| T211 supervisor 身份校验 | `506e57c` → `217f0ee` | CONDITIONAL → 合入前置闭合 | #373、#380 | `610a34e` | #381 |
| T214 小时级只读存活检查 | `f239862` → `3ce416e` → `5546cb8` → `a6f4f7f`（并 main）→ `2b59e90` | CONDITIONAL → PASS | #378、#384 | `e11e0a2` | #390 |
| T212 supervisor 的"无法判定" | `9281257` → `b859149`（并 main）→ `e9f3beb` → `9108534` → `2c4ccf0` | CONDITIONAL → PASS → 一行修正 PASS | #379、#385、#388 | `bafe386` | #392 |
| T213 日报与安装脚本 | `f9dd473` → `49c2116` → `1f76057` → `b34c54a` → `1ad6312`（并 main）→ `bb0ef4d` | CONDITIONAL → PASS | #375、#386 | `18041e5` | #393 |
| T218 链路基线 | `d03bc4d` → `24e1a5f` → `a44c8cd` → `4c720c3` → `f88727b` → `6b56c59` | CONDITIONAL ×3 → PASS | #377、#382、#383、#387、#395 | `2392698` | #396（在跑） |
| T209 网关不拉起终端 | `0ed98a7` → `e50892c`（并 main）→ `1902bef` | 进行中 | #389 失败 → #391 通过 | 未合入 | — |

### 2. 反事实验证（D118：改实盘代码的提交都带在父提交上失败的测试）

- T211：新增 23 条，在 `70cd7eb` 上 22 条失败，在父提交 `56ea8dd` 上 18 条失败（其余是接口不存在）。
- T214：19 条新测试在 `f239862` 上全部失败；评审者独立核：最终的 28 条里 23 条在 `f239862` 上失败。
- T212：12 条在 `506e57c` 上全部失败；修正的 10 条在 `b859149` 上 8 条失败；一行修正的那 1 条在 `e9f3beb` 上失败。
- T213：日报 12 条加安装 4 条在 `76fe9db` 上 16 条失败；修正后日报 18 条、安装 6 条在 `f9dd473` 上行为失败；R7 的 3 条在 `1f76057` 上失败。
- T209：guard 文件在 `707e447` 上收集失败（模块和异常类不存在），keeper 的 12 条全部失败。同一份演示脚本在两边的差别：终端进程不在时 `initialize` 的调用次数，网关 1 → 0，日报 1 → 0；风控和对账启动时连不上，由抛 `NotConnectedError` 变成返回未连接的网关。
- T218 是 `scripts/ops/` 下的只读工具，不在 D118 的清单里；每一轮返工都在上一轮的提交上跑过新旧对比（最后一轮：新增的 2 条在 `4c720c3` 上失败）。

### 3. 合并后在 main 上的验证

- 每次合并之后、推送之前，在 main 上跑了该项的测试加卫生和文档测试：T211 142 条；T214 170 条；T212 193 条；T213 507 条里 506 通过（那 1 条见 §5）；T218 137 条。
- 只读探针（`docs/reviews/probes/2026-11-23-round23-probes.py`）在 `bafe386` 上：P23-1、3a、3b、3c、4、5 是 NOT-REPRODUCED；P23-2 仍复现（T209 未合入）；P23-6 仍复现（O23-6）。

### 4. 主会话按 D80 定的口径（代决记录）

- **T212**：一轮里只要出现过一次无法判定就计一次；连续 3 轮升级为拒绝；缓存命中不计数也不清零。缓存期内已有 halt 的自动解除沿用缓存的"干净"结论——只写进 runbook，不改条件。**这是会被带进正式窗口的已知行为，要让用户知情。** git 以非零退出码结束、git 的定位走 PATH 两条不做，留给 T215。
- **T213**：重试门槛按"尝试真正开始的时刻"判，每次尝试（含补生成日的第一次）都判窗；`--paper-state` 豁免。账户首笔读取失败按取数失败重试；配置错误、未预期异常、写盘失败、网关期限都发 `DAILY_REPORT_FAILED`。R7：两个代码页不同时拒绝非 ASCII 路径。**与施工计划的出入**：夏令时换日窗到 22:00 UTC，任务 21:57 启动，"3 次、间隔 5 分钟"做不到；合入的是保守实现（夏令时约 232 天脚本内不重试），作为已接受的限制上报外部审核，不记成"已按计划完成"。
- **T214**：日报按"已到期的最近一份"判（22:30 UTC 之前要求前天的）；通知发不出、状态文件写不了都不改变检查结论；同一个生产级问题第一次发、每 24 小时最多再一条、消失时报恢复；webhook 只发 https（回环可用 http），不跟随重定向。装计划任务之前还要改的 N-1、N-2、N-3、N-5、N-7、N-8、N-9 在分支上（`bdcef43`），复核通过后第二次合入。
- **T218**：窗口判据每一天单独判；临界段、跨零点的尾巴、不给日历、日历没覆盖到、日志里有多个账号的日子一律不计入；事后用副本一次跑 `--from/--to`。
- **T209**：`TerminalNotConnectedError` 保留 `NotConnectedError` 子串（正式检查按它定类）；启动等待只把 `NotConnectedError` 当"终端还没起来"，其余异常照旧是启动失败；终端不允许算法交易时，进入该状态就发一条异常类告警。看护任务用独立的安装脚本，不并进 `setup_scheduled_tasks.ps1`。

### 5. 偏差与如实记录

- **T218 返工四轮**。主会话的第一版有三处阻断（未恢复的中断记成 0、超过 24 小时的恢复读不到、退出码 2 并了几种情况）；第二版又有一条（窗口判据只对最早那天生效，而且被一条测试当成预期钉住了）；第三版还有一条（读不了的日志之后的日子被计入）。
- **主会话的一个错误说法在这里更正**：上一条日检记录里"11-22 的日报确实缺失"不成立。日报任务在次日 21:57 UTC 才生成前一天的日报（T214 评审 B-1）。
- **T209 的分支 CI 第一次失败（#389）**：`tests/test_t110_deadline.py` 的 9 条用子进程跑，套件的替身够不到子进程，CI 的机器上没有 terminal64.exe，`connect()` 直接抛 `TerminalNotConnectedError`。这 9 条在本机能过，只是因为本机恰好有终端在缺省路径下运行。已在 `1902bef` 修正，#391 通过。
- **一条端到端测试在高负载下抖动**：`test_launch_path_demo_run_start_process_hidden_is_verified_and_terminated` 在一次 507 条的相关测试里失败一次（"termination failed"，当时机器负载约 60%），单独重跑 3 次通过。机理在查，见 §7。
- **评审者的一次越界**：T212 的第一位评审者清理时按创建时间删了 6 个 `r23-*` 探针临时目录，其中 3 个来自主会话的一次探针运行（早已结束，没有损失）。之后给评审者的任务说明里都写明只删自己创建的。
- **主会话给一个实现子代理的任务说明里前提写错了**：说看护脚本已合入 main，实际只在 T209 的分支上。子代理发现后停下汇报，没有照错的做。

### 6. 观察运行、磁盘、链路基线

- 观察运行在 20:00、21:23、22:21 UTC 的只读检查都健康：没有 halt、否决、killSwitch；supervisor 自 16:43:18 UTC 起每分钟检查；四个角色的心跳新鲜；当天最后一条告警在 16:30:06。
- 磁盘：20:19 UTC 16.45 GB，22:21 UTC 16.35 GB（17:04 UTC 曾到 6.04 GB）。来源仍是别的项目的任务，等用户处置。
- 链路基线从 UTC 11-24 起算。终端数据目录里的日志现在有 34 天（10-22 起）；本地 11-23 的日志已经复制到 `data/ops-evidence/link-baseline-20261124/logs/`（不入库，sha256 已核）。此后每天复制前一个本地日期的文件，最后一天之后再多留一个。

### 7. 还没做的（定版之前）

- T209 合入；T214 的第二次合入。
- T213 评审的 N1 到 N4；安装脚本对尾随分隔符和 `%` 的老问题（N3）；看护任务的安装脚本和 runbook §6.5 的改写；上面那条端到端测试抖动的机理。都在分支 `pre-freeze-followups` 上做，之后要独立评审。
