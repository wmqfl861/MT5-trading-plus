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
