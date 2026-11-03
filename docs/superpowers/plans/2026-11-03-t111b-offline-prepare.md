# T111b 离线发布前置校验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 T111b 已要求的固定SHA、独立部署目录、授权快照只新增currency_base等检查变成可离线验证的清单生成入口，供后续维护窗口审查。

**Architecture:** 新单用途脚本读取调用者给定的既有detached worktree及两份快照；复用既有发布要求，纯离线核对git/文件/字段，输出确定性且脱敏的JSON。不给工具创建部署目录、安装任务、连网关、管理进程或解除保护的能力。

**Tech Stack:** Python stdlib、pytest、临时合成Git库及JSON fixture。

**Spec:** `docs/handoff/2026-11-02-single-session-handoff.md` T111b L158–168/U3/D62；`docs/ops/demo-runbook.md` §5.6、§6.2；已交付 `scripts/live_supervisor.py` 固定部署验证契约。

## Global Constraints

- “不热改正在运行的 Demo，部署只走 runbook 的维护窗口。”
- “部署目录与开发目录分离”；固定发布SHA，新SHA新目录。
- “按 runbook §5.6 刷新授权快照（带 `currency_base`），核对 diff 只新增该字段。”
- “仓库是公开的。账号（含模拟盘账号）、服务器名、IP、令牌、本机绝对路径、`config.json`、`mcp.json` 一律不入库。”
- 主会话与开发代理不得自我批准。U3仍须用户决定；离线清单不代表真实DEMO保险丝、进程健康、部署或正式10日窗口已完成。
- 不访问真实snapshot/data/config、MT5或正式台账，不检查/停止/启动真实进程，不创建计划任务，不操作halt/pause，不创建实际部署worktree。
- 不改src/mt5live、src/mt5gw或运行脚本；当前任务新增离线脚本+测试+最小文档使用段。

## Review Focus

- HEAD指向相同树但SHA不同、attached分支、dirty/untracked、路径别名均不能伪装成所声明detached发布。
- Snapshot必须结构有效且同一对象集；只新增currency_base，不能丢行、改原字段、bool/int混同或掩盖重复键。
- 必需文件路径须对应实际发布树，哈希与版本不能仅基于调用者声称。
- 输出不能包含敏感路径、账户、主机或完整snapshot；失败有可操作原因且无成功暗示。
- 工具不以“离线检查通过”表示U3/保险丝/部署/正式窗通过，也不偷偷补做运行时步骤。

### Task 1: 实现固定SHA和快照差异的离线前置入口

**Files:**
- Create `scripts/t111b_prepare.py`。
- Create `tests/test_t111b_prepare.py`。
- Modify only necessary use instructions in `docs/ops/demo-runbook.md` §5.6/§6.2。

**Interfaces:**
- 显式参数：既有release directory、完整40位expected SHA、before/after授权snapshot；必要的部署参数只作为已存在路径检查/占位清单，不输出绝对路径或敏感值。
- 返回确定性JSON/stdout，区分static_checks_passed和runtime_checks_pending；失败非零退出；不生成时间戳随机项、不覆盖输入。
- 函数接口保持脚本级简明，复用stdlib/subprocess和原验证规则，不增加服务或注册框架。

- [ ] Read exact current runbook and verify_deployment schema/requirements; write bounded interface and test outline in report before implementation.
- [ ] Write failing tests using independent temporary Git trees and synthetic snapshots: correct detached/clean/fullSHA; wrong SHA/same-tree-otherSHA; attached/dirty/untracked; nonworktree/missing required script/config references; invalid JSON/duplicate keys and path alias cases where relevant. Preserve RED command/output.
- [ ] Implement read-only Git/file inspection and required deployment artifact hashes. Reject unsupported/malformed inputs clearly. Never create/update worktrees, execute checked scripts, fetch remote, enumerate processes or connect services.
- [ ] Write snapshot-diff RED tests: allow currency_base newly added at correct symbol record shape, require valid nonempty string per contract; existing fields and record set deeply identical including types; reject changed prior currency_base, deleted/added symbols, extra changes, duplicate names/JSON keys, nonfinite values, malformed/unknown schema. Both files must be independently specified and nonaliased where distinction is required. Use actual public schema in synthetic examples.
- [ ] Implement exact snapshot validation and minimal deterministic manifest: releaseSHA, detached/clean, relative required artifact names/hash, snapshot input hashes and allowed diff summary; never raw account/path/snapshot values. Include incomplete maintenance-window items as pending, no claim of deployment readiness beyond checked static facts.
- [ ] Add CLI tests for help, pass/fail exit status, stable output, privacy and no input mutation; update runbook with a concise local invocation using placeholders and exact interpretation. Do not duplicate whole runbook or alter runtime procedures.
- [ ] Run focused tests/CLI with explicit exit status, retain evidence; self-review and [OPS] commit exact files. Report verification, limitations and any schema uncertainty to controller. Full regression and independent review by controller.
