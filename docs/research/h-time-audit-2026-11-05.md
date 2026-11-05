# H 单文件时间投影器（2026-11-05）

`audit_time_file(path, *, source, symbol, timeframe, timestamp_semantics, start, end) -> dict` 位于 `scripts/h_time_audit.py`。参数均须为字符串，`path` 为显式本地绝对文件名。CLI 的七个必需长选项与此接口逐项对应：`--path --source --symbol --timeframe --timestamp-semantics --start --end`。计划文本的“八个”是计数笔误；不额外增加授权、输出或发现目录选项。成功 stdout 只有一个 JSON；拒绝输出 `{"error":{"code":"…"}}`，退出码 2，不回显路径或原异常。API 抛出仅携带稳定 `code` 的 `AuditError`。

| source / symbol / timeframe | timestamp_semantics | 可选非空半开范围 |
| --- | --- | --- |
| histdata / EURUSD / H1 | utc_bar_open | UTC `[2007-01-01, 2021-05-17)` |
| fred / USTEC / D1 | fred_observation_date | 日期 `[2006-11-22, 2021-05-17)` |
| mt5 / USTEC / D1 | utc_bar_open | UTC `[2006-11-22, 2021-05-17)` |

UTC 边界须含完整时间及显式 `Z` 或 `+00:00`，最多九位小数；UTC 输出固定九位纳秒。日期边界及输出仅为 `YYYY-MM-DD`。`series` 由品种和周期派生，如 `EURUSD_H1`。声明不认证来源、时区转换、数据使用资格或真实文件许可。

只接受唯一顶层标量 `time` 及唯一平面物理时间叶子；拒绝嵌套/同名/前缀歧义、INT96 和指向外部文件的时间列块。HistData/MT5 支持原生 INT64 timestamp 的 s/ms/us/ns，时区仅缺省或精确 `UTC`；缺省按调用者的 UTC 标签声明解释，D1 不要求午夜。内部使用精确有符号 64 位纳秒域，越界拒绝。FRED 支持 date32/date64，以及生产格式 naive timestamp[ns] 严格午夜；拒绝其他单位、时区、非午夜、字符串、裸整数。date64 按底层毫秒检查整日；Parquet DATE 是 INT32 日，PyArrow 写入 date64 后通常读回 date32，写入前已丢弃的子日信息无法从文件恢复。FRED 日期标签不表示收盘或价格可见时刻。

同一个只读内容句柄交给真实 `ParquetFile(memory_map=False, buffer_size=0, pre_buffer=False)`，按 row group 编号递增，以 `columns=['time'], use_threads=False, use_pandas_metadata=False` 分批读取。schema、footer、批次和前后 fstat 使用该句柄。先校验整列时间，包括范围外 null、非法标签和越界值，之后才筛选统计；不通过价格/列统计跳过数据，不调用业务读者、pandas、dataset 或 SQL。

统计保留所选行的物理顺序和纳秒精度，包括数量、精确唯一数、N−unique 重复数、相邻严格下降、H1 非整点计数、min/max。D1 非整点项为 null。仅列前 20 个相邻正向观察间隔，给出精确 `duration_ns` 或 `duration_days`；正向总数和最大间隔仍扫描全部所选行。无正向间隔时最大值为 null；空选择返回 `no_observations` 和零/空统计。不推导实际缺 bar、完整日历、覆盖年数或会话完整性。全列整数和精确唯一集合保留在内存中，本工具不是超大文件的资源隔离器。

读取承诺是“只解码 time，应用不消费或输出其他列值/统计，不主动全文件读/hash”，不是零价格字节 I/O：footer 含其他列元数据，尾部预读可能覆盖价格页，小文件甚至可能全部进入缓冲。`start/end` 只限制统计，不是读取或锁箱隔离。真实使用前仍须单独批准精确快照绑定、整列 time、footer 及可能触及的尾部字节，并确认整个文件非锁箱；若不能批准，应由材料方提供真正 time-only 或已裁范围文件。当前工程验收全部为人工临时 Parquet，未读取真实材料。

路径实现局限于 Windows 固定本地目录树、直接 `\Device\HarddiskVolumeN` 映射的固定盘符。先拒 URL/UNC/设备命名空间/ADS/相对路径/glob/点组件/保留设备名/尾随点空格等词法歧义，并拒 SUBST、网络或未知映射；不 resolve、不自动发现根目录。从核验后的卷根开始，使用官方 `NtCreateFile(FILE_OPEN)`，逐组件相对已持有的父目录句柄打开，设置 `FILE_OPEN_REPARSE_POINT`、同步 I/O 及目录/非目录类型要求；句柄属性再拒 reparse、设备、离线或未知对象。目录使用读取/列目录权限、共享 READ|WRITE、不共享 DELETE，固定到读取结束；最终文件仅读取权限、共享 READ，直接将 HANDLE 移交一个 Python 内容对象。目录和最终文件都不设置 backup intent。人工探针及真实拒绝测试发现属性专用目录句柄不能阻止重命名，`CreateFileW(BACKUP_SEMANTICS)` 在本机可绕过共享或显式 DACL，因此采用不带备份意图的局部原生打开，不调整进程 token、权限、DACL 或环境来让正链通过。本机实际 file/ancestor symlink、junction、独占共享、文件及祖先目录 DACL 拒绝和固定期间重命名均受测，无全部 skip 或恒拒绝替代正链；DACL 拒绝用例仅在人工临时对象上设置拒绝并在 finally 恢复。

非 Windows、非直接固定卷、缺失 API、权限/共享失败或未知对象均 `path_refused`；最小使用范围就是上述 Windows 本地固定树，不提供 POSIX 回退或通用安全框架。不声称抵抗受信 OS、卷映射管理员或目录所有者的恶意变更。前后 fstat 仅检测可见变化；发生变化则丢弃结果，不重试；稳定元信息不证明不可变快照。输出固定为 `kind=time_projection_diagnostic`、`research_status=not_evaluated`、`execution_status=not_released`、`snapshot_identity=unverified`。

专项验收为 `tests/test_h_time_audit.py`，使用既有 Python 3.12 / PyArrow 26.0.0；覆盖真实多 row group、小批次、冷 CLI、价格扰动不变、原生句柄与投影参数观测、错误清洗及无结果写入。命令、完整 RED/GREEN 日志、退出码、运行前后 tree 和三文件 SHA 位于本工作树 `.superpowers/sdd/2026-11-05-h-time-audit/`，不提交这些临时证据。独立代码审核和整合由主会话负责。

Win32 语义核对依据：[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[QueryDosDeviceW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-querydosdevicew)、[NtCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)。
