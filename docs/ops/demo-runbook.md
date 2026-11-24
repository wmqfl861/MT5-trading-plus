# 演练运维手册（阶段 F，两周；T45/T49）

`demo_run.ps1` 有两种**互斥模式**（无 `-Mode` 拒绝裸跑）——成交路径、手数
口径与验收边界完全不同，不能混用：

| | `-Mode PaperSoak`（影子盘延长观察） | `-Mode Demo`（MT5 模拟盘，蓝图阶段 F 正式口径） |
|---|---|---|
| 成交路径 | `--paper` PaperBroker 影子券商（真终端行情，**绝不连真实下单通道**） | 真实 demo 账户：下单经 MT5 网关（保险丝只放行 trade_mode=DEMO） |
| 手数 | 按配置（示例 `risk_fraction=0.005`） | **固定 `volume_lots=0.01`**；出现 `risk_fraction` 或非 0.01 → 拒绝启动 |
| 风控/对账 | `--paper-state` 聚合读 `data\live\paper\state-<magic>.json` | 直连 demo 终端（`--gw-config`），不用 paper 状态 |
| 启动前专项预检 | 七项通用预检 | 七项通用预检 + 配置手数校验 + **DEMO 保险丝与网关预检**（只读 dry：连接、读账户核对 trade_mode=0、逐品种报价；绝不下单） |
| 产生的证据 | 无经纪商成交滑点/拒单/重连证据 | 真实成交滑点、服务器端 SL/TP、拒单与重连均可观测 |
| 验收口径 | 只做日常检查（`--day`/`--summary`）；**PaperSoak 的证据不被正式机械预检采信** | 日常检查 + `--accept --formal-window` 离线机械预检（§4）；机械满足仍须独立核实历史真实性及 A5 |

共同骨架：每策略一个运行器、一个风控 `--loop`、一个对账 `--loop`。验收
标准见蓝图 §7；检查器是 `scripts/demo_check.py`（两档口径，见 §2）。

## 1. 启动（第 0 天）

```powershell
# 一次性：在维护窗口显式指定模式安装每日/每周计划任务。
# 部署树与运行目录分离时，解释器和各目录要用参数显式给出（T213，见 §6.3.1）；
# 任务命令要用的文件不在，或路径里有包装器写不了的字符时，脚本拒绝安装（见 §6.3.1）。
powershell -ExecutionPolicy Bypass -File scripts\setup_scheduled_tasks.ps1 -Mode Demo

# 影子盘延长观察（PaperSoak；示例配置 demo-configs\MA-XAUUSD-M15.toml）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Mode PaperSoak

# MT5 模拟盘（Demo；示例配置 demo-configs\DEMO-MA-XAUUSD-M15.toml，固定 0.01 手）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Mode Demo

# 只做预检与生命周期检测（不启动任何进程；Demo 模式含网关 dry 预检）
powershell -ExecutionPolicy Bypass -File scripts\demo_run.ps1 -Mode Demo -PrecheckOnly

# 固定部署的启动/补缺和停止使用 §6.2/§6.3 的完整路径与 SHA。
# 旧无固定身份的 -Stop/已登记实例补缺已禁用，不能仅凭 PID/basename 操作。
```

- 通用预检（两种模式同）：点差模型/研究缓存/合约快照/会话配置/风控配置/
  磁盘/magic 唯一，任一不过不启动任何进程。Demo 另有手数校验（走
  `scripts/config_check.py` 的 LiveConfig 真解析器按**实际加载值**比对
  固定 `volume_lots=0.01` 且无 `risk_fraction`——注释/多行字符串/表
  作用域骗不过）与 DEMO 保险丝/网关预检（账户非模拟盘或读不到账户
  状态 → fail-closed 拒启动）。
- **生命周期（T110）**：实例登记在 `data\live\instances.json`，除角色/PID/
  登记时刻（可能回退到启动后登记时间）外，还记录完整 argv、deployment_sha、部署目录与数据目录。固定
  Demo 部署的检测/停止与 supervisor 共用同一 `verified_tree`：核验完整
  身份与全部父子进程，存活的角色跳过，只有整树被证明缺失才补起。命令行
  不可读、PID 复用或存活孤儿 worker 都拒绝。旧登记不能自动迁移，旧无固定
  身份的补缺/停止入口已禁用。检测→启动→登记→运行窗追加仍在
  `instances.lock`（FileShare.None）临界区内，停止保留登记。完整操作见
  §6.2/§6.3；`pids.txt` 只是追加式历史，不参与身份判定。
- **运行窗登记（C-1）**：每次启动（含恢复重启）向
  `data\live\run-manifest.json` **追加**一条 runs（kind=start|restart）；
  该文件读失败或 JSON 损坏时脚本 fail-closed 拒绝启动。正式机械预检不再
  采用当前 mode 的最早 run 起算，必须按 §4 绑定留存部署原件与完整日历；
  同 SHA 重启不清窗，异 SHA/mode/路径混入后再切回也拒绝。
- 产物：`data\live\instances.json`（当前登记）、`data\live\run-manifest.json`
  （运行窗，追加式）、`data\demo\pids.txt`（追加式 PID 历史）、
  `data\demo\demo-run-<Mode>-*.json`（本次清单，含跳过的存活运行器）、
  每进程一个 `runner|risk|reconcile-<name>-<stamp>.log`（+ `.err`）。
- 顺序已内建：先运行器（写意图/心跳/paper 状态）→ 8 秒后风控 → 对账。
  **不要**先起风控（心跳未落盘会被写成 halt.json 一键停机）。
- 安装器默认只创建日报和周报任务，不覆盖快照、K 线、tick 或 swap 任务。
  历史数据任务需另行核对终端身份与配置后显式加 `-IncludeDataTasks`，
  当前阶段 F 安装命令不加该参数。
- 日报模式由安装时的 `-Mode Demo` / `-Mode PaperSoak` 显式固定。Demo
  使用 `--demo --gw-config`，拒绝实盘解锁配置，核验 `trade_mode=0` 后才读
  成交流水；PaperSoak 只读 `--paper-state`。影子目录归档或缺失不会自动
  切换到终端。当前阶段禁止真实账户连接与下单；模式变更只在维护窗口执行。
- 重启机器后：按 §6.2 重用固定 SHA 和完整路径，只补已核验退出的整树。
  旧登记缺身份字段时先人工核验迁移，不能删登记后重跑。每次新进程生成
  新日志；恢复向 run-manifest.json 追加记录，运行窗不清。

### 1a. runner 启动二次确认 trade_mode=0（3B 规格，H 线基石阶段 3）

`demo_run.ps1` 的 DEMO 保险丝/网关预检（只读 dry：连接、读账户核对
trade_mode=0）在**启动前**把过一道关；但预检通过与 runner 实际连上
网关之间存在空窗——终端可能被切到另一账户、配置可能指向别的
`--gw-config`、多终端场景下预检与运行器甚至可能不是同一连接。因此
**规格要求（实现落在 `scripts/live_runner.py`，属阶段 1+2 批次）**：

1. runner 完成网关连接后、**进入决策循环之前**，必须重新读取账户状态，
   核对 `trade_mode == 0`（DEMO）；Demo 演练配置下这是第二道独立防线，
   不替代 `demo_run.ps1` 预检。
2. 核对结果 ≠ 0（含 CONTEST=1 / REAL=2），或**读不到账户状态** →
   fail-closed：写告警（`data\live\alerts.log`，TRADE_MODE 语义），
   不发送任何下单请求，直接退出（非零退出码）——绝不"先跑起来再说"。
3. 该二次确认只在**启动时**做一次即可，不要求逐决策轮核对（逐轮口径
   属实盘批次，另行评审）。
4. 验证口径：假网关账户 `trade_mode=2` → runner 拒绝进入循环且零下单；
   `trade_mode=0` → 正常启动；账户状态不可读 → 同 REAL 处理（fail-closed）。

5. 实跑单实例锁按 `server|login|magic` 哈希，存放于
   `%LOCALAPPDATA%\mt5-trading-plus\locks\runner-<hash>.lock`，不随
   `data_root` 改变；连接前另有 magic-only 临时锁，防启动竞速。
6. **跨版本升级保护**：新 runner 同时取得身份锁与旧版
   `data\live\runner-<magic>.lock`。若旧版进程仍持有目录锁，新版拒绝
   启动并提示先停止旧进程；不得手工删除锁文件来绕过内核锁。退出时内核
   自动释放锁，锁文件本身常驻磁盘。不同数据目录的并发实例由身份锁阻止。

## 2. 每日检查（每个交易日，5 分钟；两档口径）

```powershell
# 日常档 --day（宽松：无异常证据即退出码 0；"无样本"如实标注、不判不通过）
.venv/Scripts/python.exe scripts/demo_check.py --day 2026-11-10

# 日报（换日休市窗口 05:57 计划任务自动生成（T182，旧 00:20 已废止）；PaperSoak 读 paper 状态 + 决策日志点差比对）
type data\reports\live\daily-2026-11-10.md
type data\logs\task_live_daily_report.log      # 任务输出（排查用）
```

- 日常档返回 0 只说明"未发现异常证据"，**不等于取得验收证据**；汇总口径
  同（`--summary` 不再宣称"全部通过"，只说"未发现异常证据（日常口径）"）。
- 重点看：心跳连续性、无法解释持仓差异=0、日报存在且点差无 `**超标**`。
- 心跳异常先看 `data\live\alerts.log` 与对应 `.err` 日志，再按 §3 处理。
- 每周日 23:00 UTC（本机周一 07:00）`MT5Quant\demo_weekly_check` 自动跑
  `demo_check --summary` 全周汇总，落 `data\demo\weekly-check.md`（覆盖写）；
  平时手动：`.venv/Scripts/python.exe scripts/demo_check.py --summary`。

### 2a. 链路基线（T218，第二十三轮；只读）

```powershell
# 一个 UTC 日（不给 --date：取 UTC 的前一天，不是本地日历的前一天）
.venv/Scripts/python.exe scripts/ops/link_baseline.py --terminal-log-dir <终端数据目录>\logs --date 2026-11-24 --format table
# 连续几天（一次最多 63 天）；表格只有计数和秒数，可以直接抄进 phase-f-log
.venv/Scripts/python.exe scripts/ops/link_baseline.py --terminal-log-dir <终端数据目录>\logs --from 2026-11-24 --to 2026-11-28 --format table
```

- 只读终端日志文件和系统日志（DNS-Client 1014），不 import MetaTrader5、不连接
  终端。输出不含账号、服务器名、接入点、IP、本机路径。
- 读的是交易终端数据目录下的 `logs`。终端日志会被终端清理（保留多少天没有
  核实），所以要留副本：每个本地日期过完之后（本地第二天），把那一天的
  `YYYYMMDD.log` 复制到 `data\ops-evidence\` 下（不入库）。**副本要从前一个本地
  日期开始留，最后一天之后再多留一个本地日期**：一个 UTC 日（本地 08:00 到次日
  08:00）跨两个本地日期的文件，少一个就出不完整。
- **怎么跑**：5 个交易日过完后，用留存的副本一次跑 `--from … --to …`，并给
  `--calendar`。不要每天跑"昨天"：一个 UTC 日要等它结束、终端写下新日志之后才出
  得完整，结果会随运行时刻变。
- 一个 UTC 日**计入基线**（`baseline_day`）要同时满足：
  - 给了日历，这一天是交易日，并且判定会用到的日子日历都覆盖到——就是这一天，
    有中断从前一天延续进来或延续到后一天时，还有那两天（没覆盖到的按工作日算，
    偏严，但这一天不计入）；
  - 日志接得上：当天开始之前和当天结束之后都有日志行（没被截断）——这说的是
    日志没缺，不是"全天都有日志"；
  - 终端全程在：相邻两条日志行相隔不到 7 小时（终端在线时约每 6 小时写一条扫描
    行；这个阈值只有 7 天样本，可以用 `--absent-gap-s` 调）；
  - 没有让窗口失败的中断；没有到数据末尾仍未恢复的中断；
  - `near_breach_segments` 和 `tails_into_trading_day` 都是 0；
  - 当天的日志里至多一个账号（`accounts_seen` 不大于 1）；
  - 不在"读不了的日志"的影子里（见下面退出码一条）。
  连续 5 个交易日都计入才起算（D116）；任何一天 `would_fail_window` 为真就不起算。
  后面几条取偏严：这些情形窗口可能失败、中断可能被交叉配对，脚本不下结论，这一天
  不计入，要人逐条看。表格的"基线"一栏在不计入时写出原因：先看别的栏（窗口失败、
  终端不在、数据不全……），那些都没问题时才写属于日历和账号的那一种。
- 窗口判据与 `demo_check.py` 一致、不放宽，**每一天单独判**：中断开始后第 900 秒
  还没恢复、那一刻是交易日，记失败；中断跨进后面的交易日，落在交易日里满 900 秒
  按失败计。前一天晚上断线、次日恢复的，两天都不合格。
  `near_breach_segments` 是离 900 秒不到一分钟就恢复的段（终端日志和进程的计时
  起点相差约一分钟，窗口里可能已经触发了 78）；`tails_into_trading_day` 是零点前
  开始、进了交易日不足 900 秒就恢复的尾巴。
- 日历：`--calendar` 给正式检查的日历 JSON（`days: [{date, trading}]`）或日历源
  文件（每行 `YYYY-MM-DD weekday|weekend|holiday [理由]`）。不给日历的日子一律不
  计入。给了日历而日历没覆盖到的日子按周一至周五算（把节假日当交易日，偏严），
  缺的日期列在 `calendar_missing_days` 里（只列会影响到这一天判定的前后三天），
  这样的日子同样不计入。记 11-24 起的 5 个交易日时，日历要把 D83 的节假日
  （11-26、11-27）标成休市。
- 一段跨日的中断在两天里各出现一次（开始那天算整段，延续的那天只算落在当天的
  部分，记为"延续"）：段数和合计秒数跨日相加会重复，不要跨日求和。
- 退出码：0 出了统计；3 有日子出不了统计（`no_terminal_log`、`log_unreadable`、
  `log_dir_unreadable`，stdout 一行 JSON）；2 参数错误。退出码 3 的日子一律按
  不计入处理，不要当成"没有断线"。原因是 `log_unreadable` 的，**把那个文件重新复制
  后重跑**。不修的话多半凑不满：读不了的文件里可能记着一段延续到次日的中断，脚本
  把从它覆盖的时段起、到其后第一条 `authorized` 为止的日子都判成不计入
  （`uncertain_due_to_unreadable`，表格的"窗口判据"写"不确定"）；连接很稳时终端不写
  `authorized`，这段"不知道"会一直延伸到数据末尾。
- 退出码 3、`terminal_absent` 为 null（当天的日志缺了一个本地日期）、`dns_timeouts`
  为 null（系统日志不覆盖那一整天）都是"不知道"，不是"没问题"。
- 已知限制：进程的计时起点和终端日志的 lost 时刻相差约一分钟；终端日志的时钟
  按固定偏移换算（`--log-utc-offset-hours`，默认 8），换主机或换时区后要改。

## 3. 常见问题（恢复原则：只移除停机信号；仅重启已确认退出的进程）

**killSwitch 一键停机后恢复**（演练项，预期会触发一次）：
```powershell
New-Item -ItemType File data\live\killSwitch      # 停：风控下一轮写 halt.json
#   各执行器开仓腿随即被拒；这是"一键停机"演练项的证据（RISK_HALT 告警）
Remove-Item data\live\killSwitch, data\live\halt.json -ErrorAction SilentlyContinue
# 恢复 = 只移除停机信号；进程若还活着就什么都不用再启动（killSwitch 不杀进程）
```

**进程重启/停止（T110：完整树核验）**：

先按 §6.3 禁用 supervisor 并设置 killSwitch；用 §6.2 的同一组固定 SHA 与
显式路径调用 `live_supervisor.py --inspect` 检查，`--stop` 停止。`demo_run.ps1
-Stop` 仅是该停止入口的转发，也要求相同完整部署参数。全部角色身份核验
完成后才开始子先父后的停止，登记保留。某个父 PID 不存在、日志已有退出
行、或任务管理器不见 launcher，均不能证明 worker 已退出。

补缺启动仍用 §6.2 完整命令，共用同一树核验。不可读 PID、身份不符、存活
孤儿 worker 均保守拒绝。禁止单杀 launcher、清掉 instances.json 后重跑或
用历史 pids.txt 杀进程。旧版/PaperSoak 无固定身份登记须在维护窗口人工
确认整树及数据状态后迁移，不能自动补起。

**日报缺失/内容异常排查**（`data\reports\live\daily-*.md` 没生成）：
0. 先看 `data\live\alerts.log` 里当天的 `DAILY_REPORT_FAILED`：消息按日写了实际尝试了几次、
   没有尝试的原因，以及是哪一类失败、退出码是多少（T213，见 §6.3.1）。启动或重试的时刻不在
   换日窗内（D82）时日报不连终端、退出码 3——手工补跑或者在窗外补跑任务都会这样。
1. `type data\logs\task_live_daily_report.log`——最常见：影子状态不可用
   （退出码 2，fail-closed 不落半截日报）→ 确认运行器活着、
   `data\live\paper\state-*.json` 存在且非空；
2. 点差比对报 `model_sha256` 不一致 → 重跑 `scripts/build_cost_model.py`
   重建 `data\costs\spread_model.json`（日报拒绝用被动过的模型）；
3. 手动补跑。计划任务每次顺带补最近三天的日报，缺得更多的要逐天指定 `--date`。**Demo 模式
   同样只能在换日窗内做**（要连终端，D82）；窗外运行不连接、退出码 3。保持已生成
   `scripts/task_live_daily_report.cmd` 中的模式和配置路径；首次生成前使用与安装器相同的
   配置选择顺序。Demo：
```powershell
$gwConfig = @("config.json", "gateway.json") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $gwConfig) { throw "缺少 Demo 网关配置" }
.venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-11-10 `
    --demo --gw-config $gwConfig --sessions-config config\sessions.toml `
    --spread-model data\costs\spread_model.json --log-dir data\demo
```

PaperSoak 离线补跑不需要网关配置，也不连终端、不受换日窗限制，直接使用影子状态：
```powershell
.venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-11-10 `
    --paper-state data\live\paper --sessions-config config\sessions.toml `
    --spread-model data\costs\spread_model.json --log-dir data\demo
```

## 4. 正式窗口机械预检（离线材料；历史真实性及 A5 另审）

本入口仅核对明确提供的离线副本。当前实施范围只使用人工临时合成材料；
不授权真实 data/config/DB、账户/MT5、进程、任务、维护或部署操作。不要将
运行中的目录作为本批输入，也不要因结果而清除保护。旧 `--day/--summary`
用法和日常退出码保持不变。现有 `demo_run.ps1` 帮助及启动提示中以裸
`--accept` 作为阶段 F 验收的指引已过时；正式使用须按本节的
`--accept --formal-window` 与四个显式离线路径执行机械预检。exit3 仅表示
机械满足，历史真实性及 A5 另审，不能从该旧提示推断 A5 已通过。

```powershell
# evidence 是已保全的离线材料根，下面四条路径必须逐一显式指定。
.venv/Scripts/python.exe scripts/demo_check.py --accept `
    --formal-window C:\offline-evidence\formal.json `
    --data-dir C:\offline-evidence\data `
    --log-dir C:\offline-evidence\logs `
    --reports-dir C:\offline-evidence\reports `
    --sessions-config C:\offline-evidence\sessions.toml
$LASTEXITCODE
```

裸 `--accept`、不合法描述、缺显式路径在读取默认 data/config 前拒绝。
`--formal-window` 只能配合 `--accept`；没有 `--start/--end/--now`、批准开关
或环境变量时间覆盖。帮助用单独的 `--help`（单独调用仍退出0）；正式调用中
包括 `--h/--he` 等 argparse 帮助缩写也统一退出2并保留固定结果字段。

| 退出码 | 正式分支含义 |
|---|---|
| 2 | 参数、原件、身份绑定或日历无效/不完整，无法判定 |
| 1 | 材料可解析，但天数、样本不足或有异常/当前保护 |
| 3 | `mechanical_checks_passed=true`，全部机械检查满足 |
| 0 | 正式分支禁止返回 |

所有正式结果固定 `historical_identity_verified=false` 和
`a5_status=not_evaluated`，机械满足不表示阶段 F 验收完成。脚本不写入
`approved_by`。完整历史原件、其同期真实性、真实部署完成时刻和 A5 必须由
独立责任人核实。SHA256 只证明本批输入字节一致，不能证明历史真实性；
**没有留存原件时，禁止用当前 instances 或新写 JSON 补造历史身份。**

### 4.1 固定索引与留存原件

下列是字段结构说明（不是可直接替代原件的示例证据）：

```text
Ref = {path, sha256}
RunBinding = {
  run_index, manifest_snapshot: Ref,
  launcher_snapshot: Ref, instances_snapshot: Ref
}
FormalWindowV1 = {
  schema_version: 1, mode: "Demo", deployment_sha, magics,
  anchor: RunBinding + {
    deployment_claim: {
      source: Ref, source_lines: [first, last], excerpt_sha256,
      completed_utc, processes: [{kind, magic, pid}]
    }
  },
  run_bindings: [RunBinding],
  restart_bindings: [{line_number, before_instances: Ref, after_instances: Ref}],
  calendar: Ref
}
Calendar = {
  schema_version: 1, timezone: "UTC", market: "XAUUSD", source: Ref,
  days: [{date, trading, reason: "weekday"|"weekend"|"holiday",
          source_lines: [first, last]}]
}
```

- 描述/日历/索引严格拒绝未知或重复键。整数不接受 bool；`magics` 为非空、
  不重复正整数；`deployment_sha` 为完整小写 40hex；文件及选段摘要为小写
  64hex SHA256；`run_index` 零基，文本与 journal 物理行号一基。
- `Ref.path` 相对 formal.json 所在目录解析，引用不得逃出该证据根。身份
  原件内部的部署/配置路径只做离线规范化比较，不访问其所指配置或代码。
  对外仅输出逻辑别名/错误码，不转印绝对路径或底层异常。
- 全文摘要来自原文件 bytes。选段摘要严格对源 UTF-8 原 bytes 执行
  `splitlines(keepends=True)`，按 `[first,last]` 一基闭区间取行并连接，
  **保留原 CRLF/LF 和行结束符**后计算 SHA256；不能先解码再规范化换行。
  全文编码错误拒绝。每个实际工件只读一次，同批 bytes 用于哈希与解析，
  同一工件通过别名重复引用时也不得声明冲突哈希。
- anchor 指向留存 manifest 快照最后一项；绑定完整 runner magic 集合、
  risk/reconcile 角色 PID 和同期逐次 launcher 清单/instances。risk 与
  reconcile 的生产 magic 为 null，不冒充某个 runner。部署原文选段必须
  出现完整 SHA、明确 UTC `completed_utc` 及完整 PID 数字词元，与机器原件
  一致；短 SHA、近似时间不能补齐。登记时间不称为独立 OS 创建时间。
- 当前 `data/live/run-manifest.json` 必须保留锚快照整个 runs 内容前缀，
  后续每 run 都有 binding。每条锚后 `data/live/ops-restarts.jsonl` 记录
  都有以物理行号关联的前/后 instances 副本；journal 时间在 launch 前，
  不强求等于新实例启动时间。坏行、缺原件、删改前缀拒绝。每段核对 SHA、
  Demo、部署/数据目录、角色/magic/PID、关键 argv；切到别的身份再切回
  仍拒绝，同 SHA 的 kind=start 或 supervisor 重启不重起算。
- 当前 `data/live/instances.json` 必须与整条链最后状态一致。后移 anchor
  若与部署 PID 或初始材料矛盾则拒绝；全套原件一起被替换，机械比较本身
  无法证明或发现，因此历史未核实/A5 未评估始终不变。

#
**部署核对单（T180 评审 F-8）**：①部署记录里只能出现**恰好一个**完整 SHA（写"从 8a80503 升级到 <新SHA>"含两个 SHA 会被生成器拒绝）；②留存步骤在 skipped_alive_runners（有存活进程未全量启动）时照样成功，但该 run 永不能作锚点——**部署必须先整树停机再启动**；③装了计划任务的，把安装脚本最后打印的 `TASK_PARAMETERS` 行抄进部署记录（T213）。这一行里是本机的绝对路径、任务名和两个时刻字段（`daily_report.local_time`、`weekly_check.utc`）；**抄进之前把路径换成占位符**（`<部署树>` 等），本机路径不入库。

### 4.1.1 运行时自动留存的工件与描述符生成器（T180）

上述原件由运行时自动产生，不靠事后手工复制：

- **部署启动**（`demo_run.ps1 -Mode Demo`，锁内）：把本次
  `run-manifest.json`、本次运行清单（launcher 盘存）、最终 `instances.json`
  原字节复制到 `<DataDir>\live\evidence\run-<序号>\{manifest,launcher,
  instances}.json`（序号＝runs 里本次启动的零基下标；首次部署即 run-0
  锚点件），并在启动输出打印各 sha256 供 phase-f-log 部署记录引用。任一
  复制失败退出 1：进程可能已启动，但**本次启动不能作锚点**，须排查后
  重新部署再起算。PaperSoak 模式不留存（不进正式窗口）。
- **supervisor 重启**（`live_supervisor.py::supervise`，instances.lock 内）：
  重启前把当前 `instances.json` 原字节复制到
  `evidence/instances/<UTC时刻>-<序号>-before.json`，写入新登记后再复制
  `-after.json`（序号＝该次重启在 `ops-restarts.jsonl` 的一基物理行号），
  并把两条 `{path, sha256}` 写入 `evidence/instances/index.jsonl`（seq/utc/
  component 与 journal 行对应）。**journal 行本身保持恰好六字段**——检查器
  正式分支要求行键集合与之逐字相等，把证据字段写进 journal 行会使
  `journal.schema` 退出 2（已实测），因此绑定关系放在 index.jsonl（第十七
  轮计划 T180 第 1 条"写进同一条记录"按此口径落地）。复制失败写告警
  `SUPERVISOR_EVIDENCE` 并**照常重启**（重启优先于证据）；该告警类型不在
  检查器白名单里，出现即让正式检查退出 2——证据不全时这是预期的
  fail-closed。
- **描述符生成器**（`scripts/formal_descriptor.py`）：只从已留存工件
  （run-\*/、evidence/instances/）、`ops-restarts.jsonl`、phase-f-log 部署
  记录行号与日历源文件组装 `formal.json`（同根另写 `calendar.json`），
  任何一项缺失、哈希不符或记录有歧义即报错退出，不补、不猜。用法：

```text
python scripts/formal_descriptor.py --root <离线证据根（含 data/）> \
    --deploy-log <根内 phase-f-log 副本> --deploy-lines <first> <last> \
    --calendar <根内日历源>
```

  部署记录行区间须恰好含**一个完整 40hex SHA、一个显式 UTC 完成时刻**
  （`...Z` 或 `+00:00`）并逐字包含锚点全部启动器 PID；锚点＝记录所列且
  launcher 全角色拉起的唯一 run。日历源每行 `YYYY-MM-DD
  weekday|weekend|holiday [理由]`，须逐自然日覆盖完成日至生成当日，
  缺日即报错。生成与检查同一天执行（检查器另取自身观测时刻）。

### 4.2 日历、事件与当前保护

日历必须从声明部署完成 C 的 UTC 日期逐自然日覆盖到程序一次观测 T 的
日期，含今天、周末和休市日，唯一连续且不缺日。来源选行须有具体日期及
对应 `weekday/weekend/holiday` 词元；holiday 行还必须有具体休市理由
（例如 `2026-12-25 holiday Christmas closure`）。无活动不能推定假日。

S 为首个交易日 00:00 且 S≥C；日中部署跳过当天余段，恰 00:00 可计该日。
未来锚或窗口未开始拒绝。S 至 T **全部已结束交易日**至少十日并全部检查，
不能只选十个有活动的日期；自然日存活要求覆盖首尾、周末与休市。当前日
不强求未到期整日日报，但检查已发生异常及已到期小时心跳（当前小时最初
十分钟保留既有宽限）。每个参与 magic 还必须至少有一条窗内有效自身运行器
日志事件，然后才可用逐日风控心跳作佐证；登记/重启原件或其他策略日志不能
替代这个门槛。十日以后的异常仍会阻断。

成交、orders、alerts、日志、SL/TP、重连/恢复、暂停解除、command/ack 及
归因/滑点引用均先校验自身事件时刻，再按 `[S,T]` 筛选，再派生日/小时/
magic。禁止用 mtime、decision bar、下一条日志时间替代事件时间；正常无
时间的启动头/执行明细仅作元数据，不支持事件或存活证据，无时间异常拒绝。
暂停解除须同 magic；未来恢复不计。两轮全平必须不同 id，各轮全部 magic
关联 ack 满足 `S≤command_ts≤ack_ts≤min(T,command_ts+1小时)`，`closed`
为非负整数且拒 bool；0 仅按既有协议作为该关联 ack 的空仓证明。

当前 pause（含全局 pause/独立 reconcile-block）、pending 出场、意图与
known_positions 差分、state_corrupt、killSwitch、halt、halt-risk-veto
独立检查，窗前创建仍阻断，不依赖活动 latest_day；合法 pending 意图保留
60 秒在途宽限。策略/全局 pause 的未来时刻即使 `paused=false` 也作为已知
当前异常返回1，与未来 state/intent 口径一致，不借另一策略的演练解除放行。
窗口内每个 magic 的最新 `RECONCILE_MISMATCH` 必须有同 magic 的有效后续
解除 `alert_ts < release_ts <= T`；较早、等时、未来或其他 magic 的解除不计。
这项独立于“至少完成一次暂停演练”，已正确解除的历史告警不会永久阻断。

`state/pause/expected/reconcile-block` 策略文件必须使用规范 ASCII
`prefix-{magic}.json` 名称（无前导零）；非规范名字或重复逻辑 magic 退出2，
不能按扫描顺序覆盖原件。坏状态/不可读拒绝；检查只读，不执行恢复或清理。
保留离线原件、描述、命令输出与退出码供独立 A5 审核，不能把 exit3 包装成正式批准。

## 5. 故障恢复（H 线基石阶段 1/2 落地后的新机制）

本节针对阶段 1（交易安全）与阶段 2（平仓安全）引入的持久化机制给出
运维口径。**总原则不变：只移除停机信号、只重启已确认退出的进程；
新增文件的任何异常都优先 fail-closed，宁可停着也不猜。**

### 5.1 execution journal（`data\live\execution-journal-<magic>.jsonl`）

- 每次下单**发送前**追加一行：decision_id / request_uuid / 腿 / 状态
  （pending → sent → known_result）；同一 decision_id 的重发会被 journal
  幂等拒绝——这是"崩溃重启后同一决策不重复开仓"的主防线。
- 崩溃/强杀后重启：runner 先读 journal 的 pending/sent/unknown 行，
  核对券商持仓 + 挂单 + 成交历史。**confirmed 必须有与本请求关联的成交**
  （优先匹配 broker order_ticket，其次匹配本决策唯一 comment）；同
  magic/symbol 的其他成交或单纯出现新持仓，不足以确认本请求已成交。
- **空历史 + 空持仓 + 空挂单不是 rejected 的正面证据**：订单可能已成交
  后又平仓，或历史归档延迟。只有明确 TradeRejectedError 或 §5.4 人工
  仲裁可写 rejected；其他情况无限保持 unknown，暂停开新仓（平仓不受限）。
- MT5 `PLACED` 只代表请求已受理，不代表成交；即使返回 volume 非零，
  也不得记 fill/confirmed。任何本 magic 的未完成挂单（包括旧版本、
  人工或其他路径创建、未关联 journal 的挂单）都会阻止新开仓；平仓/
  全平仍可执行，直到挂单终态得到核实。
- journal 尾部撕裂行（写到一半断电/强杀）按 unknown 处理，程序不 crash；
  不要手工编辑或截断 journal——撕裂行会在下次仲裁/对账中被消化。
- **禁止**删除 journal 来"清掉"待决项：待决项只能通过 §5.4 仲裁工具或
  券商侧核对闭环。

### 5.2 consumed deals（`data\live\consumed-deals-<magic>.jsonl`）

- 每笔**已用于解释持仓变化**的成交（ticket / volume / time / reason）
  的水位文件；部分减量（同 ticket 持仓量减少）同样要消费——旧的部分
  SL deal 永远不允许拿来解释新消失的仓位。
- **丢失或损坏 → fail-closed**：防线（pending exit）视为仍在 + 写告警，
  等同 state_corrupt 处理。恢复动作不是删文件重跑，而是：保持停机/
  暂停开仓状态，先人工核对券商成交历史，再由维护流程显式重建水位并
  留痕（重建期间开仓通道保持关闭）。

### 5.3 跨数据目录进程锁

- 实例锁按 `server + login + magic` 哈希（login 跨券商可撞号，必须带
  server），锁文件位于 OS 状态目录：
  `%LOCALAPPDATA%\mt5-trading-plus\locks\`——**换数据目录（LogDir/
  data root）绕不开**。锁获取时机在网关连接之后（需读 login）。
- 锁冲突 = 同 server+login+magic 已有存活实例：跳过并告警，绝不并行
  第二份（同 magic 双写 state/journal/consumed 会互相覆盖）。
- 进程退出后内核自动释放锁；磁盘上的锁文件可以保留。不得仅凭父 PID
  已退出删除锁来绕过互斥；按 §6.3 核验整棵树，清理前保留维护记录。

### 5.4 人工仲裁工具（`scripts\live_ack_execution.py`）

- 用途：**永久 unknown** 项的唯一出口。程序无法自动判定时（历史窗口
  不足、券商回报缺失），由人工在券商终端核对成交/持仓后，用该工具把
  journal 条目置 confirmed / rejected，操作写回 journal 留痕（含操作者
  与核对依据）。
- 使用前提：先停开仓（killSwitch 或 -Stop），再仲裁；仲裁完成后按 §3
  只重启已退出的进程。
- 严禁用"编辑 jsonl 手改状态"替代该工具——绕过留痕的修改会让对账
  失去依据。

- 仲裁前先读取并校验券商持仓；查询失败时不写 journal 终态，可直接
  修复连接后重试。若 journal 仲裁终态已落但意图文件写入失败，只允许用
  **相同 decision_id + 相同 status + 相同 reason** 重试补写意图；不同
  结论/原因或普通执行终态均拒绝改写。工具会在意图成功恢复后才写
  ACK_EXECUTION 告警。

### 5.5 重启对账三步（每次崩溃恢复自动执行，人工复核同序）

1. journal 待决项（pending/sent/unknown）逐条核对券商实际持仓/挂单/
   成交历史；
2. expected（`expected-<magic>.json`）vs 券商实际持仓比对——expected
   损坏/缺失时从 journal + 券商实际持仓重建，并写差异告警；
3. 差异写 pause（暂停开仓），人工按 §5.4 闭环后才恢复。

### 5.6 维护窗口刷新授权快照（3A 后续，R8）

快照链路已支持 `currency_base`（基础币种）字段：mode=2 隔夜费校验
只在快照带该字段时才能给出确定判定，**旧快照（无该字段）会被显式
拒绝而不是退回利润币种代算**。因此下一次维护窗口应重跑
`scripts/snapshot_specs.py` 刷新授权快照（`data\specs\latest.json`），
并用 `snapshot_specs.py` 的 diff 输出确认只新增了 `currency_base` 一类
字段、其余合约参数无漂移；刷新前后各留一份 history 版本供审计。

两份已授权快照也可交给 §6.2 的 `t111b_prepare.py` 离线核对：只接受当前
公开的品种名→字段对象结构，原字段及类型须保持一致，新增 `currency_base`
须为非空字符串。唯一不参与参数比较的元数据是 `captured_at_utc`（沿用
`mt5gw.snapshot.diff_snapshots` 契约）；重复键、未知字段、空快照均拒绝。

## 6. T110 超时防护与外部 supervisor（维护窗口部署）

### 6.1 完成心跳、异常证据与边界

网关所有 MT5 调用（包括 initialize、terminal_info、account_info、last_error、
shutdown 和下单）由同一个专用 worker 执行，锁等待也有 deadline。网关配置
可设置有限正数 `query_timeout_s`（默认 30 秒）、`order_timeout_s`（默认
60 秒）。交易的账户核验、保险丝、发送与 last_error 仍在同一串行原子区内。
超时输出 CRITICAL，写 `gateway-stall.json` 与 `exit-<component>.json`，
以 **rc75** 硬退出；在途交易证据按进程登记，任一线程/连接先超时都尝试把
全部已登记交易 journal 记为 unknown，expected 保持 pending。首次超时负责
落证据，其他超时线程不抢先退出；正常结束的证据登记立即移除。恢复仍按
§5 核对，超时不证明订单未成交。

三个角色均启用 faulthandler，逐轮重置堆栈 watchdog，stderr 落对应 `.err`；
其他不可恢复异常输出 CRITICAL + traceback 并写 exit marker（rc、原因、时间）。
完成心跳分别为 `heartbeat-<magic>.json`、`heartbeat-risk.json`、
`heartbeat-reconcile.json`，含 PID 与 deployment_sha。数据读取失败不刷新
完成心跳；risk/reconcile 的 DataUnavailableError（账户/持仓取数失败）按
T184 独立时间窗计——段首/恢复各一条告警，连续 `--account-unavailable-limit-s`
（缺省 300 秒）才以 79 退出并写 halt；文件访问类（PermissionError/
TimeoutError）连续 5 轮、其他异常停机退出（断线走 T141 的 15 分钟窗/78）。
heartbeat open PermissionError 最多重试 3 次（间隔 50ms），
持续失败保留 traceback 和退出证据。

RiskGate 在组装账户快照前检查 halt/killSwitch 与独立 risk 心跳；缺失、未来、
损坏或超过 `max(120s, 3×interval)` 即拒开仓并告警，纯平仓不经开仓闸门。
健康检查在读取心跳（supervisor 同时读取日志 mtime）后取观测时钟，与账户
快照/决策截止时间分离；读取期间新写入的心跳不误判未来，真正未来仍拒绝（这句说的是心跳；
日志的修改时间另有不超过 2 秒的容差，见 §6.3）。
当前部署固定 risk interval=10s，reconcile=30s，runner=2s。risk/reconcile
角色文件不计入策略登记。周末报价陈旧但循环完整执行时照常更新心跳和周期
健康日志，不能用“无决策/无 tick/空 .err”判定冻结。

Python Future timeout、Timer 和 faulthandler **不能保证在原生扩展持续持有
GIL 时继续推进**；外部 supervisor 才是硬卡死兜底。超时证据写入另有 5 秒
退出兜底，磁盘不可写时可能只留下 stderr 或发送前 pending journal；不得
把“无 marker”解释为健康。网络、磁盘或 OS 本身整体失去响应也超出本机
supervisor 的保证。历史故障原因以历史取证记录为准，不能据本次修复统一
归因为 heartbeat PermissionError。

### 6.2 固定部署和统一路径

在维护窗口准备**固定完整 SHA 的 detached、干净、隔离部署目录**，并明确
同一个 DataDir、ConfigDir、PythonExe、GatewayConfig、RiskConfig、
SessionsConfig、SpreadModel、LogDir。部署代码目录可与数据目录不同；
`demo_run.ps1 -DataDir` 会显式传给三个角色，runner 同时覆盖配置中的
`data_root` 与自定义 heartbeat_path。网关预检和三角色使用同一个
`-GatewayConfig`，所有 Python 子进程均为 `-u`。

已有 release 目录和两份独立授权快照可先运行纯离线前置检查（占位值须替换，
工具不创建部署目录、不读取部署配置内容、不执行输入脚本或运行时步骤）：

```powershell
python scripts/t111b_prepare.py --release-dir '<existing-release-tree>' `
  --expected-sha '<40-character-lowercase-fixed-sha>' `
  --before-snapshot '<authorized-before.json>' --after-snapshot '<authorized-after.json>'
```

JSON/stdout 确定性输出固定 SHA、detached/clean、三角色必需脚本的相对名与
SHA-256、快照文件 SHA-256 和允许差异计数；检查失败退出 1。整树索引存在
`assume-unchanged` 或 `skip-worktree` 标志即拒绝（含非入口模块及未改动文件），
输出 `release_hidden_index_flags` 且 `clean=false`，不自动清除标志。
三个明确入口 `live_reconcile.py`、`live_runner.py`、`risk_process.py` 须匹配
HEAD blob；仅允许这些 UTF-8 文本脚本的统一 CRLF 工作树对应 LF blob，输出
SHA-256 始终取实际文件字节。不放宽混合换行、裸 CR、编码/BOM、空白或实质
内容变化，不调用 clean/smudge/process 过滤器。其他文件不适用该身份等价例外。
CLI 缺参数、未知选项/值及参数格式解析错误退出 2，stdout 固定输出
`{"errors":["cli_arguments_invalid"],"schema_version":1,"static_checks_passed":false}`，
stderr 为空，不回显参数、路径或原始错误；正常 `--help` 仍显示帮助并退出 0。
过深 JSON 在解析阶段按 `snapshot_before_invalid_json` 或
`snapshot_after_invalid_json` 稳定失败，不输出回溯。
可重复提供 `--path 'NAME=<existing-absolute-path>'`，NAME 限于 `data_dir`、`config_dir`、
`python`、`gateway_config`、`risk_config`、`sessions_config`、`spread_model`、
`log_dir`，只核对存在性和文件/目录类型，输出不包含路径值。未提供的项目列入
`static_prerequisites_pending`；`static_checks_passed=true` 仅指已执行的检查，
缺路径时 `deployment_prerequisites_complete=false`，不表示完整发布前置通过。
即使全部路径存在，也未验证配置内容或 DEMO 保险丝；`runtime_checks_pending`
始终保留维护窗口批准、真实健康/进程、授权及部署/正式窗事项，不能据此批准 U3。

示例仅为维护窗口操作模板（先替换占位值，**开发/测试不执行**）

```powershell
$deploy = (Resolve-Path '.\deployments\<fixed-sha>').Path
$data = (Resolve-Path '.\data').Path
$configs = (Resolve-Path '.\data\demo-configs').Path
$python = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$gateway = (Resolve-Path '.\gateway.json').Path
$risk = (Resolve-Path '.\config\risk.toml').Path   # 日常与正式窗口都用它；risk-demo.toml（日亏上限 0.5 USD）只用于全平演练
$sessions = (Resolve-Path '.\config\sessions.toml').Path
$spread = (Resolve-Path '.\data\costs\spread_model.json').Path
$research = (Resolve-Path '.\data_research\costs\spread_model.json').Path
$logs = (Resolve-Path '.\data\demo').Path
$sha = '<40-character-fixed-sha>'

# 维护窗口，旧进程整棵树已核验退出后首次启动。
# 不删除 journal/consumed/expected；旧登记缺 T110 身份字段时须人工迁移核验。
& (Join-Path $deploy 'scripts\demo_run.ps1') -Mode Demo `
  -DeploymentSha $sha -DataDir $data -ConfigDir $configs -PythonExe $python `
  -GatewayConfig $gateway -RiskConfig $risk -SessionsConfig $sessions `
  -SpreadModel $spread -ResearchCache $research -LogDir $logs
```

Demo 启动会在锁内自动留存锚点证据（`evidence\run-<序号>\` 三副本，§4.1.1）
并打印各 sha256；留存失败退出 1，本次启动不能作锚点。随后写进 phase-f-log
的部署记录必须能让 `formal_descriptor.py` 机械解析：记录行内恰好一个完整
SHA、一个显式 UTC 完成时刻（如 `2026-11-12T04:35:12Z`），并逐字写出
instances.json 登记的全部启动器 PID（解释器 PID 另列并注明口径，不算入
锚点 PID）。

supervisor 拒绝非 Demo/实盘解锁配置、缺失或损坏登记、不符 SHA/路径/参数、
PID 创建时间不符、未知子进程或缺 launcher 但 worker 仍活着的情况：写 halt
与 SUPERVISOR_REFUSED，留给人工核验，不猜测身份。PID、创建时间、脚本及
全部参数匹配后才核验 venv 父子树；停止按子进程到 launcher 顺序执行，
使用绑定创建时间的 Windows 进程句柄防 PID 复用竞态。启动器与 supervisor
共用 `instances.lock`（Windows FileShare.None），核验/停止/启动/登记互斥。

### 6.3 每分钟计划任务安装、卸载和人工停机

**身份校验的失效模式表（T211，2026-11-23）**。supervisor 只重启"干净、钉死"的部署里的角色；
进程树里有它不认识的进程就拒绝，并且不结束任何没有通过校验的进程。下表每一行对应
`tests/test_t211_supervisor_identity.py` 里的一个用例（第 12、13 行对应其中走 `supervise()` 的用例）：

| # | 进程形态 | 结果 |
| --- | --- | --- |
| 1 | 启动器 + 解释器 worker | 通过 |
| 2 | 形态 1，启动器或 worker 下挂一个系统 conhost 叶子 | 通过；conhost 不进终止清单 |
| 3 | 形态 2，另有进程的 `parent_pid` 指向 conhost，但创建时间早于 conhost | 通过（陈旧父链接） |
| 4 | conhost 下有创建时间不早于它的子进程 | 拒绝 |
| 5 | 不在系统目录的 `conhost.exe` | 拒绝 |
| 6 | 启动器已退出，只剩它的系统 conhost | 视为缺失，可重启 |
| 7 | 启动器已退出，PID 上是创建时间不符且参数不符的进程 | 视为缺失，可重启 |
| 8 | 启动器 PID 上是参数相符、创建时间不符的进程 | 拒绝 |
| 9 | 启动器已退出，worker 还在 | 拒绝 |
| 10 | worker 下有未知子进程，包括 terminal64.exe | 拒绝；被拒绝点名的进程是终端时，另发 `SUPERVISOR_TERMINAL_CHILD` |
| 11 | 登记之外有参数相符的 worker | 拒绝 |
| 12 | supervisor 的 halt 在场，同时有 `halt-risk-veto.json` 或 killSwitch | 不自动解除；`supervisor-state.json` 记 `clear_blocked`，人工核对后移除 |
| 13 | 角色被判 `heartbeat_or_log_stale` | 写 halt 并重启；`SUPERVISOR` 告警的 `checks=` 和 `supervisor-state.json` 的 `stale_checks` 列出该角色每条子条件的取值（通过的也在内） |

"系统 conhost"指映像的完整路径等于 `<SystemRoot>\System32\conhost.exe`（按 `ntpath` 归一大小写和分隔符后
比较）；系统根目录取不到时不放过任何进程。第 7 行的孤儿判断：`parent_pid` 等于该 PID、创建时间落在
"登记时间减 1 秒"到"占用者创建时间"之间的进程，仍按孤儿处理（拒绝）；创建时间不早于占用者的，是占用者
自己的子进程，不算。拒绝原因带角色、PID、映像文件名、父 PID，写进 `SUPERVISOR_REFUSED` 告警和
`supervisor-state.json` 的 `last_refusal`。两条走生产启动路径的测试（`WindowsProcesses.launch()` 和
`demo_run.ps1` 的 `Start-Process -WindowStyle Hidden`）在 `tests/test_t110_supervisor.py` 里，都先断言快照里
确实有以该角色为父的 conhost，否则按失败处理并打印进程树。

**日志年龄的 2 秒容差（2026-11-24）**：第 13 行里"日志年龄"这一条原来是 `0 <= age <= 超时`。角色刚写完日志时，
文件的最后写入时间可以比 supervisor 紧随其后读到的时钟略微超前（最多一个时钟步长，本机约 1 毫秒；量到的幅度约
1 微秒），`age` 是一个很小的负数，健康的角色就被判成陈旧：写 halt、重启。现在的条件是 `-2 秒 <= age <= 超时`
（`LOG_AGE_TOLERANCE_S`）——日志时间"来自未来"不超过 2 秒算健康，超过照旧按不健康处理。`checks=` 里的
`log_age_s=` 照实记，负值也记。心跳那一侧"来自未来"的判定没有动。

**"无法判定"不是拒绝（T212，2026-11-23）**：核验部署时 git 超时或起不来，这一轮记为无法判定——发
`SUPERVISOR_DEFERRED`（记录类），不重启任何角色。只读的进程树和心跳检查照常：四个角色都健康就不写 halt；
有角色缺失或失活照旧写 halt，重启留到核验成功的那一轮。无法判定期间也不解除已有的 halt。SHA 不符、工作树
不干净、非 Demo 配置照旧是拒绝。角色缺失或失活、而部署不干净要到重启前的完整核验才发现时，halt 的原因是
`risk_lost` 或 `component_lost`，"不干净"这个原因写在 `SUPERVISOR_REFUSED` 告警和 `supervisor-state.json` 的
`last_refusal` 里。

计数与升级：一轮里只要有一次核验无法判定——本轮开头那一次，或者重启前的完整核验——这一轮就计一次；连续
三轮无法判定，第三轮升级为拒绝（发 `SUPERVISOR_REFUSED`，异常类）。`supervisor-state.json` 的
`undeterminable` 记这个连续轮数：沿用缓存的"干净"结论本身既不计数也不清零，只有整轮都没有出现无法判定
才归零。升级时如果还没有 halt，写 halt `supervisor_refused`；已经有 halt（不论是 supervisor 早先或这一轮
写的，还是别的进程写的），保留它原有的原因、owner 和 token，不改写成 `supervisor_refused`。升级之后计数
从 1 重新开始：git 一直不回答时，告警按"推迟、推迟、拒绝"每三轮循环一次，其中拒绝是异常类。

开销与缓存：SHA 和 detached 状态每轮都查。`.git/HEAD` 的内容和索引文件的大小、修改时间都没变时，沿用上次
"干净"的结论（链接工作树按 `.git` 文件里的 `gitdir:` 找到真正的 git 目录），不需要重启的轮次里完整的
`git status` 至多 15 分钟一次。**要重启任何角色的那一轮，先做一次完整核验，不用缓存**。

缓存的盲区：上一次完整核验之后的 15 分钟内，新增的未跟踪文件、已跟踪文件的未提交改动（包括删除）都不会被
发现；要到下一次完整核验才发现，届时按拒绝处理（发 `SUPERVISOR_REFUSED`，之前没有 halt 时写
halt `supervisor_refused`）。`git add` 会改写索引，指纹随之改变，下一轮就做完整核验并发现。这段时间里，
已有 halt 的自动解除沿用的是缓存的"干净"结论。重启不受这个盲区影响：要重启角色的那一轮一定先做完整
核验，部署不干净就拒绝，不重启。

git 和 powershell 都按绝对路径调用。git 找不到按无法判定处理。powershell 找不到是**拒绝**：没有进程清点
就不知道各角色是否健康，按拒绝处理才稳妥（发 `SUPERVISOR_REFUSED`，之前没有 halt 时写 halt
`supervisor_refused`）。

以上各段写于 2026-11-23；其中计数与升级的口径、缓存的盲区、powershell 找不到的处理，是同日独立评审
（`docs/reviews/2026-11-23-t212-review.md`）之后修订的。

**安装方式**：任务经树内的隐藏启动器 `scripts\ops\hidden_task.vbs` 和包装脚本 `scripts\ops\supervisor_once.ps1` 调用
部署树的 `live_supervisor.py`，用下面的注册命令。11-19 到 11-23 用的树外 `host.py` 包装是临时补丁，随新候选部署时
撤除；留档见 `docs/ops/ops-patches/2026-11-19/`。撤除之前，生产上的观察运行继续用包装链，不对它热改。

沿用 §6.2 的变量，仅在维护窗口明确批准后安装。任务只运行一次 supervisor，
由 Windows 每分钟触发；不要在本次开发验证中安装或执行真实业务动作。
§6.2 的变量不能以反斜杠结尾（启动器以退出码 5 拒绝），也不能含 `%`（退出码 4）。也不能含双引号：它到不了启动器（WSH 解析命令行时就把引号吃掉了，参数会变样），所以任务参数里的路径不带双引号，见 §6.3 里启动器的退出码那一条。

```powershell
# 手工的 --inspect / --stop 直接调解释器；任务走下面的隐藏启动器。
$supervisor = Join-Path $deploy 'scripts\live_supervisor.py'
$argv = @('-u', $supervisor, '--directory', $deploy, '--sha', $sha,
  '--data-dir', $data, '--config-dir', $configs, '--python', $python,
  '--gateway-config', $gateway, '--risk-config', $risk,
  '--sessions-config', $sessions, '--spread-model', $spread, '--log-dir', $logs)

# 注册：wscript.exe → scripts\ops\hidden_task.vbs → scripts\ops\supervisor_once.ps1
# → 解释器跑 live_supervisor.py。包装脚本的参数和 supervisor 的选项一一对应。
$launcher = Join-Path $deploy 'scripts\ops\hidden_task.vbs'
$once = Join-Path $deploy 'scripts\ops\supervisor_once.ps1'
$named = [ordered]@{ Directory = $deploy; Sha = $sha; DataDir = $data; ConfigDir = $configs
  Python = $python; GatewayConfig = $gateway; RiskConfig = $risk
  SessionsConfig = $sessions; SpreadModel = $spread; LogDir = $logs }
$arguments = "//B //Nologo `"$launcher`" `"$once`"" +
  (($named.GetEnumerator() | ForEach-Object { " -$($_.Key) `"$($_.Value)`"" }) -join '')
$action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\wscript.exe') `
  -Argument $arguments -WorkingDirectory $deploy
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
# 主体显式给出，和 §6.5 的安装脚本同一种写法：不给的话运行级别是 cmdlet 的缺省值，和看护任务不一致。
$principal = New-ScheduledTaskPrincipal `
  -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
  -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskPath '\MT5Quant\' -TaskName 'live_supervisor2' `
  -Action $action -Trigger $trigger -Settings $settings -Principal $principal

# 部署记录要抄的（见下）：登录类型、运行级别、所在会话、启动器和包装脚本的 sha256。
(Get-ScheduledTask -TaskPath '\MT5Quant\' -TaskName 'live_supervisor2').Principal |
  Select-Object LogonType, RunLevel, UserId
"session of this PowerShell: $((Get-Process -Id $PID).SessionId)"
Get-FileHash -LiteralPath $launcher, $once -Algorithm SHA256 | Select-Object Hash, Path

# 计划内停机：先停用两个任务，防 supervisor 按职责补回缺失进程，防看护把要关的终端拉起来。
# 顺序是先停用看护任务，再停 supervisor，最后才关终端（下面）。
Disable-ScheduledTask -TaskPath '\MT5Quant\' -TaskName 'terminal_keeper'
Disable-ScheduledTask -TaskPath '\MT5Quant\' -TaskName 'live_supervisor2'
# 然后设置 killSwitch，核验并停止整个父子树；仅杀 venv launcher 不够。
New-Item -ItemType File -Force (Join-Path $data 'live\killSwitch')
# $argv 已含解释器之后的脚本及全部固定部署参数。
& $python @argv --inspect
if ($LASTEXITCODE -ne 0) { throw 'identity verification refused; keep halted' }
& $python @argv --stop
if ($LASTEXITCODE -ne 0) { throw 'tree stop incomplete; preserve registry and investigate' }
# Stop 保留登记；不要自动删除 halt/killSwitch/journal。
# 卸载（不改变业务进程、halt、killSwitch 或历史证据）
Unregister-ScheduledTask -TaskPath '\MT5Quant\' -TaskName 'live_supervisor2' -Confirm:$false
```

**为什么经启动器**（2026-11-24）：任务的动作如果是控制台程序（`python.exe`、`powershell.exe`，哪怕带
`-WindowStyle Hidden`），每分钟触发一次就在交互桌面上弹一次控制台窗口。`wscript.exe` 没有控制台；
`hidden_task.vbs` 用 `WScript.Shell.Run` 以隐藏窗口启动 PowerShell、等它结束、把退出码原样交回，所以任务的
"上次运行结果"就是 supervisor 的退出码，"不启动新实例"（IgnoreNew）看到的也是真正的运行时间。11-19 到 11-23
的树外包装就是为这件事加的；现在启动器和包装脚本都在部署树里，随定版 SHA 一起固定，包装脚本和任务参数里
不需要工作区路径（D112）。

- **包装脚本**（`scripts\ops\supervisor_once.ps1`）：参数和 `live_supervisor.py` 的选项一一对应，原样传过去；
  自己切到部署目录再调解释器（任务的工作目录经 wscript 之后不再保证）。
- **日志**：supervisor 的标准输出和标准错误按解释器写出的字节追加到
  `<数据目录>\ops-evidence\supervisor\supervisor.log`，每次运行是一行 `start`、输出（先标准输出、后标准错误）、
  一行 `exit <退出码>`。超过 5 MB 转成 `supervisor.log.1`，只留一代。编码是解释器重定向输出时自己用的那一种
  （系统 ANSI 代码页；本机是 UTF-8），包装脚本不转码。告警照旧在 `alerts.log`，这个日志是补充；崩溃回溯走标准
  错误，只有在这里看得到。
- **退出码**：0 和 2 是 supervisor 自己的（2 是拒绝，原因在 `alerts.log` 的 `SUPERVISOR_REFUSED` 和
  `supervisor-state.json`）；其他非零值多半是它自己崩溃，回溯在 `supervisor.log`。3 是包装脚本没能运行
  supervisor，原因也在 `supervisor.log`。启动器自己的：2 没有参数，3 脚本不存在，4 参数里有 `%`，5 参数以反斜杠
  结尾，6 启动器自己的语句失败或脚本没能启动。带 `%` 或以反斜杠结尾的参数一律不传：`WScript.Shell.Run` 会展开
  `%名字%`，末尾的反斜杠会让收尾的引号被当成转义。双引号没有退出码：它到不了启动器——WSH 解析命令行时就把引号
  吃掉了，参数传过去会被截断或变样（实测 `a"b` 到了脚本里是 `ab`）。所以任务参数里的路径不能带双引号，要拒在
  引号还看得见的地方：装任务的入口（§6.5 的安装脚本拒绝带双引号的路径）。
- **部署记录里要抄的**（D112）：任务的登录类型、运行级别和所在会话，动作（`wscript.exe`、启动器和包装脚本的
  路径，抄之前换成占位符），两者的 sha256。sha256 是 64 位十六进制，描述符生成器找的是恰好 40 位的 SHA，
  不会被数成第二个；记录行里照旧只写一个完整的 UTC 时刻。上面那条命令还会把账户名（`UserId`）打在屏幕上：
  **账户名只在本机核对，不抄进入库的部署记录**（卫生扫描的口径），要在本机留档就放进不入库的文件。
- **计划内停机期间这两类通知是预期的**：停机先停用看护任务，再停 supervisor，最后才关终端（上面代码块
  里的顺序）。装了带 `--expect-keeper` 的小时任务（§6.6）之后，停机期间存活检查会报 `keeper_stale`——
  状态文件超过 10 分钟就是生产级，第一次看到就通知；终端关着、运行器在等它的时候，还会报
  `terminal_absent:<运行器>`。看到这两条，先核对是不是计划内停机。
- **解释器路径**（`-Python`）是显式参数，现在指向工作区里的 `.venv`。D112 要求正式窗口起算前把生产的解释器、
  部署树、数据、配置、日志全部移出 agent 工作区（T215）；移出之后重新注册任务。
- **不弹窗要人工确认一次**：装完之后等下一次触发，确认交互桌面上没有窗口出现，结论写进部署记录。

supervisor 发现 risk 丢失时先写自己所有的 halt 与告警；发现缺失/停止推进的
角色时仅重启该角色，追加 `ops-restarts.jsonl`（utc、component、reason_code、
old_pid、new_pid、deployment_sha——恰好六字段，正式检查器按行键集合逐字
校验），同时在同一临界区内留存重启前/后的 instances 原字节副本并把
`{path, sha256}` 绑定写入 `evidence/instances/index.jsonl`（§4.1.1；T180）。
留存失败写告警 `SUPERVISOR_EVIDENCE` 并照常重启——重启优先于证据，但该次
重启的副本缺失会让描述符生成与正式检查如实失败，须按 §5 证据缺失处置。
不清运行窗。失败重启会在下次任务继续
核验，身份不确定时拒绝。人工停机后应保持任务禁用，直至维护完成。

自动解除只处理 `owner=supervisor` 且 token 未变化的 halt；要求三角色完成
心跳/周期日志连续新鲜至少 5 分钟、每次检查间隔不超过 90 秒、reconcile
确认无差异且无在途 pending。任何重启或中断重置计时。killSwitch、人工
halt、风险 halt 从不自动删除。若 supervisor halt 已存在时又触发独立风险
停机，保留首次原因/owner，同时写 `halt-risk-veto.json`；该 veto 只能经人工
核对原因、确认风险消除后移除，不可为恢复自动开仓直接删它。
**清除 halt 时要一并核对 `halt-risk-veto.json`**：它留着，supervisor 以后写的任何 halt 都不会
自动解除（`supervisor-state.json` 里出现 `clear_blocked`）；11-23 实测因此停了 3 小时 24 分。

### 6.3.1 日报与周检任务的安装和运行（T213）

**安装**：部署树是纯检出，没有 `.venv`，也不放运行数据。`setup_scheduled_tasks.ps1`
从部署树运行，解释器和各目录用参数显式给出，与 `demo_run.ps1` 的部署参数逐项一致
（变量沿用 §6.2，都给绝对路径：相对路径按启动安装脚本的那个进程的当前目录解析，既不是
PowerShell 的当前位置，也不是部署树。会话配置用运行器同一个 `$sessions`——日报的换日窗
必须和运行器同一个定义源）：

```powershell
powershell -ExecutionPolicy Bypass -File "$deploy\scripts\setup_scheduled_tasks.ps1" `
  -Mode Demo -PythonExe $python -GatewayConfig $gateway `
  -DataDir $data -RunnerLogDir $logs -ReportsDir "$data\reports" `
  -SpreadModel $spread -SessionsConfig $sessions
```

不给的参数沿用部署树下的旧位置。安装之前先核对任务命令要用的文件都在：解释器、会话配置、
点差模型，Demo 模式还有网关配置（部署树下的 `config.json`、`gateway.json` 或
`-GatewayConfig`）。缺哪一项就拒绝安装并说明缺什么，**不建目录、不写包装器、不建任务**。
不带 `-IncludeDataTasks`（D82）。

两份包装器（`.cmd`）由 cmd.exe 按控制台代码页读取，新开控制台的缺省值是系统 OEM 代码页。
安装脚本在建目录之前读出系统的 ANSI 和 OEM 两个代码页，分两种情形，都在建目录、建任务之前
判断：

- 两个代码页相同：包装器按这个代码页写；任何路径里有它表示不了的字符，就拒绝安装并说明是
  哪个路径。
- 两个代码页不同：这台机器上带非 ASCII 字符的路径装出来的任务跑不起来，脚本不去猜哪种编码
  对 cmd 有效——路径里只要有一个非 ASCII 字符就拒绝安装；纯 ASCII 路径照常安装。

生产根目录的名字建议只用 ASCII 字符。

路径参数末尾的分隔符（`\` 或 `/`）在写进包装器之前去掉：包装器里的路径都带引号，
写成 `"…\data\"` 时 Python 解析命令行把 `\"` 当成转义的引号，这个参数和下一个并成一个。
作为参数的三个目录（`-DataDir`、`-RunnerLogDir`、`-ReportsDir`）不能是磁盘根目录
（根目录的分隔符去不掉）。**路径里有 `%` 的拒绝安装**：cmd 读 `.cmd` 时把两个 `%`
之间的内容当成变量展开，装出来的任务跑不起来。

安装脚本最后打印一行 `TASK_PARAMETERS {…}`，抄进部署记录（§4 部署核对单第③条；抄之前路径
要换成占位符）。

11-17 到 11-23 的教训：任务命令写的是部署树里不存在的 `.venv\Scripts\python.exe`，日报和
周检每次都以 rc=1 失败（`The system cannot find the path specified.`），Python 根本没有启动。

**日报的运行**（`live_daily_report.py`）：

- `--date` 当天总是重新生成；顺带检查它之前两天的日报（`--backfill-days`，默认 3），
  缺哪天补哪天，已有的不覆盖。补生成的日报写实际的生成时刻；它的"账户""心跳"两行也是
  生成时刻的快照，不是所报告那一天的情况，正文里没有另外的标注。
- **每一次尝试开始之前都判换日窗**，每个目标日的第一次尝试和补生成的日子都一样：开始的
  时刻不在窗内就不连接，那一天的日报记为未生成（D82）。窗的定义源是 `--sessions-config`
  （`sessions.toml`），不给就用 `SessionPolicy` 的缺省窗。`--paper-state` 只读影子状态、
  不连终端，不受此限。用法和配置问题在判窗之前报告，所以窗外运行也照常看得到。
- **任务不设"错过后尽快运行"**（`schtasks /Create` 不带 `/Z`；安装脚本就是这么写的，
  不要手工加）。任务晚启动时脚本照样按换日窗判：窗外不连接终端，这几天的日报记为
  未尝试，以未完成退出（退出码 3）并发一条 `DAILY_REPORT_FAILED`；缺的日报由次日
  窗内的运行补生成。
- 连接失败或取数失败时重试，至多 3 次（`--attempts`），间隔 300 秒（`--retry-interval-s`）。
  重试按它真正开始的时刻判窗：睡醒之后已在窗外，就不睡、不再试。
- **夏令时不重试，这是已知的限制**。换日窗全年的交集只有本机 05:55–06:00：冬令时窗到
  07:00，05:57 启动可以做满 3 次（05:57、06:02、06:07）；夏令时窗到 06:00，05:57 启动后距
  关窗只剩 3 分钟，放不下间隔 5 分钟的第二次，只尝试 1 次。逐日核对 2026-11-01 至
  2027-10-31：冬令时 133 天都能做满 3 次，夏令时 232 天只有 1 次——**每年约 232 天脚本内
  不重试**，当天失败要等到次日 05:57 补生成。第二十三轮施工计划 §2.4 第 2 条写的"共 3 次、
  间隔 5 分钟、都在换日窗内"，在夏令时只有"都在换日窗内"成立（间隔仍是 5 分钟，但用不上），
  这一条只算部分完成。固定的任务时刻下三者不能同时满足；实现保持保守方案（以 D82 为准，
  任务时刻和间隔都不改）。不改任务时刻、不放宽 D82 而要全年做满 3 次，只能把间隔缩到
  89 秒以内（例如 60 秒），那要另行决定。
- 一次运行结束时有日报没生成：发一条 `DAILY_REPORT_FAILED` 告警（记录类），退出码 3。
  告警按日写实际尝试的次数，没有尝试的写原因（换日窗外）。**其他失败也发这一种告警**，
  每次运行最多一条，消息里写明是哪一类和退出码：配置或用法错误（点差模型坏了、会话配置
  缺失或读不了、缺网关配置、账户不是 Demo；退出码 2，不重试）；未预期异常和日报写盘失败
  （退出码 1，照旧带回溯）；网关期限（退出码 75，进程由网关直接结束，告警在它退出之前
  写）。`live` 目录不存在时不写告警，也不为写告警建目录。日报是否齐全照旧由正式检查原有
  的规则判。
- 网关期限（退出码 75）时，网关把标记文件 `gateway-stall.json`、`exit-gateway.json` 写到
  当前目录的 `data\live` 下。日报包装器先 `cd` 到部署树，没有设 `MT5_LIVE_DIR`，所以部署树
  和数据目录分开时，这两个文件落在部署树的 `data\live`，不在 `-DataDir` 里。

### 6.4 验收与部署边界

`demo_check` 可用 `ops-restarts.jsonl` 解释相应运行器重启，但不会用它补齐
日志/心跳空洞，也不会据重启记录证明连续存活。逐小时日志验收仍是有限证据，
不能替代实际故障演练。离线 blocked-fake、未知订单、PID 复用/父子树、健康
窗口与 PowerShell 接线测试通过，仅说明代码契约可验证；**CI、真实 Demo
断线/硬卡死/重启演练和计划任务安装均待主控在维护窗口单独执行**。

### 6.5 终端看护与网关不拉起终端（T209，第二十三轮）

**规则**。交易终端只由看护任务启动，或由维护窗内的人工操作启动。worker
（运行器、风控、对账）和所有一次性脚本（日报等）走的是同一个网关：调
`initialize` 之前先在进程内查终端进程在不在（按网关配置的 `terminal_path`
比对映像路径；读进程表用 ctypes，路径比较经 `os.path.realpath` 读文件系统，
都不起子进程）。不在就不调 `initialize`，抛
`TerminalNotConnectedError`——真实的 MetaTrader5 包会在 `initialize` 里把终端
拉起来，终端从此是 worker 的子进程，supervisor 拒绝这棵进程树（11-21 的
684 分钟）。

**读不到终端进程也算"不在"**。探针打不开那个进程（权限、完整性级别不同）、
或者查进程表自己出错时，都报"不在"。只有探针把异常抛出来，告警文字里才带
"探测出错"和异常类名；真实探针把打不开进程、查进程表出错都自己在内部接住，所以
用真实探针时这几个字实际上不会出现，报的就是普通的"终端进程不在"，**和终端真的
不在分不出来**。这种"不在"不会自愈：每次连接和重连都被拒，15 分钟后以 78 退出，
supervisor 重启它，从头再来；同一时刻看护脚本报的是"在运行"。
**所以 worker 的运行账户也要能读到终端进程的映像路径**——和看护任务用同一个
账户、同一个会话最稳妥。部署后头几轮看到"终端进程不在"而终端明明开着，先查
这一条。

**启动时终端还没起来**。按断线处理（T141）：进程每轮写心跳、留 DISCONNECTED
段，不写 fatal halt；连续 15 分钟连不上以退出码 78 结束，交给 supervisor。全灭之后
不需要人工清 halt。等待除了看时钟还有次数上限（上限时长 ÷ 间隔 + 2 次），时钟
不走时也会停下。`--once` 启动连不上同样会等满 15 分钟，不是立即失败。

**没有看护任务时的表现**（新代码已部署、看护任务没装）：终端不在，角色各等
15 分钟、以 78 退出，supervisor 重启它们，循环下去，不写 halt。心跳一直是新鲜的，
所以"角色消失超过 10 分钟"这类检查不会触发。这比旧行为更安静，所以看护任务必须
和新候选一起部署。

**终端不允许算法交易**（算法交易按钮被关，或经销商禁用了 API）。运行器每个
决策轮读一次 `terminal_info()`；不允许时本轮不决策、不开仓，心跳照写，日志写
`algo_trading_disabled`。状态变化各发一条告警：`ALGO_TRADING_DISABLED`（正式
检查里是异常类）、`ALGO_TRADING_RESTORED`（记录类）。恢复后下一轮照常。
生产包的 `terminal_info()` 带不带这两个字段，这个项目还没有读过实测值——影子盘、
干跑、离线模式都不走这条路，PaperSoak 确认不了。按 D104 的例外，在观察运行停下
之后、第一个 Demo 进程起来之前，选一次没有业务进程连着终端的维护窗，主会话
一次性读终端状态，把两个实测值写进部署记录。缺字段时运行器按"不允许"处理，
第一轮就发 `ALGO_TRADING_DISABLED`。

**看护脚本** `scripts\ops\terminal_keeper.ps1`：

```powershell
# 只看判断结果，不启动
powershell -ExecutionPolicy Bypass -File scripts\ops\terminal_keeper.ps1 -TerminalPath "<终端安装目录>\terminal64.exe" -DryRun
```

- 由独立的计划任务每分钟运行；终端进程不在就启动它，别的什么都不做：不连接
  终端、不下单、不结束任何进程、不重启已在运行的终端（D82）。
- `-TerminalPath` 必填，必须是 `terminal64.exe` 的完整路径；不探测、不猜。按
  **最终路径**判断（目录联接、符号链接、配置里写的短文件名先展开，和网关探针是
  同一种比法，已知的两处差别见下一条）：经目录联接启动的终端，用真实路径也认得
  出来。同名但最终路径不同的是另一个实例。
- **拼写要一致**：`-TerminalPath`、网关配置里的 `terminal_path` 和终端实际启动时
  的拼写，三者用同一种。已知的不一致：终端经 8.3 短文件名启动的，探针和看护脚本
  都认不出它，看护任务会每分钟再启动一个——**终端不要经短文件名启动**；链接的
  目标路径自己又穿过另一个链接时，看护脚本只认链接那一种拼写；`subst` 和映射盘的
  拼写没有验证过——**不要用**。所以装任务之前必须用 `-DryRun` 核对一次（见下面
  "看护任务的安装"一条）。
- 有 `terminal64.exe` 进程却读不到映像路径时不动（退出码 1，
  `refused reason=cannot_tell`）：宁可这一分钟不启动，也不重复启动。本机有两个
  安装，任何一个读不到都会让看护每分钟拒绝一次。
- **看护被拒绝（退出码 1）没有任何告警通道**，只有任务计划程序里该任务的"上次
  运行结果"。装任务之前要定下谁来发现它：调度日检核对 `terminal_keeper` 任务的
  上次结果码和 `-LogPath` 的最后几行（U10 定了渠道之后，由小时级存活检查一并看）。
- 退出码 0 = 在运行 / 已启动 / `-DryRun`；1 = 拒绝或启动失败。每次输出一行
  `KEEPER …`，不含本机路径。状态变化（启动、拒绝）写进 `-LogPath`。
- 互斥量按终端的最终路径命名：同一个终端一次只有一个看护实例在判断和启动；
  给另一个终端另装的看护互不影响。
- **维护时要关终端，先停用看护任务**，否则一分钟内它会把终端拉起来；§6.3 的人工停机序列
  里有这一步（先停用看护任务，再关终端）。装了带 `--expect-keeper` 的小时任务之后，停机期间
  存活检查报 `keeper_stale`（以及运行器等终端时的 `terminal_absent`）是预期的。
- **经网关的一次性脚本不再自动拉起各自的终端**。研究用的第二个终端（如果有）
  需要人工启动。
- **看护任务的安装**：每分钟一次的独立任务，由 `scripts\ops\install_terminal_keeper.ps1`
  注册，经通用的隐藏启动器 `scripts\ops\hidden_task.vbs` 调用看护脚本——和 supervisor
  任务（§6.3）同一个启动器，同样的原因：wscript 没有控制台，动作是控制台程序的任务
  每分钟会在交互桌面上弹一次窗口。

  ```powershell
  # 1. 终端运行着的时候先核对看护脚本的判断，必须输出 running（上面那一条命令）
  # 2. 只打印将要注册的内容：这条路径不查询、不注册、不注销
  powershell -ExecutionPolicy Bypass -File scripts\ops\install_terminal_keeper.ps1 `
    -TerminalPath "<终端安装目录>\terminal64.exe" -DeployDir "<部署树>" -DataDir "<数据目录>"
  # 3. 动作、触发器、主体、设置核对无误后，同一条命令加 -Install 才注册
  # 4. 看任务现在的状态（只读）。装完等一两分钟查一次：看护脚本状态文件里最近的一条也在里面
  powershell -ExecutionPolicy Bypass -File scripts\ops\install_terminal_keeper.ps1 `
    -Status -DataDir "<数据目录>"
  ```

  - 不带 `-Install` / `-Uninstall` 时脚本只做检查、只打印，不碰任务计划程序。打印的
    `KEEPER_TASK_PARAMETERS {…}` 里 `mode` 是 `plan`，注册之后打印的同一行里是
    `install`。这一行是本机的绝对路径和账户：**抄进入库的部署记录之前要脱敏**，
    账户名只在本机核对。
  - 任务的设置：每分钟一次，第一次在一分钟之后；"不启动新实例"（`IgnoreNew`）——看护
    一次只要一两秒，上一次没跑完就不再起一个；单次运行的时间上限 2 分钟；主体是当前
    用户，登录类型 `Interactive`、运行级别 `Highest`——和 supervisor 任务一样：终端要在
    交互桌面上启动，看护要读得到同一个用户的进程的映像路径。
  - 安装只注册任务：不往部署树里写文件（supervisor 要求部署树是干净的），不建日志目录。
    检查全在注册之前，任何一项不对都拒绝并一次列全：路径不是完整路径、`terminal64.exe`
    不在或名字不对、部署树里没有看护脚本或启动器、路径带 `%`（启动器以退出码 4 拒绝）或双引号（启动器看不到它：WSH 解析命令行时就把引号吃掉了，参数传过去会变样——所以在这里拒）、路径含系统
    ANSI 代码页表示不了的字符（和 §6.3 的安装脚本同一条规矩）。同名的任务已经在了就
    拒绝，不覆盖：换参数先 `-Uninstall`。
  - `-Status` 只读：打印 `KEEPER_TASK_STATUS {…}`——状态、上次运行的时刻和上次结果，
    时刻是 UTC。上面"看护被拒绝没有告警通道"那一条要核对的上次结果码，就用它查。给了
    `-DataDir` 时还打印一行 `KEEPER_STATE {…}`：看护脚本状态文件里最近的一条（`ts`、`outcome`、
    `reason`、`age_s`）。装完等一两分钟查一次：`outcome` 是 `running` 或 `started`、`age_s` 只有
    一两分钟，看护就跑成了；`KEEPER_TASK_STATUS` 里 `state` 是 `Ready`、`last_task_result` 是 0。
  - 退出码：0 做完了；1 拒绝或出错，原因在错误输出里；4 是 `-Status` 或 `-Uninstall`
    时任务没有安装。
  - **状态文件**：看护脚本每一次做过判断的运行，把结果写进 `-LogPath` 所在目录下的
    `state.json`（任务的动作里就是 `<数据目录>/ops-evidence/terminal-keeper/state.json`）：
    `ts`、`outcome`（`running` / `started` / `refused` / `start_failed`）、`reason`（拒绝和启动
    失败的原因代码）、`running`、`others`、`pid`，不含路径和账户。只在给了 `-LogPath` 且不是
    `-DryRun` 时写；`busy` 的那个实例不写（它没有做判断）；写不进去只警告，不改退出码。
    小时级存活检查读它（§6.6）。
  - **不要手工拼 `Register-ScheduledTask`**：任务的动作、触发器、主体、设置只在安装
    脚本里定义一份。部署记录里抄的是打印出来的那一行（路径和账户脱敏）：任务的路径和
    名字、`start_boundary`、`repeat_interval`、`multiple_instances`、
    `execution_time_limit`、`logon_type`、`run_level`、启动器和看护脚本的 SHA-256。
  - 不在安装脚本之外另装同名的任务；卸载只用 `-Uninstall`（只注销，不删日志）。

**验收**（第二十三轮计划 §2.2 第 7 条）：P23-2 的网关一半已翻转——探针在
T209 上报 NOT-REPRODUCED（终端退出后 `initialize` 被调 0 次）。演练 C 在
PaperSoak 阶段做：结束终端 → 看护任务两分钟内拉起 → worker 自动恢复 →
supervisor 全程没有拒绝。取证时不要只看 supervisor 有没有拒绝（它每分钟才取一次
快照）：用进程创建事件看 worker 的进程树里有没有出现过 `terminal64.exe`——
真实的 MetaTrader5 包在终端**已经**运行时调用 `initialize()` 会不会也生成一个
短命的 `terminal64.exe` 子进程，还没有验证；如果是，T209 挡不住它。

### 6.6 小时级只读存活检查（T214，第二十三轮）

`scripts/ops/liveness_check.py` 设计为由一个独立的计划任务每小时运行一次，不依赖任何 agent 会话。**任务还没有安装**（装之前要做的事见本节末）。它不 import MetaTrader5、不连接交易终端：只读登记表、心跳、supervisor 状态、控制文件和磁盘余量（带了 `--expect-keeper` 时还读看护脚本的状态文件），唯一的写是它自己的状态文件。判级和排通知的纯逻辑在 `scripts/ops/liveness_logic.py`。

```powershell
# 手工跑一次：输出一行 JSON。退出码 0 没有生产级问题，3 有，2 是检查自己出错
.venv/Scripts/python.exe scripts/ops/liveness_check.py --live-dir <数据目录>/live --reports-dir <数据目录>/reports --disk-path <数据目录> --no-notify
# 装了看护任务（§6.5）的部署加 --expect-keeper；每小时的任务把它写进命令行
```

带 `--no-notify` 只判定、不发通知；去掉它之前先读下面"通知"一段。

**问题清单**（输出的 `findings`，每项带代码、第一次看到的时刻、持续秒数、是不是生产级）：

| 代码 | 什么时候算生产级 |
| --- | --- |
| `role_missing:<角色>`、`heartbeat_stale:<角色>`、`supervisor_state_stale`、`registry_unreadable` | 持续满 10 分钟 |
| `terminal_absent:<运行器>`、`keeper_absent`、`keeper_stale`、`keeper_refused`（后三个只在带了 `--expect-keeper` 时评） | 持续满 10 分钟；`keeper_stale` 和启动等待的 `terminal_absent` 用现场自己的年龄，满了 10 分钟的第一次看到就是生产级 |
| `halt`、`pause:<magic>`、`kill_switch`、`risk_veto` | 持续满 1 小时 |
| `disk_low`（低于 10 GB）、`disk_unknown`、`report_missing`、`registry_invalid`、`clock_ahead:<来源>` | 一出现就是 |

- 时长取"检查器第一次看到至今"和现场自己的年龄（心跳年龄、文件修改时间）里大的那个。心跳已经停了 10 分钟的角色，第一次看到就算生产级。
- 有生产级问题退出码是 3；只有不算生产级的发现时退出码是 0，`ok` 是 false。
- 登记表不存在、读不出、解析不了是 `registry_unreadable`；顶层不成形状、空的、条目对不上身份是 `registry_invalid`。

**看护任务和终端**（看护脚本见 §6.5）：

- `--expect-keeper` 表示这个部署装了看护任务。为什么要明说：看护的状态文件是看护脚本自己写的，"装了任务但一次也没跑成"和"根本没装"在文件系统上一样，都还没有文件；而现在的观察运行没有装看护任务，定版前也不该因为这一条每 24 小时收到一次通知。所以由小时任务的命令行带这个开关，不带给三类看护发现都不评，输出的 `keeper` 是 `not_checked`。带了：状态文件不在是 `keeper_absent`；10 分钟没有更新是 `keeper_stale`（看护每分钟运行一次，状态文件自己的年龄满了 10 分钟，第一次看到就是生产级）；最近一次是 `refused` 或 `start_failed` 是 `keeper_refused`。状态文件读不出、不是对象、没有 `ts` 的按文件修改时间算年龄；`ts` 来自未来记 `clock_ahead:keeper`。`outcome`、`reason` 只有长得像标识符的才进输出。
- `terminal_absent:<运行器>` **不受这个开关管**，永远评：万一装小时任务的人漏了开关，"终端不在、交易停着"这一条最要紧的仍然有人看。信号来自运行器自己的心跳：等终端的时候心跳照写，`role_missing`、`heartbeat_stale` 都不会触发。两种说法都算——启动等待（心跳的 `last_error` 是 `waiting_for_terminal`）；连上之后终端进程没了（网关的 `TerminalNotConnectedError` 带着类名进了 `last_error`）。启动等待的时长取"检查器第一次看到至今"和"现在减去登记里的 `started_utc`"里大的那个——启动等待的运行器从启动起就在等，这个数是下界：已经等了 10 分钟以上的，当次就是生产级，不用再等一个小时。丢了终端没有可信的起点（它每等 15 分钟以 78 退出、被 supervisor 重启，`started_utc` 也会归零），只靠检查器自己的时钟。
- 这一类只覆盖运行器：对账、风控两个角色等终端和断线时写的心跳不带这类标记。部署之后如果要它们也被看到，得先让它们写（改生产代码，另议）。

**日报的口径**：22:30 UTC 之前要求前天的日报在，22:30 起要求昨天的（日报任务 21:57 UTC 生成前一天的日报，留 33 分钟宽限）。某一天的日报只在它生成的那一刻（次日 21:57 UTC）部署已经在跑才要求，依据是登记表里最早的 `started_utc` 早于那一刻。所以：部署在 21:57 UTC 之后的，当晚的日报不要求；四个角色同一天全部重启之后，生成时刻早于新登记的日报都不要求；在 21:57 UTC 之前启动的，当晚 22:30 起就要求。这条规则只信登记里的 `started_utc`：被写早了会多报，不会漏报。日报任务晚启动 33 分钟以上，当小时的检查先报缺，下一小时报"已恢复"。

**读不出的文件**：心跳读不出、损坏、不是对象，记成陈旧；supervisor 状态文件读不出、损坏、不是对象，按文件修改时间算年龄；`halt.json`、`pause-<magic>.json` 存在但读不出，按存在处理（生产对它们是 fail-closed）。检查自己不会因此出错。

**通知**：消息是"MT5 Demo 值守："加问题代码和时长，不带账号、路径、服务器名。同一个生产级问题第一次发现时发一条，还在的话每 24 小时最多再发一条，消失时发一条"已恢复"；通知确实发出去了才记。渠道配置读本机不入库的 `*.local.json`（缺省 `notify.local.json`，形状是 `{"type": "webhook", "target": "<URL>"}`；载荷是 `{"text": <消息>}`，其余渠道和载荷格式等 U10）。webhook 只发 https，http 只发回环地址，不跟随重定向。**配置文件里有接收地址，不得入库。**

输出的 `notified`：`sent` 发了；`none` 没有要发的（包括问题已经通知过、还在 24 小时之内）；`no_channel` 没有配置渠道（不管有没有问题都写这个）；`unsupported_channel` 配置了没有实现的渠道；`failed` 发送出错（配 `notify_error`，只有异常类名；下一次按"还没通知过"再发）；`disabled` 带了 `--no-notify`。**该发而没发出去的是后面四个值。**

**状态文件**缺省在 `<数据目录>/ops-evidence/liveness/state.json`（`live` 和 `reports` 之外；`data/` 不入库）。它记每个仍在的问题第一次被看到的时刻、上一次发通知的时刻，和还没发出去的"已恢复"。读不出或形状不对的当空状态重新起算，另记 `state_reset`。删掉它等于全部重新发现，会重发一轮通知。写不了不改结论，另记 `state_error`。

**退出码 2**：检查自己出错。那一行带 `step`（`observe` 或 `evaluate`）和异常类名，不带路径和异常原文；参数写错也是 2，输出是用法说明。用 `pythonw` 启动（没有标准输出）时那一行被丢掉，不报错，退出码仍是 0 或 3。

**与施工计划的出入**：计划写这个脚本 300 行以内；入口 214 行，纯逻辑在第二个模块（333 行），合计 547 行（评审记录 M-4）。

**装计划任务之前要做的**（都没有做；清单见评审记录 §8）：

1. U10 定通知渠道，接收地址只放本机。**U10 没有答复时不起算（D113）。** 渠道定了再定 N-4：消息里带不带停机原因的代码。
2. 日报任务（§6.3.1）先装好、启用。观察运行期间 `live_daily_report` 是停用的（11-21 起，运维日志 11-23 一节的第 8 项）；现在装存活检查，当晚 22:30 UTC 起就会报缺日报。
3. 安装小时级任务：在工作区之外，显式的绝对路径；运行账户能打开生产进程；登录类型和会话写进部署记录。装了看护任务（§6.5）的部署，命令行带 `--expect-keeper`，和看护任务在同一个维护窗里生效。
4. 通知演练：停一个假角色，一小时内收到；再加一条反向的，健康现场 24 小时零通知。
5. 施工计划 §3.2 第 5 条：把"主会话缺位"写进调度指令。
6. 调度日检核对存活检查自己：最近一小时运行过，退出码是 0 或 3，`notified` 不是"没有渠道"，`notify_error`、`state_error` 为空。

**维护时**：停机演练会触发生产级问题，先停用这个任务，或者接受通知；维护结束后会发"已恢复"。

### 6.7 T215 对照实验：关闭 agent 桌面端，哪些进程会死（D112 的前置）

**为什么要做**：11-21 01:24 UTC 有人从托盘退出 agent 桌面端，8 秒后又打开。四个生产进程活了下来，被连带结束的是交易终端，
之后 supervisor 每分钟拒绝一次，持续 684 分钟（第二十三轮审核 O23-7、运维日志 11-23 一节的取证结论）。而 11-17 的记录是四个进程
随桌面端退出整树死亡。生产必须在不依赖 agent 会话的载体上跑，而且要知道"关闭桌面端"到底按什么清理——按工作区关联，还是按会话。
运维日志 11-23 一节要求把"一次性任务由 agent 会话触发"和"由调度器自行触发"分开测，所以是四组。四组都是只休眠的假角色，
不碰生产、不 import MetaTrader5、不读写任何生产目录：

| 组 | 谁触发 | 解释器 | 起始目录 | 进程 |
| --- | --- | --- | --- | --- |
| A | 计划任务：一次性的时间触发，到点由任务计划程序自己启动 | `<工作区>\.venv\Scripts\pythonw.exe`（venv 的启动器） | `<工作区>\data\ops-evidence\t215-<日期>\a\`（工作区里被忽略的路径下新建的空目录） | 两个：启动器和它拉起的基础解释器 |
| B | 同 A | `<基础解释器目录>\pythonw.exe` | `<用户目录>\t215-<日期>\b\`（工作区之外新建的空目录） | 一个 |
| C | 主会话的 shell 直接启动，起完那个 shell 就退出 | 同 B | `<用户目录>\t215-<日期>\c\` | 一个 |
| D | 计划任务：一次性任务，注册之后由主会话当场触发（`Start-ScheduledTask`） | 同 B | `<用户目录>\t215-<日期>\d\` | 一个 |

- A 和 B 只差"解释器和目录在哪"，D 和 B 只差"谁触发"。C 是阳性对照：关闭如果清到会话直接启动的进程，它应当死。
- B、C、D 的目录不在 `<工作区>` 下，也不在 `<生产根>` 里。也不要放在 `<工作区父目录>` 下——那是 agent 的工作区根目录，
  清理如果按它关联，三组会被一起带走，读法就分不出来了。
- 正在跑的观察运行是现行方式的真实样本：一次性任务、由 agent 会话触发、解释器和目录都在工作区内（运维日志 11-23 一节的
  补记 §2）。它可能随这次关闭一起停，这正是要看的；对着 A、D 两组的结果读，单独记。

**怎么读**。先判这次实验算不算数：

- 用户动手之前一刻复核过四组都在，记下了时刻。没有这一次复核，结果不算。
- 关闭的时刻不晚于"假角色创建时刻加它的寿命"（4 小时）。假角色到点自己退出，晚于它的结果作废。
- "活着"指 PID 还在，而且创建时刻和记录逐字相同；PID 被别的进程复用不算活着。

算数的，按下面的读，从上往下，先命中的为准：

| 结果 | 读法 |
| --- | --- |
| C 也活 | 这次关闭没有清到会话直接启动的进程，实验没有结论：查关闭方式（是不是从托盘退出、退干净没有），重做 |
| D 和 B 的生死不一样 | 两组只差谁触发，结果却相反，自相矛盾：不下结论，先取证。D 死而 B 活，说明"由 agent 会话当场触发的一次性任务"会随桌面端死——观察运行正是这一种启动方式，§6.8 的载体任务就只能用调度器自行触发的 |
| A 活、B 死 | 不下结论，先取证 |
| 观察运行和假角色的结果相反（例如观察运行整棵活着而 A 死了，或者观察运行死了而 A 活着） | 不下结论，先取证 |
| A 死、B 活 | 清理按工作区关联进行，和谁触发无关：按 §6.8 迁出即可 |
| A、B 都活，D 也活 | 计划任务载体启动的进程没有随桌面端死：11-17 的父进程记录有误，把当时的实际启动方式查清、写明 |
| A、B 都死，D 也死 | 会话级的清理，这台机器上换目录解决不了：不起算，转 U11 |

两条补充：

- **"A 死"只看启动器**：它的 PID 不在了，或者那个 PID 上的创建时刻变了，就是死。启动器死、解释器还活着，按"A 死"读，
  解释器的生死另记。
- 观察运行死了或者活着，不改变假角色的读法；它停了之后的事见下面"注意"。

**做法**（主会话执行；第 1 步起需要用户在场，第 5 步是用户动手，第 6、7 步由重开之后的新会话按已推送的记录做）：

1. **约时间**（主会话和用户）。和用户约好关闭桌面端的时刻，用户确认在场之后再起假角色——假角色的寿命从起它算起，
   用户不在，寿命会在等的时候走完。约好的时刻要给第 2–4 步留出余量。前提：观察运行照常跑，不碰它；不碰 `\MT5Quant\`
   下的任何任务。
2. **保全现场**（主会话）。`data\live` 的快照和一份完整进程表，存进 `data\ops-evidence\t215-<日期>\`（不入库）。复制时被
   进程独占的文件（单实例锁）复制失败是预期的，记下复制了哪些、各自的 sha256。
3. **起四组**（主会话）。假角色都是 `pythonw.exe -c "import time; time.sleep(14400)"`，4 小时后自己退出。
   - 先建目录：A 的起始目录，B、C、D 的起始目录，和任务文件夹 `\MT5Quant-Exp\`——按 cmdlet 的文档 `Register-ScheduledTask`
     不会自建文件夹，这一点没有验证过；先建了再注册总是对的。
   - C：在主会话的 shell 里 `Start-Process`。
   - A、B：各注册一个一次性的计划任务，时间触发设在一两分钟之后，只触发一次，由任务计划程序自己启动。
   - D：注册同样的一次性任务，触发时刻设在一个不会到的将来时刻（它不该自己触发），注册之后立刻 `Start-ScheduledTask`。
   - 三个任务的设置：主体是当前用户，登录类型 `Interactive`、运行级别 `Highest`，和真的 supervisor 任务、看护任务一样
     （§6.5 安装脚本里 `New-ScheduledTaskPrincipal` 的写法）；"不启动新实例"；**单次运行时间上限要大于假角色的寿命**。
     照抄看护安装脚本的 2 分钟，假角色两分钟就被任务计划程序结束，读出来是"都死了"。
   - 注册用的命令（占位符先替换）：

   ```powershell
   $life = 14400                                      # 假角色寿命（秒）
   $code = '-c "import time; time.sleep(' + $life + ')"'
   $principal = New-ScheduledTaskPrincipal `
     -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
     -LogonType Interactive -RunLevel Highest
   $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
     -ExecutionTimeLimit (New-TimeSpan -Seconds (2 * $life))
   $exp = '<用户目录>\t215-<日期>'
   $outside = '<基础解释器目录>\pythonw.exe'
   $groups = [ordered]@{
     A = @{ Exe = '<工作区>\.venv\Scripts\pythonw.exe'; Cwd = '<工作区>\data\ops-evidence\t215-<日期>\a'
           At = (Get-Date).AddMinutes(2) }
     B = @{ Exe = $outside; Cwd = "$exp\b"; At = (Get-Date).AddMinutes(2) }
     D = @{ Exe = $outside; Cwd = "$exp\d"; At = (Get-Date).AddYears(1) }
   }
   New-Item -ItemType Directory -Force -Path "$exp\c" | Out-Null
   Start-Process -FilePath $outside -ArgumentList $code -WorkingDirectory "$exp\c"     # 组 C
   # 任务文件夹不会自建：先建。已经在了会报错，那一种不用管
   $svc = New-Object -ComObject 'Schedule.Service'; $svc.Connect()
   try { $null = $svc.GetFolder('\').CreateFolder('MT5Quant-Exp') } catch { }
   foreach ($g in $groups.Keys) {
     $item = $groups[$g]
     New-Item -ItemType Directory -Force -Path $item.Cwd | Out-Null
     $action = New-ScheduledTaskAction -Execute $item.Exe -Argument $code -WorkingDirectory $item.Cwd
     $trigger = New-ScheduledTaskTrigger -Once -At $item.At
     Register-ScheduledTask -TaskPath '\MT5Quant-Exp\' -TaskName "T215-$g" `
       -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
   }
   Start-ScheduledTask -TaskPath '\MT5Quant-Exp\' -TaskName 'T215-D'
   ```

   - 核对（两三分钟后取一次进程表）：四组都在。A 是两个进程，解释器的父 PID 是启动器；A、B、D 的父进程链往上到头，
     中间没有 agent 桌面端的进程；C 的父 PID 通常已经是一个不存在的 PID（那个 shell 退出了）。哪一组不在：这一轮作废，
     按第 7 步收尾之后重来。
4. **用户动手之前一刻的记录**（主会话）。用户说可以关了，先做这一步，做完再请用户关：
   - 复核四组都在，记下时刻（UTC）。
   - 取一份完整进程表（只读，例如 `Get-CimInstance Win32_Process`；创建时刻记成 UTC 的 `o` 格式，和 supervisor 的进程表
     同一种格式），从中抄出清单，每个进程都记 PID、创建时刻、会话 ID、父 PID、往上到头的父进程链（名字和 PID）：
     - 四组假角色的每一个进程。**A 的启动器和解释器都记**，标明父子；
     - 观察运行的四个角色：每棵树的启动器、解释器、conhost；
     - **终端**：生产终端（按映像路径认；机器上还有第二个安装的终端，也记，分开标）的映像路径也要记。只读进程表，
       不连终端。观察运行的 supervisor 是旧代码，拒绝时的告警只有一句 `process script/arguments mismatch`，不带角色和
       PID——终端的记录要靠自己记全。
   - 写进 phase-f-log 并推送。重开之后做核对的是新会话，它什么都没起过，只能按已推送的记录认进程。
   - 再只读核对一次任务清单：`\MT5Quant-Exp\` 下三个任务都跑过；`\MT5Quant\` 下的没有动。
5. **用户从托盘退出 ZCode 桌面端**，等至少 30 秒，再打开（用户）。关闭的时刻从桌面端自己的运行日志里取（11-21 的取证
   用过），用户记得的话也记下来。
6. **重新打开之后**（新会话）：
   - 取一份完整进程表，和第 4 步的清单逐个比：PID 还在而且创建时刻相同才算活着。再拿整份表和关闭前的那一份对比——
     成了孤儿的解释器只有这样才看得到。
   - 另记：观察运行还活不活；终端是原来的那个进程，还是被重新拉起的（父进程是谁）；supervisor 任务有没有把缺的角色
     拉起、有没有拒绝（`alerts.log` 里的 `SUPERVISOR`、`SUPERVISOR_REFUSED`，`supervisor-state.json`）；任务计划程序
     操作日志里三个实验任务的事件。
   - 结果和判读写进 phase-f-log。观察运行如果随关闭停了，先做完这一步的记录再谈处置，不为了"让它过"而手工重启（D109）。
7. **收尾**（新会话）：
   - 只结束第 4 步清单里标为假角色的进程，A 的解释器也在内；条件是 PID 相同、创建时刻逐值一致。不按映像名结束。
     结束不了（运行级别是 Highest）的不换别的办法，等它自退。
   - 注销三个实验任务，再删 `\MT5Quant-Exp\` 文件夹（`$svc.GetFolder('\').DeleteFolder('MT5Quant-Exp', 0)`；任务不先注销，
     文件夹删不掉）。
   - 进程都结束了再删目录，按确切路径删：A 的起始目录，和 `<用户目录>\t215-<日期>\`。证据目录
     `data\ops-evidence\t215-<日期>\` 留着，不入库。

**注意**：

- **假角色和真角色有两处不一样**，读结果时记住。假角色是 `pythonw.exe`，没有控制台，所以没有 conhost 子进程；生产角色是
  `python.exe`（隐藏窗口，带 conhost）。读到"A、B 都活"的时候，先排除"清理是按控制台进行的"。运行级别是 Highest 的进程，
  没有提升的桌面端也结束不了——实验任务的主体必须和真 supervisor 任务一致，理由在这一条。
- **终端可能被连带结束**（11-21 01:24 就是这样）。观察运行跑的是没有 T209 的旧代码（`cd6fc4a`）：终端没了，worker 重连时
  `initialize()` 会把终端拉起来，终端从此挂在 worker 的进程树里，supervisor 随即每分钟拒绝一次（§6.3 失效模式表第 10 行），
  写 halt、发告警。**这是证据，只记录、不处置**（D109）；处置留到 §6.8 的维护窗。候选的网关不拉起终端（§6.5）。
- **"supervisor 会拉起缺的角色"只对整棵进程树都退出的情形成立**（`live_supervisor.py` 的 `process_missing` 分支）。
  启动器没了而解释器还在，是拒绝（第 9 行），不重启。
- D104 的"终端的父进程不能是工作进程"在第 4 步顺带核一次，第 6 步看它有没有变。

### 6.8 迁出 agent 工作区（D112；T215 之后）

**目标**：生产用的解释器、部署树、数据、配置、日志、包装脚本全部在 agent 工作区之外；任何包装脚本、计划任务的参数里都
不出现工作区的路径。迁完做宿主退出演练（关闭桌面端后四角色、supervisor、终端存活 30 分钟以上，心跳连续），结果入
phase-f-log。

**先知道三件事**（都对着候选的代码核过）：

1. **supervisor 只认 Demo。** `scripts\live_supervisor.py` 的 `_registered_trees` 对登记里 `mode` 不是 `Demo` 的条目一律拒绝
   （`registry deployment/mode/arguments mismatch`），`--inspect`、`--stop` 和每分钟的 `supervise()` 都走它；
   `scripts\demo_run.ps1` 在 PaperSoak 下也拒收 `-DeploymentSha`。所以 PaperSoak 上装不了 supervisor，也没有 `--stop` 可用。
2. **两种模式不能共用数据目录。** `demo_run.ps1` 看到登记里有 mode 不同的条目直接拒绝（`mode conflict`）。
3. **`demo_run.ps1` 的缺省参数是相对路径，按脚本自己所在的仓库解析。** 漏给一个，数据或配置就悄悄落进部署树；
   `data\`、`gateway.json` 又被 `.gitignore` 忽略，干净检查发现不了。所以下面的命令每一个参数都显式写成绝对路径。

**生产根目录**（工作区之外，纯 ASCII 的路径，不带 `%`、不带双引号，不以反斜杠结尾；位置由用户定，不定就选用户目录下
一个专门的目录；不能经过符号链接或目录联接，见下表"部署树"一行）：

```
<生产根>\
  deploy\          部署树：独立的一份仓库，detached 在候选的完整 SHA 上，干净
  venv\            独立的虚拟环境；基础解释器也在工作区之外
  gateway.json     内容是 {}（终端路径取默认值）
  start-demo.ps1   Demo 的载体任务运行的包装脚本；start-paper.ps1 是 PaperSoak 的
  data\            Demo 的数据目录
    live\          登记、心跳、journal、证据
    demo\          运行日志
    specs\latest.json   costs\spread_model.json   demo-configs\*.toml
  data-paper\      PaperSoak 专用的数据目录，结构同 data\，跑完归档
  research\costs\spread_model.json     研究库缓存副本
  archive\         归档：工作区的旧 data\、PaperSoak 的数据目录、旧任务的导出
```

运行和装任务之后 `data\` 下还会多出三个目录：`logs\`（日报和周检的任务日志）、`reports\`（日报）、`ops-evidence\`
（supervisor、看护、存活检查的日志和状态）。它们是脚本自己建的，不用手工建。

**每一项从哪里来、怎么核对**（`scripts\demo_run.ps1` 启动前检查的就是这些）：

| 项 | 现在 | 迁出时 | 核对 |
| --- | --- | --- | --- |
| 部署树 | `<工作区父目录>\demo-deploy-cd6fc4a`，是工作区仓库的一个工作树 | 不用 `git worktree`：工作树的 `.git` 是一个指回工作区仓库的文件。单独克隆一份，再 `git checkout --detach <候选的完整 SHA>`。从本地仓库克隆要加 `--no-hardlinks`（缺省时对象是硬链接），克隆完把 `origin` 改指远端或者删掉——不然工作区的路径留在 `<部署树>\.git\config` 里 | `git -C <部署树> rev-parse HEAD` 等于候选的完整 SHA；`git -C <部署树> symbolic-ref -q HEAD` 没有输出、退出码 1；`git -C <部署树> status --porcelain --untracked-files=all` 是空的；`.git\config` 里没有工作区的路径。**不手工往部署树里放任何东西。**运行和装任务会在里面生成一些被 `.gitignore` 忽略的文件——`__pycache__\`、日报和周检的包装器 `scripts\task_*.cmd`、日报任务在网关期限时写下的 `data\live\` 标记——它们不会让干净检查判脏，也不要挪动或删除（挪了 `.cmd`，任务就跑不了） |
| 解释器 | `<工作区>\.venv` | `<基础解释器目录>\python.exe -m venv <生产根>\venv`，再装 `requirements.txt` | 第 1 步的四条命令 |
| 终端路径 | `<终端安装目录>\terminal64.exe`。网关配置是 `{}`，取的是 `src\mt5gw\config.py` 里 `terminal_path` 的缺省值 | 不变 | 看护任务的 `-TerminalPath` 和这个缺省值逐字一致（装之前用 `-DryRun` 核对，§6.5）；运行中的终端映像路径也是这一种拼写 |
| 点差模型缓存 | `<工作区>\data\costs\spread_model.json` | 复制到 `<生产根>\data\costs\` | sha256 和原来的一样 |
| 合约快照 | `<工作区>\data\specs\latest.json` | 复制到 `<生产根>\data\specs\`（脚本按数据目录找它） | 同上 |
| 两个策略的配置 | `<工作区>\data\demo-configs-consolidated\` 下的两个 `.toml` | 复制到 `<生产根>\data\demo-configs\` | sha256 写进部署记录。部署树里受跟踪的 `demo-configs\` 是同名的示例目录，别拿错。预检：每个配置 `volume_lots` 是 0.01、magic 唯一 |
| 研究库缓存副本 | `<工作区>\data_research\costs\spread_model.json` | 复制到 `<生产根>\research\costs\` | sha256 |
| 风控、会话配置 | 部署树里的 `config\risk.toml`、`config\sessions.toml` | 直接用候选部署树里的 | `-RiskConfig` 是 `<部署树>\config\risk.toml`（`daily_loss_cap_usd = 500.0`）；`risk-demo.toml`（0.5 美元）只用于全平演练（O21-7）。两份的 sha256 和上限的值写进部署记录 |
| 网关配置 | `<工作区>\gateway.json`（3 个字节） | 在 `<生产根>\` 里建同样的文件 | 内容是 `{}`；`allow_live_trading` 的缺省值是 False |
| 日志 | `<工作区>\data\demo` | `<生产根>\data\demo` | supervisor 的包装脚本把输出落到 `<生产根>\data\ops-evidence\supervisor\supervisor.log`（§6.3） |
| 数据目录 | `<工作区>\data`（观察运行在用） | 新的 `<生产根>\data\live`。旧的不搬，整体归档（第 3 步） | 新部署之前账户上两个 magic 零持仓、零挂单 |

**顺序**（第 1、2 步不碰观察运行，也不连终端；第 3 步起是维护窗）：

1. **建生产根目录**（主会话）。观察运行还在跑，这一步不碰它、不连终端、不动 `\MT5Quant\` 下的任务。克隆、建 venv、装包
   有负载，避开换日窗。
   - 做什么：按上面的布局建目录；克隆、`checkout --detach`；建 venv，
     `<生产根>\venv\Scripts\python.exe -m pip install -r <部署树>\requirements.txt`；复制各项；建 `gateway.json`。
   - 核对部署树：上表"部署树"一行的三条 git 命令，再加一句翻 `.git\config`。
   - 核对路径：`<生产根>` 和它的每一级上级都不是符号链接，也不是目录联接——对每一级看一次
     `(Get-Item -LiteralPath <那一级路径>).LinkType`，都应当是空的。代码只查符号链接（`live_supervisor.py` 的
     `verify_deployment` 对九个路径和它们的每一级上级做 `is_symlink()`，查到按 `unsafe path` 拒绝）；目录联接它查不出来，
     只能在这里核。
   - 核对复制来的文件：源和目标的 sha256 相同（`Get-FileHash -LiteralPath <源>, <目标> -Algorithm SHA256`）。
   - 核对新解释器，四条都要过：

     ```powershell
     $py = '<生产根>\venv\Scripts\python.exe'
     & $py -c "import sys; print(sys.executable); print(sys.prefix); print(sys.base_prefix)"
     & $py -m pip check
     & $py -c "import MetaTrader5; print('MetaTrader5 import ok')"     # 只是导入，不连终端
     & $py -c "import sys; sys.path.insert(0, r'<部署树>\src'); import mt5gw, mt5live; print('import ok')"
     ```

     第一条打出的三个路径都不在工作区里：`sys.executable`、`sys.prefix` 在 `<生产根>\venv` 下，`sys.base_prefix` 在
     `<基础解释器目录>` 下。第三条必须有：`MetaTrader5` 在网关里是延迟导入的，下一步的预检证明不了它装上了。
   - 出错了怎么办：哪一条不过就修哪一条，修好重跑，不带着问题往下走。
2. **不连终端的预检**（主会话）。前提：第 1 步的核对都过了。观察运行还在用终端（D82），所以预检不能连终端。

   ```powershell
   $root = '<生产根>'
   & "$root\deploy\scripts\demo_run.ps1" -Mode Demo -PrecheckOnly -SkipGatewayCheck `
     -DeploymentSha '<候选的完整 SHA>' `
     -PythonExe "$root\venv\Scripts\python.exe" -DataDir "$root\data" `
     -ConfigDir "$root\data\demo-configs" -GatewayConfig "$root\gateway.json" `
     -RiskConfig "$root\deploy\config\risk.toml" -SessionsConfig "$root\deploy\config\sessions.toml" `
     -SpreadModel "$root\data\costs\spread_model.json" `
     -ResearchCache "$root\research\costs\spread_model.json" `
     -LogDir "$root\data\demo" -MinFreeGB 40
   ```

   - 跑的必须是 `<生产根>\deploy\scripts\demo_run.ps1`。它以自己所在的仓库为根；从工作区里的那一份跑，SHA 对不上，被拒。
   - `-SkipGatewayCheck` 只能和 `-PrecheckOnly` 同用，单独用直接退出 1。这个分支对 DEMO 保险丝的源码只做语法解析和
     sha256，不执行它；临时文件写在 `%TEMP%` 下，用完就删。生命周期检测走 `live_supervisor.py --inspect`（带了
     `-DeploymentSha` 才做），那个脚本不 import MetaTrader5，只读。`--inspect` 顺带把部署树、九个路径、网关配置、风控和
     会话配置、策略配置整体验证一遍——网关配置文件就是在这一步被读到的。
   - 磁盘：脚本的门槛缺省是 5 GB，看的是数据目录所在盘的剩余空间。传 `-MinFreeGB 40`，让它替你核"磁盘余量不低于
     40 GB"这一条起算前提。
   - 核对：退出码 0，没有 `[X]` 行；最后一节是 `-PrecheckOnly：检查完成，不启动任何进程`，并且是"未发现存活实例"
     （新的数据目录里还没有登记）。过了之后 `<生产根>\data\` 下会多出 `live\`、`demo\` 和一个锁文件，这是它建的，
     没有启动任何进程，也不碰观察运行的登记。
   - 出错了怎么办：按 `[X]` 行修，重跑到全过才往下走。
3. **维护窗：停观察运行**（主会话；**需要用户**——其他 magic 的遗留仓位）。前提：第 1、2 步都过了；T215 做完、记录写完
   （观察运行是它的真实样本）；在维护窗内。下面的顺序不能换——`--stop` 之后四个角色都没了，全平指令就没有人执行了：
   1. **停用旧任务**，逐个点名。先只读导出全部任务，按实际的清单核；下面这些是从文档里凑出来的：`\MT5Quant\` 下
      `live_supervisor2`（树外 `host.py` 包装版，每分钟触发）；根目录下的一次性载体任务 `\emergency-restart-1123` 和
      `\deploy-consolidated-20261117`；本来停用的也逐个核对仍是停用——`live_supervisor`、`live_daily_report`、
      `demo_weekly_check`、`refresh_bars`、`refresh_ticks`、`snapshot_specs`、`watch_swap`；装过看护任务的话，
      `terminal_keeper` 也停用。清单里凡是动作指着工作区的，一个不留。导出的 XML 存进 `<生产根>\archive\tasks-<日期>\`。
   2. **killSwitch**：在 `<工作区>\data\live\` 下建 `killSwitch`。风控下一轮写 `halt.json`，停开仓。
   3. **写全平指令**：用观察运行的部署树里的 `mt5live.commands.write_flatten(<工作区>\data\live, reason="moveout-<日期>",
      now=<当前 UTC>)`。只写文件，不碰终端；平仓由运行器经网关执行，halt 不挡平仓。killSwitch 只停机、不平仓，
      代替不了这一步。
   4. **等确认**：两个 magic 的 `flatten.ack-<magic>` 出现、风控把指令归档（`commands\archive\` 下多一个文件，归档之后
      ack 被删掉）；从对账日志确认 902610、902501 零持仓、零挂单。
   5. **其他 magic 的遗留仓位或挂单**（例如 902601 的）：记下 ticket，**请用户在终端界面里手工平掉**，写进 phase-f-log。
      不写脚本调 MT5 接口去平（D82），主会话做不了界面操作。确认账户上所有 magic 零持仓、零挂单。
   6. **`--inspect`**：用观察运行自己的那一套参数——`<工作区>\.venv\Scripts\python.exe`、观察运行的部署树里的
      `scripts\live_supervisor.py`、`cd6fc4a` 的完整 40 位、`<工作区>\data` 等，照 11-23 的启动参数。退出码不是 0，走
      下面"被拒绝时"。
   7. **`--stop`**：同一套参数。`demo_run.ps1 -Stop` 只是它的转发。核对：四棵树（启动器、解释器、conhost）都不在进程表里；
      登记保留。
   8. **保全现场**：`<工作区>\data\live` 的快照、`data\ops-evidence\`、`alerts.log`、supervisor 的日志。被独占的锁文件
      复制不了，属正常，记进清单。
   9. **旧数据目录归档**：把 `<工作区>\data` 整个复制到 `<生产根>\archive\workspace-data-<日期>\`，对源和目标各出一份
      文件清单和 sha256，两份逐行相同。**原件不删**——删除要用户确认（O21-6）。归档之前确认第 1 项做完了：停用着的旧
      supervisor 任务一旦被重新启用，会按登记缺失写 halt，或者去补进程。
4. **D104：读两个字段的实测值**（主会话；单列一步，只在维护窗内做这一次）。前提：第 3 步做完了——没有任何业务进程
   连着终端，PaperSoak 还没起；终端在运行。候选的网关在终端进程不在时不调 `initialize`，直接报"MT5 终端进程不在"
   （§6.5）。终端不在的话**不从 agent 会话启动它**：先做第 5 步，由看护任务拉起；或者请用户从桌面手动启动（D111 允许
   维护窗内的人工操作）。
   - 方法：走仓库的 `MT5Gateway`（候选部署树里的），只读。加载 `<生产根>\gateway.json`，
     `with MT5Gateway(cfg) as gw: info = gw.terminal_info()`，取 `trade_allowed`、`tradeapi_disabled`；记下终端的 build
     和 MetaTrader5 包的版本（`requirements.txt` 钉的是 5.0.6231）。不取行情、不下单。
   - 看读取进程下面有没有闪过 `terminal64.exe`：读之前取一次进程表（`Get-CimInstance Win32_Process`，只读），查父 PID
     等于读取进程 PID 的进程。读取脚本在 `terminal_info()` 返回之后、关闭网关之前停上十几秒，这段时间里每秒查一次；
     第二次必须在读取进程退出之前取。真实的 MetaTrader5 包在终端已经运行时调 `initialize()` 会不会生成一个短命的
     `terminal64.exe` 子进程，还没有验证过，这是验证它的机会。
   - 读取脚本放在 `<生产根>\` 下，不入库。
   - 期望：`trade_allowed=True`、`tradeapi_disabled=False`（起算前提第 5 条）。`terminal_info()` 缺这两个字段时，运行器
     按"不允许"处理，第一轮就发 `ALGO_TRADING_DISABLED`。不是期望值就停下汇报，不起业务进程。
   - 结果——两个实测值、终端 build、包版本、读取进程下面有没有出现过终端、时刻——写进部署记录和 phase-f-log。
5. **装看护任务**（主会话）。终端由看护任务启动。装之前先注销同名的旧任务：安装脚本遇到同名任务会拒绝，不覆盖（卸载
   只用 `-Uninstall`）。命令见 §6.5"看护任务的安装"，路径给成：

   ```powershell
   powershell -ExecutionPolicy Bypass -File "<生产根>\deploy\scripts\ops\install_terminal_keeper.ps1" `
     -TerminalPath "<终端安装目录>\terminal64.exe" -DeployDir "<生产根>\deploy" -DataDir "<生产根>\data"
   # 打印的计划核对无误之后，同一条命令加 -Install。装完用 -Status -DataDir "<生产根>\data" 查
   ```

   - 核对：打印的 `KEEPER_TASK_PARAMETERS` 里动作、主体（`Interactive`、`Highest`）、`-LogPath`（在
     `<生产根>\data\ops-evidence\terminal_keeper\` 下）都对；终端运行着的时候，看护脚本自己的 `-DryRun` 输出 `running`。
     装完等一两分钟：`-Status` 里 `state` 是 `Ready`、`last_task_result` 是 0，`KEEPER_STATE` 的 `outcome` 是 `running`、
     `age_s` 只有一两分钟。
   - 终端本来不在：一两分钟内被拉起，新终端的父进程是看护的 PowerShell，不是 agent 会话，也不是任何 worker。拉起之后
     补做一次 `-DryRun`（必须输出 `running`），再回到第 4 步。
   - 出错了怎么办：安装脚本把不对的地方一次列全，有不对的就什么都没做，改了再跑。看护被拒绝（退出码 1）没有告警
     通道，只有任务的上次结果码，`-Status` 就是查它用的。
6. **PaperSoak**（主会话；演练需要用户）。前提：第 3–5 步都做完了；看护任务在位。
   - **单独的数据目录** `<生产根>\data-paper\`。里面要有 `specs\latest.json`（脚本按数据目录找它）；策略配置、点差模型、
     风控和会话配置都用参数指向，和 Demo 共用同一批只读的文件。
   - 预检。不连终端——PaperSoak 没有网关预检；不带 `-DeploymentSha`，PaperSoak 拒收：

     ```powershell
     & "$root\deploy\scripts\demo_run.ps1" -Mode PaperSoak -PrecheckOnly `
       -PythonExe "$root\venv\Scripts\python.exe" -DataDir "$root\data-paper" `
       -ConfigDir "$root\data\demo-configs" -GatewayConfig "$root\gateway.json" `
       -RiskConfig "$root\deploy\config\risk.toml" -SessionsConfig "$root\deploy\config\sessions.toml" `
       -SpreadModel "$root\data\costs\spread_model.json" `
       -ResearchCache "$root\research\costs\spread_model.json" `
       -LogDir "$root\data-paper\demo" -MinFreeGB 40
     ```

   - **经计划任务载体启动**。一次性任务运行 `<生产根>\start-paper.ps1`；包装脚本调
     `<生产根>\deploy\scripts\demo_run.ps1 -Mode PaperSoak`，参数就是上面这些去掉 `-PrecheckOnly`，全部是 `<生产根>` 里
     的绝对路径；输出重定向到 `<生产根>\data-paper\demo\` 下的一个日志文件，退出码原样交回。载体任务的主体和 supervisor
     任务一样（`Interactive`、`Highest`）。**触发方式用一次性的时间触发**（一两分钟之后，由任务计划程序自己启动）：
     §6.7 里 D 死而 B 活的话只能用这一种；D 也活时仍然用它。
   - 启动后核对：四个启动器的父进程链是计划任务的宿主，不是 agent 会话；四棵树都是"启动器 → 解释器 + conhost"。
     父进程是 agent 会话的话停掉，改用计划任务重新部署。
   - **这一段没有 supervisor**（本节开头第 1 件事）：角色退出了不会有人拉起。任何角色退出、任何异常类告警，停下查根因，
     不直接重试（D92）。`demo_run.ps1` 也不会在同一个数据目录里给 PaperSoak 补缺——登记还在，它以
     `verified-tree-required` 拒绝。
   - 至少一个完整交易日。每个交易日做一次日常档检查，结论写 phase-f-log。
   - 这一段做的演练：
     - **演练 C 里不需要 supervisor 的部分**：结束终端 → 看护任务两分钟内拉起终端 → 运行器自动恢复。PaperSoak 的两个
       运行器连终端取行情，风控和对账读纸面状态、不连终端。取证按 §6.5 的验收段：用进程创建事件看 worker 的进程树里
       有没有出现过 `terminal64.exe`。
     - **宿主退出演练**（请用户关闭一次桌面端）：关闭前记下四个角色的启动器和解释器、终端的 PID、创建时刻、父进程链、
       会话 ID，做法同 §6.7 第 4 步。30 分钟以上之后核对都活着、心跳连续、看护任务照常每分钟运行。结果入 phase-f-log。
   - 演练 A、B 和带 supervisor 的演练排到第 8 步的末尾。
7. **Demo 烧机**（主会话）。
   - **停 PaperSoak**。PaperSoak 没有 `--stop` 可用（supervisor 和 `demo_run.ps1 -Stop` 都只认 Demo 的登记，见本节开头
     第 1 件事），只能人工停，顺序照 §6.3 的停机序列：
     1. 在 `<生产根>\data-paper\live\` 下建 `killSwitch`。
     2. 取进程表，按命令行认出四个启动器和四个解释器——数据目录都是 `<生产根>\data-paper`、解释器在 `<生产根>\venv`
        下——逐个记下 PID、创建时刻、父进程链。对不上的不碰。
     3. 子先父后：先解释器，再启动器。每个进程结束之前再读一次创建时刻，和记下的一致才结束——用
        `Stop-Process -InputObject <那个进程对象>`，不要拿 PID 再找一遍。
     4. 再取一次进程表，核对这八个进程都不在。纸面仓位不平，随数据目录归档。
   - **归档 PaperSoak 的数据目录**：复制到 `<生产根>\archive\data-paper-<日期>\`，数量和 sha256 清单对得上。原件不删，
     删除要用户确认。
   - **时点（D97）**：部署记录须在当日 12:00 UTC 前写成，赶不上就改到次日部署；从部署完成到当日 24:00 UTC 不少于
     12 小时，并且含一次换日窗。
   - 前提：账户上两个 magic 零持仓、零挂单（PaperSoak 不下单，第 3 步之后应当仍是零）；终端在运行、看护任务在位；
     `<生产根>\data\live\` 是新的——没有登记，没有 halt、pause、killSwitch、全平指令。
   - **经计划任务载体启动**。一次性任务运行 `<生产根>\start-demo.ps1`；包装脚本调
     `<生产根>\deploy\scripts\demo_run.ps1 -Mode Demo`，参数同第 2 步、去掉 `-PrecheckOnly -SkipGatewayCheck`，输出重定向
     到 `<生产根>\data\demo\` 下的一个日志文件——锚点证据的 sha256 只打印在这份输出里，部署记录要用。这次带 DEMO 保险丝
     和网关预检：连终端，读账户状态、逐品种读一笔报价，不下单。触发方式和主体同第 6 步。
   - 启动后核对：
     - 四个启动器的父进程链是计划任务的宿主，不是 agent 会话（D98）。是 agent 会话的话停掉，改正后重新部署。
     - 风控进程命令行里的 `--config` 是 `risk.toml`，不是 `risk-demo.toml`。不对就停掉，改正后重新部署。
     - 四棵树是"启动器 → 解释器 + conhost"，没有 `terminal64.exe`；输出里证据留存那一段没有报错。
   - **立刻写部署记录的启动部分**——完整 SHA、UTC 完成时刻、启动器 PID、各份配置的 sha256。D97 的 12:00 UTC 是硬线，
     其余各项装完任务之后补齐。
   - 烧机期的判据见第二十一轮增补 §3。
8. **装其余任务**（主会话）。前提：第 7 步的核对都过了。
   1. **supervisor**：先注销同名的旧任务 `\MT5Quant\live_supervisor2`——第 3 步已经停用；同名任务还在时，不带 `-Force`
      的注册会被拒——再按 §6.3 的注册命令装，变量全部换成 `<生产根>` 里的：`$deploy` 是 `<部署树>`，`$python` 是
      `<生产根>\venv\Scripts\python.exe`，`$data` 是 `<生产根>\data`，`$configs`、`$gateway`、`$risk`、`$sessions`、
      `$spread`、`$logs`、`$sha` 同第 2 步。登录类型 `Interactive`、运行级别 `Highest` 写在命令里。等它连续健康 5 分钟
      以上（`supervisor-state.json` 的 `healthy_since` 连续，`alerts.log` 没有 `SUPERVISOR_REFUSED`）；等到下一次触发，
      确认交互桌面上没有窗口弹出。
   2. **日报和周检**：§6.3.1 的命令，解释器和各目录用参数显式给出（`$deploy`、`$python`、`$gateway`、`$data`、
      `$logs`、`$spread`、`$sessions` 同上）。不给的参数沿用数据目录或者部署树下的旧位置，所以都给。安装脚本按名字覆盖
      旧任务，不用先注销；不带 `-IncludeDataTasks`。打印的 `TASK_PARAMETERS` 脱敏后抄进部署记录。
   3. **小时级存活检查**（§6.6）：等 U10 定了渠道再装；装的时候带 `--expect-keeper`，全部用显式的绝对路径。
   4. **三个数据任务**（`refresh_bars`、`refresh_ticks`、`snapshot_specs`）：不重装。它们的动作指着工作区，第 9 步要过，
      就得在停用状态下注销，第 3 步导出的 XML 留着底；A5 之后在第二终端上重装（D82、T198）。
   5. **PaperSoak 那一段做不了的演练在这里做**，supervisor 连续健康之后，每项的结果入 phase-f-log：演练 A（结束风控进程，
      supervisor 拉起）；演练 B（结束一个运行器，supervisor 拉起）；演练 C 带 supervisor 的那一次核对（结束终端之后
      supervisor 全程没有拒绝，`terminal64.exe` 没有出现在任何 worker 的进程树里）；宿主退出演练（请用户再关闭一次
      桌面端：四角色、supervisor、终端存活 30 分钟以上，心跳连续）。
9. **核对没有残留的工作区路径，写部署记录**（主会话）。
   - 只读导出全部计划任务（`Export-ScheduledTask`），连同包装脚本的内容一起搜工作区的路径，应当一处都没有。搜的
     范围：任务的动词和参数；`<生产根>\` 下的包装脚本（`start-demo.ps1`、`start-paper.ps1`）；部署树里被任务调用的
     脚本和生成的包装器（`scripts\ops\` 下的 `.ps1`、`.vbs`，`scripts\task_*.cmd`）；`<部署树>\.git\config`；
     `<生产根>\venv\pyvenv.cfg`；`data\live\instances.json`、`run-manifest.json` 和 `data\demo\demo-run-*.json` 里记的路径。
   - 生产进程的命令行和它们的父进程链里也没有；候选的进程树里没有工作区的解释器（映像路径都不在 `<工作区>` 下）。
   - 搜到一处就停下来处理——改参数，重装那一个任务——不带病往下。
   - 都过了，写部署记录，清单见下。

**`--inspect` 或 `--stop` 被拒绝时**（第 3 步的第 6、7 项）。T215 可能把观察运行打成了核验不过的形状——启动器没了而
解释器还在（§6.3 失效模式表第 9 行），或者终端挂在某个 worker 下面（第 10 行）。这时 §6.3 的停机序列走不下去：

- `--stop` 被拒绝时它什么都没结束：它先核验全部四个角色的身份，有一个不过就什么都不结束，核验过了才开始一棵一棵停。
- **先保全现场**（D109）：进程表（PID、创建时刻、命令行、父 PID）、`instances.json`、`supervisor-state.json`、
  `alerts.log`、退出标记，存 `<生产根>\archive\observe-<日期>\`，带 sha256 清单。拒绝的原文（角色、PID、映像名、父 PID）
  抄进 phase-f-log。
- **登记文件原样归档**。不清登记后满组重跑，不单杀启动器，不按历史 `pids.txt` 杀进程——§3"进程重启/停止"里的三条
  禁令照旧。
- **主会话人工逐个核对身份之后才处置**。每个进程的 PID、创建时刻（和登记里的相差不超过 1 秒）、命令行（脚本和全部
  参数同登记里的一样）、父进程链，逐项对得上才算认出来；对不上的不结束，记下来。处置的顺序是子先父后：先解释器，
  最后启动器。conhost 随它服务的进程自己退出，不用去结束它。
- 终端挂在 worker 下面时先停 worker：旧代码的 worker 会在重连时把终端再拉起来，worker 都停了它才不会再拉。停完之后
  终端是一个父 PID 已经不存在的进程。要不要重启它由主会话在维护窗里定——要重启，就从桌面手动启动，或者停掉它让看护
  任务拉起（D111 允许的两种方式），新终端的父进程就不能再是工作进程。
- 全过程写进 phase-f-log。

**用户注销、远程桌面断开时各会发生什么**（施工计划 §3.1 第 3 条。登录类型是从安装命令读出来的；后果是按计划任务
对 `Interactive` 的定义和代码推的，没有在这台机器上试过，也不要在窗口内试）：

- 这一部署里的任务——supervisor、看护、一次性的载体任务、日报和周检、小时级存活检查——登录类型都是 `Interactive`
  （只在用户登录时运行），跑在该用户的交互会话里。supervisor 和看护的运行级别是 `Highest`，由安装命令显式给出；日报和
  周检是 `schtasks /Create` 建的，运行级别是缺省值。装完按 §6.3 那条命令把每个任务的 `Principal` 读一遍，以读到的为准。
- **用户注销**：会话结束，会话里的进程全部结束——四个角色、终端、agent 桌面端自己。注销期间这些任务不再触发，持仓没有
  任何进程管。重新登录之后，看护任务一分钟内把终端拉起来；supervisor 任务发现四棵树整棵缺失，把角色拉起（先写一条
  halt，连续健康 5 分钟后自动解除）。**窗口内不得注销，不得让这个会话结束。**
- **远程桌面断开而没有注销**：会话还在，只是断了连接；会话里的进程继续运行，`Interactive` 的任务照常触发。断开本身
  不结束任何东西。要注意的是重连：11-17 10:43 用户侧的远程桌面重连之后一分多钟内，终端的算法交易开关被关、桌面端退出、
  四个进程死亡，机理到 11-23 仍未查明（O23-7）。窗口内尽量不重连；重连之后立刻跑一次 §6.6 的存活检查。
- 会话策略如果设了"断开多少分钟之后结束会话"，断开就等于注销。装任务之前只读地查一次，有这一项就请用户改掉。

**部署记录要有的**（第二十二轮增补 §3 第 5 步，"包装脚本 sha256"一项换成"没有树外补丁"；加 D112 和施工计划 §3.1
第 3 条的两项）：

1. 候选的完整 SHA 恰好一个（40 位小写），一个显式的 UTC 完成时刻。D97：当日 12:00 UTC 之前写成。
2. `instances.json` 里 4 个启动器 PID 逐字照录；解释器 PID 另列，注明口径（不算入锚点 PID）。
3. 两份策略配置的 sha256；风控配置的路径、sha256 和 `daily_loss_cap_usd` 的值（500.0）；会话配置的 sha256。
4. **没有树外补丁**：`host.py` 包装链已撤除，supervisor 走树内的 `scripts\ops\hidden_task.vbs` 和
   `scripts\ops\supervisor_once.ps1`；两者的 sha256。
5. 每个任务的登录类型、运行级别、所在会话，和动作的完整命令行——supervisor、看护、日报、周检、小时级存活检查、
   一次性的载体任务。路径换成占位符，账户名不入库。载体任务的包装脚本（`<生产根>\start-demo.ps1`）的 sha256。
6. `trade_allowed`、`tradeapi_disabled` 的实测值，终端的 PID 和父进程（父进程不能是工作进程），终端 build、
   MetaTrader5 包的版本——维护窗内、业务进程启动之前读的那一次（第 4 步）。
7. 核对结果：四个启动器的父进程链是计划任务的宿主；风控的 `--config` 是 `risk.toml`；交互桌面上没有弹窗；没有残留的
   工作区路径（第 9 步）。
8. 演练 A、B、C 和宿主退出演练的结果——各自做完之后补记。

**与施工计划的出入**。施工计划 §4 把演练 A、B、C 和宿主退出演练都排在 PaperSoak 阶段，并且都要看到 supervisor。supervisor
不认 PaperSoak 的登记（本节开头第 1 件事），所以演练 A、B、带 supervisor 的那次演练 C 和"supervisor 也存活"的宿主退出
演练只能在 Demo 起来、supervisor 启用之后做，用户要多关闭一次桌面端。PaperSoak 阶段做的是演练 C 里不需要 supervisor 的
部分，和"四角色加终端"的那一次宿主退出演练。

## 7. 执行方分工（D63/D64，2026-11-07 第十六轮生效）

- **交易终端专用（全时段，D82/T192，2026-11-13 第十八轮生效，取代"仅正式窗口内"口径）**：任何会话（含子代理、调度会话）任何时段都不得对交易终端执行 `mt5.initialize()` 或任何取数。**例外只有三个**：窗口开始前的影子盘、计划内的一次换日禁入探针（§7a）、换日窗内运行的日报任务。研究取数一律走第二终端或既有离线缓存（11-06 11:22 事故教训）。数据类计划任务**已停用到正式检查结束（D82，修正第十七轮"改每周六"的口径）**。
- **正式窗口内交易终端只服务三个实盘进程**（T182，第十七轮增补，同第十七轮计划 §1 第一条规则）：正式窗口内，交易终端只给运行器、风控、对账三个进程用，不在其上运行临时脚本或数据下载，临时查询也不连这个终端；确需交易时段取数时，用第二个终端（便携模式另装、独立数据目录，可选做法见第十七轮计划 T182 第 4 步）。数据类计划任务已改每周六（休市日）运行（`scripts/setup_scheduled_tasks.ps1`；**正式窗口期停用，D82**）。
- **运行时唯一执行方＝主会话**（用户指定，D64）：重启进程、清除 halt/pause/killSwitch、演练、部署、正式窗口计数，只由主会话做；git 身份 `zcode-main`；main 分支只由主会话写入。
- **每日调度会话只读**（13:15 UTC，标题"每日只读运维检查"）：只做检查与日检记录（git 身份 `zcode-scheduler`，提交到 `ops/phase-f-log` 分支或本地 ops 目录由主会话合并）；不做任何运行时动作、不计数交易日、不运行 `demo_check --accept`、不推 main。发现"需要处理"事项（进程死亡/心跳过期/新 halt 或 pause/对账差异/日报缺失/磁盘<30GB/真实账户迹象）只报告，由主会话处置。
- **正式窗口按 D63 起算**：部署树旧版 `demo_check --accept` 结果不作为 A5；验收只用 main 新版 `--accept --formal-window`；A5 材料交外部审核会话。

**读数注记（R1，T140/T141 评审）**：`rejected` / `blocked_broker_unavailable` 回报里，若 error 串含"净持仓未知"，表示该轮 net_volume 查询也失败且无前值——net 字段的 0.0 是 float 结构占位、**不是"净持仓=0"的测量**；处置前先从券商侧核实实际持仓，不要按 0 处理。

### 7a. 换日禁入探针（窗口内演练项，T192/D82 例外之一）

目的：XAUUSD 生产 M15 策略在换日禁入窗（NY 17:00 前 5 分钟至后 60 分钟）内
结构上不可能自然产生开仓尝试（窗内无 M15 收线、22:00 后报价陈旧被跳过），
需用探针在窗内真实走一遍闸门。

步骤（每个正式窗口只做一次）：
1. 先确认无陈旧 halt（`data\live\halt.json` 不存在）；
2. 建一次性计划任务于 **21:56 UTC**（报价仍新鲜、已在禁入窗内），以演练
   magic **902603** 跑真实运行器 `--once`：数据目录用生产 live，探针日志
   写到 `data\drill\logs`（不得落在日志目录，否则正式检查判无效）；
   配置复用 `data\drill\drill-902603-buyhold.toml`（BuyHold 恒信号）；
3. 跑完核对三件：alerts.log 有 `RISK_REJECT … forbidden_session`（902603）；
   902603 无任何成交与订单行；探针自身的心跳/状态文件按 11-05 设计清理
   （防风控误判陈旧心跳）；
4. 描述符生成时带 `--drill-magic 902603`（T191），正式检查将正确归属。

### 7b. 环境事件与共享机器纪律（T185 §14 取证结论合入，2026-11-13）

- **断线类（NotConnectedError −6 / 平台日志 connection lost）属预期内环境事件**：
  出网路径抖动会周期性触发，终端通常 1 秒内自愈；单窗 >900 秒由 T141 以
  rc=78 退出、supervisor 拉起。日检（T194）解析数据目录 `Logs\yyyymmdd.log`
  统计当日 `connection … lost` 行数与最长重连空窗（>60 秒记观察、>900 秒记
  "需要处理"），并摘录 authorized 行 ping/链路质量（连续 >300ms 或 <50%
  视为劣化）。
- **整机重启纪律（D85）**：正式窗口期内任何会话不得重启这台电脑；Windows
  更新已暂停至 2026-12-05、启用"有用户登录时不自动重启"（T193，A5 后恢复）；
  电源设为交流供电不睡眠/不休眠/硬盘不关。重启后恢复序列=先终端后三进程。
- **证据留存**：发生 rc=75/−6/重启类事件后，当日冷拷贝 `Logs\yyyymmdd.log`
  （前后各一天）入 `data/ops-evidence/`，与 exit/gateway 标记同存。
- 中期评估：同类环境原因连续两次导致窗口失败时，下一轮审核评估迁移云主机/
  VPS（计划 §5）。
