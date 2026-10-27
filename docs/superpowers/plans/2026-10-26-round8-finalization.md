# R8 收尾施工计划（2026-10-26）：正式验收三连 + 演练准备

> **For agentic workers:** 多子代理并行执行。每个任务：先写失败测试（凡有代码改动）→ 实现 →
> 全量回归 → 提交（<type>(T##): 摘要）→ 推送 → CI 绿 → 勾选复选框并附证据。
> 完成后由 codex（gpt-6-astra，思考等级 max）复审一轮，蓝图按结果更新。

**Goal:** 按蓝图 v3.1 路线图完成三个放行级验收（C 正式标定、E 四组对账、D′ 影子盘），
并准备阶段 F（两周模拟盘）。完成后项目进入"等日历时间"状态（模拟盘跑满两周 + 用户操作）。

**输入文档：** docs/blueprint.md（v3.1 + R7-5 更新）、docs/reviews/2026-10-26-review-round7-codex.md。

**基线：** d95d572（1632 tests，CI 绿）。

## T42：T39 正式重跑（修复后实现 + 运行指纹）

上一轮五分片系修复前启动、无运行指纹（R7-N-I1）。本轮重跑：

- [x] 每分片产物 meta 带运行指纹（提交 2fa27a3）：meta.fingerprint = git 提交/工作区
      干净位/冻结门槛哈希/各序列成本版本（含点差缓存+越幅+隔夜费身份 sha）/脚本字节
      哈希；合并器把 fingerprint 纳入实验身份校验。失败测试 ×2：
      test_merge_rejects_fingerprint_mismatch（不一致拒合）、
      test_meta_records_run_fingerprint（meta 记录指纹）——均绿。
- [x] 研究库缓存同步确认：五分片启动日志零"缓存无止损越幅表"警告；合并产物
      meta.fingerprint.cost_versions 五序列全部含 `ov[eca09e24]`（stop_level 口径）。
- [x] 5 分片（seeds 0-3/4-7/8-11/12-15/16-19）12:43 UTC 并行启动、~22:30 全部完成；
      merge 出 data/reports/gates/gate-power-v2/gate-power.{json,md}（160+160+480 条，
      13 条 flags；合并 md 含三节补充——完整组合通过率/校准逐种子分布/分品种 G5
      误报率，提交 c63a86b，测试 test_merge_md_includes_r7_supplement_sections）。
- [x] 正式报告：docs/research/gate-power-v2-2026-10-26.md（运行指纹、R7 解释限定
      a–i 逐项回应、v1→v2 差异归因=越幅真实接入、门槛建议只建议、USTEC≈1.0
      未收敛如实标注）。
- [x] 蓝图 §2 P4/P5 与路线图 C 行更新（随本提交）。

**注意：** 分片是后台计算（无 LLM 消耗），用 Bash 后台跑；种子与分片参数不变时
成本版本会因缓存重建而变化（ov[eca09e24]），新运行自动与新缓存配对。

## T43：T40 四组对账（越幅缓存 v2 + I6 修复后）

reconciliation-v3 报告的 §5–§7 还是占位。本轮重跑并回填：

- [x] 四组对账完成（run#442–445，--ops-ack×4 入账）：XAUUSD H1 + USTEC M5 各
      immediate / after_signal_change，MaCrossATR，8,364 笔配对。
- [x] 成本缓存 stop_level 口径确认（summary 记 ov[eca09e24] loaded）；
      越幅分量使用已由 T48/R8-I3 修正为仅 stop↔stop 适用。
- [x] v3 报告回填 + T48/R8-I3 修订节：分口径符合率（97.45%，修订前口径
      98.13%）、违规 213 条四分类（TP 穿价/TP gap/原因分叉/跨 bar 配对）、
      纯 stop↔stop 子集 2,119 行零违规。
- [x] 结构差异归因如实报告：158→213 的增量来自 A-stop/B-nonstop 原因分叉
      （旧口径误将路径差算作越幅），unmatched 1,329 与两模型存续期差异一致
      （A 于 2026-07-17 首次权益 -36.56、B 于 2026-09-07 -5.66）。
- [x] 蓝图 §2 P3 与路线图 E 行更新（T42 随本提交一并更新至 R8 终态）。

## T44：T34 影子盘一日双策略（真终端，阶段 D′ 验收）

R7 修复（账户级风控聚合视图、一致快照、确认绑定事件、净零残仓、内核字节锁）已落地。

- [x] 两份 paper 配置（M15）：XAUUSD immediate 902431 / EURUSD after_signal_change 902432
      （data/live/paper-*.toml，SL/TP 由 ATR 自然触发——当日 SL 出场 5 次）。
- [x] 四进程 10:58–21:04 UTC 连续运行（2 运行器 + 风控 + 对账，日志 data/live/paper-*.log）。
- [x] 演练全过：killSwitch（10s 内 halt、两轮拒单进 alerts、13s 恢复）；连续两次日亏全平
      （12:59:56 / 13:15:07，第二次同样真全平——uuid/command_id 绑定防旧 ack 提前归档，
      归档滞留于永不 ack 的旧干跑 902426 属已知项，人工恢复并报告注明）。
- [x] 收盘统计（docs/reports/live-paper-2026-10-26.md，脱敏）：83 决策 / 21 笔成交
      （=入场 11+出场 10）、SL 保护性出场 5 次全解释零暂停、无法解释暂停 0、
      持仓期间调仓 0、心跳无超时。demo_check.py 当日预检 6/7 过（日报项收盘后生成属预期）。
- [x] 蓝图 §2 P7 / 路线图 D′ 行更新（提交 2a0b386，阶段 F 放行）。

## T45：阶段 F 准备 + 蓝图 + codex 复审

- [x] 模拟盘准备（不开跑，提交 81e4385）：demo_run.ps1（一键启动+七项预检）+
      demo_check.py（蓝图 §7 八条逐日检查+演练证据汇总）+ demo-configs/ 两份示例；
      demo_check.py 已用 2026-10-26 影子盘真实数据预验（6/7 过，日报项正确待生成）。
- [x] 蓝图 §2 / §5 全表更新（T43 a4df077、T44 2a0b386、T42 随本提交；P3/P4/P5/P6/P7、
      C/D′/E 行、禁止事项与开放问题清单同步至 R8 终态）。
- [ ] codex 复审（gpt-6-astra max）：覆盖 8e8bb76..HEAD 的 R8 改动 + 三个验收报告。
- [ ] 按复审结果修复或标注，最终推送。

## 禁止事项（不变）

- 不跑 G5（锁箱预算等用户 D16 决策）；不开新研究卡（基线重跑除外）；
- 不改 gates.toml；不碰仓库可见性；只在模拟盘下单；报告入库前去账号/IP/令牌。
