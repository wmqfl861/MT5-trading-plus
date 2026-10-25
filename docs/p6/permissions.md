# 权限隔离方案（P6 / T15）

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
| `MT5Research` | 研究 agent 会话 | 锁箱物理副本**拒读**；lockbox.json / gates.toml / 禁改源码**只读** |
| `MT5Review` | 审查 agent 会话 | 与研究同（两者都不许改引擎/关卡/风控/门槛） |
| 管理员（人/主会话） | 引擎、关卡、门槛、G5、实盘 | 全权；唯一能写锁箱与 `gates.toml` 的角色 |

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

## 2. `data\lockbox.json`：研究账户只读（不能拒读！）

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
`/grant "MT5Research:(OI)(CI)M"` 单独授权即可。

## 3. `config\gates.toml`：研究账户只读（+ 冻结指纹双保险）

```bat
icacls "%REPO_ROOT%\config\gates.toml" /inheritance:r /grant:r "Administrators:F" /grant:r "SYSTEM:F" /grant:r "MT5Research:R" /grant:r "MT5Review:R"
icacls "%REPO_ROOT%\config\gates.toml" /deny "MT5Research:(WD,AD,DE)" /deny "MT5Review:(WD,AD,DE)"
```

代码侧已有第二道锁：`mt5quant.validation.gates.FROZEN_GATES_SHA256`
在每次 `bt gate` 加载时核对 sha256，不符 → 拒绝运行（改了门槛反而一个
测试都跑不了）。改门槛 = 管理员一次提交内同时改文件与冻结值（T13 校准
后重新冻结走同一流程）。

## 4. 源代码禁改（引擎/关卡/风控）：git 钩子为主，ACL 为辅

主执行点是入库的 pre-commit 钩子 **`scripts/hooks/pre-commit-readonly-paths`**
（Python，无扩展名；可导入函数 `check_staged`/`forbidden_reason` + 薄壳
`main()`，守护测试 `tests/test_p6_docs.py`）。它拒绝暂存区触碰：

- `src/mt5quant/backtest/`（含 `engine_a.py`、`engine_b.py`、report、truncation）
- `src/mt5quant/validation/`（关卡与冻结指纹）
- `src/mt5live/`（实盘与风控）
- `config/gates.toml`
- 任意位置的 `engine_*.py`（未来新引擎自动受保护）

**入库但不自动安装**（钩子装在工作副本 `.git/hooks/`，不入版本库）。
需要时由主会话安装：

```bash
printf '#!/bin/sh\nexec ".venv/Scripts/python.exe" scripts/hooks/pre-commit-readonly-paths "$@"\n' > .git/hooks/pre-commit
```

可选 ACL 加固（对两个 agent 账户把禁改目录设为只读；策略库
`src/mt5quant/strategies/` 仍可写）：

```bat
icacls "%REPO_ROOT%\src\mt5quant\backtest" /deny "MT5Research:(WD,AD,DE,DC)" /deny "MT5Review:(WD,AD,DE,DC)"
icacls "%REPO_ROOT%\src\mt5quant\validation" /deny "MT5Research:(WD,AD,DE,DC)" /deny "MT5Review:(WD,AD,DE,DC)"
icacls "%REPO_ROOT%\src\mt5live" /deny "MT5Research:(WD,AD,DE,DC)" /deny "MT5Review:(WD,AD,DE,DC)"
```

注意：钩子与 ACL 拦的是"误改"，不防蓄意绕过（`--no-verify`、改用别的
账户）——蓄意绕过属流程违规，靠台账 actor 与 git 历史事后审计。

## 5. MCP 侧：锁箱钳制已保证，无需额外配置

远程研究 agent 经隧道访问的 `mt5mcp`（`scripts/run_mcp_server.py`，
只监听回环，见 `docs/ops/mcp-remote-access.md`）在**服务端**读
`data/lockbox.json` 并钳制：请求越过 cutoff → 报错（文案含"锁箱"）；
不传时间上界 → 钳到 cutoff 并在返回里带 `lockbox_cutoff` 字段。
即远程 agent 与本地 `research_bars` 同一约束，锁箱数据出不了服务进程，
无需在 MCP 侧再做账户隔离。

## 6. 验证清单（设完 ACL 后，人工执行一次）

```bat
:: a) 锁箱物理副本：研究账户应"拒绝访问"
runas /user:MT5Research "cmd /c dir C:\mt5-lockbox"
:: b) lockbox.json：应能读（内容可见）但拒绝写入
runas /user:MT5Research "cmd /c type %REPO_ROOT%\data\lockbox.json"
:: c) 查看有效 ACL
icacls "C:\mt5-lockbox" & icacls "%REPO_ROOT%\data\lockbox.json"
```

功能级验证（研究账户会话内）：
`bt screen --card H### ... --end <越过 cutoff 的时刻>` → 应收到
`LockboxError`（锁箱语义生效）；`bt gate` 改门槛后应拒绝运行（冻结
指纹生效）。

## 7. 备份与回滚

改 ACL 前备份、出问题按表恢复：

```bat
icacls "C:\mt5-lockbox" /save acl-lockbox.bak /t
icacls "%REPO_ROOT%\data\lockbox.json" /save acl-lockbox-json.bak
:: 回滚（在备份文件所在目录）：
icacls . /restore acl-lockbox.bak
```
