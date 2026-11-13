# T153 Low 残留修复独立审核：e3cf663（F-1..F-5 闭合核验）

- 提交：`e3cf663`（3 文件 +446/−38：scripts/bt.py +36/−8、src/mt5quant/research/rehearse.py +196/−38 内含新函数、tests/test_rehearse.py +252/−3），分支 t153-lowlow-wt，worktree `t153-lowlow-wt`，基线 4ac1550（T153 彩排命令合入提交）。
- 审核对象：2026-11-13-t153-review.md 遗留 F-1..F-5（均 Low）——本提交声称全部闭合。
- 审核者：独立审核子代理（未参与实现；解释器=主会话 `.venv`（Python 3.12.10）；除本文档外零写入；正式台账未接触）。
- 方法：`git show e3cf663` 全 diff 逐行 + 当前源码（rehearse.py 905 行/bt.py 6077 行/rounds.py 装载器/validation events 与 H-08/F1 运行侧）交叉核对 + 红线回归 + 测试复跑。

**结论：approved**。F-1..F-5 全部真闭合（逐条判定见 §1）；红线回归零触碰（§2）；测试复跑 24 passed（§3）；新引入问题仅 1 处 Low 外观类（N-1，不阻塞）。

## 0. 审核执行摘要

| 动作 | 结果 |
| --- | --- |
| `git show e3cf663` 全 diff 逐行（3 文件） | 与基线 4ac1550 核对，改动面 = 声称的 5 项 + 报告/CLI 措辞，无夹带 |
| F-1..F-5 逐条闭合核验（读码直证） | 全部闭合（§1） |
| 红线回归（D67 argv/判定、零污染断言、G5） | 零触碰（§2） |
| `pytest -q tests/test_rehearse.py tests/test_repo_hygiene.py`（亲跑） | 24 passed in 165.68s（§3） |
| F-4 删除逻辑路径安全性专项（任务重点） | 无任何能删到沙盒外的输入路径（§4） |
| 改动函数其他调用点排查 | 无（formal_round_status/_seed_shadow_checks/_cleanup_sandbox 均仅 run_rehearsal 与测试调用） |

## 1. F-1..F-5 逐条闭合判定

### F-1 formal_round_status fail-closed —— **闭合**

- 代码（rehearse.py:210–233）：文件不存在 → None（与 formal_chain_heads 同口径）；任何 `sqlite3.Error`（损坏/占用/无 rounds 表）→ `RehearseError("…无法确认该轮是否已开启，拒绝彩排（fail-closed，F-1）")`；仅"查无该轮行"返回 None。原 fail-open 的 `except sqlite3.Error: return None` 已删。
- **拒绝先于沙盒创建**：run_rehearsal 内 `formal_round_status` 在 :580 调用、:582 拒绝；沙盒路径 :587–592 才定义、首个 `mkdir` 在 :596——异常上抛点严格早于任何写入。CLI 侧 `except rehearse.RehearseError`（bt.py:1212–1214，既有子句）→ rc=1。
- 测试 `test_formal_round_status_fail_closed_on_unreadable`：损坏字节直调抛错（match"无法确认"）、heads 兜底同样抛错（未削弱）、文件不存在仍 None、CLI 端到端 rc==1 且 `not (rehearse).exists()`（拒绝时零写入）。既有 happy-path 断言（open/未开轮/absent，:215–218）未动。
- 原 F-1 的"沙盒+夹具已建才兜底中止"残留窗口随之消失——现在读不到状态连沙盒都不建。

### F-2 no_approved_by 措辞与行为一致 —— **闭合**

- 新措辞（rehearse.py:761–766）："不传 --approve、不产生任何批准动作、不写正式台账任何行、不写任何 lockbox 行；影子台账 rounds 行的 approved_by 是预注册必填字段的继承值（沙盒内随真实 round open 代码路径写入，非彩排新增批准）"——与原审核建议措辞等价。
- 模块 docstring 红线段同步改为同一口径（继承值说明）；grep 全库无残留旧措辞"不写任何 approved_by"（src/scripts/docs/research 均无命中）。
- 测试不只断言文字：`test_rehearse_bar_round_end_to_end_no_pollution` 断言措辞含"继承值"/"不写正式台账任何行"进报告与 md，**并断言事实本身**——影子 rounds 行 `approved_by == "彩排测试"`（= 预注册值，证明"继承"表述为真）、正式台账 counts rounds==0（既有断言未动）。措辞=行为，双向钉死。

### F-3 夹具补齐 candidate_sha256 / precheck_source+calendar_coverage —— **闭合（走真实绑定路径）**

- **H-08 侧**：`_seed_shadow_checks` 每行带 `candidate_sha256` = 沙盒候选登记文件当前字节摘要（rehearse.py:407–412）。运行侧 `_candidate_frozen_check`（bt.py:535–565）读最新 pass 行的绑定值与当前文件比对——真实绑定路径被点亮。专项测试 `test_fixture_covers_h08_candidate_binding` 双向验证：内容一致 → None；预筛后改候选文件 → 报错含 "H-08"。端到端另断言夹具 payload 的 sha 与文件摘要逐位相等。
- **F1 侧**：事件候选夹具行带 `precheck_source.{stable_key, calendar_sha256}` + `calendar_coverage`（rehearse.py:367–394）。关键核验：
  - coverage 用与运行侧**同一构造器** `mt5quant.validation.events.fomc_calendar_events`、同一半开主段窗 `[首根日, 末根日)`（rehearse.py:377 `days[(days>=days[0]) & (days<days[-1])]`；运行侧 bt.py:4007 `days[(days>=pool_start) & (days<pool_end)]`，pool 界即彩排传的 `--event-days-start/--event-days-end` = days[0]/days[-1]）——同式，非手工拼凑。
  - 运行侧 `_verify_fomc_registration`（bt.py:4143–4184）是**真比对**：CLI SHA ≠ 登记 SHA → ValueError；`_FOMC_COVERAGE_PAIRS` 六字段逐对相等核对（缺字段也 fail-closed）。夹具 coverage dict 恰含六键（era_start/default_event_set/window_events/events_with_entry_anchor/dropped_no_bar/dropped_no_anchor），键名与 `_FOMC_COVERAGE_PAIRS` 一一对应。
  - 事件端到端断言 V3 关卡 details 里 `precheck_binding.calendar_sha256_match is True`、`coverage_match is True`、`"no_registered_precheck" not in binding`——兼容路径确实不再走。
- 原 F-3 的两个"打了折扣的开轮核对覆盖"缺口均补齐为真实路径覆盖。

### F-4 沙盒自动清理（含护栏）—— **闭合**

- 行为：缺省（无 `--keep-sandbox`）彩排**成功**（verdict=pass）后自动清理；失败/被拒不清理（专项测试以 `--stage-timeout 0.001` 制造机械失败，断言"沙盒保留"、JSON `sandbox_cleaned is False`、影子台账仍在）；`--keep-sandbox` 一律保留。护栏不过/删除失败 → 如实改记 `sandbox_cleaned=False` 并重写 md+JSON（不虚报清理成功）。成功用例断言 sandbox_root 只剩空目录（根保留不删）、md 报告保留且含"已按缺省自动清理"、正式台账零污染照旧。
- 护栏（rehearse.py:428–454）：`_cleanup_sandbox` 要求 tag 非空、`target != root`、`target.parent == root`（仅直接子目录）、`sandbox.name == tag`、目录内须有夹具形状（`repo/`+`store/`）；`shutil.rmtree(onexc=…)` 回调对 git 只读对象先 chmod 再重试（venv Python 3.12.10，`onexc` 形参 3.12 起可用）；任何 OSError → False。
- 护栏单测覆盖四向：正常删除 / **tag 不符不删** / **形状不符（无 repo+store）不删** / **sandbox_root 本身不删**（`test_cleanup_sandbox_guard_never_deletes_outside`）。
- CLI 与 md 双处如实输出清理/保留状态。

### F-5 覆盖边界文档化 + 钉死 —— **闭合**

- **双处钉住**：① rehearse.py 模块 docstring 新增"覆盖边界（F-5，如实声明）"段（--allow-unbounded-data 豁免理由 + "裸库被拒（X-03）不在彩排暴露面内" + 混合 mode 只彩排首候选 + 概率轮边界）；② bt.py 子命令 `description` 同三要点。测试 `test_rehearse_coverage_boundary_documented` 同时断言 docstring 关键字面量与 `round rehearse --help` 实际输出（COLUMNS=200 防折行）。
- **混合模式边界测试**：`test_rehearse_mixed_mode_round_only_first_candidate`（R10：首候选 C1001 bar、次候选 C1002 event）行为钉死——rehearsed_candidate==C1001、mode==bar、screen/gate argv 含 `--allow-unbounded-data` 且不含 `--event-kind`、关卡集 G0..G4 无 V3、md 含"v1 只彩排首候选"、无污染、verdict=pass。原 F-5 的两处口径差异从"argv 可审计+暂可接受"升级为"文档声明+行为测试双钉"。

## 2. 红线回归检查（全部通过）

| 红线项 | 核验结果 |
| --- | --- |
| D67 语义未变 | **通过**。阶段 argv 构造（rehearse.py:631–682）与判定逻辑（:684–714）不在任何 diff hunk 内，亲读现源确认与基线一致；`days` 仅逐字上移（供夹具绑定用，公式不变）。报告仅**增**字段（sandbox_cleaned、措辞重写、md 沙盒注记）——属任务允许的"只增报告字段"。 |
| 零污染断言未削弱 | **通过**。heads_before/after + unchanged 逻辑（:641、:716–725）未触碰；测试 diff 仅删 3 行辅助函数重构行，零 assert 删除；既有正式台账 chain 头+表计数断言原样，且新增用例（默认清理、混合轮）均附带 `unchanged/counts==0` 断言。 |
| G5 阻断未触碰 | **通过**。bt.py diff 仅 cmd_round_rehearse 输出/参数与 parser 两 hunk；对 diff grep G5/approve/begin_lockbox/lockbox 零命中。彩排 gates 参数钉死 G0–G4/V3 不含 G5（未变）。 |
| 其余红线（离线/只读台账/不伪造批准/gates 只读/种子确定性/已开轮拒绝） | **通过**。import 面仅增 stdlib（os/shutil/stat）；正式台账仍只经 `_ro_connect`（mode=ro）；已开轮拒绝升级为 fail-closed（F-1，收紧非放松）。 |

## 3. 复跑记录

```
$ python -m pytest -q tests/test_rehearse.py tests/test_repo_hygiene.py   # t153-lowlow-wt @ e3cf663
........................                                                 [100%]
24 passed in 165.68s (0:02:45)
```

test_rehearse.py 15 例（原 8 例全保留 + F-1 fail-closed、F-2 措辞+事实、F-3 H-08 专项、F-4 默认清理/失败保留/护栏三向、F-5 文档双处/混合模式行为共 7 新例）；test_repo_hygiene.py 9 例全绿。

## 4. F-4 删除逻辑路径安全性专项（任务指定重点）

结论：**无任何能删到本次彩排沙盒之外内容的输入路径**。逐向量核验：

1. **轮次 id 注入**：tag = `<R#>-<UTC时刻>`，R# 经 `rounds.py:56 _ROUND_ID = ^R\d{1,4}$` 严格校验（load_round 强制，且文件名主干须与 id 一致）——tag 中不可能出现分隔符/`..`/绝对盘符。
2. **即使无 id 正则（纵深防御）**：id 含 `/` 或 `\` → `sandbox.name != tag` 拦截；id 构造绝对路径（如 `C:/x`，Windows 下 Path 拼接会替换基座）→ `sandbox.name != tag` 同样拦截；任何解析后逃逸 sandbox_root 的路径 → `target.parent != root` 拦截。
3. **sandbox_root 本身**：`target == root` 显式拦截，单测覆盖；root 以下只有 `<tag>` 这一个直接子目录可成为删除对象，且必须同时满足名字精确等于本次生成的 tag + 含 repo/store 夹具形状。
4. **符号链接**：`sandbox.resolve()` 先解析再判父目录——symlink 指向 root 外 → parent 不等，拦截；指向 root 内兄弟目录 → 删除对象仍在 sandbox_root 内，且实际不可利用（tag 含秒级时刻戳，且彩排自身先 `mkdir` 该路径——预置同名条目会让彩排在清理前就失败）。
5. **--sandbox-root 指向敏感目录（如仓库根）**：删除对象仍是"本次彩排刚在该处创建的自含沙盒目录"本身，不波及任何其他内容；缺省值为 data/rehearse。
6. **rmtree 异常面**：只读 git 对象经 onexc 回调 chmod 重试；重试仍 OSError → 整体 except OSError → False（保留沙盒、如实报告）。onexc 形参与 venv Python 3.12.10 匹配。

## 5. 新引入问题

- **N-1 [Low，外观/自洽性，不阻塞]** 缺省清理成功时，CLI 末尾仍打印"原始 JSON：{report['report_json']}"（bt.py:1236–1237）——该路径已随沙盒删除，与前一行"沙盒已自动清理"自相矛盾（md 报告侧已如实写明"原始 JSON/影子台账随之删除"）。不会导向错误研究结论（md 为 A1 材料、内容完整）。建议后续：`sandbox_cleaned` 时该行不打印或标注"（已随沙盒删除）"。
- **N-2 [信息，已核清]** `sandbox_cleaned=True` 先写入沙盒内 JSON 再执行删除——幸存的 md 注记准确；护栏失败时 md+JSON 重写为 False。fail-honest 方向正确。
- **N-3 [信息，已核清]** F-1 收紧后，"无 rounds 表的旧正式台账"（非损坏、真实存在的历史形态）也会拒绝彩排（fail-closed 方向）——这是原 F-1 建议方案（收窄 catch）取舍后的有意行为，docstring 已写明，测试钉死；如未来真实旧库需彩排，应迁移台账而非放宽。

无 Medium/High 问题。

## 6. 结论

**approved**。原审核 F-1..F-5 五处 Low 残留全部真闭合：F-1 拒绝严格先于沙盒创建且 heads 兜底未削弱；F-2 措辞与影子 rounds 行事实双向钉死；F-3 两载荷经同一构造器/同一比对函数走真实绑定路径（H-08 双向、F1 六字段+SHA 全亮）；F-4 缺省清理+四向护栏单测+失败保留，路径安全性专项无沙盒外删除向量；F-5 双处文档钉住+混合模式行为测试。红线三项回归（D67 argv/判定、零污染断言、G5）零触碰。复跑 24 passed。新引入仅 N-1 一处 Low 外观问题，不阻塞合入。
