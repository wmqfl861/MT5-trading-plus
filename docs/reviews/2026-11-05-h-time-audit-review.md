# H 时间诊断独立代码审核（2026-11-05）

本记录由主会话转录未参与提案、范围裁定或实现的独立审核者直接返回的结论。**Spec PASS / Quality PASS；Critical、Important、Minor 均无发现。** 结论仅覆盖单文件时间投影器的工程实现，不授予真实文件读取、研究或 Demo 许可。

## 固定版本

- 基点及父提交：`c0df5a1d133eadbe8a8c089fba0758ed840d830b`。
- 受审提交：`e7a0f95f6443586fc7e8aefefd2dfa8674544e0e`。
- 受审 tree：`76eaf36c05f67d03edf4f11b445184b4ee217064`。
- 仅新增[脚本](../../scripts/h_time_audit.py)、[专项测试](../../tests/test_h_time_audit.py)及[模块说明](../research/h-time-audit-2026-11-05.md)，共1092行。
- 审核前后 HEAD、index 和文件未变，工作树干净；差异格式检查通过。

依据为[实施计划](../superpowers/plans/2026-11-05-h-time-audit.md)、[独立范围裁定](2026-11-05-h-time-scope-review.md)及实际代码、测试和日志。

## 独立判断

1. **单句柄投影。** 最终原生 HANDLE 直接移交一个 Python 文件对象，交给真实 ParquetFile；schema、footer、批次及前后 fstat 都基于该对象。固定 `memory_map=False, buffer_size=0, pre_buffer=False`，递增 row group 读取，批次限定 `columns=['time'], use_threads=False, use_pandas_metadata=False`，没有按路径重新打开数据。
2. **解码前排除歧义。** 同时检查唯一顶层标量 time、唯一平面物理叶子、支持的物理及逻辑类型和非重复结构；拒绝 INT96、嵌套、重名、time.extra 冲突及外部列块路径。未将 columns 的前缀选择行为当成安全保证。
3. **保留精度与原序。** null 和时间域检查先于范围选择，使用底层整数保留纳秒，不经 Python datetime 截断。范围外非法值也拒绝整个输入。重复数、严格下降、H1非整点、半开范围、前20条正向间隔、完整选择的最大正间隔以及空选择均符合裁定，跨批次和 row group 不重置。
4. **FRED 日期兼容。** 已核对生产适配及落盘的 naive datetime64[ns]。脚本接受严格午夜的 naive ns 或 date32/date64；date64 检查整日性，输出日期标签。说明准确披露写 Parquet 时可能已转成 date32，不能恢复已丢失的子日信息。MT5 D1 非午夜 UTC 开盘标签可接受，不推导收盘时刻。
5. **Windows 权限缺口已修复。** 先限制直接本地固定卷映射，再从持有的父目录句柄逐组件执行 NtCreateFile(FILE_OPEN)，包含 no-follow 和目录/文件类型要求，不含 FILE_OPEN_FOR_BACKUP_INTENT。检查成功句柄的类型与属性，祖先不共享 DELETE、最终文件只共享 READ；所有权移交和拒绝路径清理一致，祖先在 finally 逆序释放。作者祖先目录 DACL 失败测试、修复后的原生调用断言及窄探针支持修复结论。
6. **输出和用途边界。** 声明、范围及路径在内容读取前验证，CLI仅提供七个必需选项并清洗参数及运行错误。无输入路径、价格或原异常回显，固定未评估/未发布状态。无业务 loader、研究、fit、收益、ledger、MT5、网络、DB 或结果写入调用；旧 synthetic 预筛门未改。说明准确披露 footer/tail 可能读到价格页乃至整个小文件，没有承诺零价格字节I/O或把统计范围当读取隔离。

## 测试与证据归属

作者最终专项 **120 passed / 12.28s / exit 0，无 skip**。命令为：

```text
python -I -B -m pytest -q --tb=short -p no:cacheprovider --basetemp <临时合成测试目录> tests/test_h_time_audit.py
```

审核者实读完整专项日志、前后文件摘要、提交核验、有效行为 RED、祖先权限 RED、修复后的26项聚焦 GREEN 和原生目录探针。探针记录 reparse 未跟随、rename 被拒、DACL 返回 `0xc0000022`；这些是作者运行证据。最终受测前后 tree 等于提交 tree，三文件提交 blob 的 SHA256 与作者受测文件一致。审核 checkout 因 Git 换行为 CRLF，与作者 LF 原始字节不同；已独立核实只存在换行映射，没有内容漂移。

审核者没有重跑120项套件或追加探针；代码核对未留下需要新执行的疑点。审核者只执行只读 Git 和摘要检查，没有把作者的权限、链接或冷 CLI 测试称为独立复现。

## 未验证与保留条件

真实快照的来源、许可、非锁箱资格、内容身份，以及 MT5 server-time→UTC 转换真实性未核实。该工具不判断价格正确性、会话完整性、实际缺bar、研究有效性或 Demo 资格。

支持域限 Windows 直接本地固定卷及固定目录树；POSIX、UNC、SUBST、网络/可移动卷不在本版范围。未验证对抗受信 OS、卷映射管理员或恶意目录所有者的保障，不承诺不可变快照身份、零价格字节I/O或超大文件资源隔离。前后fstat只检查可检测变化。

审核期间未修改 checkout、HEAD 或 index，未派子代理、填写批准字段、安装依赖或访问真实数据、配置、账户及运行状态。技术通过后仍须最终集成回归与对应 main CI。
