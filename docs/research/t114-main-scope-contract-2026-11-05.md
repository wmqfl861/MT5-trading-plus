# T114 主段覆盖元数据契约（2026-11-05）

本模块完成固定主段的离线元数据校验，尚未接入既有 CLI。`metadata_valid` 仅表示所给元数据通过本契约，所有结果的 `execution_status` 恒为 `not_released`。它不证明真实覆盖、来源批准、候选通过或研究可执行。

实现：[`src/mt5quant/h_main_scope.py`](../../src/mt5quant/h_main_scope.py)。合成测试：[`tests/test_h_main_scope.py`](../../tests/test_h_main_scope.py)。依据：[T114 草案](t114-r2-draft-2026-11-03.md)及 [merge 头部契约](../../src/mt5data/ext/merge.py)。模块仅依赖标准库，直接放在 `mt5quant` 根包，不引入研究、执行器或数据模块，不改包初始化、旧 API、候选/轮次草案与冻结实现。

## 固定范围与接口

- 交易：EURUSD/H1，主段为 UTC `[2007-01-01T00:00:00Z, 2021-05-17T00:00:00Z)`，仅 `histdata` 进入主段。`mt5` 交易段全部排除；主段外 `histdata` 片段也排除。
- 信号：USTEC/D1，允许 `fred` 或 `mt5`。信号独立处理，不裁到交易起止；保留前月和其他主段外元数据。此处不读取或推断任何收盘锚价。
- 本版本不支持替换品种、区间、时区、阈值或来源优先级。

```python
TimeInterval(start, end)
CoverageSegment(symbol, timeframe, source, start, end)
validate_h_main_scope(*, coverage, missing_intervals) -> ScopeReport
```

`TimeInterval`、`CoverageSegment` 和 `ScopeReport` 为标准库冻结记录。时间在区间构造时规范为 UTC-aware `datetime`；naive datetime 按 UTC。字符串接受明确的 ISO 子集：`YYYY-MM-DD`，或日期后接 `T`/空格和 `HH:MM[:SS[.ffffff]]`，可附 `Z` 或 `±HH:MM`。不接受模糊日期、紧凑日期、其他任意分隔符、数字时间戳、`date` 对象、闰秒、24 点、超过六位的小数秒或无法表示的 UTC 日期；不截断精度。每个区间必须满足 `start < end`，比较在 UTC 归一化之后执行。

`coverage` 必须为 `CoverageSegment` 序列（例如 list 或 tuple）；不把 dict、普通元组或生成器隐式转换成记录。合法品种/周期与小写来源组合仅为：

| symbol | timeframe | source |
| --- | --- | --- |
| EURUSD | H1 | histdata、mt5 |
| USTEC | D1 | fred、mt5 |

来源缺失、未知、大小写或空白不规范、非法组合、额外输入对均拒绝。错误类型抛 `TypeError`，无效值抛 `ValueError`；缺口证据未知也不会掩盖输入格式错误。来源名称只是调用者的声明，不代表来源已获批准。

## 缺口证据与规范化

`missing_intervals` 是以 `("EURUSD", "H1")`、`("USTEC", "D1")` 为键的映射；每个值是 `TimeInterval` 序列。两对都必须显式声明，且不接受额外键。

| 输入 | 含义 | 状态影响 |
| --- | --- | --- |
| 某对值为 `[]` 或 `()` | 调用者声明已核对且没有缺失 | 可继续校验 |
| 某对缺 key 或值为 `None` | 该对缺口证据未知 | `waiting_data` |
| 整体 `None` | 两对证据均未知 | `waiting_data` |
| 非空区间序列 | 声明实际缺失的时间半开区间 | 扣除与覆盖相交部分 |

先按品种/周期/来源合并相邻、重复、交叠覆盖；每对缺口同样规范为并集。交易先裁到主段，再扣自身缺口；信号仅扣自身缺口。所有相交与扣除使用左闭右开语义，触边不产生交集。输出不重复计算同一来源的重叠覆盖或缺口。

扣除后的信号如果存在不同来源的严格重叠，抛 `ValueError`，包括主段前后保留的元数据。不同来源触边相邻合法并分别保留；显式缺口已经完整移除的重叠不构成保留区间冲突。缺口证据未知时不假定能消除任何来源冲突，也不选择来源优先级。

交易扣洞后为空，或信号扣洞后没有任何主段内严格重叠，返回 `waiting_data`。这里只检查各自存在性：不证明整个主段被覆盖，也不证明信号与每段交易逐一对齐、前月收盘存在或日历有效。真实证据的完整性与真实性仍是后续前置条件。

## ScopeReport

| 字段 | 返回内容 |
| --- | --- |
| `status` | `metadata_valid` 或 `waiting_data` |
| `trade_intervals` | `CoverageSegment` tuple；规范化后的主段交易区间 |
| `signal_intervals` | `CoverageSegment` tuple；保留来源及主段外信号元数据 |
| `excluded` | `(CoverageSegment, reason)` tuple 的 tuple；已知被排除的输入覆盖 |
| `reasons` | 去重、按字符串升序排列的原因代码 tuple |
| `execution_status` | 恒为 `not_released`，不能由构造参数覆盖 |

**任何 `waiting_data` 结果的 `trade_intervals` 和 `signal_intervals` 均为空 tuple**，防止调用者误用局部结果。`excluded` 仍可保留已知诊断信息，但并非完整覆盖的补集，也不表示未知部分已获验证。

区间排序键为 `(symbol, timeframe, start, end, source)`，排除项再以原因代码打破平局。重复或重排输入不改变结果。输入容器不被修改，返回值不引用可变输入容器；返回记录及所有嵌套集合不可变。

稳定原因代码如下：

| 原因 | 含义 |
| --- | --- |
| `missing_intervals_unknown:EURUSD/H1` | 交易缺口证据未知 |
| `missing_intervals_unknown:USTEC/D1` | 信号缺口证据未知 |
| `trade_coverage_empty` | 未保留任何主段 HistData 交易覆盖 |
| `signal_main_overlap_absent` | 未保留任何与主段相交的信号覆盖 |
| `trade_mt5_diagnostic` | MT5 交易覆盖全部排除 |
| `trade_outside_main` | HistData 交易覆盖的主段外片段排除 |
| `declared_missing` | 显式缺口从相应覆盖中实际移除的片段 |

后三个排除代码也汇总到 `reasons` 中；合法裁切或扣洞并不自动变为 `waiting_data`。排除元数据保留来源，不应跨来源累加为研究年数或样本量。

## legacy gap_spans 转换边界

旧 `mt5data.ext.merge.gap_spans` 是相邻**已有 bar** 的 `(前末 bar 时刻, 后首 bar 时刻)`，包括旧模块识别的长缺口及被剔除块的两侧端点；旧长缺口规则为差值严格大于 30 天。本接口的 `TimeInterval(start, end)` 是实际缺失时间的 `[start, end)`，两者不能直接互传。

例如，只有外部已独立确认某来源是连续小时网格且 bar 时间为开盘时间后，才能判断已有 `00:00`、`03:00` 两根 bar 中间缺失的 bar 起始时间范围是 `[01:00, 03:00)`；不能把 `[00:00, 03:00)` 当作缺失而删除已存在的前一根。本模块不做这项转换，不推测周期、休市、交易日历、收盘映射或 30 天阈值。未来转换需单独明确并审核来源/日历/时间标签契约，再显式传入真正缺失区间。

## 验证与未完成事项

测试仅使用合成元数据，覆盖主段/MT5/后段混合、触边空交、缺口并集与精确扣除、未知与显式空证据、来源/输入对拒绝、信号前月保留与独立缺口、UTC 等价与无效时间、输入深度不变和结果不可变。隔离解释器冷导入阻止研究/引擎/数据模块，调用测试阻止文件 I/O。聚焦测试命令为 `PYTHONUTF8=1 python -m pytest tests/test_h_main_scope.py -q --tb=short`；实际解释器、受测 tree 和退出码只保存在任务私有 scratch 日志中。

本次不读取 bars、收益、data、SQLite 或 ledger，不收文件路径/数据根参数，不写文件，不连接 MT5，不执行研究、预筛或部署。没有候选 pass、功效、年数、累计 N、样本、成本或收益输出。

事件/随机事件范围控制、全段覆盖与覆盖年数口径、D33 累计 N 及其功效报告、合法完整日历、FRED/MT5 收盘映射、锚价与符号一致率均未在此完成。现有 CLI 的主段隔离、结果/台账写入范围及执行放行仍需后续落实和独立审核。`approved_by` 继续为空；R2/A1、main-only 真实预筛、真实研究和部署均未获本实现批准。
