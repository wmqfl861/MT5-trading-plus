# D61 V2-A 隔离计算核实施范围裁定（2026-11-05）

本记录由主会话转录未参与方案编写或实现的独立范围审核者直接返回的结论。审核基点为 `c0df5a1d133eadbe8a8c089fba0758ed840d830b`，依据为已归档的[验证设计](../research/d61-estimator-v2-validation-design-2026-11-03.md)及[前次设计审核](2026-11-04-d61-design-review.md)。

**裁定：conditional。** 允许先行实现 V2-A 纯内存计算核与固定人工样例，无需等待 T121 实际可行族或真实产品支持集。以下三项须写入实施规格，随后即可开发和运行限定的人工验收；不增加研究审批层。本裁定不批准完整统计矩阵、V2-B、产品替换、真实输入或 Demo 运行。

## 实施前固定条件

1. **支持下端能够精确返回。** 输入整数和有限 binary64 按精确有理值解释。额外要求全局 `a=min(a_o)` 可精确表示为有限 binary64：`f` 有限且 `Fraction.from_float(f)==Fraction(a)`，否则 `ValueError`，检查位于空输入等提前返回之前。`±(2**53+1)` 作全局下端应拒绝，`2**100` 可精确表示不能仅按大小拒绝。其他端点不必全部增加该限制。
2. **日聚合、缺失与空值语义。** 每层先选择对应行，再按全局 `day_id` 聚合；动作池仅含查询动作、跨局面池化，不能跨动作或先平均父格。相同 `(situation,action,day_id)` 特征冲突拒绝。目标 W 用 a 填每条缺失计划行，以每日全部计划行数作分母；中心 Y/Z 以每日全部可见行数作共同分母，完全缺失日不进入中心资格日数。计划日来自显式行，不补 ID 空洞。`D_c=0` 时 `lower=guard=float(a)`、`meanW=radius=None`，池非空可保留中心诊断；`D_c>0` 时即使无中心也计算目标半径和 guard，仅将 lower 取 a。条件收益是否未知按实际公式分母判断，零分母返回 None、贡献为0；局部缺类但池有该类时可得到已知收缩条件均值。复制不变性限完整全局日的所有相关行等倍复制。
3. **固定私有 Decimal 上下文及保守转换。** 显式上下文 `prec=80,Emin=-999999,Emax=999999,clamp=0`，不继承调用方精度、舍入或 traps。允许正常 Inexact/Rounded；数值异常、溢出和非有限结果转为 ValueError。ln/sqrt 采用正确 HALF_EVEN 舍入，Fraction 转 Decimal 的除法采用80位 ROUND_CEILING，固定链为：

```text
ell_up = next_plus(ln(Decimal(900)))
q = Fraction(ell_up)/(2*D)
t = ROUND_CEILING_80(Decimal(q.numerator)/Decimal(q.denominator))
s_up = next_plus(sqrt(t))
h_up = Fraction(s_up)*(b-a)
```

分子、分母构造 Decimal 必须精确，所有 next_plus 显式使用同一80位上下文。精确 Fraction 计算后续上下界，诊断 float 不参与计算。lower/guard 同向下舍入，radius 向上舍入；比较候选 float 的精确 Fraction，必要时指定方向 nextafter 一次并重验有限性。由上包围和 `mu-abs(mu-w)<=w` 保持理想公式的保守方向及 `a<=lower<=guard`。

## 实现后固定验收

- 池化手算：同动作，日0有 S1 一条 win/4、S2 两条 loss/-2，日1有 S1 一条 win/2。动作池2日、中心1；`q_win=2/3,q_loss=1/3,v_win=5/3,v_loss=-2/3`。不能得到行均值1/2或父格日均值4/3。
- 缺失手算：a=-2，目标日0有 win/4和一条缺失，日1全缺失，`D_c=2`、可见日数1、`meanW=-1/2`。另验99/100可见日两层回退、目标空池非空、全缺失、零分母 None。
- 固定支持 `win:[0,4],loss:[-2,0],time:[-1,1]`，f0/f1各800日、全胜1.90，下界≥0.10；无计划样本na返回-2。负控制-1采用合法loss结局。完整日复制1/3/10、排列及共享池污染检查规定不变量。
- 独立 oracle 须有有理余项界或明确误差界，仅比较两种 Decimal 精度不足以证明。检验半径上包围、最终下界不高于理想式和guard；覆盖裁剪、零日、端点拒绝、次正规数、溢出、调用方Decimal上下文改变后结果不变。
- 保留输入拒绝、输入不变及冷导入隔离。作者提供固定版本 RED/GREEN、命令、退出码；未参与实现者审核新代码。

## 审核依据与边界

独立审核者读取公开设计和请求，并参考 [Python Decimal 文档](https://docs.python.org/3.12/library/decimal.html#decimal.Decimal.ln)及[Decimal平方根规范](https://speleotrove.com/decimal/daops.html#refsqrt)。审核者以人工常量进行纯算术核对：使用 `ln(900)=8ln(2)+2ln(15/8)` 的正项有理级数及余项上界，配合精确平方比较验证半径方向。宽度6、D=800时，半径向上输出0.39122101448515906，恒定1.90的下界1.5087789855148408。该检查未运行估计器、研究抽样或测试套件。

完整 D61 研究协议、预算与开轮许可仍另行处理；T122 依赖 T121 实际可行族，旧 P2-10 RED 和 D61/T92 阻断保持。审核者未修改文件、HEAD或index，未读取真实输入或运行环境，未填写 `approved_by` 或派代理。本次条件写入[实施计划](../superpowers/plans/2026-11-05-d61-v2a-kernel.md)，不代表实现已经通过。
