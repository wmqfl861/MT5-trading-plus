# 就绪后续批次独立整合审核（2026-11-06）

本记录由主会话转录未参与本批三个模块实现、范围裁定或模块审核的独立整合审核者直接返回的结论。**Spec PASS / Quality PASS。** 初次整合审核发现1项非阻断 Minor；文案修复后，同一审核者裁定该项 **ADDRESSED**，修复差异未发现新增 Critical、Important 或 Minor。

## 固定审核版本

- 比较基点：`f55ac34e27ba79b872171047023702511696a50c`。
- 整合受审 HEAD：`849cc73594416c1cc475dc43c5a04e380a858138`，tree `2a87d54a20efd357b3afa2d2d64aaffa2f11bcc6`。
- 文案修复：`dffd1c55b9940452be42969bba4609cbdea2c9b5`，父提交为上述受审 HEAD，tree `01819c438f66f6337c460063def0b67dc1b3e0d1`。
- 两轮均只读核对固定 checkout；审核前后 HEAD、index 和文件保持不变，工作树干净。
- 本报告提交时，本批最终全仓回归和最终待推送提交的 main CI 尚待执行，不能以此前 `c0df5a1` / #304 的成功替代。

## 整合核对

1. **终审源完整承接。** 审核者独立比较 Git blob、作者受测 SHA256 和 checkout 字节。H 的3文件、P 的3文件、Ops 的4文件分别与 `e7a0f95`、`0626dd7`、`2cc35ca` 一致，checkout 差异仅为明确的 LF/CRLF 映射。本批23个变更文件及归档完整 diff 与固定 HEAD 一致，差异格式检查通过。
2. **未增加产品或运行时接入。** 除文档、测试外，本批只增加[时间诊断](../../scripts/h_time_audit.py)、[V2-A纯内存核](../../src/mt5quant/prob/d61_v2a.py)，并修改既有[检查器](../../scripts/demo_check.py)。冻结研究/产品代码、运行器、launcher、supervisor、依赖和CI均未改。V2-A没有新增包级导出或产品消费者，也未进入既有T121固定计算闭包。
3. **接口与消费者衔接一致。** H的七个CLI参数与API一致；FRED生产端naive datetime64[ns]格式与工具兼容，日期标签、UTC开盘标签和footer/tail读取范围没有混用。正式检查器的角色、magic、argv与现有launcher/supervisor记录格式衔接，周报仍调用日常summary；旧日常loader和汇总保留，正式分支继续只有2/1/3出口。遗留print_accept的旧通过文案没有现行CLI调用路径。
4. **阶段F历史没有升级为当前验收。** 上游 `f55ac34` 仅追加阶段F日志。审核者核对日志顶部的磨合期说明、仍标计划的探针及待核实结果，没有将历史“交易日9/10”视作D57正式十交易日完成或A5。蓝图、交接及准备验收继续保留实际窗口、演练和真实材料缺项。
5. **公开交付事实可追溯。** 审核者通过公开GitHub API确认 [tests #304](https://github.com/wmqfl861/MT5-trading-plus/actions/runs/42005274111) 为 `main` push、完整SHA `c0df5a1d133eadbe8a8c089fba0758ed840d830b`、completed/success，明确属于上一批。本轮核对的92个本地文档链接均有受跟踪目标；公开说明未预报本批全量或CI成功。

## Minor 与定点复核

初审发现日常summary的两处提示仍引导裸 `--accept` 作为最终验收，launcher帮助和启动提示也沿用旧说法；实际正式入口需要 `--formal-window` 和四个显式离线路径，且机械满足退出3、A5另审。

审核者用内存合成上下文执行实际print_summary复现旧提示，随后经真实脚本入口执行裸accept，实际返回2，并输出固定历史身份未核实、A5未评估及缺formal-window错误。审计守卫未记录业务文件读取。这是用法提示脱节，没有造成正式通过或默认业务读取绕过。

`dffd1c5` 仅修改summary两个字符串和[runbook §4](../ops/demo-runbook.md#4-正式窗口机械预检离线材料历史真实性及-a5-另审)开头的说明：提示指向完整新用法，并明确exit3不等于A5；runbook标注旧launcher提示过时。受保护的 `scripts/demo_run.ps1` 字节不变。

同一审核者核对完整小diff、Git身份及源码，确认仅两个字符串值改变，无判据或退出码逻辑变更；原Minor已闭合，维持整合审核的Spec PASS / Quality PASS。作者现有summary定向测试记录为 **1 passed、42 deselected / 2.72s / exit0**；定点审核未重跑该测试或追加探针。

## 验证证据归属与保留条件

| 模块 | 审核者实读的作者验证 |
| --- | --- |
| H 时间诊断 | 120 passed / 12.28s / exit0，受测前后及提交身份一致 |
| P V2-A | 121 passed / 6.80s / exit0，受测前后及提交身份一致 |
| Ops 最终修复 | 168 passed / 282.21s / exit0，绑定五项修复后的固定树 |

本轮未重复模块套件或全仓。各模块原审核者的判断和探针分别保留在[H审核](2026-11-05-h-time-audit-review.md)、[P审核](2026-11-05-d61-v2a-kernel-review.md)、[Ops审核](2026-11-05-demo-formal-window-review.md)，不冒称由整合审核者重新复现。

尚未判断或验证：本批最终全仓和同提交CI、真实快照及来源/许可/非锁箱资格、完整D61统计研究、产品替换、实际部署与当前健康、正式窗口和实际演练结果、A5。审核者没有读取真实data/config/DB、账户、MT5、业务进程、任务、部署或保护状态，也没有派代理、填写批准字段或修改受审代码。

主会话须记录本结论后冻结最终提交，取得该提交自身的完整回归和main CI证据。技术PASS不授予真实输入、研究或Demo运行许可。
