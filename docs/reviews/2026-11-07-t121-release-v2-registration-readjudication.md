# t121-release-v2 登记 scoped 重裁（2026-11-07）— 主会话转录

## 落盘说明（主会话转录注）

重裁唯一强制条件=重绑提交 R 已落地：R=e62bc02（`git diff 9374283 e62bc02 --stat` 恰 1 文件 production_adapter.py +4/-1，常量行+3 行注释）。R-1..R-6 机械校验证据：R-1 grep=9084d7c9…全文 64 位；R-2 如上；R-3 33 文件表沿袭重裁记录 (b) 表（R-2 证明除 adapter 行外 32 文件与 9374283 逐字节相同，adapter 行 R 值：blob-content-sha256 77fede9e89382f4946b6972f5bd8996b9b54d820759e7c0e24d7049811561d6d / raw-sha256 567c67b7c8e103bb7c5af86900e4ae7d7818215352efa39499568dc9698aee18）；R-4 v2=9084d7c9…/v1=c78a78d1…（git cat-file blob 内容复算）；R-5 闭包=Module A import 探针 33/33 + 重裁者静态 AST 探针 33/33 双证；R-6 test_v2_usdjpy_release_runs_mixed_kinds_and_watermark_windows 由红转绿（1 passed 实测）。发现 1（fb57e02 夹具卫生违规）已于 504a86c 修复（占位值替换，19/19 绿），位于 R 之后不改变 R 的 diff 有效性。

---

以下为重裁者记录原文（转录）。

## 重裁结论

**Conditional APPROVED（附一项强制完成条件）。** 对请求所列三项事实基础：事实 1（脱敏语义中性）与事实 2（CODE_FILES 32→33＋grids 收紧）独立核验**通过**；事实 3 的指定钉扎值 **9374283 不获批准为 code.repo_commit——DENIED**，理由：该提交上 `PREREG_V2_SHA256` 常量仍为被取代的旧值 32aaa92c…，而该提交上的 prereg v2 blob 已是 9084d7c9…，运行期校验必然失败（已实证复现：`tests/test_t121_release_v2.py::test_v2_usdjpy_release_runs_mixed_kinds_and_watermark_windows` 在 9374283 失败，拒绝原因恰为 `preregistration bytes mismatch`，18 通过/1 失败）；若改在 manifest 绑定旧值 32aaa92c 则落入总裁定 §4 撤回触发项 (6)"绑定被取代的 v2 SHA"，同为禁止。**解除路径（唯一强制条件）**：落地单一目的重绑提交 R，通过机械校验协议后，按本记录新 D 值完成登记并执行。其余全部登记值不变且即时定稿。

## 一、三项事实的独立核验

**事实 1（历史脱敏，语义中性）：通过。** prereg v2 git-blob 旧值 32aaa92c…（backup-pre-redaction=24b23e1 与 0082b19 两点独立重算一致）→ HEAD 值 9084d7c951cd246f4cae3f2dec5c5ec2c13d79a3d84b3c0a4e036ab1ca191315；prereg v1 blob 不变 c78a78d1…。`git diff backup-pre-redaction 55426ed` 恰 2 文件 4 行占位符替换（prereg v2 仅 §1/§1-bis 各一行、C005.precheck.json 两行）；**§4 冻结区零触碰**。推送状态：origin/main=1097fe4，HEAD 不是其祖先——重写链与后续提交均未推送，"零外泄"陈述与现场一致；backup-pre-redaction 分支仅存本地。全部本地绑定工件不变（account-fee-terms 1aa60bd6…/cost bundle 7abeb458…/spread 组件 1419a0b4…/D60 5cf9a989…/转录件 390cfa76…/总裁定 4644adb2…/范围请求 81892e11…；研究库 calibrated e2cae60c…/spread_model c35550b9…/sessions a7bfe5d2…/H1 48855f91…/D1 94d969bd… 逐一复算相符）。基线 diff 评审文档 git-blob 四点均为 40a0d8a2…；前次登记审核记录敏感扫描零命中。

**事实 2（CODE_FILES 32→33＋R2 收紧落地）：通过（附治理注记）。** adapter 全量 diff（bed580c→HEAD）恰两个 hunk 共 +13 行：①CODE_FILES 增补第 33 成员（含 3 行注释）；②grids 键集恰等检查 `set(grids)==set(pool)`，fail-closed、双向差异列示、位于 `if grids is not None` 块内——v1 路径零变化，与原 R2(b) 裁定逐字相符。31 文件双哈希逐字节相同；第 33 文件内容与 0082b19 时逐字节相同（1fedcd05… 两点一致）。静态 AST 闭包探针：33/33 闭环零缺口。治理注记：Module A 两提交（3623e47/b5b41f1）无专门独立评审记录；本记录对其全部 13 行 diff 的逐行复核＋静态闭包探针＋v1 零变化分析＋测试重跑构成绑定相关部分的独立核验记录（登记记录引用本节，或另补聚焦评审）。

**事实 3（新链与钉扎）：链条核验通过；指定钉扎值不可执行。** 新链 934ea20..9374283 = 13 个重放等价提交＋6 个新提交；仅 3623e47/b5b41f1 触及 CODE_FILES。登记状态现场确认：registration 目录仅存 manifest-draft.json（磁盘件陈旧：0082b19/32 文件/32aaa92c——重新物化时须按本记录再生）；无 release.json、无 trust 快照、无 claim、无 executions——前次批准从未物化，execution_id 未消费，无须撤销动作。不可执行性（实证）：9374283 上常量仍 32aaa92c…（e549f6d 换绑的是 N1 FRED 装配器，非 t121 adapter）→ 运行期 `preregistration bytes mismatch` 拒绝；任何新链提交均含脱敏后文档，故重绑提交不可避免。

## 二、强制完成条件：重绑提交 R

单一目的提交；diff 仅改 `src/mt5quant/prob/production_adapter.py` 的 `PREREG_V2_SHA256` 一行（32aaa92c…→9084d7c9…，可附注释行）；其余 32 个 CODE_FILES 与 9374283 逐字节相同；树 clean；按项目规范配聚焦评审记录；为运行时 HEAD 祖先。机械校验协议：R-1 grep==9084d7c9…全文 64 位；R-2 diff 9374283..R 恰 1 文件；R-3 33 文件循环重算（adapter 行以 R 值替换并记录）；R-4 v2 blob@R=9084d7c9…、v1=c78a78d1…、rules canonical=b7ce05c5…；R-5 闭包 33/33；R-6 目标测试转绿。R 落地后按本记录直接登记；任何超出范围的 diff 须回传重裁。

## 三、新 D 值（R 校验通过后生效）

| 项 | 值 |
|---|---|
| execution_id | `t121-release-v2-usdjpy-exec-001`（不变，未消费） |
| code.repo_commit | **R = e62bc02**（按转录注 R-1..R-6 证据定稿） |
| rules.prereg_sha256 | 9084d7c951cd246f4cae3f2dec5c5ec2c13d79a3d84b3c0a4e036ab1ca191315 |
| rules.sha256 | b7ce05c5343030d4dedc00146a130530bbf40bd0c7e183c47a4d9441fb89c725（不变——RULES 无 prereg 字段，blob 变化仅传导 prereg_sha256 字段与 adapter 常量） |
| valid_from / expires_at | 2026-11-08T00:00:00Z / 2026-11-20T00:00:00Z（不变） |
| review.json | 重签：decided_at=2026-11-07（本重裁），supersedes 前次批准（未物化，无撤销动作），binding_sha256 按新 manifest 重算 |
| conditions | 原 3 项闭合沿袭＋新 3 项：`prereg-redaction-equivalence-closed-2026-11-07`、`module-a-33-closure-closed-2026-11-07`、`prereg-const-rebind-closed-at-R` |

**33 文件钉扎表**：@9374283 现场重算的 33 文件双哈希表以重裁者留存证据为准（重裁运行产物）；除 adapter 行外对 R 逐字节成立（R-2 证明），adapter 行 R 值见转录注。重物化时从 R 现场重算装配入 manifest。

## 四、新发现（须处置，与登记解耦）

1. fb57e02 卫生违规（tests/test_export_research_store_scoped.py 夹具写入真实形态账号/服务器值，卫生测试实证失败）——推送前必须修复；已于 504a86c 修复（占位值，19/19 绿），位于 R 之后不影响 R 有效性（非 CODE_FILES）。
2. 请求陈述两处修正："32 文件内容零变化"实际为 31 文件（adapter 例外=已评审 13 行）；"v2 blob 变化传导至 RULES canonical"不成立（canonical 不变，传导止于 prereg_sha256 字段与 adapter 常量）。

## 五、沿袭条件与前置

首批批准全部前置沿袭不变（release.json 剥离 `_*` 键；覆盖率重算 0.7592；R1 早年粒度全行标注；R3 传输披露行+payload 逐字节断言；input.json gap_spans 重推导复现 []；六 payload/sessions/sources 物化回填；快照绑定 release.json raw-bytes；输出独占、一次执行、零结局量、提交即停 A3）。新增：R-1..R-6 证据入登记记录（见转录注）；manifest-draft 依新 D 值重新物化；Module A 独立核验以本记录第一节为凭。

## 六、方法与证据归属

独立重裁者（同前次登记审核者，未参与脱敏/Module A/新链任何制备）。全程只读、零文件修改、未派代理、不填 approved_by；价格列零读取；SHA 全部现场重算。实证项：重放等价 diff、33 文件双哈希、静态 AST 闭包探针、canonical 重算、两测试重跑。采信项（有独立记录在案）：REDACTION-RECORD 映射全表（抽查证实）、s4c-s5 批评审 PASS。

**重裁有效窗口**：登记与执行须在 2026-11-20T00:00:00Z 前完成；R 的落地与校验不得改变除 adapter 常量行外的任何绑定事实，否则回传重裁。

