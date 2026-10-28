# 台账重封保真证明与按事件序号锚定（T88 / 第十三轮 K7）

日期：2026-10-28 ｜ 任务：R13-T88 ｜ 审核来源：`docs/reviews/2026-10-28-review-round13.md` K7、K9（d96c523）
涉及：`scripts/ledger_anchor.py`、`src/mt5quant/research/ledger.py`（仅文档注记）、`tests/test_ledger_anchor.py`

## 0. 结论

1. **两次历史重封事后补证通过**：T63（round_id）与 T80（run_class）两次迁移按迁移前列集合重算旧前缀，均逐字节复现 git 锚定的旧头（`72709d12…` / `ddd8106f…`）——两次迁移都只加了列，旧内容原样保留（§2–§3）。
2. **重封准入补上保真前置**：`--accept-rechain` 新增第 ⑤ 项前置——按迁移前列集合（`--new-columns` 声明的新增列除外）重放旧前缀必须等于旧头；"加列之外还改旧行内容"的伪装迁移现在被拒绝（§4）。
3. **锚定改为按事件序号重放**：新锚行带 `events≤` 高水位列，前缀重放按 `ledger_events.event_id` 进行，替代按时刻——d96c523 处理过的同刻度竞态根因消除；旧锚（无该列）兼容读取（§5–§6）。

生产台账与锚文件**本次一概未写**：补证走只读推导（`mode=ro`，文件 md5 前后一致：`049d6c4b…` / `888847cf…`）；新锚定机制只实现不执行，下次锚定自然生效。

## 1. 补证方法（只读推导）

- 数据源：生产台账 `data/research/ledger.sqlite`（当前 446 行 runs，含 T63、T80 两次迁移后的内容），sqlite URI `mode=ro` 只读打开——绝不触发迁移、绝不写文件。
- 旧头来源：`docs/ops/ledger-anchors.md` 两条 `rechain accepted` 注记里的 `rechain_from`（git 内审计链原文）。
- 推导：按 rowid 取前 N 行，行内容按**目标列集合**（= 当时列集合 = 当前内容列 − 该次迁移"当时新增列"）规范化（键排序、紧凑分隔符、非 ASCII 原样）逐行重算
  `row_hash = sha256(规范化行 JSON + prev_hash)`，与旧头比对。任何一行旧内容被改过一个字节，重算链头即偏离旧头（sha256 原像，造不出）。
- 工具：`scripts/ledger_anchor.py --prove-fidelity <ledger> --old-head-file <json> --new-columns <json>`（T88 新增，只读）。

## 2. T63（runs.round_id 迁移）补证实测

- 当时新增列：`round_id`（`run_class` 是 T80 后来加的，T63 时点也不存在——按 T63 时点列集合需同时去掉两者）。
- 目标列集合：runs 当前内容列 − {`round_id`, `run_class`}；其余两表未迁移，全列。

```
$ python scripts/ledger_anchor.py --prove-fidelity data/research/ledger.sqlite \
    --old-head-file old-heads-t63.json --new-columns '{"runs":["round_id","run_class"]}'
  ✓ runs：前 441 行按目标列集合重算 == 旧头 72709d121229813e…（迁移保真成立）
  ✓ gates：前 76 行按目标列集合重算 == 旧头 68a1dd2f7939eca3…（迁移保真成立）
  ✓ lockbox_uses：前 0 行按目标列集合重算 == 旧头 （空表）…（迁移保真成立）
```

重算头逐位等于锚文件 2026-10-26T07:50:12Z 锚定的 `72709d121229813edeac5db063d529d81fc8eeb37f7e5e9a53062f07ea782c0c`（441 行）。

## 3. T80（runs.run_class 迁移）补证实测

- 当时新增列：`run_class`（此 时点 round_id 已存在，属旧列）。
- 目标列集合：runs 当前内容列 − {`run_class`}。

```
$ python scripts/ledger_anchor.py --prove-fidelity data/research/ledger.sqlite \
    --old-head-file old-heads-t80.json --new-columns '{"runs":["run_class"]}'
  ✓ runs：前 445 行按目标列集合重算 == 旧头 ddd8106fc0d11356…（迁移保真成立）
  ✓ gates：前 76 行按目标列集合重算 == 旧头 68a1dd2f7939eca3…（迁移保真成立）
  ✓ lockbox_uses：前 0 行按目标列集合重算 == 旧头 （空表）…（迁移保真成立）
```

重算头逐位等于锚文件 2026-10-27T17:10:54Z 锚定的 `ddd8106fc0d11356e13b90a022babae85c2efc1fc92c80d311105b20f5d69ad4`（445 行）。两次补证顺带证明 gates / lockbox_uses 两链跨两次迁移内容未动。

## 4. 保真检查：`--accept-rechain` 前置 ⑤（新）

K7 指出的缺口：原准入只查（①链干净 ②原因非空 ③旧头=最近锚定），不证明迁移保留了旧内容——迁移时改一行旧内容再整链重算，准入同样会通过。

新前置 ⑤（`_fidelity_problems`）：对 old-head 文件里每个表，按**迁移前列集合**（当前内容列 − `--new-columns` 声明的新增列）重放旧前缀 N 行，必须等于旧头。

- 声明的列必须在当前列集合里（防拼写绕过）；**未声明**的表按全部当前列重算（从严：加了列不声明，重算含新列必对不上旧头，同样拒绝）；
- 新增列声明写进准入注记：`> rechain accepted: …；rechain_from=<旧头>；new_columns=<声明>`；
- 失败测试（先行红后绿）：`test_accept_rechain_refuses_old_content_mutation`——加列迁移夹带一行 `actor` 改写，链干净、旧头合法、声明如实，准入仍拒（红：改前 "DID NOT RAISE"，准入曾被放行）；`test_accept_rechain_undeclared_new_column_fails`——漏声明同样拒。
- 重放口径：按最近锚定行的事件高水位/时刻回滚归档改写（旧前缀=旧锚定位置当时的状态）；生产 `ledger_events` 现为空表，两次补证即按当前内容直读。

## 5. 按事件序号锚定（events≤ 列）

原机制（R7/N-I3）按**时刻**重放：归档事件 `recorded_utc ≥ 锚定 ts` 视为锚定后，取首笔的 `old_content`。d96c523 修过它的同刻度漏判（严格 `>` 漏归档 → 误报），改成 `≥` 后仍残留反向竞态：同刻度且事件实际早于锚定读取时会取错 old（可复核误报）。

新机制（T88）：

- **锚行记录高水位**：新锚行第 8 列 `events≤` = 锚定时该表在 `ledger_events` 的最大 `event_id`（无事件记 0），**计入 anchor_hash**（规范化内容多一键 `events`；旧锚行不含该键，复算口径不变——锚链不断）。
- **重放按序号**：`_replay_head_at` 给定 `max_event_id` 时，`event_id > max_event_id` 的归档事件视为锚定后，取首笔（event_id 升序）的 `old_content` 回滚——"锚定之后"由全序整数判定，与时钟无关。
- **旧锚兼容**：锚行无 `events≤`（解析得 None）→ 退回按时刻 `≥` 重放（d96c523 口径）。解析器同一正则兼容 7 列/8 列行；`--verify` 按各自行的口径复算 digest。实测：生产锚文件（7 列，31 行）新代码 `--verify` 通过。
- **幂等键不变**：仍按（表， 行数， 链头）判重；高水位不参与。

## 6. 竞态消除论证

- **event_id 是全序**：`ledger_events.event_id` 为 AUTOINCREMENT 主键；台账全部写事务 `BEGIN IMMEDIATE`（保留锁，写者串行），行内容写、归档、提交同一事务——event_id 分配序 = 提交序 = 写入序。同刻度两事件的先后由 id 唯一确定（这正是失败测试 `test_same_tick_two_events_ordered_by_event_id` 钉住的：冻结时钟下三次"时刻全同"的操作，两个锚各按自己的高水位选中正确的 old，验证双双通过；旧按时刻机制必误报）。
- **高水位与链头同一快照**：`write_anchor` 在同一只读连接里取链头与 `MAX(event_id)`——快照内事件 id ≤ 水位、快照后提交的事件 id > 水位（串行写 + 单调 id 保证），不存在"锚定读到内容却没读到对应事件"或反向窗口。锚定 ts 只是展示字段，不再参与判定。
- **不放松检出**：伪造归档让重放对上锚头仍是 sha256 原像问题；直改 `event_id` 会被 `chain_meta` 的 `ledger_events` 行内容摘要报出（`_events_digest` 含 event_id）。`ledger.py` 模块文档补了该单调性承诺注记（本次唯一改动，无行为变化）。

## 7. 验证与执行边界

- 失败测试先行：`tests/test_ledger_anchor.py` 四个新测试（准入保真拒绝 ×2、同刻度序号序 ×1、旧锚兼容 ×1）实现前全红——两个准入测试红因"准入被放行（DID NOT RAISE）"、同刻度测试红因按时刻重放误报"gates：行数与锚定相同但链头不符"、兼容测试红因锚行无 events≤ 列；实现后 19/19 绿，既有准入①–④与 R7/N-I3 测试不回归（`tests/test_research_ledger.py` 54/54 绿）。
- 生产台账/锚文件零写入：两次补证与 `--verify` 均只读（md5 前后一致：台账 `049d6c4b…`、锚文件 `888847cf…`）；**新锚定机制未对生产执行**——下次计划任务锚定时新行自然带 `events≤`，此后前缀重放自动按序号进行。
- 新锚文件表头（新文件）含 `events≤` 列；既有文件表头不回改，追加行多一列，解析与渲染均兼容（生产 7 列锚文件新代码 `--verify` 实测通过）。

## 8. 复现

```
# 两次补证（只读；old-heads json 取自锚文件 rechain_from 注记）
python scripts/ledger_anchor.py --prove-fidelity data/research/ledger.sqlite \
  --old-head-file <t63.json> --new-columns '{"runs":["round_id","run_class"]}'
python scripts/ledger_anchor.py --prove-fidelity data/research/ledger.sqlite \
  --old-head-file <t80.json> --new-columns '{"runs":["run_class"]}'
python scripts/ledger_anchor.py --verify      # 生产锚文件兼容校验（只读）
```
