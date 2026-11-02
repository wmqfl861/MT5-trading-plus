# T112 H-only 源码冻结边界独立治理审核

日期：2026-11-02。结论：**conditional（条件性，尚未生效）**。

审核角色：受用户本次明确委派的独立审核子代理，非 T112 开发者。本报告仅裁定 T112 的 H-only `src/mt5quant/research/candidates.py` 扩展触发 P 全文件冻结哈希的处置；不构成整个 T112 模块批准，也不构成立即修改冻结哈希、批准 P 方法或开启研究轮次的许可。

## 1. 授权来源、恢复范围与版本身份

授权来源为用户本次审核委派：“审核交独立子代理”“审核结论须落 docs/reviews 后才能据此实施”，并明确要求独立裁定 T112 H-only 源码基线处置。用户后续明确“继续开发啊”，恢复不依赖 Demo 差异的离线开发与审核，并要求将已完成的 conditional 正文落入本报告。未援引或虚构任何外部批准。

本报告沿用此前已完成的定点静态核验，不重复扫描或运行产品验证。Demo 持仓差异的原因仍未解释，实际 Demo 恢复仍受阻断；离线报告恢复不构成运行时恢复许可。本审核未调查或裁定该运维差异，也不以该差异干预本次源码冻结边界结论。三份蓝图／交接 D62 草稿尚待精确 diff 后另行审核，不在本报告覆盖范围内。

本次恢复委派明确的版本隔离事实如下：

- 当前远端 `main` 为 `f6c7b8112a73f111b3553f5413521fc75940a81e`（简称 `f6c7b81`）。
- 本地 Demo 运行目录的 `main` 保持 `2c3dd330404019f2b9382c055c76b1ce28f8e221`（简称 `2c3dd33`），运行目录与离线开发隔离。
- 此前静态核验取得的审核工作树及请求来源工作树 HEAD 均为 `f6c7b81`，本地 `main` 引用为 `2c3dd33`。这是分支引用和运行版本隔离，不是同一提交的源码内容不一致；不得将其表述为补丁漂移证据。

本报告以不可变提交 `f6c7b81` 为比较基点。远端与运行目录的当前身份澄清来自本次恢复委派；本次报告落盘不重新访问远端，不声称已核验任何后续提交。

## 2. 审核材料与证据边界

此前定点读取材料包括：

- `.superpowers/single-session-handoff/t112-frozen-source-review-request.md`；
- 暂停工作树相对 `f6c7b81` 的完整 `src/mt5quant/research/candidates.py` diff；
- 基点的 `src/mt5quant/research/gate.py`、`tests/test_research_gate.py`、`docs/research/research-gate.md`；
- `docs/handoff/2026-11-02-single-session-handoff.md` 的 T112 要求、提交分类及停止规则；
- 与 P 数值和冻结登记直接相关的源码定义及下述文件比较。

核验方式为只读 Git 对比、源码文本解析及 SHA-256 计算。未导入产品模块，未读取真实 data，未运行研究、标签、收益计算或测试套件，未连接 MT5，未派遣其他子代理。最后一次静态复核的断言全部通过，审核分支当时位于 `f6c7b81` 且工作树干净。

请求材料列出的七项 `tests/test_research_gate.py` 失败是执行侧已有复现记录，本审核未复跑，不能作为本审核新产生的测试结果：

1. `test_insufficient_data_when_adopted_but_inputs_missing`；
2. `test_insufficient_data_uncalibrated_and_lower_bound[0.01]`；
3. `test_insufficient_data_uncalibrated_and_lower_bound[0.025]`；
4. `test_pass_only_when_adopted_calibrated_and_days_met`；
5. `test_gate_boundary_is_sample_size_evidence_boundary`；
6. `test_gate_result_carries_acceptance_snapshot`；
7. `test_acceptance_snapshot_source_hashes`。

## 3. 已独立核实的静态证据

### 3.1 全文件身份与变化范围

`gate.py` 的 `EXPECTED_ACCEPTANCE_SOURCE_SHA256` 登记了整个 `candidates.py`，不是仅登记四个 P 函数。按门禁相同的 UTF-8／LF 归一规则计算，结果如下。

| 对象 | SHA-256 | 状态 |
| --- | --- | --- |
| 基点 `f6c7b81` 的 `candidates.py` | `7af9e89c8fa07ca763ffddcb9ae38fdf52416610fe50b3ee3e0a18238abc5794` | 与旧冻结登记一致 |
| 已检查的暂停补丁 `candidates.py` | `c9236b22ce90b963178df22e7ab4195093b16362a06a4ef02ec3c60cbc32036c` | 已核实当前暂停快照；仅为待最终代码复核的新哈希候选值，未批准登记 |

基点 Git blob 为 `f288f1970a8b70775bb74e7e04a230c83ac0926a`；暂停文件按 Git 路径规则计算的 blob 为 `62cfd945265eee59b1b5bf1253b52b2866323357`。Git blob 身份与门禁的 LF 归一 SHA-256 是不同标识，不相互替代。

完整文件 diff 与顶层定义源码比较显示：修改 `Declared`、`_DECLARED_KEYS`、`_validate_declared`；新增 `_DECLARED_REQUIRED`、`runtime_coverage`、`require_runtime_coverage`；未删除顶层定义。新增声明字段为 `signal_symbol`、`signal_timeframe`、`side_rule`。

新哈希绑定的只是已核实暂停快照。最终代码有任何修订，均须重新计算并更新证据，不得直接沿用该候选哈希。

### 3.2 四个 P 函数全文逐字一致

按源码行号提取完整函数文本，包含函数内部注释与文档字符串并保留末行换行，LF 归一后逐字比较。下列四函数两端全文相同；不是仅证明 AST 结构相同。

| 函数 | 基点与暂停补丁相同的函数文本 SHA-256 |
| --- | --- |
| `precheck_prob` | `b7f7f8de6f6a04561d486e32bb6b7d34ed67055332a4b590fc0c61e8b259f3b1` |
| `sample_size_evidence` | `3a5f7a8a71f8b1297b5ecf3e1453215820b9f8a161c522e6b18ba6fd6ffe0af7` |
| `_sample_size_numbers` | `bc813ead0bb3473bff396dd4c95a6a38c5f5834affff2caf6999b7de67ae88fb` |
| `measured_n_required` | `a9cf44b060a6475573a29faa62694218aec5dcc6d138b93095c4d1aa989ef853` |

### 3.3 其他冻结源、依赖与登记

其余四个冻结源文件均与基点 LF 归一全文一致，且各自 SHA-256 符合原冻结登记。

| 文件 | 基点与暂停工作树相同的 SHA-256 |
| --- | --- |
| `src/mt5quant/strategies/library/prob/level_decision.py` | `b81b1b39e8a8d809f1e567c309db342606d944eca02bfff866806ceb4ddbabc8` |
| `src/mt5quant/prob/tables.py` | `52248e1e8b60f2638672605c7edf10551d3bb522d7744ebf336dbe026bc0ccc7` |
| `src/mt5quant/prob/decision.py` | `705a7e05942bbc9971705d20c5ed95e9041fe40c0d3ac104cb947684c5842214` |
| `src/mt5quant/validation/prob_mode.py` | `30da71bda3281fad0aa281a16c826cca0964e52dd69d4d301a6e28b1bd0e7719` |

另已核实 `src/mt5quant/research/gate.py`、`src/mt5quant/research/power.py`、`tests/test_research_gate.py`、`config/gates.toml` 与基点全文一致。

已检查的 P 数值定义保持不变：`_PROB_BASE_RATE=0.40`、`_POWER_ALPHA=0.05`、`_POWER_MIN=0.5`、`_POWER_N_TESTS=1`，以及实测锚点 `((0.02, 4000.0), (0.03, 2000.0), (0.05, 1000.0))`。证据三态定义、`_COST_MULTIPLE_MIN=3.0`、`_DAYS_PER_YEAR=365.25` 同样未变。冻结验收常量仍为 `lb_z=1.0`、`threshold_mult=2.0`、`min_cell=100`、`prior_strength=100`。

`METHOD_REGISTRY` 与 `PRIMARY_METHOD` 所在文件全文未变；三方法登记如下，`PRIMARY_METHOD` 仍为 `month-within`。

| 方法 | status | adopted |
| --- | --- | --- |
| `month-within` | `failed_cluster_control` | `False` |
| `quarter` | `not_adopted` | `False` |
| `cluster-permutation-v1` | `not_adopted_two_stage_cp` | `False` |

这些证据证明已检查快照中的源码身份和数值定义未改，不等于已执行数值等价回归，也不证明未检查调用方的行为正确。

## 4. 独立裁定与理由

裁定为 **conditional**：可以为完成独立审查的最终 T112 H-only 补丁准备一次限定源码基线更新；第 5 节全部条件满足前，该处置不得生效或合入。不得将本报告表述为 `approved` 的 T112 模块审核，也不得表述为立即重锚许可。

支持限定更新的理由是：handoff T112 明确要求候选声明扩展及运行前覆盖一致性检查，已检查差异与该 H 线目标相关；四个 P 函数、相关常量及已检查依赖没有变化。因此，存在通过完整证明和独立审查，更新共享文件源码身份而不改变 P 判定口径的合理路径。

保持条件性而不直接批准的理由是：`gate.py` 明确规定源文件改变即阻断，即使数值常量未变；现有门禁测试与 `docs/research/research-gate.md` 保留漂移阻断原则。旧基线下的源码漂移符合保护设计，不能据此弱化断言、跳过测试或直接刷新哈希。

四个函数全文相同只构成当前快照的局部证据。共享装载器可同时读取 `[declared]` 和 `[prob]`，新增声明字段、覆盖函数及调用方的兼容性仍须完整模块审核。特别是新增 `signal_timeframe` 的必要性、适用范围和接口约束，应由 T112 模块审查明确；本报告不预先批准该设计。实际覆盖、信号驱动方向、随机化事件规则、数据身份等 T112 验收也不在本次裁定中完成。

## 5. 生效条件与后续验证

以下条件必须全部闭合，并形成可追溯证据。

1. **审核记录先落盘。** 本报告必须先进入 `docs/reviews`，实施与后续审核引用其提交身份。报告落盘只是必要条件，不单独使重锚生效；不得填写轮次 `approved_by` 或将用户审核委派替代具体方法批准。
2. **绑定基点到最终代码的完整证明。** 记录明确基点、最终代码提交、完整文件 diff、旧哈希及最终新哈希。重新核验四个 P 函数全文、相关数值／全局定义、依赖、方法登记以及全部冻结源文件。必须证明 `f6c7b81` 到最终代码的整体变化，不能只证明暂停快照到最终版本的增量。如合入基点发生变化，应把该变化纳入证明并复核。最终文件若不再对应本报告候选新哈希，须更新哈希与审核证据，不能自动继承本次条件性结论。
3. **完成独立 T112 模块代码审查。** 检查共享装载接口、H/P 调用边界、旧候选兼容性、新增字段及覆盖函数，并确认 H 扩展未间接改变 P 行为。完整模块审核结论须另落 `docs/reviews`，所有阻塞问题闭合。本报告不能代替该模块审核。
4. **保持 P 数值与方法登记。** 以合成回归证明样本量边界、证据三态、解析与实测取严规则保持一致，包括未标定和仅下界档不因日数增加获准通过、已标定档的边界行为保持。`METHOD_REGISTRY`、`PRIMARY_METHOD`、冻结验收常量保持不变，现有三方法不得因重锚获得 PASS。现有数值自检和样本量／门禁边界回归必须保留。
5. **全文件冻结及真实漂移负测试保持有效。** 不缩小冻结覆盖范围。重锚后，在隔离测试环境制造实际 `candidates.py` 源码文本变化，保持数值常量不变，并采用仅用于测试的已采纳方法与充分样本输入，验证该文件进入 `source_drift` 且 `research_gate` 返回 FAIL。正常重锚快照应无漂移，其他冻结源的漂移保护仍须有效。仅伪造快照返回值或改变预期哈希的测试不足以覆盖该要求；负测试结束后不得遗留源码或登记篡改。
6. **独立 `[S]` 提交、验证闭合后合入。** 最终代码及重锚前置证明经独立审查确认后，由执行方准备单独的 `[S]` 冻结基线更新提交，明确关联本报告、最终代码身份、旧／新哈希及验证依据。不得把阈值、方法登记或估计器变更夹入该提交，也不得先改哈希使原七项失败变绿后倒推批准。保留原门禁断言，完成相关合成测试、漂移负测试、全量回归及 CI；最终模块审核、完整证明与验证全部闭合后才可合入并使该次重锚生效。

合成验证不授权读取真实数据或运行研究。条件未闭合时，源码冻结冲突继续按原规则阻断。

## 6. 允许下一步与硬边界

允许继续不依赖 Demo 差异的离线开发和独立审核，围绕最终代码补齐上述证明、模块审查和合成负测试，再按条件准备共享源码基线更新。此前暂停快照的静态证据可以引用，但不得冒充最终版本的新验证结果。

本报告不授权以下事项：

- 将全文件哈希改为函数级哈希、移除冻结文件、自动接受当前文件哈希，或跳过／弱化漂移测试；
- 改变样本量数值、验收阈值、估计器语义、方法批准状态、`config/gates.toml` 或研究批准记录；
- 以本裁定批准整个 T112、P1／P2 方法、研究轮次或任何 `approved_by` 字段；
- 读取真实 data、运行真实研究／标签／收益计算、连接 MT5，或解除 Demo 运行时恢复阻断；
- 将本报告推广为以后所有 H-only 同文件修改的自动重锚例外。

本次交付仅为单文件 `[DOC]` 独立裁定记录。产品源码、冻结哈希和测试均未由审核者修改；报告提交后仍保持 **conditional，待最终 code review、完整 proof、negative test 及其余生效条件闭合**。
