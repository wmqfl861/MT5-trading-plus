# 模拟交易运行链路 Implementation Plan（2026-11-05）

> **For agentic workers:** Execute with subagent-driven-development in four separate worktrees; controller assigns independent module reviews and final integration review.

**Goal:** 补齐阻止H/P进入可验收模拟交易的实际软件接线，并以真实入口消费合成输入证明行为。真实数据、研究资格和部署前置分别保留，不把工程测试代替目标完成。

**Architecture:** 复用现有策略、成本、PaperBroker、RiskGate和台账契约。H新增用途限定只读主段预筛编排；P将字节验证与一次执行准入接到原计数内核；T78分为模型装载和号段风控。所有正路径用隔离合成工件/假网关；运行中的部署目录与配置不动。

**Tech Stack:** Python3.12、pandas、pytest、TOML/JSON；不增加第三方依赖。

**Spec:** [交接D62/T114/T121](../../handoff/2026-11-02-single-session-handoff.md)、[T78/D43/D47](2026-10-28-p-line-plan.md#t78风控号段子上限与-p-线实盘接入m阶段-f-结束后h-线会话负责)、[离线实现裁定](../../reviews/2026-11-05-demo-readiness-engineering-ruling.md)、[H范围契约](../../research/t114-main-scope-contract-2026-11-05.md)、[P准入契约](../../research/t121-production-admission-contract-2026-11-05.md)。

## Global Constraints

- Demo-only、0.01手；真实账户连接/下单禁止。只用合成价格、模型、费表、临时状态及FakeGateway，本批不连接真实MT5或读取真实data/config/ledger，不部署或改保护标记。
- 已获独立裁定允许P adapter及T78离线工程提前实现；实现仍须独立审核，裁定不批准任何真实release/数据/计数或阶段F部署。
- 冻结candidates.py/gate.py/power.py/tables.py/decision.py/level_decision.py/prob_mode.py、gates.toml、sampler/seed/strata、成交语义保持。新工程不自批、不填真实approved_by、拒绝默认零费用或自称approved元数据。
- 用户要求持续开发到可模拟交易；goal-progress中的E1–E8须实际证据闭合。无可行数据或研究结果如实阻断，不能放宽门槛制造成功。
- 不建通用权限、签名、交易或数据平台。不重造PaperBroker、取数器或研究引擎。必要共享计算可以窄提取，旧入口兼容须测试。
- 精确文件所有权；作者不派子代理/不推送，每个模块[H]/[P]/[OPS]提交，主会话集成。报告与路径只在ignored scratch，公开文件无真实账户/主机/绝对路径/配置。

## Review Focus

- 正路径必须真实消费模型/经过原计数内核/经过RiskGate和执行器，不能只mock helper得到成功。
- P授权检查在data/cost读取之前，一次消费跨重启/worktree；失败不重用；同批已验证bytes解析，避免TOCTOU。
- H主段和诊断输入隔离必须由实际bars证明，辅助预热不虚增年数；未知缺口/成本/N不静默补齐。
- 模型缺失、哈希/范围/身份错误在网关和运行状态写入之前拒绝，with_model返回的新实例被运行器实际使用，不fit。
- 分线暂停不能被对账自动清除覆盖；不能让子线暂停挡住账户500总上限处理，也不能令新magic或重启绕过。

### Task 1: H 主段只读预筛编排与入口

**Files:** Create `src/mt5quant/h_readonly_precheck.py`, `scripts/h_readonly_precheck.py`, `tests/test_h_readonly_precheck.py`, concise `docs/research/t114-readonly-precheck-2026-11-05.md`。只新增这四文件，不改冻结源码或既有bt入口。

**Interface:** `run_h_readonly_precheck(candidate, *, inputs, cost_model, contract_sizes, cumulative_n=None) -> dict`；inputs包含两个固定品种周期的bars/source_tags/显式coverage/missing_intervals，以及各自来源资格元数据。该版本通过显式合成输入工程测试；CLI没有默认数据根，未知production资格在读取价格工件前拒绝，输出状态不构成正式candidate pass。输入结构具体字段在模块说明固定，禁止额外泛化。

- [ ] 先写合成RED：主段/MT5诊断/后段HistData混合，修改诊断价格和未来辅助价格不会改变主段结果；缺口未知/源错误/错长度拒绝；UTC等价/触边/前月信号保留；无输入/台账写入，不能出现event_returns/labels/fit调用。
- [ ] 消费已有h_main_scope真实筛选bars：EURUSD/H1只保留HistData固定主段与合法月份，USTEC/D1保留显式来源和主段前40天预热，诊断不进入主段cost/ref/sigma/years。严格核对bars唯一有序、来源长度和显式coverage对应；缺口端点格式显式区分，不能直接拿legacygap当missing。将显式missing与过滤后实际邻bar构造runtime_coverage所需gap切段，利用现有runtime_coverage/merged_coverage_years口径；辅助年数只计算主段内部分。
- [ ] 预筛仅复用候选声明与成本/功效规则，输出文献/持仓检查、成本分量/参考价、每输入coverage、无条件窗口波动、单次功效及累计N下功效。固定N取调用者明确已核对元数据（正整数非bool）；缺N返回未知，不能用候选数/预算代替。通过/等待判定只为工程诊断，所有生产execution_status保持not_released。
- [ ] frozen candidates.precheck依赖磁盘load_bars/_contract_size，不准全局monkeypatch或改该文件；优先调用现有可用纯函数。若必须局部编排其已冻结公式，保持相同常量/数值规则且用合成完整无缺口输入对拍原precheck；不得复制整个precheck。成本spread通过source_costs仅external×1.5，commission/contract等显式，不能默认零；参考价取主段最后合法收盘。波动只取未跨声明缺口、且窗起点/端点均在主段的无条件窗口，不按候选信号挑选；与window_volatility无缺口结果对拍。
- [ ] CLI输入必须显式指定合成JSON/TOML和成本/规格；用标准现有成本模型解析，无自动读取真实库。拒绝production/self-approved模式，不写candidate precheck或SQLite；仅stdout或新独占输出目录记录结果和输入/规则/代码hash。实际读取同批bytes，未知无输出fake数值。真实数据未来接入及随机mtd_sign缺当月锚仍列阻断，不改sampler。
- [ ] 真CLI subprocess合成端到端证明范围、诊断扰动不变、缺N未知、输入不变/无ledger，聚焦tests/test_h_readonly_precheck.py和必要现有窗口纯函数兼容。精确[H]提交，报告运行命令/exit/受测tree。

### Task 2: P 字节准入与计数成本内核接线

**Files:** Create `src/mt5quant/prob/production_adapter.py`, `src/mt5quant/prob/production_releases.py`, `tests/test_t121_production_adapter.py`, concise `docs/research/t121-production-adapter-2026-11-05.md`; modify minimally `src/mt5quant/prob/feasibility.py` and `scripts/pline_feasibility.py`。静态信任文件为首轮审核后的必要修订，完整六条件见[独立裁定补充](../../reviews/2026-11-05-demo-readiness-engineering-ruling.md#实施中独立复核的设计修订)。不得改counting/gaps或冻结源。

**Interfaces:** `check_t121_admission(input_meta, cost_meta, review_binding)`保持纯声明/unverified/not_released；`verify_artifact_bytes(raw: bytes, expected_ref)`按明确摘要域验证后只解析原bytes；生产入口由 `release_id` 与审核manifest驱动。原run_feasibility和trigger_cost合成API保留拒绝production；可提取私有共用内核供受控路径消费，不改synthetic标签来绕门。

- [ ] 按修订后的独立裁定，固定静态生产快照初始空、未初始化，不宣称当前有效；调用者不能覆盖。manifest 绑定稳定实现 I、固定闭包和预定快照版本，登记 trust 后得到 H；验证 I→H 祖先与实际逐文件身份，manifest 不绑定最终 H/trust 字节。用途T121-count-cost、XAUUSD/EURUSD、H1→H1/H4、发现段截止2016-05-01半开，账户用途Demo/USD与scopeID明确，所有六组件均绑定。
- [ ] 定义明确版本清单并严格校验容器/类型/重复键/非有限/日期/摘要域：审核原记录hash、input/manifest/sixcost/sessions/code/rules身份，scope/observed/applies/dependencies/条件闭合/有效期、当前信任快照版本、一次execution_id。rawbytes与canonical摘要不混用。先核实可信完整快照并持久观察水位，再判release；新版revoked/unknown/expired/future仍保持水位，无效快照不推进，同版本冲突及旧版拒绝。
- [ ] 共享锁内重新验证当前快照、水位、release、manifest、review和条件。耗时实现/Git核验完成后紧邻claim重新取得可信时刻；claim持久化完成后再次复核时效，已到期则保留claim、拒绝data/cost/内核。跨进程/重启/worktree共享固定用户状态根，禁止data/output/env改变；失败不回收。revoked/unknown不读任何材料、不建claim或输出，重复active允许读取绑定metadata以取得execution_id但不得读data/cost。真实生产CLI不暴露私有fixture开关。
- [ ] 绑定与范围通过后只读一次artifact bytes并核验，parse同bytes而不重读路径；内核不fit/labels/returns，原计算规则/96rows8summaries保持。缺成本/无资格依原规则等待，获准也不保证feasible。输出标明release/context/执行身份与完整hash，不包含原账户或本机路径。
- [ ] 新CLI模式与旧synthetic模式显式互斥；缺mode新参数时旧命令行为保持。有效合成正链必须真实临时Git完成I→封印manifest/review→仅提交trust→新解释器正常loader/main/原内核，96/8输出且已知非零成本；不patch信任/Git身份/准入/claim判定。覆盖旧负例及新版拒绝后回滚、版本冲突、同步撤回先于claim、闭包/未登记trust变化、核验和持久化期间跨过到期点、失败claim不回收。冷import guard禁止标签/拟合/DB研究/MT5。
- [ ] 本轮不添加真实release、不读真实工件、不放行2026摘要或charged新口径。聚焦新增+旧T121测试，完整命令/exit和固定tree后[P]提交。

### Task 3: T78 冻结模型进入运行器

**Files:** Modify `src/mt5live/config.py`, `scripts/live_runner.py`; create `tests/test_t78_model_loading.py`, concise `docs/ops/t78-model-loading-2026-11-05.md`。必要旧集成测试调整限其模型启动场景，先报告。不得修改冻结artifact/level_decision/table/gate或执行器。

**Interfaces:** LiveConfig新增可选model_path/model_sha256（成对，64位hex）；有模型的启动还须card_path/experiment_sha256绑定现有卡和模型身份。`load_live_strategy(cfg)`复用artifact.load_model→严格身份/范围校验→接收with_model返回新实例→validate_live_strategy；main在guarded_main前完成模型预检，将实例传入_run，不重新实例化、不fit。

- [ ] 写RED证明真实fittable策略在runner.main能使用冻结合成模型而不是ModelMissingError；两份有效模型实际生成不同signal/开仓结果，禁止mock generate_signals；fit一旦调用即失败。无模型、错hash/类型/坏JSON/身份缺失或错配、卡与symbol/timeframe/params/bracket/allowed集合/训练窗purge/experimenthash不符，在网关/锁/heartbeat/intent/paperstate之前返回2，原状态不变。
- [ ] model/card相对路径以配置文件目录解析，与cwd/data-dir无关。旧H无模型字段逐字段默认行为保持；非fittable模型字段拒绝或明确不使用，禁止静默加载无关model。沿用artifact原始bytes哈希和已有模型identity，不造新modelID；严格拒绝旧缺省跳过的关键身份，必要校验仅在新live预检，不改冻结策略兼容。
- [ ] 模型hash匹配只证明身份，不是研究批准。真实P在线运行仍未获许可：在main将未获独立release的fittable在线启动明确拒绝（生产release初始没有批准模型）；合成正路径用私有fakefixture/纯offline paper，不暴露--approved类开关、不把模型自填字段当许可。说明后续研究/阶段F/维护窗口仍前置，保留原Demo保险丝/0.01规则。若现有可信模型发布资格机制可复用，先按代码确认；没有则生产保持未发布，不造权限平台。
- [ ] test runner.main走真实装配+PaperBroker/FakeGateway，各config/state在tmp；核对模型实际被消费、with_model新实例传递、未fit、错误零副作用、旧H信号/配置兼容和time_stop/week_close拒绝保持。聚焦新测试+必要旧config/runner测试，保存日志/exit/tree，[OPS]提交。

### Task 4: T78 H/P 号段日亏子上限

**Files:** Modify `src/mt5live/riskproc.py`, `src/mt5live/gate.py`, minimally `src/mt5live/reconcile.py`; create `tests/test_t78_line_caps.py`, `docs/ops/t78-line-caps-2026-11-05.md`。独立首轮审核发现paper反序列化将bool等非法magic转成整数，修复范围因此增加`src/mt5live/paper.py`的两个原始magic校验点，成交算法不变。不得修改model任务config.py/live_runner或其他代理测试。真实config/risk*.toml与部署不改，新配置只在合成fixture与公开示例中给出。

**Interfaces:** RiskConfig可选`line_caps: tuple[LineCap,...]=()`，LineCap包含name/magic_min/magic_max/daily_loss_cap_usd；TOML可选[[line_caps]]。新T78示例H902000–902999/P903000–903999各250USD，账户500不变。旧无line_caps配置维持原语义；重叠/重复name/非法magic/非有限或非正cap/bool类型拒绝。

- [ ] 先RED：H损益≤-250只阻H开仓、P继续；反之同理，恰边界包含。账户≤-500仍全平+halt且不能被线pause优先短路；平仓/反手平仓腿不受线cap阻挡。沿daily_pnl UTC日，deals交易profit+commission+swap+fee和positions浮动profit+swap按magic号段聚合，不计出入金、不重算其他币种。
- [ ] RiskGate从同次账户snapshot构造全账户与分线损益；RiskProcess定时周期和同步check_order都实施linecap，分线快照缺失/NaN/异常magic不放行配置线；没有新cap的旧调用保持兼容。不能仅写pause而闸门不检查，也不能仅闸门算而monitor没有后果。新注册magic/重启也不能在当天绕过已触发cap。
- [ ] T78要求pause-<magic>.json reason=line_cap和告警；独立线状态先持久锁存，再处理兼容pause，覆盖后来加入magic。人工/未知/损坏pause保持；新的对账mismatch使用独立reconcile-block，即使人工pause=false也拒开，对账一致仅清自身记录。Gate在共同pause路径锁内一致读取两份状态，锁超时或读失败拒开，判断/告警在锁外且不持锁跨账户/报价/下单调用。
- [ ] 日界策略明确保守：UTC当日触发保持到下一UTC日，即使浮亏恢复仍不自动开；隔日只解除自身可证明归属的上一日linecap，其他pause保持，坏状态/未来日期不自动清。无全平/全局halt后果由子cap生成；账户全平路径始终保留。只在合成目录测试文件后果，真实state不碰。
- [ ] 真实RiskProcess→RiskGate→LiveExecutor/PaperBroker+reconcile合成链验收：两线同时存在，触单线cap不平仓/不影响另线、人工/对账暂停同时存在不被覆盖、对账一致不解除线pause、重启/新magic、隔日恢复所有权、账户cap全平仍有效、两次flatten编号旧测试不变。只跑新测试及直接相关risk/gate/reconcile回归；记录RED/GREEN命令exit/tree，[OPS]提交。
