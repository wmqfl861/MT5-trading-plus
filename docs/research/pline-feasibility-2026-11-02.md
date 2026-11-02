# T121 P 线可行性筛查报告（未运行，A3 待审）

日期：2026-11-02。**筛查未运行，完整合规成本不可用；全部 96 个预登记组合及 8 个族/品种汇总为 `waiting_data`。** 这是依据冻结缺测记录作出的不可执行状态判断，不是实测结果、成本超限结论或机制不存在的证据。触发数、UTC 触发日数、实际成本及其三倍值均为“未运行/未知”；成本门和样本量门均未评估，不能称为通过或失败。

## 1. 依据、批准边界与报告身份

- 已审核[预注册稿](pline-feasibility-prereg-2026-11-02.md)：提交 `16dca5c05e6ab5064c822c88eb2e9fb8f44ad728`。
- [独立 A2 最终报告 §6](../reviews/2026-11-02-t121-a2-review.md)：提交 `121f98df60eba4b69c92e5bc7fe7076fad8372ea`。§6.1 绑定上述预注册版本，§6.3 明确允许直接依据冻结记录制作本份未运行报告。作者工作区暂未合入该审核文件，本次只读取其已提交版本；主会话集成时合入，引用不指向另一个未审核版本。
- **A2 规则已批准，真实数据运行未放行；本报告是 A3 待审材料，A3 尚未批准。** 预注册稿中送审时的待审文字由独立 A2 报告的绑定结论解释，不由开发者改填批准字段。

本报告只静态枚举登记组合并转述已审核的缺测处置，未读取任何真实 data、核验真实输入资格或连接 MT5；未调用计数、成本、诊断、标签、拟合、收益或研究路径，未新建或修改产品代码。无触发后结局、实测 Δp 或实际绩效。报告不以先补齐入口为前提，也不授予真实运行许可。

## 2. 冻结常量与设计族

以下均为预注册常量，不是观测结果。样本量门需要实际合格 UTC 触发日数才能评估；本报告没有该观测值。

| 项目 | 冻结值或规则 |
| --- | --- |
| 预期效应 Δp | .02，设计假定，未测量 |
| 登记毛效应 ΔEV | .06R，等于 Δp×(2+1)，不是实测收益 |
| 成本门槛 | 合规冻结成本≤.02R，等价于 ΔEV≥3×成本；等号允许 |
| D52 解析所需日数 | 3014，候选内四假设，Bonferroni α=.05/4、p0=.40、power=.50 |
| 设计效应 DEFF | 1.5，固定假定 |
| 要求 UTC 触发日数 | 4521，等于 ceil(3014×1.5)；仅为门槛 |
| 发现段截止 | 2016-05-01 00:00 UTC，不含截止时刻 |
| 日单位 | 触发根时间戳的 UTC 日期；不跨品种相加，不以纽约会话代替 |
| 动作与方向 | 每局面全部 A1/A2 为 follow>base，A3/A4 为 fade>base |

| 设计族 | 周期 | 止损距离 | 止盈距离 | 持有上限 |
| --- | --- | --- | --- | --- |
| F1 | H1 | 1×ATR | 2×ATR | 24 根 |
| F2 | H1 | 2×ATR | 4×ATR | 24 根 |
| F3 | H4 | 1×ATR | 2×ATR | 12 根 |
| F4 | H4 | 2×ATR | 4×ATR | 12 根 |

每族覆盖 XAUUSD、EURUSD，每品种覆盖 S1/S2/S3，每局面包含四动作。4×2×3×4=96 是登记组合数；每族/品种的 12 个组合及 4×2=8 项汇总也都是静态枚举规模，不是触发计数。其余网格、参数、资格、S2 会话映射、S3 H1=480/H4=120 根回看及日等权聚合规则均沿用绑定预注册，本报告不作修改。

## 3. 共同阻断项与缺测处置

下表适用于全部 96 行及全部 8 项汇总。冻结缺测事实来自已审核预注册与独立 A2 报告；本次没有到真实库重验。数据资格未核验不等于已经发现数据不合格。

| 项目 | XAUUSD | EURUSD | 本报告处置 |
| --- | --- | --- | --- |
| 符合 D50 且获准的点差依据 | 未具备，未知 | 未具备，未知 | 现有 2026 点差摘要仅来源披露；不换算成本、不计算诊断或已知分量下限，不参与 feasible/fail、设计族去留或 T122 判定 |
| 符合 D50 且获准的滑点依据 | 未具备，未知 | 未具备，未知 | 依赖上述 2026 点差摘要的滑点同样不计算；不把模型比例当作独立可用费用证据 |
| commission 佣金依据 | 来回每手数值、币种、账户细分类型与适用费表未知/未获准 | 来回每手数值、币种、账户细分类型与适用费表未知/未获准 | 不采用后期 calibrated 数值，不用缺记录或接口缺省值补齐 |
| contract 合约依据 | 每手大小、计量与报价币种、来源及适用版本未知/未获准 | 每手大小、计量与报价币种、来源及适用版本未知/未获准 | specs 哈希仅绑定身份，不能代替数值与适用性依据 |
| overshoot 止损越幅 | 冻结记录为缺表，合规模型未具备 | 冻结记录为缺表，合规模型未具备 | 不采用缺表返回的默认费用；p90 规则仍是冻结情景规则，不计算情景下限或实际平均成本下界 |
| swap 隔夜费 | 仅当前 Demo 的 free 单项假定 | 仅当前 Demo 的 free 单项假定 | 依据[第十五轮报告 §1.1 与 H15-8](../reviews/2026-11-02-review-round15.md)的用户确认；不是计费实测，不外推其他账户或真实账户，不能补齐其他必需项 |
| 执行入口及合成验证 | 未完成 | 未完成 | 完整 H4 桶、S2/S3 映射与回看窗、共同资格、日聚合和无标签路径均未完成执行前置验证 |
| 合法数据与其他输入资格 | 未运行/未知 | 未运行/未知 | 本次未读取真实输入，不能声称资格已通过或失败 |

任一必需成本项未知就使受影响整行 `waiting_data`。当前两品种均有上述缺项，因此全部登记组合均无法筛查；不得删去缺成本触发、用部分分量均值或 Demo free 假定使整行通过。没有另行批准且符合 D50 的完整成本依据，不得产生可行结论。

本报告不现场拟合、换缓存、补测或重新校准，不用后期经验摘要补齐未知；也不计算“冻结情景已知分量的下限”。未来若使用该术语，只能指获准冻结情景的已知分量，不能指实际平均成本下界。

## 4. 全部 96 个预登记组合明细

每行的“实际成本及三倍成本”表示两项测量均为“未运行/未知”。全部行的成本门、样本量门均未评估；状态仅由 §3 共同缺项及 A2 §6.3 的冻结处置得到，不由触发计数或成本数值计算得到。

| 设计族 | 品种 | 局面 | 动作 | 固定方向 | 触发数 | UTC 触发日数 | 实际成本及三倍成本（R） | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | XAUUSD | S1 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S1 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S1 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S1 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S2 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S2 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S2 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S2 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S3 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S3 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S3 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | XAUUSD | S3 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S1 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S1 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S1 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S1 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S2 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S2 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S2 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S2 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S3 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S3 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S3 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F1 | EURUSD | S3 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S1 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S1 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S1 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S1 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S2 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S2 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S2 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S2 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S3 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S3 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S3 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | XAUUSD | S3 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S1 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S1 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S1 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S1 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S2 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S2 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S2 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S2 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S3 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S3 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S3 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F2 | EURUSD | S3 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S1 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S1 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S1 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S1 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S2 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S2 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S2 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S2 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S3 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S3 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S3 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | XAUUSD | S3 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S1 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S1 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S1 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S1 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S2 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S2 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S2 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S2 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S3 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S3 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S3 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F3 | EURUSD | S3 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S1 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S1 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S1 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S1 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S2 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S2 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S2 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S2 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S3 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S3 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S3 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | XAUUSD | S3 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S1 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S1 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S1 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S1 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S2 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S2 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S2 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S2 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S3 | A1 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S3 | A2 | follow>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S3 | A3 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |
| F4 | EURUSD | S3 | A4 | fade>base | 未运行/未知 | 未运行/未知 | 未运行/未知 | waiting_data |

## 5. 族/品种汇总（全部 8 项）

“登记组合数”只表示每项所含的 S1/S2/S3×A1–A4。下表没有跨动作或跨品种汇总真实计数、求成本均值，也没有评估样本量门。

| 设计族 | 品种 | 登记组合数 | 触发数 | UTC 触发日数 | 实际成本及三倍成本（R） | 成本门 | 样本量门 | 汇总状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F1 | XAUUSD | 12 | 未运行/未知 | 未运行/未知 | 未运行/未知 | 未评估 | 未评估 | waiting_data |
| F1 | EURUSD | 12 | 未运行/未知 | 未运行/未知 | 未运行/未知 | 未评估 | 未评估 | waiting_data |
| F2 | XAUUSD | 12 | 未运行/未知 | 未运行/未知 | 未运行/未知 | 未评估 | 未评估 | waiting_data |
| F2 | EURUSD | 12 | 未运行/未知 | 未运行/未知 | 未运行/未知 | 未评估 | 未评估 | waiting_data |
| F3 | XAUUSD | 12 | 未运行/未知 | 未运行/未知 | 未运行/未知 | 未评估 | 未评估 | waiting_data |
| F3 | EURUSD | 12 | 未运行/未知 | 未运行/未知 | 未运行/未知 | 未评估 | 未评估 | waiting_data |
| F4 | XAUUSD | 12 | 未运行/未知 | 未运行/未知 | 未运行/未知 | 未评估 | 未评估 | waiting_data |
| F4 | EURUSD | 12 | 未运行/未知 | 未运行/未知 | 未运行/未知 | 未评估 | 未评估 | waiting_data |

## 6. A3 待审结论与后续边界

本报告提交 A3 独立审核的结论是：**规则已批准、筛查未运行，全部登记组合和族/品种均待数据，当前没有可行证据。** 这不表明任何成本或样本量门已经通过或失败，也不证明任何机制不存在。按 T121 的冻结处置，P 线保持待数据/休眠、保留基础设施；该处置由本报告进入 A3 审核，不能据此启动 T122、P1 或 P2。

主会话将本报告与绑定预注册、独立 A2 最终报告交同一独立审核者作 A3 审核，并负责后续模块集成。本报告作者不批准 A3，不填写或修改 `approved_by`。开发者完成文档交付不等于真实运行获准。

未来重启仍限于冻结的 D60 获批扩池、新合法数据或新文献机制等条件，并须经过相应审核；如要实际筛查，还须版本化并审核符合 D50 的完整成本依据，满足合法数据、执行入口及合成验证前置，再取得明确执行放行。后期经验摘要用途的任何变更必须先独立批准。A3、P1 方法门、正式轮次与 D61 的独立要求继续有效。
