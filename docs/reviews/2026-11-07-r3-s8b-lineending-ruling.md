# S8b A1 级裁定：R3 主检验 run3 H-08 换行阻断——工作树形态复原与 run4 授权（2026-11-07）

S8（docs/reviews/2026-11-07-r3-s8-rebind-ruling.md）的同一位 A1 独立裁定者
延续裁定。run3 按 S8 授权逐字执行（命令==run1 command.txt 原文逐字节、
64 字符日历 SHA、运行前核验全绿）后被 **H-08 轮次文件冻结核对机械拒绝**
（装配段之前、任何数据装载/runs 落账之前）。裁定者对本事件全程只读复核：
证据目录 7 文件全读、全部哈希/git 取证/时间线由裁定者独立重算、run4 路径
全部原始字节核对点穷举排查（发现实现方未点名的**第二处必炸点**，见 §2）；
未修改任何文件、未填 approved_by、未派子代理、未跑 gate。

**总裁定：APPROVED。** (a) 工作树换行复原属**形态复原**，不属 E1 意义上的
登记内容变更，授权经本裁定确认延续；(b) 复原边界=**恰两文件**
（research/rounds/R3.toml、research/candidates/C005.toml——后者是本裁定
独立排查发现的第二个原始字节钉阻断点，只复原前者 run4 会在下一道检查再被
拒）；(c) run4 仍以 run1 command.txt 原文为准；(d) 引擎不改，防复发走
.gitattributes（原则批准、聚焦评审、run4 之后落地）；(e) 复原不占"第三次
修复循环"，但 **run4 为本谱系最后一轮授权执行**。

## 0. 事件独立复核（不采信实现方断言，逐项重算）

- **执行忠实性**：run3 命令留档与 run1 command.txt **逐字节相同**（cmp
  实测）；日历 SHA=64 字符全文（S8 F-A 防线遵守）；cwd/解释器/PYTHONUTF8
  合规；主仓 HEAD=e159014==9374283+文档与 t121 旁线（bt.py 逐字节相同，
  C005/H 线冻结源 diff 为空——`src/mt5quant/prob/production_adapter.py`
  属 t121/P 线适配器常量重绑，不在 C005 装配/统计路径；run1/2/3 三次横幅
  策略指纹同为 8742cd4f…，旁证不变）。
- **拒绝事实**：bt.py（e159014 第 628 行）`file_sha = hashlib.sha256(
  rnd_path.read_bytes()).hexdigest()`——**原始字节**、无换行归一；工作树
  R3.toml=CRLF、3828 字节、sha `59113358…` ≠ 台账钉 `72dc6403…`。stdout
  4 行与源码拒绝文案一致；stderr 空；exit=1；发生点先于装配段（与 S8 F-A
  次序描述一致：CLI 日历 sha 消费在其后）。
- **零后果**：台账直查——runs=450（不变）、trial_reservations=0、gates
  max rowid=78（不变）、C005 最新行仍 rowid27 pass、R3 open/budget=2、
  ledger_events 无新行。预算机械口径 **0/2 未耗**。
- **预注册零变更**（本裁定独立重算）：git blob@HEAD==blob@d42471b==
  `72dc6403…`（LF、3805 字节）**逐字节==台账钉**；工作树 LF 归一化 sha
  ==钉；内容逐字相等（仅换行差异）。涉该文件的提交恰两个
  （5c93285→d42471b，即历史脱敏 rebase 的重放前后对）——与"重放触发
  checkout 物化"叙述自洽。core.autocrlf=true；`git ls-files --eol`=
  i/lf w/crlf；git status clean（autocrlf 往返使 LF/CRLF 两态对 git 均显示
  clean——这正是漂移不可被 git status 察觉的原因）。
- **时间线**：R3.toml、research/cards/H102.toml、research/candidates/
  C005.toml 三文件 mtime 同为 2026-11-07T02:41:29（C005.precheck.json
  02:41:38）——晚于 run2（01:05）、早于 fix2 s5-s7 产物与 S8 签署
  （06:14）。run1/run2 通过 H-08 证明其时工作树仍为 LF 态。R1/R2.toml
  另于 11-05T18:58 被同机制物化过（R1 已关闭、H-08 先查 status 不会走到
  文件哈希，故从未暴露）。具体触发命令无法从现有证据区分——实现方如实
  留档不猜测，裁定者认同该处理。

## 1. 事项 (a)：换行复原=形态复原，不属"新变更"——**裁定成立**

理由（四层）：
1. **权威内容=git blob==台账钉**：开轮入账的冻结对象就是这 3805 字节 LF
   内容；它自开轮以来零变更（blob 与钉逐字节相等）。变的是工作树对同一
   内容的**呈现形态**（autocrlf 物化），没有任何提交、没有任何内容差异
   （LF 归一化逐字相等）。
2. **E1 条款管的是登记内容**："任何变更即失效须重审重绑"的立法对象是
   C005/日历/预筛绑定输入等登记内容。本事件登记内容零变更；复原操作是把
   工作树**写回钉定字节**——使呈现层重新等于冻结层，方向恰与"改登记"
   相反。
3. **先例**：fix1-F1 日历工件换行修复（S4c/S5 批评审判定"内容零变、LF↔CRLF
   反推哈希数学证明"）——同为换行形态类事件按非内容变更处置。本事件比
   F1 更弱：连文件内容都未重写，只是 checkout 副作用。
4. **H-08 拒绝本身是设计正确的信号**：冻结核对按原始字节设计（fail-closed
   正确侧）；它报告的是"工作树形态≠冻结形态"这一真事实。修复侧应是工作树
   （复原到钉定字节），而非引擎（改哈希口径=弱化冻结源）或台账（换钉=
   改登记史）。

**条件**：复原须程序化断言（见 §2 协议），且仅限本裁定枚举的文件。若任一
文件 LF 归一化内容 ≠ 钉——那就是真内容变更，本裁定即失效、停。

## 2. 事项 (b)：复原操作边界——**恰两文件（本裁定独立排查结论）**

裁定者穷举 run4（bt gate）路径全部原始字节核对点（bt.py `read_bytes()`/
hashlib 全部位点 + 台账钉字段）：

| 核对点 | 文件 | 钉 | 当前实测 | 处置 |
|---|---|---|---|---|
| H-08 轮次冻结（bt.py:628） | research/rounds/R3.toml | `72dc6403…`（rounds 表） | CRLF `59113358…`，LF 归一==钉 | **必须复原** |
| H-08 候选冻结（bt.py:549，`_candidate_frozen_check`） | research/candidates/C005.toml | `d00adad5…`（rowid27 `candidate_sha256`） | CRLF `7bdbccc3…`，LF 归一==钉 | **必须复原**（实现方未点名；只复原 R3.toml 则 run4 在此再被拒） |
| 日历装载（bt.py:3791 `fomc_calendar_events(path, sha, …)`） | …/n1-fomc-calendar/fomc_meetings.json | `a53577d9…` | **实测==钉**（mtime 11-06 20:51，未被 02:41 事件触及） | 不动 |
| 候选草案（装配器侧钉；bt 门不直接消费） | …/n1-draft/C005.toml | `d00adad5…` | 实测==钉 | 不动 |
| 卡片（cards 表） | research/cards/H102.toml | 无字节钉（path+git_commit；行指向 e159014） | CRLF，LF 归一==blob | 不动（无钉消费；避免扩大操作面） |
| 策略指纹（bt.py:349） | 策略源码集 | 非门（随运行落台账） | 8742cd4f…（三次运行一致） | 不动 |
| 研究库六工件+日历导出 | data_research/… | 六钉 | run3 前全 MATCH（实现方证 + S8/本裁定抽验） | 不动 |

**复原协议（逐文件）**：
1. 前置断言：当前文件 LF 归一化 sha256 == 钉（证明形态复原前提成立）；
2. 写回：把 git blob 内容（LF 字节）写入工作树文件（`git cat-file` 取 blob，
   直接写盘）；
3. 后置断言：写回文件原始字节 sha256 == 钉；字节长度==blob 长度；
4. 证据留档：前/后 sha、长度、blob 来源（HEAD 与 d42471b 双验）、操作时间，
   存 run4 证据目录（研究库 sdd/2026-11-07-r3-main-test-run3/ 或新开
   run4 目录）；
5. **禁止**：git add/commit/stash/restore 等 index 操作（status 本就 clean，
   无需也不得动 index）；不得触碰两文件之外的任何文件；不得"顺手"重整
   仓库其他换行；.gitattributes 在 run4 落地**之前**不得加入（见 §4）。
6. 可选卫生项（非 run4 前提、不与 run4 混同一操作）：research/rounds/R1.toml、
   R2.toml 同协议复原至各自 blob（LF 归一==R1 台账钉 `58161509…` 已实测；
   R1 已关闭/R2 从未开轮，无消费者）；做不做均不影响本裁定。

## 3. 事项 (c)：run4 命令边界——**不变**

run4=run1 command.txt 原文重跑（同 S8 §5 全部边界逐字沿用）：bt.py gate
--card H102 --strategy …fomc_preannouncement:PreFomcAnnouncementDrift
--gates G0,G1,G2,G3,G4,V3 --event-kind fomc_calendar --fomc-calendar <主仓
日历工件> --fomc-calendar-sha256
a53577d96c699a1a6e7bab2bfb5f80d43408380ea3e2f48c7e88c1e76ec971c5（64
字符全文）--event-days-start 1986-01-02 --event-days-end 2021-05-17；
cwd=研究库工作区、PYTHONUTF8=1、解释器=研究库 .venv（-B）；主仓=当前
HEAD e159014 工作树（bt.py/H 线冻结源对 9374283 逐字节不变、对 run1 基线
0082b19 全程 diff 为空——两段均已裁定者复核）；复原后启动前把 §2 两文件的
后置断言输出并入运行前核验留档。预算：runs 落账计 1/2（机械口径，S8 §2
不变）；A1-D 节 15/213 子集分列、E7 charged 并列、E4/E5/E9/E10 义务全部
沿 S8 原文。

## 4. 事项 (d)：autocrlf/哈希口径治理归属与 .gitattributes——**引擎不改；
原则批准 .gitattributes（聚焦评审后、run4 之后落地）**

- **归属**：原始字节钉是冻结源的 fail-closed 设计（本轮事件证明它在按
  设计工作——发现的就是真差异），**引擎与台账侧一律不动**。治理在工作树
  形态侧：钉定文件必须以钉定形态（LF）呈现。登记侧工具链今后写此类文件
  一律 LF（S1-S4c 的程序已是如此——manifest/blob 均 LF）。
- **.gitattributes 原则批准**（主仓新增，建议内容）：

  ```
  research/rounds/*.toml      text eol=lf
  research/candidates/*.toml  text eol=lf
  research/cards/*.toml       text eol=lf
  .superpowers/h-new-candidates/** text eol=lf
  ```

  走聚焦评审（仓库配置变更；末行为日历/草案等被钉工件的防未来 checkout
  物化——日历本轮虽未中招，新克隆同险）。**时序：run4 之后**——避免在
  授权窗口内叠加仓库状态变更。**效力边界须记录**：attributes 只防未来
  checkout 物化漂移，既不检测也不修复现存 CRLF（检测层=原始字节钉，修复
  层=本裁定 §2 复原协议）；加 attributes 后 `git status` 对 CRLF 工作文件
  仍显示 clean（clean 过滤归一），不能当核对手损。

## 5. 事项 (e)：修复循环计数——**复原不占第三次修复循环；run4 为最后一轮**

- S8 的"最后一轮授权修复循环"约束的是**研究管线变更**（代码/绑定/登记
  内容）。换行复原是环境呈现层复原、经 A1（本裁定）逐文件确认，不属该类。
  但 run3 失败确实触发了 S8 撤销条件 2 并消耗了"回 A1"义务——本 S8b 即
  该回归的产物，且是**最后一次单点授权**。
- **run4=本谱系最后一轮授权执行**：若 run4 再次于 runs 落账前失败（无论
  何种原因），实现侧一律停在原地完整报告，**不得再有任何复原/修复尝试**；
  R3 的走向（继续/以从未执行关闭/其他）交主会话整体治理裁量——不再从本
  裁定链发放第四次单点修复授权。预算口径不变：run4 若执行并落账 runs
  →1/2；若再被拒 →仍 0/2（机械口径），但授权终止。

## 6. S8 裁定的缺口承认（如实记录）

S8 §0 核对 R3.toml 采用 **LF 归一化**口径（"盘上文件 LF 归一化 sha==台账"）
并判一致，未对 bt.py H-08 的**原始字节**口径做差异标记——而 CRLF 态在
S8 签署（06:14）前已存在（02:41），该阻断在授权时点即已潜伏。S8 六钉/
冻结源/rowid27 的其余全部核验不受影响（run3 前复核+本裁定重验均成立）。
本裁定以原始字节口径重验全部钉并穷举核对点（由此捕获 §2 第二处必炸点），
作为该缺口的对合（closing）。

## 7. 撤销条件（任一成立即失效）

1. §2 任一前置断言失败（LF 归一化 ≠ 钉=真内容变更）——停，转全量重审；
2. 复原触碰枚举两文件之外的任何文件，或伴随任何 git index/commit 操作；
3. run4 于 runs 落账前再次失败（→§5：授权链终止，回主会话整体裁量）；
4. 复原与 run4 之间任何钉/工件/台账变更（六工件钉、rowid27 最新性、
   五链完好、冻结源不变——S8 撤销条件 1/3/4/5 全文并入本条）；
5. 时间线取证被发现与"checkout 物化"叙述矛盾（如存在未披露的对此二
   文件的实际内容编辑）。

（裁定只写本文件；不 commit，主会话集成。）
