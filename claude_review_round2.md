## 总评

第一轮里最关键的交易安全部分修得扎实。全库只剩 `connection.py:81` 一处调用 `order_send`，并且和 `account_info` → 白名单保险丝在同一次持锁内完成；经网关 API 我没有找到能绕过保险丝或锁的路径。C2、C3、I1–I5、I7 也都有代码和测试佐证。

但修复轮留下了几处和上次复审同类的回归：
- a4fd729 的 `volume_current` 修复漏改了 `order_history()`：真包的 `TradeOrder` 没有 `volume` 字段，这个接口在真机上必崩。
- 冒烟兜底读取了 `PendingOrder` 并不存在的 `magic` 字段。
- 周末放行只适配 GMT+2/+3 的券商，而本库实际用的 Exness 是 GMT+0（`meta.json` 可证）。结果是一旦周末跑过刷新，开市后约 40 个日内序列每次刷新都会报"空洞"，直到手动全量回填。

另外，`--force` 会整体丢弃旧历史，而收缩保护的报错文案正把用户引向这个开关；I13（公网明文 HTTP，令牌写在提示文件里）没有修。

结论是**修后可**：Critical 0，新增 Important 4，Minor 12。

验证方式说明：本次会话的权限层拦截了所有 Python 调用（包括 pytest），测试没能复跑。以上结论来自静态审读、真包二进制里的字段名核对，以及 `data/` 下 `meta.json` 的实际数据。

## 第一轮修复复核（35 项）

- **C1 部分修复**
  - 持仓侧已修好：`modify_position` 用现值回填 sl/tp，并带上 symbol（`trading.py:169-176`），测试见 `test_trading.py:312-330`。
  - 挂单侧的 price/sl/tp 已回填（`:147-156`），但 `type_time` 硬编码为 GTC，也没带 expiration/stoplimit（`:155`）。原因是 `PendingOrder` 根本没有这些字段（`models.py:109-120`）。
  - 冒烟⑤分辨不出修复前后（见测试一节）。
- **C2 已修复，但兜底有新缺陷**
  - 已到位：强制 `allow_live=False`（`smoke.py:42-45`）；`st is None or trade_mode!=0` 时拒单（`:93`）；行情缺失不下单（`:98`）；下单前二次核身（`:108-112`）；整段包在 try/finally 里（`:104-196`）。
  - 新缺陷：兜底撤单崩溃，见 N3。
- **C3 已修复**：块错误上抛（`downloader.py:87`）；一根没取到即失败（`:126-129`）；与旧文件取并集，收缩时拒绝写入（`:101-115`）；CLI 失败返回 exit 1（`download_bars.py:54-57,66-68`）；有测试断言旧文件逐字节不变（`test_mt5data_downloader.py:124-138`）。残留问题见 N4。
- **I1 已修复**：`__post_init__` 与 JSON 装载同口径做类型校验（`config.py:40-52`）；放行条件是 `allow_live is True`（`:91`）。
- **I2 已修复**：同一把 RLock 内依次 重连 → account_info → 保险丝 → order_send（`connection.py:67-81`）；保险丝改成白名单（`config.py:87-96`）；6 个入口都有 `order_send==0` 的断言（`test_trading.py:122-137`）。
- **I3 已修复（有残留）**：10010 计为成功（`trading.py:23`）；返回 None 时抛 `OrderResultUnknownError`（`:231-235`）；PLACED 时 `confirmed=False`（`:238`）。残留见 m8。
- **I4 已修复**：`cancel_order` 走同一出口（`trading.py:179-183`）；冒烟④b 在真机上撤过单。
- **I5 已修复**：`call_status` 在同一次持锁内取 last_error 来裁决 None（`connection.py:56-65`、`account.py:169-176`）；平仓按 ticket 精确查持仓（`account.py:104-114`）。
- **I6 已修复，但空洞检测会误报**：按水位算需要的根数，并翻页直到与旧数据重叠（`downloader.py:155-163`）；没有旧文件时转全量回填（`:149-153`）。误报问题见 N1。
- **I7 已修复**：末根收盘判定没有任何宽容（`store.py:62`），测试精确到"恰在收盘 / 差 1 秒"（`test_mt5data_store.py:55-70`）。但 README:93-95 说的"与 tick 时钟解耦"不属实，见 m2。
- **I8 基本修复**：临时文件 + `os.replace` + 重试（`store.py:66-107`）。残留见 m6。
- **I9 已修复**：用 `copy_ticks_range` 取整个窗口再裁尾（`market.py:71-98`）。
- **I10 已修复，但没有测试**：重连后仍是 `connected=False` 时抛 `NotConnectedError`（`connection.py:100-108`）。没有任何测试把 `initialize_restores_connection` 设成 False（只出现在 `fakes.py:69,108`）。
- **I11 部分修复**：列表类查询已原子化。`state/get/quote/bars`，以及 `trade_send` 返回 None 之后读 last_error，仍是第二次持锁（`account.py:99-101`、`symbols.py:44-48`、`trading.py:231-235`）。
- **I12 已修复**：白名单正则加拒绝纯点号（`tools.py:24,29-34`）。残留见 m7；服务仍以 Administrator 身份运行。
- **I13 未修复**
  - `mcp.json` 仍是 host=0.0.0.0、port=47621。
  - `mcp-remote-prompt.txt:9/14/22/28` 里仍是 `http://<SERVER_IP>:47621/mcp`，并且写着与 `mcp.json` 一致的现行令牌（本报告不复述令牌）。
- **I14 部分修复**：DuckDB 用 Top-N 读取，加上 `Semaphore(2)`（`tools.py:167-172`、`server.py:18`）。SDK 用 `to_thread` 执行同步工具（`mcp/server/mcpserver/utilities/func_metadata.py:164`），所以信号量是有效的。但 period 没有上限，见 m4。
- **I15 数学已修复，但预热不足**：`_wilder` 先把种子（前 n 个有效值的均值）写进去，再用 `ewm(alpha=1/n, adjust=False, ignore_na=True)` 递推。这与 Wilder 的 `avg=(avg·(n−1)+x)/n` 严格等价（`indicators.py:24-39`）。预热长度的问题见 m3。
- **I16 部分修复**
  - 已改：成功码改为 `(1,'Success')`（`fakes.py:71`）；`positions_get` 支持 `ticket=`（`:161`）。
  - 未改：假 `order_send` 仍接受任意 dict，不模拟"缺省字段=0"（`:173-174`）；假 K 线仍是新→旧顺序、秒数固定为 :30、越界返回 `()`（`test_mt5data_downloader.py:37,55-67`）；测试里没有 numpy 结构化数组；没有 `run_http` 组装的集成测试。
- **M1 库已修复，脚本回退**：`load` 遇到缺失文件即抛错（`config.py:72-84`）；但 `smoke.py:42-45` 在路径写错时静默回落到默认配置。
- **M2 已修复**：带时区输入被拒绝（`models.py:18-25`）；水位字段改名为 `updated_at_utc`（`store.py:100`）。
- **M3 已修复**：`_parse_dt` 拒绝带时区时间（`server.py:21-30`）。
- **M4 已修复**：超上限拒绝；step/NaN/inf 一律拒绝并转成 10014（`trading.py:42-54,220-226`）。
- **M5 已修复**：补了 10010/10015/10027/10031，10007 文案已更正（`errors.py:37-71`）。
- **M6 已修复**：`time_done=0` 时返回 None（`account.py:88`）。
- **M7 部分修复**：`symbol_select` 失败会上抛（`market.py:106-113`）；但每次 `call` 前仍先调一次 `terminal_info`（`connection.py:101`）。
- **M8 已修复**：布林带改用 ddof=0（`indicators.py:62`）；MACD/ATR 的口径已写进文档（`:3-10`、`server.py:67-68`）；全平时 RSI=50（`:49`）。
- **M9 已修复**：macd/bbands 已暴露，fast/slow/signal/std_mult 都生效（`server.py:59-82`）。
- **M10 基本修复**：请求头按 latin-1 解码，只放行 lifespan（`auth.py:25-32`）；配置项严格类型（`mt5mcp/config.py:34-45`）。但 token 类型没有校验。
- **M11 部分修复**：meta 已记录 server/login（`store.py:101-102`）；但 CLI 仍把品种名强转大写（`download_bars.py:31`），不读 `config.json`（`:35`），取账户失败时用 null 覆盖数据来源（`:36-40`）。
- **M12 已修复**：改为报告 max_gap（`tools.py:111-134`）；数据目录缺失时抛错（`:57-59`）。
- **M13 回退**：空列表 `[-1]` 已防住（`smoke.py:73,76`）；但兜底范围扩大成"该品种上所有同 magic 的持仓和挂单"（`:187-194`），见 N3。
- **M14 基本修复**：设计文档已补。残留：README 第 69-70 行与第 86-87 行重复；计划文档仍写着"本地不落任何历史库"（`plans/…:20`）。
- **M15 未改（已文档化）**：README:41-43 写明了锁的粒度。
- **M16 已修复（Deal/Position）**：`models.py:92-139`。但 `PendingOrder` 仍缺 magic/type_time/expiration/stoplimit，这正是 N3 和 C1 残留的根源。

## 新发现的问题

### Critical：无

保险丝和锁没有绕过路径：账户读不到就拒单（`connection.py:76-79`）；重连后仍掉线也拒单（`:104-108`）。数据侧凡是不带 force 的路径都取并集合并，我没有找到会静默截断或改写已有数据的路径。

### Important

**N1 空洞检测在实际券商（Exness，GMT+0）上周末误报，刷新会持续失败**
- **位置**：`src/mt5data/downloader.py:55-66`（`_is_weekend_gap`）、`:167-182`、`:43-52`
- **问题**
  - 放行条件要求缺口里每个整点都落在周六或周日。
  - 外汇、金属、指数的周收市时间是纽约周五 17:00，换成 GMT+0 是周五 21:00（冬令时 22:00）。所以缺口起点总在周五，`weekday()==4`，判定为 False。
  - 服务器是 GMT+0 的证据：`data/bars/EURUSD/H1/meta.json` 里，`updated_at_utc` 为 14:02 时，`last_closed_time` 是 13:00；EURUSD M1 在 14:00:57 时末根是 13:59。
- **触发过程**
  - tick 滞后不超过 6 小时时，`server_now = wall+(tick−wall) ≡ tick`。所以周五收市后 6 小时内的刷新，会把最后一根当作未收盘扣下。
  - 周六凌晨之后滞后超过限幅，offset 回退为 0，这时任何一次刷新都会把周五末根当作已收盘存入。
  - 开市后再刷新：`expected_next` 是周五 21:00，第一根新 bar 在周日，于是抛"空洞"。
  - 抛错时不会落盘，下一次刷新的边界不变，所以**此后每次刷新都会失败，直到手动全量回填**。
- **影响范围**：EURUSD、XAUUSD、USTEC 的 M1 到 H2/H3（13–14 个周期，约 40 个序列）。H4 及以上周期和 BTCUSD 不受影响。USTEC/XAUUSD 的日内休市在特定调度时刻也会触发同样的问题（未逐一核对 Exness 的休市表）。
- **为什么重要**：README 建议把刷新做成定时任务，而定时任务必然会在周末运行。现有测试用的是一段整段落在周日的人造缺口（BASE=2023-10-22，周日），没有覆盖真实形态。
- **怎么修**：放弃"old_last+tf 必须紧邻"的算术判断，改为验证翻页结果里确实包含 `time==old_last` 那一根（可再比对 OHLC），以此证明与旧数据接续。接续点之后的缺口属于终端自身的序列（休市），不是刷新造成的。同时补一条 GMT+0 周末的回归测试。

**N2 `order_history()` 在真包上必然抛 AttributeError**
- **位置**：`src/mt5gw/account.py:86`（`volume=row.volume`）
- **问题**
  - 真包的 `TradeOrder` 只有 `volume_initial` 和 `volume_current`，没有 `volume`。`MetaTrader5/_core.cp312-win_amd64.pyd` 里的字段名可以证明这一点；`history_orders_get` 返回的也是这个结构。
  - a4fd729 只修了 `_pending`（`:60-61`），漏了这里。
  - 测试夹具 `HistRow` 自带 `volume` 字段（`test_account.py:28-30`），把问题掩盖了；冒烟脚本也从没调用过 `order_history`。
- **为什么重要**：时间窗内只要有一笔历史委托，这个公开接口就会崩。抛出的不是 `GatewayError`，按网关异常捕获的策略会直接崩溃。
- **怎么修**：改用 `row.volume_initial`；夹具按真包字段命名；冒烟⑦加一次 `order_history` 调用。

**N3 冒烟兜底在它该兜的场景里失效，还会误平策略持仓**
- **位置**：`scripts/smoke.py:184-196`；`models.py:109-120`
- **问题**
  - `o_.magic` 访问的是不存在的字段，抛 AttributeError。`except GatewayError` 接不住，异常逃出 finally，挂单留在账户上。只要该品种有任何挂单就会触发：④b 撤单失败、④ 返回 PLACED 或结果未知，正是兜底要处理的情形。
  - 持仓兜底会平掉该品种上所有 magic 等于 `gw.config.magic` 的持仓。这个 magic 来自共用的 `config.json`，默认值 20261022 对所有网关使用者都一样，所以同一模拟账户上正在做前向测试的策略持仓也会被平掉。
- **为什么重要**："finally 里保证兜底"在失败路径上并不成立。影响范围只限模拟盘，但会破坏模拟盘上的策略状态。
- **怎么修**：只清理冒烟自己产生的 ticket；给冒烟单独的 magic；给 `PendingOrder` 补上 magic 字段；兜底用 `except Exception`，并打印残留清单。

**N4 `--force` 会丢弃全部旧历史，而报错文案在引导用户使用它**
- **位置**：`downloader.py:103-104`（force 时直接 `return new_df`，不合并）、`:107-109`（文案"确要覆盖请用 force=True 重跑"）
- **问题**：换机或重装后 MaxBars 会回到默认 10 万（README:113-115 说明这个值是手工调大的），券商的留存窗口也可能前移，两者都会让收缩保护拦下回填。照文案加上 `--force` 后，XAUUSD M1 的 3,345,048 根（2014→2026）会被替换成约 10 万根。这是唯一一份数据，没有备份。N1 的报错又要求用户"重跑全量回填"，两条路径会串在一起。
- **为什么重要**：C3 要防的正是"截断数据覆盖完整历史"，现在离它只差一个开关。
- **怎么修**：force 也取并集合并（并集不会丢行）；另设 `--replace`，替换前自动备份旧文件；文案写明 MaxBars 这一成因。

### Minor

- **m1 改挂单会静默改成 GTC**（C1 残留，`trading.py:146-157`）：DAY/SPECIFIED 订单改一次就永不过期；STOP_LIMIT 单会因 stoplimit=0 被拒。README:82-84 说"一律回填当前值"不属实。
- **m2 `server_now` 并没有与 tick 时钟解耦**（`downloader.py:43-52`）：实际行为偏保守，所以无害，但 README:93-95 和设计文档:11-12 的描述不对。超限后回退 offset=0，对 GMT−4/−5 的券商是高估方向，可能把当天未收盘的 D1 存进库。`:186-187` 的防御分支在时钟回退的过渡期会被触发，注释说"理论不发生"不成立。
- **m3 Wilder 预热不足，"残余<1e-4"不属实**（`tools.py:26,163-165`）
  - 首个返回点的种子残余是 (1−1/n)^(warmup−n)：n=14 约 1e−8，n=30 约 4e−4，n≥52 时约 1.8%。
  - `5n` 这个预热长度只对 EMA 成立（约 e^−10）。
  - MACD 的预热没考虑 signal，signal=200 时残余约 7%。
  - 后果：同一时间戳的值会随 limit 变化。
  - 另外，ATR 的第一根 TR 取 H−L 并计入种子，与 TA-Lib 差一根，只影响短序列。
- **m4 period 无上限**（`tools.py:163-172`）：period=10^9 会让 LIMIT 覆盖全表，334 万行直接载入 pandas，绕过了 I14 的有界读取。
- **m5 翻页的边角情况**（`downloader.py:86-98`）
  - 达到 max_chunks 时静默返回部分数据。
  - 总根数恰好是 chunk 的整数倍时，下一页会越界。旧代码对越界写的是 `except …: break`，说明真包越界多半返回 None。C3 修复后这里改为上抛，整个序列的回填会失败（`download_bars.py:7` 示例的 `--chunk 50000` 配上默认 MaxBars 10 万正好整除）。
  - 这一点没有在真机验证。
- **m6 原子写的残留**（`store.py:90,104,67-74`）：临时文件名固定，也没有进程间锁，定时刷新和手动回填并发时会互相覆盖；没有 fsync，只能保证原子、不能保证持久；重试总预算只有 2 秒。
- **m7 白名单正则用了 `$`**（`tools.py:24,30`）：`"EURUSD\n"` 能通过校验（目前无害），应改用 `re.fullmatch`。另外，`test_mt5mcp_tools.py:77` 里的 `"\\attacker\share"` 实际只有一个反斜杠，并不是 UNC 路径。
  - SQL 拼接面复核：since/until 进入 SQL 前经过 `fromisoformat` → `isoformat` 规范化（`server.py:21-30` → `tools.py:83-85`），字符集只有数字和 `-:T.`，没有注入面。路径经过 `_sql_literal` 转义，品种名里不会出现引号或通配符。仍建议改为参数绑定。
- **m8 `OrderResult` 语义不一致**（`trading.py:236-248`）：`ok` 永远是 True；`confirmed` 对 DEAL 表示"成交确认"，对 PENDING/SLTP/MODIFY/REMOVE 表示"请求完成"，挂单返回 PLACED 时会被标成"未确认"；DONE_PARTIAL 没有请求量字段；README:78-80 说 PLACED 时"成交号为空"，但代码只在 deal 为 0 时置空。
- **m9 结果未知时诊断不原子**（`trading.py:231-235`）：`trade_send` 返回 None 后读 last_error 是第二次持锁，诊断信息可能对不上号。
- **m10 下载 CLI**（`download_bars.py:31,35,36-40`）：仍把品种名转大写；不读 `config.json`；取账户失败时用 null 覆盖数据来源；不校验券商是否一致。
- **m11 冒烟脚本**：`smoke.py:42-45` 配置路径写错时静默用默认值；`:154` 的 `pos.volume*point*500` 量纲不对（EURUSD 0.02 手约等于 1 pip），在有 stops level 的券商上会被 10016 拒绝。
- **m12 纵深防御与杂项**
  - `MT5Connection.call` 不拦截 `order_send`。另外，真包自带的 `Buy/Sell/Close` 直接调用 `order_send`（`MetaTrader5/__init__.py:261-341`），策略直接 import 就能绕开网关，建议写进 README。
  - `allow_live=True` 时没有钉死 login。
  - 下单路径不检查品种是否可见（`trading.py:250-255`）。
  - token 类型没有校验（`mt5mcp/config.py:28-33`）。
  - `tools.py:59` 的报错会回显服务器绝对路径。

## 测试与验证质量评价

静态计数 `def test_` 共 146 个，与声明一致；本次会话没能运行。

**写得好的**
- 保险丝 6 个入口逐一断言 `order_send==0`，账户读不到时 fail-closed（`test_trading.py:122-148`）。
- C3 断言旧文件逐字节不变。
- I7 的收盘边界精确到秒。
- RSI(2) 有手算序列（`test_mt5mcp_indicators.py:20-24`），可以独立复核。

**不贴近真实、因而漏掉真问题的**
1. **夹具字段名与真包不一致**：`HistRow`/`OrdRow` 用的是 `volume`，所以漏掉了 N2。`fakes.py:3` 声称"常量与真实包一致"，但 `COPY_TICKS_ALL` 是 2，真包是 −1（`MetaTrader5/__init__.py:27`）。
2. **周末测试是人造的**：变量 `friday` 实际是周日（`test_mt5data_downloader.py:164-172,202-214`），所以漏掉了 N1。
3. **冒烟⑤分辨不出 C1**：持仓初始 SL=0，只改 TP，再断言 SL==0（`smoke.py:149-161`）。修复前的 bug 同样会得到 SL=0，所以"真机验证了 C1"不成立。应先设一个非零 SL，再只改 TP，断言 SL 不变。
4. **冒烟兜底分支**在通过的那一轮里一行都没执行，所以漏掉了 N3。
5. **I10 路径没有任何覆盖。**
6. **下载器假数据**：新→旧顺序、秒数 :30、越界返回 `()`。"恰好整倍数"的路径（`:154-161`）只是因为返回 `()` 才通过。
7. **断言过宽**：几处 `pytest.raises(Exception, …)`（`test_trading.py:305,342,404`、`test_account.py:73`）会把 AttributeError 这类真 bug 也当作通过，应收紧到 `DataUnavailableError`。

**覆盖缺口**：ATR 只测了常数 TR；没有指标窗口不变性测试（能直接暴露 m3）；没有 `run_http` 组装的集成测试；没有 `"."`、`".."`、尾随换行这类品种名用例。

## 结论：修后可

交易主干可以继续在模拟盘使用。剩余前置条件：

1. **交易侧（上实盘前）**
   - 修 N2。
   - 修 m1，或者撤回 README 里"一律回填"的说法。
   - 按"非零 SL 保持不变"重写冒烟⑤，在真机上重新验收 C1。
   - 修 N3，然后人为制造一次撤单失败，验证兜底确实生效。
2. **数据侧（启用定时刷新前）**
   - 修 N1，补 GMT+0 周末回归测试；这个周末过后核对刷新日志。
   - 修 N4，让 force 也取并集，替换前自动备份。
   - 修好 m6 之前，定时刷新和手动回填不要并发。
3. **MCP 侧（继续对公网开放前）**
   - 解决 I13：上 TLS（反向代理或隧道）；轮换令牌；把令牌从 `mcp-remote-prompt.txt` 里移除，改由远程端从密钥管理中注入。
   - 给 period 设上限（m4）。
4. **验证**：本次会话没能执行任何 Python。N1 和 N2 的结论来自代码推导，加上真包字段名和 `meta.json` 的证据。修复时建议各补一条用真包字段名、GMT+0 时间轴构造的回归测试，并重跑 pytest 和冒烟。
