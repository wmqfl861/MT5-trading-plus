# T121 离线计数成本入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成 T121 预注册明示尚缺的只计数/成本入口，交付合成测试和独立代码审核；不执行真实筛查。

**Architecture:** 在既有计数和资格函数上增加明确周期/预计算价位参数，默认行为保持。以一个用途受限的纯内存模块完成完整 H4、成本可用性和96行报告，并提供薄命令入口；不经过候选预筛、研究门或标签器。输入须明确指定、输出独占、工件和规则身份可追溯，缺证据整行 waiting_data。

**Tech Stack:** Python、pandas、pytest；复用现有成本、会话、ATR和动作函数。

**Spec:** `docs/research/pline-feasibility-prereg-2026-11-02.md` §2–§8；`docs/reviews/2026-11-02-t121-a2-review.md` 最终结论；`docs/reviews/2026-11-02-t121-a3-review.md` §3；交接D62离线续行范围。

## Global Constraints

- “主会话与开发代理不得自我批准，不得填写 `approved_by`。”
- “研究运行必须属于已批准、已开启的轮次。”
- “不在真实事件或真实触发上计算任何收益或结局来‘看看’；可行性筛查只允许数触发和算成本。”
- “不新增框架性模块；修复优先在原位做最小改动。”
- “仓库是公开的。账号（含模拟盘账号）、服务器名、IP、令牌、本机绝对路径、`config.json`、`mcp.json` 一律不入库。”
- 本模块仅离线工程及合成验证。T121报告仍未运行waiting_data，T122/P1/P2关闭；2026后期成本摘要不用于任何诊断或设计判定。冻结candidates/gate/power/tables/decision/level_decision/prob_mode、gates.toml及成交引擎不改。
- 共同约束另见主会话提供的本地 constraints.md。

## Review Focus

- 四根不连续、来源未知或截止边缘的H1不能被聚合成貌似合法的H4。
- S2桶内跨纽约17:00与DST不允许提前用尚未完成会话；资格窗必须包含真正H1来源窗。
- A2/A4 anchor后的跨洞不能通过动作根回看掩盖；S3通道与资格回看均为H4=120/H1=480。
- 任一合格触发成本缺项保留整行waiting_data，非法数值保留cost fail，禁止删触发后通过。
- 输入身份、审批用途和范围错误不能因给出一个hash或默认0被接受；真实未来价格不得参与资格或计费。

### Task 1: 实现T121入口和合成验证

**Files:**
- Modify minimally: `src/mt5quant/prob/counting.py`, `src/mt5quant/prob/gaps.py`。
- Create: `src/mt5quant/prob/feasibility.py`（用途限定的纯内存计数/成本计算，必要时一个小数据类即可，禁止插件或通用框架）。
- Create: `scripts/pline_feasibility.py`（薄CLI，参数、显式输入、独占输出和工件身份）。
- Tests: `tests/test_t121_feasibility.py`，按实际需要扩展 `tests/test_prob_counting.py` / `tests/test_prob_gaps.py`。
- Create concise documentation: `docs/research/t121-offline-entry-2026-11-03.md`。

**Interfaces:**
- 保留count_triggers及资格函数现有默认参数，新增关键字参数透传预计算levels与实际回看起点、bars_per_day、s3_bars；签名以最小兼容实现为准，报告确切接口。
- 新计算入口接收已明确来源的H1 bars、source tags、gap spans、冻结sessions及显式成本证据，不自己连接网关/下载/查询台账，不导入labels/fit/engine执行器。
- CLI提供 `--help`；读取明确指定的输入与证据，独占输出目录；不自动搜索真实data、默认成本或候选。生产输入的用途许可/范围/hash缺失须拒绝或waiting_data，不能由CLI开关伪造审批。

- [ ] Read spec once and record the bounded implementation approach in report; consult existing helpers only where directly reused. If the approved design cannot fit these boundaries, report the specific dependency before expanding.
- [ ] Write failing synthetic tests and capture command/output/exit code: complete four-H1 UTC buckets and source propagation (all MT5, one external, missing/unknown source, duplicates/missing root); S2 DST and intra-H4 session transition with no future availability; H1/H4 S3 480/120 and anchor/gap eligibility; entry/exit i+1/i+K+1 before fixed cutoff.
- [ ] Implement minimal counting and qualification plumbing. Freeze four families H1/H4 ×SL1/2, TP2R, K24/12; grids XAUUSD5/EURUSD.005; S1/S2/S3 all A1-A4, feature warmup2000, approach24, max_gap_days30. Discovery cutoff2016-05-01 UTC exclusive.
- [ ] Add failing synthetic tests for explicit legal cost evidence and missing/hash/range/coverage failures; external spread-only1.5; hourly n_days>=20; worst qualified exit hours; commission/contract, stop overshoot version2 basis stop_level, nonpositive overnight_fee converted to nonnegative charge; weekend/DST endpoint nights; finite positive signal price/ATR/R/contract; no future price dependence. Confirm expected failures before implementation.
- [ ] Implement exact cost rules from §5.2. No default zero or 2026-cache diagnostic. Complete qualification is distinct from missing cost. Source unknown affects row, cannot drop it to pass. Aggregate per-trigger costs within UTC trigger day, then equal-weight days. Record n/day distribution and adjacent-day gaps only as descriptions.
- [ ] Add output and boundary tests: all96 rows and8 summaries, no result fields, null unknown vs actual0, n_required4521 and cost<=.02R equality, constant deltaEV.06R; no random seeds; fail and waiting reasons preserved independently; output overwrite refusal and identity receipts; AST and runtime tripwires prohibit label/fit/returns paths.
- [ ] Run focused regression tests with PYTHONUTF8=1 and explicit exit code, preserve RED/GREEN logs in local scratch. Do not run real data or full study; controller runs full regression on integrated version.
- [ ] Self-review diff; document invocation, evidence limitations and concrete remaining execution prerequisites; preserve prior approved prereg/report unchanged. Commit precise files with [P] prefix. Report commit, logs, interface summary and concerns for independent review.
