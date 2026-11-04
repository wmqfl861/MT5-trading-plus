# T121 离线入口的独立模块审核与修复复核（2026-11-04）

本记录由主会话转录独立审核者直接返回的结论，并区分实施者测试证据与审核者定点探针。审核者未参与实现；本记录不是主会话自我批准。先前审核服务错误和重复显示的旧报告不计作结论，以下采用明确绑定最终提交的有效复审。

## 版本与结论

- 模块基点：`d758416c1b06a14db3d6ca0014f7d1c1b24cf44f`。
- 首轮受审提交：`0036e6f1bbb27ad0b2f09fefd81df22a1264a871`，包含计数/资格参数、合成可行性模块、CLI、测试及使用说明。
- 修复受审提交：`084cde17a759958150848b6146e632104354fe78`，提交树 `58b15377fae75af365f0a8ad162e7dacd1f7fb0d`。
- 首轮独立结论：Spec FAIL、Quality NEEDS_FIXES，四项 Important，无 Critical。
- 同一审核者定点复审范围为 `0036e6f..084cde1`，最终结论：**四项均 ADDRESSED，Spec PASS，Quality PASS；修复 diff 未发现新的功能性破坏。**

该结论是离线工程技术审核。生产输入仍为 `not_released` / `waiting_data`，不授予实际筛查、研究、P1/P2 或部署许可。

## 首轮问题与定点闭合

下表行号均指最终受审版本；复审读取修复 diff、最终测试日志，并补充有限合成探针，没有重跑完整套件。

| 问题 | 修复与独立复核证据 |
| --- | --- |
| 非法收费星期导致漏计隔夜费 | `feasibility.py:171–176,329–335` 严格拒绝 bool、字符串、小数和越界 weekday。非法星期与缺 overshoot 同时存在时保留 fail 和 waiting，成本为 null。首轮合成探针中非法星期曾使同一情景由0.0236R/fail变成0.0036R/pass；该路径已关闭。 |
| 成本缺测掩盖其他非法值 | `feasibility.py:287–310,338` 独立校验 p90、覆盖字段及 event hour，完成校验后再统一返回。NaN/Inf/负 p90 与缺覆盖、非法 event hour 与缺 overshoot 均保留双重原因；生产 evidence 仍在数值校验前拒绝。 |
| 等价 UTC 缺口产生不同计数 | `feasibility.py:121,156–168,528–549` 统一端点为 UTC-naive，资格、身份收据和矩阵路径采用一致时间基准。审核者探针确认 naive、Z、等价非 UTC offset 产生相同 qualification、receipt 和报告；反向、NaT、非法时间及长度错误仍拒绝。 |
| 冷启动加载标签、概率表和回测引擎 | `prob/__init__.py:10–39` 改为惰性 legacy export，保留11个原公开名及对象身份；`pline_feasibility.py:20–29` 将该文件纳入代码收据。首次导入前安装 guard 的 CLI help/合成计算测试通过；旧 named/wildcard/子模块导入保持。 |

第四项涉及原包初始化的既有依赖。为满足新入口原定的隔离契约，修复范围最小扩展至 `prob/__init__.py`，没有修改标签、表、引擎或冻结方法本身。修复提交只含该文件、`feasibility.py`、`pline_feasibility.py` 和专项测试；旧计数/资格默认参数未在此修复轮再次更改。

## 模块范围与验证证据

首轮审核已检查完整四根 H4 与来源传播、截止边界、S2 已完成会话及实际 H1 窗首、S3 480/120 与 anchor 资格、仅用未来时间戳判资格、信号已知价计费、外部 spread-only 倍数、UTC 日等权、96行/8汇总及旧参数兼容；除上述四项外未发现其他阻塞。该范围不包括真实数据与成本用途批准。

实施者保存、主会话及审核者读取的测试结果：

| 阶段 | 结果 |
| --- | --- |
| 首轮固定树聚焦回归 | 101 passed，退出码0 |
| 四项修复 RED | 30 failed、68 passed，退出码1 |
| 中间修复 | 97 passed、1 failed，退出码1；失败为代码收据缺少新增初始化文件哈希，随后补齐 |
| 最终修复专项 | 98 passed，退出码0 |
| 最终暂存树兼容聚焦集 | 137 passed，退出码0；受测树与 `084cde1` 提交树一致 |

最终聚焦命令：

```text
python -m pytest tests/test_t121_feasibility.py tests/test_prob_counting.py tests/test_prob_t97_wiring.py::TestSuppressAcrossGaps tests/test_prob_t97_wiring.py::TestSituationWindowStart tests/test_prob_t97_wiring.py::TestEligibilityMask tests/test_prob_t97_wiring.py::TestEligibilityP203 tests/test_prob_t97_wiring.py::TestEligibleBarsP203 tests/test_prob_t97_wiring.py::TestCountTriggerSuppression -q
```

审核者另行执行了非法星期/小时与缺测并存、UTC 表示等价、冷启动 guard 和旧 API 身份的窄探针；没有把读取日志写成独立重跑测试。全量回归、集成版本身份及推送后 main CI 由主会话在交付阶段补证，不以137项局部通过代替。

## 保留条件

- 合成标记是调用者声明，哈希只能绑定内容，不能证明数据来源或批准。当前入口不能用来将真实输入改标合成后执行。
- 生产准入 adapter、合法数据范围及 manifest、完整符合 D50 的成本证据仍须独立审核和明确执行放行。当前 charged 合成费率格式的限制见[使用说明](../research/t121-offline-entry-2026-11-03.md)。
- [A2](2026-11-02-t121-a2-review.md)、[A3](2026-11-02-t121-a3-review.md)的原范围与未运行报告保持；没有实际触发、成本或可行设计族证据，不启动 T122。
- 冻结门槛、源码基线、方法登记及成交语义不因本模块改变。D61 和正式轮次仍约束 P2；本次未读取真实数据、台账、锁箱或连接 MT5。
