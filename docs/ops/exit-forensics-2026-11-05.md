# 取证补记：11-05 16:15（风控）与 11-06 11:22（对账）进程退出（T142，2026-11-07 只读取证）

> 范围：第十六轮审核 O16-3 指出的两次退出原因不明——11-05 16:15 风控、
> 11-06 11:22 对账。只读取证，未做任何运行时操作（D64）。
> 证据源：部署数据目录（`<主工作区>/data/demo/*.log[.err]`、
> `<主工作区>/data/live/exit-*.json`、`<主工作区>/data/logs/task_refresh_ticks.log`）。
> 结论先行：**两次退出都是 T110 网关期限硬停（rc=75，`terminal_info`
> 单次调用超 30 秒），不是未捕获异常，也不是 11-01 的 PermissionError 签名；
> 终端为何无响应，现有证据无法确定。**

## 1. 证据清单

| 文件 | 内容要点 |
| --- | --- |
| `data/demo/risk-account-20261104-212206.log.err` | 仅一行：`CRITICAL:root:MT5 deadline call=terminal_info duration=30.594s limit=30.000s rc=75` |
| `data/live/exit-risk.json` | `rc=75, reason=gateway_deadline, call=terminal_info, duration_s=30.594, ts=2026-11-05T16:15:49.603988Z, pid=10800` |
| `data/demo/reconcile-all-20261104-212206.log.err` | 仅一行：`CRITICAL:root:MT5 deadline call=terminal_info duration=49.765s limit=30.000s rc=75` |
| `data/live/exit-reconcile.json` | `rc=75, reason=gateway_deadline, call=terminal_info, duration_s=49.765, ts=2026-11-06T11:23:35.963526Z, pid=9796` |
| `data/live/gateway-stall.json` | 与 exit-reconcile.json 同一记录（pid=9796）——该文件是单槽位，11-05 风控那份已被 11-06 对账覆盖 |
| `data/live/exit-gateway.json` | `rc=75, call=copy_ticks_range, duration_s=37.969, ts=2026-11-05T16:15:49.845085Z, pid=8980` |
| `data/logs/task_refresh_ticks.log:63` | `CRITICAL:root:MT5 deadline call=copy_ticks_range duration=37.969s limit=30.000s rc=75`（与上一行同一事件） |
| `data/demo/risk-account-20261104-212206.log`（stdout） | 最后正常轮 `2026-11-05 16:15:08 UTC [live] 心跳=runner_stopped halt=是`（10 秒节奏） |
| `data/demo/reconcile-all-20261104-212206.log`（stdout） | 最后正常轮 `2026-11-06 11:22:16 UTC 对账一致（0 个持仓）`（30 秒节奏） |
| `data/live/alerts.log` | 两个窗口内**零条目**（期限硬停走 `os._exit`，进程内告警来不及写——见 §5） |

两进程均自 11-04 21:22 UTC 起（文件名 `-20261104-212206`）在部署树
`demo-deploy-8a80503` 上运行，数据目录指向主工作区 `data/`。

## 2. 11-05 16:15 风控退出：结论 = T110 期限硬停（rc=75）

- 时间线：11-05 14:22 runner 死于 `TradeRejectedError 10021 No prices`
  （O16-2，盘中终端无报价——终端侧已有异常迹象）；14:45 风控检出 runner
  fatal 写 halt；此后风控降为"心跳=runner_stopped、halt=是"的空转轮；
  16:15:18 左右开始的新一轮里 `terminal_info` 调用 30.594 秒未返回，
  超过 30 秒期限 → T110 看门狗写标记后 `os._exit(75)`
  （`exit-risk.json` ts=16:15:49.604）。
- `.log.err` 的 CRITICAL 行与 `exit-risk.json` 逐字段一致（时长 30.594s）。
- **同秒旁证**：16:15:49.845，计划任务 ticks 刷新（pid 8980）在同一终端上
  的 `copy_ticks_range` 也超限 37.969s 被 rc=75 硬停（`exit-gateway.json` +
  `task_refresh_ticks.log:63`）。两个独立进程在同一秒死于同型期限超限
  → **终端整体无响应**（API 层面），不是某个进程自身的缺陷。

## 3. 11-06 11:22 对账退出：结论 = 同签名（rc=75），"11:22" 是挂起起点

- 时间线：对账每 30 秒一轮；11:22:16 最后一次正常轮"对账一致（0 个持仓）"；
  ≈11:22:46 开始的下一轮里 `terminal_info` 挂起 49.765 秒 → 11:23:35.96
  被硬停（`exit-reconcile.json`、`gateway-stall.json`）。审核报告写的
  "11:22" 对应最后正常轮/挂起起点，标记里的精确退出时刻是 11:23:35.96。
- 也就是说：风控死后终端曾恢复（对账 11-05 深夜至 11-06 上午轮询正常），
  11-06 11:22 起再次无响应，对账成为该次事件里唯一在询问终端的常驻进程。
- 与 11-05 的风控退出**同一签名**（`terminal_info` 超限、rc=75）。

## 4. 上游根因：无法确定

终端（MT5 API 层）为何在 11-05 16:15 与 11-06 11:22 两次整体无响应，
现有证据**无法确定**。缺失的证据形态（正是本次取证无法闭合的部分）：

1. MT5 终端自身 `MQL5/Logs`（11-01 取证时即发现两日均不存在）；
2. Windows Application/System 事件日志在 11-05 16:00–16:30Z、
   11-06 11:15–11:30Z 两窗的采集（本次未做；11-01 窗口做过，无崩溃/
   注销证据）；
3. 终端 journal/传输日志或崩溃转储（终端未随进程退出留下任何产物）。

可确认的关联：11-05 14:22 的 "10021 No prices" 已是终端侧行情/连接劣化
的先兆，两次无响应与其相隔 2 小时/21 小时，无进一步因果证据。

## 5. 与 T142 的关系（O16-3 的"退出原因不明"究竟缺什么）

- **rc=75 这一类的取证其实是完备的**：`.log.err` 一行 CRITICAL +
  `exit-<component>.json`/`gateway-stall.json` 结构化标记，本次取证即靠
  它们闭合。缺的不是证据，是把证据读出来记录在案（本文补上），以及
  supervisor 未上线导致风控死后停摆 21 小时、对账死后停摆 2 小时
  （11-06 13:17 人工恢复）——那是 T143 的范围。
- **真正的证据缺口是主循环 Python 级异常**（11-01 风控 PermissionError
  那类）：此前风控未知异常落到 guarded_main 的 rc=2（只有 traceback，无
  结构化标记），对账 `run_once` rc=2（意图文件缺失等）在 --loop 下无限
  等待、不退出也不留痕。T142 落地后：已知可恢复（数据暂缺/文件被占用/
  锁等待超时）记录后继续，连续 5 轮或未知异常 → 写
  `loop-exit-<component>.json`（reason=unrecoverable_loop/<具体>）+ halt
  后以专用码 79 退出；断线仍走 T141 的 15 分钟时间窗（78），不进 5 轮
  计数。rc=75 行为不变（`os._exit` 防原生/GIL 挂死，本就是设计内）。

## 6. 退出码表（supervisor/取证统一汇总；锚点在 `src/mt5live/monitor.py` T142 注释块）

| rc | 含义 | 留痕 | halt |
| --- | --- | --- | --- |
| 0 | 正常完成 / Ctrl-C 中断 | `exit-<component>.json`（completed/interrupted） | 否 |
| 1 | 单轮业务失败（--once 轮询失败/对账不一致——动作不是故障） | `exit-<component>.json` | 否 |
| 2 | fatal：启动失败或主循环外的未知异常（guarded_main） | `exit-<component>.json` + traceback | **写** |
| 3 | 单实例拒绝（instances 内核锁） | `exit-<component>.json` | 否 |
| 75 | T110 网关期限硬停（单次 MT5 调用 >30s，`os._exit`） | `gateway-stall.json` + `exit-<component>.json` | 否 |
| 78 | T141 断线专用：连续 15 分钟连不上 | `disconnect-exit-<component>.json` | 否（supervisor 决定） |
| 79 | T142 主循环不可恢复：已知可恢复异常连续 5 轮失败或未知异常 | `loop-exit-<component>.json`（reason=unrecoverable_loop/<具体>） | **写**（fail-closed：监视者离开必须停交易） |

T142 边界（测试钉死，`tests/test_t142_loop_grading.py`）：断线类只走
T141 的 900s/78 时间窗，**不进 5 轮计数**（统一计数会让 5 轮 × interval
早于 900s 触发，破坏 T141 验收场景"断线 3 分钟恢复三进程不退出"）；断线
降级轮既不计数也不清零；只有完整成功的轮清零（对账 rc=1 是动作，视为成功）。

——取证执行：T142 施工会话（worktree `demo-t142`，分支 `t142-grading`），
2026-11-07；未改动任何运行时状态。
