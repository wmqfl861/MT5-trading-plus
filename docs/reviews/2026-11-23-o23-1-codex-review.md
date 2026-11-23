# O23-1 第一版（`4609be1`）的 codex 独立审核记录（2026-11-23）

> 本文件是留档：把当时只存在本机、未入库的 codex 审核结论按原文摘录入库（仅把本机路径换成占位符）。
> 它**不是**第二十三轮审核的一部分，也不构成对任何提交的批准。

- **审核对象**：`4609be1`（`scripts/live_supervisor.py`，`verified_tree` 按文件名跳过 `conhost.exe`）。
- **审核方**：本机 OpenAI Codex CLI（`codex exec`），模型 `gpt-6.2-astra`，推理强度 max；在 Windows 本机实跑。
- **委托方**：主会话（用户指令："立即修复，并且要根治，然后让codex审核，指定模型是gpt-6.2-astra/max"）。
- **结论**：**REJECTED**（两个阻断项、一个中等级残留项）。
- **之后的处置**：主会话按阻断项改出 `ded9407`（完整系统路径 + 叶节点），**没有再送 codex 复审**，
  也没有测试和入库评审；第二十三轮审核据此定 O23-1（D110：两个提交不作为窗口候选，并入 T211 重做）。
- **对本记录有用的实测事实**：codex 用 `WindowsProcesses.launch()` 在本机实起临时进程，
  确认 `CREATE_NO_WINDOW` 启动的角色会带一个系统 `conhost.exe` 叶节点（P23-5 所指的测试空白，在生产启动路径上是真实存在的）。

## codex 原文（结论部分，逐条）

**REJECTED — `4609be1` 修复了现场的误拒，但放宽了 supervisor 对未知进程的校验。** 我在 Windows 上通过 `WindowsProcesses.launch()` 启动了临时 Python 进程。`CREATE_NO_WINDOW` 确实产生了一个系统 `conhost.exe` 叶节点，补丁也正确返回了 Python worker 和启动器，顺序为子进程在前。阻断批准的是两个条件：代码仅凭文件名授予豁免，并在检查该节点的后代之前返回。

1. **[高，阻断] `conhost.exe` 文件名不能唯一标识系统控制台宿主。** `executable` 来自 Windows CIM 的 `ExecutablePath`，但 `.name.lower()` 丢掉了目录。其他目录中的同名映像会获得相同豁免。应使用操作系统报告的完整映像路径，与本机受信任的 `%SystemRoot%\System32\conhost.exe` 核对。现场 argv 中的 `0x4` 可以解释这次观测，不能证明映像身份。仓库已有恢复方案同时核对系统路径和叶节点。 [豁免判断](<workspace>/scripts/live_supervisor.py:181) · [既有恢复方案](<workspace>/docs/handoff/2026-11-19-recovery-decision.md:162)

2. **[高，阻断] conhost 已进入 `seen`，其后代被整个跳过。** `seen.add()` 在 `return` 之前，子进程循环在其后。末尾的全表扫描能把业务 argv 匹配的后代报为未登记 worker，但参数不同或不可读的后代可能被漏过，整树仍可通过。建议把例外保留在 `visit()`，仅对路径已确认、按现有父 PID 和创建时间规则判定为叶节点的宿主返回。发现有效子节点就拒绝该树。 [遍历与重复 worker 检查](<workspace>/scripts/live_supervisor.py:175)

3. **路径归一化没有解决这次 argv 误拒。** 对比父提交，`_argv_matches()` 函数体没有改动。它原本就会对两个绝对路径调用 `canonical()`，而现场 conhost 的 argv 只有两项，在长度检查处就已返回 `False`。Windows 实测中，原版和新版都能匹配普通路径的大小写、正反斜杠差异，也都把同一现存文件的 `\\?\` 拼写与普通拼写判为不等。这个故障无需第二层 pathlib 兜底。 [canonical](<workspace>/scripts/live_supervisor.py:53) · [argv 比较](<workspace>/scripts/live_supervisor.py:127)

4. **原有三组测试在本机复跑为 `60 passed in 37.88s`。** `t110_supervisor`、`t110_health` 和 `repo_hygiene` 全部通过。提交只改了实现，现有 Windows 进程树测试也没有显式断言 conhost 豁免的边界。需要补入回归的情形是系统叶节点通过、非系统路径同名映像拒绝、宿主带有效后代拒绝，以及启动器先退出时的恢复行为。 [现有 Windows 进程树测试](<workspace>/tests/test_t110_supervisor.py:224)

5. **已确认的系统宿主应排除在 `result` 外。** `result` 同时决定哪些 PID 可以作为健康心跳来源，以及哪些进程可以被终止。把系统宿主排除是正确的。单纯在 `_identity()` 里给 conhost 提前返回，会让调用方把它当作校验成功并加入 `result`。应保留根进程的强制身份校验，由遍历层明确排除合格的系统叶节点。 [心跳 PID 检查](<workspace>/scripts/live_supervisor.py:387) · [终止列表消费方](<workspace>/scripts/live_supervisor.py:323)

6. **[中，残留] 启动器先退出、直接 conhost 短暂存活时，本轮仍拒绝拉起。** 我用隔离的进程清单验证了 `supervise()`：移除 risk 启动器、仅保留其系统 conhost 时，得到 `missing launcher with surviving/duplicate worker`，重启数为零。下一轮移除已退出的宿主后，只重启了 `risk`。这表明残留宿主会延迟恢复，持续时间取决于宿主何时退出。可在该状态下做有界重新采样，同时保留对真实孤儿 worker 和身份不明进程的拒绝。 [启动器缺失分支](<workspace>/scripts/live_supervisor.py:163)

---

（codex 输出末尾：`tokens used` 119,181。完整会话记录留在本机，未入库。）
