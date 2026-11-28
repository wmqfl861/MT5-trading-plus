# F3 数据资格核验（T221，2026-11-28）

> **执行记录，不是资格结论。** 四个币对的资格结论要独立审核（D60、D117；第二十三轮施工计划 §6 第 2 条）。
>
> - 任务出处：第二十三轮施工计划 §6 第 2 条（决策 D117）和 D60；方法沿用预注册草案 `docs/research/f3-prereg-draft-2026-11-23.md` §3。
> - 编制时没有连接交易终端，没有导出研究库，没有开研究轮次，没有计算任何策略收益（包括"先看一眼"）。本报告不含任何价格、收益、效应的数字；日期一律 UTC；不含本机绝对路径、账号、服务器名、令牌。
> - 文中的数字只有：提交号、测试计数、下载命令输出里的字节数、盘点时读到的工件清单、行数和时间戳、覆盖空洞的天数。没有价格、收益、效应的数字。

## 0. 结论摘要

| 事项 | 结果 |
| --- | --- |
| 代码（草案 §3.3 第 1 条） | 已改、已提交：定盘锚点闸按显式币对清单执行，EURUSD 加 GBPUSD、USDJPY、USDCHF、AUDUSD。新测试 31 条，在父提交 a122c78 上 25 条失败、6 条通过 |
| 下载 | **停在第一年**。GBPUSD 2003 下载失败：响应体传到一半连接中断。这不是"站点没有这一年"，也不是 HTTP 429/403；原因分不清，按下载纪律停下——不重试那一年，也不继续后面的年份和币对 |
| 入库 | 四个币对入库 **0 个年份**。`data/ext/` 下没有它们的序列，`data/ext/_raw/histdata/` 下没有它们的 zip |
| 逐年闸门 | **一条都没跑**。没有数据，也就没有口径判定和锚点判定 |
| 资格核验 | **没有做**。四个币对的资格状态仍是草案 §2.1 里的"待做" |
| 盘点（只读） | USDJPY 在研究库有 MT5 段（H1、D1），主库没有；其余三对两处都没有。五个币对的显式交易日历工件都没有。四个币对的点差、佣金、合约规格工件都没有 |
| 缺窗清单 | 四个币对都是"日历工件没有，缺窗清单待定" |

## 1. 代码改动（草案 §3.3 第 1 条）

### 1.1 改了什么

两个提交：

- **b1654b1**：`scripts/fetch_ext_data.py` 里 `_histdata_year_gate` 的锚点闸原先写的是 `if pair.upper() == "EURUSD":`，改成对模块级常量 `HISTDATA_FIX_ANCHOR_PAIRS`（EURUSD、GBPUSD、USDJPY、USDCHF、AUDUSD）执行；加上新测试文件。锚点的规则没有变：
  - 只有锚点判 `inconsistent`（月末定盘窗的尖峰在错的小时）才排除该年；
  - `undetermined` 不排除，以标签规则检测为准入库，`anchor_note` 写进记录；
  - 年判 `inconsistent`/`undetermined` 的年份先按月回收，回收子集再测锚点；
  - 清单外的品种（如 XAUUSD）不执行锚点闸，只过口径闸。
- **7370816**：只改测试文件的模块 docstring（写明在父提交上的失败数）。

说明文字跟着改准：模块 docstring、`_histdata_year_gate` 的 docstring、`cmd_histdata` 开头打印的那一句（币对名从常量取，运行时打印为 `定盘锚点币对（AUDUSD/EURUSD/GBPUSD/USDCHF/USDJPY）另加定盘锚点月末口径`）。

### 1.2 没有改的

- `src/mt5data/ext/approvals.py` 没有动。"HistData 的批准和锚点条件是否自动覆盖四个新币对"是草案 §10.1 第 9 项，留给外部审核裁定。模块 docstring 里注明了这一点，脚本不就此作结论。
- `src/mt5data/ext/` 下的其余代码、`consistency.fix_window_vol_spike` 的函数和参数都没有动。伦敦 16:00 定盘窗对这些币对是同一时刻。
- 门槛、配置、策略代码都没有动。

### 1.3 测试：`tests/test_t221_anchor_gate_pairs.py`（新文件，31 条）

| 用例 | 条数 | 在父提交上 |
| --- | --- | --- |
| 清单恰好是 EURUSD 加四个新币对 | 1 | 失败（常量不存在） |
| 新币对：锚点 `inconsistent` 的年份被排除 | 4 | 失败（年份被启用） |
| 新币对：锚点 `consistent` 的年份入库 | 4 | 失败（记录里没有锚点字段） |
| 新币对：锚点 `undetermined` 的年份入库并写 `anchor_note` | 4 | 失败（同上） |
| 新币对：年判 `inconsistent` 的回收年，回收子集锚点 `inconsistent` → 排除 | 4 | 失败（年份被启用） |
| 新币对：回收子集锚点 `consistent` → 回收月份入库 | 4 | 失败（记录里没有锚点字段） |
| 同一份样本，新币对的锚点记录与 EURUSD 的逐项相等 | 4 | 失败（同上） |
| EURUSD 的三种锚点结果不变 | 3 | 通过 |
| XAUUSD、USDCAD 不执行锚点闸 | 2 | 通过 |
| 抽稀样本与整年 M1 样本的闸门判定逐项相同 | 1 | 通过 |

样本沿用 `tests/test_ext_data.py` 的合成年度 zip（`_synth_weeks_utc`、`_labels_for_rule`）。为了让这个文件只花一分钟左右，样本做了抽稀：闸门读的两类 bar——口径观测（周日首根、换日空档后的首根）和月末日下午搜索窗里的 bar——保留 M1，其余抽成 15 分钟一根。最后一条用例把抽稀样本和整年 M1 样本的判定逐项比对，钉住"抽稀不改变判定"。

证据：

- **父提交上红**。提交之后建临时工作树（`git worktree add … a122c78 --detach`），放入提交 7370816 里的测试文件（sha256 与提交里的文件相同），运行：
  `python -B -m pytest -q -p no:cacheprovider tests/test_t221_anchor_gate_pairs.py`
  → **25 failed, 6 passed**，退出码 1（2026-11-28 07:57:47–07:58:42 UTC）。关键失败：
  - `test_anchor_pair_list_is_explicit`：`AttributeError: module 'fetch_ext_data_t221' has no attribute 'HISTDATA_FIX_ANCHOR_PAIRS'`；
  - `test_inconsistent_anchor_year_excluded_for_new_pair[GBPUSD]`（USDJPY、USDCHF、AUDUSD 相同）：`{'used': True, 'calendar': 'us_wall', …}`——锚点 `inconsistent` 的年份在父提交上被启用。
  临时工作树随后删除（`git worktree remove --force`）。
- **分支上绿**（7370816）：
  - `tests/test_t221_anchor_gate_pairs.py`：31 passed；
  - `tests/test_fred_yearly_verification.py`：22 passed；
  - `tests/test_ext_data.py`：50 passed。
- ruff（仓库配置，只查错误类规则）：`scripts/fetch_ext_data.py`、`tests/test_t221_anchor_gate_pairs.py` → `All checks passed!`。

## 2. 下载与两道闸

### 2.1 纪律的执行

- 一次一条命令、一条命令一个年份：`--years <单个年份>`，`--pace-seconds 10`；没有并发，没有改请求头、代理和 `adapters.py` 的重试参数；没有用 `--force`。
- 磁盘余量（C 盘，`df -h /c`）：开始前 74 GB；GBPUSD 开始前 74 GB；结束时 74 GB。没有低于 40 GB，也没有走到"每 10 个年份"的检查点。
- 闸门计算串行；下载期间没有跑测试。
- 没有连接交易终端：没有 `import MetaTrader5`，没有启动或结束任何终端进程。

### 2.2 命令

形式（`--ext-root` 是顶层参数，写在子命令 `histdata` 之前）：

```
PYTHONUTF8=1 <venv>/Scripts/python.exe -B scripts/fetch_ext_data.py \
    --ext-root <主工作树>/data/ext histdata \
    --pairs <币对> --years <单个年份> --pace-seconds 10
```

一共 **2 条**，2026-11-28 08:02:12–08:02:44 UTC：

| # | 时刻（UTC） | 内容 | 退出码 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 08:02:12–08:02:15 | GBPUSD 2003，但 `--ext-root` 写在了子命令后面 | 2 | argparse 报 `unrecognized arguments`，**没有发任何请求** |
| 2 | 08:02:28–08:02:44 | GBPUSD 2003 | 1 | **下载失败**，见下 |

第 2 条的输出（原样）：

```
↓ HistData GBPUSD 2003 …
✗ GBPUSD 2003 下载失败：POST https://www.histdata.com/get.php 失败（重试 2 次）：IncompleteRead(1638400 bytes read, 627421 more expected)
✗ GBPUSD：无可用年份（全部被逐年闸门拒绝），不落盘
```

最后一行是脚本在"没有可落盘的年份"时的固定措辞。这一次实际上一条闸门都没有跑：这个币对一个 zip 都没有，全量重建的循环是空的。

### 2.3 判读：为什么停下

| 判据 | 实测 |
| --- | --- |
| 是不是"站点没有这一年"（HTTP 404 或等价信息） | **不是**。下载表单页（GET）取到了 tk，否则到不了 POST；按报错推算，POST 响应头给的总长是 2,265,821 字节。站点有这个文件 |
| 是不是限流（HTTP 429/403） | 没有看到这两个状态码 |
| 是什么 | 传输中途断开：响应体读到 1,638,400 字节时连接中断，还差 627,421 字节 |
| 重试了几次 | 适配器内部按既有参数试了 2 次，都失败。脚本只打印最后一次的错误；第一次的错误和两次是不是断在同一处，都看不到 |
| 本机有没有走代理 | 没有（环境变量和系统代理设置都为空），请求是直连 |

断连的原因是站点一侧的限流或故障、网络路径，还是别的，**分不清**。按纪律这属于"分不清是哪一种就停下"，所以：

- 没有重试 GBPUSD 2003；
- 没有请求 GBPUSD 2004–2025；
- 没有请求 USDJPY、USDCHF、AUDUSD。

现场核对：`data/ext/_raw/histdata/` 仍是 36 个 zip（EURUSD 23 个、XAUUSD 13 个）；`data/ext/histdata/` 下仍只有 EURUSD、XAUUSD。失败的那次没有留下任何文件。

### 2.4 逐币对

| 币对 | 请求的年份 | 成功 | 站点没有 | 失败（原因） | 入库的年份 | 覆盖起止 | 研究年数 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GBPUSD | 2003 | 0 | — | 2003（传输中断） | 无 | 无 | 无 |
| USDJPY | 没有开始 | 0 | — | — | 无 | 无 | 无 |
| USDCHF | 没有开始 | 0 | — | — | 无 | 无 | 无 |
| AUDUSD | 没有开始 | 0 | — | — | 无 | 无 | 无 |

逐年闸门结果表、回收的月份、锚点判定、启用年份、缺口清单：四个币对都**没有内容**，因为一个年份的 zip 都没有拿到。

## 3. 盘点（只读）

只读目录和工件的清单、`meta.json`、manifest 和 parquet 的 `time` 列；没有读价格列。

### 3.1 MT5 段

| 币对 | 主库 `data/bars/` | 研究库 `data_research/bars/` |
| --- | --- | --- |
| GBPUSD | 没有 | 没有 |
| USDJPY | **没有** | **有**：只有 H1 和 D1（没有 M1 和其他周期） |
| USDCHF | 没有 | 没有 |
| AUDUSD | 没有 | 没有 |

USDJPY 研究库那一段（`meta.json` 与 parquet 的 `time` 核对一致）：

| 周期 | 行数 | 起 | 止 | 其中 cutoff（2025-10-24）之前的行 | cutoff 当天及之后的行 |
| --- | --- | --- | --- | --- | --- |
| H1 | 180,090 | 1971-01-03 22:00 | 2026-11-06 11:00 | 173,647 | 6,443 |
| D1 | 15,813 | 1971-01-03 | 2026-11-05 | 15,489 | 324 |

- **草案 §2.1 里那处不一致**：T155 §3 写"缺 GBPUSD/USDJPY/USDCHF/AUDUSD 的 H1/M1"，对应的是主库；P 草案 §4 写"USDJPY = MT5 only"，对应的是研究库里的这一段。两句话说的是两个库。就 H1 而言，USDJPY 在研究库里有 MT5 段。
- **这一段没有裁到 cutoff**：含 cutoff 之后的行（H1 6,443 行）。使用时必须截断（P 草案 §4 已写明"库内晚于 cutoff 的 USDJPY 行一律截断"）。
- 对铁律 19（草案 §3.1 第 5 步）的含义：数据到位后，USDJPY 可以做与 MT5 重叠期的核对；GBPUSD、USDCHF、AUDUSD 没有 MT5 段，按草案 §3.3 第 6 条退化为"口径检测 + 锚点 + 夏令时边界测试"，如实记"无独立第二源交叉验证"。

### 3.2 显式交易日历工件

| 位置 | 有什么 |
| --- | --- |
| 研究库 `data_research/calendars/` | 只有 `USTEC.json` 和它的出证说明 `PROVENANCE.md` |
| 主库 `data/calendars/` | 目录不存在 |

**EURUSD 和四个新币对的显式交易日历工件都没有。** 草案 §2.2 拟定"五个币对共用一份、以 EURUSD 的为准、开轮前冻结"——这份工件现在不存在，要在开轮前另做。

### 3.3 成本输入（点差、佣金、合约规格）

| 工件 | 覆盖的品种 | 四个新币对 |
| --- | --- | --- |
| `costs/spread_model.json`（主库与研究库是同一份，sha256 相同） | BTCUSD、EURUSD、USTEC、XAUUSD | 都没有 |
| `costs/calibrated.json`（研究库） | EURUSD | 都没有 |
| `costs/calibrated.json`（主库） | BTCUSD、EURUSD、USTEC、XAUUSD | 都没有 |
| `specs/latest.json`（主库与研究库各一份，品种清单相同）及主库 `specs/history/` 的 18 份快照 | XAUUSD、BTCUSD、EURUSD、USTEC | 都没有 |
| `data/ticks/`（点差模型的原料） | BTCUSD、EURUSD、USTEC、XAUUSD | 都没有 |

四个币对的点差、佣金、合约规格工件**都没有**。补这些要 tick，终端取数被 D82、D117 禁止（草案 §4.3）。本任务没有去造。

## 4. 缺窗清单

缺窗清单只在该币对的显式交易日历工件存在时才生成，而且只看事件月里入场、出场那两根 H1 bar 在不在。§3.2 的结论是五个币对都没有日历工件，§2 的结论是四个币对没有数据。所以：

| 币对 | 缺窗清单 |
| --- | --- |
| GBPUSD | 日历工件没有，缺窗清单待定 |
| USDJPY | 日历工件没有，缺窗清单待定 |
| USDCHF | 日历工件没有，缺窗清单待定 |
| AUDUSD | 日历工件没有，缺窗清单待定 |
| EURUSD（草案 §3.2 要求盘点） | 日历工件没有，缺窗清单待定 |

## 5. 数据到位之后还要做的（本任务没有做）

1. 四个币对 2003–2025 的下载、逐年口径检测、定盘锚点闸，以及从 manifest 的 `yearly_verification` 抄录的逐年结果表。
2. 研究年数。现有函数是 `src/mt5quant/research/candidates.py` 的 `coverage_segments` 和 `merged_coverage_years`，与 ext-data 报告 §9.1 是同一口径：在超过 30 天的内部空洞处切开；manifest 里 `used=False` 的年份整年切开。读代码时注意到一处，续做时要先定口径：**回收年里被月级闸门丢掉的月份，现有函数只在 parquet 实测的相邻 bar 间隔超过 30 天时才切开**（`_manifest_gap_intervals` 只认整年）。而丢单个月份留下的空洞有多长，取决于丢的是哪个月、周末落在哪几天：`_histdata_year_gate` 的回收分支按换算后 UTC 的自然月留行，空洞从上一个月最后一根 bar 到下一个月第一根 bar，下限是那个月的长度加一分钟。
   - EURUSD 已丢的几个月，实测空洞（`data/ext/histdata/EURUSD/M1` 的 `time` 列）：2019-10 是 31 天 00:01，2023-02 至 07 是 181 天，2023-10 是 32 天 03:01，2024-10 是 31 天 00:01。四处都过线，所以现有数字里看不出这个差别。
   - 按这个切法推算（未实测）：30 天的月份留下 30 天 00:01，刚过线；2 月只有 28 或 29 天，空洞贴着月初月末时不过线，这个月仍计在年数里。也就是说，丢掉的月份切不切开取决于日历，不取决于它有没有启用。
   - 续做时建议两个数都报：现有函数的数（预筛工具用的是它），和把未启用的月份也切开之后的数。本任务没有改这个函数。
3. 显式交易日历工件（五个币对共用一份）和缺窗清单。
4. 四个币对的成本输入。目前没有合规来源（草案 §4.3）。

## 6. 本报告不授权的事

| 事项 | 说明 |
| --- | --- |
| 资格结论 | 本报告是执行记录。四个币对的数据资格结论要独立审核（D60、D117）；本报告没有结论——核验没有做 |
| 导出研究库 | 没有导出，也不授权导出。四个币对不在 `data/ext/` 里，不在研究库里 |
| 计算收益 | 没有计算任何策略收益，包括"先看一眼"；也不授权 |
| 草案 §10.1 第 9 项 | "HistData 的批准和锚点条件是否自动覆盖四个新币对；资格结论由子代理还是外部审核会话独立审核"——待外部审核裁定。`approvals.py` 没有改 |
| 改门槛、配置、策略 | 没有改 |
| 连接交易终端 | 没有连；补 MT5 段和 tick 不在本任务范围内 |
| 开轮、预筛、候选登记 | 没有做，不在授权内 |
