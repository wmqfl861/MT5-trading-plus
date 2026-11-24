# 网关退避原因修正：独立评审（2026-11-25）

- **评审对象**：分支 `gateway-backoff-reason` 的提交 `2518646`（`2518646cd4a44490c3e03b2f69c6b734edc0e8ee`），父提交 main `8a9fa1f`（`8a9fa1fd28f934b6cf522b1add3b4307765f6469`）。评审时 HEAD 就是它，工作树干净。它修的是我在任务 K 的同类扫描里实测的一处弱点：退避窗口内的断线不带原因，存活检查认不出"连上之后终端没了"。
- **评审者**：新上下文的子代理，独立于实现者，没有参与实现。
- **评审方式**：读码；父提交的普通副本上跑反事实；分支的普通副本上串行跑点名的和相关的测试文件；一个进程内的实验核语义。没有跑全量。
- **记录时间**：2026-11-24 20:49 UTC（评审 20:42 UTC 开始），取自系统时钟。文件名里的日期按本地日期。
- **证据级别**：**已验证** = 我亲自运行并看到；**读码结论** = 只读代码得出；**未验证** = 没有验证。
- **结论：PASS**（对 `2518646`）。只做了这一件事；父提交上 4 条都是行为失败；相关的现有测试没有被改动，也没有变红；四个语义问题都没有带出副作用。两条措辞上的建议和一件范围披露，见 §5、§6。

## 1. D118。已验证

- **只做了这件事**：`src/mt5gw/connection.py` 的 +14 / −2 全在这一个改动里——`__init__` 多一个 `_last_reconnect_reason`（`:96`）；`_attempt_reconnect` 失败时留下原因（`:310`），两条成功路径清掉（`:282`、`:298`）；`_ready_or_raise` 在退避窗口内那一句的结尾改成 `self._last_reconnect_reason or self.last_error()`（`:336`），探活即成功的那一支也清掉（`:345`）。`reconnect()` 没有动。期限、锁、`_fatal_timeout` 都没有碰。
- **反事实**（父提交 `8a9fa1f` 的普通副本，只换入 `tests/test_gateway_backoff_reason.py`）：**4 条都失败，都是行为失败**。前三条失败在旧的窗口内文案上——"…重连退避中（约 N 秒后再试）：(-10004, 'No IPC connection')"里没有原因，第三条是"(-6, 'terminal is not connected')"里没有"交易服务器未连接"；第四条 `assert None == 'lost'`。与实现方报的一致。分支的副本上 4 条通过。
- **相关的现有测试**：提交没有改任何已有的测试文件。我按"用到网关连接、断线路径、运行器、执行器、风控、对账、日报、存活检查、正式检查"挑了 26 个文件，在分支的副本上串行跑：**655 通过、0 失败、0 跳过**（`test_t201_reconnect_fix` 5、`test_t141_disconnect` 15、`test_connection` 9、`test_gateway` 6、`test_trading` 46、`test_account` 21、`test_symbols_market` 17、`test_t110_deadline` 23、`test_t209_terminal_guard` 37、`test_t183_presend_unavailable` 8、`test_t140_rejection` 9、`test_mt5live_executor` 73、`test_mt5live_runner` 37、`test_mt5live_riskproc` 61、`test_mt5live_reconcile` 31、`test_mt5live_integration` 53、`test_daily_report_demo_safety` 8、`test_live_daily_report` 7、`test_t213_daily_report` 36、`test_liveness_keeper` 11、`test_t214_liveness_check` 49、`test_t110_health` 21、`test_t184_account_window` 14、`test_t142_loop_grading` 13、`test_demo_check` 43、`test_formal_alert_classification_guard` 2）。没有一条断言押在旧的窗口内文案上。

## 2. 语义。已验证（进程内实验：真实的 `MT5Connection`、`tests/test_t201_reconnect_fix.py` 的替身模块、注入的时钟和探针）

**(a) 定类不变。** 窗口内和到点两种文字，分别按运行器、执行器、风控的写法组成四条告警，交给 `scripts/demo_check.py` 自己的函数：

| 告警 | 到点 | 窗口内 |
| --- | --- | --- |
| `BROKER_UNAVAILABLE`（执行器的断线和持仓查询失败两种） | 记录类 | 记录类 |
| `DISCONNECTED` 段首 | 记录类，开降级段 | 同左 |
| `DISCONNECTED` rc 78 退出 | 交易日是异常，非交易日有 supervisor 重启才是记录类（T210 的既有口径） | 同左 |

两种文字都含 `NotConnectedError` 和 `TerminalNotConnectedError`，T209 的口径在窗口内也成立。存活检查对两种文字都判 lost。

**(b) 原因会过期，但上界很小，不影响判级。** 终端回来之后、下一次到点重连之前，窗口内的文字确实还说"MT5 终端进程不在"——实验里终端的 `terminal_info` 已经报已连接，随后的两轮仍这么写，到点重连成功后才恢复、原因清掉。上界是当前这个退避窗口的剩余部分，最多 60 秒（`RECONNECT_BACKOFF_S` 的最大值）。这段时间网关也确实是断开的（IPC 要等 `initialize` 重建），说错的只是原因。对存活检查没有影响：`terminal_absent` 的 lost 那一种只靠检查器自己的时钟、持续满 10 分钟才升生产级，而检查器每小时采样一次，恢复之后的下一次采样看到的已经是正常。实现方的测试②只测了重连成功之后清掉，这个窗口没有测试；记在案，不建议为它加代码。

**(c) 锁。** `_ready_or_raise` 只在三处被调用，都在 `_serialized` 的临界区里（`call`、`call_status`、`trade_send`），所以这个读取实际是在锁内。退一步说，即便在锁外，字符串赋值在 CPython 里是原子的，读到的最坏是一个旧的原因。没有问题。代码注释里"读在锁外，同 `_next_reconnect_at`"说得不够准（见建议 S1）。

**(d) `reconnect()` 的行为没有变。** 每 10 秒调一次 `reconnect()`、终端先消失再回来，父提交和分支上的返回值序列（17 次 False，然后 True）和 `initialize` 的调用次数完全相同。风控、对账的轮询循环受影响的只是窗口内异常的文字。

## 3. 契约测试④。读码结论，加运行

- 两种写法是照代码原样：测试先断言 `error = f"NotConnectedError: {exc}"` 和 `error = f"broker_unavailable: NotConnectedError: {exc}"` 这两个字符串原样出现在 `scripts/live_runner.py` 里（`:981`、`:1024`），再把 91 轮里每一轮的异常按这两种写法格式化，`_terminal_wait` 每一轮都判 lost。已验证（4 条通过）。
- 真实的运行器写进心跳的不只是这两处。运行器在 `:1042` 会用执行器回报的 `error` 覆盖它。我查了执行器里所有 `blocked_broker_unavailable` 的回报：断线路径（`src/mt5live/executor.py:1172`）、持仓查询失败（`:713`）、未决挂单查不到（`:920` 附近）三种，`error` 里都带异常的原文和类名。所以不管是哪一段落的，心跳的 `last_error` 都带着原因，判别条件每一轮都成立。
- 判别条件本身（同时含"MT5 终端进程不在"和 `NotConnectedError`）在三种别的原因下不成立，实现方的测试③覆盖了，我复核通过。

## 4. 同类扫描的收尾。已验证

- 全仓库（`src`、`scripts`）里匹配"重连退避中""秒后再试"的只有 `src/mt5gw/connection.py` 自己；`tests/` 里只有新测试和 `tests/test_t201_reconnect_fix.py`（它押的是到点那一轮的"等待终端自行重连"，没有改，通过）。
- 手册里没有描述这句窗口内文案的地方；`scripts/ops/liveness_check.py` 模块说明里举的例子是到点那一句，仍然准确。
- 没有因为这次改动新对不上的地方。

## 5. 建议

- **S1** `src/mt5gw/connection.py:92–95` 的注释"读在锁外，同 `_next_reconnect_at`"：读取实际在三处调用的临界区里。改成"写在 `_attempt_reconnect` 的锁内；读在 `call`、`call_status`、`trade_send` 的同一个锁内"更准。
- **S2** 原因是"终端在线、交易服务器未连接"时，窗口内的文字读起来自相矛盾："终端与交易服务器断开，重连退避中……：终端在线、交易服务器未连接——等待终端自行重连……"。信息是对的，只是措辞；以后动这一段时可以顺手理顺。

## 6. 范围披露

- 任务说明里写的是两个文件（`connection.py` 和新测试），提交实际是**三个**：还给旧的评审记录 `docs/reviews/2026-11-13-t201-review.md` 追加了第 8 节（+74 / −0，纯追加）。那一节标明是实现子代理的自测记录、不是评审结论，内容与我在这里核到的一致，包括误跑全量那件事。把跟进记在 T201 的记录后面是合理的；只是说明里漏了提。
- main 自父提交 `8a9fa1f` 之后前进到 `02a891e`。只读地比较过：main 在这期间改过的相关文件只有 `scripts/ops/liveness_logic.py`（看护状态文件的路径，任务 K），和这个提交不改同一处；`connection.py`、运行器、执行器 main 没有动。我没有做合并预演。

## 7. 未验证事项

- CI 的结果。
- 真实的运行器循环我没有重跑。§3 里"心跳的 `last_error` 带着原因"是读码结论（行号在那一节），契约测试用的是从运行器源码核对过的两种字面写法。
- 没有跑的文件：supervisor 的几个、`test_t78_model_loading.py`（重）、`test_demo_run_ps1.py`。它们不用被改的那一段代码。
- 真实的 MetaTrader5 包在退避窗口内的 `last_error()` 返回什么，不影响这个改动（有原因时不再调它）。

## 8. 过程披露

- **对仓库**：只新建本文件。没有 add、commit、checkout、stash、push、reset，没有做合并预演。两个副本（分支 `2518646`、父提交 `8a9fa1f`）都是 `git archive` 导出的普通目录，放在我的临时根目录里，没有建工作树。
- **对机器**：没有碰交易终端，没有 import 真实的 MetaTrader5 包（所有运行里 import 尝试 0 次），没有读写任何 `data/live` 或 `data/ops-evidence`（分支的工作树里有一个被忽略的 `data` 目录，里面没有文件，这两个子目录都不存在；除了仓库自己的卫生测试那一个文件——它需要 git——我的测试都是在副本里跑的，跑之前和之后那个目录里文件数都是 0），没有做任何计划任务操作，没有往共享的 venv 里装东西。
- **跑测试的方式**：显式文件列表，逐个核对存在；列表不足 20 个就不跑；逐个文件串行。总共 29 次 pytest（新文件在分支副本上 1 次、父提交副本上 1 次、现有文件 26 次，都在副本里；卫生测试 1 次，在工作树里），没有一次不带文件参数。
- **我自己的两处疏漏**，都在出结论前改正：实验脚本对混合了 None 的集合排序出错；读反事实输出的小脚本漏掉了第四条的首个断言，另查确认。
- **收尾时的核对**：临时根目录按确切路径删除，删除前核对里面没有指向外面的重解析点；分支工作树的 `git status --short` 只有本文件一行。
