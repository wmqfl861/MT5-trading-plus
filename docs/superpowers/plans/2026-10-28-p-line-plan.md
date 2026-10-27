# P 线施工计划（2026-10-28）：概率决策线 v1（T71–T78）

> **For agentic workers:** 按本文"总执行顺序"推进（它同时排好了第十一轮计划 T61–T70 的顺序）。每个任务的流程：
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
- 风控号段子上限与实盘接入（阶段 F 之后）。

**输入文档（先读）：**
1. `docs/blueprint-p-line.md`：P 线蓝图 v1，包括框架、研究流程、铁律 20–22、决策 D38–D50、附录模板。
2. `docs/blueprint.md`（v4）：§2 当前禁止事项、§4 铁律、§6 研究方向、§8 决策（含按审核建议代用户作出的决定）。
3. `docs/superpowers/plans/2026-10-28-round11-research-process.md`：第十一轮计划 T61–T70。本计划多个任务依赖它。
4. `docs/ops/demo-launch-proposal-2026-10-27.md`：阶段 F 启动方案。
5. 代码现状：
   - `src/mt5quant/backtest/engine_a.py` 文件头（成交、成本、SL/TP 语义）；
   - `src/mt5quant/strategies/base.py`（策略接口、ExitRules）；
   - `src/mt5live/executor.py`（启动时拒绝 `time_stop_bars` 与 `week_close_exit`）；
   - `src/mt5quant/validation/walkforward.py`、`src/mt5quant/research/cards.py`（卡模式严格拒绝未知键）。

**基线：** 提交 `b77b040`（第十一轮文档）；GitHub Actions 1728 passed、2 skipped。

## Global Constraints（所有任务隐含遵守）

- **P 线禁止事项**（蓝图 v4 §2）：
  - T75 标定报告与 T77 轮次草案经审核批准之前，不在真实局面触发上计算任何标签、拟合或 Δp。
  - 在此之前允许做的：合成数据上的单元测试；用随机时点、随机化触发和植入效应做对账与标定；只数触发次数、不看结果的计数（T76）。
- **不改现有执行语义**（D44）：
  - T71–T77 不修改 `src/mt5quant/backtest/engine_a.py`、`engine_b.py`、`src/mt5live/`、`src/mt5gw/`；
  - T78 等阶段 F 结束后才做。
- **非拟合策略逐位不变**：凡改到 `bt`、关卡、walk-forward、G0、台账的任务，都要证明现有策略（B1–B6、MaCrossATR）的结果逐位不变。
- **标定期间不改被标定的代码路径**：T65、T66、T75 的标定分片运行期间，不合并会改变其代码路径的改动。
- **依赖**：不新增第三方依赖（正态分位用标准库 `statistics.NormalDist`）。
- **脱敏**（铁律 9）：报告、日志不含账号、服务器名、IP、令牌、本机绝对路径。
- **测试命令**（Git Bash）：`./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`
- **提交**：消息格式 `<type>(T##): 摘要`；不改写 git 历史、不强推、不改仓库可见性。

## 总执行顺序（两份计划合并）

| 顺序 | 任务 | 所属 | 依赖 | 备注 |
| --- | --- | --- | --- | --- |
| 0 | 阶段 F 启动 | 运维 | — | 按启动方案，2026-10-28 开盘后启动（蓝图 v4 §8，D49 授权）；先做方案里的启动前检查单 |
| 1 | T70 脱敏与杂项 | 第十一轮 | — | |
| 2 | T61 基线重新定位 | 第十一轮 | — | |
| 3 | T62 候选登记与预筛 | 第十一轮 | T61 | |
| 4 | T72 首达标签与概率表 | P 线 | — | 只新增 `src/mt5quant/prob/`，可与 T62、T63 交替 |
| 5 | T63 轮次预注册 | 第十一轮 | T62 | |
| 6 | T67 外部数据 | 第十一轮 | — | 下载耗时，交替推进 |
| 7 | T64 项目级 N | 第十一轮 | T63 | |
| 8 | T66 事件模式与标定 | 第十一轮 | T64 | 批准点 |
| 9 | T65 关卡 v3 与标定 | 第十一轮 | T64、T66 | 批准点 |
| 10 | T68 首轮候选与 R1 草案 | 第十一轮 | T62、T63、T65、T66、T67 | 批准点 |
| 11 | T73 拟合钩子与分段 | P 线 | T63、T64、T65（标定跑完） | |
| 12 | T74 局面库与动作库 | P 线 | T72、T73 | |
| 13 | T75 概率模式标定 | P 线 | T66、T72、T74 | 批准点 |
| 14 | T76 检验力与 P 线预筛 | P 线 | T62、T67、T75 | |
| 15 | T77 P 线候选与轮次草案 | P 线 | T63、T75（已批准）、T76 | 批准点 |
| 16 | T69 规则文档 + T71 P 线规则 | 两份 | 各阶段结束时 | |
| 17 | T78 风控号段子上限与实盘接入 | P 线 | 阶段 F 结束 | |

规模：S ≈ 半个工作日，M ≈ 1 个工作日，L ≈ 2 个工作日及以上，仅供排序。

## 批准点（D49）

用户已授权按审核建议决策，但批准不由执行 agent 作出。到达批准点时：

1. 把报告或草案提交并推送，报告开头写"状态：待审核"，列出预先写定的判定标准和实测结果；
2. 在会话里告诉用户："请把 <报告路径> 交给审核会话"；
3. 停在这里，不进入依赖它的任务。其他不依赖它的任务可以继续。

批准点包括：

- T65、T66 标定报告；
- T68 的 R1 草案；
- T75 标定报告；
- T77 候选与轮次草案；
- 每一次 G5 使用之前；
- 每一轮结束报告（进入下一阶段之前）。

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

## T71：P 线规则与权限（S）

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
- [ ] 失败测试先行：
  - 合成数据上随机抽 200 个**随机时点**（不是局面触发）：标签与引擎 A 单笔结果逐笔一致（出场原因映射、`net_r` 与 `r_multiple` 差 < 1e-12）；
  - 同一根 bar 同时触及止损和止盈 → loss；开盘跳空穿过止损 → loss（stop_gap）；禁入窗推迟入场与引擎一致；手数为 0 → skipped；
  - purge：标签窗口越过 `train_end` 的触发被剔除；
  - 收缩：n = 0 的格等于基准，n 很大时趋近经验频率；不足 `min_cell` 的格退回上一级；
  - `ev_lower` 随 z 单调不增；全部低于门槛 → None；并列时取序最前者；
  - 模型文件往返一致，同一输入两次生成的哈希相同，哈希不符 → ModelIntegrityError。
- [ ] **真数据对账证据**（本机运行，不进 CI）：XAUUSD H1 研究库上随机时点 200 个，逐笔一致，日志路径写进复选框。随机时点不是研究结论，不违反禁止事项。

## T73：拟合钩子、分段与模型身份（L）

**Files:**
- `src/mt5quant/strategies/base.py`：新增可选协议，`fittable = False` 为缺省；
- `src/mt5quant/research/cards.py`：P 卡字段 `segment` 与 `[prob]` 段，`evaluation_mode = "probability"`；
- `src/mt5quant/research/ledger.py`：runs 增加 `model_sha256`、`prob_version`、`fit_start`、`fit_end`、`segment`，迁移时保持哈希链；
- `scripts/bt.py`：新增 `bt fit`；screen 与 gate 自动拟合；分段约束；
- `src/mt5quant/validation/walkforward.py` 与关卡 v3 主检验：逐窗重新拟合 + purge；
- `src/mt5quant/validation/lookahead.py`：G0 用冻结模型；
- 测试。

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
- [ ] 失败测试先行：
  - 非拟合策略逐位不变；
  - 拟合不读 `train_end` 之后的数据，最后 K 根的触发被 purge；
  - 合成的非平稳数据上，各训练窗的模型哈希不同；
  - 冻结模型的拟合策略能过 G0；故意在 `generate_signals` 里现场统计全部历史的坏策略被 G0 查出；
  - discovery 卡读 `>= split` 被拒；validation 卡在 `< split` 上评估被拒；
  - 台账迁移后 `bt ledger verify` 通过。

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
- [ ] 失败测试先行：
  - 合成价格路径上，A1–A4 的触发与手算一致（阻力、支撑各一组）；
  - 价格序列取反后，动作与方向镜像对应；
  - 持有到第 K 根归零；止损后同一次触发不再重入（经引擎验证）；
  - G0：冻结模型下截断一致，窗口不变性 ≥ 100 个随机探针；
  - 没装模型 → ModelMissingError；模型哈希不符 → ModelIntegrityError；
  - 连续亏损后手数不变（`basis = "initial"`；铁律 20）。

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
- **设计**（报告开头写清，先于运行）：
  - 品种：XAUUSD H1、EURUSD H1，以及两者合并；
  - 负对照：触发数 n ∈ {500, 1000, 2000}，每档 200 个种子，计算单假设误报率；6 个假设一组，计算 Holm 联合误报率；
  - 正对照：植入 Δp ∈ {0.02, 0.03, 0.05} × n ∈ {500, 1000, 2000, 4000}，每档 100 个种子，计算检出率；
  - P2 的样本外校准否决项：无信息特征下 Brier 技能分 > 0 的比例（应接近 0），以及植入信息下的检出率。
- **选择规则**（写进报告，先于结果）：
  - 负对照的单假设误报率与 Holm 联合误报率都 ≤ 5%，才采用；
  - 超出时，加长块长或收紧 α 后重新标定；
  - 报告检出率曲线，并据此写出"概率模式样本量规则"；实测要求比蓝图 §3.4 的解析表更严的，以实测为准。
- [ ] 失败测试先行：
  - 随机化后，小时、星期与 ATR 分位的分布与原触发一致（卡方检验，或逐桶计数相等）；
  - 同一种子结果逐位一致；
  - 合成数据上未植入效应时，误报率接近名义水平（1,000 次）；
  - 植入 Δp = 0.05、n = 2,000 时检出率 > 90%；
  - Holm 校正与手算一致。
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
  4. 检验力：发现段内可用触发数 ≥ 样本量规则（取 T75 实测与解析表中更严的一个）的 50% 检验力要求，否则 `waiting_data`；
  5. 机制空缺：`price_levels` 是新类别。
- **计数不看结果**：计数工具只输出触发时刻、小时、品种，不调用标签与引擎。用静态检查证明它不 import `mt5quant.prob.labels`。
- **分界日期**：`bt segment propose --symbols ... --timeframe H1` 取研究库（含外部数据）中这些品种共同覆盖期的中点，取整到月初，结果确定。
- [ ] 失败测试先行：
  - 检验力函数复现表中数值；
  - 预期 Δp 不够覆盖成本 → fail；触发数不足 → `waiting_data`；
  - 同样的输入，分界日期相同；
  - 计数输出不含结果列；计数模块不 import 标签模块。

## T77：P 线首批候选与第一轮草案（M）——批准点

**Files:** `research/candidates/C101.toml`–`C103.toml` 及对应的 `*.precheck.json`；`research/rounds/R2.toml`（草案，`approved_by` 留空；若届时 R2 已被占用，用下一个可用编号）；报告 `docs/research/candidates-P1-<日期>.md`。

- [ ] **文献功课**（每个候选）：
  - 原始文献、发表后或其他市场的复现、反驳，以及检索记录（检索词、数据库、日期）；
  - 起点：Osler（2003；纽约联储 Staff Report 125 版本）；
  - 数据挖掘方面的警示：Sullivan、Timmermann、White（1999）；
  - C102 找不到文献就判 fail，不开卡。
- [ ] **候选内容**：
  - 按蓝图 P 线 §7 写明局面、品种、要检验的"动作 × 方向"（每个候选 ≤ 4 条，全轮 ≤ 12 条）；
  - 预期 Δp 按文献折算，没有复现时按 −58%；
  - 跑 `bt candidate check`。
- [ ] **轮次草案**：
  - 候选 ≤ 3；`evaluation_mode = "probability"`；`segment = "discovery"`；
  - `[segments] split` 取自 `bt segment propose`；
  - 试验预算 ≤ 12 次去重试验；`lockbox_uses_max = 0`（P1 不用锁箱）；
  - 推进规则：Holm 校正后显著，且 Δp 点估计满足事前成本要求；
  - 结束条件：全部假设有结论，或预算用尽。
- [ ] **候选报告**：材料对比表，列出机制、文献三栏、折算效应、事前成本、触发数与检验力、预筛结论。**提交后停下，等审核批准；批准前不开卡、不跑任何运行。**

## T78：风控号段子上限与 P 线实盘接入（M，阶段 F 结束后）

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
5. 实盘接入（T78）：P 线进入前向样本之前完成。

## 明确不做

- 修改 `engine_a.py`、`engine_b.py` 的成交与出场语义；
- 每个动作各用一套止损止盈；限价单、突破挂单；按信心调整手数（这些都是 v2，D44、D46、D48）；
- 马丁、网格、摊平（铁律 20）；
- 机器学习模型；LLM 参与逐笔决策；
- 在批准点之前推进依赖它的任务；
- 实盘真金下单；修改现行 `config/gates.toml`（只能经 T65 批准）。
