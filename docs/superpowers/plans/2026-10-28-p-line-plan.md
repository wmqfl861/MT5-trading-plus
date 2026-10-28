# P 线施工计划（2026-10-28，第二版）：概率决策线 v1（T71–T78、T90–T93）

> **第二版（2026-10-28，依据 `docs/reviews/2026-10-28-review-pline-02.md`）**：
> - 总执行顺序改为两个会话分列；
> - T73 拆成 T73A（现在可做）与 T73B（等 T65 批准）；T73A 不改台账表结构；
> - T75 的代码可先写；标定设计改为 12 个假设一组、以触发日数为样本单位，首批只要求 XAUUSD；
> - T76 增补审核二的修正项（Q1–Q3、Q6）；T77 按 D51–D53 调整；
> - 新增 T90–T93，覆盖 P1 运行入口、P1 现象检验、P2 决策系统和确认阶段；
> - 新增"双会话分工"一节。
>
> **For agentic workers:** P 线会话按本文"总执行顺序"中 P 线会话的部分推进，H 线会话负责的任务见同一节。每个任务的流程：
> 1. 先写失败测试；
> 2. 实现；
> 3. 本任务测试通过；
> 4. **全量回归**；
> 5. 提交并推送；
> 6. **确认 GitHub Actions 为绿**（红了先修，不推新功能）；
> 7. 勾选复选框，并**在同一行写上证据**（测试名、报告路径或日志路径）。
>
> 没有证据的复选框一律不勾。遇到"批准点"就停下（见"批准点"一节），不得自我批准。

**Goal:** 按 `docs/blueprint-p-line.md` 建成概率决策线 v1 的基础设施，并把首批候选与第一轮草案交审核：
- 首达标签、概率表、决策规则；
- 拟合钩子、发现段与验证段、模型身份；
- 局面库与动作库；
- 概率模式评估及其标定；
- 检验力与预筛；
- 首批候选与轮次草案；
- 风控号段子上限与实盘接入（阶段 F 之后，H 线会话负责）；
- P1 现象检验、P2 决策系统、确认阶段（G5、影子盘、前向样本）。

**输入文档（先读）：**
1. `docs/blueprint-p-line.md`：P 线蓝图 v1.1，包括框架、研究流程、铁律 20–22、决策 D38–D54、§8 双会话分工、附录模板。
1a. `docs/reviews/2026-10-28-review-pline-01.md`、`docs/reviews/2026-10-28-review-pline-02.md`：P 线审核一、二；复现脚本 `docs/reviews/probes/2026-10-28-pline-probes.py`。
2. `docs/blueprint.md`（v4）：§2 当前禁止事项、§4 铁律、§6 研究方向、§8 决策（含按审核建议代用户作出的决定）。
3. `docs/superpowers/plans/2026-10-28-round11-research-process.md`：第十一轮计划 T61–T70。本计划多个任务依赖它。
4. `docs/ops/demo-launch-proposal-2026-10-27.md`：阶段 F 启动方案。
5. 代码现状：
   - `src/mt5quant/backtest/engine_a.py` 文件头（成交、成本、SL/TP 语义）；
   - `src/mt5quant/strategies/base.py`（策略接口、ExitRules）；
   - `src/mt5live/executor.py`（启动时拒绝 `time_stop_bars` 与 `week_close_exit`）；
   - `src/mt5quant/validation/walkforward.py`、`src/mt5quant/research/cards.py`（卡模式严格拒绝未知键）。

**基线：** 第一版 `b77b040`（第十一轮文档；GitHub Actions 1728 passed、2 skipped）；第二版 `dacf08f`（tests run#100 绿；全量 2012 passed、1 skipped）。

## Global Constraints（所有任务隐含遵守）

- **P 线禁止事项**（蓝图 v4 §2）：
  - T75 标定报告与 T77 轮次草案经审核批准之前，不在真实局面触发上计算任何标签、拟合或 Δp。
  - 在此之前允许做的：合成数据上的单元测试；用随机时点、随机化触发和植入效应做对账与标定；只数触发次数、不看结果的计数（T76）。
- **不改现有执行语义**（D44）：
  - T71–T77、T90–T93 不修改 `src/mt5quant/backtest/engine_a.py`、`engine_b.py`、`src/mt5live/`、`src/mt5gw/`；
  - T78 等阶段 F 结束后才做。
- **非拟合策略逐位不变**：凡改到 `bt`、关卡、walk-forward、G0、台账的任务，都要证明现有策略（B1–B6、MaCrossATR）的结果逐位不变。
- **标定期间不改被标定的代码路径**：T65、T66、T75 的标定分片运行期间，不合并会改变其代码路径的改动。
- **依赖**：不新增第三方依赖（正态分位用标准库 `statistics.NormalDist`）。
- **脱敏**（铁律 9）：报告、日志不含账号、服务器名、IP、令牌、本机绝对路径。
- **测试命令**（Git Bash）：`./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`
- **提交**：消息格式 `<type>(T##): 摘要`；不改写 git 历史、不强推、不改仓库可见性。

## 总执行顺序（两个会话分列，截至 0c76de9）

**H 线会话**（主工作区，`main`）：阶段 F → 第十一轮计划的剩余任务（按该计划）→ T71（随 T69）→ T78（阶段 F 结束后）→ 台账锚定改为事件序号（审核一越界修复的后续）。

**P 线会话**（worktree `../mt5-pline`，分支 `pline/dev`）：

| 顺序 | 任务 | 依赖 | 状态与备注 |
| --- | --- | --- | --- |
| 1 | T72 首达标签与概率表 | — | 完成（含审核一修正） |
| 2 | T74 局面、动作、特征部分 | T72 | 完成（含审核一修正） |
| 3 | T76 检验力、分段与预筛 | T62、T67 | 完成 |
| 4 | T76 审核二修正（Q1–Q3、Q6） | — | 完成（0d92448，审核三复核通过） |
| 5 | T73A 拟合协议、P 卡与轮次字段、台账字段、`bt fit`、G0 冻结模型、分段约束 | T63、T64 | 完成；审核三修正待做 |
| 6 | T74 收尾（`LevelDecisionV1` 等） | T73A | 完成；审核三修正待做 |
| 7 | T75 代码与合成数据测试 | T72、T74 | 完成；审核三修正待做；不跑正式标定 |
| 8 | T90 P1 运行入口 `bt prob test` | T73A、T75 代码 | 完成；审核三修正待做；真实运行要等 T77 批准 |
| 8a | 审核三修正（S1–S9、D1–D2、小改、补齐测试） | — | **现在做**；验收：复现脚本 `docs/reviews/probes/2026-10-28-pline-probes-03.py` R1–R6 全部 PASS |
| 8b | T77 前半：文献功课、预期 Δp、候选草稿，然后计数 | — | **8a 之后可做**；计数用计数工具，预筛结论等 T75 批准、T76 收尾后再出 |
| 9 | T73B 逐窗拟合与 purge、关卡 v3 主检验接线 | T65 批准 | 等 H 线审核会话批准 T65 |
| 10 | T75 正式标定与报告 | T66 批准 | 等 H 线审核会话批准 T66；报告是 P 线批准点 |
| 11 | T76 收尾：接入 T75 实测样本量规则 | T75 批准 | |
| 12 | T77 首批候选与第一轮草案 | T63、T75 批准、T76 收尾 | P 线批准点 |
| 13 | T91 P1 现象检验轮 | T77 批准、T90 | 轮次结束报告是 P 线批准点 |
| 14 | T92 P2 决策系统 | T91 批准且有通过的假设、T73B、T74 收尾 | 轮次草案、轮次结束报告各是一个 P 线批准点 |
| 15 | T93 确认：G5、影子盘、前向样本 | T92 批准、T78 | 每次用 G5 前是 P 线批准点；部署由 H 线会话执行 |

T66 若在审核中被要求修改，T75 的代码跟着调整后再正式标定；T65 同理影响 T73B。

阻塞时（依赖未满足、停在批准点），先做不依赖它的任务；都做完了，就停下告诉用户在等什么、等谁。

规模：S ≈ 半个工作日，M ≈ 1 个工作日，L ≈ 2 个工作日及以上，仅供排序。

## 批准点（D49）

用户已授权按审核建议决策，但批准不由执行 agent 作出。到达批准点时：

1. 把报告或草案提交并推送，报告开头写"状态：待审核"，列出预先写定的判定标准和实测结果；
2. 在会话里告诉用户："请把 <报告路径> 交给审核会话"；
3. 停在这里，不进入依赖它的任务。其他不依赖它的任务可以继续。

批准点与归属（D54）：

| 批准点 | 交给 |
| --- | --- |
| T65、T66 标定报告；T68 的 R1 草案；H 线各轮结束报告与 G5 | H 线审核会话 |
| T75 标定报告；T77 候选与轮次草案；T91 轮次结束报告；T92 轮次草案与轮次结束报告；P 线每次 G5 使用之前 | P 线审核会话 |

是否已批准，以 main 上 `docs/reviews/` 里对应审核会话的报告为准。P 线轮次文件的 `approved_by` 由 P 线审核会话批准时填写，执行会话不得填写或修改（蓝图 P 线 §8）。

## 双会话分工（D54）

细则见蓝图 P 线 §8。P 线会话的日常操作：

- **目录**：只在 `../mt5-pline`（分支 `pline/dev`）里工作。主工作区的未提交改动、未跟踪文件和进程一律不碰。研究库与台账通过目录链接 `data_research`、`data/research` 共用，不链接 `data/live`。
- **文件**：只改蓝图 P 线 §8 列出的 P 线文件。共用代码只在对应的 H 线任务已在 main 上完成之后才改，且保证非拟合策略的结果逐位不变。
- **推送**（每个任务结束时）：
  ```bash
  git fetch origin && git rebase origin/main
  ./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
  git push origin pline/dev:main      # 只推本分支的提交；被拒就重新同步，不得强推
  ```
  只 `git add` 自己改过的文件。rebase 冲突如果出在不归自己的文件里，停下告诉用户。
- **证据**：推送后确认 GitHub Actions 为绿再勾复选框。CI 证据写提交号加 tests 工作流的 run_number（以 GitHub API 为准，例如 `gh api repos/<owner>/<repo>/actions/runs?head_sha=<sha>`），不要自行推算编号。
- **CI 红**：出在不归自己的文件里时，告诉用户、停止推送，不自行修复。

## U0：只能由用户亲手完成的事项（agent 不得代办，只列清单提醒）

- [ ] 阶段 F 期间（约 10-28 至 11-10），这台电脑和 MT5 终端保持开机、联网。
- [ ] 在 Exness 个人区确认模拟盘账户是否 swap-free，以及将来实盘打算用的账户类型。
- [ ] S1 清单：轮换 MCP 令牌、删除防火墙入站规则、确认 RDP、决定仓库是否改为私有。
- [ ] GitHub 的 ACL 与 main 分支保护。
- [ ] MT5 策略测试器对账（图形界面操作）。

以下事项已按审核建议决定（蓝图 v4 §8），agent 照此执行，不再等待：

- D16 锁箱预算；
- D33–D35 原则同意，最终按标定报告的批准点；
- 外部数据源规则：只用免费、条款允许个人研究的来源，原始数据不进公开仓库；
- 阶段 F 的启动窗口、期限与演练密度。

---

## T71：P 线规则与权限（S，H 线会话随 T69 完成）

**Files:** `docs/p6/research-agent.md`、`docs/p6/review-agent.md`、`docs/p6/permissions.md`；路径白名单（本地钩子与 CI 同源，与 T69 同一处实现）；守护测试；`README.md`。

- [ ] **规则文档**：
  - 写明 P 线流程：候选 → 预筛 → 轮次 → P1 现象检验 → P2 决策系统 → G5 → 前向样本；
  - 写明发现段与验证段的规则：P 线的预期可以取自发现段，不得取自验证段与锁箱；
  - 写明铁律 20–22。
- [ ] **路径白名单**：
  - 研究角色可以新增或修改 `src/mt5quant/strategies/library/prob/**` 与 `tests/test_strategies_prob_*.py`；
  - 研究角色可以新增 `research/cards/P*.toml`（只允许状态 A）、`research/candidates/C1*.toml` 与 `*.precheck.json`；
  - 研究角色对 `src/mt5quant/prob/**`、`scripts/prob_*.py` 只读。
- [ ] 失败测试先行：
  - 研究角色提交改动 `src/mt5quant/prob/labels.py` → 被拒；
  - 新增 `src/mt5quant/strategies/library/prob/x.py` → 放行；
  - 新增 `research/cards/P001-v1.toml` → 放行，修改已提交的 → 被拒。
- [ ] **README**：指向 `docs/blueprint-p-line.md` 与本计划。

## T72：首达标签与概率表（M）

**Files:** 新建 `src/mt5quant/prob/__init__.py`、`labels.py`、`tables.py`、`decision.py`、`artifact.py`；测试 `tests/test_prob_labels.py`、`test_prob_tables.py`、`test_prob_decision.py`、`test_prob_artifact.py`。

**Interfaces:**
```python
# mt5quant.prob.labels
@dataclass(frozen=True)
class Bracket:
    sl_atr: float
    tp_atr: float
    horizon_bars: int

def label_triggers(bars, triggers, costs, *, symbol, timeframe, bracket, contract_size,
                   sizing, vol_bounds, train_end=None) -> pd.DataFrame
    # triggers 列：time（触发 bar，收盘判断）、action、side（+1/−1）、level、特征列
    # 返回追加列：outcome ∈ {"win","loss","time","skipped"}、net_r、entry_time、exit_time、exit_reason
    # train_end 给定时剔除标签窗口越过 train_end 的触发（purge）

# mt5quant.prob.tables
@dataclass(frozen=True)
class CellEstimate:
    n: int
    p_win: float
    p_loss: float
    p_time: float
    r_win: float
    r_loss: float
    r_time: float
    se_win: float
    se_loss: float

class ProbTable:
    @classmethod
    def fit(cls, labels, *, group_by: list[str], prior_strength: int, min_cell: int) -> "ProbTable"
    def lookup(self, situation: str, action: str, features: dict) -> CellEstimate
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, d: dict) -> "ProbTable"

# mt5quant.prob.decision
def ev_lower(est: CellEstimate, *, z: float) -> float               # 单位 R，含成本
def choose(candidates: list[tuple[str, CellEstimate, float]], *, z: float,
           threshold_mult: float) -> str | None                     # (动作, 估计, 平均来回成本R)；None = 不做

# mt5quant.prob.artifact
def save_model(model: dict, root) -> tuple[str, str]                # (路径, sha256)
def load_model(path, *, expected_sha256: str) -> dict               # 哈希不符 → ModelIntegrityError
```

- **标签与引擎同口径**：
  - 每个触发截取有界切片（回看 + K + 2 根），用引擎 A 跑一笔：信号在触发 bar t 到 t+K−1 为方向值，t+K 归零；
  - ExitRules 为 `sl_atr`/`tp_atr` + `after_signal_change`，定手数用共享函数；
  - `exit_reason` 映射：take_profit / take_profit_gap → win；stop / stop_gap → loss；signal → time；未成交（禁入窗推迟超出持有上限、手数为 0）→ skipped（计数，不进概率）；
  - `net_r` 取引擎的 `r_multiple`。
- **收缩**：
  - p_格 = (k + m·p_基准) / (n + m)；r̄ 同样向基准收缩；
  - 标准误按格内实际样本数 n 计算：sqrt(p(1−p)/n)；
  - n < min_cell 的格退回上一级（局面 × 动作）。
- **决策**：
  - EV_下界 = (p_win − z·se_win)·r_win + (p_loss + z·se_loss)·r_loss + 余下比例·r_time；
  - 门槛为 threshold_mult × 平均来回成本；
  - 并列时按 A1 < A2 < A3 < A4 的顺序取第一个（确定性）。
- **模型文件**：规范化 JSON（键排序、UTF-8、无多余空白）取 sha256；字段见蓝图 P 线附录 B。
- [x] 失败测试先行（tests/test_prob_labels.py、test_prob_tables.py、test_prob_decision.py、test_prob_artifact.py，共 35 个测试，先红后绿）：
  - 合成数据上随机抽 200 个**随机时点**（不是局面触发）：标签与引擎 A 单笔结果逐笔一致（出场原因映射、`net_r` 与 `r_multiple` 差 < 1e-12）——`test_labels_match_engine_200_random_times`；
  - 同一根 bar 同时触及止损和止盈 → loss；开盘跳空穿过止损 → loss（stop_gap）；禁入窗推迟入场与引擎一致；手数为 0 → skipped——`test_same_bar_sl_tp_stop_priority_is_loss`、`test_gap_through_stop_is_loss`、`test_blocked_entry_delayed_matches_engine`、`test_zero_lots_is_skipped`；
  - purge：标签窗口越过 `train_end` 的触发被剔除——`test_purge_drops_triggers_crossing_train_end`；
  - 收缩：n = 0 的格等于基准，n 很大时趋近经验频率；不足 `min_cell` 的格退回上一级——`test_empty_cell_falls_back_to_base`、`test_large_n_approaches_empirical`、`test_thin_cell_falls_back_to_situation_action_parent`；
  - `ev_lower` 随 z 单调不增；全部低于门槛 → None；并列时取序最前者——`test_ev_lower_monotone_non_increasing_in_z`、`test_choose_none_when_all_below_threshold`、`test_choose_tie_takes_first_in_action_order`；
  - 模型文件往返一致，同一输入两次生成的哈希相同，哈希不符 → ModelIntegrityError——`test_to_dict_from_dict_roundtrip`、`test_same_input_same_hash`、`test_load_model_tampered_content_raises`。
- [x] **真数据对账证据**（本机运行，不进 CI）：XAUUSD H1 研究库（52,146 根，成本 rel@NY p90 快照 + swap 快照）随机时点 200 个（seed=20261027），结局 loss 90 / win 85 / time 25，逐笔对照不一致 0 处、max|net_r−r_multiple|=0.000e+00；日志 `data/prob/t72-evidence-2026-10-27.log`（本机 worktree，结论存档于本提交说明）。随机时点不是研究结论，不违反禁止事项。
  （CI 如实注：8d14c41 自身的 CI 因锚定测试同刻度偶发为红，与本任务内容无关；同树重跑 8739667 绿、修复后 d96c523 绿——见下方审核修正行。）
- [x] **审核修正（R12，2026-10-28）**：① 概率表 r̄ 收缩改按结局伪样本数 r̄_o=(Σr_o+m·p_base_o·r_base_o)/(k_o+m·p_base_o)——同分布格=基准条件均值、大样本趋近经验条件均值、EV 手算一致三测（test_rbar_same_distribution_equals_base_mean/test_rbar_large_n_approaches_empirical_conditional_mean/test_ev_from_lookup_matches_hand_calc）；② purge 改按 bar 下标（t+K+1 存在且 time<train_end），周末/节假日缺口测试（test_purge_weekend_gap_dropped_by_bar_index/test_purge_holiday_gap_dropped_by_bar_index）；③ 模型文件原子写对齐仓库统一模式（唯一 tmp+fsync+replace 带重试，tests/test_prob_artifact.py）；④ 新增切片 vs 全序列对账 100 例（|Δnet_r|<1e-6，test_slice_label_matches_full_series_engine）。CI 绿灯 2df2ca6（tests run#97 success，全量 1981 passed, 1 skipped；编号已由审核二按 GitHub API 更正）。

## T73：拟合钩子、分段与模型身份（L；第二版拆为 T73A、T73B）

**T73A（现在可做；T63、T64 已在 main 完成）Files:**
- `src/mt5quant/strategies/base.py`：新增可选协议，`fittable = False` 为缺省；
- `src/mt5quant/research/cards.py`：P 卡字段 `segment` 与 `[prob]` 段（复用 `candidates.py` 里 `[prob]` 的校验），`evaluation_mode = "probability"`，`primary_statistic = "delta_p_one_sided"`；其他卡的未知键仍然严格拒绝；
- `src/mt5quant/research/rounds.py`：
  - 新增 `[segments] split`（格式 `YYYY-MM-01`，必须是月初）；
  - `lockbox_uses_max = 0` 只在全部候选的 `[evaluation]` 都是 `mode = "probability"` 时允许；上限 1 不变（D16）；
  - 新增 `[hypotheses]`：概率模式的候选必填，列出本轮要检验的假设（即 Holm 的假设族），每条都必须在候选登记的假设里；非概率候选不得出现；格式见蓝图 P 线附录 A；
  - H 线现有轮次文件的装载结果逐字段不变；
- 台账（`src/mt5quant/research/ledger.py` 只读或只加辅助函数）：**不改 runs 表结构，不触发生产台账迁移**。两个会话共用一份台账，改表结构要按 T80 的流程重锚。P 线字段这样放：
  - 决定试验身份的（`segment`、`split`、假设、止损止盈）放进 `params_json`；
  - 来源信息（`model_sha256`、`prob_version`、`fit_start`、`fit_end`、`purge_bars`）放进 `metrics_json` 的 `metrics.prob`；
  - 两处都已进入哈希链；
- `scripts/bt.py`：新增 `bt fit`；screen 与 gate 对拟合策略先拟合再评估（单一训练窗）；分段约束；T73B 完成前，拟合策略的 walk-forward 与关卡 v3 主检验一律拒绝运行，报错写明要等 T73B；
- `src/mt5quant/validation/lookahead.py`：G0 用冻结模型；
- 测试。

**T73B（等 H 线审核会话批准 T65）Files:**
- `src/mt5quant/validation/walkforward.py` 与关卡 v3 主检验：拟合策略逐窗重新拟合 + purge；非拟合策略逐位不变；
- 关卡 v3 增加 P 线专属否决项"样本外校准"（Brier 技能分 > 0，阈值与做法以 T75 标定为准），只对拟合的概率策略生效；
- 测试。

T73B 改的是 T65 的代码路径。T65 批准后才动，并证明 T65 标定用到的结果逐位不变。

**Interfaces:**
```python
class BaseStrategy:
    fittable: bool = False
    def fit(self, bars_by_symbol: dict, costs_by_symbol: dict, params: dict, *,
            train_end, purge_bars: int) -> dict: ...        # 返回模型 dict（附录 B 格式）
    def with_model(self, model: dict) -> "BaseStrategy": ...   # 返回装好模型的新实例；不改自身
```

- **非拟合策略不受影响**：`fittable = False` 的策略走原路径，所有输出逐位不变。证据：
  - 现有测试全过；
  - B1 在固定夹具上的 G3 结果改动前后逐位一致。
- **拟合规则**：
  - 只用 `time < train_end` 的 bar，并按 `purge_bars` 剔除标签越界的触发；
  - walk-forward 每个训练窗重新拟合，测试窗用冻结模型；
  - G0 用研究段前 70% 拟合出的冻结模型做截断与窗口不变性检查。
- **分段**：
  - 分界日期读自轮次文件 `[segments] split = "YYYY-MM-01"`（T77 写入，`bt` 校验格式与月初）；
  - `segment = "discovery"` 的卡不得读取 `>= split` 的数据；
  - `segment = "validation"` 的卡只在 `[split, cutoff)` 内评估样本外表现，训练窗可以包含更早的数据。
- **模型身份**：
  - 每次运行记录所用模型的 sha256；
  - 同样的输入重新拟合，哈希必须相同；
  - 模型文件写到 `data_research/models/<卡>/<sha256>.json`。
- [x] T73A 失败测试先行（提交 69ad65f，tests run_number 108 success；34 个测试先红后绿；全量 2068 passed, 1 skipped——含既有 B1/walk-forward 套件，非拟合策略逐位不变；runs 表结构不变，verify_chain 通过）：
  - 非拟合策略逐位不变（现有测试全过；B1 在固定夹具上的 G3 结果改动前后逐位一致）；
  - 拟合不读 `train_end` 之后的数据，最后 K 根的触发被 purge；
  - 冻结模型的拟合策略能过 G0；故意在 `generate_signals` 里现场统计全部历史的坏策略被 G0 查出；
  - discovery 卡读 `>= split` 被拒；validation 卡在 `< split` 上评估被拒；
  - 轮次文件：`[segments] split` 非月初或格式错 → 拒；概率模式轮次 `lockbox_uses_max = 0` → 过；含非概率候选的轮次为 0 → 拒；> 1 仍拒；概率模式候选缺 `[hypotheses]`、或列出候选没登记的假设 → 拒；H 线现有轮次文件装载结果不变；
  - P 线字段写入 `params_json`、`metrics_json` 后 `bt ledger verify` 通过，runs 表结构不变。
- [ ] **审核三修正（`docs/reviews/2026-10-28-review-pline-03.md` D1 与小改；现在做）**：
  - P 卡在 standard 模式下允许 `segment` 与 `[prob]`；拟合接线按"策略 fittable"判断，不按 `evaluation_mode`；probability 卡只能走 `bt prob test`，screen、gate 拒绝；
  - validation 卡的评估切到 [split, cutoff)，训练窗 < split；G0 冻结模型只用 < split 部分的前 70%，并传 `fit_meta`；
  - 卡的 `[prob]` 与策略类常量逐项核对，不一致拒绝；
  - 轮次 `[hypotheses]` 与候选登记的假设交叉校验，重复条目拒绝；
  - 还原 H 线 `precheck` 判定那一行（`not cost_ok`）；
  - 补齐缺的测试：坏策略被 G0 查出；validation 卡在 < split 上评估被拒；`[hypotheses]` 列出候选没登记的假设被拒；standard 卡配拟合策略能拟合、能评估。
- [ ] T73B 失败测试先行：
  - 合成的非平稳数据上，各训练窗的模型哈希不同；每个训练窗末尾 K 根内的触发被 purge；
  - 非拟合策略的 walk-forward 与关卡 v3 结果逐位不变；
  - 样本外校准否决项：预测恒等于基准率的模型（技能分为 0）与反向信息的模型被否决，植入信息的模型通过。

## T74：局面库、动作库与决策策略 v1（M）

**Files:** 新建 `src/mt5quant/strategies/library/prob/__init__.py`、`situations.py`、`actions.py`、`features.py`、`level_decision.py`；测试 `tests/test_strategies_prob_*.py`。

- **局面**（蓝图 P 线 §2.1）：
  - S1 整数关口，网格取自卡的 `level_grid`；
  - S2 前一交易日高低点，按 17:00 America/New_York 划分；
  - S3 20 日高低点，按 20 × 24 根 H1 计，复用 B1 的通道计算。
  - "接近价位"按卡的 `approach`（M、D、d）判定。
- **动作**：A1–A4，阻力与支撑对称，参数取卡的 `breakout_atr` 与 `retest_window_bars`。
- **特征**：
  - `trend_sma200`：收盘价相对 SMA(200) 的上或下；
  - `atr_pct_2000`：ATR14 在最近 2,000 根中的分位，以 0.5 分高低；
  - 两者都只用有界窗口。
- **策略类 `LevelDecisionV1`**：
  - `fittable = True`；`fit` 调 `mt5quant.prob.labels` 与 `tables`；
  - `generate_signals` 每根 bar 找出触发，查表后用 `mt5quant.prob.decision.choose` 决策；
  - 入场后信号保持方向 K 根，然后归零（信号层持有上限）；持仓期间出现的新触发忽略；
  - 没装模型就调用 → ModelMissingError；
  - ExitRules：`stop_loss_atr = sl`、`take_profit_atr = tp`、`reentry = "after_signal_change"`；不声明 `time_stop_bars` 与 `week_close_exit`。
- **每张卡绑定一个子类**：止损止盈、局面、动作、网格都写成类常量，同 H003-v2 的子类做法。
- **回看声明**：`lookback_bars` ≥ max(2,000, 480 + M, 200) + K + 24。
- [x] **局面/动作/特征（不依赖 T73 拟合协议的部分）**：`situations.py`（Approach M/D/d + S1 网格/S2 前一交易日 17:00 NY 划界（DST 由 zoneinfo，EST 22:00/EDT 21:00 UTC 边界实测）/S3 B1 同法通道 shift(1)）+ `actions.py`（A1–A4 阻力支撑对称，b 与 m 取卡参数，A2 带 ±0.25 ATR）+ `features.py`（trend_sma200 / atr_pct_2000 有界窗口、未满窗 None）。测试 `test_strategies_prob_situations.py`（9）+ `test_strategies_prob_actions.py`（7）+ `test_strategies_prob_features.py`（4），共 20 个，先红后绿：A1–A4 触发与手算一致（阻力 `test_actions_hand_computed_resistance`、支撑 `test_support_actions_mirror_hand_case`）；镜像对称（价格围绕价位反射后 kind 互换、方向取反、bar 不变——`test_s1_support_touch_and_mirror`、`test_actions_mirror_symmetry`）。
  （CI 如实注：572396d 自身的 CI 因锚定测试同刻度偶发为红，与本任务内容无关；绿灯证据来自 d96c523。）
- [x] **审核修正（R12，2026-10-28）**："接近价位"定义更正——本根进入触及带；前 M 根都在带外且价位同侧（阻力 high_j < L − d·ATR_j），其中至少一根 ≥ D·ATR；蓝图 P 线 §2.1 同步更正；渐进接近测试（旧定义拒绝、新定义触发，test_s1_ramp_approach_accepted_new_definition），既有手算测试全部复绿。CI 绿灯 2df2ca6（tests run#97 success，全量 1981 passed, 1 skipped；编号已由审核二按 GitHub API 更正）。
- [x] **剩余项（T73A 后完成；LevelDecisionV1 + 7 测试，提交 8066cde，tests run_number 109 success；全量 2075 passed；夹具随 H 线 T82 接口变化的修复 32dc829/fb1d60b——run 113/114）**：
  - 持有到第 K 根归零；止损后同一次触发不再重入（经引擎验证）；
  - G0：冻结模型下截断一致，窗口不变性 ≥ 100 个随机探针；
  - 没装模型 → ModelMissingError；模型哈希不符 → ModelIntegrityError；
  - 连续亏损后手数不变（`basis = "initial"`；铁律 20）。
- [ ] **审核三修正（S4、S5、S8、D2；现在做）**：
  - 每次决策后第 i+K 根信号固定为 0，这一根上的新触发忽略（不链式续持）；
  - `fit` 不改实例状态（成本用局部变量算、写进模型）；
  - `fit` 的标签传与 bt 相同的入场政策；合约大小缺失时报错，不默认 1.0；
  - 决策把（局面, 动作, 方向, 估计, 成本）全部交给 `choose`；同一根上相反方向的触发都过门槛时不做；
  - 补齐缺的测试：每段同向信号 ≤ K 根、每笔交易持有 ≤ K 根；止损后同一次触发不再重入（经引擎验证）；连续亏损后手数不变；同一实例拟合两次结果相同；
  - 验收：复现脚本 `docs/reviews/probes/2026-10-28-pline-probes-03.py` 的 R3、R4 PASS。

## T75：概率模式评估与标定（L）——批准点

**Files:** 新建 `src/mt5quant/validation/prob_mode.py`、`scripts/prob_calibration.py`（分片运行、运行指纹，与 `gate_calibration.py` 同一套规范）；报告 `docs/research/prob-mode-calibration-<日期>.md`。

**Interfaces:**
```python
def randomize_triggers(triggers, bars, *, seed: int,
                       match=("ny_hour", "weekday", "atr_quantile")) -> pd.DataFrame
def delta_p_test(real_labels, null_labels, *, block: str = "month", sims: int, seed: int) -> dict
    # 单侧 Δp、p 值；同一天的多个触发先按日聚合
def holm(pvalues: dict[str, float]) -> dict[str, float]
def plant_delta(labels, delta: float, *, seed: int) -> pd.DataFrame
def brier_skill(p_pred, outcome, p_base) -> float
```

- **标定只用随机化触发与植入效应**，不生成任何真实局面触发的标签。
  - 随机时点的标签不得与计数工具输出的真实触发时刻做任何连接（连接就等于看了真实触发的结局）；
  - 为省时间，可以先把一批随机时点的标签算好，各种子从池中抽样；这个池只放在本次运行的临时目录里，运行结束删除。
- **时间安排**：代码与合成数据测试在 T66 批准前就可以写；正式标定运行与报告等 T66 批准后再做（T66 若被要求修改，先跟着调整）。
- **设计**（报告开头写清，先于运行）：
  - 品种：XAUUSD H1 必做（首批候选只用它，D53）；EURUSD H1 与合并口径可选，供以后加入 EURUSD 时使用；
  - 样本单位：按日聚合后的触发日数，与 `delta_p_test` 一致；
  - 负对照：触发日数 n ∈ {500, 1000, 2000}，每档 200 个种子，计算单假设误报率；12 个假设一组（与第一轮的结构一致：3 个局面 × 4 个动作，同一次随机化），计算 Holm 联合误报率；
  - 正对照：植入 Δp ∈ {0.02, 0.03, 0.05} × n ∈ {500, 1000, 2000, 4000}，每档 100 个种子，计算检出率；
  - P2 的样本外校准否决项：无信息特征下 Brier 技能分 > 0 的比例（应接近 0），以及植入信息下的检出率。
- **选择规则**（写进报告，先于结果）：
  - 负对照的单假设误报率与 Holm 联合误报率都 ≤ 5%，才采用；
  - 超出时，加长块长或收紧 α 后重新标定；
  - 报告检出率曲线，并据此写出"概率模式样本量规则"（单位为触发日数，给出 4 个假设与 12 个假设两种口径）；实测要求比蓝图 §3.4 的解析表更严的，以实测为准。
- [x] 失败测试先行（代码与合成数据测试完成——提交 7c4769b，tests run_number 110 success；全量 2085 passed；**正式标定运行与报告仍等 T66 批准**）：
  - 随机化后，小时、星期与 ATR 分位的分布与原触发一致（卡方检验，或逐桶计数相等）；
  - 同一种子结果逐位一致；
  - 合成数据上未植入效应时，误报率接近名义水平（1,000 次）；
  - 植入 Δp = 0.05、n = 2,000 时检出率 > 90%；
  - Holm 校正与手算一致。
- [ ] **审核三修正（S3、S7、S9；现在做）**：
  - `randomize_triggers` 的桶键加入 ATR 分位档（口径 `atr_pct_2000`）；同桶没有候选时拒绝；
  - `delta_p_test` 只用 outcome ∈ {win, loss, time} 的标签；p 值改为 (1 + k) / (1 + B)；
  - 补齐缺的测试：ATR 分位匹配；误报率 1,000 次；Δp = 0.05、n = 2,000 时检出率 > 90%（多个种子）；
  - 正式标定的设计里加一组成簇负对照（计数得到的真实触发时刻整体平移若干周，只看平移后时刻的标签）；月内置换替代 D42 的按月块自助，作为方法变更在报告里写明理由与标定结果；正式运行 B ≥ 10,000；
  - 验收：复现脚本 03 的 R2、R6 PASS。
- [ ] **报告**：误报率表、检出率曲线、样本量规则、校准否决项的标定结果。**提交后停下，等审核批准。**

## T76：检验力、分段工具与 P 线预筛（M）

**Files:**
- `src/mt5quant/research/power.py`：新增比例检验的检验力函数；
- `src/mt5quant/research/candidates.py`：`mechanism_class` 增加 `price_levels`，加入概率模式的预筛；
- `scripts/bt.py`：`bt candidate check` 支持概率模式；新增 `bt segment propose`；
- 只数触发的计数工具（放在 `src/mt5quant/prob/`）；
- 测试。

**Interfaces:**
```python
# mt5quant.research.power（单侧；以已知基准赢率为对照；Bonferroni）
def power_proportion_one_sided(p0: float, dp: float, n: int, *, alpha: float = 0.05,
                               n_tests: int = 1) -> float
def n_needed_proportion(p0: float, dp: float, *, alpha: float = 0.05, power: float = 0.8,
                        n_tests: int = 1) -> int
```

- **数值须复现蓝图 P 线 §3.4 的表**，例如：
  - `n_needed_proportion(0.40, 0.03, power=0.8)` = 1661；
  - `(0.40, 0.03, power=0.8, n_tests=6)` = 2808；
  - `(0.40, 0.05, power=0.5)` = 260。
- **概率模式的预筛**（在蓝图 v4 五条硬条件上具体化）：
  1. 文献：同蓝图 v4；
  2. 决策周期 ≥ H1；
  3. 事前成本：按文献折算的预期 Δp 换算成期望净收益（R），必须满足蓝图 P 线 §3.4 的"毛利 ≥ 3 倍成本"。成本取成本模型 v3 在触发所在纽约小时的来回成本，触发小时分布来自计数；
  4. 检验力：逐条假设，发现段内可用触发日数 ≥ 样本量规则（取 T75 实测与解析表中更严的一个）的 50% 检验力要求，否则该条 `waiting_data`（第二版按 D52 修订，见下方审核二修正）；
  5. 机制空缺：`price_levels` 是新类别。
- **计数不看结果**：计数工具只输出触发时刻、小时、品种，不调用标签与引擎。用静态检查证明它不 import `mt5quant.prob.labels`。
- **分界日期**：`bt segment propose --symbols ... --timeframe H1` 取研究库（含外部数据）中这些品种共同覆盖期的中点，取整到月初，结果确定。
- [x] 失败测试先行（CI 绿灯 3a772db，tests run#99 success，全量 2012 passed, 1 skipped；编号已由审核二按 GitHub API 更正）：
  - 检验力函数复现表中数值（`test_n_needed_reproduces_blueprint_table` 九格参数化 + `test_review_examples`）；
  - 预期 Δp 不够覆盖成本 → fail（`test_precheck_prob_cost_fail`）；触发数不足 → `waiting_data`（`test_precheck_prob_insufficient_triggers_waiting`）；
  - 同样的输入，分界日期相同（`test_propose_split_deterministic_and_month_floored`）；
  - 计数输出不含结果列（`test_count_output_has_no_result_columns` 同义用例 `test_count_output_has_no_outcome_columns`）；计数模块不 import 标签模块（AST 静态检查 `test_count_module_does_not_import_labels`）。
- [x] **实现摘要**：
  - `power.py` 新增 `power_proportion_one_sided`/`n_needed_proportion`（正态近似，z 取位与模块夏普口径同约定；蓝图 §3.4 九格全复现，含 6306 边界格 raw=6305.909）——tests/test_prob_power.py 10 例；
  - `mt5quant/prob/counting.py`：`count_triggers`（局面→事件→动作触发，输出 time/ny_hour/symbol/situation/action/side/level/kind/atr/close，无任何结局列）+ `triggers_before`（发现段子集）；AST 静态检查不 import `prob.labels`——tests/test_prob_counting.py 5 例；
  - `candidates.py`：`MECHANISM_CLASSES` 增 `price_levels`；`[prob]` 段装载校验（ProbDeclared：situations/hypotheses≤4/bracket/level_grid 全品种/approach/breakout_atr/retest_window_bars/expected_delta_p∈(0,0.5)；price_levels 研究候选必带 [prob] 且不带 [declared]）；`precheck_prob` 五条件（成本口径 ΔEV_R=Δp×(tp_R+1) ≥ 3×触发小时均值成本(R)；检验力按假设（局面×动作，跨品种/方向合并）发现段最小触发数 vs 解析样本量，**t75_rule_pending=True——T75 实测样本量规则批准后取两者更严，届时接线**）；`propose_split`（各品种计入段并集→共同覆盖最长段中点→取整月初）；
  - `bt.py`：`bt candidate check` 概率模式分支（逐品种×局面计数→发现段子集→precheck_prob；`--split` 可显式指定月初，缺省自动提案）+ `bt segment propose` 子命令（真实库冒烟：XAUUSD H1 → 2019-12-01）——tests/test_prob_precheck.py 15 例。
  - 计划测试行对应：复现表 `test_n_needed_reproduces_blueprint_table`（九格参数化）+ `test_review_examples`；成本 fail `test_precheck_prob_cost_fail`、触发不足 waiting `test_precheck_prob_insufficient_triggers_waiting`；分界确定 `test_propose_split_deterministic_and_month_floored`；计数无结果列 `test_count_output_has_no_outcome_columns`、不 import 标签 `test_count_module_does_not_import_labels`（AST）。
- [x] **审核二修正（`docs/reviews/2026-10-28-review-pline-02.md`；提交 0d92448，tests run_number 105 success；全量 2034 passed, 1 skipped；探针四项全 PASS——P1 r_win=1.950/EV=+0.2100R、P2 周五触发剔除、P3 117 事件/0.89 ATR、P4 成本逐品种 FAIL→PASS）**：
  - Q1 事前成本逐品种：每个品种都要满足 ΔEV ≥ 3 倍成本（等价于取成本最高的品种）；保留 `checks["ex_ante_cost_prob"]["passed"]` 这个键；补两品种测试（一便宜一贵，贵的不满足 → fail）；
  - Q2 佣金换算：`commission_per_lot / cs`（价格单位），再除以 `sl_atr × ATR` 换成 R；补一条非零佣金的手算测试；
  - Q3 检验力的多重校正：`n_tests` 取候选登记的假设条数（4）；给开轮复核提供同一函数的全轮口径（T77 用）；测试复现蓝图 P 线 §3.4 的 D52 表（4 个假设：3,014 / 1,340 / 483；12 个假设：4,176 / 1,856 / 669）；
  - Q6 样本单位与逐条判定：检验力样本数按发现段内的触发日数计；逐条判定，结果写进 `checks["power_prob"]` 的逐条明细；不足的假设判 waiting_data 并列出，候选至少一条通过才算通过；
  - `precheck_prob` 对每个条件都给出结果，不短路；不改 `tests/test_prob_precheck.py` 里 `prob_data`、`counted_df` 的调用方式（复现脚本在用）；
  - 已知限制写进预筛输出的 note：事前成本未计入隔夜费与止损越幅；基准赢率固定为 0.40（比按止损止盈推算的 1/3 保守）；
  - 验收：本任务测试与全量回归通过；`./.venv/Scripts/python.exe docs/reviews/probes/2026-10-28-pline-probes.py` 四项 PASS（P4 由 FAIL 转 PASS）；CI 绿，证据写提交号与 tests run_number。
- [ ] **收尾（T75 批准后）**：`t75_rule_pending` 改为 False；检验力取解析表与 T75 实测样本量规则中更严的一个，单位同为触发日数；测试覆盖"实测更严时以实测为准"。

## T77：P 线首批候选与第一轮草案（M）——批准点

**Files:** `research/candidates/C101.toml`–`C103.toml` 及对应的 `*.precheck.json`；`research/rounds/R2.toml`（草案，`approved_by` 留空；若届时 R2 已被占用，用下一个可用编号）；报告 `docs/research/candidates-P1-<日期>.md`。

**可以先做的部分（审核三）**：文献功课与预期 Δp 折算不碰数据，T75 批准前就可以做，候选文件草稿先提交；预期 Δp 提交之后，再跑计数（只数不看结果）。次序不能反，先看计数再定预期 Δp，会有把预期抬高来过检验力门槛的风险。计数结果与 D52 表的对照写进候选报告草稿；如果触发日数不够，按铁律 18 进"待数据"，不放宽标准。

- [ ] **文献功课**（每个候选）：
  - 原始文献、发表后或其他市场的复现、反驳，以及检索记录（检索词、数据库、日期）；
  - 起点：Osler（2003；纽约联储 Staff Report 125 版本）；
  - 数据挖掘方面的警示：Sullivan、Timmermann、White（1999）；
  - C102 找不到文献就判 fail，不开卡。
- [ ] **候选内容**：
  - 品种只用 XAUUSD（D53）；
  - 按蓝图 P 线 §7 登记本局面的全部四个动作，方向由动作定义决定（D51）：`S#:A1:follow>base`、`S#:A2:follow>base`、`S#:A3:fade>base`、`S#:A4:fade>base`；不得依据计数或任何看过的数据增删动作、改方向；
  - 预期 Δp 只按文献折算，没有复现时按 −58%；不得取自发现段的结果（发现段就是 P1 的检验数据）；
  - 跑 `bt candidate check`（审核二修正后的版本）。
- [ ] **轮次草案**：
  - 候选 ≤ 3；`[evaluation]` 每个候选 `{ mode = "probability", primary = "delta_p_one_sided" }`；
  - `[segments] split` 写入前重跑 `bt segment propose --symbols XAUUSD --timeframe H1` 确认（审核时为 2019-12-01）；
  - 试验预算 = 本轮假设条数（≤ 12 次去重试验）；`lockbox_uses_max = 0`（P1 不用锁箱，需 T73A）；`dsr_trial_scope` 按 T64 的项目级口径；
  - 本轮假设表 = 各候选预筛通过的假设；再按全轮条数做一次检验力复核（D52，只做一遍），不足的假设判 waiting_data 移出；最终假设表写进轮次文件的 `[hypotheses]`，作为 Holm 的假设族；
  - 推进规则：Holm 校正后显著，且 Δp 点估计满足事前成本要求；
  - 结束条件：全部假设有结论，或预算用尽；
  - `approved_by` 留空，由 P 线审核会话批准时填写。
- [ ] **候选报告**：材料对比表，列出机制、文献三栏、折算效应、事前成本（逐品种）、逐条假设的发现段触发日数与检验力（4 条与全轮两种口径）、预筛结论。**提交后停下，等 P 线审核会话批准；批准前不开卡、不跑任何运行。**

## T78：风控号段子上限与 P 线实盘接入（M，阶段 F 结束后，H 线会话负责）

**Files:**
- `src/mt5live/riskproc.py`；`config/risk.toml`、`config/risk-demo.toml`：新增 `[[line_caps]]`；
- `src/mt5live/config.py`：`LiveConfig` 增加 `model_path`、`model_sha256`；
- `src/mt5live/runner.py`：拟合策略启动时装载并校验模型；
- `demo-configs/`：P 线示例配置；
- 测试（单元 + 假件终端集成）。

- **号段子上限**：
  - 配置格式：`[[line_caps]] name / magic_min / magic_max / daily_loss_cap_usd`；缺省 H 线 902000–902999 与 P 线 903000–903999 各 250 美元；
  - 触发时：该号段全部 magic 写 `pause-<magic>.json`（reason = line_cap），同时告警，不强平；
  - 账户级 `daily_loss_cap_usd = 500` 不变，仍然全平并停机。
- **模型装载**：
  - `model_path` 与 `model_sha256` 必须同时给出；
  - 哈希不符或文件缺失 → 拒绝启动（fail-closed）；
  - 非拟合策略的配置不受影响。
- [ ] 失败测试先行：
  - 子上限触发只暂停该号段，另一号段照常开仓；
  - 账户上限仍然全平；
  - 模型哈希不符 → 拒绝启动；
  - 非拟合策略配置逐字段行为不变；
  - 执行器对 `time_stop_bars`、`week_close_exit` 的启动拒绝保持不变。

## T90：P1 运行入口 `bt prob test`（M）

**Files:** 新建 `src/mt5quant/prob/p1.py`（编排：计数 → 标签 → 随机化零假设 → Δp 检验）；`scripts/bt.py` 新增 `bt prob test` 与 `bt prob round-report`；测试 `tests/test_prob_p1.py`。

**时间安排**：T73A 与 T75 的代码完成后就可以写，只用合成数据测试；真实数据上的运行属于 T91，要等 T77 批准。

- **`bt prob test --card <P###-v#>`**：
  - 前置检查，任一不满足就拒绝运行：
    - 卡已提交并登记（先卡后测）；
    - 卡的 `evaluation_mode = "probability"`、`segment = "discovery"`；
    - 所属轮次能装载（`approved_by` 非空），且该候选的 `[evaluation].mode = "probability"`；
    - 卡的假设表与轮次文件 `[hypotheses]` 里该候选的条目一致；
    - 本轮剩余试验预算够用；
  - 数据：只读 `time < split` 的 bar；标签取 `train_end = split`，越过分界的触发按 bar 下标剔除；
  - 每条假设：
    - 真实触发 → 标签；
    - 随机化触发（T75 的 `randomize_triggers`；种子由卡号与假设确定性生成，并记录）→ 标签；
    - `delta_p_test`（按日聚合、按月块自助）→ 原始 p 值；
  - 一张卡的全部假设一次跑完，不能只跑其中几条；
  - 台账（T73A 的字段约定，不改表结构）：
    - 每条假设一行 run，`run_class = "research"`，带 `round_id`；
    - `params_json` 含假设、止损止盈、`segment`、`split`、种子；
    - `metrics_json.metrics` 含触发次数与触发日数、真实与零假设赢率、Δp、单侧 p 值、`prob_version`；
    - 相同输入重跑按现有去重规则不增加 N。
- **`bt prob round-report <R#>`**：
  - 本轮全部卡都跑完才能出；
  - 在 `[hypotheses]` 列出的全部假设上做 Holm，按轮次文件的推进规则判定；
  - 输出 `docs/research/round-<R#>-p1-<日期>.md`（报告头"状态：待审核"）与同名 JSON。
- [x] 失败测试先行（只用合成数据；7 测试，绿灯 fb1d60b run_number 114——f58118a run#112 与 32dc829 run#113 因测试夹具未跟上 H 线 T82 的 overnight_fee price 形参而红，业务代码无误；全量 2108 passed, 1 skipped）：
  - 植入 Δp = 0.05、触发日数 2,000 → 检出；未植入 → 不检出（固定种子）；
  - 前置检查逐条：未批准的轮次、validation 卡、读到 `>= split` 的数据、假设表与 `[hypotheses]` 不一致、预算不足 → 拒绝；
  - 越过分界的触发被剔除；
  - 同一输入两次运行结果逐位一致，台账去重 N 不变；
  - 有卡未跑时 round-report 拒绝；Holm 与手算一致。
- [ ] **审核三修正（S1、S2、S6、S8；现在做）**：
  - `bt prob test` 补 `--actor`、`--ledger`，`_SP` 改为 `SESSIONS_PATH`；
  - 走 `_round_context`（轮次已开启、未关闭、候选预筛 pass），并核对轮次文件 sha256 与开启时入账的 `toml_sha256` 一致；
  - 标签传与 bt 相同的入场政策；合约大小缺失时报错；
  - 试验身份（`params_json.prob`）加入评估代码版本；
  - `round-report`：成本条件取候选预筛 `ex_ante_cost_prob.cost_r_by_symbol` 的最大值，按 ΔEV = Δp̂ × (tp_R + 1) ≥ 3 × 成本判定，报告分列"Holm 显著"与"成本条件"；一条假设有多条成功运行时拒绝；按候选与卡号过滤；用上 `--rounds-dir`；
  - 补齐缺的测试：端到端命令行测试（临时 git 仓库、已提交的卡、已开启的轮次、合成数据），覆盖 `bt prob test` 与 `round-report`；越过分界的触发被剔除（经 `run_p1_card`）；有卡未跑时 round-report 拒绝（经命令）；同一输入重跑台账去重 N 不变；
  - 验收：复现脚本 03 的 R1、R5 PASS。

## T91：P1 现象检验轮（M）——批准点

**前提：** T77 已获 P 线审核会话批准（轮次文件的 `approved_by` 已由审核会话填写）；T90 完成。

- [ ] **开卡**：
  - 每个进入本轮的候选一张 P 卡（`P001-v1` 起，按蓝图 P 线附录 A），绑定 T74 的局面与动作；
  - 假设表与轮次文件 `[hypotheses]` 一致；卡里不出现任何结果；
  - 先提交卡，再运行。
- [ ] **运行**：逐卡 `bt prob test`，然后 `bt prob round-report`。
- [ ] **轮次报告**：
  - 逐条假设：触发日数、真实与零假设赢率、Δp 及区间、原始 p、Holm 校正后的 p、推进结论；
  - 同一套代码上的对照复核：植入效应能检出，随机化触发不检出；
  - 已知限制；
  - 不通过的假设按现有规则进墓地。
- 看完结果后不得改卡重跑。任何改动只能作为新版本，在新一轮登记。
- **报告头"状态：待审核"；提交后停下，交 P 线审核会话。**

## T92：P2 决策系统（L）——两个批准点

**前提：** T91 已批准且至少一条假设通过；T73B、T74 收尾完成。

- [ ] **决策卡**：
  - `segment = "validation"`、`evaluation_mode = "standard"`，绑定 `LevelDecisionV1` 的子类；
  - 只用 P1 通过的"局面 × 动作"；
  - 特征、收缩与门槛取蓝图 P 线 §2.4–§2.5 的缺省值（m = 100、min_cell = 100、lb_z = 1.0、门槛 2 倍成本、两个特征），不调参；要偏离缺省值，只能依据发现段，并在草案里写明依据。
- [ ] **轮次草案**：
  - 新的轮次编号；
  - `[evaluation]` 的主检验按 T65 批准的关卡 v3 填写；
  - `lockbox_uses_max = 1`（G5 在本轮内使用，见 T93）；试验预算 = 决策卡张数；
  - `[segments] split` 与 P1 轮相同；
  - **提交后停下，交 P 线审核会话批准（第一个批准点）。**
- [ ] **运行（草案批准后）**：
  - 验证段 walk-forward，逐窗重新拟合 + purge（T73B）；
  - 关卡 v3 主检验、否决项、样本外校准否决项；诊断。
- [ ] **轮次报告**：`docs/research/round-<R#>-p2-<日期>.md`，含：
  - 各窗口的模型哈希；
  - 校准（Brier 技能分）；
  - 成本敏感性与交易次数。
  - **报告头"状态：待审核"；提交后停下（第二个批准点）。**

## T93：确认——G5、影子盘与前向样本（M，持续 6–12 个月）——批准点

**前提：** T92 已批准；T78 已由 H 线会话落地。

- [ ] **G5**：
  - 先查 D16 剩余预算：每个锁箱 3 次、每轮 ≤ 1 次，两条线共用；
  - **每次使用前停下，交 P 线审核会话批准**；
  - 按现有锁箱流程：先写 started，再读锁箱数据。
- [ ] **部署准备（G5 通过后）**：
  - 模型：用 T92 草案写定的最终拟合窗拟合一次，哈希记入台账；
  - 准备模型文件、哈希，以及影子盘（magic 903001–903099）与模拟盘（903101–903199）的配置草案；
  - 写交接说明：文件、哈希与配置。
- 部署与运行由 H 线会话执行（实盘链路、`data/live/`、`config/` 归 H 线）。P 线会话不启动、不停止任何实盘进程。
- [ ] **前向样本**：
  - 每月一份 `docs/research/forward-<卡>-<年月>.md`：交易次数、实测赢率与概率表预测的对比（Brier）、实盘与回测对账、成本偏差；
  - 6–12 个月后按 D37 下结论，交 P 线审核会话。
- 只用模拟盘，不进真金。

---

## 集成与验收

1. 每个任务结束：全量 pytest 全绿 → 提交 → 推送 → Actions 为绿 → 复选框写上证据。
2. 基础设施（T72–T74）结束，以下都由测试证明：
   - 标签与引擎逐笔一致；
   - 非拟合策略逐位不变；
   - 拟合不越界，purge 生效；
   - 冻结模型的拟合策略能过 G0；
   - 分段约束生效。
3. 评估（T75）：标定报告给出误报率、检出率与样本量规则，交审核。
4. 首批候选（T76–T77）：候选报告与轮次草案交审核；批准后才进入阶段 P1。
5. 实盘接入（T78，H 线会话）：P 线进入前向样本之前完成。
6. P1 运行入口（T90）：前置检查与 purge 由测试证明；合成数据上植入效应能检出、零假设不检出。
7. P1（T91）：轮次报告交审核；只有通过的假设能进入 P2。
8. P2（T92）：轮次草案与轮次报告都交审核；样本外校准否决项生效。
9. 确认（T93）：每次 G5 前经审核批准；前向样本按月报告。

## 明确不做

- 修改 `engine_a.py`、`engine_b.py` 的成交与出场语义；
- 每个动作各用一套止损止盈；限价单、突破挂单；按信心调整手数（这些都是 v2，D44、D46、D48）；
- 马丁、网格、摊平（铁律 20）；
- 机器学习模型；LLM 参与逐笔决策；
- 在批准点之前推进依赖它的任务；
- 实盘真金下单；修改现行 `config/gates.toml`（只能经 T65 批准）；
- 改台账 runs 表结构（P 线字段放进已有的 JSON 列，见 T73A）；
- P 线会话部署、启停实盘进程（归 H 线会话）。
