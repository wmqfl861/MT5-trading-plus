# V3 诊断层 window_shift 异常降级修复——独立聚焦评审（路径 A 第 3 步，2026-11-07）

评审对象：主仓 commit **ca626ae**（父 27bbdfe；`v3_event_diagnostics`
window_shift 段 try/except ValueError 异常降级 + TDD 测试）。

评审者身份：独立聚焦评审者，未参与范围裁定、处置裁定、TDD 实现与
run5 执行。**全程只读**：`git worktree add --detach
<workspace>/demo-review-diag-ca626ae ca626ae`
（按调度指令建立并保留）；除本评审记录外零文件修改、未派子代理、
未跑 gate、未写台账、未 commit/push。解释器=研究库 .venv
（PYTHONUTF8=1、-I、PYTHONDONTWRITEBYTECODE=1、pytest
-p no:cacheprovider——评审用运行不向 worktree 写入任何字节，运行后
worktree `git status` clean 实证）。评审基线：
docs/reviews/2026-11-07-v3-diag-degrade-scope-ruling.md §2+§4、
docs/reviews/2026-11-07-r3-disposition-ruling.md 裁定量 (c)。

---

## 总结论

- **Spec PASS**：§2 授权边界逐项遵守（含捕获面、键集、reason 原文、
  行区、不得触碰面 7 项零变更）；§4 TDD 证据链完整且被独立复现；
  处置裁定 (c)4 修复形态约束全部满足。**§7 撤销条件零触发**。
- **Quality PASS**：无必须修复项。发现项 F1-F6 全部非阻断（§3）。

---

## 0. 评审方法与独立复现基础

- diff 直读（`git diff 27bbdfe ca626ae`）+ AST 解析 + **父子两版模块
  并行加载的运行时行为比对**（父版 events.py 以独立模块名从临时目录
  加载，与 worktree ca626ae 版在同进程同输入同种子下输出比对）。
- 台账 mode=ro 直查（run#451 七元组）；三指纹用**生产函数本身**独立
  重算（`cards.strategy_fingerprint` / `vgate.gates_sha256`）。
- 证据目录：研究库 .superpowers/sdd/2026-11-07-r3-main-test-run5/
  diag-degrade-impl/（7 个工件逐一直读；diff-authorized-scope.patch
  的 +/- 行与实际 commit diff **逐行相同**，153=143+10，仅上下文行
  数不同）。
- 评审者自建独立合成形状复现降级（不复用实现方测试代码）：假日缺口
  1996-07-04 形状、双缺口形状（1996-07-04+2002-01-29）、正常可解析
  strict 形状、缺列坏输入形状。

## 1. 实现方主张逐项核验（10 项主张全部成立）

| # | 主张 | 独立核验结果 |
|---|---|---|
| 1 | diff 恰两处 | ✓ `git diff --name-only` = events.py + tests/test_validation_events.py，无第三文件 |
| 2 | 正常路径逐字节不变仅缩进+4 | ✓ 旧段 9 行（父版 1051-1059，与裁定 §2.1 行区**逐行吻合**）与新 try 体 9 行去除 +4 缩进后**逐字节相等**（脚本比对 True）；运行时佐证见 §2-E |
| 3 | 降级键集与 year_block 同构 | ✓ AST：两降级分支 keys 均为 `['passed','reason','note']`；运行时：单年块强制 year_block 降级后 `sorted(ws)==sorted(yb)==['note','passed','reason']` |
| 4 | reason=str(e) 原文无合成键 | ✓ 运行时 reason=`'event unavailable: exact window missing for fomc_calendar:1996-07-03'`（逐字符原文，含失败标签）；无 net_mean/n_events/dropped |
| 5 | 单一 except ValueError（MRO 实证） | ✓ 源码唯一捕获面 `except ValueError as e`；运行时 `SignalUnavailable.__mro__[:4]=[SignalUnavailable, ValueError, Exception, BaseException]`，`issubclass(...,ValueError)=True`（events.py:317 `class SignalUnavailable(ValueError)` 直读） |
| 6 | RED 1 failed→GREEN 30 passed | ✓ RED-run.txt：`1 failed, 2 passed, 27 deselected`，失败=断言 1（不抛异常）处 `SignalUnavailable` 自 events.py:514 抛出、经父版 **1052 行**（=`frame = event_returns(bars, shifted, …)`，已对照父版 blob 逐行定位证实）传播——**真 RED，非假 RED**；另 2 条钉定测试修复前通过（§4.2"修复前亦应通过"成立）。GREEN 30 passed **评审独立复现**：worktree 全文件 30 passed in 224.27s（实现方 220.76s） |
| 7 | 回归四组 a/b/c/d | ✓ (a) 测试文件 numstat=120 增/0 删，现存 T86 测试零改动（评审定向运行通过）；(b)(c) 见 §2-F/G；(d) frozen-source-comparison.txt 与评审独立重算全部吻合（§2-H） |
| 8 | 全量 12 failed=run4 基线逐名相同 / 4181 passed | ✓ 评审对两份 fullsuite 工件 `grep ^FAILED` 逐名 diff：**完全相同**（12 项全部位于 tests/test_demo_run_ps1.py）；4178→4181 恰 +3=新增 3 测试 |
| 9 | 指纹三不变重算 | ✓ 见 §2-H（四项哈希+ENGINE_VERSION 全 MATCH） |
| 10 | 证据目录 | ✓ 7 工件齐全（EVIDENCE/RED/GREEN/REGRESSION/FULL-SUITE/diff-stat/frozen-comparison/patch），时间戳 11:27→12:41 与 TDD 时序一致（RED 11:27 先于 GREEN 11:31/回归 11:41/冻结比对 11:32→提交 12:41） |

## 2. 八项评审重点

### A. 降级语义正确性（核心）——成立

1. **逐键同构**：两降级分支键集均为 ｛passed, reason, note｝（AST+运行时
   双证）；值形态同构（passed=True 常量 / str(e) / 只报告语义 note）。
   实现方测试以"单年块输入强制 year_block 降级"作对照分支取键集——
   对照构造忠实（year_block 降级由 event_test 对单年块无定义触发，
   与实现细节解耦）。
2. **reason 完整保留失败标签**：运行时原文逐字符验证（含
   "fomc_calendar:1996-07-03" 标签）。双失败形状（评审自建
   1996-07-04+2002-01-29 双缺口，复现裁定 §0.3 的 2/213 机制）：
   reason 恰引用**迭代序首个**失败标签（[e2,e1]→2002-01-30、
   [e1,e2]→1996-07-03，两种顺序均单标签）——与裁定 §0.3 预告行为
   逐字一致（真实计数归报告层，见 F2）。
3. **降级不掩盖——信息严格增加论证（三向对比复核）**：
   - vs 修复前反事实：bt.py:4975-4980 `_run` 包装直读证实——修复前
     整个 V3 关卡行退化为 ｛passed: False, error:
     "SignalUnavailable: event unavailable: exact window missing for
     fomc_calendar:1996-07-03"｝，**丢失全部四项诊断+主检验 p 值+
     五层否决明细**；修复后保留同一异常消息原文（reason）+其余三项
     完整诊断（year_block p_value/segmented_means 分段/known_gaps，
     评审运行时证实齐全）+完整判定。信息严格增加成立。惟一文本差：
     reason=str(e) 不含 error 行的 "SignalUnavailable: " 类型前缀
     （裁定 §2.1(i) 明文指定该形态；见 F4，非缺陷）。
   - vs 主检验域：一字未动（numstat=0+hunk 枚举仅 2 处），fail-closed
     域完整（见 G）。
   - vs "非 strict 计数理想"：不恢复=遵守裁定 §3.1（strict/非 strict
     混排属设计变更，不在授权内）。
4. **2/213 形状代表性**：合成测试（1 个假日命中事件+3 个全可解析
   事件）是裁定 §0.3 复现形状的最小合成版——机制（假日邻接 +1 平移
   →精确匹配失败→514 分支）与生产 213 事件集完全同源；规模差异
   （1/4 vs 2/213）不改变被测语义。评审另以双缺口形状补验多失败
   行为（见上）。

### B. 捕获面纪律——成立

- **单一 except ValueError**，无 bare/Exception 宽捕获（AST 全函数
  Try 节点枚举：函数内恰 2 个 Try，另一个为 known_gaps 段既有的
  `except (TypeError, ValueError)` 预存代码，零改动）。
- **window_shift 段异常类型枚举**（源码直读）：`shift_events_by_bars`
  （965-983）仅抛 ValueError（n_bars 校验——本段恒 n_bars=1 不可达；
  无正间隔——退化输入）；`event_returns`（458-560）strict 路径抛
  SignalUnavailable⊂ValueError（507/514）+ ValueError（553
  contract_size fail-closed、455 _side_sign 方向校验）；输出构造
  （int/float 强转）理论 ValueError。**非 ValueError 全部传播**：
  评审运行时实证——删 close 列输入使 `KeyError('close')` 自
  v3_event_diagnostics 原样穿透（不被降级吞没）。KeyError/TypeError
  等真实实现缺陷路径保持 fail-closed，符合 §2.1"禁止更宽捕获"。
- **except 块内无再抛可能**：块体仅 `str(e)`（对已捕获异常对象取
  字符串，不抛）+ 常量字典字面量 + 对普通 dict 的 setitem——无可
  抛调用点（AST 证）。

### C. 主检验域保护（(c) 组钉定）——成立

- 钉定测试 `test_strict_event_returns_still_raises_signal_unavailable`
  用 `ev.declared_event_side("static_long")`（DeclaredEventSide，
  **strict_windows=True 类属性 events.py:385**——与生产 bt.py:1558-1562
  event_setup 装配的 side_fn 同一类），直调 `event_returns` 分别命中
  507 分支（"window missing"：exit 越数据尾）与 514 分支（"exact
  window missing"：exit 落缺口日）——**两分支各一例，构造形状与
  生产主检验/随机化抽样的调用形状一致**；第三例直调
  `randomization_test`（生产主检验入口 bt.py:1568 同函数、无包装）
  断言 SignalUnavailable 传播。生产侧复核：bt.py:1568/1584 两处
  调用点均无 try/except，主检验 fail-closed 语义未被触碰。
- 该测试为**项目首批** strict_windows/SignalUnavailable 直接测试
  （裁定 §0.4 指出此前 tests/ 零覆盖）——主检验域语义从"仅生产
  代码承担"升级为"测试钉定"，是本次修复的正向外部性。

### D. 冻结面——独立核对全部通过

- 机器冻结注册表 `src/mt5quant/research/gate.py:49-61`
  `EXPECTED_ACCEPTANCE_SOURCE_SHA256` = 恰 5 文件
  （prob/level_decision.py、prob/tables.py、prob/decision.py、
  research/candidates.py、validation/prob_mode.py）——**events.py
  不在其中**；研究库同路径 gate.py 与 worktree 版 `cmp` **逐字节
  相同**；gate.py 全文 grep "events" 零命中。
- 项目冻结清单 docs/research/research-gate.md：冻结面为 D52 概率族
  假设冻结等，无 events.py。
- 台账钉定/身份四项**用生产函数独立重算**（worktree@ca626ae）：
  strategy_fingerprint=8742cd4f53ab5dc8…f1d780 ==台账 run#451
  .strategy_sha（`cards.strategy_fingerprint` 生产闭包解析，3 策略
  文件，events.py 不在闭包）；gates_sha256=d0a8b2f3105366e4…86bb17b
  ==台账（**注意：为 `vgate.gates_sha256` 的 LF 归一哈希**——裸字节
  sha256 为 9144cf5c…，评审已核生产实现为 CRLF→LF 归一后哈希，
  非异常）；R3.toml=72dc6403…（3805 B）/C005.toml=d00adad5…
  （11853 B）均逐位不变；ENGINE_VERSION=A-1.7（engine_a.py，未触碰）。
- §2.2 不得触碰面 7 项：hunk 枚举（events.py 全文件 diff 恰 2 hunk
  =docstring+window_shift 段）+ 全仓 name-only diff（恰 2 文件）
  + frozen-source-comparison.txt numstat 全 0 ——三重证实。

### E. 正常路径零变化——字节级+行为级双重证实

- 字节级：try 体 9 行去除 +4 缩进与父版 1051-1059 **逐字节相等**
  （含续行缩进整体平移，无任何字符改动）。
- 行为级（评审加强证）：父版/ca626ae 版两模块同进程、同输入、同
  seed（sims=50/seed=11）下 `v3_event_diagnostics` 完整输出 dict
  **精确相等（含键序与浮点位）**——strict 可解析形状 True、非
  strict lambda 形状 True；window_shift 正常键序
  [passed, net_mean, n_events, dropped, note] 与守恒
  n_events+dropped==len(evs) 均保持。R1 先例（非 strict dropped
  计数语义）由此钉死（裁定 §3.3-3/4）。

### F. 钉定测试 (b) 组——成立

`test_v3_event_diagnostics_window_shift_strict_normal_path_pinned`：
strict side_fn + 平移集全可解析 → 无 reason 键、
｛net_mean,n_events,dropped｝⊆键集、n_events+dropped==len(evs)
——与裁定 §4.2(b) 逐字对应；评审独立运行通过，且与 §2-E 行为级
比对互证。

### G. RED 证据真实性——成立

（见 §1-6。）补充：RED 期 27 deselected+3 选中 = 修复前测试文件已
含全部 3 条新测试（先写测试后改实现，TDD 时序由工件时间戳+行号
定位双重佐证）；RED 失败点=断言 1（诊断调用不抛异常）——失败原因
与裁定 §0.3 复现（SignalUnavailable 传播）完全一致，非超时/收集
错误等假 RED 形态。

### H. 全量失败集 vs run4 基线——成立

（见 §1-8。）环境归因独立复核：本机 C: 盘当前 4.5G free（<demo_run.ps1
预检 5G 阈值，与先例 engine-single-spec-review §5 的 4.7G/97% 同一
机器状态），12 项失败全部为磁盘预检类、全部位于
tests/test_demo_run_ps1.py，与本次改动无涉（该文件不 import
mt5quant.validation.events 的被改函数路径）。

### I. docstring+注释裁量（评审重点 6）——可接受（记为 F5）

- docstring：§2.1 明文可选授权"补一句降级说明"，实现加一句（2 行
  排版）且未改既有语义表述——**在授权内**。
- except 块 4 行中文注释 + note 尾部引用裁定文书：超出 §2.1 字面
  最小形态（year_block 先例无注释、note 更短），但 (i) 为非执行
  文本，位于授权段内，零语义面变化；(ii) note 仍以"只报告，不否决
  关卡结论"只报告语义领衔，裁定引用为附录性溯源；(iii) 实现方在
  EVIDENCE.md/commit 消息自披露该裁量。判定：自文档化取向、
  **可接受**，不构成 §7-3 越界（键集仍同构、无合成计数）。留痕
  见 F5（引擎代码内嵌治理文书引用的长期卫生问题）。

### J. 测试红线（§4.3）——遵守

测试文件零删改（numstat 120/0，现存 3 条 T86 诊断测试原样）；全部
断言针对引擎函数行为与 evaluate_v3 结构语义，**无任何对 run6 台账
行内容的预写期望**（逐断言直读证实）。测试名
`test_v3_event_diagnostics_window_shift_degrades_on_strict_holiday_hit`
与裁定 §4.1 指定名逐字一致，断言 1-5 逐条落实（含断言 5 以真实
`vgate.evaluate_v3` 对照 diagnostics=None 判定一致）。

## 3. 残留风险（F 编号，全部非阻断）

- **F1（证据透明度，小）**：REGRESSION-run-event-files.txt 只记录
  "161 passed"汇总，未留档 4 个事件相关文件的清单（从各文件
  collect 计数无法唯一重构该 161 的组成）。非阻断：全量套件
  （4181 passed）覆盖全部文件，评审已独立复现主文件 30/30。
- **F2（读账义务，裁定已预告）**：降级 reason 只记迭代序首个失败
  标签（评审双缺口实验证实）；生产形状真实计数 2/213 不在引擎记录
  内。**读 run6 台账 V3 行时不得把单标签误读为"仅一事件受影响"**
  ——报告层并列真实计数是裁定 §0.3 指定的离线义务，属 run6 报告
  步骤检查项，非本修复缺陷。
- **F3（捕获面固有性，授权形态内）**：except ValueError 同时覆盖
  段内非 SignalUnavailable 的 ValueError（如 553 contract_size
  fail-closed）——与 year_block 段捕获面完全一致，是裁定 §2.1
  指定形态的固有属性；非 ValueError 实现缺陷仍传播（KeyError 运行
  时实证）。
- **F4（文本细节）**：reason=str(e) 不含异常类型名（error 行会有
  "SignalUnavailable: " 前缀）——裁定 §2.1(i) 明文指定 str(e) 形态，
  类型信息由 docstring/note 语义补偿；信息总量仍严格增加。
- **F5（代码卫生，可接受裁量）**：except 块注释与 note 内嵌治理
  文书引用（2026-11-07-v3-diag-degrade-scope-ruling §2/§3.1）——
  若上游文书被修订/废止，引擎记录中的引用文本将成陈旧快照。判定
  可接受（§2-I）；未来同类改动建议 note 只留语义、出处留 commit
  消息/评审文书。
- **F6（机器状态，与本提交无关）**：本机 C: 盘 4.5G free<5G 阈值
  ——全量 12 项环境性失败将持续复现直至清理磁盘；run6 引擎路径
  不受影响（该预检属 demo_run.ps1 演练启动检查，非研究 gate）。

## 4. 结论

| 维度 | 结论 |
|---|---|
| §2 授权边界（含 §2.2 七项不得触碰面） | **PASS**（零越界；裁量项 F5 判可接受） |
| §4 TDD（RED/GREEN/回归 a-d/红线） | **PASS**（RED 真实；GREEN 与回归被评审独立复现） |
| 处置裁定 (c)4 修复形态约束 | **PASS**（最小 diff 同构降级；1996-07-03 确定性复现用例+降级不改变关卡结论断言均在） |
| §7 撤销条件 | **零触发** |
| **Spec** | **PASS** |
| **Quality** | **PASS**（F1-F6 非阻断；无 NEEDS_FIXES 项） |

治理链状态：第 3 步（本评审）完成 → 待第 4 步 A1 级整体裁量
（run6 授权+保活形态 P1-P4+P3 再终止条款）。本评审未触碰台账、
未跑 gate；worktree demo-review-diag-ca626ae 按指令保留。

## 5. 签署（占位）

- 评审者：独立聚焦评审者（2026-11-07；核验方法与全部中间产物见
  本记录 §0-§3）
- approved_by：____________（占位，主会话裁量；本评审记录不代填）

（评审只写本文件；不 commit，主会话集成。）
