# T121 离线计数与成本入口（2026-11-03）

本交付是**合成输入上的离线工程实现与验证**。它落实 [T121 预注册](pline-feasibility-prereg-2026-11-02.md) 和 [A2 最终裁定 §6](../reviews/2026-11-02-t121-a2-review.md) 中的计算规则；不构成真实筛查结果或运行许可。生产输入即使带有自称批准字段，也返回未计数的 `waiting_data`。未来生产准入 adapter 尚须独立审核、合法数据与成本证据审核及明确执行放行，当前入口不能立即执行真实筛查。T121 原报告仍未运行，T122/P1/P2 保持关闭。

## 调用与兼容接口

```sh
python scripts/pline_feasibility.py --help
python scripts/pline_feasibility.py \
  --input scratch/synthetic-input.json \
  --cost-evidence scratch/synthetic-cost-evidence.json \
  --sessions config/sessions.toml \
  --output scratch/new-t121-output
```

四项输入必须明确指定，没有数据搜索、默认成本、候选加载、审批开关或随机种子。输出目录必须不存在；失败产物保留，重跑使用新目录。`report.json` 含全部 96 行、8 个族/品种汇总、逐原因排除计数、输入范围/来源分段/排除月份/缺口；`receipt.json` 绑定输入与成本文件 SHA256、配置与资格规则 SHA256、A2 绑定预注册 SHA256、Git SHA、代码文件 SHA256、脏树标志及报告 SHA256，不写本机路径。工件哈希证明身份，不能证明批准。

纯内存主接口为：

```python
run_feasibility(h1_by_symbol, *, source_tags, gap_spans, sessions,
                input_evidence, cost_evidence) -> dict
```

各映射按 `XAUUSD` / `EURUSD` 索引；`sessions` 必须匹配当前冻结 `config/sessions.toml`，禁止注入成本缺失小时作为资格排除。缺品种保留对应未知行。`count_triggers` 保留既有默认值，仅新增关键字参数 `levels_by_bar=None`、`situation_starts=None`、`bars_per_day=24`、`s3_bars=480`；`level_rows_for_situation` 新增 `bars_per_day=24`；`eligibility_reasons` / `eligibility_mask` 新增关键字参数 `situation_starts=None`。窗首为实际 H1 UTC 时间戳、每输入 bar 一项，A2/A4 按 anchor 索引，不能四舍五入为 H4 根下标。计数的 ATR 改为复用既有 `costs.volatility.atr_series`，与引擎 Wilder ATR14 合成对拍一致，避免导入执行器。

局部纯函数 `prepare_bars(h1, source_tags, timeframe)`、`s2_levels(h1, target_times)`、`qualify_triggers(...)`、`trigger_cost(...)` 与 `summarize_hypothesis(...)` 支持定点合成验证；不会自行读取文件。

## 显式合成输入和证据

CLI 输入 JSON 顶层是 `symbols`；每品种包含 `bars`、`source_tags`、`gap_spans` 和 `evidence`。bars 为 `time/open/high/low/close` 记录，时间按 UTC 解释，H1 开盘标签唯一且有序；可选 `closed=false` 明确排除未完整根，否则调用方声明输入是已收盘 H1。来源使用 `mt5` 或 `ext:HistData:<年份>`；未知不推定为 MT5，也不删除后以剩余样本通过。每个 H4 只接收同一 UTC 桶的 4 个连续完整根。

输入 evidence 必须包括 `kind=synthetic`、`purpose=T121-count-cost`、`payload_sha256`、`range_start/range_end` 和 `source_manifest`（`used_years/sources/excluded_months`）。`input_payload` 生成需绑定的 bars/source_tags/gap_spans 对象，`payload_sha256` 使用排序键、紧凑分隔符的 JSON SHA256。声明范围必须覆盖输入且不越发现段。缺哈希、范围、用途或 manifest 时不计数。synthetic 标记是调用者的合成输入声明，不是证明实际文件由何处产生的安全认证；不能把生产数据改标后用于研究。

成本 JSON 按品种映射，每份 evidence 明确携带 `kind=synthetic`、`purpose=T121-count-cost`、`symbol`、Demo/USD 的账户与报价币种、`basis_ref`、`applies_start/applies_end`、`observed_start/observed_end`、`payload_sha256` 和 `payload`。payload 包含：

- `spread`：`mode`（absolute/relative）和纽约 `hours` 表，每小时 `p90/n_days`。
- `slippage`：显式 `spread_fraction/fixed_price/event_hours/event_multiplier`，使用达标点差表。
- `overshoot`：`version=2`、`basis=stop_level`、纽约小时相对价 `p90/n_days` 表。
- `commission`：`round_trip_per_lot` 和明确 `basis`；零必须显式提供。
- `contract`：有限正 `size` 与 `unit`。
- `swap`：`basis=free/charged`、`scope=Demo`；charged 合成例另需 `long_per_day/short_per_day/triple_weekdays/charge_weekdays`，可选已知 `ref_price` 作相对转换。本入口的 charged 合成证据格式目前只接受有限非正的 long/short 费率，比底层 SwapModel 接受正负费率的接口更窄；此限制须在未来生产准入审核中显式处理。当前 A2 实际依据仍只覆盖 Demo free，合成 charged 运算测试不批准实际 charged 费表。

完整、可直接构造的合成格式见 `tests/test_t121_feasibility.py` 的 `input_identity`、`cost_evidence` 和 CLI 测试。未读真实缓存，不为现有 2026 摘要计算任何绑定或诊断情景，不以缺省零补佣金/合约/越幅/隔夜费。

## 规则与结果解释

四族 H1/H4 × SL1/2 ATR、TP2R、K24/12；网格 XAUUSD=5、EURUSD=.005；S1–S3、A1–A4 全保留；warmup2000、approach24、最大宽洞30天、截止2016-05-01 UTC 不含。S2 用 H1 纽约17:00会话极值，在 H4 开盘绑定最近已完成会话；S3 通道与资格窗同步为 H1=480、H4=120 根。持有端点是 i+1 与 i+K+1，资格及计费只读取未来时间戳。

外部来源只放大点差1.5倍；点差来回一次、滑点两腿、佣金来回一次、覆盖达标小时最大 p90 越幅全额计入。入场点差小时和未知出场小时集合要求 n_days≥20；不达标属于成本缺测。隔夜费检查有限且≤0后取负转支出，按端点、纽约换日、周末和三倍日计算，价格换算使用信号已知价。p90 是冻结情景，不是期望成本上界。

计数与成本使用同一合格集合；成本先日内均值、再按触发根 UTC 日等权。样本门≥4521日、成本门≤.02R，均含等号；ΔEV=.06R、DEFF=1.5固定。成本非法保留 fail，样本不足/来源或必需成本缺测保留独立 waiting 原因。未运行为 null；实际零触发为0，不能混淆。每日触发分布与相邻日间隔仅作描述。族/品种汇总保留全部行，不能由合成 feasible 推导真实设计可行。

后续真实筛查仍需独立模块/整合审核、经审核的生产准入 adapter、获准的明确源范围、完整且符合 D50 的冻结成本、执行放行及产物身份核对；完成后也必须停在 A3，不自行进入 T122 或方法门。本次未改变预注册、旧报告、冻结 candidates/gate、门槛或成交语义。
