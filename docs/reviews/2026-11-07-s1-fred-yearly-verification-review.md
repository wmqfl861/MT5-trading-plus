# S1 聚焦评审：FRED 逐年核验程序（commit 55426ed，方案 A 通道 S1）

- 评审对象：commit `55426ed505e791eb0d99cc35448511221fa8e745`（`[S] FRED yearly verification program (channel plan A/S1)`）
- 设计基线：`.superpowers/sdd/2026-11-07-r3-main-test-fix1/fred-yeargate-channel-design.md`（§6 方案 A / A.1 / A.2，风险 R1/R5/R6）
- 评审人：独立评审代理（只读）；评审 worktree `<workspace>/demo-review-s1-55426ed`（`git worktree add --detach … 55426ed`，保留备查）
- 评审方法：独立通读实现与测试 → 对照设计逐条核验主张 → 在评审 worktree 独立复跑测试 → 编写探针脚本穷举 fail-closed 边界（探针脚本置于 `<workspace>/s1-review-probes/`，仅 import 只读）。全程不修改被评审文件、不派子代理、不联网、不触碰真实 FRED 工件（`data/ext/_raw/`、`data_research/ext/`）。
- 环境：PYTHONUTF8=1；`…/664d5945-…/.venv/Scripts/python.exe`（3.12.10，pandas 2.x，pytest 9.1.1）。

## 结论

- **Spec：PASS** —— 设计 §6 A.1/A.2 的 S1 范围（新程序 + 单测，V1-V5 + fail-closed + S3 补丁形态，零既有文件改动）全部落地且与设计一致；无超范围改动。
- **Quality：PASS**（附 F 项观察与 S2 义务，其中 F1 需在 S2 开跑前定案，见下）—— fail-closed 完备性经穷举探针核验成立；测试真实且 22/22 独立复现 GREEN；V2 与真实入库路径同构；merge.py 消费逐字段兼容。

## 0. 交付面核验（实现方主张逐条）

| 主张 | 判定 | 证据 |
| --- | --- | --- |
| 恰 2 新文件（603+568 行） | 属实 | `git show --name-status 55426ed` = A/A 两文件，1171 insertions，0 deletions；`git show 55426ed:scripts/…\|wc -l`=603、tests=568；未触碰任何既有文件（含冻结源 `src/mt5data/ext/`） |
| V1：raw sha 命中 manifest.source_files + parquet rows/first/last，不符整体 halt exit 2 零逐年声明 | 属实 | `scripts/fred_yearly_verification.py:338-368`（sha 命中 + rows/first/last 三重比对，违例抛 ValueError）；`:545-579` halt 路径 exit 2、`yearly_verification={}`。键名 `rows/first_time/last_time` 与真实 manifest schema（`src/mt5data/ext/adapters.py:352-354`、`src/mt5data/ext/core.py:197-215`）一致——设计文档 §3.1 的"first/last"是简写，程序用真键名，正确。V1 语义镜像 `validate_manifest(df=…)+load_ext_series` 的 rows 核对 |
| V2：调既有 adapters.fred_daily_from_bytes 逐位复算按年切片比对，单边在场年份 fail-closed | 属实 | `:370-379` import 不改源；`:85-105` `_frames_bitwise_equal`（列∈`_COLUMNS`、dtype 相等、`np.array_equal` 序敏感；datetime64 单位归一 ns）。同构性见 §2；单边在场测试 `test_raw_year_absent_from_parquet_fails_closed` / `test_parquet_year_absent_from_raw_fails_closed` |
| V3：零周末/零重复/严格递增/年内洞>30 天否决，节假日洞只记录 | 属实 | `:116-136` `_calendar_shape`；`:222-237` 四个否决门；holiday_gap_count 只入证据。语义核对见 §3 |
| V4：fresh CSV 字节注入（--fresh-csv 不联网）逐观测 value/date diff，任一值差→used=false，未注入→used=false | 属实 | `:381-391`（字节注入、解析异常→v4_error）；`:239-268`（未注入/异常/recomputed 缺失三路全 fail-closed）；`:141-169` `_revision_diffs`。比对精度语义见 §4 与 F2 |
| V5 跨源声明不可用 | 属实 | 顶层 `cross_source="unavailable (declared, design R5)"`（`:65,446`，halt 证据同样带），与设计 A.1 V5"如实写、不假装"一致 |
| 条目 schema 键=年字符串（merge.py 直接消费） | 属实 | `entries[str(year)]`；merge.py:217-227 `int(str(y))` 消费兼容（§7） |
| fail-closed 安全网（单年异常→条目 used=false） | 属实 | `:399-406` 每年 try/except→`_minimal_failed_entry`；整体异常→halt（`:571-579`）。穷举核验见 §1 |
| --emit-manifest-patch 产出 S3 补丁 + fields_must_remain_unchanged 守卫 | 属实 | `:457-479`；守卫清单为设计 §6 A.2 要求（usage_restriction/converter_version/rows/first/last）**超集**（另含 close_only/source_files/terms_note/price_side/original_timezone/dst_rule，共 11 项）。补丁只在 verify 成功后构建（halt 早退），只声明不落盘——与设计"S3 才写 manifest"分工一致 |
| 22/22 GREEN（RED 先行 21 失败） | 属实 | 本评审在评审 worktree 独立复跑：`22 passed in 5.96s`；sdd 目录 `RED-pytest.txt`=21 tests 全失败于 `scripts/…未实现`（真实 RED），`GREEN-pytest.txt`=22 passed。21≠22 的差异见 F4 |
| 合成夹具未碰真实工件 | 属实 | 测试全部 tmp_path 夹具 + 复用真实转换器函数；`make_demo.py` 自述合成、只写 sdd demo/ 目录；程序无网络 import（仅 urllib 无——全文无 urllib/requests），写路径仅 `--output`/`--emit-manifest-patch` 两处 caller 指定路径（`:591,594`），`--dry-run` 全抑制 |

## 1. fail-closed 完备性（评审重点 1）——**成立**

used=true 的唯一产生点：`_verify_year` 的 `used = not reasons`（`:270`）。reasons 为空要求同时满足：

1. 该年 parquet 切片 rows>0（`:206-209`）；
2. V2：v2_error 为 None **且** 重算切片与 parquet 切片逐位相等（`:212-219`）；
3. V3：周末/重复/非递增/年内洞>30 四门全过，异常也入 reasons（`:222-237`）;
4. V4：fresh 已注入且可解析 **且** recomputed 在场 **且** value_diffs=0 **且** date_diffs=0（`:241-268`，比对异常入 reasons）。

绕过路径逐条探针/读码穷举：

| 假想绕过 | 实际行为 | 证据 |
| --- | --- | --- |
| 年份切片空（parquet 无该年行） | reasons 首条"数据不在场"，used=false | 代码 `:206-209`；test_raw_year_absent… |
| parquet 某年零行 + 原始有 | 同上（条目年由 parquet ∪ 重算取并集，单边即产生条目并否决） | `:393-396`；测试 12/13 |
| 空事例集（entries={} 却 exit 0 伪装全过） | **不可达**：空 parquet 的 time.min/max=NaT，而 `pd.Timestamp(None)==pd.Timestamp(None)` 为 False → V1 覆盖比对必假 → halt exit 2（探针 P1 实测）；且真实侧 `validate_manifest` 强制 rows≥1，rows=0 manifest 不可能出自既有入库链 | 探针 P0/P1；`core.py:203-204` |
| raw 为垃圾但 manifest sha 恰好登记了它（V1 过、转换炸） | v2_error → 该年 used=false，reason 记异常原文（探针 P2 实测） | 探针 P2 |
| fresh CSV 年份对齐偏移/加年段 | fresh 独有**新年份**只进 summary.fresh_only_years（信息性，正确：绑定域之外的增量不是声明对象）；fresh 在**既有年份内**多日期 → date_diffs>0 → 否决；fresh 缺绑定日期 → 同理否决 | `:249-265,411-415`；test_fresh_date_diff / test_fresh_only_years |
| fresh 垃圾/头列改名 | 转换器抛 ExtDataError → v4_error → 全部年 used=false，不 crash | test_fresh_csv_garbage_no_crash |
| 字符串解析失败（era 参数/manifest 覆盖字段/输入文件） | 全部 halt exit 2，stdout 仍为可解析 JSON（halt 证据） | `:549-569`；test_missing_input_file / test_v1_parquet_rows_mismatch |
| parquet 无 time 列 | V1 覆盖段 KeyError → 整体 halt（不是逐年否决，但 halt=零声明，安全方向） | test_malformed_parquet_halts_no_crash |
| 单年 _verify_year 内部意外异常 | try/except → `_minimal_failed_entry`（used=false，条目在场） | `:399-406` |
| 时区边界 | 时间全程 naive 日期标签（适配器 `tz_localize(None)`+`datetime64[ns]`，`adapters.py:333`），dayofweek/diff 无时区依赖；若 parquet 带 tz，V1 与 manifest naive 首尾必失配 → halt | `adapters.py:331-337` |
| NaN 语义 | `np.array_equal` 默认 NaN≠NaN → 双侧 NaN 记不一致（偏否决，fail-closed 方向）；FRED dropna 后无 NaN，实际不触发 | 探针 P7 |

结论：**不存在可达的无证据 used=true 路径**。halt（exit 2）与条目否决（used=false）两级安全网均实装且被测试覆盖。

## 2. V2 逐位比对语义与入库路径同构性（评审重点 2）——**成立**

- **同一转换器**：`from mt5data.ext.adapters import fred_daily_from_bytes`（import 冻结源不改；commit 对 src/ 零改动）。converter_version 由此钉在 1（manifest 声明一致）。
- **同一字节入口**：真实入库 `fetch_ext_data.py:298-317 cmd_fred` = 读盘字节 → `fred_daily_from_bytes(raw, symbol="USTEC", downloaded_at_utc=…, series_id=…, source_name=…)`。程序调用 `:373-377` 同签名（source_name 缺省仅影响 manifest 文件名不影响 df；downloaded_at_utc 只进 manifest 不进 df）。**同构**。
- **消费口径一致**：比对列=store._COLUMNS（time/open/high/low/close/tick_volume/spread/real_volume），与 merge.py/`merge_bars`/`concat_mt5_priority` 消费列一致；dtype 严格相等（datetime64 单位差异归一 ns——parquet 往返与适配器输出均 ns，归一是防误报不是放行：单位不同仍需值相等）；顺序经 `np.array_equal` 元素序比对实质校验行序。两个已知收窄（均安全方向）：列集限于 _COLUMNS（额外列被忽略，但下游也只消费 _COLUMNS，探针 P6）；索引不比对（reset 后行序已由值序覆盖）。
- **V4 用重算侧（rc）而非 parquet 侧对账**（`:249`）——parquet 被篡改无法掩盖修订，`test_roundtrip_mismatch_vetoes_year` 显式断言"V4 零差不救 V2"。

## 3. V3 门限正确性（评审重点 3）——**与设计一致，两处需知悉的边界**

- R14-L3 语义：`candidates.py:640 _GAP_SPLIT=Timedelta(days=30)`、`:689-717 _split_span` 在 `e-s > 30 天` 空洞切开（自然日 Timedelta，不含观测日端点本身）；V3 否决线 `max_internal_gap_days > 30`（`:231`）同取**相邻观测自然日间隔、严格大于 30 才否决**——30 天整通过，两端点日不计入。语义一致。
- 差异 1（无害）：V3 用 `Timedelta.days`（向下取整），candidates 用精确 Timedelta——D1 日期标签全部午夜归一，间隔恒整日，两者永不分歧。
- 差异 2（设计如此，见 F3）：V3 是"**年内**相邻间隔"（设计 A.1 原文"年内相邻间隔 ≤30 天"）。跨年边界洞（如 12 月→次年 2 月的 66 天洞）两侧年份各自 max_gap 都小、双双放行（探针 P4 实测）。这不虚增覆盖：candidates `_observed_gaps:653` 按**全序列**相邻间隔切段，>30 天跨年洞照切；merge 运行时序列本身带洞。used=true 声明的是"该年在场数据过核验"，与 HistData salvage 部分月先例语义相容（设计 §2.2）。
- 周末判定时区基准：naive 日历日期上 `dayofweek∈{5,6}`（`:119`），无时区问题；节假日洞 `pd.bdate_range`（周一-五网格，无美国节假日历）只作计数证据——与设计"工作日洞数逐年在案（≈9/年）作证据不作门"一致。
- merge.py/_manifest_gap_intervals 一致性：used=false 年 → `[Y-01-01,Y+1-01-01)` 整年切洞（`candidates.py:657-686`，`rec.get("used") is False`）；程序对**全部在场年**逐条产条目（全量年段变体），消除设计 §1.3 的"未列年份不切洞"不对称。

## 4. V4 规则（评审重点 4）——**规则如实实现；两处语义边界必须记录在案**

- 判定粒度：按年 × 按观测日。value diff=公有日期 close 不等；date diff=对称差（bound_only+fresh_only 合计），首例（日期+双侧值/缺失侧）入证据（`:141-169`）。任一 value diff 或 date diff → 该年 used=false，reason 引"设计 R1 预写规则"——与设计 A.1/R1 逐字对齐。
- **浮点口径（实测定性）**：双侧同走 `pd.read_csv`（经同一转换器）→ float64 后**精确相等**比较。这不是字符串比对：文本重排版（"1003.00"→"1003.00000000000000000000"）零差（探针 P3a）——正确（管线全程 float64，值等即等）；但 **pandas C 解析器默认精度（'high'）约 15-16 位有效数字**，第 16-17 位的文本修订会被解析坍缩为同一 double 而不可见（探针 P3b：bound "…4567" vs fresh "…4568" → value_diffs=0；`float_precision="round_trip"` 本可分辨）。由于入库/复算/fresh 三处同用同一解析器，凡解析不可见的差异对**整条下游管线同样不可见**——校验强度与消费口径自洽，真实 FRED 指数收盘 2-4 位小数的任何实际修订远高于该分辨率（0.01/千点 ≈ 1e-6 相对差，探针确认 2 位小数差必检出）。结论：可接受，但应在 S2 证据中如实注明该精度口径（F2）。
- **era 升级钩子（设计 R1：era 内差→A1）**：程序层如实记录——`summary.era_coverage.not_used_years/fully_used`（`:283-314`）精确给出"era 窗口内哪些年失守"，即 S2 操作者的 A1 上报信号；升级本身是流程动作不属程序。钩子充分。
- **S2 必然事件（F1，最重要）**：fresh 重下载是**活的**（今日 2026-11-07，绑定 cutoff 2025-10-23）。末年 2025 的 fresh 切片必然含 cutoff 后新日期 → date_diffs≥1 → **按现行预写规则 2025 必然 used=false**，设计 §6 A.4-S2"零差预期下全部年份 used=true"不可实现。这不是 S1 代码缺陷（fail-closed 方向正确），而是 S2 投喂协议缺口：S2 须在注入前将 fresh 裁剪到绑定域（≤ last_time），并对**原件 sha + 裁剪件 sha 双留档**（程序只记注入件 sha，`:384`），或取得设计方对"末年 fresh 尾部豁免"的显式裁定（那是对预写规则的修改，需 R1 层级签字，不得由实现方自释）。机械后果评估：2025 used=false 不触装配器 `_used_gate`（touched=1986..2021），但会在 candidates 切出 2025 整年洞并使 S2 报告偏离"全部 used=true"预期——须在 S2 前定案。

## 5. patch 形态（评审重点 5）——**达标（超集）**

`build_manifest_patch`（`:457-479`）：`fields_to_add` = yearly_verification + label_rule（与设计 §6 A.2 顶层文案**逐字一致**，`LABEL_RULE:63`）；`fields_must_remain_unchanged` 11 项，含设计点名的 usage_restriction/converter_version/rows/first_time/last_time 全部（first/last 用真键名 first_time/last_time，正确）。守卫为声明式清单（供 S3 落盘时逐项核对）——与设计"S3 才写 manifest"的通道分工一致，本程序不越权。halt 时补丁不产出（main 流程早退），不会给 S3 喂空证据补丁。

## 6. 测试质量（评审重点 6）——**真机制测试；两处小缺口（F4/F5）**

- 22 用例全部驱动真实机制（CLI main → verify → 证据 JSON 断言），无自我实现式断言：夹具经**真实适配器** fred_daily_from_bytes 造 parquet/manifest（`build_store`），违例用显式字段覆盖（篡改 sha/rows/df/行序），断言到具体数值（rows/raw_rows/max_gap=3/value_diffs 计数/首例 dict/exit code）。
- RED 证据真实：21 tests 全失败于"scripts/fred_yearly_verification.py 未实现"（sdd `RED-pytest.txt`），实现后 22 passed（GREEN + 本评审复跑）。
- 边界覆盖：闰年（1992 在 gap 测试中 used=true；bdate_range 含 02-29）、年末切片（整年 bdate_range）、周末行/重复/乱序/长洞四否决门、V1 三 halt 路、单边年、dry-run/输出一致性、era 参数化。CSV 尾部换行/BOM 无显式用例——但两侧同解析器同行为，BOM 会致列名失配→halt/否决（fail-closed 方向），实际风险仅"未测"不是"错"（F5）。
- 回归取证（sdd `regression.txt`）方法 sound：本 commit 对既有文件零改动（A/A 证实），相邻域 114 passed；全量 44 failed 逐域归因于并行工作流（t31/12/1），因果独立成立。注：regression 记录于旁系 commit 24b23e1（906e172 之上的早期落点），其 S1 两文件与 55426ed **逐字节相同**（diff=0），两树差异仅 2 个无关数据/文档文件——证据适用（F6）。

## 7. merge.py 消费兼容（评审重点 7）——**逐字段兼容**

| 消费点（worktree 实测行号） | 程序产出 | 判定 |
| --- | --- | --- |
| `merge.py:217-227`：`rec.get("used") is True`；键 `int(str(y))` 坏键跳过；条目非 dict 跳过 | `used = not reasons` 为 Python bool → JSON `true/false`；探针 P5 实测 json round-trip 后 `is True` 成立；键 `"1994"` 字符串 int 化无歧义；条目恒 dict | **兼容，无类型陷阱**（used 永不为字符串/1/0） |
| `merge.py:233-238`：`ext_df[time.dt.year.isin(used_years)]` 无条件先行 | 条目覆盖 parquet ∪ 重算全部在场年（全量年段变体）→ 无"未列年份"灰区 | 兼容 |
| `candidates.py:657-686`：`rec.get("used") is False` → 整年切洞 | bool False 同理 round-trip 保真 | 兼容 |
| 装配器 `_used_gate`（S5 消费面） | exit 0 ⇔ 全部在场年 used=true；任一年失守 exit 1 且 summary.era_coverage.not_used_years 可读 | 信号充分（S2/S5 义务，非本 commit 范围） |

## 8. 残留风险与观察（F 编号，按重要性排序）

- **F1（Medium，S2 前必须定案）**：fresh 活数据在末年（2025）必然产生 fresh-only 尾部日期 → 按现行预写规则该年必 used=false，与设计 S2"全部 used=true"预期冲突。处置：S2 注入前裁剪 fresh 至绑定域并双 sha（原件+裁剪件）留档，或取得 R1 层级对末年尾部豁免的显式裁定。程序本身无需改（fail-closed 方向正确）。
- **F2（Low，文档义务）**：V4 值比对的有效分辨率=pandas C 解析器默认精度（~15-16 位有效数字，非文本级、亦非 round_trip 级）；第 16 位后的文本修订不可见（探针 P3b）。与全管线消费口径自洽（同一解析器三处共用），真实 2-4 位小数修订必检出；S2 证据应注明该口径。
- **F3（Low，设计确认项）**：V3 洞门限为"年内相邻间隔"，跨年边界 >30 天洞不否决（设计 A.1 原文如此；下游 `_observed_gaps` 全序列切段兜底覆盖年数不受虚增）。建议 S2 报告照实提示该口径，避免读者误读 used=true=无洞。
- **F4（Low，过程）**：RED 证据覆盖 21/22 用例；`test_malformed_parquet_halts_no_crash` 为 RED 之后补写（无独立 RED 记录）。同族 halt 路已有 3 个 RED 用例，机制风险低；实现方主张"21 失败"如实，非虚报。
- **F5（Low，测试缺口）**：`--emit-manifest-patch -`（stdout 分隔符模式）无测试（文件模式已测）；BOM/尾换行无显式用例。均为低风险补测项，可并入 S2 前的顺手补强，不阻塞。
- **F6（Info）**：regression.txt 取证哈希为 24b23e1（同代码旁系落点）；S1 文件两 commit 逐字节一致，证据适用。建议最终 S1 报告以 55426ed 重述取证哈希。
- **F7（Info，正向核验）**：空条目集伪装通过（exit 0 且零条目）经探针证实**不可达**（NaT≠NaT → V1 必 halt；validate_manifest 强制 rows≥1）。
- **F8（Info）**：schema 相对设计 A.2 示例有两处更名（max_gap_days→max_internal_gap_days、weekday_holes→holiday_gap_count）及若干增键（monotonic/performed/first_*_diff）——均属证据字段，merge.py 只消费 `used`，键名更精确且在模块 docstring 有定义，无契约影响。

## 9. 评审侧产物与可复核性

- 评审 worktree：`<workspace>/demo-review-s1-55426ed`（detached 55426ed，测试复跑后 `git status` 干净，保留）。
- 探针脚本：`<workspace>/s1-review-probes/probe{1,2,3}.py`（P0 NaT 语义 / P1 空 parquet / P2 垃圾 raw+登记 sha / P3a-b V4 浮点精度 / P4 跨年洞 / P5 JSON bool round-trip / P6 额外列 / P7 NaN 方向）。
- 独立复跑命令：`PYTHONUTF8=1 …/.venv/Scripts/python.exe -m pytest tests/test_fred_yearly_verification.py -v -p no:cacheprovider`（评审 worktree 内）→ 22 passed。

## 10. 对通道后续步骤的输入

- S2 开跑前置义务：F1 裁定/裁剪协议；建议同时落 F2 精度口径说明。
- S3 消费补丁时逐项执行 fields_must_remain_unchanged（11 项）核对——本程序只声明不执行，这是设计分工不是缺口。
- S5 换绑后 `_used_gate` 的 touched-years 判定与 exit 0/1 信号已可机械衔接。

（评审只写本文件；不 commit。主会话集成。）
