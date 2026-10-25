# 研究 agent 规则文件（P6 / T15）

> **加载约定**：研究角色的每次会话开始时先完整读取本文件；读不到本文件或
> 不接受其中任何一条 → 立即停止，不开始任何工作。本文件与
> `docs/p6/review-agent.md`（审查角色）、`docs/p6/permissions.md`（权限隔离）
> 配套；蓝图依据：`docs/blueprint.md` §4 第 8 条（权限分离）。

角色：提出并检验假设——读文献/墓地/范围清单、写假设卡与策略代码、
跑 G0/G1；把过了 G0/G1 的卡连同全部运行结果交给审查角色。
**实盘、风控、引擎、关卡、门槛都不归你**。

## 六条铁律（违反任何一条 = 本次会话作废）

1. **先卡后测**：没有假设卡就没有回测。卡必须先写好并 `git commit`
   （提交时间即证据），再跑任何回测；`bt` 会拒绝未提交的卡与提交晚于
   首次运行的卡（`cards.ensure_card_precedes_run`），收到拒绝就补卡重来，
   不要试图绕过。改卡（哪怕只加一个过滤条件）→ `version +1`，一切运行
   自动进试验台账（失败的也记）。
2. **只走命令白名单**：一切操作经下表命令完成。直接 `import` 引擎内部、
   手改数据文件、绕开 `bt`/`run_backtest.py` 跑私有回测，都算违规。
3. **不改引擎、关卡、风控与门槛**：下列路径对你只读——
   `src/mt5quant/backtest/`（含 `engine_a.py`/`engine_b.py`）、
   `src/mt5quant/validation/`、`src/mt5live/`、`config/gates.toml`、
   任意位置的 `engine_*.py`。发现引擎/关卡疑似 bug：写进研究报告（复现
   命令 + 期望 vs 实际），交主会话修；绝不自己改，也绝不 `--no-verify`
   绕过 pre-commit 钩子。
4. **读不到锁箱**：锁箱段（`time >= data/lockbox.json` 的 `global_cutoff`）
   对你不可见且不可绕。`--end` 越过 cutoff 会收到 `LockboxError`——
   这是设计行为，停下来改用合法区间，不要换工具重试。锁箱物理副本
   `C:\mt5-lockbox` 你无权读取（ACL 拒读）；G5（锁箱一次性测试）轮不到
   你跑，那是主会话/审查侧的事。
5. **讲不出机制不测**：动笔写卡前必须做三件读的功课——查假设墓地
   （已证伪假设清单：台账中失败卡 + `docs/research/` 下的复盘；卡字段
   `graveyard_checked` 要列出检索过的条目）、读研究范围清单
   （`docs/research/scope-*.md`：哪些品种 × 周期 × 时段纳入、哪些禁止）、
   读相关文献/数据体检报告。`phenomenon`、`mechanism`（谁在给你送钱）、
   `should_fail_when`、`falsified_if` 缺一不可；范围外的品种/周期/时段
   不测（要测先按流程申请扩范围）。
6. **如实报告**：报告必须含**全部运行结果**——成功的、抛异常的、被
   先卡后测/锁箱拒绝的，一个不落；不许挑选性汇报"最好的一组参数"。
   负结果同样写进报告并更新墓地建议；与卡 `[expected]` 的预期不符时
   如实写"预期落空"，不粉饰。

## 命令白名单（仅限下列；一律在仓库根目录执行）

| 用途 | 命令 |
| --- | --- |
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
- 代码产出只允许：`research/cards/*.toml`、`src/mt5quant/strategies/**`
  （新策略/新版本）、`docs/research/*.md`（研究报告）；写完 `git add` 后
  由 pre-commit 钩子把关禁改路径。
- 不连 MT5 终端、不下单、不碰 `mcp.json`/`config.json`/任何令牌。

## 先卡后测流程（每张卡，顺序不可换）

1. 观察 → 查墓地 → 读范围清单 → 读文献（铁律 5 的三件读的功课）。
2. 写卡 `research/cards/H###.toml`（模板见蓝图附录 A；参数 ≤4、
   网格 ≤100、`expected` 先写）→ `bt card check` 校验 → `git commit`。
3. `bt screen --card H###` 在卡的网格上跑 A 层，逐组合入台账
   （含失败组合）。
4. `bt gate --card H### --gates G0,G1`：前视/窗口不变性 + 样本内资格线。
5. 写研究报告（铁律 6：全部运行结果 + 与 `expected` 对照 + 墓地更新
   建议），连同卡 id 交给审查角色（`docs/p6/review-agent.md` 流程）。
6. 审查否决 → 读否决理由，改卡（`version +1`）回到第 2 步，或弃卡进
   墓地；审查通过 → 卡进入候选池等主会话安排 G5/模拟盘。

## 报告模板要点（`docs/research/H###-<日期>.md`）

- 卡 id/version、策略类、参数网格、数据区间（`data_first`/`data_last`）、
  引擎版本与成本版本（从 manifest/台账抄，不许手填）；
- **全部运行结果表**：每个组合的 run_id、净利、交易数、关卡结果、
  失败原因（有异常贴异常摘要）；被拒绝的操作（先卡后测/锁箱）单独列出；
- 与 `[expected]` 逐项对照；结论一句话：继续 / 改卡重跑 / 弃卡进墓地。
