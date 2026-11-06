# T121 observation_kind 语义扩展与预注册 v2 聚焦评审（2026-11-06）

主会话转录独立聚焦评审者结论。评审者未参与实现；固定只读 checkout `demo-p-t121v2-review-20261106` @ `25de02b68b192c096c03ac37bc2a194a516a8a71`（基点 `934ea20`），全程 clean。

**Spec PASS / Quality PASS。** PASS 仅覆盖语义改动与 v2 文档本身；不授权 release 登记、不运行 T121、不批准 v2 生效（v2 生效属登记审核）。

## 核心确认

1. **裁定 B/E 逐条落实且更严**：非 measured 类 observed 必须 None（逐个 is None 检查，比"允许 null"严）；scenario 必填正有限 multiplier（精确 type 拒 bool、isfinite 拒 NaN/Inf）；缺省=measured 使旧 manifest 无静默语义变化（COMPONENT_FIELDS 与 v1 _keys 逐字符相等，程序化比对确认）。
2. **v1 逐字节不变**：measured 半段条件表达式逐字相同（仅求值顺序调换，接受集一致）；PARTS/SYMBOLS/PURPOSE/CUTOFF/PREREG/RULES 常量逐行 identical；六条件/claim/水位/时效/synthetic 零接触；v1 prereg blob SHA 两端一致。
3. **测试为真机制测试**：fixture 只 patch \_implementation，manifest 走真实 \_validate\_manifest 全程；枚举闭合/evidence\_refs 三分支/bool multiplier/非 measured 携带观测区间拒/字段集精确——断言路径唯一；29 新条目程序化清点自洽。
4. **只加严不放宽**：v1-合法 manifest 接受集零变化；新接受仅显式 measured（等价）+三非 measured 类（B/E 授权本体）；v1 拒 v2 仍拒路径全保留；allowed\_basis 白名单未扩一词。短路安全。
5. **内核层天然 fail-closed**：feasibility.py 逐行 cost evidence 仍要求发现段内观测窗口——非 measured 语义在内核层落 waiting\_data，无绕过路径。
6. **v2 文档抽验无漂移**：0.02R 等号/4521 等号/DEFF1.5/96 行/截止 2016-05-01 不含/网格七参数/成本公式组件——与 v1 逐项相符；六组件表与 B/E 逐格对应。

## 归属登记审核的显式裁定事项

- allowed\_basis 未扩：按现状 spread/slippage（2026 观测区间>CUTOFF）与 overshoot（scenario basis 不在白名单）**三者无法按表现状登记**，仅 commission/swap/contract 可登记——方向安全（代码比文档严），编码须登记审核裁定+新受审 diff。
- 账户条款 JSON 转录工件：登记 manifest 固定 SHA 时绑定，执行前必须存在。
- v2 §3（USDJPY/D60 入池声明）：超出 B/E 命名范围的附加惰性声明（不改冻结规则/品种池），登记审核应显式接受或删除。
- 内核层（feasibility.py 观测窗口）应纳入登记/执行审核视野（v2 §5 未点名，Minor 披露完整性）。
- 措辞碰撞（§1"保守上界情景"构造性 vs §2.1 禁跨期方向宣称）登记审核顺手统一。

## 发现清单

无缺陷项。Minor×2（§3 范围偏差归属、内核层披露缺口）、Cosmetic×1（两测试裸 raises 无 match，拒绝路径唯一不影响）。证据归属：代码/测试机制/diff hunk/常量逐字符比对=评审者直接核查；RED→GREEN 244 passed=作者证据未重跑（无疑点触发）。

依据：[T121 范围裁定](../../../.superpowers/sdd/2026-11-06-p-t121-real/scope-review.md)、[六条件终审](2026-11-05-t121-production-adapter-review.md)。评审期间未修改任何文件/HEAD/index，未派代理。
