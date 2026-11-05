# D57 正式窗口机械预检 Implementation Plan

> **For agentic workers:** Use subagent-driven-development in the assigned worktree. Controller assigns an independent code review. No nested agents.

**Goal:** 修复阶段F验收的跨部署窗口与窗外证据误计，提供绑定原记录的机械预检，并保留独立A5的实际验收责任。

**Architecture:** 在现有demo_check增加formal分支，先加载严格证据索引和日历、建立部署身份链，再构造统一事件窗口及独立当前状态视图。复用已有判据，不修改launcher/supervisor/交易运行代码，不建通用授权系统。日常day/summary保持。

**Tech Stack:** Python3.12标准库、既有检查器依赖、pytest。无新增依赖。

**Spec:** [独立范围裁定](../../reviews/2026-11-05-demo-formal-window-scope-review.md)。其固定schema、退出码、窗口与证据规则是本任务要求，覆盖初始请求中的旧正例exit0和deployment_record歧义。

## Global Constraints

- 本批仅合成临时证据，禁止真实data/config/DB/账户/MT5/进程/任务/部署读取或写入，不清保护。不改历史运行日志。
- 正式分支绝无exit0或“阶段F验收通过”；机械满足exit3，历史身份verified=false、a5_status=not_evaluated固定。无新approval/start/end/now开关。
- 原始证据单次读取、同批bytes哈希/解析、输入不变；描述只是转录索引，不能制造历史事实。
- 仅checker/专用测试/原测试必要兼容调整/runbook用法；不修改demo_run、live_supervisor或冻结研究/交易源码。不抽通用证据框架。
- 精确[OPS]提交、作者不推送/派代理/自填approved_by。主会话独立审核后统一集成全量和同提交CI。

## Review Focus

- manifest没有SHA且instances被重写，当前身份不能回填历史；每次launcher/supervisor重启须绑定留存原件。
- required days由完整日历与一次now产生，首尾无活动和十日之后异常不可被活动跨度隐藏。
- 两侧归因/滑点与演练均按自身事件时刻和magic关联，旧样本或未来解除不可补足证据。
- 持续pause/pending/halt即使建于窗前仍检查，不能因今天无活动而绕过。
- 机械成功不能被命令退出码或人类可读文案包装成正式验收；旧日常行为不退化。

### Task 1: 正式窗口证据解析、判据修复与CLI

**Files:** Modify `scripts/demo_check.py`, `docs/ops/demo-runbook.md`; create `tests/test_demo_formal_window.py`; minimally modify `tests/test_demo_check.py` only where existing CLI accept assertions intentionally conflict with the approved contract. 保留原断言的实质检查，不删测试或排除用例来求绿。

**Interfaces:**

```python
resolve_formal_window(manifest, descriptor, bound_records, *, now) -> FormalWindow
build_accept_ctx(raw_ctx, formal_window) -> dict
```

局部dataclass/结构以周围代码为准；至少包含deployment_sha、anchor、start_utc、end_utc、required_trading_days和required_calendar_days。单批读取缓存供正式loader使用，避免build_ctx重复打开已绑定文件。时间注入只供函数/测试，不成为生产CLI或环境变量入口。

- [ ] 先构造合成反例，真实CLI复现裸accept可进入旧默认读取或旧窗样本/活动跨度误计。保存有效RED；新增接口缺失可以作为初始RED，但窗口逻辑缺陷还须由明确原行为反例支撑。
- [ ] 新CLI `--accept --formal-window` 且显式data/log/reports/sessions路径。parse与descriptor基本校验先于默认data/config或旧build_ctx。非accept搭配formal-window拒绝；不支持任意起止时间。day/summary保持现有解析/读取/输出。
- [ ] 实现裁定的完整FormalWindowV1/RunBinding/Ref/deployment_claim和Calendar schema。拒重复/未知键、bool充整数、错误枚举/日期/时区/哈希。文档准确固定原文选段摘要域：对源UTF-8原bytes按splitlines(keepends=True)选一基闭区间并连接，保留原行结束符计算SHA；编码错误拒绝。引用路径以描述文件目录为显式证据根解析，原始身份内路径只比较不跟随；不能读取所指运行配置或部署代码。校验引用bytes一次并缓存复用，拒同一实际工件的冲突hash。路径对外只输出逻辑别名/错误码，避免泄露真实绝对路径或原异常。
- [ ] deployment_claim全文/选段hash、完整SHA/UTC完成时间/PID字面与机器原件核对，不能用短串子串误匹配PID。过程角色/magic按实际生产字段核实；risk/reconcile无单策略magic时按既有记录合法值处理，不凭空赋runner magic。锚指manifest快照末run，绑定初始完整角色集合/逐次启动清单/instances；不将登记时刻称为独立OS创建时间。
- [ ] 当前manifest必须保留旧runs数组前缀，后续每run恰有原件binding；校验完整Demo/SHA/部署和数据目录/角色magic/PID/关键argv。现有生产文件按真实合法结构接受，不放宽descriptor。校验所有锚后supervisor journal物理行及其before/after实例角色PID链，坏行/不可读/缺原件拒绝。异SHA/mode/路径混入后切回也拒绝，同SHA重启不重起算；锚后移与初始材料矛盾拒绝。机械一致不证明全套原件真实，输出仍固定未核实。
- [ ] 严格完整UTC日历覆盖C日期至T日期，日期唯一连续、交易/休市原因与引用行依据一致，工作日休市需具体日期/理由；无活动不能当假日。S为首个交易日00:00≥C；未来锚或未开始窗拒绝。已结束交易日至少10且全部检验；当前未结束日不强求日报，但既发生异常和应到存活证据检查。连续性要求含全部自然日，不取已有活动首尾。
- [ ] 正式原始读者对关键坏行/无事件时刻/不可读文件失败关闭，不能先经旧宽松loader丢行再验证。deals/orders/alerts/logs/归因及滑点参考/演练一律以自身事件时刻筛[S,T]；不要用mtime/decisionbar/下一条日志时间填空。重新派生日、小时、magic。可复用合法解析纯函数，旧day/summary宽容语义无需全局改掉。
- [ ] SL/TP只算窗内；killSwitch恢复和pause解除加T上界，pause必须同magic；flatten命令/ack满足裁定不等式、相同id、全部magic及非负int closed。归因/滑点匹配也包含magic和窗内参考。未来及窗外证据不得支持成功。
- [ ] 当前状态单独视图，不按创建时间筛选，不依赖activity latest_day；pause、pending exits、intent/positions差异、killSwitch、halt、halt-risk-veto、异常或不可读均阻断，保留已有合法在途宽限。输入检查不能顺便执行恢复或清理。
- [ ] 统一正式结果：invalid证据exit2；完整可解析但天数/样本不足或异常exit1；机械满足exit3。所有正式结果恒historical_identity_verified=false/a5_status=not_evaluated；不得输出正式通过。保持普通日报/汇总exit行为。
- [ ] 新专项用真实CLI子进程、临时原始证据及同批hash，不mock窗口裁决。覆盖裁定9类反例及合成机械正例：旧10+新9、缺历史/短SHA/近似时间、launcher与supervisor混合后切回、同SHA重启和后移锚、首末日/假日洞、旧SLTP/未来ack/不同magic解除、窗前持续保护、坏journal/删前缀、参数先拒读。对当前时间夹具按一次now构造已结束日，必要时函数级固定时钟补边界测试，生产不加测试开关。所有输入bytes不变；正例exit3且不能含“阶段 F 验收通过”。
- [ ] 兼容验证运行完整tests/test_demo_check.py、新专项及直接涉及的既有T110 checker测试（定位实际名称后给理由），不重跑T121/T78全套或全仓；若旧accept断言冲突，只按新明示契约迁移，保留判据负例和day/summary用例。runbook只更新正式用法、退出码和证据需求，明确不能凭新JSON补历史。
- [ ] 保存RED/GREEN命令/exit、最后受测树及任务文件前后SHA；精确[OPS]提交，提交tree与受测tree一致且clean。证据本树`.superpowers/sdd/2026-11-05-demo-formal-window/`，直接回传完整正文与路径，不额外写共享报告Markdown。未参与实现者独立代码复核后才能集成。

## Self-check

范围裁定schema/退出码/时间/身份/持续保护逐项落实；初始方案exit0已被独立裁定明确替换。H/P新增文件与本任务无交集，共享状态文档由主会话处理。原件字节选段规则是本计划的明确格式决定，只做机械绑定；不会升级为真实性或A5批准。复杂之处限现有checker，若必须拆文件或改生产记录格式，先报告具体依赖，不自行扩张。
