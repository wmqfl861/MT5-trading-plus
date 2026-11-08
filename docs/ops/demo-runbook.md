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
# 一次性：在维护窗口显式指定模式安装每日/每周计划任务
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
1. `type data\logs\task_live_daily_report.log`——最常见：影子状态不可用
   （退出码 2，fail-closed 不落半截日报）→ 确认运行器活着、
   `data\live\paper\state-*.json` 存在且非空；
2. 点差比对报 `model_sha256` 不一致 → 重跑 `scripts/build_cost_model.py`
   重建 `data\costs/spread_model.json`（日报拒绝用被动过的模型）；
3. 手动补跑（保持已生成 `scripts/task_live_daily_report.cmd` 中的模式和配置路径；首次生成前使用与安装器相同的配置选择顺序）。Demo：
```powershell
$gwConfig = @("config.json", "gateway.json") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $gwConfig) { throw "缺少 Demo 网关配置" }
.venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-11-10 `
    --demo --gw-config $gwConfig `
    --spread-model data\costs\spread_model.json --log-dir data\demo
```

PaperSoak 离线补跑不需要网关配置，直接使用影子状态：
```powershell
.venv/Scripts/python.exe scripts/live_daily_report.py --date 2026-11-10 `
    --paper-state data\live\paper `
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

#### 4.1.1 运行时自动留存的工件与描述符生成器（T180）

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
快照/决策截止时间分离；读取期间新写入的心跳不误判未来，真正未来仍拒绝。
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
$risk = (Resolve-Path '.\config\risk-demo.toml').Path
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

沿用 §6.2 的变量，仅在维护窗口明确批准后安装。任务只运行一次 supervisor，
由 Windows 每分钟触发；不要在本次开发验证中安装或执行真实业务动作。

```powershell
$supervisor = Join-Path $deploy 'scripts\live_supervisor.py'
$argv = @('-u', $supervisor, '--directory', $deploy, '--sha', $sha,
  '--data-dir', $data, '--config-dir', $configs, '--python', $python,
  '--gateway-config', $gateway, '--risk-config', $risk,
  '--sessions-config', $sessions, '--spread-model', $spread, '--log-dir', $logs)
$quoted = ($argv | ForEach-Object { '"' + $_ + '"' }) -join ' '
$action = New-ScheduledTaskAction -Execute $python -Argument $quoted -WorkingDirectory $deploy
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
  -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskPath '\MT5Quant\' -TaskName 'live_supervisor' `
  -Action $action -Trigger $trigger -Settings $settings

# 计划内停止业务前先停用 supervisor，防它按职责补回缺失进程。
Disable-ScheduledTask -TaskPath '\MT5Quant\' -TaskName 'live_supervisor'
# 然后设置 killSwitch，核验并停止整个父子树；仅杀 venv launcher 不够。
New-Item -ItemType File -Force (Join-Path $data 'live\killSwitch')
# $argv 已含解释器之后的脚本及全部固定部署参数。
& $python @argv --inspect
if ($LASTEXITCODE -ne 0) { throw 'identity verification refused; keep halted' }
& $python @argv --stop
if ($LASTEXITCODE -ne 0) { throw 'tree stop incomplete; preserve registry and investigate' }
# Stop 保留登记；不要自动删除 halt/killSwitch/journal。
# 卸载（不改变业务进程、halt、killSwitch 或历史证据）
Unregister-ScheduledTask -TaskPath '\MT5Quant\' -TaskName 'live_supervisor' -Confirm:$false
```

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

### 6.4 验收与部署边界

`demo_check` 可用 `ops-restarts.jsonl` 解释相应运行器重启，但不会用它补齐
日志/心跳空洞，也不会据重启记录证明连续存活。逐小时日志验收仍是有限证据，
不能替代实际故障演练。离线 blocked-fake、未知订单、PID 复用/父子树、健康
窗口与 PowerShell 接线测试通过，仅说明代码契约可验证；**CI、真实 Demo
断线/硬卡死/重启演练和计划任务安装均待主控在维护窗口单独执行**。

## 7. 执行方分工（D63/D64，2026-11-07 第十六轮生效）

- **正式窗口内交易终端只服务三个实盘进程**（T182，第十七轮增补，同第十七轮计划 §1 第一条规则）：正式窗口内，交易终端只给运行器、风控、对账三个进程用，不在其上运行临时脚本或数据下载，临时查询也不连这个终端；确需交易时段取数时，用第二个终端（便携模式另装、独立数据目录，可选做法见第十七轮计划 T182 第 4 步）。数据类计划任务已改每周六（休市日）运行（`scripts/setup_scheduled_tasks.ps1`）。
- **运行时唯一执行方＝主会话**（用户指定，D64）：重启进程、清除 halt/pause/killSwitch、演练、部署、正式窗口计数，只由主会话做；git 身份 `zcode-main`；main 分支只由主会话写入。
- **每日调度会话只读**（13:15 UTC，标题"每日只读运维检查"）：只做检查与日检记录（git 身份 `zcode-scheduler`，提交到 `ops/phase-f-log` 分支或本地 ops 目录由主会话合并）；不做任何运行时动作、不计数交易日、不运行 `demo_check --accept`、不推 main。发现"需要处理"事项（进程死亡/心跳过期/新 halt 或 pause/对账差异/日报缺失/磁盘<30GB/真实账户迹象）只报告，由主会话处置。
- **正式窗口按 D63 起算**：部署树旧版 `demo_check --accept` 结果不作为 A5；验收只用 main 新版 `--accept --formal-window`；A5 材料交外部审核会话。

**读数注记（R1，T140/T141 评审）**：`rejected` / `blocked_broker_unavailable` 回报里，若 error 串含"净持仓未知"，表示该轮 net_volume 查询也失败且无前值——net 字段的 0.0 是 float 结构占位、**不是"净持仓=0"的测量**；处置前先从券商侧核实实际持仓，不要按 0 处理。
