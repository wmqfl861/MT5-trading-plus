# H 真实输入装配与主段预筛聚焦评审（2026-11-06）

主会话转录独立聚焦评审者直接返回的结论。评审者未参与实现与范围裁定；固定只读 checkout `demo-h-real-review-20261106` @ `6976f4506656b314bd3601d6c398aaa2e5a476be`（父 `d29fff7`），全程 HEAD/index/文件未改。

**裁定：Spec PASS / Quality PASS。** 3 项轻微非阻塞发现；无 NEEDS_FIXES。本 PASS 解除该次真实运行产物的 provisional 状态供 A1 使用；不批准 R2 开轮、不填 approved_by、不授权其他真实读取。

## 范围前置条件逐条核查（全部落实）

- **白名单双绑定**：REAL_WHITELIST 与计划 7 文件路径+SHA256 逐字一致；哈希不匹配/不可读/路径逃逸/键集不符均 fail-closed；CLI 无覆盖旗标。
- **资格顺序**：7 文件哈希 → manifest 逐年 used 门 → 价格解码，测试用非法 parquet 字节证明门先于解码。
- **互串拒绝**：synthetic/real 输入双向 PermissionError；非法资格值拒绝。
- **synthetic 逐字节不变**：新代码为带缺省关键字参数，缺省路径错误消息与旧硬编码逐字节相同；冻结 CLI 零 diff（`--mode real` 仍被拒）；旧套件 61 passed。
- **运行边界**：无 MT5/网络/研究库写入；独占输出；provenance 记录 7 工件哈希；真实运行 stderr 空、退出 0。
- **2019 处置**：included=true/inconsistent/月度回收/锚点 consistent；2019-10 空洞恰为 [2019-10-01T00:00, 2019-11-01T00:00)，bars 由 `_cut_holes` 自动切除。
- **违例月仅冻结规则自检**：2020-10 是 run_h_readonly_precheck 内冻结 `_month_sunday_hours_ok` 输出（模块本次未改），526 bar 由冻结代码路径丢弃。
- **缺口预注册**：GAP_RULES 写入 provenance 且 canonical-JSON SHA256 入 hashes.rules（评审者重算一致）；规则单测覆盖周末排除/假日洞/连洞/31天空周/收盘后小时。

## 装配器正确性

- 窗口过滤先于计算；USTEC 窗 [2006-11-22, 2021-05-17)=主段−40天，warmup 26 根。
- 缺口闭式对账：EURUSD 显式1+推导28→并后28；首洞为窗口头部不产生切段；28洞−1头洞+1个2020-10 removed-batch边=28切点=**29 spans** 与 result 一致。USTEC 132洞中3个边界洞不产生主段边→129边=**130 spans** ✓。个案抽查（2020-11-30T05:00 稀疏尾洞等）全部吻合预注册规则。

## 功效计算真实性（42.4% vs 草案粗估58%）

评审者用冻结模块窄人工重算，**全部复现**：

- implied SR 位级复现 `(0.00031−8.9912e-05)/0.00182161×√12=0.41853376714956897`；power 0.4245≈0.42448509；MDS 0.7845835719342883 位级相同。
- σ=0.00182 来自冻结 `_window_sigma`（零改动）：ddof=1 无条件窗口波动、纽约 close 小时窗、双 close 界定主段、cumulative cut 不跨缺口；3679 窗 vs 88,260/24≈3677.5 自洽。
- 覆盖年数 min(14.098,13.136)=13.136（USTEC 绑定）。USTEC 保守扣除确系冻结 runtime_coverage 行为。EURUSD 14.098=主段14.379−约102.5天对账吻合。
- 成本倍数 3.448 全冻结件组成：spread=1.5×p90(NY h10)=7.93e-05；slippage=0.1×p90=1.06e-05；commission 0（实测）；overnight 0；合计 8.99e-05；参考价 1.21498。
- **58% 差异定量归因**：σ=0.0014 且 T=13.136→57.8%（≈粗估）；真实 σ=0.00182 且 T=14.379→45.1%。差异由真实定盘窗σ、保守年数、主段隔离共同构成，**非装配错误**。
- **engineering_waiting 判定正确**：成本门通过→非 fail；single 0.424<0.5→single_power_below_minimum；cumulative_n=None→cumulative_n_unknown。

## 产物与证据核对

result.json SHA256 与文档一致；stdout 与文件逐字节相同；hashes.code 14 文件 blob 级对应提交；RED 13 failed→GREEN 83 passed（新13+旧61+hygiene9）。USTEC 派生洞含 2007-01-02（Ford 国葬休市）等真实日历事实，佐证真实管线。

## 逐项发现

1. **轻微（文档，已修）**：h-real-precheck-2026-11-06.md 成本单位标注差10倍——实际 8.99e-05=0.899bp（点差分量 0.793bp）；result.json 字段本就精确。已在评审后一行更正。
2. **观察**：year_handling 收录窗外 2023/2024 salvage 年标 included=true 而 holes 空——标签略误导，无计算影响，登记透明。
3. **观察**：FRED manifest 无逐年声明，USTEC used 门为"无可验证项"空转——范围审核者实读过该 manifest 未要求逐年验证；A1 应知悉 USTEC 资格是"无声明可查"而非"已验证"。

## 拒绝评判

- σ 相对原始 parquet 字节的真值（需读研究库本体，超授权）；本评审以实现正确性、内部自洽与下游位级复现背书。
- MDS 展示口径适当性（冻结既有行为）；result 同时给出 50% 门直接 power 值，判定不受影响。

依据：[实施计划](../superpowers/plans/2026-11-06-h-real-precheck.md)、[范围裁定](2026-11-05-d61-v2a-scope-review.md 同批模式)。评审者实读完整 diff、冻结模块（h_main_scope/candidates/power/costs/merge）、result.json 全量并重算关键数值。
