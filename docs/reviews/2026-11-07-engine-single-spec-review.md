# 聚焦评审：bt gate 选参层 single_spec 扩展（提交 065abf7）——Spec PASS / Quality PASS

2026-11-07。评审者=独立聚焦评审者（治理链第 3 步；未参与范围裁定、缺口调查、
实现与 R3 执行链）。

- 评审对象=提交 `065abf788a8b90c4963286189c78483af920d387`（`[S] bt gate param
  selection supports single_spec (event-card capability extension)`，父
  `8f7d0ec2f6dd557b65ccb4f1bcee26ec4a96357a`），位于分支 `demo-next-20261105`。
- 评审基准=范围裁定 docs/reviews/2026-11-07-event-card-selection-scope-ruling.md
  （入库 `d6888da`，CONDITIONAL APPROVED，条件 C1-C6；其 §2 授权边界与调查文档
  .superpowers/sdd/2026-11-07-r3-main-test-run4/event-card-param-gap-scope.md §5.3
  回归清单为本记录基准）。
- 实现证据=.superpowers/sdd/2026-11-07-r3-main-test-run4/engine-extension-impl/
  （13 文件，含 README）。
- 评审方法：只读。`git worktree add --detach
  <workspace>/demo-review-engine-065abf7 065abf7`（保留）；
  未修改任何跟踪文件、未派子代理、未跑 gate、未运行任何 pytest；台账仅
  `mode=ro` URI 直查；解释器=研究库 .venv（-I、PYTHONUTF8=1）。函数级 diff、
  LF 归一哈希、台账对照均由评审者独立重算，不采信实现方留档断言（留档仅作
  交叉核对）。

## 0. 结论

- **Spec：PASS**——C1-C6 逐项核验全部闭合（C4 以"逐项归因的环境性例外"成立，
  见 §2 C4 与 F5）；裁定 §5 撤销条件 1-4 均不成立。
- **Quality：PASS**——无阻断性代码缺陷；残留观察 F1-F7（均非阻断）。

## 1. diff 边界（C1）——逐行核对通过

`git diff 8f7d0ec 065abf7 --name-status` 全树恰 4 文件：scripts/bt.py（M）+
tests/test_research_bt_script.py、tests/test_t112_research_inputs.py、
tests/test_validation_gates.py（均 M、纯新增行，tests 侧删除行数=0）。
217 insertions / 7 deletions 与主张一致（7 处删除全部在 bt.py：docstring 3 +
_check_card_lineage 3 + _select_params_auto 旧 raise 1）。留档 diff-full.txt 与
`git diff` 实际输出逐字节相同（评审者 diff -u 比对为空）。

bt.py 恰 5 个 hunk，逐段对照裁定 §2：

| hunk（新行号） | 裁定授权段 | 核对 |
|---|---|---|
| @@ -62,9 +62,11 模块 docstring | §2.5（可选项） | 仅"附录 A 枚举"措辞补 single_spec 说明，无行为 |
| @@ -491,10 +493,15 `_check_card_lineage` | §2.2 | `legal = set(_SELECTION_METRICS) | {"single_spec"}`，与授权形态逐字一致；dict 本体不动；拒绝文案改列 `sorted(legal)`（裁定补强 2 所指纯文案，裁定明示不强制，改列全合法集属一致性选择，判在界内） |
| @@ -1191,7 +1198,15 `_select_params_auto` | §2.4（仅 1194 行消息构造） | 判据行 `if rule not in _SELECTION_METRICS:` 原样（dict 原体，见 §3 机制证明）；唯一删除=旧 1194 raise，替换为 C2 注释+自持消息；算法本体零触碰 |
| @@ -4406,6 +4421,24 `_gate_choose_params` | §2.1 | single_spec 分支位于 auto 分支开头、`_select_params_auto` 调用之前；selection dict 四键形态 `{"selection","rule","grid_points":1,"params"}` 与裁定 §2.1 逐字一致；恰 1 组取 `combos[0]`；≠1 组 ValueError 消息含"网格"（"参数网格有 N 组""网格恰 1 组"两处）；一行选择记录 print；枚举路径原样 fall-through |
| @@ -5111,6 +5144,13 关卡报告段 | §2.3（C3） | `elif selection["selection"] == "single_spec":` 第三分支，渲染 `## 参数选择（single_spec）`；位于 auto 之后、else=manual 之前，结构上不可能误标 manual |

零触碰清单核验：src/、config/gates.toml、research/cards/、research/candidates/、
research/rounds/、run1 command.txt、.superpowers 工件、data/research/ledger.sqlite
均不在 4 文件变更集内（台账另见 §6 只读对照）。两预期 M 文件
（research/candidates/C005.toml、research/rounds/R3.toml，S8b 形态复原产物）
不在提交内——未被本次提交触碰（工作树事后状态见 F4）。

测试文件选择在授权清单内：RED-1×2 + RED-2 落 tests/test_validation_gates.py
既有 M5 测试组之后；RED-3 落 tests/test_research_bt_script.py 既有
test_card_check_rejects_unknown_selection_rule 之后（parametrize 三张已提交卡，
对应"RED-3×3"）；C2 直调防御测试落 tests/test_t112_research_inputs.py（裁定 §2
"建议"项）。所有既有测试零改动（tests 侧删除行=0，含 §5.3 九项，见 §3）。

**RED-1b ThresholdLong 替身正当性**：核验成立。(a) `lookahead.static_scan`
（src/mt5quant/validation/lookahead.py:164-174）缺省读 `inspect.getsourcefile(
type(strategy))` 指向的**模块文件全文**——若替身类内联定义在测试函数里，整个
测试文件会被当作策略源扫描（必然伪阳性）；替身自带独立 .py 源文件（tmp_path
下 `fake_mod.__file__` + `exec(compile(..., file))`）是使 G0 静态扫描语义正确
的构造。(b) 不削弱被测面：被测面=选参层恒等语义（card.param_grid()→combos[0]
→runs/gates 落账），gate 对 params 的消费点=engine validate_params 对
params_schema（threshold_pct float，grid (0.0,0.02)，与真实 b7 同构）的校验；
不使用真实 b7 的原因是其双品种（USTEC 信号+EURUSD 成交）/月末事件窗数据装配
远超选参层测试所需——替身把测试面聚焦在变更层，合理。

## 2. C1-C6 逐项核验

**C1（diff 边界+两枚举路径零变化）——闭合。** §1 边界核对 + §3 机制证明。

**C2（防御路径消息不退化）——闭合。** 判据仍 dict 原体（single_spec 不在
_SELECTION_METRICS → 直调 `_select_params_auto` 仍 raise，fail-closed 不变）；
新消息自持（含卡 id、规则名 repr、可自动规则列表、single_spec 通道指引），
不再调用 `_check_card_lineage`。C2 测试
test_select_params_auto_single_spec_direct_call_defends 断言
`pytest.raises(ValueError)` + `"single_spec" in str(ei.value)`——naive-GREEN
（只放宽合法集不修 1194）会使 `_check_card_lineage` 对 single_spec 返回 None →
`ValueError(None)` → str="None" 不含 "single_spec" → 测试变红，钉死有效。留档
c2-hazard-demonstration.txt（HEAD 8f7d0ec 内存加载+仅放宽合法集 → 直调得到
`ValueError with payload: None`）的复现逻辑经评审者静态复核成立：旧 1194 行为
`raise ValueError(_check_card_lineage(card, f"卡 {card.id}"))`（8f7d0ec 实测
1193=判据/1194=raise），合法集放宽后该函数对 single_spec 恒 None，退化推导
逐字成立。该测试 RED 阶段为绿属预期（HEAD 消息经未放宽的 lineage 含规则名），
README §"C2 防御钉在 RED 阶段为绿的说明"披露在案，定性为防御钉而非 RED 例
正确。

**C3（报告第三分支）——闭合。** elif 分支代码核验（§1 表）；结构上
single_spec 不可能落入 else=manual。注：无直接断言报告文本的测试（裁定 §2
测试清单亦未要求），该分支正确性由代码审查保证（F3）。

**C4（TDD 与回归）——闭合（含环境性例外，见 F5）。**
- RED 先行留档：red1-red2-run.txt 3 failed + red3-c2-run.txt 3 failed = 6，
  逐一核验均为**真实断言失败**（非 import/收集错误）：RED-1a/1b 失败于
  `assert rc == 0`（rc==1，captured stdout 明确显示旧枚举拒绝消息
  "✗ 卡 H70x：selection_rule 'single_spec' 不在支持枚举 ['max_net_profit',
  'max_sharpe'] 内"）；RED-2 失败于 `assert "网格" in out`（旧拒绝消息不含
  "网格"，rc==1 本身已达）；RED-3×3 失败于 `assert rc == 0`（card check 旧
  拒）。red3-c2-run.txt 另有 2 passed（max_moon_phase 仍拒对照 + C2 防御钉，
  与 README 口径一致；测试名未逐名留档，见 F2）。
- GREEN：green-new-tests.txt 7 passed（=新测试恰 7 例：3+3+1）；全量套件
  4193 collected = 4178 passed + 12 failed + 3 skipped，12 个失败全部位于
  tests/test_demo_run_ps1.py → 7 例新测试必在 passed 内（算术强制）。
- §5.3 九项回归零改动全绿：九项测试名在 065abf7 全部在场（833/881/906/928/
  467/t112:317 函数体含 338-340 行）；tests 侧删除行=0 → 一字未改；留档
  regression-c4-list.txt 9 passed。test_research_cards.py 整文件未被触碰。
- 全量：见 §5 归因复核。

**C5（冻结面零触碰证明）——闭合，评审者独立重算。** 以 065abf7 git blob 按
acceptance_snapshot 同口径（UTF-8 文本、CRLF→LF 归一、sha256）重算：
gate.py:49-61 注册五文件哈希全部==注册值（level_decision b81b1b39…、tables
52248e1e…、decision 705a7e05…、candidates fb788db8…、prob_mode 30da71bd…），
source_drift=[]；gates.toml LF 归一 sha256=d0a8b2f3105366e4173adcf15fb6dc65
0a032e277425ca7277abdc92086bb17b==冻结指纹；且五文件+gates.toml 的 blob 在
8f7d0ec 与 065abf7 间逐字节相同（=src/ 空 diff 的机器证明）。留档
frozen-source-zero-diff-proof.txt 与独立重算一致。

**C6（交付形态）——实质闭合，含一处程序性偏离（F7）。** 证据链完整（工作树
交付证据 13 文件）；唯提交 065abf7 发生在本评审通过之前，且裁定文书自身亦以
d6888da 入库（裁定原文"本记录不 commit"）。该偏离属主会话裁量行为、未 push
（065abf7 不在任何 remote 分支），不影响代码正确性与可回退性，记录备查。

## 3. 机制证明独立复核（两枚举路径零变化）

评审者自做函数级抽取 diff（非采信留档）：

- `_SELECTION_METRICS` dict：8f7d0ec vs 065abf7 整块逐行相等（`==` 判定 True），
  逐字节不变。
- `_select_params_auto` 全函数（42 行→50 行）：unified diff 唯一 hunk 即旧
  raise 行（删 1）→ C2 注释+自持消息（增 9），与留档 mechanism-zero-change-
  proof.txt 完全一致；SQL 查询/过滤/取最大/并列取 run_id 最小/返回结构/1216
  无候选文案在 hunk 外逐字节原样。
- 消费点全量核对（065abf7 grep）：`_check_card_lineage` 消费点收窄为仅
  cmd_card_check（bt.py:2336）——放宽合法集的行为面变化恰为裁定 §3 纳入的
  修复性对齐（card check 三卡由拒转过，RED-3 钉正向、max_moon_phase 回归钉
  反向）；`selection` 消费点=gates details 写入（4990）+报告三分支（5139/
  5147/else）——无引擎/关卡判定逻辑读取，`selection:"single_spec"` 为向后
  兼容追加（台账 gates 表 distinct selection 现值={auto,manual,None}）。
- 判据行测试（test 906 max_moon_phase）断言 `"selection_rule" in out`：新 C2
  消息含 "selection_rule" → 零改动保持绿，rc==1/零 runs 的 fail-closed 行为
  不变。

结论：对 max_net_profit/max_sharpe 两枚举路径与 manual 路径，行为逐字节不变
（diff 机械证明+消费点穷举）；对 single_spec 为新增第三通道（恒等选择）。

## 4. single_spec 语义正确性

- 返回结构：`{"selection":"single_spec","rule":"single_spec","grid_points":1,
  "params":combos[0]}`——与裁定 §2.1 逐字一致；`(params, selection)` 经
  `_gate_choose_params` 单一漏斗返回两调用点（主路径+拟合前占位），raise 点
  次序与裁定核验 3 相同（先 cards 幂等重登、后选参 raise、后一切预算相关写入）。
- H102 形态（无 [params]）：cards.py:140-146（冻结文件，只读核对）
  `param_grid()` 无参→`[{}]` → params={}（RED-1a 断言 json.loads(params_json)
  =={}）。
- H101 形态（[params] threshold_pct=[0.0]）：`[{"threshold_pct": 0.0}]` →
  params={"threshold_pct": 0.0}。台账 mode=ro 直查：run 449/450 params_json
  原文均为 `{"threshold_pct": 0.0}`——单键 dict 序列化字节一致；run 450 gates
  V3 行 `params_selection = {"selection": "manual", "params":
  {"threshold_pct": 0.0}}`（R1 manual 先例）与调查/裁定所引逐字一致，新通道
  params 值与先例已实证执行值逐字节相同。
- ≠1 组：`ValueError("卡 X selection_rule=single_spec 但参数网格有 N 组——…
  网格恰 1 组才与登记语义一致…拒绝运行（fail-closed）")`——消息含"网格"
  （RED-2 断言），rc==1、零 runs（失败先于一切预算相关写入）。
- 三张已提交卡（H101.toml:20 区/H101-v2.toml:14/H102.toml:16 均
  selection_rule="single_spec"；H101/H101-v2 带 [params] threshold_pct=[0.0]、
  H102 无 [params]）经 param_grid 均恰 1 组 → 全部落入恒等选择，无 ≠1 组
  误配路径。

## 5. 全量套件与 12 失败归因复核

fullsuite-run.txt：12 failed / 4178 passed / 3 skipped（4193 collected，
55m40s）。12 个失败全部位于 tests/test_demo_run_ps1.py。归因复核：

- 代码关系零：该测试文件与 scripts/demo_run.ps1 均不在本次 4 文件变更集内
  （对 HEAD 零 diff）；两文件 grep 无 bt.py/selection/single_spec 任何引用；
  测试文件 import 仅标准库+pytest（经 subprocess 跑 PowerShell 脚本，不导入
  bt.py）。
- 根因真实：demo_run.ps1:321-326 预检"磁盘剩余 ≥ 5 GB（盘 C:）"（MinFreeGB
  缺省 5）；评审者现场实测 C: 盘 145G/150G used、4.7G free（97%）——机器状态
  与归因一致；隔离重跑（fullsuite-demo-run-ps1-attribution-rerun.txt）同失败
  集 12 failed/14 passed，失败输出含 `[X] 磁盘剩余 4.61 GB ≥ 5 GB（盘 C:）`。
- 结论成立：12 失败=环境性磁盘项，与本次 diff 无调用关系；其余全绿（含全部
  bt gate/screen/card check/cards 装载/t112/验证关卡测试与 7 例新测试）。
  C4"全量套件绿"以归因口径成立；字面全绿在本机磁盘 <5GB 状态下不可达成，
  属机器前置条件而非提交属性（F5）。

## 6. 台账与冻结面只读对照（评审时点）

mode=ro 直查：runs 最大 run_id=450 且 count=450、gates 最大 rowid=78、
ledger_events 最大 rowid=1、trial_reservations=0、cards H102
registered_utc=2026-11-06T22:41:11.921487Z——与裁定 §1 核验 3 记录完全一致，
实现会话零主台账写入主张成立（实现期 pytest 仅 tmp_path 临时台账）。

## 7. 残留风险与观察（F 编号，均非阻断）

- **F1（证据代差披露缺口）**：RED-1b 的 RED 留档（red1-red2-run.txt）运行的是
  测试早期版本（ThresholdLong 内联定义于测试函数体）；提交版改为自带源文件的
  fake module。失败原因（旧枚举拒绝）与断言集不变、RED 有效性不受影响，但
  留档 RED ≠ 提交版测试逐字节，README 未披露该代差。建议后续 RED 留档附测试
  文件当时内容的哈希或直接对最终版补一次 RED 复跑（在实现前置入 stash 型
  对照）。
- **F2（留档粒度）**：red3-c2-run.txt 的 2 个 passed、green-new-tests.txt 的
  7 passed、regression-c4-list.txt 的 9 passed 均未带 `-v` 测试名与命令行，
  个别（green-new-tests 的 "70 deselected"）无法从留档反推确切选测命令；正确
  性由全量套件算术（12 失败全部 demo_run_ps1）与零改动核对交叉支撑。建议后续
  证据统一 `-v` + 命令行留档。
- **F3（C3 分支无测试钉）**：报告第三分支正确性目前仅由代码审查保证，无直接
  断言"参数选择（single_spec）"报告文本的测试（裁定测试清单未要求，非违例）。
  run5 报告落地时人工确认一眼即可。
- **F4（工作树事后状态，非本提交问题）**：demo-next-20261105 工作树在提交后
  （08:25-08:26，晚于 065abf7 08:20:43）出现未跟踪 .gitattributes（S8b §4
  事项 (d) 的 eol=lf 钉）与 C005.toml/R3.toml 工作树态变化（status 现显
  M C005 + D R3，文本 diff 为空；D 系 eol 属性归一化语义）。两文件均不在
  065abf7 内，本评审判定与提交无关；但 run5 前置核验（run4 §2 全套）必须按
  原始字节 sha256 钉复核 R3.toml/C005.toml 实物字节，不能以 git status 干净
  或文本 diff 空为据（.gitattributes 注释自述同口径）。
- **F5（run5 环境前置）**：C: 盘 97%/4.7GB<5GB 会同样杀死 demo_run.ps1 预检
  类测试与任何依赖该预检的链路；run5 执行前建议清理磁盘至 ≥5GB 并重跑
  tests/test_demo_run_ps1.py 确认 12→0，使"全量绿"从归因口径收敛为字面口径。
- **F6（H101-v2 覆盖缺口）**：RED-3 覆盖三卡 card check，但 gate 主路径仅以
  合成卡（H705/H706/H707）测试；真实 H101/H101-v2/H102 的 gate 全链路由 run5
  本身承担（裁定风险 6 已载：本扩展只保证越过 run4 死点）。无行动项，沿裁定。
- **F7（C6 程序性偏离）**：实现与裁定文书在本评审通过前已 commit（065abf7/
  d6888da，均未 push）。未影响评审输入完整性（评审即针对该提交），如后续步骤
  需严格"评审后裁量 commit"口径，主会话应在 run5 授权文书中追认或说明。

## 8. 签署

- 评审者：独立聚焦评审者（2026-11-07；只读核验留档于本记录 §1-§6）
- 结论：**Spec PASS / Quality PASS**（F1-F7 为非阻断观察）
- 下一步入口：治理链第 4 步（A1 整裁量/run5 授权），边界=裁定 §4 步骤 4 行；
  run5 前置核验须含 F4（两文件原始字节钉）与 F5（磁盘 ≥5GB）事项。
