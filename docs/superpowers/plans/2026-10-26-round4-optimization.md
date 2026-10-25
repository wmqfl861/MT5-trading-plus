# 第四轮优化施工计划（2026-10-26）：测量修正 · 验证管线 · 实盘接线

> **For agentic workers:** 按"执行顺序"一节逐任务推进。每个任务：失败测试先行 → 实现 → 本任务测试通过
> → **全量回归** → 提交并推送 → 确认 GitHub Actions 为绿 → 勾选复选框并**在同一行写上证据**
> （测试名、报告路径或日志路径）。没有证据的复选框一律不勾。
> 可用 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务执行。

**Goal:** 修正第四轮审核发现的问题：实盘风控链路接线（C1、C2），G5 逻辑（C3），隔夜费与换日窗口径（H1、H2），
研究库隔离（H3），卡谱系与试验计数（H4），walk-forward 窗口（H5），G4 内存（H6），证据补齐（H7），以及 M1–M12。
完成后进入两周模拟盘演练（蓝图 v3 §7）。

**输入文档（先读）：**
1. `docs/reviews/2026-10-25-review-round4.md`：问题清单与 7 个复现探针
2. `docs/blueprint.md`（v3）：现状评级、目标架构、铁律 v3、路线图、决策 D10–D18、附录 A/B

**基线：** 提交 `7c9b757`；GitHub Actions 980 passed。

**Tech Stack:** Python 3.12（`.venv`）、pandas / numpy / pyarrow / duckdb；标准库 `sqlite3`、`tomllib`、`zoneinfo`。
不新增第三方依赖（确需新增先问用户）。

## Global Constraints（所有任务隐含遵守）

- **禁止事项（直到对应任务完成）**：T21 完成前不跑任何 G5；T24 完成并通过集成测试前，不以非干跑模式启动
  `live_runner.py`；T20、T21、T23 完成前不开新研究卡（基线重跑除外）。
- 时间：随夏令时移动的事件（换日、交易时段）一律用交易所本地时间定义（`zoneinfo`），禁止固定 UTC 小时。
- 金额：账户货币；成本方法 docstring 写明单位；缺数据就报错，不允许静默按 0 或按 1。
- 回测与实盘一致：禁入规则、定手数、出场语义只有一份实现，引擎与实盘共同调用。
- `config/gates.toml` 只在 T21（G3、G5 键）与 T22（若有证据支持调整）中修改，修改时同一提交更新
  `FROZEN_GATES_SHA256` 并在蓝图决策记录追加一行。
- 跨进程功能必须有集成测试（多个组件 + 真实文件系统 + FakeMT5），只测单个组件不算完成。
- 报告与日志进 `docs/reports/` 前去掉账号、IP、令牌。
- 测试命令（Git Bash）：`./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`。
- 提交消息格式 `<type>(T##): 摘要`；不改写 git 历史、不强推、不改仓库可见性（只由用户决定）。

## 执行顺序

| 顺序 | 任务 | 规模 | 依赖 | 解决的问题 |
| --- | --- | --- | --- | --- |
| 1 | T20 隔夜费与换日窗 | M | — | H1、H2 |
| 2 | T28 成本模型缓存与性能 | S | T20 | M8 |
| 3 | T21 验证管线修正 | L | — | C3、H4、H5、H6、M4–M7 |
| 4 | T23 共享定手数 + 基线重跑 | M | T20、T21 | M2 |
| 5 | T22 关卡正负对照实验 | M | T21、T23 | H7（判别力） |
| 6 | T25 执行语义对齐 | M | T23 | M1、M3 |
| 7 | T24 实盘接线 | L | T20、T23、T25 | C1、C2、M9、M10 |
| 8 | T27 研究库隔离与锁箱预算 | M | T28 | H3、M11 |
| 9 | T29 权限白名单与文档对齐 | S | T21、T27 | M12、L1–L3 |
| 10 | T26 证据补齐 | M | T24、T25，用户操作 | H7（对账） |
| 11 | 模拟盘两周演练 | — | 以上全部 | 蓝图 §7 |

规模：S ≈ 半个工作日，M ≈ 1 个工作日，L ≈ 2 个工作日及以上（按以往节奏估计，仅供排序）。

---

## U0：用户操作（agent 不得代办，只列清单并等待确认）

- [ ] S1：`mcp.json` 改回 127.0.0.1 并轮换令牌；删除 MCP 端口的防火墙入站规则；确认 RDP 不暴露公网
      或已限制来源 IP；决定仓库改 Private 还是清理历史。
- [ ] 模拟盘开 2–3 笔小手数并持有过夜（至少一笔跨周三、一笔跨周末），供 T20 校准隔夜费。
- [ ] T26 的 MT5 策略测试器对账（图形界面操作，步骤见 `mql5/Experts/*.mq5` 头注）。
- [ ] 决策 D16（锁箱预算与退役规则），见蓝图 §8。

## T20：隔夜费与换日窗（H1、H2）

**Files:** `src/mt5quant/costs/swap.py`、`src/mt5quant/costs/spread.py`、`src/mt5data/sessions.py`、
新建 `config/sessions.toml`、`src/mt5quant/backtest/engine_a.py`、`src/mt5quant/backtest/engine_b.py`、
`src/mt5live/riskproc.py`、`scripts/calibrate_costs.py`、`scripts/run_backtest.py`、`scripts/bt.py`、对应测试；
新报告 `docs/research/rollover-dst-<日期>.md`。

**Interfaces:**
```python
# mt5quant.costs.swap
@dataclass(frozen=True)
class SwapRule:
    long_per_day: float
    short_per_day: float
    triple_weekdays: frozenset = frozenset({2})          # Python weekday（周一=0）
    charge_weekdays: frozenset = frozenset({0, 1, 2, 3, 4})  # 周末不计（Exness 全部品种组）
    charge_local: str = "17:00"                          # 计费时刻：交易所本地时间
    charge_tz: str = "America/New_York"                  # 21:00 GMT（夏）/ 22:00 GMT（冬）
def charge_nights(from_ts, to_ts, rule: SwapRule) -> int   # 只数 charge_weekdays 上的计费时刻，三倍日 ×3

# mt5data.sessions（共享禁入规则，引擎与风控共同读取 config/sessions.toml）
@dataclass(frozen=True)
class SessionPolicy:
    def entry_allowed(self, symbol: str, t_utc: datetime) -> bool: ...
    def rollover_window(self, day: date) -> tuple[datetime, datetime]: ...   # naive UTC
def load_session_policy(path="config/sessions.toml") -> SessionPolicy
```
`config/sessions.toml` 初值（以 DST 拆分实测为准再定稿）：换日 17:00 America/New_York，禁入从前 5 分钟到后
60 分钟，BTCUSD 豁免；EURUSD 扩展窗按实测设定；点差闸门倍数 3.0（T24 使用）。

三倍日：快照路径与实测校准路径一律取快照的 `swap_rollover3days`（MT5 星期枚举，经 `mt5_to_py_weekday` 换算），
不再出现 `triple_weekdays=frozenset()`。Exness 官方表：外汇、金属周三，指数周五，加密货币交叉盘周三、其余周五；
rollover 报告里列出四个品种的快照值并与官方表核对，不一致时停下报告用户。

- [x] 失败测试先行（证据：test_costs_swap.py::test_friday_to_monday_is_one_night / test_full_week_is_seven_nights_with_wednesday_split / test_charge_moment_dst_summer_21_utc / _winter_22_utc；test_calibrate_costs.py::test_rate_includes_triple_day_round_trip；test_mt5data_sessions.py T20 节；test_backtest_engine_{a,b}.py 禁入推迟节；test_mt5live_riskproc.py SessionPolicy 节；test_run_backtest_script.py::test_missing_hour_forbid_defers_engine_entry_end_to_end）：
  - 周五 20:00 → 周一 20:00 = 1 夜；整周 = 7 夜（含周三 ×3）；冬令时日期的计费时刻是 22:00 UTC、
    夏令时是 21:00 UTC（用 2026-07-15 与 2026-12-15 钉死）；
  - 校准：周二 → 周四持仓，实际隔夜费 −40，校准费率 −10，回测按同一规则（含三倍日）复算回 −40；
  - 免隔夜费判定必须跨过计费时刻（22:00 入场、次日 20:00 出场不算过夜）；
  - `SessionPolicy`：2026-07-15 21:10 UTC 禁入、2026-12-15 21:10 UTC 允许、2026-12-15 22:10 UTC 禁入；
  - 引擎 A、B：信号要求在禁入时段开仓 → 推迟到下一个允许的 bar 开盘；平仓不受限；
  - 风控 `check`：禁入判断改用 `SessionPolicy`（删掉 `BUILTIN_FORBIDDEN_HOUR_UTC`；`config/risk.toml` 的
    `[[forbidden]]` 迁到 `config/sessions.toml`，禁入规则只保留一处）；
  - 点差模型按纽约本地小时分桶；某小时缺统计时按 `config/sessions.toml` 的 `missing_hour_policy`
    （`"forbid"` = 该小时禁入，出场取全天最大 p90），不再让整次运行抛错。
- [x] 实现（A-1.4/B-0.2；证据：全量 1112 passed， deaf6ef）；`run_backtest.py`、`bt.py` 默认加载 `config/sessions.toml` 并把它的 sha256 写进 manifest 与
      `cost_model_version`。
- [ ] 实测报告 `docs/research/rollover-dst-<日期>.md`：tick 按冬、夏季拆开，统计四个品种在 UTC 20–23 点的
      点差 p50/p99；用 10-26 至 10-30 这一周确认 Exness 冬令时跟随欧洲还是美国；据此定稿 `config/sessions.toml`。
- [ ] U0 的过夜成交到位后重跑 `scripts/calibrate_costs.py`，实测与快照推算并列写进报告。

## T28：成本模型缓存与性能（M8）

**Files:** 新建 `scripts/build_cost_model.py`；`src/mt5data/ticks.py`（`load_ticks(columns=...)`）；
`scripts/run_backtest.py`、`scripts/bt.py`（加载缓存）；`src/mt5quant/backtest/engine_b.py`（性能）；测试。

- [ ] `build_cost_model.py`：从 tick 只读 `time_msc/bid/ask` 三列，构建相对点差模型（T20 的纽约小时分桶），
      写 `data/costs/spread_model.json`，内含各品种 tick 水位（rows、last_msc）与模型 sha256。
- [ ] `build_costs` 改为加载该文件；tick 水位变化而模型未重建 → 报错并提示重建（不静默用旧模型）。
- [ ] 引擎 B：不再为每笔 tick 构造 `pd.Timestamp`（只在成交时构造）；bar 边界用 `searchsorted` 预计算。
      在 XAUUSD 一个月 tick 上测速，前后耗时写进提交说明（目标 ≥5 倍）。
- 验收：XAUUSD H1 单次回测不再加载原始 tick（峰值内存从约 4 GB 降到 1 GB 以内，写进提交说明）。

## T21：验证管线修正（C3、H4、H5、H6、M4、M5、M6、M7）

**Files:** `src/mt5quant/validation/{gates,stats,walkforward,robustness}.py`、`src/mt5quant/research/{cards,ledger}.py`、
`scripts/bt.py`、`config/gates.toml`（G3、G5 键，重新冻结）、`research/cards/*`（补谱系字段）、对应测试。

**T21a G5 重写（C3）**
```python
# mt5quant.validation.stats
def block_bootstrap_sharpes(returns, *, window_len: int, block_len: int,
                            sims: int, seed: int) -> np.ndarray   # 年化口径与 report 一致
# mt5quant.validation.gates
def run_g5(ledger, card, *, lockbox_result, reference_returns, expected_trades: float,
           approved_by: str, cfg) -> dict
```
- 参照分布：G3 拼接的样本外收益（没有通过 G3 的卡不允许跑 G5），按锁箱长度做块自助重采样
  （块长 = 一个交易日的 bar 数）。
- 判定（单侧）：期望 > 0；交易数 ≥ 0.5 × 按样本内频率折算的预期；锁箱夏普 ≥ 参照分布 p5。
  高于 p95 → `warning="异常好，复核数据与前视"`，不判失败。
- `config/gates.toml` 的 `[g5]` 换成下列键（删掉 `lockbox_interval_lo/hi_percentile`），与 T21b 同一提交重新冻结：
  ```toml
  [g5]
  lockbox_sharpe_min_percentile = 5.0    # 锁箱夏普 ≥ 参照分布 p5（单侧）
  lockbox_warn_percentile = 95.0         # 高于 p95 只告警，不判失败
  lockbox_min_trades_ratio = 0.50        # 交易数 ≥ 预期的 50%
  lockbox_bootstrap_sims = 2000
  ```
- 锁箱运行：从 cutoff 前 `lookback_bars` 根起步，只统计 `entry_time ≥ cutoff` 的交易与 cutoff 之后的权益收益。
- `lockbox_uses` 增加 `status` 列：读锁箱数据**之前**先写 `started`，评估结束再写结果；
  已有 `started` 记录的卡同样视为已用过（防止中途崩溃后重跑）。
- [ ] 失败测试先行：保持优势的策略（锁箱夏普 2.0，参照 p5 = 0.8）通过；退化为随机（夏普 0.0）不通过；
      夏普 5.0 通过但带告警；没有预热段时信号与带预热段不同（用 B1 构造）；`started` 后崩溃不能重跑。

**T21b G3 日历窗口（H5）**
- `config/gates.toml` 的 `[g3]` 改为 `train_months_hourly = 24`、`test_months_hourly = 6`、
  `train_months_intraday = 12`、`test_months_intraday = 3`、`min_oos_ratio = 0.50`；重新冻结指纹。
- 各段 `oos_net`、`oos_trades` 只计 `entry_time ≥ 测试窗起点` 的交易。
- [ ] 失败测试先行：M5 序列的测试窗为 3 个日历月；预热段里的交易不计入 `oos_trades`。

**T21c G4 内存与试验计数（H6、M4）**
- 回撤蒙特卡洛分批（每批 ≤ 20 次，37 万根时每个矩阵约 60 MB），改为块自助重采样（块长 = 一个交易日的 bar 数）。
- N = 该谱系中按 (strategy, params, symbol, timeframe, data_sha256, cost_model_version) 去重后的成功运行数；
  试验夏普同样去重；报告同时给出项目级 N。
- [ ] 失败测试先行：37 万根 × 1000 次的回撤模拟峰值内存 < 500 MB（`tracemalloc` 断言）；
      同一组参数先 screen 后 gate，N 只计 1 次。

**T21d 卡谱系与先卡后测（H4）**
- 卡字段新增 `family`、`parent`、`strategy`、`selection_rule`（见蓝图附录 A）；旧卡缺字段时
  `family = id`、`strategy` 必须由 `--strategy` 给出，并打印迁移提示。
- 先卡后测按**卡文件**（id + version）判定：该文件的提交时间早于该文件首次运行，且
  `git status --porcelain -- <卡文件>` 为空（工作区干净）。
- `--strategy` 与卡上的 `strategy` 不一致 → 拒绝运行。
- [ ] 失败测试先行：探针 P6 的场景（改了没提交）被拒；`H012-v2` 是新文件，能在 `H012-v1` 跑过之后运行，
      且 N 累计两者；策略不匹配被拒。

**T21e 参数选择与杂项（M5、M6、M7）**
- `bt gate --params auto`（默认）：按卡的 `selection_rule` 从同一数据哈希、同一成本版本的 screen 运行里选参，
  并把选择过程（候选数、选中的 run_id）写进关卡报告；显式 `--params` 记为 `selection="manual"`。
- `bt screen` 的 gates_hash 改用 `gates.gates_sha256`（探针 P7）。
- 随机入场基准：入场时刻按策略实际入场的纽约本地小时分布抽样，多空比例与策略一致。
- [ ] 失败测试先行：auto 选中净利最高的组合并记录 run_id；CRLF 文件两条命令记录同一哈希；
      只在 10:00–11:00 入场的策略，其随机基准的入场也全部落在该时段。

## T23：共享定手数（M2）+ 基线重跑

**Files:** 新建 `src/mt5quant/sizing.py`；`engine_a.py`、`engine_b.py`、`validation/robustness.py`、
`scripts/run_backtest.py`、`scripts/bt.py`；测试；新报告 `docs/research/gates-calibration-v2-<日期>.md`。

```python
@dataclass(frozen=True)
class RiskSizing:
    risk_fraction: float              # 单笔风险占资金比例，如 0.005
    basis: str = "initial"            # "initial"（不复利，便于比较）| "equity"
    fallback_stop_atr: float = 1.0    # 策略无止损时按 k×ATR 估风险距离
def lots_for_risk(capital: float, risk_fraction: float, stop_distance: float,
                  contract_size: float, vol_min: float, vol_step: float,
                  vol_max: float) -> float        # 向下取整到步长；低于最小手数返回 0.0（跳过并记录）
```
- 引擎增加 `sizing: RiskSizing | None`（与 `volume_lots` 二选一）；品种的 `vol_min/step/max` 从快照读取；
  每笔交易的手数写进 `Trade.volume_lots`；因手数为 0 跳过的入场计数写进 summary。
- CLI 与关卡新增 `--risk-per-trade`（默认 0.005），manifest 记录定手数方式。
- [ ] 失败测试先行：XAUUSD（合约 100）、资金 10,000、风险 0.5%、止损距离 5.0 → 0.10 手；
      低于最小手数返回 0 且不开仓；`basis="equity"` 随权益变化。
- [ ] **基线重跑**：六张卡在 T20（隔夜费、禁入）+ T23（定手数）新口径下重跑 screen 与 G0–G1
      （过 G1 的继续跑 G2–G4），写 `gates-calibration-v2`：新旧结果对照，并说明每处变化来自哪项修正。

## T22：关卡正负对照实验（H7）

**Files:** 新建 `src/mt5quant/validation/synthetic.py`、`scripts/gate_calibration.py`；测试；
报告 `docs/research/gate-power-<日期>.md`。

- 负对照（测误报率）：用块置换打乱真实序列的日收益（块长 1 天，保留波动聚集、破坏可预测性），
  在打乱数据上跑六个基线与随机策略，各 20 个种子，统计每道关卡的通过率。
- 正对照（测检出率）：在真实 XAUUSD H1 与 USTEC M5 上植入已知优势：按固定规则产生"事件 bar"，
  其后 k 根 bar 的 OHLC 一致地加上漂移 d；配套的 `PlantedEdgeStrategy` 只交易事件。
  调整 d，使扣成本后的年化夏普分别约为 0.3、0.6、1.0、1.5，每档 20 个种子。
- 产出：每道关卡（G1–G4 各子检查）的检出率与误报率表。
- 决策规则（写进报告）：真实夏普 1.0 时某道关卡检出率 < 50%，或负对照误报率 > 5%，就提出门槛调整，
  经用户确认后修改 `gates.toml` 并重新冻结。
- [ ] 失败测试先行：植入器把 d=0 时数据逐位还原；`PlantedEdgeStrategy` 通过 G0；同一种子结果可复现。
- 计算量：G2 随机入场在本实验里可降到 50 次（报告注明），正式关卡仍用 200 次。

## T25：执行语义对齐（M1、M3）

**Files:** `engine_b.py`、`validation/lookahead.py`、`src/mt5live/runner.py`、测试。

- [ ] 引擎 B 实现 `reentry="after_signal_change"`、`ruined` 标记与 `week_close ≤ H1` 校验。对齐测试：
      tick 序列恰好复现 bar OHLC 时，A、B 两层在 `immediate` 与 `after_signal_change` 两种模式下交易时间一致。
- [ ] 窗口不变性：G0 默认 100 个随机探针（种子由卡 id 派生，如 `zlib.crc32(id)`，可复现），并对一段连续 500 根 bar 逐根比对窗口信号与全史信号。
      B1–B6 全部重跑 G0，结果写进 `gates-calibration-v2`。
- [ ] 实盘运行器的计算窗口改为 `max(3 × lookback_bars, 1000)`（可配置），
      `required_data.lookback_bars` 只作为下限。

## T24：实盘接线（C1、C2、M9、M10、M1 实盘部分）

**Files:** `src/mt5live/{runner,executor,riskproc,reconcile,monitor}.py`、新建 `src/mt5live/gate.py`、
`scripts/live_runner.py`、`scripts/risk_process.py`、新建 `scripts/live_daily_report.py`、
`scripts/setup_scheduled_tasks.ps1`（日报任务）、新建 `tests/test_mt5live_integration.py`。

**数据来源（C2）**
```python
class TerminalBarsSource:
    """在运行器进程内直接向终端取最近 N 根已收盘 bar（沿用 drop_unclosed 的收盘判定）。"""
    def closed_bars(self, symbol: str, timeframe: str, n: int) -> pd.DataFrame | None: ...
    # 返回 None = 休市（最新报价陈旧超过阈值）→ 本网格点静默跳过并记日志
```
- 有行情但等不到预期 bar：在 `max_wait_s` 内重试，仍没有才报"数据滞后"。parquet 只用于 `--dry-run --offline`。

**风控闸门（C1）**
```python
class RiskGate:
    def check_open(self, symbol: str, side: str, lots: float, magic: int, now: datetime) -> RiskDecision
    # 组装 RiskState：账户全部持仓、快照合约大小、实时报价、本地下单日志（频率）、
    # 当日盈亏（deals）、停机标志（halt.json / killSwitch / pause.json）→ RiskProcess.check_order
```
- 执行器每次开仓、加仓前调用 `check_open`；拒绝 → 不下单，原因写心跳 `last_error` 与 `alerts.log`。平仓不受限。
- 执行器每轮先检查 `commands/flatten.json`：存在 → 平掉本 magic 全部持仓，写 `flatten.ack-<magic>`；
  风控进程在所有登记的 magic 都确认后把指令文件归档。
- 执行器每次执行后写 `expected-<magic>.json`；对账进程读取全部 `expected-*.json`。
- 心跳改为每策略一个文件 `heartbeat-<magic>.json`；风控进程检查所有登记策略，任一超时 → 全局停机。
- 点差闸门：开仓前当前点差 > `sessions.toml` 中的倍数 × 模型 p90（该小时）→ 跳过并记录。
- 定手数：`RiskSizing` + 账户权益 + 止损距离（T23 同一函数）；风控只设上限。
- 出场语义：实现 `reentry="after_signal_change"`（检测到止损、止盈成交后记 `blocked_dir`，持久化到
  `state-<magic>.json`，重启后恢复）；`time_stop_bars`、`week_close_exit` 未实现前，策略用到就拒绝启动。
- 日报：`scripts/live_daily_report.py` 调用 `render_daily_report`，加入每日计划任务。

**集成测试（tests/test_mt5live_integration.py，FakeMT5 + 临时目录，运行器、执行器、风控进程、对账一起跑）**
- [ ] 正常开平仓；重复触发不重复下单；进程重启不重复下单
- [ ] killSwitch：之后不再开仓，平仓照常
- [ ] 日亏达到上限：写 flatten 与 halt → 执行器全平并确认 → 不再开仓
- [ ] 冬令时日期 22:10 UTC 禁入、夏令时日期 21:10 UTC 禁入
- [ ] 对账不一致 → pause → 不再开仓；恢复一致后自动解除
- [ ] 心跳超时 → 全局停机
- [ ] 周末与每日维护时段：不报错、不下单
- [ ] 点差尖峰：跳过开仓
- [ ] `after_signal_change`：止损后信号仍为 1 不重入；信号归零再变 1 后重入；重启后状态保持
- [ ] **干跑一个交易日**（真实终端，`--dry-run`）：日志脱敏后写 `docs/reports/live-dryrun-<日期>.md`

## T27：研究库隔离与锁箱预算（H3、M11）

**Files:** 新建 `scripts/export_research_store.py`；`scripts/run_backtest.py`、`scripts/bt.py`（默认读研究库）；
`docs/p6/permissions.md`、`README.md`；`src/mt5quant/research/ledger.py`（锁箱预算）；`scripts/reconcile_ab.py`。

- [ ] `export_research_store.py`：把 `time < cutoff` 的 K 线导出到 `data_research/bars/**`（同布局，meta 记录来源哈希），
      连同 T28 的成本模型文件复制到 `data_research/costs/`；研究侧 CLI 的 `--data-root` 默认为 `data_research`。
- [ ] 研究库导出后自检：研究库里任何序列的 `last_time` 都早于 cutoff，否则导出失败。
- [ ] `permissions.md`：研究、审查账户对 `data\bars`、`data\ticks`、`C:\mt5-lockbox` 拒读，对 `data_research` 只读；
      README 删去"直读 data/bars"的示例，改为研究库用法，并注明主库仅限管理员与实盘侧。
- [ ] `reconcile_ab.py` 读锁箱期数据必须显式 `--ops-ack`，并在台账里记一行 `actor=ops`。
- [ ] 锁箱预算：台账新增 `lockbox_budget(cutoff, max_uses)`；G5 在预算用尽时拒绝。
      具体数值与退役规则等用户对 D16 的决定，未决定前 `max_uses` 默认 5。

## T29：权限白名单与文档对齐（M12、L1、L2、L3）

- [ ] `scripts/hooks/pre-commit-readonly-paths` 改为白名单：研究、审查角色只能新增 `research/cards/*.toml`
      （已提交的卡文件不得修改）、修改 `src/mt5quant/strategies/library/**`、`tests/test_strategies_*.py`、
      `docs/research/**`；其余路径一律拒绝。新增与修改用 `git diff --cached --name-status` 区分
      （卡文件只放行状态 `A`）。守护测试覆盖新增卡、修改旧卡、改成本模型、改 `bt.py`、改钩子本身五种情形；
      现有 `tests/test_p6_docs.py::test_hook_allows_research_paths` 里的 `docs/p6/research-agent.md`
      改为应拦截（研究规则由管理员维护，研究角色不能改自己的规则）。
- [ ] `docs/p6/research-agent.md`、`review-agent.md` 与 T21 的谱系规则、T27 的研究库对齐（删去与代码矛盾的
      "version +1 改同一文件"）。
- [ ] 本计划与上一份计划的复选框做一次核实：没有证据的取消勾选并注明原因
      （例如上一份计划 T10 的 MT5 测试器对账）。
- [ ] 每完成一个阶段，更新蓝图 §2 现状表（证据等级如实填写）。

## T26：证据补齐（H7）

- [ ] 多笔 A/B 对账：`MACrossRef` 同逻辑的 Python 策略加 ATR 止损、止盈，多空都做，在 XAUUSD H1 与 USTEC M5
      的 tick 覆盖期（2026-01 起）各跑一遍，`immediate` 与 `after_signal_change` 两种模式，合计 ≥ 100 笔；
      按出场类型给出时间差与净利差的分布。需要 `--ops-ack`（读锁箱期数据只做执行口径核对，不产生研究结论）。
- [ ] MT5 策略测试器对账（U0 用户操作）：用户跑完 EA 后，agent 运行 `reconcile_mt5_tester.py` 并写报告。
- [ ] 模拟盘影子对账（演练期间）：用演练期的真实 tick 回放 B 层，与模拟盘实际成交逐笔比对，
      按小时校准 `SlippageModel`（成本版本号递增）。
- 报告：`docs/reports/reconciliation-v2-<日期>.md`。

---

## 集成与验收

1. 每个任务结束：全量 pytest 全绿 → 提交 → 推送 → Actions 为绿 → 复选框写上证据。
2. 阶段 A（T20、T28、T23 的基线重跑）结束：`gates-calibration-v2` 报告。
3. 阶段 B、C（T21、T22）结束：`gate-power` 报告；若调整门槛，记录决策并重新冻结。
4. 阶段 D（T24、T25）结束：集成测试全过 + 干跑报告。
5. 以上全部完成后，进入两周模拟盘演练，按蓝图 §7 验收清单逐项打勾（每项附日志路径）。

## 明确不做

机器学习模型；M1 剥头皮；实盘真金下单；改写 git 历史或改变仓库可见性；在 T21 之前跑 G5；
在 T24 之前以非干跑模式启动运行器。
