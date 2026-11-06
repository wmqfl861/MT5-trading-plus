# H 真实输入装配器与主段预筛（2026-11-06，provisional）

本文记录 `scripts/h_assemble_real_precheck.py`（装配器 + 一次性真实预筛 CLI）、`src/mt5quant/h_readonly_precheck.py` 新增的 real 资格分支，以及 2026-11-06 对主段 [2007-01-01, 2021-05-17) 的唯一一次真实预筛运行。产物恒为 `research_status=not_evaluated`、`execution_status=not_released`，且标注 **provisional**：聚焦独立评审通过前不交 A1。依据：`.superpowers/h-real-precheck-scope-request.md` 的独立范围裁定（conditional approved，条件全部落入实施计划 `docs/superpowers/plans/2026-11-06-h-real-precheck.md`）。

## 绑定与资格门（打开任何价格工件之前完成）

七个研究库工件按**相对路径 + SHA256 双绑定**（`REAL_WHITELIST`，与计划白名单逐字一致；命令行不暴露任何覆盖入口，`verify_hash` 参数仅供测试绑定合成副本）：`ext/histdata/EURUSD/H1/{data.parquet, manifest.json}`、`ext/fred/USTEC/D1/{data.parquet, manifest.json}`、`costs/{calibrated.json, spread_model.json}`、`specs/latest.json`。任一哈希不匹配、缺文件或白名单键集不符即 fail-closed；未绑定路径从不读取。manifest 逐年 used 门：窗口触到的每个自然年（EURUSD 2007–2021；USTEC 2006–2021）须 `used=true`——FRED manifest 无逐年声明，登记为 `no_yearly_verification_declared`。2019 按裁定纳入（used=true；provenance 登记 inconsistent + 月度回收 + 定盘锚点 consistent），2019-10（undetermined、未回收月）按规则显式登记为空洞 [2019-10-01T00:00, 2019-11-01T00:00)，落在洞内的 bar 自动切除，不手工加剔；年内违例月由冻结周日开盘规则自检（本次实测丢弃 `2020-10`，见结果 `bad_months`）。价格解码在全部资格校验之后：pyarrow 仅投影 `time+close`，从已校验字节单次读取（`ParquetFile(BytesIO(raw))`），先过滤窗口再进一切计算。MT5 序列不进输入（tags 全为 histdata/fred）。

## 预注册缺口推导规则（写入产物 `provenance.gap_rules`）

- **EURUSD/H1（小时网格）**：每个 UTC 周一 00:00 → 该周最后一根周一至周五 bar 开盘+1h（周五收盘）的带内，完整小时网格中无 bar 的极大连续段记缺失 [首根缺失bar开盘, 下一根既有bar开盘)。周末构造性排除；带内假日/稀疏小时保守记缺失；整周无 Mon–Fri bar 则无带不推导（由显式登记与冻结 >30 天切段兜底）。
- **USTEC/D1（工作日网格）**：首根至末根 delivered bar 间的每个工作日为期望网格，缺失工作日的极大连续段记 [首缺失日 00:00, 下一根既有bar 00:00)；周末构造性排除。
- 显式登记：manifest 回收排除月（used 年 salvage=true 且 monthly 条目不在 salvaged_months）→ [月初, 次月初)。推导与显式登记取并集（合并交给冻结 `_union`）。规则判空才传 `[]`。

## 成本口径（provenance 逐项登记）

commission = calibrated.json EURUSD round_trip **0.0**（成交实测：deals_in 8 / deals_out 12 / 0.13 手）；swap = **规格快照、非成交实测**（calibrated.json swap.snapshot：long −5.9 / short 0.0，绝对 USD/手/夜，无 ref_price/contract 相对基准，triple=周三）；spread = spread_model.json 标准结构（relative / bar_open_m1 / America/New_York 小时桶，model_sha256 `abed3b8f…`；2026 tick 基准施于 2007–2021 属**情景外推**，产物记录 24 小时 p90 统计量与校准 watermark，来源层加成 ×1.5 = 冻结 `external-spread-x1.5-v1`）；slippage = 项目口径 0.1×p90（docs/reports/scope-2026-10-25.md）；contract = 100000（specs/latest.json）。

## real 资格分支与互斥

`h_readonly_precheck` 新增 `required_eligibility` 关键字（缺省 `synthetic_only`，synthetic 行为与旧消息逐字节不变）：real 模式恰接受 `real_precheck_only` 声明，synthetic/real 输入互串双向 PermissionError；非法资格值 ValueError。冻结 CLI（`scripts/h_readonly_precheck.py`）本任务零改动，`--mode` 仍只收 synthetic——真实运行走装配器直调 API（计划允许的最小方案）。`cumulative_n` 恒 None（单次功效门不依赖 N；α/N 功效 unknown）。

## 唯一一次真实运行（结果摘要）

命令（cwd=本工作树，既有 venv 解释器，PYTHONUTF8=1）：

```
python scripts/h_assemble_real_precheck.py \
  --data-root <研究库 data_research 绝对路径（只读）> \
  --candidate docs/research/drafts/t114/C002.toml \
  --output <独占输出目录>
```

产物 `result.json`（payload SHA256 `0c976e42e1f8edd38ddedfd33c37e02b43a893665c08b4d516189fc697435cd3`；stdout 与文件逐字节一致）：候选 C002（T114 离线草案，未提升）；`diagnostic_status=engineering_waiting`，reasons = `single_power_below_minimum` + `cumulative_n_unknown`。EURUSD/H1 交付 88,260 bar（窗口内 88,786 − 2020-10 违例月 526），覆盖 14.098 年、29 段；USTEC/D1 交付 3,644 bar（26 根预热），13.136 年。缺口：EURUSD 显式 1 + 推导 28（并后 28，2019-10 空洞恰为 [2019-10-01T00:00, 2019-11-01T00:00)）；USTEC 推导 132。事前成本倍数 **3.448**（来回 8.99bp：点差 7.93bp×1.5 来源加成 + 滑点 1.06bp + 佣金 0 + 隔夜 0；参考价 1.21498）≥ 3 通过；单次检验力 **42.4%** < 50%（σ=0.00182、implied SR 0.419 < min-detectable 0.785；年数取 min=13.14，USTEC 侧因空洞切段连周末日一并扣除属冻结 runtime_coverage 口径）——主段（不含 2021-05 后 MT5 延长段）不过功效门，如实记录，等待聚焦评审与 A1 裁量。

## 测试与证据

新套件 `tests/test_h_real_precheck.py`（13 项，合成 parquet 副本，不绑真实路径）：白名单哈希不匹配/缺文件/键集不符拒绝、未绑定文件不被读、used=false/缺年/FRED 逐年门在价格解析前拒绝（stub 非法 parquet 字节证明顺序）、窗口过滤与 2019-10 切除、provenance 登记、synthetic/real 互串双向拒绝、非法资格值拒绝、小时带/工作日网格缺口规则单测（周末排除/假日洞/跨周末连洞/31 天空周不推导/收盘后小时不在带内）、冻结 CLI `--mode real` 与 real 资格仍被拒、装配器 CLI 对合成根 fail-closed、CLI 无白名单覆盖旗标、`REAL_WHITELIST` 与计划逐字一致。旧 `tests/test_h_readonly_precheck.py` 61 项全绿（synthetic 模式与 synthetic_only 门逐字节不变）。RED（13 failed）→ GREEN（13 passed）、旧套件 61 passed、真实运行 stdout/退出码与产物哈希存本工作树 `.superpowers/sdd/2026-11-06-h-real-precheck/`（不提交）。本产物为工程诊断：不构成研究结论、不构成生产输入放行、不触发任何执行。
