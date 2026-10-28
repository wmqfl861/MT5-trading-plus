# P 线下一批工作计划（2026-10-29：T76 收尾 + T77 收口）——修订版 v2

**修订记录**：v2 按独立计划审核（2026-10-29）五条必改修订——假设数维度插值定为
z² 换算；开区间格语义统一；边界外推定沿段斜率 + 非负截断；夹具/签名保护约束点名；
计划勾选归主会话、模块 B 不预写结论。

**授权记录**：用户 2026-10-29 授权——"需要审核批准的按你推荐的做"。据此，T75 标定报告
（`docs/research/prob-mode-calibration-2026-10-29.md`，提交 `e7c2dd5`）按执行侧推荐采纳：
月内置换方法 + 实测样本量规则（实测严于 D52 解析，以实测为准）。T76 收尾据此接线。
轮次文件的 `approved_by` 仍只由 P 线审核会话填写，本批不涉及。

**流程**（用户指定）：计划 → 独立子代理审计划（已完成，审核意见已并入本文）→
并行开发 → 独立子代理审成果 → 全量回归 → 提交推送 → CI 绿 → 勾选（勾选只由主会话做）。

## 模块 A：T76 收尾——接入实测样本量规则（代码 + 测试）

**文件**：`src/mt5quant/research/candidates.py`、`scripts/bt.py`（打印同步）、
`tests/test_prob_precheck.py`（新增/更新用例）。**保护约束（探针依赖，审核点名）**：
不改 `tests/test_prob_precheck.py` 里 `prob_data`/`counted_df` 的模块级定义与
`precheck_prob` 的签名（探针 `docs/reviews/probes/2026-10-28-pline-probes.py` 直接
import 并调用）。

### 实测规则（精确定义）

1. **单假设实测锚点**（T75 报告 §2.5/§3，基准赢率 0.34 口径）：
   - `dp < 0.02`：`None`——低于标定档（n=4000 检验力仅 34%），**恒 waiting_data**；
   - `dp = 0.02`：下界 4000、`open_ended=True`（判 pass 需 days > n_required）——与 T75 报告 ">4,000" 开区间格一致（实现取舍记录：计划 v1 此处曾误写 dp≤0.02→None，与测试规格矛盾，按测试规格/报告格修正）；
   - `dp ∈ (0.02, 0.03]`：下界 4000@0.02 与 2000@0.03 **线性插值**，`open_ended=True`
     （0.02 端继承开区间语义）；
   - `dp ∈ (0.03, 0.05]`：2000@0.03 与 1000@0.05 线性插值；
   - `dp > 0.05`：沿 0.03→0.05 段斜率**外推**（非 clamp），`max(值, 0)` 截断；
     dp 大到实测外推低于解析时由第 3 条 `max` 兜底（dp≳0.07 后解析主导，如实注明）。
2. **假设数维度**（任意 n_hyp ∈ [1, +∞)，审核推荐方案）：
   `n_required_measured(n_hyp, dp) = 单假设值(dp) × (z_α(n_hyp)/z_α(1))²`
   （z 取 `power._z_alpha(0.05, n_hyp)`；即 T75 报告推导 4/12 列的同一公式，
   (z₄/z₁)²≈1.856、(z₁₂/z₁)²≈2.572）。单假设 `None` 或 `open_ended` 原样传播
   （None → 恒 waiting；open_ended → 判 pass 需 `days > n_required`）。
   与报告取整表（3700/5100 等）相差 ≤3%：**实现按推导式而非取整表**，常量注释写明。
3. **取严**：`n_required = max(解析 D52(n_hyp, dp), 实测(n_hyp, dp))`；实测为 None 时
   该假设不可过（waiting_data）。`checks["power_prob"]` 增加：
   `n_required_measured`（None 时如实 None）、`n_required_used`、`open_ended`（bool）、
   `rule_source: "t75-measured+z2"`；`t75_rule_pending` 改 **False**；既有键
   （`n_required_analytic`/`per_hypothesis`/`unit`/`insufficient`/`passing_count`）保留。
4. **bt.py 打印同步**：逐假设显示解析/实测两口径与取严结果；删"批准前按解析表"措辞。
   `candidates.py` 的 docstring 与 note 中"接线待 T75"类文字同步更新。
5. **失败测试先行**：
   - 实测更严时以实测为准：4 假设 Δp=0.03 → 解析 1340 vs 实测 2000×1.856=3712 → required 3712；
   - 解析更严（外推区）：4 假设 Δp=0.10 → 实测外推≈500，解析 301 左右……（构造使解析>实测
     的档位，如 Δp=0.20：实测外推为 0 截断，解析正数 → 取解析；计划 v1 括注 "Δp=0.10→实测外推≈500" 系笔误，该值在 0.06 处，0.10 处外推为负→0 截断）；
   - 开区间：4 假设 Δp=0.02 → 4000×1.856=7424、open_ended=True，`days=7424` 不算过
     （须 `>`），`days=7425` 过；
   - dp≤0.02 单假设列 None → 恒 waiting（4 假设 Δp=0.015 传播 None）；
   - **单调性性质测试**：n_required 对 Δp 非增（同 n_hyp）、对 n_hyp 非减（同 Δp），
     扫描覆盖外推方向错误；
   - `t75_rule_pending is False`；
   - **既有用例更新（决定翻转，非纯数值）**：`test_precheck_prob_pass`（1 假设 Δp=0.04：
     实测 1500 > 解析 406 → required 1500）夹具天数 500→1600；`test_q6_days_unit_and_
     per_hypothesis`（2 假设 Δp=0.04：实测 1500×1.420=2130）A3 天数 650→2200；
     L175 的 `is True` 改 `is False`。

## 模块 B：T77 收口——文档骨架（不跑 bt、不预写结论）

**文件**：仅 `docs/research/candidates-P1-2026-10-29.md`。**计划文件与勾选归主会话，B 不动。**

1. §5 补齐：实测样本量规则对照表（引 T75 报告 §3：实测/推导值 vs D52 解析逐格、
   "以更严者为准"结论；注明 4/12 假设推导口径）。
2. §6 新增"轮次决定（执行侧推荐，待 §4 运行数据确认）"：推荐**不提交 R2 轮次草案**——
   若预筛确认无可运行假设，开轮无意义，按铁律 18 停在候选阶段；写明解锁路径（放宽
   approach 的新版本候选、EURUSD 等 HistData 验证后加入、外部数据延长，均属新版本/
   新轮次登记，不改本批预期）。**不预写"全部 waiting_data"**——事前成本条件独立判定，
   可能 fail；§4 结论等主会话跑 `bt candidate check` 后照实填。

## 并行结构

- 子代理 A：模块 A（candidates.py + bt.py + tests/test_prob_precheck.py）。
- 子代理 B：模块 B（candidates-P1 报告 §5/§6）。
- 两模块文件不相交。主会话在 A 合入后：跑 `bt candidate check C101/C102/C103`
  （--data-root data_research；台账经共享 junction 入账）→ 照实填 §4 → 成果审核子代理
  复核全部产物。

## 验收

- 模块 A 测试 + 全量回归绿；探针 02/03/04 仍全 PASS（A 完成后主会话复跑）；
- `bt candidate check` 三候选结论落盘（precheck.json + 台账哈希链）；
- 候选报告 §4/§5/§6 齐备；计划勾选（T76 收尾行 + T77 相应行注明轮次决定）由主会话完成；
- CI 绿（提交号 + run_number 写进证据行）。
