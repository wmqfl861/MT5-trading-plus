# D61 估计器 v2 验证设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 D61 及 P2-10 已登记后续提案形成可独立评审、版本化的 v2 纯合成验证设计，不运行统计矩阵或更换产品估计器。

**Architecture:** 只增加新的设计文档，引用既有P2-10的RED证据与生成器接口。预写候选算法、条件矩阵、种子、指标、判定和停止规则，后续实现与运行均服从本设计独立审核结论；旧研究和产品门槛原样保留。

**Tech Stack:** Markdown、现有Python研究器静态参考。

**Spec:** `docs/research/p2-10-coverage-2026-11-01.md` §8–§9；`docs/blueprint-p-line.md` D61；交接 §2/D62。

## Global Constraints

- “按 §8 的版本化提案准备验证设计，经审核后才能运行；当前不修改估计器与冻结门槛，不重跑本研究。”
- “阈值 lb_z=1.0 / 2×成本 / min_cell=100 / m=100 一律不动。”
- 主会话与开发代理不得自我批准，不得填写 `approved_by`；模块技术通过不代替研究批准。
- 不读取真实数据或旧研究原始产物，不运行模拟矩阵、labels/fit或任何研究命令，不修改生产估计器、冻结哈希、配置、旧报告/旧RED。
- 不为并行而扩展到T122、方法标定或数据采集。共同约束见本地 constraints.md。

## Review Focus

- 点态覆盖与选中决策误开放的统计对象必须清楚，不用矩阵均值掩盖失败条件。
- 日/簇有效样本、重复行不增n、零/全胜边界和r不确定度同时可被证伪。
- 多档格间异质性与共享基准污染必须覆盖，不能只沿用浓度40。
- 生成器真值、相关结构、K跨界处理不能与候选估计器共享同一计算错误。
- 规格、种子/重复次数、分析规则和停止条件先于运行冻结，任何调参产生新版本。

### Task 1: 准备独立版本的D61验证设计

**Files:**
- Create `docs/research/d61-estimator-v2-validation-design-2026-11-03.md`。
- Read-only references: `scripts/p2_10_study.py`, `tests/test_p2_10_study.py`, 已有P2-10预注册/最终报告与相关生产接口，仅按设计所需定位阅读。

**Interfaces:**
- 输出完整但未运行的研究设计。算法/矩阵/种子/判定应可据此实现，无“运行后选择最佳”或可移动门槛。
- 设计明确标注待独立审核；没有研究批准字段或伪造结果，旧RED仍生效。

- [ ] Read the named spec and limited existing generator/estimator signatures. Identify exact quantities needing replacement; retain baseline for comparison.
- [ ] Specify a bounded candidate set with explicit formulas/pseudocode and units addressing day/cluster effective n, boundary regularization, per-action shared target contamination and return mean uncertainty. Any new choice must be labeled proposed, justified without simulated outcomes, and must not relax frozen product thresholds.
- [ ] Prewrite independent generator truth and a concrete finite condition matrix covering homogeneous plus multiple heterogeneity strengths, extreme and mixed cells, cross-cell contamination, cluster sizes and repeated rows, small/large independent-day n, return uncertainty and K-boundary/truncation. State sizes and reproducible deterministic seed derivation without executing simulations.
- [ ] Define estimands, per-condition coverage, false-open rate and uncertainty accounting, planned acceptance/rejection with multiplicity treatment, sample/rep counts, deterministic controls, known-negative baseline, stopping and rerun/version rules. No criterion based on choosing favorable runs/conditions.
- [ ] Describe synthetic unit checks, independence tripwires, code/inputs/protocol hashes, append-only outputs, future implementation/review/execution gates. Preserve P2-10 RED/T92 block and requirement for later product code review/full regression/CI.
- [ ] Perform static checks of references, matrix sizes/constant arithmetic and document consistency only; no fitting or generator execution. Do not add mirror tests for prose. Self-review, commit only new document with [P] prefix; report evidence and unresolved methodological questions to controller for independent scientific/spec review.
