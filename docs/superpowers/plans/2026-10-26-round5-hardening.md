# 第五轮施工计划（2026-10-26）：实盘语义修正 · 测量补强 · 研究完整性

> **For agentic workers:** 按"执行顺序"一节逐任务推进。每个任务的流程：
> 1. 先写失败测试；
> 2. 实现；
> 3. 本任务测试通过；
> 4. **全量回归**；
> 5. 提交并推送；
> 6. **确认 GitHub Actions 为绿**（红了先修，不推新功能）；
> 7. 勾选复选框，并**在同一行写上证据**（测试名、报告路径或日志路径）。
>
> 没有证据的复选框一律不勾。

**Goal:** 修正第五轮审核发现的问题：
- 实盘：对账死锁（C1）、逐 bar 调仓（C2）、全平只生效一次（C3）、跨进程文件契约（M1、M2、M8）；
- 补影子盘（M3）；
- 测量：EURUSD 禁入窗与冬令时口径（H1、H5）、成本模型补强（H2）；
- 研究完整性：代码指纹与台账防篡改（H3、M6）、研究隔离落地（H4）；
- 关卡标定收尾（T22、M4、M5），对账口径收紧（H6）。

完成后进入两周模拟盘演练（蓝图 v3.1 §7）。

**输入文档（先读）：**
1. `docs/reviews/2026-10-25-review-round5.md`：问题清单与复现探针 P8–P13
2. `docs/reviews/probes/2026-10-25-round5-probes.py`：探针脚本；修复后再跑一遍，输出应与注释里的"期望"一致
3. `docs/blueprint.md`（v3.1）：四级证据、目标架构、铁律 v3.1（新增 14、15 条）、路线图、决策 D19–D29

**基线：** 提交 `2f31c60`；GitHub Actions 1325 passed。

**Tech Stack:** Python 3.12（`.venv`）；pandas、numpy、pyarrow、duckdb；标准库 `sqlite3`、`tomllib`、`zoneinfo`、`hashlib`。
不新增第三方依赖（确需新增先问用户）。

## Global Constraints（所有任务隐含遵守）

- **禁止事项**（直到对应任务完成）：
  - T30–T34 完成、影子盘跑满 1 个交易日之前，不以非干跑模式（含模拟盘）启动 `live_runner.py`；
  - T37、T39 完成之前不跑 G5；
  - T35–T38 完成之前不开新研究卡（基线重跑除外）。
- **集成测试的必备情形**：本轮新增的实盘集成测试必须包含三类情形——ATR 逐 bar 变化、服务器端 SL/TP 出场、连续两次全平；只测单个组件不算完成。
- **跨进程文件**：一律经同一个原子写函数（T33）；指令文件带编号；每策略各写各的文件。
- **容差**：容差只能依据机制设定（铁律第 14 条）。为让结果合规而放宽容差，须单独写明理由并经用户确认。
- **锁箱期数据**：只用于运维测量（换日时刻、成本、执行口径），必须带 `--ops-ack`，台账记 `actor=ops`，不产生研究结论。
- **`config/gates.toml`**：只在 T39 中、且有证据并经用户批准时修改；修改时同一提交更新 `FROZEN_GATES_SHA256`，并在蓝图决策记录里追加一行。
- **脱敏**：报告与日志进入 `docs/` 之前去掉账号、IP、令牌。
- **测试命令**（Git Bash）：`./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`。
- **提交**：消息格式 `<type>(T##): 摘要`；不改写 git 历史、不强推、不改仓库可见性（只由用户决定）。

## 执行顺序

| 顺序 | 任务 | 规模 | 依赖 | 解决的问题 |
| --- | --- | --- | --- | --- |
| 1 | T35 换日口径与 EURUSD 窗口（**时间敏感**） | S | — | H1、H5 |
| 2 | T30 对账意图语义 | L | — | C1 |
| 3 | T31 实盘只在开仓时定手数 | S | — | C2 |
| 4 | T32 全平指令编号协议 | S | — | C3 |
| 5 | T33 跨进程文件契约 | S | T30–T32 | M1、M2、M8、L3 |
| 6 | T34 影子券商 + 1 个交易日影子盘 | M | T30–T33 | M3 |
| 7 | T36 成本模型补强 + 基线 v3 | L | T35 | H2、M7 |
| 8 | T37 代码指纹 + 台账哈希链 | M | — | H3、M6 |
| 9 | T38 研究隔离落地 | M | T37 | H4 |
| 10 | T39 关卡标定收尾 | M | T36、T37 | T22、M4、M5 |
| 11 | T40 A/B 对账口径收紧 | S | T36 | H6（容差） |
| 12 | T41 流程与文档 | S | 每阶段 | H6、L1–L4 |
| 13 | 模拟盘两周演练 | — | 以上全部 | 蓝图 §7 |

规模按以往节奏估计，仅供排序：S ≈ 半个工作日，M ≈ 1 个工作日，L ≈ 2 个工作日及以上。

---

## U0：用户操作（agent 不得代办，只列清单并等待确认）

- [ ] **S1**：
  - `mcp.json` 改回 127.0.0.1 并轮换令牌；
  - 删除 MCP 端口的防火墙入站规则；
  - 确认 RDP 不暴露公网，或已限制来源 IP；
  - 决定仓库改 Private 还是清理历史（目前仍为 public）。
- [ ] **校准持仓**：确认是否已开。若未开，**10-26（周一）20:00 UTC 之前**用 `scripts/calibration_positions.py --action open` 开出。T35 会给脚本补上 BTCUSD、USTEC 两笔，持有到 10-30 之后再平。
- [ ] **MT5 策略测试器对账**（图形界面操作，步骤见 `mql5/Experts/*.mq5` 头注）。
- [ ] **决策 D16**（锁箱预算与退役规则），见蓝图 §8。
- [ ] **T38 完成后**：
  - 按 `docs/p6/permissions.md` 执行 ACL；
  - 研究 agent 改用独立 Windows 账户运行；
  - 决定是否开启 main 分支保护（研究分支经 PR 合并、CI 路径白名单必须通过）。

## T35：换日口径与 EURUSD 窗口（H1、H5）——10-26 开始

**Files:**
- 新建 `scripts/measure_rollover.py`、`scripts/watch_swap.py`；
- 修改 `config/sessions.toml`、`src/mt5data/sessions.py`（扩展窗长度校验）、`scripts/calibration_positions.py`；
- 修改 `scripts/setup_scheduled_tasks.ps1`（本周的 swap 轮询任务）；
- 更新 `docs/research/rollover-dst-2026-10-25.md` 第 3、4 节；
- 对应测试。

**一、换日时刻（历史 K 线，现在就做）**
- `measure_rollover.py`：读主库 M5 K 线（锁箱前年份，无需 `--ops-ack`）。对 USTEC、XAUUSD，统计以下错位窗口里每日"无 bar 空档"在 UTC 的起止：
  - 2021–2025 年每年 3 月（美国第二个周日 → 欧洲最后一个周日）；
  - 2021–2025 年每年 10 月底（欧洲最后一个周日 → 美国 11 月第一个周日）。
- 与两侧"美欧同为夏令时 / 同为冬令时"的对照周比较。
- **判定**：错位窗口里空档在 21:00 UTC → 跟随纽约；在 22:00 UTC → 跟随欧洲。
- EURUSD 没有长时间的每日休市：用 tick（回补后，见 T36）按纽约本地小时统计 20:00–23:00 UTC 的点差 p50/p99；2026 年 3 月的错位窗口属于锁箱期，需带 `--ops-ack`。

**二、隔夜费计费时刻（只有本周能测）**
- `watch_swap.py`：只读；10-26 至 10-30 每天 20:30–23:30 UTC，每 60 秒读一次校准持仓（magic 20261025）的 `swap` 字段，变化即记一行（时刻、品种、swap 前后值）。
- 加进计划任务，只在本周运行；输出写 `data/reports/costs/swap-watch-<日期>.csv`。
- **判定**：变化发生在约 21:00 UTC → 计费锚点为纽约 17:00（与 `SwapRule` 缺省一致）；在约 22:00 UTC → 改锚点，并在蓝图追加决策。

**三、EURUSD 扩展窗**
- 按纽约本地小时重测 EURUSD 换日后的点差恢复时刻，重设 `[forbidden.extended.EURUSD] end_local`。预期为删除这一项或不晚于 19:00；**禁止**再出现跨越亚洲时段的窗口。
- `SessionPolicy` 加校验：扩展窗长度（换日时刻到 `end_local`）> 3 小时 → `SessionConfigError`，除非显式写 `allow_long_window = true` 并附理由注释。

**四、校准脚本**
- `CALIBRATION` 增加 BTCUSD（持有跨周末，检验"加密货币周末不收"）和 USTEC（持有跨周五，检验周五三倍），手数一律取快照的 `volume_min`，不写死。
- 删除"由计划任务平仓"这句提示，或真的加上平仓计划任务——二选一，按实际写。

**失败测试先行：**
- [ ] 2026-07-15 22:30 UTC 与 2026-07-16 01:30 UTC 的 EURUSD 允许开仓（探针 P11 反转）；
- [ ] 扩展窗 > 3 小时且无 `allow_long_window` → 拒绝加载；
- [ ] `measure_rollover` 在合成 K 线上（构造 21:00 与 22:00 两种空档）判定正确；
- [ ] `watch_swap` 在 FakeBroker 上：swap 字段在 21:00 变化 → 记录 1 行，时刻正确。

**报告：** `rollover-dst` 第 3 节写历史判定表与本周 swap 计费时刻，第 4 节写过夜实测费率与快照推算的对比。结论与 `SwapRule`、`sessions.toml` 不一致时，停下来报告用户。

## T30：对账意图语义（C1，决策 D19、D20）

**Files:**
- `src/mt5gw/models.py`、`src/mt5gw/account.py`（`Deal.reason`）；
- `src/mt5live/{reconcile,executor,gate}.py`；
- `scripts/live_reconcile.py`，新建 `scripts/live_ack_external.py`；
- `tests/fakes_live.py`（`close_external` 增加 `reason`），`tests/test_mt5live_integration.py`；
- 文档 `docs/p6/` 中涉及 expected 的段落。

**Interfaces:**
```python
# mt5gw.models.Deal 增加字段
reason: int | None          # MT5 deal.reason 原值；常量以本机 MetaTrader5 包为准（DEAL_REASON_SL/TP/SO…）

# mt5live.reconcile
@dataclass(frozen=True)
class Intent:
    symbol: str
    magic: int
    side: str                # "buy" / "sell"
    volume: float            # 目标净手数（≥0）
    status: str              # "pending"（已决定、订单未回）| "confirmed"
    cause: str               # decision_id | "flatten:<id>" | "external_exit:<deal_ticket>" | "ack_external"
    ts: datetime             # naive UTC

def write_intent(path: str, intent: Intent) -> None      # 经 T33 的原子写
def load_intents(live_dir: str) -> list[Intent]           # 读全部 expected-<magic>.json（兼容旧格式）
def diff_positions(intents, actual, *, now: datetime, grace_s: float = 60.0,
                   volume_tolerance: float = 1e-6) -> ReconcileResult
    # pending 且 now - ts < grace_s 的 magic 本轮不判；其余按目标比对
def write_pause(live_dir: str, magic: int, *, paused: bool, reason: str | None,
                diffs=(), now: datetime | None = None) -> str   # pause-<magic>.json
```

**语义：**
1. **下单前**：执行器写 `Intent(target, "pending", cause=decision_id)`。
2. **订单返回后**：写 `Intent(实际净持仓, "confirmed")`。部分成交如实记录，并写告警。
3. **每轮开头识别外部出场**：
   - 已知持仓消失时，按 `position_id` 查当日成交；
   - 平仓成交的 `reason` ∈ {SL, TP, SO} → **已解释**：意图改为实际净持仓（confirmed，`cause=external_exit:<deal>`），`after_signal_change` 的禁入照旧；
   - 其他原因（人工、未知）→ **不改意图**，写告警，交给对账进程处理。
4. **持仓凭空出现**（本 magic 但不是自己下的单）→ 不改意图，交给对账进程处理。
5. **暂停按策略隔离**：对账进程只写 `pause-<magic>.json`；全局 `pause.json` 只留给人工。闸门同时读两者；读不出来按"暂停"处理（见 T33）。
6. **人工确认**：`live_ack_external.py --magic N --reason "..."` 把当前实际持仓登记为意图（`cause=ack_external`，写日志），用于人工平仓后恢复交易。

**失败测试先行（集成，LiveStack + 对账进程一起跑）：**
- [ ] 止损出场（reason=SL）后对账一致、无暂停；`immediate` 下一根照常重开，`after_signal_change` 下禁止重入（探针 P8 反转）
- [ ] 止盈出场（reason=TP）同上
- [ ] 人工平仓（reason=CLIENT）→ 只写 `pause-<本magic>.json`；另一 magic 照常开仓；`live_ack_external` 之后恢复
- [ ] 对账进程恰好在"pending 已写、订单未回"时运行（在 FakeBroker 下单钩子里调用对账）→ 不暂停
- [ ] 部分成交 → 意图 = 成交量，有告警；下一轮补足
- [ ] 旧格式 `expected-*.json` 能读（兼容），写出的都是新格式

## T31：实盘只在开仓时定手数（C2，决策 D22）

**Files:** `src/mt5live/executor.py`（`LiveExecutor._target_volume`）、`tests/test_mt5live_integration.py`。

```python
def _target_volume(self, decision, net: float) -> float:
    if decision.signal == 0:
        return 0.0
    if net != 0 and (net > 0) == (decision.signal > 0):
        return net                        # 同向持仓：保持入场时的手数，不调仓
    lots = (decision.volume_lots if decision.volume_lots is not None
            else self._sizer.lots_for(decision))
    return decision.signal * lots if lots > 0 else 0.0
```

- [ ] 失败测试先行：
  - 6 根 bar 的 ATR 依次为 2.0/2.4/1.8/2.6/2.0/1.7、信号恒为 +1 → 市价单 1 笔、平仓 0 笔、持仓 1 张（探针 P9 反转）；
  - 反手 → 平仓后按当时 ATR 与权益重新定手数；
  - 止损后再入场 → 重新定手数；
  - 固定 `volume_lots` 的配置行为不变。
- [ ] 执行器文档写明：持仓期间不调仓；加仓、减仓只会在固定手数配置被人为改动后出现，并写告警。

## T32：全平指令编号协议（C3，决策 D21）

**Files:** 新建 `src/mt5live/commands.py`（风控进程、闸门、执行器共用）；`riskproc.py`、`gate.py`、`executor.py`；新建 `scripts/live_retire.py`；测试。

```python
# mt5live.commands
@dataclass(frozen=True)
class FlattenCommand:
    id: str                   # uuid4().hex
    reason: str
    ts: datetime

def write_flatten(live_dir: str, *, reason: str, now: datetime) -> FlattenCommand
    # 已有未归档指令 → 返回它（不覆盖，首次原因优先）
def read_flatten(live_dir: str) -> FlattenCommand | None
    # 文件存在但读不出 → 抛 CommandUnreadableError（调用方按"有指令"处理）
def write_ack(live_dir: str, magic: int, command_id: str, *, closed: int, now: datetime) -> None
def archive_if_all_acked(live_dir: str, magics: set[int], now: datetime) -> bool
    # 所有登记 magic 的 ack.id == 当前指令 id → 归档指令并删除这些 ack；否则 False
```

- **执行器**：本 magic 的 ack 已对应当前指令 id → 不再重复平仓（返回 `flatten_acked`，不下新仓）。否则全平并写 ack。
- **`live_retire.py --magic N`**：确认该 magic 无持仓后，移走它的 heartbeat、expected、state 文件（归档），使它不再算作登记策略。
- [ ] 失败测试先行：
  - 第一次全平 → 归档 → 解除停机 → 第二次日亏 → 执行器再次全平（探针 P10 反转）；
  - 旧 id 的 ack 不触发归档；
  - 闸门触发的全平走同一协议；
  - 指令文件损坏 → 执行器按"有指令"处理（全平），并告警。

## T33：跨进程文件契约（M1、M2、M8、L3）

**Files:** 新建 `src/mt5live/fileio.py`（`write_json_atomic`、`append_jsonl`，复用 `mt5gw.snapshot._atomic_replace` 的重试）；`runner.py`、`executor.py`、`gate.py`、`reconcile.py`、`riskproc.py`、`monitor.py`、`scripts/live_runner.py`；测试。

- **原子写**：心跳、状态、ack、意图、暂停、指令一律经 `fileio.write_json_atomic`。
- **心跳失败不退出**：运行器写心跳失败时记日志、下一轮重试，不退出；连续 3 次失败写告警。
- **停机类文件 fail-closed**：`read_pause` 读不出 → 暂停（原因 `pause_unreadable`），并告警。
- **下单日志分文件**：改为 `orders-<magic>.jsonl`，闸门读全部 `orders-*.jsonl`（兼容旧 `orders.jsonl`）。坏行按"此刻的一笔下单"计入频率（保守），写告警，不阻断其他策略。
- **点差模型缺失**：实盘找不到点差模型文件时拒绝启动，除非显式 `--no-spread-gate`（启动日志标红）。
- [ ] 失败测试先行：
  - monkeypatch `os.replace` 前两次抛 PermissionError → 心跳写入成功、运行器不退出；
  - pause 文件损坏 → 拒绝开仓并给出原因；
  - 另一策略的下单日志有坏行 → 本策略闸门照常裁决，且频率计数多计 1；
  - 无点差模型且未给 `--no-spread-gate` → 启动失败。

## T34：影子券商 + 1 个交易日影子盘（M3，决策 D26）

**Files:** 新建 `src/mt5live/paper.py`；`scripts/live_runner.py`（`--paper`）、`scripts/risk_process.py`、`scripts/live_reconcile.py`（`--paper-state`）；测试；报告 `docs/reports/live-paper-<日期>.md`。

```python
class PaperBroker:
    """实现执行器与闸门用到的 TradingService / AccountService 子集；报价与 K 线来自真实终端。"""
    def __init__(self, market, *, state_path: str, contract_sizes: dict[str, float],
                 balance: float = 10_000.0): ...
    def poll_protective(self, now: datetime) -> list:   # 每轮按实时 bid/ask 检查 SL/TP，触发即平仓，成交带 reason
```

- **账户状态**：落 `data/live/paper/state.json`（经 `fileio`），风控进程与对账进程用 `--paper-state` 读取，不碰真实账户。
- **范围**：影子盘运行完整链路——闸门、定手数、意图、对账、风控、全平协议。只有成交是模拟的。
- **验收**：
  - 1 个交易日，2 个策略（一个 `immediate`、一个 `after_signal_change`，M5 或 M15，确保有止损/止盈出场）；
  - 期间演练 killSwitch，以及用测试风控配置把日亏上限调低、连续触发两次全平。
- [ ] 失败测试先行：PaperBroker 的 SL/TP 触发口径与引擎一致（多头按 Bid、空头按 Ask）；状态文件重启可恢复。
- [ ] 影子盘报告（脱敏）：决策数、订单数（应 = 入场 + 出场）、保护性出场数、暂停次数（无法解释的应为 0）、全平两次是否都执行、心跳与告警摘要。

## T36：成本模型补强 + 基线 v3（H2、M7，决策 D29）

**Files:** `scripts/download_ticks.py`（运维回补）、`scripts/build_cost_model.py`、`src/mt5quant/costs/{spread,slippage}.py`、`src/mt5quant/backtest/engine_a.py`（止损越幅）、`config/sessions.toml`（`[costs] min_days_per_hour`）；测试；报告 `docs/research/costs-v3-<日期>.md`、`docs/research/gates-calibration-v3-<日期>.md`。

1. **回补 tick**：USTEC、BTCUSD、EURUSD 从 2026-01-02 起回补，与 XAUUSD 对齐；属锁箱期，运维用途，带 `--ops-ack`。报告写明各品种覆盖起止、天数、冬夏季天数。
2. **采样口径**：`SpreadModel` 缺省改为 `sampling="bar_open_m1"`，即每个 M1 边界后的第一笔 tick（按时间加权，且只用很少内存）；保留 tick 加权口径用于对比。
   - 每个纽约小时记 `n` 与 `n_days`；
   - `n_days < min_days_per_hour`（缺省 20）→ 视为缺统计（按 forbid 处理）；
   - 按月流式构建，控制内存。
3. **止损越幅**：新增 `StopOvershootModel`（品种 × 纽约小时，越幅相对价格的 p50 / p90），由 tick 上"首笔穿价 tick 与价位之差"的统计得到。
   - A 层 `stop` 出场成交价加 p50 越幅（`stop_gap` 已按开盘价，不重复加）；
   - p90 进 G2 敏感性；
   - `cost_model_version` 递增。
4. **早年成本敏感性**：G2 的成本放大检查同时报告"2020 年以前点差 ×1.5"这一情形（只报告，不改门槛）。
5. **重跑**：六张基线卡在新成本模型下重跑 screen 与 G0–G1（过 G1 的继续跑 G2–G4），写 `gates-calibration-v3`，与 v2 逐项对比并归因。

- [ ] 失败测试先行：
  - 合成 tick 上 `bar_open_m1` 采样结果与手算一致；
  - `n_days` 不足 → 缺统计；
  - 越幅只加在 `stop`；
  - 流式构建与一次性构建结果一致（同一数据）。

## T37：代码指纹 + 台账哈希链（H3、M6，决策 D23、D27）

**Files:** `src/mt5quant/research/{ledger,cards}.py`、`scripts/bt.py`、`scripts/run_backtest.py`、`src/mt5quant/backtest/report.py`；新建 `scripts/ledger_anchor.py`；测试。

```python
# mt5quant.research.cards
def strategy_fingerprint(strategy_ref: str, repo_root) -> str
    # 策略类所在模块文件 + 它在 strategies/library 内 import 的模块，逐个 git hash-object，排序后整体 sha256
def ensure_strategy_clean(strategy_ref: str, repo_root) -> None
    # 上述文件未提交或工作区不干净 → StrategyDirtyError

# mt5quant.research.ledger
# runs 表新增 strategy_sha、prev_hash、row_hash；gates 与 lockbox_uses 同样带 prev_hash、row_hash
def verify_chain(self) -> list[str]        # 返回断链或篡改的行；空列表 = 完好
```

- **去重键**：加入 `strategy_sha`。
- **绑定**：同一卡版本首跑时登记 `strategy_sha`；之后代码指纹不同 → 拒绝运行，提示新建下一版本。
- **哈希链**：`row_hash = sha256(规范化行 JSON + prev_hash)`。
  - `bt ledger verify` 校验整条链；
  - `ledger_anchor.py` 把链头写进 `docs/research/ledger-anchors.md`，由管理员每日提交（计划任务）。
- **旧库迁移**：旧行补 `strategy_sha = "legacy"`，并从第一行起重建哈希链。迁移本身记一行 `actor=migration`。
- [ ] 失败测试先行：
  - 改策略代码后用同参数重跑，N 加 1；
  - 首跑后改代码 → 同卡版本拒绝运行；
  - 策略文件未提交 → 拒绝运行；
  - 手工改一行台账 → `verify` 报出该行；
  - 删一行 → 报出断链。

## T38：研究隔离落地（H4，决策 D24、D25）

**Files:** `scripts/run_backtest.py`、`scripts/bt.py`（缺省数据根）；新建 `.github/workflows/research-allowlist.yml`；`scripts/hooks/pre-commit-readonly-paths`（把检查逻辑抽成可被 CI 调用的函数）；`docs/p6/permissions.md`、`README.md`；测试。

- **缺省读研究库**：
  - 研究侧 CLI 的 `--data-root` 缺省为 `data_research`；
  - 读主库须显式 `--main-store`；读主库的锁箱期再加 `--ops-ack`；
  - G5 路径要求 `--main-store`，并且已获批准。
- **CI 路径白名单**：对 `research/**` 分支发起的 PR，`research-allowlist.yml` 用 `git diff --name-status origin/main...HEAD` 按研究角色规则检查，违规即失败。规则与本地钩子同一份代码。
- **用户侧清单**：`permissions.md` 写清楚 ACL 命令、研究账户的运行方式、分支保护的设置步骤（见 U0）。
- [ ] 失败测试先行：
  - 不带参数时数据根为 `data_research`；
  - 读 `data/` 而不带 `--main-store` → 报错；
  - CI 检查函数对"修改旧卡、改 bt.py、改钩子本身"三种情形报违规，对新增卡与策略库修改放行。

## T39：关卡标定收尾（T22、M4、M5）

**Files:** `src/mt5quant/validation/synthetic.py`、`scripts/gate_calibration.py`；测试；报告 `docs/research/gate-power-<日期>.md`。

- **日内零假设**：新增 `slot_permute_bars`：
  - 把同一纽约本地时刻（同一个 bar 槽位）的 bar 收益在不同日期之间置换；
  - 保留各时段的波动分布，破坏日内的先后依赖和跨日次序；
  - 多日持仓策略继续用现有的日块置换。按卡的持仓周期选用零假设，并在报告中写明。
- **实验规模**：
  - 正对照：夏普 0.3 / 0.6 / 1.0 / 1.5，每档 20 个种子；
  - 负对照：两种零假设，各 20 个种子。
- **G5**：用与锁箱等长的合成窗口，量化 G5 的检出率与误报率。
- **合并评估变体**：多品种合计交易数、同时要求每个品种为正（蓝图 §6 的提议）；只出数据，不改门槛。
- **决策规则**（写进报告）：真实夏普 1.0 时某道关卡检出率 < 50%，或负对照误报率 > 5% → 提出门槛调整，经用户确认后修改 `gates.toml` 并重新冻结。
- [ ] 失败测试先行：
  - `slot_permute_bars` 保持每个槽位的收益多重集不变；
  - 种子相同 → 结果逐位一致；
  - 在植入"日内第 k 根 bar 漂移"的数据上，置换后漂移消失。

## T40：A/B 对账口径收紧（H6，铁律第 14 条）

**Files:** `scripts/reconcile_ab.py`、`tests/test_reconcile_ab.py`、`tests/test_backtest_reference.py`；报告 `docs/reports/reconciliation-v3-<日期>.md`。

- 撤回本轮放宽的常量（120 秒、1 倍初始风险、6 根 bar），改为按机制核对：
  - **入场**：B 层成交时刻 = 该 bar 边界后第一笔 tick 的时刻（误差 ≤ 1 毫秒）；A 层 = bar 开盘。边界后超过 2 秒才有第一笔 tick 的行单列为"静默边界"，不算违规；
  - **保护性出场**：B 层成交 tick = 按正确一侧（多头 Bid、空头 Ask）第一笔穿过价位的 tick；A 层 = 价位（跳空时为开盘价）；
  - **净利差拆解**：拆成"模型点差 vs 实际点差""止损越幅（T36）""其余"三项。"其余"> 1 个点差的行判为违规。
- 用 T36 的成本模型重跑四组对账，写 v3 报告。
- [ ] 失败测试先行：
  - 人为把 B 层空头止损改成按 Bid 触发 → 被判违规（旧的 1R 容差测不出来）；
  - 静默边界的行不判违规。

## T41：流程与文档（H6、L1–L4）

- [ ] **补证据**：第四轮计划 T21、T22、T24、T25、T26、T27、T29 的复选框逐条补证据（测试名、报告路径），没有证据的不勾并注明原因。
- [ ] **修正报告笔误**：对账 v2 报告把 `missNY[17]` 误写成"缺 17 个纽约小时"（L1），改为"缺纽约 17 点这一小时"。
- [ ] **写明口径**：在文档里写明"日亏按 UTC 自然日"（L2）与"G3 测试窗收益含预热段持仓的浮动"（L4）。
- [ ] **CI 纪律**：每次推送后用 `gh run watch` 或 Actions 页面确认结果；红了先修，修好之前不推新功能。
- [ ] **探针复跑**：每完成一个阶段，更新蓝图 §2 现状表（四级证据如实填写），并重跑探针脚本，把输出贴进提交说明。

---

## 集成与验收

1. 每个任务结束：全量 pytest 全绿 → 提交 → 推送 → Actions 为绿 → 复选框写上证据。
2. 实盘阶段（T30–T33）结束：
   - 新增集成测试全过；
   - 探针 P8、P9、P10 的输出与"期望"一致。
3. 影子盘（T34）：1 个交易日报告，无法解释的暂停 = 0、持仓期间调仓 = 0、两次全平都执行。
4. 测量阶段（T35、T36）结束：
   - `rollover-dst` 与 `costs-v3` 两份报告；
   - `gates-calibration-v3`。
5. 完整性阶段（T37、T38）结束：`bt ledger verify` 通过；CI 路径白名单生效。
6. 标定（T39）结束：`gate-power` 报告；若调整门槛，记录决策并重新冻结。
7. 以上全部完成后进入两周模拟盘演练，按蓝图 §7 验收清单逐项打勾，每项附日志路径。

## 明确不做

- 机器学习模型；M1 剥头皮；实盘真金下单；
- 改写 git 历史或改变仓库可见性；
- 在 T37、T39 之前跑 G5；
- 在 T34 影子盘通过之前以非干跑模式启动运行器。
