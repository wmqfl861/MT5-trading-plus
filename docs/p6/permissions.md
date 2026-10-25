# 权限隔离方案（P6 / T15，T27、T29、T38 修订）

蓝图 §4 第 8 条：**研究 agent 不能改引擎、关卡、风控与门槛配置，读不到
锁箱数据。** 本文档给出 Windows 侧的具体隔离手段；规则（谁做什么）见
`docs/p6/research-agent.md` 与 `docs/p6/review-agent.md`。

> 本文所有路径与账户名均为**占位符**：`%REPO_ROOT%` = 仓库工作副本根
> 目录；`MT5Research` / `MT5Review` = 两个本地标准账户（示例名）。
> 不含任何真实 IP / 账号 / 令牌。命令以管理员身份在 CMD/PowerShell 执行，
> 执行前先备份现 ACL（见文末"备份与回滚"）。

## 0. 账户模型

| 账户（示例名） | 用途 | 隔离要点 |
| --- | --- | --- |
| `MT5Research` | 研究 agent 会话 | 主库 `data\bars`、`data\ticks` 与 `C:\mt5-lockbox` **拒读**；研究库 `data_research\` 只读；lockbox.json / gates.toml / 禁改源码只读 |
| `MT5Review` | 审查 agent 会话 | 与研究同（两者都不许改引擎/关卡/风控/门槛，也不许读主库） |
| 管理员（人/主会话） | 引擎、关卡、门槛、G5、实盘、研究库导出 | 全权；唯一能写锁箱、`gates.toml` 与 `data_research` 的角色 |

git 提交身份与试验台账 `actor` 字段区分两角色（`bt screen --actor
research` / `bt gate --actor review-gate`），事后可审计。

## 1. 锁箱物理副本 `C:\mt5-lockbox`：研究账户拒读

`export_lockbox.py` 导出的锁箱段物理副本（G5 / 实盘侧专用）。研究角色
**完全拒读**（deny 优先于任何 allow）：

```bat
:: 1) 断继承，只留管理员与 SYSTEM
icacls "C:\mt5-lockbox" /inheritance:r /grant:r "Administrators:(OI)(CI)F" /grant:r "SYSTEM:(OI)(CI)F"
:: 2) 拒绝研究/审查两个账户（含所有子目录与文件）
icacls "C:\mt5-lockbox" /deny "MT5Research:(OI)(CI)F"
icacls "C:\mt5-lockbox" /deny "MT5Review:(OI)(CI)F"
```

## 2. 主库拒读、研究库只读（T27 / H3：物理隔离）

主库 `data\bars`、`data\ticks` 含**完整锁箱段**——loader 约束挡不住有
shell 的 agent（一行 `read_parquet` 就能看完锁箱）。研究/审查账户对两处
**拒读**；研究侧改读导出的研究库 `data_research\`（管理员跑
`scripts/export_research_store.py` 产出：只含 `time < cutoff` 的 K 线 +
成本模型缓存文件，**不含 tick、不含 lockbox.json**——tick 库对研究账户
拒读，研究侧成本经 `data_research\costs\spread_model.json` 缓存提供；
研究库没有锁箱段，锁箱语义自然满足）。

**研究侧 CLI 缺省（T38/H4/D24，2026-10 落地）**：`scripts/run_backtest.py`
与 `scripts/bt.py` 的 `--data-root` 缺省即 `data_research`（`--research-store`
保留为兼容别名）；缺省时研究库不存在 → 报错指路 `export_research_store`，
**绝不静默回退主库**。读主库须显式 `--main-store`（`--data-root` 指到主库
也一样要求）；读主库的锁箱期（`--end` 越过 cutoff）再加 `--ops-ack`
（运维测量专用，run_backtest 记 manifest、bt 台账记 `actor=ops`）；G5 需
锁箱段，只能由管理员经 `--main-store` + `--approve` 跑。数据根三分类
（`run_backtest.classify_store`）：`data_research` → 研究库、`data` →
主库、其余 → 临时/测试根。

```bat
:: 主库 K 线与 tick：研究/审查账户完全拒读（deny 优先于任何 allow）
icacls "%REPO_ROOT%\data\bars" /inheritance:r /grant:r "Administrators:(OI)(CI)F" /grant:r "SYSTEM:(OI)(CI)F"
icacls "%REPO_ROOT%\data\bars" /deny "MT5Research:(OI)(CI)F"
icacls "%REPO_ROOT%\data\bars" /deny "MT5Review:(OI)(CI)F"
icacls "%REPO_ROOT%\data\ticks" /inheritance:r /grant:r "Administrators:(OI)(CI)F" /grant:r "SYSTEM:(OI)(CI)F"
icacls "%REPO_ROOT%\data\ticks" /deny "MT5Research:(OI)(CI)F"
icacls "%REPO_ROOT%\data\ticks" /deny "MT5Review:(OI)(CI)F"

:: 研究库 data_research：只读（可读可列，拒绝写入/追加/删除/改 ACL）
icacls "%REPO_ROOT%\data_research" /inheritance:r /grant:r "Administrators:(OI)(CI)F" /grant:r "SYSTEM:(OI)(CI)F" /grant:r "MT5Research:(OI)(CI)R" /grant:r "MT5Review:(OI)(CI)R"
icacls "%REPO_ROOT%\data_research" /deny "MT5Research:(WD,AD,DE,DC,WDAC)" /deny "MT5Review:(WD,AD,DE,DC,WDAC)"
```

注意：拒读 `data\bars` 会让 `data\lockbox.json` 之外的锁箱钳制对研究
账户失效吗——不会：loader 钳制（`research_bars`）仍在，但研究账户此时
读不到主库序列（`load_bars` 报错/返回空），这正是隔离目的；cutoff 必须
对研究可见的那一份是 `data\lockbox.json`（见下节，单独放行只读）。

## 3. `data\lockbox.json`：研究账户只读（不能拒读！）

**为什么不能拒读**：加载器 `mt5data.lockbox._load_raw` 先
`os.path.exists` 再 `open`——拒读会让 exists 变 False，加载器把
"读不到"当成"没有锁箱"，研究侧**全量放行**（静默禁用锁箱，比不设
ACL 更糟）。所以正确姿势是**允许读、拒绝写/删**（cutoff 必须对研究
可见，钳制才能生效）：

```bat
icacls "%REPO_ROOT%\data\lockbox.json" /inheritance:r /grant:r "Administrators:F" /grant:r "SYSTEM:F" /grant:r "MT5Research:R" /grant:r "MT5Review:R"
:: 再显式拒绝写/追加/删（防原子替换 os.replace 绕过只读）
icacls "%REPO_ROOT%\data\lockbox.json" /deny "MT5Research:(WD,AD,DE)" /deny "MT5Review:(WD,AD,DE)"
```

加固（可选）：若父目录 `%REPO_ROOT%\data\` 仍授予研究账户删除子项
权限，原子替换可绕过文件级只读——对 `data\` 目录追加
`icacls "%REPO_ROOT%\data" /deny "MT5Research:(DC)"`（同理
`MT5Review`）。研究要写的台账在 `data\research\` 子目录，另行
`/grant "MT5Research:(OI)(CI)M"` 单独授权即可（`--research-store`
时 bt 的台账缺省就是 `data\research\ledger.sqlite`——研究库本身只读，
台账不能落在里面）。

## 4. `config\gates.toml`：研究账户只读（+ 冻结指纹双保险）

```bat
icacls "%REPO_ROOT%\config\gates.toml" /inheritance:r /grant:r "Administrators:F" /grant:r "SYSTEM:F" /grant:r "MT5Research:R" /grant:r "MT5Review:R"
icacls "%REPO_ROOT%\config\gates.toml" /deny "MT5Research:(WD,AD,DE)" /deny "MT5Review:(WD,AD,DE)"
```

代码侧已有第二道锁：`mt5quant.validation.gates.FROZEN_GATES_SHA256`
在每次 `bt gate` 加载时核对 sha256，不符 → 拒绝运行（改了门槛反而一个
测试都跑不了）。改门槛 = 管理员一次提交内同时改文件与冻结值（T13 校准
后重新冻结走同一流程）。锁箱使用预算（T27/M11/D16）**不在** gates.toml
里：`max_uses` 缺省 5 硬编码在 `mt5quant.research.ledger`，首登记后以
台账库 `lockbox_budget` 表为准——调预算是管理员改库，不动代码与门槛。

## 5. 源代码禁改（引擎/关卡/风控）：git 钩子为主，ACL 为辅

主执行点是入库的 pre-commit 钩子 **`scripts/hooks/pre-commit-readonly-paths`**
（Python，无扩展名；T29 起为**白名单 + 缺省拒绝**模式）。可导入函数：
`check_paths(changes, role=None)`（**T38/D25：CI 共用的同一份规则**）、
`check_staged(changes, role=None)`（按 `(状态, 路径)` 列表返回逐条违规
信息）、`violation_reason(role, status, path)`（单条判定）、`parse_staged`
（解析 `git diff --name-status` 输出，含 CI 的 `origin/main...HEAD` 形态）、
`resolve_role`（命令行 `--role` 优先于环境变量 `MT5QUANT_COMMIT_ROLE`）
与薄壳 `main(argv, env)`；守护测试 `tests/test_p6_docs.py`。

**CI 侧硬执行（T38/D25/H4）**：`.github/workflows/research-allowlist.yml`
对 `research/**` 头分支发起的 PR 用 `git diff --name-status
origin/main...HEAD` 取改动，`python -c` 装载钩子文件并调用
`check_paths(changes, role="research")`——规则与本地钩子**同一份代码**，
本地钩子的角色靠环境变量自报（H4：设 `admin` 即绕过），CI 侧绕不过。
非 `research/**` 头分支的 PR 跳过检查并留 notice；强制力来自把该检查设为
main 分支保护的 required status check（设置步骤见下"分支保护"清单）。

白名单（研究/审查角色**只**放行以下路径，其余一律拒绝）：

- `A research/cards/*.toml`——**仅新增**卡文件（git 状态 A）；已提交的卡
  不得修改/删除（M/D/R 拒）；
- `src/mt5quant/strategies/library/**`——策略库；
- `tests/test_strategies_*.py`——策略测试；
- `docs/research/**`——研究文档。

明确在拒绝之列（M12 漏项补齐）：成本模型 `src/mt5quant/costs/`、
`src/mt5quant/research/`（台账与锁箱预算）、`src/mt5data/`（锁箱加载器）、
`scripts/bt.py` / `scripts/run_backtest.py`、钩子自身、CI 配置、
`config/*.toml`、`docs/p6/*`（角色规则由管理员维护，研究角色不能改自己
的规则）、README 与蓝图。

**入库但不自动安装**（钩子装在工作副本 `.git/hooks/`，不入版本库）。
需要时由主会话安装：

```bash
printf '#!/bin/sh\nexec ".venv/Scripts/python.exe" scripts/hooks/pre-commit-readonly-paths "$@"\n' > .git/hooks/pre-commit
```

可选 ACL 加固（对两个 agent 账户把禁改目录设为只读；白名单里策略库
`src/mt5quant/strategies/library/` 仍可写）：

```bat
icacls "%REPO_ROOT%\src\mt5quant\backtest" /deny "MT5Research:(WD,AD,DE,DC)" /deny "MT5Review:(WD,AD,DE,DC)"
icacls "%REPO_ROOT%\src\mt5quant\validation" /deny "MT5Research:(WD,AD,DE,DC)" /deny "MT5Review:(WD,AD,DE,DC)"
icacls "%REPO_ROOT%\src\mt5live" /deny "MT5Research:(WD,AD,DE,DC)" /deny "MT5Review:(WD,AD,DE,DC)"
```

注意：钩子与 ACL 拦的是"误改"，不防蓄意绕过（`--no-verify`、改用别的
账户）——蓄意绕过属流程违规，靠台账 actor 与 git 历史事后审计。

## 6. MCP 侧：锁箱钳制已保证，无需额外配置

远程研究 agent 经隧道访问的 `mt5mcp`（`scripts/run_mcp_server.py`，
只监听回环，见 `docs/ops/mcp-remote-access.md`）在**服务端**读
`data/lockbox.json` 并钳制：请求越过 cutoff → 报错（文案含"锁箱"）；
不传时间上界 → 钳到 cutoff 并在返回里带 `lockbox_cutoff` 字段。
即远程 agent 与本地 `research_bars` 同一约束，锁箱数据出不了服务进程，
无需在 MCP 侧再做账户隔离。

## 7. 验证清单（设完 ACL 后，人工执行一次）

```bat
:: a) 锁箱物理副本：研究账户应"拒绝访问"
runas /user:MT5Research "cmd /c dir C:\mt5-lockbox"
:: b) lockbox.json：应能读（内容可见）但拒绝写入
runas /user:MT5Research "cmd /c type %REPO_ROOT%\data\lockbox.json"
:: c) 主库 bars / ticks：研究账户应"拒绝访问"（T27/H3）
runas /user:MT5Research "cmd /c dir %REPO_ROOT%\data\bars"
runas /user:MT5Research "cmd /c dir %REPO_ROOT%\data\ticks"
:: d) 研究库：能列能读、拒绝写入
runas /user:MT5Research "cmd /c dir %REPO_ROOT%\data_research"
runas /user:MT5Research "cmd /c type %REPO_ROOT%\data_research\lockbox_check.tmp"
:: e) 查看有效 ACL
icacls "C:\mt5-lockbox" & icacls "%REPO_ROOT%\data\lockbox.json" & icacls "%REPO_ROOT%\data_research"
```

功能级验证（研究账户会话内）：`bt screen --card ...`（缺省即读
`data_research`，无需 flag）应能跑；缺省下 `data_research` 不存在 →
报错指路 `export_research_store`（不回退主库）；`--data-root data` 而无
`--main-store` → 拒绝；`--main-store --end <越过 cutoff>` 而无 `--ops-ack`
→ 锁箱报错；`bt gate --gates G5`（研究库模式）→ 拒绝并指路 `--main-store`；
`bt gate` 改门槛后应拒绝运行（冻结指纹生效）。研究库新鲜度由管理员侧
`scripts/export_research_store.py --verify-only` 复核。

## 8. 研究账户的运行方式（U0：用户操作，agent 不代办）

ACL 只对"以哪个账户身份运行的进程"生效——研究 agent 必须真的运行在
`MT5Research` 账户下，隔离才成立：

1. **建账户**（管理员，一次性）：`net user MT5Research /add`（设强口令；
   `MT5Review` 同理）；不需要远程桌面登录权限。
2. **以研究账户运行会话**：计划任务（`/ru MT5Research /rp <口令>`）或
   `runas /user:MT5Research "cmd /c ..."` 拉起研究 agent 的进程；工作副本
   经 ACL 只读/只写分区给两账户（见 §2、§5）。
3. **git 身份**：研究会话内设 `git config user.name`（如 `research-agent`）
   与提交邮箱；环境变量 `MT5QUANT_COMMIT_ROLE=research`（钩子白名单按它
   裁决；CI 侧还有 research-allowlist 兜底）。
4. **分支纪律**：研究工作一律在 `research/**` 分支上做，经 PR 合并——
   本地钩子可被 `--no-verify` 绕过（流程违规，靠台账 actor 与 git 历史
   事后审计），CI 的 research-allowlist 检查绕不过（配合下述分支保护）。
5. **台账**：写 `data\research\`（`--research-store`/缺省研究库模式下
   bt 的台账缺省就是 `data\research\ledger.sqlite`，见 §3 加固段）。

## 9. main 分支保护（U0：用户操作，agent 不代办）

研究分支经 PR 合并、CI 路径白名单必须通过——把这一条变成硬约束，需要在
GitHub 仓库设置里开启分支保护（管理员在网页上操作）：

- [ ] Settings → Branches → Add branch protection rule，`Branch name
      pattern` 填 `main`；
- [ ] 勾选 **Require a pull request before merging**（研究/审查分支只能
      经 PR 进 main；管理员直推 main 也会被拦——刻意，改引擎/门槛应留
      PR 记录）；
- [ ] 勾选 **Require status checks to pass before merging**，选中
      `tests` 与 `research-allowlist` 两个检查（后者对 `research/**` 头
      分支强制路径白名单，D25）；
- [ ] 勾选 **Require branches to be up to date before merging**（避免
      白名单检查基于过期的 main）；
- [ ] 私有仓库的 GitHub Free 计划不含 required checks——若该项不可勾，
      升级计划或改为人工核对 `research-allowlist` 为绿后再合并，并在
      本清单旁注明实际做法；
- [ ] 设完后用一条违规 PR（如 `research/test-hook` 分支改
      `scripts/bt.py`）验证：`research-allowlist` 检查应为红。

## 10. 备份与回滚

改 ACL 前备份、出问题按表恢复：

```bat
icacls "C:\mt5-lockbox" /save acl-lockbox.bak /t
icacls "%REPO_ROOT%\data" /save acl-data.bak /t
icacls "%REPO_ROOT%\data_research" /save acl-research-store.bak /t
icacls "%REPO_ROOT%\data\lockbox.json" /save acl-lockbox-json.bak
:: 回滚（在备份文件所在目录）：
icacls . /restore acl-lockbox.bak
icacls . /restore acl-data.bak
icacls . /restore acl-research-store.bak
```
