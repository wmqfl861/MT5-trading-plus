# S4c+S5 两提交独立批聚焦评审——2026-11-07

- **对象**：两提交独立评审（主树 `demo-next-20261105`，评审时主树 HEAD = e549f6d，确认）：
  - **fb57e02** `feat(s4c): export_research_store 域限定导出模式 --only-ext-source（TDD）`
    （父 b5b41f1；`scripts/export_research_store.py` +66 / `tests/test_export_research_store_scoped.py` 新 390 行）；
  - **e549f6d** `fix2: N1 装配器 FRED manifest 换绑（yearly_verification 版）+ 日历导出 LF 写盘（F1）`
    （父 fb57e02；`scripts/h_assemble_n1_precheck.py` +73 / `tests/test_h_n1_precheck.py` +87）。
- **范围**：S4c 域限定导出（设计 s4b/05-minimal-change-design.md）＋ S5 装配器换绑
  （fred manifest 08ff3dff…→6f068426…）+ fix1-F1 换行修复 + year_handling 如实化；
  含 S6 载荷 diff 主张与 S7 台账 rowid=27 登记的独立复核。
- **方法**：独立评审者，全程只读。`git worktree add --detach
  <workspace>/demo-review-s45-e549f6d e549f6d`（保留未删，一树覆盖两提交）；
  未修改任何仓库文件、未派子代理、未填 approved_by、未动研究库台账（台账仅
  `sqlite3 mode=ro` 读；五链校验在 `C:/tmp/s45-review/ledger-copy.sqlite` **副本**上复跑）。
  解释器 `…/664d5945-…13/.venv/Scripts/python.exe`，PYTHONUTF8=1。所有 sha/逐叶 diff/
  台账 SQL/测试重跑均由评审者独立执行，不依赖实现方脚本断言输出。
- **结论**：**Spec PASS ／ Quality PASS**（两提交各自成立；发现清单纯观察项，
  无需修项）。fix1 评审 F1（换行）经 e549f6d 正确闭合，已独立复算确认。

## 结论摘要

| 主张（实现方报告） | 评审方法 | 结果 |
|---|---|---|
| fb57e02 改动面 = 05 设计四件套（argparse 项+分流+新函数+keyword 过滤） | `git show fb57e02` 全 hunk 直读 vs 05 设计逐条 | ✅ 逐条相符（CLI 项/守卫文案/`_export_ext_only` 体/过滤位置均照设计）；唯一偏差=分流放置（见 §fb57e02-1，成立） |
| 分流须置于 verify-only 分支之前（设计放置注记与其守卫矛盾，以设计代码为准） | 代码控制流推演 + 测试实测 | ✅ 偏差**必要且正确**：按设计注记放置则 `--only-ext-source X --verify-only` 在 verify-only 分支先行返回（dest 缺失时静默 rc=0"自检通过"），互斥守卫半数不可达；前置后守卫可达（测试断言两种组合均 rc=1）。守卫代码承载语义意图（fail-closed 互斥），注记是机械细节笔误——以代码为准合理，且偏差已在提交信息+证据 00 中如实申报 |
| 默认路径零变化（无新 flag 行为等价） | 代码结构审（全部改动在 `args.only_ext_source` 真值或 `only_sources is not None` 短路之后；keyword-only 默认 None；唯一既有调用点 :544 不传新参）＋ 182 既有测试在 e549f6d 独立重跑 | ✅ 结构性零变化成立；`test_export_research_store/ext_data/ext_merge/mt5data_store/lockbox/lockbox_budget/schedule` **182 passed（评审者实跑 175.69s）**——机制证明成立 |
| 域外来源 continue（不导出/不拒绝/零触碰）、过滤在 check_exportable 前 | 代码审 + 绕过路径排查 | ✅ 域外在 `check_exportable`/`load_ext_series`/`save_ext_series` 全部之前 continue；域内白名单/使用限定/validate_manifest/slice_before_cutoff/research_cutoff 戳/导出后 `verify_ext_research_store`（覆盖 dest/ext 全树，超集方向）全数照常；无绕过路径：请求未批准来源→全拒 raise→rc1；开放段空/来源不存在→exported=0→rc1；无全局 cutoff→rc1（见 F1 输出面小瑕） |
| 10 新测试 RED→GREEN、pre/post 逐 sha 断言真实 | 测试文件直读 + 评审者实跑 | ✅ `_tree_sha` 全目录 sha256 快照断言真实（非 mock）：过期成本门对照（全量 rc=1 vs 域限定 rc=0 同测内）、bars/costs/specs/calendars 逐字节不变（排除 ext/fred/）、fred parquet 逐位不变+manifest 因 exported_at_utc 变、无 .staging/.superseded、互斥两组合 rc=1、v1 锁箱拒绝；RED 10/10（argparse 不识别新 flag，00-RED-GREEN.txt）诚实；GREEN 评审者实跑 10 passed（并入下述 32 passed） |
| S4c 真实执行：21 断言全 PASS、单文件变化 | 04-post-verify.txt=21 PASS+ALL CHECKS PASS；verify_post.py 逻辑直读（断言真实：pre/post 全树比对、分组逐 sha、暂存残留、主库钉） | ✅ 独立复验：pre/post JSON 树自洽（194 文件，恰 `ext/fred/USTEC/D1/manifest.json` 一处变）；**当前生产 data_research 194 文件全树重算**——fred manifest `6f068426…`、parquet `c6645256…`、rows/first/last/cutoff 自洽、yearly_verification 41 条 1986..2026 全 used=true 独立确认；与 post-state 仅 2 文件不同（calendars/USTEC.json+PROVENANCE.md，即 S5-F1 重导，时序自洽）；评审者从只读 worktree 对生产 dest 重跑 `--verify-only`：**ext 段零违例**（仅 USDJPY bars 2 条已知外部写入违例，与 S4c 无涉）；两侧分发一致（脚本 sha `ab4cffe3…` 双侧相等） |
| e549f6d 白名单恰一条目、其余钉逐位不变 | diff 直读 + 测试锁全集 + 生产文件 sha 独立重算 | ✅ N1_WHITELIST 仅 manifest `08ff3dff…→6f068426…`（旧 sha 注释留档）；parquet `c6645256…`/costs `c35550b9…`/specs `144bb224…`/CALENDAR `a53577d9…`/CANDIDATE `d00adad5…` 全部独立重算命中；`test_whitelist_rebind_is_exactly_the_manifest_sha` 锁全集 |
| F1 修复：newline="\n" 落盘 sha==b0dea4b2…、PROVENANCE 如实 | 代码审 + 三重复算 | ✅ `_write_calendar_export` 固定 `write_text(..., newline="\n")`、返回文本 sha、exclusive 拒覆盖保留在 main；生产 USTEC.json 实测 sha `b0dea4b2…`、632150 字节、零 CR；**LF→CRLF 反推哈希=旧件 `67479203…` 逐位命中**（证明 F1 修复=纯换行差异消除，内容零变）；PROVENANCE.md 双 sha 并记+F1 成因+重导记录+manifest 换绑注（last_time 两版一致）——如实 |
| year_handling 两路径（有→新文案/无→原文案逐字保留）；GAP_RULES/N1_RULES 零改动、hashes.rules 与 rowid26 相同 | 代码审 + 删除行与 `_YEAR_HANDLING_NO_YEARLY` 逐字比对 + 测试逐字锁定 + 载荷机器证明 | ✅ 无逐年分支返回旧 dict **逐字节相同**（basis+note 与被删内联文本比对）+ `GAP_RULES["explicit_registration"]` 原文返回；`gap_rules=dict(GAP_RULES)` 逐运行副本浅拷贝仅改顶键，常量零改（diff 无 GAP_RULES/N1_RULES hunk）；hashes.rules 域 rowid26↔27 深度相等（`ce0f7399…` 双份一致）；used 门 fail-closed（touched 年非 used=true→BindingError） |
| S6 载荷 diff 恰 8 组、checks 全树逐字节不变、数字零漂移 | 评审者独立递归逐叶 diff（两份 result.json 落盘字节 sha 先复核 `cd538fbb…`/`94af2dfb…`） | ✅ 差异叶 80（实现方 79，计数口径差：旧空 dict 标记，F3）全部归入声称 8 组，无意外、无缺失；`result.checks`/`provenance.calendar`/`hashes.rules`/selection/coverage/coverage_event_era/costs/missing_intervals/counts/coverage_declared/eligibility/note/status 全部深度相等；hashes.artifacts 仅 manifest 键、hashes.code 仅装配器键；确定性：calendar-reexport（冒烟）result.json sha==`94af2dfb…`==正式跑；stdout==result.json 逐字节；stderr 仅导出一行（b0dea4b2…） |
| rowid=27 append-only/supersedes/机器核对 + 五链完好 | 台账只读 SQL + 副本 verify_chain() 复跑 | ✅ C005 三行 25/26/27；rowid26 未触碰（stable_key 仍 fix1-rerun、payload sha `cd538fbb…`）；rowid27 存储==registered-payload.json；stable_key/supersedes（引 rowid=26+旧 sha）/result_payload_sha256==`94af2dfb…`/A1 四钉（approval/spec/calendar/candidate）逐字沿用；checks 26↔27 深度相等、calendar_coverage 与新产物 provenance.calendar 一致；**五链校验副本复跑 problems=NONE**；runs=450、trial_reservations=0（预算未动） |
| e549f6d 测试 RED 4→GREEN 22、相邻 463 | tdd-red/green 直读 + 评审者实跑 | ✅ RED=更新钉测试+3 新测试（含 `test_frozen_n1_pins_match_plan_binding` 因换绑而红——诚实）；GREEN 评审者实跑 `test_h_n1_precheck.py`+`test_export_research_store_scoped.py` 共 **32 passed**；全量 15 failed 经 stash 基线复核 14 既有+1 偶发（t110 单独重跑过）——方法论可信，未整跑复核（同 fix1 评审口径） |

## fb57e02（S4c）逐项核验

### 1. 与 s4b/05 设计的符合度（含申报偏差）

四件套逐条比对：CLI 项（名称/action/metavar/help 文案逐字同设计）、`_export_ext_only`
（守卫顺序、无 cutoff 拒绝、ExtDataError 捕获、exported==0 拒绝、verify_ext、
摘要文案——仅错误提示补充括注，语义不变）、`export_ext_series` keyword 与 2 行过滤
（含注释语义）全部照设计。**唯一偏差=分流放置**：设计"改动 2"注记写"verify-only
分支之后、staging 块之前"，但其自身守卫代码 `if args.include_ext or args.verify_only:
拒绝` 在该位置对 `--verify-only` 半不可达（verify-only 分支先行 return）——实测推演：
dest 缺失时该组合会静默走完 verify-only 并打印"✓ 研究库自检通过"rc=0，scoped 请求被
无声忽略。实现前置分流使守卫可达（测试断言两组合 rc=1+文案）。**以设计守卫代码为准、
放置注记为笔误的裁量合理**：守卫承载 fail-closed 语义意图且 05 文档自身标题即"供聚焦
评审；本次未实现"（草案）；偏差在提交信息与 s4c 证据 00 中双重如实申报。判定：偏差
必要、正确、已申报——符合设计意图。

### 2. 默认路径零变化

结构证明：`only_sources: set[str] | None = None`（keyword-only 默认 None）+ 过滤首条件
`only_sources is not None` 短路；main 新分支守卫 `if args.only_ext_source:`（缺省
None→假）；`_export_ext_only` 无 flag 不被调用；全仓 grep 确认 `export_ext_series`
唯一既有调用点（_export_snapshot :544）不传新参。机制证明：182 既有测试评审者在
e549f6d 只读 worktree 实跑全绿。两项叠加成立。

### 3. continue 语义与过滤位置（绕过排查）

- 域外（未列出）来源：循环体首行 continue——先于 `check_exportable`（白名单+使用
  限定）、`load_ext_series`（validate_manifest）、`slice_before_cutoff`、
  `save_ext_series` 全部环节：不导出、不计 rejected、不 raise、零触碰（测试断言输出
  无 "stooq"/"白名单"、dest 不创建）。过滤前置的必要性成立：若在 check_exportable
  之后，域外未批准来源会计 rejected，与"ext 库只剩未批准序列→整体失败"（:287-290）
  叠加会把域限定模式变成整体 ExtDataError——设计注记明确此点，实现相符。
- 域内来源零豁免：白名单/使用限定 fail-fast（缺 usage_restriction → raise→rc1，
  测试覆盖）、cutoff 裁剪+research_cutoff 戳、导出后 `verify_ext_research_store`
  枚举 dest/ext 全树真实 parquet 复检（orphan/缺戳/越界→rc2，测试预置 orphan 覆盖）。
- 无绕过路径：指定未批准来源→全序列 rejected→:287 raise→rc1；来源不存在/开放段
  全空→exported=0→rc1；v1 锁箱（无全局 cutoff）→rc1（测试覆盖）。请求混合来源时
  approved+unapproved 并存且 exported>0 的路径不出错但也不导出未批准序列（数据面
  fail-closed 成立；输出面小瑕见 F1）。

### 4. 测试质量与 RED 证据

10 例测试机制真实：`_tree_sha` 生产者=读者同一目录全量 sha256（零触碰断言的事实
来源）；`test_scoped_skips_cost_gate_and_touches_nothing_but_ext_fred` 在同一测试内
完成"全量导出成库→推 tick 水位使成本缓存过期→全量路径 rc=1→外部写 USDJPY bars+
calendars→域限定 rc=0→除 ext/fred 外逐字节不变+无暂存残留"的完整因果对照。
RED 证据（00-RED-GREEN.txt）：10/10 failed，原始报错 argparse unrecognized
arguments——flag 不存在阶段的诚实 RED。评审者 GREEN 实跑通过（见摘要表）。

### 5. 真实执行证据（s4c-scoped-export-impl/）与独立复验

- 03-run-output.txt=生产实跑（10,033 根、摘要行与实现文案一致）；04-post-verify.txt
  **恰 21 条 PASS**+ALL CHECKS PASS；tools/verify_post.py 直读：断言为真实比对
  （pre/post 全树、分组逐 sha、主库双钉、暂存残留检查），无硬编码通过。
- 评审者独立复验（不改研究库）：pre/post JSON 树自洽（恰一文件变）；**当前生产
  data_research 全树 194 文件 sha 重算**与 post-state 全等（仅 calendars 两文件
  因随后 S5-F1 重导而异——时序自洽且反向印证两阶段）；fred manifest=`6f068426…`、
  parquet=`c6645256…`、manifest rows/first/last/research_cutoff 与 parquet 自洽、
  yearly_verification 41 条（dict 按年键）1986..2026 全 used=true 独立确认；
  load 侧（05-load-side-check）与评审者复核一致；**从只读 worktree 以生产 dest
  重跑 `--verify-only`：exit=2 仅 USDJPY/D1+H1 两条 bars 违例（外部进程写入的
  已知状态，域限定模式不触碰不清运），ext 段零违例**——S4c 写入产物至今仍过
  H-01 口径复检。两侧分发（脚本 sha `ab4cffe3…`）相等确认。

## e549f6d（S5）逐项核验

### 1. 白名单换绑恰一条目

diff 直读：N1_WHITELIST manifest 条目 `08ff3dff…→6f068426…` 为唯一值变更（+5 行
出处注释）；其余三 entries 与 CALENDAR_SHA256/CANDIDATE_SHA256 无 hunk。生产文件
独立重算：costs/specs/calendar/candidate 四钉全命中；manifest/parquet 与 S4c 产物
一致。测试侧 N1_WHITELIST 副本同步更新且被 `test_frozen_n1_pins_match_plan_binding`
（module==test 双向锁）+新测试（全集逐键锁）双重锁定——无漂移空间。

### 2. F1 修复正确性（三重独立复算）

代码：`_write_calendar_export` 统一"序列化文本→LF 写盘→返回文本 sha"，main 导出
路径改调它（exclusive 拒覆盖保留原位）。复算：(a) 生产 USTEC.json sha=`b0dea4b2…`
（与 fix1 PROVENANCE 所记 LF 文本哈希逐位一致）、632150 字节、全文零 CR、10033
sessions、complete_through=2025-10-23、close_time=day+1d；(b) **当前 LF 字节把 \n
替换回 \r\n 后 sha256=旧件 `67479203…` 逐位命中**——数学上证明 F1 修复只消除换行
差异、内容零变；(c) PROVENANCE.md 如实：双 sha 并记、F1 成因（write_text 平台
换行转换）、重导命令与换绑 manifest 注（last_time 两版一致）俱全。real-run.stderr
恰一行导出记录（sha b0dea4b2…）。

### 3. year_handling 派生逻辑（两路径）与规则零改动

- 有 yearly_verification（used 门 basis=yearly_verification）→ 新 basis
  `yearly_verification_all_touched_years_used`+touched 范围/条数如实文案；无 →
  `dict(_YEAR_HANDLING_NO_YEARLY)`，与被删的 fix1 内联硬编码**逐字节相同**（评审者
  比对删除行）；`_explicit_registration` 无逐年分支返回 `GAP_RULES` 常量原文。
  两分支均有测试：新文案断言年份范围入 note；无逐年 fixture 断言**逐字相等**（非
  子串）+常量本身仍含旧文案（规则域不变的自证）。
- 常量零改动：diff 无 GAP_RULES/N1_RULES hunk；`gap_rules=dict(GAP_RULES)` 逐运行
  浅拷贝只改顶键 explicit_registration。机器证明：rowid26↔rowid27 载荷
  `hashes.rules` 深度相等（值 `ce0f7399…`，与 used-gate 预检记录一致）。
- `_used_gate` 本身未被改（fix1 已有）：touched 年非 used=true → BindingError
  fail-closed；真实 manifest 实测 basis=yearly_verification、1986..2021 共 36 条
  全过（评审者从载荷独立复核，含 calendar:None 形态）。

### 4. S6 载荷 diff 独立复核

评审者独立递归逐叶 diff（fix1 real-run/result.json vs s5-s7-rebind/real-run/
result.json；两文件落盘字节 sha 先复核=`cd538fbb…`/`94af2dfb…`）：差异叶 80，全部
归入声称 8 组（binding.files manifest sha、hashes.artifacts 同键、used_gate.basis、
used_gate.years 73 叶、year_handling.basis、year_handling.note、gap_rules.
explicit_registration、hashes.code 装配器），**无意外组、无缺失组**；冻结面
（result.checks 全树/provenance.calendar/hashes.rules/selection/coverage/
coverage_event_era/costs/missing_intervals/counts/coverage_declared/eligibility/
note/status）全部深度相等——"恰 8 组逻辑 diff、checks 全树逐字节不变、数字零漂移"
主张成立。确定性：calendar-reexport（冒烟）result.json sha==正式跑==`94af2dfb…`；
stdout==result.json 逐字节。

### 5. rowid=27 登记与五链校验

台账（`data/research/ledger.sqlite`，评审者 mode=ro SQL）：C005 恰 25/26/27 三行；
rowid27 存储 result_json==registered-payload.json；stable_key/supersedes（引
rowid=26+`cd538fbb…`）/result_payload_sha256==`94af2dfb…`；rowid26 未触碰；
checks 26↔27 深度相等；calendar_coverage==rowid26==新产物 provenance.calendar；
A1 四钉逐字沿用。**五链校验：评审者复制台账至 C:/tmp 后以 `Ledger.verify_chain()`
复跑——problems=NONE（runs/gates/lockbox_uses/candidate_checks/rounds+
trial_reservations 六表行哈希链+chain_meta 快照全过）**；runs=450、
trial_reservations=0（预算未动）。

### 6. 测试 RED→GREEN 证据可信度

tdd-red.txt：4 failed = `test_frozen_n1_pins_match_plan_binding`（既有钉测试因换绑
而红——证明钉真起作用）+3 新测试（换绑恰一条目/全年 used=true 接受+两路径如实登记
含无逐年分支逐字保留/LF-only 字节机制）；tdd-green 22 passed。评审者在只读 worktree
实跑 `test_h_n1_precheck.py`+`test_export_research_store_scoped.py` **32 passed**
（-B + no:cacheprovider，worktree 无痕）。LF 测试断言落盘字节==序列化文本字节、
无任何 CR、返回 sha==落盘 sha——机制级而非快照级。全量回归 15 failed 的处置
（stash 基线 14 既有逐一重合+1 偶发单独重跑通过）方法论可信。

## 发现清单

- **F1（低危·观察，fb57e02）**：域限定模式输出面对 rejected 计数静默——请求混合
  来源（approved+unapproved 同传）且 exported>0 时摘要不报拒绝数（全量路径有
  "⚠ 白名单拒绝 N 个"对应物），且摘要"来源 […]"列出的是请求集而非实际导出集；
  全拒时的 ExtDataError 文案仍写"--include-ext 白名单拒绝了全部…"。数据面
  fail-closed 不受影响（未批准序列仍不进研究库），纯输出保真度问题。建议后续
  顺手补一句拒绝计数提示。
- **F2（信息·观察，fb57e02）**：`_export_ext_only` docstring"自检只跑
  verify_ext_research_store（范围 = 写入范围）"措辞不精确——该校验枚举 dest/ext
  **全树**（所有来源），是写入范围（指定来源）的超集；超集方向保守（他源历史
  违例也会拦下本次导出），无正确性问题。设计 05 原文同此措辞，实现如实。
- **F3（信息·观察，e549f6d 证据工具）**：payload-diff.txt 记"raw differing
  leaves: 79"，评审者按"空 dict 记一标记叶"口径得 80；折叠后 8 组完全一致，
  无实质影响。记录以免未来读者对 diff 工具计数口径生疑。
- **F4（信息·观察，既有行为非本次引入）**：ext 段 manifest.json 落盘为 CRLF
  （save_ext_series 既有行为，fred manifest 实测含 CRLF）——与 fix1-F1 同类的
  平台换行敏感点；因白名单钉的就是落盘字节 sha（`6f068426…`），当前无出证不符
  问题，但未来若跨平台重导，manifest sha 会因换行漂移。留意即可，无需现在处理。

## 结论

- **fb57e02（S4c）：Spec PASS ／ Quality PASS**。与 05 设计符合（唯一偏差必要、
  正确、已申报）；默认路径零变化有结构证明+182 既有测试双证；域限定语义
  fail-closed 无绕过；测试与执行证据真实且经评审者独立复验（生产库 sha 重算+
  --verify-only ext 段零违例）。
- **e549f6d（S5）：Spec PASS ／ Quality PASS**。换绑恰一条目且全钉独立命中；
  fix1-F1 正确闭合（含 LF↔CRLF 反推数学证明）；year_handling 如实化两路径
  逐字可证、规则域字节不变；S6"恰 8 组 diff+checks 逐字节不变"与 S7 append-only
  登记+五链完好均经评审者独立复核成立。
- **批结论：Spec PASS ／ Quality PASS**。F1-F4 均为观察项，不构成 NEEDS_FIXES。
- 评审 worktree `demo-review-s45-e549f6d`（e549f6d detached）保留备查。
