# 研究门禁（ResearchGate）与实验指纹（T107 模块 A）

状态：现行（2026-11-01）。来源：[Issue #4](https://github.com/wmqfl861/MT5-trading-plus/issues/4)
第 3 项 + 计划 `docs/superpowers/plans/2026-11-01-t107-p0-gates-p210.md` 模块 A；
实现 `src/mt5quant/research/gate.py`，测试 `tests/test_research_gate.py`。

## 四态与优先序

`research_gate(*, method_id, days=None, dp=None, n_hyp=None,
calibration_ok=True, data_root=None, split=None, counted=None)
-> GateResult`，状态 ∈ {PASS, FAIL, INSUFFICIENT_DATA, NOT_RUN}，
按下列顺序**首个命中**（`GATE_STATES` 只是枚举，优先序以本节为准）：

1. **NOT_RUN**：method_id 未登记于 `METHOD_REGISTRY`
   （`src/mt5quant/validation/prob_mode.py`），或登记状态 = `calibrating`。
   登记缺失按未运行处理（fail-closed：登记缺失不是放行理由）。
   calibrating 且登记在册 → NOT_RUN **优先于** FAIL（方法标定中不是
   "失败"，是"还没跑"）。
2. **FAIL**：登记 `adopted=False`（含 `failed_cluster_control` /
   `not_adopted_*` 全部形态），**或**调用方显式传入
   `calibration_ok=False`。**校准失败必须阻断：不存在任何参数组合
   （dp/days/n_hyp）能从 FAIL 到 PASS**——这是 FAIL 与
   INSUFFICIENT_DATA 的本质区别：后者补数据可达 PASS，前者只有方法
   重新完整标定、成簇负对照通过并在 METHOD_REGISTRY 登记
   `adopted=True`（批准动作另批，状态只增改不删行）。
3. **INSUFFICIENT_DATA**：方法 `adopted=True` 但样本量证据不足——
   证据 ∈ {lower_bound_only, uncalibrated}（P2-08 三分），或 days <
   该 dp 的充分门槛。**门槛来源 =
   `candidates.sample_size_evidence` 的同一判定（max(解析 D52,
   T75 实测)），gate 不另立数字**。dp（期望效应，来自候选登记
   `expected_delta_p`）或 days（触发日数，来自计数工具）给不出时
   同样按本态处理，理由注明缺了什么——不猜、不默认达标。
4. **PASS**：`adopted=True` 且证据 = calibrated_sufficient 且 days
   达标。**当前仓库三方法全部 `adopted=False`，任何输入都到不了
   PASS**——这是诚实状态（测试钉死：`test_current_registry_
   never_pass_sweep`）。

## 脚本成功 ≠ 研究通过

CLI 退出码 0 只表示"脚本跑完了"，不构成 PASS。P1 报告是否可作为推进
依据以 ResearchGate 为准：方法门（P2-09）标注"本报告不构成推进依据"
时，报告照常生成（诊断价值保留），但结论列一律"不推进（方法阻塞）"。
round-report 的接线输出一行四态（见下节），它与方法门标注并列、不替代
彼此；两者都只是**读侧**状态呈现。

## 执行点分布（gate 是读侧单点）

阻断动作不在 gate 里，gate 只是把分散的判定收敛为单一状态读出：

- **开轮 D52 冻结**（`bt round open` → `_prob_family_freeze`）：
  样本量证据不足的假设机械移出冻结 Holm 族、族空拒绝开轮——写入侧
  阻断；
- **round-report 强制不推进**（P2-09，`bt prob round-report`）：方法
  未批准 → 结论一律"不推进（方法阻塞）"，exit code 不变——读侧强制；
- **ResearchGate**（本模块）：以上两处的判定依据（METHOD_REGISTRY +
  sample_size_evidence）统一读出为四态；round-report 接线处的输入为
  `method_id=PRIMARY_METHOD`、`calibration_ok` 取登记（adopted=False
  即 FAIL）、样本量判定走 sample_size_evidence 同一数字。

## 阈值反调禁令与快照

**按候选结果反调验收阈值 = 违规。** `ACCEPTANCE_CONSTANTS` 是只读快照：

- `constants`：现行值**活读**自决策层——lb_z/threshold_mult 来自
  `level_decision.DecisionPolicy` 缺省（1.0 / 2.0），min_cell /
  prior_strength 来自 `LevelDecisionV1` 类常量（100 / 100，即
  `ProbTable.fit` 的实际入参）；
- `expected`：登记值（T107 冻结）；
- `sources`：源文件（level_decision.py、tables.py）的 sha256；
- `drift`：活读值 ≠ 登记值的键（空 = 未漂移）。

GateResult 恒携带该快照；消费方（报告/审核）应断言 `drift == []` 且
源文件哈希与快照一致——**快照漂移即阻断**。任何"为了让本轮候选通过
而调 lb_z/threshold_mult/min_cell/prior_strength"的动作都会先表现为
快照漂移；阈值变更只能走独立提案轮次，不与使用它的研究同批。

## 失败实验保留策略

失败实验（FAIL / INSUFFICIENT_DATA / 作废 / 异常）的记录**只追加不
删除**：台账 runs 行（含 error 行）不回收、轮次报告不重写（同假设多条
成功运行 → 拒出报告而非挑一条）、METHOD_REGISTRY 状态只增改不删行
（历史判定可复核）、研究脚本的结果目录只追加。失败样本是方法学与
标定的输入（T75 系列标定正是靠保留的失败对照），删失败记录=销毁证据。

## run_id / 指纹约定

`experiment_fingerprint(*, strategy_ref, card, model_dict, bars_meta,
cost_version, sessions_sha, params, features, evaluator_sha=None)
-> {"run_id", "sha256", "components"}`——六维显式组件，任何一维变化 →
sha256 变：

| 维 | 内容 | 缺失处理 |
| --- | --- | --- |
| code | 策略引用 + 卡号 + 闭包文件集（`cards.strategy_files` 逐文件 sha256） | 引用/卡号缺失 → ValueError；闭包不可得 → files 记 None 并注明 |
| data | bars_meta（generation / content_sha256 / 根数等，调用方按 manifest 传） | 缺失/空 → ValueError |
| params | 运行参数 params + 冻结模型 model_dict | 缺失/空 → ValueError |
| features | 排序后特征名列表（顺序无关，集合敏感） | None/空 → ValueError |
| cost | cost_version + sessions_sha | 任一缺失 → ValueError |
| evaluator | evaluator_sha（复用 `bt.py _p1_evaluator_sha` 口径） | None → 显式标注"评估代码未绑定"，不冒充覆盖 |

- `sha256` = components 规范化序列化（键序无关）的 sha256；
- `run_id` = sha256 前 16 位 hex——报告与结果目录的短标识；同输入幂等，
  输入变化即新身份（换评估实现 = 新实验，X-05 同一思想）；
- 必填组件缺失 → ValueError，不静默空串（P2-07 教训）。

## 相关

- Issue #4（P2-10 项）；二审报告 `docs/reviews/2026-10-30-global-code-audit-2.md`
  §4.3/4.4（P2-09/P2-10）；
- 方法登记与批准语义：`src/mt5quant/validation/prob_mode.py` 的
  METHOD_REGISTRY / PRIMARY_METHOD / primary_method_approved；
- 样本量证据三分：`src/mt5quant/research/candidates.py` 的
  sample_size_evidence（P2-08）；标定报告
  `docs/research/prob-mode-calibration-2026-10-29*.md`；
- P2-10 统计覆盖研究（模块 B）沿用本指纹与四态口径。
