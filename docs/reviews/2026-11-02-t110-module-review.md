# T110 独立复核结论与集成核对记录（2026-11-02）

本文件由主会话整理，是独立审核结论的转录与集成证据记录，**不是主会话自我批准，也不冒充审核者直接撰写的文件**。独立审核者因其工具上下文的报告输出限制直接返回结论，没有产生报告提交；其原结论、范围和未验证项在下文分别保留。

## 1. 受审版本与结论

- 原规格：交接文档 T110，及第十五轮 H15-3。
- 受审实现：`b488d6f3bf8239723139d1530b47998f1df8c80f`，基点 `533213f5c2e97b10fb1cda6e9d1b154b5327e0ca`，22 个文件。
- 审核者与实现者分离，使用独立 worktree。第一轮审核完整冻结补丁；修复后由同一审核者核对三项原发现及修复引入的问题，没有重复运行实现者的测试。
- 原审核者最终结论：**“三项原发现均为 ADDRESSED；本轮修复差异中未确认新的阻塞 finding。”**
- Spec compliance 原结论：本轮三项修复符合对应规格，原审查阻塞项可关闭。
- Quality 原结论：定点复核通过，未确认修复引入新的阻塞问题。以上均限于所审 SHA 和修复范围，不替代最终整合验收。

## 2. 原发现及定点复核结果

以下代码行号均指受审提交 `b488d6f`。

| 原发现 | 独立复核结果 | 证据位置与边界 |
| --- | --- | --- |
| P1：循环起点时间早于后来更新的健康证据，导致误判 future、误 halt 或重启 | ADDRESSED | `src/mt5live/monitor.py:199` 在读取心跳后取观测时钟；`gate.py:302`、`riskproc.py:470` 与 `scripts/live_supervisor.py:344` 分离业务截止时间和健康时钟。真正未来、缺失、陈旧、不可读的证据仍拒绝。 |
| P2：查询线程先超时，只能看到自己的 ContextVar，遗漏另一线程在途订单的 unknown | ADDRESSED | `src/mt5gw/connection.py:36` 进程级登记在途证据回调；`:71` 首次超时取得唯一 fatal owner、冻结并尝试写入所有相关回调。原 RLock 交易原子区保留，退出码仍为75。磁盘不可写或原生扩展持续持有GIL的边界保留。 |
| P1：固定部署仍走旧启动器父PID停止／补缺路径，可能留下存活worker或并起第二份 | ADDRESSED | `scripts/demo_run.ps1:184` 路由到 supervisor；`live_supervisor.py:247` 核验登记、完整argv、创建时间和父子关系，`:301` 停止入口要求killSwitch，核验后以绑定创建时间的句柄子先父后终止并保留登记。旧身份、不确定PID及存活孤儿路径保守拒绝。 |
| 缺父但worker存活测试缺断言 | 已补齐 | `tests/test_t110_supervisor.py:187` 明确断言 refused、零启动、零终止，纳入最终聚焦回归。 |

测试覆盖同连接锁竞争、异连接查询先超时、正常回调结束后移除证据；Windows fake 覆盖 launcher/worker、父死子活、不可读身份、PID复用和固定SHA启动／停止入口。fake验证不替代真实MT5演练。

## 3. 已有验证证据

实现者最终聚焦命令覆盖：

```text
python -m pytest tests/test_t110_deadline.py tests/test_t110_health.py
  tests/test_t110_supervisor.py tests/test_mt5live_gate.py
  tests/test_mt5live_integration.py tests/test_mt5live_riskproc.py
  tests/test_mt5live_paper.py tests/test_demo_run_ps1.py tests/test_demo_check.py
  tests/test_connection.py tests/test_gateway.py tests/test_config.py -q
```

- **334 passed，105.14秒，退出码0**；主会话与独立审核者均直接读取原始结果。
- 原始本地证据：`.superpowers/task-t110/review-final.log` 与 `review-final.exit`（开发 worktree；不提交运行目录或敏感原始日志）。
- H15-3：NOT-REPRODUCED，退出码0，只执行该探针。
- 早期完整套件：2832 passed、4 failed、3 skipped；四失败属于旧paper夹具缺risk心跳。补齐后已由最终聚焦覆盖；该早期完整运行**不是最终版本全绿证明**。
- 第二轮旧完整运行因审查发现而停止，记录 `stopped_by_review`，未完成、不计通过。

## 4. 主会话集成核对

本模块作为 `6d85ef1` 合入隔离集成分支。主会话用 Git 对比确认 `src/mt5live/`、`src/mt5gw/`、三个业务脚本、supervisor和启动器与受审 `b488d6f` 无差异。runbook相对受审稿的增量仅保留 main 已有的独立 Demo／PaperSoak 日报补跑示例；不影响产品代码。

同批独立 `[OPS]` 提交 `1f739d7`（来源 `20d5093`）仅追加11-02事故记录。该19行文档补丁由另一未参与编写的独立审核者只读核对，返回 **approved**；此结论只覆盖脱敏与证据措辞，不批准运行时恢复。缺仓原因依旧未明，不能把同品种反向成交时间接近当作持仓关联或SL/TP原因证明。

集成后的全量测试与对应main CI尚须在交付时分别核对；本记录不预写通过数字。交付证据以实际main提交和tests run_number为准。

## 5. 未完成条件

- 未部署新代码、未安装监督计划任务、未执行真实Demo故障演练。
- 网关期限的Python线程需要GIL，原生GIL卡死由外部监督兜底；磁盘不可写时不能承诺unknown或退出标记无条件持久化，原发送前pending仍需恢复对账。
- 现有Demo未解释缺仓继续阻断实际恢复、解除halt/pause与部署。离线技术通过不能消除运行时事故或启动正式十交易日窗口。
- 最终完整回归、推送后main CI及维护窗口中的真实演练是后续条件。没有填写 `approved_by`、批准研究、放宽门槛或改变用户U1–U5决定权。
