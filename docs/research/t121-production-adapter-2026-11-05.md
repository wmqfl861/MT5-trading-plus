# T121 字节准入接线（2026-11-05）

状态：离线工程实现，待 D62 独立验收。只允许合成测试；没有真实 release、来源、工件、读取或计数批准，不改变 A2/A3、T122、D61 及阶段 F 前置。

## 入口与信任边界

`scripts/pline_feasibility.py` 保留无 `--mode` 的原 synthetic 命令；显式 `--mode production --release-id ID --release-manifest FILE --output NEW_DIR` 走字节准入。这两类参数互斥。固定静态 `production_releases.py` 仅承载 `SNAPSHOT_JSON` 字面量；正式版本为空、未初始化、无有效期，不宣称 fresh。适配器核对其实际 HEAD 与原登记来源 Git blob、工作树原 bytes，再解析数据，不执行信任文件。完整快照包含 schema、initialized、正整数版本、有效区间、registry、states 和 context；所有登记条目须明确 active/revoked，缺状态或未知状态值属 invalid，合法 registry 中无请求 ID 才是 unknown。无 JSON、CLI 或环境变量覆盖信任；未来真实登记仍需独立 review。未知/撤回/过期等拒绝不读材料、不建输出或 execution claim，但可信完整的新版本会保留观察水位。初始未初始化快照不创建状态。

`check_t121_admission(input_meta, cost_meta, review_binding)` 只是无 IO/clock 的声明摘要，恒为 `unverified` / `not_released`，不是完整元数据认证器，也不创建许可。`verify_artifact_bytes(raw, expected_ref)` 先核验文件原字节摘要，再解析传入的同批 bytes；不接受 canonical 摘要冒充文件身份。

## 固定 v1 清单

清单 JSON 对象精确包含 `schema=t121-release-v1`、`release_id`、`execution_id`、`snapshot_version`、`valid_from`、`expires_at`、`scope`、`input_ref`、`source_manifest_ref`、`sessions_ref`、`costs`、`code`、`rules`、`conditions`、`review_ref`。未知/缺字段、重复键、非有限数、错误容器、布尔冒充整数、无时区或倒序时间均拒绝。

- `scope` 固定 T121-count-cost、XAUUSD/EURUSD、H1→H1/H4、Demo/USD；明确非敏感 scope ID 和 account scope ID、申请/发现段半开区间，发现段终点固定为 `2016-05-01T00:00:00Z`。每项成本的账户范围必须一致。输出不包含 account scope ID 或工件路径。
- 文件引用精确含 `artifact_id`、`artifact_version`、`path`、`format`、`digest_domain=raw-bytes`、`sha256`。路径相对清单目录且不能越界。input/source manifest/review/成本为 JSON，sessions 为 TOML。
- input 为 `t121-input-v1`，两品种各含 bars、source_tags、显式 gap_spans；source manifest 为 `t121-sources-v1`，逐品种含 used_years、sources、excluded_months、覆盖申请范围的连续来源 segments。实际 bars 还需符合这些身份与范围；未知 source tag 保留旧规则的 waiting_data。
- 每个品种的 spread/slippage/commission/contract/overshoot/swap 六项均独立绑定工件、source ref、用途、品种、原账户范围、观测/适用半开区间、basis/units/conversion_basis、精确 dependencies。v1 只接直接原工件（source ref 等于 artifact ref），未定义转换的来源保持拒绝。slippage→spread，commission/swap→contract 依赖身份逐项核对。
- 原审核记录为 `t121-review-v1`，绑定 record/authority/reviewer/independence、结论、审核时刻及有效期，其 canonical-json-v1 摘要覆盖清单除 review_ref 以外的全部字段；review_ref 又以 raw bytes 绑定原记录。可信性来自固定注册表，单独填写 approved/hash 无效。非空 conditions 必须有逐项原始闭合证据。
- `code.repo_commit` 是稳定实现提交 I，固定 `CODE_FILES` 每项绑定 `raw_sha256` 与 `git_blob_sha256`；只排除静态 trust 数据文件，不允许调用者改闭包。I 必须是实际 HEAD H 的祖先，同时 I/H 的每个闭包 Git blob、工作树 bytes、本进程装载时的源码 bytes 须相符。只接受 exact bytes 或 Windows 明确完整 LF→CRLF 映射；不执行 Git filters/hooks。`rules` 绑定原 RULES 的 canonical-json-v1 和预注册 git-blob-bytes。清单绑定预定版本 v，不绑定最终 trust 摘要/登记 H；因此可以先固定 I、封印 manifest/review，再仅提交 trust 的 manifest rawSHA 登记。回执另记实际 H、I、完整快照 canonical 摘要及 trust raw/Git 身份和来源提交。

2026 经验摘要不能参与本用途；charged 生产格式仍拒绝。free 仍限已绑定的 Demo/USD 原账户单项，并不能补齐其他成本；未获独立 release 不会因字段完整而执行。获准路径的缺值、无覆盖和不足日数保持原等待/失败规则，不能推导 feasible。

## 一次消费与内核

在固定状态根的同一互斥协议内，先核对信任来源/完整结构/合法时间区间并持久观察版本与 canonical 摘要，再判 requested release 与当前有效性。可信更高版本即使 revoked、合法 unknown、过期或未生效也推进水位；invalid/未初始化不推进。同版本同摘要可重复观察，同版本异摘要或低版本拒绝且不覆盖，摘要不包含路径/HEAD/观察时刻。active 后清单/审核失败也不回退水位。水位损坏或持久化异常 fail closed，不清空、不降级。

读取 data/cost 前，claim 在同一共享锁内重新加载受信快照、比较当前版本/摘要与持久水位、重查 active/manifest rawSHA、审核和条件以及实现闭包。耗时 Git/磁盘/摘要核验与 claim 序列化全部完成后，紧邻独占创建重新读取系统 UTC clock，检查快照、清单、审核有效区间及条件闭合时间的交集，终点等号拒绝。claim 文件 fsync 和所需目录持久化完成后再次取时复核；若持久化期间越过有效期，保留已写 claim 与水位，拒绝 data/cost、计算及输出，不回收执行。新版水位先持久完成则旧 claim 拒绝；claim 持久完成并通过最终时效检查后属于已消费执行，不提供运行中撤回。生产根取 OS 用户 profile 固定目录，不受 HOME、APPDATA、XDG、数据根、输出或 worktree 影响。同一 execution ID 不能换 release 重用；进程中断留下 claim/锁，读取、解析、计算和输出失败不回收，无自动清理/重试许可接口。

占用后各工件每路径读一次，核验和解析复用内存 bytes。只窄提取已有 `_run_feasibility_kernel` / `_trigger_cost_kernel`，保持计数、资格、成本公式、96 行与 8 汇总。原公开 `run_feasibility` / `trigger_cost` 仍只接受 synthetic，production 工件没有改标 synthetic。新输出独占目录，receipt 包含 release/context/执行身份、manifest raw hash、各工件/代码/规则摘要和 report hash。

## 合成验证范围与限制

原私有边界 fixture 仅在测试中隔离 trust/clock/state，生产 CLI 无测试开关。fix1 的登记验收在临时真实 Git 历史执行 I→封印合成 manifest/review→仅提交 trust→新解释器正常 loader/main，启动器仅隔离 clock/state，不 patch trust、Git、准入或 claim 判断，验证 96/8、非零触发及已知非零成本。真实新进程验证 v2 revoked/合法 unknown/过期/未生效水位阻止旧 v1 副本、同版本冲突、未登记 trust/来源错/祖先闭包变化拒绝；以文件 IO 同步控制旧初检完成后新撤回先持久，旧进程再尝试 claim，禁止 data/cost 读取。已知 active 的重复 execution 可先读绑定 manifest/review 获取执行 ID，但须在 data/cost 与内核前拒绝，原 claim 不变。未知/撤回路径仍禁止所有材料读取。

原合成验证继续覆盖真实计数/成本、同批 bytes、独占输出、摘要域、篡改、范围/账户/时效/条件、失败不回收、两进程竞争及新解释器重启。跨路径使用临时完整 Git 源码副本和共享隔离状态，不创建第二个正式注册工作树；这些临时登记仅为生成的测试数据。

冷 import guard 在首次工程包导入前阻止 labels/tables/P1、backtest、research、SQLite、MT5/live 导入。旧冻结 gaps/volatility 允许导入 `mt5data.store` 的常量/函数定义；测试不调用 store IO，未改冻结依赖。聚焦回归包含完整旧 T121 测试（含读取受版本控制的公开冻结 `config/sessions.toml` 的原 CLI 用例），另以临时合成 sessions 验证旧 CLI 冷启动兼容。

这里只验证进程级重启与本机文件系统，未做 OS 重启、断电/存储故障注入或真实账户/材料验证；Windows 没有 POSIX 目录 fsync，文件内容仍 fsync。固定用户状态不能防止有权修改代码/用户状态的操作者主动删除记录；新增真实 release 前仍需独立验收这些运行假设。没有研究 DB、标签、拟合、收益或真实执行证据。
