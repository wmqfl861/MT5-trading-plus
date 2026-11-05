# D57 正式窗口检查独立审核（2026-11-05）

主会话转录未参与实现的独立审核者返回的结论。初版受审提交 `c994849e898f7c13434fce2193d670decee87762`，父提交 `c0df5a1d133eadbe8a8c089fba0758ed840d830b`，tree `4263ddb24d863b15b6e80256cddc30fa7675d435`。

**最终 Spec PASS / Quality PASS。** 初审曾为 Spec FAIL / Quality NEEDS_FIXES，发现5项 Important；原作者在 `2cc35cadeec9068782717a88f88e9273ed91053c` 修复后，同一审核者逐项裁定全部 ADDRESSED，修复差异未发现新增 Critical、Important 或 Minor。最终提交的父版本为 `c994849`，受测及提交 tree 为 `a57e05597d12ee9761edbd8bab9ab892e58c08e7`。

## 已复现的问题

| 问题 | 独立实测与影响 | 必须保持的修复要求 |
| --- | --- | --- |
| 非规范文件名覆盖同magic状态 | 规范 `pause-902602.json` 为true时返回1；增加全角数字别名、同magic且false的文件后返回3，规范暂停原件仍在。state读取也采用相同归并方式 | 文件名须与ASCII规范名完全一致，拒绝别名及重复逻辑magic，不得按遍历顺序覆盖 |
| 未来解除被用于当前状态 | 一条策略的正常演练已满足时，另一策略 `paused=false` 但时间为观测时刻后一天仍返回3；存在窗前暂停告警时亦如此 | 策略及全局pause时刻独立校验；未来解除不能支持当前未暂停，窗前仍有效的暂停继续阻断 |
| 晚期未解除mismatch被旧演练掩盖 | 早期A演练完成，临近观测时刻新增B的RECONCILE_MISMATCH，B仅有更早或未来解除，仍返回3 | 分开判断演练是否完成与每个magic最新异常是否解除；须有同magic且 `alert_ts < release_ts <= T` 的证据 |
| 整个窗口缺少自身runner日志仍可通过 | 删除一条策略全部runner日志，保留风控心跳，正式CLI仍返回3；旧存活消费者在同一输入下拒绝 | 恢复每个magic至少一条窗内有效自身runner事件的门槛，之后才能逐日使用风控佐证 |
| 正式调用的帮助缩写返回0 | `--accept --he`、`--accept --h` 均仅打印帮助并返回0，缺少固定历史身份/A5状态 | 统一正式解析、帮助及退出路径，返回2和固定未评估字段；单独帮助保持原行为 |

初审位置分别在 `scripts/demo_check.py` 的637/660、657/862、930/1999、896/1898、2499/2508行附近。原件缓存、部署身份关联、完整日历、窗内成交与全平关联等已读实现符合裁定方向；本轮失败由以上具体反例导致，不是要求机器证明无法独立认证的历史真实性。

## 证据归属

- 作者完整聚焦集：新专项78项、旧checker43项、直接相关T110一项，共 **122 passed / 157.18s / exit0**。受测前后tree及四文件摘要一致，提交tree与受测tree相同，工作树clean。
- 作者留存真实CLI合成场景：机械满足返回3、旧十日混入新九日返回1、缺原始身份返回2；输入字节保持不变。
- 早期119 passed/1 failed的中间态语法错误、冻结后的原场景复测及后续完整GREEN均保留，没有将该历史失败认定为当前提交缺陷。
- 独立审核者实际执行12次定点CLI调用及一次新旧存活消费者对比。使用临时合成材料和真实CLI，没有mock窗口判定；输入前后摘要一致。未重跑122项套件或全仓。
- 审核者核对HEAD/parent/tree、clean和差异格式；新专项测试checkout的字节差异已确认为LF/CRLF映射，其余内容与受测提交一致。

## 修复复核结果

原作者在固定版本 `2cc35ca` 完成五项修复，同一审核者限定复核 `c994849..2cc35ca`，逐项裁定如下：

| 原问题 | 裁定及依据 |
| --- | --- |
| 非规范文件名覆盖 | **ADDRESSED**。state/pause/expected 使用 ASCII 数字并核对完整规范文件名，拒绝重复逻辑magic；reconcile-block采用同样约束。全角数字、前导零均不能进入后写覆盖路径。 |
| 未来pause被采信 | **ADDRESSED**。每个策略pause及全局pause的未来时刻单独记为当前异常，不依赖演练是否完成，也不丢弃窗前状态。退出1与既有未来state/intent口径一致，审核者接受该分类。 |
| 未解除mismatch被掩盖 | **ADDRESSED**。对窗内每个magic的最新mismatch分别要求同magic、paused=false且 `alert_ts < release_ts <= T`。较早、等时、未来、其他magic及缺失解除均阻断；合法更晚解除保持可满足。 |
| 缺自身runner日志 | **ADDRESSED**。每个参与magic须有窗内自身运行器事件，登记及retained restart不计作自身日志；之后才允许逐日风控心跳佐证。 |
| 正式帮助缩写exit0 | **ADDRESSED**。Parser统一处理正式调用的print_help、exit、error，转为FormalInvalid并输出固定状态、退出2。单独及日常帮助继续原行为。 |

fix1仅修改checker、专项测试和runbook三文件，旧 `tests/test_demo_check.py` 与父提交差异为空；正式约束没有进入day/summary判据，身份链、日历和运行模块未扩张。审核者未发现修复差异引入的新问题。

作者实际执行证据为：**RED 28 failed、18 passed、78 deselected、exit1 → 定向46 passed、119.07s、exit0 → 完整168 passed、282.21s、exit0**。完整集无排除，包含专项124项、旧checker43项、指定T110 checker一项。受测前后tree与提交tree相同，四个任务文件原字节SHA不变，精确提交三文件且clean。此前122项GREEN与历史失败日志均保留。

复核者实读修复简报、完整diff、源码上下文、新增测试及RED/GREEN/COMMIT记录，独立核对归档diff、Git身份和文件摘要。专项测试的checkout原字节差异仅为LF/CRLF转换，其余文件原字节与作者证据一致。本轮没有重跑168项或追加CLI探针；五项疑点由代码与已有反例、对照测试闭合。

依据：[实施计划](../superpowers/plans/2026-11-05-demo-formal-window.md)、[范围裁定](2026-11-05-demo-formal-window-scope-review.md)。正式分支继续仅返回2/1/3，机械满足为3；历史真实性未核实、A5未评估。

本轮未访问真实数据、配置、DB、账户、MT5、进程、任务、保护状态或部署，未修改审核checkout、HEAD或index，未派子代理或填写批准字段。历史真实性、实际窗口、演练结果和A5仍需各自证据；本审核不批准实际Demo运行。
