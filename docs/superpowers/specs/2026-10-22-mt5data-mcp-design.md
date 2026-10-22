# mt5data / mt5mcp 组件设计（补记）

背景：这两件是在基石（mt5gw）交付后按用户指令新增的（K线库 → MCP 数据服务），
本文补记当时口头确认的设计要点，供后续维护对照。完整修复记录见
claude_review.md 与对应 git 提交（2026-10-22 修复轮）。

## mt5data（K线库）
- 意图：建模/回测用的本地行情底座；本机直读（Parquet+DuckDB），不经 MCP。
- 布局：data/bars/{SYMBOL}/{TF}/data.parquet + meta.json（行数、首末时间、
  updated_at_utc、server/login 数据来源）。
- 铁律：只存已收盘 bar（末根收盘当且仅当收盘时刻 ≤ 服务器当前时间；
  服务器当前时间 = 墙钟 UTC + 券商时差自校准，限幅 ±6h，超限回退 offset=0）；
  写入原子（tmp + os.replace 重试）；回填错误必须上抛、与旧文件合并、
  收缩（更短/更晚起步）拒绝写入（--force 对冲）；刷新必须与旧数据接续，
  空洞报错；一根未取到即失败。
- 非目标：tick 库、实时订阅、跨机存储（Parquet+DuckDB 够用；出现多用户
  直连 SQL 需求时再评估 PostgreSQL，迁移路径 = COPY）。

## mt5mcp（MCP 数据服务）
- 意图：给远程服务器的 agent 一扇有界的数据门；本机建模不走它。
- 工具：list_series / get_bars(≤500) / describe_series(max_gap) /
  compute_indicator(sma/ema/rsi/atr/macd/bbands，尾部 limit+预热期)。
- 安全：Bearer 令牌（≥32 字符，常量时间比对，全 ASGI 包裹无路径豁免）；
  品种名白名单正则（防路径穿越/UNC-SMB）；DNS 重绑定防护默认关（令牌是
  真门锁，可配置开启）；跨公网必须 TLS。
- 资源：DuckDB 下推过滤 + LIMIT，不整文件载入；工具执行受 Semaphore(2)。
