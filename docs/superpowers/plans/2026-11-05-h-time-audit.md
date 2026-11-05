# H 单文件时间诊断 Implementation Plan

> **For agentic workers:** Execute the one task with subagent-driven-development in its assigned worktree. Controller assigns independent code review. No nested agents.

**Goal:** 提供实际读取单个Parquet时间列的诊断函数与CLI，验证时间标签、顺序和观察间隔，为后续有界真实输入检查提供可用工具。

**Architecture:** 一个薄脚本，只用标准库与已锁定PyArrow26。路径及声明先验证，以同一内容句柄核验schema和逐row-group读取time；精确整数时间统计后输出JSON。现有业务读者、synthetic预筛和研究路径均不修改。

**Tech Stack:** Python3.12，PyArrow26.0.0，pytest。使用既有venv，不加依赖。

**Spec:** [独立范围裁定](../../reviews/2026-11-05-h-time-scope-review.md)，其中条件为本任务完整约束；初始请求中的歧义由该裁定取代。

## Global Constraints

- 只以临时人工Parquet验收，不读取真实快照、data/config/DB/账户/MT5或运行状态。声明参数不构成来源或使用批准。
- 只解码time；承认footer和尾部预读可能包含价格字节。不主动读全文件/hash，不消费价格/统计/pandas metadata，不写结果或输入。
- 新增脚本、专项测试与简短说明三文件；不改已有预筛、冻结研究、依赖或包初始化。不造权限、签名或通用路径平台。
- source/时间解释/路径错误在输入内容读取前拒绝；异常输出固定码，不泄露私有路径、价格或异常原文。
- 作者精确[H]提交，不push、不派代理、不自批。主会话独立审核后整合并统一全量。

## Review Focus

- 读取后FRED物理schema与生产naive ns午夜兼容，日期标签不偷换收盘时间。
- time唯一顶层标量+唯一物理叶子，拒INT96/外部列块；不凭columns前缀假设投影安全。
- 全列校验且范围只筛统计；跨批次row group、纳秒差异和非相邻重复不被排序/精度截断掩盖。
- Windows路径的链接/设备/ADS/祖先风险在内容读取前处理，能力不足拒绝；fstat不是身份或快照证明。
- 真实冷CLI正负链，error/argparse全路径清洗，价格扰动不影响诊断。

### Task 1: 显式单文件time投影及CLI

**Files:** Create `scripts/h_time_audit.py`, `tests/test_h_time_audit.py`, `docs/research/h-time-audit-2026-11-05.md`。

**Interfaces:**

```python
audit_time_file(path, *, source, symbol, timeframe,
                timestamp_semantics, start, end) -> dict
main(argv=None) -> int
```

API严格接收字符串绑定和本地绝对path。CLI提供对应八个必需长选项，不提供默认数据根、output、allow/approved或发现目录选项。API拒绝抛带稳定code的局部异常（不含原始路径），CLI输出单个JSON错误并非零退出；参数错误同样清洗。成功JSON含scope裁定的固定状态、派生series及时间统计，不含输入路径。

- [ ] 先写实际人工Parquet及行为测试，保存有效RED。schema与投影的负例必须走真实文件；路径/API声明负例用观测守卫证明输入未打开。
- [ ] 固定三source/品种/周期/语义配对及边界，按裁定表。UTC边界只接受明确Z/+00:00的ISO时间，不自动猜时区，最多9位小数，规范为精确纳秒；FRED边界仅日期。start<end且位于允许域。输出UTC ISO时间完整保留纳秒；FRED输出YYYY-MM-DD。
- [ ] 实现局部安全打开。先拒词法URL/UNC/设备命名空间/ADS/驱动器相对/glob/点或点点组件/保留设备名/尾随点空格，再逐级不跟随地检查祖先和目标。Windows可用标准库ctypes调用本地CreateFileW，以OPEN_REPARSE_POINT读取属性，拒reparse；保持祖先目录句柄固定到内容读取结束，使用适当共享标志防替换，最终普通文件只读句柄交给PyArrow。不得普通open后靠fstat声称从未跟随。POSIX可用dir_fd逐级O_NOFOLLOW且固定目录句柄；平台无能力或对象有歧义一律path_refused。不要为了跨平台抽出通用模块。若目标平台无法小范围落实安全打开，立即报告具体失败点和最小收窄选择，不接受全正链均跳过或恒拒绝作为完成。固定本地目录树是边界，不声称防受信OS/目录所有者恶意变更。
- [ ] 对一个已打开内容对象构造ParquetFile，memory_map=False,buffer_size=0,pre_buffer=False。schema检查唯一顶层time及唯一平面physical leaf；类型限定DATE或原生INT64 TIMESTAMP，不兼容INT96。投影叶子每个row group的file_path不得指向外部。应用不访问其余列statistics或完整metadata/pandas/schema输出。
- [ ] 按row group编号递增iter_batches，columns=['time'],use_threads=False,use_pandas_metadata=False。内部批次常量便于测试跨界，不暴露改变读取范围的CLI选项。无pandas/dataset/SQL/业务loader。把合法timestamp按单位转精确整数纳秒，date按精确日；用Arrow安全cast或底层整数保持精度，不能经Python datetime先丢ns。
- [ ] FRED读回date32/date64或naive timestamp[ns]严格午夜；date64校验原整数为整日；拒其他timestamp单位/时区/非午夜。HistData/MT5 timestamp s/ms/us/ns且timezone None或精确UTC；naive仅表示调用者已声明UTC标签；D1允许非午夜。全列中的null、越界无法表示或非法标签均拒绝，包括选择范围外；不得先filter再验证隐藏坏行。
- [ ] 所选行按物理序连续统计count、唯一、N-unique重复、相邻严格下降、H1非整点、min/max。时间集合用精确整数。正向相邻间隔详情前20条，含起止和精确差值（UTC使用duration_ns，date使用duration_days），positive_interval_count和details_truncated，最大正间隔扫描全部所选；无正向间隔max为None。D1非整点项为None或明确not_applicable。空选择status=no_observations，零计数、空/None范围与间隔。不得推导实际missing/coverage/year或完整日历。
- [ ] 输出kind/research_status/execution_status/snapshot_identity固定，statistics_scope=selected_rows_in_physical_order；time_domain和input_time_type规范类型名；read_scope明确全列/footer/tail预读与调用者声明未认证。一个句柄前后fstat检查可检测变化，变化丢弃输出且不重试；稳定不称不可变或已核验身份。
- [ ] 真实正例：PyArrow26创建time+人工价格Parquet，至少2个row group，小批次边界有下降、非相邻重复和ns差异，H1非整点、end等号、范围外行、空选择；真实冷子进程CLI断言精确结果。FRED生产dtype一致读回、date32/date64可接受域、非午夜拒绝；MT5非午夜D1正例。
- [ ] 覆盖缺time/重名/嵌套/坏schema/INT96/null/范围外坏标签、损坏文件、可检测变化、权限、路径祖先和最终reparse及所有词法拒绝。观测单内容句柄与固定Parquet参数，禁止其他数据文件、全表、hash和业务loader；仅人工价格值改变时诊断相等，运行前后人工文件bytes不变（测试外部校验允许hash）。路径负例无实际链接权限时明确报告平台限制，不将本平台能测试的项目全部skip。
- [ ] 冷导入真实调用证明无研究/标签/fit/收益/ledger/执行/MT5/network/DB调用、无结果写入。允许解释器及PyArrow加载自身代码/动态库。stdout/stderr检查不含输入路径、人工价格和原异常，未知CLI参数也不泄露。
- [ ] 简短说明接口、时间域、schema兼容、全列与尾部读取/路径能力/真实使用前需具体快照绑定的限制。运行专项测试全部，必要的直接兼容测试仅限具体依赖；不跑全仓/T78/T121旧套件。保存命令、exit、RED/GREEN、受测前后tree及三文件SHA，精确[H]提交，clean，直接返回结果不写额外报告Markdown。

## Plan self-check

独立裁定的七类条件覆盖时间、投影、读取范围、路径、统计及输出。文件所有权与P/运维无交集；真实路径许可仍未确定，不影响人工工程验收。安全打开的具体平台能力若不够需如实报告，不把未来许可问题扩成新框架。本计划不证明数据存在或资格，也不开放真实预筛。
