# T221 定盘锚点闸按币对清单执行：独立评审（2026-11-28）

- **评审对象**：分支 `t221-f3-data-qualification` 的提交 `b1654b1`（`b1654b1237d8888c7fba317be35fc3f7afb4348b`，代码和测试）和 `7370816`（`7370816cd51e47ea392f0511cc2a6e94f1307b74`，只改测试文件的模块 docstring），父提交 main `a122c78`（`a122c783fefaa3dcccb80711930b2e73e4057d86`）。它要补的是 F3 预注册草案 §3.3 第 1 条的缺口：`scripts/fetch_ext_data.py::_histdata_year_gate` 的定盘锚点闸只对名为 EURUSD 的币对执行，改成对一份显式清单执行（EURUSD 加 GBPUSD、USDJPY、USDCHF、AUDUSD）。`8283799` 和 `0a1c67d` 是数据资格报告，不在本次范围。评审开始时分支 HEAD 是 `8283799`；评审期间另一个子代理提交了 `0a1c67d`（只改报告文件）。被评审的六个代码和测试文件在 `7370816`、`0a1c67d` 和工作树里逐 blob 相同。
- **评审者**：独立评审子代理（模型 GLM-5.4，ZCode），没有参与实现。
- **依据**：`docs/research/f3-prereg-draft-2026-11-23.md` §3.1 第 2 步、§3.3 第 1、2 条、§10.1 第 9 项；`docs/handoff/2026-11-23-round23-plan.md` §6 第 2 条（D117）；`docs/research/ext-data-2026-10-27.md` §8.3、§9.2、§9.3（T94/R14-L4、L9 的规则）；`docs/reviews/2026-10-29-review-round14.md` 的 L4、L9。
- **评审方式**：读码；父提交的临时工作树上跑反事实；分支上串行跑点名的三个测试文件；系统临时目录里的探针（内存里对闸门函数做变异、合成样本上比对抽稀前后、假下载函数加临时目录里的端到端）。没有跑全量，没有读任何真实价格数据，没有计算任何策略收益。
- **记录时间**：2026-11-28 08:57 UTC，取自系统时钟。文件名里的日期按本地日期。
- **路径约定**：`<工作树>` 是被评审的工作树；`<父提交工作树>` 是我为反事实建的临时工作树（在 `<工作区>` 下，用完已删）；`<工作区>` 是工作区根目录；`<主工作树>` 是主工作树，共享的 venv 在那里；`<评审临时目录>` 是我在系统临时目录下建的目录，探针脚本和 P3 留下的临时数据都在里面（用完已删，没有进仓库）；`<系统临时目录>` 是系统自己的临时目录。
- **证据级别**：**已验证** = 我亲自运行并看到；**读码结论** = 只读代码得出；**未验证** = 没有验证。
- **评审不等于批准**：合入由主会话按流程做。本记录不设批准字段。
- **结论：PASS**（对 `b1654b1` 和 `7370816` 的代码和测试）。没有阻断合入的项，也没有合入前必须改的项。改动是一行判断加一个常量，锚点规则逐字未变，清单外的品种照旧不执行锚点闸；31 条新用例在父提交上 25 红 6 绿，红的里面 8 条是"该排除的年份被启用"的行为红；内存里的 11 个变异杀死 10 个。存活的那个说明 L9 的"回收年用回收子集测锚点"没有被用例钉住，行为本身是对的，列在 §6 的建议里。PASS 只针对这份代码和测试：四个新币对的数据能不能用，还卡在草案 §10.1 第 9 项的裁定和资格报告的评审上，见 §7。

## 1. 改动范围与逐项核对

**只做了这件事**。已验证。`git diff --numstat a122c78 7370816`：`scripts/fetch_ext_data.py` +22 / −8，`tests/test_t221_anchor_gate_pairs.py` +281 / −0（新文件），没有别的文件；`src/`、`config/`、`docs/` 下为空。`7370816` 只改测试文件的模块 docstring（+5 / −4），用例代码没动。脚本的改动分四处：新常量（`:87–91`）、`_histdata_year_gate` 的判断那一行（`:184`）和它的 docstring（`:125–128`）、模块 docstring（`:22–26`）、`cmd_histdata` 开头的打印（`:206–211`）。

| 核对项 | 结果 | 证据 |
| --- | --- | --- |
| 清单常量的内容和拼法 | `HISTDATA_FIX_ANCHOR_PAIRS = frozenset({"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD"})`，与草案 §2.1 的五个币对逐字一致。全大写、没有空白；是不可变的集合 | `scripts/fetch_ext_data.py:90–91`；`test_anchor_pair_list_is_explicit`；变异 M1、M1b 被杀（§5） |
| 锚点规则一个字没变 | `_histdata_year_gate` 的可执行部分在两个提交之间只差一行：`if pair.upper() == "EURUSD":` 换成 `if pair.upper() in HISTDATA_FIX_ANCHOR_PAIRS:`。锚点块 `:185–197`、口径检测和月回收 `:139–183` 逐字相同 | 两版函数体去掉 docstring 后 diff 只有这一行（已验证）；`src/mt5data/ext/` 下的文件不在改动清单里 |
| 只有 `inconsistent` 才排除 | `:188–192` | `test_inconsistent_anchor_year_excluded_for_new_pair` ×4、`test_salvaged_year_anchor_inconsistent_excluded_for_new_pair` ×4；变异 M7 被杀（13 条红） |
| `undetermined` 不排除，`anchor_note` 写进记录 | `:193–197` | `test_undetermined_anchor_year_used_with_note_for_new_pair` ×4，EURUSD 的 `test_eurusd_behavior_unchanged[None]`；变异 M3（undetermined 也排除）、M4（不写 anchor_note）各被 5 条杀死 |
| 回收年用回收子集测锚点 | 锚点调用在回收拼接（`:166–183`）之后，读的是 `s.df` | 行为已验证：探针 P1、P2d 里回收年的锚点只读到回收月份里的月末（6 个，1080 根）。用例只钉到"回收年跑了锚点闸"，没有钉住"用的是回收子集"（变异 M5 存活），见 S1 |
| 清单外品种不执行锚点闸 | XAUUSD、USDCAD 不在清单里 | `test_pair_outside_list_skips_anchor_gate` ×2；变异 M2 被杀；探针 P3 的 S5 |
| `approvals.py` 没有被改 | 实现方的说法属实 | `git diff a122c78 7370816 --name-only` 只有上面两个文件（已验证） |
| 文字与行为一致 | 模块 docstring 写的是"清单外品种只过口径闸"；函数 docstring 和常量注释列的是同样的五个币对；`cmd_histdata` 的打印从常量取币对名，实际输出是"定盘锚点币对（AUDUSD/EURUSD/GBPUSD/USDCHF/USDJPY）" | 读 diff；探针 P3 的 S1 抓到的实际输出 |
| 没有把"批准是否覆盖四个新币对"写成已定 | 模块 docstring `:24–26` 写的是"批准文本是否覆盖四个新币对，待外部审核裁定（F3 预注册草案 §10.1 第 9 项），本脚本不就此作结论"；提交说明 `b1654b1` 同样只写"没有动" | 读 diff 和提交说明 |

## 2. 反事实。已验证

- **命令**：`git worktree add <工作区>\t221-review-parent a122c78 --detach`；`git show 7370816:tests/test_t221_anchor_gate_pairs.py > <父提交工作树>\tests\test_t221_anchor_gate_pairs.py`。
- **身份核对**：`<父提交工作树>` 的 HEAD 是 `a122c78`；换入后测试文件的 blob 与 `7370816` 里的相同（`d9009f0…`）；`scripts/fetch_ext_data.py` 的 blob 与 `a122c78` 的相同（`dd730d7…`）、与 `7370816` 的不同（`0755e8a…`），里面没有 `HISTDATA_FIX_ANCHOR_PAIRS`，`:172` 仍是 `if pair.upper() == "EURUSD":`；`src/mt5data/ext/consistency.py` 与 `7370816` 的相同。工作树里只有这一个未跟踪文件。
- **运行**：`cd <父提交工作树>` 后 `<主工作树>\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_t221_anchor_gate_pairs.py` → **25 失败、6 通过**（53.8 秒）。同一个文件在 `<工作树>` 上 → **31 通过**（63.8 秒）。实现方报的两个数都对。
- **导入来源**：两个工作树里，`mt5data`、`mt5data.ext.*`、`tests.test_ext_data` 和被加载的 `scripts/fetch_ext_data.py` 都来自各自的工作树，不是 `<主工作树>` 的 `src`（已验证，见 §10 的插件）。
- **25 条红逐条看过**，分三类：

  | 类别 | 用例 | 父提交上红在哪 |
  | --- | --- | --- |
  | 行为红（8 条） | `test_inconsistent_anchor_year_excluded_for_new_pair` ×4 | `:175`，`s is None` 不成立：记录是 `used: True, calendar: us_wall`，该排除的年份被启用了 |
  | | `test_salvaged_year_anchor_inconsistent_excluded_for_new_pair` ×4 | `:214`，同上，记录是 `used: True, calendar: inconsistent`，回收后的年份被启用了 |
  | 记录红（16 条） | `test_consistent_anchor_year_used_for_new_pair` ×4 | `:190`，`KeyError: 'fix_anchor_month_end'`，锚点闸没有跑 |
  | | `test_undetermined_anchor_year_used_with_note_for_new_pair` ×4 | `:201`，同上 |
  | | `test_salvaged_year_anchor_consistent_used_for_new_pair` ×4 | `:232`，同上 |
  | | `test_new_pair_anchor_record_equals_eurusd_on_same_sample` ×4 | `:247`，同上 |
  | 名字红（1 条） | `test_anchor_pair_list_is_explicit` | `:164`，`AttributeError`，常量不存在 |

  所以那 25 条红钉住的是行为：8 条直接钉"该排除的年份被启用"，16 条钉"锚点闸对新币对没有跑、记录里没有锚点字段"，那是同一个缺陷从记录这一侧看过去的样子。只因为新名字不存在而红的只有 1 条。
- **6 条改前改后都绿**的是回归护栏：抽稀一致性 1 条、EURUSD 三种结果 3 条、清单外两种 2 条。它们在父提交上绿是对的。
- **内存里复现了反事实**：把常量改回只有 EURUSD、闸门函数不动，直接调用用例，得到同样的 25 红 5 过（§5 的 M1）。

## 3. 对照依据

| 依据 | 要求 | 落点 | 判定 |
| --- | --- | --- | --- |
| 草案 §3.3 第 1 条 | 锚点闸改成按币对清单生效，带测试和独立审核 | 常量 `:90–91`；31 条用例；本评审 | 满足 |
| R23 §6 第 2 条（D117） | 窗口未起算期间下载四个币对，做逐年口径闸门和定盘锚点闸；每个新品种的资格结论要独立审核 | 清单恰好是这四个加 EURUSD | 满足。这份代码对四个新币对执行锚点闸的授权出处就是这一条 |
| 草案 §3.1 第 2 步 | 月末口径；核心窗 [15:57, 16:03) 伦敦墙钟，对平移 ±1 小时的对照窗；落在别的小时是 `inconsistent`，排除；分不出是 `undetermined`，不排除 | `:185` 调 `cons.fix_window_vol_spike(s.df, month_end_only=True)`，函数和它的默认值都没动 | 满足（代码逐字未变） |
| ext §8.3、R14 L4 | 只有 `inconsistent` 才排除；`undetermined` 以标签规则检测为准入库，manifest 注明 | `:188–197` | 满足 |
| ext §9.3、R14 L9 | 年判 inconsistent/undetermined 的年份按月回收，回收子集再测锚点 | `:156–185` | 代码满足；用例没有钉住"子集"，见 S1 |
| R14 L4 的附注 | 这类校验只用与方向无关的结构量，不计算与假设方向相关的统计量 | 锚点函数只算分钟收益绝对值的均值 | 满足（未变） |
| 草案 §3.3 第 2 条、§10.1 第 9 项 | 批准是否覆盖四个新币对，待审核确认 | `approvals.py` 没有动，脚本 docstring 写明待裁定 | 满足：代码没有越权下结论。裁定落地时要注意的事见 N1–N3 |

## 4. 不许变的东西没变

| 不许变的 | 核对 | 证据 |
| --- | --- | --- |
| 锚点块、口径检测、月回收 | 逐字相同 | §1 第二行 |
| `consistency.py`、`adapters.py`、`approvals.py`、`core.py` | 没有动 | 改动清单里没有；`approvals.py` 的 blob 在 `a122c78`、`7370816`、`0a1c67d` 相同 |
| `cmd_histdata` 的循环：跳过已存在的年份、pace、全量重建 | 只改了开头那一段打印 | 函数体 diff 只有 `print` 那一处；探针 P3：S3a 不加 `--force` 再跑，下载函数一次都没有被调用；S3b 加 `--force` 重下；S3a、S3b 都是从原始 zip 全量重建后落盘，不与旧序列合并。pace 是读码结论：`:219–220` 没动 |
| EURUSD 的三种结果 | 改前改后都绿 | `test_eurusd_behavior_unchanged` ×3；`tests/test_ext_data.py` 50 条全过 |
| XAUUSD 不执行锚点闸 | 改前改后都绿 | 清单外两条用例；`test_xauusd_skips_fix_anchor` |
| 小写、带空白的币对名 | `cmd_histdata` 先 `strip().upper()` | 探针 P3 的 S6：`--pairs ' gbpusd'` 照样按 GBPUSD 过锚点闸 |
| `config/`、`docs/`（本文件除外） | 没有动 | 改动清单里没有 |

## 5. 测试质量

- **抽稀样本不会造成假绿或假红**。已验证（探针 P1）。闸门只读两类 bar：周开盘和换日空档后的首根（都在整点，在抽稀网格上），以及每个月最后一个伦敦日期下午 [14:00, 17:00) 的 bar（抽稀时整段保留 M1）。所以：
  - 口径检测读到的观测集合，整年 M1 和抽稀两份样本逐条相同、顺序也相同（us_wall 标签 260 个，mixed 标签 261 个）；逐年和逐月判定的完整返回值相等。抽稀改变的只是众数步长（1 分钟变 15 分钟）和空档下限（30 分钟变 45 分钟），换日空档在抽稀样本里是 75 分钟，仍然被认出来。
  - 锚点读到的 bar 集合完全相同（us_wall 标签 1620 根，mixed 标签 1080 根）。
  - 五种样本组合（us_wall 标签乘三种尖峰位置、mixed 标签乘两种）下，`used`、`calendar`、三种规则的偏差数、容差、转换版本、回收标记、回收月份、逐月判定、锚点的结论、位置、观测数、`reason`、`anchor_note` 逐项相等。实测均值的相对差最大 0.33%（us_wall）和 0.83%（mixed），用例放宽到 5%。
- **"整年样本对抽稀样本"那条用例证明了什么**。它只覆盖五种组合里的一种（us_wall 标签、尖峰在 C−1h、EURUSD），在那种组合上证明判定逐项相等。它自己没有断言判定的绝对值（没有写 `status == "inconsistent"`）；但 M1 那一份如果没被排除，`rec_m1["reason"]` 就会 `KeyError`，所以它隐含要求 M1 样本被排除；抽稀样本的绝对判定由其它用例钉住。合起来是成立的。它的局限是没有覆盖回收年（见 S3）。
- **`undetermined` 这条路径钉住了**。四个新币对各有用例：年份启用、`fix_anchor_month_end.status == "undetermined"`、`anchor_note` 含"undetermined"、没有 `reason`。变异 M3（undetermined 也排除）和 M4（不写 anchor_note）各被 5 条杀死。
- **回收年这条路径有用例，但没有钉住"回收子集"**。`test_salvaged_year_anchor_inconsistent_excluded_for_new_pair` 和 `…_consistent_used…` 各 4 条，钉的是：年判 inconsistent 的年份先回收（`salvage` 为真、回收月份里没有被丢掉的 7–10 月）、回收后跑了锚点闸、inconsistent 排除、consistent 入库且入库序列不含被丢掉的月份。但样本里的尖峰放在每一个月末，回收子集里有、整年里也有，所以分不出锚点看的是哪一份。变异 M5（回收年的锚点改看整年按 us_wall 转换的序列）在这 30 条用例上一条都没有杀死。P2d 用一份区分样本证明 M5 不是等价变异，见 S1。
- **变异矩阵**。已验证（探针 P2，内存里改，不动文件；直接调用用例，30 条快的，另有一条 22 秒的只对 M9 跑）：

  | 变异 | 结果 |
  | --- | --- |
  | M0 对照，不变异 | 30 通过 |
  | M1 清单退回只有 EURUSD（就是父提交的行为） | 25 红 5 过，与 §2 的 25 条是同一批 |
  | M1b 清单少了 USDCHF | 7 红（清单用例，加 USDCHF 的 6 条） |
  | M2 不看清单，所有品种都执行 | 2 红（XAUUSD、USDCAD） |
  | M3 undetermined 也排除 | 5 红（四个新币对的 undetermined，加 EURUSD 的） |
  | M4 不写 anchor_note | 5 红（同上） |
  | M5 回收年的锚点改看整年 | **0 红，存活** |
  | M6 不写锚点记录 | 27 红，只剩清单用例和清单外两条通过 |
  | M7 inconsistent 也不排除 | 13 红 |
  | M8 consistent 的年份也写 anchor_note | 5 红（四个新币对的 consistent，加 EURUSD 的） |
  | M9 锚点不用月末口径 | 1 红，只有抽稀一致性那一条，红在 `observations` |
  | M13 清单判断取反 | 29 红，只剩清单用例通过 |

- **从另一个测试模块导入辅助函数**：先例很多（`tests/test_p10_cross_line.py:37` 等）。只导入了 `REPO_ROOT`、`load_script`、`_synth_weeks_utc`、`_labels_for_rule` 四个名字，没有导入用例，所以不会被收集两遍（实收 31 条）。加载脚本用的模块名 `fetch_ext_data_t221` 与 `tests/test_ext_data.py` 里的六个都不同。
- **脆不脆**：断言落在可观察的行为上。`fix_anchor_month_end`、`anchor_note`、`salvaged_months` 这些键是 manifest 的字段（`scripts/h_assemble_real_precheck.py:252` 读前者），钉它们不算实现细节。`location == "minus_1h"` 和 `reason` 里的"inconsistent"字样是函数的既有可能取值，既有的 EURUSD 用例也这么断言。`test_anchor_pair_list_is_explicit` 钉的是清单的确切内容，以后增删币对必须改它，这是有意的。没有随机性，不会飘。
- **运行时间**：全文件 63.8 秒，其中抽稀一致性那一条 22 秒。单次闸门在整年 M1 样本上约 19–21 秒（含合成和打包），在抽稀样本上约 1.5 秒（探针 P1）。实现方说的"抽稀前约 8 分钟"我没有实测。

## 6. 发现的问题

严重程度三档：阻断合入 / 合入前必须改 / 建议。**没有阻断合入的项，也没有合入前必须改的项。**

### 建议

- **S1（建议）"回收年用回收子集测锚点"没有被用例钉住。** 已验证（变异 M5、探针 P2d）。
  - 把回收年的锚点改成看整年按 us_wall 转换的序列，新文件的 30 条用例全过。
  - 区分样本：mixed 标签，定盘窗 [15:57, 16:03) 的尖峰只放在被丢掉的 7–9 月的月末日下午。真实代码判 `undetermined`、年份入库、带 `anchor_note`（锚点读 6 个月末、1080 根）；M5 把那三个月的标签按 us_wall 转换，得到的时刻比真实早一小时，尖峰落到 minus_1h，判 `inconsistent`、整年排除（余量 7.33，读 1452 根）。两份镜像样本（尖峰只在回收月份）和新文件现有的样本上，两者判定相同。
  - 建议补一条用例，就用这份区分样本，断言 `used` 为真、锚点 `undetermined`、有 `anchor_note`。构造只需在 `_year_zip` 里给尖峰加一个月份过滤。既有的 `tests/test_ext_data.py` 里，回收年的用例都用 `pair="XAUUSD"` 以避开锚点闸，所以这条规则在 EURUSD 上也从来没有被用例钉住过。不是这次引入的回归。
- **S2（建议）回收年加 `undetermined` 的组合没有用例。** ext §9.3 的实测里，EURUSD 2024 年正是这一种（年判 inconsistent、回收 11 个月、锚点 undetermined、按 L4 入库）。行为是对的（探针 P3 的 S7b，GBPUSD 端到端：入库，`anchor_note` 在）。补一条 `_year_zip("mixed", None)` 的用例即可。
- **S3（建议）抽稀一致性只覆盖五种组合里的一种。** 建议把回收年（mixed 标签）那一种也纳入比对，至少纳入无尖峰的那一种。五种我都比过了，一致（§5）。
- **S4（建议）月末口径只被间接钉住。** `month_end_only=True` 被改掉时，只有抽稀一致性那一条会红（M9），因为所有判定用例的尖峰都放在每一天。读码结论：`tests/test_ext_data.py` 里也是如此。要想直接钉住，需要一份"月末的尖峰在定盘窗、其余日子的尖峰更强且在 C−1h"的样本。不是这次引入的。
- **S5（建议）过期的注释和 docstring，不在本次改动里。** `tests/test_ext_data.py:1047`、`:1191`、`:1211` 还写着"EURUSD 另加""锚点闸只加给 EURUSD"；`src/mt5data/ext/consistency.py:499`、`:514–515` 的 docstring 还写着"EURUSD 启用条件"。用例本身仍然成立。提交说明里"说明文字跟着改"覆盖的是脚本，这几处下次动到时顺手改。
- **S6（建议）清单在脚本里手写了四遍**：常量、模块 docstring、函数 docstring、常量上面的注释。打印是从常量取的。以后增删币对时四处要一起改，漏一处不会被发现。
- **S7（建议）合成样本里锚点只读到 9 个月末。** 已验证（探针 P1）。`_synth_weeks_utc` 的周从周日 17:00（纽约）开市，2021 年 1 月 31 日、2 月 28 日、10 月 31 日都是周日，这三个月"样本里最后一个伦敦日期"落在周日晚上，下午的搜索窗里没有 bar。整年 M1 和抽稀两份样本都是 9 个，回收年是 6 个。不影响判定（余量约 20），但"每个月"的说法与实际差三个月。既有的 EURUSD 用例是同一个样本。
- **S8（建议）没有经 `cmd_histdata` 的用例。** 新文件直接驱动 `_histdata_year_gate`。改动的那段打印、manifest 里 `yearly_verification` 带不带锚点字段，我用探针 P3 在新币对上端到端核对了，都对（§8）。要补的话可以用假的下载函数加 `tmp_path`。

## 7. 主会话须知

这些不是这份代码的缺陷，是读码和探针看到的、与草案 §10.1 第 9 项的裁定和后面的资格核验有关的事。

- **N1 新币对的 manifest 会带着"EURUSD 另加定盘锚点"的文字。** 已验证（探针 P3 的 S2）。GBPUSD 入库后，manifest 的 `usage_restriction` 和 `terms_note` 里写的仍是"只启用逐年时间口径验证通过的年份（EURUSD 另加伦敦 16:00 定盘窗……）"，不提 GBPUSD；而实际对这个币对执行了锚点闸（`yearly_verification` 里有锚点记录）。文字比实际做的保守，不是放宽。出处是 `src/mt5data/ext/approvals.py:42–49`、`:85–88` 和 `src/mt5data/ext/adapters.py:46–55`，都是批准文本，实现方没有动，等裁定，这是对的。
- **N2 导出不按品种拦。** 读码结论。`check_exportable`（`approvals.py:116–142`）只查来源在白名单里、`usage_restriction` 与批准表逐字一致，不看品种。所以"批准是否覆盖四个新币对"在代码里没有任何东西拦着，只能靠流程。裁定下来之前，不要让四个新币对的 histdata 序列进入研究库导出。
- **N3 裁定之后改批准文本，代价比看起来大。** 读码结论。`check_exportable` 逐字比对：批准文本一改，库里现有的 EURUSD、XAUUSD 的 histdata manifest 不重建就会被拒绝导出；重建之后 manifest.json 的字节变了，而 `scripts/h_assemble_real_precheck.py:52–67` 的 `REAL_WHITELIST` 按 sha256 钉着 EURUSD H1 的 manifest.json，要重新钉。所以 §10.1 第 9 项落地不是改一句话，需要单独立项。
- **N4 下载失败、一个 zip 都没有时的那句打印。** 已验证（探针 P3 的 S4）。输出是先"GBPUSD 2021 下载失败：……"，再"GBPUSD：无可用年份（全部被逐年闸门拒绝），不落盘"，两行开头各带脚本自己的叉号标记，返回 1。后一句不实：没有任何年份进过闸门。实现方已披露，不在这次改动里。资格报告记录的那次下载失败如果重跑，看到的就是这两行，读日志的人容易误读成闸门拒绝了年份。
- **N5 分支与 main。** main 在 `90a33ae`，比分支基点 `a122c78` 多 1 个提交，与本次改动的文件不重叠。评审期间分支多了报告的两个提交（`8283799`、`0a1c67d`），只改 `docs/research/f3-data-qualification-2026-11-28.md`。
- **N6 新币对在真实数据上的锚点表现没有验证。** 没有数据。按 L4，月末定盘尖峰弱的币对会有更多年份判 `undetermined` 并入库，这些年份的标签对齐只靠口径检测，草案 §3.1 第 2 步已经写明"只能证伪、不能证实"。每个币对有哪些年份是 `undetermined`，资格报告要如实列（草案 §10.2 第 1 项）。常量注释里"伦敦 16:00 WMR 定盘窗对这些币对同一时刻"与仓库的设计文档一致（T155 §3 F3：EURUSD 之外的锚点窗同理可用月末定盘尖峰核对；草案 §2.2 五个币对同在伦敦 16:00 出场）；市场事实本身我没有独立核实。
- **N7 `_histdata_year_gate` 的一个既有边界。** 读码结论。锚点结果里没有该年份的条目时，`:187` 的 `status` 是 `None`，两个分支都不进，年份照常启用，记录里 `fix_anchor_month_end` 是 null，也不写 `anchor_note`。正常的年度 zip 走不到这里（有回收月份的年份一定有 bar；搜索窗里观测不足时函数也返回一条 `undetermined`，不是不返回）。清单扩大之后经过这段代码的年份多了，登记备查。

## 8. 探针与未验证事项

探针都在 `<评审临时目录>` 里，只操作内存里的合成数据和 `<评审临时目录>` 下 P3 自己建的子目录，不读任何真实数据。每个探针开头装两个守卫：meta_path 最前面一个 finder，对 MetaTrader5 的 import 在找到模块之前就拒绝并计数；`socket` 的连接和域名解析一律拒绝并计数。每次运行结束打印的两个计数都是 0。

| 探针 | 做了什么 | 结果 |
| --- | --- | --- |
| P1 | 抽稀样本对整年 M1：口径观测集合、逐年逐月判定、锚点读到的 bar、五种组合的闸门判定 | 见 §5 第一条。合成样本 355,732 根，抽稀后 25,276 根（7.1%） |
| P2 | 11 个内存变异加 1 个对照 | 见 §5 的变异矩阵 |
| P2d | M5 的区分样本和两份镜像样本 | 见 S1 |
| P3 | 经真实的 `cmd_histdata`，下载函数换成假的，`--ext-root` 指向 `<评审临时目录>` 下的子目录，每个场景一个新目录 | S1 GBPUSD 锚点 inconsistent：那一年"登记后不用"，返回 1，只留原始 zip。S2 GBPUSD undetermined：入库 M1/H1/D1，manifest 的 `yearly_verification` 里锚点是 undetermined、有 `anchor_note`。S3a、S3b：跳过已存在的年份、`--force` 重下。S4：见 N4。S5 XAUUSD：入库，记录里没有锚点字段。S6：小写币对名。S7a、S7b：GBPUSD 的回收年，inconsistent 排除、undetermined 入库，打印里有"回收 8 个月" |

未验证：

- 四个新币对的真实年度 zip 没有跑过闸门（没有数据）。真实数据上锚点、口径检测和月回收的表现不在本次评审能验证的范围里。
- `cmd_histdata` 的 pace 没有运行验证，是读码结论（那两行没动）。
- 数据资格报告（`8283799`、`0a1c67d`）没有评审，按安排另做。
- CI 的结果没有看。点名之外的测试文件没有跑。
- 抽稀之后，每个月末搜索窗第一分钟（伦敦 14:00）的收益是跨最多 15 分钟算的，不是 1 分钟。它不在三个窗口里；会不会影响 `peak_minute_london` 和 `fix_vs_window_median` 两个字段，没有测（这两个字段没有用例断言）。

## 9. 结论

**PASS**（对 `b1654b1` 和 `7370816` 的代码和测试）。

改动只做了它说要做的事，锚点规则逐字未变，文字没有越权下结论。新用例在父提交上钉住了行为，内存变异里与本次改动有关的都被杀死。没有阻断合入的项，也没有合入前必须改的项。八条建议里最值得补的是 S1（回收子集）和 S2（回收年加 undetermined），各一条用例。

PASS 不等于批准，也不等于四个新币对的数据可以用。后者还要：草案 §10.1 第 9 项的裁定（N1–N3 是它落地时要注意的事）、资格报告的评审、D60 要求的逐币对独立审核。

## 10. 执行过的命令与限制核对

**git**

- 只读：`git status --short`、`git branch --show-current`、`git log --oneline`、`git show --stat`、`git show`、`git diff a122c78 7370816 -- …`（`--stat`、`--numstat`、`--name-status`、`--name-only`）、`git diff 7370816 HEAD --stat`、`git diff --stat a122c78 main -- …`、`git worktree list`、`git rev-parse`、`git rev-list --count`、`git hash-object`、`git merge-base`（含 `--is-ancestor`）、`git grep`、`git ls-files`、`git config --get core.autocrlf`。对 main 只做过只读的比较。
- 写入只有三件：建临时工作树 `git worktree add <工作区>\t221-review-parent a122c78 --detach`；用 `git show 7370816:<路径> >` 往里换入一个测试文件；`git worktree remove --force` 同一个路径。删除前核对过：它是工作树列表里 HEAD 为 `a122c78` 的 detached 项，里面只有那一个未跟踪文件，没有缓存目录。删除后路径不存在，`git worktree list` 里没有它，主仓库 `.git/worktrees/` 下的对应目录也没有了。别的工作树没有碰。
- 本文件是唯一新建的文件，提交在分支 `t221-f3-data-qualification` 上、在当时的 HEAD（`0a1c67d`）之上，是新的提交，只含这一个文件。没有 amend，没有 rebase，没有 add 或 commit 别的文件，没有 push，没有对 main 做任何写操作，没有改写历史，没有做合并预演。

**pytest**（都是 `cd` 到对应工作树后 `<主工作树>\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider` 加明确的文件名；共享的 venv 里没有装并行插件，全程串行，一次只有一个 pytest 进程；`-B` 和 `-p no:cacheprovider` 是为了不在工作树里写字节码和缓存）

1. `<父提交工作树>`：`tests/test_t221_anchor_gate_pairs.py` → 25 失败、6 通过。
2. `<父提交工作树>`：同一文件，`-k "test_eurusd_behavior_unchanged"` → 3 通过、28 条不选。这一条是为了补导入来源的证据。
3. `<工作树>`：`tests/test_t221_anchor_gate_pairs.py` → 31 通过。
4. `<工作树>`：`tests/test_fred_yearly_verification.py` → 22 通过。
5. `<工作树>`：`tests/test_ext_data.py` → 50 通过。
6. `<工作树>`：`tests/test_repo_hygiene.py` → 11 通过。这一次在本文件 `git add` 之后、提交之前跑，为的是用仓库自己的敏感信息扫描核对本文件（它只跑一次只读的 `git ls-files`，再读文件）；结果零违规。这个文件是主会话没有点名的，是我自己加的。

每次之后 `git status --short` 都干净（本文件写好之后是只有本文件未跟踪）。没有跑全量，没有做压力实验。pytest 自己在系统临时目录下建的用例目录由它轮换，我没有手动删。

**MetaTrader5**：没有任何一次 `import MetaTrader5`。pytest 的六次都经过 `tests/conftest.py` 的守卫（真实包被加载就直接抛错）。第 2–5 次另带我自己的插件（`-p t221_where`，放在 `<评审临时目录>`，不进工作树），它在 meta_path 最前面计数并拒绝，结束时打印的尝试次数都是 0；第 1 次用的是这个插件的第一版，计数打印被 pytest 的捕获吞掉了，但拒绝是生效的，那一次的失败类型只有 `AttributeError`、`AssertionError`、`KeyError`。探针的计数见 §8。第 6 次（卫生测试）没有带插件，那时 `<评审临时目录>` 已经删了，只有 conftest 的守卫。几次内联脚本没有装守卫，它们只导入标准库、pandas 和被测的模块。没有启动、没有停止 `terminal64.exe`，没有碰终端的数据目录。

**外部请求**：没有发任何外部请求。没有运行 `scripts/fetch_ext_data.py` 的任何子命令（`inventory` 也没有跑）；探针 P3 是在自己的进程里直接调用 `cmd_histdata`，下载函数换成假的，`socket` 的连接被守卫拒绝并计数（0 次）。没有向任何外部站点发请求，没有做网络检索。

**计划任务和进程**：没有注册、注销、启用、停用、运行、查询任何任务。没有启动、结束任何业务进程：`terminal64.exe`、halt 状态的观察运行、另一个子代理的下载都没有碰。我自己起的只有 git、grep 这类只读命令，以及 pytest、ruff 和 python 探针。

**安装**：没有 `pip install`，没有 `pip upgrade`，共享的 venv 没有任何改动（python 都带 `-B`）。

**文件**：除本文件外没有新建或改动任何文件。`scripts\`、`src\`、`tests\`、`config\` 只读；`docs/research/f3-data-qualification-2026-11-28.md` 没有碰；`<主工作树>` 下除 `.git` 里临时工作树的登记（已随删除清掉）之外没有写任何东西；`data\live`、`data\demo`、`data\ext` 没有读也没有写。没有读任何真实价格数据，没有计算任何策略收益。

**ruff**：`<系统临时目录>\zc-ruff-isolated\wheel\bin\ruff.exe check --no-cache` 加两个改动的文件（版本 0.17.6，`All checks passed!`）。`--no-cache` 是为了不在被评审的工作树里写缓存。

**探针**：一个 pytest 插件和四个探针脚本，连同 P3 留下的临时数据，都在 `<评审临时目录>`（输出只打印在我的终端里，没有落盘）。运行 11 次：P1 三次，前两次因为探针里我自己的错跑到一半中止；P2 四次（三批，外加一次单独重跑 M6 以记下它的汇总行）；P2d 一次；P3 三次，前两次因为我的场景设计出错中止（同一个目录里已有原始 zip 而没加 `--force`；年份被拒之后去读不存在的 manifest）。都带 `-B`，串行，每次之后 `git status --short` 都干净。另有 8 次内联的 python 脚本，都带 `-B`，串行：核对插件能否导入 1 次；在抽稀样本上试我自己写的选择函数 1 次；按 UTF-8 打印源码行 2 次；语法检查 2 次；给探针文件打补丁 1 次（只写 `<评审临时目录>` 里的探针文件）；删除前列出 `<评审临时目录>` 的内容、用 `os.lstat` 核对里面没有重解析点 1 次。除打补丁那一次外都只读。提交本文件之前，我按确切路径删除了 `<评审临时目录>`：删除前核对它是我建的那一个，里面没有指向外面的重解析点，删除后确认路径不存在。
