# H/P 输入前置 Implementation Plan（2026-11-05）

> **For agentic workers:** Use subagent-driven-development. H/P use separate worktrees and independent task reviewers; shared integration is controller-owned.

**Goal:** 补齐T114主段范围的离线元数据契约，并准备T121生产准入的版本化送审契约。

**Architecture:** H新增一个标准库纯函数模块，不连接数据层或既有预筛CLI。其结果仅证明显式覆盖/缺失区间的机械裁切，不证明数据获准或事件可运行。P仅新增准入文档，保留现有synthetic-only入口。两任务无共享产品文件，最终状态文档由主会话更新。

**Tech Stack:** Python 3.12 stdlib、pytest、Markdown，无新增依赖。

**Spec:** [T114准备说明](../../research/t114-r2-draft-2026-11-03.md) §未决证据/草案提升；[T121入口](../../research/t121-offline-entry-2026-11-03.md) §后续真实筛查；[T121独立复核](../../reviews/2026-11-04-t121-entry-review.md) §保留条件；[交接D62](../../handoff/2026-11-02-single-session-handoff.md)。

## Global Constraints

- “主会话与开发代理不得自我批准，不得填写 `approved_by`。”
- “研究运行必须属于已批准、已开启的轮次。”
- “不在真实事件或真实触发上计算任何收益或结局来‘看看’。”
- 不读取真实data/价格/成交/SQLite/验证/锁箱；不连接MT5、运行预筛/研究/拟合/模拟/部署或操作业务进程。仅合成输入与静态文档。
- 不修改既有预筛CLI、正式候选/卡/轮次、冻结源码/门槛/成交语义、sampler、实盘与配置。生产入口仍 `not_released` / `waiting_data`，R2不开，旧P2-10 RED和T92阻断保留。
- 不新增通用平台，不扩池、采集、下载或新数据用途；一个目的限定模块/一份草案即可。公开内容不含账户、主机、本机路径、令牌或原始数据。
- 不重做90fbc88已交付四模块。原worktree/部署目录不操作。主会话唯一推送，各作者不得派子代理。

## Review Focus

1. 缺口两侧存在bar的旧 `merge.gap_spans` 与显式缺失区间不同，不可隐式互转；未知缺口证据不可等同空列表。
2. naive UTC、Z与等价时区表示、边界接触及重叠输入必须确定性处理，输入不能被修改。
3. 交易主段只使用HistData；信号覆盖不按交易起点截掉前月锚点。范围有效不表示连续覆盖/足够样本/合法未来窗口或入轮许可。
4. metadata/hash不建立审核授权；独立审核记录与执行许可必须分别绑定，调用者自填字段不能自动放行。
5. 旧2026成本摘要用途拒绝、Demo free的单项适用性、charged费率格式限制不能被新契约草案改写成已批准。

### Task 1: H 主段覆盖元数据校验

**Files:** Create `src/mt5quant/h_main_scope.py`, `tests/test_h_main_scope.py`, `docs/research/t114-main-scope-contract-2026-11-05.md`。

**Interfaces:** 标准库不可变记录 `TimeInterval(start, end)`、`CoverageSegment(symbol, timeframe, source, start, end)`、`ScopeReport`；入口 `validate_h_main_scope(*, coverage, missing_intervals) -> ScopeReport`。只接受显式元数据，不收文件/数据根/收益/bars/ledger参数。放在mt5quant根以避免validation/research包初始化引入执行器或台账依赖，不改包初始化。

- [ ] 基于已给T114说明与merge.py头部契约，写有意义合成RED测试。不得广扫旧研究或读取真库。
- [ ] 固定主段 `[2007-01-01T00:00:00Z,2021-05-17T00:00:00Z)`、交易对EURUSD/H1、信号对USTEC/D1；此版本不支持调用者改区间/品种/时区/阈值。时间输入支持datetime/ISO字符串，naive按UTC，输出规范UTC-aware；拒绝无效/反向/零长/非时间数值，禁止模糊猜测。
- [ ] coverage为CoverageSegment序列；canonical source为小写 `histdata`、`mt5`、`fred`，具体有效配对仅EURUSD/H1 histdata或mt5、USTEC/D1 fred或mt5。来源缺失/未知、错误类型/额外输入对、保留的信号不同来源重叠均明确拒绝（ValueError/TypeError），不推定批准或来源优先级。相同来源的相邻/重复/重叠区间可规范为并集；输入顺序/重复不影响结果。
- [ ] missing_intervals按固定品种周期对映射，值为TimeInterval序列。每对都须显式给出；缺key、None或整体None返回waiting_data及稳定原因，空序列才表示调用者声明“已核对且无缺失”。区间明确为缺失时间的[start,end)，不是旧gap_spans两侧bar端点；不自动检测30天、不猜bar周期换算、不因未给空洞证据走成功路径。
- [ ] 交易先只取histdata，与固定主段求交、扣显式missing并规范并集。mt5交易与主段外histdata片段返回excluded及原因；空洞重叠相邻去重，输出不重计。USTEC的合法fred/mt5覆盖单独扣自身missing且保留主段前锚点/主段外元数据，不裁到交易start；不读取或推断锚价。信号没有任何主段内重叠，或交易扣洞后空，须waiting_data。该存在性检查不证明整个主段覆盖。
- [ ] ScopeReport包含status（metadata_valid/waiting_data）、规范交易/信号区间、excluded、稳定排序原因和恒定execution_status=not_released；waiting_data不输出可用主段区间，防调用方误用局部结果。合法范围裁切只给metadata_valid，不产生candidate pass/power/years/N/样本/成本/收益，也不写文件或加载输入。声明未知证据是真实性前置，不能因schema有效认定真实批准。
- [ ] 测试至少覆盖：主段/MT5/后段混合、左闭右开边界/触边空交、相邻/重复/交叉空洞扣除、未知vs已知空缺口、来源与输入对错误、信号前月保留/独立缺口/无主段重叠、UTC等价/无效时间、输入深度不变/结果不可变、冷导入不加载研究/引擎/数据模块和调用无文件I/O。无需新增镜像测试或修改旧测试。
- [ ] 聚焦pytest仅新文件，保存RED/GREEN完整命令/exit/受测版本；代码与文档说明legacy转换、事件范围/年数/D33/日历未完成。精确[H]提交3文件、clean，供独立规格/质量审核。若接口设计实际需更大范围，先报告具体原因，不自行扩展。

### Task 2: P 生产准入契约送审草案

**Files:** Create only `docs/research/t121-production-admission-contract-2026-11-05.md`。

**Interfaces:** 拟议纯内存 `check_t121_admission(input_meta, cost_meta, review_binding)` 仅在文档中定义输入/返回规则；本批无实现、无真实准入或计算，执行状态恒为not_released。该稿不是又一个已完成的synthetic schema检查器。

- [ ] 定点读现有T121入口及复核保留条件、A2/A3用途限制和D50；不超过所需片段，不读真实工件。
- [ ] 给出有版本身份的最小元数据契约：独立审核记录绑定提交/文件SHA、用途、明确品种/周期/源manifest与发现段范围；成本组件各自的工件身份、来源/观测期/适用期/账户用途/币种/费率口径。未知字段保留未知，实际成本数值不提供。
- [ ] 分开内容身份、独立审核有效性与一次执行许可。调用者给出的review_binding/approved字符串/文件名/hash不证明授权；须由已明确的可信协调路径核验原审核记录、范围与条件。设计中写明来源验证和撤回/条件未闭合的处理仍需独立决定，不新造签名服务或通用权限系统。
- [ ] 新契约不接受synthetic改标production，不把现有2026摘要转为允许用途；缺commission/contract/overshoot或合法spread/slippage仍blocked。Demo/USD free仅原账户范围，不能放行完整成本；charged符号/正费率限制的生产适配列未决而非批准。
- [ ] 定义返回缺项/冲突/身份绑定结果，任何完整材料也不能触发加载/计数/成本/label/fit/台账，所有执行仍not_released。结构可校验和研究放行是不同步骤；生产适配、真实证据审核与执行许可须分别完成。
- [ ] 预写有限未来合成验收：缺用途/范围/hash，内容篡改，过期/范围不符/条件未闭合审核，跨品种/账户错绑，自填批准字段，完整资料仍无副作用，临界截止与字段类型冲突。只写验收约定，本批不执行不生成新系统。
- [ ] 标明本稿未批准、未实现、未运行，所需独立决定（可接受审核绑定、D50来源/适用期/成本口径），保留T122/P1/P2/D60/D61阻塞。静态链接/内部一致性/diff检查，精确[P]提交单文档。交独立规格/设计质量审核，允许归档不等批准真实读取。
