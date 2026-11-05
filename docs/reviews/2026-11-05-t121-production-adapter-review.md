# T121 生产准入适配独立审核与修复复核（2026-11-05）

本记录由主会话转录独立审核者直接返回的结论，区分实施者测试和审核者合成探针。审核者未参与实现。最终受审版本为 `5e261e6`，结论为 **Spec PASS / Quality PASS**：原撤回水位、循环绑定及 fix1 新增消费时效 Important 均已闭合，fix2 diff 未发现新的阻塞问题。此技术记录不构成真实 release、数据读取或研究批准。

## 固定版本与证据

- 基点：`def8744d90f75519ed9486e8d1ee7efa79ba2e6a`。
- 初版：`8e89e83b9b295a95e2522a381569ee765dc3869f`，tree `44250d239f1d880948081532e67d5af9f089dea7`。
- fix1：`b213749564b7e86d6db88bc5e38ea98687e2a594`，tree `e7606e611ec5fbfaba96838e4e1cb8c5673e1a4b`，父提交为初版。
- fix2：`5e261e6ad7ad242b187fc8f5e5e20023cd0546b4`，tree `d47bb49b083b166e21e43995b505f80c1929d09e`，父提交为fix1；同一审核者最终 **Spec PASS / Quality PASS**。
- 初版作者相关集：191 passed、退出0。
- fix1 作者 RED：2 failed、退出1。最终相关集209 passed / 455.36s / 退出0；受测与提交tree相同，六个任务文件测试前后原始字节哈希不变，提交后clean。

聚焦命令如下；原有 T121 回归无排除项：

```text
python -m pytest -q tests/test_t121_production_adapter.py tests/test_t121_feasibility.py --tb=short -s
```

## 原两项 Important 与 fix1 裁定

**R1：新版撤回没有留下观察水位。** 初版在 `_trusted` 遇到 revoked 提前退出，水位只在 `_claim` 推进。审核者用两个新进程先观察v2撤回，再用同一状态根执行旧v1 active，实际生成96行/8汇总及非零触发，水位回到v1。fix1 将完整可信快照的持久观察与具体 release 的消费分开；新版 revoked、合法 unknown、expired、future 先保存水位，再拒绝执行。无效快照不推进，旧版本和同版本冲突拒绝。原审核者裁定 **ADDRESSED**。

**R2：登记与实现身份循环绑定。** 初版 registry 位于 adapter 源码，保存 manifest 原始字节SHA；manifest 同时绑定 adapter 完整源码和当前HEAD，首次登记便使原清单失效。原正测替换信任快照，未证明正常登记可行。fix1 把静态 trust 移到固定 `production_releases.py`，manifest 绑定稳定实现I及固定计算闭包；随后只登记trust得到H，验证祖先与逐文件身份，运行回执另记H和trust来源。原审核者裁定 **ADDRESSED**。

六条独立修订条件完整整理于[工程裁定补充](2026-11-05-demo-readiness-engineering-ruling.md#实施中独立复核的设计修订)。fix1复核逐项确认静态信任、单向身份、观察与消费分离、限定拒绝副作用和正常登记证据成立；锁内最终授权中的时间有效性仍有下述缺口。

fix1 正链在临时真实 Git 历史固定实现I，封印合成manifest/review，仅提交静态trust生成H，再由新解释器正常loader/main进入原内核。未替换trust、registry、Git身份、准入或claim判定；启动器只隔离clock/state。实际产生96行/8汇总、目标行1次触发、`cost_r=0.10675`。另有v2拒绝后v1回滚、同版本冲突、跨进程同步撤回先于claim、闭包变化和未登记trust拒绝证据。

重复active测试允许读取已绑定metadata以取得execution_id，但以哨兵禁止data/cost与内核，核对原claim/report字节不变及无新output。revoked/unknown继续禁止全部材料读取。中间测试曾错误禁止该metadata读取，修正测试边界后得到上述最终209项结果；生产拒绝逻辑没有因此放宽。

## fix1 新增 Important：耗时核验中到期仍消费

审核者在 `production_adapter.py:478–487` 确认 `_claim` 先取得 `now` 并检查时效，再执行逐文件Git和磁盘核验的 `_implementation`，之后直接创建claim。共享锁能串行化水位，不能阻止时间流逝。

审核者执行正常登记、新解释器、正常加载器的临时合成窄探针，仅隔离clock/state并观察真实I/O：manifest/review在 `00:00:01` 到期，锁内初检为 `00:00:00`，闭包读取期间将隔离时钟推进到 `00:00:02`。claim打开时已经过期且clock此前仅读取两次；程序仍返回0，产生1个claim和96行/8汇总，目标行1次触发、`cost_r=0.10675`。该探针证明时效拒绝失效，没有修改受信代码或伪造registry。

原审核者要求先完成耗时核验，再紧邻claim重新读取可信时钟、核对snapshot/manifest/review及条件。既有设计以claim持久化完成为授权点，因此落盘后也须复核时效；到期时保留claim、拒绝后续data/cost，不回收执行。

fix2 已固定为 `5e261e6ad7ad242b187fc8f5e5e20023cd0546b4`，父 `b213749`，受测及提交tree `d47bb49b083b166e21e43995b505f80c1929d09e`。作者在耗时身份核验、摘要和claim序列化之后，以及claim持久化完成后，分别重新取得可信时钟，检查已核验snapshot/manifest/review/conditions的有效时间交集。真实临时Git登记定向反例为 **5 failed、1 passed、退出1 → 6 passed、退出0**；最终完整相关集 **215 passed / 552.87s / 退出0，无排除项**，七文件原始字节哈希前后一致。落盘中到期保留claim和水位，禁止data/cost、内核与output；以原有效时钟重试仍为consumed。正常有效时间仍产生96/8及`cost_r=0.10675`。这些是作者测试证据。同一审核者已完成 fix2 定点复核：**时效 Important ADDRESSED，Spec PASS / Quality PASS，修复 diff 未发现新的阻塞问题**。审核者核对有效区间交集包含条件 `closed_at` 下界，终点等号和时钟回退越过下界均拒绝；最终两次取时分别在 `production_adapter.py:505`、`:513`，后者位于持久化之后、退出共享锁之前。

## 证据归属与保留边界

fix1复核读取作者209项日志、源码、提交/tree和归档diff，没有重跑该套件；另执行上述时效窄探针。fix2复核实读作者RED、定向GREEN、最终215项日志与提交记录，独立核对源码、测试机制、归档diff、Git身份及七文件摘要；审核checkout四项原始字节仅有LF/CRLF映射差异，其余三项精确匹配，均与提交对应。fix2没有未解疑点，未重跑测试或追加探针。各轮审核checkout、HEAD、index保持不变，未派子代理。

正式trust保持空、未初始化，没有真实release。固定受信代码、Git、系统时钟及本机消费状态是边界；无法得知从未观察的未来撤回，不防主动删除状态或篡改受信代码。OS重启、断电和存储故障未验证；Windows目录持久化能力限制沿模块说明保留。2026成本摘要用途、完整D50依据、charged格式及真实研究/部署条件均未由技术审核放行。
