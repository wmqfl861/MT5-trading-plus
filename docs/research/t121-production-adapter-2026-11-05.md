# T121 字节准入接线（2026-11-05）

状态：离线工程实现，待 D62 独立验收。只允许合成测试；没有真实 release、来源、工件、读取或计数批准，不改变 A2/A3、T122、D61 及阶段 F 前置。

## 入口与信任边界

`scripts/pline_feasibility.py` 保留无 `--mode` 的原 synthetic 命令；显式 `--mode production --release-id ID --release-manifest FILE --output NEW_DIR` 走字节准入。这两类参数互斥。生产注册表固定于受审代码、初始为空，仅映射 release ID 到清单 **raw bytes SHA256**；当前撤回快照也由代码提供，没有 JSON、CLI 或环境变量覆盖。未知 release 在打开清单及 data/cost 前拒绝，不创建输出或消费记录。未来新增真实条目、当前快照与原审核来源仍需独立 review，不能由实现者自填。

`check_t121_admission(input_meta, cost_meta, review_binding)` 只是无 IO/clock 的声明摘要，恒为 `unverified` / `not_released`，不是完整元数据认证器，也不创建许可。`verify_artifact_bytes(raw, expected_ref)` 先核验文件原字节摘要，再解析传入的同批 bytes；不接受 canonical 摘要冒充文件身份。

## 固定 v1 清单

清单 JSON 对象精确包含 `schema=t121-release-v1`、`release_id`、`execution_id`、`snapshot_version`、`valid_from`、`expires_at`、`scope`、`input_ref`、`source_manifest_ref`、`sessions_ref`、`costs`、`code`、`rules`、`conditions`、`review_ref`。未知/缺字段、重复键、非有限数、错误容器、布尔冒充整数、无时区或倒序时间均拒绝。

- `scope` 固定 T121-count-cost、XAUUSD/EURUSD、H1→H1/H4、Demo/USD；明确非敏感 scope ID 和 account scope ID、申请/发现段半开区间，发现段终点固定为 `2016-05-01T00:00:00Z`。每项成本的账户范围必须一致。输出不包含 account scope ID 或工件路径。
- 文件引用精确含 `artifact_id`、`artifact_version`、`path`、`format`、`digest_domain=raw-bytes`、`sha256`。路径相对清单目录且不能越界。input/source manifest/review/成本为 JSON，sessions 为 TOML。
- input 为 `t121-input-v1`，两品种各含 bars、source_tags、显式 gap_spans；source manifest 为 `t121-sources-v1`，逐品种含 used_years、sources、excluded_months、覆盖申请范围的连续来源 segments。实际 bars 还需符合这些身份与范围；未知 source tag 保留旧规则的 waiting_data。
- 每个品种的 spread/slippage/commission/contract/overshoot/swap 六项均独立绑定工件、source ref、用途、品种、原账户范围、观测/适用半开区间、basis/units/conversion_basis、精确 dependencies。v1 只接直接原工件（source ref 等于 artifact ref），未定义转换的来源保持拒绝。slippage→spread，commission/swap→contract 依赖身份逐项核对。
- 原审核记录为 `t121-review-v1`，绑定 record/authority/reviewer/independence、结论、审核时刻及有效期，其 canonical-json-v1 摘要覆盖清单除 review_ref 以外的全部字段；review_ref 又以 raw bytes 绑定原记录。可信性来自固定注册表，单独填写 approved/hash 无效。非空 conditions 必须有逐项原始闭合证据。
- `code` 绑定完整 Git HEAD 和入口、适配器、计数成本依赖的原文件字节摘要；`rules` 绑定原 RULES 的 canonical-json-v1 和预注册的 git-blob-bytes，后者沿用旧固定 SHA，不把 Windows 检出换行与 Git blob 摘要混用。

2026 经验摘要不能参与本用途；charged 生产格式仍拒绝。free 仍限已绑定的 Demo/USD 原账户单项，并不能补齐其他成本；未获独立 release 不会因字段完整而执行。获准路径的缺值、无覆盖和不足日数保持原等待/失败规则，不能推导 feasible。

## 一次消费与内核

入口读取系统 UTC clock，检查当前信任/撤回版本、清单、原审核、范围、条件和代码规则；data/cost 读取前以独占创建、文件 fsync 和持久快照版本记录原子占用 execution_id。生产根取 OS 用户 profile 下固定状态目录，不受 HOME、APPDATA、XDG、数据根、输出或 worktree 影响；同一 execution ID 不能换 release 重用。进程中断留下 claim/锁，读取、解析、计算和输出失败不回收。旧快照不能降低已持久记录的版本，也不提供自动清理/重试许可接口。

占用后各工件每路径读一次，核验和解析复用内存 bytes。只窄提取已有 `_run_feasibility_kernel` / `_trigger_cost_kernel`，保持计数、资格、成本公式、96 行与 8 汇总。原公开 `run_feasibility` / `trigger_cost` 仍只接受 synthetic，production 工件没有改标 synthetic。新输出独占目录，receipt 包含 release/context/执行身份、manifest raw hash、各工件/代码/规则摘要和 report hash。

## 合成验证范围与限制

私有 fixture 仅在测试中 patch 内部 trust/clock/state，生产 CLI 无测试开关。验证真实 main 经字节准入到非零触发和手算成本、同批 bytes、独占输出、未知 release 读前拒绝、双摘要域、篡改、范围/账户/时效/撤回/条件、失败后不回收、两进程竞争和新解释器重启。跨路径测试从临时源代码副本加载入口并复用同一隔离状态，未创建第二个注册 Git worktree。

冷 import guard 在首次工程包导入前阻止 labels/tables/P1、backtest、research、SQLite、MT5/live 导入。旧冻结 gaps/volatility 允许导入 `mt5data.store` 的常量/函数定义；测试不调用 store IO，未改冻结依赖。聚焦回归包含完整旧 T121 测试（含读取受版本控制的公开冻结 `config/sessions.toml` 的原 CLI 用例），另以临时合成 sessions 验证旧 CLI 冷启动兼容。

这里只验证进程级重启与本机文件系统，未做 OS 重启、断电/存储故障注入或真实账户/材料验证；Windows 没有 POSIX 目录 fsync，文件内容仍 fsync。固定用户状态不能防止有权修改代码/用户状态的操作者主动删除记录；新增真实 release 前仍需独立验收这些运行假设。没有研究 DB、标签、拟合、收益或真实执行证据。
