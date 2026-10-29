# T100 计划：T75 标定重跑（修正版代码）与实测样本量规则重derive

来源：codex 全局审核 P-02/P-03/P-12 修正（T98 波次 1，b0bb06a）改动了
标定方法与记录口径——旧 `t75-formal` 运行（2026-10-29 采纳的
`docs/research/prob-mode-calibration-2026-10-29.md`）的数值需按修正后
代码重derive。`measured_n_required` 的 P-03 注记已写明"重标定属下一批
批准点"；本批即该批准点（用户已授权按执行侧推荐）。

硬约束不变：不改引擎/实盘/gates.toml；不动 H 线所有权文件；三套受保护
探针不改；**标定分片运行期间不改被标定代码路径**（prob_mode.py/
labels.py/p1.py/prob_calibration.py——本批丙模块只改消费方
candidates.py，不违反）；并发进程数 ≤ CPU 核数一半。

## 预写判定标准与锚点推导规则（先于运行定死，防事后挑选）

判定标准（与旧报告 §0 相同 + P-02 新成簇设计）：
1. 负对照单假设（n ∈ {500,1000,2000} 名义，各 200 种子）与 12 假设
   Holm 联合（n=1000，200 种子）误报率均 ≤ 5%；
2. 成簇负对照（P-02 新设计：real 与 null **均**独立整周平移 wk∈
   {3,7}+0..51 周宽范围、NY 本地小时保持、越界丢弃，null 非 real 平移
   时刻的子集）：**wk=3 与 wk=7 各自**误报率 ≤ 5%（与 merge 按 wk
   分组口径对齐，不留 pooled 事后解释空间）；
3. Brier：无信息技能分>0 比例≈0、植入信息检出；
4. B=10,000、p 值 +1 修正；
5. **任一项不满足 → 不更新锚点/不采纳**，报告如实记录并暂停（升级
   处置属下一批），绝不放宽。

锚点推导规则（单位=**实际触发日数**，P-03 口径修正的核心）：
- **取数来源**：每格（Δp∈{0.02,0.03,0.05} × 名义 n∈{500,1000,2000,
  4000}，100 种子）的 power 与 `n_days_real` 中位数从**分片 JSON**
  （`pos-s*of8.json` 的 rows）取——summary.json 的 pos 段只有
  power/dp_hat_median/seeds，没有 n_days；merge 属被标定保护路径，
  **不为汇总扩 merge**；
- 每个 Δp 求功率过 50% 的最小实际日数：相邻格跨越 50% → 实际日数轴
  线性插值向上取整；最大格仍 <50% → 锚点=该格实际日数中位数、语义
  **严格大于**（开区间）；最小格已 ≥50% → 取该格并如实注明外插风险；
- **open_ended 处置（预写）**：开区间语义随实测锚点扩展——若 0.03
  或 0.05 档的最大格仍 <50%，丙模块同步把 measured_n_required 的
  open_ended 分段改为"锚点开区间档集合"并如实记录（分段逻辑跟随
  实测、非事后挑选）；但**任一非 0.02 档锚点超出测试范围（> 最大格
  实际日数中位数）且功率远低于 50%（<40%）→ 与旧运行严重不一致，
  触发判定标准第 5 条暂停升级**（不改代码）；
- Δp<0.02 未测出 → None（恒 waiting_data，不变）；
- 4/12 假设口径 = 单假设锚点 × z² 比（(z₄/z₁)²、(z₁₂/z₁)²，不变）；
- 新锚点与解析 D52 取严（precheck 现行逻辑，不变）。

## 模块甲（计算主线）：重跑 t75-formal-v2

- stamp=`t75-formal-v2`（旧 `t75-formal` 原样保留作历史证据）；
- **仓库根运行**（DATA_ROOT="data_research" 相对 CWD 解析）；
- `pool` 重建（N_POOL=4200、POOL_SEED 不变；pool.meta.json 按 P-12
  保留，pool.parquet merge 后删）——**所有 run（含 cluster）必须在
  pool 完成后启动**（run 指纹绑定 pool 源指纹，cluster 先跑会因
  pool.meta.json 不存在得到空指纹、merge 时指纹不一致被拒）；
- 5 相位 × 8 分片并发（≤CPU/2=8 进程；一相位 8 分片同跑恰满额；
  manifest.json 并发首写在 Windows 有小概率文件锁冲突——先启
  shard0 或错峰 ~1 秒）→ `merge --stamp t75-formal-v2` → 新
  summary.json；
- 产物：`data/prob/t75-calibration/t75-formal-v2/summary.json` +
  分片 JSON + manifest + pool.meta.json（data/ 整目录在 .gitignore，
  不进 git）。

## 模块乙（与甲并行）：P-02 修正后行为探针

- 新文件 `docs/reviews/probes/2026-10-29-p02-cluster-postfix.py`：
  合成成簇 bars + 捕获式标签器验证 phase_cluster 修正后行为——
  real 与 null 的标签时刻**均无**原始触发时刻；null 时刻 ≠ real 平移
  时刻的子集（独立平移）；平移为整周粒度（NY 本地小时保持）；越界
  丢弃不末端堆积。任一不符退出码 1。
- 实现要点：stub 的 `delta_p_test` 必须返回含 n_days_real/n_days_null
  的 dict；`_costs_and_sizes` stub 须接受 swap_ref_price kwarg；短
  合成 bars 下 patch `CLUSTER_EXTRA_WEEKS_MAX`（或造 ≥54 周 bars），
  否则平移全越界只剩空帧路径。
- **不改** codex 审计探针 `2026-10-29-global-audit.py`——它在其
  H-02 段（lockbox，行 ~100）因 H 线修复而崩溃，属 H 线会话处理；
  其 P 段（cluster）的旧行为断言由本新探针接替覆盖（该探即使到达
  cluster 段也会因 stub 缺新 kwarg/n_days 键而崩——接替必要）。

## 模块丙（甲出数后）：锚点接线更新

- `candidates.py`：`_MEASURED_ANCHORS` 按新推导更新；`measured_n_required`
  docstring 去掉"需重审"注记、锚点单位改"实际触发日数"（预筛消费方
  逐日计数与锚点同单位——P-03 的目的）；
- `tests/test_prob_precheck.py`：test_measured_rule_* 与 7424 等硬编码
  断言按新锚点更新（保持六例覆盖语义：实测更严/解析更严/开区间严格
  大于/None/单调）；
- 复核现有 C101–C106 的判定不翻转（计数 25–1333 日 vs 新锚点，预期
  仍全部 waiting_data/screened_fail——差距 5–300×；如实记录新差距）。

## 模块丁（甲出数后，可与丙并行）：报告 v2

- 新文件 `docs/research/prob-mode-calibration-2026-10-29-v2.md`：
  判定标准（含 P-02/P-03/P-12 修正声明——成簇对照重设计、抽样单位改
  实际日数、swap_ref_price 与正式 P1 同口径、manifest 完备性）、全部
  新数字表、新锚点表（1/4/12 假设 × 3 档 Δp）与 D52 对照、对现有候选
  影响、已知限制（沿用池只含 XAUUSD 等）；状态"待审核"，随后按授权
  采纳并注明；
- 旧报告文件头加一行"已被 v2 取代（P-03 单位口径），见 …-v2.md"；
- `docs/research/candidates-P1-2026-10-29.md` §3 样本量行加 v2 附记。

## 执行顺序

1. 计划审核子代理；
2. 甲（后台分片）+ 乙（并行）；
3. 甲出数 → 判定标准核对 → 丙 + 丁（并行子代理）；
4. 成果审核子代理 → 全量回归 + 三探针（R5 预期 FAIL 不变）→ 推送 →
   CI 绿。

## 明确不做

- T73B（等 D34 冻结）；T91/T77 新轮次（数据不足结论不因锚点微调
  改变——丙模块只复核不重开）；审计探针的 H-02 段（H 线所有权）；
- 被标定代码路径（prob_mode/labels/p1/prob_calibration）的任何改动
  ——若重跑暴露其缺陷，停下报告，不顺手修（避免指纹失效后再跑）。

## 执行记录（2026-10-29）

- 模块甲：`t75-formal-v2` 重跑完成（B=10,000、pool N=4200/POOL_SEED
  不变、manifest 完备集 2,400 job 校验通过、指纹 `b9f423fe…`）。非
  成簇相位与旧运行**逐值一致**（逐种子逐比特复核）；成簇负对照
  （P-02 新设计）**判定标准 2 失败：w7 档 FPR=11% > 5%**（w3 档 4%
  通过）；
- 处置：按预写判定标准第 5 条**不采纳、锚点不更新、如实记录**；
  信息性实际日数锚点推导（dp0.02 >1,732 开区间 / dp0.03 ≈1,201 /
  dp0.05 ≈512）标注"未采纳"；升级处置选项移交下一批；
- 模块丙：因不采纳改为 **docstring 记录**（`candidates.py`
  `measured_n_required` 注记 P-03 单位口径与 v2 复核结论，
  `_MEASURED_ANCHORS` 不变；已由主会话完成）；
- 模块丁：报告 v2 =
  `docs/research/prob-mode-calibration-2026-10-29-v2.md`（含判定、
  复现表、未采纳推导、影响分析与升级选项）；
- 模块乙：探针完成
  `docs/reviews/probes/2026-10-29-p02-cluster-postfix.py` **5/5 断言
  全 PASS**（含"两组均无原始触发时刻"的反向自证、整周平移 NY 小时
  保持、越界丢弃不堆积）。
