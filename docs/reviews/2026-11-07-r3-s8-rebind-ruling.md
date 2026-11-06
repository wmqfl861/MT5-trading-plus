# S8 A1 级裁定：R3 主检验（C005 Pre-FOMC 漂移）fix2 重绑链——2026-11-07

主会话转录 A1 独立裁定者结论。裁定者未参与本链任何实现/评审/装配；全程只读
（主仓 HEAD 9374283 直读、研究库台账只读 SQL、台账副本 verify_chain、全部
sha/逐叶 diff/逐年计数由裁定者独立重算，不采信实现方脚本断言输出）；未填
approved_by、未派子代理、未跑 gate、未修改任何被审文件（本记录除外）。

**总裁定：APPROVED（附授权边界与撤销条件）。** 四事项：E1 重绑有效
（rowid27 为 R3 主检验有效登记预筛）；trial_budget 按机械口径 0/2 未耗、
授权 run3 执行（run3 记 1/2）；E3 FRED USTEC/D1 放行为 R3 研究运行输入
（限本绑定工件与本轮次）；F1 处置（fresh 对账域=绑定快照域）确认。
发现两处留档不一致（均不翻裁定，见 §F），其中 run2 命令留档 sha 少一字符
——run3 授权以 run1 command.txt 原文为准。

## 0. 独立核验基础（不信实现方断言，逐项抽验结果）

- **台账三行直查**（研究库 `data/research/ledger.sqlite`，mode=ro）：
  C005 恰 rowid 25/26/27 三行（pass/pass/pass）。supersedes 链
  25(`1a450369…`)←26(`cd538fbb…`)←27(`94af2dfb…`)，与各行
  `precheck_source` 自述一致；26↔27 `checks` **深度相等**、25→26 恰
  3 叶（`power.coverage_segments/coverage_merged_years/coverage_merge_method`）；
  登记 payload 26↔27 差异恰 7 叶、全在 `precheck_source`（fix1→fix2 叙述、
  result_json_path、result_payload_sha256、stable_key、supersedes）与
  `reasons[1]`。rowid27 存储 JSON 与盘上 `registered-payload.json` 相等。
- **sha 现场重算**：rowid25/26/27 登记 payload sha 分别等于盘上
  `2026-11-06-n1-precheck`、fix1 `real-run/result.json`、
  s5-s7 `real-run/result.json` 的实测 sha256（逐位命中）。S6 确定性：
  `calendar-reexport/result.json` == 正式跑 == `94af2dfb…`。
- **研究库工件重算**：`ext/fred/USTEC/D1/manifest.json` sha256
  `6f068426077c2c935ba1bb1d43946144db966fa47708d64e1d3007f39b3469d6`；
  `data.parquet` sha256 `c6645256…`（==N1_WHITELIST 原钉，未变）；
  manifest rows/first/last/research_cutoff（10033/1986-01-02/2025-10-23/
  2025-10-24）与 parquet 自洽；`yearly_verification` 41 条、键 1986..2026、
  **全部 used=true**、零 used=false；`source_files[0].sha256`=`c3d915d7…`
  （绑定原件未动）；`usage_restriction`/`terms_note`/`converter_version=1`
  原样保留；`label_rule` 如设计文案。
- **A1 四钉**（rowid27 载荷 `precheck_source`）：approval（本链上游 A1 记录
  路径）、spec（预筛规格说明路径）、`calendar_sha256`=
  `a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e88c1e76ec971c5`、
  `candidate_sha256_bound`=
  `d00adad5f6303b557a97300e2c64c6737be8f2a3d07a72ca7206f57af5040bfa`
  ——逐字在场；且 full result.json `hashes.artifacts` 六钉（另含 costs
  `c35550b9…`、specs `144bb224…`）经裁定者对研究库实物逐文件重算**全部
  命中**；`binding.files` 与 `hashes.artifacts` 公共键逐值相等。
- **S6 载荷 diff 独立复算**：rowid26↔rowid27 全载荷逐叶 diff=43 叶，折叠
  后恰落入声称 8 组（binding.files manifest sha、hashes.artifacts 同键、
  used_gate.basis、used_gate.years 36 叶、year_handling.basis、
  year_handling.note、gap_rules.explicit_registration、hashes.code 装配器），
  无意外组无缺失组；`checks`/`provenance.calendar`/`hashes.rules`/
  selection/coverage/coverage_event_era/costs/missing_intervals/counts/
  coverage_declared/eligibility/status 全部深度相等——σ/功率/成本/计数
  **零漂移**主张成立。used_gate touched years 1986..2021 共 36 条在
  used_gate 记录与绑定 manifest 中**双侧** used=true。
- **fresh 双 sha 证据**（s2-s4 目录）：fresh 全量 CSV sha
  `029f8125…`（10655 数据行）→ 修剪到绑定快照域
  [1986-01-02, 2026-10-26] 后 sha `c3d915d7…`，与绑定原件 NASDAQ100.csv
  **逐字节相同**（裁定者三文件实测）；域外排除 8 行（2026-10-27..2026-11-05）
  计数留档、域下 0 行。正式证据 JSON：41/41 used=true、V2 roundtrip 全真、
  V4 value_diffs=0/date_diffs=0 逐年全零、cross_source=声明不可用；基线跑
  （未注入 fresh）exit=1——"无 V4 证据即 used=false"的 fail-closed 有实跑
  证明。主库侧：主 manifest 现值 `d83a167a…`、主 parquet `651a67a7…`
  （10285 根至 2026-10-26）、raw `c3d915d7…`；**41 条逐年 rows 与主库
  parquet 逐年计数逐一相等**（裁定者复核，41/41）。
- **台账全局**：rounds R1 closed / R3 open（trial_budget=2、
  lockbox_uses_max=1、approved_by 已填）；runs=450（run1 前后未变）；
  trial_reservations=0；lockbox_uses/lockbox_budget 空（0/3）。**五链校验**
  （台账复制至临时目录后 `Ledger.verify_chain()` 复跑）：problems=[]。
  R3.toml 台账 toml_sha `72dc6403…`==盘上文件 LF 归一化 sha；与草案唯一
  差异=approved_by 填写（恰为 E2 义务）。C005 正式登记文件与被钉草案
  内容逐字一致（仅换行差异，钉为草案字节 sha `d00adad5…`）。
- **冻结源不变**：run1 基线 0082b19 → HEAD 9374283，`scripts/bt.py`、
  `src/mt5data/ext/merge.py`、`src/mt5quant/research/candidates.py`、
  `src/mt5quant/h_readonly_precheck.py` diff 为空；期间变更仅 fix1/fix2
  链文件、S4c 导出器、核验程序+测试与 t121/pline/文档旁线。
- **批评审在案**：S1 核验程序评审 PASS（含 fail-closed 穷举探针）；S4c+S5
  批评审 PASS（fb57e02/e549f6d，含 S6/S7 独立复核与 F1 闭合的 LF↔CRLF
  反推数学证明）；fix1 评审（NEEDS_FIXES→F1 经 e549f6d 闭合）记录在案。
  日历工件 `calendars/USTEC.json` 实测 sha `b0dea4b2…`、PROVENANCE 双 sha
  并记。

## 1. 事项一：E1 重绑有效性——**有效**

A1（2026-11-06）E1 条款：登记内容任何变更即失效，须重审重绑。本链变更的是
预筛绑定输入之一（FRED manifest 08ff3dff→6f068426），触发该条款。核验其
"重审重绑"是否走完：

1. **先程序后声明**（R6 伪声明风险闭合）：used=true 的唯一来源是核验程序
   （55426ed，S1 评审 PASS、fail-closed 穷举核验、22/22 测试独立复现）；
   无 V4 证据即 used=false（基线跑 exit=1 实证）。manifest 41 条逐年证据
   内嵌（HistData 先例形态），非手填。
2. **数据面零变更**：parquet/costs/specs/日历/候选五钉逐位不变（重算命中）；
   仅 manifest 一文件因追加 yearly_verification+label_rule+exported_at 换 sha；
   usage_restriction 一字未动（导出侧硬校验）。
3. **重跑重登记**：S6 真实预筛重跑确定性产出 `94af2dfb…`；与 rowid26 相比
   恰 8 组逻辑 diff、checks 全树逐字节不变、数字零漂移（独立复算确认）；
   S7 append-only rowid27（supersedes=26）、五链完好、A1 四钉逐字沿用。
4. **评审覆盖**：通道设计（S0）→S1 聚焦评审→S4c+S5 批聚焦评审（含 S6/S7
   独立复核）→本 S8 A1 级裁定收口。每一步治理级别与设计 A.4 表一致。

**裁定**：换绑链满足 E1"重审重绑"；**rowid27（stable_key
`N1:C005:main-only-real-precheck:2026-11-07-fix2-fred-yearverify-rebind`，
payload `94af2dfb…`）为 R3 主检验的有效登记预筛**。原 A1 批准的五项修订、
真实预筛判定、D33 N=2 确认、单次功效 59.5%≥50% 门——其数值载体在 rowid27
中逐字节延续，批准效力经重绑链延续至 rowid27。

## 2. 事项二：trial_budget 记账口径与 run3 授权——**机械口径 0/2；授权 run3**

事实：R3 trial_budget=2（close_rule 括注"1 主规格 + 1 修复重跑"）；run1
（coverage_segments 空，装配段拒）与 run2（年份闸门，装配段拒）均零 runs、
零 trial_reservations（台账实测 runs=450 不变、reservations=0）；fix1
RUN-REPORT 自述"修复重跑额度已使用（程序性）"，且实现方自己声明该口径
分歧须 A1 裁定、不得自释。

**裁定**：
- 预算的计量单位是 close_rule 的**去重研究试验**，其唯一权威登记载体是
  append-only 台账的 runs（D33 谱系口径即以此取数：R1 计 1=runs 449/450
  去重）。run1/run2 均在装配段被 fail-closed 拒绝、未产生任何 runs 或
  预约，**不构成去重研究试验**；机械口径 **0/2 未耗**。
- fix1 报告"额度已使用（程序性）"系实现方程序性自述，在台账中无任何对应
  记账（runs/reservations 均未动），**不构成预算扣减依据**；若采信无台账
  对应物的平行记账，将破坏 append-only 台账作为唯一登记之源的原则。
- **run3 为 C005 主规格的首次真实运行，其 runs 落账后计 1/2**。
- 附带约束（防"修复循环"膨胀，呼应括注意图）：本次为**最后一轮授权修复
  循环**。若 run3 再次于装配段或 runs 落账前失败、或其闭合需要任何新的
  代码/绑定/登记变更，本授权即失效，须回到 A1 级新裁定；不存在隐含的
  第三次修复重跑额度。

## 3. 事项三：E3 production input release——**放行（限域）**

FRED USTEC/D1 此前为 `real_precheck_only`（h_readonly_precheck 声明模式，
装配器产物恒带 `production_input_release_required` blocker——放行是治理
动作非代码旗标）。A1 E3 要求 R3 研究运行前走完放行。

**裁定：放行 FRED USTEC/D1 为 R3 主检验（C005）研究运行输入**，依据：
- **V1 工件完整性**：raw CSV sha `c3d915d7…`==manifest.source_files；
  parquet rows/first/last 与 manifest 三重一致。
- **V2 转换逐位复算**：41 年 roundtrip_identical 全真（同一冻结转换器
  converter v1，与入库路径同构，S1 评审核验+本次证据复核）。
- **V3 日历形状**：零周末行/零重复/严格递增/无 >30 天年内洞（逐年证据在
  manifest）。
- **V4 零修订**：2026-11-07 同源重下载，修剪到绑定快照域后与绑定原件
  **逐字节相同**——绑定全域（1986-01-02..2026-10-26，为研究消费域
  [1986-01-02, 2025-10-23) 的超集）实测零修订，证据双 sha 留档。
- **V5 跨源腿如实声明不可用**（无第二批准来源），补偿旁证=A1 已做的 FOMC
  日历独立闭式复算（310/257/213）+入库时 MT5 重叠期相关 0.9287。
- D36 入库批准在案；usage_restriction 逐字保留且导出侧硬校验；T94/R14
  逐年闸门条件经程序性证据满足（41/41）。

**限域**：本放行覆盖**本绑定工件对**（研究库 manifest `6f068426…` +
parquet `c6645256…`）用于 R3/C005 主检验及其诊断；不是对 FRED 来源的
全局生产批准；V5 跨源缺口与 V4 浮点分辨率口径（pandas C 解析器默认精度，
S1-F2）留在案上，后续任何 FRED 绑定沿用时须重新核验修订（fresh 对账）。

## 4. 事项四：F1 处置确认——**确认**

S1-F1：fresh 是活数据（下载日 2026-11-07 > 绑定末观测 2026-10-26），末年
fresh-only 尾部日期按预写规则必触发 used=false，与"全部 used=true"预期冲突；
S1 评审给出两个处置选项：(a) 注入前裁剪 fresh 至绑定域+双 sha 留档；
(b) 末年尾部豁免的显式裁定。主会话采用 (a)。

**裁定：确认 (a)**。理由：预写 V4 规则的立法意图是"检测修订"，修订的忠实
语义=**已捕获域内**的值/日期变化；快照后的新增观测是时间流逝不是改史。
裁剪到绑定域正是该规则的形式化，且域外行（8 行）排除计数留档、域内任何
差异照旧逐年级否决——fail-closed 性质无任何削弱。选项 (b) 则是对单一年份
的临时豁免，弱于且劣于 (a)。附带说明：本链对账域取主 manifest 域
（末 2026-10-26，含研究截断后的 2025 尾部与 2026 段）——对账域为研究
消费域的超集，零修订结论对消费域必然成立，属加强不属放宽。

## F. 发现的不一致与观察（如实记录，均不翻裁定）

- **F-A（留档缺陷，run3 授权已针对性收紧）**：fix1 目录 `command-run2.txt`
  所载命令的 `--fomc-calendar-sha256` 为 **63 字符**
  （`…c7e8c1e76ec971c5`，较真值少一个 `8`），与其自述"与 run1 完全相同
  的原文重跑"不符；run1 `command.txt` 为正确 64 字符且==文件实测 sha==台账
  登记钉。run2 死点在装配段覆盖核对，先于 CLI sha 的任何消费点
  （`_verify_fomc_registration` 与日历装载均在后），故该笔误对 run2 的
  结果、台账、预算均无影响（零 runs）；但无法从留档区分笔误仅在文档或
  实际命令亦带病。**run3 授权明确以 run1 `command.txt` 原文（正确 64 字符
  sha）为准**，且 bt.py 的 F1 机械核对将在运行时把 CLI sha 与 rowid27
  登记钉逐字比对，任何此类笔误届时 fail-closed。
- **F-B（观察，域限定导出的固有形态）**：研究库 manifest 内
  yearly_verification 的逐年 rows 记主库未裁剪序列（如 2025 条目 rows=250
  而研究库 parquet 2025 切片为 203 行至 10-23；"2026" 条目 205 行在研究库
  parquet 无对应行）。裁定者复核：主库侧 41/41 逐年计数逐一相符；merge.py
  只消费 used 布尔；研究库 parquet 的全部在场年份（1986..2025）均有
  used=true 条目。无功能影响，留档以免未来读者误读。
- **F-C（观察，既有状态非本链引入）**：研究库 USDJPY bars 存在 ≥cutoff 的
  外部进程写入（verify-only 报 2 条违例，S4 全量导出因此拒绝后改走域限定
  通道）——批评审已记录的已知状态。C005 输入为路径+sha 双绑定，不受波及；
  但该污染是研究库卫生欠账，建议主会话另行走清理/取证，不属本裁定范围。
- **F-D（观察）**：ext manifest 落盘为 CRLF（批评审 F4）——白名单钉的就是
  落盘字节 sha（`6f068426…`），当前无出证不符；跨平台重导会使 manifest
  sha 漂移，届时须重绑。

## 5. 授权范围（run3 命令边界）

1. **命令**：run1 `command.txt`（`.superpowers/sdd/2026-11-07-r3-main-test/
   command.txt`）**原文重跑**，即 `bt.py gate --card H102 --strategy
   mt5quant.strategies.library.fomc_preannouncement:PreFomcAnnouncementDrift
   --gates G0,G1,G2,G3,G4,V3 --event-kind fomc_calendar --fomc-calendar
   <主仓日历工件路径> --fomc-calendar-sha256
   a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e88c1e76ec971c5
   --event-days-start 1986-01-02 --event-days-end 2021-05-17`。
   日历 SHA 参数**必须为上列 64 字符全文**（不得采用 run2 留档的 63 字符
   变体）；cwd=研究库工作区、PYTHONUTF8=1、解释器=研究库 .venv（-B）、
   bt.py=主仓 HEAD 9374283 工作树。事件窗参数与登记绑定一致，零变更。
2. **代码边界**：run3 之前主仓 bt.py 与 src/ 冻结源不得有任何新改动
   （0082b19→9374283 冻结源 diff 为空已核）；装配器工作树状态须与
   `hashes.code` 记录一致（`67f2abc8…`）。run3 **不得**伴随任何新的代码、
   绑定、登记内容变更——需要变更即触发撤销条件 2。
3. **预算记账**：run3 的 runs 落账后计 **1/2**（机械口径）；运行期
   trial_reservations 照常预约/释放；本裁定不豁免任何运行期机械检查
   （随机化入口缺锚 fail-closed、≥1999 次抽取同冻结日历、static_long、
   seed 确定性等按 R3.toml advance_rule 原样执行）。
4. **报告义务沿用**：A1（2026-11-06）D 节义务——**15/213 个月末重叠事件
   子集的窗贡献分列（子集均值 vs 全集均值），不作否决项**；charged 口径
   并列参考（E7）；诊断段边界（MT5/延长段恒非盲不进主检验，E10）；
   开轮其余前置（E4 锁箱 0/3 机械确认、E5 D33 按 runs 实际取数重列、
   E9 provisional 解除落档等）仍为主会话程序义务，本裁定不代行、不豁免。

## 6. 撤销条件（任一成立即失效，须回 A1 级重审）

1. 六个绑定工件（manifest `6f068426…`、parquet `c6645256…`、costs
   `c35550b9…`、specs `144bb224…`、日历 `a53577d9…`、候选 `d00adad5…`）
   在 run3 前任何时点再变，或台账 C005 最新行不再是 rowid27；
2. run3 于装配段或 runs 落账前再次失败，或其闭合需要任何新的代码/绑定/
   登记变更（即出现第三次修复循环需求）；
3. 绑定域内发现任何 FRED 修订证据（同源重下载与绑定原件不再逐字节一致）
   ——σ/覆盖/功率基线须重算，属重大数据事件；
4. 台账任何链校验出问题、或本裁定所依赖的任一 sha/证据被证伪；
5. 任何 used=true 声明被发现非核验程序产出（伪声明）。

（裁定只写本文件；不 commit，主会话集成。）
