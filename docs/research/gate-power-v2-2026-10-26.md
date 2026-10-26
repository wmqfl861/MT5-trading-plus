# 关卡正负对照实验 v2（gate-power-v2，T42 / T39 正式验收重跑）

> 取代 `docs/research/gate-power-2026-10-26.md`（该版系修复前分片的诊断产物，
> R7-N-I1 判定"正式统计验收未完成"）。本版五分片全部启动于修复后代码
> （提交 2fa27a3，2026-10-26 12:43 UTC），产物 meta 带运行指纹并经合并器
> 一致性校验（分片间指纹不一致即拒绝合并，测试覆盖）。

- **运行指纹**：git `2fa27a3f5f75`（gates[`68f49237d1b5`]、脚本[`2f4813dcbade`]）；
  工作区"不干净"= 启动时唯一未跟踪文件 `config/risk-demo.toml`（T44 影子盘
  演练配置，与实验代码路径无关——启动时完整 `git status --porcelain` 归档于
  `data/reports/gates/shard8-launch-state.txt`，该文件当晚已随 2a0b386 提交）。
- **成本版本**（5 序列，全部含越幅 `ov[eca09e24]`——stop_level 口径这次真实接入，
  见 §3 差异归因）：`tick-spread-p90[rel@NY/bar_open_m1#01888819]+slip0.1-…-ov[eca09e24]-sess[a7bfe5d2]`，
  逐序列完整串见 `data/reports/gates/gate-power-v2/gate-power.json` 的
  `meta.fingerprint.cost_versions`（EURUSD comm0-calibrated，其余 comm0-cli；
  USTEC/XAUUSD 带 missNY[17] 禁入小时）。
- 种子 0–19（5 分片 × 4 并行）、档位 [0.3, 0.6, 1.0, 1.5]、零假设按卡持仓周期
  auto 分派（日内 slot / 多日 day）、G2 随机入场实验降次 50（正式关卡 200 不变）。
- **合并产物**：`data/reports/gates/gate-power-v2/gate-power.{json,md}`——md 含
  全部原始表 + 三节补充（完整组合通过率 / 校准逐种子分布 / 分品种 G5 误报率），
  本文件只做解释与验收陈述，数字以合并产物为准。

## 1. 运行构成与独立样本数（R7-限定 a）

800 条记录 = 负对照 160（6 基线 × 20 种子 + RND×2 序列 × 20 种子）+
正对照 160（2 品种 × 4 档 × 20 种子）+ G5 480（检出臂 160 + 误报臂 320）。
G5 误报臂沿四档重复同一零假设窗口，去重后**独立条件 = 80**（每 null
2 品种 × 20 种子）；所有通过率按上述分母解释，不按 800 条独立样本计精度。

## 2. R7 解释限定（a–i）逐项回应

| R7 限定 | 本版处理 |
| --- | --- |
| (a) 运行构成 | §1 如实分列；精度按独立条件数解释 |
| (b) auto 零假设分派 | 口径不变（按卡持仓周期），各表按 null 分列呈现 |
| (c) 完整组合通过率 | md 补充节直接呈现：**正对照 passed_all 0/160、负对照 0/160**（day 0/60、slot 0/100）——G0–G4 完整组合在零假设与植入优势下全部拒绝，分检查表的高通过率不改变这一事实 |
| (d) 结构性检查 | G0 / buy_hold / cross_symbol / mc_drawdown / G5.trade_count 不参与决策规则标记（口径备注不变） |
| (e) slot 零假设范围 | 槽位置换保留槽位无条件均值——结论限定在"日内先后依赖与跨日次序类优势为零"，不外推为一切日内优势为零 |
| (f) 校准分布与容差 | md 补充节给出 8 组逐种子分布；**USTEC M5 ≈1.0 档未收敛**（seed=0 实测 1.343 超出 ±0.15 容差，按 best effort 接受并如实标注）——该档检出率对应的是"实际强于标称"的优势；XAUUSD ≈1.0 档 20 种子均值 0.668（中位 0.750，−0.098～1.118），标称强度与实测分布的差距仍在，低检出率不能全部归因于门槛 |
| (g) G5 参照设计 | 参照 = 无植入策略全研究段收益的块自助（G3 样本外参照的替身），与真实 G5 流程不可比——`sharpe_vs_reference` 的 95%/88%（slot/day）**不解释为真实 G5 的优势误报率**，该检查本用于识别相对参照的劣化 |
| (h) flags 定性 | 13 条 flags 维持**探索性诊断**定位（§4），20 种子共享历史路径与重叠窗口，不构成逐项放宽门槛的依据 |
| (i) 分品种 G5 | md 补充分列：**G5.passed 误报——slot：XAUUSD 50%、USTEC 5%；day：XAUUSD 35%、USTEC 0%**——品种异质性显著（XAUUSD 参照 p5 更低是主因，同 g） |

## 3. v1 → v2 差异归因（越幅真实接入的复跑证据）

v1 诊断分片（01:51 UTC 启动）运行时研究库成本缓存**尚无 stop_level 越幅表**
（R7-C4：v1 产物全部 no-overshoot）；R7-3 重建缓存（`ov[eca09e24]`）后本版
分片才启动。数字差异与该口径切换一致：

| 检查（负对照 slot） | v1 | v2 | 方向 |
| --- | --- | --- | --- |
| G1.net_profit_positive | 4% | 10% | 越幅抬高成本→零假设下净利更难为正？"为正"反而变多，源于置换路径与出场结构的交互，如实记录 |
| G2.random_entry_benchmark | 12% | 9% | ↓ |
| G3.oos_sharpe | 6% | 10% | ↑ |
| G5.sharpe_vs_reference（slot） | 98% | 95% | ↓ |
| 分品种 G5.passed：USTEC slot | 15% | **5%** | 越幅接入后 USTEC 零假设窗口更难过 G5 |
| 分品种 G5.passed：XAUUSD slot | 40% | **50%** | XAUUSD 参照下零假设通过率上升 |

结论：**越幅接线在本版产物中由 cost_versions 的 `ov[eca09e24]` 直接自证**
（R7-N-C4"版本串声称含越幅但缓存没有"的漏洞关闭），无需再依赖日志推断。

## 4. 决策规则与门槛建议（只建议）

13 条 flags（12 个不同检查；随机入场基准双触发）与 v1 集合一致，
全部列于合并产物"门槛调整建议"节。要点：

- 低检出（1.0 档 <50%）：G1.gross_cost_multiple（USTEC 0%）、G1.profit_factor
  （USTEC 0%）、G2.cost_scaling（USTEC 20%）、G2.drop_best_5pct（两品种 0%）、
  G2.random_entry_benchmark（XAUUSD 40%）——日内高频成本结构下这些子检查
  对 1.0 档优势不敏感，与 §2(f) 标称-实测差距叠加，不宜单归因于门槛；
- 误报超 5%：G1.net_profit_positive（day 12%）、G2.delay_1_bar（day 15%）、
  G2.param_plateau（slot 42%）、G3.oos_sharpe（day 23%）、G5.expectation_positive
  （slot 28%）与 G5.sharpe_vs_reference（95%，见 §2g 不可比限定）。
  结构性检查（G1.trade_count 100% 等）不参与标记（§2d）。

**门槛不动**：修改 `config/gates.toml` 须用户批准并在同一提交更新
FROZEN_GATES_SHA256 重新冻结。本报告不改变任何门槛。

## 5. 验收陈述（阶段 C）

- T42 验收项逐条对上：运行指纹 ✓（含 git 提交，合并器校验）、缓存 stop_level
  同步 ✓（五序列 `ov[eca09e24]`）、5 分片种子 0–19 并行 + 合并 ✓（种子不重叠/
  条件完整/身份一致校验全过）、正式报告 ✓（本文件）、蓝图 §2 P4/P5 与 §5 C 行
  更新 ✓（随本提交）。
- **T39（关卡标定的正式统计验收）就此补齐证据链**：正负对照 + G5 判别力 +
  双零假设 + 解释限定逐项落地。G5 放行仍维持蓝图禁止事项的其余前置
  （台账并发/审计闭环的复核交本轮 codex 终审；锁箱使用预算 D16 仍待用户决策）。
- 后续（阶段 G）：三张 v2 卡（H002-v2 / H003-v2 / H006-v2，b75092c 已提交，
  先卡后测就绪）可在本轮终审通过后开跑 screen。
