# 第六轮审核报告（codex / gpt-6-astra，思考等级 max，2026-10-26）

审查范围：37d503f..97f21b6（第五轮 T30-T41 全部实现 + 实验分片方案）。
验证方式：codex 只读审查 + 全量测试实跑（1549 passed）+ 探针复核 + 针对性补充复现。
原文由 codex 生成，本文件为末尾结论部分全文（工作过程略）。

---

### 总评（3–5 句）

本次审查覆盖 `37d503f..97f21b6` 的 14 个提交、82 个变更文件，并按指定顺序核对了第五轮审核、施工计划和蓝图。全量测试实际为 **1549 passed，耗时 230.09 秒**；P9 的逐 bar 调仓和 P10 的旧确认编号问题在正常成交路径上已修复，但原 C1/C3 在成交历史延迟、人工平仓、部分成交和并发写入条件下仍未闭环。新增实现还存在成本模型未接入正式回测、台账篡改被正常写入重新封链、运维豁免进入研究关卡，以及实验负对照和分片统计失真的问题，**当前不具备合并及阶段放行条件**。审查未修改仓库文件，结束时工作区干净；没有运行 `gate_calibration.py` 实验或干预后台五个分片进程。

| 探针 | 实际复核结果 |
|---|---|
| **P8 对账死锁** | **部分修复**：成交历史立即可读时可恢复，策略暂停也已隔离；历史延迟和人工出场竞态仍有缺陷。此外原探针只读全局 `pause.json`，打印 `None` 不能证明策略未暂停。 |
| **P9 逐 bar 调仓** | **正常路径已反转**：六根变 ATR、同向信号，只有一次开仓、零次平仓，始终为 0.16 手。 |
| **P10 重复全平** | **正常路径已反转**：第二条指令不会被旧 ack 吞掉；但部分平仓仍会被错误确认为“全平完成”。 |
| **P11 EURUSD 禁入窗** | **行为已反转**：六小时缩至 65 分钟；删除长期扩展窗的实证覆盖仍不足，见 Important。 |
| **P12 G5 判别力** | **未反转**：参考分布约为 p5=-1.37、p95=1.99，锁箱 Sharpe=-0.3、期望值=0.5 的例子仍通过。这不是要求擅改冻结门槛，而是说明 T39 的判别力报告不可缺席。 |
| **P13 G4 内存** | 一日块长运行峰值约 250 MB；默认 `block_len=1` 仍存在一次性分配约 3 GB 起点索引的路径。这是原 M9 的遗留项，本轮没有解决。 |

### Critical（必须修：错误结果/数据损坏/资金风险/锁箱旁路）

1. **全平部分成交后仍写完成 ack，剩余仓位会被永久跳过。**  
   位置：[src/mt5live/executor.py:632](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/executor.py:632)、[src/mt5live/commands.py:156](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/commands.py:156)。

   `_consume_flatten()` 调用 `close_all()` 后，无条件写 ack，并把当前实际仓位写成 confirmed；下轮只要编号匹配便返回 `flatten_acked`。补充复现中，0.16 手只成交一半，**残余 0.08 手、ack 已存在、下一轮跳过、归档仍成功**。这使 T30 的“确认实际成交量”和 T32 的“全平完成”被错误等同，日亏止损可能留下真实风险敞口。

   **修复建议：**只有重新查询确认该 magic 的持仓和挂单均已清零，才写完成 ack；部分成交保持命令未完成并继续处理残仓。判据应检查持仓明细，不能仅检查净额为零。

2. **外部出场状态机仍会丢失待核对事件，也会在对账前自动重开并洗白人工操作。**  
   位置：[src/mt5live/executor.py:437](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/executor.py:437)、[src/mt5live/executor.py:716](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/executor.py:716)。

   发现消失的持仓后，若第一次查成交历史为空或失败，只写告警，随后仍在第 746 行用当前仓位覆盖 `known_positions`。恢复历史查询后，原 ticket 已不再进入 `gone`，不会重试；复现结果是实际空仓、意图仍为 0.16 手、对账继续失败。另一方面，“未解释”没有同步阻止开仓：人工平仓后让执行器先于对账进程运行，`immediate` 策略会直接重开，再把新仓位确认为合法；复现为 `action=open`，后续对账返回成功，完全绕过人工确认。

   **修复建议：**持久化未解决出场事件，保留持仓标识及所需成交上下文，持续重查；未解释事件应在执行器内立即禁止增加风险，直到确认为 SL/TP/SO 或收到人工确认，不能依赖另一个进程稍后写 pause。

3. **影子账户无法支持计划要求的两个运行器，且常驻风控、对账读到的是启动快照。**  
   位置：[scripts/live_runner.py:283](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/live_runner.py:283)、[src/mt5live/paper.py:276](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/paper.py:276)、[src/mt5live/paper.py:434](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/paper.py:434)。

   所有运行器共用 `paper/state.json`，但各自启动时加载一份内存账本，后续整份覆盖。两个实例分别开仓的复现中，**磁盘只剩第二个 magic，两个实例还分配了相同 ticket 7000001**。`PaperAccountView` 同样只在构造时读文件，常驻风险和对账循环不会刷新；复现为旧视图看到零仓位，新视图看到一笔。此外，无保护性成交的轮询不保存最新浮盈，复现中本地浮亏为 -102，磁盘仍为 -2。

   **修复建议：**由单一账户服务负责撮合、余额和票号分配，或实现跨进程事务及锁内重读；风险和对账每轮取得同一版本的新快照，并持续发布浮盈和快照时间。修复前，影子盘不能充当 T34 的全链路验收证据。

4. **T36 越幅成本没有接入正式回测，最小样本天数限制也未生效。**  
   位置：[scripts/run_backtest.py:532](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/run_backtest.py:532)、[scripts/run_backtest.py:577](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/run_backtest.py:577)、[src/mt5live/gate.py:341](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/gate.py:341)。

   构建器已把越幅表写入缓存，但 `build_costs()` 创建 `Costs` 时未传 `overshoot_model`，所以正式回测的 `stop_overshoot()` 始终返回零，G2 的越幅敏感性也随之退化。补充复现使用含非零越幅的 v2 缓存，预期越幅 1.0，实际为 0.0。`entry_policy_for()` 未传 `min_days_per_hour`，实盘点差闸门也只取 `stat.p90`；仅有一天样本的小时，在配置要求 20 天时仍允许入场。

   **修复建议：**统一研究回测、实盘和 A/B 对账的成本加载契约，校验并注入越幅模型，同时贯通样本天数限制；用真实缓存格式从 CLI 入口验证。修复后重跑基线及相关关卡，当前 v3 报告不能作为“已计入越幅”的证据。

5. **台账哈希链会由正常写入替篡改重新封链，删尾和删除锁箱记录也不会报警。**  
   位置：[src/mt5quant/research/ledger.py:258](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:258)、[src/mt5quant/research/ledger.py:388](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:388)、[scripts/ledger_anchor.py:88](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/ledger_anchor.py:88)。

   每次合法写入都会 `_rechain()` 整张表，却不先验证旧链。复现中，修改历史行后验证能报错，正常追加一次 `record_run()` 后错误立即消失；删除最后一行同样验证通过。删除唯一的 `lockbox_uses` 记录后，验证仍通过，且 `lockbox_used=False`，一次性预算被重置。锚定脚本只追加当前链头，没有核对可信历史锚；锚文件又处于研究可修改的 `docs/research/**` 白名单内。

   **修复建议：**采用追加式事件账本，状态变更追加事件而非改写历史；在同一事务内验证并追加链，统计及锁箱预算使用前验证完整性。把序号、行数和前缀哈希锚定到研究账户不能改写的位置，并实际验证历史前缀；禁止普通写入自动重封已有记录。

6. **`--ops-ack` 能生成正式研究结果，锁箱隔离仍有旁路。**  
   位置：[scripts/bt.py:668](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/bt.py:668)、[scripts/bt.py:729](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/bt.py:729)、[scripts/bt.py:898](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/bt.py:898)。

   `ops_ack` 被传入研究数据加载器，直接读取包含锁箱的行情，之后照常运行、登记关卡；`actor=ops` 只是标签。临时数据上的真实 CLI 复现中，cutoff 为 12:00，`bt gate --main-store --ops-ack --gates G0` 使用数据到 15:00，返回成功并登记 `G0 passed=1`，而 `lockbox_used=False`。同一路径也供 G1–G4 使用。另外，G5 仅拒绝 `research_mode`，自定义数据根能绕过文档要求的显式 `--main-store` 检查。

   **修复建议：**研究 `screen/gate` 禁止使用运维豁免；运维测量进入独立命令及记录类型，不能参与选参、研究关卡或试验统计。G5 应明确校验主库授权与批准，基准研究段仍须截在 cutoff 前，锁箱读取必须发生在一次性登记之后。

7. **日内“零假设”保留了原日期每根 bar 的开收盘收益，无法证明交易优势已被打散。**  
   位置：[src/mt5quant/validation/synthetic.py:227](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/validation/synthetic.py:227)。

   代码置换的是 `close_i / close_(i-1)`，随后用同一个因子缩放原 bar 的 OHLC，因此恒有：

   `new_close / new_open == old_close / old_open`

   原日期、原时槽的 bar 内涨跌和形状没有置换，变化主要被放进相邻 bar 的跳空。用仓库自己的漂移样本复现，开盘到收盘的漂移置换前后均约 **0.0303554846**，单根收益最大变化只有浮点误差。对下一根开盘入场、bar 内止损止盈的策略，这不是所宣称的零假设，误报率实验会被污染。

   **修复建议：**先明确要消除的可预测关系，联合置换相对 OHLC 路径及相关特征，按因果顺序重建价格；通过真实交易策略验证植入优势消失。还须处理方案本身的限制：保留每个时槽的收益多重集，就不会消除该时槽的无条件均值漂移。

8. **分片合并器不兼容真实产物，并错误合并 G5 误报率、漏报低检出率。**  
   位置：[scripts/merge_gate_power.py:65](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:65)、[scripts/merge_gate_power.py:87](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:87)、[scripts/merge_gate_power.py:123](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:123)。

   已复现三类问题：G5 生成器写 `setup`，合并器读 `key`，会触发 `KeyError`；正对照 `calibration` 被转成列表，而报告器调用字典 `.get()`，会触发 `AttributeError`；G5 负对照按 `arm=false_positive` 分桶，把 `day/slot` 两种零假设混在一起，例如 100% 与 0% 被合成 50%，违反取最差值的规则。flags 又用 `positive_rates or g5_rates` 二选一：存在普通正对照时，G5 检出率为零也可能不产生低检出率告警；[scripts/gate_calibration.py:1346](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/gate_calibration.py:1346) 同样存在此问题。

   **修复建议：**统一产物 schema；按 `null` 分别计算 G5 误报率，按规则取最大值；同时遍历普通正对照和 G5 检出率。普通正负对照按拼接后的 runs 重算比率这一点是正确的，但种子检查目前只相信 metadata，还应校验实际运行记录的种子、重复、缺样，以及数据、成本、关卡和实验配置一致性。

### Important（应修）

1. **T33 的原子文件替换不足以支持多个写者。**  
   位置：[src/mt5gw/snapshot.py:123](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5gw/snapshot.py:123)、[src/mt5live/commands.py:85](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5live/commands.py:85)。

   公共写入函数使用固定的 `path + ".tmp"`，两个写者会争用同一临时文件；受控交错复现得到一个成功、一个 `FileNotFoundError`。即便把底层写入串行化，`write_flatten()` 的“先读、再创建”仍能让两个调用返回不同编号，最后只保存一个，违反已有指令幂等复用的契约。`read_flatten()` 还没有把读取时的 `OSError/PermissionError` 转为约定的不可读指令异常。

   **修复建议：**使用独立临时文件，并对指令创建、编号核对、归档及 ack 清理实施跨进程锁或 CAS；补足读取异常的保守处理。仅更换临时文件名不能解决业务层竞态。

2. **成本模型变了，试验身份却没有变。**  
   位置：[scripts/run_backtest.py:543](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/run_backtest.py:543)、[src/mt5quant/research/ledger.py:580](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/research/ledger.py:580)。

   `cost_model_version` 不包含缓存内容哈希、采样口径或越幅模型身份。复现中，合法修改缓存并更新 SHA 后，模型点差从 0.1 变为 10.0，成本版本字符串仍完全相同。台账去重和自动选参依赖该字符串，可能把不同成本假设下的试验混为同一身份，也不满足 T36 明确要求的成本版本递增。

   **修复建议：**把完整成本契约及内容摘要纳入不可变版本标识，并让筛选、关卡、对账共用这一标识。

3. **越幅统计计算的是相邻 tick 跳动，不能直接称为真实止损越幅分位数。**  
   位置：[src/mt5quant/costs/overshoot.py:105](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/src/mt5quant/costs/overshoot.py:105)。

   模型把上一笔报价设成假想止损价，再统计 `abs(current-prev)`；真实止损是预先固定的价位及首次满足 `Bid<=SL`／`Ask>=SL` 的事件。例如 Bid 为 `100→99.9→99.8`、SL 为 99.9，真实首次触发越幅为零，当前模型统计的向下跳动却约为 0.1。两者的条件分布不同，当前算法还排除了恰触价位的零越幅事件。

   **修复建议：**用预先确定的止损价位回放首次触发事件，保留零越幅并对齐触发侧及价格精度；或者明确标为跳动代理模型，先验证偏差再用于成本结论。A-1.6 中多头减、空头加，以及 `stop_gap` 不重复加的处理本身正确。

4. **CI 白名单检查执行的是待审 PR 自己提供的检查器。**  
   位置：[.github/workflows/research-allowlist.yml:39](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/.github/workflows/research-allowlist.yml:39)。

   工作流从 PR checkout 导入 `scripts/hooks/pre-commit-readonly-paths`；PR 可以同时修改检查器，使其返回无违规，随后自行通过。分支名不以 `research/` 开头还会直接成功退出，因此分支命名不能承担角色权限判断。

   **修复建议：**从受保护的基准版本执行检查器，把 PR 仅作为待检查的差异数据；角色豁免绑定受保护的身份或审批机制，并保护工作流自身。现有单元测试验证了函数规则，没有验证这一信任边界。

5. **实盘默认点差文件与加载器格式不兼容，默认启动路径失败。**  
   位置：[scripts/live_runner.py:131](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/live_runner.py:131)。

   构建器产物是含 `version/model/model_sha256` 的缓存包装，而 `SpreadModel.load()` 期待裸模型。对当前默认成本文件只读调用加载器，实际抛出 `bucket_tz=None` 不兼容错误；按报错提示重新构建仍会得到相同包装格式。

   **修复建议：**使用统一的缓存加载器，校验包装版本和内容哈希后取得模型；补一条“构建器产物直接交给默认 live/paper 启动入口”的集成检查。

6. **T40 以毫秒时间代替 tick 身份，会误判正确成交，也可能漏掉更早触发。**  
   位置：[scripts/reconcile_ab.py:378](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/reconcile_ab.py:378)。

   保护性出场核对总取出场毫秒内最后一笔报价作为成交 tick；`searchsorted(..., side="right")` 又跳过入场毫秒内的所有 tick。数据层明确保留同毫秒多笔行情。给现有正确止损样例追加同毫秒后续报价后，真实 B 引擎仍在首笔正确成交，对账却报 `no_trigger_at_fill`；另一个例子报 `fill_price_mismatch`。

   **修复建议：**保留稳定的 tick 序号或同毫秒内序位，按实际事件次序核对入场、首次穿价和收盘成交；不要通过扩大时间或价格容差修补。

7. **T35 的历史 DST 判断较扎实，但 EURUSD 删除扩展窗的全年依据尚未补齐。**  
   位置：[docs/research/rollover-dst-2026-10-25.md:108](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/research/rollover-dst-2026-10-25.md:108)、[docs/research/costs-v3-2026-10-26.md:12](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/research/costs-v3-2026-10-26.md:12)。

   排除锁箱窗口后，历史测量复现为 XAUUSD 9/9、USTEC 5/5 跟随纽约，支持每日休市的美国 DST 口径。但 EURUSD 删除扩展窗仍引用约八个工作日、两个周日、全夏令时的旧 tick 加权样本，文档承诺的回补后复核未见落地。成本报告还声称覆盖“10 月欧洲先切错位窗”，而相关数据截至 10 月 23 日，2026 年欧洲切换发生在 10 月 25 日，时间上不成立；历史休市证据也不能替代尚待观测的 swap 计费时刻。

   **修复建议：**用回补后的冬夏及已有错位窗口数据，按 `bar_open_m1`、工作日/周日和分钟粒度复核禁入边界，并修正文档覆盖声明。删除旧六小时窗口有依据，但“全年无需扩展”的结论需要额外证据。

8. **测试没有过宽异常断言，但关键假件和验收场景偏理想化。**  
   位置：[tests/fakes_live.py:252](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/fakes_live.py:252)、[tests/test_gate_calibration_script.py:567](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/tests/test_gate_calibration_script.py:567)。

   AST 扫描的 421 处 `raises` 中，**没有 `Exception/BaseException` 过宽断言**；假件的服务签名和成交原因常量总体也有对齐。但 FakeBroker 的平仓总是全成交，成交历史立即可见；影子账户没有覆盖真实多个常驻读写者。新增分片测试只造负对照，两个分片样本数还相等，既漏掉真实正对照/G5 schema，也不能有效区分“合并样本重算”和“直接平均分片比率”。

   **修复建议：**补充部分平仓、历史延迟及失败恢复、执行器先于对账的竞态、多进程共享账户，以及不等样本数的完整三实验臂产物测试；测试应验证业务不变量，而非只验证辅助函数返回结构。

### Minor（可选）

1. **原 P8 探针已不适配分策略暂停文件。**  
   位置：[docs/reviews/probes/2026-10-25-round5-probes.py:34](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/reviews/probes/2026-10-25-round5-probes.py:34)。

   `_paused()` 仍只读取 `pause.json`，会把新的 `pause-<magic>.json` 漏掉，削弱“探针反转”的证据。建议分别检查全局暂停和各 magic 暂停，并断言它们在执行器、对账两种调度顺序下的状态变化。

2. **合并脚本硬编码了当前机器的仓库绝对路径。**  
   位置：[scripts/merge_gate_power.py:24](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/scripts/merge_gate_power.py:24)。

   测试把脚本复制到临时目录后，依赖这个固定 Windows 路径找到 `gate_calibration.py`；当前机器全绿不能说明其他机器或 CI 可运行。建议通过模块导入或明确传入仓库根定位依赖，测试使用可控路径，不设置个人工作区兜底。

3. **第五轮状态和证据索引尚未同步。**  
   位置：[docs/blueprint.md:84](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/blueprint.md:84)、[docs/superpowers/plans/2026-10-26-round5-hardening.md:366](C:/Users/Administrator/.zcode/workspace/664d5945-068c-46aa-ba82-7141f134aa13/docs/superpowers/plans/2026-10-26-round5-hardening.md:366)。

   蓝图仍把已存在的实现列为待施工，第五轮证据复选框也未同步。建议逐项标明“实现完成／测试通过／真数据待补／真机待验收”，并链接证据；保持 T34 一天双策略影子盘、T39 完整实验及 T40 四组全量对账为未验收，不能用测试数量替代这些证据。

### 结论：可合并？修后可

**当前不可合并，也不能据此放行非干跑交易或 G5。** 应先修复全平确认、外部出场状态机、影子账户一致性、成本接线、台账和锁箱旁路，以及实验负对照与分片统计问题，再进行对应回归。

修复后还需重跑受影响的成本基线和标定实验，完成 T34 一天双策略影子盘及 T40 四组机制对账。后台分片日志目前仍在负对照阶段；即使现有运行正常结束，其结果也需先按上述模型和统计缺陷判断哪些可以保留、哪些必须重做。
