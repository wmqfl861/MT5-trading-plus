# mt5gw — MT5 量化系统基石（数据与交易网关）

一个 Python 网关库，独占与本机 MT5 终端的通信，向策略层提供**品种目录、
行情、账户、交易**四类能力。网关内不含任何策略逻辑——SL/TP 只透传，
绝不自动添加；错误如实抛出，绝不静默吞掉。

## 安装

```bash
py -3.12 -m venv .venv                 # 或任意 64 位 Python 3.11–3.13
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

唯一运行依赖是 MetaQuotes 官方的 `MetaTrader5` 包；测试另需 `pytest`。

## 最小用例

```python
from mt5gw import MT5Gateway

with MT5Gateway() as gw:                # 终端路径可用 config 定制
    specs = gw.symbols.all()            # ① 全部品种及规格
    q = gw.market.quote("EURUSD")       # ② 实时报价（同步穿透，取终端最新值）
    bars = gw.market.bars("EURUSD", "M1", 100)   # 历史 K 线（旧→新）
    ticks = gw.market.ticks("EURUSD", 50)        # 窗口内最新 50 笔 tick
    st = gw.account.state()             # ③ 余额/净值/杠杆（持仓/挂单/历史见 gw.account 其他方法）
    res = gw.trading.market_order("EURUSD", "buy", 0.01)  # ④ 下单
    gw.trading.close_position(res.order_ticket)  # 平仓（部分平仓传 volume）
    gw.trading.modify_position(res.order_ticket, sl=1.08)  # 改持仓 SL/TP（只改一侧另一侧保持现值）
    gw.trading.cancel_order(8001)                # 撤挂单
```

从配置文件构造：`MT5Gateway.from_config_file("config.json")`
（键见 `config.example.json`，缺省即安全默认）。

## 使用约束（产品级）

- **终端必须保持运行并登录**：网关与终端同机通信，终端关了就没有数据。
- 终端掉线/重启后，网关在下次调用时自动重连一次；仍失败抛 `NotConnectedError`。
- 行情为同步按需拉取，不缓存不轮询；事件驱动（"新 tick 就推"）属策略段需求。
- MetaTrader5 包非线程安全：网关内部已串行化所有调用，上层可多线程使用。
  注意锁是进程内的：大块 K 线拉取期间同进程的其他调用会排队（下载器建议
  独立进程跑，本仓库的 CLI 即如此）。

## 实盘保险丝

**默认一切真实下单只允许发生在模拟盘。** 保险丝是白名单语义：只有模拟盘
（trade_mode=DEMO）免检放行；竞赛盘、实盘、任何未知值一律拒绝，除非显式解锁。
校验在连接层的同一次持锁内执行（取账户状态 → 保险丝 → 账号钉死 → order_send
原子完成），账户读不到时 fail-closed 拒单。解锁值必须是严格的 JSON 布尔
（字符串 "false" 会被当场拒绝，代码构造同样校验）。**解锁实盘必须同时配置
`expected_login` 钉死目标账号**——防止解锁后被切到别的实盘账户。

⚠️ MetaTrader5 官方包自带 `mt5.Buy()/Sell()/Close()` 之类快捷函数会**绕过本
网关**（无保险丝、无锁）——策略代码只允许 `import mt5gw`，不得直接 import
MetaTrader5。

解锁步骤（自担风险）：

1. 复制 `config.example.json` 为 `config.json`
2. 将 `"allow_live_trading"` 改为 `true`
3. 重启使用本网关的策略进程

## 时间戳约定

包返回的秒/毫秒时间戳统一转成**贴着 UTC 标签的 naive `datetime`**，
实际是经纪商服务器时间（与 UTC 可能差若干小时）。比较时间请用同一来源，
不要与本地时钟直接相减。传入时间窗参数时网关统一转整数秒，无时区偏移。

## 已知缺失与约定（诚实清单）

- **按星期的交易时段表**：MQL5 专有接口，Python 包取不到，本网关不提供、
  不伪造；`SymbolSpec.trade_mode` 可判断品种当前能否交易。
- `positions()/pending_orders()` 的 None 歧义按 `last_error` 裁决：成功码
  视为空集，否则抛错——查询失败绝不伪装成"空仓"。
- `ticks(count)` 取"最新报价前推 5 分钟"窗口内**最新**的 count 笔（窗口取整
  再裁尾）；需要精确区间时显式传 `since=`。
- 品种需加入 Market Watch 才能取行情，网关已自动处理（`symbol_select`）。
- **交易量取整**：下单量向品种步长向下取整（如 0.015 → 0.01）；取整后
  低于最小量或超过最大量一律**拒绝**（retcode 10014，绝不静默压量）。
- 市价单请求会附上下单瞬间的参考价（买 ask / 卖 bid）并尊重 `deviation`；
  部分券商（如 Exness）要求显式参考价。
- 成功码集合：`10009 DONE`、`10010 DONE_PARTIAL`（`partial=True`，仓位已建立）、
  `10008 PLACED`（`filled=False`：尚无成交回报，多见于挂单受理；成交价/成交号
  为空，调用方需自行核对）。`order_send` 无返回时抛 `OrderResultUnknownError`——
  订单状态未知，先核对持仓再决定是否重发，绝不当"被拒"处理。
  `OrderResult` 另带 `requested_volume` 与服务器回报 `volume`，部分成交时两者不等。
- `modify()` 改挂单、`modify_position()` 改持仓 SL/TP、`cancel_order()` 撤挂单：
  三者的未传字段一律**回填当前值**——MT5 语义里缺省即 0（等于删除止损），
  本网关绝不把"没传"当 0 发出。
- 历史委托/挂单里出现未知订单类型时如实标注为 `typeN`，不崩溃。

## K 线数据库（mt5data）

存储为 Parquet 文件，布局 `data/bars/{品种}/{周期}/data.parquet`（另带 `meta.json`
水位：行数、首末时间、更新时间）。列：`time, open, high, low, close,
tick_volume, spread, real_volume`；只存**已收盘**的 bar（末根 bar 收盘当且仅当
其收盘时刻 ≤ 服务器当前时间；服务器当前时间 = 墙钟 UTC + 由最新 tick 自校准
的券商时差，限幅 ±6h、超限回退 offset=0——注意这是**墙钟近似而非完全解耦**：
对 GMT 正偏移券商方向保守，极少数负偏移券商的极端情形可能晚一天收录 D1）。时间戳为服务器时间贴 UTC 标签的 naive datetime。

下载/刷新（终端须在线）：

```bash
.venv/Scripts/python.exe scripts/download_bars.py --symbols XAUUSD,BTCUSD,USTEC,EURUSD          # 全量回填
.venv/Scripts/python.exe scripts/download_bars.py --symbols XAUUSD,BTCUSD,USTEC,EURUSD --refresh  # 增量刷新（重叠证明接续）
```

数据保护：回填与既有文件**一律取并集**（不丢行）；新数据比现存短/晚起步时
拒绝写入并提示常见原因（终端"图表最大柱数"被重置）；`--force` 只放弃收缩
检查（仍并集）；彻底替换用 `--replace`（旧文件自动备份为
`data.parquet.bak-时间戳`）。跨进程写有按序列的文件锁；写入原子（临时文件 +
fsync + os.replace）。品种名保留大小写（`XAUUSDm` 等后缀品种可用）。

数据读取（T27/H3 物理隔离 + T38/H4 缺省切换）：**主库 `data/bars`、
`data/ticks` 仅限管理员与实盘侧**（含完整锁箱段，研究/审查账户拒读，
ACL 见 `docs/p6/permissions.md`）。研究侧读**研究库** `data_research/`
——管理员跑 `scripts/export_research_store.py` 导出的隔离副本（只含
`time < cutoff` 的 K 线 + 成本模型缓存文件；tick 不进研究库，成本经
`data_research/costs/spread_model.json` 提供）。**`run_backtest.py` 与
`bt.py` 的 `--data-root` 缺省即 `data_research`**（T38/D24；
`--research-store` 为 T27 兼容别名，通常不用）；缺省时研究库不存在 →
报错指路 `export_research_store`，不回退主库；读主库须显式 `--main-store`，
读主库的锁箱期（`--end` 越过 cutoff）另需 `--ops-ack`（运维测量专用，
bt 台账记 actor=ops）：

```python
import duckdb, pandas as pd
df = pd.read_parquet("data_research/bars/XAUUSD/H1/data.parquet")      # 研究侧
con = duckdb.connect()
df = con.execute("SELECT * FROM read_parquet('data_research/bars/*/*/data.parquet', filename=true) WHERE ...").fetchdf()
```

历史深度：终端"图表最大柱数"已从默认 10 万调大到 1 亿
（`%APPDATA%\MetaQuotes\Terminal\<实例>\config\common.ini` 的 `[Charts] MaxBars`，
UTF-16 编码，改前需先关终端再改再启动）。当前各序列已取到 Exness 服务器
留存的最深历史：XAUUSD 全周期回到 2014-01；BTCUSD/USTEC/EURUSD 回到
2021 年中（券商上线/留存的起点）。M1 级全量约 2,820 万根、786MB。
注：新品种首次深挖 M1 时，第一笔大请求可能因终端向服务器同步历史而超时
返回"无数据"，等一两分钟重跑同序列即可（缓存已暖，秒级完成）。

## tick 数据库（mt5data.ticks）

布局 `data/ticks/{品种}/data.parquet`（`meta.json` 水位：行数、首末
`time_msc`、更新时间、数据来源）。列：`time_msc`（int64 毫秒）、`bid/ask/last/volume`
（float64）、`flags`（int64）。回补从目标时刻**往回**分块取（连续 3 个空窗
视为历史尽头正常收尾）；增量刷新从旧水位含边界取到当前，**抓回页必须包含
旧末根 tick（接续证明）**，否则报错拒写。原子写与跨进程锁与 K 线库同一套。

```bash
.venv/Scripts/python.exe scripts/download_ticks.py --symbols XAUUSD --mode backfill --start 2026-10-10
.venv/Scripts/python.exe scripts/download_ticks.py --symbols XAUUSD --mode refresh
```

## 数据质检与重采样（mt5data.quality / resample / sessions）

- `scripts/quality_report.py --symbols ... --timeframes M1,H1`：逐序列完整性
  报告（`data/reports/quality/`）——覆盖率、缺口分类（周末/每日休市/节假日/
  未预期，夏令时用 zoneinfo 按交易所本地时间判定）、重复、乱序、尖刺、
  点差异常。
- `resample_bars(df_m1, "H1")`：label=closed=left 约定，spread 取 bar 内最小
  （MQL5 语义）；`compare_with_broker(res, broker)` 与经纪商原生周期逐根比对
  （价格差异判失败、成交量差异只记录）。

## 锁箱（mt5data.lockbox）

研究侧只能读到 `time < cutoff` 的数据（当前全部序列按"历史>5 年→锁最近
12 个月"规则锁定，见 `data/lockbox.json`）：`research_bars()` 显式请求越过
cutoff 直接报错（绝不静默截断）；MCP `get_bars` 同一约束（越界报错、缺省
时间钳到 cutoff 并在响应中带 `lockbox_cutoff` 字段）。实盘/信号侧不受限
（直读 `store.load_bars`）。`scripts/export_lockbox.py` 把锁定段导出为独立
目录的物理副本（供 G5 锁箱测试与实盘侧读取）；`scripts/export_research_store.py`
（T27）反向导出**研究库** `data_research/`（开放段 + 成本缓存，导出后自检
任何序列 `last_time < cutoff`），研究/审查账户只能读研究库。每个锁箱另有
**使用预算**（D16 待定，缺省 5 次 G5；用尽即拒，台账 `lockbox_budget` 表）。

## 研究层（mt5quant：成本模型 / 策略 / A 层回测）

- **成本（P2）**：`mt5quant.costs`——tick 实测点差（按 UTC 小时的中位/p90/p99）、
  滑点（比例×p90 点差＋固定，事件小时加倍）、佣金（每手来回）、隔夜费
  （多空分开、三倍日、恒为成本方向）；`scripts/cost_audit.py` 出热力图与
  纳入/排除清单（`data/reports/costs/`）。
- **策略（P3 接口）**：`mt5quant.strategies`——统一接口 `params_schema /
  required_data / generate_signals / exit_rules`；参数 ≤4 个、网格 ≤100 组
  在类定义时强制；只做内存计算，不做 I/O；回测与实盘共用同一份代码。
- **A 层回测（P3）**：`mt5quant.backtest`——信号 bar 收盘、下一根开盘成交；
  bid 基准（多头 Ask 入场）；同 bar 双触止损优先；成本按小时模型叠加；
  `check_no_lookahead` 截断测试查前视偏差；报告带 manifest（git 提交号、
  数据快照哈希、参数、成本模型版本、引擎版本、数据区间、合约大小）。
- **金额口径（引擎 A-1.1，2026-10-25）**：`run_backtest(..., contract_size=)`
  为必填参数——盈亏 = 价格差 × 手数 × 合约大小（账户货币），佣金/隔夜费按
  手数计；SL 被跳空穿过按开盘价成交（`stop_gap`）；空头 SL/TP 按 Ask 触发
  （与 MT5 一致）。CLI 的合约大小取 `--contract-size`，否则读
  `data/specs/latest.json`（先跑 `scripts/snapshot_specs.py`），都没有就报错。
- 每次 CLI 运行写入独占目录 `data/reports/backtests/{品种}_{周期}_{UTC戳}_{随机}/`，
  历史运行不会被覆盖（P4 试验台账的前提）。

```bash
.venv/Scripts/python.exe scripts/snapshot_specs.py      # 首次：写 data/specs/latest.json（含合约大小）
.venv/Scripts/python.exe scripts/run_backtest.py --strategy mt5quant.strategies.library.buy_hold:BuyHold \
    --symbol XAUUSD --timeframe H1 --params '{}' --end 2025-09-01 --commission 0
```

研究侧缺省即读 `data_research`（T38 起无需 flag；`--research-store` 为
兼容别名）；主库 `data/bars` 仅管理员与实盘侧可读（CLI 侧读主库须显式
`--main-store`，锁箱期另需 `--ops-ack`）。

审核报告与后续计划：`docs/reviews/`（各轮审核）、`docs/blueprint.md`（蓝图与现状）、
`docs/superpowers/plans/2026-10-26-round5-hardening.md`（当前施工计划；上一轮为 `2026-10-26-round4-optimization.md`）。

> **当前禁止事项**（第五轮审核，详见蓝图 v3.1 §2）：
> - 计划 T30–T34 完成、影子盘跑满 1 个交易日之前，不以非干跑模式（含模拟盘）启动 `scripts/live_runner.py`。原因：止损出场后对账会永久暂停全部策略，按风险定手数会逐 bar 调仓，第二次全平指令会被吞掉。
> - T37、T39 完成之前不跑 G5。
> - T35–T38 完成之前不开新研究卡。

> **实盘口径备注**：日亏上限按 **UTC 自然日**（00:00 UTC 换日）计算，与交易日
> （纽约 17:00 换日）不同——刻意选择：口径简单，且与回测年化口径一致
> （`src/mt5live/riskproc.py::daily_pnl`；第五轮审核 L2）。

## 每日调度

`scripts/setup_scheduled_tasks.ps1`（安装：右键"使用 PowerShell 运行"或
`powershell -File`；删除命令在其头注释）：每日 00:05 品种参数快照比对、
00:10 K 线增量刷新（`scripts/refresh_all.py`，日志在 `data/logs/`）、
00:15 tick 增量刷新（有 tick 库才装）。`scripts/measure_server_clock.py`
可随时补测服务器时钟偏移（证据存 `data/server_clock.json`，供夏令时
切换前后对比）。

## MCP 数据服务（mt5mcp，给远程 agent 用）

本机建模**直读研究库 Parquet/DuckDB（`data_research`，不要走 MCP）**——
主库 `data/bars`、`data/ticks` 仅管理员与实盘侧可读；MCP 是给远程服务器上的 agent
开的一扇有界的门：四个工具 `list_series` / `get_bars`（≤500 根，DuckDB 下推
过滤，不整文件载入）/ `describe_series`（含最大相邻间隔）/ `compute_indicator`
（服务端现算 sma/ema/rsi/atr/macd/bbands，只读尾部 limit+预热期，并发受信号量
限制）。指标不落库，参数随便改。品种名经白名单校验，杜绝路径穿越。指标口径：
RSI/ATR 为 Wilder 平滑（种子=前 period 根均值，种子期 NaN）；Bollinger 用总体
标准差；MACD 信号线为 EMA（MT5 内置用 SMA，注意口径差异）。

```bash
.venv/Scripts/python.exe scripts/run_mcp_server.py --init     # 生成 mcp.json（随机令牌，默认只听 127.0.0.1:8310）
.venv/Scripts/python.exe scripts/run_mcp_server.py            # HTTP 模式（Bearer 令牌鉴权）
.venv/Scripts/python.exe scripts/run_mcp_server.py --stdio    # 本机 agent 用 stdio
```

**远程访问只经隧道，见 `docs/ops/mcp-remote-access.md`**（SSH 本地端口转发 /
Tailscale / WireGuard；远程 agent 连 `http://127.0.0.1:<端口>/mcp`）——令牌明文走
HTTP 会被链路窃听重放。配置已 fail-closed：`host` 非回环地址且未显式写
`"allow_plaintext_public": true` 时启动直接报错。令牌 ≥32 字符且要轮换
（曾明文传输过就换）。数据不会自动更新——
远程 agent 看到的是库的最近一次 `refresh_all.py` / `download_bars.py --refresh`。
锁箱对 MCP 同样生效：远程（研究侧）只能读到 `lockbox.json` 的 cutoff 之前。
