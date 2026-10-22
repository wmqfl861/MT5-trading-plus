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
校验在连接层的同一次持锁内执行（取账户状态 → 保险丝 → order_send 原子完成），
账户读不到时 fail-closed 拒单。解锁值必须是严格的 JSON 布尔（字符串 "false"
会被当场拒绝，代码构造同样校验）。

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
- `positions()/pending_orders()` 返回空列表时，无法区分"没有持仓/挂单"与
  "查询失败"——这是包的 None 歧义，本层按空集处理并记 debug 日志。
- `ticks(count)` 取"最新报价前推 5 分钟"窗口内**最新**的 count 笔（窗口取整
  再裁尾）；需要精确区间时显式传 `since=`。
- 品种需加入 Market Watch 才能取行情，网关已自动处理（`symbol_select`）。
- **交易量自动取整**：下单量会向品种步长向下取整并夹在最小/最大量之间
  （如 0.015 → 0.01）；取整后低于最小量则拒绝（retcode 10014）。
- 市价单请求会附上下单瞬间的参考价（买 ask / 卖 bid）并尊重 `deviation`；
  部分券商（如 Exness）要求显式参考价。
- 成功码集合：`10009 DONE`、`10010 DONE_PARTIAL`（部分成交，仓位已建立）、
  `10008 PLACED`（已受理，`OrderResult.confirmed=False`，成交价/成交号为空，
  调用方需自行核对）。`order_send` 无返回时抛 `OrderResultUnknownError`——
  订单状态未知，先核对持仓再决定是否重发，绝不当"被拒"处理。
- `modify()` 改挂单、`modify_position()` 改持仓 SL/TP、`cancel_order()` 撤挂单：
  三者的未传字段一律**回填当前值**——MT5 语义里缺省即 0（等于删除止损），
  本网关绝不把"没传"当 0 发出。
- 历史委托/挂单里出现未知订单类型时如实标注为 `typeN`，不崩溃。
- `positions()/pending_orders()` 的 None 歧义按 `last_error` 裁决：成功码视为
  空集，否则抛错——查询失败绝不伪装成"空仓"。

## K 线数据库（mt5data）

存储为 Parquet 文件，布局 `data/bars/{品种}/{周期}/data.parquet`（另带 `meta.json`
水位：行数、首末时间、更新时间）。列：`time, open, high, low, close,
tick_volume, spread, real_volume`；只存**已收盘**的 bar（末根 bar 收盘当且仅当
其收盘时刻 ≤ 服务器当前时间；服务器当前时间用"墙钟 UTC + 券商时差"自校准
估算，与 tick 时钟解耦，周末行情冻结也不误删/误留；无任何宽容秒数）。时间戳为服务器时间贴 UTC 标签的 naive datetime。

下载/刷新（终端须在线）：

```bash
.venv/Scripts/python.exe scripts/download_bars.py --symbols XAUUSD,BTCUSD,USTEC,EURUSD          # 全量回填
.venv/Scripts/python.exe scripts/download_bars.py --symbols XAUUSD,BTCUSD,USTEC,EURUSD --refresh  # 增量刷新
```

本机建模直接读文件（不要走 MCP）：

```python
import duckdb, pandas as pd
df = pd.read_parquet("data/bars/XAUUSD/H1/data.parquet")            # 全速 pandas
con = duckdb.connect()
df = con.execute("SELECT * FROM read_parquet('data/bars/*/*/data.parquet', filename=true) WHERE ...").fetchdf()
```

历史深度：终端"图表最大柱数"已从默认 10 万调大到 1 亿
（`%APPDATA%\MetaQuotes\Terminal\<实例>\config\common.ini` 的 `[Charts] MaxBars`，
UTF-16 编码，改前需先关终端再改再启动）。当前各序列已取到 Exness 服务器
留存的最深历史：XAUUSD 全周期回到 2014-01；BTCUSD/USTEC/EURUSD 回到
2021 年中（券商上线/留存的起点）。M1 级全量约 2,820 万根、786MB。
注：新品种首次深挖 M1 时，第一笔大请求可能因终端向服务器同步历史而超时
返回"无数据"，等一两分钟重跑同序列即可（缓存已暖，秒级完成）。

## MCP 数据服务（mt5mcp，给远程 agent 用）

本机建模**直读 Parquet/DuckDB，不要走 MCP**；MCP 是给远程服务器上的 agent
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

**跨公网必须加 TLS**（反向代理或 SSH/WireGuard 隧道）——令牌明文走 HTTP 会被
链路窃听重放；`host` 改 `"0.0.0.0"` + 防火墙放行端口；令牌 ≥32 字符且要轮换
（曾明文传输过就换）。端点为 `http://<主机>:<端口>/mcp`。数据不会自动更新——
远程 agent 看到的是库的最近一次 `download_bars.py --refresh`。
