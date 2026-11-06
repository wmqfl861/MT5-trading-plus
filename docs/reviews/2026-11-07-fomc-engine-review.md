# FOMC 日历事件引擎适配聚焦评审（2026-11-07）

主会话转录独立聚焦评审者结论。固定只读 checkout 主集成树 @ `d12c83526c0b8c60753f2ebd0b926153256e44f4`（父 `1c8f754`），全程 clean；窄探针用独立解释器（真实冻结工件+合成日历，不读行情）。

**Spec PASS / Quality NEEDS_FIXES（1 项主修复 F1，主检验开跑前应闭合；其余 nit/观察）。**

## 核心确认（重点 1–7 全部 PASS）

1. **构造器正确**：SHA 三态 fail-closed；结构校验（era/announcement==end/公告日重复）；缺 bar/缺锚丢弃登记不移窗；主段过滤边界正确（半开窗由调用方施加）；EventWindow 与 month_end 同构。**独立探针复现**：真实工件 a53577d9… + 合成日历 → 257/213/dropped 0/周二82-周三123-周四8/首 1994-09-27（锚 09-26）/末 2021-04-28——与 A1 A.4 及 C005.precheck.json calendar_coverage 逐字段一致；端点两口径计数不变；锚跳洞语义正确。
2. **month_end/london_fix 逐字不变**：events.py 零删除行；bt.py 10 行删除全枚举（docstring/签名/choices）；既有两分支语句未动；163 事件套件全绿。
3. **抽样池限主段真实**：数据流 main_days→days→randomization_test 显式传入；全量日历不进随机化；随机窗探针（seed=7）全部池内、星期分布精确保持、零重合、出场不越池。E10 满足。
4. **CLI 四参成对+运行前拒绝**：Ledger 打开前绑定，拒绝零 runs（committed 测试四类缺参/他 kind 携带/坏 SHA）；非研究回退对 fomc 恒 raise。
5. **策略类身份方案正当**：event_kind 属性使 --event-kind 一致性核对真正接线（裸 BuyHold 会绕过）；bar 级恒多只进基线/脚手架不进主检验（D35 两否决 not_applicable 预写）。
6. **event_returns 口径正确**：FRED close-only open==close 物化 + 交易日零点锚 → 毛收益=前收进/公告日收出登记定义；static_long 恒+1。
7. **无新破坏**：唯一卫生失败为父辈 P 线登记文书的既有失败（单测复跑+溯源确认）。

## F1（主修复——登记绑定机械核对缺失）

`_fomc_event_binding` 只校验四参自洽；构造器只绑到**传入的** SHA——与登记侧无机械交叉核对。`_load_h_research_inputs` 已装载台账最近 precheck（其 precheck_source.calendar_sha256=a53577d9… 与 calendar_coverage 就在手上）却无处比对。后果：(a) 自洽的重抓日历可无机器异议跑完主检验（H102 falsified_if 的 fail-closed 目前只靠评审层执行）；(b) `--event-days-start 1994-09-27` 字面取值会致首事件 dropped_no_anchor → 212 vs 登记 213/0/0（探针 P1b 实测），无机器比较；(c) gates.details_json 不含 calendar_sha256 与主段窗，机器审计链断开。**修复（小）**：fomc 分支有 precheck 载荷时比对 SHA 与 window_events/dropped 逐字段，不符 ValueError；report 并入 V3 details_json。

## N1–N3（nit/运行注记）

N1：日期解析宽松（"Jan 1 2024" 通过；aware 串 TypeError 裸 traceback）——改严格 YYYY-MM-DD+捕 TypeError。N2：成本小时口径（FRED D1 零点→NY19/20 vs 声明 entry_hour_ny=16）与 month_end D1 同一共犯既有约定、两侧同口径二阶影响——首次运行报告并列声明。N3：真实运行须传 --event-days-start 早于 1994-09-26、--event-days-end 2021-05-17 方复现 213/0/0——并入 F1 机器化。

## F1/N1 修复定点复核（2026-11-07，转录）

修复提交 `26b2f90a60fbbf5248bbdad52bc9e6b0db651733`（父 `af7801b`，仅 bt.py +100/−7 与测试 +139）。同一评审链复核：**F1 ADDRESSED / N1 ADDRESSED / 无新破坏 / Spec PASS / Quality PASS。**

- F1 五要素机械验证成立：比对源确为台账 candidate_checks 最新 result_json（无重算/无硬编码 213）；六对字段名与真实登记工件逐一解析核对；比对位于台账写入前（CLI 反例断言零 runs）；SHA/计数不符 ValueError；details_json 审计链落账（calendar_sha256/pool/precheck_binding/window_events/dropped）；无载荷合成路径标注。
- N1 严格解析（str 门+TypeError 捕获+NaT/aware 门+isoformat 回代）；评审者探针补测 10 个边角全部正确拒绝。
- 引擎已 PASS 部分零触碰（diff 范围核对）；触及路径两测试文件 17/17+52/52 实测。
- 残留观察（不阻塞）：载荷存在但缺 coverage 字段时按 no_registered_precheck 放行——低风险宽裁量，C005 重登记流程变更时宜收紧。

主检验经此完整链运行。
