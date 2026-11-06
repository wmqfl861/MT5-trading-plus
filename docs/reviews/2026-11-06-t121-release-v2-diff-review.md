# t121-release-v2 前置实施聚焦评审（2026-11-06）

主会话转录独立聚焦评审者结论。固定只读 checkout `demo-p-release-review-20261106` @ `12029a3dcde7a05e599af70a122dd9a52b0413b2`（基点 `983107f`），全程 clean。

**Spec PASS / Quality PASS。** 通过范围仅覆盖本 diff 与 prereg v2 修订文档；不授权 release 登记、不授权运行 T121、不构成 v2 生效批准（登记审核另步）。

## 核心确认

1. **裁定 2a/2b/2d+发现2 四条件全部落实**：allowed_basis 仅增一词；水位常量六个经 epoch 换算与裁定核验 msc 值逐一相符（无舍入）；键集闭合=模块级断言+池校验使 KeyError 不可达；外包络==逐品种 min/max 测试锁死；借用/超限 1ms 拒测试齐。
2. **两处字面偏差均正当且实现正确**：①measured 双分支——分支1与基点逐字符相同，两分支接受域不相交（水位 lo>CUTOFF≥分支1 end），v1 合法 manifest 恒走分支1行为逐字不变；整体替换会拒 2014→2015 观测窗违反 v1 零变化。②grids 替换语义——合并会 144 行违背方案 II；v1 manifest 无 grids 键走缺省冻结路径行为不变。
3. **L641-642 修复正确**：实测 AdmissionRefused（非 TypeError）事实校正成立；缺陷本质（claim 后崩溃）为真；measured-only 聚合+逐品种≥1measured 前置；混合 kind 全链 96 行+8 汇总与既有测试 `.244/(32/14)` 数字锁死。
4. **v1 零变化测试为真机制测试**（真实 _validate_manifest，仅 patch _implementation）；v1 接受集增量恰为裁定授权三项。
5. **prereg v2 修订与裁定 2c 逐点一致**；§4 与基点逐字节相同；冻结数字抽验（0.02R/4521/96行/截止）全部原样；blob SHA 32aaa92c… 复算相符（基点 a5f1fcba… 按裁定被取代）。

## 移交登记审核的两轻微项

1. **grids 键集仅 ⊆ 池**（production_adapter.py:444）：grids 键集⊇池的 manifest 可通过并产出多余 waiting_data 行，弱化 96/8、48/4 机械保证——无计数/算术风险（多余品种无输入、步长仍锁、报表可审计），建议登记审核收紧为 grids 存在时 set(grids)==set(pool)。
2. **内核 overshoot payload 编码**（feasibility.py:311-313）：内核仍要求 payload basis=="stop_level"&&version==2 才做越幅算术；情景组件的 payload 须以该编码 reconciliation（§1 明言不冒充 stop_level 模型）——fail-closed 无错误算术路径，登记审核须显式裁定 payload 口径并在 §5 补披露。

**观察（无需行动）**：接受集扩张全部属裁定授权本体（任意子池顺序无关/水位精确窗/可选 grids）；规范 v1 行为可证不变。

## 证据归属

评审者直接核查：代码逐行比对、分支接受域分析、RULES/§4 字节同一性、水位常量换算、blob SHA 重算、.244 算术交叉引用、测试机制真实性。作者证据（RED 7+2→GREEN 261）未重跑（无疑点触发）。Declined-to-judge：被绑实物内容（研究库仓，登记时现场重验）、锚/账户 JSON SHA/装配步事项、v2 生效与登记（另步）。
