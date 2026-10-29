# T101 计划：成簇对照 w7 处置——季度块置换（block="quarter"）+ 全套重标定 v3

来源：T100 重跑（fe8bde3）发现月内置换在 7 周尺度成簇结构下误报率
11% > 5%（判定标准 2 失败，未采纳、锚点维持）。报告 v2 §6 预写的
升级处置选项之一"加长置换块长（月→季）"——本批执行该选项并全套验证。
用户 2026-10-30 指示按老流程做完所有可做任务。

硬约束不变：不改引擎/实盘/gates.toml；H 线所有权文件不动；三套受保护
探针不改（delta_p_test 缺省 block="month" 保持逐位不变——探针 03 直调
它）；重标定分片运行期间不再改被标定代码路径；并发 ≤ CPU/2=8。

## 预写判定标准（先于运行定死）

与 T100 相同的五条 + 两条新增：
1. 负对照：单假设（n ∈ {500,1000,2000}，各 200 种子）**与 12 假设
   Holm 联合（n=1000，200 种子）**误报率均 ≤ 5%（与 T100 逐字对齐）；
2. **成簇负对照 w3 与 w7 各自 ≤ 5%（本批修复目标）**；
3. Brier：无信息 ≈0 / 植入检出；
4. B=10,000、+1 修正；
5. 任一项不满足 → 不采纳、锚点不动、报告 v3 如实记录、再升级
   （下一选项：收紧 α；本批不连做）；
6. **检验力崩塌触发器**：dp=0.05 档最大格（名义 n=4000）功率 < 50%
   （v2 月内置换基线为 100%）→ 全档开区间化、P1 无可检验假设——
   视为与设计意图严重不符，按第 5 条暂停升级（功率温和下降属正常
   代价，由锚点如实吸收，不设人为下限；本条取代 T100 的"非 0.02
   档超界且 <40% 暂停"守卫——放松方向只影响"锚点本就开区间"的
   保守侧）；
7. 若全过 → **采纳**：正式方法改季度块、锚点按 T100 预写规则以
   **实际日数**单位重derive并接线。

锚点推导规则：沿用 T100 计划预写（分片 rows 取 n_days_real 中位数、
50% 功率最小实际日数、相邻格线性插值向上取整、开区间扩展随实测、
4/12 假设 z² 比、与 D52 取严）。

## 模块甲：失败测试先行 + block="quarter" 实现

- `src/mt5quant/validation/prob_mode.py` 的 `delta_p_test`：现签名已有
  `block: str = "month"`（仅回显、分组固定 freq="M"）。改为
  block ∈ {"month","quarter"} 显式校验（**入口处**、与 sims 校验并列、
  先于空标签早退分支——bogus block + 空标签不得静默返回 p=1.0）；
  分组 PeriodIndex freq 随之 "M"/"Q"——季度块=同日历季度（Q-DEC：
  1-3/4-6/7-9/10-12 月）内随机重分组，统计量/向量化/+1 修正结构零
  改动；**缺省 "month" 逐位不变**。
- 失败测试先行（tests/test_validation_prob_mode.py 增补）：
  1. block="quarter" 跨月同季数据与 block="month" 的 p 值不同
     （构造 1 月+2 月两组、季内合并改变置换空间）——现在红
     （当前 quarter 静默按月处理，二者相同）；
  2. block="bogus" → ValueError（含"空标签 + bogus"组合也抛，不静默
     返回 p=1.0）——现在红（静默接受）；
  3. 同 seed 确定性、结果含 block 字段回显；
  4. 季度和守恒（置换不跨季重分组——季内池总和不变的手工小例）。
- **桩签名机械更新**（甲落地后防 TypeError；非行为改动）：
  `tests/test_prob_calibration.py` 两处 fake（_patch_cluster_heavy 与
  test_phase_neg_pos_record_actual_day_counts 的 lambda 加 block 形参
  或 **kw）；`docs/reviews/probes/2026-10-29-p02-cluster-postfix.py`
  的 fake_delta_p 同样加（该探针是 T100 自建、**非受保护**，仅机械
  改桩签名，断言口径不动——v2 报告引用它作证据，须保持可运行）。
- `scripts/prob_calibration.py`：模块常量 `BLOCK = "quarter"`，4 处
  delta_p_test 调用传入；`_run_fingerprint` 的设计常量 JSON 加
  `"block": BLOCK`（与脚本字节哈希冗余、自文档化）；manifest 只从
  job 集推导、不含方法参数（加 BLOCK 不改 manifest，新 stamp
  `t75-formal-v3` 新目录无冲突）。

## 模块乙（与丙交替）：重跑 t75-formal-v3

- 仓库根运行；pool 重建（stamp `t75-formal-v3`）→ 5 相位 × 8 分片
  （逐相位后台任务，≤8 进程，错峰 1 秒）→ merge → summary；
- 判定标准逐条核对（预写 1–6）；锚点按预写规则推导（从分片 rows）。

## 模块丙（乙全过后）：采纳接线

- `src/mt5quant/prob/p1.py` run_p1_card：delta_p_test 调用加
  `block="quarter"`（正式 P1 与标定同方法）+ 断言测试（spy 或结果
  block 字段经 run_p1_card 透出——按现有测试风格选）；
- `candidates.py`：`_MEASURED_ANCHORS` ← v3 实际日数锚点；docstring
  改写（单位=实际触发日数、方法=季度块置换 v3 采纳、T100/T101 沿革）；
  open_ended 分段按预写规则随实测；
- `tests/test_prob_precheck.py`：硬编码值（4000/2000/1000、7424/
  3712/1856 等）按新锚点更新，六例覆盖语义不变；
- 复核 C101–C106 判定（预期：新锚点若更严则更无悬念；若放松仍远超
  计数上限——如实记录新差距，不重开轮次）。
- **若判定标准任一不满足：丙整体不做**，仅报告 v3 记录。

## 模块丁：报告 v3 与文档收尾

- 新文件 `docs/research/prob-mode-calibration-2026-10-29-v3.md`：
  方法变更声明（月内置换 → 季度块置换；理由=成簇 w7 过度拒绝的
  病因与 D42 沿革）、全数字表（与 v2 并列对照）、判定标准逐条结论、
  锚点表（1/4/12 假设）与 D52 对照、采纳/不采纳决定、对候选影响；
  状态待审核 + 按授权采纳处置句；
- v2 报告头部加取代行；candidates-P1 §4.2 附记更新（若锚点变）；
- **收尾勾选**（用户指示做完所有任务）：P 线总计划 T75 报告复选框
  （证据 v2+v3+提交号）、T77 轮次草案行（存档状态确认句）、T101
  本计划执行记录。

## 执行顺序

1. 计划审核子代理；
2. 甲（失败测试先行→实现，直接做）；
3. 乙（重跑，逐相位后台）；
4. 判定标准核对 → 通过：丙+丁并行子代理；不通过：丁（记录版）；
5. 成果审核子代理 → 全量回归 + 三探针 → 推送 → CI 绿。

## 明确不做

- 收紧 α（下一选项，本批不连做）；T73B/T91（仍阻塞）；引擎/实盘/
  gates.toml；受保护探针；审计探针 H-02 段（H 线所有权）；
- 被标定路径除 delta_p_test 的 block 参数与标定脚本 BLOCK 常量外的
  任何改动。

## 执行记录（2026-10-30）

- **甲完成**（失败测试先行 → 实现；改动在本批工作树、随本批提交）：
  - 失败测试先红后绿 4 例（`tests/test_validation_prob_mode.py`）：
    ①`test_delta_p_test_quarter_differs_from_month_on_cross_month`（同季
    跨月 1+2 月数据上 quarter 与 month 的 p 值不同——红：当前 quarter
    静默按月处理）；②`test_delta_p_test_rejects_bad_block`（block=
    "bogus" → ValueError，含"空标签+bogus"组合不静默返回 p=1.0）；
    ③`test_delta_p_test_quarter_deterministic_and_echoed`（同 seed
    确定、block 字段回显、缺省回显 "month"）；④计划项"季度和守恒"以
    `test_delta_p_test_quarter_equals_month_within_single_months` 等价
    表达（互不同月/互不同季时季度块与月块结果除 block 字段外逐位相同
    ——即季度只合并季内月份、从不跨季重分组）；
  - `delta_p_test` 入口显式校验 block ∈ {"month","quarter"}（先于空
    标签早退）、分组 freq "M"/"Q"（Q-DEC 日历季），统计量/向量化/+1
    修正零改动、缺省 "month" 逐位不变；
  - 桩签名机械更新三处：`tests/test_prob_calibration.py` 两处 fake
    （_patch_cluster_heavy 与 test_phase_neg_pos_record_actual_day_
    counts 的 lambda 加 block=None 形参）+
    `docs/reviews/probes/2026-10-29-p02-cluster-postfix.py` 的
    fake_delta_p（断言口径不动）；
  - `scripts/prob_calibration.py`：`BLOCK="quarter"`、4 处调用传入、
    指纹设计常量加 `"block"`（manifest 不含方法参数，不冲突）。
- **试点（过程记录，未入正式数据）**：20 种子 w7 档误报 25%——仅用于
  确认可运行并决定是否值得跑全量，不参与判定。
- **乙完成**：`t75-formal-v3` 全量重跑（B=10,000；manifest 2,400 job
  完备且唯一，与分片 rows 逐键核对无缺/重/多余；指纹
  `2461281476b0…`、40 分片单一指纹；池与 v2 同源，池源指纹
  `994a6635…`）。运行次数：标定 5 相位 × 8 分片 = **40 进程**（并发
  ≤8、逐相位后台、错峰 1 秒）。
- **判定：标准 2 失败——cluster w7 = 8%（8/100）> 5%**（w3=4% 过；
  标准 1/3/4/6 全过：neg 单假设 3.0%/2.5%/0.5%、Holm(12,n=1000)
  0.5%、brier 0%/100%、检验力崩塌触发器 dp0.05@n4000 功率 100% 未
  触发；功率较 v2 温和下降：dp0.03@n2000 51% vs 53%、dp0.02@n4000
  28% vs 34%）。
- **按预写规则 5：不采纳**——正式方法维持月内置换、锚点不动
  （`_MEASURED_ANCHORS` 名义口径）、下一选项（收紧 α）留下一批，
  本批不连做不试错。
- **丙：整体未做**（判定未过、无任何接线——`run_p1_card`/
  `_MEASURED_ANCHORS`/`tests/test_prob_precheck.py` 硬编码值均未动，
  candidates-P1 §4.2 无需附记）。
- **丁：本批**——`docs/research/prob-mode-calibration-2026-10-29-v3.md`
  （失败记录版，全部数字从 v3/v2 分片 JSON 重算复核）+ v2 报告头部 v3
  注记 + P 线总计划 T75 报告勾选与 T77 存档确认句。
