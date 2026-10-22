## 总评

mt5gw 的分层清晰（连接 / 领域服务 / 门面）。项目的几条铁律在主干上基本落实了：保险丝挂在所有公开交易入口的最前面；对 MT5 包的直接调用只发生在锁内；读路径拿到 None 会带上 last_error 抛错。mt5mcp 与交易栈做了进程级隔离，返回条数在服务端硬性封顶，鉴权中间件也没找到可绕过的地方。

但有 3 个必须修的问题：
- `modify_position` / `modify` 会把没传的 SL/TP 当作 0 发出去，静默删除止损。
- 冒烟脚本的"仅模拟盘"硬闸在账户查询失败时会被跳过（fail-open）。
- 回填会吞掉中途的错误，用截断的数据覆盖已有的完整历史，还报告成功。

另外还有一批应当修的缺口：保险丝的类型健壮性与原子性、成交结果的语义、K 线管道吞错并破坏"只存已收盘 bar"的承诺、MCP 入参未校验且令牌明文走公网。测试数量不少，但大多是对宽松的 FakeMT5 断言请求字典，恰好没覆盖上面这些最危险的语义。

说明：本环境不允许执行 Python，所以测试套件没有跑过，下面的结论全部来自静态审读，并对照了 venv 里 pandas 和 MCP SDK 的源码。凡是推断出来、没在真机上验证的地方，都在对应条目里注明了。

## 优点（具体到 file:line）

1. **保险丝覆盖全部 5 个公开交易入口，且都在构造请求之前执行**：`src/mt5gw/trading.py:77`、`:113`、`:141`、`:161`、`:172`。`state()` 取不到账户时会抛错（`src/mt5gw/account.py:89-91`），所以保险丝是 fail-closed 的。`tests/test_trading.py:92-110` 用"order_send 调用次数为 0"来断言请求确实没被发出。
2. **JSON 配置做了类型硬校验**：会拦下字符串 `"false"`、把 bool 当 magic 等写法，见 `src/mt5gw/config.py:44-56`，测试在 `tests/test_config.py:48-59`。
3. **对 MT5 包的直接调用全部集中在 connection.py，并且都在 RLock 内**：涉及 connect、shutdown、last_error、terminal_info、initialize，见 `src/mt5gw/connection.py:21-29`、`:35-38`、`:52-55`。grep 确认 src/ 和 scripts/ 里没有别的直接调用；`tests/test_connection.py:63-103` 用 max_concurrent==1 验证了串行化。
4. **读路径拿到 None 一律抛错并附带 last_error**：`src/mt5gw/symbols.py:39-48`，`src/mt5gw/market.py:38-39`、`:56-59`、`:86-89`。
5. **时间统一按 UTC 换算，传给 MT5 的是整数秒**：`src/mt5gw/models.py:13-20`、`src/mt5gw/account.py:117-120`。这避开了 MT5 包"按本机时区解释 datetime"的坑。测试断言的是精确的 epoch 整数（`tests/test_symbols_market.py:138-152`、`tests/test_account.py:114-128`）。
6. **几处细节处理得当**：
   - 未知的订单/成交类型如实标成 `typeN`（`src/mt5gw/account.py:21-32`、`:64`）。
   - 部分平仓前先校验持仓余量（`src/mt5gw/trading.py:181-185`）。
   - 成交方式优先选 FOK，从源头避免部分成交（`src/mt5gw/trading.py:29-35`）。
7. **mt5data 的收盘与去重逻辑有可取之处**：
   - MN1 按 31 天判定收盘，偏保守，绝不会提前放行未收盘的月线（`src/mt5data/store.py:14-21`）。
   - 按位置分页时，新 bar 到来只会造成重叠，不会漏根；再由 merge 按 time 去重并保留新值（`src/mt5data/downloader.py:38-51`、`src/mt5data/store.py:32-45`）。
8. **指标的基础定义正确**：SMA 没问题；EMA 用 α=2/(n+1)、以首值为种子，和 MT5 内置 EMA 的初始化方式一致；TR 的定义（包括第一根取 H−L）正确（`src/mt5mcp/indicators.py:7-12`、`:41-45`）。
9. **mt5mcp 与交易栈隔离，返回条数封顶**：它只 import 存储层和指标模块（`src/mt5mcp/tools.py:13-14`），进程里根本碰不到交易栈；返回条数在服务端硬夹在 [1,500]（`:59`、`:114`），调用方突破不了。
10. **鉴权中间件不可绕过**：
    - 它包住整个 ASGI 应用，不按路径或方法放行，HEAD、OPTIONS、`/MCP`、`/mcp/` 全都要令牌。
    - 令牌比对是常量时间的，空令牌会被拒绝（`src/mt5mcp/auth.py:7-10`、`:22-41`）。
    - SDK 的 `/mcp` 路由只接受 HTTP（`.venv/Lib/site-packages/mcp/server/lowlevel/server.py:827-832`），websocket 连接进不来。
    - 默认只监听 127.0.0.1，令牌至少 32 个字符（`src/mt5mcp/config.py:28-33`、`:46`）。
    - 9eccfc8 曾把 mcp.json 误提交进库；经哈希比对，现在用的令牌已经换过、不在 git 历史里，仓库也没有配置 remote。

## 问题清单——按严重度分级

### Critical（必须修）

**C1. 改持仓/改挂单会把未传的 SL/TP 当作 0 发出，静默删除止损或止盈**
- **位置**：`src/mt5gw/trading.py:159-167`（modify_position）、`:141-149`（modify）
- **问题**：签名是 `sl=None, tp=None`，`:159-160` 还明确允许只给其中一个，但请求里只放了非 None 的字段。MT5 交易请求里没给的字段按 0 处理，而 `TRADE_ACTION_SLTP` 的 sl/tp 是绝对值，0 就表示"没有止损/止盈"。后果如下：
  - `modify_position(t, tp=x)` 会删掉现有的 SL；`modify_position(t, sl=x)` 会删掉现有的 TP。
  - `modify()` 也一样：不传 price 就是 price=0，会被拒；不传 sl/tp 就会被清空；type_time 和 expiration 会被重置成 GTC 和 0。
  - MQL5 官方示例里这两种操作都带了 `symbol`，本实现没带；会不会因此被拒，没有在真机上验证。
- **为什么重要**：铁律是"SL/TP 只透传，绝不自动添加"，这里却变成了"自动删除"。策略只是想移一下止盈，持仓就丢了止损，敞口没有上限，而且全程没有任何报错。测试（`tests/test_trading.py:250-260`、`:295-303`）只覆盖了 sl/tp 都传的情况，冒烟脚本也没测改单，这个语义从来没在真机上验证过。
- **怎么修**：
  - 先用 `positions_get(ticket=)` / `orders_get(ticket=)` 读出当前值，把 None 补成现值，真正做到"None 表示不变"；同时带上 symbol；读不到就抛错。
  - 让 FakeMT5 按"未给字段 = 0"的语义更新内存里的持仓，再补一条"只改一边、另一边保持不变"的测试。
  - 在模拟盘冒烟里加一次 modify_position。

**C2. 冒烟脚本的"仅模拟盘"硬闸会被跳过，存在在实盘下单的路径**
- **位置**：`scripts/smoke.py:83`（`if st is not None and st.trade_mode != 0`）、`:38-42`、`:92-96`
- **问题**：
  1. 第③步 `account.state()` 只要抛了 GatewayError，`st` 就是 None，整个硬闸被跳过，直接进入第④步下单。
  2. 脚本默认读 `config.json`，而 README:46-50 正是让用户在这个文件里把 `allow_live_trading` 设成 true。如果用户已经这样解锁、终端又登录着实盘，第④步里库的 `_fuse()` 只要重查账户成功（比如瞬断已恢复），就会放行实盘单。
  3. 如果③和④之间终端切换了账户，结果也一样。
  4. 如果第②步 `quote()` 失败，`quote` 为 None，第④步照样下单；接着 `:95` 的 `quote.ask` 抛出 AttributeError，它不在 `except GatewayError` 的捕获范围内，脚本崩溃，刚开的仓没人平。
- **为什么重要**：设计文档的反目标写明，"在实盘账户上执行任何测试单"就算失败。所以"冒烟脚本是否绝无可能在实盘下单"的答案是：**否**。
- **怎么修**：
  - 冒烟脚本强制用 `dataclasses.replace(load(path), allow_live_trading=False)`，不管配置文件里写了什么。
  - 硬闸改成 `if st is None or st.trade_mode != 0: return 1`。
  - 第④步下单前再查一次 trade_mode 和 login，必须和第③步一致。
  - 没拿到 spec 或 quote 就不下单；下单之后用 try/finally 保证一定尝试平仓。

**C3. 回填吞掉中途错误，用截断的数据覆盖已有的完整历史，还报告成功**
- **位置**：`src/mt5data/downloader.py:39-42`（`except DataUnavailableError: break`）、`:56`；`scripts/download_bars.py:50-55`
- **问题**：
  - 回填不读旧文件。第 k 块返回 None 就直接 break，然后把前 k−1 块覆盖写进 data.parquet。终端向服务器同步更早的历史时容易超时，所以最容易出问题的恰恰是后面几块。
  - 原有的深历史会被冲掉，例如 XAUUSD M1 从 2014 年起的 3,344,926 根。
  - CLI 照样打印 ✓ 并以 0 退出，last_error 也丢了；如果第一块就失败，则打印"· 无数据"，同样以 0 退出。
  - README:104-105 把"超时了就重跑回填"写成推荐做法，而重跑正是会触发覆盖的那条路。
- **为什么重要**：回测用的底库会被静默截断，研究结论跟着出错，而且没有人会知道。
- **怎么修**：
  - 区分"取尽"和"失败"：第一块就失败、或者 last_error 不是"没有更多数据"时，向上抛错。
  - 回填时与旧文件合并；如果结果的行数比现有少、或起始时间比现有晚，就拒绝写入，除非显式传 `--force`。
  - rows==0 要算作失败。
  - 配合 I8 改成原子写。

### Important（应当修）

**I1. 用代码构造配置时，类型混淆能打开实盘**
- **位置**：`src/mt5gw/config.py:27-31`（没有 `__post_init__` 校验）、`:63`（`not allow_live` 是按真假值判断的）
- **问题**：`GatewayConfig(allow_live_trading="false")`、`dataclasses.replace(cfg, allow_live_trading=os.getenv(...))` 都会解锁实盘，`ensure_trade_allowed(2, "false")` 也不会抛错。JSON 那条路堵上了，代码这条路还开着。
- **为什么重要**：从环境变量或命令行读开关是很常见的写法，保险丝本该对这类错误免疫。
- **怎么修**：在 `__post_init__` 里做和 `load()` 一样的类型校验；保险丝改成只有 `allow_live is True` 才放行。

**I2. 保险丝不在唯一出口、与 order_send 不是原子的，而且用的是黑名单**
- **位置**：`src/mt5gw/trading.py:211-214`、`:224-227`；`src/mt5gw/connection.py:37-38`
- **问题**：
  - (a) `_send()` 本身不查保险丝。现在 5 个入口都调了 `_fuse()`，但以后新加交易方法时只要漏调一次，就绕过去了。
  - (b) `_fuse()` 和 `order_send` 分属两次加锁，中间还夹着 symbol_info、tick 等调用。如果这期间有人在终端界面上切换了账户，单子会发到新账户；而 `call()` 在这一刻其实会发现 connected=False、重新 initialize，却不会再检查一次保险丝。
  - (c) 只拦 REAL（值为 2）；CONTEST 和任何意外的值都放行，而设计文档原文是"非模拟盘一律拒绝"。
- **为什么重要**：铁律要求保险丝在触达 order_send 之前、不可绕过地生效。
- **怎么修**：把校验挪进 `_send`，在同一次持锁内依次完成"重新取 account_info 检查 trade_mode（可选：钉死预期的 login）→ order_send"；改成白名单：`trade_mode == DEMO or allow_live is True`。

**I3. 成交结果的语义不诚实**
- **位置**：`src/mt5gw/trading.py:19`、`:226-229`；`src/mt5gw/models.py:147`
- **问题**：
  - 如果券商对 IOC 品种的部分成交回报 10010 DONE_PARTIAL，此时仓位已经部分建立，代码却抛出 `TradeRejectedError("未知返回码 10010")`。
  - `order_send` 返回 None（例如 IPC 中断）时，订单实际状态是未知的，却被说成"被拒"。
  - 市价单收到 10008 时返回 ok=True，但 deal_ticket=0，成交价可能也是 0。
  - models.py:147 的注释还写着 ok 等于 retcode==10009，与代码不符。
- **为什么重要**：策略以为没成交而重下一单，敞口就翻倍了；或者把 0 当成成交价。
- **怎么修**：
  - DONE_PARTIAL 作为成功返回，同时带上请求量和实际成交量。
  - order_send 返回 None 时抛一个专门的"结果未知"异常，提示调用方去核对持仓。
  - 市价单收到 PLACED 时单独标记为"成交未确认"。

**I4. 不能撤挂单（缺少 TRADE_ACTION_REMOVE）**
- **位置**：整个 `src/mt5gw/trading.py`；`tests/fakes.py:59` 定义了 REMOVE 常量，但没有任何地方用到
- **问题**：网关能挂单，却不能撤单。
- **为什么重要**：上层要撤单只能直接调用 mt5，锁和保险丝都会一起失效。
- **怎么修**：补一个 `cancel_order(ticket)`，同样走保险丝和 `_send`。

**I5. 把 None 当空列表吞掉，还误导平仓路径**
- **位置**：`src/mt5gw/account.py:121-122`、`:130-131`（deals、order_history）；`:100-102`、`:111-113`（positions、pending_orders）；`src/mt5gw/trading.py:173-175`
- **问题**：
  - deals 和 order_history 在拿到 None 时直接返回空列表，连日志都没打，README 的诚实清单里也没写。
  - 更要命的是平仓路径：`positions_get` 查询失败时，`close_position` 报的是"持仓不存在（可能已平仓）"。
- **为什么重要**：策略可能因此停止管理一笔其实还在的持仓。
- **怎么修**：
  - 拿到 None 时立刻读 last_error：真实包的成功码是 (1,'Success')，这种情况视为空集；其他情况抛 DataUnavailableError。这样从根上消除歧义，而不只是写进文档。
  - close_position 直接用 `positions_get(ticket=)` 查单个持仓。

**I6. 增量刷新会吞错，还会静默留下空洞**
- **位置**：`src/mt5data/downloader.py:71-74`、`:66`（lookback 固定为 5000）
- **问题**：
  - 刷新失败时照样保存文件，打印"✓ 新增=0"，还会更新 meta 里的 updated_at。
  - 如果距离上次刷新超过 5000 根 bar，中间就直接缺一段，而且没有任何提示。对 M1 来说 5000 根约 3.47 天，BTCUSD 是 7×24 交易，从周四停到周一就会出问题。
  - describe_series 统计的缺失根数被周末缺口淹没，也看不出这个空洞。
  - 旧文件不存在时，`--refresh` 会静默建出一个只有 5000 根的文件。
- **为什么重要**：README 推荐把 `--refresh` 当定时任务跑，空洞会悄无声息地进入回测。
- **怎么修**：根据水位算出需要的根数，或者按位置往回翻页，直到和旧文件的最后一根重叠；接不上就报错，或者转成回填；错误要向上抛。

**I7. "只存已收盘 bar"的承诺被 5 秒宽容打破**
- **位置**：`src/mt5data/store.py:25`、`:58`；`src/mt5data/downloader.py:26-28`
- **问题**：
  - 代码把"现在"取成最后一笔 tick 的时间。行情活跃时，只要最后一笔 tick 落在当前 bar 的最后 5 秒内，还没收盘的 bar 就会入库。对 M1 来说，大约 5/60≈8% 的刷新会碰上；在下一次刷新之前，MCP 和回测看到的都是半根 bar。
  - 这 5 秒宽容本意是周末收盘后保住最后一根 bar，但只有当最后一笔 tick 恰好落在收盘前 5 秒内时才起作用；注释里"收盘时刻等于最后 tick 时刻"这个前提并不成立。
  - 测试里伪造的 bar 时间没对齐整分钟（`tests/test_mt5data_downloader.py:54`，秒数固定是 30），所以这个问题永远测不出来。
- **为什么重要**：直接违反了 README:80-81 的承诺。
- **怎么修**：
  - 去掉这 5 秒宽容。
  - 判定规则改为：后面已经有 bar 的，一定已收盘；最后一根要求 bar 的收盘时间不晚于服务器当前时间。服务器当前时间用"墙钟 UTC + 券商时差（做成配置项）"来估算，而不是用最后一笔 tick 的时间。
  - 只对新拉到的数据做剔除，不要动已经入库的旧数据。

**I8. Parquet 文件和水位文件不是原子写入**
- **位置**：`src/mt5data/store.py:71`、`:80-81`
- **问题**：直接覆盖 data.parquet（XAUUSD M1 有 91MB）和 meta.json。写到一半崩溃，这唯一的一份数据就毁了；MCP 同时在读的话会读到半截文件；两个文件之间也可能对不上。
- **为什么重要**：这是整个 K 线库仅有的一份数据，没有备份。
- **怎么修**：先写到临时文件，再用 `os.replace` 替换（Windows 上如果有读者占着文件句柄，需要重试）；meta.json 最后写，或者干脆把水位写进 parquet 的元数据里。

**I9. `ticks()` 默认返回的是窗口里最早的 N 笔，而不是最新的 N 笔**
- **位置**：`src/mt5gw/market.py:78-85`
- **问题**：`copy_ticks_from` 从锚点（最新报价时间往前推 5 分钟）开始往后取 count 笔。流动性好的品种 5 分钟里有成百上千笔，取 50 笔拿到的是大约 5 分钟前的数据，`ticks[-1]` 并不是最新的一笔。冒烟脚本（`scripts/smoke.py:64-66`）和 README:25 都把它当成了最新数据。
- **为什么重要**：策略会把 5 分钟前的 tick 当成当前行情。
- **怎么修**：用 `copy_ticks_range(锚点, 最新时间+1秒)` 把窗口内的 tick 全取出来，再取最后 count 笔。

**I10. 掉线检测名不副实**
- **位置**：`src/mt5gw/connection.py:37-38`、`:57-62`
- **问题**：
  - `initialize` 之后不会再检查一次 `terminal_info().connected`，无论连没连上都会继续执行调用。`tests/test_connection.py:25-33` 甚至把"重连后仍是 connected=False 却照常放行"写成了预期行为。
  - 按 MT5 包的语义推断（没在真机上验证）：券商断线、但本机与终端的 IPC 正常时，initialize 仍然返回 True，于是读到的是终端缓存里的旧报价和旧 K 线，`NotConnectedError` 永远不会抛出。
  - 这与 README:37 以及施工计划 Task4 的承诺不符。
- **为什么重要**：交易和行情都可能在毫无提示的情况下用上陈旧数据。
- **怎么修**：重连后再查一次 connected；如果仍为 False，交易路径直接抛错，读路径把"数据可能陈旧"的标记交给调用方。

**I11. 失败调用与读取 last_error 不是原子的（锁覆盖的遗漏）**
- **位置**：`src/mt5gw/connection.py:52-55`，以及各处"`call(...)` 返回 None 后再调 `last_error()`"的地方
- **问题**：connect、shutdown、last_error 各自都加了锁，这一点是对的。但"失败的那次调用"和"读取 last_error"分在两次加锁里，中间其他线程的调用可能已经把错误码覆盖掉了，于是异常信息会出现"K线不可用：(1,'Success')"这类张冠李戴的内容。
- **为什么重要**：README 声明上层可以多线程使用，而多线程下错误信息会对不上号，违背了"错误如实上抛"的原则。
- **怎么修**：新增一个 `call_checked`，在同一次持锁内完成调用；结果为 None 时当场读取 last_error 并抛出。

**I12. MCP 入参没有校验：可以路径穿越，也可以注入 UNC 路径**
- **位置**：`src/mt5mcp/tools.py:23-30` → `src/mt5data/store.py:28-29`
- **问题**：`symbol` 参数被原样拼进文件路径。
  - 传 `..\..\` 可以探测或读取任意位置的 `<周期>\data.parquet`。
  - 在 Windows 上，`os.path.join` 遇到 `\\主机\共享` 这样的 UNC 路径，会丢弃前面的部分、从这里重新开始拼；随后的 `Path.exists()` 会向外部主机发起 SMB 连接，可能泄露服务进程所用账户（从路径看是 Administrator）的 NTLM 哈希。
  - 任何持有令牌的一方都能触发，包括被提示注入的远程 agent。
- **为什么重要**：MCP 服务对公网开放，这条路径能把本机凭据泄露给外部主机。
- **怎么修**：`symbol` 用白名单正则校验（例如 `^[A-Za-z0-9._#+-]{1,32}$`），或者只接受 list_series 里已有的目录名；服务改用低权限账户运行。

**I13. 令牌明文走公网（部署层面）**
- **位置**：本地 `mcp.json` 中 host=0.0.0.0、port=47621；`mcp-remote-prompt.txt:9` 给远程 agent 的地址是 `http://103.236.80.135:47621/mcp`；`mcp_server.log:4` 里有非本机来源的请求
- **问题**：Bearer 令牌通过明文 HTTP 穿过公网，链路上任何人都可以抓到并重放。README:121-122 自己也建议加 TLS。
- **为什么重要**：令牌一旦泄露，攻击者就能利用 I12 和 I14 拿到凭据或拖垮交易主机。
- **怎么修**：在前面加一层带 TLS 的反向代理，或者改走 SSH / WireGuard 隧道，并且只放行已知的来源 IP；做完之后换一个新令牌。

**I14. MCP 单次请求的服务端开销没有上限，而且和交易终端在同一台机器上**
- **位置**：`src/mt5mcp/tools.py:30`、`:116`
- **问题**：每次请求都把整个 parquet 文件读进内存（XAUUSD M1 文件 91MB，展开后大约 200MB 以上），再在全部历史上算指标。SDK 用线程池执行同步工具（`.venv/Lib/site-packages/mcp/server/mcpserver/utilities/func_metadata.py:164`），并发请求可能把内存压垮，连带 MT5 终端和策略进程。返回条数有上限，计算量没有。
- **为什么重要**：持有令牌、或者偷到令牌的人，就能拖垮跑实盘的那台主机。
- **怎么修**：
  - 用 pyarrow 的过滤功能或 duckdb，只读取最后一段或指定的时间窗。
  - 算指标时只读"limit + 预热期"所需的根数。
  - 加并发信号量，限制同时执行的请求数。
  - 按文件修改时间做缓存。

**I15. RSI / ATR 的种子与预热期不符合 Wilder 的定义**
- **位置**：`src/mt5mcp/indicators.py:20-21`、`:46`
- **问题**：
  - 递推公式本身（α=1/n）是对的，但种子用的是第一个价格变化值 / 第一根 TR；Wilder、MT5 和 TA-Lib 都用前 n 根的平均值做种子。
  - 没有设置 min_periods，所以 RSI 从第 1 根、ATR 从第 0 根就开始出值。
  - 种子的影响按 (1−1/n)^k 衰减：n=14 时，第 14 根还剩约 35%，第 30 根约 11%。
  - 以库里的 BTCUSD MN1 为例：它只有 64 根，`compute_indicator(rsi, limit=50)` 返回的前十几个点里，种子的残余影响仍超过 10%。
  - `tests/test_mt5mcp_indicators.py:20-23` 还把第 1 根的 RSI=100 写成了预期值。
- **为什么重要**：在月线、周线这类短序列上，返回给 agent 的指标值可能和 MT5 图表上的读数差得不少。
- **怎么修**：种子改成前 n 根的平均值，前 n 根输出 NaN，与 MT5 / TA-Lib 对齐；在工具描述里写明计算口径。

**I16. 测试给出了虚假的信心**
- **位置**：见下面各项
- **问题**：
  - `tests/fakes.py:167` 的假 order_send 接受任意字典，交易测试只检查发出去的请求内容，不模拟 MT5"未给字段=0"的语义，所以测不出 C1。
  - `tests/fakes.py:70` 的成功码是 (0,"success")，真实包是 (1,'Success')。
  - `tests/fakes.py:155` 的 positions_get 不支持 `ticket=` 参数。
  - 下载器测试里的假 copy_rates（`tests/test_mt5data_downloader.py:49-59`）有三处与真实包不同：按新到旧返回、bar 时间没对齐、越界时返回空元组而不是 None。所以 C3 和 I7 的路径都没被覆盖到。
  - 测试里从来没用过真实包返回的 numpy 结构化数组，`src/mt5gw/market.py:16-21` 里处理 numpy 的分支只在冒烟时跑过。
  - 冒烟脚本的硬闸完全没有测试；保险丝测试漏掉了 modify_position（`tests/test_trading.py:100-110`）。
  - 鉴权测试只在一个玩具应用上测了 GET 请求（`tests/test_mt5mcp_auth.py:59-76`），没有对 `run_http` 实际组装出来的服务做集成测试。
- **为什么重要**：测试全绿，最危险的几种情况却完全没有防护。
- **怎么修**：改成有状态的 FakeMT5（维护持仓和订单簿、遵循"未给字段=0"的语义、返回 numpy 结构化数组、越界时返回 None）；把冒烟脚本的硬闸抽成一个可以单独测试的函数。

### Minor（可缓）

- **M1** `src/mt5gw/config.py:40-41`、`:57`：配置文件路径写错时，会静默使用默认值（包括默认的终端路径），可能连到另一个终端而没人发现；拼错的键也会被静默忽略。建议至少打警告，路径不存在时最好直接报错。
- **M2** 时区处理有两处不一致：
  - `src/mt5gw/models.py:18-20`：`_epoch_seconds` 遇到带时区的 datetime 时只是把时区标签换成 UTC，并不做换算，UTC+8 的时间会被静默错开 8 小时。建议直接拒绝带时区的输入。
  - `src/mt5data/store.py:78`：updated_at 是真正的 UTC 时间，却和服务器时间放在同一份 meta 里，再经 `src/mt5mcp/tools.py:51` 一起返回给 agent，容易被误读。
- **M3** `src/mt5mcp/server.py:17-20`：形如 `"…Z"` 的时间会被解析成带时区的时间，和不带时区的数据列比较时抛 TypeError；工具描述里也没说明这里的时间是券商服务器时间。建议去掉或拒绝时区信息，并在描述里写清楚。
- **M4** `src/mt5gw/trading.py:38-46`：下单量超过上限时会被静默压到最大值（要 1000 手实际下 100 手）。volume_step 为 0 时抛 ZeroDivisionError，传入无穷大时抛 OverflowError，都没有转成 TradeRejectedError。建议超限直接拒绝，并统一异常类型。
- **M5** `src/mt5gw/errors.py:47-59`：缺少 10010、10015、10027、10031 等常见返回码的说明；其中 10027（客户端禁用了自动交易）是用 Python API 时最常见的首次失败原因。`:50` 把 10007 写成"被交易服务器取消"，官方含义是"被交易者本人取消"。
- **M6** `src/mt5gw/account.py:78`：time_done 为 0 时用 time_setup 顶替，这等于伪造数据，应当返回 None。
- **M7** `src/mt5gw/market.py:105-108`：symbol_select 失败被忽略，后续报错会指向错误的原因。`src/mt5gw/symbols.py:51-55` 是一份没人调用的重复实现。每次 call 都会额外调一次 terminal_info，结果取一次报价要 4 次 IPC 往返。
- **M8** `src/mt5mcp/indicators.py` 里有几处与常见口径不同，都应在文档里写明：
  - `:36`：布林带用的是样本标准差（ddof=1），而 Bollinger 的原始定义、MT5 的 iBands、TA-Lib 用的都是总体标准差（ddof=0），带宽因此宽了约 √(20/19)≈2.6%。
  - `:28-31`：MACD 信号线用 EMA（Appel 的标准定义），而 MT5 内置的 MACD 用 SMA。
  - `:46`：ATR 用 Wilder 平滑，而 MT5 的 iATR 是 TR 的简单平均。
  - `:24`：涨跌幅都为 0 时 RSI 取 100，MT5 取 50。
- **M9** `src/mt5mcp/server.py:47-49`：compute_indicator 暴露了 fast/slow 两个参数，但没有任何指标用到；macd 和 bbands 写了却没有对外暴露。
- **M10** MCP 鉴权与配置的几处小问题：
  - `src/mt5mcp/auth.py:28`：请求头按 UTF-8 严格解码，遇到非 UTF-8 的头会直接返回 500（这不构成绕过）。
  - `src/mt5mcp/auth.py:26-27`：对所有非 HTTP 的连接类型都直接放行，应当只放行 lifespan。
  - `src/mt5mcp/config.py:34-40`：用 bool()/int() 强制转换，字符串 `"false"` 会被当成 True。
  - `src/mt5mcp/config.py:29`：只检查令牌长度，README:121 说"弱令牌会被拒绝"言过其实。
  - `src/mt5mcp/config.py:18-19`：注释说"SDK 默认也是关闭"并不准确——SDK 在监听本机、且没有传入安全设置时，会自动开启 DNS 重绑定防护（`lowlevel/server.py:752-758`）。
- **M11** `scripts/download_bars.py` 的几处问题：
  - `:29` 把品种名强制转成大写，`XAUUSDm` 这类带后缀的品种会下载失败，或者存到错误的目录。
  - `:33` 不读 config.json，自定义的终端路径不会生效。
  - meta 里没有记录 server 和 login，换了券商或服务器之后，数据会静默混进同一个序列。
- **M12** `src/mt5mcp/tools.py` 的两处统计/降级问题：
  - `:82-97`："缺失根数"绝大部分来自周末；MN1 按 31 天步长算出的期望根数也没有意义，所以发现不了真正的空洞。建议改成报告最大时间间隔。
  - `:41-43`：数据目录不存在时直接返回空列表，而这个目录又取决于启动时的工作目录，等于静默降级。
- **M13** `scripts/smoke.py` 的两处健壮性问题：
  - `:113-115`：找不到刚开的持仓时，会按 magic 取最新一笔去平，可能误平同一 magic 下策略自己的持仓。
  - `:61`、`:66`：列表为空时，`[-1]` 会抛 IndexError。
- **M14** 文档与测试的几处偏差：
  - README:26 说 `state()` 包含持仓、挂单和历史成交，实际并不包含。
  - 施工计划的全局约束写着"本地不落任何历史库"，与 mt5data 冲突；mt5data 和 mt5mcp 都没有设计文档。
  - `tests/test_mt5data_store.py:84` 的断言永远为真。
  - `tests/test_mt5data_downloader.py:31-33` 用了按本地时区计算的 `.timestamp()`；目前是没人调用的死代码，但留着就是隐患。
- **M15** 锁的粒度：initialize 最长可能阻塞 60 秒，拉取大块 K 线期间也一直持锁，同一进程里的报价和交易都会被卡住。下载器是独立进程、有自己的锁，所以"独占终端"只在单个进程内成立。
- **M16** 数据模型字段不全：Deal 缺少 entry、position_id、magic，Position 缺少 swap、price_current，策略层既分不清开仓和平仓成交，也做不了归因。

## 结论：可否投入生产使用（是/否/修后可）

**修后可。** 目前不建议用于实盘，K 线库也暂时不适合作为可信的回测数据源。前置条件如下：

1. **交易侧**：修完 C1、C2、I1–I5、I10，并在模拟盘冒烟中补上改持仓、挂单、撤单、部分平仓的真机验收。
2. **数据侧**：修完 C3、I6–I8，然后对现有数据库做一次带重叠校验的全面核对，必要时重新回填。
3. **MCP 侧**：在继续对公网开放之前，先完成 I12 和 I13（入参白名单、加 TLS 并更换令牌）。

其余各项可以排期处理。
