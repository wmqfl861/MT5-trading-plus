# 换日与隔夜费口径（T20 / H1 / H2）：规则核对与 DST 实测计划

- 日期：2026-10-26（T20 施工）；核对数据：`data/specs/latest.json`（scripts/snapshot_specs.py 抓取）
- 对应决策：D10（隔夜费：周末不计、三倍日按品种组、计费时刻 17:00 纽约）、
  D11（换日禁入窗按 17:00 纽约定义，回测与风控共用一份配置）
- 对应代码：`src/mt5quant/costs/swap.py`（SwapRule v2）、`src/mt5quant/costs/spread.py`
  （纽约本地小时分桶）、`src/mt5data/sessions.py`（SessionPolicy）、
  `config/sessions.toml`（禁入窗唯一来源）
- 测试证据：`tests/test_costs_swap.py`、`tests/test_mt5data_sessions.py`、
  `tests/test_calibrate_costs.py`、`tests/test_backtest_engine_a.py`（禁入推迟）、
  `tests/test_backtest_engine_b.py`（B-0.2）、`tests/test_mt5live_riskproc.py`

## 1. 规则框架（本报告核对的对象）

| 项 | 口径（T20 落地） | 依据 |
| --- | --- | --- |
| 计费时刻 | 17:00 America/New_York（zoneinfo 逐日换算：夏令时 21:00 GMT、冬令时 22:00 GMT） | Exness 官方说明；铁律 v3 第 1 条（禁止固定 UTC 小时） |
| 周末 | 周六、周日不计费（`charge_weekdays = 周一..周五`，全部品种组） | Exness 官方：所有品种组（含加密货币）周末不收 |
| 三倍日 | 按品种组：外汇、金属周三；指数、股票周五；加密货币交叉盘周三、其余周五。实现一律取快照 `swap_rollover3days`（MT5 星期枚举，经 `mt5_to_py_weekday` 换算） | Exness 官方表 |
| 换日禁入窗 | 17:00 纽约前 5 分钟到后 60 分钟；BTCUSD 豁免；EURUSD 扩展到 23:00 本地（初值） | `config/sessions.toml`；决策 D11 |
| 点差分桶 | 纽约本地小时（DST 拆开 21/22 点 UTC 不再混桶）；缺统计小时 forbid（禁入 + 出场取全天最大 p90） | H2 修复 |
| 点差闸门 | 当前点差 > 3.0 × 模型该小时 p90 → 跳过开仓（T24 实盘接线时使用） | `config/sessions.toml [costs]` |

钉死测试（已绿）：周五 20:00→周一 20:00 = 1 夜；整周 = 7 夜（周一×1+周二×1+
周三×3+周四×1+周五×1，逐段断言）；2026-07-15（EDT）计费时刻 = 21:00 UTC、
2026-12-15（EST）= 22:00 UTC；校准往返——周二→周四实际 −40、校准费率 −10、
回测按同一规则（含三倍日）复算回 −40。

## 2. 快照三倍日 × Exness 官方表核对（结论：一致）

`data/specs/latest.json`（点数模式 swap_mode=1；金额=swap × point × contract_size）：

| 品种 | swap_rollover3days（MT5 枚举） | → Python weekday | Exness 官方表（品种组） | 核对 |
| --- | --- | --- | --- | --- |
| XAUUSD（金属） | 3（周三） | 2（周三） | 金属：周三 | 一致 |
| EURUSD（外汇） | 3（周三） | 2（周三） | 外汇：周三 | 一致 |
| BTCUSD（加密货币，非交叉盘） | 5（周五） | 4（周五） | 加密货币：交叉盘周三、其余周五 | 一致 |
| USTEC（指数） | 5（周五） | 4（周五） | 指数：周五 | 一致 |

四品种快照值与官方表全部一致，无需停下报告用户；`SwapModel.from_snapshot` 与
实测校准路径（`scripts/run_backtest.py::build_costs`）一律按快照取三倍日。
快照每手每夜参考（点数换算）：XAUUSD 多 −490.8 点 × 0.001 × 100 = −49.08 USD/手/夜；
EURUSD 多 −5.9 点 × 1e-5 × 1e5 = −5.9 USD/手/夜；BTCUSD 多 −1786.5 点 × 0.01 × 1
= −17.865；USTEC 多 −633.8 点 × 0.01 × 1 = −6.338（空头当前快照为 0）。

## 3. Exness 冬令时跟随欧洲还是美国：DST 周实测（待 10-31 后补写）

> **占位节（数据依赖）**：本节需要 2026-10-26 至 10-30（欧洲已切冬令时、
> 美国未切）这一周的完整 tick 才能落笔。10-31 之后按下列流程定稿。

判定方法（写死，避免临场发挥）：

1. 数据：`data/ticks/{XAUUSD,EURUSD,BTCUSD,USTEC}` 中 2026-10-19..10-23（全夏令时周）
   与 2026-10-26..10-30（欧洲冬、美国夏）两周的 tick，按 UTC 20:00–23:00 逐小时
   统计相对点差 (ask−bid)/mid 的 p50/p99（四品种 × 4 小时 × 2 周 一张表）。
2. 判定：若换日点差尖峰在 10-26..10-30 周从 21:00 UTC 移到 22:00 UTC
   （21 点回落、22 点抬升）→ Exness 跟随**欧洲**（10-25 已切换）；若尖峰仍停在
   21:00 UTC、11-02 之后才移到 22:00 → 跟随**美国**（11-01 切换）。
3. 定稿动作：
   - 若跟随美国：`config/sessions.toml` 与 `SwapRule` 默认（America/New_York）
     不变，仅需在实测表下注明"已确认"；
   - 若跟随欧洲：换日锚点时区改为"欧洲口径"（如 Europe/Helsinki 或 Exness
     服务器对应规则）——这是**配置级变更**，须改 sessions.toml / SwapRule 默认、
     重新钉死 DST 测试锚点、在蓝图决策记录追加一行（D10/D11 修订），并同步
     本报告结论。
4. 同周顺带核对：EURUSD 扩展窗终点 23:00 本地是否足够（22:00–23:00 UTC 段的
   p50/p99 是否已回落到日内水平），不够就调整 `end_local` 初值。
   （背景：docs/research/scope-2026-10-25.md 报告过 EURUSD 22:00 的"残余换日"，
   需用冬夏拆分后的数据重新判定它是否只是冬令时样本的换日尖峰。）

### 3.1 实测结果（待补）

| 周 | 品种 | UTC 20 p50/p99 | 21 p50/p99 | 22 p50/p99 | 23 p50/p99 |
| --- | --- | --- | --- | --- | --- |
| 10-19..10-23（全夏） | （待补） | | | | |
| 10-26..10-30（欧冬美夏） | （待补） | | | | |

结论（待补）：跟随欧洲 / 跟随美国；sessions.toml 是否需要改、改了什么。

## 4. 过夜实测校准（U0 用户操作依赖，待补）

> **占位节**：等用户在模拟盘开出跨周三、跨周末的过夜小手数持仓（U0 清单第 2 项）
> 后执行，流程固定：

1. 用户确认过夜成交在账户历史可见（至少一笔跨周三、一笔跨周末）。
2. 主会话重跑 `scripts/calibrate_costs.py`（窗口覆盖上述持仓）。
3. `data/reports/costs/calibration.md` 的实测每手每夜费率与本报告第 2 节的
   快照推算并列，偏差 > 20% 时停下报告用户（脚本自动告警）。
4. 实测确认后，`docs/research/gates-calibration-v2-<日期>.md`（T23 基线重跑）
   采用"实测优先、快照兜底"的成本链路。

实测结果（待补）：……
