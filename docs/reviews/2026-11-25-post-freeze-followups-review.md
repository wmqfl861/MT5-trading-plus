# F1 独立评审：定版之后的小跟进——只改测试、conftest 和 README 的四个提交（2026-11-25）

## 0. 评审对象、评审者、依据、方式、时间

- **评审对象**：分支 `post-freeze-followups`，尖端 `90a17147bdc78e7aac83fbffc5d2138c9d64a5d3`（远端尖端相同，工作树干净）。基点是 main 当时的 `93d3ced`。四个提交，按顺序：

  | # | 提交 | 内容 |
  | --- | --- | --- |
  | 1 | `a2eac0223ba7920e6840a6070cb8131b75cc6caf` | `tests/test_t217_static_gate.py` 加三条自检（规则集、四条规则各自会报、豁免文件的基线）；ruff 子进程加 300 秒超时；README 第 16 行补 ruff |
  | 2 | `4e7d58da76e27930716f01a163700078646b89e6` | `tests/conftest.py` 写明"子进程把结构化结果交回 Python 时写 UTF-8 文件，不经标准输出"，新增 `powershell_report(folder, script)`；安装脚本的 `survey()` 改用它；两条新测试 |
  | 3 | `1e70d204ec2a8da31b3bedcafda7b3c4a0b848c1` | 清点脚本多清点 `using` 语句，一律拒绝；参数化的变异用例（先 6 种） |
  | 4 | `90a17147bdc78e7aac83fbffc5d2138c9d64a5d3` | 实例成员从拒绝名单改成允许清单 `ALLOWED_INSTANCE_MEMBERS`（32 项）；变异用例加到 9 种 |

  自基点以来一共动了 4 个文件（+283 −30）：`README.md`、`tests/conftest.py`、`tests/test_install_terminal_keeper.py`、`tests/test_t217_static_gate.py`。没有碰实盘代码，没有碰 `pyproject.toml`，没有碰被豁免的 5 个研究侧文件（`git diff --quiet 93d3ced 90a1714` 逐个核过）。
- **评审者**：独立的评审子代理，没有参与这四个提交的实现。定版前评审记录（`docs/reviews/2026-11-24-prefreeze-review.md`）里对 `tests/test_install_terminal_keeper.py` 的三次复核是同一位评审者做的，这次沿用了那里的变异形状。
- **依据**：主会话的评审任务（六项重点、约束）；定版前评审记录 §9–§11（静态检查、闸门、报告走文件的那几条）；`docs/reviews/2026-11-24-t217-review.md` 的建议 J-1 至 J-4（本提交对应的条目，只看了提交说明里的引述，没有重读那份记录）。
- **评审方式**：只读，以找问题为导向。在评审自建临时目录里的四个分离工作树（`90a1714` 和它的三个祖先 `1e70d20`、`4e7d58d`、`93d3ced`）上跑点名的文件；变异的安装脚本副本只让静态检查解析，从不运行；守门测试的自检在副本树上做。探针和副本都在工作树之外，不入库，评审后按确切路径删除。
- **硬约束的遵守**：没有连接、启动或结束终端，没有 import MetaTrader5（每一次 pytest 和探针结束都核对了 `sys.modules`，全部是 False）；没有读写 `data\live`、`data\ops-evidence\` 和任何生产目录；**没有做任何计划任务操作**——对任务计划程序只做过只读查询（列任务、读操作日志），运行安装脚本那份测试文件的前后各核对了一次，变更事件按任务路径过滤；没有往共享的 venv 里装东西——ruff 用的是主会话隔离的那一份 0.17.6，只把它的 wheel 目录放进 `PYTHONPATH` 给 `python -m ruff` 用，用完之后核对了共享的 venv 里仍然找不到 ruff；没有跑全量，所有运行串行；没有加负载。证明见 §7。
- **时间**：2026-11-24T19:45:22Z 起（系统时钟的 UTC；本机时间是 11-25 凌晨，文件名按本机日期）。
- **证据标注**：**已验证**＝本人亲自运行并看到输出；**读码结论**＝只读代码得出；**主会话转告**＝本人看不到的内容（CI 的结果、实现方自己的那几轮）。

## 1. 结论

**PASS**（对 `90a1714`）。没有阻断项，没有应改；建议 4 条，都是残余和文档上的小处，不挡合入。

- 四个提交只改了声明的文件，实盘代码、`pyproject.toml` 和 5 个被豁免的研究侧文件一个字节没有动。
- 静态检查的判据只紧不松：`using` 的四种写法（`using module` 按名字、按路径，`using assembly`，`using namespace`）都被拒绝；实例成员换成了 32 项的允许清单，清单上没有能把命令名或类型名藏进去的项；此前能通过检查的反射链、成员名放在变量里的写法现在都被拒绝。本人前三次复核里的变异形状在尖端上全部被抓住（副本只解析）。
- 闸门还在：静态检查不过时整份文件一条用例也不运行安装脚本（已验证）。
- `powershell_report()` 和 `survey()` 的结果从 UTF-8 文件读，不读标准输出；旧报告先删；后一次没有写出报告时报错，不返回前一次的（已验证，含反事实）。
- 守门测试的三条自检在规则集被改空、加了 ignore、不拷 `pyproject.toml`、豁免多一处或少一处时都失败（已验证，副本树上做）。

## 2. 运行的命令与关键输出

### 2.1 测试（干净的 `90a1714`，全部通过，MetaTrader5 始终没有被加载）

| 文件 | 结果 | 时刻（UTC） |
| --- | --- | --- |
| `tests/test_install_terminal_keeper.py` | 42 通过（原 31，加 2 条报告相关的、9 种变异） | 19:54:27–19:56:24 |
| `tests/test_optional_dep_skip.py` | 5 通过 | 19:56 |
| `tests/test_strict_deps_mode.py` | 4 通过 | 19:56 |
| `tests/test_t217_static_gate.py`（隔离的 ruff 0.17.6） | 4 通过（含对整棵树的 `ruff check .`） | 19:56 |

运行安装脚本那份文件前后的只读核对：

| 时刻（UTC） | `\MT5Quant\` 下 | 带 keeper 的任务 | 开工（19:45:22Z）之后所有任务的 106、140、141、142 |
| --- | --- | --- | --- |
| 19:54:24–19:54:27（运行前） | 8 个：`demo_weekly_check`、`live_daily_report`、`live_supervisor`、`live_supervisor2`（Ready，其余 7 个 Disabled）、`refresh_bars`、`refresh_ticks`、`snapshot_specs`、`watch_swap` | 0 | 0 条 |
| 19:56:24–19:56:27（运行后） | 同上 | 0 | 0 条 |

### 2.2 静态检查的形状（尖端和两个父提交；副本只解析）

做法：测试文件自己的 `survey()` 加 `static_problems()`，对安装脚本的副本逐个跑。父提交上用的是父提交自己的清点和判据。

| 往安装脚本的副本里加什么 | `4e7d58d`（第 3 个提交的父提交） | `1e70d20`（第 4 个提交的父提交） | `90a1714`（尖端） |
| --- | --- | --- | --- |
| `using module ScheduledTasks`（按名字） | 通过 | 抓住 | 抓住 |
| `using module .\modules\ScheduledTasks\ScheduledTasks.psm1`（按路径） | 通过 | 抓住 | 抓住 |
| `using assembly System.Web` | 通过 | 抓住 | 抓住 |
| `using namespace System.Runtime.InteropServices` | 通过 | 抓住 | 抓住 |
| `[Activator]::CreateInstance([type]::GetTypeFromProgID('Schedule.Service'))` | 抓住 | 抓住 | 抓住 |
| 别名 `ipmo` 按路径导入真模块 | 抓住 | 抓住 | 抓住 |
| 反射链 `''.GetType().Assembly.GetType('System.Type').GetMethod('GetTypeFromProgID').Invoke(…)` | 通过 | 通过 | 抓住（`.GetType`、`.Assembly`、`.GetMethod`、`.Invoke` 四处） |
| 成员名放在变量里（`$x.$member`） | 通过 | 通过 | 抓住（记成 `(dynamic) $x.$member`） |
| 实例方法（`$service.Connect()`） | 通过 | 通过 | 抓住（`.Connect`） |
| 本人加的几种：成员名加引号（`$state.'Invoke'`）；成员名加括号（`$x.('Get' + 'Type')`）；换个对象写 `$service.$key`；先把字符串转成 `[type]` 再 `.GetMethod(…)` | 通过 | 通过 | 都抓住 |
| 本人前几次的形状：`& $wscript …`；注册命令包进函数；注册命令在 `else` 分支；注册命令在顶层 | 都抓住 | 都抓住 | 都抓住 |
| `#requires -Modules ScheduledTasks`（注释里的指令） | 通过 | 通过 | 通过（见建议-3） |

尖端上原样的安装脚本：64 个命令、16 个类型、16 个静态成员、32 个实例成员、0 条 `using`，没有问题。32 个实例成员是：`.ACP`、`.OEMCP`、`.AddMinutes`、`.ToUniversalTime`、`.ToString`、`.TotalSeconds`、`.Contains`、`.Trim`、`.TrimEnd`、`.Length`、`.Count`、`.Hash`、`.ToLower`、`.GetBytes`、`.GetString`、`.Name`、`.Value`、`.ts`、`.age_s`、`(dynamic) $state.$key`、`.Repetition`、`.Interval`、`.StartBoundary`、`.MultipleInstances`、`.ExecutionTimeLimit`、`.LogonType`、`.RunLevel`、`.State`、`.LastRunTime`、`.LastTaskResult`、`.NextRunTime`、`.NumberOfMissedRuns`——与 `ALLOWED_INSTANCE_MEMBERS` 逐项相同（提交的测试也断言了集合相等）。

对照实现方的说法：9 种变异里 COM 和 `ipmo` 在 `4e7d58d` 上本来就被抓住，其余 7 种在 `4e7d58d` 上通过、在尖端上被抓住；在 `1e70d20` 上前 6 种通过、3 种实例成员的通过后被 `90a1714` 抓住。与本人的结果逐条一致（19:50:49–19:52:37Z）。

### 2.3 闸门（副本树，19:53:57–19:54:08Z）

| 副本里的改动 | 跑法 | 结果 |
| --- | --- | --- |
| 加一行无害的 `Get-Date` | 整份文件 | 42 个 error，每条都是闸门那一句；`calls.jsonl`、`invoke.ps1` 都是 0 个 |
| 注册命令包进函数（行为不变） | 整份文件 | 同上，42 个 error |
| 开头加 `using module <路径>`（危险的那一种） | 只选 11 条静态用例 | 11 个 error，都是闸门那一句；31 条动态用例没有被选中，驱动文件 0 个 |

autouse 夹具本身这四个提交没有动（diff 里没有它）。

### 2.4 `powershell_report()` 和 `survey()`

- 读码：`powershell_report(folder, script)` 把一次性脚本写到 `folder\report.ps1`，报告路径经环境变量 `MT5_CHILD_REPORT` 交给子进程（`folder\report.txt`）；**先 `unlink` 旧报告**再起子进程；退出码不是 0、或者报告文件不在，都断言失败，标准输出和标准错误只贴进失败信息；返回值是 `report.read_text(encoding="utf-8-sig")`。`survey()` 把清点脚本写到目录下，经 `powershell_report()` 运行，`-Out $env:MT5_CHILD_REPORT`，结果 `json.loads(report)`——不再经过 `powershell()` 的标准输出。清点脚本本身没有改。
- 提交的两条测试在尖端上通过：同一个目录先后清点两次、后一次不落盘时 `survey()` 报"the child wrote no report"；报告里的"§ 运行记录"原样回来，子进程写到标准输出的不参与，后一次没有报告时报错。
- **反事实**（19:57:17–19:57:34Z）：把 conftest 里"先删旧报告"那一行换成 `pass`，这两条都失败（`DID NOT RAISE AssertionError`）；原样是 2 通过。两种情况下驱动文件都是 0 个。
- conftest 里控制台代码页那一段：写的是 Windows PowerShell 5.1 按控制台的代码页编码被重定向的输出，CI 的机器是 437、`§` 在其中是字节 0x15，开发和生产的机器是 65001，定版前的 CI #420 就是这样失败的。与定版前评审记录 §11.3 的实测一致（437 下同一个列号报错，1252 和 65001 下通过）。

### 2.5 守门测试的自检（副本树，隔离的 ruff 0.17.6，19:52:54–19:53:11Z）

| 副本树里改了什么 | 结果 | 失败的是 |
| --- | --- | --- |
| 没改 | 4 通过 | — |
| 没有 ruff | 1 通过，3 跳过 | — |
| 严格模式、没有 ruff | 3 失败，1 通过 | 三条要跑 ruff 的 |
| `select` 改空 | 2 失败，2 通过 | 规则集那条、四条规则各自会报那条 |
| `ignore` 加 `F401` | 2 失败，2 通过 | 同上两条 |
| 不拷 `pyproject.toml` | 2 失败，2 通过 | 四条规则那条（报出了 `E722`、`S110`，说明跑的是 ruff 的缺省规则）；守门测试那一条也失败，是本人的改法让 `shutil` 成了没用的导入 |
| 豁免清单去掉 `events.py` | 2 失败，2 通过 | 基线那条；守门测试那条也失败（它的两处死导入现在被报出来） |
| 一个豁免文件多豁免一条规则（`F401` 加 `F841`） | 1 失败，3 通过 | 基线那条 |
| `events.py` 末尾多一个死导入 | 1 失败，3 通过 | 基线那条 |

被豁免的 5 个文件和 `pyproject.toml` 在这四个提交里一个字节没有动。超时（300 秒）加上之后四条测试照常通过，没有 ruff 时的跳过和严格模式下的失败也和提交说明写的一样。

## 3. 逐项回答

**1. 静态检查的判据**（已验证，§2.2）：
- `using`：清点脚本把每一条 `UsingStatementAst` 原样记进报告，`static_problems()` 见一条报一条，不分种类。按名字、按路径的 `using module`，`using assembly`，`using namespace` 四种写法在尖端上都被拒绝；在 `1e70d20` 之前四种都通得过。清单测试另有一条断言：安装脚本里没有 `using`。
- 实例成员允许清单（32 项）：清单测试断言安装脚本的实例成员恰好是这些，脚本不再用到的也要拿掉。逐项看过：`.Name`、`.Value`、`.State`、`.Count`、`.Length`、`.Hash`、`.Interval`、`.ts`、`.age_s`、`.ACP`、`.OEMCP` 和触发器、设置、主体、任务信息上的几个属性名，都是读一个值；`.ToString`、`.Trim`、`.TrimEnd`、`.Contains`、`.ToLower`、`.AddMinutes`、`.ToUniversalTime`、`.TotalSeconds`、`.GetBytes`、`.GetString` 是字符串、日期和编码上的方法。**没有能把命令名或类型名藏进去的项**：能造出东西或调用东西的（`.GetType`、`.Assembly`、`.GetMethod`、`.Invoke`、`.CreateInstance`、`.PSObject`、`.InvokeCommand` 这些）都不在清单上；本人另试的"成员名加引号""成员名加括号""换个对象写 `$service.$key`""先转成 `[type]` 再 `.GetMethod`"四种都被抓住。
- 成员名在变量里的写法（`$object.$name`）：清点脚本按成员那一节是不是写死的字符串常量来分——是的记成 `.名字`，按名字判；不是的记成整段表达式、前面标 `(dynamic)`，按整段文本判。放行的只有看护状态文件时的那一处 `$state.$key`。这是按文本、不按位置判的，见建议-2。
- 每种变异在父提交上通过、在本提交上被抓住：与实现方报的一致（§2.2）。

**2. 闸门还在**：是（已验证，§2.3）。

**3. `powershell_report()` 和 `survey()`**：见 §2.4，四点都成立，说明写得准。

**4. 守门测试的自检**：三条各自在说的那几种情形下失败；豁免文件一个字节没动；超时加上后行为不变（已验证，§2.5）。

**5. README 那一句**：与事实一致。`requirements-dev.txt` 里是 `pytest==9.1.1` 和 `ruff==0.17.6`；没有 ruff 时守门测试是跳过的（本机 1 通过 3 跳过）；CI 上真的跑——`tests.yml` 里 pytest 作业带 `MT5_STRICT_DEPS: "1"`（严格模式下没有 ruff 是失败），另有独立的 lint 作业跑 `python -m ruff check .`。规则那半句"语法错误、未定义名、没用到的导入和局部变量"对应 `E9`、`F821`、`F401`、`F841`。措辞上有一点："那条测试是跳过的"——现在是三条要跑 ruff 的测试都跳过，不跑 ruff 的规则集那条照常跑；不影响意思。

**6. 实现方自己报的几件事**：照实记在 §5。

## 4. 发现清单

### 阻断

无。

### 应改

无。

### 建议（4 条）

1. **规则集自检没有钉住"哪些文件归它管"。** 位置：`tests/test_t217_static_gate.py` 的 `test_the_rule_set_is_exactly_the_four_error_rules`。它核的是选中的规则恰好四条，没有核 `[tool.ruff]` 的 `extend-exclude`（现在是 `docs/**`）和 `exclude`，也没有核仓库里是否只有一份 ruff 配置：把某个目录加进排除清单，或者在子目录里再放一份 `pyproject.toml`／`ruff.toml`，那里的文件就不归这道门管，`ruff check .` 照样通过，这条测试也不动声色。加两条断言就行：排除清单恰好是现在这一项（没有 `exclude`）；带 `[tool.ruff]` 的配置文件全仓库只有根目录那一份。
2. **实例成员允许清单按名字判、不按对象判。** 位置：同上文件的 `ALLOWED_INSTANCE_MEMBERS` 和清点脚本里实例成员那一段。`(dynamic) $state.$key` 是按整段文本放行的：换一个对象、只要变量也叫 `$state` 就放行；`.Name`、`.Value` 这类名字可以读任何对象的同名属性。现在没有路能拿到危险的对象（命令名、类型、静态成员都在清单上，`GetType`、`Assembly`、`GetMethod`、`Invoke` 都被拒），所以不必改，记为残余。
3. **`#requires -Modules <名字>` 是注释，清点看不到。** 按名字只找得到沙箱模块，无害；记在案。
4. **conftest 里"开发和生产的机器是 65001"只对现在这台机器成立。** 生产现在就跑在这台机器上，这句话属实；T215 把生产迁到别的机器之后，要记得这一句和随之而来的假设（本机通过不等于 CI 通过）要重新核。

## 5. 实现方自己报的几件事（主会话转告，照实记）

- 有一轮 `-k` 过滤词里带了 `sandbox`，无意选中了 4 条在沙箱里运行安装脚本的用例（参数 `-Install -Uninstall`，被互斥拒绝），前后核对无变化。本人能核实的是结果：19:00:00Z 之后到 19:57Z，操作日志里所有任务的 106、140、141、142 是 **0 条**，与主会话 19:43Z 查到的一致。这 4 条用例给安装脚本的参数是互斥的两个开关，安装脚本在第一步就拒绝，碰不到任务计划程序。
- 只读查询做了 13 次；三轮前后核对的具体时刻没有记下。本人自己的核对时刻见 §7。
- pytest 临时目录里有它留下的沙箱副本没有删（共用的根目录）。不是本人建的，本人没有碰；由实现方按确切路径清。

## 6. 未验证事项

- CI 本人看不到（主会话说已派发）。全量套件没有跑。
- 控制台代码页 437 下这批改动的表现本人这次没有重跑；报告走文件那一条在定版前评审记录 §11.3 已经量过，这次只在本机（65001）跑。
- 带 `using module <路径>` 的脚本在 Windows PowerShell 5.1 里实际会怎样，本人没有运行（只解析）；只读查询用了多少次、实现方那几轮的确切时刻，本人无从核对。
- 实现方留下的沙箱副本本人没有查看。
- `docs/reviews/2026-11-24-t217-review.md` 里 J-1 至 J-4 的原文本人没有重读，只按提交说明的引述理解。

## 7. 没有越界的证明

- **建过、删过的**：
  - 评审自建的临时目录一个（`<用户目录>\AppData\Local\Temp\` 下）：import 守卫两个文件、只读核对脚本一个、探针三个、变异的安装脚本副本（尖端 18 个、两个父提交各 18 个）、闸门实验的 3 棵副本树、守门测试自检的 9 棵副本树、报告反事实的 2 棵、pytest 的 basetemp。
  - 分离工作树四个（`90a1714`、`1e70d20`、`4e7d58d`、`93d3ced`），都在上面的临时目录里。
  - 开工时本人把一个守卫文件写进了一个拼错的目录（`<用户目录>\AppData\Local\Temp\` 下一个名字带 `guard-placeholder` 的目录），当时就发现，按确切路径删掉了文件和目录，重新写到正确的位置。
  - 都按确切路径删除；没有按前缀或时间批量删除任何东西。分支的工作树里只新建了本文件；没有 `git add`、commit、checkout、stash、push、reset。
- **ruff**：主会话隔离的那一份，只读。本人按文件名在用户目录下找它的位置时只列了名字，并排除了生产部署树的目录和数据目录；共享的 venv 里没有装 ruff，用完之后核对过仍然找不到。
- **没有残留进程**；没有 import MetaTrader5。
- **操作日志**（`Microsoft-Windows-TaskScheduler/Operational`，每个事件的时刻先换成 UTC 再比较）：开工（19:45:22Z）之后，两次核对（19:54:24Z、19:56:24Z）和收尾前的最后一遍（19:57:37Z）里，所有任务的 106、140、141、142 都是 0 条；往前看到 19:00:00Z 也是 0 条。`\MT5Quant\` 下始终是那 8 个任务，带 keeper 的任务 0 个。

## 8. 评审过程披露

1. 本人写的脚本出过三处错，都在当次改正：变异脚本第一版把字节和字符串拼在一起，运行前改掉；闸门和自检的驱动第一次因为行尾的写法和已经存在的目录报错，改了之后换一个目录重跑。
2. "不拷 `pyproject.toml`"那一种，本人的改法（把那一行换成 `pass`）让守门测试本身也失败了一次，原因是 `shutil` 成了没用的导入；那是本人改法的副作用，不是这份测试的问题。"豁免清单去掉一条"那一种守门测试也失败，是预期的。
3. 为了找到主会话隔离的 ruff，本人在用户目录的几个常见位置按文件名搜了一遍（见 §7），没有读任何文件的内容。
4. 任务里说的"`using` 的三种"本人按四种写法核（`using module` 按名字和按路径各算一种）。
5. `ALLOWED_INSTANCE_MEMBERS` 的项本人是按名字逐个判断的，没有对每一项去做"用它能不能造出或调用东西"的实验；结论里的"没有能把命令名或类型名藏进去的项"是读码结论，加上 §2.2 里那几种实际被拒绝的形状。

## 9. 工作树状态

- **卫生扫描**（19:59:33Z）：仓库的扫描器直接扫本文件 0 处；`tests/test_repo_hygiene.py` 11 通过、`tests/test_p6_docs.py` 75 通过（都在分支的工作树里，带了不写缓存的参数）。之后本人只往本文件里写了这一节，又扫了一遍，结果相同。
- **清理**（20:00:01–20:00:10Z）：临时目录和四个分离工作树按确切路径删除，`git worktree list` 里不再有评审的工作树；删之前只读清点过，没有引用它的进程。共享的 venv 里仍然找不到 ruff。
- **收尾时的只读核对**（20:00:10Z）：开工之后所有任务的 106、140、141、142 仍是 0 条；`\MT5Quant\` 下 8 个任务，带 keeper 的 0 个。
- **分支的工作树**（HEAD `90a1714`，与远端尖端相同）：`git status --short` 只有一行，就是本文件未跟踪；本人没有 `git add`、commit、checkout、stash、push、reset。
