# H 真实输入装配与主段预筛 Implementation Plan

> 单任务；子代理开发，独立聚焦评审后才能将运行产物交 A1。

**Goal:** 把本机已确认的真实 HistData/FRED/成本/规格文件装配为 h_readonly_precheck 的显式输入，新增 real 资格模式，实际运行主段 [2007-01-01,2021-05-17) 预筛并产出结果 JSON（provisional，评审通过后才可交 A1）。

**依据:** 独立范围裁定 conditional approved（2026-11-06，四项 A–D + 前置条件），全文见 `.superpowers/h-real-precheck-scope-request.md` 对应审核者返回（主会话转录存 `.superpowers/sdd/2026-11-06-h-real-precheck/scope-review.md`）。既有已审模块：h_readonly_precheck（main d29fff7，synthetic_only 门）。

## Global Constraints

- synthetic 模式与 synthetic_only 门**逐字节不变**；旧测试全部保持通过。
- real 模式在打开任何价格工件前完成整体资格校验；绑定文件按下面 SHA256 白名单**路径+哈希双绑定**，任一不匹配 fail-closed。
- EURUSD 只读 time+close；USTEC 只读 time+close；MT5 序列不进入输入。价格列必须先过滤主段/信号窗再进入计算。
- 预筛产物恒 `research_status=not_evaluated`、`execution_status=not_released`；不开 R2、不填 approved_by、不动 sampler/seed/strata、不触发真实事件收益/标签。
- 运行期不连 MT5/网络；独占输出目录；不写 candidate precheck/SQLite/ledger；不修改冻结研究源与运行中 Demo。
- 缺锚 fail-closed 不变。本任务不计算 FRED/MT5 重叠月符号一致率（T114 另行送审项）。

## 绑定文件（SHA256 白名单，数据根=data_research/）

| 文件 | SHA256 |
| --- | --- |
| ext/histdata/EURUSD/H1/data.parquet | 10543bb6afa0c14ef033e1b719233c570830af7911a617519681e91dd0ec1863 |
| ext/histdata/EURUSD/H1/manifest.json | 4e0cc51be2e0b257a8e31ac14cce2ca4d9320d12b9eccf2b5b14e712c5ac78b3 |
| ext/fred/USTEC/D1/data.parquet | c6645256e2cf2ceb10474318c2e5f926a5157cc0c9d50027e55f1482200a71c6 |
| ext/fred/USTEC/D1/manifest.json | 08ff3dff0f48330e48832c06eb21bbf8b7564f81616d5783cd183ce31311c5fe |
| costs/calibrated.json | e2cae60cf76e70f0845933863ac552c1ec379373e1b73302625c883bfa267c4c |
| costs/spread_model.json | c35550b90fbefef0d61b6c767e9e5365d6480cffaee232a68da696edb667ce85 |
| specs/latest.json | 144bb2245479e3557c571c9f5f83ca92db9d269211da7845c9d5aa2467d3150f |

## Task 1: 装配器 + real 资格模式 + 真实预筛运行

**Files:** Create `scripts/h_assemble_real_precheck.py`、`tests/test_h_real_precheck.py`、`docs/research/h-real-precheck-2026-11-06.md`；Modify `src/mt5quant/h_readonly_precheck.py`、`scripts/h_readonly_precheck.py`（仅新增 real 分支）。

**Interfaces:**
- `assemble_real_inputs(data_root: Path, *, verify_hash) -> dict`：产出 run_h_readonly_precheck 的 inputs 结构 + 成本/规格 + provenance。
- 预筛 CLI 新增 `--mode real`（或装配器直接调用 API 后写结果；实现者按现有 CLI 风格选最小方案，报告说明）。

### 实施要点

- [ ] **资格门**：先校验白名单（路径+SHA256），再读 manifest 逐年 used；触到的每个自然年 used=true 才继续。2019（used=true/inconsistent+月度回收+锚点consistent）纳入；产物 provenance 登记 2019 处置与 2019-10 空洞显式登记（自动切段处理，不手工加剔）。
- [ ] **装配 EURUSD**（histdata H1）：读 time+close（pyarrow 列投影；单句柄）；过滤 [2007-01-01,2021-05-17) 后进入一切计算；source_tags 全 "histdata"；coverage 由 manifest 声明段转换；used 门通过年内的**违例月**由冻结周日开盘规则（merge 纯函数）自检剔除——不自行加剔。
- [ ] **装配 USTEC**（fred D1）：窗口 [2006-11-22,2021-05-17)（40天预热+主段）；date 标签；tags 全 "fred"。
- [ ] **缺口推导（预注册规则，写入产物）**：EURUSD——每个 UTC 周一00:00→周五收盘带内，相邻bar的小时网格洞记缺失 [首根缺失bar开盘, 下一根既有bar开盘)；周末构造性排除；周内无日历工件的假日保守记缺失（工程保守口径，只减覆盖）。USTEC D1——工作日网格同理（日粒度）。2019-10 空洞须出现在结果 [2019-10-01T00:00,2019-11-01T00:00)。规则判空才传 []。
- [ ] **成本**：commission=calibrated EURUSD round_trip 0.0（实测，记录 deals 8/12、0.13手）；swap=规格快照 long −5.9/short 0.0 绝对USD/手/晚——产物标注"规格快照、非成交实测"；spread=spread_model.json 标准结构（相对/情景基准，施于2007–2021属情景，产物记录基准来源与统计量）；contract=100000（specs）。commission/swap 绝对域（无 ref_price/contract 相对基准）。
- [ ] **运行**：真实预筛一次（cumulative_n=None——单次功效门不依赖N；alpha/N 功效报告 unknown），结果 JSON + provenance 写独占输出目录。产物为 provisional（评审通过前不交 A1）。
- [ ] **测试（合成副本，不绑真实路径跑单测）**：白名单哈希不匹配拒绝；未绑定路径拒绝；used=false 年拒绝；synthetic/real 输入不可互串；synthetic 旧套件全绿；缺口推导规则单测（构造含周末/假日洞/31天洞的合成序列断言输出区间）。真实文件的装配仅运行一次并留证（测试用临时合成 parquet 副本，schema 同构）。
- [ ] 记录 RED→GREEN、受测 tree、精确 [H] 提交、clean。日志 `.superpowers/sdd/2026-11-06-h-real-precheck/`。
- [ ] 完成后主会话派聚焦独立评审（real 模式差异 + 装配器 + 真实运行产物），评审通过后产物才可交 A1。

## Self-check

范围裁定 A(1-4)/B/C(1-4)/D 与前置条件 1-5 已逐条映射到上述要点；swap 已按"规格快照"标注。文件所有权与在制任务无冲突。
