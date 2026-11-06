# t121-release-v2 登记审核（独立批准者，2026-11-07）— 主会话转录

主会话转录独立登记审核者结论（全程只读、零文件修改、未填 approved_by、未派代理；价格列零读取——唯一价格量=装配期已记录的中位数，本次未重读；时间列-only 投影与全部 SHA 现场重算）。32/32 CODE_FILES 零偏差；evidence_refs 校验格式相符。

**总裁定：APPROVED。** 批准登记 t121-release-v2（品种池恰为 {USDJPY}，方案 II），并授权执行**一次** T121 只计数/只成本运行（48 行＋4 汇总，提交即停 A3）。本批准为 prereg v2 生效条件链的最后一环。

## R1–R10 逐项裁定

**R1（1971-73 固定汇率时代处置）→ APPROVED：保留＋强制披露。** 理由：(1) T121 内核零结局量——固定价不可能偏倚任何被估计量；(2) 平报价机械上不产生网格穿越→该时代零触发、成本算术仅在真实触发上运行（自中性）；(3) 机械剔除属"依数据特征事后重选段"，为裁定保留给重新登记的事项；门在两种处置下均结构可达（12568 或 ~11830 > 4521）。**附加约束**：run report 必须在**全部行**（不限于 feasible 行）标注早年"每日一根平报价"粒度，与外推时距声明并列——列为 A3 约束性交付项。

**R2（grids 键集收紧）→ APPROVED（manifest 层即刻生效，代码收紧为未来登记前置）。** 现状代码（production_adapter.py:435/444）接受 grids 键集 ⊆ SYMBOLS（可为池超集）；本 release manifest 装配为**恰等** `{'USDJPY': 0.5}` == set(pool)，本运行不可能产生多余 waiting_data 行，48/4 机械保证成立。裁定：(a) 本 manifest 编码批准；(b) `set(grids)==set(pool)`（grids 存在时）的代码收紧**必须在任何未来携带 per-release grids 的 manifest 登记之前**经评审 diff 合入——不阻塞本次执行（避免作废已评审二进制 0082b19）；(c) v1 零变化不受影响。

**R3（overshoot payload 传输口径）→ APPROVED（传输编码＋情景声明双层，附强制条件）。** 内核门（feasibility.py:311-313）仅在 payload `basis=='stop_level' && version==2` 时执行越幅算术，否则 fail-closed 落 `overshoot_version_basis`；adapter 白名单已含 manifest 侧 `scenario_multiple_of_spread_p90`。数值上 payload hours p90 与 spread 组件逐字节相同（×1.0），传输算术==申报情景算术；任何编码偏差零算术。**强制条件**：(1) 物化时程序化断言 overshoot payload 与 spread payload 的 p90/n_days 逐字节相等；(2) run report 增加显式披露行："overshoot 为 1.0× 同品种同 NY 小时 p90 点差情景，经冻结 stop_level/version-2 内核门传输，无实测 stop-level 证据"；(3) **不改动 prereg v2 文档**补 §5——改 blob 将破坏已重算的双绑定（32aaa92c…）且不构成任何规则变更，披露落登记记录＋运行产物（§1"不冒充 stop_level 模型"已由 payload provenance 块满足）。

**R4（account_scope_id 编码）→ APPROVED。** `_id` 字符集 `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}` 现场确认排除 '@'；manifest 用 `<demo-login>-<demo-server>`（实际值见绑定工件 account-fee-terms.json 1aa60bd6…），与费用条款工件 `<demo-login>@<demo-server>` 的映射已记录于 provenance。同时**显式裁定**：account-fee-terms.json（1aa60bd6…，现场重算相符）作为 commission/swap 的 declared_terms 证据工件**获准绑定**本 release（其状态由 pending_registration_adjudication 经本次裁定闭合）；执行前必须存在且字节一致（adapter verified-read 强制）。

**R5（实现提交钉扎）→ APPROVED：钉 0082b19addd65d8b02bc9531ceb3e5959872bfec（当前 HEAD）。** 独立全量复核：32/32 CODE_FILES 的 git-blob SHA-256 与工作树 raw SHA-256 **零偏差**；CODE_FILES 键集与 adapter 常量恰等；HEAD==0082b19、树 clean；FOMC-only 中间提交（d12c835/af7801b/26b2f90）不在 CODE_FILES。运行期 `_implementation` 强制 ancestor＋blob/raw/loaded-code 四重同一，任何漂移 fail-closed。

**R6（锚读法）→ APPROVED：登记推导成立（中位 109.21 → 锚 125 → 步长 0.5）。** 先例锚集 {XAUUSD 1250, EURUSD 1.25, USDJPY 125} 恰为 1.25-级数 {1.25,2.5,5}×10^k 的三个成员，谱系支持 1.25-级数为操作读法；严格 {1,2,5}×10^k 读法给锚 100（0.4%×100=0.4→最近值仍 0.5），**步长在两种读法下同为 0.5，且 manifest 只绑定步长**；逐日中位交叉核 122.8 → 125 → 0.5 一致；裁定稳健窗 (87.5,187.5) 同时包含两中位数与两候选锚。解释性注记成立，无未决依赖。计数前锁定、事后禁改（2d.2）已由 manifest 结构落实。

**R7（研究库现场重验，前置 4）→ CLOSED（本次审核现场完成）。** calibrated.json e2cae60c… ✓；costs/spread_model.json c35550b9… ✓，其 watermarks 毫秒精确值（XAUUSD 1767308700025/1793204266005、EURUSD 1767312001291/1793204130156）与 adapter `MODEL_WATERMARK_WINDOWS` 常量、prereg §1 水位注记**三方逐字相符**（epoch 换算独立复核）；config/sessions.toml（研究库 LF 字节）a7bfe5d2… ✓，解析策略与 `SessionPolicy(exempt_symbols={'BTCUSD'})` 冻结等价（17:00 America/New_York、±5/60min、3×p90 闸、min_days 20）；bars/USDJPY/{H1,D1} 拷贝件 4/4 与源字节同一（H1 48855f91…/D1 94d969bd…，meta content_sha256 自洽；H1 全序列 180,090 行、首根 1971-01-03T22:00Z 与 meta 一致）；水位外包络 [2026-01-01T23:05:00.025Z, 2026-11-06T12:03:45.848Z] 与内核常量一致。

**R8（D60＋H 线链入，前置 5）→ CLOSED（闭合工件随登记步落盘）。** D60 同意（5cf9a989… ✓）范围=评估阶段扩池，经 prereg v2 §3 声明＋裁定 2c（本裁定链为其生效批准之一）＋本次登记批准闭合为 §1-bis 登记对象，链完整。H 线条目必须枚举本次装配的全部非结局聚合量：段内中位收盘（逐 bar 109.21／逐日 122.8）与覆盖计数（114,530/12,568/2,288 等）——除此外装配期无任何价格读取（authorized_read 单次聚合＋时间列投影，provenance 记录一致）。

**R9（sessions 字节源）→ APPROVED：绑定研究库 LF 字节（a7bfe5d2…）。** 现场 bytes 存在且哈希相符；demo 工作树 CRLF 变体（76278259…）不绑定；解析策略同一性由 adapter 运行期强制，双保险。物化步须原样拷贝研究库字节至 artifacts/sessions.toml。

**R10（quote_currency）→ APPROVED（备案）。** adapter L318 强制 `currency==quote_currency=='USD'`、内核证据硬编码两者 'USD'——该字段为冻结 schema 的账户侧币种，非对侧报价币；T121 无 JPY 账户换算路径（相对成本×价格、佣/隔夜经 contract size-per-lot 换算），无数值影响。

## A–E 裁定

**A. prereg v2 批准生效：是。** §1-bis 与 manifest 逐点一致（池{USDJPY}、48=4×1×3×4＋4 汇总、六组件表逐格对应——spread measured/2026 水位、slippage 0.1×p90、commission declared 0.0、swap declared free 0.0（显示值 0.0/−13.7 只披露）、contract spec_snapshot 100000/digits 3/tick 0.001/tick_value 0.6316879966646874、overshoot scenario 1.0×；锚机械规则 125→0.5；强制披露 1-4 全部在 a3 清单）。§4 冻结项零触碰采信 diff 评审的字节同一性结论并抽验冻结数字（4521、CUTOFF 2016-05-01 不含、0.02R 等号、DEFF1.5、冻结 grids XAUUSD 5.0/EURUSD 0.005）与代码常量相符；blob 双绑定 32aaa92c…/c78a78d1… 现场重算相符，rules 版本串 t121-offline-v1-usdjpy 正确路由 v2 blob。**生效范围限本 release；批后不得后改。**

**B. 六组件证据链：充分。** 全部证据 SHA 现场重算相符（1aa60bd6/1419a0b4/7abeb458/e2cae60c/转录件 390cfa76）；水位窗毫秒精确值工件↔adapter 常量↔manifest observed 三方一致（逐符号精确相等分支+内核外包络双层防御均经代码核实）；measured 聚合仅取 measured 组件（L641-642 修复后），declared/spec/scenario observed=null 强制；scenario multiplier=1.0 必填正有限；overshoot 无证据冒充路径。水印→发现段（applies）方向由 applies⊆[首根，CUTOFF] 强制，跨期外推仅以情景标注存在。

**C. 发现段与覆盖规则：成立。** 时间列-only 独立复核全相符：段内 114,530 bars（首 1971-01-03T22:00Z／末 2016-04-29T21:00Z，+1h≤CUTOFF）、12,568 个 UTC 日、周一至五 10,280/周六 0/周日 2,288（早年周日有 bar 的密度如实登记，周末行不自动无效，入场可许性归 sessions 运行时口径）、日历上限 16,555、最大连续间隙 18.0 天、>30 天缺口 0（推导规则=冻结 max_gap_days=30，执行装配重_deriv 必须复现 []，否则 halt）、逐年样本（239/251/250/250/312/311）一致、bars/日 p50=1/p90=24/max=24。特征预热 2000 根在 1970s 密度下约耗前 8 年、禁入窗（rollover 17:00 NY ±5/60min）与 hold/future 窗均**只减不增**合格触发日——12,568 仅为上界、官方门 4,521 严格按冻结规则、运行产物必须按实际合法覆盖重算可达性（强制 3），三 caveat 全部在册。**注记**：draft 信息字段 coverage_ratio 0.7588 应为 0.7592（12,568/16,555）——装饰性偏差，不入任何门，release.json 物化时机械重算或删除。

**D. 正式登记值（批准即生效）：**
- `execution_id` = **t121-release-v2-usdjpy-exec-001**（charset 相符；一次性，失败不回收）
- `snapshot_version` = 登记步 trust 快照机械赋值的 int（adapter 强制相等，非自由值）
- `valid_from` = **2026-11-08T00:00:00Z**（登记日＋1）
- `expires_at` = **2026-11-20T00:00:00Z**（裁定 §4 自 2026-11-06 的 14 天外界；review.json expires 取同值，decided_at/valid_from 取 2026-11-07，满足 decided_at ≤ review.valid_from ≤ manifest.valid_from、manifest.expires ≤ review.expires）
- `review_ref` = {artifact_id: t121-release-v2-registration-review, artifact_version: t121-release-v2, path: review.json, format: json, digest_domain: raw-bytes, sha256: review.json 落盘字节哈希}；review.json 按 t121-review-v1 全键写：decision=approved、authority_ref=usdjpy-release-ruling-2026-11-06（＋本报告）、reviewer_id/independence_basis=本独立批准者（未参与请求/裁定/装配/前序评审）、binding_domain=canonical-json-v1、binding_sha256=sha256(canonical(manifest − review_ref))
- `code.repo_commit` = **0082b19addd65d8b02bc9531ceb3e5959872bfec**（32 文件钉扎表原样）
- `conditions` = 3 项闭合：`research-reverify-2026-11-07`（本报告 R7）、`d60-hline-chain-2026-11-07`（R8）、`account-fee-terms-adjudicated-2026-11-07`（R4），各附 {condition_id, status:'closed', closed_at:2026-11-07T…Z} JSON 工件

**E. 执行边界：确认。** 恰一次运行；USDJPY-only 48＋4 完整输出（F1-F4×S1-S3×A1-A4＋4 族汇总）；零结局量（无 label_triggers/ProbTable/回测器路径；AST 依赖审计＋合成 tripwire 沿产出验证）；输出目录独占不存在-才创建（`not output.exists()` 已核实拒绝覆盖）；逐行情景标注/证据等级/账户范围/外推时距、分解披露（2.1×p90 vs 0.02R 对照 declared 小计）、NY17 结构披露、null/0 区分；官方门严格冻结规则、披露不得替代门；不启动 T122/A4/P1/P2；MT5 原生 H4/D1 不进计算（H4=H1 冻结重采样，H4/D1 原件不绑定）；提交即停 A3。

## 前置（全部为机械条件，随登记/物化步执行，不再需另轮裁定）

1. release.json 物化时**剥离全部下划线注记键**（`_input_spec`/`_payload_spec`/`_scope_field_notes`/`_code_note`/`_rules_note`/`_conditions_note`/`_review_note`/`_sessions_note`/`_sources_spec`）——adapter 各层 `_keys` 精确键集校验，残留即 halt。
2. 覆盖率信息字段重算（0.7592）或删除（见 C 注记）。
3. R1 早年粒度标注、R3 传输口径披露行、R3 payload 逐字节相等断言（三者为约束性 A3/物化交付）。
4. R2 grids 收紧作为未来任何 per-release grids manifest 的登记前置（登记记录在案）。
5. 六个 cost payload/sessions/sources 按规范物化后哈希回填；input.json 自拷贝 H1 时间列重推导 gap_spans 须复现 []。
6. 快照登记绑定 release.json raw-bytes 哈希；execution_id 一次消费。

## 核验记录（本报告即审核记录）

SHA 现场重算 22 项全符：manifest-draft 788a6966…、ruling 4644adb2…、diff-review 40a0d8a2…、prereg v1/v2 blob c78a78d1…/32aaa92c…、fee-terms 1aa60bd6…、cost-bundle 7abeb458…、spread-component 1419a0b4…、D60 5cf9a989…、转录件 390cfa76…、研究库 calibrated e2cae60c…/spread_model c35550b9…/sessions a7bfe5d2…、bars 拷贝与源 4/4、32×2 代码哈希。rules canonical SHA 以研究 .venv 解释器重算得 b7ce05c5…（相符）；水位 msc epoch 换算、4521/CUTOFF/SYMBOLS/白名单/内核门/_id 字符集/独占输出各经代码直读核实；时间列-only 覆盖复核如 C。

**结论：approved——登记 t121-release-v2 并按上述 D 值与边界执行一次性 T121 只计数/只成本运行。**
