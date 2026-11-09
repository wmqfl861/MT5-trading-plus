# R3 关闭留痕证据（分段重算 + run5 离线复现）——公开拷贝

R3 关闭报告 `docs/research/r3-closure-report-2026-11-07.md` 引用的两组计算
证据原工件存于研究库本地 `.superpowers/sdd/` 目录（不入公开仓库）。本目录
（D72 / T171）是它们的**脱敏拷贝**：除"脱敏"节列出的替换外逐字保留。台账
`data/research/ledger.sqlite` 仍是唯一权威记录；本目录全部脚本**只读或经
TEMP COPY**，对台账零写入、零运行、零预约。

## 文件清单与来源

| 文件 | 来源目录（研究库 `.superpowers/sdd/`） | 内容 |
| --- | --- | --- |
| `t150-closure-segments.py` | `2026-11-07-r3-main-test-run6/` | T150 分段重算脚本（离线、确定性、报告层） |
| `t150-closure-segments.json` | 同上 | 上列脚本输出 |
| `t150-closure-segments.txt` | 同上 | 上列脚本 stdout 日志 |
| `v3-replication.py` | `2026-11-07-r3-main-test-run5/` | run5 中断后 V3 离线确定性复现：冻结装配 + 身份核对 + 随机化 draws（分块 + JSONL 断点） |
| `v3-draw-worker.py` | 同上 | 1999 次 draw 的分片 worker |
| `v3-finisher.py` | 同上 | 分片合并 + 生产聚合公式 + 引擎等价反事实 |
| `v3-offline-replication.json` | 同上 | 最终输出（身份、引擎等价结果、崩溃前数值、A1-D、E7、N 组成） |
| `v3-replication-log.txt` | 同上 | 复现日志 |
| `a1d-e7-obligations.txt` | 同上 | A1-D（月末 15/213 子集）与 E7（每事件夜数）义务数值 |

## 数据来源与口径

- **输入 = 与 run#451 / run#452 完全相同的冻结装配**：FOMC 日历工件
  （sha256 `a53577d9…971c5`）、H102 卡（USTEC D1）、研究库成本缓存
  （`rel@NY/bar_open_m1#abed3b8f` p90；swap-snapshot `d5b50486` /
  relref `257d0355`）、合并 FRED+MT5 序列（data_sha256
  `h-input-v1:f233c9bb…f645`）；主段事件窗 pool=[1986-01-02, 2021-05-17)。
- **身份核对（等价锚点）**：run6 脚本对 run#452 四项
  （data_sha256 / cost_model_version / params / strategy_sha 前缀）全
  MATCH，且 213 事件 observed_mean 与固定 10 年段四桶均与台账 V3 行
  **逐位相同**；run5 脚本对 run#451 同四项 + gates_hash 全 MATCH。
- **引擎代码**：主库工作树 `scripts/bt.py`（run6 时 blob `065abf7`＝写入
  run#452 的引擎；run5 复现时工作树 HEAD `72d1014`，见各脚本 docstring）。
- **确定性**：随机化 seed `10640155159524715549`（= `vstats.rng_seed("H102")`，
  与 G4 行及 run5 复现一致），1999 draws；分段口径＝生产
  `segmented_means` 的 entry_time 年分组惯例，边界 1994-01-01 /
  2004-01-01 / 2014-01-01 / 主段窗终点 2021-05-17（开区间）。2021-05-17
  之后为登记**非盲诊断段**（R3.toml advance_rule），不进主检验、不进 N、
  不进判定。

## 与报告的关系（`r3-closure-report-2026-11-07.md`）

- §5 固定 10 年段表 ← `t150-closure-segments.json`
  `main_segment.ten_year_buckets_reproduced`（与台账逐位相同）。
- §5 T150 三段表（75 / 80 / 58 事件）← `main_segment.t150_segments`。
- §6 非盲诊断段表（35 事件＝FRED 尾段 1 + MT5 段 34）←
  `diagnostic_post_boundary`（同一脚本、同一冻结装配机械复算）。
- §7 A1-D 月末重叠子集表 ← `v3-offline-replication.json` `a1d` 节与
  `a1d-e7-obligations.txt`（15 个重叠日清单）。
- §8.1 E7 夜数复检 ← `v3-offline-replication.json` `e7` 节
  （212×1 夜 + 1×2 夜 = 214）。
- §3 确定性预期对照（p=0.0085、seed 一致）← `engine_equivalent_v3_result`
  与 `report_only_precrash.primary`（引擎本会写出的 V3 行＝中断反事实，
  未写台账；台账 run#451 至今无 V3 行）。

## 脱敏说明

- 绝对路径前缀（本机用户目录下的工作区根，即
  `C:/Users/<用户名>/.zcode/workspace/` 形态）→ `<workspace>/`
  （出现在各脚本的 `MAIN` / `RESEARCH` 常量与 t150 日志末行）；
  `<workspace>` 为该工作区根目录占位符，`demo-next-20261105`＝主库工作树、
  `664d5945-…` ＝研究库工作树。
- 账号@服务器 / 真实服务器名 / 公网 IPv4：原件即无出现，无需占位。
- 本目录经仓库 hygiene 扫描（`tests/test_repo_hygiene.py` 四类模式）零命中。
- 未收录（留在本地来源目录）：8 个 `v3-draws-shard-*.jsonl` 与
  `worker-*.log`（中间产物，可由 seed + 脚本确定性重生成）、RUN5-REPORT.md
  本体（`2026-11-07-r3-main-test-run5/RUN5-REPORT.md`）。
