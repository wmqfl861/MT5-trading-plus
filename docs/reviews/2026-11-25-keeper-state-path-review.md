# 看护状态文件路径修正：独立评审（2026-11-25）

- **评审对象**：分支 `keeper-state-path` 的提交 `21392e7`（`21392e78774b00ad379b745814982a697ec88fc1`），父提交 main `93d3ced`（`93d3cedbbf177dc98fdd26735491ceddccbd2e5f`）；5 个文件，+86 / −7。评审时 HEAD 就是它，工作树干净。背景：候选 `2a25802` 已定版、未部署，这个提交要进候选并重新定版。
- **评审者**：新上下文的子代理，独立于实现者，没有参与实现。
- **依据**：主会话的说明（缺陷、裁定、修正范围）；`scripts/ops/install_terminal_keeper.ps1`、`scripts/ops/terminal_keeper.ps1`、`scripts/ops/liveness_logic.py`、`scripts/ops/liveness_check.py`、`docs/ops/demo-runbook.md` §6.5、§6.6。
- **评审方式**：读码；在父提交的普通副本上复现缺陷；在分支上串行跑点名的五个文件；反事实；对"同一个东西两处各写一遍"做一遍专门的扫描。全程只读，没有跑全量。
- **记录时间**：2026-11-24 20:09 UTC（评审 20:02 UTC 开始），取自系统时钟。文件名里的日期按本地日期。
- **证据级别**：**已验证** = 我亲自运行并看到；**读码结论** = 只读代码得出；**未验证** = 没有验证。
- **结论：PASS**（对 `21392e7`）。缺陷属实，修正只做了这一件事，契约测试是真的端到端，两条反事实都是行为失败。扫描里另发现一处同类的弱点（运行器心跳里"终端进程不在"这句话不是每轮都在，存活检查的 `terminal_absent:<运行器>` 的"lost"一种多半看不到），不在本提交的范围里，不阻塞它，见 §5。

## 1. 缺陷属实，后果如所述。已验证

- **读码**：安装脚本给看护任务的 `-LogPath` 是 `<数据目录>\ops-evidence\terminal_keeper\terminal_keeper.log`（`scripts/ops/install_terminal_keeper.ps1:135`、`:299`，任务动作在 `:301`）；它的 `-Status` 读的也是这个目录下的 `state.json`（`:143`）。看护脚本的 `Write-State` 把 `state.json` 写在 `-LogPath` 所在目录（`scripts/ops/terminal_keeper.ps1:93–116`）。父提交的存活检查缺省到 `ops-evidence/terminal-keeper/state.json` 去找（连字符，`liveness_logic.py:212`），说明文字和 runbook §6.5 也是连字符。
- **复现**（不改仓库）：父提交的普通副本里，在临时数据目录下按安装脚本的约定放一个看护写出的 `state.json`（`outcome=refused`、`reason=cannot_tell`），用父提交的 `observe()` 不带 `keeper_state` 去读：

| | 父提交 `93d3ced` | 分支 `21392e7` |
| --- | --- | --- |
| `observe()` 用缺省路径 | `present: False`；评估得 `keeper_absent` | `present: True`，`refused` / `cannot_tell`；评估得 `keeper_refused` |
| `observe()` 显式给路径 | `present: True`，`refused` / `cannot_tell` | 同左 |

- **后果**：`keeper_absent` 持续满 10 分钟（`NOT_REVIVED_S`）就是生产级（`liveness_logic.py:227–234`），所以"照安装脚本装好后一直报 `keeper_absent`，最终生产级"属实；`keeper_stale`、`keeper_refused` 读不到真东西。显式给 `--keeper-state` 可以绕开，但缺省路径是给部署用的那个。

## 2. 修正只做了这件事。已验证

- diff 的每一处：`liveness_logic.py` 缺省路径一行加 `observe` 的说明；`liveness_check.py` 模块说明和 `--expect-keeper` 的帮助文字；runbook §6.5 状态文件那一条；`tests/test_liveness_keeper.py` 写状态文件的辅助函数（说明一行、路径一行）。全是把 `terminal-keeper` 换成 `terminal_keeper`，没有别的改动。
- **断言没有放宽**：`tests/test_liveness_keeper.py` 的 diff 只有辅助函数里的那两行（+2 / −2），断言一行没碰。
- **显式给路径的行为不变**：`state_path = Path(keeper_state) if keeper_state is not None else …` 那一行没有改，改的只是 `else` 后面的目录名；上表的第二行在父提交和分支上结果相同。
- 全仓库在 `21392e7` 上已经没有 `terminal-keeper` 这个写法（`git grep`，零命中，手册和评审记录也在内）。

## 3. 契约测试是真的端到端。已验证（读辅助函数，加运行）

`tests/test_keeper_state_path.py` 两条。端到端那条的四段：

1. **安装脚本**：在沙箱里跑（`tests/test_install_terminal_keeper.py` 的 `sandbox` 和 `run`）。驱动脚本先核对替身在位，不在位以 99 退出、安装脚本根本不跑，跑完再核对一遍；`PSModulePath` 里只有沙箱模块那一个目录，接触任务计划程序的命令全是只记录的替身。这一段用计划模式（不带开关），测试断言任务计划程序的调用记录是空的。静态检查夹具 `the_installer_passes_the_static_checks` 在这个文件里是自动生效的（模块级、按名字导入），先于用例运行。**先过静态检查，属实。**
2. **看护脚本**：`tests/test_keeper_state.py` 的 `run_keeper` 在同一个 PowerShell 会话里执行仓库里的真实 `terminal_keeper.ps1`，只把 `Get-CimInstance`、`Start-Process` 换成记录函数；辅助函数自己只写驱动脚本和调用记录。不带 `-DryRun`，所以走的是脚本里 `Write-Keeper` 和 `Write-State` 的代码。**状态文件不是测试写的，属实。**
3. **存活检查**：`observe(str(data / "live"), …, expect_keeper=True)`，不带 `keeper_state`，用的是缺省路径。
4. **安装脚本的 `-Status`**：替身说任务在，只查询；读到的 `outcome` 是 `started`。

拼写检查那条列出九个文件；连字符的写法在测试里是拼出来的，所以它自己也过得了。

## 4. 测试与反事实。已验证

- **分支上**（20:04–20:05 UTC，串行，带 import 守卫）：`tests/test_keeper_state_path.py` 2 条、`tests/test_liveness_keeper.py` 11 条、`tests/test_keeper_state.py` 6 条、`tests/test_t209_terminal_keeper.py` 13 条、`tests/test_t214_liveness_check.py` 49 条，合计 **81 通过、0 失败、0 跳过**。真实 MetaTrader5 包的 import 尝试 0 次。
- **反事实**（父提交 `93d3ced` 的普通副本，只换入新测试文件）：**两条都失败，都是行为上的**。拼写检查报 `scripts/ops/liveness_logic.py`；端到端失败在 `{'present': False}`——看护那一段在父提交上是过的，状态文件确实写出来了，只是存活检查读不到。再换入改过路径的 `tests/test_liveness_keeper.py`：**3 失败、8 通过**，与实现方报的一致。
- **任务计划程序**：跑测试前后各做一次只读核对（任务路径加名字的清单散列、任务总数、名字里带 keeper 的个数、操作日志里注册/更新/删除/停用四类事件的条数，按任务路径过滤）。四次核对（跑测试之前、之后、反事实之后、收尾时）结果相同：任务总数 204，清单散列相同，名字里带 keeper 的 0 个，`MT5Quant` 路径下 9 个，30 分钟窗口里四类事件 0 条（操作日志是启用的）。
- CI：主会话说已派发，结果我没有看到。

## 5. "同一个东西两处各写一遍"的扫描

对得上的（读码结论，个别有运行）：

- 看护状态文件的六个字段（`ts`、`outcome`、`reason`、`running`、`others`、`pid`）和两个读取方（存活检查、安装脚本的 `-Status`）一致；`outcome` 的四个取值和存活检查认的两个（`refused`、`start_failed`）一致。
- 任务动作里的 `-TerminalPath`、`-LogPath` 和看护脚本的参数名一致；启动器的调用形式（`wscript.exe //B //Nologo hidden_task.vbs <脚本> [参数]`）和安装脚本拼出来的动作一致。
- 任务路径 `\MT5Quant\`、任务名 `terminal_keeper`：安装脚本的缺省、手册 §6.3 的停用步骤、别的安装脚本用的是同一种。
- 存活检查读的文件名和写入方一致：`heartbeat-<角色或 magic>.json`、`pause-<magic>.json`、`halt.json`、`halt-risk-veto.json`、`killSwitch`、`instances.json`、`supervisor-state.json` 的 `last_checked`、`<报表目录>/live/daily-<日期>.md`。
- 日报任务的包装器传给日报脚本的参数，脚本全都接受。
- `ops-evidence` 下的四个目录（`terminal_keeper`、`liveness`、`supervisor`、`drill-block`）各有固定的写入方，没有第二个名字。

**新发现（不在本提交的范围，建议并入定版前的跟进）。已验证。**

- 存活检查判"运行器连上之后终端没了"，靠的是运行器心跳的 `last_error` 里同时出现"MT5 终端进程不在"和 `NotConnectedError`（`liveness_logic.py:107–116`）。这句话不是每轮都在：网关在重连退避窗口内抛的是另一句固定文案（"终端与交易服务器断开，重连退避中……"），只有到点重连的那一轮才抛带"终端进程不在"的那句。
- 实测：用真实的 `MT5Connection` 和替身模块，终端进程消失后按 2 秒一轮调 91 轮（三分钟），心跳文字能被存活检查认出的只有 6 轮（第 0、6、16、46、106、166 秒），稳态后每 60 秒一轮。
- 后果：存活检查每小时采样一次，`terminal_absent:<运行器>` 的"lost"这一种多半看不到。启动等待那一种（`waiting_for_terminal`）是稳定的标记，不受影响。
- 它和 D2 的关系：D2 把"运行器的心跳里一直写着终端进程不在"列为发现看护失效的信号之一，这个说法对"连上之后丢了终端"不成立。
- 改法二选一：网关在退避窗口内的那句里带上一次失败的原因（`src/mt5gw/connection.py` 的 `_ready_or_raise`）；或者运行器在退避期间沿用上一轮的原因。哪一种都要配一条把运行器（或网关）和存活检查串起来的契约测试，做法同这次的 `tests/test_keeper_state_path.py`。

## 6. 手册

- runbook §6.5 状态文件那一条改成了 `<数据目录>/ops-evidence/terminal_keeper/state.json`，与安装脚本 `:143` 和存活检查的缺省一致。已验证。
- 手册别处没有连字符的写法（全仓库零命中）。§6.5 里状态文件的字段说明与看护脚本写出的六个字段一致。

## 7. 未验证事项

- CI 的结果。
- `tests/test_install_terminal_keeper.py` 这个文件我没有重跑（不在点名的五个里）；契约测试借用了它的沙箱和静态检查，那两层保证我是读它的说明和代码确认的。
- 运行器的真实轮询间隔我按 2 秒算的（按日志里"每 2 秒一条"的记载）；§5 那条的"每 60 秒一轮"与轮询间隔无关，"占多少比例"有关。
- 存活检查的小时任务还没有安装（手册 §6.6），它的命令行里带不带 `--expect-keeper` 现在无从核对。

## 8. 过程披露

- **对仓库**：只新建本文件。没有 add、commit、checkout、stash、push、reset。父提交是用 `git archive` 导出成普通副本放在我的临时目录里的，没有建工作树。
- **对机器**：没有碰交易终端，没有 import 真实的 MetaTrader5 包，没有读写任何 `data/live` 或 `data/ops-evidence`（分支的工作树里没有 `data` 目录），没有做任何计划任务操作，没有往共享的 venv 里装东西。任务计划程序只做过三次只读的清单核对。所有测试的临时文件都在我自己的临时根目录里。
- **收尾时的核对**：临时根目录按确切路径删除；删除前核对里面没有指向外面的重解析点；分支工作树的 `git status --short` 只有本文件一行；再核对一次任务清单与之前相同。
- **我自己的两处疏漏**，都在出结论前改正：第一次跑反事实时忘了把新测试文件拷进父提交的副本，pytest 报"文件不存在"，补拷之后才是真的反事实；复现脚本取发现项时用错了键名。§5 那一条是先从代码推断、再用实验坐实的，推断时的比例和实测一致。
- 主会话的提交说明里写任务总数是 285，我在同一台机器上数到的是 204。两边各自前后一致就行，这个差别我没有去查。
