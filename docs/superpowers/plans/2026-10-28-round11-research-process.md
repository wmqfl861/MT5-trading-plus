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

规模按以往节奏估计，仅供排序：S ≈ 半个工作日，M ≈ 1 个工作日，L ≈ 2 个工作日及以上。

---

## U0：用户操作与决定（agent 不得代办，只列清单并等待确认）

> 2026-10-27 增补：用户授权按审核建议决策（蓝图 v4 §8 D49）。下面前四项已经决定，agent 照此执行；需要批准的报告到批准点停下，交审核会话（见 `docs/superpowers/plans/2026-10-28-p-line-plan.md`"批准点"）。后两项仍须用户亲手完成。

- [x] **阶段 F 启动窗口**：已决定——按提案建议，2026-10-28 开盘后启动，连续 10 个交易日（约至 11-10），每天最多一项演练（蓝图 v4 §8）。
- [x] **D16 锁箱预算**：已决定——本锁箱最多 3 次 G5，每个研究轮次最多 1 次，H 线与 P 线共用（蓝图 v4 §8）。
- [x] **D33–D35 原则同意**：已同意（蓝图 v4 §8）。最终批准在 T65、T66 的标定报告出来之后，由审核会话按报告中预先写定的选择规则确认。
- [x] **外部数据源规则**：已决定——只用免费、条款允许个人研究使用的来源，不买付费数据，条款不明的不用，原始数据不进公开仓库（蓝图 v4 §8 D36）。T67 的来源清单交审核会话确认。
- [ ] **账户隔夜费状态**：在 Exness 个人区确认模拟盘账户是否为 swap-free；将来实盘打算用的账户类型。
- [ ] **沿用的用户事项**：S1 清单、ACL 与分支保护、MT5 策略测试器对账。

## T70：脱敏与杂项（F1、F2、F4、F6、F7）

**Files:** `docs/ops/demo-launch-proposal-2026-10-27.md`、`docs/reviews/*-codex.md`、`docs/reports/live-paper-2026-10-26.md`、`src/mt5quant/validation/stats.py`；新建 `tests/test_repo_hygiene.py` 与 `config/hygiene-allowlist.txt`。

- [x] **F1**：Demo 启动提案里的模拟盘账号与服务器名已改为占位符 `<demo-login>@<server>`（审核侧随第十一轮文档提交处理；历史中仍有记录，是否改密码由用户决定）。
- [ ] **F2**：codex 复审报告中的本机绝对路径（形如 `C:/Users/<用户名>/.zcode/workspace/<id>/`）改为仓库相对路径。
- [ ] **CI 敏感信息扫描**：`test_repo_hygiene.py` 对 `git ls-files` 的文本文件扫描以下内容：
  - `\d{7,}@`（账号@服务器）；
  - `Exness-MT5(Trial|Real)\d+`；
  - `C:[/\\]Users[/\\](?!<)`；
  - 非私有网段的 IPv4。
  - 已知例外写进 allowlist，每条附理由。
  - 失败测试先行：在临时目录构造含账号的文件 → 扫描报错；占位符 `<demo-login>` 不报。
- [ ] **F4**：影子盘报告与相关文档把"1 个交易日"改为如实的时长（10:58–21:04 UTC，约 10 小时）。
- [ ] **F6**：`mc_drawdown_distribution` 的起点改为逐批抽取，缺省 `block_len=1` 时的峰值内存也受控。
  - 失败测试先行：37 万根 × 1000 次、`block_len=1` 峰值 < 500 MB（tracemalloc）；
  - 同一种子下，结果与旧实现的 `block_len=288` 路径逐位一致（只改抽取方式时须证明不改变分布；若随机流因此改变，写明并更新相关快照测试）。
- [ ] **F7**：蓝图已由审核侧重写为 v4，本任务只核对 §2 的事实与代码一致，有出入就改蓝图并在提交说明里列出。
- 暂不做 **F5**（对账自行解释出场）：涉及 `src/mt5live/`，放到阶段 F 结束后。

## T61：基线重新定位与 v2 卡撤回（D30）

**Files:** 新建 `research/candidates/`；`scripts/bt.py`、`src/mt5quant/research/cards.py`；`docs/p6/research-agent.md`；测试。

- [ ] **核对 v2 卡是否跑过**：用只读方式查台账（`bt ledger stats --card H002-v2` 等），把运行次数写进提交说明。
- [ ] **标定样品登记**：为 H001–H007 写候选登记 `C900`–`C906`，`status = "specimen"`，并写明"用于校准，不作研究候选"。
- [ ] **v2 卡撤回**：为三张 v2 卡写候选登记，`status = "withdrawn"`。
  - 理由逐条写清：H002-v2 的网格与预期笔数取自 v3 样本内；H003-v2、H006-v2 是日内持仓，事前成本不过。
  - 已跑过的，在登记里写明运行次数，这些次数计入项目级 N。
- [ ] **`bt` 拒绝撤回的卡**：卡对应的候选为 `withdrawn` → 拒绝运行。
- [ ] **样品卡运行受限**：只允许带 `--specimen` 运行，台账记 `actor=calibration`，且不进入任何研究轮次的统计。
- [ ] **研究规则改写**：`docs/p6/research-agent.md` 删去"审查否决 → 同谱系新建 v2"的默认路径，改为"新版本须作为候选登记并列入轮次"。
- [ ] 失败测试先行：
  - 撤回卡被拒；
  - 样品卡不带 `--specimen` 被拒；
  - 带 `--specimen` 时台账 actor 为 calibration。

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
- [ ] 失败测试先行：
  - `power_one_sided(1.0, 6)` ≈ 0.64、`(1.0, 4.3, n_tests=20)` ≈ 0.13；`min_detectable_sharpe(6)` ≈ 1.46，`(6, n_tests=20)` 为 None；
  - 持仓 < H1 → fail；
  - 成本倍数 < 3 → fail；
  - 检验力 < 50% → waiting_data；
  - `search_log` 为空 → 拒绝加载；
  - 预筛结果写进台账，并能通过哈希链校验。

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
- [ ] 失败测试先行：
  - 未提交的轮次文件不能开启；
  - 卡不在轮次内被拒；
  - 超预算被拒；
  - 关闭后再运行被拒；
  - 轮次记录在台账哈希链内。

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
- [ ] 失败测试先行：
  - 不随夏令时变化的美东时间源 → 转换后在夏令时切换周 bar 时刻正确；
  - cutoff 之后的数据不进研究库；
  - manifest 缺字段 → 拒绝入库；
  - 一致性核对在合成数据上能检出时区错位一小时。
- [ ] **报告**：
  - 覆盖表（品种 × 周期 × 起止年份 × 年数）；
  - 一致性核对结果；
  - 延长后的检验力表（用 T62 的函数）。

## T64：项目级试验计数（D33）

**Files:** `src/mt5quant/research/ledger.py`、`scripts/bt.py`、`src/mt5quant/validation/gates.py`；测试。

- **新增口径**：`stats()` 新增 `project_n_dedup_research`，只计研究轮次内的运行，排除 `actor=calibration/ops`。
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
