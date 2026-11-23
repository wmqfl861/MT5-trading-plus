# ops-patches/2026-11-19 · recovery-20261119A 生产补丁留档（D102）

> **撤除日期：2026-11-23（T211 合入 main）**。自 T211 起 supervisor 直接用部署树的 `live_supervisor.py`，
> 不再安装本目录的包装链；树内实现和失效模式表见 `docs/ops/demo-runbook.md` §6.3。
> 生产上仍在跑的 `host.py` 在新候选部署时撤除（D110），撤除前不对观察运行热改。

2026-11-19 僵局恢复（codex 执行单 `docs/handoff/2026-11-19-recovery-decision.md`）期间
实际部署的**树外运维包装器**留档。这些文件不在任何 git 树内，生产原件放在
`<workspace>/data/ops-evidence/recovery-20261119A/`（证据目录，不入库），本目录是
**路径占位符化后的审计副本**——供独立审核（计划 §2.3-3）与 11-22 干净重部署对照。

## 2026-11-23 更新（O23-1 之后，以本节为准）

- 11-23 主会话排查 supervisor 拒绝时改了生产上的 `host.py` 和 `sup-wrapper.ps1`（10:57–11:16 UTC）。本目录同步为改后的版本，sha256 见下表。
- 11-23 版 `host.py` 除启动标志外还做了两处 monkey-patch：`canonical()` 改用 pathlib 归一化；包装 `verified_tree()`，
  调用原版之前先把"映像路径等于 `%SystemRoot%` 下的 `System32\conhost.exe`、且没有子进程"的叶节点从进程表里滤掉。
  部署树的 `live_supervisor.py` 文件一行未改，但判定行为被这两处改变了。
- 这版 `host.py` **没有独立评审记录，也没有测试**。codex 只否决了树内的第一版 `4609be1`（`docs/reviews/2026-11-23-o23-1-codex-review.md`）。
  第二十三轮审核据此定 D110：`host.py` 随新候选撤除，树内修复由 T211 重做（失效模式表见施工计划 §2.1）。
- 11-21 的取证（`docs/ops/phase-f-log.md` 2026-11-23 条 §6）：supervisor 在生产上被卡住的主因不是 conhost，
  而是 worker 拉起的交易终端进了 worker 的进程树。本补丁对那一种形态无效。

## 背景（为什么需要树外补丁）

第二十一轮部署后 supervisor 首次演练 A 暴露 O22-4：`live_supervisor.py` 重启子进程时用
`CREATE_NO_WINDOW`（0x08000000），每个被拉起的子进程都会带一个 `conhost.exe` 子进程，
而 supervisor 的 verified-tree 身份校验会把未知后代整树拒绝（`SUPERVISOR_REFUSED`）。
部署树代码处于冻结 SHA，不能热改；codex 裁定 B＝树外 `host.py` 包装器作临时运维级补丁。
正式修复（T197 类）合入前的所有重启/补缺都依赖本目录这套文件。

## 文件清单（用途 / 对应执行单步骤 / 生产文件 sha256）

| 文件 | 用途 | 执行单步骤 | 生产文件 sha256（脱敏前原件） |
| --- | --- | --- | --- |
| `host.py` | 树外 supervisor 宿主（11-23 版）：① `Popen` 的 `creationflags` CREATE_NO_WINDOW→0；② `canonical()` 改用 pathlib 归一化；③ 包装 `verified_tree`，滤掉系统 conhost 叶节点。原版 `live_supervisor.py` 文件一行未改，②③ 改变了判定行为（见上方 11-23 更新） | 演练 A 裁定 B（表格"supervisor"行） | `9260842ecd348729933d55d29bac4bcf291676660f431ec87637ade324e55425` |
| `start.ps1` | `demo_run.ps1` 的树外副本（codex §1c 的 4 处替换：`$RepoRoot` 钉定部署树、`config_check.py`/`save_run_evidence.ps1` 改经 `$RepoRoot`、`-WindowStyle Hidden`→`-NoNewWindow`）——四角色以计划任务宿主为父启动，conhost=0 | §1c 修正运维启动方式 | `8270581236f441fe77707286c7e9b1c493ac49efd862d5347e8da9961e14d8e1` |
| `start-wrapper.ps1` | `start.ps1` 的单参包装：四角色启动参数（SHA/目录/gateway 等）全部固化于此，任务与人工只引本文件 | §1d（四角色重启，表格行"四角色重启"） | `5df36372ee6366373a7fe34068a3a18641de99bba65dd1740eb23cec1a104004` |
| `sup-wrapper.ps1` | supervisor 参数固化包装：以 venv python 运行 `$PSScriptRoot\host.py` 并传 §6.2 全套固定参数（与 `live_supervisor.py --directory/--sha/...` 部署参数逐项一致） | §1e 安装每分钟 supervisor | `b7445890548bf36a26f848e8e2c2ff4235256e46489182393c8e1f259e2fcb99` |
| `sup-launcher.vbs` | 静默启动器：`wscript`（无控制台）以 `Shell.Run ... ,0,True` 拉起 `sup-wrapper.ps1`，避免 `powershell -WindowStyle Hidden` 的闪窗；退出码透传 | §1e（任务静默化，live_supervisor2 注册） | `e44272ea599be7435bf091f9f2fe572b875b292205bc1b9a6c5788716e3f8139` |
| `live_supervisor2-task.xml` | `\MT5Quant\live_supervisor2` 计划任务定义（每分钟触发；动作＝`wscript //B //Nologo sup-launcher.vbs`） | §1e | 原始导出（脱敏前）sha256 `000888a2433712ac74121230724e91786eb998f83ac959e86ca0fe821ca45813` |

调用链：任务（每分钟）→ `sup-launcher.vbs` → `sup-wrapper.ps1` → `host.py`（补丁）→
部署树 `scripts/live_supervisor.py`（原版逻辑）→ 按需经 `start-wrapper.ps1`/`start.ps1`
补缺四角色。

## 占位符对照（副本与生产文件的唯一差异）

| 占位符 | 生产值 |
| --- | --- |
| `<workspace>` | 主仓工作区绝对路径（生产包装器中以字面路径写死，本副本替换为占位符） |
| `<deploy>` | 部署树（detached 固定 SHA `cd6fc4aaac7d3886be0fbdfa4f437345b6b1b8c5`）绝对路径 |

除以下三项外副本与生产文件逐字节一致：

1. 上表两类绝对路径替换为占位符；
2. 任务 XML 另将 `<UserId>` 的机器 SID 替换为 `<user-sid>`（机器标识，同属脱敏项）；
3. 任务 XML 由 UTF-16 转存 UTF-8 并改 XML 声明（仓库内文件须可被 CI hygiene 扫描；
   UTF-16 会因解码失败被扫描器跳过）。

## 生产安装方式（当前实际）

runbook §6.3 开头的"当前实际安装方式（临时——树外 host.py 包装器）"一节即为这套文件
的安装/停机入口；§6.3 原注册命令（直接 `Register-ScheduledTask` 指向部署树
`live_supervisor.py`）**会复现 O22-4（conhost 子进程），A5 前不要用**。

## 审核（计划 §2.3-3，待独立子代理）

- [ ] host.py 只改启动标志与控制台继承，supervisor 判定逻辑一行未动（**11-19 版成立；11-23 版不成立**，见上方更新）；
- [ ] 异常路径不吞错（`raise SystemExit(s.main())` 透传原版退出码）；
- [ ] sup-wrapper.ps1 传给 `live_supervisor.py` 的参数与部署参数（§6.2/@sv）逐项一致。
