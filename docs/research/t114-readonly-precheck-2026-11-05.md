# T114 主段只读预筛接线（2026-11-05）

新增 [`run_h_readonly_precheck`](../../src/mt5quant/h_readonly_precheck.py) 与显式合成 [CLI](../../scripts/h_readonly_precheck.py)，实际消费 bars 并复用既有主段元数据契约、覆盖并集、来源成本和功效计算。结果仅为离线工程诊断；`research_status=not_evaluated`、`execution_status=not_released` 恒定。`engineering_pass` 不构成 candidate pass、数据资格、R2/A1 或研究批准。

## 固定接口与范围

```python
run_h_readonly_precheck(candidate, *, inputs, cost_model,
                       contract_sizes, cumulative_n=None) -> dict
```

`candidate` 是冻结装载器校验的 H 声明，固定 EURUSD/H1 交易和 USTEC/D1 信号。`inputs` 仅有这两个字符串键，每项严格包含：

- `bars`：DataFrame，至少有唯一的 `time`、`close` 列；时间是 bar 开盘时间，唯一、升序，aware 转 UTC，naive 按 UTC；H1 必须整点；价格有限且正。
- `source_tags`：与 bars 等长的规范来源；EURUSD 为 `histdata`/`mt5`，USTEC 为 `fred`/`mt5`。每根 bar 必须落在同来源声明覆盖内。
- `coverage`：该输入对的 `CoverageSegment` 列表。
- `missing_kind="missing_half_open"`、`missing_intervals`：实际缺失 `[start,end)` 的 `TimeInterval` 列表。必须显式给出，`[]` 表示已核对为空；未知拒绝。已有 bar 不能落在声明缺失内；不把 legacy 已有相邻 bar 端点当缺失。
- `source_eligibility`：覆盖中每个来源对应 `synthetic_only`。这是合成夹具声明，不证明实际生产资格；其他声明拒绝。

EURUSD 只保留 UTC `[2007-01-01,2021-05-17)` 的 HistData bars，再按冻结 `ext.merge` Sunday/DST 纯函数剔除违例月。USTEC 保留主段及起点前 40 天预热，主段之后的数据排除。范围触边按半开语义；前月信号保留。MT5 交易诊断及后段价格不会进入主段参考价、费用、波动或年数。

保留 bar 的实际开盘时间跨度交给 `runtime_coverage`，其内部调用 `merged_coverage_years` 合并。显式缺失、被排除的内部 bar 块和覆盖断点转换成过滤后真实相邻 bar 端点；沿用原有严格大于 30 天自动切段。辅助预热不计年数。输出的 counted 标记只用于合成覆盖算术，不代表来源批准。

## 计算口径与最小适配

- 文献/持仓声明由现有候选校验器复核。费用倍数门 3、单次功效门 0.5、alpha 0.05 和 Lo 近似直接复用冻结常量/功效函数。`cumulative_n` 只接受调用者明确已核对的正整数，拒绝 bool；缺失输出 `n_tests=null,power=null,status=unknown`，不以候选数或预算补齐。
- 参考价是最后保留的合法主段 bar 收盘值。费用沿用旧预筛的单次较不利全幅点差、双边滑点、来回佣金/合约价值、声明计费夜数乘较不利双向隔夜费。`source_costs` 仅把 external spread 乘 1.5；滑点、佣金、隔夜不随此倍率改变。合约及费用必须显式，缺分量不补零；明确全零总成本因倍数无定义而拒绝。
- API 和 CLI 当前仅支持**绝对每手隔夜费率**；`SwapRule.ref_price/contract_size` 非空时表示相对基准，数值计算前拒绝，不能把原每日金额直接除以当前合约价值。旧 `precheck` 对拍仅限绝对费率域，不证明相对口径正确。未来相对模式须另立规则并独立审核。API 要求 `CommissionModel(per_symbol={"EURUSD": fee})` 的显式记录，`fee=0` 合法；缺 EURUSD 记录时不接受默认值回退，即使默认值非零。
- `window_volatility` 所在包初始化会间接加载 gates/引擎，因此新入口没有运行时导入它。新模块仅适配其无条件 `close[i+h]/close[i]-1`、纽约入场小时和 `ddof=1` 公式，并增加主段收盘端点/缺口切段。H1 起点取锚 bar 开盘加一小时，不按候选信号选择窗口；起点和终点收盘时刻均在主段且窗口不跨缺口。无缺口合成数据逐位对拍原函数与旧 `precheck`，不复制整个预筛。
- 无法定义波动或没有正覆盖时功效为未知，不伪造 0；恒价的已定义零波动保留冻结的声明 Sharpe 回退。已知波动时用 min(声明 Sharpe, 隐含 Sharpe)。累计 N 功效单独报告，不替换原单次功效门。
- 新入口没有调用旧 `precheck` 的磁盘成本/价格路径。CLI 对已读取 bytes 解析后用现有 `SpreadStat/SpreadModel`、`SlippageModel`、`CommissionModel`、`SwapRule/SwapModel` 装配。Spread 保留原 `to_dict()` 结构和 `mode/sampling/bucket_tz` 语义；此入口比旧路径严格，要求这些键显式存在、拒绝未知字段。Swap 的日历选项沿用原类约定；预筛按声明 fee-nights 计算，不模拟日历收费。

## CLI 与工件身份

```sh
python scripts/h_readonly_precheck.py --mode synthetic \
  --inputs fixture/inputs.json --candidate fixture/candidate.toml \
  --costs fixture/costs.json --specs fixture/specs.json \
  --cumulative-n 17 --output fixture/new-output
```

所有输入均显式支持 JSON/TOML；不提供默认数据根。manifest 顶层为 `mode="synthetic"` 和 `inputs`；每项以相对 manifest 目录的 `bars_file` 代替 API 的 bars/source_tags，覆盖与缺失记录以对象表达。价格工件恰有 `bars=[{time,close},...]` 和 `source_tags`。资格在打开任何价格工件前整体校验，production/self-approved 模式拒绝。

成本顶层恰含 `mode="synthetic"`、`spread`、`slippage`、`commission_per_lot`、`swap`。Spread 是标准导出结构；slippage 显式含 `spread_fraction/fixed_price/event_hours/event_multiplier`；佣金为 `{EURUSD: number}`；swap 为 `{EURUSD: {long_per_day,short_per_day,...日历可选项}}`，日历可选项为 `triple_weekdays/charge_weekdays/charge_local/charge_tz`，非空相对基准 `ref_price/contract_size` 拒绝。规格恰含 `mode="synthetic"` 与 `contract_sizes={EURUSD: positive_number}`。未知键、重复 JSON 键、非有限数、缺必需分量均拒绝；显式零费不是缺失。

每个解析路径只读取一次，原 bytes 同时用于解析及 raw SHA256；输出区分工件 raw hash、规则 canonical JSON hash 和代码文件 hash。哈希证明内容身份，不授予资格。默认仅 stdout；`--output` 必须是新独占目录，只创建独占 `result.json`。不写 candidate precheck、SQLite 或任何输入。

## 依赖边界、测试与未决

[`tests/test_h_readonly_precheck.py`](../../tests/test_h_readonly_precheck.py) 使用合成 bars、临时合成旧格式库和真实 subprocess CLI，验证范围与诊断扰动不变、UTC/触边/前月预热、短缺口与坏月切段、费用分量、缺 N 未知、原函数/旧预筛无缺口对拍、全部 JSON/TOML 工件、同批 bytes 身份、输入不变和独占输出。

冷进程在首次导入前安装守卫。冻结 `research.__init__` 会加载 ledger 定义，此为明确的窄依赖例外；不实例化 Ledger、不调用 ledger 函数、不连接 SQLite、迁移或运行哈希链。守卫继续禁止引擎、labels、fit、event_returns、随机化和 merge/数据读取器；只允许指定独占结果文件写入。未改包初始化或伪造模块绕过依赖。

真实来源年度资格、完整交易日历、FRED/MT5 收盘映射、实际主段覆盖、真实费用依据、D33 累计 N、mtd_sign 随机时点缺当月收盘锚仍未解决。这里没有执行真实研究、收益/标签、采样、拟合、MT5、部署或 Demo 放行；所有真实接入须另有明确范围和独立审核。主会话统一执行全仓验证，本任务只运行聚焦合成及直接兼容测试。
