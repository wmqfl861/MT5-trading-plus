# H 单文件时间投影实施范围裁定（2026-11-05）

主会话转录未参与本轮提案或实现的独立审核者返回的裁定。基点 `c0df5a1d133eadbe8a8c089fba0758ed840d830b`。

**conditional：允许实现薄的单文件 time 投影器，仅用临时合成 Parquet 做工程验收。以下条件直接进入实施规格；真实文件访问尚未获准。** 无需新建授权平台，不接入或放宽现有 synthetic 预筛门。

## 时间语义

| source / symbol / timeframe | timestamp_semantics | 可选半开范围 |
| --- | --- | --- |
| histdata / EURUSD / H1 | utc_bar_open | UTC `[2007-01-01T00:00:00Z,2021-05-17T00:00:00Z)` 内非空子区间 |
| fred / USTEC / D1 | fred_observation_date | 日期 `[2006-11-22,2021-05-17)` 内非空子区间 |
| mt5 / USTEC / D1 | utc_bar_open | UTC `[2006-11-22T00:00:00Z,2021-05-17T00:00:00Z)` 内非空子区间 |

series由symbol/timeframe派生。FRED生产适配器和落盘代码使用 naive `datetime64[ns]`，必须兼容读取后的 `timestamp[ns,tz=None]` 且每个值为严格午夜，同时可接受date32/date64，date64不得隐藏非整日值。拒绝FRED带时区、非午夜、字符串和裸整数；输出 `YYYY-MM-DD`，不是收盘或价格可见时刻。`close_only`不说明时间语义。

HistData/MT5接受原生timestamp，单位s/ms/us/ns，时区缺省或精确UTC；缺省仅在显式utc_bar_open声明下解释为已转换的UTC标签，该声明不证明转换完成。拒绝其他时区、字符串、裸整数及INT96自动转换。保持纳秒精度；MT5 D1合法UTC非午夜开盘不得误拒，也不能加24小时推导收盘。真实MT5快照的server-time→UTC转换仍需独立核实。

## 读取与输出边界

- 只建立一个内容读取句柄并将文件对象传给PyArrow；schema/footer/批次及前后fstat均使用该句柄，不能先按路径读schema再重开。
- `ParquetFile(memory_map=False,buffer_size=0,pre_buffer=False)`；按row group递增读取，批次 `columns=['time'],use_threads=False,use_pandas_metadata=False`。PyArrow26默认pre_buffer为True，必须显式关闭。
- 解码前确认唯一顶层标量time及唯一对应物理叶子，拒绝同名歧义、嵌套time、不支持逻辑类型和需要外部文件的投影列块。列前缀匹配本身不足以证明唯一叶子。
- 扫描全文件time列后过滤选择范围；范围外null/非法标签也拒绝整个输入，不用列统计跳过row group。start/end仅为统计选择，不是读取或锁箱隔离。
- 准确承诺为：仅解码time，应用不消费/输出其他列值或统计，不主动全文件读/hash。footer含其他列元数据，初始尾部预读可能覆盖价格页、小文件可能整文件进入缓冲；不能声称零价格字节I/O。不访问其他列statistics、完整metadata字典、pandas metadata，不输出schema全文。
- 未来实际许可须覆盖精确文件整列time、footer及可能触及的尾部字节，并确认整个文件非锁箱。如果这些字节或范围外时间禁止访问，材料方必须先提供真正的time-only或已裁范围文件。

## 路径与信息清洗

输入为显式本地绝对常规文件；先校验词法和声明，再访问输入。拒URL/UNC/glob/目录、symlink/junction/reparse及相关祖先，Windows还拒驱动器相对路径、设备命名空间、ADS、保留设备名、尾随点/空格与 `..` 等需要折叠的输入；不能先resolve再过滤或自动找数据根。

逐级检查不跟随链接；权限不足、类型未知或平台能力不足返回path_refused。最终句柄应确认为预期普通文件。静态lstat加普通open不能宣称防路径替换竞态，最终组件no-follow也不自动保护祖先。第一版限可维持前提的固定本地目录树，能力不足拒绝；不能用事后fstat为已经跟随的读取补票。前后fstat仅检测可见变化，变化后丢弃结果不重试，稳定元信息不证明不可变快照。

成功stdout一个JSON；拒绝为稳定错误码，非零退出且无部分统计。包括CLI参数错误在内，stdout/stderr不回显输入绝对路径、价格或原异常。固定 `kind=time_projection_diagnostic,research_status=not_evaluated,execution_status=not_released,snapshot_identity=unverified`，不增加approval或PASS字段。

## 统计及验收

输出明确time_domain、input_time_type、timestamp_semantics、规范化边界和 `statistics_scope=selected_rows_in_physical_order` 及整列/footer读取说明。selected_count=N；唯一标签精确计数；重复为N减唯一数；下降为所选原序相邻值严格下降，跨批次/row group不重置。不排序去重，H1非整点仅计数。相邻观察间隔按所选原序子序列定义，不是缺bar；仅列正向间隔端点、最多20条及截断标记，扫描全选择计算最大正间隔。不输出missing_intervals、覆盖年数或会话完整结论。无选择行返回no_observations、零计数、空min/max和间隔。

实际验收须包括：锁定PyArrow26创建含人工time/价格的临时Parquet，经真实ParquetFile和冷进程CLI读取；FRED实际读回naive ns午夜与拒绝类；MT5非午夜；多row group和小批次的重复/下降/纳秒精度/触边/范围外非法；只有价格变化时诊断不变；一个内容句柄与指定参数；无额外数据文件或全表/hash读取；路径拒绝先于内容读取、权限/损坏/变化检测及错误信息清洗。新工具不导入预筛、研究、收益、拟合、采样、ledger或执行模块，不写结果文件。

审核依据包括现有 [FRED适配](../../src/mt5data/ext/adapters.py)、[落盘](../../src/mt5data/ext/core.py)、[MT5时间约定](../../src/mt5data/store.py)与PyArrow26官方 [Parquet实现](https://github.com/apache/arrow/blob/apache-arrow-26.0.0/python/pyarrow/parquet/core.py#L318)、[文件对象路径处理](https://github.com/apache/arrow/blob/apache-arrow-26.0.0/python/pyarrow/fs.py#L130)、[footer读取](https://github.com/apache/arrow/blob/apache-arrow-26.0.0/cpp/src/parquet/file_reader.cc#L439)。审核者没有执行合成Parquet探针（其隔离解释器未装PyArrow），也未运行套件或访问真实材料。实施后的测试和代码审核仍须完成。
