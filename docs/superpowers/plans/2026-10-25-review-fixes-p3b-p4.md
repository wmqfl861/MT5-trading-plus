# 修复轮 + P3-B + P4 施工计划（2026-10-25）

> **For agentic workers:** 按任务编号顺序执行（T1 的用户操作可与 T2 起并行）。每个任务：
> 失败测试先行 → 实现 → 本任务测试通过 → **全量回归** → 提交并推送 → 勾选本文件的复选框。
> 可用 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务执行。

**Goal:** 清掉第三轮审核的遗留问题（S1、R1–R16），完成 P3-B（tick 级引擎与对账）和
P4（试验台账、假设卡、关卡 G0–G5），为 P5 基线策略打底。

**输入文档（先读）：**
1. `docs/reviews/2026-10-25-review-round3.md`——问题清单（S1、R1–R16）与本轮已修项（F1–F10）
2. `docs/blueprint.md`——蓝图 v2：架构、铁律、路线图、决策记录

**基线：** 提交 `045bffe`（引擎 A-1.1）。全量 **389** 个测试全部通过（GitHub Actions 实测）。

**Tech Stack:** Python 3.12（`.venv`）、pandas / numpy / pyarrow / duckdb；标准库 `sqlite3`、
`tomllib`、`zoneinfo`。**不新增第三方依赖**（确需新增先问用户）。

## Global Constraints（所有任务隐含遵守）

- 时间、金额、成交口径以 `docs/blueprint.md` §4 为准；改引擎语义必须同时更新
  `engine_a.py` 顶部 docstring、`ENGINE_VERSION` 和对应测试。
- 金额字段一律账户货币；成本模型方法的单位写进 docstring；任何"缺省按 1 / 按 0"的
  静默兜底都不允许——缺数据就报错，或显式开关（如 `--allow-zero-swap`）并写进 manifest。
- 测试命令（Git Bash）：`./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`；
  单测：`... tests/<file>::<test> -v`。
- 每个任务一个或多个提交，消息格式 `<type>(<任务号>): <摘要>`（如 `fix(T2): 锁箱全局 cutoff`），
  测试全绿才提交；提交后 `git push`。
- 不提交 `data/`、`config.json`、`mcp.json`、任何令牌/IP/账号。
- 真实下单只在模拟盘；需要终端的验证写成脚本，由主会话在本机执行，子代理不初始化 MT5 真连接。
- 不改写 git 历史、不强推、不改仓库可见性——这些只由用户决定。
- 代码风格与现库一致：中文 docstring、fail-fast 如实抛错、frozen dataclass、原子写复用 `mt5data.store`。

## Review Focus（最可能咬人的五类问题，测试落在归属任务）

1. **金额单位**：价格单位与账户货币混用（T3、T5、T9 的测试必须用 contract_size ≠ 1 的手算数字）。
2. **前视**：窗口不变性、锁箱旁路（T2、T12）。
3. **时区**：服务器时间与 UTC 的偏差（T6）。
4. **成交口径**：A 层、B 层、MT5 策略测试器三者对同一笔交易的解释一致（T9、T10）。
5. **数据完整性**：tick 分区迁移不丢行、刷新只动当月（T4）。

---

## 修复轮（R）

### T0：基线核对（0.5 天）

- [x] `git status` 干净后 `git pull`；全量 pytest，记录通过数（预期 389）。有失败先修，不得带病推进。
- [x] 跑 `scripts/snapshot_specs.py`，确认 `data/specs/latest.json` 里四个品种的 `contract_size`。
- [x] 跑 `scripts/run_backtest.py --strategy mt5quant.strategies.library.buy_hold:BuyHold
      --symbol XAUUSD --timeframe H1 --params '{}' --end <锁箱 cutoff> --volume-lots 0.01`；
      人工核对：净利 ≈ (末根收盘 − 首次入场价) × contract_size × 0.01 − 成本；收益率与金价涨幅量级一致。
      （注意：引擎不模拟保证金与爆仓，1 手黄金配 1 万本金会让权益变负、收益率指标失真，见 T7。）
- [x] 在提交说明或 `docs/reviews/` 追加一段"T0 核对结果"（测试数、四品种合约大小、BuyHold 净利）。

### T1：安全加固 S1（代码 0.5 天 + 用户操作）

**Files:** `src/mt5mcp/config.py`、`scripts/run_mcp_server.py`、`tests/test_mt5mcp_*.py`、
新建 `docs/ops/mcp-remote-access.md`、README 的 MCP 段。

- [x] 失败测试先行：`host` 不是回环地址（127.0.0.1 / ::1 / localhost）且配置里没有
      `"allow_plaintext_public": true` → `load_config` 抛错，文案指向隧道文档。
- [x] 实现上述 fail-closed 校验；`--init` 生成的 `mcp.json` 只写回环地址。
- [ ] 写 `docs/ops/mcp-remote-access.md`：两种接法（SSH 本地端口转发；Tailscale/WireGuard），
      远程 agent 侧连接 `http://127.0.0.1:<端口>/mcp`；令牌轮换步骤（`--init` 重新生成，远程端同步）。
- [ ] **列清单交给用户执行并等待确认**（不要自己做）：
      1) `mcp.json` 改回 127.0.0.1 并轮换令牌；2) 删除防火墙里 47621 的入站放行规则；
      3) 确认 RDP 未暴露公网或已限制来源 IP；4) 仓库改 Private，或由用户决定是否
      `git filter-repo` 清理历史中的公网 IP 后强推。
- 验收：测试通过；用户确认清单 1–3 已完成（4 由用户决定）。

### T2：锁箱全局统一 R2 + R10（1 天）

**Files:** `src/mt5data/lockbox.py`、新建 `scripts/lockbox_init.py`、`src/mt5mcp/tools.py`、
`tests/test_lockbox.py`、`tests/test_mt5mcp_tools.py`。

**Interfaces:**
- `lockbox.json` v2：`{"version": 2, "global_cutoff": "<naive-UTC ISO>", "created_utc": "...",
  "note": "..."}`。`load_config` 同时兼容 v1（键 `SYMBOL/TF`）；v2 存在时 `cutoff_for` 对所有
  序列返回 `global_cutoff`。
- `compute_global_cutoff(inventory, now) -> datetime`：对每个序列算 `compute_cutoff`，取**最早**者
  （锁得最多 = 最保守），截到日 00:00。
- `write_global_cutoff(root, cutoff, *, note)`：已有 v2 且新 cutoff 晚于旧值 → `LockboxError`
  （"锁箱只能更早不能更晚"）；早于或等于 → 允许并保留旧值进 `history` 字段。
- `scripts/lockbox_init.py`：`--cutoff YYYY-MM-DD` 或 `--auto`；打印每个序列开放/锁定行数。
- `list_series`：配置了 cutoff 时 `rows`、`last_time` 只统计开放段（R10）。

- [x] 失败测试先行：v2 对所有品种/周期生效；cutoff 前移（变晚）被拒；变早允许；v1 兼容；
      `research_bars`、MCP 三个读取路径与 `list_series` 都按全局 cutoff 钳制。
- [x] 实现；本机执行 `lockbox_init.py --auto` 生成 v2 并重跑 `export_lockbox.py`。
- 说明：tick 库只用于成本统计，不受锁箱约束（在 docstring 写明豁免理由）。

### T3：相对点差 R1（1.5 天）

**Files:** `src/mt5quant/costs/spread.py`、`slippage.py`、`__init__.py`（Costs 聚合）、`audit.py`、
`src/mt5quant/backtest/engine_a.py`、`scripts/run_backtest.py`、`scripts/cost_audit.py`、对应测试。

**Interfaces（成本接口 v2，引擎与成本模型同步改）：**
```python
def spread_p90(self, symbol: str, hour_utc: int, *, price: float | None = None) -> float: ...
def slippage(self, symbol: str, hour_utc: int, *, price: float | None = None) -> float: ...
```
- 返回值仍是**价格单位**。相对模型必须拿到 `price`（缺失即抛 `ValueError`）；绝对模型忽略它。
- `SpreadModel.from_ticks(symbol, ticks, *, mode="relative")`：relative 统计
  `(ask − bid) / ((ask + bid) / 2)` 的按小时中位/p90/p99；`spread_p90(..., price=p)` 返回 `rel_p90 × p`。
  `to_dict`/`save` 写入 `mode`；`load` 读不到 `mode` 时按 `absolute`（兼容旧文件）。
- 引擎：所有 `spread_p90`/`slippage` 调用传 `price=`（开仓/平仓用该笔成交的 bid 基准价，
  空头 Ask 触发判断用当根 high/low，估值用 close）。`ENGINE_VERSION = "A-1.2"`。
- `cost_audit`：relative 模式下格值 = 相对成本 ÷ 相对振幅（`(high−low)/close` 的小时均值），
  比率不随价格水平变化。
- 可选子任务（时间允许再做）：`BarSpreadCalibrator`——用 K 线 `spread` 字段（bar 内最小点差，
  全历史都有）× 按小时倍数（tick p90 ÷ 同期 bar 最小点差）重建历史点差。

- [ ] 失败测试先行：价格翻倍 → 相对模型点差翻倍；absolute 模式行为与旧版逐位一致；
      引擎把 price 传给成本模型（用记录调用参数的假成本断言）；`contract_size=100` 的手算交易；
      cost_audit 比率在整体价格 ×2 时不变。测试里所有假成本类加 `price=None` 参数。
- [ ] 实现；`run_backtest.py --costs auto` 默认 relative，`cost_model_version` 写明 `rel`。

### T4：tick 按月分区 R3（1.5 天）

**Files:** `src/mt5data/ticks.py`、`scripts/download_ticks.py`、新建 `scripts/migrate_ticks_layout.py`、
`scripts/cost_audit.py` 与 `scripts/run_backtest.py` 的 tick 读取、`tests/test_mt5data_ticks.py`。

**Interfaces:**
- 布局：`ticks/{SYMBOL}/{YYYY}/{MM}.parquet` + `ticks/{SYMBOL}/meta.json`
  （rows、first_msc、last_msc、months 列表、updated_at_utc、server、login）；月份按 `time_msc`
  所在的服务器时间月份。
- `load_ticks(root, symbol, *, start_msc=None, end_msc=None)`：只打开涉及的月份文件。
- 回补：窗口结果先放列表，最后按月分组，每月与该月旧文件合并后原子写（不再逐块 `concat`）；
  `_fetch_window` 用 `pd.DataFrame(arr)` 直接转结构化数组（不再 `arr.tolist()`）。
- 刷新：只读 `last_msc` 所在月及之后的月；重叠证明不变。
- 迁移脚本：读旧 `data.parquet` → 按月写 → 校验总行数与首末 `time_msc` 相等 → 旧文件改名
  `data.parquet.migrated-<UTC戳>`；检测到旧布局时 `load_ticks` 抛错并提示先迁移。

- [x] 失败测试先行：跨月回补分到两个文件；刷新只改写当月文件（其他月份 mtime 不变）；
      迁移前后行数与首末时间守恒；按区间读取只打开必要月份；旧布局给出迁移提示。
- [x] 实现；本机执行迁移脚本，之后才允许做深度 tick 回补。

### T5：真实佣金与隔夜费 R4（1.5 天）

**Files:** `src/mt5gw/models.py`、`symbols.py`、`snapshot.py`；`src/mt5quant/costs/swap.py`、
`commission.py`；新建 `scripts/calibrate_costs.py`；`scripts/run_backtest.py`；对应测试。

**Interfaces:**
- `SymbolSpec` 增加 `swap_mode: int = 0`、`swap_rollover3days: int = 3`（`getattr` 带缺省），
  快照白名单同步增加。**MT5 的星期枚举是周日=0**，换成 Python `weekday()`（周一=0）：
  `py = (mt5_day - 1) % 7`——单独写函数并测试。
- `SwapModel.from_snapshot(snap, symbols, *, charge_hour_utc)`：
  - `swap_mode` 为"点数"：每手每夜金额 = `swap × point × contract_size`；
  - "按品种/保证金/存款货币计"：直接为每手金额（账户货币 = USD 时）；
  - "禁用"：0；
  - 利率类与"重开仓"类：抛 `NotImplementedError`（fail-fast，不猜）。
  - 枚举值用真包常量（`mt5.SYMBOL_SWAP_MODE_POINTS` 等）先在本机打印核对，再写进代码常量。
- `scripts/calibrate_costs.py`：用 `gw.account.deals(since, to)` 汇总模拟盘真实成交的
  每手佣金（按品种、按开平）与每夜隔夜费（多/空），与快照推算值并列输出
  `data/reports/costs/calibration.md` 与 `data/costs/calibrated.json`；顺带判断账户是否免隔夜费、
  计费时刻是几点。
- `run_backtest.py --costs auto`：佣金与隔夜费优先 `calibrated.json`，其次快照；都没有 → 报错，
  除非显式 `--allow-zero-swap`（写进 manifest）。

- [x] 失败测试先行：三种 swap_mode 的手算换算；三倍日枚举换算；利率类抛错；
      校准脚本对假 deals 的汇总数字；`--allow-zero-swap` 缺失时报错。
- [ ] 实现；本机在模拟盘开平几笔（含过夜）后跑校准脚本。

### T6：时区显式化 R5（0.5 天）

**Files:** `src/mt5gw/config.py`、新建 `src/mt5data/clock.py`、`scripts/refresh_all.py`、
`scripts/download_ticks.py`、`scripts/run_backtest.py`、测试。

**Interfaces:**
- `GatewayConfig.server_utc_offset_hours: float = 0.0`（严格类型校验，同现有字段）。
- `clock.check_server_offset(measured_offset_s, expected_hours, *, tol_s=120) -> None`：
  偏差超限抛 `ClockMismatchError`。
- `refresh_all.py`、`download_ticks.py`：开始前用 `server_now()` 的实测偏移校验（行情冻结时跳过并记日志）。
- `run_backtest.py`：读取 `data/server_clock.json` 最新有效样本校验；没有样本 → 警告但不阻断。

- [x] 失败测试先行：偏移 0 通过；+7200 秒在期望 0 时抛错；陈旧样本跳过。
- [x] 实现；在 `docs/blueprint.md` §4 第 1 条把"待 R5"改为"已落地"。

### T7：引擎小修 R7 / R8 / R9 / R16（1 天）

**Files:** `src/mt5quant/strategies/base.py`、`src/mt5quant/backtest/engine_a.py`、测试。

- `ExitRules.reentry: str = "immediate"`，可选 `"after_signal_change"`：保护性出场
  （stop/stop_gap/take_profit/take_profit_gap/time_stop/week_close）之后，直到信号离开该方向
  （变 0 或反向）才允许同向再入场。非法值在策略定义时抛。
- `week_close_exit=True` 且周期 > H1 → `ValueError`（说明 D1/H4 会提前一整根平仓）。
- `Trade` 增加 `exit_fill_time`：按收盘价出场（time_stop/week_close/end_of_data）= bar 时间 + 周期；
  其他 = bar 时间。隔夜费按 `(entry_time, exit_fill_time]` 计。`exit_time` 保持原义（bar 时间）。
- 权益 ≤ 0（爆仓）：引擎停止开新仓并在 `BacktestResult`/summary 标记 `ruined=True` 与首次发生时刻
  （不模拟逐笔保证金，但绝不让负权益进入收益率计算）。
- `ENGINE_VERSION` 递增；docstring 同步。

- [ ] 失败测试先行：止损后信号仍为 1 时，`immediate` 下一根重入、`after_signal_change` 不重入
      直到信号归 0 再回 1；H4 + week_close 抛错；time_stop 的 `exit_fill_time` = bar 收盘时刻、
      恰跨计费时刻时隔夜费多计一夜。
- [ ] 实现。

### T8：CI 与小项 R12 / R13 / R15（0.5 天）

- [x] `.github/workflows/tests.yml`（windows-latest、Python 3.12、全量 pytest）已由审核方建立，
      首次运行 389 passed；之后每次推送看 Actions 是否为绿，红了先修再继续。
- [ ] `cost_audit.py`：合约大小缺失时报错（与 `run_backtest.py` 一致），保留 `--contract-size` 覆盖。
- [ ] `mt5mcp.tools._validate_symbol`：不再转大写，按原样校验后使用（Windows 行为不变）。
- [ ] 修复轮收尾：跑 `cost_audit.py --timeframes M1,M5,M15,H1`（相对点差 + 校准佣金），
      写 `docs/research/scope-<日期>.md`：哪些品种 × 周期 × 时段进入研究、哪些时段禁止交易、理由。

---

## P3-B：tick 级引擎与对账

### T9：B 层 tick 引擎（4 天）

**Files:** 新建 `src/mt5quant/backtest/engine_b.py`、`tests/test_backtest_engine_b.py`；
`scripts/run_backtest.py` 增加 `--engine a|b`。

**Interfaces:**
```python
def run_backtest_ticks(strategy, bars, ticks_loader, costs, *, symbol, params, contract_size,
                       volume_lots=1.0, initial_capital=10_000.0, timeframe,
                       latency_ms=0, slippage_price=0.0) -> BacktestResult
```
- 信号：与 A 层完全相同（同一个 `generate_signals`，只用已收盘 bar）。
- 开平仓：bar i 收盘时刻 + `latency_ms` 之后的**第一笔 tick**成交——多头按该 tick 的 ask 买入、
  bid 卖出；空头相反；点差来自真实 tick，不再用点差模型；`slippage_price` 为额外不利滑点。
- SL/TP：逐 tick 判断——多头 bid ≤ SL / bid ≥ TP，空头 ask ≥ SL / ask ≤ TP；
  按触发那笔 tick 的价格成交（跳空自然体现），同一 tick 同时满足时止损优先。
- time_stop / week_close / end_of_data：该 bar 收盘前最后一笔 tick 成交。
- 佣金、隔夜费口径与 A 层一致（每手 × 手数）。
- tick 覆盖不到的区间直接报错并给出覆盖范围（不静默跳过）；按月流式读取（依赖 T4）。

- [ ] 失败测试先行（全部手造 tick 序列）：入场成交在信号 bar 收盘后的第一笔 tick 的 ask；
      空头止损由 ask 触发；tick 级跳空按触发 tick 成交；latency_ms 把成交推迟到下一笔；
      tick 覆盖不足报错；用"tick 恰好复现 bar OHLC"的合成数据时 A/B 两层交易列表时间一致、
      价格差只来自点差建模差异。
- [ ] 实现。

### T10：A/B 对账 + MT5 策略测试器对账（3 天）

**Files:** 新建 `scripts/reconcile_ab.py`、`mql5/Experts/BuyHoldRef.mq5`、`mql5/Experts/MACrossRef.mq5`、
`scripts/reconcile_mt5_tester.py`、`docs/reports/reconciliation-<日期>.md`。

- [ ] `reconcile_ab.py`：同一策略、同一 tick 覆盖期分别跑 A、B，逐笔对齐输出差异表
      （入场/出场时间、价格、净利），汇总每笔净利差的分布。
- [ ] 两个参考 EA：信号逻辑与 Python 版逐行对应（BuyHold；收盘价 SMA 快慢线交叉，参数写死），
      `OnTester` 把成交写成 CSV（`MQL5\Files`）。
- [ ] 在 MT5 策略测试器用"基于真实 tick"模式跑同一时段，`reconcile_mt5_tester.py` 与 B 层逐笔比对。
- [ ] 把实测差异与约定容差写进报告和本计划（例如：信号类交易的时间必须完全一致；
      每笔净利差 ≤ 1 个点差）。容差确定后写进 `tests/` 的回归用例。

---

## P4：试验台账与验证关卡

### T11：试验台账与假设卡（3 天）

**Files:** 新建 `src/mt5quant/research/__init__.py`、`ledger.py`、`cards.py`；`scripts/bt.py`；
`research/cards/`（假设卡目录，入库）；测试。

**Interfaces:**
- 台账：SQLite `data/research/ledger.sqlite`，表：
  - `cards(card_id, version, path, git_commit, committed_at_utc)`
  - `runs(run_id, card_id, card_version, strategy, params_json, symbol, timeframe, data_first,
    data_last, data_sha256, engine_version, cost_model_version, gates_hash, metrics_json,
    created_utc, actor)`——**失败的运行也记**
  - `gates(run_id, gate, passed, details_json)`
  - `lockbox_uses(card_id UNIQUE, used_at_utc, approved_by, result_json)`
- 假设卡：TOML（标准库 `tomllib` 读），字段见 `docs/blueprint.md` 附录 A（id、version、symbols、timeframe、
  session、phenomenon、mechanism、rules、params（≤4，网格 ≤100）、expected、should_fail_when、
  falsified_if、graveyard_checked）。`cards.load_card(path)` 做模式校验。
- "先卡后测"：`cards.committed_at(path)` 取 `git log -1 --format=%cI -- <path>`；卡未提交或
  提交时间晚于该卡首次运行 → 拒绝运行。
- `bt.py`：`bt card check <path>`、`bt screen --card H012`（在卡的网格上跑 A 层，逐组合记入台账）、
  `bt ledger stats --card H012`（试验次数，供 DSR 用）。

- [x] 失败测试先行：卡模式校验（参数超 4 个 / 网格超 100 / 缺字段）；未提交的卡拒绝；
      每次运行（含抛异常的）都有一行 runs；lockbox_uses 第二次插入失败。
- [x] 实现；写一张示例卡（BuyHold 基准）跑通 `bt screen`。

### T12：关卡 G0–G5（4 天）

**Files:** 新建 `src/mt5quant/validation/{__init__,gates,lookahead,robustness,walkforward,stats}.py`、
`config/gates.toml`（门槛，入库，数值取 `docs/blueprint.md` 附录 B）、`scripts/bt.py` 增加 `bt gate`；测试。

- G0：截断测试（已有）+ **窗口不变性**（新）：`check_window_invariance(strategy, bars, params,
  lookback, probes)`——任取 t，用 `bars[t−L+1..t]` 算出的末位信号必须等于全量信号第 t 位；
  + 静态扫描禁用写法（负向 `shift(`、`center=True`、全样本 `mean()/std()` 标准化）。
- G1：样本内按真实成本净盈利；交易次数达标。
- G2：成本 ×1.5 / ×2；参数邻域高原；按年分段；跨品种；随机入场蒙特卡洛（同交易数、同持仓时长分布、
  同出场规则）分位；多头策略对比买入持有；去掉最好 5% 交易；延迟一根 bar 执行。
- G3：walk-forward（滚动训练/测试窗，只在卡的网格内重选参数，记录样本外）。
- G4：Deflated Sharpe Ratio（Bailey & López de Prado 2014），试验次数 N 取台账中该卡家族的运行数；
  蒙特卡洛回撤分布。
- G5：锁箱一次性——必须 `--approve` 且写入 `lockbox_uses`；第二次请求直接拒绝。
- `config/gates.toml` 的 sha256 写进每次运行；与冻结值不符 → 拒绝运行。

- [ ] 失败测试先行：用 EMA 全史计算、`lookback` 不够的策略被窗口不变性抓出；
      故意过拟合的策略（在噪声上挑最优参数）过不了 G2/G4；随机信号策略过不了随机入场基准；
      G5 第二次被拒；改动 `gates.toml` 后拒绝运行。
- [ ] 实现；用 BuyHold 示例卡端到端跑 G0–G4，产出标准关卡报告。

---

## 之后的阶段（下一份计划细化）

- **T13（P5，2 周）**：实现 B1 通道突破（4 品种 H1）、B2 纳指开盘区间突破（M5/M15）、
  B3 伦敦时段突破亚盘区间（EURUSD/XAUUSD M15）、B4 带趋势过滤的均值回归（M15/H1）、
  B5 纳指日内动量（开盘半小时 → 尾盘半小时）、B6 买入持有/波动率目标基准；全部走 G0–G4，
  据结果复核 `config/gates.toml` 后冻结。
- **T14（P7，3 周，与 P4–P5 并行）**：`mt5live`——信号运行器（bar 收盘后几秒触发，只读已收盘 bar，
  调用与回测相同的代码，窗口长度 = `lookback_bars`）；执行适配器（目标仓位 → 订单，按 magic 管理，
  幂等、对账）；独立风控进程（白名单、手数上限、组合美元暴露、日亏损上限、禁止时段、一键停机、
  心跳）；SL/TP 随单提交为服务器端止损，价位 = 实际成交价 ± k × ATR（信号 bar），与引擎同口径；
  每日用回测引擎重跑当天数据比对信号与成交。与定时刷新共用终端时安排单写者或错峰（R14）。
- **T15（P6，持续）**：研究 agent / 审查 agent 的规则文件与权限隔离（锁箱目录与 `config/` 对研究账户
  只读或不可读），一切操作经 `bt` 命令行。

## 集成与验收

1. 每个任务结束：全量 pytest 全绿 → 提交 → 推送 → 勾选本文件复选框。
2. 修复轮结束（T0–T8）：可交易性审计结论 `docs/research/scope-<日期>.md`。
3. P3-B 结束：`docs/reports/reconciliation-<日期>.md`，容差写进回归测试。
4. P4 结束：BuyHold 示例卡的 G0–G4 报告；`docs/blueprint.md` §2 现状表更新。

## 明确不做

机器学习模型；M1 剥头皮；实盘下单；改写 git 历史或改变仓库可见性（由用户决定）。
