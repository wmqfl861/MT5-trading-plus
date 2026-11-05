# D61 V2-A 计算核独立代码审核（2026-11-05）

主会话依据未参与本轮方案、范围裁定或实现的独立审核者直接返回的完整正文整理。本记录区分作者测试与审核者的独立核对，不构成完整 D61 研究或产品放行。

**Spec PASS / Quality PASS；Critical、Important、Minor 均无发现。**

## 固定版本与范围

- 基点及父提交：`c0df5a1d133eadbe8a8c089fba0758ed840d830b`。
- 受审提交：`0626dd7b7a65e39eb982ce8ac39fcd2b23dc9e11`。
- 受审 tree：`7be5c08f2213b2f1c9edcd59bc11f656bf2f7589`。
- 仅新增[计算核](../../src/mt5quant/prob/d61_v2a.py)、[专项测试](../../tests/test_d61_v2a.py)、[模块说明](../research/d61-v2a-kernel-2026-11-05.md)，共932行。
- 审核依据为[实施计划](../superpowers/plans/2026-11-05-d61-v2a-kernel.md)、[范围裁定](2026-11-05-d61-v2a-scope-review.md)和归档设计。审核前后 checkout、HEAD 和 index 均未改，工作树干净。

## 独立代码判断

| 项目 | 审核结论 |
| --- | --- |
| 严格输入与空输入 | 查询筛选前验证全部行、支持集、查询及固定 `lb_z=1`。拒绝 bool、非法枚举、非有限数、越界收益、非成对缺失及同 `(s,a,day)` 特征冲突。整数直接转精确 Fraction；全局 a 的有限 binary64 精确性检查先于空输入返回，拒绝 `±(2**53+1)` 并允许精确 `2**100`。 |
| 全局日与分母 | 每层先选行，再按全局日聚合。同动作跨局面的同日行共同平均，没有把 `(parent,day)` 当独立日。目标 W 逐条缺失填 a，以全部计划行为分母；中心 Y/Z 使用当日可见行分母，全缺失日不增加中心资格，不补 ID 空洞。 |
| 回退、收缩与未知值 | cell/parent 各自按100个可见日过门，随后回退非空动作池；m=100只参与中心。条件收益按实际公式分母判 None，局部缺类但池中存在时可得到已知收缩均值；全池未出现类别保持 None。 |
| 空目标与全缺失 | D=0返回lower=guard=a，mean_w/radius=None，可保留池中心诊断。D>0即使中心未知也计算radius/guard，仅lower取a。半径只使用目标计划日D。 |
| 保守数值链 | 私有80位Decimal上下文明确精度、指数范围、clamp、flags和traps。ln/sqrt按HALF_EVEN后取同context的next_plus，Fraction商按CEILING；后续公式保持Fraction。最终float与精确有理值比较，必要时定向nextafter一次并重验方向与有限性。lower/guard向下、radius向上；无诊断回流、精度重试或开门epsilon。 |
| 独立oracle | 测试以正项有理级数及显式余项包围ln900，再用整数isqrt和精确平方比较包围平方根。期望区间不依赖被测半径，验证上包围及最终lower/guard不高于理想公式。 |
| 复制、排列和污染 | 完整全局日相关行按1/3/10倍复制，核心float.hex不变、行计数如实增长、日数不变。固定目标改变S2同动作池后，W/D/radius/guard保持；精确不等式和相同方向转换保证 `a≤lower≤guard`。不承诺单行复制不变。 |
| 隔离及外部上下文 | 核只导入标准库，不进入产品包级导出。冷进程真实导入和调用限制业务文件及外部操作；输入保持不变。测试修改当前和DefaultContext的精度、舍入、指数范围、traps与flags后，结果和外部状态均保持。 |

手算池为2个全局日、中心1，`q=(2/3,1/3,0)`、`v=(5/3,-2/3,0)`；缺失样例D=2、可见日1、mean_w=-0.5。以上来自代码、断言机制和已有作者输出的交叉核对。

## 临近0.10的失败与修正

作者曾出现91 passed / 1 failed：原“below”测试常量实际位于阈值上方。修正仅将原值作为above，并取前一个binary64作为below，增加独立有理区间断言；最终仍要求 `low.lower < 0.10 <= high.lower`，没有放宽门槛。

审核者实读失败日志、诊断文件及修正前后测试快照，并执行一次不导入计算核或测试模块的独立纯有理探针。采用另一分解 `ln900=8ln2+2ln(3/2)+2ln(5/4)`，每项64个正项及几何尾界，包围宽度小于 `10^-60`；以精确平方比较判断理想下界，不调用Decimal或平方根函数。结果为：

| 人工收益 | 相对binary64 0.10及精确1/10 |
| --- | --- |
| 0.491221014485159 | 均严格在下方 |
| 0.49122101448515904，原below | 均严格在上方 |
| 0.4912210144851591，原above | 均严格在上方 |

探针退出0。失败、修正和最终提交的计算核SHA256相同，未修改核公式迎合测试。

## 测试证据与身份

| 阶段 | 作者实际结果 |
| --- | --- |
| baseline | 旧兼容27 passed |
| red-contract | 缺实现4 failed，非收集错误 |
| green-aggregation | 66 passed |
| red-bound | 已导入核上的4项下界测试失败 |
| green-bound | 91 passed、1 failed，保留临界常量失败 |
| green-bound-corrected | 92 passed、exit0 |
| green-final | 新核94＋旧兼容27＝121 passed、6.80s、exit0 |

最终命令：

```text
python -m pytest -q tests/test_d61_v2a.py tests/test_prob_tables.py tests/test_prob_decision.py
```

审核者实读上述日志、身份JSON、固定样例输出、运行及提交脚本，并独立核对Git blob和受测快照。受测前后tree与提交tree相同，三文件作者raw/blob SHA相同。审核checkout为CRLF、作者受测快照及Git blob为LF，仅规范化CRLF后逐字节相同，没有内容漂移。

**121项是作者执行证据，审核者没有重跑套件。** 审核者的新增执行仅为上述独立有理探针，未把它描述为统计研究。

## 未验证范围

未重跑全仓或CI，未验证其他Python或平台组合、完整2304条件矩阵、覆盖率与误开放率、V2-B、真实支持集及跨日独立假设、T121/T122实际可行族、真实labels/fit、产品接入或Demo。未访问真实数据、配置、DB、账户、MT5、业务进程、任务或部署，未派代理或填写批准字段。

本结论只覆盖固定提交的隔离计算核和限定人工验收。旧P2-10 RED与D61/T92阻断保持，完整研究、产品替换及Demo条件仍须分别取得证据。
