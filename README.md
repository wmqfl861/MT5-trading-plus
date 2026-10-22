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
    ticks = gw.market.ticks("EURUSD", 50)        # 最新窗口内的 tick
    st = gw.account.state()             # ③ 余额/净值/杠杆/持仓/挂单/历史成交
    res = gw.trading.market_order("EURUSD", "buy", 0.01)  # ④ 下单
    gw.trading.close_position(res.order_ticket)  # 平仓
```

从配置文件构造：`MT5Gateway.from_config_file("config.json")`
（键见 `config.example.json`，缺省即安全默认）。

## 使用约束（产品级）

- **终端必须保持运行并登录**：网关与终端同机通信，终端关了就没有数据。
- 终端掉线/重启后，网关在下次调用时自动重连一次；仍失败抛 `NotConnectedError`。
- 行情为同步按需拉取，不缓存不轮询；事件驱动（"新 tick 就推"）属策略段需求。
- MetaTrader5 包非线程安全：网关内部已串行化所有调用，上层可多线程使用。

## 实盘保险丝

**默认一切真实下单只允许发生在模拟盘。** 账户为实盘（trade_mode=REAL）时，
所有交易方法在触达下单接口之前直接抛 `LiveTradingBlockedError`。

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
- `ticks(count)` 缺省锚定"最新报价时间前推 5 分钟"窗口；冷门品种窗口内
  tick 可能少于请求数。需要精确区间时显式传 `since=`。
- 品种需加入 Market Watch 才能取行情，网关已自动处理（`symbol_select`）。
- **交易量自动取整**：下单量会向品种步长向下取整并夹在最小/最大量之间
  （如 0.015 → 0.01）；取整后低于最小量则拒绝（retcode 10014）。
- 市价单请求会附上下单瞬间的参考价（买 ask / 卖 bid）并尊重 `deviation`；
  部分券商（如 Exness）要求显式参考价。
- 挂单受理码 `10008 PLACED` 与完成码 `10009 DONE` 都视为成功。
- `modify()` 只改挂单；持仓的 SL/TP 用 `modify_position()`（纯透传，
  移动止损等由策略层驱动）。
- 历史委托/挂单里出现未知订单类型时如实标注为 `typeN`，不崩溃。

## K 线数据库（mt5data）

存储为 Parquet 文件，布局 `data/bars/{品种}/{周期}/data.parquet`（另带 `meta.json`
水位：行数、首末时间、更新时间）。列：`time, open, high, low, close,
tick_volume, spread, real_volume`；只存**已收盘**的 bar（形成中的最后一根
不入库，避免建模"偷看未来"）。时间戳为服务器时间贴 UTC 标签的 naive datetime。

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

已知上限：MT5 终端"图表最大柱数"（默认 10 万）限制了单次可见历史——M1~M30
各周期最多取回约 10 万根（M1 ≈ 最近 10 周；H1 及以上通常已到券商完整历史）。
要更深的小周期历史，需在终端 工具→选项→图表 把"图表最大柱数"调大
（或设为无限制）并重启终端后重跑全量回填。
