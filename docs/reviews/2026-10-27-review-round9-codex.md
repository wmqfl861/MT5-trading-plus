**第九轮定向复审报告（Codex，2026-10-27，Asia/Taipei）**

**结论：修后可。当前不能宣告 R8 九项发现全部闭环，阶段 F 的 Demo 工具前置未就绪，三张 v2 卡的 screen 前置也未满足。** 本轮确认了注入反例反转、整臂缺样拒绝、对账核心修复与重算，以及 T44 时间线更正；同时复现了修复边界上的 **6 项 Important**，另有 **1 项 Minor** 数字错误。逐项判定为 **2 项已闭环、7 项部分闭环**；部分项中已经有效的修复应保留。

审核范围为 `82be091..0d24729`，HEAD 为 `0d24729d9a3bc3154d28adab718a11f6bda8195f`，初始工作区干净。区间确为 **12 个提交**：计划 1 个、T46–T49 修复 4 个、T50 文档集成 1 个、CI 相关 6 个。任务列出的 5 个 CI 提交之外，还有区间内的 `1d12f87`（临时诊断及退出码传播修正），本轮一并核对。T50a 的文档终态按 `5f64f77` 核验。

本机执行 `.venv/Scripts/python.exe -m pytest tests/ -q`，结果 **1678 passed in 254.64s**。只读查询 GitHub Checks 确认 [0d24729 的 pytest 检查](https://github.com/wmqfl861/MT5-trading-plus/actions/runs/40434492495/job/121959789632) 为 success，完成于 `2026-10-27T01:49:20Z`；其公开 notice 的准确内容是 **1676 passed, 2 skipped in 150.83s**。注解没有列出跳过用例名称，不能把它写成“CI 上 1678 项全部执行”；本机本轮全量没有 skip。

复核读取既有分片、合并报告、对账 CSV/summary、影子日志及归档文件；生产台账使用 SQLite `mode=ro`，不经过会迁移数据库的构造函数。新增的[只读复审探针](docs/reviews/probes/2026-10-27-round9-audit.py:1)及[结果 JSON](docs/reviews/probes/2026-10-27-round9-audit.json:1)保存了反转和残留反例。所有修改输入的探针均使用临时副本；PowerShell 生命周期探针仅用 PaperSoak 的 `-PrecheckOnly`，Demo 多行参数探针只解析收到的文本。没有启动交易运行器、连接 MT5、下单、运行 G5/screen、读取原始 bars/ticks 或锁箱段，也没有修改产品代码、交易配置、门槛、台账和正式产物。

R8 各项判定如下。“已闭环”限定于该项原问题；U0 等既有外部前置单独保留。

| R8 编号 | 判定 | 证据与剩余边界 |
| --- | --- | --- |
| **I1：CI head_ref 注入** | **已闭环（注入路径）** | [workflow:59](.github/workflows/research-allowlist.yml:59) 经步骤 env 传值，[67 行](.github/workflows/research-allowlist.yml:67) 用带引号的 `${HEAD_REF:?...}`，run 块无 Actions 表达式内插，基准钩子缺失直接失败。[真实 Bash 回归:523](tests/test_p6_docs.py:523)执行到检查器前的骨架，不只是搜文本。本轮额外证实：旧版注入 exit 0、未到检查器；新版同串 exit 42、到达标记；缺 env、空 env、缺基准均 exit 1，见[结果:21](docs/reviews/probes/2026-10-27-round9-audit.json:21)。workflow 自身随 PR 执行的可信入口/U0 仍未闭环，不能据此放行研究隔离。 |
| **I2：合并器整臂与身份** | **部分** | [应有实验臂:370](scripts/merge_gate_power.py:370)能拒绝正式分片缺 negative/positive/g5 任一整臂；[身份键:201](scripts/merge_gate_power.py:201)已包含 max_bars/g5_cutoff/g5_window_source。全部删除 fingerprint 默认拒绝，显式 `--allow-legacy` 才输出降级标记；但全部置为 `null` 或 `{}` 仍正常合并且不降级，见 **R9-I6**。运行指纹仍未增加输入 K 线内容/实际范围/规格身份，不能扩大为完整数据身份已经封闭。 |
| **I3：对账错误越幅抵消** | **部分：代码及本批重算已闭环，报告解释未完全闭环** | [_decompose_net:482](scripts/reconcile_ab.py:482)要求两侧精确为 stop、同入场 bar、实际/模型越幅有限且不低于浮点噪声容差；P17 得到 `overshoot_comp=0, rest=60, ok=false`。[P17 回归:733](tests/test_reconcile_ab.py:733)、[负越幅:800](tests/test_reconcile_ab.py:800)、跨 bar 回归均通过。四组重算、213 条违规及 97.45% 可独立复算；但[报告:338](docs/reports/reconciliation-v3-2026-10-25.md:338)仍断言所有差异“不是任一层的行为错误”，超出已验证子集；旧符合率还有 R9-M1。 |
| **I4：阶段 F 模式与 0.01 手** | **部分** | [双模式参数及拒绝裸跑:105](scripts/demo_run.ps1:105)、[Demo 示例:16](demo-configs/DEMO-MA-XAUUSD-M15.toml:16)、[运行器分支:370](scripts/demo_run.ps1:370)确已落地，Demo 不带 `--paper`，风控/对账也不带 `--paper-state`。但是 PowerShell 5.1 预检代码传参无法解析（**R9-I1**），0.01 手校验可被合法 TOML 字符串绕过（**R9-I2**），验收器的实际证据源仍主要是 paper（**R9-I3**）。 |
| **I5：空证据假验收** | **部分** | 原 P16 在 `--day` 返回 0、`--accept` 返回 1，汇总不再冒称最终通过，[测试:224](tests/test_demo_check.py:224)和[独立结果:369](docs/reviews/probes/2026-10-27-round9-audit.json:369)一致。但是 paper 证据、只有标题的日报、无有效指令的两份零 closed ack 仍可得到“阶段 F 证据齐全”，见 **R9-I3/I4**。 |
| **I6：单实例及恢复** | **部分** | [读取存活 PID:243](scripts/demo_run.ps1:243)、[追加清单:328](scripts/demo_run.ps1:328)、[跳过已有运行器:365](scripts/demo_run.ps1:365)修复了“同模式、同 LogDir、顺序重启时复制全组”的原路径。[runbook:74](docs/ops/demo-runbook.md:74)恢复步骤已改为移除信号、仅补退出进程。检测只覆盖当前 LogDir，忽略清单的模式列，也没有检测与创建之间的排他保护；不能宣称全生命周期单实例，见 **R9-I5**。 |
| **I7：结构性 flags 与统计陈述** | **部分** | [合并器标注:309](scripts/merge_gate_power.py:309)在新渲染结果中有效，JSON flags 未改变，gate_calibration.py 零字节改动。[报告:101](docs/research/gate-power-v2-2026-10-26.md:101)已将正式验收降级为诊断，并保留独立校准/可比参照/不确定性缺口。但现存正式合并 md 的 [G1.trade_count 行:133](data/reports/gates/gate-power-v2/gate-power.md:133)仍没有标注；[p5 更正:48](docs/research/gate-power-v2-2026-10-26.md:48)修正了大小关系，却又把“阈值更高”解释为“更易越过”，仍与 `sharpe >= p5` 相反。 |
| **M1：live-paper 时间线及轮数** | **已闭环（指定事实更正）** | 第三次黄金 SL `14:54:29`、`15:00:06` 开空、`16:30:06` 翻多反手、`20:15` 平多转空与[原始日志:6988](data/live/paper-XAUUSD-M15.log:6988)、[7156 行](data/live/paper-XAUUSD-M15.log:7156)、[9800 行](data/live/paper-XAUUSD-M15.log:9800)、[16472 行](data/live/paper-XAUUSD-M15.log:16472)相符。P18 独立计数对账 1208、风控 3622、21 成交腿=16 主动请求+5 SL，报告更正属实。“10h06m”按观测窗口解释成立；这项关闭不构成 F 放行。 |
| **M2：蓝图及计划一致性** | **部分** | P2/P3/P4/P6/P7/P8 及包状态表有实质更新，D19/D24/D29 已更新。可是[页首:5](docs/blueprint.md:5)、[P5:42](docs/blueprint.md:42)、[路线图 C/D′/E:182](docs/blueprint.md:182)、[D20–D27:237](docs/blueprint.md:237)仍有旧状态。P5 仍称六卡逐卡复跑待做，与本轮再次核实的 run#364–430、67/67 含 ov 相矛盾。禁止事项将 T39 前置划除、只剩 R9+D16，又与“正式判别力标定仍开放”和阶段 G 隔离要求冲突。T43 执行/重算勾选有据，但最后“路线图 E 已更新”勾选不符终态。 |

**Important R9-I1：Demo 专项预检在 Windows PowerShell 5.1 的原生参数传递中损坏 Python 源码。**

[demo_run.ps1:272](scripts/demo_run.ps1:272)定义含大量双引号的 `$demoFusePy`，在[305 行](scripts/demo_run.ps1:305)经 `& $PythonExeAbs -c $demoFusePy @symbols` 传给原生进程。Windows PowerShell 5.1 的参数重建会剥除这些引号；BOM 解决的是读取 ps1 源文件的编码，无法保留这里的原生参数引号。

本轮抽取完全相同的 here-string，先确认原字符串可由 Python 解析，再经 `powershell -NoProfile -File` 传给只执行 `ast.parse(sys.argv[1])` 的子程序。收到的文本在第 16 行变成 `print(fFAIL`，结果为 `SyntaxError: '(' was never closed`，[证据:397](docs/reviews/probes/2026-10-27-round9-audit.json:397)。未执行该字符串、未导入网关。按 runbook 使用 Windows PowerShell 启动 Demo 时，该预检路径不能到达账户验证；现有测试只覆盖 Demo 的拒绝配置，成功预检用例都在 PaperSoak 模式，见[测试:148](tests/test_demo_run_ps1.py:148)。应改用可靠的脚本文件或标准输入传递，并加入不连接终端的 Demo 预检成功路径验证。

**Important R9-I2：0.01 手校验检查文本行，没有约束实际加载的 TOML 值。**

[demo_run.ps1:216](scripts/demo_run.ps1:216)用两次 Select-String 判断 risk_fraction 不存在且恰有一行 `volume_lots = 0.01`。本轮临时配置的实际顶层值是 `volume_lots = 0.02`，同时在合法的 `created_utc` 多行字符串中放入一行 `volume_lots = 0.01`。真正的 [LiveConfig 加载器:131](src/mt5live/config.py:131)成功读出 **0.02**，启动器原样的两个谓词却返回 **ACCEPT / exit 0**，[证据:408](docs/reviews/probes/2026-10-27-round9-audit.json:408)。这是隔离执行配置检查得到的结果，没有启动 Demo。

现存 DEMO-MA 示例本身为 0.01，普通 risk_fraction/0.02 配置的拒绝回归也有效；缺口是强制边界。应对所有配置调用同一 TOML/LiveConfig 解析器，比较实际 `volume_lots` 和 `risk_fraction`，避免注释、字符串或表作用域影响检查。

**Important R9-I3：验收器仍可用 paper 或无实际成本样本的证据宣布阶段 F 通过，并且未接入 Demo 成交证据。**

[build_ctx:808](scripts/demo_check.py:808)只从 `live/paper/state-*.json` 取得持仓与成交；未读取模式清单，也没有 Demo 成交快照输入。[SL/TP 日志兜底:689](scripts/demo_check.py:689)匹配“保护性出场 … reason=”，但该日志只在 [live_runner.py:466](scripts/live_runner.py:466)的 `paper is not None` 分支产生。真实 Demo 轮询的执行日志没有这个 reason 格式，[508 行](scripts/live_runner.py:508)只打印动作和主动请求。于是无 paper 遗留时真实 Demo 的 SL/TP 可能永远不可验收；有遗留 paper 文件/日志时则可能错误借用它们。

反例直接使用仓库的 [build_full_evidence:92](tests/test_demo_check.py:92)：它仅构造影子账本、`[paper]` 风控日志、文本日报和手工归档指令，**没有任何经纪商成交/滑点证据，`--accept` 仍返回 0 并打印“阶段 F 证据齐全”**。进一步把十份日报全部改成只有 `# Daily report` 的标题，仍返回 0；删除纸面成交账本、只留两条 `[paper] 保护性出场` 日志，也返回 0，见[完整 CLI 结果:376](docs/reviews/probes/2026-10-27-round9-audit.json:376)。这直接否定了 [runbook:13](docs/ops/demo-runbook.md:13)和[129 行](docs/ops/demo-runbook.md:129)“PaperSoak 必然无法通过”的声明。

原因还包括 [accept_spread_samples:750](scripts/demo_check.py:750)只检查非空及两个否定词，没有核实结构化点差样本；滑点仅在提示文字中，没有判据。五条 [ACCEPT_CHECKS:792](scripts/demo_check.py:792)也没有落实蓝图 §7 的全部演练：killSwitch、心跳超时停机、分策略不一致暂停、重连、幂等、换日禁入演练仍可能全缺，而逐日检查照样返回 true。每日出现一条日志也不能独立证明整日进程连续运行。

应让 F 验收绑定本轮 Demo 运行清单、时间范围、magic 集合和真实成交/持仓/报价来源，记录并核对实际滑点、点差样本及规定演练；缺失的项目明确未验收。当前工具可以保留为日常诊断，不能把其退出码 0 当作完整 F 终验收。

**Important R9-I4：双全平验收只取编号并集，未要求有效的两轮指令及各参与策略完成证据。**

[accept_double_flatten:723](scripts/demo_check.py:723)把归档指令 ID 加入集合，再把任何带 `id` 和 `closed` 字段的 ack ID 并入；[735 行](scripts/demo_check.py:735)以集合大小 ≥2 判通过。它不要求 ack 对应存在的指令、不检查 magic、时间范围和两轮真实清仓，也没有验证 closed 的实际含义。

本轮删除临时目录的所有指令，仅放两个不同 ID 的 `{"id": "…", "closed": 0}` 文件，既无 magic、时间戳，也无配套指令，**完整 `--accept` 仍 exit 0**，见[结果:381](docs/reviews/probes/2026-10-27-round9-audit.json:381)。已有 D′ 的两份 10 月 26 日指令也能被未来 F 验收无条件复用。归档即全员完成的假设不能用于所有文件：[实际 T44:144](docs/reports/live-paper-2026-10-26.md:144)已经记录两轮都是人工移动，第一次完整 ack 没有保留；自动归档路径还会[删除 ack:201](src/mt5live/commands.py:201)，所以必须补充可核对的完成记录。

应按本轮指令、时间、参与 magic 逐一关联完成证据，并证明两轮实际清仓、第二轮仍有效；不能只数告警、归档文件或 UUID。原执行器“持仓/挂单清空才 ack”的修复仍成立，本项针对验收器读取证据的方式。

**Important R9-I5：单实例检测随 LogDir 改变失效，模式冲突与并发启动也未关闭。**

[demo_run.ps1:235](scripts/demo_run.ps1:235)只读取当前 `$LogDir/pids.txt`；[253 行](scripts/demo_run.ps1:253)解析 runner/magic 或 risk/reconcile，忽略已记录的 Mode 列。临时清单登记当前存活 Python PID、magic=902601、Mode=Demo：用同目录做 PaperSoak 预检也会把它当作可复用实例；仅换 LogDir、保持同一 magic 和 DataDir 时，预检 exit 0，却报告没有该存活运行器，[结果:414](docs/reviews/probes/2026-10-27-round9-audit.json:414)。若继续启动，会绕过现有重复检测；本轮没有执行启动。

同一 LogDir 内的读取和后续 Start-Process/追加清单之间也没有锁，两个同时启动的调用可均读到“无实例”。追加历史 PID 的重用问题虽有注释提示，停止命令仍会遍历所有历史 PID。建议把所有权绑定到实际 live 数据目录、账户/模式及 magic，使用进程生存期排他锁，验证 PID 创建时间和角色身份；恢复只复用匹配模式的实例，冲突应拒绝。原“顺序重跑同组不再复制存活进程”的改进有效，但尚不足以支持“绝不并行第二份”的绝对声明。

**Important R9-I6：fingerprint 键存在但为空，绕过必填与 legacy 降级。**

[_all_shards_missing_fingerprint:210](scripts/merge_gate_power.py:210)只检查键不存在；[_meta_identity_problems:358](scripts/merge_gate_power.py:358)只检查分片间相等。对正式五分片的临时副本，把所有 `meta.fingerprint` 设成 `null` 或 `{}`，实际 merge CLI 均 **exit 0**，输出 **legacy_no_fingerprint 不存在/false**；删除键则 exit 1，带 `--allow-legacy` 删除键才正确降级，见[结果:335](docs/reviews/probes/2026-10-27-round9-audit.json:335)。

应校验指纹是非空对象且必需字段具有有效类型/内容，再做相等比较；明确哪些历史缺失可进入 legacy，畸形指纹应拒绝。本批原始五分片的指纹实际完整且一致，因此这项防线缺陷不使已有 800 条记录失效。

**Minor R9-M1：T43 修订前符合率的分数与百分比不符。**

[报告修订表:422](docs/reports/reconciliation-v3-2026-10-25.md:422)、[蓝图 P3:40](docs/blueprint.md:40)、[T43 计划:45](docs/superpowers/plans/2026-10-26-round8-finalization.md:45)以及旧复跑摘要沿用了 `98.13%（8206/8364）`，实际是 **98.1109517%，应为 98.11%**。修订后 `8151/8364=97.4533716%`，**97.45% 正确**。应同步修正旧基准百分比，不改变 158→213 的事实。

对账产物的独立复算支持以下结果，使用四个源目录逐一对应四个 `_r8i3-*` 目录，没有把旧新两套产物混作独立样本：

| 组 | 配对 | 原净利违规 | 新净利违规 | 新净利容差内 | 同 bar stop↔stop / 违规 |
| --- | ---: | ---: | ---: | ---: | ---: |
| XAUUSD H1 immediate | 382 | 18 | 18 | 364 | 134 / 0 |
| XAUUSD H1 ASC | 161 | 5 | 5 | 156 | 62 / 0 |
| USTEC M5 immediate | 4170 | 73 | 94 | 4076 | 1046 / 0 |
| USTEC M5 ASC | 3651 | 62 | 96 | 3555 | 877 / 0 |
| **合计** | **8364** | **158** | **213** | **8151，97.45%** | **2119 / 0** |

[本轮逐目录结果:901](docs/reviews/probes/2026-10-27-round9-audit.json:901)确认：新 overshoot/rest/net_ok/row_ok 与从旧 CSV 独立计算一致；summary 违规计数和 net_violations.csv 数量、分类一致；非净利拆解字段仅有 CSV 浮点重序列化误差，`net_tol` 最大差约 `4.44e-16`，没有放宽容差。五类净利违规为 **TP 穿价 121、A-stop/B-nonstop 原因分叉 57、其他原因分叉 16、TP gap 12、跨 bar 7**，合计 213。61 条 A-stop/B-nonstop 中，57 条超容差（原已违规 2、新浮出 55），其余 4 条仍在容差内；撤销该类旧越幅分量合计 **1917.98163442 USD**。时间违规 21/65、原因违规 77、unmatched 51+1278=1329 不变；36 个 before_ready、50 个 level_unknown 也与 CSV 一致。

[重算脚本:70](scripts/recalc_reconciliation_v3_r8i3.py:70)只读四份既有 diff.csv/summary，按新规则选择可保留的越幅并重算净利判据；保留的同 bar stop↔stop 行均通过旧出场机制检查。本批无需重新读取行情即可复算这些字段。新 summary 的 `meta.recalc=true/source_run` 与源目录一致，`ops_ack=true` 是[继承源 meta:127](scripts/recalc_reconciliation_v3_r8i3.py:127)，不能解释成此次新增豁免。本轮只读台账核实历史 ops 为 **run#442–445**，**445 之后无新增 actor=ops**，验链和外部锚均无错误，[证据:811](docs/reviews/probes/2026-10-27-round9-audit.json:811)。这支持“本地重算”的来源声明；不把它扩张为重新逐 tick 证明所有行为一致。

T43 报告仍须收紧两处解释：[338 行](docs/reports/reconciliation-v3-2026-10-25.md:338)“全部同源……不是任一层的行为错误”，以及[354 行](docs/reports/reconciliation-v3-2026-10-25.md:354)以资金配置解释两模型不同存续期后断言“非引擎分歧”。现有分类、源文件和部分检查通过，可以证明差异数量与已观察机制，不能证明尚未成功核验的 50 条 level_unknown 没有任何实现问题。另[273 行](docs/reports/reconciliation-v3-2026-10-25.md:273)把 57 条都写成原先被抵消到容差内，与 §9 自己列出的“原已违规 2 条”冲突；[288 行](docs/reports/reconciliation-v3-2026-10-25.md:288)的“最大行 +206.15”应限定为 TP gap 类，因为新清单已经包含 +271.82 的原因分叉行。

合并器与正式产物方面，五分片的 negative/positive/g5/flags 四节重新聚合后与正式 JSON **逐项相等**，三个补充节文本一致；记录数仍是 160/160/480，正、负对照 passed_all 均为 0/160。[字节证据:3](docs/reviews/probes/2026-10-27-round9-audit.json:3)确认 `gate_calibration.py` 在两端提交的 Git blob 完全相同，`config/gates.toml` 区间 diff 也为空。工作区脚本哈希与正式分片 `2f4813dc…` 匹配；Git blob 的 `429296fb…` 是 LF 版本，CRLF 规范化后与工作区一致，不能将换行差异误报为修复破坏指纹。

新渲染会给 G1.trade_count 加结构性限定，但目前报告引用的旧 `data/reports/gates/gate-power-v2/gate-power.md` 还没有该限定，[结果:364](docs/reviews/probes/2026-10-27-round9-audit.json:364)。应保留原始分片，另行更新或明确区分合并展示产物。p5 的数值更正属实：USTEC −2.993～−1.688，XAUUSD −1.341～−0.930；然而 [gate_calibration.py:682](scripts/gate_calibration.py:682)采用 `sharpe >= p5`，固定同一测试 Sharpe 时下限越低越容易通过。不能用“USTEC p5 更低所以更难过、XAUUSD p5 更高所以更易过”解释整套 G5 通过率。删除这条因果解释、保留描述性结果即可；独立强度校准、可比 G3 样本外参照及不确定性仍开放。

CI 后续提交的终态没有放松现有业务断言。按提交及最终差异核实如下：

| 提交 | 实际变化 | 审核判断 |
| --- | --- | --- |
| `a8f4cd6` | PrecheckOnly 成功测试自备 specs/spread/research 三份文件并显式传路径；PowerShell 输出编码包装；优先显式 Git Bash | 测试环境自足。dummy 文件服务于本来只查存在性的 PaperSoak 预检，没有把 Demo 账户检查 mock 成通过；WSL bash 遮蔽处理未改注入串或 exit 42 断言。 |
| `1ec01fd` | pytest 失败时追加 FAILED/ERROR 注解 | [tests.yml:32](.github/workflows/tests.yml:32)仍保存 pytest `$LASTEXITCODE` 并最终原样 exit；不会把红灯改绿。 |
| `40e9d66` | Console.OutputEncoding 赋值 try/catch、附失败诊断 | 改环境兼容与错误信息，原返回码及 ASCII 内容断言保持。 |
| `c3cde13` | 合并 PowerShell 信息/错误流，提取诊断首行 | 改捕获方式，没有删除业务断言。 |
| `1d12f87` | 去掉 wrapper 尾部错误的 `exit $LASTEXITCODE`，加恒失败临时行为探针 | 属区间内第 6 个 CI 相关提交，不能漏审；退出码传播修正有必要，临时探针不承担业务保护。 |
| `0d24729` | 两个 ps1 各前加 UTF-8 BOM；删除上个提交的恒失败诊断探针 | **二进制比较证明每个 ps1 仅增加 `EF BB BF` 三字节，余下字节完全相同**，无夹带行为更改。业务测试未删；R9-I1 是 T49 路径残留，不是这次 BOM 提交引入。 |

另外比较 `bcb831c` 与 HEAD 的两个受影响测试文件：所有 `test_*` 函数及其 `assert.test` AST 一致，[结果:16](docs/reviews/probes/2026-10-27-round9-audit.json:16)。该证据说明 CI 调整没有篡改这些既有断言的判断条件；它不证明原断言已经覆盖真实 Demo 成功路径、证据来源或全生命周期单实例。

文档同步仍应按证据完成，而不是把计划提交说明当作完成证明：

- [蓝图 P2:39](docs/blueprint.md:39)关于 stop_level 缓存和 swap 费率身份的实质更正有效，但把 swap 修复标成 `R8-I2` 容易与本轮合并器编号混淆，应改为对应 R7-I2/上一轮复核项。P3 新计数正确；P4 的“指纹必填”受 R9-I6 限定；P8 的就绪声明受 R9-I1–I5 限定。
- [P5:42](docs/blueprint.md:42)没有完成计划要求的 67 条六卡复跑更新；[P19:814](docs/reviews/probes/2026-10-27-round9-audit.json:814)再次证明 H002/H003/H004/H005/H006/H007 的 run#364–430 全部含 ov。页首仍称 R7/1609，D20/D21/D22/D23/D26/D27 仍写待做，路线图 C 仍待第八轮终审，E 仍称“结构性对账检查通过”。
- [禁止事项:56](docs/blueprint.md:56)划掉 T39 正式统计验收前置，写剩余 R9+D16；[路线图 G:187](docs/blueprint.md:187)却仍要求 A/B/C 完成且隔离三层落地。这不是本轮可以默认为完成的条件变更。P6 已诚实写 ACL/分支保护/可信入口仍开放，不能被“复审通过后 screen”一句覆盖。
- [live-paper 页首:14](docs/reports/live-paper-2026-10-26.md:14)仍写“阶段 F 放行”，应限定为 D′ 已观测场景通过，并引用本轮 F 工具结论。M1 的时间/计数更正可以关闭，放行越级文字仍归 M2/阶段边界处理。
- [T43 复选框:41](docs/superpowers/plans/2026-10-26-round8-finalization.md:41)的四组执行、成本加载和报告重算动作有产物支持，不能解读为全部机制验收通过。应把“213 条四分类”改为当前产物的五类，修正旧百分比，并收紧“结构差异已归因”的绝对表达；[51 行](docs/superpowers/plans/2026-10-26-round8-finalization.md:51)额外的“路线图 E 已更新”勾选应在真正更新后再确认。

对 `2026-10-27-r9-probe-rerun.json` 的判断是：**原反例反转摘要大体可信，但不足以支持九项全部关闭。** 本轮独立复跑确认了 P14 原整臂/身份变更拒绝、P16 严格档拒绝空证据、P17 新签名下 +60 不再被抵消。P15 旧内插锚点已消失属预期，应使用新行为回归；直接按旧脚本全跑会在旧锚点/旧签名处失败，不是产品回归。

该摘要中 P17 的 `16728/371/4238/122` 是旧新两套目录的合计：8364×2、158+213、2119×2、61×2。它如实注明了混合口径，但不能把 371 当作修后违规数或把 16728 当作独立配对样本。P18/P19 原摘要主要转引报告及未入库临时文件；本轮已用本地原始产物和只读台账重新验证，结论不再依赖这些转引。原 JSON 的 `98.13%` 仍须按 R9-M1 更正。新增反例说明只检查 P14/P16/P17 原输入是否反转，会遗漏修复边界。

对三个放行问题的回答如下：

| 放行问题 | 明确回答 | 需要满足的条件 |
| --- | --- | --- |
| **阶段 F 以 `-Mode Demo` 启动的工具前置是否就绪？** | **否。** | 先解决 PowerShell 5.1 多行参数传递、按 TOML 实际值约束 0.01 手、模式/数据目录一致的单实例及恢复，并接通 Demo 证据采集与正确验收。旧 magic 登记退役、paper 遗留状态/日报口径切换、拟用配置目录也应在启动清单中明确。修复复核后，启动仍须在用户可见的安排下另行执行。本轮未启动。10 个交易日及实测 SL/TP/演练样本属于启动后的验收任务。 |
| **三张 v2 卡（b75092c）的 screen 开跑前置是否满足？** | **否。** | 三卡先提交、策略代码在库、成本基线重跑已有证据；但 [P6:43](docs/blueprint.md:43)的 U0 隔离/ACL/研究账户执行/受保护可信 CI 入口仍无落地证据，[G 阶段:187](docs/blueprint.md:187)要求的 C 完成也不能由“诊断展示完成”代替。需关闭相关合并器身份与统计陈述缺口，并明确 C 的正式验收证据或经正式决策调整其里程碑定义。F 两周运行完成并不是该 G 行新增的隐含前置。 |
| **G5 维持禁跑（待 D16）之外还有阻断吗？** | **有。** | F 保留 R9-I1–I5 工具/证据阻断；screen 保留 U0 三层隔离及现行 C 完成条件；关卡正式放行保留 R9-I6 身份校验与 T39 正式判别力标定缺口。D16 不是 G5 当前唯一未完成条件。MT5 测试器、swap 计费时刻/过夜实测等既有开放项继续按各自阶段管理，本轮没有新增其完成证据。 |

建议下一轮优先修正 Demo 的实际执行边界和证据契约：先让 Windows PowerShell 5.1 的合法 Demo 预检可在无终端假件下走通，再以同一配置解析器验证手数，补稳定的进程所有权与恢复规则，最后用真实 Demo 数据格式约束验收。为本轮六个反例增加具有业务判别力的回归，避免用“文件存在”“编号数够”“没有否定词”替代取得证据。合并器应先校验有效指纹，再比较一致性；保持原五分片与冻结门槛。

随后同步合并展示、gate-power 的 p5 解释、对账限定和蓝图所有关联状态；把“代码已修”“该批产物已核实”“阶段可以放行”分别落到具体证据。无需为修正这些已可由现有产物证明的问题重读锁箱或重跑 G5。按阶段列出待关闭项，再进行定向复核。

本轮复现命令（仅审查产物和临时副本）：

```powershell
.\.venv\Scripts\python.exe docs/reviews/probes/2026-10-27-round9-audit.py --out docs/reviews/probes/2026-10-27-round9-audit.json
```

交付为本报告及配套探针/结果 JSON；未修改产品代码或正式运行产物。
