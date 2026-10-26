# 台账链头锚定（T37/M6/D27；R6/C5 链式锚）

由 `scripts/ledger_anchor.py` 生成；管理员每日提交本文件（计划任务）。
链头 = 各链式表按 rowid 最后一行的 `row_hash`；行数为该表当前行数。
每条锚带 prev_anchor（上一条锚的 anchor_hash，首条接全零）与
anchor_hash = sha256(内容 + prev_anchor)——锚文件自成链，改任何历史
锚都会破坏其后所有锚。与本文件记录不符的台账即发生过改写/删行（即便
整链被重算）；比对用 `scripts/ledger_anchor.py --verify`。

| 锚定 UTC | 台账 | 表 | 行数 | 链头 row_hash | prev_anchor | anchor_hash |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-10-25T17:23:56.020981+00:00 | data/research/ledger.sqlite | runs | 186 | 162199b5ee7abfcbd8d89f075d2f041e2700f874caf648efa342a6552a18cec0 | 0000000000000000000000000000000000000000000000000000000000000000 | 07f87f526cf0c131056bb5c7a6912731b1385f90ed1eaa2900d9e0ce83729b89 |
| 2026-10-25T17:23:56.020981+00:00 | data/research/ledger.sqlite | gates | 40 | 1ecb76981af61e3392cc90426a1441ac3381a5dc86369bc555d16cfe8a46bcca | 07f87f526cf0c131056bb5c7a6912731b1385f90ed1eaa2900d9e0ce83729b89 | c2056dd6ad78240ed0b26f4f8b24384cd593685673b6fbaebb74341cd7da3da3 |
| 2026-10-25T17:23:56.020981+00:00 | data/research/ledger.sqlite | lockbox_uses | 0 | （空表） | c2056dd6ad78240ed0b26f4f8b24384cd593685673b6fbaebb74341cd7da3da3 | df32ebb212292777bc8e6332e5a2ab354a723d1f8b041c91cb9d1b02fc8e2330 |
