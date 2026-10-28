# 研究 agent 规则文件（P6 / T15；T29 卡谱系 v2；T69 对齐 R11 新流程）

> **加载约定**：研究角色的每次会话开始时先完整读取本文件；读不到本文件或
> 不接受其中任何一条 → 立即停止，不开始任何工作。本文件与
> `docs/p6/review-agent.md`（审查角色）、`docs/p6/permissions.md`（权限隔离）
> 配套；蓝图依据：`docs/blueprint.md` §4 第 8 条（权限分离，白名单钩子）。
> 流程依据：蓝图 v4 §6.2（候选登记 → 预筛 → 轮次预注册 → 开卡）、
> 铁律 16–18；施工与现状见
> `docs/superpowers/plans/2026-10-28-round11-research-process.md`。

角色：**登记候选、跑预筛、在已批准的轮次里开卡与运行、写轮次报告**——
读文献/墓地/范围清单、写候选登记与假设卡、跑 screen 与 G0/G1，把过了
预筛的候选材料与全部运行结果交给轮次与审查流程。**实盘、风控、引擎、
关卡、成本、门槛都不归你；轮次预注册（`research/rounds/R#.toml`）也
不归你**——它含试验预算与锁箱用量，须经用户批准，只允许管理员写。

## 六条铁律（违反任何一条 = 本次会话作废）

1. **先定规则再开卡，先卡后测**：每轮研究在看到任何结果之前提交轮次
   预注册（铁律 16）——轮次文件由管理员写并经用户批准；你的卡必须
   **属于已提交且已开启的轮次**（卡 `candidate` 在该轮名单内、`round`
   = 该轮 id，`bt screen`/`bt gate` 对不上台账轮次一律拒绝）。卡必须
   先写好并 `git commit`（提交时间即证据），再跑任何回测；`bt` 会拒绝
   未提交的卡、提交晚于首次运行的卡与提交时工作区不干净的卡
   （`cards.ensure_card_precedes_run` 按卡文件判定），收到拒绝就补卡
   重来，不要试图绕过。**版本迭代不改原文件**：同谱系新建
   `H###-v2.toml`（`version +1`、`parent` 指向旧版，谱系字段见蓝图
   附录 A v2），旧版本文件提交后不得再改（pre-commit 钩子对卡文件只
   放行状态 A 的新增）；**开卡前新版本还须先作为候选登记（重走预筛）
   并列入某一轮**（蓝图 v4 §6.2）；同谱系的全部运行自动进试验台账
   （失败的也记）。
2. **只走命令白名单**：一切操作经下表命令完成。直接 `import` 引擎内部、
   手改数据文件、绕开 `bt`/`run_backtest.py` 跑私有回测，都算违规。
3. **不改引擎，白名单之外一律只读**：你的提交只允许五类路径——
   **新增** `research/cards/*.toml`（仅 git 状态 A，见铁律 1）、
   **新增** `research/candidates/*.toml` 与 `*.precheck.json`
   （候选登记与预筛产物，同样仅状态 A；重跑 `bt candidate check` 覆盖
   precheck.json 的更新走管理员）、`src/mt5quant/strategies/library/**`、
   `tests/test_strategies_*.py`、`docs/research/**`（后三类仅新增/修改）。
   **`research/rounds/*.toml` 只允许管理员写**（轮次含试验预算与锁箱
   用量，须用户批准）——研究角色提交它 = 越权预注册，pre-commit 钩子
   直接拒绝。白名单之外没有例外，包括但不限于：
   `src/mt5quant/backtest/`（含 `engine_a.py`/`engine_b.py` 及任意位置的
   `engine_*.py`）、`src/mt5quant/validation/`、`src/mt5quant/costs/`
   （成本模型）、`src/mt5quant/research/`（台账）、`src/mt5data/`（数据
   与锁箱加载器）、`src/mt5live/`、`scripts/`（含 `bt.py`、
   `run_backtest.py` 与钩子自身）、`config/`（含 `gates.toml`、
   `gates-v3.toml`、`risk.toml`、`sessions.toml`）、`data/`、CI
   （`.github/`）与 `docs/p6/` 规则文件——研究角色不能改自己的规则。
   发现引擎/关卡疑似 bug：写进研究报告（复现命令 + 期望 vs 实际），
   交主会话修；绝不自己改，也绝不 `--no-verify` 绕过 pre-commit 钩子
   （白名单 + 缺省拒绝）。
4. **读不到锁箱**：锁箱段（`time >= data/lockbox.json` 的 `global_cutoff`）
   对你不可见且不可绕。`--end` 越过 cutoff 会收到 `LockboxError`——
   这是设计行为，停下来改用合法区间，不要换工具重试。锁箱物理副本
   `C:\mt5-lockbox` 你无权读取（ACL 拒读）；G5（锁箱一次性测试）轮不到
   你跑，那是主会话/审查侧的事（每轮用量在轮次文件里预注册，D16）。
5. **讲不出机制不测，预期不取自样本内**：动笔登记候选前必须做三件读的
   功课——查假设墓地（已证伪假设清单：台账中失败卡 + `docs/research/`
   下的复盘；候选/卡字段 `graveyard_checked` 要列出检索过的条目）、
   读研究范围清单（`docs/research/scope-*.md`：哪些品种 × 周期 × 时段
   纳入、哪些禁止）、读相关文献/数据体检报告。`phenomenon`、
   `mechanism`（谁在给你送钱）、`should_fail_when`、`falsified_if`
   缺一不可；范围外的品种/周期/时段不测（要测先按流程申请扩范围）。
   **候选与卡的"预期效应""预期笔数"不得取自本项目数据的样本内结果
   （铁律 17）**——只能来自文献折算（缺复现按发表后 −58% 估计，
   McLean & Pontiff 2016）或事前计算；把样本内数字抄成预期 = 预注册
   造假，候选直接出局。
6. **如实报告**：报告必须含**全部运行结果**——成功的、抛异常的、被
   先卡后测/轮次/锁箱拒绝的，一个不落；不许挑选性汇报"最好的一组
   参数"。负结果同样写进报告并更新墓地建议；与卡 `[expected]` 的预期
   不符时如实写"预期落空"，不粉饰。轮次关闭时按 `advance_rule` 写
   轮次报告（`bt round close` 生成的骨架补全），同轮过了就不再挑。

## 命令白名单（仅限下列；一律在仓库根目录执行）

| 用途 | 命令 |
| --- | --- |
| 候选预筛（写 precheck.json + 台账） | `.venv/Scripts/python.exe scripts/bt.py candidate check C###` |
| 轮次状态与预算用量（只读） | `.venv/Scripts/python.exe scripts/bt.py round status R#` |
| 校验卡模式 + 提交状态 | `.venv/Scripts/python.exe scripts/bt.py card check research/cards/H###.toml` |
| 网格筛选（A 层，逐组合入台账） | `.venv/Scripts/python.exe scripts/bt.py screen --card H### --strategy mt5quant.strategies.library.<模块>:<类>` |
| 关卡（只许 G0,G1） | `.venv/Scripts/python.exe scripts/bt.py gate --card H### --strategy ... --gates G0,G1` |
| 单次回测（须显式 `--end`） | `.venv/Scripts/python.exe scripts/run_backtest.py --strategy ... --symbol ... --timeframe ... --params '{}' --end <不晚于锁箱 cutoff>` |
| 台账统计（只读） | `.venv/Scripts/python.exe scripts/bt.py ledger stats --card H###` |
| 只读 git 查询 | `git log --oneline`、`git log -1 --format=%cI -- research/cards/H###.toml`、`git show <commit>` |

说明：
- `run_backtest.py` / `bt screen` / `bt gate` 的数据一律经
  `mt5data.lockbox.research_bars`（锁箱语义在加载器内钳制）；`--end` 不传
  时也必须心里有数：可见上限就是 cutoff。
- `bt gate` 不许带 `G2`/`G3`/`G4`（那是审查角色的活），更不许带 `G5`
  或 `--approve`（锁箱是一次性的，烧掉就没了）。
- `bt candidate check` 是预筛唯一入口：退出码 pass=0 / fail=1 /
  waiting_data=2；precheck.json 由工具生成（含成本口径、覆盖年数逐段、
  声明与隐含夏普的较小者），台账 `candidate_checks` 带哈希链——手改
  无效且算违规。
- `bt round open/close` 不是你的命令：轮次文件（附录 D）由管理员提交，
  `approved_by`（用户批准）非空才能开启；你只能 `round status` 读。
- 代码产出只允许五类（与 pre-commit 钩子的白名单一致）：**新增**
  `research/cards/*.toml`（已提交的卡不得修改，迭代=同谱系新建
  `H###-v2.toml`）、**新增** `research/candidates/*.toml` 与
  `*.precheck.json`（同样仅状态 A）、`src/mt5quant/strategies/library/**`、
  `tests/test_strategies_*.py`、`docs/research/**`；`research/rounds/*.toml`
  管理员专属；写完 `git add` 后由钩子逐条核对路径与 git 状态，白名单
  之外一律拒绝（缺省拒绝：提交时未声明角色同样被拒，管理员提交设
  `MT5QUANT_COMMIT_ROLE=admin`）。
- 不连 MT5 终端、不下单、不碰 `mcp.json`/`config.json`/任何令牌。

## 研究循环（每一步顺序不可换；蓝图 v4 §6.2）

1. **观察与功课**：观察 → 查墓地 → 读范围清单 → 读文献（铁律 5 的
   三件读的功课）。
2. **候选登记**：写 `research/candidates/C###.toml`（模板见蓝图附录 C：
   机制类别、文献三件套——原始论文 / 发表后或样本外复现 / 已检索的
   反驳（写检索词与日期，`search_log` 必须非空）、`expected_effect`
   必须写依据且不取自样本内）→ 提交。
3. **预筛五硬条件**（`bt candidate check`，工具判定，全过才可入轮）：
   文献（原始 + 复现 + 反驳检索）；持仓周期 ≥ H1；事前成本（每笔预期
   毛利 ≥ 3 倍来回成本，成本模型 v3）；检验力（按折算后预期夏普，取
   声明与隐含夏普较小者，单次检验力 ≥ 50%——**不足 50% 的不开卡，进
   "待数据"队列（铁律 18）**，靠外部数据延长研究段后再筛）；机制空缺
   （补组合里还没有的机制类别）。结果落进 `C###.precheck.json` 与台账。
4. **轮次预注册**（管理员 + 用户批准，不是你的步骤）：管理员写
   `research/rounds/R#.toml`（本轮候选 ≤3、每候选评估方式与主统计量、
   试验预算、锁箱用量 ≤ D16 剩余额度、推进规则、结束条件、`approved_by`
   留用户批准），提交且工作区干净后 `bt round open`；预算用尽或轮次
   关闭后同轮不再有新试验。
5. **开卡与运行**：在已开启轮次内写卡 `research/cards/H###.toml`
   （附录 A v3：`candidate`/`round`/`evaluation_mode`/`primary_statistic`
   等谱系字段；卡的 `strategy` 字段绑定策略类，`bt` 拒绝用其他策略跑
   这张卡；参数 ≤4、网格 ≤100、`expected` 先写且不取自样本内）→
   `bt card check` 校验 → `git commit` → `bt screen` 在卡的网格上跑
   A 层（逐组合入台账，含失败组合）→ `bt gate --card H### --gates G0,G1`
   （前视/窗口不变性 + 样本内资格线）。低频事件类候选按轮次注册的
   `evaluation_mode="event"` 走事件模式（T66，标定待批准）。
6. **关卡 v3 与轮次报告**：关卡 v3 提案（`config/gates-v3.toml`，主检验
   + 稳健性否决 + 诊断，联合误报/检出率已标定）**现不生效，待用户
   批准**；批准后替换 `gates.toml` 并重新冻结，`suite="v3"` 路径成为
   关卡缺省，审查角色按新否决集复核（`docs/p6/review-agent.md` 流程）。
   轮次结束由管理员 `bt round close` 生成 `docs/research/round-R#-<日期>.md`
   骨架（本轮全部运行 + 按 `advance_rule` 的判定），你负责补全报告
   （铁律 6：一个不落）。审查否决 → 读否决理由，或弃卡进墓地——
   **没有"否决后直接开 v2"的默认路径**：同谱系新版本必须重新候选
   登记、过预筛、入轮（蓝图 v4 §6.2，研究循环从第 2 步重来）。

## 报告模板要点（`docs/research/H###-<日期>.md` 与轮次报告）

- 卡 id/version、谱系（`family`/`parent`/`candidate`/`round`，v2 卡注明
  上一版）、策略类（与卡 `strategy` 字段一致）、参数网格、数据区间
  （`data_first`/`data_last`）、引擎版本与成本版本（从 manifest/台账抄，
  不许手填）；
- **全部运行结果表**：每个组合的 run_id、净利、交易数、关卡结果、
  失败原因（有异常贴异常摘要）；被拒绝的操作（先卡后测/轮次/锁箱）
  单独列出；
- 与 `[expected]` 逐项对照（预期出处：文献折算或事前计算）；结论一句
  话：继续 / 改卡重跑（回候选登记）/ 弃卡进墓地。
