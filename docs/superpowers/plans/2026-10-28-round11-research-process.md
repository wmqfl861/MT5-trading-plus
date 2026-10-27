# 第十一轮施工计划（2026-10-28）：研究体系重建（阶段 R）

> **For agentic workers:** 按"执行顺序"逐任务推进。每个任务的流程：
> 1. 先写失败测试；
> 2. 实现；
> 3. 本任务测试通过；
> 4. **全量回归**；
> 5. 提交并推送；
> 6. **确认 GitHub Actions 为绿**（红了先修，不推新功能）；
> 7. 勾选复选框，并**在同一行写上证据**（测试名、报告路径或日志路径）。
>
> 没有证据的复选框一律不勾。

**Goal:** 把研究循环的入口从"直接开卡"改为一套有据可查的选题与评估体系：
- 基线重新定位（标定样品）、v2 卡撤回；
- 候选登记与预筛、研究轮次预注册、项目级试验计数；
- 关卡 v3（用正负对照标定整体误报率与检出率）、低频事件模式；
- 外部数据延长研究段；
- 首轮候选登记与 R1 预注册草案，交用户批准。

同时处理第十一轮审核的 F1–F8。

**输入文档（先读）：**
1. `docs/reviews/2026-10-27-review-round11.md`：实现复核（F1–F8）、基线选择意见的核对、修改方案摘要
2. `docs/blueprint.md`（v4）：§2 当前禁止事项、§4 铁律 16–19、§6 研究方向 v4、附录 A/C/D/E 模板
3. `docs/reviews/probes/2026-10-27-round11-power.py`：检验力速查（T62 的检验力函数须与它的输出一致）
4. `docs/research/gate-power-v2-2026-10-26.md`、`docs/research/gate-power-2026-10-26.md`：现行关卡的正负对照数据（T65 的出发点）

**基线：** 提交 `8457e89`；GitHub Actions 1728 passed、2 skipped。

**与阶段 F 的关系**：阶段 F（Demo 两周演练）由用户确认窗口后按 `docs/ops/demo-launch-proposal-2026-10-27.md` 运行，与本计划并行。演练期间不改 `src/mt5live/`、`src/mt5gw/`；确需修改时，按"先停 -Stop、修复、复审、再启动"处理。

## Global Constraints（所有任务隐含遵守）

- **禁止事项**（蓝图 v4 §2）：
  - T61–T64 完成、T65 经用户批准之前，不跑任何研究 screen，不开新卡；
  - T64、T65 完成且用户对 D16 作出决定之前，不跑 G5；
  - 真实账户下单仍禁止。
- **`config/gates.toml` 不动**：关卡 v3 先放在不生效的 `config/gates-v3.toml` 里做标定，经用户批准后再替换并重新冻结。
- **研究数据**：只读研究库；外部数据只用 cutoff 之前的部分；读锁箱期一律 `--ops-ack` 且不产生研究结论。
- **预期不取自样本内**：候选与卡的"预期效应""预期笔数"不得取自本项目数据的样本内结果；只能来自文献折算或事前计算（铁律 17）。
- **依赖**：不新增第三方依赖（下载用标准库 `urllib`；确需新增先问用户）。
- **脱敏**：报告、日志、复审文档不得含账号、服务器名、IP、令牌、本机绝对路径（铁律 9；T70 会加 CI 扫描）。
- **测试命令**（Git Bash）：`./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`
- **提交**：消息格式 `<type>(T##): 摘要`；不改写 git 历史、不强推、不改仓库可见性。

## 执行顺序

| 顺序 | 任务 | 规模 | 依赖 | 解决的问题 |
| --- | --- | --- | --- | --- |
| 1 | T70 脱敏与杂项 | S | — | F1、F2、F4、F6、F7 |
| 2 | T61 基线重新定位与 v2 卡撤回 | S | — | D30 |
| 3 | T62 候选登记与预筛工具 | M | T61 | D31、铁律 17/18 |
| 4 | T63 研究轮次预注册 | M | T62 | D32、铁律 16 |
| 5 | T67 外部数据接入（下载耗时，可与 T64–T66 交替推进） | L | — | D36、铁律 19 |
| 6 | T64 项目级试验计数 | S | T63 | D33 |
| 7 | T66 低频事件模式与标定 | M | T64 | D35 |
| 8 | T65 关卡 v3 设计与标定 | L | T64、T66 | D34 |
| 9 | T68 首轮候选登记与 R1 预注册草案 | M | T62、T63、T65、T66、T67 | 首轮选题 |
| 10 | T69 规则文档与权限 | S | 每阶段 | P6 规则对齐 |

**第十二轮审核增补**（2026-10-27，见 `docs/reviews/2026-10-27-review-round12.md`）：剩余 H 线任务按下面的顺序做。P 线计划的"总执行顺序"里，把 T79、T81 插在 T63 之前，T80 与 T64 同批。

| 顺序 | 任务 | 规模 | 依赖 | 解决的问题 |
| --- | --- | --- | --- | --- |
| a | T79 质检长缺口修复与主库缺口普查 | M | — | 第十二轮 G1（两条线的数据都受影响） |
| b | T81 外部数据来源结论落地 | S | — | D36 结论、G5 |
| c | T63 研究轮次预注册 | M | T62 | D32、G6 |
| d | T64 + T80 项目级 N 与预筛加固 | M | T63 | D33、G2、G3、G4 |
| e | T66 → T65 → T68 | — | 见上表 | 批准点不变 |

规模按以往节奏估计，仅供排序：S ≈ 半个工作日，M ≈ 1 个工作日，L ≈ 2 个工作日及以上。

---

## U0：用户操作与决定（agent 不得代办，只列清单并等待确认）

> 2026-10-27 增补：用户授权按审核建议决策（蓝图 v4 §8 D49）。下面前四项已经决定，agent 照此执行；需要批准的报告到批准点停下，交审核会话（见 `docs/superpowers/plans/2026-10-28-p-line-plan.md`"批准点"）。后两项仍须用户亲手完成。

- [x] **阶段 F 启动窗口**：已决定——按提案建议，2026-10-28 开盘后启动，连续 10 个交易日（约至 11-10），每天最多一项演练（蓝图 v4 §8）。
- [x] **D16 锁箱预算**：已决定——本锁箱最多 3 次 G5，每个研究轮次最多 1 次，H 线与 P 线共用（蓝图 v4 §8）。
- [x] **D33–D35 原则同意**：已同意（蓝图 v4 §8）。最终批准在 T65、T66 的标定报告出来之后，由审核会话按报告中预先写定的选择规则确认。
- [x] **外部数据源规则**：已决定——只用免费、条款允许个人研究使用的来源，不买付费数据，条款不明的不用，原始数据不进公开仓库（蓝图 v4 §8 D36）。T67 的来源清单交审核会话确认。**审核结论（第十二轮）**：FRED、Bitstamp 批准；HistData 暂不批准（待条款核实）；Yahoo 不批准；Dukascopy 原则可用、当前不可达——落地见 T81。
- [ ] **账户隔夜费状态**：在 Exness 个人区确认模拟盘账户是否为 swap-free；将来实盘打算用的账户类型。
- [ ] **沿用的用户事项**：S1 清单、ACL 与分支保护、MT5 策略测试器对账。

## T70：脱敏与杂项（F1、F2、F4、F6、F7）

**Files:** `docs/ops/demo-launch-proposal-2026-10-27.md`、`docs/reviews/*-codex.md`、`docs/reports/live-paper-2026-10-26.md`、`src/mt5quant/validation/stats.py`；新建 `tests/test_repo_hygiene.py` 与 `config/hygiene-allowlist.txt`。

- [x] **F1**：Demo 启动提案里的模拟盘账号与服务器名已改为占位符 `<demo-login>@<server>`（审核侧随第十一轮文档提交处理；历史中仍有记录，是否改密码由用户决定）。
- [x] **F2** 已完成（184bb73）：4 份 codex 报告 252 处/116 行替换（与审核 F2 计数吻合），独立成果审核 253 对链接逐字节比对行号语义不变；tests/test_repo_hygiene.py 账号/服务器名/本机路径三类零残留。
- [x] **CI 敏感信息扫描** 已完成（184bb73+2df378e）：四模式扫描+allowlist 五条全带理由（成果审核逐条验证必要性与失效检测）；自指残留由 2df378e 清零，提交态全量 1748 passed 2 skipped（干净 worktree 复核），CI 绿。
  - `\d{7,}@`（账号@服务器）；
  - `Exness-MT5(Trial|Real)\d+`；
  - `C:[/\\]Users[/\\](?!<)`；
  - 非私有网段的 IPv4。
  - 已知例外写进 allowlist，每条附理由。
  - 失败测试先行：在临时目录构造含账号的文件 → 扫描报错；占位符 `<demo-login>` 不报。
- [x] **F4** 已完成（184bb73）：live-paper 报告两处措辞更正+与蓝图 §2/§5 对齐（成果审核核对一致）。
- [x] **F6** 已完成（184bb73）：tracemalloc 实测峰值 2.98GB→296MB<500MB；成果审核复刻旧实现三组构造例逐位相等（随机流与分批无关）；tests/test_validation_stats.py::test_mc_drawdown_block_len1_memory_under_500mb。
  - 失败测试先行：37 万根 × 1000 次、`block_len=1` 峰值 < 500 MB（tracemalloc）；
  - 同一种子下，结果与旧实现的 `block_len=288` 路径逐位一致（只改抽取方式时须证明不改变分布；若随机流因此改变，写明并更新相关快照测试）。
- [x] **F7** 已完成（184bb73）：核对无出入（成果审核独立抽核 P4 0/160 与 gate-power-v2 §2(c)、1728+2skip 与 CI 记录一致）。
- 暂不做 **F5**（对账自行解释出场）：涉及 `src/mt5live/`，放到阶段 F 结束后。

## T61：基线重新定位与 v2 卡撤回（D30）

**Files:** 新建 `research/candidates/`；`scripts/bt.py`、`src/mt5quant/research/cards.py`；`docs/p6/research-agent.md`；测试。

- [x] 已完成（44d9a22）：三张 v2 卡 total_runs 均=0（成果审核 SQL 复核，台账 mtime 未动）；七基线卡次数写入 C900–C906 notes。
- [x] 已完成（44d9a22）：research/candidates/C900–C906，成果审核逐张核对说明与引用。
- [x] 已完成（44d9a22）：C907–C909，理由与蓝图 §6.1 逐条对应（H002-v2 样本内网格/预期、H003-v2/H006-v2 事前成本 1.41<3），0 次如实记录。
  - 理由逐条写清：H002-v2 的网格与预期笔数取自 v3 样本内；H003-v2、H006-v2 是日内持仓，事前成本不过。
  - 已跑过的，在登记里写明运行次数，这些次数计入项目级 N。
- [x] 已完成（44d9a22）：_enforce_candidate_status 接线 screen/gate 两路；成果审核复跑 7 测试含撤回双路拒（消息含 withdrawn、ledger 0 runs）。
- [x] 已完成（44d9a22）：不带 --specimen 拒；带时 actor=calibration 实测入账；--specimen 用于非样品卡拒（防统计逃逸）。
- [x] 已完成（44d9a22）：旧默认路径删除（diff 逐行核实），新流程引用蓝图 v4 §6.2，守护测试 test_p6_docs.py 绿。
- [x] 失败测试先行（44d9a22）：10 个测试先红后绿（撤回×2/样品×3/登记守护/装载器×2/规则守护），全量 1748+2skip 绿（提交态干净 worktree）。

## T62：候选登记与预筛工具（D31，铁律 17、18）

**Files:** 新建 `src/mt5quant/research/candidates.py`、`src/mt5quant/research/power.py`、`scripts/power_check.py`；`scripts/bt.py`（`bt candidate check`）；测试。

**Interfaces:**
```python
# mt5quant.research.power（Lo 2002 近似；与 docs/reviews/probes/2026-10-27-round11-power.py 逐位一致）
def sharpe_se(sr: float, years: float) -> float
def power_one_sided(sr: float, years: float, *, alpha: float = 0.05, n_tests: int = 1) -> float   # Bonferroni
def min_detectable_sharpe(years: float, *, alpha: float = 0.05, power: float = 0.8,
                          n_tests: int = 1) -> float | None                                         # 不可达返回 None

# mt5quant.research.candidates
@dataclass(frozen=True)
class Candidate: ...                      # 字段见蓝图附录 C
def load_candidate(path) -> Candidate     # 缺必填字段 → CandidateError
def precheck(candidate, *, data_root, cost_model, research_years: dict[str, float]) -> PrecheckResult
    # 事前成本倍数、每年事件或交易数、研究年数、单次检验力、决定（pass / fail / waiting_data）与理由
```

- **结果落地**：预筛结果写到 `research/candidates/C###.precheck.json`（工具生成，随候选一起提交），并记入台账新表 `candidate_checks`（带哈希链）。候选 TOML 的 `[precheck]` 段不再手填。
- **事前成本**：用候选声明的折算后预期效应（每笔或每事件的预期收益）与成本模型 v3 在入场、出场时刻的来回成本相除。持仓过夜的，按快照隔夜费计入成本（保守）。
- **研究年数**：从研究库按品种与周期实际覆盖计算，不用常数。
- **文献检查**：`replications` 与 `rebuttals` 允许为空，但 `search_log` 必须非空；`expected_effect` 必须写依据。
- [x] 全部完成（fe9dead→变基 2ef6e7b）：power 与探针 46 格浮点逐位相等+CLI 数据行字节级一致（成果审核独立复算证实）；五条件失败测试+装载拒绝 36 项红→绿；candidate_checks 入台账哈希链（审核独立篡改实验 UPDATE/DELETE/伪造 INSERT 全检出）；成本口径与引擎 A/G1 逐项对照同源（审核给行号）；全量 1872 绿+CI 绿。

## T63：研究轮次预注册（D32，铁律 16）

**Files:** 新建 `src/mt5quant/research/rounds.py`；`scripts/bt.py`（`bt round open|status|close`）；`src/mt5quant/research/ledger.py`（rounds 表、runs 加 `round_id`）；测试。

- **轮次文件**：`research/rounds/R#.toml`（蓝图附录 D）。`bt round open R1` 的前置条件：
  - 文件已提交且工作区干净；
  - 候选全部 `screened_pass`；
  - 新谱系 ≤ 3；
  - `approved_by` 非空；
  - `lockbox_uses_max` 不超过 D16 的剩余预算。
- **运行约束**：
  - `bt screen` / `bt gate` 只接受属于"已开启轮次"的卡（卡的 `round` 字段 = 该轮 id，`candidate` 在该轮名单内）；
  - 累计去重试验数超过 `trial_budget` → 拒绝；
  - 本轮 G5 次数超过 `lockbox_uses_max` → 拒绝。
- **关闭轮次**：`bt round close R1` 生成 `docs/research/round-R1-<日期>.md` 骨架：
  - 列出本轮全部运行（含失败与被拒），以及按 `advance_rule` 的判定；
  - 关闭后该轮不能再运行。
- [x] 全部完成（b758eed→0c9799f）：独立成果审核 60 项端到端检查全过（真实 CLI+临时 git 仓库：未提交拒/候选非 pass 拒/三变体轮次拒/超预算拒/超锁箱拒/关闭后拒/rounds 链篡改检出）；与 T61 协同（withdrawn 先拒、specimen actor=calibration 不入轮次统计）；d96c523 锚定重放纪律沿用；全量 1903 绿 CI 绿。生产台账已迁移+重锚（T63b 准入路径，acd3aa4 注记审计链）。
  - 追加（审核观察 1）：D16"每轮≤1 次"目前靠 approved_by 把关非机器强制——待用户口径表态。

## T67：外部数据接入（D36，铁律 19）

**Files:** 新建 `src/mt5data/ext/`（每个来源一个适配器）、`scripts/fetch_ext_data.py`、`scripts/check_ext_consistency.py`；`scripts/export_research_store.py`（纳入外部数据）；测试；报告 `docs/research/ext-data-<日期>.md`。

1. **来源清单**（交用户确认，见 U0）。每个来源写明：品种与年份范围、价格口径（买价/卖价/中间价）、原始时区与夏令时规则、使用条款摘要、是否含买卖价。候选包括：
   - 含买卖价的外汇与黄金历史报价（可回到 2003–2005 年）；
   - 纳指 100 指数日线；
   - BTC 现货交易所的长历史。
2. **适配**：统一成仓库的 K 线格式（naive UTC），存 `data/ext/<来源>/<品种>/<周期>/`，每个文件带 manifest（来源、下载时间、原始文件哈希、转换版本）。
3. **一致性核对**：与 MT5 数据的重叠期逐项核对，**写实测值，不预设阈值**：
   - 收益相关性（H1、D1）；
   - bar 时刻对齐（含 3 月、10 月底的夏令时切换周）；
   - 价格水平差（买价与中间价之差的分布）；
   - 缺口与节假日；
   - 周末结构。
4. **早年点差**：有买卖价的来源按年 × 纽约小时估计相对点差，作为"历史点差情景"进 G2 敏感性；没有的用情景倍数。
5. **研究库**：只导出 cutoff 之前的外部数据；研究库的元数据写明每个序列的来源与年数。
- [x] 全部完成（4794642→变基 3a51519）：19 项失败测试红→绿；成果审核用原始 zip 标签独立证实 HistData 美东+欧盟 DST 规则（春拨 0 根/秋拨 120 根先夏后冬）、主库独立验证 EURUSD 27 天缺口；重叠期 H1 相关 0.9947–0.9992；--include-ext 默认关待用户确认；ext-data 报告脱敏 0 命中。
- [x] 报告完成（docs/research/ext-data-2026-10-27.md）：覆盖表/一致性实测/延长后检验力表（XAUUSD 20.5 年 SR1.0 单次 98%、纳指 39.8 年 100%——审核抽 8 格与 power 函数实算一致）；来源条款摘要标待用户确认。

## T64：项目级试验计数（D33）

**Files:** `src/mt5quant/research/ledger.py`、`scripts/bt.py`、`src/mt5quant/validation/gates.py`；测试。

- **新增口径**：`stats()` 新增 `project_n_dedup_research`，只计研究轮次内的运行，H 线与 P 线合并计算（D16/P 线 D38 共用）；排除标定与运维运行。排除依据是 `bt` 写入台账的运行类别字段，不看 `--actor` 文本（第十二轮 G4，见 T80）。
- **G4 取数**：G4 的 N 与试验夏普取自 `dsr_trial_scope` 指定的范围（`family` | `project`）。
- **缺省不变**：现行 `gates.toml` 无此键，缺省仍按 `family`，保持冻结语义；`gates-v3.toml` 里写 `project`。
- **报告**：关卡报告同时列出谱系、轮次、项目三种 N。
- [ ] 失败测试先行：
  - 两个谱系各 10 次试验，`project` 口径 N = 20、`family` 口径 N = 10；
  - 标定运行不计入；
  - 缺省口径与现行冻结门槛下的结果逐位不变。

## T66：低频事件模式与标定（D35）

**Files:** 新建 `src/mt5quant/validation/events.py`；`scripts/gate_calibration.py`（事件模式的对照）；测试；报告 `docs/research/event-mode-calibration-<日期>.md`。

**Interfaces:**
```python
# mt5quant.validation.events
def month_end_events(trading_days, *, last_n: int = 1, first_n: int = 3) -> list[EventWindow]
def london_fix_events(trading_days, *, tz: str = "Europe/London", hhmm: str = "16:00",
                      lead_minutes: int = 60) -> list[EventWindow]     # 夏令时由 zoneinfo 处理
def event_returns(bars, events, *, side_fn, costs, symbol) -> pd.DataFrame   # 每事件净收益（含成本与隔夜费）
def event_test(returns, *, block: str = "year", sims: int, seed: int) -> dict  # 单侧，均值 > 0
def randomize_event_times(events, trading_days, *, seed: int) -> list[EventWindow]  # 保持月内位置与星期分布
```

- **零假设与正对照**：零假设为事件时间随机化；正对照为在事件窗口植入已知效应 δ。
- **检验力曲线**：给出事件数 × 效应大小 → 检验力，并据此写出"事件模式样本量规则"：在折算后预期效应下，检验力 ≥ 50% 所需的事件数。
- **多品种合并**：同一机制的多个品种合并事件，合并前做相关性调整（同一天的事件按日聚合后再检验）。
- [ ] 失败测试先行：
  - 月末事件跳过周末与节假日；
  - 伦敦 16:00 在 3 月、10 月切换周换算正确；
  - 事件收益扣成本与隔夜费；
  - 同种子结果逐位一致；
  - 随机化后植入效应消失、未植入时检验的误报率接近名义水平（合成数据上 1000 次）。

## T65：关卡 v3 设计与标定（D34）

**Files:** 新建 `config/gates-v3.toml`（不生效）；`src/mt5quant/validation/gates.py`（`suite="v3"` 路径）；`scripts/gate_calibration.py`、`scripts/merge_gate_power.py`；测试；报告 `docs/research/gates-v3-proposal-<日期>.md`。

- **结构**（蓝图附录 B 提案框架）：
  - 数据卫生否决（G0）；
  - 一个主检验（否决）；
  - 稳健性否决：成本 ×1.5、延迟一根、多头策略相对买入持有的超额；
  - 其余为诊断，只报告；
  - G5 不变。
- **主检验的两个备选方案**，都要标定：
  - 方案甲：walk-forward 拼接样本外收益的 PSR/DSR，N 取项目级；
  - 方案乙：同一收益序列按年块自助的单侧 p 值，按 Bonferroni 校正。
- **标定**：沿用正负对照框架。
  - 品种与周期：XAUUSD H1、XAUUSD H4（或 D1）、多品种合并变体；
  - 设计：夏普 0.5 / 1.0 / 1.5 各 20 个种子，负对照两种零假设各 20 个种子；
  - 报告**联合**通过率（主检验 + 全部否决项一起），不再只报告单项。
- **选择规则**（写进报告，先于结果）：在负对照联合误报率 ≤ 5% 的方案里，选夏普 1.0 联合检出率最高的那个。两个方案都超 5% → 收紧显著性重新标定。检出率达不到 50% 的类别标为"现有数据上测不了"。
- **外部数据**：T67 完成后，对延长后的研究段重做一次标定，给出"延长前 / 延长后"的检出率对比。
- **报告结论交用户批准**。批准后：`gates-v3.toml` 替换 `gates.toml`，同一提交更新 `FROZEN_GATES_SHA256`，并在蓝图追加决策。
- [ ] 失败测试先行：
  - v3 路径不影响现行 `gates.toml` 冻结校验；
  - 诊断项失败不改变通过结论；
  - 否决项失败即不通过；
  - 联合通过率在合并器里由逐条记录重算。

## T68：首轮候选登记与 R1 预注册草案

**Files:** `research/candidates/C001.toml`、`C002.toml` 等；`research/rounds/R1.toml`（草案，`approved_by` 留空）；报告 `docs/research/candidates-R1-<日期>.md`。

- [ ] **登记候选**（蓝图 §6.6），每个都补齐发表后证据与反驳检索记录，并跑 `bt candidate check`：
  - C001 月末效应（USTEC）；
  - C002 月末对冲调仓（USTEC 当月涨跌 → EURUSD，伦敦 16:00 定盘前）；
  - 可再登记 1–2 个其他机制类别的候选。
- [ ] **候选报告**：材料对比表，包括机制类别、文献（原始 / 复现 / 反驳）、折算后效应、事前成本、检验力（延长前后）、预筛结论。
- [ ] **R1 草案**：最多 3 个候选，写明评估方式、试验预算、锁箱用量（≤ 1）、推进规则、结束条件。**提交草案后停下，等用户批准**；批准前不开卡、不跑任何运行。

## T69：规则文档与权限

- [ ] **规则文档**：`docs/p6/research-agent.md` 与 `review-agent.md` 改为新流程：候选 → 预筛 → 轮次 → 卡 → 关卡 v3 → 轮次报告；写明"预期不取自样本内"。
- [ ] **路径白名单**（本地钩子与 CI 同源）：
  - 研究角色可以新增 `research/candidates/*.toml` 与 `*.precheck.json`（状态 A）；
  - `research/rounds/*.toml` 只允许管理员写（轮次含预算与锁箱用量，须用户批准）。
  - 守护测试覆盖以上三种情形。
- [ ] **README**：指向本计划，更新当前禁止事项。
- [ ] **阶段结束**：每完成一个阶段，更新蓝图 §2（四级证据如实），并重跑 `2026-10-25-round5-probes.py` 与 `2026-10-27-round11-power.py`，把输出贴进提交说明。
- [ ] **管理员路径**（第十二轮 G7）：在 `docs/p6/permissions.md` 写明管理员与审核会话的文档如何进入 main，避免 `research-allowlist` 红灯常态化。可选做法：管理员文档直推 main，或用经用户批准的 PR 标签豁免。

## T79：质检长缺口修复与主库缺口普查（第十二轮 G1）

**Files:** `src/mt5data/sessions.py`（`gap_is_weekend`）、`src/mt5data/quality.py`；新建 `scripts/scan_gaps.py`；测试；报告 `docs/research/data-gaps-<日期>.md`。

- **改判定**：缺口只有在"与周末窗口基本重合"时才算 `weekend`。具体条件：缺口起点不早于周五收盘前一根 bar，终点不晚于周日开盘后一根 bar（按周期换算，夏令时由现有窗口函数处理）。超出的部分按其余规则分类；跨越多个周末的长缺口报为 `unexpected`，或新增 `long_gap` 类别。
- **普查**：对主库与研究库全部 品种 × 周期 跑一遍，列出所有 `unexpected` 与 `long_gap`，并写明处理方式：
  - 能从终端补的：用现有下载脚本补齐，补后复跑质检；
  - 补不了的：写进序列 `meta.json` 的 `known_gaps`。研究工具（按年分段、walk-forward、事件研究）读到已知缺口时，在报告里注明该窗口样本不完整。
- **EURUSD 2021-06**：优先处理这段 27 天缺口，写明是补齐了还是登记为已知缺口。
- [ ] 失败测试先行：
  - 探针 `docs/reviews/probes/2026-10-27-round12-gap-probe.py` 的场景——27 天缺口不得归为 `weekend`；
  - 正常周末（含夏令时切换周 49 小时）仍归为 `weekend`；
  - 周五提前收盘与周日晚开的常规偏差不误报；
  - `known_gaps` 能被按年分段与事件研究读到。

## T80：预筛加固与运行类别（第十二轮 G2、G3、G4；与 T64 同批）

**Files:** `src/mt5quant/research/candidates.py`、`src/mt5quant/research/ledger.py`、`scripts/bt.py`；测试。

- **隐含夏普（G2）**：
  - 工具从研究库计算该品种在持仓长度上的无条件收益波动 σ（随机时点抽样，与候选的信号无关，属于品种性质）；
  - 隐含夏普 =（声明每事件毛收益 − 事前来回成本）÷ σ × √（每年事件数）；
  - 检验力取声明夏普与隐含夏普中较小的那个；两者与 σ 的计算口径都写进 `C###.precheck.json`。
- **外部数据年数（G3）**：`coverage_years` 合并研究库里已批准的外部序列（`data_research/ext/<来源>/<品种>/<周期>/`），按序列取最长连续覆盖；precheck JSON 逐段列出来源与年数。未批准来源（如 HistData）不计入。
- **运行类别（G4）**：
  - `bt` 拒绝 CLI 传入保留的 actor 名（`calibration`、`ops`、`migration`）；
  - 台账 `runs` 新增 `run_class`（`research` | `calibration` | `ops` | `migration`），由 `bt` 按候选状态与轮次写入，进入哈希链；
  - T64 的研究 N 只按 `run_class = research` 计数。
- [ ] 失败测试先行：
  - 声明夏普 1.5、但毛收益与成本隐含夏普 0.3 → 检验力按 0.3 算；
  - 已批准外部序列使研究年数增加，未批准来源不计入；
  - `--actor calibration` 被拒；
  - 样品卡运行的 `run_class = calibration`；
  - 旧台账迁移后 `run_class` 按 actor 推断，并记一行迁移。

## T81：外部数据来源结论落地（D36、第十二轮 G5）

**Files:** `scripts/fetch_ext_data.py`、`scripts/export_research_store.py`、`src/mt5data/ext/`；`docs/research/ext-data-2026-10-27.md`（增补节）；测试。

- **批准的来源**：FRED NASDAQ100 与 Bitstamp（CryptoDataDownload）日线导入研究库（`--include-ext` 只对这两个来源开启）。manifest 写明使用限定：只作研究用途、不再分发。
- **HistData**：在 histdata.com 查找明确的使用条款（下载页、FAQ、页脚）。原文与查阅日期存到本地 `data/ext/terms/`（不入库），报告里引用。
  - 找到允许个人研究使用的明文 → 交审核会话复核后才启用；
  - 找不到 → 不进研究库，已下载样本只保留作一致性核对。
- **逐年时间口径验证**（为 HistData 早年文件准备，G5）：用市场结构锚点逐年核对标签口径，规则写进 `consistency.py`。锚点是外汇每周开盘在纽约周日 17:00、每日换日在纽约 17:00。口径与 2020–2021 实测规则不一致的年份，单独登记，不得套用同一转换。
- **Yahoo**：从适配器的可选来源里移除。
- **Dukascopy**（可选）：在用户网络下测试接通；接通后先做条款核对与一致性核对，再交审核会话。
- **USTEC 口径**：报告写明 FRED 指数收盘（纽约 16:00）与券商差价合约日线（纽约 17:00 结算）的差异。事件窗口用指数收盘定义时，实盘执行按差价合约在 16:00 前后成交，并在候选登记里写清。
- [x] 全部完成（84b5c9a+P3/P4 修正 c1623b2）：独立成果审核通过（白名单实测拒 6 个 histdata 序列、cutoff 纪律核对、条款核查独立抽查印证无 Terms 页、合成未测年份 2019/2022 攻击测试判别真实、9 失败测试红→绿）；实际导出 fred USTEC 39.8 年+cdd BTCUSD 10.9 年入研究库；HistData 维持不批准（原文存 data/ext/terms）。

---

## 集成与验收

1. 每个任务结束：全量 pytest 全绿 → 提交 → 推送 → Actions 为绿 → 复选框写上证据。
2. 选题体系（T61–T64）结束，以下行为都由测试证明：
   - 撤回卡被拒；
   - 不在已开启轮次里的卡被拒；
   - 超预算被拒；
   - 预筛结果进入台账哈希链。
3. 评估体系（T65、T66）结束：`gates-v3-proposal` 与 `event-mode-calibration` 两份报告，都报告联合误报率与检出率；交用户批准。
4. 数据（T67）结束：`ext-data` 报告，包括覆盖表、一致性核对、延长后的检验力。
5. 首轮（T68）：候选报告与 R1 草案交用户批准；批准后才进入阶段 G。

## 明确不做

- 在 R1 获批之前开卡或跑研究 screen；
- 修改现行 `config/gates.toml`（等用户批准关卡 v3）；
- 在阶段 F 演练期间修改 `src/mt5live/`、`src/mt5gw/`（除非按"停—修—复审—再启动"处理）；
- 机器学习模型；M1 剥头皮；实盘真金下单；
- 改写 git 历史或改变仓库可见性。
